# tart-ci

Ephemeral macOS GitHub Actions runners on Apple Silicon.

Each CI job runs in a fresh [Tart](https://tart.run) VM cloned from a custom
macOS + Xcode image (built with Packer). The VM registers as an **ephemeral**
runner, runs exactly one job, then is destroyed. See
[What's baked into the guest image](guest/IMAGE_CONTENTS.md) for the full
contents (runner binary, Metal toolchain, mise toolchain, Maestro).

The work splits in two: [build the guest image](#building-the-images) (a Packer
step, required either way), then [run the orchestrator](#running-the-orchestrator)
that clones VMs from it per job.

The default flow is **single-machine**: the same Mac that builds the image
also runs the host loop and clones the VM directly from its local Tart store —
no registry, no push, no pull. A local OCI registry is only needed when the
orchestrator runs on a different host than the one that built the image — see
[Multi-host: consume a registry image](#multi-host-consume-a-registry-image).

## Prerequisites

```sh
brew install mise                        # task runner + pins tart/packer/jq/python
brew install hudochenkov/sshpass/sshpass # runner shells into the VM via sshpass (no mise backend)
```

`tart`, `packer`, `jq`, and `python` are pinned in `mise.toml` — `mise install`
(below) provides them, so only `sshpass` needs Homebrew.

```sh
git clone <this-repo>
cd tart-ci
mise trust        # approve this repo's mise.toml + tasks
mise install      # install the pinned tart/packer/jq/python
```

## Tasks

All repo scripting runs through mise (`mise tasks` to list):

| Task | Does |
|------|------|
| `mise run vm:build [--xcode <tag>]` | build the guest image with Packer |
| `mise run vm:copy <dest> <vm> [--dest-store <path>]` | rsync a built image to an ssh host or local path (no registry) |
| `mise run runners:start` | start a single host loop (one VM per job) |
| `mise run runners:parallel [N]` | run N host loops in parallel (default 2) |
| `mise run runners:drain` | signal loops to stop after their current job |
| `mise run runners:install-daemon [N]` | install a LaunchDaemon that runs `runners:parallel` at boot |
| `mise run runners:uninstall-daemon` | stop and remove that LaunchDaemon |
| `mise run runners:clean [--offline-only]` | deregister orphan runners on GitHub |
| `mise run vm:clean [-y] [PREFIX]` | delete dead job-VM clones from the local Tart store |

## Building the images

```sh
mise run vm:build                                   # build guest image (defaults to Xcode 26.3)
mise run vm:build --xcode 26.4.1                    # or pin a different Xcode/macOS base
mise run vm:copy user@mac-mini atlas-app-ci-26.3    # optional: ship to another host (no registry)
```

Required for both flows. Run on whichever Mac will produce the image
(single-machine: this Mac; multi-host: the build host).

### (Optional) Keep Tart state on a specific disk

Tart defaults to `~/.tart`. Point `TART_HOME` somewhere else (e.g. an external
NVMe) if you want and export it in every shell that runs `tart` / `packer` /
`host/runner.py`. The host loop also picks it up from `.env`, so the cleanest
setup is to put `TART_HOME=/path/to/store` in `.env` and forget about it.

### Build the guest image

```sh
mise run vm:build                       # defaults to xcode_image_tag=26.3
mise run vm:build --xcode 26.4.1        # pin a different Xcode/macOS base
```

This pulls `ghcr.io/cirruslabs/macos-tahoe-xcode:26.3` over HTTPS (no auth needed),
boots it, bakes the CI toolchain on top, and leaves a local Tart VM named after
the base tag — `atlas-app-ci-26.3`. The Xcode version lives in the name (there
are no image tags without a registry) so an upgrade can build `atlas-app-ci-26.4`
alongside the old one and you flip over by pointing `REGISTRY_IMAGE_NAME` at it.
Verify with `tart list`. For exactly what's baked, see
[What's baked into the guest image](guest/IMAGE_CONTENTS.md).

### Ship to another host without a registry

When the build Mac and the runner host differ but you don't want to stand up a
registry, copy the VM directory straight across. `tart clone` is store-local
and `tart push`/`pull` need a registry, so a plain `rsync` of the stopped VM is
the simplest transport for a single consumer:

```sh
mise run vm:copy user@mac-mini atlas-app-ci-26.3
# remote uses a custom store?  add --dest-store /Volumes/nvme/tart
# dest is a local path / mounted share (a Tart store root)?
mise run vm:copy /Volumes/nvme/tart atlas-app-ci-26.3
```

The task refuses to copy a running VM and reads the **source** store from local
`TART_HOME` (else `~/.tart`). The dest can be either:

- an **ssh host** — writes to the *remote's* `~/.tart` (resolved on the remote,
  so a different home/user is fine; pass `--dest-store` if it keeps its store
  elsewhere), or
- a **local path** (anything starting with `/` `~` `.`, e.g. a mounted share) —
  treated as the Tart store root; the VM lands in `<path>/vms/`.

Then on the mini: `tart list` shows `atlas-app-ci-26.3`, set
`REGISTRY_IMAGE_NAME=atlas-app-ci-26.3` (and `REGISTRY_URL=` empty) in `.env`,
and start the loop. Re-run after every rebuild.

The task uses Homebrew rsync (`/opt/homebrew/bin/rsync`, >= 3.1.0) and fails if
it's missing or too old — macOS openrsync can't write sparse, so without it the
`disk.img` would land fully expanded. `brew install rsync` on the build Mac (and
in ssh mode on the destination too — the task points the remote at it via
`--rsync-path`). Caveat: an **SMB-mounted** dest still lands non-sparse (~120 GB)
regardless — smbfs can't punch holes; use ssh mode for a compact copy.

### Ship via a registry (multi-host)

When the orchestrator runs somewhere other than the build host — for example,
building on a beefy Mac Studio and running jobs on a fleet of Mac minis — a
registry transports the image; the runner pre-caches it on first use.
The consuming side (pre-pull + config) lives under
[Multi-host: consume a registry image](#multi-host-consume-a-registry-image).

1. **Start the registry** (on the host that distributes images):
   ```sh
   cd registry
   docker compose up -d
   curl http://localhost:5001/v2/_catalog        # {"repositories":[]}
   ```
   Port 5001, not 5000: macOS AirPlay Receiver squats on :5000 and intercepts
   pushes with HTTP 403.

2. **Push the image** (on the build host). With a registry the version belongs
   in the tag, so map the local `atlas-app-ci-26.3` to `atlas-app-ci:26.3`:
   ```sh
   tart push --insecure atlas-app-ci-26.3 localhost:5001/atlas-app-ci:26.3
   ```

Re-push after every rebuild.

## Running the orchestrator

```sh
cp .env.example .env          # fill auth fields + set REGISTRY_IMAGE_NAME=atlas-app-ci-26.3
mise run runners:start        # one ephemeral runner (single-machine default)
mise run runners:parallel 2   # or N runners in parallel
```

### Configure the host loop

```sh
cp .env.example .env
```

Edit `.env`:
- `GITHUB_AUTH_METHOD=app` — recommended. Set `GITHUB_APP_ID`, `GITHUB_APP_PRIVATE_KEY_PATH`, `GITHUB_OWNER`.
- `GITHUB_AUTH_METHOD=token` — PAT fallback. Set `GITHUB_API_TOKEN` with `Administration: Read & write` on the target repo.
- `REGISTRY_URL=` — leave empty for the single-machine flow.
- `REGISTRY_IMAGE_NAME=atlas-app-ci-26.3` — the VM name from `tart list` (matches the Xcode tag you built).
- `TART_HOME=` — set if you used a custom Tart store while building.

### Single-machine flow

What you want when the same Mac builds the image and runs the jobs. With
`REGISTRY_URL=` empty, `REGISTRY_IMAGE_NAME` is used as a bare Tart VM name and
the loop clones directly from the Packer-built VM — no push, no pull, no
registry.

**1. Add the workflow.** Commit a workflow to the target repo whose
`runs-on` carries the labels we register (`self-hosted, macos, arm64, tart` —
see `RUNNER_LABELS` in `.env`). They must all be present or the job never
gets picked up.

**2. Start the runner loop.** From the repo root (so the loop finds `.env` and
writes `runner.log` here):

```sh
mise run runners:start
```

It mints a registration token → clones+boots a VM → registers an ephemeral
runner → waits for a job. One VM per job. `runner.log` records all events in
logfmt (`ts=`, `level=`, `runner=`, `run_id=`, `msg=`).

**3. Trigger and watch.** Push to the target repo (or use **Run workflow** if
you added `workflow_dispatch`).

- Host: `tail -f runner.log`, `tart list` to see VMs appear/disappear.
- GitHub: repo → **Settings → Actions → Runners** for the ephemeral runner,
  **Actions** tab for job logs.

### Running multiple runners in parallel

`runner.py` handles N concurrent loops in-process via threads. Each gets a
unique runner name (`{RUNNER_NAME}-1`, `{RUNNER_NAME}-2`, …) — no risk of
runners kicking each other off GitHub:

```sh
mise run runners:parallel 2   # spawns runner-1, runner-2
# Ctrl-C once: wait for current jobs, de-register, exit
# Ctrl-C twice within 3s: force-kill immediately
```

All runners write to a single `runner.log`; use `runner=` to filter in
LogQL (`{job="runner"} | logfmt | runner="myhost-1"`).

Sizing: each VM consumes `VM_RAM` + `VM_CPU` from `.env`. N × those must fit
the host. Disk is CoW from the base image, so growth is mostly the per-job
delta. Apple's Virtualization framework caps macOS guests at 2 concurrent VMs.

### Draining runners

To stop runners cleanly without interrupting a live job:

```sh
mise run runners:drain        # write sentinel — each runner exits after current job
mise run runners:clean        # remove sentinel + deregister any orphans
```

### Shutdown behaviour

- **Ctrl-C once** (or `SIGTERM` from launchctl): graceful — waits for the
  current job to finish, de-registers, deletes the VM, then exits.
- **Ctrl-C twice within 3 s**: force — kills the VM immediately without
  de-registering.

### Run at boot (LaunchDaemon)

To make a dedicated host start its runner loops automatically, install a
LaunchDaemon instead of leaving `runners:parallel` in a terminal:

```sh
mise run runners:install-daemon 2   # render plist, load it (asks for sudo)
mise run runners:uninstall-daemon   # stop and remove it
```

The task renders `/Library/LaunchDaemons/com.atlas.tart-ci.runners.plist` from
live values (the `mise` binary, this repo as `WorkingDirectory`, the current
user) and drives `mise run runners:parallel N`. `RunAtLoad` starts it at boot
and `KeepAlive` restarts the supervisor if it dies. It runs as the current
user, so enable **auto-login** on the host — macOS guests need an active user
session. Combined output goes to `runner.log` in the repo root.
Inspect with `sudo launchctl print system/com.atlas.tart-ci.runners`.

### Cleaning up orphan runners

Killing the loop mid-listen leaves runners registered (offline) in GitHub
until GC (~14 days). Clean them up by prefix:

```sh
mise run runners:clean                # all matching RUNNER_NAME prefix
mise run runners:clean --offline-only # only the stale ones
```

That's the GitHub side. The same crashes also leave the **job-VM clones**
(`runner_<name>_<random>`) behind in the local Tart store (`tart list`). Clear
those with the Tart-side counterpart — it skips any VM that's still running:

```sh
mise run vm:clean        # lists stopped runner_* clones, confirms, deletes
mise run vm:clean -y     # skip the prompt
```

### Multi-host: consume a registry image

For the orchestrator running on a different host than the build Mac, after the
image has been [shipped via a registry](#ship-via-a-registry-multi-host).

`tart clone <registry>/<image>` only pulls when the image isn't already cached
locally. Pre-pull once with `--insecure` on each orchestrator host (Tart treats
anything without `--insecure` as HTTPS; localhost is not special-cased). The
loop then finds it cached and skips its own pull. Re-run after every rebuild + push.

#### Steps

1. **Pre-pull on each orchestrator host**:
   ```sh
   tart pull --insecure localhost:5001/atlas-app-ci:26.3
   tart list   # should show localhost:5001/atlas-app-ci:26.3
   ```

2. **Configure `.env`** on each orchestrator:
   ```
   REGISTRY_URL=localhost:5001
   REGISTRY_IMAGE_NAME=atlas-app-ci:26.3
   ```

3. **Start the loop** (`mise run runners:start`, or `runners:parallel`). It
   finds the image cached and skips its own pull.

## Troubleshooting

**VM never gets an IP** — `tart ip <name>` hangs forever. The VM failed to
boot or networking didn't come up. Run once with graphics to watch:
`tart run atlas-app-ci-26.3`. Confirm the image actually booted during the build.

**SSH never reachable** — loop stuck at "waiting for SSH". Almost always a
user/password mismatch: cirruslabs images use `admin/admin`; ensure
`VM_USERNAME=admin` / `VM_PASSWORD=admin` in `.env`. Also confirm `sshpass`
is installed (`which sshpass`).

**`Invalid configuration provided for token`** — the registration token is
empty or wrong. For `GITHUB_AUTH_METHOD=token`: PAT lacks `Administration: Read & write`
or is expired. For `GITHUB_AUTH_METHOD=app`: check `GITHUB_APP_ID`,
`GITHUB_APP_PRIVATE_KEY_PATH`, and that the App is installed on `GITHUB_OWNER`.
Test the PAT path by hand:
```sh
curl -s -XPOST -H "Authorization: bearer $GITHUB_API_TOKEN" \
  https://api.github.com/orgs/<owner>/actions/runners/registration-token
```
You should get a JSON `token`, not a `message`/404.

**Runner registers but never picks up the job** — label mismatch. The job's
`runs-on` labels must all be present in `RUNNER_LABELS`. We register
`self-hosted,macos,arm64,tart`; if you change `runs-on`, keep them in sync.

**Two parallel jobs but only one runs** — both runners registered with the
same name. Use `mise run runners:parallel` — it appends `-1`/`-2` per thread
automatically.

**`packer build`: disk size … smaller than current** — `disk_size_gb` in
`atlas-app.pkr.hcl` is below the base image's actual size. Tart can grow,
not shrink. Bump it to at least the base size.

**`packer build` can't find the plugin** — `mise run vm:build` runs `packer init`
first; if you invoke packer by hand, run `packer init .` in `guest/` first.

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
`localhost:5001/atlas-app-ci:26.3` before starting the loop.

## Not in scope (yet)

Monitoring/Grafana, mounted SPM/Tuist/DerivedData caches, registry TLS.
