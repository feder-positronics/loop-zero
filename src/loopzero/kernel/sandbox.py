#!/usr/bin/env python3
"""Allowlisted Bubblewrap boundary for Guardian-controlled untrusted execution."""

from __future__ import annotations

from .settings import settings

import errno
import fcntl
import grp
import json
import os
import pwd
import stat
import sys
import tempfile
from collections import deque
from collections.abc import Mapping, Sequence
from contextlib import contextmanager
from pathlib import Path


from .trusted_exec import TrustedExecutableError, system_executable

CODEX_AUTH_FD_ENV = settings.env("CODEX_AUTH_FD")
SANDBOX_BOUNDARY_ENV = settings.env("GUARDIAN_SANDBOX_BOUNDARY")
NETWORK_DENIED_BOUNDARY = "network-denied"
HOST_NETWORK_BOUNDARY = "host-network"
GUARDIAN_NODE_ROOT = settings.sandbox_node_root
GUARDIAN_BIN_ROOT = settings.sandbox_bin_root
GUARDIAN_COREPACK_HOME = settings.sandbox_corepack_home
_DEFAULT_SANDBOX_PATH = f"{GUARDIAN_BIN_ROOT}:/usr/bin:/bin"
SAFE_PASSTHROUGH_ENV = settings.sandbox_passthrough | {SANDBOX_BOUNDARY_ENV}


class SandboxError(RuntimeError):
    """The untrusted execution namespace could not be constructed safely."""


class UnsafeCredentialError(SandboxError):
    """A provider credential exists but violates the owner-only safety contract."""


MAX_CREDENTIAL_BYTES = 1024 * 1024


def _credential_snapshot_payload(descriptor: int) -> bytes:
    """Validate broker output and remove durable refresh authority."""
    if isinstance(descriptor, bool) or not isinstance(descriptor, int) or descriptor < 0:
        raise UnsafeCredentialError("Codex credential descriptor is invalid")
    try:
        metadata = os.fstat(descriptor)
    except OSError as exc:
        raise UnsafeCredentialError("Codex credential descriptor is unavailable") from exc
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_uid != os.getuid()
        or stat.S_IMODE(metadata.st_mode) & 0o077
        or metadata.st_size <= 2
        or metadata.st_size > MAX_CREDENTIAL_BYTES
    ):
        raise UnsafeCredentialError("Codex credential descriptor is unsafe")
    try:
        payload = os.pread(descriptor, metadata.st_size, 0)
        decoded = json.loads(payload)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise UnsafeCredentialError("Codex credential JSON is invalid") from exc
    if len(payload) != metadata.st_size or not isinstance(decoded, dict):
        raise UnsafeCredentialError("Codex credential snapshot is incomplete")
    if decoded.get("auth_mode") != "chatgpt" or decoded.get("OPENAI_API_KEY") not in (None, ""):
        raise UnsafeCredentialError("Codex ChatGPT credential is unsafe")
    tokens = decoded.get("tokens")
    if not isinstance(tokens, dict):
        raise UnsafeCredentialError("Codex token set is missing")
    for name in ("access_token", "id_token", "refresh_token", "account_id"):
        if not isinstance(tokens.get(name), str) or not tokens[name]:
            raise UnsafeCredentialError(f"Codex credential {name} is invalid")
    # The access token already bounds the child to the current login lifetime.
    # Substituting it for the refresh token preserves the Codex file schema while
    # removing the durable capability, byte-for-byte with the original broker.
    tokens["refresh_token"] = tokens["access_token"]
    return json.dumps(decoded, separators=(",", ":")).encode("utf-8")


