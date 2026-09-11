#!/usr/bin/env python3
"""Canonical host-side authority root for durable detached jobs."""

from __future__ import annotations

from .settings import settings

import argparse
import hashlib
import os
import pwd
import re
import stat
import subprocess
from collections.abc import Callable, Sequence
from pathlib import Path


class JobStoreError(RuntimeError):
    """The detached-job authority root cannot be derived safely."""


_PRIVATE_MODE = 0o700
_LEASE_MODE = 0o600
_JOB_NAME = re.compile(r"(?![.-])[A-Za-z0-9._-]+\Z")


def _reject_symlink_components(path: Path) -> None:
    current = Path(path.anchor)
    for part in path.parts[1:]:
        current /= part
        if current.is_symlink():
            raise JobStoreError("job authority root cannot contain a symlink")


def _account_home() -> Path:
    try:
        home = Path(pwd.getpwuid(os.getuid()).pw_dir)
    except (KeyError, OSError) as exc:
        raise JobStoreError("OS account home is unavailable") from exc
    if not home.is_absolute():
        raise JobStoreError("OS account home is invalid")
    return home


def _account_state_root() -> Path:
    return settings.account_state_root(_account_home()) / "jobs"


def _directory_state(path: Path) -> os.stat_result:
    try:
        state = path.lstat()
    except OSError as exc:
        raise JobStoreError(f"job authority directory is unavailable: {path}") from exc
    if stat.S_ISLNK(state.st_mode) or not stat.S_ISDIR(state.st_mode):
        raise JobStoreError(f"job authority component is not a directory: {path}")
    return state


def _validate_account_directory(path: Path) -> None:
    state = _directory_state(path)
    if state.st_uid != os.getuid():
        raise JobStoreError(
            f"job authority component is not owned by this account: {path}"
        )
    if stat.S_IMODE(state.st_mode) & 0o022:
        raise JobStoreError(
            f"job authority component is writable by another account: {path}"
        )


def _ensure_directory(path: Path, *, private: bool) -> None:
    try:
        path.mkdir(mode=_PRIVATE_MODE)
    except FileExistsError:
        pass
    except OSError as exc:
        raise JobStoreError(f"cannot create job authority directory: {path}") from exc

    state = _directory_state(path)
    if state.st_uid != os.getuid():
        raise JobStoreError(
            f"job authority directory is not owned by this account: {path}"
        )
    if private and stat.S_IMODE(state.st_mode) != _PRIVATE_MODE:
        try:
            path.chmod(_PRIVATE_MODE)
        except OSError as exc:
            raise JobStoreError(
                f"cannot protect job authority directory: {path}"
            ) from exc
        state = _directory_state(path)
        if state.st_uid != os.getuid() or stat.S_IMODE(state.st_mode) != _PRIVATE_MODE:
            raise JobStoreError(
                f"job authority directory permissions are invalid: {path}"
            )


