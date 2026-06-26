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

## Run at login (LaunchAgent)

```sh
mise run runners:daemon:install 2    # renders plist, loads it (no sudo)
mise run runners:daemon:status       # pid, last exit code, log tail
mise run runners:daemon:logs         # follow daemon.log (tail -f)
mise run runners:daemon:stop         # pause: unload + disable (keeps plist)
mise run runners:daemon:start        # resume: re-enable + (re)start
mise run runners:daemon:uninstall    # stop + remove
```

Installs a per-user LaunchAgent at
`~/Library/LaunchAgents/com.atlas.tartelette.runners.plist`, configured for the
repo path and N runners. `KeepAlive` restarts the supervisor if it crashes.
Output goes to `~/Library/Logs/tartelette/daemon.log` — on the boot volume, not
the repo, because launchd opens that path during spawn setup and cannot open a
path on an external volume (it fails with `EX_CONFIG` and writes no log).

A user **LaunchAgent** (not a system LaunchDaemon) is used deliberately: the
host needs an active user session anyway (see below), and a system daemon runs
outside that session — it can't reach the user's environment or external
volumes (e.g. a repo or `TART_HOME` on `/Volumes/...`), failing with
`EX_CONFIG`. `install`/`uninstall` also remove any legacy system daemon from an
earlier version.

### Repo or TART_HOME on an external volume

If the repo or `TART_HOME` lives on an external/secondary volume (`/Volumes/...`),
macOS **TCC** gates background launchd items from that volume. The first time
the agent runs, macOS prompts to let `mise` access the volume — **click Allow**
(or grant it under System Settings → Privacy & Security → Files and Folders /
Removable Volumes). Until then `mise` hangs silently in `getcwd()`/`open()`:
`daemon:status` shows `state = running` with no log progress and no child
process. A Terminal/SSH session is *not* gated this way, so it works there —
which makes the agent-only hang confusing. After granting access, restart with
`mise run runners:daemon:start`.

**Requires auto-login** — macOS guests need an active user session on the host,
and the agent loads at login (not at the pre-login boot phase).

```sh
launchctl print "gui/$(id -u)/com.atlas.tartelette.runners"    # check status
```

### Stopping cleanly

`runners:daemon:uninstall` (and `:stop`) send SIGTERM, which the host loop
handles **gracefully** — it finishes the in-flight job, then tears down the VM
and deregisters from GitHub. But launchd only waits ~20s before escalating to
SIGKILL, so if a CI job is mid-run (usually minutes) the supervisor is
hard-killed first, leaving an orphan `runner_*` VM and a stale GitHub
registration.

To stop a busy host without orphans, **drain first**:

```sh
mise run runners:drain               # finish current job, then exit (no respawn)
# wait for jobs to finish (watch: mise run runners:daemon:logs)
mise run runners:daemon:uninstall    # now safe to remove
```

If you did kill it mid-job, clean up the leftovers with `runners:clean`
(GitHub side) and `vm:clean` (Tart side) — see below.

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
