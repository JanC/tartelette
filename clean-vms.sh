#!/usr/bin/env bash
# Delete dead ekiden job-VM clones from the local Tart store. Each job clones a
# VM named `runner_<RUNNER_NAME>_<random>` and deletes it after the job; loops
# that are Ctrl-C'd or crash leave these behind (see `tart list`).
#
# This is the Tart-side cleanup. It does NOT deregister runners on GitHub —
# use ./clean-runners.sh for that.
#
# Usage: ./clean-vms.sh [-y|--yes] [PREFIX]
#   PREFIX defaults to "runner_" (the prefix ekiden gives every job clone).
#   -y/--yes skips the confirmation prompt.
#
# Requires: tart, jq. Picks up TART_HOME from .env if present.

set -euo pipefail

ROOT="$(cd "$(dirname "$0")" && pwd)"
[ -f "$ROOT/.env" ] && { set -a; . "$ROOT/.env"; set +a; }

ASSUME_YES=0
PREFIX="runner_"
for arg in "$@"; do
  case "$arg" in
    -y|--yes) ASSUME_YES=1 ;;
    -*) echo "unknown flag: $arg" >&2; exit 1 ;;
    *) PREFIX="$arg" ;;
  esac
done

# Never touch a VM that's currently running (an in-flight job).
targets=()
while IFS= read -r name; do
  targets+=("$name")
done < <(tart list --format json \
  | jq -r --arg p "$PREFIX" '.[] | select(.Source=="local" and (.Name | startswith($p)) and .Running==false) | .Name')

if [ ${#targets[@]} -eq 0 ]; then
  echo "no stopped local VMs matching prefix '$PREFIX'"
  exit 0
fi

printf '%s\n' "${targets[@]}"
if [ "$ASSUME_YES" -ne 1 ]; then
  read -r -p "delete the above ${#targets[@]} VM(s)? [y/N] " ans
  [ "$ans" = "y" ] || [ "$ans" = "Y" ] || { echo "aborted"; exit 0; }
fi

for name in "${targets[@]}"; do
  tart delete "$name" && echo "deleted $name"
done
