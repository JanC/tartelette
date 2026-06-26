# Runner Rewrite Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace `vendor/ekiden/host/launch.sh` + `gh-runner-token.sh` with an in-tree Python script that owns the full runner lifecycle including parallelism, graceful shutdown, and logfmt-structured logging.

**Architecture:** `host/gh_token.py` is a pure token-fetching module (also a CLI); `host/runner.py` is the main loop with `--parallel N` threading, signal handling, and drain sentinel support. mise tasks become thin wrappers. The ekiden submodule is removed last, after all new code is verified.

**Tech Stack:** Python 3.10+ stdlib only (`subprocess`, `threading`, `signal`, `logging`, `urllib.request`, `pathlib`, `uuid`, `dataclasses`). pytest for tests. mise for task running.

---

### Task 1: Test infrastructure

**Files:**
- Create: `tests/__init__.py`
- Create: `tests/test_placeholder.py`
- Modify: `mise.toml`
- Modify: `mise/tasks/test` (new file)

- [ ] **Step 1: Add Python to mise.toml**

```toml
[tools]
"aqua:cirruslabs/tart" = "2.32.1"
packer = "1.15.4"
jq = "1.8.1"
python = "3.10"
```

- [ ] **Step 2: Install python via mise**

Run: `mise install`
Expected: Python 3.10.x installed, `mise exec -- python --version` prints `Python 3.10.x`

- [ ] **Step 3: Install pytest**

Run: `mise exec -- pip install pytest`
Expected: pytest installed

- [ ] **Step 4: Create test directory**

```bash
mkdir -p tests
touch tests/__init__.py
```

- [ ] **Step 5: Write placeholder test**

`tests/test_placeholder.py`:
```python
def test_sanity():
    assert 1 + 1 == 2
```

- [ ] **Step 6: Create mise test task**

`mise/tasks/test`:
```bash
#!/usr/bin/env bash
#MISE description="Run the test suite"
set -euo pipefail
ROOT="${MISE_PROJECT_ROOT:?run via mise}"
cd "$ROOT"
exec python -m pytest tests/ -v "$@"
```

Make it executable: `chmod +x mise/tasks/test`

- [ ] **Step 7: Run tests**

Run: `mise run test`
Expected:
```
tests/test_placeholder.py::test_sanity PASSED
1 passed in 0.XXs
```

- [ ] **Step 8: Commit**

```bash
git add mise.toml mise/tasks/test tests/
git commit -m "chore: add Python 3.10 and pytest test infrastructure"
```

---

### Task 2: `host/gh_token.py` — token module

Ports `vendor/ekiden/host/gh-runner-token.sh` to Python. Handles both `app` and `token` auth, both `registration` and `installation` token kinds.

**Files:**
- Create: `host/__init__.py`
- Create: `host/gh_token.py`
- Create: `tests/test_gh_token.py`

- [ ] **Step 1: Write failing tests**

`tests/test_gh_token.py`:
```python
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from host.gh_token import _b64url, _derive_scope, _derive_runners_url

def test_b64url_no_padding():
    # base64url must have no '=' padding
    result = _b64url(b"hello")
    assert "=" not in result
    assert result == "aGVsbG8"

def test_b64url_url_safe():
    # bytes that produce '+' or '/' in standard base64 must become '-' or '_'
    # b'\xfb\xff' -> standard base64 '+/8=' -> b64url '-_8'
    result = _b64url(b"\xfb\xff")
    assert "+" not in result
    assert "/" not in result

def test_derive_scope_org():
    config = {"GITHUB_OWNER": "myorg", "GITHUB_REPO": ""}
    assert _derive_scope(config) == "orgs/myorg"

def test_derive_scope_repo():
    config = {"GITHUB_OWNER": "myorg", "GITHUB_REPO": "myrepo"}
    assert _derive_scope(config) == "repos/myorg/myrepo"

def test_derive_runners_url_org():
    config = {"GITHUB_OWNER": "myorg", "GITHUB_REPO": ""}
    assert _derive_runners_url(config) == "https://api.github.com/orgs/myorg/actions/runners"

def test_derive_runners_url_repo():
    config = {"GITHUB_OWNER": "myorg", "GITHUB_REPO": "myrepo"}
    assert _derive_runners_url(config) == "https://api.github.com/repos/myorg/myrepo/actions/runners"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `mise run test tests/test_gh_token.py`
Expected: `ImportError` or `ModuleNotFoundError` (module does not exist yet)

- [ ] **Step 3: Implement `host/gh_token.py`**

`host/__init__.py`: empty file.

`host/gh_token.py`:
```python
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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `mise run test tests/test_gh_token.py`
Expected:
```
tests/test_gh_token.py::test_b64url_no_padding PASSED
tests/test_gh_token.py::test_b64url_url_safe PASSED
tests/test_gh_token.py::test_derive_scope_org PASSED
tests/test_gh_token.py::test_derive_scope_repo PASSED
tests/test_gh_token.py::test_derive_runners_url_org PASSED
tests/test_gh_token.py::test_derive_runners_url_repo PASSED
6 passed
```

- [ ] **Step 5: Commit**

