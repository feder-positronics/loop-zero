"""Run checks in bwrap, which gives no CPU/memory quotas; ulimit is best-effort."""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import resource
import shutil
import tempfile
import threading
import time
from collections.abc import Callable
from contextlib import ExitStack, contextmanager
from dataclasses import asdict, replace
from os.path import commonpath
from pathlib import Path

from . import __version__, _proc
from . import config as config_mod
from .types import CheckReport, CheckResult, Config, LoopZeroError

GIT_TIMEOUT = 60.0
DEFAULT_TIMEOUT = 3600.0
TIMEOUT_EXIT = 124
HEARTBEAT_INTERVAL = 30.0
SANDBOX_HOME = "/tmp/home"
# Only these system directories are exposed read-only by default; home tool paths
# require explicit `[checks] ro_paths`. Host homes, sockets and processes stay hidden.
SYSTEM_RO = ("/usr", "/bin", "/sbin", "/lib", "/lib32", "/lib64", "/etc", "/opt")
# Writable binds are consumer-trusted shared caches (checks can poison them).
# Scratch binds are per-run empty directories for venvs/caches, preserving read-only source.
# Precedence inside the sandbox: SANDBOX_ENV defaults < allowlisted host vars < Config.env.
SANDBOX_ENV = {
    "PYTHONDONTWRITEBYTECODE": "1",
    "RUFF_CACHE_DIR": "/tmp/ruff-cache",
    "UV_CACHE_DIR": "/tmp/uv-cache",
    "PYTEST_ADDOPTS": "-p no:cacheprovider",
}


class SandboxUnavailable(LoopZeroError):
    """bubblewrap is missing or cannot create a sandbox on this host."""


def run_checks(config: Config, worktree: Path, *, timeout: float = DEFAULT_TIMEOUT,
               progress: Callable[[str], None] | None = None,
               status: Callable[[str], None] | None = None,
               _resolved: bool = False) -> CheckReport:
    """Run checks, stopping at the first failure; status emits only stage metadata.

    progress receives each command before launch, as before. Optional status receives
    heartbeats from a reporting thread and completion from the calling thread.
    """
    worktree = worktree.resolve()
    if not config.check_runner and shutil.which("bwrap") is None:
        raise SandboxUnavailable(
            "bwrap binary missing: bwrap not found on PATH\n"
            "sudo apt install bubblewrap"
        )
    head = _git(config, worktree, "rev-parse", "HEAD").strip()
    dirty = bool(_git(config, worktree, "status", "--porcelain").strip())
    if config.check_runner and dirty:
        return CheckReport(head, True, (CheckResult("<dirty worktree>", 1, 0.0,
                                                   "Runner requires a clean worktree"),))
    base = config_mod.base_revision(worktree, config.base_branch) if config.check_runner else ""
    previous = _previous(config, worktree) if config.inputs and not dirty else {}
    common_dir = _git_dir(config, worktree, "--git-common-dir")
    git_dir = _git_dir(config, worktree, "--git-dir")

    _validate_scratch(config.scratch, worktree, common_dir, git_dir)
    with ExitStack() as stack:
        prefix = []
        if not config.check_runner:
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
                # Mount points must exist before bwrap binds the source read-only.
                (worktree / entry).mkdir(parents=True, exist_ok=True)
            prefix = bwrap_argv(config, worktree, home, common_dir, git_dir, scratch)
            probe(config, worktree, prefix)
        missing, preparations = (None, ()) if _resolved else _preflight(config, worktree)
        results = [missing] if missing else []
        commands = () if missing else (*preparations, *config.checks)
        for index, command in enumerate(commands):
            if progress:
                progress(command)
            key = _input_key(config, worktree, command, base) if not dirty else None
            old = previous.get(command)
            if key and old and old.get("input_key") == key and old.get("exit_code") == 0:
                result = CheckResult(command, 0, 0.0, old.get("tail", ""), key, old["head"])
                if status:
                    status(f"reused {old['head'][:8]}")
            else:
                with _stage_status(status, index + 1, len(commands)):
                    result = (_dispatch_one(config, worktree, head, base, command, timeout)
                              if config.check_runner else _run_one(config, worktree, prefix, command, timeout))
                if key:
                    result = replace(result, input_key=key)
            if status:
                status(f"stage {index + 1}/{len(commands)}: exit {result.exit_code} "
                       f"({result.duration_s:.1f}s)")
            if index >= len(preparations) or result.exit_code:
                results.append(result)
            if result.exit_code != 0:
                break
    final_head = _git(config, worktree, "rev-parse", "HEAD").strip()
    final_dirty = bool(_git(config, worktree, "status", "--porcelain").strip())
    if (final_head, final_dirty) != (head, dirty):
        results.append(CheckResult("<worktree changed during run>", 1, 0.0, (
                    f"HEAD before: {head}; HEAD after: {final_head}; "
                    f"dirty before: {dirty}; dirty after: {final_dirty}"
        )))
        dirty = True
    return CheckReport(head=head, dirty=dirty, results=tuple(results))


