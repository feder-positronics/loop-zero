"""worktree.py against real temporary repositories with a bare `origin`."""

from __future__ import annotations

from pathlib import Path

import pytest

from loopzero import worktree
from tests.conftest import git


@pytest.fixture
def repo(git_repo: Path, tmp_path: Path) -> Path:
    """`git_repo` wired to a bare origin that already holds `main`."""
    origin = tmp_path / "origin.git"
    git(git_repo, "init", "-q", "--bare", "-b", "main", str(origin))
    git(git_repo, "remote", "add", "origin", str(origin))
    git(git_repo, "push", "-q", "origin", "main")
    return git_repo


def commit_on_origin(repo: Path, name: str) -> str:
    """Advance origin/main from a scratch clone so `start` has to fetch to see it."""
    clone = repo.parent / f"clone-{name}"
    git(repo, "clone", "-q", str(repo.parent / "origin.git"), str(clone))
    (clone / name).write_text(name)
    git(clone, "add", name)
    git(clone, "commit", "-q", "-m", name)
    git(clone, "push", "-q", "origin", "main")
    return git(clone, "rev-parse", "HEAD").strip()


def test_start_creates_branch_worktree_and_task_file(repo: Path) -> None:
    upstream = commit_on_origin(repo, "feature.txt")
    wt = worktree.start(repo, "my-task", "main")
    assert wt == repo / ".worktrees" / "my-task"
    assert (wt / "feature.txt").exists(), "fetched origin/main before branching"
    assert worktree.branch(wt) == "lz/my-task"
    assert worktree.head(wt) == upstream
    assert worktree.base_sha(wt) == upstream
    text = worktree.task_text(wt)
    assert text.startswith("# My task\n")
    headings = [ln for ln in text.splitlines() if ln.startswith("## ")]
    assert headings == ["## Objective", "## Acceptance", "## Base", "## Checks", "## Review",
                        "## Notes"], "matches core/HANDOFF.md"
    base_section = text.split("## Base\n", 1)[1].split("\n## ", 1)[0]
    assert base_section.strip().splitlines() == [f"main @ {upstream}", f"Base: {upstream}"]
    assert "lz/my-task" in git(repo, "branch", "--list", "lz/my-task")


def test_start_excludes_worktrees_and_task_dir(repo: Path) -> None:
    exclude = repo / ".git" / "info" / "exclude"
    exclude.parent.mkdir(parents=True, exist_ok=True)
    exclude.write_text("*.log")  # no trailing newline on purpose
    wt = worktree.start(repo, "t1", "main")
    lines = exclude.read_text().splitlines()
    assert lines == ["*.log", ".worktrees/", ".loopzero/"]
    assert not worktree.is_dirty(wt), "fresh worktree with task.md is clean"
    assert git(repo, "status", "--porcelain") == ""
    worktree.start(repo, "t2", "main")
    assert exclude.read_text().count(".worktrees/") == 1


def test_start_refuses_existing_branch(repo: Path) -> None:
    git(repo, "branch", "lz/dup")
    with pytest.raises(worktree.WorktreeError, match="branch already exists"):
        worktree.start(repo, "dup", "main")
    assert not (repo / ".worktrees" / "dup").exists()


def test_start_refuses_existing_directory(repo: Path) -> None:
    (repo / ".worktrees" / "dup").mkdir(parents=True)
    with pytest.raises(worktree.WorktreeError, match="directory already exists"):
        worktree.start(repo, "dup", "main")


@pytest.mark.parametrize("slug", ["../escape", "a/b", "bad..name", "trail.lock", "sp ace", ""])
def test_start_rejects_bad_slug(repo: Path, slug: str) -> None:
    with pytest.raises(worktree.WorktreeError, match="invalid slug"):
        worktree.start(repo, slug, "main")
    assert not (repo / ".worktrees").exists(), "rejected before fetch or worktree add"


@pytest.mark.parametrize("slug,title", [
    ("add-workflow-config-note", "Add workflow config note"),
    ("fix_bug--123", "Fix bug 123"),
    ("x", "X"),
])
def test_title_from_slug(slug: str, title: str) -> None:
    assert worktree.title_from_slug(slug) == title


def test_branch_name_uses_git_check_ref_format(repo: Path) -> None:
    assert worktree.branch_name(repo, "ok-1.2_x") == "lz/ok-1.2_x"
    with pytest.raises(worktree.WorktreeError):
        worktree.branch_name(repo, "has~tilde")


def test_start_reports_git_failure_with_command(repo: Path) -> None:
    with pytest.raises(worktree.GitError) as info:
        worktree.start(repo, "nobase", "does-not-exist")
    assert info.value.command[:2] == ("git", "fetch")
    assert info.value.tail


def test_dirty_head_and_diff(repo: Path) -> None:
    wt = worktree.start(repo, "work", "main")
    base = worktree.base_sha(wt)
    assert not worktree.is_dirty(wt)
    (wt / "new.py").write_text("print(1)\n")
    assert worktree.is_dirty(wt), "untracked files count as dirty"
    git(wt, "add", "new.py")
    git(wt, "commit", "-q", "-m", "add new")
    assert not worktree.is_dirty(wt)
    assert worktree.head(wt) != base
    diff = worktree.diff_since(wt, base)
    assert "+print(1)" in diff and "new.py" in diff
    (wt / "new.py").write_text("print(2)\n")
    assert "print(2)" not in worktree.diff_since(wt, base), "diff covers commits only"
    assert worktree.is_dirty(wt)


def test_task_helpers_errors(repo: Path) -> None:
    wt = worktree.start(repo, "tk", "main")
    worktree.task_file(wt).write_text("# no base line\n")
    with pytest.raises(worktree.WorktreeError, match="Base:"):
        worktree.base_sha(wt)
    worktree.task_file(wt).unlink()
    with pytest.raises(worktree.WorktreeError, match="missing"):
        worktree.task_text(wt)


def test_cleanup_removes_worktree_and_branch(repo: Path) -> None:
    wt = worktree.start(repo, "done", "main")
    worktree.cleanup(repo, wt)
    assert not wt.exists()
    assert git(repo, "branch", "--list", "lz/done") == ""
    assert "done" not in git(repo, "worktree", "list")


def test_cleanup_refuses_dirty(repo: Path) -> None:
    wt = worktree.start(repo, "dirty", "main")
    (wt / "scratch.txt").write_text("x")
    with pytest.raises(worktree.WorktreeError, match="dirty"):
        worktree.cleanup(repo, wt)
    assert wt.exists() and "lz/dirty" in git(repo, "branch", "--list", "lz/dirty")


def test_cleanup_missing_worktree(repo: Path) -> None:
    with pytest.raises(worktree.WorktreeError, match="no such worktree"):
        worktree.cleanup(repo, repo / ".worktrees" / "ghost")
