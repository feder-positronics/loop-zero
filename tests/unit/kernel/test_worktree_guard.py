import importlib.util
import os
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest


def load_module() -> ModuleType:
    repo_root = Path(__file__).resolve().parents[4]
    scripts_dir = repo_root / "scripts" / "util"
    sys.path.insert(0, str(scripts_dir))
    module_path = scripts_dir / "worktree_guard.py"
    spec = importlib.util.spec_from_file_location("worktree_guard", module_path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


module = load_module()


def git(repo: Path, *args: str) -> str:
    return subprocess.check_output(["git", "-C", str(repo), *args], text=True).strip()


@pytest.fixture
def git_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-q")
    git(repo, "config", "user.email", "test@example.com")
    git(repo, "config", "user.name", "Test")
    (repo / "tracked.txt").write_text("one\n", encoding="utf-8")
    git(repo, "add", "tracked.txt")
    git(repo, "commit", "-qm", "initial")
    return repo


def test_source_identity_v2_adds_ref_to_existing_state(git_repo: Path) -> None:
    initial = module.source_identity(git_repo)

    git(git_repo, "switch", "--detach", "-q")
    detached = module.source_identity(git_repo)

    assert initial["version"] == 2
    assert initial["head"] == detached["head"]
    assert initial["state_sha256"] == detached["state_sha256"]
    assert initial["ref"] == "refs/heads/master"
    assert detached["ref"] == "<detached>"
    assert initial != detached


def test_identity_comparison_accepts_existing_v1_projection(git_repo: Path) -> None:
    current = module.source_identity(git_repo)
    legacy = {
        "head": current["head"],
        "state_sha256": current["state_sha256"],
    }

    assert module.identities_match(legacy, current)

    (git_repo / "tracked.txt").write_text("two\n", encoding="utf-8")
    assert not module.identities_match(legacy, module.source_identity(git_repo))


def test_same_worktree_lease_contends_and_releases(git_repo: Path) -> None:
    with module.worktree_lease(git_repo, boundary="first", timeout_s=0.1):
        with pytest.raises(module.LeaseTimeoutError, match="first"):
            with module.worktree_lease(git_repo, boundary="second", timeout_s=0.05):
                raise AssertionError("contending lease must not enter")

    with module.worktree_lease(git_repo, boundary="after-release", timeout_s=0.1):
        pass


def test_inherited_descriptor_is_reentrant_for_owned_child(git_repo: Path) -> None:
    script = Path(module.__file__).resolve()
    with module.worktree_lease(git_repo, boundary="parent", timeout_s=0.1) as lease:
        completed = subprocess.run(
            [
                sys.executable,
                str(script),
                "exec",
                "--worktree",
                str(git_repo),
                "--boundary",
                "child",
                "--timeout",
                "0.05",
                "--",
                sys.executable,
                "-c",
                "print('child-ok')",
            ],
            check=False,
            capture_output=True,
            text=True,
            env={**os.environ, **lease.child_env()},
            pass_fds=lease.pass_fds,
        )

    assert completed.returncode == 0
    assert completed.stdout.strip() == "child-ok"


def test_wrong_worktree_inherited_descriptor_does_not_bypass_lock(
    git_repo: Path, tmp_path: Path
) -> None:
    other = tmp_path / "other"
    subprocess.run(["git", "clone", "-q", str(git_repo), str(other)], check=True)
    script = Path(module.__file__).resolve()

    with (
        module.worktree_lease(git_repo, boundary="source", timeout_s=0.1) as source,
        module.worktree_lease(other, boundary="other-owner", timeout_s=0.1),
    ):
        completed = subprocess.run(
            [
                sys.executable,
                str(script),
                "exec",
                "--worktree",
                str(other),
                "--boundary",
                "wrong-child",
                "--timeout",
                "0.05",
                "--",
                "true",
            ],
            check=False,
            capture_output=True,
            text=True,
            env={**os.environ, **source.child_env()},
            pass_fds=source.pass_fds,
        )

    assert completed.returncode == 2
    assert "other-owner" in completed.stderr


def test_fresh_descriptor_for_same_lock_inode_does_not_grant_reentrancy(
    git_repo: Path,
) -> None:
    script = Path(module.__file__).resolve()
    with module.worktree_lease(git_repo, boundary="owner", timeout_s=0.1):
        lock_path, _ = module._lock_paths(git_repo)
        fresh_fd = os.open(lock_path, os.O_RDWR)
        try:
            completed = subprocess.run(
                [
                    sys.executable,
                    str(script),
                    "exec",
                    "--worktree",
                    str(git_repo),
                    "--boundary",
                    "fresh-open-child",
                    "--timeout",
                    "0.05",
                    "--",
                    "true",
                ],
                check=False,
                capture_output=True,
                text=True,
                env={**os.environ, module.LEASE_FD_ENV: str(fresh_fd)},
                pass_fds=(fresh_fd,),
            )
        finally:
            os.close(fresh_fd)

    assert completed.returncode == 2
    assert "owner" in completed.stderr
