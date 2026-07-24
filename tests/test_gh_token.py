import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

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
