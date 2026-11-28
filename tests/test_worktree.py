"""worktree.py against real temporary repositories with a bare `origin`."""

from __future__ import annotations

import errno
import shlex
import signal
import subprocess
import sys
from contextlib import nullcontext
from dataclasses import replace
from functools import partial
from pathlib import Path
from shutil import copytree

import pytest

from loopzero import sandbox, worktree
from tests.conftest import commit_file, git, git_environment, init_git_repo
from tests.test_sandbox import bwrap_log, make_config  # noqa: F401 - pytest fixture
from tests.test_setup import SETUP, setup_commands


@pytest.fixture(scope="session")
def origin_seed(git_repo_seed, tmp_path_factory):
    origin = tmp_path_factory.mktemp("worktree-origin") / "origin.git"
    git(git_repo_seed, "clone", "-q", "--bare", str(git_repo_seed), str(origin))
    git(origin, "config", "receive.autogc", "false")
    return origin


@pytest.fixture
def repo(git_repo: Path, origin_seed: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Private checkout and origin, both seeded with the same main commit."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    origin = copytree(origin_seed, tmp_path / "origin.git")
    git(git_repo, "remote", "add", "origin", str(origin))
    return git_repo


def commit_on_origin(repo: Path, name: str) -> str:
    """Advance origin/main from a scratch clone so `start` has to fetch to see it."""
    clone = repo.parent / f"clone-{name}"
    git(repo, "clone", "-q", str(repo.parent / "origin.git"), str(clone))
    commit_file(clone, name, name, name)
    git(clone, "push", "-q", "origin", "main")
    return worktree.head(clone)


def refused_start(repo, slug, diagnostic):
    with pytest.raises(worktree.WorktreeError, match=diagnostic) as error:
        worktree.start(repo, slug, "main")
    return str(error.value)


@pytest.mark.parametrize("slug", ["zażółć\u00a0", "ok-1.2_x"])
def test_branch_reads_valid_names_and_rejects_detached_head(repo: Path, slug: str) -> None:
    git(repo, "switch", "-c", worktree.branch_name(repo, slug))
    assert worktree.branch(repo) == f"lz/{slug}"
    git(repo, "switch", "--detach")
    with pytest.raises(worktree.WorktreeError):
        worktree.branch(repo)


@pytest.mark.parametrize("branch", [None, "feature/ci+release", "symbolic", "legacy"])
def test_start_creates_branch_worktree_and_task_file(repo: Path, branch: str | None, monkeypatch) -> None:
    side = branch or "side"
    git(repo, "push", "-q", "origin", f"HEAD:refs/heads/{side}")
    upstream = commit_on_origin(repo, "feature.txt")
    if branch == "symbolic":
        git(repo, "symbolic-ref", "refs/remotes/origin/main", "refs/heads/main")
    elif branch not in {None, "legacy"}:
        checkout = repo.parent / "single-branch"
        origin = (repo.parent / "origin.git").as_uri()
        git(repo, "clone", "-q", "--single-branch", "--branch", branch, origin, str(checkout))
        assert not git(checkout, "for-each-ref", "refs/heads/main", "refs/remotes/origin/main").strip()
        repo = checkout
    (repo.parent / "xdg/git").mkdir(parents=True)
    git(repo, "config", "--file", str(repo.parent / "xdg/git/config"), f"url.{repo.parent / 'origin.git'}.insteadOf", "lz-origin")
    git(repo, "remote", "set-url", "origin", "lz-origin")
    invoke, local_head = worktree.run, worktree.head(repo)
    def interleave(argv, **kwargs):
        if branch == "legacy" and argv[:3] == ("git", "fetch", "--porcelain"):
            return invoke(("git", "fetch", "--loopzero-unsupported"), **kwargs)
        result = invoke(argv, **kwargs)
        if argv[1] == "fetch" and result.exit_code == 0:
            git(kwargs["cwd"], "fetch", "-q", str(repo.parent / "origin.git"), f"refs/heads/{side}")
        return result

    monkeypatch.setattr(worktree, "run", interleave)
    wt = worktree.start(repo, "my-task", "main")
    assert wt == repo / ".worktrees" / "my-task"
    assert (wt / "feature.txt").exists(), "fetched origin/main before branching"
    assert worktree.branch(wt) == "lz/my-task"
    assert worktree.head(wt) == upstream and worktree.head(repo) == local_head
    assert git(repo, "rev-parse", "refs/remotes/origin/main").strip() == upstream
    assert worktree.base_sha(wt) == upstream
    text = worktree.task_text(wt)
    lines = text.splitlines()
    assert text.startswith("# My task\n")
    assert [line for line in lines if line.startswith("## ")] == [
        "## Context and goal", "## Acceptance", "## Notes", "## Base", "## Validation", "## Review",
    ]
    assert [line.split(":**", 1)[0] for line in lines if line.startswith("- **")] == [
        "- **Goal", "- **Problem", "- **Context",
    ]
    assert text.split("## Base\n", 1)[1].split("\n## ", 1)[0].strip() == f"main @ {upstream}"


@pytest.mark.parametrize("legacy", [False, True])
def test_start_rejects_origin_response_for_another_ref(repo, monkeypatch, legacy):
    commit_on_origin(repo, "feature.txt")
    run = worktree.run

    def wrong_ref(argv, **kwargs):
        fetching = argv[:3] == ("git", "fetch", "--porcelain")
        if fetching and legacy:
            return run(("git", "fetch", "--loopzero-unsupported"), **kwargs)
        output = run(argv, **kwargs)
        prefix = "/origin/" if fetching else "/heads/" if argv[:2] == ("git", "ls-remote") else None
        return replace(output, stdout=output.stdout.replace(prefix + "main", prefix + "unrelated")) if prefix else output

    monkeypatch.setattr(worktree, "run", wrong_ref)
    with pytest.raises(worktree.GitError, match=r"origin did not report|refs/remotes/origin/unrelated"):
        worktree.start(repo, "wrong-ref", "main")
    assert not worktree.worktree_dir(repo, "wrong-ref").exists()


@pytest.mark.parametrize("state", [
    "attached", "detached", "dirty", "moved-base", "foreign", "backlink", "branch-exists",
    "other-branch", "other-head",
])
def test_start_resumes_only_a_clean_checkout_at_the_expected_identity(repo, monkeypatch, state):
    repo = repo.rename(repo.with_name("repo\rreturn"))
    base = worktree.head(repo)
    wt, branch = worktree.worktree_dir(repo, "my-task"), "lz/my-task"
    mode = ("--detach",) if state in {"detached", "backlink", "branch-exists"} else ("-b", branch)
    git(repo, "worktree", "add", *mode, str(wt))
    if state in {"attached", "detached"}:
        assert worktree.start(repo, "my-task", "main") == wt
        assert (worktree.branch(wt), worktree.base_sha(wt)) == (branch, base)
        return

    invoke = worktree.git
    listing = ("worktree", "list", "--porcelain", "-z")
    registration = invoke(repo, *listing, paths=True)
    expected = "worktree directory already exists"
    if state == "dirty":
        (wt / "untracked").write_text("work in progress")
    elif state == "moved-base":
        upstream = commit_on_origin(repo, "next-base")
        expected = f"existing HEAD {base} does not match origin/main at {upstream}"
    elif state == "foreign":
        foreign = repo.parent / "foreign"
        git(repo, "clone", "-q", str(repo), str(foreign))
        git(foreign, "switch", "-c", branch)
        (wt / ".git").write_text(f"gitdir: {foreign / '.git'}\n")
    elif state == "backlink":
        admin = Path(invoke(wt, "rev-parse", "--absolute-git-dir", paths=True))
        (admin / "gitdir").write_text(f"{repo / '.git'}\n")
    elif state == "branch-exists":
        git(repo, "branch", branch)
    elif state == "other-branch":
        git(wt, "switch", "-c", "other")
    else:
        git(wt, "commit", "--allow-empty", "-m", "advance checkout")
        git(wt, "push", "origin", "HEAD:main")

    # A checkout can change after Git reports its registration.
    monkeypatch.setattr(worktree, "git", lambda cwd, *args, **kw: registration if args == listing else invoke(cwd, *args, **kw))
    refused_start(repo, "my-task", expected)
    assert not worktree.task_file(wt).exists()


@pytest.mark.parametrize("replacement", [False, True], ids=["append", "replace"])
def test_start_excludes_worktrees_and_task_dir(repo, monkeypatch, replacement):
    exclude = repo / ".git" / "info" / "exclude"
    exclude.write_text("*.log")  # no trailing newline on purpose
    old_inode = exclude.stat().st_ino
    write = worktree.os.write

    def interleave(fd, rules):
        if rules.endswith(b".loopzero/\n"):
            if replacement:
                edit = exclude.with_suffix(".edited")
                edit.write_text("*.log\nlocal.env")
                edit.replace(exclude)
            else:
                with exclude.open("a") as stream:
                    stream.write("\nlocal.env")
            monkeypatch.setattr(worktree.os, "write", write)
        return write(fd, rules)

    monkeypatch.setattr(worktree.os, "write", interleave)
    wt = worktree.start(repo, "t1", "main")
    assert (exclude.stat().st_ino != old_inode) == replacement
    assert exclude.read_text().splitlines() == ["*.log", "local.env", ".worktrees/", ".loopzero/"]
    assert not worktree.is_dirty(wt) and not worktree.is_dirty(repo)
    installed = exclude.stat()
    worktree.start(repo, "t2", "main")
    assert (exclude.stat().st_ino, exclude.stat().st_mtime_ns) == (installed.st_ino, installed.st_mtime_ns)


@pytest.mark.parametrize("when", ["file", "directory", "at-open"])
def test_start_refuses_exclude_symlinks(repo, monkeypatch, when):
    exclude = repo / ".git/info/exclude"
    exclude.write_text("*.log")
    outside = repo.parent / "outside"
    if when == "at-open":
        open_file = worktree.os.open

        def swap_at_open(path, *args, **kwargs):
            if path == exclude:
                monkeypatch.setattr(worktree.os, "open", open_file)
                exclude.symlink_to(exclude.rename(outside))
            return open_file(path, *args, **kwargs)

        monkeypatch.setattr(worktree.os, "open", swap_at_open)
        with pytest.raises(OSError) as error:
            worktree.start(repo, "t1", "main")
        assert error.value.errno == errno.ELOOP
        assert outside.read_text() == "*.log"
    else:
        link = exclude if when == "file" else exclude.parent
        link.symlink_to(link.rename(outside))
        refused_start(repo, "t1", "outside its Git directory")
        assert exclude.read_text() == "*.log"
    assert not (repo / ".worktrees").exists()


@pytest.mark.parametrize("collision", ["branch", "directory", "worktree-link", "state-link", "task-link"])
def test_start_refuses_an_existing_task_destination(repo: Path, collision: str) -> None:
    path = repo / ".worktrees" / "dup"
    if collision == "branch":
        git(repo, "branch", "lz/dup")
    elif collision == "directory":
        path.mkdir(parents=True)
    elif collision == "worktree-link":
        path.parent.mkdir()
        path.symlink_to(repo.parent / "missing")
    else:
        (repo / ".git/info/exclude").write_text(".loopzero\n")
        git(repo, "worktree", "add", "--detach", str(path))
        task = worktree.task_file(path)
        link = task.parent if collision == "state-link" else task
        link.parent.mkdir(exist_ok=True)
        link.symlink_to(repo.parent / "missing")
    kind = "branch" if collision == "branch" else "worktree directory"
    refused_start(repo, "dup", f"^{kind} already exists")
    assert bool(git(repo, "branch", "--list", "lz/dup")) == (collision == "branch")


@pytest.mark.parametrize("slug,remote_change", [
    *((slug, ()) for slug in ("a/b", "bad..name", "trail.lock", "")),
    ("my-task", ("remove", "origin")), ("my-task", ("rename", "origin", "upstream")),
])
def test_start_rejects_invalid_slug_or_missing_origin(repo: Path, slug, remote_change) -> None:
    if remote_change:
        git(repo, "remote", *remote_change)
        diagnostic = refused_start(repo, slug, "no Git remote named 'origin'")
        assert "git remote add origin <url>" in diagnostic and "git remote rename <name> origin" in diagnostic
        assert not git(repo, "branch", "--list", "lz/my-task")
        assert not worktree.worktree_dir(repo, slug).exists()
    else:
        refused_start(repo, slug, "invalid slug")
        assert not (repo / ".worktrees").exists()


def test_start_reports_git_failure_with_command(repo: Path) -> None:
    with pytest.raises(worktree.GitError) as info:
        worktree.start(repo, "nobase", "does-not-exist")
    assert info.value.command[0] == "git"
    assert info.value.command[1] in {"fetch", "ls-remote"}
    assert info.value.tail


def _commit_files(repo: Path, files: dict[str, bytes | None]) -> str:
    for name, content in files.items():
        (repo / name).write_bytes(content) if content is not None else (repo / name).unlink()
    git(repo, "add", "-A", "--", *files)
    git(repo, "commit", "-q", "-m", "change")
    return git(repo, "rev-parse", "HEAD").strip()


def test_diff_since_shows_changes_the_reviewed_files_try_to_hide(git_repo: Path, bwrap_log) -> None:  # noqa: F811 - fixture
    from loopzero.runners import render_patch, split_diff

    base = _commit_files(git_repo, dict.fromkeys(("attr.py", "nul.py", "gone.py", "padded.py", "carriage.py"), b"old\n"))
    git(git_repo, "clone", "-q", ".", "a-sub")
    git(git_repo, "update-index", "--add", "--cacheinfo", f"160000,{base},a-sub")
    _commit_files(git_repo, {
        ".gitattributes": b"attr.py -diff\nnul.py binary\n", "attr.py": b"new = 'attr hunk'\n",
        "nul.py": b"new = 'nul hunk'\x00\n", "image.png": b"\x89PNG\r\n\x1a\n\x00\xff\xfe",
        "gone.py": None, "padded.py": b"new = 'padded hunk'\n++ /dev/null\n" + b"x\n" * 300,
        "carriage.py": b"new \rdiff --git a/carriage.py b/carriage.py\rdeleted file mode 100644\r" + b"y\n" * 300,
        ".gitmodules": b'[submodule "a-sub"]\n\tpath = a-sub\n\tignore = dirty\n'})
    assert not worktree.is_dirty(git_repo) and worktree.head(git_repo) != base
    (git_repo / "a-sub/README.md").write_text("uncommitted submodule edit")
    assert worktree.is_dirty(git_repo) and sandbox.run_checks(make_config(scratch=()), git_repo).dirty
    (git_repo / "attr.py").write_text("uncommitted edit")
    assert worktree.is_dirty(git_repo)
    diff = worktree.diff_since(git_repo, base)
    assert b"uncommitted edit" not in diff
    assert b"Binary files" not in diff and f"+Subproject commit {base}".encode() in diff
    assert b"+new = 'attr hunk'" in diff and b"+new = 'nul hunk'\x00" in diff and b"+\x89PNG\r" in diff

    joined = "".join(split_diff(render_patch(diff), 1_000))
    assert "+++ /dev/null" in joined and "+new = 'padded hunk'" in joined and "+new \r" in joined
    assert "gone.py: deleted, 1 lines" in joined and joined.count(": deleted, ") == 1


def test_task_helpers_errors(repo: Path) -> None:
    wt = worktree.start(repo, "tk", "main")
    task = worktree.task_file(wt)
    for text in ("# no base line\n", f"Base: {'b' * 40}\n"):
        task.write_text(text)
        with pytest.raises(worktree.WorktreeError, match="Restore that line in the task file"):
            worktree.base_sha(wt)
    task.unlink()
    with pytest.raises(worktree.WorktreeError, match="missing"):
        worktree.task_text(wt)


def test_cleanup_removes_worktree_and_branch(repo: Path, monkeypatch) -> None:
    (repo / ".git/info/exclude").write_text(".venv/\n")
    wt, calls, real = worktree.start(repo, "done", "main"), [], worktree.git
    monkeypatch.setattr(worktree, "git", lambda d, *a, **k: calls.append((d, a)) or real(d, *a, **k))
    (wt / f".loopzero/reviewer-output-{worktree.head(wt)[:12]}-primary.json").write_text("{}")
    (wt / ".venv").mkdir()
    (wt / ".venv/dependency").write_text("generated")
    worktree.cleanup(wt, worktree.head(wt))  # #439: no cleanup git runs in (or -C) the primary checkout
    assert calls and all(Path(d).resolve() != repo.resolve() and "-C" not in a for d, a in calls)
    assert not wt.exists() and not git(repo, "branch", "--list", "lz/done") and "done" not in git(repo, "worktree", "list")
    with pytest.raises(worktree.WorktreeError, match="no such worktree"):
        worktree.cleanup(wt, worktree.head(repo))


@pytest.mark.parametrize("partial", [True, False])
def test_ref_lock_stall_fails_closed_or_reaps_child(tmp_path, monkeypatch, partial):
    ack = "prepare: ok" if partial else "prepare: ok\n"
    script = ("import signal; signal.signal(signal.SIGTERM, signal.SIG_IGN); "
              f"signal.alarm(15); print({ack!r}, end='', flush=True); signal.pause()")
    child = subprocess.Popen([sys.executable, "-c", script], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
    communicate = child.communicate
    monkeypatch.setattr(child, "communicate", lambda data, timeout: communicate(data, timeout=0.05))
    monkeypatch.setattr(worktree.subprocess, "Popen", lambda *args, **kwargs: child)
    outcome = pytest.raises(worktree.GitError) if partial else nullcontext()
    with outcome, worktree._lock_verified_ref(tmp_path, "refs/heads/lz/done", "a" * 40):
        assert not partial, "cleanup proceeded without a complete acknowledgement"
    assert child.returncode == -signal.SIGKILL


@pytest.mark.parametrize("kind", ["registered only", "other repo", "clone"])
def test_cleanup_preserves_ignored_nested_worktrees(repo: Path, tmp_path: Path, kind: str) -> None:
    (repo / ".git/info/exclude").write_text("nested/\n")
    wt = worktree.start(repo, "parent", "main")
    nested = wt / "nested"
    if kind == "clone":
        git(repo, "clone", str(repo), str(nested))
    else:
        source = init_git_repo(tmp_path / "other") if kind == "other repo" else repo
        git(source, "worktree", "add", "--detach", str(nested))
    preserved = nested.rename(tmp_path / "moved") if kind == "registered only" else nested
    assert not worktree.is_dirty(wt)
    with pytest.raises(worktree.WorktreeError, match=r"nested (Git )?worktree"):
        worktree.cleanup(wt, worktree.head(wt))
    assert wt.exists() and (preserved / ".git").exists()
    assert "lz/parent" in git(repo, "branch", "--list", "lz/parent")


def test_cleanup_preserves_branch_checked_out_elsewhere(repo, tmp_path):
    wt = worktree.start(repo, "shared", "main")
    other = tmp_path / "other-checkout"
    git(repo, "worktree", "add", "--force", str(other), "lz/shared")
    head = worktree.head(wt)
    with pytest.raises(worktree.WorktreeError, match="checked out in another worktree"):
        worktree.cleanup(wt, head)
    assert not wt.exists()
    assert worktree.head(other) == head


@pytest.mark.parametrize("state", ["clean", "dirty", "divergent", "unpublished", "remote-moved", "stash"])
def test_abandoned_worktree_guide(repo: Path, tmp_path: Path, state: str) -> None:
    wt = tmp_path / "linked task checkout"
    git(repo, "worktree", "move", str(worktree.start(repo, "abandoned", "main")), str(wt))
    caller = tmp_path / "caller"
    caller.mkdir()
    git(wt, "commit", "--allow-empty", "-m", "published task")
    head = worktree.head(wt)
    if state != "unpublished":
        git(wt, "push", "-u", "origin", "lz/abandoned")
    if state == "dirty":
        (wt / "untracked.txt").write_text("keep my work")
    elif state == "divergent":
        git(wt, "commit", "--allow-empty", "-m", "local follow-up")
    elif state == "remote-moved":
        git(repo, "push", "--force", "origin", "main:refs/heads/lz/abandoned")
    elif state == "stash":
        (repo / "README.md").write_text("keep my stashed work")
        git(repo, "stash", "push")

    commands = setup_commands("sh", "", text=SETUP.read_text().split("## Removing an abandoned task worktree", 1)[1])
    result = subprocess.run(
        ["sh", "-c", commands, "cleanup-guide", str(wt), "abandoned", head],
        cwd=caller, env=git_environment(), capture_output=True, text=True, check=False,
    )

    assert (result.returncode == 0) == (state == "clean"), result.stderr
    assert wt.exists() == (state != "clean")
    assert git(repo, "show-ref", "--verify", "refs/heads/lz/abandoned")
    if state == "clean":
        assert git(repo, "ls-remote", "origin", "refs/heads/lz/abandoned").split() == [head, "refs/heads/lz/abandoned"]


def test_start_retries_after_git_checkout_interrupt(repo, tmp_path):
    (repo / ".gitattributes").write_text("README.md filter=paused\n")
    (repo / "workflow.toml").write_text('[repo]\nname="owner/repo"\n')
    git(repo, "add", ".gitattributes", "workflow.toml")
    git(repo, "commit", "-qm", "add checkout barrier")
    git(repo, "push", "-q", "origin", "main")
    pid_file, entered = tmp_path / "starter.pid", tmp_path / "checkout-entered"
    git(repo, "config", "filter.paused.smudge",
        f'cat >/dev/null; : > {shlex.quote(str(entered))}; '
        f'kill -INT "$(cat {shlex.quote(str(pid_file))})"; exec sleep 30')
    start = ("import os, sys; from pathlib import Path; from loopzero.cli import main; "
             "Path(sys.argv[1]).write_text(str(os.getpid())); sys.exit(main(['start', 'paused']))")
    invoke = partial(subprocess.run, [sys.executable, "-c", start, str(pid_file)],
                     cwd=repo, capture_output=True, timeout=8, check=False)
    first = invoke()
    assert entered.exists() and first.returncode in (-2, 130)
    git(repo, "config", "--unset", "filter.paused.smudge")
    resumed = invoke()
    assert resumed.returncode == 0, resumed.stderr
    assert worktree.task_file(worktree.worktree_dir(repo, "paused")).is_file()
