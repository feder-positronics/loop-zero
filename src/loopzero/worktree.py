"""Owned worktree helpers: create, inspect and remove `lz/<slug>` worktrees."""

from __future__ import annotations

import errno
import fcntl
import os
import re
import stat
import subprocess
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager, suppress
from pathlib import Path
from select import select
from uuid import uuid4

from loopzero._proc import build_env, defer_sigterm, run, run_git_paths
from loopzero.types import LoopZeroError

GIT_ENV = ("PATH", "HOME", "XDG_CONFIG_HOME", "LANG", "LC_ALL", "TERM", "GIT_SSH_COMMAND", "SSH_AUTH_SOCK")
_ABSOLUTE_COMMON_DIR = ("rev-parse", "--path-format=absolute", "--git-common-dir")
EXCLUDES = (b".worktrees/", b".loopzero/")
_BASE_SECTION = re.compile(r"^## Base\n[^\n]+ @ ([0-9a-fA-F]{7,40})[ \t]*$", re.MULTILINE)


class WorktreeError(LoopZeroError):
    """Raised for refused operations or failing git commands."""


class GitError(WorktreeError):
    def __init__(self, command: tuple[str, ...], tail: str, *, exit_code: int | None = None, stderr: str = "") -> None:
        self.command = command
        self.tail = tail
        self.exit_code = exit_code
        self.stderr = stderr
        super().__init__(f"{' '.join(command)} failed: {tail.strip()[-400:]}")


def git(cwd: Path, *args: str, env_allowlist: tuple[str, ...] = GIT_ENV, timeout: float = 120,
        paths: bool = False, raw: bool = False, **kwargs) -> str:
    """Run git, raising GitError on failure; scalar output is stripped unless `raw`."""
    argv = ("git", *args)
    done = (run_git_paths if paths else run)(argv, cwd=cwd, env_allowlist=env_allowlist, timeout=timeout, **kwargs)
    if done.exit_code != 0:
        raise GitError(argv, done.stderr or done.stdout, exit_code=done.exit_code, stderr=done.stderr)
    return done.stdout if raw or paths or kwargs.get("preserve_newlines") else done.stdout.strip()


def branch_name(repo_root: Path, slug: str) -> str:
    """Return `lz/<slug>` after git itself confirms it is a well-formed branch name."""
    name, detail = f"lz/{slug}", ""
    if slug and "/" not in slug and not slug.startswith("."):
        done = run(["git", "check-ref-format", "--branch", name], cwd=repo_root,
                   env_allowlist=GIT_ENV, timeout=30)
        if done.exit_code == 0:
            return name
        detail = f" ({done.stderr.strip()})"
    raise WorktreeError(f"invalid slug: {slug!r}{detail}. Choose a task slug such as `fix-check-output`.")


def title_from_slug(slug: str) -> str:
    """`add-workflow_config-note` -> `Add workflow config note`."""
    title = " ".join(re.sub(r"[-_]+", " ", slug).split())
    return title[:1].upper() + title[1:]


def worktree_dir(repo_root: Path, slug: str) -> Path:
    return Path(repo_root) / ".worktrees" / slug


def task_file(wt: Path) -> Path:
    return Path(wt) / ".loopzero" / "task.md"


START_MARKER = "<!-- Created by loopzero start. Keep this line for cleanup. -->"


def require_started(wt: Path, root: Path) -> None:
    """Require both the managed location and the local start record."""
    if wt.parent == worktree_dir(root, "").resolve():
        with suppress(FileNotFoundError, UnicodeDecodeError):
            if START_MARKER in read_local_state(wt, "task.md").splitlines():
                return
    raise WorktreeError(f"worktree ownership is unverified, preserving {wt}")


@contextmanager
def _local_state_directory(wt: Path, *, create: bool = False, lock: bool = False, wait: Callable[[float], None] | None = None):
    path = wt / ".loopzero"
    if git(wt, "ls-files", "-z", "--", ".loopzero"):
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
            deadline = time.monotonic() + 60
            while (remaining := deadline - time.monotonic()) > 0:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    (wait or time.sleep)(min(0.1, remaining))
            else:
                raise TimeoutError(f"task state is busy in {wt}; retry when the other command finishes")
        yield fd
    finally:
        os.close(fd)


