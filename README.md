# tart-ci

Ephemeral macOS GitHub Actions runners on Apple Silicon, built on
[ekiden](https://github.com/mirego/ekiden).

Each CI job runs in a fresh [Tart](https://tart.run) VM cloned from a custom
macOS + Xcode image (built with Packer, runner binary baked in). The VM
registers as an **ephemeral** runner, runs exactly one job, then is destroyed.

The default flow is **single-machine**: the same Mac that builds the image
also runs the host loop and clones the VM directly from its local Tart store.
A local OCI registry is only needed when the orchestrator runs on a different
host than the one that built the image — see [Multi-host with a registry](#multi-host-with-a-registry).

## How this relates to ekiden

ekiden is vendored **unmodified** as a git submodule at `vendor/ekiden`, pinned
at commit [`198d918`](https://github.com/mirego/ekiden/commit/198d9180f4a2c9d1ef8db16c24acae9f56850a80).
Everything else here is a thin overlay:

| Path | Purpose |
|------|---------|
| `guest/atlas-app.pkr.hcl` | our Packer template — replaces ekiden's `guest/*.pkr.hcl` |
| `.env.example` | config for ekiden's host loop, using **ekiden's own variable names** |
| `run-parallel.sh` | wrapper to run N concurrent host loops |
| `clean-runners.sh` | deregister orphan runners from GitHub |
| `registry/docker-compose.yml` | optional local `registry:2`, only for the multi-host flow |

The **host run-loop is ekiden's** (`vendor/ekiden/host/launch.sh`) — we don't
fork it. It reads its config from a `.env` in the working directory and needs no
arguments. The only reason we don't use ekiden's `guest/` templates is that they
build a base OS from an IPSW (user `runner`, plus Homebrew/asdf/Android), whereas
we base directly on the prebuilt cirruslabs Xcode image (user `admin`).

## Prerequisites

```sh
brew install cirruslabs/cli/tart        # Tart
brew install packer jq                   # Packer + jq (uuidgen ships with macOS)
brew install hudochenkov/sshpass/sshpass # launch.sh shells into the VM via sshpass (not in core Homebrew)
```

```sh
git clone --recurse-submodules <this-repo>
# already cloned without --recurse-submodules?
git submodule update --init
```

## Single-machine flow

This is what you want when the same Mac builds the image and runs the jobs.
ekiden's loop checks `tart list` for a local VM matching `REGISTRY_PATH` and
only pulls when it isn't there ([launch.sh:165-179](vendor/ekiden/host/launch.sh#L165-L179)).
With `REGISTRY_URL=` empty, `REGISTRY_PATH` collapses to the bare
`REGISTRY_IMAGE_NAME` and the loop clones directly from the Packer-built VM.

### 1. (Optional) Keep Tart state on a specific disk

Tart defaults to `~/.tart`. Point `TART_HOME` somewhere else (e.g. an external
NVMe) if you want and export it in every shell that runs `tart` / `packer` /
`launch.sh`. The host loop also picks it up from `.env`, so the cleanest
setup is to put `TART_HOME=/path/to/store` in `.env` and forget about it.

### 2. Build the guest image

```sh
cd guest
packer init .
packer build .                          # defaults to xcode_image_tag=26.3
# pin a different Xcode/macOS:  packer build -var xcode_image_tag=26.4.1 .
```

This pulls `ghcr.io/cirruslabs/macos-tahoe-xcode:26.3` over HTTPS (no auth needed),
boots it, downloads the latest GitHub Actions runner into `~/actions-runner`, and
leaves a local VM named `atlas-app-ci`.

### 3. Configure the host loop

```sh
cp .env.example .env
```

Edit `.env`:
- `GITHUB_API_TOKEN` — fine-grained PAT with `Administration: Read & write` on the target repo.
- `REGISTRY_URL=` — leave empty for the single-machine flow.
- `REGISTRY_IMAGE_NAME=atlas-app-ci` — bare VM name (no `:latest`; Packer doesn't tag).
- `TART_HOME=` — set if you used step 1.

`.env` is read by ekiden via `xargs`, which treats quote characters as
delimiters even inside comments. **No `'` or `"` anywhere in the file**
(including comments) — otherwise the loader silently fails to set any
variable and the loop dies with `Target image not found`.

### 4. Add the workflow

Commit `.github/workflows/ci.yml` to the target repo. See
[Example workflow](#example-workflow).

### 5. Start the runner loop

From the repo root (so `launch.sh` finds `.env` and writes `runner.log` here):

```sh
bash vendor/ekiden/host/launch.sh
```

It mints a registration token → clones+boots a VM → registers an ephemeral
runner → waits for a job. One VM per job; Ctrl-C deletes the current VM
and exits.

### 6. Trigger and watch

Push to the target repo (or use **Run workflow** if you added `workflow_dispatch`).

- Host: `tail -f runner.log`, `tart list` to see VMs appear/disappear.
- GitHub: repo → **Settings → Actions → Runners** for the ephemeral runner,
  **Actions** tab for job logs.

## Running multiple runners in parallel

`launch.sh` is a single serial loop (one VM at a time). For N concurrent
runners, run N copies. Each needs a unique `RUNNER_NAME`, otherwise
`config.sh --replace` makes them kick each other off in GitHub. Wrapper:

```sh
./run-parallel.sh 3        # spawns runner-1, runner-2, runner-3
# Ctrl-C stops everything and cleans .runners/
```

Each runner gets its own workdir under `.runners/runner-N` with a per-runner
`.env` (the wrapper rewrites `RUNNER_NAME` because ekiden reloads `.env` every
iteration and would otherwise clobber any env-var override). Stdout is
prefixed per runner; per-runner logs land at `.runners/runner-N/runner.log`.

Sizing: each VM consumes `VM_RAM` + `VM_CPU` from `.env`. N × those must fit
the host. Disk is CoW from the base image, so growth is mostly the per-job
delta.

## Cleaning up orphan runners

Killing the loop mid-listen leaves runners registered (offline) in GitHub
until GC (~14 days). Clean them up by prefix:

```sh
./clean-runners.sh                # all matching RUNNER_NAME prefix
./clean-runners.sh --offline-only # only the stale ones
```

## Multi-host with a registry

Use this only when the orchestrator runs somewhere other than the build host
— for example, building the image on a beefy Mac Studio and running jobs on
a fleet of Mac minis. The registry transports the image; ekiden pulls it on
first use and caches locally.

### Why a workaround is needed

ekiden's `launch.sh` pulls with `tart pull` and **no `--insecure` flag**, and
Tart treats anything without `--insecure` as HTTPS (localhost is *not*
special-cased). A plain-HTTP registry therefore can't be pulled by the loop
as written, and we don't fork the submodule.

`launch.sh` only pulls **when the image isn't already cached locally**. So
we **pre-pull once with `--insecure`** on each host; the loop then finds it
cached and skips its own pull entirely. Re-run the pre-pull after every
rebuild + push.

### Steps

1. **Start the registry** (on the host that distributes images):
   ```sh
   cd registry
   docker compose up -d
   curl http://localhost:5001/v2/_catalog        # {"repositories":[]}
   ```
   Port 5001, not 5000: macOS AirPlay Receiver squats on :5000 and intercepts
   pushes with HTTP 403.

2. **Build and push** (on the build host):
   ```sh
   cd guest && packer init . && packer build .
   tart push --insecure atlas-app-ci localhost:5001/atlas-app-ci:latest
   ```

3. **Pre-pull on each orchestrator host**:
   ```sh
   tart pull --insecure localhost:5001/atlas-app-ci:latest
   tart list   # should show localhost:5001/atlas-app-ci:latest
   ```

4. **Configure `.env`** on each orchestrator:
   ```
   REGISTRY_URL=localhost:5001
   REGISTRY_IMAGE_NAME=atlas-app-ci:latest
   ```

5. **Start the loop** (`bash vendor/ekiden/host/launch.sh`). It finds the
   image cached and skips its own pull.

## Example workflow

Add to the target repo's `.github/workflows/ci.yml`. **Swap `App` for your
actual Tuist scheme name.**

```yaml
name: CI

on:
  push:
  workflow_dispatch:

jobs:
  build:
    runs-on: [self-hosted, macos, arm64, tart]
    steps:
      - uses: actions/checkout@v4

      # If the repo pins tooling with mise (tuist, etc.):
      - name: mise install
        run: mise install
        # remove this step if the repo doesn't use mise

      - name: Tuist install
        run: tuist install

      - name: Tuist generate
        run: tuist generate --no-open

      - name: Build
        run: |
          xcodebuild \
            -workspace App.xcworkspace \
            -scheme App \
            -destination 'generic/platform=iOS Simulator' \
            build
```

## Troubleshooting

**`Target image not found` immediately on start** — `.env` failed to load.
Almost always a stray quote or apostrophe in the file (xargs treats them as
delimiters and bails). Strip them all, even from comments.

**VM never gets an IP** — `tart ip <name>` hangs forever. The VM failed to
boot or networking didn't come up. Run once with graphics to watch:
`tart run atlas-app-ci`. Confirm the image actually booted during `packer build`.

**SSH never reachable** — loop stuck at "Waiting for SSH". Almost always a
user/password mismatch: cirruslabs images use `admin/admin`; ensure
`VM_USERNAME=admin` / `VM_PASSWORD=admin` in `.env`. Also confirm `sshpass`
is installed (`which sshpass`).

**`Invalid configuration provided for token`** — the registration token is
empty or wrong. PAT lacks `Administration: Read & write`, is expired, or
`GITHUB_REGISTRATION_ENDPOINT` points at the wrong repo. Test by hand:
```sh
curl -s -XPOST -H "Authorization: bearer $GITHUB_API_TOKEN" \
  https://api.github.com/repos/<owner>/<repo>/actions/runners/registration-token
```
You should get a JSON `token`, not a `message`/404.

**Runner registers but never picks up the job** — label mismatch. The job's
`runs-on` labels must all be present in `RUNNER_LABELS`. We register
`self-hosted,macos,arm64,tart`; if you change `runs-on`, keep them in sync.

**Two parallel jobs but only one runs** — both runners registered with the
same name. `RUNNER_NAME` in `.env` overrides any env-var override from a
wrapper (ekiden reloads `.env` every iteration). Use `run-parallel.sh` —
it materializes a per-runner `.env` to dodge this.

**`packer build`: disk size … smaller than current** — `disk_size_gb` in
`atlas-app.pkr.hcl` is below the base image's actual size. Tart can grow,
not shrink. Bump it to at least the base size.

**`packer build` can't find the plugin** — run `packer init .` in `guest/` first.

**Image tag doesn't exist** — `26.3` is valid as of writing; newest is `26.5`.
List tags:
```sh
TOKEN=$(curl -s "https://ghcr.io/token?scope=repository:cirruslabs/macos-tahoe-xcode:pull" | jq -r .token)
curl -s -H "Authorization: Bearer $TOKEN" \
  https://ghcr.io/v2/cirruslabs/macos-tahoe-xcode/tags/list | jq -r '.tags[]' | sort -V
```

**Multi-host: `http: server gave HTTP response to HTTPS client`** — missing
`--insecure` on `tart push`/`tart pull`. Both must have it for a
plain-HTTP registry.

**Multi-host: orchestrator pulls over HTTPS and fails** — image wasn't
pre-cached on that host. Re-run the pre-pull step; confirm `tart list` shows
`localhost:5001/atlas-app-ci:latest` before starting `launch.sh`.

## Not in scope (yet)

Monitoring/Grafana, mounted SPM/Tuist/DerivedData caches, GitHub App auth,
registry TLS.
