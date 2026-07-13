#!/usr/bin/env python3
"""Serialize repository-owned worktree writers and pin exact source state."""

import argparse
import fcntl
import hashlib
import json
import os
import secrets
import subprocess
import sys
import time
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

LEASE_FD_ENV = "INTELFLO_WORKTREE_LEASE_FD"
LEASE_BOUNDARY_ENV = "INTELFLO_WORKTREE_LEASE_BOUNDARY"
LOCK_FILENAME = "worktree-boundary.lock"
OWNER_FILENAME = "worktree-boundary.owner.json"


class WorktreeGuardError(RuntimeError):
    """Base error for source and lease boundary failures."""


class LeaseTimeoutError(WorktreeGuardError):
    """Raised when another writer owns the worktree lease past the deadline."""


@dataclass(frozen=True)
class LeaseHandle:
    fd: int
    lock_path: Path

    @property
    def pass_fds(self) -> tuple[int, ...]:
        return (self.fd,)

    def child_env(self) -> dict[str, str]:
        return {LEASE_FD_ENV: str(self.fd)}


def inherited_pass_fds() -> tuple[int, ...]:
    """Return the live inherited lease descriptor for owned subprocesses."""
    raw_fd = os.environ.get(LEASE_FD_ENV)
    if raw_fd is None:
        return ()
    try:
        fd = int(raw_fd)
        os.fstat(fd)
    except (OSError, ValueError):
        return ()
    return (fd,)


def _git_bytes(worktree: Path, *args: str) -> bytes:
    completed = subprocess.run(
        ["git", "-C", str(worktree), *args],
        capture_output=True,
        check=False,
    )
    if completed.returncode != 0:
        raise WorktreeGuardError(f"git {args[0]} failed in {worktree}")
    return completed.stdout


def _git_text(worktree: Path, *args: str) -> str:
    return _git_bytes(worktree, *args).decode().strip()


def git_dir(worktree: Path) -> Path:
    raw = _git_text(worktree, "rev-parse", "--path-format=absolute", "--git-dir")
    return Path(raw).resolve()


def _lock_paths(worktree: Path) -> tuple[Path, Path]:
    directory = git_dir(worktree)
    return directory / LOCK_FILENAME, directory / OWNER_FILENAME


def _inherited_lease(lock_path: Path) -> LeaseHandle | None:
    raw_fd = os.environ.get(LEASE_FD_ENV)
    if raw_fd is None:
        return None
    try:
        fd = int(raw_fd)
        fd_stat = os.fstat(fd)
        lock_stat = lock_path.stat()
    except (OSError, ValueError):
        return None
    if (fd_stat.st_dev, fd_stat.st_ino) != (lock_stat.st_dev, lock_stat.st_ino):
        return None
    try:
        # Idempotent on a duplicated descriptor sharing the locked OFD. A
        # separately opened stale descriptor must acquire the lock here.
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return None
    os.set_inheritable(fd, True)
    return LeaseHandle(fd=fd, lock_path=lock_path)


