#!/usr/bin/env bash
# Run N ekiden host loops in parallel. Each gets its own workdir with a
# per-runner .env so RUNNER_NAME stays unique (ekiden reloads .env every
# iteration via `export $(xargs <.env)` and would otherwise clobber any
# env-var override from the wrapper).
#
# Usage: ./run-parallel.sh [N]   (default 2)
# Workdirs: .runners/runner-1, .runners/runner-2, ... (wiped on start and exit)
# Logs:     <workdir>/runner.log (ekiden writes this itself; gone on cleanup)
# Stop:     Ctrl-C, or pkill -f vendor/ekiden/host/launch.sh
#
# Each VM consumes VM_RAM + VM_CPU from .env. N times those must fit the host.

set -euo pipefail

N="${1:-2}"
ROOT="$(cd "$(dirname "$0")" && pwd)"
LAUNCH="$ROOT/vendor/ekiden/host/launch.sh"
SRC_ENV="$ROOT/.env"
RUNNERS_DIR="$ROOT/.runners"

[ -f "$SRC_ENV" ] || { echo "missing $SRC_ENV" >&2; exit 1; }

BASE_NAME="$(grep -E '^RUNNER_NAME=' "$SRC_ENV" | head -1 | cut -d= -f2-)"
BASE_NAME="${BASE_NAME:-runner}"

# Stale workdirs from a previous run (or crash) are misleading; nuke them.
rm -rf "$RUNNERS_DIR"

pids=()
cleanup() {
  echo "[parallel] stopping"
  kill "${pids[@]}" 2>/dev/null || true
  wait 2>/dev/null || true
  rm -rf "$RUNNERS_DIR"
  echo "[parallel] cleaned $RUNNERS_DIR"
}
trap cleanup INT TERM EXIT

for i in $(seq 1 "$N"); do
  WORKDIR="$ROOT/.runners/runner-$i"
  mkdir -p "$WORKDIR"
  # Per-runner .env: strip RUNNER_NAME from the source, then append a unique one.
  grep -v '^RUNNER_NAME=' "$SRC_ENV" > "$WORKDIR/.env"
  echo "RUNNER_NAME=${BASE_NAME}-${i}" >> "$WORKDIR/.env"

  NAME="${BASE_NAME}-${i}"
  # ekiden writes runner.log inside WORKDIR; we just prefix stdout for the console.
  (cd "$WORKDIR" && "$LAUNCH" 2>&1 | sed -u "s/^/[$NAME] /") &
  pids+=($!)
  echo "[parallel] started $NAME pid=$! (log: $WORKDIR/runner.log)"
done

wait
