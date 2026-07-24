import logging
from pathlib import Path

from host.gh_token import GitHubAPIError
from host import runner


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
