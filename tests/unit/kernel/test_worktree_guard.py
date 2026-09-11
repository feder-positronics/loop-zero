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


def test_successful_handoff_transfers_live_lease_to_inherited_child(
    git_repo: Path,
) -> None:
    child = None
    with module.worktree_lease(
        git_repo,
        boundary="handoff",
        timeout_s=0.1,
        transfer_on_success=True,
    ) as lease:
        child = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(2)"],
            pass_fds=lease.pass_fds,
        )

    try:
        with pytest.raises(module.LeaseTimeoutError):
            with module.worktree_lease(git_repo, boundary="contender", timeout_s=0.05):
                raise AssertionError("transferred lease must remain live")
    finally:
        assert child is not None
        child.terminate()
        child.wait(timeout=5)

    with module.worktree_lease(git_repo, boundary="after-child", timeout_s=0.1):
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


def test_nested_caller_cannot_transfer_ancestor_lease(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with module.worktree_lease(git_repo, boundary="parent", timeout_s=0.1) as lease:
        for name, value in lease.child_env().items():
            monkeypatch.setenv(name, value)
        with pytest.raises(module.WorktreeGuardError, match="cannot transfer"):
            with module.worktree_lease(
                git_repo,
                boundary="nested-handoff",
                timeout_s=0.1,
                transfer_on_success=True,
            ):
                raise AssertionError("nested transfer must not enter")


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


# --- guard-commit (#3418 P4 single-writer commit boundary) ------------------


SCRIPT_PATH = (
    Path(__file__).resolve().parents[4] / "scripts" / "util" / "worktree_guard.py"
)


def _init_guard_repo(path: Path) -> Path:
    path.mkdir()
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    (path / "f.txt").write_text("x\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(path), "add", "f.txt"], check=True)
    return path


def _guard_commit(
    repo: Path, *, env: dict[str, str] | None = None
) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(SCRIPT_PATH), "guard-commit", "--worktree", str(repo)],
        capture_output=True,
        env=env,
        text=True,
        timeout=30,
    )


def test_guard_commit_allows_free_worktree(tmp_path: Path) -> None:
    repo = _init_guard_repo(tmp_path / "free-repo")
    result = _guard_commit(repo)
    assert result.returncode == 0, result.stderr


def test_guard_commit_allows_owned_descendant_after_fd_is_closed(
    tmp_path: Path,
) -> None:
    repo = _init_guard_repo(tmp_path / "owned-descendant-repo")
    with module.worktree_lease(repo, boundary="commit-autofix", timeout_s=0.1) as lease:
        environment = {**os.environ, **lease.child_env()}
        # Model uv/pre-commit closing every non-standard descriptor while
        # preserving the owned process environment.
        completed = subprocess.run(
            [
                sys.executable,
                str(SCRIPT_PATH),
                "guard-commit",
                "--worktree",
                str(repo),
            ],
            check=False,
            capture_output=True,
            text=True,
            env=environment,
            close_fds=True,
        )

    assert completed.returncode == 0, completed.stderr


def test_guard_commit_allows_owned_descendant_after_descriptor_closes_twice(
    tmp_path: Path,
) -> None:
    repo = _init_guard_repo(tmp_path / "owned-grandchild-repo")
    descendant = (
        "import subprocess, sys; "
        "result = subprocess.run(sys.argv[1:], close_fds=True); "
        "raise SystemExit(result.returncode)"
    )

    with module.worktree_lease(repo, boundary="commit-autofix", timeout_s=0.1) as lease:
        lease_environment = lease.child_env()
        assert module.LEASE_NONCE_ENV in lease_environment
        result = subprocess.run(
            [
                sys.executable,
                "-c",
                descendant,
                sys.executable,
                str(SCRIPT_PATH),
                "guard-commit",
                "--worktree",
                str(repo),
            ],
            capture_output=True,
            text=True,
            env={**os.environ, **lease_environment},
            pass_fds=lease.pass_fds,
            timeout=30,
        )

    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("inherited_nonce", [None, "wrong-nonce"])
