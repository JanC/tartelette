#!/usr/bin/env bash
# Deregister GitHub Actions runners whose name starts with the BASE_NAME from
# .env (RUNNER_NAME). Useful after killing run-parallel.sh leaves orphans.
#
# Usage: ./clean-runners.sh [--offline-only]
# Requires: jq, plus either
#   GITHUB_AUTH_METHOD=token: GITHUB_API_TOKEN + GITHUB_REGISTRATION_ENDPOINT
#   GITHUB_AUTH_METHOD=app:   GITHUB_APP_ID + GITHUB_APP_PRIVATE_KEY_PATH
#                             + GITHUB_OWNER (+ GITHUB_REPO for repo scope)

set -euo pipefail

ROOT="$(cd "$(dirname "$0")" && pwd)"
SRC_ENV="$ROOT/.env"
[ -f "$SRC_ENV" ] || { echo "missing $SRC_ENV" >&2; exit 1; }

set -a; . "$SRC_ENV"; set +a

: "${RUNNER_NAME:?RUNNER_NAME unset (used as the name prefix to match)}"

if [ "${GITHUB_AUTH_METHOD:-token}" = "app" ]; then
  : "${GITHUB_OWNER:?GITHUB_OWNER unset}"
  if [ -n "${GITHUB_REPO:-}" ]; then
    RUNNERS_URL="https://api.github.com/repos/${GITHUB_OWNER}/${GITHUB_REPO}/actions/runners"
  else
    RUNNERS_URL="https://api.github.com/orgs/${GITHUB_OWNER}/actions/runners"
  fi
  TOKEN=$(GH_TOKEN_KIND=installation "$ROOT/vendor/ekiden/host/gh-runner-token.sh")
else
  : "${GITHUB_API_TOKEN:?GITHUB_API_TOKEN unset}"
  : "${GITHUB_REGISTRATION_ENDPOINT:?GITHUB_REGISTRATION_ENDPOINT unset}"
  # https://api.github.com/repos/<owner>/<repo>/actions/runners/registration-token
  # -> https://api.github.com/repos/<owner>/<repo>/actions/runners
  RUNNERS_URL="${GITHUB_REGISTRATION_ENDPOINT%/registration-token}"
  TOKEN="$GITHUB_API_TOKEN"
fi

OFFLINE_ONLY=0
[ "${1:-}" = "--offline-only" ] && OFFLINE_ONLY=1

list_json=$(curl -fsS -H "Authorization: bearer $TOKEN" "$RUNNERS_URL")

filter='.runners[] | select(.name | startswith($p))'
[ $OFFLINE_ONLY -eq 1 ] && filter="$filter | select(.status==\"offline\")"

targets=()
while IFS= read -r line; do
  targets+=("$line")
done < <(echo "$list_json" | jq -r --arg p "$RUNNER_NAME" "$filter | \"\\(.id) \\(.name) \\(.status)\"")

if [ ${#targets[@]} -eq 0 ]; then
  echo "no runners matching prefix '$RUNNER_NAME'$([ $OFFLINE_ONLY -eq 1 ] && echo ' (offline)')"
  exit 0
fi

printf '%s\n' "${targets[@]}"
read -r -p "delete the above ${#targets[@]} runner(s)? [y/N] " ans
[ "$ans" = "y" ] || [ "$ans" = "Y" ] || { echo "aborted"; exit 0; }

for line in "${targets[@]}"; do
  id="${line%% *}"
  curl -fsS -X DELETE -H "Authorization: bearer $TOKEN" \
    "$RUNNERS_URL/$id" && echo "deleted $line"
done
