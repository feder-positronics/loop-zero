"""pytest bootstrap: make ``src/`` importable without an install."""

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

os.environ.setdefault("LOOPZERO_ENV_PREFIX", "INTELFLO")

sys.dont_write_bytecode = True
REPO = Path(__file__).resolve().parents[1]
SRC = REPO / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

REVISION = "0123456789abcdef0123456789abcdef01234567"
GIT_ENV = {"PATH": "/usr/bin:/bin", "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1"}


def git(root: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", "-C", str(root), "-c", "user.name=t", "-c", "user.email=t@example.invalid", *args],
        check=True,
        capture_output=True,
        text=True,
        env=GIT_ENV,
    )
    return proc.stdout


def minimal_workflow(
    revision: str = REVISION, extra: str = "", *, include_identity: bool = True
) -> str:
    identity = (
        '[package]\nproduct_name = "Test Consumer"\n'
        'commit_identity = "test-only commit identity"\n\n'
        if include_identity
        else ""
    )
    return (
        'profiles = ["python"]\n\n'
        "[core]\n"
        'repository = "https://github.com/feder-positronics/loop-zero"\n'
        f'revision = "{revision}"\n'
        'path = "vendor/loop-zero"\n\n'
        + identity
        + "[checks]\n"
        'required = ["tests"]\n'
        'advisory = []\n'
        'scheduled = ["health"]\n\n'
        "[hooks]\n"
        'worktree_setup = ["make setup"]\n'
        'acceptance = ["make test"]\n'
        + extra
    )


@pytest.fixture(autouse=True)
def private_gh_home_under_tmp(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Keep the per-command private gh home out of the real account state root.

    The CI sandbox binds the account home read-only, and unit tests must never
    write under ``~/.local/state`` anyway.
    """
    state_root = tmp_path / "account-state"
    monkeypatch.setattr(
        "loopzero.integrations.github._account_state_root", lambda: state_root
    )
    return state_root


@pytest.fixture
def consumer(tmp_path: Path) -> Path:
    """A consumer repository with a deposited snapshot and a committed workflow.toml."""
    root = tmp_path / "consumer"
    root.mkdir()
    shutil.copytree(REPO / "core", root / "vendor" / "loop-zero")
    (root / "workflow.toml").write_text(minimal_workflow(), encoding="utf-8")
    (root / "AGENTS.md").write_text("# Consumer rules\n\nKeep these.\n", encoding="utf-8")
    git(root, "init", "-q", "-b", "main")
    git(root, "add", ".")
    git(root, "-c", "core.hooksPath=/dev/null", "commit", "-qm", "base")
    return root
