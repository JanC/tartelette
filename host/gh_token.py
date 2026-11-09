#!/usr/bin/env python3
"""GitHub runner token fetcher. Replaces vendor/ekiden/host/gh-runner-token.sh.

CLI usage:
    python host/gh_token.py [--kind registration|installation]
    Reads config from .env in the current directory. Prints token to stdout.
"""
import base64
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path


class GitHubAPIError(RuntimeError):
    """A GitHub API response error with its HTTP status preserved."""

    def __init__(self, method: str, url: str, status: int, message: str):
        self.status = status
        super().__init__(f"GitHub API {method} {url} -> HTTP {status}: {message}")


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _derive_scope(config: dict) -> str:
    owner = config["GITHUB_OWNER"]
    repo = config.get("GITHUB_REPO", "")
    return f"repos/{owner}/{repo}" if repo else f"orgs/{owner}"


def _derive_runners_url(config: dict) -> str:
    return f"https://api.github.com/{_derive_scope(config)}/actions/runners"


def _build_jwt(app_id: str, private_key_path: str) -> str:
    now = int(time.time())
    header = _b64url(json.dumps({"alg": "RS256", "typ": "JWT"}).encode())
    payload = _b64url(
        json.dumps({"iat": now - 60, "exp": now + 540, "iss": app_id}).encode()
    )
    unsigned = f"{header}.{payload}"
    result = subprocess.run(
        ["openssl", "dgst", "-sha256", "-sign", private_key_path],
        input=unsigned.encode(),
        capture_output=True,
        check=True,
    )
    signature = _b64url(result.stdout)
    return f"{unsigned}.{signature}"


def _gh_api(method: str, url: str, token: str) -> dict:
    req = urllib.request.Request(
        url,
        method=method,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            body = resp.read()
            return json.loads(body) if body else {}
    except urllib.error.HTTPError as e:
        body = e.read().decode(errors="replace")
        try:
            msg = json.loads(body).get("message", body)
        except Exception:
            msg = body
        raise GitHubAPIError(method, url, e.code, msg) from e


def _fetch_via_app(config: dict, kind: str) -> str:
    scope = _derive_scope(config)
    jwt = _build_jwt(config["GITHUB_APP_ID"], config["GITHUB_APP_PRIVATE_KEY_PATH"])

    install = _gh_api("GET", f"https://api.github.com/{scope}/installation", jwt)
    install_id = install["id"]

    access = _gh_api(
        "POST",
        f"https://api.github.com/app/installations/{install_id}/access_tokens",
        jwt,
    )
    install_token = access["token"]

    if kind == "installation":
        return install_token

    reg = _gh_api(
        "POST",
        f"https://api.github.com/{scope}/actions/runners/registration-token",
        install_token,
    )
    return reg["token"]


def _fetch_via_pat(config: dict, kind: str) -> str:
    if kind == "installation":
        return config["GITHUB_API_TOKEN"]
    scope = _derive_scope(config)
    reg = _gh_api(
        "POST",
        f"https://api.github.com/{scope}/actions/runners/registration-token",
        config["GITHUB_API_TOKEN"],
    )
    return reg["token"]


def fetch_token(config: dict, kind: str = "registration") -> str:
    """Return a GitHub token. kind='registration' for runner enrolment,
    kind='installation' for API calls (list/delete runners)."""
    if kind not in ("registration", "installation"):
        raise ValueError(f"unknown kind: {kind!r} (expected registration or installation)")
    method = config.get("GITHUB_AUTH_METHOD", "token")
    if method == "app":
        return _fetch_via_app(config, kind)
    elif method == "token":
        return _fetch_via_pat(config, kind)
    else:
        raise ValueError(f"unknown GITHUB_AUTH_METHOD: {method!r} (expected app or token)")


def list_runner_names(config: dict) -> set[str]:
    """Return every registered runner name in the configured GitHub scope."""
    token = fetch_token(config, kind="installation")
    runners_url = _derive_runners_url(config)
    names: set[str] = set()
    page = 1

    # GitHub returns at most 100 runners per page.  Include every page so a
    # busy host does not accidentally reuse a name hidden on a later page.
    while True:
        listing = _gh_api("GET", f"{runners_url}?per_page=100&page={page}", token)
        runners = listing.get("runners", [])
        names.update(runner["name"] for runner in runners if runner.get("name"))
        if len(runners) < 100:
            return names
        page += 1


def delete_runner(config: dict, name: str) -> bool:
    """Best-effort deregister the GitHub Actions runner with the given name.
    Returns True if a runner was found and deleted, False if none matched.
    Ephemeral runners self-remove after a completed job; this cleans up the
    stale registration left behind by an interrupted or failed run."""
    token = fetch_token(config, kind="installation")
    runners_url = _derive_runners_url(config)
    listing = _gh_api("GET", runners_url, token)
    for runner in listing.get("runners", []):
        if runner.get("name") == name:
            _gh_api("DELETE", f"{runners_url}/{runner['id']}", token)
            return True
    return False


def find_workflow_run_for_runner(config: dict, runner_name: str) -> dict | None:
    """Return the active workflow run assigned to a repo-level runner.

    GitHub's runner protocol only prints a job display name.  The Actions API
    exposes the runner name on each job, which lets the host resolve that job
    back to its workflow-run URL.  Organization-level runners are deliberately
    skipped because the REST API has no equivalent organization-wide job list.
    """
    repo = config.get("GITHUB_REPO", "")
    if not repo:
        return None

    owner = config["GITHUB_OWNER"]
    token = fetch_token(config, kind="installation")
    base_url = f"https://api.github.com/repos/{owner}/{repo}/actions/runs"
    runs = _gh_api("GET", f"{base_url}?status=in_progress&per_page=100", token)
    for run in runs.get("workflow_runs", []):
        run_id = run.get("id")
        if not run_id:
            continue
        jobs = _gh_api(
            "GET", f"{base_url}/{run_id}/jobs?filter=latest&per_page=100", token
        )
        if any(job.get("runner_name") == runner_name for job in jobs.get("jobs", [])):
            return run
    return None


if __name__ == "__main__":
    import argparse
    from pathlib import Path

    parser = argparse.ArgumentParser(description="Fetch a GitHub runner token")
    parser.add_argument(
        "--kind",
        choices=["registration", "installation"],
        default="registration",
    )
    parser.add_argument("--env", default=".env", help="path to .env file")
    args = parser.parse_args()

    env_path = Path(args.env)
    if not env_path.exists():
        print(f"error: {env_path} not found", file=sys.stderr)
        sys.exit(1)

    # Minimal env loader (full version lives in runner.py)
    config: dict = {}
    for line in env_path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        key, _, value = line.partition("=")
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ('"', "'"):
            value = value[1:-1]
        config[key.strip()] = value

    try:
        print(fetch_token(config, kind=args.kind))
    except Exception as e:
        print(f"error: {e}", file=sys.stderr)
        sys.exit(1)
