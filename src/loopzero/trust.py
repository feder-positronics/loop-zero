"""Shared executable and subprocess trust primitives.

Workflow hooks may intentionally execute repository programs and pass
candidate-controlled operands.  This module establishes only the provenance of
``argv[0]``: either a symlink-free repository executable or a regular executable
in an explicitly allowed, symlink-free directory.  It does not claim that
operands, scripts interpreted by that executable, or their output are trusted.
"""

from __future__ import annotations

import os
import stat
from collections.abc import Mapping, Sequence
from pathlib import Path

from .config import ConfigError

DEFAULT_EXECUTABLE_PATH = (Path("/usr/bin"),)


class TrustedExecutableError(RuntimeError):
    """A required executable does not satisfy the shared allowlist policy."""


def _writable_by_current_account(metadata: os.stat_result) -> bool:
    """Match the legacy protected-system-path ownership decision exactly."""
    if os.geteuid() == 0:
        return metadata.st_uid != 0 or bool(
            metadata.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
        )
    if metadata.st_uid == os.geteuid():
        return bool(metadata.st_mode & stat.S_IWUSR)
    if metadata.st_gid in {os.getegid(), *os.getgroups()}:
        return bool(metadata.st_mode & stat.S_IWGRP)
    return bool(metadata.st_mode & stat.S_IWOTH)


def no_symlink_components(path: Path) -> bool:
    if not path.is_absolute() or ".." in path.parts:
        return False
    current = Path("/")
    try:
        for part in path.parts[1:]:
            current /= part
            if stat.S_ISLNK(os.lstat(current).st_mode):
                return False
    except OSError:
        return False
    return True


def allowed_path(entries: Sequence[str]) -> tuple[Path, ...]:
    allowed: list[Path] = []
    problems: list[str] = []
    for entry in (*map(str, DEFAULT_EXECUTABLE_PATH), *map(str, entries)):
        path = Path(entry)
        if not path.is_absolute():
            problems.append(f"--path-entry {entry!r}: must be an absolute directory")
        elif not no_symlink_components(path):
            problems.append(f"--path-entry {entry!r}: symlink or unavailable path component")
        elif not stat.S_ISDIR(os.lstat(path).st_mode):
            problems.append(f"--path-entry {entry!r}: directory is unavailable")
        else:
            allowed.append(path.resolve(strict=True))
    if problems:
        raise ConfigError(problems)
    return tuple(allowed)


def regular_executable(path: Path) -> bool:
    try:
        return (
            no_symlink_components(path)
            and stat.S_ISREG(os.lstat(path).st_mode)
            and os.access(path, os.X_OK)
        )
    except OSError:
        return False


def repository_executable(root: Path, token: str) -> bool:
    candidate = root / token
    try:
        if not regular_executable(candidate):
            return False
        candidate.resolve(strict=True).relative_to(root.resolve(strict=True))
        return True
    except (OSError, ValueError):
        return False


def resolve_executable(root: Path, token: str, allowed: tuple[Path, ...]) -> Path | None:
    """Resolve only argv[0]; command operands remain candidate-controlled."""
    if "/" in token:
        path = Path(token)
        if not path.is_absolute():
            return (root / path).resolve() if repository_executable(root, token) else None
        for entry in allowed:
            if not regular_executable(path):
                return None
            try:
                executable = path.resolve(strict=True)
                directory = entry.resolve(strict=True)
            except OSError:
                continue
            if executable.parent == directory:
                return executable
        return None
    for directory in allowed:
        candidate = directory / token
        if not regular_executable(candidate):
            continue
        try:
            executable = candidate.resolve(strict=True)
            allowed_real = directory.resolve(strict=True)
        except OSError:
            continue
        if executable.parent == allowed_real:
            return executable
    return None


def system_executable(name: str) -> Path:
    if not name or Path(name).name != name:
        raise TrustedExecutableError("trusted executable name must be one basename")
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
        raise TrustedExecutableError(f"trusted {name} executable is unavailable")
    executable = found.resolve()
    try:
        metadata = tuple(component.stat() for component in (executable, *executable.parents))
    except OSError as exc:
        raise TrustedExecutableError(f"trusted {name} executable is unavailable") from exc
    if (
        not executable.is_file()
        or not os.access(executable, os.X_OK)
        or any(_writable_by_current_account(item) for item in metadata)
    ):
        raise TrustedExecutableError(f"trusted {name} executable is not protected")
    return executable


def trusted_subprocess_environment(
    source: Mapping[str, str] | None = None,
) -> dict[str, str]:
    original = os.environ if source is None else source
    return {
        name: value
        for name, value in original.items()
        if not name.startswith(("LD_", "DYLD_"))
    }


def git_environment(source: Mapping[str, str] | None = None) -> dict[str, str]:
    environment = trusted_subprocess_environment(source)
    environment.update(
        {
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_SYSTEM": os.devnull,
            "GIT_NO_REPLACE_OBJECTS": "1",
            "GIT_TERMINAL_PROMPT": "0",
        }
    )
    return environment