def lock_local_state(wt: Path, *, wait: Callable[[float], None] | None = None):
    """Serialize check execution, receipt publication and Mergify admission for this worktree."""
    return _local_state_directory(wt, create=True, lock=True, wait=wait)


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
    expected_head: str | None = None, preserve: Callable[[], bool] | None = None, locked_fd: int | None = None,
) -> str | None:
    lock = locked_fd is None and (expected_head is not None or preserve is not None)
    with _local_state_directory(wt, create=True, lock=lock) as fd:
        if locked_fd is not None and not os.path.samestat(os.fstat(fd), os.fstat(locked_fd)):
            raise WorktreeError("local state directory changed during check")
        if expected_head is not None and (current_head := head(wt)) != expected_head:
            return current_head
        _regular_state(wt, fd, name)
        if preserve is not None and preserve():
            return None
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
    done = run(["git", "rev-parse", "--verify", "--quiet", f"refs/heads/{branch}"],
               cwd=repo_root, env_allowlist=GIT_ENV, timeout=30)
    return done.exit_code == 0


def _ensure_excludes(repo_root: Path) -> None:
    common = Path(git(repo_root, *_ABSOLUTE_COMMON_DIR, paths=True)).resolve()
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


def _fetch_base(repo_root: Path, base_branch: str) -> str:
    remote_ref, tracking = f"refs/heads/{base_branch}", f"refs/remotes/origin/{base_branch}"
    try:
        valid = run(("git", "check-ref-format", remote_ref), cwd=repo_root, env_allowlist=GIT_ENV, timeout=30)
        symbolic = run(("git", "symbolic-ref", "-q", tracking), cwd=repo_root, env_allowlist=GIT_ENV, timeout=30)
        modules = repo_root / git(repo_root, "rev-parse", "--git-path", "modules", paths=True)
        if valid.exit_code == 0 and symbolic.exit_code == 1 and not modules.exists():
            fetch = ("git", "fetch", "--porcelain", "--verbose", "--no-tags", "--no-prune",
                     "--no-write-fetch-head", "--refmap=", "origin", f"+{remote_ref}:{tracking}")
            result = run(fetch, cwd=repo_root, env_allowlist=GIT_ENV, timeout=120)
            if result.exit_code != 129:  # Older Git does not accept --porcelain.
                row = result.stdout.removesuffix("\n").rsplit(" ", 2)
                if result.exit_code or len(row) != 3 or row[-1] != tracking:
                    raise GitError(fetch, result.stderr or result.stdout or f"origin did not report {remote_ref}")
                return row[1]
        lookup = ("ls-remote", "--refs", "origin", remote_ref)
        base, _, ref = git(repo_root, *lookup).partition("\t")
        if ref != remote_ref:
            raise GitError(("git", *lookup), f"origin did not report a unique {remote_ref}")
        git(repo_root, "fetch", "--quiet", "--no-tags", "--no-write-fetch-head", "--refmap=", "origin", base)
        git(repo_root, "update-ref", "--no-deref", tracking, base)
        return base
    except GitError:
        if "origin" in git(repo_root, "remote").splitlines():
            raise
        raise WorktreeError(
            "no Git remote named 'origin'. Add it with `git remote add origin <url>` using the URL for the "
            "repository in `[repo] name`, or rename that remote with `git remote rename <name> origin`."
        ) from None


