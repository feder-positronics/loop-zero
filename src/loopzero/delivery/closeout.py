"""Trusted-revision closeout snapshot bootstrap."""

from __future__ import annotations

import os
import pwd
import shutil
import subprocess
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from ..config import Profile
from ..kernel.gitscope import trusted_git_command
from ..kernel.sandbox import environment as sandbox_environment
from ..trust import allowed_path, resolve_executable, trusted_subprocess_environment


class BootstrapError(RuntimeError):
    """A trusted closeout bootstrap invariant failed."""


@dataclass(frozen=True)
class CloseoutSettings:
    snapshot_path: Path
    temp_prefix: str
    trusted_bin_dir: Path
    env_prefix: str

    @classmethod
    def from_profile(cls, profile: Profile) -> "CloseoutSettings":
        required = ("closeout_snapshot_path", "closeout_temp_prefix", "trusted_bin_dir")
        missing = [key for key in required if not profile.toolchain.get(key)]
        if missing:
            raise BootstrapError("closeout configuration is missing: " + ", ".join(missing))
        return cls(
            Path(profile.toolchain["closeout_snapshot_path"]),
            str(profile.toolchain["closeout_temp_prefix"]),
            Path(profile.toolchain["trusted_bin_dir"]),
            profile.env_prefix,
        )

    def env(self, suffix: str) -> str:
        return f"{self.env_prefix}_{suffix}"


@dataclass(frozen=True)
class CloseoutSnapshot:
    checkout: Path
    toolchain: Path
    revision: str


def trusted_tools(profile: Profile, names: Sequence[str]) -> dict[str, Path]:
    """Resolve closeout executables through the package's single trust seam."""
    settings = CloseoutSettings.from_profile(profile)
    permitted = allowed_path((str(settings.trusted_bin_dir),))
    resolved: dict[str, Path] = {}
    for name in names:
        executable = resolve_executable(profile.root, name, permitted)
        if executable is None:
            raise BootstrapError(f"trusted closeout executable is unavailable: {name}")
        resolved[name] = executable
    return resolved


def isolated_environment(
    profile: Profile,
    source: Mapping[str, str],
    *,
    home: Path | None = None,
    user: str | None = None,
) -> dict[str, str]:
    """Build the complete closeout child environment from a positive allowlist."""
    settings = CloseoutSettings.from_profile(profile)
    account = pwd.getpwuid(os.getuid()) if home is None or user is None else None
    trusted_home = Path(account.pw_dir) if home is None and account else Path(home)
    trusted_user = account.pw_name if user is None and account else str(user)
    allowed = {key: value for key, value in source.items() if key in {"GH_TOKEN", "GITHUB_TOKEN", "TERM", "COLORTERM"}}
    allowed.update(
        {
            "HOME": str(trusted_home), "USER": trusted_user, "LOGNAME": trusted_user,
            "GH_CONFIG_DIR": str(trusted_home / ".config/gh"), "LANG": "C.UTF-8",
            "LC_ALL": "C.UTF-8", "PATH": str(settings.trusted_bin_dir), "TMPDIR": "/tmp",
            "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_SYSTEM": os.devnull, "GIT_TERMINAL_PROMPT": "0",
            "GIT_NO_REPLACE_OBJECTS": "1", "PYTHONNOUSERSITE": "1",
            "PYTHONSAFEPATH": "1", "PYTHONDONTWRITEBYTECODE": "1",
        }
    )
    return trusted_subprocess_environment(allowed)


def materialize_snapshot(profile: Profile, *, revision: str) -> CloseoutSnapshot:
    """Create a detached checkout and expose only its configured toolchain subtree."""
    if len(revision) != 40 or any(ch not in "0123456789abcdef" for ch in revision):
        raise BootstrapError("trusted revision must be 40 lowercase hex")
    settings = CloseoutSettings.from_profile(profile)
    checkout = Path(tempfile.mkdtemp(prefix=settings.temp_prefix))
    completed = subprocess.run(
        trusted_git_command(profile.root.resolve(), "worktree", "add", "--detach", str(checkout), revision),
        capture_output=True, text=True, check=False,
        env=sandbox_environment(os.environ),
    )
    if completed.returncode:
        shutil.rmtree(checkout, ignore_errors=True)
        raise BootstrapError("trusted closeout snapshot could not be materialized")
    toolchain = checkout / settings.snapshot_path
    if toolchain.is_symlink() or not toolchain.is_dir():
        cleanup_snapshot(profile, CloseoutSnapshot(checkout, toolchain, revision))
        raise BootstrapError("configured closeout snapshot path is unavailable")
    return CloseoutSnapshot(checkout, toolchain, revision)


def cleanup_snapshot(profile: Profile, snapshot: CloseoutSnapshot) -> None:
    subprocess.run(
        trusted_git_command(
            profile.root.resolve(), "worktree", "remove", "--force", str(snapshot.checkout)
        ),
        capture_output=True, text=True, check=False,
        env=sandbox_environment(os.environ),
    )
    shutil.rmtree(snapshot.checkout, ignore_errors=True)


__all__ = [
    "BootstrapError", "CloseoutSettings", "CloseoutSnapshot", "cleanup_snapshot",
    "isolated_environment", "materialize_snapshot", "trusted_tools",
]
