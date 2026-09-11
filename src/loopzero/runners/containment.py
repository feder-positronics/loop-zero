"""Authority and filesystem containment builders for runtime children.

These functions build data only: an allowlisted environment and bubblewrap
argv.  Process ownership, execution, retry, and routing remain in their
respective layers.
"""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
from collections.abc import Mapping, Sequence
from pathlib import Path, PurePosixPath

from .settings import RuntimeSettings, get_settings


class ContainmentError(RuntimeError):
    """The requested child boundary could not be built safely."""


_ALWAYS_REMOVED = frozenset({"GH_TOKEN", "GITHUB_TOKEN", "SSH_AUTH_SOCK"})
_UV_MANAGED_PYTHON_ROOT = Path.home() / ".local" / "share" / "uv" / "python"


def _conveys_parent_authority(name: str) -> bool:
    normalized = name.casefold()
    return name in _ALWAYS_REMOVED or "lease" in normalized or "nonce" in normalized


def worker_child_environment(
    base: Mapping[str, str] | None = None,
    *,
    writable_root: Path | None = None,
    extra: Mapping[str, str] | None = None,
    settings: RuntimeSettings | None = None,
) -> dict[str, str]:
    """Build a worker/probe environment without parent publication authority.

    Only exact names in :attr:`RuntimeSettings.child_env_allowlist` and locale
    category variables (``LC_*``) survive.  Lease/nonce names and common Git or
    SSH publication credentials are denied even if a consumer adds them to the
    allowlist.  ``extra`` is merged before filtering and therefore grants no
    bypass.
    """
    active = settings or get_settings()
    candidate = dict(os.environ if base is None else base)
    if extra is not None:
        candidate.update(extra)
    environment = {
        name: value
        for name, value in candidate.items()
        if (name in active.child_env_allowlist or name.startswith("LC_"))
        and not _conveys_parent_authority(name)
    }
    if writable_root is not None:
        resolved_root = writable_root.resolve()
        environment[active.env_name("OUTER_WORKER_SANDBOX")] = "1"
        raw_tmpdir = environment.get("TMPDIR")
        if raw_tmpdir is None or not Path(raw_tmpdir).resolve().is_relative_to(
            resolved_root
        ):
            environment["TMPDIR"] = str(resolved_root)
        raw_codex_home = environment.get("CODEX_HOME")
        if raw_codex_home is None or not Path(raw_codex_home).resolve().is_relative_to(
            resolved_root
        ):
            codex_home = Path(environment["TMPDIR"]) / "codex-home"
            codex_home.mkdir(mode=0o700, exist_ok=True)
            environment["CODEX_HOME"] = str(codex_home)
    return environment


def _writable_by_current_account(metadata: os.stat_result) -> bool:
    if os.geteuid() == 0:
        return metadata.st_uid != 0 or bool(
            metadata.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
        )
    if metadata.st_uid == os.geteuid():
        return bool(metadata.st_mode & stat.S_IWUSR)
    if metadata.st_gid in {os.getegid(), *os.getgroups()}:
        return bool(metadata.st_mode & stat.S_IWGRP)
    return bool(metadata.st_mode & stat.S_IWOTH)


def _system_executable(name: str) -> Path:
    """Resolve an account-immutable executable from the OS default path."""
    if not name or Path(name).name != name:
        raise ContainmentError("trusted executable name must be one basename")
    found = next(
        (
            Path(directory) / name
            for directory in os.defpath.split(os.pathsep)
            if (Path(directory) / name).is_file()
            and os.access(Path(directory) / name, os.X_OK)
        ),
        None,
    )
    if found is None:
        raise ContainmentError(f"worker isolation requires {name}")
    executable = found.resolve()
    try:
        metadata = tuple(
            component.stat() for component in (executable, *executable.parents)
        )
    except OSError as exc:
        raise ContainmentError(f"worker isolation requires {name}") from exc
    if (
        not executable.is_file()
        or not os.access(executable, os.X_OK)
        or any(_writable_by_current_account(item) for item in metadata)
    ):
        raise ContainmentError(f"trusted {name} executable is not protected")
    return executable


