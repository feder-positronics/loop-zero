"""Owned worktree helpers: create, inspect and remove `lz/<slug>` worktrees."""

from __future__ import annotations

import errno
import fcntl
import os
import re
import subprocess
import sys
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from pathlib import Path
from select import select

from loopzero._proc import build_env, run

GIT_ENV = ("PATH", "HOME", "LANG", "LC_ALL", "TERM", "GIT_SSH_COMMAND", "SSH_AUTH_SOCK")
_ABSOLUTE_COMMON_DIR = ("rev-parse", "--path-format=absolute", "--git-common-dir")
EXCLUDES = (b".worktrees/", b".loopzero/")
_BASE_LINE = re.compile(r"^Base:\s*([0-9a-fA-F]{7,40})\s*$", re.MULTILINE)
_TASK_TEMPLATE = """# {title}

## Context and goal
- **Goal:** <one plain sentence: what the owner or users now notice (and cost/saving if known), because this change fixes the problem>
- **Problem:** <one or two plain sentences before code names: what goes wrong (one problem only), who notices, and evidence/cost; refs in brackets at the end>
- **Context:** <plain newcomer context; explain domain terms from read sources or describe what they do instead; rulings and dispatch belong in Notes>

Refs #<issue number, or remove this line>

## Acceptance
- <observable behaviour, e.g. "a new plan opens by restating the ask"; not wording, e.g. "plan/SKILL.md line 12 says X">

## Notes

## Base
{base_branch} @ {base}
Base: {base}

## Validation
(filled by `loopzero check`)

## Review
(filled by `loopzero review`)
"""


class WorktreeError(Exception):
    """Raised for refused operations or failing git commands."""


class GitError(WorktreeError):
    def __init__(self, command: tuple[str, ...], tail: str) -> None:
        self.command = command
        self.tail = tail
        super().__init__(f"{' '.join(command)} failed: {tail.strip()[-400:]}")


def _git(
    cwd: Path, *args: str, timeout: float = 120,
    preserve_whitespace: bool = False, preserve_newlines: bool = False,
) -> str:
    argv = ("git", *args)
    done = run(argv, cwd=cwd, env_allowlist=GIT_ENV, timeout=timeout,
               preserve_newlines=preserve_newlines or preserve_whitespace,
               encoding=sys.getfilesystemencoding() if preserve_whitespace else None)
    if done.exit_code != 0:
        raise GitError(argv, done.stderr or done.stdout)
    if preserve_newlines and not preserve_whitespace:
        return done.stdout
    return done.stdout.removesuffix("\n") if preserve_whitespace else done.stdout.strip()


def branch_name(repo_root: Path, slug: str) -> str:
    """Return `lz/<slug>` after git itself confirms it is a well-formed branch name."""
    if not slug or "/" in slug or slug.startswith("."):
        raise WorktreeError(f"invalid slug: {slug!r}")
    name = f"lz/{slug}"
    done = run(["git", "check-ref-format", "--branch", name], cwd=repo_root,
               env_allowlist=GIT_ENV, timeout=30)
    if done.exit_code != 0:
        raise WorktreeError(f"invalid slug: {slug!r} ({done.stderr.strip()})")
    return name


def title_from_slug(slug: str) -> str:
    """`add-workflow_config-note` -> `Add workflow config note`."""
    title = " ".join(re.sub(r"[-_]+", " ", slug).split())
    return title[:1].upper() + title[1:]


def worktree_dir(repo_root: Path, slug: str) -> Path:
    return Path(repo_root) / ".worktrees" / slug


def task_file(wt: Path) -> Path:
    return Path(wt) / ".loopzero" / "task.md"


def _branch_exists(repo_root: Path, branch: str) -> bool:
    done = run(
        ["git", "rev-parse", "--verify", "--quiet", f"refs/heads/{branch}"],
        cwd=repo_root, env_allowlist=GIT_ENV, timeout=30,
    )
    return done.exit_code == 0


def _ensure_excludes(repo_root: Path) -> None:
    common = Path(_git(repo_root, *_ABSOLUTE_COMMON_DIR, preserve_whitespace=True)).resolve()
    info = (common / "info").resolve()
    exclude = (info / "exclude").resolve()
    if not info.is_relative_to(common) or not exclude.is_relative_to(common):
        raise WorktreeError(f"Git exclude is outside its Git directory: {exclude}")
    exclude.parent.mkdir(parents=True, exist_ok=True)
    while True:
        fd = os.open(exclude, os.O_RDWR | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW, 0o666)
        with os.fdopen(fd, "rb") as stream:
            fcntl.flock(fd, fcntl.LOCK_EX)
            lines = set(stream.read().splitlines())
            missing = [entry for entry in EXCLUDES if entry not in lines]
            if missing:
                # An editor can append an unterminated line before this write.
                rules = b"\n" + b"\n".join(missing) + b"\n"
                if os.write(fd, rules) != len(rules):
                    raise WorktreeError(f"incomplete write to Git exclude: {exclude}")
            with suppress(FileNotFoundError):
                if os.path.samestat(os.fstat(fd), exclude.lstat()):
                    return


