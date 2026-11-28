"""Shared fixtures: a throwaway git repo and a PATH directory for fake executables."""

from __future__ import annotations

import os
import textwrap
import subprocess
from collections.abc import Callable
from pathlib import Path
from shutil import copytree

import pytest

from loopzero import _proc, github

GIT_ENV = {
    "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "maintenance.auto", "GIT_CONFIG_VALUE_0": "false",
}


def isolate_github_auth(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("GH_TOKEN", "GITHUB_TOKEN"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(github, "APP_TOKEN_HELPER", tmp_path / "absent-app-token")
    monkeypatch.setattr(github, "_app_token_cache", ("", 0.0))
    monkeypatch.setattr(github, "_logins", {})
    monkeypatch.setattr(github, "_etag_cache", {})


@pytest.fixture(autouse=True)
def isolated_reviewer_home(tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch) -> None:
    """Give tests disposable user and reviewer state outside their checkouts."""
    home = tmp_path_factory.mktemp("test-home")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("CODEX_HOME", str(home / ".codex"))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(home / ".claude"))
    for name in ("OPENAI_API_KEY", "ANTHROPIC_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN", "CLAUDE_SECURESTORAGE_CONFIG_DIR"):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture(autouse=True)
def no_host_github_credentials(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    isolate_github_auth(tmp_path, monkeypatch)
    monkeypatch.setattr(github, "PR_LOCK_DIR", tmp_path / "pr-locks")


for _role in ("AUTHOR", "COMMITTER"):
    GIT_ENV |= {f"GIT_{_role}_NAME": "test", f"GIT_{_role}_EMAIL": "test@example.com"}


def git_environment() -> dict[str, str]:
    return _proc.build_env(("PATH", "LANG", "LC_ALL"), GIT_ENV)


def git(repo: Path, *args: str) -> str:
    """Run git in `repo` with a deterministic identity; return stdout."""
    return subprocess.run(
        ["git", *args], cwd=repo, env=git_environment(), capture_output=True, text=True, check=True
    ).stdout


def commit_file(repo: Path, name: str, text: str, message: str) -> None:
    (repo / name).write_text(text)
    git(repo, "add", "--", name)
    git(repo, "commit", "-qm", message)


def init_git_repo(repo: Path) -> Path:
    """Initialize branch `main` with a single commit of README.md."""
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    git(repo, "config", "maintenance.auto", "false")
    commit_file(repo, "README.md", "hello\n", "init")
    return repo


@pytest.fixture(scope="session")
def git_repo_seed(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return init_git_repo(tmp_path_factory.mktemp("git-repo-seed") / "repo")


@pytest.fixture
def git_repo(tmp_path: Path, git_repo_seed: Path) -> Path:
    return copytree(git_repo_seed, tmp_path / "repo")


@pytest.fixture
def fake_bin(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A directory prepended to PATH; drop executable scripts here to shadow tools."""
    directory = tmp_path / "bin"
    directory.mkdir()
    monkeypatch.setenv("PATH", f"{directory}{os.pathsep}{os.environ.get('PATH', '')}")
    return directory


def bwrap_argv(log: Path) -> list[str]:
    return log.read_text().splitlines()


def review_summary(head, kind="primary", *, verdict="approve", family="claude"):
    return (f"loopzero {kind} review by {family} on `{head}`, unknown: "
            f"**{verdict}** (0 critical, 0 important, 0 suggestion)")


def write_fake_tool(directory: Path, name: str, body: str) -> Path:
    path = directory / name
    body = textwrap.dedent(body)
    path.write_text(body if body.startswith("#!") else "#!/bin/sh\n" + body)
    path.chmod(0o755)
    return path


@pytest.fixture
def fake_tool(fake_bin: Path) -> Callable[[str, str], Path]:
    return lambda name, body: write_fake_tool(fake_bin, name, body)


@pytest.fixture
def app_token_helper(fake_tool, monkeypatch):
    def install(body="echo synthetic-app-token\n"):
        monkeypatch.setattr(github, "APP_TOKEN_HELPER", fake_tool("app-token", body))

    return install
