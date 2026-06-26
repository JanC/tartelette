# Building the guest image

## Build

```sh
mise run vm:build                   # Xcode 26.3 (default)
mise run vm:build --xcode 26.4.1    # pin a different version
```

Pulls `ghcr.io/cirruslabs/macos-tahoe-xcode:<tag>` (no auth), bakes the CI
toolchain on top, and leaves a local VM named `ios-app-ci-<tag>`. The Xcode
version is in the VM name so you can build a new version alongside the old one
and flip by updating `REGISTRY_IMAGE_NAME`. Verify with `tart list`.

See [IMAGE_CONTENTS.md](../guest/IMAGE_CONTENTS.md) for what's baked in.

## Single-machine: no registry needed

With `REGISTRY_URL=` empty in `.env`, the runner clones directly from the
Packer-built VM in the local Tart store — no push, no pull.

## Ship to another host without a registry

```sh
mise run vm:copy user@mac-mini ios-app-ci-26.3
# custom remote store:
mise run vm:copy user@mac-mini ios-app-ci-26.3 --dest-store /Volumes/nvme/tart
# local path / mounted share:
mise run vm:copy /Volumes/nvme/tart ios-app-ci-26.3
```

Uses Homebrew rsync (>= 3.1.0) — `brew install rsync` on the build Mac (and on
the destination for ssh mode, via `--rsync-path`). SMB mounts land non-sparse;
use ssh mode for a compact copy.

On the destination: `tart list` should show the VM. Set
`REGISTRY_IMAGE_NAME=ios-app-ci-26.3` and `REGISTRY_URL=` in `.env`, then
start the loop. Re-run after every rebuild.

## Ship via a registry (multi-host)

For distributing to a fleet of runner hosts.

### Start the registry

Run on the host that serves images:

```sh
cd registry && docker compose up -d
curl http://localhost:5001/v2/_catalog    # {"repositories":[]}
```

Port 5001 — macOS AirPlay Receiver squats on :5000.

### Push

```sh
tart push --insecure ios-app-ci-26.3 localhost:5001/ios-app-ci:26.3
```

Re-push after every rebuild.

### Pre-pull on each runner host

`tart clone` only pulls when the image isn't cached. Pre-pull once with
`--insecure` (Tart doesn't special-case localhost for HTTPS):

```sh
tart pull --insecure localhost:5001/ios-app-ci:26.3
tart list    # shows localhost:5001/ios-app-ci:26.3
```

Then configure `.env`:

```
REGISTRY_URL=localhost:5001
REGISTRY_IMAGE_NAME=ios-app-ci:26.3
```

Re-run the pre-pull after every rebuild + push.

## Keep Tart state on a specific disk

```sh
# in .env:
TART_HOME=/Volumes/nvme/tart
```

Tart defaults to `~/.tart`. Set `TART_HOME` to a faster or larger disk and the
runner picks it up automatically.
