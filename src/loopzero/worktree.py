"""Owned worktree helpers: create, inspect and remove `lz/<slug>` worktrees."""

from __future__ import annotations

import errno
import fcntl
import os
import re
import stat
import subprocess
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from pathlib import Path
from select import select
from uuid import uuid4

from loopzero._proc import build_env, run, run_git_paths

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


def _git(cwd: Path, *args: str, timeout: float = 120,
         preserve_whitespace: bool = False, preserve_newlines: bool = False) -> str:
    argv = ("git", *args)
    if preserve_whitespace:
        done = run_git_paths(argv, cwd=cwd, env_allowlist=GIT_ENV, timeout=timeout)
    else:
        done = run(argv, cwd=cwd, env_allowlist=GIT_ENV, timeout=timeout, preserve_newlines=preserve_newlines)
    if done.exit_code != 0:
        raise GitError(argv, done.stderr or done.stdout)
    return done.stdout if preserve_newlines or preserve_whitespace else done.stdout.strip()


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


@contextmanager
def _local_state_directory(wt: Path, *, create: bool = False, lock: bool = False):
    path = wt / ".loopzero"
    if _git(wt, "ls-files", "-z", "--", ".loopzero"):
        raise WorktreeError(f"refusing repository-tracked local state: {path}")
    if create:
        with suppress(FileExistsError):
            path.mkdir(mode=0o700)
    try:
        fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    except NotADirectoryError as exc:
        raise WorktreeError(f"local state directory is not a directory or is a symbolic link: {path}") from exc
    try:
        if lock:
            fcntl.flock(fd, fcntl.LOCK_EX)
        yield fd
    finally:
        os.close(fd)


def lock_local_state(wt: Path):
    """Serialize receipt publication and PR-body updates for this worktree."""
    return _local_state_directory(wt, create=True, lock=True)


def _regular_state(wt: Path, fd: int, name: str) -> bool:
    with suppress(FileNotFoundError):
        if not stat.S_ISREG(os.stat(name, dir_fd=fd, follow_symlinks=False).st_mode):
            raise WorktreeError(f"local state file is not regular or is a symbolic link: {wt / '.loopzero' / name}")
        return True
    return False


def local_state_files(wt: Path, *names: str) -> tuple[bool, ...]:
    with suppress(FileNotFoundError), _local_state_directory(wt) as fd:
        return tuple(_regular_state(wt, fd, name) for name in names)
    return (False,) * len(names)


def read_local_state(wt: Path, name: str) -> str:
    with _local_state_directory(wt) as fd, os.fdopen(
        os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd), encoding="utf-8"
    ) as stream:
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
            raise WorktreeError(f"local state file is not regular: {wt / '.loopzero' / name}")
        return stream.read()


def write_local_state(
    wt: Path, name: str, text: str, *, private: bool = False, exclusive: bool = False,
    expected_head: str | None = None,
) -> str | None:
    with _local_state_directory(wt, create=True, lock=private or expected_head is not None) as fd:
        if expected_head is not None and (current_head := head(wt)) != expected_head:
            return current_head
        _regular_state(wt, fd, name)
        if private:
            os.fchmod(fd, 0o700)
        temp = f".{name}-{uuid4().hex}"
        file_fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600, dir_fd=fd)
        try:
            with os.fdopen(file_fd, "w", encoding="utf-8") as stream:
                stream.write(text)
                stream.flush()
                os.fsync(stream.fileno())
            if exclusive:
                os.link(temp, name, src_dir_fd=fd, dst_dir_fd=fd, follow_symlinks=False)
            else:
                os.replace(temp, name, src_dir_fd=fd, dst_dir_fd=fd)
        finally:
            with suppress(FileNotFoundError):
                os.unlink(temp, dir_fd=fd)
    return None


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
    if wt.is_symlink() or task.parent.is_symlink() or task.is_symlink() or task.exists():
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
    registered = _git(repo_root, "worktree", "list", "--porcelain", "-z", preserve_whitespace=True).split("\0\0")
    path_field = f"worktree {wt.resolve()}\0"
    fields = next((item for item in registered if item.startswith(path_field)), "").split("\0")
    if (len(fields) != 3 or fields[2] != f"branch refs/heads/{expected_branch}"
            or not (wt / ".git").is_file()):
        return None
    common = _git(repo_root, *_ABSOLUTE_COMMON_DIR, preserve_whitespace=True)
    if _git(wt, *_ABSOLUTE_COMMON_DIR, preserve_whitespace=True) != common or branch(wt, native=True) != expected_branch:
        return None
    current_head = head(wt)
    return current_head if fields[1] == f"HEAD {current_head}" and not is_dirty(wt) else None


