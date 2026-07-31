#!/usr/bin/env python3
"""Tartelette runner — replaces vendor/ekiden/host/launch.sh."""
from __future__ import annotations

import argparse
import dataclasses
import logging
import os
import re
import signal
import socket
import subprocess
import sys
import threading
import time
import unicodedata
import uuid
from pathlib import Path
from typing import Optional

# When launched as a script (`python host/runner.py`), sys.path[0] is host/, so
# the `host` package is not importable. Add the project root so `host.*` resolves
# in both script and module (`python -m host.runner`, tests) invocations.
if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from host.gh_token import GitHubAPIError


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


def sanitize_runner_name(name: str) -> str:
    """Convert a computer name to a lowercase, hyphen-separated runner name."""
    ascii_name = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", "-", ascii_name.lower()).strip("-") or "runner"


def default_runner_name() -> str:
    """Return a sanitized macOS Computer Name with a VM suffix."""
    try:
        name = subprocess.check_output(
            ["scutil", "--get", "ComputerName"], text=True
        ).strip()
        if name:
            return f"{sanitize_runner_name(name)}-vm"
    except (OSError, subprocess.CalledProcessError):
        pass
    return f"{sanitize_runner_name(socket.gethostname())}-vm"


def xcode_version_label(xcodebuild_output: str) -> str:
    """Return a GitHub runner label for the installed Xcode version."""
    match = re.search(r"^Xcode\s+(\d+(?:\.\d+)*)\b", xcodebuild_output, re.MULTILINE)
    if not match:
        raise ValueError(f"could not determine Xcode version from: {xcodebuild_output!r}")
    return f"xcode-{match.group(1)}"


def with_xcode_version_label(labels: str, xcodebuild_output: str) -> str:
    """Append the installed Xcode version label unless it is already present."""
    xcode_label = xcode_version_label(xcodebuild_output)
    existing_labels = [label.strip() for label in labels.split(",") if label.strip()]
    if xcode_label not in existing_labels:
        existing_labels.append(xcode_label)
    return ",".join(existing_labels)


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
            runner_name=env.get("RUNNER_NAME") or default_runner_name(),
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
        line = " ".join(parts)
        if record.exc_info:
            line += "\n" + self.formatException(record.exc_info)
        return line


class ConsoleFormatter(logging.Formatter):
    """Human-friendly stdout formatter: `HH:MM:SS LEVEL  runner/run_id  message`.
    Colorizes the level when color=True; the file keeps logfmt for grepping."""

    _RESET = "\033[0m"
    _DIM = "\033[2m"
    _LEVEL_COLOR = {
        logging.DEBUG: "\033[2m",
        logging.INFO: "\033[32m",
        logging.WARNING: "\033[33m",
        logging.ERROR: "\033[31m",
        logging.CRITICAL: "\033[1;31m",
    }
    _LEVEL_LABEL = {
        logging.DEBUG: "DEBUG",
        logging.INFO: "INFO",
        logging.WARNING: "WARN",
        logging.ERROR: "ERROR",
        logging.CRITICAL: "CRIT",
    }

    def __init__(self, color: bool = True):
        super().__init__()
        self.color = color

    def _paint(self, text: str, code: str) -> str:
        return f"{code}{text}{self._RESET}" if self.color and code else text

    def format(self, record: logging.LogRecord) -> str:
        t = time.strftime("%H:%M:%S", time.localtime(record.created))
        label = self._LEVEL_LABEL.get(record.levelno, record.levelname.upper())
        runner = getattr(record, "runner", "")
        run_id = getattr(record, "run_id", "")
        ctx = f"{runner}/{run_id}" if runner and run_id else (runner or run_id)

        parts = [
            self._paint(t, self._DIM),
            self._paint(f"{label:<5}", self._LEVEL_COLOR.get(record.levelno, "")),
        ]
        if ctx:
            parts.append(self._paint(ctx, self._DIM))
        parts.append(record.getMessage())
        line = "  ".join(parts)
        if record.exc_info:
            line += "\n" + self.formatException(record.exc_info)
        return line


def setup_logging(log_file: Path, runner_name: str) -> logging.Logger:
    """Pretty (optionally colored) output to stdout, logfmt to log_file."""
    logger = logging.getLogger(runner_name)
    logger.setLevel(logging.DEBUG)
    logger.propagate = False

    color = sys.stdout.isatty() and os.environ.get("NO_COLOR") is None
    stdout_handler = logging.StreamHandler()
    stdout_handler.setFormatter(ConsoleFormatter(color=color))

    file_handler = logging.FileHandler(log_file, mode="a")
    file_handler.setFormatter(LogfmtFormatter())

    logger.addHandler(stdout_handler)
    logger.addHandler(file_handler)
    return logger