def _previous(config: Config, worktree: Path) -> dict:
    if _git(config, worktree, "ls-files", "--", ".loopzero"):
        return {}
    try:
        data = json.loads((worktree / ".loopzero/checks.json").read_text())
        if data["dirty"] is not False or not isinstance(data["head"], str):
            return {}
        return {r["command"]: dict(r, head=data["head"]) for r in data["results"]}
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return {}


def _input_key(config: Config, worktree: Path, command: str, base: str = "") -> str | None:
    if command not in config.inputs:
        return None
    paths = (*config.inputs[command], *config.inputs.get("*", ()))
    # Empty pathspecs must not accidentally mean the whole tree.
    empty = _git(config, worktree, "hash-object", "-t", "tree", "/dev/null").strip()
    blobs = _git(config, worktree, "diff-tree", "--no-commit-id", "--raw", "-r", "-z",
                 "--no-abbrev", empty, "HEAD", "--", *paths) if paths else ""
    settings = {k: v for k, v in asdict(config).items() if k in {
        "network", "env_allowlist", "sandbox_ro", "writable", "scratch", "env", "limits", "check_runner"}}
    if config.check_runner:
        settings["base"] = base
    settings["environment"] = {k: os.environ.get(k) for k in config.env_allowlist}
    payload = [command, __version__, settings, blobs]
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()


@contextmanager
def _stage_status(status: Callable[[str], None] | None, ordinal: int, total: int):
    stopped = threading.Event()
    started = time.monotonic()

    def heartbeat():
        while not stopped.wait(HEARTBEAT_INTERVAL):
            status(f"stage {ordinal}/{total}: running "
                   f"({time.monotonic() - started:.1f}s elapsed)")

    reporter = threading.Thread(target=heartbeat, daemon=True) if status else None
    if reporter:
        reporter.start()
    try:
        yield
    finally:
        stopped.set()
        if reporter:
            reporter.join()


def _preflight(config: Config, worktree: Path) -> tuple[CheckResult | None, tuple[str, ...]]:
    # Recognize a deliberately small argv grammar, never interpret shell syntax.
    groups = None
    for command in config.checks:
        if not re.fullmatch(r"[A-Za-z0-9_./=-]+(?: +[A-Za-z0-9_./=-]+)*", command):
            continue
        words = command.split()
        script = words[1] if re.fullmatch(r"python(?:3(?:\.\d+)?)?", words[0]) and len(words) > 1 else words[0]
        path = Path(script)
        if "=" in words[0] or any(path.is_relative_to(Path(entry)) for entry in config.scratch):
            continue
        if (not path.is_absolute() and "/" in script and ".." not in path.parts
                and not any((worktree / path).resolve().is_relative_to(Path(root)) for root in config.writable)
                and not (worktree / path).is_file()):
            return CheckResult(command, 1, 0.0,
                    f"Check preflight: repo-local file {script!r} is absent from this task tree. "
                    "If trusted-base checks advanced, integrate the current configured base "
                    "into this task branch (resolve conflicts), then rerun loopzero check; "
                    "otherwise restore the required file."), ()
        match = re.fullmatch(r"uv run((?: --group [A-Za-z0-9_-]+)*) [A-Za-z0-9_./][A-Za-z0-9_./-]*(?: [A-Za-z0-9_./=-]+)*", command)
        if match:
            groups = [*(groups or []), *match[1].split()[1::2]]
    flags = "".join(f" --group {group}" for group in dict.fromkeys(groups or []))
    return None, ((f"uv sync --offline --locked{flags}",)
                  if groups is not None and not config.network and (worktree / "pyproject.toml").is_file()
                  else ())


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


