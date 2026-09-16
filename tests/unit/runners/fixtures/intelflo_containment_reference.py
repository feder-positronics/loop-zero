"""Pinned IntelFlo argv reference; see README.md for provenance and scope."""

from collections.abc import Sequence
import os
from pathlib import Path


class DispatchError(RuntimeError):
    pass


class TrustedExecutableError(RuntimeError):
    pass


def system_executable(name):
    raise AssertionError("The test must provide a fixed executable")


def _worker_runtime_read_roots(command):
    raise AssertionError("The test must provide fixed runtime roots")


def exact_repair_file_bindings(*args):
    raise AssertionError("Repair mounts are outside this reference scenario")


def _worker_mount_parent_args(paths: Sequence[Path]) -> list[str]:
    """Create only the mount-point parents needed below the empty sandbox root."""
    parents: set[Path] = set()
    for path in paths:
        for parent in path.parents:
            if parent == Path("/"):
                break
            if parent == Path("/usr") or parent.is_relative_to(Path("/usr")):
                continue
            parents.add(parent)
    result: list[str] = []
    for parent in sorted(parents, key=lambda item: (len(item.parts), str(item))):
        result.extend(("--dir", str(parent)))
    return result


def worker_isolated_command(
    command: Sequence[str],
    *,
    writable_root: Path,
    readable_roots: Sequence[Path] = (),
    credential_bindings: Sequence[tuple[int, Path]] = (),
    repair_boundary: tuple[Path, tuple[str, ...], Path] | None = None,
) -> list[str]:
    """Expose only approved worker source, runtime, and system read roots."""
    raw_socket = os.environ.get("SSH_AUTH_SOCK")
    hidden_directories: list[Path] = []
    if raw_socket:
        socket_path = Path(raw_socket)
        if not socket_path.is_absolute():
            raise DispatchError("SSH-agent socket path must be absolute")
        resolved_socket = socket_path.resolve(strict=False)
        hidden_directories = sorted(
            {socket_path.parent, resolved_socket.parent}, key=lambda path: str(path)
        )
        unsafe_mounts = {
            Path("/"),
            Path("/tmp"),
            Path("/run"),
            Path("/run/user"),
            Path(f"/run/user/{os.getuid()}"),
        }
        if any(path in unsafe_mounts for path in hidden_directories):
            raise DispatchError("SSH-agent socket directory is too broad")
    try:
        bubblewrap = system_executable("bwrap")
    except TrustedExecutableError as exc:
        raise DispatchError("worker isolation requires bubblewrap") from exc
    resolved_writable_root = writable_root.resolve()
    git_pointer = resolved_writable_root / ".git"
    protected_write_paths: list[Path] = []
    if git_pointer.is_symlink():
        raise DispatchError("worker Git metadata cannot be a symlink")
    if git_pointer.is_file() or git_pointer.is_dir():
        protected_write_paths.append(git_pointer)
    candidate_read_roots = [
        *(path.resolve() for path in readable_roots if path.exists()),
        *_worker_runtime_read_roots(command),
    ]
    read_roots: list[Path] = []
    for candidate in candidate_read_roots:
        if candidate == resolved_writable_root:
            continue
        if candidate.is_relative_to(resolved_writable_root):
            # The task owns its writable root, including runtime tooling within it.
            # Explicit protected paths such as a linked-worktree .git pointer are
            # collected independently and remain read-only.
            continue
        if any(candidate.is_relative_to(root) for root in read_roots):
            continue
        read_roots = [root for root in read_roots if not root.is_relative_to(candidate)]
        read_roots.append(candidate)
    system_files = tuple(
        path
        for path in (
            Path("/etc/ca-certificates"),
            Path("/etc/hosts"),
            Path("/etc/localtime"),
            Path("/etc/nsswitch.conf"),
            Path("/etc/passwd"),
            Path("/etc/group"),
            Path("/etc/resolv.conf"),
            Path("/etc/ssl"),
        )
        if path.exists()
    )
    mount_paths = [
        *system_files,
        *read_roots,
        resolved_writable_root,
        *protected_write_paths,
        *hidden_directories,
        *(path for _descriptor, path in credential_bindings),
    ]
    isolated = [
        str(bubblewrap),
        "--die-with-parent",
        "--unshare-pid",
        "--tmpfs",
        "/",
        "--dir",
        "/usr",
        "--ro-bind",
        "/usr",
        "/usr",
        "--symlink",
        "usr/bin",
        "/bin",
        "--symlink",
        "usr/sbin",
        "/sbin",
        "--symlink",
        "usr/lib",
        "/lib",
        "--symlink",
        "usr/lib64",
        "/lib64",
        "--tmpfs",
        "/tmp",
        "--tmpfs",
        "/run",
        *_worker_mount_parent_args(mount_paths),
        "--dev",
        "/dev",
        "--proc",
        "/proc",
    ]
    for system_path in system_files:
        isolated.extend(("--ro-bind", str(system_path), str(system_path)))
    for read_root in sorted(read_roots, key=str):
        isolated.extend(("--ro-bind", str(read_root), str(read_root)))
    isolated.extend(
        ("--bind", str(resolved_writable_root), str(resolved_writable_root))
    )
    if repair_boundary is not None:
        source_root, source_files, scratch = repair_boundary
        if resolved_writable_root == source_root.resolve():
            raise DispatchError("exact repair cannot expose a writable source root")
        isolated.extend(exact_repair_file_bindings(source_root, source_files))
        isolated.extend(("--bind", str(scratch), str(scratch)))
    for protected_path in protected_write_paths:
        isolated.extend(("--ro-bind", str(protected_path), str(protected_path)))
    for directory in hidden_directories:
        isolated.extend(("--tmpfs", str(directory)))
    for descriptor, destination in credential_bindings:
        isolated.extend(
            (
                "--perms",
                "0600",
                "--ro-bind-data",
                str(descriptor),
                str(destination),
            )
        )
    return [*isolated, "--", *command]