def _sealed_snapshot(payload: bytes) -> int:
    if hasattr(os, "memfd_create"):
        descriptor = os.memfd_create(
            "loopzero-codex-auth", os.MFD_CLOEXEC | os.MFD_ALLOW_SEALING
        )
        try:
            written = 0
            while written < len(payload):
                written += os.write(descriptor, payload[written:])
            os.fchmod(descriptor, 0o400)
            fcntl.fcntl(
                descriptor,
                fcntl.F_ADD_SEALS,
                fcntl.F_SEAL_WRITE
                | fcntl.F_SEAL_GROW
                | fcntl.F_SEAL_SHRINK
                | fcntl.F_SEAL_SEAL,
            )
            os.lseek(descriptor, 0, os.SEEK_SET)
            return descriptor
        except BaseException:
            os.close(descriptor)
            raise
    writable, raw_path = tempfile.mkstemp(prefix=settings.temp_prefix + "codex-auth-")
    path = Path(raw_path)
    try:
        written = 0
        while written < len(payload):
            written += os.write(writable, payload[written:])
        os.fchmod(writable, 0o400)
        readonly = os.open(path, os.O_RDONLY | getattr(os, "O_CLOEXEC", 0))
    finally:
        os.close(writable)
        path.unlink(missing_ok=True)
    return readonly


@contextmanager
def codex_subscription_credential(*, requested_runtime_s: float, credential_broker=None):
    """Lend one kernel-validated, refresh-free, sealed credential snapshot."""
    if requested_runtime_s <= 0:
        raise ValueError("requested_runtime_s must be positive")
    if credential_broker is None:
        raise SandboxError("credential_broker is required")
    with credential_broker(requested_runtime_s=requested_runtime_s) as descriptor:
        snapshot = _sealed_snapshot(_credential_snapshot_payload(descriptor))
    try:
        yield snapshot
    finally:
        os.close(snapshot)


def environment(source: Mapping[str, str] | None = None) -> dict[str, str]:
    original = os.environ if source is None else source
    selected = {
        name: value for name, value in original.items() if name in SAFE_PASSTHROUGH_ENV
    }
    selected.update(
        {
            "HOME": str(settings.sandbox_home),
            "PATH": _sandbox_path(),
            "GIT_CONFIG_COUNT": "3",
            "GIT_CONFIG_KEY_0": "core.hooksPath",
            "GIT_CONFIG_VALUE_0": "/dev/null",
            "GIT_CONFIG_KEY_1": "credential.https://github.com.helper",
            "GIT_CONFIG_VALUE_1": "",
            "GIT_CONFIG_KEY_2": "credential.https://gist.github.com.helper",
            "GIT_CONFIG_VALUE_2": "",
            "GIT_CONFIG_GLOBAL": "/dev/null",
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_SYSTEM": "/dev/null",
            "GIT_NO_REPLACE_OBJECTS": "1",
            "GIT_TERMINAL_PROMPT": "0",
            "GH_CONFIG_DIR": "/tmp/guardian-gh-config",
            "TMPDIR": "/tmp",
            "UV_CACHE_DIR": "/tmp/guardian-uv-cache",
            "XDG_CACHE_HOME": "/tmp/guardian-xdg-cache",
        }
    )
    if _mounted_corepack_home_available():
        selected["COREPACK_HOME"] = str(GUARDIAN_COREPACK_HOME)
    return selected


def _parents(path: Path) -> list[Path]:
    current = path.parent
    parents: list[Path] = []
    while current != Path("/"):
        parents.append(current)
        current = current.parent
    return list(reversed(parents))


_KERNEL_FILESYSTEM_ROOTS = (Path("/proc"), Path("/sys"), Path("/dev"))


def _reject_kernel_filesystem_path(path: Path, *, label: str) -> None:
    lexical = Path(os.path.abspath(path))
    if lexical == Path("/") or any(
        lexical == root or lexical.is_relative_to(root)
        for root in _KERNEL_FILESYSTEM_ROOTS
    ):
        raise SandboxError(
            f"{label} cannot be located at / or under /proc, /sys, or /dev"
        )


