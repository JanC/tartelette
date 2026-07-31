import sys, os, textwrap
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from pathlib import Path
import pytest
from host.runner import (
    Config,
    default_runner_name,
    is_drain_set,
    load_env,
    sanitize_runner_name,
    with_xcode_version_label,
    xcode_version_label,
)


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
        "REGISTRY_IMAGE_NAME": "ios-app-ci",
        "VM_USERNAME": "admin",
        "VM_PASSWORD": "admin",
    }
    cfg = Config.from_env(env)
    assert cfg.vm_ram == 8192
    assert cfg.vm_cpu == 4
    assert cfg.registry_url == ""
    assert cfg.tart_home is None


def test_config_uses_mac_name_when_runner_name_is_empty(monkeypatch):
    monkeypatch.setattr("host.runner.default_runner_name", lambda: "jans-macbook-pro-vm")
    env = {
        "GITHUB_AUTH_METHOD": "token",
        "GITHUB_API_TOKEN": "tok",
        "GITHUB_OWNER": "myorg",
        "RUNNER_URL": "https://github.com/myorg",
        "RUNNER_NAME": "",
        "RUNNER_LABELS": "self-hosted,macos",
        "REGISTRY_IMAGE_NAME": "ios-app-ci",
        "VM_USERNAME": "admin",
        "VM_PASSWORD": "admin",
    }

    assert Config.from_env(env).runner_name == "jans-macbook-pro-vm"


def test_sanitize_runner_name():
    assert sanitize_runner_name("Jan’s MacBook Pro (M4)") == "jans-macbook-pro-m4"


def test_xcode_version_label():
    assert xcode_version_label("Xcode 16.4\nBuild version 16F6\n") == "xcode-16.4"
    assert xcode_version_label("Xcode 16.0 beta 2\nBuild version 16A5171c\n") == "xcode-16.0"


def test_xcode_version_label_rejects_unexpected_output():
    with pytest.raises(ValueError, match="could not determine Xcode version"):
        xcode_version_label("xcodebuild: error: SDK not found\n")


def test_with_xcode_version_label_appends_detected_version():
    assert with_xcode_version_label(
        "self-hosted,macos,arm64,tart", "Xcode 16.4\nBuild version 16F6\n"
    ) == "self-hosted,macos,arm64,tart,xcode-16.4"


def test_with_xcode_version_label_avoids_duplicate():
    assert with_xcode_version_label(
        "self-hosted,xcode-16.4", "Xcode 16.4\nBuild version 16F6\n"
    ) == "self-hosted,xcode-16.4"


def test_default_runner_name_falls_back_to_hostname_when_scutil_fails(monkeypatch):
    def fail(*args, **kwargs):
        raise FileNotFoundError

    monkeypatch.setattr("host.runner.subprocess.check_output", fail)
    monkeypatch.setattr("host.runner.socket.gethostname", lambda: "build-host")

    assert default_runner_name() == "build-host-vm"


def test_config_missing_required_raises():
    with pytest.raises(ValueError, match="RUNNER_URL"):
        Config.from_env({
            "GITHUB_AUTH_METHOD": "token",
            "GITHUB_API_TOKEN": "tok",
            "GITHUB_OWNER": "myorg",
            # RUNNER_URL missing
            "RUNNER_NAME": "runner",
            "RUNNER_LABELS": "self-hosted",
            "REGISTRY_IMAGE_NAME": "ios-app-ci",
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
            "REGISTRY_IMAGE_NAME": "ios-app-ci",
            "VM_USERNAME": "admin",
            "VM_PASSWORD": "admin",
        })


def test_drain_false_when_no_sentinel(tmp_path):
    assert is_drain_set(tmp_path / "drain") is False


def test_drain_true_when_sentinel_exists(tmp_path):
    sentinel = tmp_path / "drain"
    sentinel.touch()
    assert is_drain_set(sentinel) is True
