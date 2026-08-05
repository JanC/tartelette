import sys, os
import pytest
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from host import gh_token
from host.gh_token import GitHubAPIError, _b64url, _derive_scope, _derive_runners_url

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


def test_github_api_error_exposes_http_status_for_retry_handling():
    error = GitHubAPIError("POST", "https://api.github.com/example", 503, "Service Unavailable")

    assert error.status == 503
    assert str(error) == "GitHub API POST https://api.github.com/example -> HTTP 503: Service Unavailable"


def test_list_runner_names_reads_all_pages(monkeypatch):
    calls = []

    monkeypatch.setattr(gh_token, "fetch_token", lambda config, kind: "token")

    def fake_api(method, url, token):
        calls.append(url)
        if url.endswith("page=1"):
            return {"runners": [{"name": f"runner-{i}"} for i in range(100)]}
        return {"runners": [{"name": "runner-100"}]}

    monkeypatch.setattr(gh_token, "_gh_api", fake_api)

    names = gh_token.list_runner_names({"GITHUB_OWNER": "myorg", "GITHUB_REPO": "myrepo"})

    assert names == {f"runner-{i}" for i in range(101)}
    assert len(calls) == 2


def test_find_workflow_run_for_runner_returns_matching_run(monkeypatch):
    monkeypatch.setattr(gh_token, "fetch_token", lambda config, kind: "token")

    def fake_api(method, url, token):
        if url.endswith("/actions/runs?status=in_progress&per_page=100"):
            return {
                "workflow_runs": [
                    {"id": 101, "html_url": "https://github.com/myorg/myrepo/actions/runs/101"},
                    {"id": 102, "html_url": "https://github.com/myorg/myrepo/actions/runs/102"},
                ]
            }
        if url.endswith("/actions/runs/101/jobs?filter=latest&per_page=100"):
            return {"jobs": [{"runner_name": "other-runner"}]}
        if url.endswith("/actions/runs/102/jobs?filter=latest&per_page=100"):
            return {"jobs": [{"runner_name": "target-runner"}]}
        raise AssertionError(url)

    monkeypatch.setattr(gh_token, "_gh_api", fake_api)

    run = gh_token.find_workflow_run_for_runner(
        {"GITHUB_OWNER": "myorg", "GITHUB_REPO": "myrepo"}, "target-runner"
    )

    assert run == {"id": 102, "html_url": "https://github.com/myorg/myrepo/actions/runs/102"}


def test_find_workflow_run_for_runner_skips_org_scope(monkeypatch):
    monkeypatch.setattr(gh_token, "fetch_token", lambda *args: pytest.fail("unexpected token fetch"))

    assert gh_token.find_workflow_run_for_runner(
        {"GITHUB_OWNER": "myorg", "GITHUB_REPO": ""}, "target-runner"
    ) is None
