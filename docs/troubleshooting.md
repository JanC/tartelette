# Troubleshooting

## VM never gets an IP

`tart ip <name>` hangs. The VM failed to boot or networking didn't come up. Run
once with graphics to watch:

```sh
tart run ios-app-ci-26.3
```

Confirm the image booted cleanly during the Packer build.

## SSH never reachable

Loop stuck at `waiting for SSH`. Almost always a credential mismatch —
cirruslabs images use `admin`/`admin`:

```
VM_USERNAME=admin
VM_PASSWORD=admin
```

Also confirm `sshpass` is installed: `which sshpass`.

## Invalid configuration provided for token

Registration token is empty or wrong.

**`GITHUB_AUTH_METHOD=token`** — PAT lacks *Administration: Read & write* or
is expired. Test:
```sh
curl -s -XPOST -H "Authorization: bearer $GITHUB_API_TOKEN" \
  https://api.github.com/orgs/<org>/actions/runners/registration-token
```
Should return a JSON `token`, not a `message`/404.

**`GITHUB_AUTH_METHOD=app`** — check `GITHUB_APP_ID`, confirm
`GITHUB_APP_PRIVATE_KEY_PATH` is an absolute path to a readable `.pem`, and
that the App is installed on `GITHUB_OWNER`.

## Runner registers but never picks up the job

Label mismatch. The job's `runs-on` labels must all be present in
`RUNNER_LABELS`. Default: `self-hosted,macos,arm64,tart`; the runner also adds
an `xcode-<major.minor>` label from the Xcode installed in the VM.

## Two parallel jobs but only one runs

Both runners registered with the same name — they're evicting each other on
GitHub. Use `mise run runners:parallel` — it appends `-1`/`-2` per thread
automatically.

## `packer build`: disk size smaller than current

`disk_size_gb` in `ios-app.pkr.hcl` is below the base image's actual size.
Tart can grow disks, not shrink them. Bump `disk_size_gb` to at least the
current base size.

## `packer build` can't find the plugin

`mise run vm:build` runs `packer init` automatically. If invoking packer
directly, run `packer init .` in `guest/` first.

## Image tag doesn't exist

List available Xcode tags:
```sh
TOKEN=$(curl -s "https://ghcr.io/token?scope=repository:cirruslabs/macos-tahoe-xcode:pull" | jq -r .token)
curl -s -H "Authorization: Bearer $TOKEN" \
  https://ghcr.io/v2/cirruslabs/macos-tahoe-xcode/tags/list | jq -r '.tags[]' | sort -V
```

## Multi-host: `http: server gave HTTP response to HTTPS client`

Missing `--insecure` on `tart push` or `tart pull`. Both need it for a
plain-HTTP registry.

## Multi-host: orchestrator fails to pull

Image wasn't pre-cached. Re-run:
```sh
tart pull --insecure localhost:5001/ios-app-ci:26.3
tart list    # confirm it appears before starting the loop
```
