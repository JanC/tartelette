# Runner rewrite design

**Date:** 2026-06-17  
**Status:** approved  
**Replaces:** `vendor/ekiden/host/launch.sh` + `vendor/ekiden/host/gh-runner-token.sh`

## Overview

Replace the ekiden git submodule with an in-tree Python script (`host/runner.py`) that owns the full runner lifecycle: token fetch, VM boot, runner registration, job execution, VM teardown, and graceful shutdown. Parallelism (N concurrent runners) is handled inside the script via threads, eliminating the bash process-group orchestration in `runners:parallel`.

## Files

```
tartelette/
  host/
    runner.py          # main script — replaces launch.sh
    gh_token.py        # token module — replaces gh-runner-token.sh
  mise/tasks/runners/
    start              # exec: python host/runner.py
    parallel           # exec: python host/runner.py --parallel $N
    drain              # touch .runners/drain
    install-daemon     # unchanged
    uninstall-daemon   # unchanged
    clean              # updated: also removes .runners/drain
  .env.example         # updated: remove ekiden notes, drop removed vars
  vendor/ekiden/       # submodule removed
```

No third-party dependencies. Stdlib only: `subprocess`, `threading`, `signal`, `logging`, `http.client`, `pathlib`, `uuid`.

## Runner loop (single instance)

One iteration per job:

1. Check drain sentinel (`$ROOT/.runners/drain`) → exit cleanly if present
2. Fetch GitHub registration token via `gh_token.py`
3. `tart clone <image> <instance>` where `instance = f"{RUNNER_NAME}_{run_id}"`
4. `tart set --memory --cpu`
5. `tart run --no-graphics` (background subprocess)
6. Poll `tart ip` until valid IP returned
7. Poll SSH until responsive
8. SSH: `config.sh --ephemeral --name --labels ...`
9. SSH: `run.sh` (blocks until job completes)
10. `tart stop <instance>`
11. `tart delete <instance>`
12. Repeat from 1

`run_id = uuid.uuid4().hex[:8]` — unique per VM lifecycle.

## Signal handling

- **First SIGINT/SIGTERM:** set `shutdown` event, log immediately:
  ```
  level=warn runner=mini1-1 msg="shutdown requested, waiting for current job to finish"
  ```
  If in step 9: finish the job, then stop+delete and exit. If between cycles: stop+delete current VM if any, exit.

- **Second SIGINT (within 3s of first):** set `force_shutdown` event, log:
  ```
  level=warn runner=mini1-1 msg="force shutdown, killing VM"
  ```
  `tart delete` best-effort, exit immediately without de-registering.

## Drain

Sentinel file: `$ROOT/.runners/drain`

- `mise run runners:drain` → `touch .runners/drain` + log message
- Checked at step 1 only — never interrupts a live job
- Each parallel thread checks independently; threads may drain at different times
- Removed by `runners:clean` only — not auto-removed on start (drain persists across restarts until explicitly cleared)

## Parallelism

`--parallel N` spins N threads, each running the loop independently:

```python
threads = [threading.Thread(target=run_loop, args=(f"{BASE_NAME}-{i}",)) for i in range(1, N+1)]
```

Shared state:
- `shutdown: threading.Event` — set on first SIGINT/SIGTERM
- `force_shutdown: threading.Event` — set on second SIGINT

No per-runner workdirs. Runner names derived in-process (`{BASE_NAME}-1`, `{BASE_NAME}-2`).

Apple Virtualization framework caps macOS guests at 2 concurrent VMs — warning printed if `N > 2`.

## Logging

**Format:** logfmt, stdout + single log file simultaneously via two `logging.Handler`s.

**Fixed fields on every line:**
```
ts=2026-06-17T10:23:01Z level=info runner=mini1-1 run_id=a3f9b2c1 msg="..."
```

- `runner` — differentiates parallel instances in LogQL
- `run_id` — ties all lines for one VM lifecycle together
- `level` — `info` / `warn` / `error`

**Log file path:** `$ROOT/runner.log` (single file, all runners).

**Key events:**

| Event | level |
|---|---|
| loop start, drain exit, shutdown signal | info / warn |
| token fetch start / done | info |
| VM clone / boot / IP acquired / SSH ready | info |
| runner config / start / done | info |
| VM stop / delete | info |
| subprocess failure (includes stderr) | error |
| shutdown requested / force shutdown | warn |

**promtail update:** replace current regex pipeline stage with `| logfmt`; extract `runner` as a Loki label.

## Config

`.env` parsed in Python: line-by-line, skip blanks and `#` comments, split on first `=`, strip whitespace, support quoted values. Loaded once at startup (not reloaded per cycle).

**Env vars:**

| Var | Auth method | Purpose |
|---|---|---|
| `GITHUB_AUTH_METHOD` | both | `app` or `token` |
| `GITHUB_APP_ID` | app | GitHub App ID |
| `GITHUB_APP_PRIVATE_KEY_PATH` | app | Path to .pem |
| `GITHUB_API_TOKEN` | token | PAT |
| `GITHUB_OWNER` | both | Org name; derives registration endpoint |
| `GITHUB_REPO` | both | Optional; repo-level runner if set |
| `RUNNER_URL` | both | GitHub URL the runner attaches to |
| `RUNNER_NAME` | both | Base name; parallel appends `-1`, `-2` |
| `RUNNER_LABELS` | both | Comma-separated labels |
| `REGISTRY_URL` | both | OCI registry; empty = local-only flow |
| `REGISTRY_IMAGE_NAME` | both | Tart image name |
| `VM_USERNAME` | both | SSH username |
| `VM_PASSWORD` | both | SSH password |
| `VM_RAM` | both | MB, default 8192 |
| `VM_CPU` | both | Count, default 4 |
| `TART_HOME` | both | Optional; set as `TART_HOME` env var on every `tart` subprocess call |

**Removed vars:** `SCHEDULE_SHUTDOWN` (replaced by drain), `TRUNCATE_SIZE` (out of scope), `LOGFILE` (hardcoded to `runner.log`), `GITHUB_REGISTRATION_ENDPOINT` (derived from `GITHUB_OWNER`/`GITHUB_REPO`).

The registration endpoint is always derived:
- org-level: `orgs/{OWNER}/actions/runners/registration-token`
- repo-level: `repos/{OWNER}/{REPO}/actions/runners/registration-token`

## mise tasks

**`runners:start`**
```bash
exec python "$ROOT/host/runner.py"
```
Keeps existing preflight: verify image in `tart list` for local-only flow.

**`runners:parallel`**
```bash
exec python "$ROOT/host/runner.py" --parallel "$N"
```
~10 lines. All process management moved to Python.

**`runners:drain`** (new)
```bash
mkdir -p "$ROOT/.runners"
touch "$ROOT/.runners/drain"
echo "drain sentinel written — runners will stop after current job"
```

**`runners:install-daemon` / `runners:uninstall-daemon`** — unchanged.

**`runners:clean`** — updated to also remove `.runners/drain`.

## Submodule removal

`vendor/ekiden` submodule removed. `gh_token.py` is a direct port of `gh-runner-token.sh` logic into a Python module with a single `fetch_registration_token(config) -> str` function.