def _cap_limit(requested: int, resource_limit: int) -> int:
    hard = resource.getrlimit(resource_limit)[1]
    return requested if hard == resource.RLIM_INFINITY else min(requested, hard)


def _run_one(
    config: Config, worktree: Path, prefix: list[str], command: str, timeout: float
) -> CheckResult:
    try:
        limits = config.limits
        done = _proc.run(
            [*prefix, "prlimit",
             f"--as={_cap_limit(limits.memory_mb * 1024 * 1024, resource.RLIMIT_AS)}",
             f"--fsize={_cap_limit(limits.file_mb * 1024 * 1024, resource.RLIMIT_FSIZE)}",
             "--", "/bin/sh", "-c", command],
            cwd=worktree,
            env_allowlist=config.env_allowlist,
            timeout=timeout,
            merge_output=True,
        )
    except _proc.ProcTimeout as exc:
        return CheckResult(command, TIMEOUT_EXIT, timeout,
                           _proc.tail(exc.output + f"\n[loopzero] timed out after {timeout:g}s"))
    return CheckResult(command, done.exit_code, done.duration_s, _proc.tail(done.stdout))


def probe(config: Config, worktree: Path, prefix: list[str]) -> None:
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
    *,
    clearenv: bool = True,
    writable_binds: tuple[tuple[Path, Path], ...] = (),
) -> list[str]:
    """Build the bwrap command line up to and including the `--` separator.

    With ``clearenv=False`` the caller's (already filtered) environment is inherited
    instead of cleared, so secrets never appear as ``--setenv`` arguments.
    ``writable_binds`` overlays specific paths inside the otherwise private HOME.
    """
    argv = [
        "bwrap", "--die-with-parent", "--new-session", "--unshare-all",
        *(["--share-net"] if config.network else []),
        "--cap-drop", "ALL",
    ]
    for path in SYSTEM_RO:
        if Path(path).exists():
            argv += ["--ro-bind", path, path]
    argv += ["--dev", "/dev", "--proc", "/proc"]
    for path in ("/tmp", "/run", "/var/run"):
        argv += ["--tmpfs", path]
    # Restore resolv.conf (or its parent for masked symlinks) over the private tmpfs.
    if config.network:
        resolv_conf = Path("/etc/resolv.conf")
        resolved = resolv_conf.resolve()
        masked_roots = (Path("/tmp"), Path("/run"))
        if resolv_conf.is_symlink() and any(resolved.is_relative_to(root) for root in masked_roots):
            source = destination = resolved.parent
        else:
            source, destination = resolved, resolv_conf
        argv += ["--ro-bind", str(source), str(destination)]
    for path in config.sandbox_ro:
        argv += ["--ro-bind-try", path, path]
    for path in config.writable:
        argv += ["--bind-try", path, path]
    argv += ["--bind", str(home), SANDBOX_HOME]
    for source, destination in writable_binds:
        argv += ["--bind", str(source), str(destination)]
    # Read-only tree/Git binds follow tmpfs so paths under /tmp stay visible.
    binds = [worktree]
    for path in (common_dir, git_dir):
        if not any(path == seen or path.is_relative_to(seen) for seen in binds):
            binds.append(path)
    for path in binds:
        argv += ["--ro-bind", str(path), str(path)]
    for entry, source in (scratch or {}).items():
        argv += ["--bind", str(source), str(worktree / entry)]
    argv += ["--chdir", str(worktree), *(["--clearenv"] if clearenv else [])]
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