def test_guard_commit_blocks_owned_descendant_without_matching_nonce(
    tmp_path: Path,
    inherited_nonce: str | None,
) -> None:
    repo = _init_guard_repo(tmp_path / "unproven-descendant-repo")

    with module.worktree_lease(repo, boundary="commit-autofix", timeout_s=0.1) as lease:
        environment = {**os.environ, **lease.child_env()}
        if inherited_nonce is None:
            environment.pop(module.LEASE_NONCE_ENV, None)
        else:
            environment[module.LEASE_NONCE_ENV] = inherited_nonce
        result = subprocess.run(
            [
                sys.executable,
                str(SCRIPT_PATH),
                "guard-commit",
                "--worktree",
                str(repo),
            ],
            check=False,
            capture_output=True,
            text=True,
            env=environment,
            close_fds=True,
            timeout=30,
        )

    assert result.returncode == 1, result.stderr
    assert "writer lease held" in result.stderr


def test_guard_commit_rejects_mismatched_descendant_identity(
    tmp_path: Path,
) -> None:
    repo = _init_guard_repo(tmp_path / "wrong-descendant-repo")
    with module.worktree_lease(repo, boundary="foreign", timeout_s=0.1) as lease:
        environment = {
            **os.environ,
            **lease.child_env(),
            module.LEASE_NONCE_ENV: "wrong-nonce",
        }
        completed = subprocess.run(
            [
                sys.executable,
                str(SCRIPT_PATH),
                "guard-commit",
                "--worktree",
                str(repo),
            ],
            check=False,
            capture_output=True,
            text=True,
            env=environment,
            close_fds=True,
        )

    assert completed.returncode == 1
    assert "writer lease held" in completed.stderr


def test_guard_commit_blocks_foreign_live_writer_then_releases(
    tmp_path: Path,
) -> None:
    import time

    repo = _init_guard_repo(tmp_path / "held-repo")
    owner_env_path = tmp_path / "owner-env"
    publish_owner_env = (
        "import os, pathlib, sys, time; "
        "pathlib.Path(sys.argv[1]).write_text("
        "os.environ['INTELFLO_WORKTREE_LEASE_FD'] + '\\n' + "
        "os.environ['INTELFLO_WORKTREE_LEASE_NONCE']); "
        "time.sleep(20)"
    )
    holder = subprocess.Popen(
        [
            sys.executable,
            str(SCRIPT_PATH),
            "exec",
            "--worktree",
            str(repo),
            "--boundary",
            "test-writer",
            "--",
            sys.executable,
            "-c",
            publish_owner_env,
            str(owner_env_path),
        ]
    )
    try:
        deadline = time.time() + 10
        while time.time() < deadline and not owner_env_path.exists():
            time.sleep(0.2)
        assert owner_env_path.exists(), "writer never published its lease environment"
        lease_fd, lease_nonce = owner_env_path.read_text(encoding="utf-8").splitlines()
        blocked = _guard_commit(
            repo,
            env={
                **os.environ,
                module.LEASE_FD_ENV: lease_fd,
                module.LEASE_NONCE_ENV: lease_nonce,
            },
        )
        assert blocked.returncode == 1
        assert "writer lease held" in blocked.stderr
        assert "test-writer" in blocked.stderr
    finally:
        holder.terminate()
        holder.wait(timeout=10)
    # Kernel releases the flock with the dead writer: commits flow again —
    # a crashed writer never wedges the worktree.
    assert _guard_commit(repo).returncode == 0


@pytest.mark.parametrize("inherit_fd", [False, True])
def test_check_inherited_requires_live_descriptor(
    git_repo: Path, inherit_fd: bool
) -> None:
    with module.worktree_lease(git_repo, boundary="parent", timeout_s=0.1) as lease:
        result = subprocess.run(
            [
                sys.executable,
                str(Path(module.__file__).resolve()),
                "check-inherited",
                "--worktree",
                str(git_repo),
            ],
            env={
                **os.environ,
                **lease.child_env(),
                module.LEASE_BOUNDARY_ENV: "commit-autofix",
            },
            pass_fds=lease.pass_fds if inherit_fd else (),
            capture_output=True,
            text=True,
            timeout=5,
        )
        assert result.returncode == (0 if inherit_fd else 1)
        with pytest.raises(module.LeaseTimeoutError):
            with module.worktree_lease(git_repo, boundary="contender", timeout_s=0.01):
                raise AssertionError(
                    "inherited check must not release the parent's lock"
                )