def _validated_destination(path: Path, *, label: str) -> Path:
    """Validate every existing component of a lexical mount destination."""
    lexical = Path(os.path.abspath(path))
    _reject_kernel_filesystem_path(lexical, label=label)
    current = Path("/")
    parts = lexical.parts[1:]
    for index, part in enumerate(parts):
        current /= part
        try:
            state = os.lstat(current)
        except FileNotFoundError:
            break
        except OSError as exc:
            raise SandboxError(f"{label} is unavailable") from exc
        if stat.S_ISLNK(state.st_mode):
            raise SandboxError(f"{label} contains a symlinked component")
        if index < len(parts) - 1 and not stat.S_ISDIR(state.st_mode):
            raise SandboxError(f"{label} is unavailable")
    try:
        resolved = lexical.resolve(strict=False)
    except (OSError, RuntimeError) as exc:
        raise SandboxError(f"{label} is unavailable") from exc
    _reject_kernel_filesystem_path(resolved, label=label)
    return lexical


def _validated(path: Path, *, directory: bool | None = None) -> Path:
    if path.is_symlink():
        raise SandboxError("sandbox mount source contains a direct symlink")
    resolved = path.resolve()
    _reject_kernel_filesystem_path(resolved, label="sandbox mount source")
    if directory is True and not resolved.is_dir():
        raise SandboxError("sandbox directory mount source is unavailable")
    if directory is False and not resolved.is_file():
        raise SandboxError("sandbox file mount source is unavailable")
    if directory is None and not resolved.exists():
        raise SandboxError("sandbox mount source is unavailable")
    return resolved


def _private_group(group_id: int) -> bool:
    """Accept owner-private groups, never shared group write authority."""
    try:
        current = pwd.getpwuid(os.geteuid())
        group = grp.getgrgid(group_id)
        members = {
            entry.pw_name for entry in pwd.getpwall() if entry.pw_gid == group_id
        }
    except (KeyError, OSError):
        return False
    return members.union(group.gr_mem) <= {current.pw_name}


def _acl_masked(path: Path) -> bool:
    """Group mode bits are the ACL mask, not owning-group rights, once ACL entries exist."""
    try:
        return "system.posix_acl_access" in os.listxattr(path)
    except OSError as exc:
        # A filesystem without extended attributes cannot carry ACL entries; any
        # other failure leaves the group bits unexplained, so fail closed.
        return exc.errno != errno.ENOTSUP


def _tool_path_components(path: Path) -> tuple[Path, set[Path]]:
    """Retain every symlink hop instead of discarding it with resolve()."""
    remaining = deque(path.absolute().parts[1:])
    current = Path("/")
    components = {current}
    links = 0
    while remaining:
        part = remaining.popleft()
        current = current.parent if part == ".." else current / part
        components.add(current)
        if current.is_symlink():
            links += 1
            if links > 40:
                raise SandboxError("sandbox runtime tool has too many symlink hops")
            target = Path(os.readlink(current))
            current = Path("/") if target.is_absolute() else current.parent
            parts = target.parts[1:] if target.is_absolute() else target.parts
            remaining.extendleft(reversed(parts))
    return current, components


def _protected_tool_path(path: Path, *, forbidden_roots: Sequence[Path]) -> Path:
    """Protect provider credentials and acceptance execution from PATH redirection."""
    lexical = path.absolute()
    resolved, traversed = _tool_path_components(path)
    roots = tuple(root.resolve() for root in forbidden_roots)
    # Checking every ancestor also catches a PATH directory alias into the
    # worktree whose executable symlink points back out to external scratch.
    components = {
        lexical,
        *lexical.parents,
        resolved,
        *resolved.parents,
        *traversed,
    }
    for component in components:
        target = component.resolve()
        if any(
            component.is_relative_to(root) or target.is_relative_to(root)
            for root in roots
        ):
            raise SandboxError(
                f"sandbox runtime tool is under a writable sandbox source: {path.name}"
            )
    # The host supplies the runtime installation. Outer host directories (for
    # example the hosted runner's 0777 tool cache) are not worker authority
    # unless exposed by a writable mount, which the ancestry check above rejects.
    # Check the launcher and its containing directory, not every host ancestor.
    for component in {lexical, lexical.parent, resolved, resolved.parent}:
        metadata = component.stat()
        # A root-owned sticky ancestor (/tmp) protects an owner-private child
        # from replacement; it is never acceptable as the executable itself.
        sticky_ancestor = (
            component != lexical
            and component != resolved
            and stat.S_ISDIR(metadata.st_mode)
            and metadata.st_uid == 0
            and metadata.st_mode & stat.S_ISVTX
        )
        shared_write = bool(metadata.st_mode & stat.S_IWOTH) or (
            bool(metadata.st_mode & stat.S_IWGRP)
            and (_acl_masked(component) or not _private_group(metadata.st_gid))
        )
        if metadata.st_uid not in {0, os.geteuid()} or (
            shared_write and not sticky_ancestor
        ):
            raise SandboxError(f"sandbox runtime tool is not protected: {path.name}")
    return resolved