```bash
git add host/__init__.py host/gh_token.py tests/test_gh_token.py
git commit -m "feat: gh_token.py — port gh-runner-token.sh to Python"
```

---

### Task 3: `load_env()` and `Config`

Pure env-file parser and typed config dataclass, both in `runner.py`.

**Files:**
- Create: `host/runner.py` (initial skeleton — just env loading + config)
- Create: `tests/test_runner_config.py`

- [ ] **Step 1: Write failing tests**

`tests/test_runner_config.py`:
```python
import sys, os, textwrap
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from pathlib import Path
import pytest
from host.runner import load_env, Config


def write_env(tmp_path: Path, content: str) -> Path:
    p = tmp_path / ".env"
    p.write_text(textwrap.dedent(content))
    return p


def test_load_env_basic(tmp_path):
    p = write_env(tmp_path, """
        FOO=bar
        BAZ=qux
    """)
    assert load_env(p) == {"FOO": "bar", "BAZ": "qux"}


def test_load_env_skips_comments_and_blanks(tmp_path):
    p = write_env(tmp_path, """
        # this is a comment
        FOO=bar

        # another comment
        BAZ=qux
    """)
    assert load_env(p) == {"FOO": "bar", "BAZ": "qux"}


def test_load_env_strips_double_quotes(tmp_path):
    p = write_env(tmp_path, 'KEY="hello world"')
    assert load_env(p) == {"KEY": "hello world"}


def test_load_env_strips_single_quotes(tmp_path):
    p = write_env(tmp_path, "KEY='hello world'")
    assert load_env(p) == {"KEY": "hello world"}


def test_load_env_value_with_equals(tmp_path):
    p = write_env(tmp_path, "KEY=a=b=c")
    assert load_env(p) == {"KEY": "a=b=c"}


def test_load_env_missing_file():
    with pytest.raises(FileNotFoundError):
        load_env(Path("/nonexistent/.env"))


def test_config_defaults():
    env = {
        "GITHUB_AUTH_METHOD": "token",
        "GITHUB_API_TOKEN": "tok",
        "GITHUB_OWNER": "myorg",
        "RUNNER_URL": "https://github.com/myorg",
        "RUNNER_NAME": "runner",
        "RUNNER_LABELS": "self-hosted,macos",
        "REGISTRY_IMAGE_NAME": "atlas-app-ci",
        "VM_USERNAME": "admin",
        "VM_PASSWORD": "admin",
    }
    cfg = Config.from_env(env)
    assert cfg.vm_ram == 8192
    assert cfg.vm_cpu == 4
    assert cfg.registry_url == ""
    assert cfg.tart_home is None


def test_config_missing_required_raises():
    with pytest.raises(ValueError, match="RUNNER_URL"):
        Config.from_env({
            "GITHUB_AUTH_METHOD": "token",
            "GITHUB_API_TOKEN": "tok",
            "GITHUB_OWNER": "myorg",
            # RUNNER_URL missing
            "RUNNER_NAME": "runner",
            "RUNNER_LABELS": "self-hosted",
            "REGISTRY_IMAGE_NAME": "atlas-app-ci",
            "VM_USERNAME": "admin",
            "VM_PASSWORD": "admin",
        })


def test_config_app_auth_missing_key_raises():
    with pytest.raises(ValueError, match="GITHUB_APP_ID"):
        Config.from_env({
            "GITHUB_AUTH_METHOD": "app",
            # GITHUB_APP_ID missing
            "GITHUB_APP_PRIVATE_KEY_PATH": "/key.pem",
            "GITHUB_OWNER": "myorg",
            "RUNNER_URL": "https://github.com/myorg",
            "RUNNER_NAME": "runner",
            "RUNNER_LABELS": "self-hosted",
            "REGISTRY_IMAGE_NAME": "atlas-app-ci",
            "VM_USERNAME": "admin",
            "VM_PASSWORD": "admin",
        })
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `mise run test tests/test_runner_config.py`
Expected: `ModuleNotFoundError: No module named 'host.runner'`

- [ ] **Step 3: Implement `host/runner.py` skeleton**

`host/runner.py`:
```python
#!/usr/bin/env python3
"""Tartelette runner — replaces vendor/ekiden/host/launch.sh."""
from __future__ import annotations

import dataclasses
import os
from pathlib import Path
from typing import Optional


def load_env(path: Path) -> dict:
    """Parse a .env file into a dict. Skips blank lines and # comments.
    Strips surrounding single or double quotes from values."""
    env: dict = {}
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ('"', "'"):
            value = value[1:-1]
        env[key] = value
    return env