def _worker_runtime_read_roots(command: Sequence[str]) -> tuple[Path, ...]:
    """Select explicit, non-credential runtime roots required by worker CLIs."""
    roots: list[Path] = []
    for candidate in (Path.home() / ".local" / "bin", Path.home() / ".local" / "lib" / "nodejs"):
        if candidate.exists():
            roots.append(candidate.resolve())
    if command:
        requested = Path(command[0])
        executable = (
            str(requested)
            if requested.is_absolute() and requested.exists()
            else shutil.which(command[0])
        )
        if executable:
            invocation = Path(executable)
            target = invocation.resolve(strict=False)
            if _UV_MANAGED_PYTHON_ROOT.is_dir() and not _UV_MANAGED_PYTHON_ROOT.is_symlink():
                resolved_uv_root = _UV_MANAGED_PYTHON_ROOT.resolve()
                try:
                    lexical_target = invocation.readlink()
                except OSError:
                    lexical_target = None
                if lexical_target is not None:
                    if not lexical_target.is_absolute():
                        lexical_target = invocation.parent / lexical_target
                    try:
                        lexical_target.relative_to(_UV_MANAGED_PYTHON_ROOT)
                        target.relative_to(resolved_uv_root)
                    except ValueError:
                        pass
                    else:
                        roots.append(resolved_uv_root)
            for candidate in (invocation.parent, target.parent):
                resolved = candidate.resolve(strict=False)
                if resolved.exists() and not resolved.is_relative_to(Path("/usr")):
                    roots.append(resolved)
    for runtime_name in ("codex", "claude", "cursor-agent"):
        executable = shutil.which(runtime_name)
        if executable is None:
            continue
        target_parent = Path(executable).resolve(strict=False).parent
        if target_parent.exists() and not target_parent.is_relative_to(Path("/usr")):
            roots.append(target_parent)
    return tuple(dict.fromkeys(roots))


def _worker_mount_parent_args(paths: Sequence[Path]) -> list[str]:
    """Create only mount-point parents needed below the empty sandbox root."""
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


def _exact_repair_file_bindings(worktree: Path, paths: tuple[str, ...]) -> list[str]:
    bindings: list[str] = []
    if not paths or len(set(paths)) != len(paths):
        raise ContainmentError("review repair requires distinct exact files")
    for relative in paths:
        parsed = PurePosixPath(relative)
        if (
            parsed.is_absolute()
            or str(parsed) != relative
            or not parsed.parts
            or any(part in {".", ".."} for part in parsed.parts)
            or parsed.parts[0] in {".git", ".audit"}
            or any(char in relative for char in "*?\\:\x00\n\r")
        ):
            raise ContainmentError("unsafe review repair file")
        target = worktree
        for part in parsed.parts:
            target /= part
            if target.is_symlink():
                raise ContainmentError("review repair file has a symlink component")
        try:
            info = target.stat()
        except OSError as exc:
            raise ContainmentError("review repair requires existing regular files") from exc
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise ContainmentError("review repair file is not an unaliased regular file")
        bindings.extend(("--bind", str(target), str(target)))
    completed = subprocess.run(
        ["git", "-C", str(worktree), "ls-files", "--cached", "-z"],
        capture_output=True,
        check=False,
        env={"PATH": os.defpath, "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1"},
    )
    if completed.returncode != 0:
        raise ContainmentError("cannot verify tracked source files for review repair")
    tracked = {os.fsdecode(path) for path in completed.stdout.split(b"\0") if path}
    if not set(paths).issubset(tracked):
        raise ContainmentError("review repair requires tracked source files")
    return bindings