def _tool(name: str, *, forbidden_roots: Sequence[Path] = ()) -> Path:
    found = next(
        (
            Path(directory) / name
            for directory in os.environ.get("PATH", os.defpath).split(os.pathsep)
            if (Path(directory) / name).is_file()
            and os.access(Path(directory) / name, os.X_OK)
        ),
        None,
    )
    if found is None:
        raise SandboxError(f"sandbox runtime tool is unavailable: {name}")
    return _protected_tool_path(found, forbidden_roots=forbidden_roots)


def _system_tool(name: str) -> Path:
    try:
        return system_executable(name)
    except TrustedExecutableError as exc:
        raise SandboxError(str(exc)) from exc


def _optional_tool(name: str, *, forbidden_roots: Sequence[Path] = ()) -> Path | None:
    """Resolve an optional model runtime without weakening command isolation."""
    try:
        return _tool(name, forbidden_roots=forbidden_roots)
    except SandboxError:
        return None


def _sandbox_path(*, corepack_runtime: Path | None = None) -> str:
    """Sanitized PATH: optional Node/Corepack bin, then Guardian tools, then OS."""
    if corepack_runtime is not None or _mounted_corepack_runtime_available():
        return f"{GUARDIAN_NODE_ROOT / 'bin'}:{_DEFAULT_SANDBOX_PATH}"
    return _DEFAULT_SANDBOX_PATH


def _corepack_runtime_root(
    corepack: Path, *, forbidden_roots: Sequence[Path] = ()
) -> Path:
    """Locate the Node distribution that owns a resolved Corepack entrypoint."""
    for candidate in corepack.parents:
        node = candidate / "bin" / "node"
        entrypoint = candidate / "bin" / "corepack"
        packaged = (
            candidate / "lib" / "node_modules" / "corepack" / "dist" / "corepack.js"
        )
        if not (
            node.is_file()
            and os.access(node, os.X_OK)
            and entrypoint.is_file()
            and entrypoint.resolve() == corepack
            and packaged.is_file()
            and packaged.resolve() == corepack
        ):
            continue
        resolved_candidate = _protected_tool_path(
            candidate, forbidden_roots=forbidden_roots
        )
        resolved_node = _protected_tool_path(node, forbidden_roots=forbidden_roots)
        if not resolved_node.is_relative_to(resolved_candidate):
            continue
        if any(
            resolved_node.is_relative_to(root.resolve())
            or resolved_candidate.is_relative_to(root.resolve())
            for root in forbidden_roots
        ):
            raise SandboxError(
                "sandbox runtime tool is under a writable sandbox source: node"
            )
        return resolved_candidate
    raise SandboxError("sandbox Corepack runtime is unavailable")


def _mounted_corepack_runtime_available() -> bool:
    mounted_corepack = GUARDIAN_NODE_ROOT / "bin" / "corepack"
    mounted_node = GUARDIAN_NODE_ROOT / "bin" / "node"
    return (
        mounted_corepack.is_file()
        and os.access(mounted_corepack, os.X_OK)
        and mounted_node.is_file()
        and os.access(mounted_node, os.X_OK)
    )


