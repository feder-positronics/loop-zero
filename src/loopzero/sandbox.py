"""Run checks in bwrap, which gives no CPU/memory quotas; ulimit is best-effort."""

from __future__ import annotations

import shlex
import shutil
import tempfile
from contextlib import ExitStack
from os.path import commonpath
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
# `Config.writable` paths are bound read-write. That is a deliberate trust decision by the
# consumer (e.g. a shared ~/.cache/uv so checks work offline); a check can poison that cache.
# `Config.scratch` entries are per-run empty tmp dirs bound over <worktree>/<entry> so tools
# can create venvs and caches without the tree itself being writable.
# Precedence inside the sandbox: SANDBOX_ENV defaults < allowlisted host vars < Config.env.
SANDBOX_ENV = {
    "PYTHONDONTWRITEBYTECODE": "1",
    "RUFF_CACHE_DIR": "/tmp/ruff-cache",
    "UV_CACHE_DIR": "/tmp/uv-cache",
    "PYTEST_ADDOPTS": "-p no:cacheprovider",
}


class SandboxUnavailable(LoopZeroError):
    """bubblewrap is missing or cannot create a sandbox on this host."""


def run_checks(config: Config, worktree: Path, *, timeout: float = DEFAULT_TIMEOUT) -> CheckReport:
    """Run `config.checks` in order until one fails, inside the sandbox, and report results.

    The sandbox sees only `SYSTEM_RO`, `config.sandbox_ro`, the worktree and its Git
    directories (all read-only), `config.writable` read-write, per-run scratch dirs over
    `config.scratch`, fresh tmpfs at /tmp, /run and /var/run, a private HOME at /tmp/home,
    no network unless `config.network`, and `SANDBOX_ENV` overlaid by `config.env_allowlist`
    host values and then `config.env`. Each command's exit code is recorded; a timeout is
    recorded as exit `TIMEOUT_EXIT` with the partial output. Nothing raises for a failing check,
    and commands after the first failure are not run or recorded.
    """
    worktree = worktree.resolve()
    if shutil.which("bwrap") is None:
        raise SandboxUnavailable(
            "bwrap binary missing: bwrap not found on PATH\n"
            "sudo apt install bubblewrap"
        )
    head = _git(config, worktree, "rev-parse", "HEAD").strip()
    dirty = bool(_git(config, worktree, "status", "--porcelain").strip())
    common_dir = _git_dir(config, worktree, "--git-common-dir")
    git_dir = _git_dir(config, worktree, "--git-dir")

    _validate_scratch(config.scratch, worktree, common_dir, git_dir)
    results = []
    with ExitStack() as stack:
        home = Path(stack.enter_context(tempfile.TemporaryDirectory(prefix="loopzero-home-")))
        scratch_root = Path(
            stack.enter_context(tempfile.TemporaryDirectory(prefix="loopzero-scratch-"))
        )
        private_parent = Path(commonpath((home, scratch_root)))
        for exposed in (*config.sandbox_ro, *config.writable):
            if private_parent.is_relative_to(Path(exposed).resolve()):
                raise SandboxUnavailable(
                    f"sandbox path {exposed!r} would expose private check directories"
                )
        scratch = {entry: scratch_root / entry for entry in config.scratch}
        for entry, source in scratch.items():
            source.mkdir(parents=True)
            # bwrap cannot create mount points inside a read-only bind, so the (empty,
            # git-invisible) directory must exist in the worktree before we start.
            (worktree / entry).mkdir(parents=True, exist_ok=True)
        prefix = bwrap_argv(config, worktree, home, common_dir, git_dir, scratch)
        _probe(config, worktree, prefix)
        for command in config.checks:
            result = _run_one(config, worktree, prefix, command, timeout)
            results.append(result)
            if result.exit_code != 0:
                break
    return CheckReport(head=head, dirty=dirty, results=tuple(results))


def _validate_scratch(
    entries: tuple[str, ...], worktree: Path, common_dir: Path, git_dir: Path
) -> None:
    """Reject scratch destinations that could escape or alter Git metadata."""
    for entry in entries:
        parts = Path(entry).parts
        if not parts or entry in ("", ".") or Path(entry).is_absolute() or ".." in parts:
            raise SandboxUnavailable(f"unsafe scratch destination {entry!r}")
        destination = worktree.joinpath(*parts)
        if parts[0] == ".git" or any(
            destination == path or destination.is_relative_to(path) for path in (common_dir, git_dir)
        ):
            raise SandboxUnavailable(f"scratch destination touches Git metadata: {entry!r}")
        current = worktree
        for part in parts:
            current /= part
            if current.is_symlink():
                raise SandboxUnavailable(f"scratch destination has symlink component: {entry!r}")


def _run_one(
    config: Config, worktree: Path, prefix: list[str], command: str, timeout: float
) -> CheckResult:
    try:
        limits = config.limits
        limited = "; ".join(
            (
                f"ulimit -v {limits.memory_mb * 1024}",
                f"ulimit -u {limits.processes} 2>/dev/null",
                f"ulimit -f {limits.file_mb * 1024}",
                f"exec /bin/sh -c {shlex.quote(command)}",
            )
        )
        done = _proc.run(
            [*prefix, "/bin/sh", "-c", limited],
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
    except _proc.ToolMissing as exc:
        raise SandboxUnavailable(
            f"bwrap binary missing while starting the namespace probe: {exc}\n"
            "sudo apt install bubblewrap"
        ) from exc
    except _proc.ProcTimeout as exc:
        raise SandboxUnavailable(f"bwrap namespace creation timed out: {exc}") from exc
    if probe.exit_code != 0:
        reason = _proc.tail(probe.stderr, 1) or f"exit {probe.exit_code}"
        remedy = ""
        probe_error = probe.stderr.lower()
        if any(word in probe_error for word in ("permission", "user namespace", "userns")):
            remedy = "\nsudo sysctl -w kernel.apparmor_restrict_unprivileged_userns=0"
        raise SandboxUnavailable(f"bwrap namespace creation failed: {reason}{remedy}")


def bwrap_argv(
    config: Config,
    worktree: Path,
    home: Path,
    common_dir: Path,
    git_dir: Path,
    scratch: dict[str, Path] | None = None,
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
    argv += ["--dev", "/dev", "--proc", "/proc"]
    for path in ("/tmp", "/run", "/var/run"):
        argv += ["--tmpfs", path]
    for path in config.sandbox_ro:
        argv += ["--ro-bind-try", path, path]
    for path in config.writable:
        argv += ["--bind-try", path, path]
    argv += ["--bind", str(home), SANDBOX_HOME]
    # Bind the worktree and every Git directory read-only *after* the /tmp tmpfs so
    # they stay visible even when they live under /tmp, and are never writable.
    binds = [worktree]
    for path in (common_dir, git_dir):
        if not any(path == seen or path.is_relative_to(seen) for seen in binds):
            binds.append(path)
    for path in binds:
        argv += ["--ro-bind", str(path), str(path)]
    for entry, source in (scratch or {}).items():
        argv += ["--bind", str(source), str(worktree / entry)]
    argv += ["--chdir", str(worktree), "--clearenv"]
    env = {**SANDBOX_ENV, **_proc.build_env(config.env_allowlist), **dict(config.env)}
    for key, value in env.items():
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