@dataclasses.dataclass
class Config:
    # Auth
    github_auth_method: str
    github_app_id: str
    github_app_private_key_path: str
    github_api_token: str
    github_owner: str
    github_repo: str

    # Runner
    runner_url: str
    runner_name: str
    runner_labels: str

    # Image
    registry_url: str
    registry_image_name: str

    # VM
    vm_username: str
    vm_password: str
    vm_ram: int
    vm_cpu: int
    tart_home: Optional[str]

    @classmethod
    def from_env(cls, env: dict) -> "Config":
        def require(key: str) -> str:
            val = env.get(key, "")
            if not val:
                raise ValueError(f"missing required env var: {key}")
            return val

        method = env.get("GITHUB_AUTH_METHOD", "token")

        if method == "app":
            app_id = require("GITHUB_APP_ID")
            app_key = require("GITHUB_APP_PRIVATE_KEY_PATH")
            api_token = env.get("GITHUB_API_TOKEN", "")
        elif method == "token":
            app_id = env.get("GITHUB_APP_ID", "")
            app_key = env.get("GITHUB_APP_PRIVATE_KEY_PATH", "")
            api_token = require("GITHUB_API_TOKEN")
        else:
            raise ValueError(f"GITHUB_AUTH_METHOD must be 'app' or 'token', got {method!r}")

        return cls(
            github_auth_method=method,
            github_app_id=app_id,
            github_app_private_key_path=app_key,
            github_api_token=api_token,
            github_owner=require("GITHUB_OWNER"),
            github_repo=env.get("GITHUB_REPO", ""),
            runner_url=require("RUNNER_URL"),
            runner_name=require("RUNNER_NAME"),
            runner_labels=require("RUNNER_LABELS"),
            registry_url=env.get("REGISTRY_URL", ""),
            registry_image_name=require("REGISTRY_IMAGE_NAME"),
            vm_username=require("VM_USERNAME"),
            vm_password=require("VM_PASSWORD"),
            vm_ram=int(env.get("VM_RAM", "8192")),
            vm_cpu=int(env.get("VM_CPU", "4")),
            tart_home=env.get("TART_HOME") or None,
        )

    def as_gh_token_config(self) -> dict:
        """Return a dict suitable for host.gh_token.fetch_token()."""
        return {
            "GITHUB_AUTH_METHOD": self.github_auth_method,
            "GITHUB_APP_ID": self.github_app_id,
            "GITHUB_APP_PRIVATE_KEY_PATH": self.github_app_private_key_path,
            "GITHUB_API_TOKEN": self.github_api_token,
            "GITHUB_OWNER": self.github_owner,
            "GITHUB_REPO": self.github_repo,
        }

    @property
    def registry_path(self) -> str:
        """Full image path for tart clone."""
        if self.registry_url:
            return f"{self.registry_url}/{self.registry_image_name}"
        return self.registry_image_name

    @property
    def tart_env(self) -> dict:
        """os.environ copy with TART_HOME injected if set."""
        env = dict(os.environ)
        if self.tart_home:
            env["TART_HOME"] = self.tart_home
        return env
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `mise run test tests/test_runner_config.py`
Expected: `9 passed`

- [ ] **Step 5: Commit**

```bash
git add host/runner.py tests/test_runner_config.py
git commit -m "feat: load_env and Config dataclass"
```

---

### Task 4: LogfmtFormatter

Custom `logging.Formatter` that produces logfmt lines.

**Files:**
- Modify: `host/runner.py` (add `LogfmtFormatter` and `setup_logging`)
- Create: `tests/test_logging.py`

- [ ] **Step 1: Write failing tests**

`tests/test_logging.py`:
```python
import sys, os, logging
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from host.runner import LogfmtFormatter


def make_record(msg: str, level=logging.INFO, runner="r1", run_id="abc123") -> logging.LogRecord:
    record = logging.LogRecord(
        name="test", level=level, pathname="", lineno=0,
        msg=msg, args=(), exc_info=None,
    )
    record.runner = runner
    record.run_id = run_id
    return record


def test_logfmt_contains_required_fields():
    fmt = LogfmtFormatter()
    line = fmt.format(make_record("VM booted"))
    assert "ts=" in line
    assert "level=info" in line
    assert "runner=r1" in line
    assert "run_id=abc123" in line
    assert 'msg="VM booted"' in line


def test_logfmt_warns_level():
    fmt = LogfmtFormatter()
    line = fmt.format(make_record("shutting down", level=logging.WARNING))
    assert "level=warn" in line


def test_logfmt_error_level():
    fmt = LogfmtFormatter()
    line = fmt.format(make_record("boom", level=logging.ERROR))
    assert "level=error" in line


def test_logfmt_msg_without_spaces_no_quotes():
    fmt = LogfmtFormatter()
    line = fmt.format(make_record("booted"))
    assert "msg=booted" in line


def test_logfmt_msg_with_spaces_quoted():
    fmt = LogfmtFormatter()
    line = fmt.format(make_record("VM is ready"))
    assert 'msg="VM is ready"' in line


def test_logfmt_no_run_id_omitted():
    fmt = LogfmtFormatter()
    record = make_record("loop start", run_id="")
    line = fmt.format(record)
    assert "run_id=" not in line


def test_logfmt_ts_format():
    fmt = LogfmtFormatter()
    line = fmt.format(make_record("x"))
    # ts=2026-06-17T10:23:01Z
    import re
    assert re.search(r"ts=\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", line)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `mise run test tests/test_logging.py`
Expected: `ImportError` (LogfmtFormatter not yet defined)

- [ ] **Step 3: Add LogfmtFormatter and setup_logging to `host/runner.py`**

Add after the `Config` class:

```python
import logging
import time


