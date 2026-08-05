# Setup

## Prerequisites

```sh
brew install mise hudochenkov/sshpass/sshpass
```

`tart`, `packer`, `jq`, and `python` are pinned in `mise.toml` and installed by
`mise install`. Only `sshpass` needs Homebrew (no mise backend).

```sh
git clone <this-repo> && cd tartelette
mise trust && mise install
```

## Configure `.env`

```sh
cp .env.example .env
$EDITOR .env
```

### Auth

Pick one method:

**GitHub App (recommended)**

Create a GitHub App with:
- Org-level runner: *Organization permissions → Self-hosted runners: Read & write*
- Repo-level runner: *Repository permissions → Administration: Read & write*
  and *Actions: Read*, then set `GITHUB_REPO=<repo>`. The Actions permission
  lets the host log a direct link when the runner accepts a job.

```
GITHUB_AUTH_METHOD=app
GITHUB_APP_ID=<numeric app id>
GITHUB_APP_PRIVATE_KEY_PATH=/absolute/path/to/app-private-key.pem
GITHUB_OWNER=<org>
GITHUB_REPO=          # leave empty for org-level
```

With a repo-level runner, the configured PAT also needs permission to read
Actions data if you want job URLs in the host log. GitHub does not expose an
organization-wide job listing, so an org-level runner only logs the job name.

**PAT fallback**

```
GITHUB_AUTH_METHOD=token
GITHUB_API_TOKEN=<fine-grained PAT with Administration: Read & write>
GITHUB_OWNER=<org>
GITHUB_REPO=          # leave empty for org-level
```

### Image

```
REGISTRY_URL=                          # empty = local-only (single machine)
REGISTRY_IMAGE_NAME=ios-app-ci-26.3  # name from `tart list`
```

For a registry: `REGISTRY_URL=localhost:5001`, `REGISTRY_IMAGE_NAME=ios-app-ci:26.3`.
See [Building → Multi-host](building.md#ship-via-a-registry-multi-host).

### Runner

```
RUNNER_URL=https://github.com/<org>
RUNNER_NAME=               # empty = a sanitized version of this Mac's Computer Name
RUNNER_LABELS=                          # empty = self-hosted,macos,arm64,tart,xcode-<version>
```

Leave `RUNNER_NAME` empty to use a sanitized version of this Mac's Computer Name
automatically. For example, `Jan’s MacBook Pro` becomes `jans-macbook-pro-vm`.
Set a unique name to override it. This avoids parallel runners across hosts colliding
on GitHub.

Leave `RUNNER_LABELS` empty to register with `self-hosted,macos,arm64,tart`
plus an `xcode-<major.minor>` label detected in the VM. For example, Xcode 16.4
adds `xcode-16.4`. Set `RUNNER_LABELS` to register exactly those labels; no
labels are added or changed.

### VM sizing

```
VM_RAM=8192   # MB
VM_CPU=4
```

### Optional

```
TART_HOME=/Volumes/nvme/tart   # custom Tart store (default ~/.tart)
```

Set this if you moved the Tart store to an external disk. Must be an absolute
path — the runner process may execute from a different working directory.