def _mounted_corepack_home_available() -> bool:
    return GUARDIAN_COREPACK_HOME.is_dir()


def _resolve_corepack_home(
    *, required: bool, forbidden_roots: Sequence[Path]
) -> Path | None:
    """Locate a trusted Corepack package-manager cache for offline pnpm use."""
    if _mounted_corepack_home_available():
        return GUARDIAN_COREPACK_HOME
    configured = os.environ.get("COREPACK_HOME")
    candidates: list[Path] = []
    if configured:
        candidates.append(Path(configured))
    candidates.append(Path.home() / ".cache" / "node" / "corepack")
    for home in candidates:
        if not home.is_dir():
            continue
        resolved = home.resolve()
        if any(resolved.is_relative_to(root.resolve()) for root in forbidden_roots):
            continue
        return _protected_tool_path(home, forbidden_roots=forbidden_roots)
    if required:
        raise SandboxError("sandbox Corepack home is unavailable")
    return None


def _resolve_corepack_runtime(
    *, required: bool, forbidden_roots: Sequence[Path]
) -> Path | None:
    """Resolve from PATH first; fall back to an already-mounted Node root."""
    try:
        corepack = _tool("corepack", forbidden_roots=forbidden_roots)
    except SandboxError as exc:
        if "writable sandbox source" in str(exc):
            raise
        if _mounted_corepack_runtime_available():
            return GUARDIAN_NODE_ROOT
        if required:
            raise SandboxError("sandbox Corepack runtime is unavailable") from None
        return None
    try:
        return _corepack_runtime_root(corepack, forbidden_roots=forbidden_roots)
    except SandboxError:
        if required:
            raise
        return None


def current_boundary_reusable(*, deny_network: bool) -> bool:
    """Reuse an active Guardian namespace when its network policy is no weaker."""
    advertised = os.environ.get(SANDBOX_BOUNDARY_ENV)
    if advertised is None:
        return False
    compatible = (
        {NETWORK_DENIED_BOUNDARY}
        if deny_network
        else {NETWORK_DENIED_BOUNDARY, HOST_NETWORK_BOUNDARY}
    )
    if (
        advertised not in compatible
        or not (GUARDIAN_BIN_ROOT / "uv").is_file()
        or stat.S_IMODE(Path("/").stat().st_mode) & 0o222
    ):
        raise SandboxError(
            "active Guardian sandbox cannot satisfy the nested network boundary"
        )
    return True