def _initialize_task(task: Path, text: str) -> None:
    try:
        write_local_state(task.parent.parent, task.name, text, exclusive=True)
    except FileExistsError as exc:
        raise WorktreeError(f"task file already exists: {task}") from exc
    except OSError as exc:
        if exc.errno not in (errno.EOPNOTSUPP, errno.ENOTSUP, errno.ENOSYS, errno.EPERM):
            raise
        raise WorktreeError(
            f"cannot publish task file {task} ({exc.strerror}): hard links are required; "
            "run loopzero start from a checkout on a filesystem that supports hard links"
        ) from exc


def head(wt: Path) -> str:
    return _git(Path(wt), "rev-parse", "HEAD")


def is_linked_checkout(wt: Path) -> bool:
    git_dir = _git(wt, "rev-parse", "--absolute-git-dir", preserve_whitespace=True)
    common_dir = _git(wt, *_ABSOLUTE_COMMON_DIR, preserve_whitespace=True)
    return git_dir != common_dir


def branch(wt: Path, *, native: bool = False) -> str:
    name = _git(Path(wt), "branch", "--show-current", preserve_whitespace=True)
    if not name:
        raise WorktreeError("HEAD is detached. Switch to a branch and retry.")
    return name if native else os.fsencode(name).decode("utf-8", "surrogateescape")


def task_text(wt: Path) -> str:
    try:
        return read_local_state(wt, "task.md")
    except FileNotFoundError:
        raise WorktreeError(
            f"missing {task_file(wt)}. Run `loopzero start <slug>` for a new task worktree, "
            "or recreate the file using the handoff template at "
            "https://github.com/feder-positronics/loop-zero/blob/main/core/HANDOFF.md"
        ) from None


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
            wire_ref = os.fsencode(ref).decode("utf-8", "surrogateescape")
            transaction.stdin.write(f"option no-deref\nverify {wire_ref} {expected_head}\nprepare\n")
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
    name = branch(wt, native=True)
    gd = Path(_git(wt, *_ABSOLUTE_COMMON_DIR, preserve_whitespace=True))
    ref = f"refs/heads/{name}"
    with _lock_verified_ref(wt, ref, expected_head):
        if branch(wt, native=True) != name:
            raise WorktreeError("local branch changed during merge cleanup")
        if is_dirty(wt):
            raise WorktreeError(f"worktree is dirty, refusing to remove: {wt}")
        _require_no_ignored_user_files(wt)
        _git(gd, f"--git-dir={gd}", "worktree", "remove", str(wt))  # never the primary checkout
    worktrees = _git(gd, f"--git-dir={gd}", "worktree", "list", "--porcelain", "-z", preserve_whitespace=True)
    if f"branch {ref}" in worktrees.split("\0"):
        raise WorktreeError(f"local branch {name!r} is checked out in another worktree")
    try:
        _git(gd, f"--git-dir={gd}", "update-ref", "--no-deref", "-d", ref, expected_head)
    except GitError:
        if _git(gd, f"--git-dir={gd}", "branch", "--list", "--", name):
            raise
