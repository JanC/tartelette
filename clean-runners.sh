#!/usr/bin/env bash
# Deregister GitHub Actions runners whose name starts with the BASE_NAME from
# .env (RUNNER_NAME). Useful after killing run-parallel.sh leaves orphans.
#
# Usage: ./clean-runners.sh [--offline-only]
# Requires: jq, GITHUB_API_TOKEN and GITHUB_REGISTRATION_ENDPOINT in .env
#           (the endpoint is parsed to derive repos/<owner>/<repo>).

set -euo pipefail

ROOT="$(cd "$(dirname "$0")" && pwd)"
SRC_ENV="$ROOT/.env"
[ -f "$SRC_ENV" ] || { echo "missing $SRC_ENV" >&2; exit 1; }

set -a; . "$SRC_ENV"; set +a

: "${GITHUB_API_TOKEN:?GITHUB_API_TOKEN unset}"
: "${GITHUB_REGISTRATION_ENDPOINT:?GITHUB_REGISTRATION_ENDPOINT unset}"
: "${RUNNER_NAME:?RUNNER_NAME unset (used as the name prefix to match)}"

# https://api.github.com/repos/<owner>/<repo>/actions/runners/registration-token
# -> https://api.github.com/repos/<owner>/<repo>/actions/runners
RUNNERS_URL="${GITHUB_REGISTRATION_ENDPOINT%/registration-token}"

OFFLINE_ONLY=0
[ "${1:-}" = "--offline-only" ] && OFFLINE_ONLY=1

list_json=$(curl -fsS -H "Authorization: bearer $GITHUB_API_TOKEN" "$RUNNERS_URL")

filter='.runners[] | select(.name | startswith($p))'
[ $OFFLINE_ONLY -eq 1 ] && filter="$filter | select(.status==\"offline\")"

mapfile -t targets < <(echo "$list_json" | jq -r --arg p "$RUNNER_NAME" "$filter | \"\\(.id) \\(.name) \\(.status)\"")

if [ ${#targets[@]} -eq 0 ]; then
  echo "no runners matching prefix '$RUNNER_NAME'$([ $OFFLINE_ONLY -eq 1 ] && echo ' (offline)')"
  exit 0
fi

printf '%s\n' "${targets[@]}"
read -r -p "delete the above ${#targets[@]} runner(s)? [y/N] " ans
[ "$ans" = "y" ] || [ "$ans" = "Y" ] || { echo "aborted"; exit 0; }

for line in "${targets[@]}"; do
  id="${line%% *}"
  curl -fsS -X DELETE -H "Authorization: bearer $GITHUB_API_TOKEN" \
    "$RUNNERS_URL/$id" && echo "deleted $line"
done
