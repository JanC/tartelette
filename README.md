# tart-ci

Ephemeral macOS GitHub Actions runners on Apple Silicon, built on
[ekiden](https://github.com/mirego/ekiden) — but with ekiden's Gitea registry
swapped for a plain `registry:2`.

Each CI job runs in a fresh [Tart](https://tart.run) VM cloned from a custom
macOS + Xcode image (built with Packer, runner binary baked in). The VM
registers as an **ephemeral** runner, runs exactly one job, then is destroyed.

## How this relates to ekiden

ekiden is vendored **unmodified** as a git submodule at `vendor/ekiden`, pinned
at commit [`198d918`](https://github.com/mirego/ekiden/commit/198d9180f4a2c9d1ef8db16c24acae9f56850a80).
Everything else here is a thin overlay:

| Path | Purpose |
|------|---------|
| `registry/docker-compose.yml` | local `registry:2` — replaces ekiden's `registry/` (Gitea) |
| `guest/atlas-app.pkr.hcl` | our Packer template — replaces ekiden's `guest/*.pkr.hcl` |
| `.env.example` | config for ekiden's host loop, using **ekiden's own variable names** |

The **host run-loop is ekiden's** (`vendor/ekiden/host/launch.sh`) — we don't
fork it. It reads its config from a `.env` in the working directory and needs no
arguments. The only reason we don't use ekiden's `guest/` templates is that they
build a base OS from an IPSW (user `runner`, plus Homebrew/asdf/Android), whereas
we base directly on the prebuilt cirruslabs Xcode image (user `admin`).

### The one gotcha: insecure HTTP pull

ekiden's `launch.sh` pulls with `tart pull` and **no `--insecure` flag**, and Tart
treats anything that isn't `--insecure` as HTTPS (localhost is *not* special-cased).
A plain-HTTP `localhost:5000` registry therefore can't be pulled by the loop as
written — and we never edit the submodule.

`launch.sh` only pulls **when the image isn't already cached locally**. So we
**pre-pull once with `--insecure`** (step 5 below); the loop then finds it cached
and skips its own pull entirely. Re-run the pre-pull after rebuilding the image.

## Prerequisites

```sh
brew install cirruslabs/cli/tart        # Tart
brew install packer jq                   # Packer + jq (uuidgen ships with macOS)
brew install hudochenkov/sshpass/sshpass # launch.sh shells into the VM via sshpass (not in core Homebrew)
```

Plus Docker or [OrbStack](https://orbstack.dev) for the registry.

```sh
git clone --recurse-submodules <this-repo>
# already cloned without --recurse-submodules?
git submodule update --init
```

## From zero to a green CI run

### 0. (Optional) Keep Tart state inside this repo

Tart defaults to `~/.tart` for its image cache and VMs. To keep everything
inside this checkout (handy if your home volume is small or you want one-shot
cleanup), point `TART_HOME` at a folder here and export it in every shell that
runs `tart` / `packer` / `launch.sh`:

```sh
export TART_HOME="$PWD/.tart"
mkdir -p "$TART_HOME"
```

Add it to your shell rc or prepend it to the commands below — otherwise Tart
silently falls back to `~/.tart` and you'll wonder where the disk went.

### 1. Start the local registry

```sh
cd registry
docker compose up -d
```

Verify (empty catalog is expected before the first push):

```sh
curl http://localhost:5000/v2/_catalog
# {"repositories":[]}
```

### 2. Tell Tart the local registry is insecure

There is no config file or env var — Tart takes `--insecure` per command. The
commands below already include it. Nothing to do here beyond knowing that.

### 3. Build the guest image

```sh
cd guest
packer init .
packer build .                          # defaults to xcode_image_tag=26.3
# pin a different Xcode/macOS:  packer build -var xcode_image_tag=26.4.1 .
```

This pulls `ghcr.io/cirruslabs/macos-tahoe-xcode:26.3` over HTTPS (no auth needed),
boots it, downloads the latest GitHub Actions runner into `~/actions-runner`, and
leaves a local VM named `atlas-app-ci`.

### 4. Push it to the local registry

```sh
tart push --insecure atlas-app-ci localhost:5000/atlas-app-ci:latest
```

Verify:

```sh
curl http://localhost:5000/v2/_catalog
# {"repositories":["atlas-app-ci"]}
```

### 5. Pre-pull the image (the insecure-pull workaround)

```sh
tart pull --insecure localhost:5000/atlas-app-ci:latest
tart list   # should show localhost:5000/atlas-app-ci:latest
```

The host loop sees it cached and won't try its own (HTTPS) pull. **Re-run this
after every `packer build` + `tart push`.**

### 6. Configure the host loop

```sh
cp .env.example .env
# edit .env: set GITHUB_API_TOKEN to a fine-grained PAT with
# "Administration: Read & write" on JanC/tuist-app
```

### 7. Add the workflow to JanC/tuist-app

Commit `.github/workflows/ci.yml` (see [Example workflow](#example-workflow) below).

### 8. Start the runner loop

From the **project root** (so `launch.sh` finds `.env` and writes `runner.log` here):

```sh
bash vendor/ekiden/host/launch.sh
```

It will: mint a registration token → clone+boot a VM → register an ephemeral
runner → wait for a job. Leave it running; it loops one VM per job. `Ctrl-C`
deletes the current VM and exits.

### 9. Trigger a run

Push to `JanC/tuist-app` (or use **Run workflow** if you add `workflow_dispatch`).

### 10. Watch

- Host: `tail -f runner.log`, and `tart list` to see the VM appear/disappear.
- GitHub: repo → **Settings → Actions → Runners** (the ephemeral runner shows up
  for the job's duration) and the **Actions** tab for job logs.

## Example workflow

Add to `JanC/tuist-app/.github/workflows/ci.yml`. **Swap `App` for your actual
Tuist scheme name.**

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

**VM never gets an IP** — `tart ip <name>` hangs forever. The VM failed to boot or
networking didn't come up. Run once with graphics to watch:
`tart run atlas-app-ci`. Confirm the image actually booted during `packer build`.

**SSH never reachable** — loop stuck at "Waiting for SSH". Almost always a
user/password mismatch: cirruslabs images use `admin/admin`; ensure
`VM_USERNAME=admin` / `VM_PASSWORD=admin` in `.env`. Also confirm `sshpass` is
installed (`which sshpass`) — without it the loop silently never connects.

**Registration token failure** — loop logs an empty/garbage token. The PAT lacks
`Administration: Read & write` on the repo, is expired, or
`GITHUB_REGISTRATION_ENDPOINT` points at the wrong repo. Test by hand:
```sh
curl -s -XPOST -H "Authorization: bearer $GITHUB_API_TOKEN" \
  https://api.github.com/repos/JanC/tuist-app/actions/runners/registration-token
```
You should get a JSON `token`, not a `message`/404.

**Registry push refused / `http: server gave HTTP response to HTTPS client`** —
you forgot `--insecure` on `tart push`/`tart pull`. Both must have it for
`localhost:5000`.

**Host loop tries to pull and fails over HTTPS** — the image wasn't pre-cached.
Re-run step 5; confirm `tart list` shows `localhost:5000/atlas-app-ci:latest`
before starting `launch.sh`. (The loop greps `tart list` for the exact path.)

**Runner registers but never picks up the job** — label mismatch. The job's
`runs-on` labels must all be present in `RUNNER_LABELS`. We register
`self-hosted,macos,arm64,tart`; if you change `runs-on`, keep them in sync.

**`packer build` can't find the plugin** — run `packer init .` in `guest/` first.

**Image tag doesn't exist** — `26.3` is valid as of writing; newest is `26.5`.
List tags:
```sh
TOKEN=$(curl -s "https://ghcr.io/token?scope=repository:cirruslabs/macos-tahoe-xcode:pull" | jq -r .token)
curl -s -H "Authorization: Bearer $TOKEN" \
  https://ghcr.io/v2/cirruslabs/macos-tahoe-xcode/tags/list | jq -r '.tags[]' | sort -V
```

## Not in scope (yet)

Monitoring/Grafana, mounted SPM/Tuist/DerivedData caches, running on a remote Mac
mini, multiple concurrent runners, GitHub App auth, registry TLS.