def is_drain_set(drain_path: Path) -> bool:
    return drain_path.exists()


def _tart(args: list, config: Config, **kwargs) -> subprocess.CompletedProcess:
    """Run a tart subcommand, injecting TART_HOME if configured."""
    return subprocess.run(["tart"] + args, env=config.tart_env, **kwargs)


def _ssh(
    ip: str, config: Config, command: str, check: bool = True, retries: int = 3
) -> subprocess.CompletedProcess:
    """Run a command on the guest VM over SSH using sshpass. Retries transient
    connection failures (ssh exit 255) — sshd is often briefly flaky right after
    boot. A non-255 exit is the remote command's own code and is not retried.
    Uses LogLevel=ERROR (not -q) so real failures land in stderr for logging."""
    result = None
    for attempt in range(retries):
        result = subprocess.run(
            [
                "sshpass", "-e", "ssh",
                "-o", "LogLevel=ERROR",
                "-o", "StrictHostKeyChecking=no",
                "-o", "ConnectTimeout=5",
                "-o", "PreferredAuthentications=password",
                "-o", "PubkeyAuthentication=no",
                "-o", "IdentitiesOnly=yes",
                f"{config.vm_username}@{ip}",
                command,
            ],
            env={**os.environ, "SSHPASS": config.vm_password},
            capture_output=True,
        )
        if result.returncode != 255 or attempt == retries - 1:
            break
        time.sleep(2)
    if check and result.returncode != 0:
        raise subprocess.CalledProcessError(
            result.returncode, result.args, result.stdout, result.stderr
        )
    return result


def _ssh_stream(
    ip: str,
    config: Config,
    command: str,
    log: logging.Logger,
    run_id: str,
    runner_name: str,
) -> None:
    """Run a guest command over SSH, relaying each output line to the logger so
    the runner's own status (e.g. 'Listening for Jobs') is visible live.
    Raises CalledProcessError on non-zero exit."""
    proc = subprocess.Popen(
        [
            "sshpass", "-e", "ssh",
            "-o", "LogLevel=ERROR",
            "-o", "StrictHostKeyChecking=no",
            "-o", "ConnectTimeout=5",
            "-o", "PreferredAuthentications=password",
            "-o", "PubkeyAuthentication=no",
            "-o", "IdentitiesOnly=yes",
            f"{config.vm_username}@{ip}",
            command,
        ],
        env={**os.environ, "SSHPASS": config.vm_password},
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )
    for line in proc.stdout:
        line = line.rstrip()
        if line:
            log.info(f"[guest] {line}", extra={"run_id": run_id, "runner": runner_name})
    if proc.wait() != 0:
        raise subprocess.CalledProcessError(proc.returncode, command)


def _wait_for_ip(
    instance: str,
    config: Config,
    log: logging.Logger,
    run_id: str,
    shutdown: threading.Event,
    tart_proc: subprocess.Popen | None = None,
) -> str:
    while not shutdown.is_set():
        if tart_proc is not None and tart_proc.poll() is not None:
            raise RuntimeError(f"tart run exited unexpectedly (code {tart_proc.returncode})")
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
        result = _ssh(ip, config, "true", check=False, retries=1)
        if result.returncode == 0:
            log.info("SSH ready", extra={"run_id": run_id, "runner": log.name})
            return
        shutdown.wait(timeout=1)


