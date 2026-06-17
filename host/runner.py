#!/usr/bin/env python3
"""Tart CI runner — replaces vendor/ekiden/host/launch.sh."""
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