class LogfmtFormatter(logging.Formatter):
    _LEVEL_MAP = {
        logging.DEBUG: "debug",
        logging.INFO: "info",
        logging.WARNING: "warn",
        logging.ERROR: "error",
        logging.CRITICAL: "critical",
    }

    def format(self, record: logging.LogRecord) -> str:
        ts = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(record.created))
        level = self._LEVEL_MAP.get(record.levelno, record.levelname.lower())
        runner = getattr(record, "runner", "")
        run_id = getattr(record, "run_id", "")
        msg = record.getMessage()
        if " " in msg or '"' in msg:
            msg = '"' + msg.replace('"', '\\"') + '"'

        parts = [f"ts={ts}", f"level={level}"]
        if runner:
            parts.append(f"runner={runner}")
        if run_id:
            parts.append(f"run_id={run_id}")
        parts.append(f"msg={msg}")
        return " ".join(parts)


def setup_logging(log_file: Path, runner_name: str) -> logging.Logger:
    """Return a logger that writes logfmt to stdout and log_file simultaneously."""
    logger = logging.getLogger(runner_name)
    logger.setLevel(logging.DEBUG)
    logger.propagate = False

    fmt = LogfmtFormatter()

    stdout_handler = logging.StreamHandler()
    stdout_handler.setFormatter(fmt)

    file_handler = logging.FileHandler(log_file, mode="a")
    file_handler.setFormatter(fmt)

    logger.addHandler(stdout_handler)
    logger.addHandler(file_handler)
    return logger
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `mise run test tests/test_logging.py`
Expected: `7 passed`

- [ ] **Step 5: Commit**

```bash
git add host/runner.py tests/test_logging.py
git commit -m "feat: LogfmtFormatter and setup_logging"
```

---

### Task 5: Drain sentinel

The drain check that each loop iteration runs before starting a new job.

**Files:**
- Modify: `host/runner.py` (add `is_drain_set`)
- Modify: `tests/test_runner_config.py` (add drain tests)

- [ ] **Step 1: Write failing tests**

Add to `tests/test_runner_config.py`:
```python
from host.runner import is_drain_set

def test_drain_false_when_no_sentinel(tmp_path):
    assert is_drain_set(tmp_path / "drain") is False

def test_drain_true_when_sentinel_exists(tmp_path):
    sentinel = tmp_path / "drain"
    sentinel.touch()
    assert is_drain_set(sentinel) is True
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `mise run test tests/test_runner_config.py -k drain`
Expected: `ImportError` (is_drain_set not yet defined)

- [ ] **Step 3: Add `is_drain_set` to `host/runner.py`**

Add after `setup_logging`:

```python
def is_drain_set(drain_path: Path) -> bool:
    return drain_path.exists()
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `mise run test tests/test_runner_config.py`
Expected: `11 passed`

- [ ] **Step 5: Commit**

```bash
git add host/runner.py tests/test_runner_config.py
git commit -m "feat: drain sentinel helper"
```

---

### Task 6: VM lifecycle helpers

SSH helpers and the per-cycle `run_once` function.

**Files:**
- Modify: `host/runner.py` (add `_tart`, `_ssh`, `_wait_for_ip`, `_wait_for_ssh`, `run_once`)

No unit tests for this task — tart and SSH require real VMs. The function is verified by running `mise run runners:start` in Task 9.

- [ ] **Step 1: Add imports at the top of `host/runner.py`**

Add to the top of `host/runner.py` (after `from __future__ import annotations`):

```python
import re
import subprocess
import threading
import uuid
```

- [ ] **Step 2: Add `_tart` helper**

Add after `is_drain_set`:

```python
def _tart(args: list, config: Config, **kwargs) -> subprocess.CompletedProcess:
    """Run a tart subcommand, injecting TART_HOME if configured."""
    return subprocess.run(["tart"] + args, env=config.tart_env, **kwargs)
```

- [ ] **Step 3: Add SSH helper**

```python
def _ssh(ip: str, config: Config, command: str, check: bool = True) -> subprocess.CompletedProcess:
    """Run a command on the guest VM over SSH using sshpass."""
    return subprocess.run(
        [
            "sshpass", "-e", "ssh", "-q",
            "-o", "StrictHostKeyChecking=no",
            "-o", "ConnectTimeout=5",
            f"{config.vm_username}@{ip}",
            command,
        ],
        env={**os.environ, "SSHPASS": config.vm_password},
        capture_output=True,
        check=check,
    )
```

- [ ] **Step 4: Add `_wait_for_ip` and `_wait_for_ssh`**

```python
def _wait_for_ip(
    instance: str,
    config: Config,
    log: logging.Logger,
    run_id: str,
    shutdown: threading.Event,
) -> str:
    while not shutdown.is_set():
        result = _tart(["ip", instance], config, capture_output=True)
        ip = result.stdout.decode().strip()
        if re.match(r"^(\d+\.){3}\d+$", ip):
            log.info("IP acquired", extra={"run_id": run_id, "runner": log.name})
            subprocess.run(["ssh-keygen", "-R", ip], capture_output=True)
            return ip
        shutdown.wait(timeout=1)
    return ""


def _wait_for_ssh(
    ip: str,
    config: Config,
    log: logging.Logger,
    run_id: str,
    shutdown: threading.Event,
) -> None:
    log.info("waiting for SSH", extra={"run_id": run_id, "runner": log.name})
    while not shutdown.is_set():
        result = _ssh(ip, config, "true", check=False)
        if result.returncode == 0:
            log.info("SSH ready", extra={"run_id": run_id, "runner": log.name})
            return
        shutdown.wait(timeout=1)
```

