"""Shared fixtures: a throwaway git repo and a PATH directory for fake executables."""

from __future__ import annotations

import os
import subprocess
from collections.abc import Callable
from pathlib import Path

import pytest

GIT_ENV = {"GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1"}
for _role in ("AUTHOR", "COMMITTER"):
    GIT_ENV |= {f"GIT_{_role}_NAME": "test", f"GIT_{_role}_EMAIL": "test@example.com"}


def git(repo: Path, *args: str) -> str:
    """Run git in `repo` with a deterministic identity; return stdout."""
    env = {**os.environ, **GIT_ENV}
    return subprocess.run(
        ["git", *args], cwd=repo, env=env, capture_output=True, text=True, check=True
    ).stdout


@pytest.fixture
def git_repo(tmp_path: Path) -> Path:
    """A fresh repository on branch `main` with a single commit of README.md."""
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    (repo / "README.md").write_text("hello\n")
    git(repo, "add", "README.md")
    git(repo, "commit", "-q", "-m", "init")
    return repo


@pytest.fixture
def fake_bin(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A directory prepended to PATH; drop executable scripts here to shadow tools."""
    directory = tmp_path / "bin"
    directory.mkdir()
    monkeypatch.setenv("PATH", f"{directory}{os.pathsep}{os.environ.get('PATH', '')}")
    return directory


@pytest.fixture
def fake_tool(fake_bin: Path) -> Callable[[str, str], Path]:
    """Write an executable script named `name` with `body` into `fake_bin`."""

    def make(name: str, body: str) -> Path:
        path = fake_bin / name
        path.write_text(body if body.startswith("#!") else "#!/bin/sh\n" + body)
        path.chmod(0o755)
        return path

    return make
