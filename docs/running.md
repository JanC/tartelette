# Running the orchestrator

## Single runner

```sh
mise run runners:start
```

Starts one loop: one VM at a time, one job per VM. Logs to `runner.log` and
stdout in logfmt format.

## Parallel runners

```sh
mise run runners:parallel 2    # default; macOS caps macOS VMs at 2
mise run runners:parallel 1    # same as runners:start
```

Runs N threads in-process, each with its own runner name
(`{RUNNER_NAME}-1`, `{RUNNER_NAME}-2`, …). All output goes to a single
`runner.log` — use the `runner=` field to filter:

```
tail -f runner.log | grep 'runner=myhost-1'
# or in LogQL: {job="runner"} | logfmt | runner="myhost-1"
```

Each VM consumes `VM_RAM` + `VM_CPU` from `.env`. N × those must fit the host.

## Shutdown

- **Ctrl-C once** (or `launchctl stop`): graceful — waits for the current job,
  de-registers the runner, deletes the VM, exits.
- **Ctrl-C twice within 3 s**: force — deletes the VM immediately, skips
  de-registration.

## Drain

Stop runners after their current job without interrupting live work:

```sh
mise run runners:drain    # write .runners/drain sentinel
```

Each runner checks for the sentinel at the top of its loop. Once the current
job finishes, it exits cleanly. Remove the sentinel (and deregister orphans) with:

```sh
mise run runners:clean
```

## Run at boot (LaunchDaemon)

```sh
mise run runners:daemon:install 2    # renders plist, loads it (requires sudo)
mise run runners:daemon:status       # pid, last exit code, log tail
mise run runners:daemon:stop         # pause: unload + disable (keeps plist)
mise run runners:daemon:start        # resume: re-enable + (re)start
mise run runners:daemon:uninstall    # stop + remove
```

Installs `/Library/LaunchDaemons/com.atlas.tart-ci.runners.plist` configured
for the current user, repo path, and N runners. `KeepAlive` restarts the
supervisor if it crashes. Output goes to `runner.log` in the repo root.

**Requires auto-login** — macOS guests need an active user session on the host.

```sh
sudo launchctl print system/com.atlas.tart-ci.runners    # check status
```

## Cleaning up orphans

Killing the loop mid-listen leaves runners registered (offline) in GitHub for
~14 days. Clean them by prefix:

```sh
mise run runners:clean                 # all runners matching RUNNER_NAME prefix
mise run runners:clean --offline-only  # only offline ones
```

Crashed loops also leave job-VM clones (`runner_<name>_<id>`) in the Tart
store. Remove them:

```sh
mise run vm:clean        # lists stopped runner_* clones, confirms, deletes
mise run vm:clean -y     # skip prompt
```

## Observability

`runner.log` uses logfmt. Each line has:

| Field | Example | Notes |
|---|---|---|
| `ts` | `2026-06-17T10:23:01Z` | UTC |
| `level` | `info` / `warn` / `error` | |
| `runner` | `myhost-1` | thread identifier |
| `run_id` | `a3f9b2c1` | unique per VM lifecycle |
| `msg` | `"job complete"` | quoted if contains spaces |

For Loki, update the promtail pipeline stage:

```yaml
pipeline_stages:
  - logfmt: {}
  - labels:
      runner:
  - timestamp:
      source: ts
      format: RFC3339
```

Point `__path__` at `runner.log` in the repo root (single file, not a glob).