def start(repo_root: Path, slug: str, base_branch: str) -> Path:
    """Create a task worktree or finish initializing an interrupted start."""
    branch = branch_name(repo_root, slug)
    wt = worktree_dir(repo_root, slug)
    task = task_file(wt)
    existing = wt.exists()
    refusal = f"worktree directory already exists: {wt}. Resume there if it belongs to this task, or run `loopzero start` with a new slug."
    if wt.is_symlink() or task.parent.is_symlink() or task.is_symlink() or task.exists():
        raise WorktreeError(refusal)
    if not existing and _branch_exists(repo_root, branch):
        raise WorktreeError(f"branch already exists: {branch}. Run `git worktree list` to find the checkout, or use a new slug.")
    base = _fetch_base(repo_root, base_branch)
    detached = not existing
    if existing:
        checkout = _resumable_checkout(repo_root, wt, branch)
        if checkout is None:
            raise WorktreeError(refusal)
        existing_head, detached = checkout
        if existing_head != base:
            raise WorktreeError(
                f"cannot resume start at {wt}: existing HEAD {existing_head} does not match "
                f"origin/{base_branch} at {base}; run loopzero start with a new slug"
            )
    _ensure_excludes(repo_root)
    if not existing:
        wt.parent.mkdir(parents=True, exist_ok=True)
        # Let Git clean up the interrupted checkout before creating the final branch.
        git(repo_root, "worktree", "add", "--quiet", "--detach", str(wt), base, graceful_interrupt=True)
    if detached:
        with defer_sigterm():
            git(wt, "switch", "--quiet", "-c", branch, graceful_interrupt=True)
    template = Path(__file__).with_name("HANDOFF.md")
    if not template.is_file():  # Source and editable installs use the repository copy.
        template = template.parents[2] / "core" / template.name
    text = template.read_text(encoding="utf-8")
    text = text.replace("<base branch> @ <sha>", f"{base_branch} @ {base}", 1)
    _initialize_task(task, text.replace("<Title from slug>", title_from_slug(slug), 1)
                     + f"\n{START_MARKER}\n")
    return wt


def _resumable_checkout(repo_root: Path, wt: Path, expected_branch: str) -> tuple[str, bool] | None:
    registered = git(repo_root, "worktree", "list", "--porcelain", "-z", paths=True).split("\0\0")
    path_field = f"worktree {wt.resolve()}\0"
    fields = next((item for item in registered if item.startswith(path_field)), "").split("\0")
    if (len(fields) != 3 or fields[2] not in ("detached", f"branch refs/heads/{expected_branch}")
            or not (wt / ".git").is_file()):
        return None
    common = git(repo_root, *_ABSOLUTE_COMMON_DIR, paths=True)
    if git(wt, *_ABSOLUTE_COMMON_DIR, paths=True) != common:
        return None
    detached = fields[2] == "detached"
    if detached:
        admin = Path(git(wt, "rev-parse", "--absolute-git-dir", paths=True))
        try:
            backlink = os.fsdecode((admin / "gitdir").read_bytes().removesuffix(b"\n"))
        except OSError:
            return None
        if ((wt / ".git").is_symlink() or (admin / backlink).resolve() != (wt / ".git").resolve()
                or _branch_exists(repo_root, expected_branch)):
            return None
    elif branch(wt, native=True) != expected_branch:
        return None
    current_head = head(wt)
    return (current_head, detached) if fields[1] == f"HEAD {current_head}" and not is_dirty(wt) else None


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
    return git(Path(wt), "rev-parse", "HEAD")


def branch(wt: Path, *, native: bool = False) -> str:
    name = git(Path(wt), "branch", "--show-current", paths=True)
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
    if not (match := _BASE_SECTION.search(task_text(wt))):
        raise WorktreeError(f"no '<branch> @ <sha>' line in the Base section of {task_file(wt)}. Restore that line in the task file using the base branch and SHA from task creation.")
    return match.group(1)


def is_dirty(wt: Path) -> bool:
    return bool(git(Path(wt), "status", "--porcelain", "--untracked-files=all", "--ignore-submodules=none"))


def diff_since(wt: Path, sha: str) -> bytes:
    # Show every file as text, whatever attributes or NUL bytes the reviewed head adds,
    # without diff drivers, and keep submodule pointers even if .gitmodules ignores them.
    diff = git(Path(wt), "diff", "--no-color", "--no-ext-diff", "--no-textconv", "--text",
                "--ignore-submodules=none", "--submodule=short", sha, "HEAD", preserve_newlines=True)
    return diff.encode("utf-8", errors="surrogateescape")


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
            if not (response := os.read(transaction.stdout.fileno(), 256)).startswith(b"prepare: ok\n"):
                raise GitError(argv, f"invalid prepare acknowledgement: {response!r}")
            yield
        finally:
            try:
                transaction.communicate("abort\n", timeout=5)
            except subprocess.TimeoutExpired:
                transaction.kill()


