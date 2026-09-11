#!/usr/bin/env python3
"""Resolve system executables without trusting a caller-controlled PATH."""

from __future__ import annotations

import os
import stat
from collections.abc import Mapping
from pathlib import Path


class TrustedExecutableError(RuntimeError):
    """A required system executable is absent or writable by this account."""


def trusted_subprocess_environment(
    source: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Remove inherited dynamic-loader injection from a trusted executable."""
    original = os.environ if source is None else source
    return {
        name: value
        for name, value in original.items()
        if not name.startswith(("LD_", "DYLD_"))
    }


def _writable_by_current_account(metadata: os.stat_result) -> bool:
    if os.geteuid() == 0:
        # Root-owned owner-write is the normal OS package boundary.  Under a
        # root harness, ownership plus group/other immutability is the useful
        # integrity check; treating root's inherent authority as a mutation
        # makes every packaged executable unusable.
        return metadata.st_uid != 0 or bool(
            metadata.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
        )
    if metadata.st_uid == os.geteuid():
        return bool(metadata.st_mode & stat.S_IWUSR)
    if metadata.st_gid in {os.getegid(), *os.getgroups()}:
        return bool(metadata.st_mode & stat.S_IWGRP)
    return bool(metadata.st_mode & stat.S_IWOTH)


def system_executable(name: str) -> Path:
    """Return one protected executable selected only from the OS default path."""
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
        metadata = tuple(
            component.stat() for component in (executable, *executable.parents)
        )
    except OSError as exc:
        raise TrustedExecutableError(
            f"trusted {name} executable is unavailable"
        ) from exc
    if (
        not executable.is_file()
        or not os.access(executable, os.X_OK)
        or any(_writable_by_current_account(item) for item in metadata)
    ):
        raise TrustedExecutableError(f"trusted {name} executable is not protected")
    return executable
