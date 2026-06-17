#!/usr/bin/env python3
"""Tart CI runner — replaces vendor/ekiden/host/launch.sh."""
from __future__ import annotations

import argparse
import dataclasses
import logging
import os
import re
import signal
import subprocess
import sys
import threading
import time
import uuid
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


def is_drain_set(drain_path: Path) -> bool:
    return drain_path.exists()


def _tart(args: list, config: Config, **kwargs) -> subprocess.CompletedProcess:
    """Run a tart subcommand, injecting TART_HOME if configured."""
    return subprocess.run(["tart"] + args, env=config.tart_env, **kwargs)


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
        result = _ssh(ip, config, "true", check=False)
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
        # Write token to a temp file to avoid exposing it in the process list
        _ssh(ip, config, f"printf '%s' '{token}' > /tmp/.runner_token && chmod 600 /tmp/.runner_token")
        try:
            _ssh(ip, config, (
                f"./actions-runner/config.sh "
                f"--url {config.runner_url} "
                f'--token "$(cat /tmp/.runner_token)" '
                f"--ephemeral "
                f"--name {runner_name} "
                f"--labels {config.runner_labels} "
                f"--unattended --replace"
            ))
        finally:
            _ssh(ip, config, "rm -f /tmp/.runner_token", check=False)

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


def parse_args(argv: list | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Tart CI runner host loop")
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