- [ ] **Step 5: Add `run_once`**

```python
def run_once(
    config: Config,
    runner_name: str,
    log: logging.Logger,
    shutdown: threading.Event,
    force_shutdown: threading.Event,
) -> None:
    """Execute one complete runner lifecycle: clone → boot → job → teardown."""
    from host.gh_token import fetch_token

    run_id = uuid.uuid4().hex[:8]
    instance = f"runner_{runner_name}_{run_id}"

    log.info("fetching registration token", extra={"run_id": run_id, "runner": runner_name})
    token = fetch_token(config.as_gh_token_config(), kind="registration")

    log.info("cloning VM", extra={"run_id": run_id, "runner": runner_name})
    _tart(["clone", config.registry_path, instance], config, check=True)

    try:
        _tart(
            ["set", instance, "--memory", str(config.vm_ram), "--cpu", str(config.vm_cpu)],
            config, check=True,
        )

        log.info("booting VM", extra={"run_id": run_id, "runner": runner_name})
        subprocess.Popen(
            ["tart", "run", "--no-graphics", instance],
            env=config.tart_env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

        ip = _wait_for_ip(instance, config, log, run_id, shutdown)
        if shutdown.is_set():
            return

        _wait_for_ssh(ip, config, log, run_id, shutdown)
        if shutdown.is_set():
            return

        log.info("configuring runner", extra={"run_id": run_id, "runner": runner_name})
        _ssh(ip, config, (
            f"./actions-runner/config.sh "
            f"--url {config.runner_url} "
            f"--token {token} "
            f"--ephemeral "
            f"--name {runner_name} "
            f"--labels {config.runner_labels} "
            f"--unattended --replace"
        ))

        log.info("starting runner", extra={"run_id": run_id, "runner": runner_name})
        _ssh(ip, config, "source ~/.zprofile && ./actions-runner/run.sh")
        log.info("job complete", extra={"run_id": run_id, "runner": runner_name})

    except subprocess.CalledProcessError as e:
        stderr = e.stderr.decode(errors="replace").strip() if e.stderr else ""
        log.error(
            f"subprocess failed: {' '.join(e.cmd)}" + (f" — {stderr}" if stderr else ""),
            extra={"run_id": run_id, "runner": runner_name},
        )
        raise

    finally:
        if not force_shutdown.is_set():
            log.info("stopping VM", extra={"run_id": run_id, "runner": runner_name})
            _tart(["stop", instance], config, capture_output=True)
        log.info("deleting VM", extra={"run_id": run_id, "runner": runner_name})
        _tart(["delete", instance], config, capture_output=True)
```

- [ ] **Step 6: Run all tests to confirm nothing broke**

Run: `mise run test`
Expected: `11 passed` (no new tests, existing tests still pass)

- [ ] **Step 7: Commit**

```bash
git add host/runner.py
git commit -m "feat: VM lifecycle helpers — tart, ssh, run_once"
```

---

### Task 7: Main — runner loop, parallelism, signal handling

The `run_loop` per-thread function, signal handlers, and `main()` with argparse.

**Files:**
- Modify: `host/runner.py` (add `run_loop`, `main`)
- Create: `tests/test_main.py`

- [ ] **Step 1: Write failing tests**

`tests/test_main.py`:
```python
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import pytest
from unittest.mock import patch
from host.runner import parse_args


def test_parse_args_defaults():
    args = parse_args([])
    assert args.parallel == 1
    assert args.env == ".env"
    assert args.log == "runner.log"


def test_parse_args_parallel():
    args = parse_args(["--parallel", "2"])
    assert args.parallel == 2


def test_parse_args_parallel_zero_rejected():
    with pytest.raises(SystemExit):
        parse_args(["--parallel", "0"])


def test_parse_args_parallel_negative_rejected():
    with pytest.raises(SystemExit):
        parse_args(["--parallel", "-1"])


def test_parse_args_custom_env():
    args = parse_args(["--env", "/tmp/custom.env"])
    assert args.env == "/tmp/custom.env"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `mise run test tests/test_main.py`
Expected: `ImportError` (parse_args not defined)

- [ ] **Step 3: Add `parse_args` to `host/runner.py`**

Add after `run_once`:

```python
import argparse