def start(repo_root: Path, slug: str, base_branch: str) -> Path:
    """Create a task worktree or finish initializing an interrupted start."""
    repo_root = Path(repo_root)
    branch = branch_name(repo_root, slug)
    wt = worktree_dir(repo_root, slug)
    task = task_file(wt)
    existing = wt.exists()
    refusal = f"worktree directory already exists: {wt}"
    if wt.is_symlink() or task.exists() or task.is_symlink() or task.parent.is_symlink():
        raise WorktreeError(refusal)
    if not existing and _branch_exists(repo_root, branch):
        raise WorktreeError(f"branch already exists: {branch}")
    remote_ref = f"refs/heads/{base_branch}"
    lookup = ("ls-remote", "--refs", "origin", remote_ref)
    try:
        advertised = _git(repo_root, *lookup)
        base, _, ref = advertised.partition("\t")
        if ref != remote_ref:
            raise GitError(("git", *lookup), f"origin did not report a unique {remote_ref}")
        _git(repo_root, "fetch", "--quiet", "--no-tags", "--no-write-fetch-head", "--refmap=", "origin", base)
    except GitError:
        if "origin" not in _git(repo_root, "remote").splitlines():
            raise WorktreeError(
                "no Git remote named 'origin'. Add it with `git remote add origin <url>` "
                "using the URL for the repository in `[repo] name`, or rename that remote "
                "with `git remote rename <name> origin`."
            ) from None
        raise
    _git(repo_root, "update-ref", "--no-deref", f"refs/remotes/origin/{base_branch}", base)
    if existing:
        existing_head = _resumable_head(repo_root, wt, branch)
        if existing_head is None:
            raise WorktreeError(refusal)
        if existing_head != base:
            raise WorktreeError(
                f"cannot resume start at {wt}: existing HEAD {existing_head} does not match "
                f"origin/{base_branch} at {base}; run loopzero start with a new slug"
            )
    _ensure_excludes(repo_root)
    if not existing:
        wt.parent.mkdir(parents=True, exist_ok=True)
        _git(repo_root, "worktree", "add", "--quiet", "-b", branch, str(wt), base)
    _initialize_task(task, _TASK_TEMPLATE.format(
        title=title_from_slug(slug), base_branch=base_branch, base=base))
    return wt


def _resumable_head(repo_root: Path, wt: Path, expected_branch: str) -> str | None:
    registered = _git(repo_root, "worktree", "list", "--porcelain", "-z").split("\0\0")
    path_field = f"worktree {wt.resolve()}"
    entry = next((item for item in registered if item.startswith(f"{path_field}\0")), "")
    fields = entry.split("\0")
    if (len(fields) != 3 or fields[2] != f"branch refs/heads/{expected_branch}"
            or not (wt / ".git").is_file()):
        return None
    if _git(wt, *_ABSOLUTE_COMMON_DIR) != _git(repo_root, *_ABSOLUTE_COMMON_DIR) or branch(wt) != expected_branch:
        return None
    current_head = head(wt)
    if fields[1] != f"HEAD {current_head}" or is_dirty(wt):
        return None
    return current_head


def _initialize_task(task: Path, text: str) -> None:
    if task.parent.is_symlink():
        raise WorktreeError(f"task directory is a symbolic link: {task.parent}")
    task.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", dir=task.parent, prefix=".task.md-", delete=False
    ) as stream:
        temp = Path(stream.name)
        try:
            stream.write(text)
            stream.close()
            # Publish the complete file without replacing an existing handoff.
            try:
                task.hardlink_to(temp)
            except FileExistsError as exc:
                raise WorktreeError(f"task file already exists: {task}") from exc
            except OSError as exc:
                if exc.errno not in (errno.EOPNOTSUPP, errno.ENOTSUP, errno.ENOSYS, errno.EPERM):
                    raise
                raise WorktreeError(
                    f"cannot publish task file {task} ({exc.strerror}): hard links are required; "
                    "run loopzero start from a checkout on a filesystem that supports hard links"
                ) from exc
        finally:
            temp.unlink(missing_ok=True)


def head(wt: Path) -> str:
    return _git(Path(wt), "rev-parse", "HEAD")


def is_linked_checkout(wt: Path) -> bool:
    git_dir = _git(wt, "rev-parse", "--absolute-git-dir")
    common_dir = _git(wt, *_ABSOLUTE_COMMON_DIR)
    return git_dir != common_dir


