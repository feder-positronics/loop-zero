"""worktree.py against real temporary repositories with a bare `origin`."""

from __future__ import annotations

import shlex
import signal
import subprocess
import sys
from pathlib import Path

import pytest

from loopzero import worktree
from tests.conftest import git


@pytest.fixture
def repo(git_repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """`git_repo` wired to a bare origin that already holds `main`."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home / ".config"))
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


def test_branch_preserves_unicode_whitespace(repo: Path) -> None:
    name = worktree.branch_name(repo, "zażółć\u00a0")
    git(repo, "switch", "-c", name)

    assert worktree.branch(repo) == "lz/zażółć\u00a0"


def test_branch_refuses_detached_head(repo: Path) -> None:
    git(repo, "switch", "--detach")

    with pytest.raises(worktree.WorktreeError):
        worktree.branch(repo)


@pytest.mark.parametrize("branch", [None, "feature/ci+release"])
def test_start_creates_branch_worktree_and_task_file(repo: Path, branch: str | None, monkeypatch) -> None:
    side = branch or "side"
    git(repo, "push", "-q", "origin", f"HEAD:refs/heads/{side}")
    upstream = commit_on_origin(repo, "feature.txt")
    if branch:
        checkout = repo.parent / "single-branch"
        origin = (repo.parent / "origin.git").as_uri()
        git(repo, "clone", "-q", "--single-branch", "--branch", branch, origin, str(checkout))
        assert not git(checkout, "for-each-ref", "refs/heads/main", "refs/remotes/origin/main").strip()
        repo = checkout
    invoke = worktree._git
    def interleave(path, *args, **kwargs):
        result = invoke(path, *args, **kwargs)
        if args[0] == "fetch":
            git(path, "fetch", "-q", "origin", f"refs/heads/{side}")
        return result

    monkeypatch.setattr(worktree, "_git", interleave)
    wt = worktree.start(repo, "my-task", "main")
    assert wt == repo / ".worktrees" / "my-task"
    assert (wt / "feature.txt").exists(), "fetched origin/main before branching"
    assert worktree.branch(wt) == "lz/my-task"
    assert worktree.head(wt) == upstream
    assert worktree.base_sha(wt) == upstream
    text = worktree.task_text(wt)
    assert text.startswith("# My task\n")
    headings = [ln for ln in text.splitlines() if ln.startswith("## ")]
    assert headings == ["## Context and goal", "## Acceptance", "## Notes", "## Base",
                        "## Validation", "## Review"], "matches core/HANDOFF.md"
    assert "- **Context:** <plain newcomer context; explain domain terms from read sources or describe what they do instead; rulings and dispatch belong in Notes>" in text
    assert "- **Problem:** <one or two plain sentences before code names: what goes wrong (one problem only), who notices, and evidence/cost; refs in brackets at the end>" in text
    assert "- **Goal:** <one plain sentence: what the owner or users now notice (and cost/saving if known), because this change fixes the problem>" in text
    prompts = [ln.split(":**", 1)[0] for ln in text.splitlines() if ln.startswith("- **")]
    assert prompts == ["- **Goal", "- **Problem", "- **Context"], "owner-facing goal leads"
    assert "Refs #<issue number, or remove this line>" in text
    base_section = text.split("## Base\n", 1)[1].split("\n## ", 1)[0]
    assert base_section.strip().splitlines() == [f"main @ {upstream}", f"Base: {upstream}"]
    assert "lz/my-task" in git(repo, "branch", "--list", "lz/my-task")


@pytest.mark.parametrize("interruption", ["sigint", "timeout"])
def test_start_retries_after_interruption_during_git_checkout(repo, tmp_path, interruption):
    repo = repo.rename(repo.with_name("repo\r"))
    (repo / ".gitattributes").write_text("README.md filter=interrupt\n")
    git(repo, "add", ".gitattributes")
    git(repo, "commit", "-qm", "apply smudge filter")
    git(repo, "push", "-q", "origin", "main")
    pid_file, entered = tmp_path / "starter.pid", tmp_path / "checkout-entered"
    smudge = tmp_path / "smudge"
    interrupt = f'kill -INT "$(cat {shlex.quote(str(pid_file))})"\n' if interruption == "sigint" else ""
    smudge.write_text(
        f"#!/bin/sh\ncat >/dev/null\n: > {shlex.quote(str(entered))}\n{interrupt}exec sleep 30\n"
    )
    smudge.chmod(0o700)
    git(repo, "config", "filter.interrupt.smudge", shlex.quote(str(smudge)))
    slug = "interrupted\u00a0"
    start = """
import os, sys
from pathlib import Path
from loopzero import worktree
Path(sys.argv[1]).write_text(str(os.getpid()))
git = worktree._git
def git_with_short_checkout_timeout(cwd, *args, **kwargs):
    if args[:2] == ("worktree", "add"):
        kwargs["timeout"] = 3
    return git(cwd, *args, **kwargs)
if sys.argv[2] == "timeout":
    worktree._git = git_with_short_checkout_timeout
worktree.start(Path.cwd(), sys.argv[3], "main")
"""
    command = [sys.executable, "-c", start, str(pid_file), interruption, slug]
    stopped = subprocess.run(command, cwd=repo, timeout=10, capture_output=True, text=True)
    assert entered.is_file()
    assert stopped.returncode == (-signal.SIGINT if interruption == "sigint" else 1)
    if interruption == "timeout":
        assert "timed out" in stopped.stderr
    git(repo, "config", "--unset", "filter.interrupt.smudge")
    wt = worktree.start(repo, slug, "main")
    assert (wt / "README.md").read_text() == "hello\n"
    assert worktree.task_file(wt).is_file() and not worktree.is_dirty(wt)


@pytest.mark.parametrize("state", ["same-base", "dirty", "moved-base"])
def test_start_retries_after_git_creates_worktree(repo, monkeypatch, state):
    base = commit_on_origin(repo, "base-change")
    wt = repo / ".worktrees" / "my-task"
    run_git = worktree._git

    def interrupt_after_add(cwd, *args, **kwargs):
        output = run_git(cwd, *args, **kwargs)
        if args[:2] == ("worktree", "add"):
            raise KeyboardInterrupt
        return output

    with monkeypatch.context() as patch:
        patch.setattr(worktree, "_git", interrupt_after_add)
        with pytest.raises(KeyboardInterrupt):
            worktree.start(repo, "my-task", "main")
    task = worktree.task_file(wt)
    assert worktree.branch(wt) == "lz/my-task" and worktree.head(wt) == base
    assert not task.exists()
    if state == "dirty":
        (wt / "untracked").write_text("work in progress")
        with pytest.raises(worktree.WorktreeError, match="worktree directory already exists"):
            worktree.start(repo, "my-task", "main")
        assert not task.exists()
        return
    if state == "moved-base":
        current_base = commit_on_origin(repo, "next-base")
        with pytest.raises(worktree.WorktreeError) as error:
            worktree.start(repo, "my-task", "main")
        for detail in (base, "origin/main", current_base, "new slug"):
            assert detail in str(error.value)
        assert worktree.branch(wt) == "lz/my-task"
        assert worktree.head(wt) == base
        assert not task.exists()
        return

    assert worktree.start(repo, "my-task", "main") == wt
    assert worktree.base_sha(wt) == base
    assert task.read_text().startswith("# My task\n")
    assert task.read_text().endswith("(filled by `loopzero review`)\n")


def test_start_excludes_worktrees_and_task_dir(repo: Path) -> None:
    exclude = repo / ".git" / "info" / "exclude"
    exclude.parent.mkdir(parents=True, exist_ok=True)
    exclude.write_text("*.log")  # no trailing newline on purpose
    old_inode = exclude.stat().st_ino
    wt = worktree.start(repo, "t1", "main")
    assert exclude.stat().st_ino != old_inode
    lines = exclude.read_text().splitlines()
    assert lines == ["*.log", ".worktrees/", ".loopzero/"]
    assert not worktree.is_dirty(wt), "fresh worktree with task.md is clean"
    assert git(repo, "status", "--porcelain") == ""
    installed = exclude.stat()
    worktree.start(repo, "t2", "main")
    assert (exclude.stat().st_ino, exclude.stat().st_mtime_ns) == (installed.st_ino, installed.st_mtime_ns)
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


@pytest.mark.parametrize("change", [("remove", "origin"), ("rename", "origin", "upstream")])
def test_start_explains_missing_origin(repo: Path, change: tuple[str, ...]) -> None:
    git(repo, "remote", *change)

    with pytest.raises(worktree.WorktreeError, match="no Git remote named 'origin'") as info:
        worktree.start(repo, "my-task", "main")

    assert "git remote add origin <url>" in str(info.value)
    assert "git remote rename <name> origin" in str(info.value)
    assert git(repo, "branch", "--list", "lz/my-task") == ""
    assert not worktree.worktree_dir(repo, "my-task").exists()


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
    assert b"+print(1)" in diff and b"new.py" in diff
    (wt / "new.py").write_text("print(2)\n")
    assert b"print(2)" not in worktree.diff_since(wt, base), "diff covers commits only"
    assert worktree.is_dirty(wt)


def test_task_helpers_errors(repo: Path) -> None:
    wt = worktree.start(repo, "tk", "main")
    worktree.task_file(wt).write_text("# no base line\n")
    with pytest.raises(worktree.WorktreeError, match="Base:"):
        worktree.base_sha(wt)
    worktree.task_file(wt).unlink()
    with pytest.raises(worktree.WorktreeError, match="missing"):
        worktree.task_text(wt)


def test_cleanup_removes_worktree_and_branch(repo: Path, monkeypatch) -> None:
    wt, calls, real = worktree.start(repo, "done", "main"), [], worktree._git
    monkeypatch.setattr(worktree, "_git", lambda d, *a, **k: calls.append((d, a)) or real(d, *a, **k))
    worktree.cleanup(wt)  # #439: no cleanup git runs in (or -C) the primary checkout
    assert calls and all(Path(d).resolve() != repo.resolve() and "-C" not in a for d, a in calls)
    assert not wt.exists() and not git(repo, "branch", "--list", "lz/done") and "done" not in git(repo, "worktree", "list")


def test_cleanup_refuses_dirty(repo: Path) -> None:
    wt = worktree.start(repo, "dirty", "main")
    (wt / "scratch.txt").write_text("x")
    with pytest.raises(worktree.WorktreeError, match="dirty"):
        worktree.cleanup(wt)
    assert wt.exists() and "lz/dirty" in git(repo, "branch", "--list", "lz/dirty")


def test_cleanup_missing_worktree(repo: Path) -> None:
    with pytest.raises(worktree.WorktreeError, match="no such worktree"):
        worktree.cleanup(repo / ".worktrees" / "ghost")


@pytest.mark.parametrize("state", ["clean", "dirty", "divergent", "unpublished", "remote-moved", "stash"])
def test_abandoned_worktree_guide(repo: Path, tmp_path: Path, state: str) -> None:
    original = worktree.start(repo, "abandoned", "main")
    wt = tmp_path / "linked task checkout"
    git(repo, "worktree", "move", str(original), str(wt))
    caller = tmp_path / "caller"
    caller.mkdir()
    git(wt, "commit", "--allow-empty", "-m", "published task")
    head = worktree.head(wt)
    if state != "unpublished":
        git(wt, "push", "-u", "origin", "lz/abandoned")
    if state == "dirty":
        (wt / "untracked.txt").write_text("keep my work")
    if state == "divergent":
        git(wt, "commit", "--allow-empty", "-m", "local follow-up")
    if state == "remote-moved":
        git(repo, "push", "--force", "origin", "main:refs/heads/lz/abandoned")

    if state == "stash":
        (repo / "README.md").write_text("keep my stashed work")
        git(repo, "stash", "push")

    setup = (Path(__file__).resolve().parents[1] / "SETUP.md").read_text()
    _, heading, section = setup.partition("## Removing an abandoned task worktree")
    assert heading, "the guide must cover closed, unmerged tasks outside retention"
    commands = section.split("```sh\n", 1)[1].split("```", 1)[0]
    result = subprocess.run(
        ["sh", "-c", commands, "cleanup-guide", str(wt), "abandoned", head],
        cwd=caller, capture_output=True, text=True, check=False,
    )

    assert (result.returncode == 0) == (state == "clean"), result.stderr
    assert wt.exists() == (state != "clean")
    assert git(repo, "show-ref", "--verify", "refs/heads/lz/abandoned")
    if state == "clean":
        assert git(repo, "ls-remote", "origin", "refs/heads/lz/abandoned").split() == [
            head, "refs/heads/lz/abandoned"
        ]