def _stage_unchanged(config: Config, worktree: Path, head: str) -> bool:
    return (_git(config, worktree, "rev-parse", "HEAD").strip() == head
            and not _git(config, worktree, "status", "--porcelain").strip())


def _unique_fields(pairs: list[tuple[str, object]]) -> dict[str, object]:
    obj = dict(pairs)
    if len(obj) != len(pairs):
        raise ValueError("duplicate result field")
    return obj


def _dispatch_one(
    config: Config, worktree: Path, head: str, base: str, command: str, timeout: float,
) -> CheckResult:
    env = {**SANDBOX_ENV, **dict(config.env)}
    binding = {"worktree": str(worktree.resolve()), "head": head, "base": base, "command": command, "network": config.network,
               "env_sha256": hashlib.sha256(json.dumps(
                   env, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()}
    try:
        if not _stage_unchanged(config, worktree, head):
            raise ValueError("Worktree changed before stage")
        executable = (config.check_runner[0] if Path(config.check_runner[0]).is_absolute()
                      else shutil.which(config.check_runner[0]))
        if not executable:
            raise ValueError("Stage runner executable missing")
        argv = (executable, *config.check_runner[1:])
        with tempfile.TemporaryDirectory(prefix="loopzero-runner-") as cwd:
            if any((Path(cwd) / path).resolve().is_relative_to(worktree.resolve())
                   or (not Path(path).is_absolute() and ".." in Path(path).parts
                       and not (Path(cwd) / path).resolve().is_relative_to(Path(cwd)))
                   for arg in (*argv, cwd)
                   for value in (arg, arg.partition("=")[2],
                                 arg[2:] if arg.startswith("-") and not arg.startswith("--") else "")
                   for path in {value, *value.split(":")}
                   if Path(path).is_absolute() or ".." in Path(path).parts):
                raise ValueError("Stage runner references task worktree")
            done = _proc.run(argv, cwd=cwd, env_allowlist=config.env_allowlist, timeout=timeout,
                             input=json.dumps({**binding, "env": env}), encoding="utf-8")
        frame = json.loads(done.stdout, object_pairs_hook=_unique_fields)
        if (done.exit_code != 0 or not isinstance(frame, dict)
                or set(frame) != {*binding, "exit_code", "duration_s", "not_started", "tail"}
                or any(frame[key] != value for key, value in binding.items())
                or type(frame["network"]) is not bool
                or type(frame["exit_code"]) is not int
                or type(frame["duration_s"]) not in (int, float)
                or not math.isfinite(frame["duration_s"]) or frame["duration_s"] < 0
                or type(frame["not_started"]) is not bool
                or not isinstance(frame["tail"], str)):
            raise ValueError("invalid or mismatched stage result")
        if not _stage_unchanged(config, worktree, head):
            raise ValueError("Worktree changed after stage")
        if frame["not_started"]:
            if frame["exit_code"] or frame["duration_s"] or frame["tail"]:
                raise ValueError("inconsistent not-started result")
            if not _stage_unchanged(config, worktree, head):
                raise ValueError("Worktree changed before fallback")
            local = run_checks(replace(config, check_runner=(), checks=(command,), inputs={}),
                               worktree, timeout=timeout, _resolved=True)
            if local.dirty or not local.results:
                raise ValueError("Local fallback failed")
            return local.results[-1]
        return CheckResult(command, frame["exit_code"], frame["duration_s"],
                           _proc.tail(frame["tail"]))
    except _proc.ProcTimeout as exc:
        return CheckResult(command, TIMEOUT_EXIT,
                           exc.duration_s if exc.duration_s is not None else timeout,
                           _proc.tail(exc.output + f"\n[loopzero] timed out after {timeout:g}s"))
    except (LoopZeroError, ValueError, TypeError, OverflowError, RecursionError, OSError) as exc:
        return CheckResult(command, 1, 0.0, f"Stage runner failed: {exc}")