def command(
    argv: Sequence[str],
    *,
    worktree: Path,
    writable_worktree: bool,
    audit_source: Path | None,
    audit_destination: Path | None,
    git_source: Path | None,
    git_destination: Path | None,
    writable_git: bool,
    read_only_roots: Sequence[Path] = (),
    read_only_files: Sequence[Path] = (),
    read_only_mounts: Sequence[tuple[Path, Path]] = (),
    read_only_file_mounts: Sequence[tuple[Path, Path]] = (),
    preserve_fds: Sequence[int] = (),
    deny_network: bool,
    include_model_runtime: bool = True,
    include_corepack_runtime: bool = False,
) -> list[str]:
    """Build a namespace with no host-root or host-home visibility.

    Mount ordering is security-significant. The broad runtime views are emitted
    first (``/usr``, selected ``/etc``, procfs, devfs, and ``/tmp``), followed
    by runtime executables and then caller mounts. Caller-controlled mount
    paths below ``/proc``, ``/sys``, or ``/dev`` are rejected before any
    argument list is built, so none can shadow the private kernel filesystems.
    """
    validated_audit_destination = (
        _validated_destination(
            audit_destination, label="sandbox audit destination"
        )
        if audit_destination is not None
        else None
    )
    validated_git_destination = (
        _validated_destination(git_destination, label="sandbox Git destination")
        if git_destination is not None
        else None
    )
    resolved_worktree = _validated(worktree, directory=True)
    worktree_destination = _validated_destination(
        worktree, label="sandbox worktree destination"
    )
    mounts: list[tuple[str, Path, Path]] = [
        (
            "--bind" if writable_worktree else "--ro-bind",
            resolved_worktree,
            worktree_destination,
        )
    ]
    if audit_source is not None:
        if audit_destination is None or not audit_destination.is_absolute():
            raise SandboxError("sandbox audit destination is invalid")
        mounts.append(
            (
                "--bind",
                _validated(audit_source, directory=True),
                validated_audit_destination,
            )
        )
    if git_source is not None:
        if git_destination is None or not git_destination.is_absolute():
            raise SandboxError("sandbox Git destination is invalid")
        mounts.append(
            (
                "--bind" if writable_git else "--ro-bind",
                _validated(git_source, directory=True),
                validated_git_destination,
            )
        )
    for path in read_only_roots:
        mounts.append(
            (
                "--ro-bind",
                _validated(path, directory=True),
                _validated_destination(
                    path, label="sandbox protected destination"
                ),
            )
        )
    for path in read_only_files:
        mounts.append(
            (
                "--ro-bind",
                _validated(path, directory=False),
                _validated_destination(
                    path, label="sandbox protected destination"
                ),
            )
        )
    for source, destination in read_only_mounts:
        mounts.append(
            (
                "--ro-bind",
                _validated(source, directory=True),
                _validated_destination(
                    destination, label="sandbox protected destination"
                ),
            )
        )
    for source, destination in read_only_file_mounts:
        mounts.append(
            (
                "--ro-bind",
                _validated(source, directory=False),
                _validated_destination(
                    destination, label="sandbox protected destination"
                ),
            )
        )
    runtime_roots: set[tuple[Path, Path]] = set()
    for root in (
        *(_validated(path, directory=True) for path in read_only_roots),
        *(_validated(source, directory=True) for source, _ in read_only_mounts),
    ):
        configured_interpreter = settings.toolchain.get("interpreter")
        if configured_interpreter is None and settings.env_prefix == "INTELFLO":
            configured_interpreter = "fastapi_backend/.venv/bin/python"
        interpreter = root / "bin" / "python" if root.name == ".venv" else (
            root / configured_interpreter if configured_interpreter else None
        )
        if interpreter is not None and interpreter.is_file():
            resolved_interpreter = interpreter.resolve()
            if not resolved_interpreter.is_relative_to(root):
                resolved_runtime = resolved_interpreter.parent.parent
                runtime_roots.add(
                    (
                        resolved_runtime,
                        _validated_destination(
                            resolved_runtime,
                            label="sandbox interpreter destination",
                        ),
                    )
                )
                if interpreter.is_symlink():
                    lexical = Path(os.readlink(interpreter))
                    if lexical.is_absolute():
                        destination = lexical.parent.parent
                        runtime_roots.add(
                            (
                                resolved_runtime,
                                _validated_destination(
                                    destination,
                                    label="sandbox interpreter destination",
                                ),
                            )
                        )
    mounts.extend(
        ("--ro-bind", source, destination)
        for source, destination in sorted(runtime_roots)
    )
    writable_sources = tuple(
        source for mode, source, _destination in mounts if mode == "--bind"
    )
    uv = _tool("uv", forbidden_roots=writable_sources)
    corepack_runtime = (
        _resolve_corepack_runtime(
            required=True,
            forbidden_roots=writable_sources,
        )
        if include_corepack_runtime
        else None
    )
    corepack_home = (
        _resolve_corepack_home(
            required=True,
            forbidden_roots=writable_sources,
        )
        if corepack_runtime is not None
        else None
    )
    codex = (
        _optional_tool("codex", forbidden_roots=writable_sources)
        if include_model_runtime
        else None
    )

    built = [
        str(_system_tool("bwrap")),
        "--die-with-parent",
        "--new-session",
        "--unshare-pid",
    ]
    if preserve_fds:
        if any(not isinstance(fd, int) or fd < 3 for fd in preserve_fds):
            raise SandboxError("sandbox preserved descriptor is invalid")
        try:
            for fd in preserve_fds:
                os.fstat(fd)
        except OSError as exc:
            raise SandboxError("sandbox preserved descriptor is unavailable") from exc
    if deny_network:
        built.append("--unshare-net")
    built.extend(
        [
            "--ro-bind",
            "/usr",
            "/usr",
            "--symlink",
            "usr/bin",
            "/bin",
            "--symlink",
            "usr/lib",
            "/lib",
            "--symlink",
            "usr/lib64",
            "/lib64",
            "--symlink",
            "usr/sbin",
            "/sbin",
            "--dir",
            "/etc",
            "--ro-bind",
            "/etc/ssl",
            "/etc/ssl",
            "--ro-bind-try",
            "/etc/hosts",
            "/etc/hosts",
            "--ro-bind-try",
            "/etc/nsswitch.conf",
            "/etc/nsswitch.conf",
            "--ro-bind-try",
            "/etc/passwd",
            "/etc/passwd",
            "--ro-bind-try",
            "/etc/group",
            "/etc/group",
            "--dir",
            "/mnt",
            "--dir",
            "/mnt/wsl",
            "--ro-bind-try",
            "/mnt/wsl/resolv.conf",
            "/mnt/wsl/resolv.conf",
            "--symlink",
            "/mnt/wsl/resolv.conf",
            "/etc/resolv.conf",
            "--proc",
            "/proc",
            "--dev",
            "/dev",
            "--tmpfs",
            "/tmp",
            "--dir",
            str(settings.sandbox_home),
            "--dir",
            "/run",
            "--dir",
            str(GUARDIAN_BIN_ROOT),
            "--ro-bind",
            str(uv),
            str(GUARDIAN_BIN_ROOT / "uv"),
        ]
    )
    if corepack_runtime is not None:
        built.extend(["--ro-bind", str(corepack_runtime), str(GUARDIAN_NODE_ROOT)])
    if corepack_home is not None:
        built.extend(["--ro-bind", str(corepack_home), str(GUARDIAN_COREPACK_HOME)])
    if codex is not None:
        built.extend(["--ro-bind", str(codex), str(GUARDIAN_BIN_ROOT / "codex")])
    created: set[Path] = {
        Path("/"),
        Path("/etc"),
        Path("/mnt"),
        Path("/mnt/wsl"),
        Path("/run"),
        GUARDIAN_BIN_ROOT,
    }
    for mode, source, destination in mounts:
        for parent in _parents(destination):
            if parent not in created:
                built.extend(["--dir", str(parent)])
                created.add(parent)
        built.extend([mode, str(source), str(destination)])
    env_bindings: list[str] = [
        "--chmod",
        "0555",
        "/",
        "--setenv",
        "HOME",
        str(settings.sandbox_home),
        "--setenv",
        "GH_CONFIG_DIR",
        "/tmp/guardian-gh-config",
        "--setenv",
        "PATH",
        _sandbox_path(corepack_runtime=corepack_runtime),
    ]
    if corepack_home is not None:
        env_bindings.extend(["--setenv", "COREPACK_HOME", str(GUARDIAN_COREPACK_HOME)])
    env_bindings.extend(
        [
            "--setenv",
            SANDBOX_BOUNDARY_ENV,
            NETWORK_DENIED_BOUNDARY if deny_network else HOST_NETWORK_BOUNDARY,
            "--cap-drop",
            "ALL",
            "--",
            *argv,
        ]
    )
    built.extend(env_bindings)
    return built