def parse_args(argv: list | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Tartelette runner host loop")
    parser.add_argument(
        "--parallel",
        type=int,
        default=1,
        metavar="N",
        help="number of concurrent runner loops (default 1, macOS caps macOS VMs at 2)",
    )
    parser.add_argument("--env", default=".env", help="path to .env file (default .env)")
    parser.add_argument("--log", default="runner.log", help="log file path (default runner.log)")

    args = parser.parse_args(argv)
    if args.parallel < 1:
        parser.error(f"--parallel must be >= 1, got {args.parallel}")
    return args
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `mise run test tests/test_main.py`
Expected: `5 passed`

- [ ] **Step 5: Add `run_loop` and `main` to `host/runner.py`**

Add at the bottom of `host/runner.py`:

```python
import signal
import sys


def run_loop(
    config: Config,
    runner_name: str,
    log: logging.Logger,
    drain_path: Path,
    shutdown: threading.Event,
    force_shutdown: threading.Event,
) -> None:
    log.info("loop starting", extra={"runner": runner_name})
    while not shutdown.is_set():
        if is_drain_set(drain_path):
            log.info("drain sentinel found, exiting loop", extra={"runner": runner_name})
            return
        try:
            run_once(config, runner_name, log, shutdown, force_shutdown)
        except Exception:
            if force_shutdown.is_set():
                return
            if not shutdown.is_set():
                log.error("run_once failed, retrying after 5s", extra={"runner": runner_name})
                shutdown.wait(timeout=5)
    log.info("loop exited", extra={"runner": runner_name})


def main() -> None:
    args = parse_args()

    root = Path(os.environ.get("MISE_PROJECT_ROOT", "."))
    env_path = Path(args.env) if os.path.isabs(args.env) else root / args.env
    log_path = Path(args.log) if os.path.isabs(args.log) else root / args.log
    drain_path = root / ".runners" / "drain"

    if not env_path.exists():
        print(f"error: {env_path} not found", file=sys.stderr)
        sys.exit(1)

    env = load_env(env_path)
    config = Config.from_env(env)

    if args.parallel > 2:
        print(
            f"[runner] warning: macOS allows at most 2 concurrent macOS VMs; "
            f"VM 3+ will fail to boot",
            file=sys.stderr,
        )

    log_path.parent.mkdir(parents=True, exist_ok=True)
    # Use first runner name for the shared logger; threads pass their own name per-record
    log = setup_logging(log_path, config.runner_name)

    shutdown = threading.Event()
    force_shutdown = threading.Event()
    _last_sigint: list[float] = []

    # Define runner_names before registering signal handlers to avoid a
    # closure race if a signal arrives before the assignment below.
    base_name = config.runner_name
    runner_names = (
        [base_name]
        if args.parallel == 1
        else [f"{base_name}-{i}" for i in range(1, args.parallel + 1)]
    )

    def handle_signal(sig, frame):
        now = time.time()
        if shutdown.is_set() and _last_sigint and (now - _last_sigint[-1]) < 3:
            for runner_name in runner_names:
                log.warning(
                    "force shutdown, killing VM",
                    extra={"runner": runner_name},
                )
            force_shutdown.set()
            shutdown.set()
        else:
            _last_sigint.append(now)
            for runner_name in runner_names:
                log.warning(
                    "shutdown requested, waiting for current job to finish",
                    extra={"runner": runner_name},
                )
            shutdown.set()

    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)

    threads = [
        threading.Thread(
            target=run_loop,
            args=(config, name, log, drain_path, shutdown, force_shutdown),
            name=name,
            daemon=True,
        )
        for name in runner_names
    ]

    for t in threads:
        t.start()

    for t in threads:
        t.join()

    log.info("all runners stopped", extra={"runner": base_name})


if __name__ == "__main__":
    main()
```

- [ ] **Step 6: Run all tests**

Run: `mise run test`
Expected: `16 passed`

- [ ] **Step 7: Commit**

```bash
git add host/runner.py tests/test_main.py
git commit -m "feat: run_loop, signal handling, main with --parallel"
```

---

### Task 8: Update mise tasks

Slim down `runners:start` and `runners:parallel`; add `runners:drain`; update `runners:clean`.

**Files:**
- Modify: `mise/tasks/runners/start`
- Modify: `mise/tasks/runners/parallel`
- Create: `mise/tasks/runners/drain`
- Modify: `mise/tasks/runners/clean`

- [ ] **Step 1: Rewrite `mise/tasks/runners/start`**

```bash
#!/usr/bin/env bash
#MISE description="Start a single runner host loop (one VM per job)"
# Thin wrapper: delegates all logic to host/runner.py.
# For N concurrent runners use `mise run runners:parallel`.

set -euo pipefail

ROOT="${MISE_PROJECT_ROOT:?run via 'mise run runners:start'}"
cd "$ROOT"
SRC_ENV="$ROOT/.env"
[ -f "$SRC_ENV" ] || { echo "missing $SRC_ENV" >&2; exit 1; }

# Local-only flow: fail fast if the image is not in tart list.
REGISTRY_URL="$(grep -E '^REGISTRY_URL=' "$SRC_ENV" | head -1 | cut -d= -f2-)"
IMAGE="$(grep -E '^REGISTRY_IMAGE_NAME=' "$SRC_ENV" | head -1 | cut -d= -f2-)"
if [ -z "$REGISTRY_URL" ] && ! tart list --format json | jq -e --arg n "$IMAGE" '.[] | select(.Name==$n)' >/dev/null; then
  echo "image '$IMAGE' (REGISTRY_IMAGE_NAME in .env) not found in tart list." >&2
  echo "build it with 'mise run vm:build' or fix REGISTRY_IMAGE_NAME. Available:" >&2
  tart list --format json | jq -r '.[] | select(.Source=="local" and (.Name|startswith("atlas-app-ci"))) | "  "+.Name' >&2
  exit 1
fi

exec python "$ROOT/host/runner.py" --env "$SRC_ENV" --log "$ROOT/runner.log"
```

- [ ] **Step 2: Rewrite `mise/tasks/runners/parallel`**

```bash
#!/usr/bin/env bash
#MISE description="Run N runner host loops in parallel (default 2)"
#USAGE arg "[n]" help="number of concurrent host loops (default 2; macOS caps macOS VMs at 2)" default="2"

set -euo pipefail

N="${usage_n:-2}"
case "$N" in
  ''|*[!0-9]*) echo "N must be a positive integer (got: $N)" >&2; exit 1 ;;
esac
[ "$N" -ge 1 ] || { echo "N must be >= 1" >&2; exit 1; }

ROOT="${MISE_PROJECT_ROOT:?run via 'mise run runners:parallel'}"
SRC_ENV="$ROOT/.env"
[ -f "$SRC_ENV" ] || { echo "missing $SRC_ENV" >&2; exit 1; }

# Local-only flow: fail fast if the image is not in tart list.
REGISTRY_URL="$(grep -E '^REGISTRY_URL=' "$SRC_ENV" | head -1 | cut -d= -f2-)"
IMAGE="$(grep -E '^REGISTRY_IMAGE_NAME=' "$SRC_ENV" | head -1 | cut -d= -f2-)"
if [ -z "$REGISTRY_URL" ] && ! tart list --format json | jq -e --arg n "$IMAGE" '.[] | select(.Name==$n)' >/dev/null; then
  echo "image '$IMAGE' (REGISTRY_IMAGE_NAME in .env) not found in tart list." >&2
  echo "build it with 'mise run vm:build' or fix REGISTRY_IMAGE_NAME. Available:" >&2
  tart list --format json | jq -r '.[] | select(.Source=="local" and (.Name|startswith("atlas-app-ci"))) | "  "+.Name' >&2
  exit 1
fi

exec python "$ROOT/host/runner.py" --parallel "$N" --env "$SRC_ENV" --log "$ROOT/runner.log"
```

- [ ] **Step 3: Create `mise/tasks/runners/drain`**

```bash
#!/usr/bin/env bash
#MISE description="Signal all runner loops to drain (finish current job then stop)"
# Creates a sentinel file that each runner checks at the top of its loop.
# Remove with 'mise run runners:clean' to resume normal operation.

set -euo pipefail

ROOT="${MISE_PROJECT_ROOT:?run via 'mise run runners:drain'}"
mkdir -p "$ROOT/.runners"
touch "$ROOT/.runners/drain"
echo "[drain] sentinel written — runners will stop after current job"
echo "[drain] remove with: mise run runners:clean"
```

Make it executable: `chmod +x mise/tasks/runners/drain`

- [ ] **Step 4: Update `mise/tasks/runners/clean`**

Replace the line that calls `vendor/ekiden/host/gh-runner-token.sh`:

Old line (around line 29):
```bash
  TOKEN=$(GH_TOKEN_KIND=installation "$ROOT/vendor/ekiden/host/gh-runner-token.sh")
```

New line:
```bash
  TOKEN=$(python "$ROOT/host/gh_token.py" --kind installation --env "$SRC_ENV")
```

Also add drain sentinel removal after the existing env source block (after line 18):
```bash
# Remove drain sentinel if present so runners resume after clean.
rm -f "$ROOT/.runners/drain"
```

Also remove the block that uses `GITHUB_REGISTRATION_ENDPOINT` for the token path (lines 32-36) and replace with:
```bash
else
  : "${GITHUB_API_TOKEN:?GITHUB_API_TOKEN unset}"
  TOKEN="$GITHUB_API_TOKEN"
fi
```

The full updated `mise/tasks/runners/clean` after these changes:
```bash
#!/usr/bin/env bash
#MISE description="Deregister GitHub Actions runners by RUNNER_NAME prefix"
# Matches runners whose name starts with RUNNER_NAME from .env. Useful after
# killing runners:parallel leaves orphans registered in GitHub.
#
# Usage: mise run runners:clean [--offline-only]
# Requires: jq, plus either
#   GITHUB_AUTH_METHOD=token: GITHUB_API_TOKEN
#   GITHUB_AUTH_METHOD=app:   GITHUB_APP_ID + GITHUB_APP_PRIVATE_KEY_PATH
#                             + GITHUB_OWNER (+ GITHUB_REPO for repo scope)

set -euo pipefail

ROOT="${MISE_PROJECT_ROOT:?run via 'mise run runners:clean'}"
SRC_ENV="$ROOT/.env"
[ -f "$SRC_ENV" ] || { echo "missing $SRC_ENV" >&2; exit 1; }

set -a; . "$SRC_ENV"; set +a

# Remove drain sentinel so runners resume after clean.
rm -f "$ROOT/.runners/drain"

: "${RUNNER_NAME:?RUNNER_NAME unset (used as the name prefix to match)}"

if [ "${GITHUB_AUTH_METHOD:-token}" = "app" ]; then
  : "${GITHUB_OWNER:?GITHUB_OWNER unset}"
  if [ -n "${GITHUB_REPO:-}" ]; then
    RUNNERS_URL="https://api.github.com/repos/${GITHUB_OWNER}/${GITHUB_REPO}/actions/runners"
  else
    RUNNERS_URL="https://api.github.com/orgs/${GITHUB_OWNER}/actions/runners"
  fi
  TOKEN=$(python "$ROOT/host/gh_token.py" --kind installation --env "$SRC_ENV")
else
  : "${GITHUB_API_TOKEN:?GITHUB_API_TOKEN unset}"
  : "${GITHUB_OWNER:?GITHUB_OWNER unset}"
  TOKEN="$GITHUB_API_TOKEN"
  if [ -n "${GITHUB_REPO:-}" ]; then
    RUNNERS_URL="https://api.github.com/repos/${GITHUB_OWNER}/${GITHUB_REPO}/actions/runners"
  else
    RUNNERS_URL="https://api.github.com/orgs/${GITHUB_OWNER}/actions/runners"
  fi
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
```

- [ ] **Step 5: Run all tests**

Run: `mise run test`
Expected: `16 passed`

- [ ] **Step 6: Commit**

```bash
git add mise/tasks/runners/start mise/tasks/runners/parallel \
        mise/tasks/runners/drain mise/tasks/runners/clean
git commit -m "feat: slim mise runner tasks, add runners:drain"
```

---

### Task 9: Update `.env.example`

Remove removed vars, fix ekiden references, add drain note.

**Files:**
- Modify: `.env.example`

- [ ] **Step 1: Rewrite `.env.example`**

```bash
# Copy to .env and fill the auth fields.
# Variable names are unchanged from ekiden for backward compatibility.

# Where Tart keeps its VM/image cache. Leave commented to use the default ~/.tart.
# Set this if TART_HOME is on an external disk.
# TART_HOME=/Volumes/T9/tart

# Auth method for minting the runner registration token: app or token.
GITHUB_AUTH_METHOD=app

# --- app: GitHub App auth (GITHUB_AUTH_METHOD=app) ---
# App installed on <ORG> with:
#   Organization permissions -> Self-hosted runners: Read and write
# For repo-level runner: set GITHUB_REPO=<REPO> and grant
#   Repository permissions -> Administration: Read and write
GITHUB_APP_ID=
# Must be an absolute path.
GITHUB_APP_PRIVATE_KEY_PATH=/absolute/path/to/app-private-key.pem
GITHUB_OWNER=<ORG>
GITHUB_REPO=

# --- token: PAT fallback (GITHUB_AUTH_METHOD=token) ---
# GITHUB_API_TOKEN=

# Where the runner attaches, and the labels it advertises.
# Must match runs-on in the workflow (self-hosted is added by GitHub automatically).
RUNNER_URL=https://github.com/<ORG>
RUNNER_LABELS=self-hosted,macos,arm64,tart
# Give each host a unique base name (e.g. the hostname: mini1, studio).
# runners:parallel appends -1/-2/... so mini1 becomes mini1-1, mini1-2.
RUNNER_NAME=runner

# Image source.
#   - Leave REGISTRY_URL empty for local-only flow (single host, no registry).
#   - Set REGISTRY_URL=localhost:5001 (or remote) for OCI registry distribution.
# REGISTRY_IMAGE_NAME must match a name in `tart list`.
REGISTRY_URL=
REGISTRY_IMAGE_NAME=atlas-app-ci-26.3

# SSH credentials baked into the cirruslabs base image.
VM_USERNAME=admin
VM_PASSWORD=admin

# VM sizing (optional).
VM_RAM=8192
VM_CPU=4
```

- [ ] **Step 2: Commit**

```bash
git add .env.example
git commit -m "chore: update .env.example — remove ekiden vars"
```

---

### Task 10: Remove ekiden submodule

The final step. Only do this after all new code is working.

**Files:**
- Delete: `vendor/ekiden/` (submodule)
- Modify: `.gitmodules`

- [ ] **Step 1: Deinit and remove the submodule**

```bash
git submodule deinit -f vendor/ekiden
git rm vendor/ekiden
rm -rf .git/modules/vendor/ekiden
```

- [ ] **Step 2: Verify .gitmodules is now empty or removed**

Run: `cat .gitmodules`
Expected: empty file or file not present (git rm removes the entry automatically)

- [ ] **Step 3: Run all tests one final time**

Run: `mise run test`
Expected: `16 passed`

- [ ] **Step 4: Commit**

```bash
git add .gitmodules
git commit -m "chore: remove ekiden submodule — replaced by host/runner.py"
```

---

## Post-implementation: promtail config

The monitoring stack lives outside this repo. After deploying, update the promtail config on the host:

Replace the `pipeline_stages` regex block with:
```yaml
pipeline_stages:
  - logfmt: {}
  - labels:
      runner:
  - timestamp:
      source: ts
      format: RFC3339
```

And update `__path__` to point to `runner.log` in the repo root (single file, not per-runner glob).
