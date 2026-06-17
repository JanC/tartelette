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
        with urllib.request.urlopen(req) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        body = e.read().decode(errors="replace")
        try:
            msg = json.loads(body).get("message", body)
        except Exception:
            msg = body
        raise RuntimeError(f"GitHub API {method} {url} -> HTTP {e.code}: {msg}") from e


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
