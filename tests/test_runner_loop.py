import logging
import subprocess
from pathlib import Path

import pytest

from host.gh_token import GitHubAPIError
from host import runner


def test_report_workflow_run_logs_url(monkeypatch, caplog):
    import host.gh_token

    config = type(
        "Config",
        (),
        {
            "github_repo": "myrepo",
            "as_gh_token_config": lambda self: {"GITHUB_REPO": "myrepo"},
        },
    )()
    monkeypatch.setattr(
        host.gh_token,
        "find_workflow_run_for_runner",
        lambda config, runner_name: {"html_url": "https://github.com/myorg/myrepo/actions/runs/42"},
    )
    log = logging.getLogger("test-runner")

    with caplog.at_level(logging.INFO, logger=log.name):
        runner._report_workflow_run(config, "my-runner", log, "run-id")

    assert "workflow run: https://github.com/myorg/myrepo/actions/runs/42" in caplog.text


def test_ssh_stream_runs_in_its_own_session(monkeypatch):
    captured = {}

    class FakeProcess:
        stdout = []

        def wait(self):
            return 0

    def fake_popen(args, **kwargs):
        captured["args"] = args
        captured["kwargs"] = kwargs
        return FakeProcess()

    monkeypatch.setattr(runner.subprocess, "Popen", fake_popen)

    runner._ssh_stream(
        "192.0.2.1",
        type("Config", (), {"vm_username": "admin", "vm_password": "secret"})(),
        "./actions-runner/run.sh",
        logging.getLogger("test-runner"),
        "run-id",
        "test-runner",
    )

    assert captured["kwargs"]["start_new_session"] is True


def test_ssh_stream_terminates_on_forced_shutdown(monkeypatch):
    class FakeStdout:
        def fileno(self):
            return 4

    class FakeProcess:
        stdout = FakeStdout()
        returncode = -15
        terminated = False

        def terminate(self):
            self.terminated = True

        def wait(self, timeout=None):
            return self.returncode

    proc = FakeProcess()
    monkeypatch.setattr(runner.subprocess, "Popen", lambda *args, **kwargs: proc)
    monkeypatch.setattr(runner.select, "select", lambda *args: ([], [], []))
    force_shutdown = _Event()
    force_shutdown.set()

    with pytest.raises(subprocess.CalledProcessError):
        runner._ssh_stream(
            "192.0.2.1",
            type("Config", (), {"vm_username": "admin", "vm_password": "secret"})(),
            "./actions-runner/run.sh",
            logging.getLogger("test-runner"),
            "run-id",
            "test-runner",
            force_shutdown=force_shutdown,
        )

    assert proc.terminated is True


def test_ssh_stream_stops_idle_runner_after_graceful_shutdown(monkeypatch):
    class FakeStdout:
        def fileno(self):
            return 4

    class FakeProcess:
        stdout = FakeStdout()
        returncode = 0
        terminated = False

        def terminate(self):
            self.terminated = True

        def wait(self, timeout=None):
            return self.returncode

    proc = FakeProcess()
    shutdown = _Event()
    shutdown.set()
    callback_calls = 0

    def deregister_if_idle():
        nonlocal callback_calls
        callback_calls += 1
        return True

    monkeypatch.setattr(runner.subprocess, "Popen", lambda *args, **kwargs: proc)

    with pytest.raises(subprocess.CalledProcessError):
        runner._ssh_stream(
            "192.0.2.1",
            type("Config", (), {"vm_username": "admin", "vm_password": "secret"})(),
            "./actions-runner/run.sh",
            logging.getLogger("test-runner"),
            "run-id",
            "test-runner",
            shutdown=shutdown,
            on_graceful_shutdown=deregister_if_idle,
        )

    assert callback_calls == 1
    assert proc.terminated is True


class _Event:
    def __init__(self):
        self.set_ = False
        self.waits = []

    def is_set(self):
        return self.set_

    def set(self):
        self.set_ = True

    def wait(self, timeout=None):
        self.waits.append(timeout)
        return False


def test_run_loop_warns_without_traceback_for_transient_github_api_failure(monkeypatch, caplog):
    shutdown = _Event()
    force_shutdown = _Event()
    calls = 0

    def fake_run_once(*args):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise GitHubAPIError("POST", "https://api.github.com/example", 503, "Service Unavailable")
        shutdown.set()

    monkeypatch.setattr(runner, "run_once", fake_run_once)
    log = logging.getLogger("test-runner")

    with caplog.at_level(logging.WARNING, logger=log.name):
        runner.run_loop(None, "test-runner", log, Path("/not-used"), shutdown, force_shutdown)

    record = caplog.records[0]
    assert record.levelno == logging.WARNING
    assert "HTTP 503" in record.getMessage()
    assert record.exc_info is None
    assert shutdown.waits == [5]
