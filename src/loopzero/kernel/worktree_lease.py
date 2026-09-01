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
LEASE_OWNER_PID_ENV = "INTELFLO_WORKTREE_LEASE_OWNER_PID"
LEASE_NONCE_ENV = "INTELFLO_WORKTREE_LEASE_NONCE"
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
    owner_pid: int | None = None
    nonce: str | None = None

    @property
    def pass_fds(self) -> tuple[int, ...]:
        return (self.fd,)

    def child_env(self) -> dict[str, str]:
        environment = {LEASE_FD_ENV: str(self.fd)}
        if self.owner_pid is not None:
            environment[LEASE_OWNER_PID_ENV] = str(self.owner_pid)
        if self.nonce is not None:
            environment[LEASE_NONCE_ENV] = self.nonce
        return environment


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


def _lock_paths(
    worktree: Path, *, git_directory: Path | None = None
) -> tuple[Path, Path]:
    directory = (
        git_directory.resolve() if git_directory is not None else git_dir(worktree)
    )
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
    raw_owner_pid = os.environ.get(LEASE_OWNER_PID_ENV)
    try:
        owner_pid = int(raw_owner_pid) if raw_owner_pid is not None else None
    except ValueError:
        owner_pid = None
    return LeaseHandle(
        fd=fd,
        lock_path=lock_path,
        owner_pid=owner_pid,
        nonce=os.environ.get(LEASE_NONCE_ENV),
    )


def _matches_owned_descendant(owner_path: Path) -> bool:
    """Match a descendant's secret nonce to the digest held by the lease owner."""
    raw_owner_pid = os.environ.get(LEASE_OWNER_PID_ENV)
    nonce = os.environ.get(LEASE_NONCE_ENV)
    if raw_owner_pid is None or nonce is None:
        return False
    try:
        owner_pid = int(raw_owner_pid)
        payload = json.loads(owner_path.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError):
        return False
    return (
        payload.get("pid") == owner_pid
        and payload.get("nonce_sha256") == hashlib.sha256(nonce.encode()).hexdigest()
        and _has_process_ancestor(owner_pid)
    )


def _parent_process_id(pid: int) -> int | None:
    try:
        stat_fields = (
            Path(f"/proc/{pid}/stat")
            .read_text(encoding="utf-8")
            .rsplit(")", 1)[1]
            .split()
        )
        return int(stat_fields[1])
    except (IndexError, OSError, ValueError):
        completed = subprocess.run(
            ["ps", "-o", "ppid=", "-p", str(pid)],
            capture_output=True,
            check=False,
            text=True,
        )
        try:
            return int(completed.stdout.strip()) if completed.returncode == 0 else None
        except ValueError:
            return None


def _has_process_ancestor(expected_pid: int) -> bool:
    current_pid = os.getpid()
    seen: set[int] = set()
    while current_pid > 1 and current_pid not in seen:
        if current_pid == expected_pid:
            return True
        seen.add(current_pid)
        parent_pid = _parent_process_id(current_pid)
        if parent_pid is None:
            return False
        current_pid = parent_pid
    return current_pid == expected_pid


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
                "nonce_sha256": hashlib.sha256(nonce.encode()).hexdigest(),
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
    if payload.get("nonce_sha256") == hashlib.sha256(nonce.encode()).hexdigest():
        owner_path.unlink(missing_ok=True)


@contextmanager
def worktree_lease(
    worktree: Path,
    *,
    boundary: str,
    timeout_s: float = 300.0,
    git_directory: Path | None = None,
) -> Iterator[LeaseHandle]:
    """Hold the per-worktree writer lease for one repository boundary."""
    resolved = worktree.resolve()
    lock_path, owner_path = _lock_paths(resolved, git_directory=git_directory)
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
        yield LeaseHandle(
            fd=fd,
            lock_path=lock_path,
            owner_pid=os.getpid(),
            nonce=nonce,
        )
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
        if dict(expected) == dict(current):
            return True
        # Governed write identities historically advertised an always-null
        # index_sha256 even though their tree/path payload did not calculate an
        # index digest.  Accept that exact truthful-shape transition only when
        # both derived status digests validate and the underlying payloads are
        # otherwise identical.  Ordinary V2 source identities have
        # state_sha256 instead and remain exact-match only.
        if "tree_sha" not in expected or "tree_sha" not in current:
            return False

        def governed_payload(
            identity: Mapping[str, object],
        ) -> dict[str, object] | None:
            payload = dict(identity)
            status_digest = payload.pop("status_sha256", None)
            if not isinstance(status_digest, str):
                return None
            if (
                hashlib.sha256(
                    json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
                ).hexdigest()
                != status_digest
            ):
                return None
            if payload.get("index_sha256") is None:
                payload.pop("index_sha256", None)
            return payload

        expected_payload = governed_payload(expected)
        current_payload = governed_payload(current)
        return expected_payload is not None and expected_payload == current_payload
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


def _guard_commit(args: argparse.Namespace) -> int:
    """Refuse a commit while another writer holds this worktree's lease.

    Single-writer commit boundary (#3418 P4): a dispatched writer's
    commit/push landing while a second agent commits in the same worktree is
    the race behind the 2026-08 provenance-corruption incidents. Allowed:
    the lock is free (no writer), this process inherited the lease descriptor,
    or this process has the matching lease nonce and descends from its live
    owner. Blocked: a *foreign live* process holds the lock.
    """
    resolved = args.worktree.resolve()
    lock_path, owner_path = _lock_paths(resolved)
    if not lock_path.exists():
        return 0
    if _inherited_lease(lock_path) is not None:
        # We hold (or just acquired) it — release immediately; the commit
        # proceeds under our own ownership.
        return 0
    if _matches_owned_descendant(owner_path):
        # Some launchers (notably uv -> pre-commit) close inherited file
        # descriptors. The random owner identity survives only in descendants
        # and must still match the live lease record written under the flock.
        return 0
    fd = os.open(lock_path, os.O_RDWR | os.O_CLOEXEC)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            owner = _owner_summary(owner_path)
            print(
                f"worktree-guard: commit blocked — writer lease held by {owner} "
                "in this worktree. Wait for the dispatched writer to finish "
                "(job.sh wait-file on its terminal artifact) or run the "
                "orphan/lease check; never commit past an active writer.",
                file=sys.stderr,
            )
            return 1
        fcntl.flock(fd, fcntl.LOCK_UN)
        return 0
    finally:
        os.close(fd)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command_name", required=True)
    exec_parser = subparsers.add_parser("exec")
    exec_parser.add_argument("--worktree", type=Path, default=Path.cwd())
    exec_parser.add_argument("--boundary", required=True)
    exec_parser.add_argument("--timeout", type=float, default=300.0)
    exec_parser.add_argument("command", nargs=argparse.REMAINDER)
    guard_parser = subparsers.add_parser(
        "guard-commit",
        help="exit 1 when a foreign live writer holds this worktree's lease",
    )
    guard_parser.add_argument("--worktree", type=Path, default=Path.cwd())
    args = parser.parse_args(argv)
    try:
        if args.command_name == "guard-commit":
            return _guard_commit(args)
        return _exec_command(args)
    except WorktreeGuardError as exc:
        print(f"worktree-guard error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