def validation_command(
    argv: Sequence[str],
    *,
    worktree: Path,
    read_only_roots: Sequence[Path] = (),
    read_only_file_mounts: Sequence[tuple[Path, Path]] = (),
    deny_network: bool = True,
) -> list[str]:
    """Build a validation child boundary covering linked-worktree Git metadata.

    The caller may inspect and adopt child file changes. Git metadata is mounted
    read-only even when the worktree itself is writable. No authority descriptors
    are preserved. Use run_validation_child to apply the environment boundary too.
    """
    from .validation import ValidationHookError, _repository_layout

    worktree = worktree.absolute()
    try:
        layout = _repository_layout(worktree)
    except ValidationHookError as exc:
        raise SandboxError(str(exc)) from exc
    git_entry = worktree / ".git"
    roots = tuple(
        dict.fromkeys((*read_only_roots, layout.common_dir, layout.git_dir))
    )
    return command(
        argv, worktree=worktree, writable_worktree=True,
        audit_source=None, audit_destination=None,
        git_source=None, git_destination=None, writable_git=False,
        read_only_roots=roots,
        read_only_files=(git_entry,) if git_entry.is_file() else (),
        read_only_file_mounts=read_only_file_mounts,
        deny_network=deny_network, include_model_runtime=False,
    )


