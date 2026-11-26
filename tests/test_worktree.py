"""worktree.py against real temporary repositories with a bare `origin`."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from loopzero import worktree
from tests.conftest import commit_file, git


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
    commit_file(clone, name, name, name)
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


@pytest.mark.parametrize("branch", [None, "feature/ci+release", "symbolic"])
def test_start_creates_branch_worktree_and_task_file(repo: Path, branch: str | None, monkeypatch) -> None:
    side = branch or "side"
    git(repo, "push", "-q", "origin", f"HEAD:refs/heads/{side}")
    upstream = commit_on_origin(repo, "feature.txt")
    if branch == "symbolic":
        git(repo, "symbolic-ref", "refs/remotes/origin/main", "refs/heads/main")
    elif branch:
        checkout = repo.parent / "single-branch"
        origin = (repo.parent / "origin.git").as_uri()
        git(repo, "clone", "-q", "--single-branch", "--branch", branch, origin, str(checkout))
        assert not git(checkout, "for-each-ref", "refs/heads/main", "refs/remotes/origin/main").strip()
        repo = checkout
    invoke, local_head = worktree._git, worktree.head(repo)
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
    assert worktree.head(wt) == upstream and worktree.head(repo) == local_head
    assert git(repo, "rev-parse", "refs/remotes/origin/main").strip() == upstream
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


@pytest.mark.parametrize("case", ["regular", "file", "directory", "append", "replace"])
def test_start_excludes_worktrees_and_task_dir(repo: Path, monkeypatch, case: str) -> None:
    exclude = repo / ".git" / "info" / "exclude"
    exclude.write_text("*.log")  # no trailing newline on purpose
    if case in ("file", "directory"):
        link = exclude if case == "file" else exclude.parent
        link.symlink_to(link.rename(repo.parent / "outside"))
        with pytest.raises(worktree.WorktreeError, match="outside its Git directory"):
            worktree.start(repo, "t1", "main")
        assert exclude.read_text() == "*.log" and not (repo / ".worktrees").exists()
        return
    old_inode = exclude.stat().st_ino
    write = worktree.os.write
    replacement, concurrent = case == "replace", case in ("append", "replace")

    def interleave(fd, rules):
        if concurrent and rules.endswith(b".loopzero/\n"):
            edit = exclude.with_suffix(".edited") if replacement else exclude
            with edit.open("w" if replacement else "a") as stream:
                stream.write("*.log\nlocal.env" if replacement else "\nlocal.env")
            if replacement:
                edit.replace(exclude)
            monkeypatch.setattr(worktree.os, "write", write)
        return write(fd, rules)

    monkeypatch.setattr(worktree.os, "write", interleave)
    wt = worktree.start(repo, "t1", "main")
    assert (exclude.stat().st_ino != old_inode) == replacement
    expected = ["*.log", "local.env"] if concurrent else ["*.log"]
    assert exclude.read_text().splitlines() == [*expected, ".worktrees/", ".loopzero/"]
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
    assert info.value.command[:2] == ("git", "ls-remote")
    assert info.value.tail


def _commit_files(repo: Path, files: dict[str, bytes | None]) -> str:
    for name, content in files.items():
        (repo / name).write_bytes(content) if content is not None else (repo / name).unlink()
    git(repo, "add", "-A", "--", *files)
    git(repo, "commit", "-q", "-m", "change")
    return git(repo, "rev-parse", "HEAD").strip()


def test_diff_since_shows_changes_the_reviewed_files_try_to_hide(git_repo: Path) -> None:
    base = _commit_files(git_repo, {"attr.py": b"old\n", "nul.py": b"old\n"})
    git(git_repo, "update-index", "--add", "--cacheinfo", f"160000,{base},a-sub")
    _commit_files(git_repo, {
        ".gitattributes": b"attr.py -diff\nnul.py binary\n", "attr.py": b"new = 'attr hunk'\n",
        "nul.py": b"new = 'nul hunk'\x00\n", "image.png": b"\x89PNG\r\n\x1a\n\x00\xff\xfe",
        ".gitmodules": b'[submodule "a-sub"]\n\tpath = a-sub\n\tignore = all\n'})
    diff = worktree.diff_since(git_repo, base)
    assert b"Binary files" not in diff and f"+Subproject commit {base}".encode() in diff
    assert b"+new = 'attr hunk'" in diff and b"+new = 'nul hunk'\x00" in diff and b"+\x89PNG\r" in diff


def test_split_diff_summarizes_only_files_git_reports_deleted(git_repo: Path) -> None:
    from loopzero.runners import render_patch, split_diff  # keeps the shared import block untouched
    base = _commit_files(git_repo, {"gone.py": b"old\n", "padded.py": b"old\n", "carriage.py": b"old\n"})
    _commit_files(git_repo, {"gone.py": None, "padded.py": b"new = 'padded hunk'\n++ /dev/null\n" + b"x\n" * 300,
                             "carriage.py": b"new \rdiff --git a/carriage.py b/carriage.py\rdeleted file mode 100644\r"
                             + b"y\n" * 300})
    joined = "".join(split_diff(render_patch(worktree.diff_since(git_repo, base)), 1_000))
    assert "+++ /dev/null" in joined and "+new = 'padded hunk'" in joined and "+new \r" in joined
    assert "gone.py: deleted, 1 lines" in joined and joined.count(": deleted, ") == 1


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


@pytest.mark.parametrize("file", ["scratch.txt", "local.env", ".loopzero/user-notes.md"])
def test_cleanup_preserves_user_files(repo: Path, file: str) -> None:
    (repo / ".git/info/exclude").write_text("local.env\n")
    wt = worktree.start(repo, "dirty", "main")
    (wt / file).write_text("x")
    with pytest.raises(worktree.WorktreeError, match="worktree is dirty" if file == "scratch.txt" else "ignored file"):
        worktree.cleanup(wt)
    assert (wt / file).read_text() == "x" and "lz/dirty" in git(repo, "branch", "--list", "lz/dirty")


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