def _require_no_nested_worktree(wt: Path, git_dir: Path, *, allow_submodules: bool) -> bool:
    root = wt.resolve()
    records = git(git_dir, f"--git-dir={git_dir}", "worktree", "list", "--porcelain", "-z",
                   paths=True)
    for field in records.split("\0"):
        if field.startswith("worktree "):
            path = Path(field.removeprefix("worktree "))
            if path != root and path.is_relative_to(root):
                raise WorktreeError(f"nested worktree {path}, refusing to remove: {wt}")

    def fail_scan(exc: OSError) -> None:
        raise WorktreeError(f"could not inspect nested worktrees in {wt}: {exc}") from exc

    links = git(wt, "ls-files", "-z", "--format=%(objectmode) %(path)", paths=True) if allow_submodules else ""
    gitfiles = {path for entry in links.split("\0")
                if entry.startswith("160000 ") and (path := root / entry[7:] / ".git").is_file()}
    modules = Path(git(wt, "rev-parse", "--absolute-git-dir", paths=True)).resolve() / "modules"
    for path, directories, files in root.walk(on_error=fail_scan):
        if path != root and (".git" in files or ".git" in directories):
            if path / ".git" not in gitfiles or not Path(git(
                path, "rev-parse", "--absolute-git-dir", paths=True)).resolve().is_relative_to(modules):
                raise WorktreeError(f"nested Git worktree at {path}, refusing to remove: {wt}")
            if ignored := git(path, "ls-files", "--others", "--ignored", "--exclude-standard"):
                raise WorktreeError(f"submodule {path} contains ignored paths, refusing to remove: {ignored}")
            if git(path, "rev-list", "--max-count=1", "--all", "--reflog", "--not", "--remotes"):
                raise WorktreeError(f"submodule {path} contains local history, refusing to remove: {wt}")
    return bool(gitfiles)


def cleanup(wt: Path, expected_head: str, *, keep_branch: bool = False) -> None:
    """Remove a clean verified worktree and its branch, preserving nested worktrees."""
    wt = Path(wt).absolute()
    if not wt.exists():
        raise WorktreeError(f"no such worktree: {wt}")
    if head(wt) != expected_head:
        raise WorktreeError("local HEAD differs from final PR head")
    name = branch(wt, native=True)
    gd = Path(git(wt, *_ABSOLUTE_COMMON_DIR, paths=True))
    ref = f"refs/heads/{name}"
    with _lock_verified_ref(wt, ref, expected_head):
        if branch(wt, native=True) != name:
            raise WorktreeError("local branch changed during merge cleanup")
        if is_dirty(wt):
            raise WorktreeError(f"worktree is dirty, refusing to remove: {wt}")
        force = ("--force",) if _require_no_nested_worktree(wt, gd, allow_submodules=keep_branch) else ()
        generated = () if keep_branch else (
            ":(exclude,top).venv/", ":(exclude,top).pytest_cache/", ":(exclude,top).mypy_cache/",
            ":(exclude,top).ruff_cache/", ":(exclude,glob,top)**/__pycache__/**",
        )
        if ignored := git(wt, "ls-files", "--others", "--ignored", "--exclude-standard",
                          "--", ".", ":(exclude,top).loopzero/", *generated):
            raise WorktreeError(f"worktree contains ignored paths, refusing to remove: {ignored}")
        git(gd, f"--git-dir={gd}", "worktree", "remove", *force, str(wt))  # never the primary checkout
    if keep_branch:
        return
    worktrees = git(gd, f"--git-dir={gd}", "worktree", "list", "--porcelain", "-z", paths=True)
    if f"branch {ref}" in worktrees.split("\0"):
        raise WorktreeError(f"local branch {name!r} is checked out in another worktree")
    try:
        git(gd, f"--git-dir={gd}", "update-ref", "--no-deref", "-d", ref, expected_head)
    except GitError:
        if git(gd, f"--git-dir={gd}", "branch", "--list", "--", name):
            raise
