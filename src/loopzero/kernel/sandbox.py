#!/usr/bin/env python3
"""Allowlisted Bubblewrap boundary for Guardian-controlled untrusted execution."""

from __future__ import annotations

import os
import shutil
import stat
from collections.abc import Mapping, Sequence
from contextlib import contextmanager
from pathlib import Path

CODEX_AUTH_FD_ENV = "INTELFLO_CODEX_AUTH_FD"
SAFE_PASSTHROUGH_ENV = frozenset(
    {
        "INTELFLO_WORKTREE_LEASE_BOUNDARY",
        "INTELFLO_WORKTREE_LEASE_FD",
        "INTELFLO_WORKTREE_LEASE_NONCE",
        "INTELFLO_WORKTREE_LEASE_OWNER_PID",
        "LANG",
        "LC_ALL",
        "TERM",
        "TZ",
    }
)


class SandboxError(RuntimeError):
    """The untrusted execution namespace could not be constructed safely."""


@contextmanager
def codex_subscription_credential():
    """Pass one protected ChatGPT login descriptor without mounting host state."""
    path = Path.home() / ".codex" / "auth.json"
    if path.is_symlink() or not path.is_file():
        raise SandboxError("Codex subscription credential is unavailable")
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags)
    try:
        metadata = os.fstat(fd)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != os.getuid()
            or metadata.st_mode & 0o077
            or metadata.st_size <= 2
            or metadata.st_size > 1024 * 1024
        ):
            raise SandboxError("Codex subscription credential is unsafe")
        yield fd
    finally:
        os.close(fd)


def environment(source: Mapping[str, str] | None = None) -> dict[str, str]:
    original = os.environ if source is None else source
    selected = {
        name: value for name, value in original.items() if name in SAFE_PASSTHROUGH_ENV
    }
    selected.update(
        {
            "HOME": "/tmp/guardian-home",
            "PATH": "/run/guardian-bin:/usr/bin:/bin",
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
    return selected


def _parents(path: Path) -> list[Path]:
    current = path.parent
    parents: list[Path] = []
    while current != Path("/"):
        parents.append(current)
        current = current.parent
    return list(reversed(parents))


def _validated(path: Path, *, directory: bool | None = None) -> Path:
    if path.is_symlink():
        raise SandboxError("sandbox mount source contains a direct symlink")
    resolved = path.resolve()
    if directory is True and not resolved.is_dir():
        raise SandboxError("sandbox directory mount source is unavailable")
    if directory is False and not resolved.is_file():
        raise SandboxError("sandbox file mount source is unavailable")
    if directory is None and not resolved.exists():
        raise SandboxError("sandbox mount source is unavailable")
    return resolved


def _tool(name: str) -> Path:
    found = shutil.which(name)
    if found is None:
        raise SandboxError(f"sandbox runtime tool is unavailable: {name}")
    return Path(found).resolve()


def _optional_tool(name: str) -> Path | None:
    """Resolve an optional model runtime without weakening command isolation."""
    found = shutil.which(name)
    return Path(found).resolve() if found is not None else None


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
    preserve_fds: Sequence[int] = (),
    deny_network: bool,
    include_model_runtime: bool = True,
) -> list[str]:
    """Build a namespace with no host-root or host-home visibility."""
    resolved_worktree = _validated(worktree, directory=True)
    mounts: list[tuple[str, Path, Path]] = [
        (
            "--bind" if writable_worktree else "--ro-bind",
            resolved_worktree,
            worktree.absolute(),
        )
    ]
    if audit_source is not None:
        if audit_destination is None or not audit_destination.is_absolute():
            raise SandboxError("sandbox audit destination is invalid")
        mounts.append(
            ("--bind", _validated(audit_source, directory=True), audit_destination)
        )
    if git_source is not None:
        if git_destination is None or not git_destination.is_absolute():
            raise SandboxError("sandbox Git destination is invalid")
        mounts.append(
            (
                "--bind" if writable_git else "--ro-bind",
                _validated(git_source, directory=True),
                git_destination,
            )
        )
    mounts.extend(
        ("--ro-bind", _validated(path, directory=True), path.absolute())
        for path in read_only_roots
    )
    mounts.extend(
        ("--ro-bind", _validated(path, directory=False), path.absolute())
        for path in read_only_files
    )
    mounts.extend(
        ("--ro-bind", _validated(source, directory=True), destination.absolute())
        for source, destination in read_only_mounts
    )
    runtime_roots: set[tuple[Path, Path]] = set()
    for root in (
        resolved_worktree,
        *(_validated(path, directory=True) for path in read_only_roots),
        *(_validated(source, directory=True) for source, _ in read_only_mounts),
    ):
        interpreter = (
            root / "bin" / "python"
            if root.name == ".venv"
            else root / "fastapi_backend" / ".venv" / "bin" / "python"
        )
        if interpreter.is_file():
            resolved_interpreter = interpreter.resolve()
            if not resolved_interpreter.is_relative_to(root):
                resolved_runtime = resolved_interpreter.parent.parent
                runtime_roots.add((resolved_runtime, resolved_runtime))
                if interpreter.is_symlink():
                    lexical = Path(os.readlink(interpreter))
                    if lexical.is_absolute():
                        runtime_roots.add((resolved_runtime, lexical.parent.parent))
    mounts.extend(
        ("--ro-bind", source, destination)
        for source, destination in sorted(runtime_roots)
    )

    built = ["bwrap", "--die-with-parent", "--new-session", "--unshare-pid"]
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
            "/tmp/guardian-home",
            "--dir",
            "/run",
            "--dir",
            "/run/guardian-bin",
            "--ro-bind",
            str(_tool("uv")),
            "/run/guardian-bin/uv",
        ]
    )
    codex = _optional_tool("codex") if include_model_runtime else None
    if codex is not None:
        built.extend(["--ro-bind", str(codex), "/run/guardian-bin/codex"])
    created: set[Path] = {
        Path("/"),
        Path("/etc"),
        Path("/mnt"),
        Path("/mnt/wsl"),
        Path("/run"),
        Path("/run/guardian-bin"),
    }
    for mode, source, destination in mounts:
        for parent in _parents(destination):
            if parent not in created:
                built.extend(["--dir", str(parent)])
                created.add(parent)
        built.extend([mode, str(source), str(destination)])
    built.extend(
        [
            "--setenv",
            "HOME",
            "/tmp/guardian-home",
            "--setenv",
            "GH_CONFIG_DIR",
            "/tmp/guardian-gh-config",
            "--cap-drop",
            "ALL",
            "--",
            *argv,
        ]
    )
    return built