def branch(wt: Path) -> str:
    name = _git(Path(wt), "branch", "--show-current", preserve_whitespace=True)
    if not name:
        raise WorktreeError("HEAD is detached. Switch to a branch and retry.")
    return name


def task_text(wt: Path) -> str:
    task = task_file(wt)
    if not task.exists():
        raise WorktreeError(
            f"missing {task}. Run `loopzero start <slug>` for a new task worktree, "
            "or recreate the file using the handoff template at "
            "https://github.com/feder-positronics/loop-zero/blob/main/core/HANDOFF.md"
        )
    return task.read_text()


def base_sha(wt: Path) -> str:
    match = _BASE_LINE.search(task_text(wt))
    if not match:
        raise WorktreeError(f"no 'Base: <sha>' line in {task_file(wt)}")
    return match.group(1)


def is_dirty(wt: Path) -> bool:
    return bool(_git(Path(wt), "status", "--porcelain", "--untracked-files=all"))


def diff_since(wt: Path, sha: str) -> bytes:
    # Show every file as text, whatever attributes or NUL bytes the reviewed head adds,
    # without diff drivers, and keep submodule pointers even if .gitmodules ignores them.
    diff = _git(Path(wt), "diff", "--no-color", "--no-ext-diff", "--no-textconv", "--text",
                "--ignore-submodules=none", "--submodule=short", sha, "HEAD", preserve_newlines=True)
    return diff.encode("utf-8", errors="surrogateescape")


def merges_since(wt: Path, sha: str) -> list[str]:
    """First-parent merges between ``sha`` and HEAD, oldest first."""
    return _git(Path(wt), "rev-list", "--first-parent", "--reverse", "--merges", f"{sha}..HEAD").split()


@contextmanager
def _lock_verified_ref(wt: Path, ref: str, expected_head: str) -> Iterator[None]:
    argv = ("git", "update-ref", "--stdin")
    with subprocess.Popen(
        argv, cwd=wt, env=build_env(GIT_ENV), stdin=subprocess.PIPE,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, encoding="utf-8", errors="surrogateescape",
    ) as transaction:
        assert transaction.stdin is not None and transaction.stdout is not None
        try:
            transaction.stdin.write(f"option no-deref\nverify {ref} {expected_head}\nprepare\n")
            transaction.stdin.flush()
            if not select([transaction.stdout], [], [], 30)[0]:
                raise GitError(argv, "timed out verifying the merged branch")
            response = transaction.stdout.readline()
            if response != "prepare: ok\n":
                raise GitError(argv, response)
            yield
        finally:
            try:
                transaction.communicate("abort\n", timeout=5)
            except subprocess.TimeoutExpired:
                transaction.terminate()


def _require_no_ignored_user_files(wt: Path) -> None:
    ignored = _git(
        wt, "ls-files", "--others", "--ignored", "--exclude-standard", "-z", preserve_newlines=True
    )
    for path in ignored.split("\0")[:-1]:
        if (
            (path in {".loopzero/task.md", ".loopzero/checks.json"}
             or re.fullmatch(r"\.loopzero/review(?:er-output)?-[0-9a-f]{12}-(primary|delta)\.json", path))
            and not (wt / path).is_symlink()
        ):
            continue
        raise WorktreeError(f"worktree contains ignored file {path!r}, refusing to remove: {wt}")


def cleanup(wt: Path, expected_head: str) -> None:
    """Remove a clean merged worktree and its branch if it has no local user files."""
    wt = Path(wt).absolute()
    if not wt.exists():
        raise WorktreeError(f"no such worktree: {wt}")
    if head(wt) != expected_head:
        raise WorktreeError("local HEAD differs from merged PR head")
    name = branch(wt)
    gd = Path(_git(wt, *_ABSOLUTE_COMMON_DIR, preserve_whitespace=True))
    ref = f"refs/heads/{name}"
    with _lock_verified_ref(wt, ref, expected_head):
        if branch(wt) != name:
            raise WorktreeError("local branch changed during merge cleanup")
        if is_dirty(wt):
            raise WorktreeError(f"worktree is dirty, refusing to remove: {wt}")
        _require_no_ignored_user_files(wt)
        _git(gd, f"--git-dir={gd}", "worktree", "remove", str(wt))  # never the primary checkout
    worktrees = _git(gd, f"--git-dir={gd}", "worktree", "list", "--porcelain", "-z")
    if f"branch {ref}" in worktrees.split("\0"):
        raise WorktreeError(f"local branch {name!r} is checked out in another worktree")
    try:
        _git(gd, f"--git-dir={gd}", "update-ref", "--no-deref", "-d", ref, expected_head)
    except GitError:
        if _git(gd, f"--git-dir={gd}", "branch", "--list", "--", name):
            raise