def _ensure_private_directory_chain(
    path: Path, *, allow_group_writable_owned_ancestors: bool = False
) -> None:
    """Create an absolute directory chain through pinned, safe descriptors."""
    flags = (
        os.O_RDONLY
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    try:
        descriptor = os.open(path.anchor, flags)
    except OSError as exc:
        raise JobStoreError("job authority root anchor is unavailable") from exc
    account_boundary_seen = False
    current = Path(path.anchor)
    try:
        components = path.parts[1:]
        for index, part in enumerate(components):
            current /= part
            is_final = index == len(components) - 1
            created = False
            try:
                child = os.open(part, flags, dir_fd=descriptor)
            except FileNotFoundError:
                try:
                    os.mkdir(part, mode=_PRIVATE_MODE, dir_fd=descriptor)
                    created = True
                    path_flags = (
                        getattr(os, "O_PATH", 0)
                        | os.O_DIRECTORY
                        | getattr(os, "O_NOFOLLOW", 0)
                    )
                    if not getattr(os, "O_PATH", 0):
                        raise JobStoreError(
                            "private job authority creation requires O_PATH"
                        )
                    pinned = os.open(part, path_flags, dir_fd=descriptor)
                    try:
                        os.chmod(f"/proc/self/fd/{pinned}", _PRIVATE_MODE)
                    finally:
                        os.close(pinned)
                    child = os.open(part, flags, dir_fd=descriptor)
                except OSError as exc:
                    raise JobStoreError(
                        f"cannot create job authority directory: {current}"
                    ) from exc
            except OSError as exc:
                raise JobStoreError(
                    f"job authority component is not a directory: {current}"
                ) from exc

            try:
                state = os.fstat(child)
                if not stat.S_ISDIR(state.st_mode):
                    raise JobStoreError(
                        f"job authority component is not a directory: {current}"
                    )
                mode = stat.S_IMODE(state.st_mode)
                if state.st_uid == os.getuid():
                    account_boundary_seen = True
                    writable_mask = (
                        0o002 if allow_group_writable_owned_ancestors else 0o022
                    )
                    if mode & writable_mask and not is_final:
                        raise JobStoreError(
                            "job authority component is writable by another account: "
                            f"{current}"
                        )
                    if is_final and mode != _PRIVATE_MODE:
                        os.fchmod(child, _PRIVATE_MODE)
                elif account_boundary_seen or (
                    mode & 0o022 and not mode & stat.S_ISVTX
                ):
                    raise JobStoreError(
                        f"job authority component is not owned by this account: {current}"
                    )
                if created and state.st_uid != os.getuid():
                    raise JobStoreError(
                        f"job authority directory is not owned by this account: {current}"
                    )
            except Exception:
                os.close(child)
                raise
            os.close(descriptor)
            descriptor = child

        state = os.fstat(descriptor)
        if state.st_uid != os.getuid():
            raise JobStoreError(
                f"job authority directory is not owned by this account: {path}"
            )
        if stat.S_IMODE(state.st_mode) != _PRIVATE_MODE:
            os.fchmod(descriptor, _PRIVATE_MODE)
        lexical = path.lstat()
        final = os.fstat(descriptor)
        if final.st_uid != os.getuid() or stat.S_IMODE(final.st_mode) != _PRIVATE_MODE:
            raise JobStoreError(
                f"job authority directory permissions are invalid: {path}"
            )
        if (lexical.st_dev, lexical.st_ino) != (final.st_dev, final.st_ino):
            raise JobStoreError(
                "job authority directory identity changed during creation"
            )
    except OSError as exc:
        raise JobStoreError(f"job authority directory is unavailable: {path}") from exc
    finally:
        os.close(descriptor)


def canonical_job_root(worktree: Path, *, configured: str | None = None) -> Path:
    """Return one canonical root; legacy recovery requires an explicit override."""
    root = worktree.resolve()
    raw = os.environ.get(settings.env("JOB_DIR")) if configured is None else configured
    if raw:
        candidate = Path(raw)
        if not candidate.is_absolute():
            candidate = root / candidate
    else:
        key = hashlib.sha256(str(root).encode("utf-8")).hexdigest()[:16]
        candidate = _account_state_root() / key
    candidate = candidate.absolute()
    _reject_symlink_components(candidate)
    return candidate


def ensure_job_root(worktree: Path, *, configured: str | None = None) -> Path:
    """Create and validate the account-owned job authority boundary."""
    root = canonical_job_root(worktree, configured=configured)
    raw = os.environ.get(settings.env("JOB_DIR")) if configured is None else configured
    if raw:
        # Pin every component while creating the operator-selected recovery root.
        # This preserves the escape hatch without trusting umask or a path that
        # another account can rename between validation and creation.
        _ensure_private_directory_chain(root)
        return root

    _validate_account_directory(_account_home())
    _ensure_private_directory_chain(root)
    return root


def ensure_job_directory(
    worktree: Path, name: str, *, configured: str | None = None
) -> Path:
    """Create one private job directory below the canonical authority root."""
    if not _JOB_NAME.fullmatch(name):
        raise JobStoreError("job name is invalid")
    directory = ensure_job_root(worktree, configured=configured) / name
    _ensure_directory(directory, private=True)
    return directory


def ensure_job_lease(
    worktree: Path, name: str, *, configured: str | None = None
) -> Path:
    """Return one stable private lease inode for a job name.

    Job directories are replaceable after their result is reaped.  The lease
    authority therefore lives in a sibling directory that cleanup never
    removes, so every launcher, supervisor, reconciler, and cleaner contends on
    the same inode across job-directory generations.
    """
    if not _JOB_NAME.fullmatch(name):
        raise JobStoreError("job name is invalid")
    lease_root = ensure_job_root(worktree, configured=configured) / ".leases"
    _ensure_directory(lease_root, private=True)
    lease = lease_root / f"{name}.lock"
    flags = os.O_RDWR | os.O_CREAT
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(lease, flags, _LEASE_MODE)
    except OSError as exc:
        raise JobStoreError(f"cannot open stable job lease: {lease}") from exc
    try:
        state = os.fstat(descriptor)
        if not stat.S_ISREG(state.st_mode):
            raise JobStoreError(f"job lease is not a regular file: {lease}")
        if state.st_uid != os.getuid():
            raise JobStoreError(f"job lease is not owned by this account: {lease}")
        if stat.S_IMODE(state.st_mode) != _LEASE_MODE:
            try:
                os.fchmod(descriptor, _LEASE_MODE)
            except OSError as exc:
                raise JobStoreError(
                    f"cannot protect stable job lease: {lease}"
                ) from exc
            state = os.fstat(descriptor)
            if (
                state.st_uid != os.getuid()
                or stat.S_IMODE(state.st_mode) != _LEASE_MODE
            ):
                raise JobStoreError(f"job lease permissions are invalid: {lease}")
        current = lease.lstat()
        if (state.st_dev, state.st_ino) != (current.st_dev, current.st_ino):
            raise JobStoreError(f"job lease pathname changed while opening: {lease}")
    finally:
        os.close(descriptor)
    return lease


def _canonical_candidate_protection_paths() -> tuple[Path, ...]:
    tool_repository = Path(__file__).resolve().parents[2]
    completed = subprocess.run(
        [
            "/usr/bin/git",
            "-C",
            str(tool_repository),
            "rev-parse",
            "--path-format=absolute",
            "--show-toplevel",
            "--git-dir",
            "--git-common-dir",
        ],
        capture_output=True,
        text=True,
        check=False,
        env={
            "GIT_CONFIG_GLOBAL": "/dev/null",
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_SYSTEM": "/dev/null",
            "GIT_NO_REPLACE_OBJECTS": "1",
            "GIT_TERMINAL_PROMPT": "0",
            "HOME": "/tmp",
            "LANG": "C.UTF-8",
            "LC_ALL": "C.UTF-8",
            "PATH": "/usr/bin:/bin",
        },
    )
    if completed.returncode != 0:
        raise JobStoreError("canonical continuation candidate root is unavailable")
    metadata = completed.stdout.splitlines()
    if len(metadata) != 3:
        raise JobStoreError("canonical continuation candidate root is unavailable")
    repository = Path(metadata[0]).resolve()
    git_dir = Path(metadata[1]).resolve()
    common_dir = Path(metadata[2]).resolve()
    primary = common_dir.parent
    candidate_store = (
        primary / settings.audit_root / "delivery-continuations" / "candidates"
    )
    _ensure_private_directory_chain(
        candidate_store, allow_group_writable_owned_ancestors=True
    )
    protected = [repository / ".git"]
    if git_dir not in protected:
        protected.append(git_dir)
    if common_dir not in protected:
        protected.append(common_dir)
    protected.append(candidate_store)
    for path in protected[:-1]:
        try:
            state = path.lstat()
        except OSError as exc:
            raise JobStoreError("canonical Git metadata is unavailable") from exc
        if stat.S_ISLNK(state.st_mode) or not (
            stat.S_ISREG(state.st_mode) or stat.S_ISDIR(state.st_mode)
        ):
            raise JobStoreError(f"canonical Git metadata is unsafe: {path}")
    return tuple(protected)


def _protected_read_only_mounts(
    paths: Sequence[Path], *, account_home: Path
) -> list[str]:
    mounts: list[str] = []
    bound: set[Path] = set()
    for protected in paths:
        if not protected.is_absolute() or protected == Path("/"):
            raise JobStoreError("protected read-only path is invalid")
        for ancestor in reversed(protected.parents):
            if (
                ancestor == Path("/")
                or ancestor == account_home
                or ancestor in account_home.parents
                or ancestor in bound
            ):
                continue
            mounts.extend(("--bind", str(ancestor), str(ancestor)))
            bound.add(ancestor)
        mounts.extend(("--ro-bind", str(protected), str(protected)))
    return mounts


def build_bound_sandbox_arguments(
    bubblewrap: str,
    *,
    protected_authority_root: Path,
    working_directory: Path,
    command: Sequence[str],
    account_home: Path | None = None,
    protected_read_only_paths: Sequence[Path] | None = None,
    list_directory: Callable[[Path], list[str]] | None = None,
    read_symlink_target: Callable[[Path], str | None] | None = None,
) -> list[str]:
    """Build the bound-job bubblewrap invocation over a synthesized root.

    Unprivileged bubblewrap always runs in a single-uid user namespace, so
    binding the real ``/`` surfaces root-owned components such as ``/`` and
    ``/home`` as the unmapped overflow uid, which correctly fails the
    authority ledger's descriptor-anchored ancestor-ownership walk and the
    trusted-executable ancestor-writability walk. Instead, ``/`` becomes a
    0555 tmpfs remounted read-only, each strict ancestor of the account home
    becomes an owned 0555 directory whose real sibling entries are bound
    back, and every other top-level entry keeps its normal host bind, so
    both walks stay sound inside the sandbox without relaxing either check.
    Anti-forgery is preserved: every ancestor of the job collection is either
    a bind mount point or a directory inside the read-only tmpfs root, so a
    child cannot rename an ancestor, recreate the lexical job path, and feed
    reconciliation forged files. The shared default jobs root is also bound
    read-only, closing writes into another worktree's authority collection.
    Additional trusted-input stores, including continuation candidates, are
    overlaid read-only after the account-home bind.
    """
    if account_home is None:
        try:
            account_home = _account_home().resolve(strict=True)
        except OSError as exc:
            raise JobStoreError("OS account home is unavailable") from exc
    if not account_home.is_absolute() or account_home == Path("/"):
        raise JobStoreError("bound job supervision requires a private account home")

    if list_directory is None:

        def list_directory(path: Path) -> list[str]:
            return sorted(os.listdir(path))

    if read_symlink_target is None:

        def read_symlink_target(path: Path) -> str | None:
            return os.readlink(path) if path.is_symlink() else None

    def bind_entry(source: Path) -> list[str]:
        target = read_symlink_target(source)
        if target is not None:
            return ["--symlink", target, str(source)]
        return ["--bind", str(source), str(source)]

    home_chain = [
        *(ancestor for ancestor in reversed(account_home.parents) if ancestor != Path("/")),
        account_home,
    ]
    home_top = home_chain[0]
    try:
        root_entries = list_directory(Path("/"))
    except OSError as exc:
        raise JobStoreError("cannot enumerate the host root for the bound sandbox") from exc
    root_view: list[str] = []
    for entry in root_entries:
        entry_path = Path("/", entry)
        if entry_path in (Path("/proc"), Path("/dev")):
            continue  # replaced by --proc and --dev-bind below
        if entry_path == home_top:
            continue  # synthesized with its real entries below
        root_view.extend(bind_entry(entry_path))
    synthesized_directories: set[Path] = set()

    def synthesize_directory(path: Path) -> None:
        if path not in synthesized_directories:
            root_view.extend(("--perms", "0555", "--dir", str(path)))
            synthesized_directories.add(path)

    for index, ancestor in enumerate(home_chain[:-1]):
        synthesize_directory(ancestor)
        next_component = home_chain[index + 1]
        try:
            entry_names = list_directory(ancestor)
        except PermissionError:
            # Execute permission is enough to reach the known account home.
            synthesize_directory(next_component)
            continue
        except OSError as exc:
            raise JobStoreError(
                f"cannot enumerate a home ancestor for the bound sandbox: {ancestor}"
            ) from exc
        for name in entry_names:
            entry_path = ancestor / name
            if entry_path == next_component:
                continue
            root_view.extend(bind_entry(entry_path))
    root_view.extend(("--bind", str(account_home), str(account_home)))

    authority_mounts: list[str] = []
    for ancestor in reversed(protected_authority_root.parents):
        if (
            ancestor == Path("/")
            or ancestor == account_home
            or ancestor in account_home.parents
        ):
            # Already unrenameable: the account home is a bind mount point and
            # its strict ancestors live in the read-only tmpfs root.
            continue
        authority_mounts.extend(("--bind", str(ancestor), str(ancestor)))
    shared_jobs_root = settings.account_state_root(account_home) / "jobs"
    shared_root_mount: list[str] = []
    if protected_authority_root.parent == shared_jobs_root:
        shared_root_mount.extend(
            ("--ro-bind", str(shared_jobs_root), str(shared_jobs_root))
        )
    if protected_read_only_paths is None:
        protected_read_only_paths = _canonical_candidate_protection_paths()
    protected_mounts = _protected_read_only_mounts(
        protected_read_only_paths,
        account_home=account_home,
    )
    return [
        bubblewrap,
        "--die-with-parent",
        "--new-session",
        "--unshare-pid",
        # 0555 keeps synthesized components outside every ancestor
        # account-writability walk (trusted_executable.py); the trailing
        # --remount-ro is the enforcement, the mode is the attestation.
        "--perms",
        "0555",
        "--tmpfs",
        "/",
        *root_view,
        *authority_mounts,
        *shared_root_mount,
        *protected_mounts,
        "--ro-bind",
        str(protected_authority_root),
        str(protected_authority_root),
        "--proc",
        "/proc",
        "--dev-bind",
        "/dev",
        "/dev",
        "--remount-ro",
        "/",
        "--chdir",
        str(working_directory),
        "--",
        *command,
    ]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--worktree", type=Path, required=True)
    action = parser.add_mutually_exclusive_group()
    action.add_argument("--ensure-root", action="store_true")
    action.add_argument("--ensure-job-directory")
    action.add_argument("--ensure-job-lease")
    args = parser.parse_args()
    try:
        if args.ensure_job_lease:
            path = ensure_job_lease(args.worktree, args.ensure_job_lease)
        elif args.ensure_job_directory:
            path = ensure_job_directory(args.worktree, args.ensure_job_directory)
        elif args.ensure_root:
            path = ensure_job_root(args.worktree)
        else:
            path = canonical_job_root(args.worktree)
        print(path)
    except JobStoreError as exc:
        parser.error(str(exc))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