def worker_isolated_command(
    command: Sequence[str],
    *,
    writable_root: Path,
    readable_roots: Sequence[Path] = (),
    credential_bindings: Sequence[tuple[int, Path]] = (),
    repair_boundary: tuple[Path, tuple[str, ...], Path] | None = None,
) -> list[str]:
    """Build bubblewrap argv exposing only approved worker and runtime roots."""
    raw_socket = os.environ.get("SSH_AUTH_SOCK")
    hidden_directories: list[Path] = []
    if raw_socket:
        socket_path = Path(raw_socket)
        if not socket_path.is_absolute():
            raise ContainmentError("SSH-agent socket path must be absolute")
        resolved_socket = socket_path.resolve(strict=False)
        hidden_directories = sorted(
            {socket_path.parent, resolved_socket.parent}, key=str
        )
        unsafe_mounts = {
            Path("/"), Path("/tmp"), Path("/run"), Path("/run/user"),
            Path(f"/run/user/{os.getuid()}"),
        }
        if any(path in unsafe_mounts for path in hidden_directories):
            raise ContainmentError("SSH-agent socket directory is too broad")
    bubblewrap = _system_executable("bwrap")
    resolved_writable_root = writable_root.resolve()
    git_pointer = resolved_writable_root / ".git"
    protected_write_paths: list[Path] = []
    if git_pointer.is_symlink():
        raise ContainmentError("worker Git metadata cannot be a symlink")
    if git_pointer.is_file() or git_pointer.is_dir():
        protected_write_paths.append(git_pointer)
    candidate_read_roots = [
        *(path.resolve() for path in readable_roots if path.exists()),
        *_worker_runtime_read_roots(command),
    ]
    read_roots: list[Path] = []
    for candidate in candidate_read_roots:
        if candidate == resolved_writable_root or candidate.is_relative_to(resolved_writable_root):
            continue
        if any(candidate.is_relative_to(root) for root in read_roots):
            continue
        read_roots = [root for root in read_roots if not root.is_relative_to(candidate)]
        read_roots.append(candidate)
    system_files = tuple(
        path
        for path in (
            Path("/etc/ca-certificates"), Path("/etc/hosts"), Path("/etc/localtime"),
            Path("/etc/nsswitch.conf"), Path("/etc/passwd"), Path("/etc/group"),
            Path("/etc/resolv.conf"), Path("/etc/ssl"),
        )
        if path.exists()
    )
    mount_paths = [
        *system_files, *read_roots, resolved_writable_root, *protected_write_paths,
        *hidden_directories, *(path for _descriptor, path in credential_bindings),
    ]
    isolated = [
        str(bubblewrap), "--die-with-parent", "--unshare-pid", "--tmpfs", "/",
        "--dir", "/usr", "--ro-bind", "/usr", "/usr", "--symlink", "usr/bin", "/bin",
        "--symlink", "usr/sbin", "/sbin", "--symlink", "usr/lib", "/lib",
        "--symlink", "usr/lib64", "/lib64", "--tmpfs", "/tmp", "--tmpfs", "/run",
        *_worker_mount_parent_args(mount_paths), "--dev", "/dev", "--proc", "/proc",
    ]
    for system_path in system_files:
        isolated.extend(("--ro-bind", str(system_path), str(system_path)))
    for read_root in sorted(read_roots, key=str):
        isolated.extend(("--ro-bind", str(read_root), str(read_root)))
    isolated.extend(("--bind", str(resolved_writable_root), str(resolved_writable_root)))
    if repair_boundary is not None:
        source_root, source_files, scratch = repair_boundary
        if resolved_writable_root == source_root.resolve():
            raise ContainmentError("exact repair cannot expose a writable source root")
        isolated.extend(_exact_repair_file_bindings(source_root, source_files))
        isolated.extend(("--bind", str(scratch), str(scratch)))
    for protected_path in protected_write_paths:
        isolated.extend(("--ro-bind", str(protected_path), str(protected_path)))
    for directory in hidden_directories:
        isolated.extend(("--tmpfs", str(directory)))
    for descriptor, destination in credential_bindings:
        isolated.extend(
            ("--perms", "--perms", "0600", "--ro-bind-data", str(descriptor), str(destination))
        )
    return [*isolated, "--", *command]


__all__ = [
    "ContainmentError",
    "worker_child_environment",
    "worker_isolated_command",
]
