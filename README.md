# tart-ci

Ephemeral macOS GitHub Actions runners on Apple Silicon.

Each CI job runs in a fresh [Tart](https://tart.run) VM cloned from a custom
macOS + Xcode image. The VM registers as an **ephemeral** runner, runs exactly
one job, then is destroyed.

## Quick start

```sh
brew install mise hudochenkov/sshpass/sshpass
git clone <this-repo> && cd tart-ci
mise trust && mise install

mise run vm:build                     # build the guest image (Xcode 26.3)
cp .env.example .env && $EDITOR .env  # fill auth + image name
mise run runners:start                # start the runner loop
```

See [Setup](docs/setup.md) for `.env` details and auth options,
[Building](docs/building.md) for image variants and multi-host distribution,
[Running](docs/running.md) for parallel runners, drain, and the LaunchDaemon.

## How it works

Each loop iteration:

1. Check drain sentinel — exit cleanly if `mise run runners:drain` was called
2. Mint a registration token (GitHub App or PAT)
3. `tart clone <image> runner_<name>_<id>`
4. `tart set` — apply `VM_RAM` / `VM_CPU` from `.env`
5. `tart run --no-graphics` in the background
6. Poll `tart ip` until a valid IP appears; clear stale SSH known-host entry
7. Poll SSH until the guest is responsive
8. SSH: `config.sh --ephemeral --name … --labels …`
9. SSH: `run.sh` — blocks until the job finishes
10. `tart stop` + `tart delete`; loop back to 1

`runner.log` records every step in logfmt (`ts=`, `level=`, `runner=`,
`run_id=`, `msg=`). Ctrl-C once waits for the current job and de-registers;
Ctrl-C twice within 3 s force-kills immediately.

## Tasks

| Task | Does |
|------|------|
| `mise run vm:build [--xcode <tag>]` | build the guest image with Packer |
| `mise run vm:copy <dest> <vm>` | rsync a built image to another host or path |
| `mise run runners:start` | start a single host loop |
| `mise run runners:parallel [N]` | run N loops in parallel (default 2) |
| `mise run runners:drain` | signal loops to stop after current job |
| `mise run runners:install-daemon [N]` | install a LaunchDaemon that runs `runners:parallel` at boot |
| `mise run runners:uninstall-daemon` | stop and remove the LaunchDaemon |
| `mise run runners:clean [--offline-only]` | deregister orphan runners on GitHub |
| `mise run vm:clean [-y]` | delete dead job-VM clones from the Tart store |