def run_once(
    config: Config,
    runner_name: str,
    log: logging.Logger,
    shutdown: threading.Event,
    force_shutdown: threading.Event,
) -> None:
    """Execute one complete runner lifecycle: clone → boot → job → teardown."""
    from host.gh_token import delete_runner, fetch_token

    run_id = uuid.uuid4().hex[:8]
    instance = f"runner_{runner_name}_{run_id}"
    registered = False

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
        tart_proc = subprocess.Popen(
            ["tart", "run", "--no-graphics", instance],
            env=config.tart_env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

        ip = _wait_for_ip(instance, config, log, run_id, shutdown, tart_proc)
        if shutdown.is_set():
            return

        _wait_for_ssh(ip, config, log, run_id, shutdown)
        if shutdown.is_set():
            return

        log.info("configuring runner", extra={"run_id": run_id, "runner": runner_name})
        xcodebuild_output = _ssh(ip, config, "xcodebuild -version").stdout.decode()
        runner_labels = with_xcode_version_label(config.runner_labels, xcodebuild_output)
        # Write token to a temp file to avoid exposing it in the process list
        _ssh(ip, config, f"printf '%s' '{token}' > /tmp/.runner_token && chmod 600 /tmp/.runner_token")
        try:
            _ssh_stream(ip, config, (
                f"./actions-runner/config.sh "
                f"--url {config.runner_url} "
                f'--token "$(cat /tmp/.runner_token)" '
                f"--ephemeral "
                f"--name {runner_name} "
                f"--labels {runner_labels} "
                f"--unattended"
            ), log, run_id, runner_name)
            registered = True
        finally:
            _ssh(ip, config, "rm -f /tmp/.runner_token", check=False)

        log.info("runner ready, waiting for a job", extra={"run_id": run_id, "runner": runner_name})
        _ssh_stream(ip, config, "source ~/.zprofile && ./actions-runner/run.sh", log, run_id, runner_name)
        log.info("job complete", extra={"run_id": run_id, "runner": runner_name})

    except subprocess.CalledProcessError as e:
        cmd = e.cmd if isinstance(e.cmd, str) else " ".join(e.cmd)
        stderr = e.stderr.decode(errors="replace").strip() if e.stderr else ""
        # run.sh dying because the user asked us to stop is expected, not a failure.
        if shutdown.is_set():
            log.info("runner interrupted by shutdown", extra={"run_id": run_id, "runner": runner_name})
        else:
            log.error(
                f"subprocess failed: {cmd}" + (f" — {stderr}" if stderr else ""),
                extra={"run_id": run_id, "runner": runner_name},
            )
            raise

    finally:
        if not force_shutdown.is_set():
            log.info("stopping VM", extra={"run_id": run_id, "runner": runner_name})
            _tart(["stop", instance], config, capture_output=True)
        log.info("deleting VM", extra={"run_id": run_id, "runner": runner_name})
        _tart(["delete", instance], config, capture_output=True)
        # Ephemeral runners self-remove after a completed job; an interrupted or
        # failed run leaves a stale registration that blocks the next start with
        # "A session for this runner already exists". Best-effort cleanup.
        if registered:
            try:
                if delete_runner(config.as_gh_token_config(), runner_name):
                    log.info("deregistered runner from GitHub", extra={"run_id": run_id, "runner": runner_name})
            except Exception as e:
                log.warning(f"failed to deregister runner: {e!r}", extra={"run_id": run_id, "runner": runner_name})


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


def choose_runner_name(base_name: str, existing_names: set[str]) -> str:
    """Choose a safe name for a standalone runner loop.

    The configured base name remains the first choice for backwards
    compatibility.  If it is in use, leave ``-1`` for the deterministic
    ``runners:parallel`` pool and choose the first available ``-2`` suffix.
    """
    if base_name not in existing_names:
        return base_name

    suffix = 2
    while f"{base_name}-{suffix}" in existing_names:
        suffix += 1
    return f"{base_name}-{suffix}"


def resolve_standalone_runner_name(config: Config) -> str:
    """Query GitHub and select a name which is not already registered."""
    from host.gh_token import list_runner_names

    return choose_runner_name(config.runner_name, list_runner_names(config.as_gh_token_config()))


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
        except GitHubAPIError as e:
            if force_shutdown.is_set():
                return
            if not shutdown.is_set():
                if 500 <= e.status < 600:
                    log.warning(
                        f"GitHub API temporarily unavailable (HTTP {e.status}), retrying after 5s",
                        extra={"runner": runner_name},
                    )
                else:
                    log.error(
                        "run_once failed, retrying after 5s",
                        exc_info=True,
                        extra={"runner": runner_name},
                    )
                shutdown.wait(timeout=5)
        except Exception:
            if force_shutdown.is_set():
                return
            if not shutdown.is_set():
                log.error(
                    "run_once failed, retrying after 5s",
                    exc_info=True,
                    extra={"runner": runner_name},
                )
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
    log = setup_logging(log_path, config.runner_name)

    shutdown = threading.Event()
    force_shutdown = threading.Event()
    _last_sigint: list[float] = []

    # Define runner_names before registering signal handlers to avoid a
    # closure race if a signal arrives before the assignment below.
    base_name = config.runner_name
    runner_names = (
        [resolve_standalone_runner_name(config)]
        if args.parallel == 1
        else [f"{base_name}-{i}" for i in range(1, args.parallel + 1)]
    )

    if args.parallel == 1 and runner_names[0] != base_name:
        log.info(
            f"runner name {base_name!r} is already registered; using {runner_names[0]!r}",
            extra={"runner": runner_names[0]},
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