def run_validation_child(argv: Sequence[str], *, worktree: Path,
                         source_environment: Mapping[str, str] | None = None,
                         read_only_roots: Sequence[Path] = (),
                         git_config_overlays: Sequence[tuple[Path, bytes]] | None = None,
                         timeout: float | None = None):
    """Spawn with filesystem and environment containment, including descendants."""
    import subprocess
    if git_config_overlays is None:
        from .validation import (
            ValidationHookError,
            _repository_config_overlays,
            _repository_layout,
        )

        try:
            overlays = _repository_config_overlays(_repository_layout(worktree))
        except ValidationHookError as exc:
            raise SandboxError(str(exc)) from exc
        git_config_overlays = tuple(
            (overlay.destination, overlay.payload) for overlay in overlays
        )
    child_environment = environment(source_environment)
    child_environment.update(settings.child_environment())
    # A consumer allowlist must never reintroduce lease, nonce or credentials.
    child_environment = {
        key: value for key, value in child_environment.items()
        if not any(part in key.upper() for part in
                   ("LEASE", "NONCE", "TOKEN", "CREDENTIAL", "AUTH_FD",
                    "SECRET", "PRIVATE_KEY", "SIGNING", "API_KEY",
                    "ACCESS_KEY", "PASSWORD", "COOKIE", "SESSION",
                    "SSH_AUTH", "BEARER"))
    }
    for key in tuple(child_environment):
        if key == "GIT_CONFIG_COUNT" or key.startswith(
            ("GIT_CONFIG_KEY_", "GIT_CONFIG_VALUE_")
        ):
            child_environment.pop(key)
    git_overrides = (
        ("core.hooksPath", "/dev/null"),
        ("core.fsmonitor", "false"),
        ("core.sshCommand", ""),
        ("core.pager", "cat"),
        ("core.editor", "false"),
        ("core.worktree", ""),
        ("credential.https://github.com.helper", ""),
        ("credential.https://gist.github.com.helper", ""),
    )
    child_environment["GIT_CONFIG_COUNT"] = str(len(git_overrides))
    for index, (key, value) in enumerate(git_overrides):
        child_environment[f"GIT_CONFIG_KEY_{index}"] = key
        child_environment[f"GIT_CONFIG_VALUE_{index}"] = value

    with tempfile.TemporaryDirectory(prefix="loopzero-validation-config-") as raw:
        config_mounts: list[tuple[Path, Path]] = []
        for index, (destination, payload) in enumerate(git_config_overlays):
            snapshot = Path(raw) / f"config-{index}"
            snapshot.write_bytes(payload)
            snapshot.chmod(0o444)
            config_mounts.append((snapshot, destination))
        return subprocess.run(
            validation_command(
                argv,
                worktree=worktree,
                read_only_roots=read_only_roots,
                read_only_file_mounts=config_mounts,
            ),
            env=child_environment, close_fds=True, pass_fds=(),
            capture_output=True, text=True, timeout=timeout, check=False,
        )