def _owner_summary(owner_path: Path) -> str:
    try:
        payload = json.loads(owner_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return "unknown boundary"
    boundary = payload.get("boundary")
    pid = payload.get("pid")
    if isinstance(boundary, str) and isinstance(pid, int):
        return f"{boundary} (pid {pid})"
    return "unknown boundary"


def _write_owner(owner_path: Path, *, boundary: str, nonce: str) -> None:
    temporary = owner_path.with_suffix(f".tmp-{os.getpid()}-{nonce}")
    temporary.write_text(
        json.dumps(
            {
                "boundary": boundary,
                "nonce": nonce,
                "pid": os.getpid(),
                "started_monotonic": time.monotonic(),
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    temporary.chmod(0o600)
    temporary.replace(owner_path)


def _clear_owner(owner_path: Path, *, nonce: str) -> None:
    try:
        payload = json.loads(owner_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return
    if payload.get("nonce") == nonce:
        owner_path.unlink(missing_ok=True)


@contextmanager
def worktree_lease(
    worktree: Path,
    *,
    boundary: str,
    timeout_s: float = 300.0,
) -> Iterator[LeaseHandle]:
    """Hold the per-worktree writer lease for one repository boundary."""
    resolved = worktree.resolve()
    lock_path, owner_path = _lock_paths(resolved)
    lock_path.touch(mode=0o600, exist_ok=True)
    if inherited := _inherited_lease(lock_path):
        yield inherited
        return

    fd = os.open(lock_path, os.O_RDWR | os.O_CLOEXEC)
    deadline = time.monotonic() + timeout_s
    while True:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            break
        except BlockingIOError:
            if time.monotonic() >= deadline:
                owner = _owner_summary(owner_path)
                os.close(fd)
                raise LeaseTimeoutError(
                    f"worktree lease held by {owner}; {boundary} timed out "
                    f"after {timeout_s:g}s"
                ) from None
            time.sleep(min(0.05, max(0.0, deadline - time.monotonic())))

    nonce = secrets.token_hex(8)
    os.set_inheritable(fd, True)
    _write_owner(owner_path, boundary=boundary, nonce=nonce)
    try:
        yield LeaseHandle(fd=fd, lock_path=lock_path)
    finally:
        _clear_owner(owner_path, nonce=nonce)
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def _status_paths(worktree: Path) -> list[str]:
    completed = subprocess.run(
        [
            "git",
            "-C",
            str(worktree),
            "status",
            "--porcelain",
            "--untracked-files=all",
        ],
        capture_output=True,
        check=False,
        text=True,
    )
    if completed.returncode != 0:
        raise WorktreeGuardError(f"git status failed in {worktree}")
    paths: set[str] = set()
    for line in completed.stdout.splitlines():
        if len(line) < 4:
            continue
        path = line[3:].strip()
        if " -> " in path:
            old, new = path.split(" -> ", 1)
            paths.update({old.strip('"'), new.strip('"')})
        else:
            paths.add(path.strip('"'))
    return sorted(paths)


def _path_digest(worktree: Path, relative_path: str) -> str:
    path = worktree / relative_path
    if path.is_symlink():
        kind = "symlink"
        content = os.readlink(path).encode()
    elif path.is_file():
        kind = "file"
        content = path.read_bytes()
    elif path.exists():
        kind = "directory"
        content = b""
    else:
        kind = "missing"
        content = b""
    mode = path.lstat().st_mode & 0o7777 if path.exists() or path.is_symlink() else 0
    payload = b"\0".join((kind.encode(), f"{mode:o}".encode(), content))
    return hashlib.sha256(payload).hexdigest()


def source_identity(worktree: Path) -> dict[str, int | str]:
    """Return V2 while preserving the comprehensive existing state hash."""
    resolved = worktree.resolve()
    head = _git_text(resolved, "rev-parse", "HEAD")
    symbolic_ref = subprocess.run(
        ["git", "-C", str(resolved), "symbolic-ref", "--quiet", "HEAD"],
        capture_output=True,
        check=False,
        text=True,
    )
    ref = symbolic_ref.stdout.strip() if symbolic_ref.returncode == 0 else "<detached>"
    state = {
        "head": head,
        "index_sha256": hashlib.sha256(
            _git_bytes(resolved, "ls-files", "--stage", "-z")
        ).hexdigest(),
        "paths": {
            path: _path_digest(resolved, path) for path in _status_paths(resolved)
        },
        "status_sha256": hashlib.sha256(
            _git_bytes(resolved, "status", "--porcelain=v2", "-z", "-uall")
        ).hexdigest(),
    }
    return {
        "version": 2,
        "ref": ref,
        "head": head,
        "state_sha256": hashlib.sha256(
            json.dumps(state, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest(),
    }


def identities_match(
    expected: Mapping[str, object], current: Mapping[str, object]
) -> bool:
    """Compare v1 telemetry or a complete V2 identity without rewriting it."""
    if expected.get("version") == 2:
        return dict(expected) == dict(current)
    legacy_keys = {"head", "state_sha256"}
    if not expected or not set(expected).issubset(legacy_keys):
        return False
    return all(current.get(key) == value for key, value in expected.items())


def _exec_command(args: argparse.Namespace) -> int:
    command = list(args.command)
    if command and command[0] == "--":
        command.pop(0)
    if not command:
        raise WorktreeGuardError("exec requires a command after --")
    with worktree_lease(
        args.worktree, boundary=args.boundary, timeout_s=args.timeout
    ) as lease:
        environment = {**os.environ, **lease.child_env()}
        environment[LEASE_BOUNDARY_ENV] = args.boundary
        os.execvpe(command[0], command, environment)
    return 0  # pragma: no cover - os.execvpe replaces the process


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command_name", required=True)
    exec_parser = subparsers.add_parser("exec")
    exec_parser.add_argument("--worktree", type=Path, default=Path.cwd())
    exec_parser.add_argument("--boundary", required=True)
    exec_parser.add_argument("--timeout", type=float, default=300.0)
    exec_parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    try:
        return _exec_command(args)
    except WorktreeGuardError as exc:
        print(f"worktree-guard error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
