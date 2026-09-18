"""Run the consumer's check commands inside a bubblewrap sandbox."""

from __future__ import annotations

import shutil
import tempfile
from pathlib import Path

from . import _proc
from .types import CheckReport, CheckResult, Config, LoopZeroError

GIT_TIMEOUT = 60.0
DEFAULT_TIMEOUT = 3600.0
SANDBOX_HOME = "/tmp/home"
_PROBE = ("bwrap", "--ro-bind", "/", "/", "true")


class SandboxUnavailable(LoopZeroError):
    """bubblewrap is missing or cannot create a sandbox on this host."""


def run_checks(config: Config, worktree: Path, *, timeout: float = DEFAULT_TIMEOUT) -> CheckReport:
    """Run every `config.checks` command in order inside the sandbox and report results.

    The sandbox exposes the host filesystem read-only (including the worktree and its
    Git directories), a private /tmp, a fresh HOME, no network unless `config.network`,
    and only `config.env_allowlist` variables. Each command's exit code is recorded;
    nothing raises for a failing check.
    """
    worktree = worktree.resolve()
    ensure_available(config)
    head = _git(config, worktree, "rev-parse", "HEAD").strip()
    dirty = bool(_git(config, worktree, "status", "--porcelain").strip())
    common_dir = _git_dir(config, worktree, "--git-common-dir")
    git_dir = _git_dir(config, worktree, "--git-dir")

    results = []
    with tempfile.TemporaryDirectory(prefix="loopzero-home-") as home:
        for command in config.checks:
            argv = bwrap_argv(config, worktree, Path(home), common_dir, git_dir) + [
                "/bin/sh",
                "-c",
                command,
            ]
            done = _proc.run(
                argv,
                cwd=worktree,
                env_allowlist=config.env_allowlist,
                timeout=timeout,
                merge_output=True,
            )
            results.append(
                CheckResult(
                    command=command,
                    exit_code=done.exit_code,
                    duration_s=done.duration_s,
                    tail=_proc.tail(done.stdout),
                )
            )
    return CheckReport(head=head, dirty=dirty, results=tuple(results))


def ensure_available(config: Config) -> None:
    """Raise `SandboxUnavailable` unless bwrap exists and can build a trivial sandbox."""
    if shutil.which("bwrap") is None:
        raise SandboxUnavailable("bwrap not found on PATH (install bubblewrap)")
    try:
        probe = _proc.run(
            _PROBE, cwd="/", env_allowlist=config.env_allowlist, timeout=GIT_TIMEOUT
        )
    except (_proc.ToolMissing, _proc.ProcTimeout) as exc:
        raise SandboxUnavailable(f"bwrap probe failed: {exc}") from exc
    if probe.exit_code != 0:
        reason = _proc.tail(probe.stderr, 1) or f"exit {probe.exit_code}"
        raise SandboxUnavailable(f"bwrap probe failed: {reason}")


def bwrap_argv(
    config: Config, worktree: Path, home: Path, common_dir: Path, git_dir: Path
) -> list[str]:
    """Build the bwrap command line up to and including the `--` separator."""
    argv = [
        "bwrap",
        "--die-with-parent",
        "--new-session",
        "--unshare-all",
        *(["--share-net"] if config.network else []),
        "--cap-drop",
        "ALL",
        "--ro-bind",
        "/",
        "/",
        "--dev",
        "/dev",
        "--proc",
        "/proc",
        "--tmpfs",
        "/tmp",
        "--bind",
        str(home),
        SANDBOX_HOME,
    ]
    # Bind the worktree and every Git directory read-only *after* the /tmp tmpfs so
    # they stay visible even when they live under /tmp, and are never writable.
    binds = [worktree]
    for path in (common_dir, git_dir):
        if not any(path == seen or path.is_relative_to(seen) for seen in binds):
            binds.append(path)
    for path in binds:
        argv += ["--ro-bind", str(path), str(path)]
    argv += ["--chdir", str(worktree), "--clearenv"]
    for key, value in _proc.build_env(config.env_allowlist).items():
        if key != "HOME":
            argv += ["--setenv", key, value]
    argv += ["--setenv", "HOME", SANDBOX_HOME, "--"]
    return argv


def _git(config: Config, worktree: Path, *args: str) -> str:
    done = _proc.run(
        ["git", *args], cwd=worktree, env_allowlist=config.env_allowlist, timeout=GIT_TIMEOUT
    )
    if done.exit_code != 0:
        raise SandboxUnavailable(
            f"git {' '.join(args)} failed in {worktree}: {_proc.tail(done.stderr, 1)}"
        )
    return done.stdout


def _git_dir(config: Config, worktree: Path, flag: str) -> Path:
    raw = Path(_git(config, worktree, "rev-parse", flag).strip())
    return (raw if raw.is_absolute() else worktree / raw).resolve()
