"""Run the consumer's check commands inside a bubblewrap sandbox."""

from __future__ import annotations

import shutil
import tempfile
from pathlib import Path

from . import _proc
from .types import CheckReport, CheckResult, Config, LoopZeroError

GIT_TIMEOUT = 60.0
DEFAULT_TIMEOUT = 3600.0
TIMEOUT_EXIT = 124
SANDBOX_HOME = "/tmp/home"
# Host directories exposed read-only by default. Nothing else from the host is visible:
# /home, /root, /run, /var/run, /proc/1 and every Unix socket stay outside. Tool caches
# under the user's home (~/.local/bin, ~/.local/share/uv, ~/.cache/uv, ...) must be listed
# explicitly in `[checks] ro_paths` (Config.sandbox_ro).
SYSTEM_RO = ("/usr", "/bin", "/sbin", "/lib", "/lib32", "/lib64", "/etc", "/opt")


class SandboxUnavailable(LoopZeroError):
    """bubblewrap is missing or cannot create a sandbox on this host."""


def run_checks(config: Config, worktree: Path, *, timeout: float = DEFAULT_TIMEOUT) -> CheckReport:
    """Run every `config.checks` command in order inside the sandbox and report results.

    The sandbox sees only `SYSTEM_RO`, `config.sandbox_ro`, the worktree and its Git
    directories (all read-only), fresh tmpfs at /tmp, /run and /var/run, a private HOME
    at /tmp/home, no network unless `config.network`, and only `config.env_allowlist`
    variables. Each command's exit code is recorded; a timeout is recorded as exit
    `TIMEOUT_EXIT` with the partial output. Nothing raises for a failing check.
    """
    worktree = worktree.resolve()
    if shutil.which("bwrap") is None:
        raise SandboxUnavailable("bwrap not found on PATH (install bubblewrap)")
    head = _git(config, worktree, "rev-parse", "HEAD").strip()
    dirty = bool(_git(config, worktree, "status", "--porcelain").strip())
    common_dir = _git_dir(config, worktree, "--git-common-dir")
    git_dir = _git_dir(config, worktree, "--git-dir")

    results = []
    with tempfile.TemporaryDirectory(prefix="loopzero-home-") as home:
        prefix = bwrap_argv(config, worktree, Path(home), common_dir, git_dir)
        _probe(config, worktree, prefix)
        for command in config.checks:
            results.append(_run_one(config, worktree, prefix, command, timeout))
    return CheckReport(head=head, dirty=dirty, results=tuple(results))


def _run_one(
    config: Config, worktree: Path, prefix: list[str], command: str, timeout: float
) -> CheckResult:
    try:
        done = _proc.run(
            [*prefix, "/bin/sh", "-c", command],
            cwd=worktree,
            env_allowlist=config.env_allowlist,
            timeout=timeout,
            merge_output=True,
        )
    except _proc.ProcTimeout as exc:
        return CheckResult(
            command=command,
            exit_code=TIMEOUT_EXIT,
            duration_s=timeout,
            tail=_proc.tail(exc.output + f"\n[loopzero] timed out after {timeout:g}s"),
        )
    return CheckResult(
        command=command,
        exit_code=done.exit_code,
        duration_s=done.duration_s,
        tail=_proc.tail(done.stdout),
    )


def _probe(config: Config, worktree: Path, prefix: list[str]) -> None:
    """Raise `SandboxUnavailable` unless bwrap can build the exact sandbox we will use."""
    try:
        probe = _proc.run(
            [*prefix, "true"], cwd=worktree, env_allowlist=config.env_allowlist, timeout=GIT_TIMEOUT
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
    ]
    for path in SYSTEM_RO:
        if Path(path).exists():
            argv += ["--ro-bind", path, path]
    for path in config.sandbox_ro:
        argv += ["--ro-bind-try", path, path]
    argv += ["--dev", "/dev", "--proc", "/proc"]
    for path in ("/tmp", "/run", "/var/run"):
        argv += ["--tmpfs", path]
    argv += ["--bind", str(home), SANDBOX_HOME]
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
