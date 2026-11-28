"""Run checks in bwrap, with best-effort resource limits from prlimit."""

from __future__ import annotations

import ast
import errno
import hashlib
import json
import os
import re
import resource
import shutil
import stat
import tempfile
import threading
import time
from collections.abc import Callable, Iterable, Iterator, Sequence
from contextlib import ExitStack, contextmanager, suppress
from dataclasses import asdict, replace
from os.path import commonpath
from pathlib import Path

from . import _proc, installed_version
from . import config as config_mod
from .types import CheckReport, CheckResult, Config, LoopZeroError, is_check_exit_code
from .worktree import GitError, WorktreeError, git, read_local_state

GIT_TIMEOUT = 60.0
DEFAULT_TIMEOUT = 3600.0
TIMEOUT_EXIT = 124
CHECK_SHELL = ("/bin/bash", "--posix", "-o", "pipefail", "-c")
CHECK_TAIL_BYTES = 16 * 1024
FIRST_HEARTBEAT_DELAY = 30.0
HEARTBEAT_INTERVAL = 300.0
SANDBOX_HOME = "/tmp/home"
CPU_GRANT_ENV = "HEAVY_SLOT_GRANTED_CORES"
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

_CHECK_CONTEXT_ENV = {*Config.env_allowlist, "TMP", "TEMP", "TMPDIR", "UV_PYTHON_INSTALL_DIR",
                      *(name for name in SANDBOX_ENV if name.endswith("_CACHE_DIR"))}
_CHECK_MODES = {"development", "production", "staging", "testing"}
_CREDENTIAL_ENV = re.compile(
    r"PASSWORD|PASSWD|PASSPHRASE|(?:^|_)(?:PWD|PIN|PAT|AUTH|TOKEN|SECRET|CREDENTIALS?|COOKIE|"
    r"(?:API|ACCESS|PRIVATE)_KEY)(?:_|$)", re.IGNORECASE,
)


class SandboxUnavailable(LoopZeroError):
    """bubblewrap is missing or cannot create a sandbox on this host."""


@contextmanager
def _private_directories():
    with ExitStack() as stack:
        try:
            yield stack
        finally:
            # Keep the stack's own exit as a fallback if SIGTERM arrives before deferral.
            with _proc.defer_sigterm():
                stack.close()


def _enter_private_directory(stack: ExitStack, prefix: str) -> Path:
    with _proc.defer_sigterm():
        return Path(stack.enter_context(tempfile.TemporaryDirectory(prefix=prefix)))


@contextmanager
def private_review_home():
    with _proc.unwind_on_sigterm(), _private_directories() as stack:
        yield _enter_private_directory(stack, "loopzero-review-home-")


def run_checks(config: Config, worktree: Path, *, timeout: float = DEFAULT_TIMEOUT,  # noqa: C901 - runner, reuse and cleanup paths
               progress: Callable[[str], None] | None = None,
               status: Callable[[str], None] | None = None,
               checkpoint: Callable[[CheckReport], None] | None = None,
               workflow: str | Path = "workflow.toml") -> CheckReport:
    """Run until a check fails, reporting commands via progress and stage metadata via status."""
    worktree = worktree.resolve()
    if shutil.which("bwrap") is None:
        raise SandboxUnavailable("bwrap binary missing: bwrap not found on PATH\nsudo apt install bubblewrap")
    head = _git(config, worktree, "rev-parse", "HEAD").strip()
    dirty = bool(_git(config, worktree, "status", "--porcelain", "--untracked-files=all", "--ignore-submodules=none").strip())
    tracked_diff = _tracked_diff(config, worktree) if dirty else ()
    base = _check_base(config, worktree)
    settings_sha256 = settings_fingerprint(config)
    previous, resuming = (_previous(config, worktree, settings_sha256, head)
                          if config.inputs and base and not dirty else ({}, False))
    common_dir, git_dir = ((worktree / _git(config, worktree, "rev-parse", flag, paths=True)).resolve()
                           for flag in ("--git-common-dir", "--git-dir"))

    _validate_scratch(config.scratch, worktree, common_dir, git_dir)
    with _proc.unwind_on_sigterm(), _private_directories() as stack:
        home = _enter_private_directory(stack, "loopzero-home-")
        scratch_root = _enter_private_directory(stack, "loopzero-scratch-")
        private_parent = Path(commonpath((home, scratch_root)))
        for exposed in (*config.sandbox_ro, *config.writable):
            if private_parent.is_relative_to(Path(exposed).resolve()):
                raise SandboxUnavailable(
                    f"sandbox path {exposed!r} would expose private check directories; set TMPDIR outside it"
                )
        scratch = {entry: scratch_root / entry for entry in config.scratch}
        for entry, source in scratch.items():
            source.mkdir(parents=True, exist_ok=True)
            # Mount points must exist before bwrap binds the source read-only.
            (worktree / entry).mkdir(parents=True, exist_ok=True)
        prefix = bwrap_argv(config, worktree, home, common_dir, git_dir, scratch)
        probe(config, worktree, prefix)
        _preflight_alternates(config, worktree, prefix, common_dir)
        missing, preparations = check_preflight(config, worktree)
        results = [missing] if missing else []
        commands = () if missing else (*preparations, *config.checks)
        last_run = _scratch_reuse_boundary(config, worktree, commands, base, previous, resuming)
        scratch_writers: set[str] = set()
        for ordinal, command in enumerate(_current_commands(config, worktree, commands, settings_sha256), 1):
            is_check = ordinal > len(preparations)
            if progress:
                progress(command)
            key = _input_key(config, worktree, command, "" if dirty else base or None,
                             status=status, workflow=workflow)
            old = previous.get(command)
            if key and old and old.input_key == key and ordinal > last_run:
                result = old
                status and status(f"reused {(old.reused_from or head)[:8]}")
            else:
                track_scratch = checkpoint is not None and bool(key) and bool(scratch) and is_check
                before = _scratch_snapshot(scratch.values()) if track_scratch else None
                with _stage_status(status, ordinal, len(commands)):
                    result = _run_one(config, worktree, prefix, command, timeout)
                result = replace(result, input_key=key)
                if track_scratch and not result.exit_code and (
                    before is None or before != _scratch_snapshot(scratch.values())
                ):
                    scratch_writers.add(command)
            if is_check or result.exit_code:
                results.append(result)
                if checkpoint is not None and (result.exit_code or result.input_key):
                    saved = tuple(results)
                    if not result.exit_code:
                        # Scratch writers must run again. The marker keeps this receipt nonpassing.
                        remaining = commands[max(ordinal, last_run):]
                        unvisited = (previous[c] for c in remaining if c in previous)
                        saved = tuple(replace(r, input_key=None) if r.command in scratch_writers else r
                                      for r in (*saved, *unvisited))
                        saved += (INCOMPLETE_CHECK,)
                    checkpoint(CheckReport(head, dirty, saved, settings_sha256))
            if status:
                status(f"stage {ordinal}/{len(commands)}: exit {result.exit_code} "
                       f"({result.duration_s:.1f}s)")
            if result.exit_code != 0:
                break
    final_head = _git(config, worktree, "rev-parse", "HEAD").strip()
    final_dirty = bool(_git(config, worktree, "status", "--porcelain", "--untracked-files=all", "--ignore-submodules=none").strip())
    if (final_head, final_dirty) != (head, dirty) or (dirty and tracked_diff != _tracked_diff(config, worktree)):
        results.append(CheckResult("<worktree changed during run>", 1, 0.0, (
                    f"HEAD before: {head}; HEAD after: {final_head}; "
                    f"dirty before: {dirty}; dirty after: {final_dirty}"
        )))
        dirty = True
    return CheckReport(head=head, dirty=dirty, results=tuple(results), settings_sha256=settings_sha256)


def _scratch_reuse_boundary(config: Config, worktree: Path, commands: tuple[str, ...],
                            base: str, previous: dict[str, CheckResult], resuming: bool) -> int:
    if config.scratch and previous:
        for ordinal in range(len(commands), 0, -1):
            command = commands[ordinal - 1]
            try:
                key = _input_key(config, worktree, command, base)
            except (SandboxUnavailable, _proc.ProcTimeout, OSError, ValueError):
                # Retry the lookup in stage order so earlier failures win.
                return ordinal
            old = previous.get(command)
            # A checkpoint can contain unvisited keyed stages with stale or missing results.
            if not key or (not resuming and (old is None or old.input_key != key)):
                return ordinal
    return 0


def _current_commands(config: Config, worktree: Path, commands: tuple[str, ...],
                      settings: str | None) -> Iterator[str]:
    if not config.trusted_base_revision:
        yield from commands
        return
    previous = config
    for command in commands:
        try:
            latest = config_mod.load_trusted(worktree, previous=previous)
        except (config_mod.ConfigError, _proc.ProcTimeout):
            # A failed local read does not establish that the pinned plan changed.
            latest = previous
        if (latest.trusted_base_revision != previous.trusted_base_revision
                and (latest.checks != config.checks or settings_fingerprint(latest) != settings)):
            raise config_mod.ConfigError("trusted check plan changed during the run. "
                                         "Run loopzero check again with the current workflow.")
        previous = latest
        yield command


INCOMPLETE_CHECK_COMMAND = "<checks incomplete>"
INCOMPLETE_CHECK = CheckResult(INCOMPLETE_CHECK_COMMAND, 1, 0.0, "")


def _scratch_snapshot(paths: Iterable[Path]) -> dict[Path, tuple[int, ...]] | None:
    """Track observable changes without reading files or following symlinks."""
    pending = list(paths)
    snapshot = {}
    try:
        while pending:
            path = pending.pop()
            info = path.lstat()
            snapshot[path] = (info.st_dev, info.st_ino, info.st_mode, info.st_size,
                              info.st_mtime_ns, info.st_ctime_ns)
            if stat.S_ISDIR(info.st_mode):
                pending.extend(path.iterdir())
    except OSError:
        return None
    return snapshot


def _previous(config: Config, worktree: Path, settings_sha256: str,
              head: str) -> tuple[dict[str, CheckResult], bool]:
    try:
        data = json.loads(read_local_state(worktree, "checks.json"))
        if (data["dirty"] is not False or not isinstance(data["head"], str)
                or data.get("settings_sha256") != settings_sha256):
            return {}, False
        previous = {}
        resuming = False
        for result in data["results"]:
            command, key = result.get("command"), result.get("input_key")
            code, tail = result.get("exit_code"), result.get("tail", "")
            if command == INCOMPLETE_CHECK_COMMAND and code == 1:
                resuming = data["head"] == head
            if (isinstance(command, str) and isinstance(key, str) and key
                    and is_check_exit_code(code) and code == 0 and isinstance(tail, str)):
                source = result.get("reused_from")
                reused_from = source if isinstance(source, str) else data["head"]
                tail = _proc.redact(tail, _check_secrets(config), *_proc.SECRET_PATTERNS)
                previous[command] = CheckResult(command, 0, 0.0, _check_tail(tail), key, reused_from)
        return previous, resuming
    except (OSError, WorktreeError, ValueError, KeyError, TypeError, AttributeError):
        return {}, False


def _check_base(config: Config, worktree: Path) -> str:
    if config.inputs:
        with suppress(config_mod.ConfigError, WorktreeError):
            return config.trusted_base_revision or config_mod.base_revision(worktree, config.base_branch)
    return ""


def _execution_settings(config: Config, base: str) -> dict:
    allowed = set(config.env_allowlist)
    allowlist = [name for name in Config.env_allowlist if name in allowed]
    allowlist.extend(sorted((allowed - set(Config.env_allowlist)) & os.environ.keys()))
    return {"network": config.network, "env_allowlist": allowlist, "sandbox_ro": config.sandbox_ro,
            "writable": config.writable, "scratch": config.scratch, "env": config.env, "limits": asdict(config.limits),
            "local_shell": CHECK_SHELL, "check_runner": ["redacted-tail-v1"],
            "environment": check_environment(config), **({"base": base} if config.inputs else {})}


def settings_fingerprint(config: Config, *, legacy_allowlist: tuple[str, ...] | None = None) -> str:
    settings = _execution_settings(config, config.base_branch)
    # Inherited host settings can differ at readiness. Stage reuse still binds them.
    settings["env_allowlist"] = (legacy_allowlist if legacy_allowlist is not None else
                                 [name for name in settings["env_allowlist"] if name != CPU_GRANT_ENV])
    for name in {"PATH", "LANG", "LC_ALL", "TERM", CPU_GRANT_ENV} - dict(config.env).keys():
        settings["environment"].pop(name, None)
    return hashlib.sha256(json.dumps([settings, config.inputs], sort_keys=True).encode()).hexdigest()


def _input_blobs(config: Config, worktree: Path, paths: tuple[str, ...]) -> str | None:
    # Empty pathspecs must not accidentally mean the whole tree.
    if not paths:
        return ""
    empty = _git(config, worktree, "hash-object", "-t", "tree", "/dev/null").strip()
    try:
        return _git(config, worktree, "-c", "core.quotePath=true", "diff-tree", "--no-commit-id",
                    "--raw", "-r", "--no-abbrev", empty, "HEAD", "--", *paths)
    except OSError as exc:
        if exc.errno == errno.E2BIG:
            return None
        raise


def check_inputs_match(config: Config, worktree: Path, report: CheckReport, *, bytes_only: bool = False) -> bool:
    base = None if bytes_only else _check_base(config, worktree)
    return all((not r.input_key if bytes_only and key == "" else
                r.input_key and key and (r.input_key.startswith(key) if bytes_only else r.input_key == key))
               for r in report.results if _has_inputs(config, r.command)
               for key in (_input_key(config, worktree, r.command, base),))


def _has_inputs(config: Config, command: str) -> bool:
    return command in config.inputs or "." in config.inputs.get("*", ())


def _input_key(config: Config, worktree: Path, command: str, base: str | None, *,
               status: Callable[[str], None] | None = None,
               workflow: str | Path = "workflow.toml") -> str | None:
    if not _has_inputs(config, command) or (base == "" and status is None):
        return None
    entries = {key: config.inputs[key] for key in (command, "*") if key in config.inputs}
    try:
        blobs = _input_blobs(config, worktree, tuple(path for paths in entries.values() for path in paths))
    except (SandboxUnavailable, _proc.ProcTimeout, OSError, ValueError):
        if not base:
            return None
        raise
    if blobs == "":
        if status:
            status(f"{workflow}: checks.inputs for {command!r} matches no files in HEAD. "
                   f"Running the check fresh with entries {entries!r}.")
        return "" if base is None else None  # Verifiable for readiness, unkeyed for reuse.
    return _hash_inputs(config, worktree, command, base, blobs) if blobs and base != "" else None


def _hash_inputs(config: Config, worktree: Path, command: str, base: str | None, blobs: str) -> str | None:
    contents = []
    try:
        root = worktree.resolve()
        for entry in filter(None, blobs.split("\n")):
            metadata, name = entry.split("\t", 1)
            if name.startswith('"'):
                name = os.fsdecode(ast.literal_eval("b" + name))
            path = root / name
            if path.parent.resolve(strict=True) != path.parent:
                return None
            mode, file_type = metadata.split()[1], path.lstat().st_mode
            if mode == "160000" and stat.S_ISDIR(file_type):
                if (not (path / ".git").exists() or _git(config, path, "rev-parse", "HEAD").strip() != metadata.split()[3]
                        or _git(config, path, "status", "--porcelain", "--untracked-files=all", "--ignore-submodules=none").strip()):
                    return None
                continue
            if mode == "120000" and stat.S_ISLNK(file_type):
                contents.append(hashlib.sha256(os.fsencode(os.readlink(path))).hexdigest())
                path = path.resolve()
                if (not path.is_relative_to(root) or not path.is_file()
                        or not _git(config, root, "ls-tree", "HEAD", "--", f":(literal){path.relative_to(root)}")
                        .startswith(("100644 ", "100755 "))):
                    continue
            elif mode not in {"100644", "100755"} or not stat.S_ISREG(file_type):
                return None
            with path.open("rb") as stream:
                contents.append(hashlib.file_digest(stream, "sha256").hexdigest())
    except (OSError, ValueError, SyntaxError, RuntimeError, SandboxUnavailable, _proc.ProcTimeout):
        return None
    prefix = hashlib.sha256(json.dumps([blobs, contents]).encode()).hexdigest() + ":"
    stage = [command, installed_version(), _execution_settings(config, base), blobs, contents] if base is not None else None
    return prefix + hashlib.sha256(json.dumps(stage, sort_keys=True).encode()).hexdigest() if stage is not None else prefix


@contextmanager
def _stage_status(status: Callable[[str], None] | None, ordinal: int, total: int):
    stopped = threading.Event()
    started = time.monotonic()

    def heartbeat():
        delay = FIRST_HEARTBEAT_DELAY
        while not stopped.wait(delay):
            status(f"stage {ordinal}/{total}: running "
                   f"({time.monotonic() - started:.1f}s elapsed)")
            delay = HEARTBEAT_INTERVAL

    reporter = threading.Thread(target=heartbeat, daemon=True) if status else None
    if reporter:
        reporter.start()
    join_reporter = True
    try:
        yield
    except _proc.Terminated:
        join_reporter = False
        raise
    finally:
        stopped.set()
        if reporter and join_reporter:
            reporter.join()


def _preflight_alternates(config: Config, worktree: Path, prefix: list[str], common_dir: Path) -> None:
    env = _proc.build_env(config.env_allowlist)
    if not {"GIT_OBJECT_DIRECTORY", "GIT_ALTERNATE_OBJECT_DIRECTORIES"} & env.keys():
        with suppress(OSError):
            try:
                (common_dir / "objects/info/alternates").lstat()
            except FileNotFoundError:
                return
    # Git resolves relative and transitive paths; probe actual mounts without broadening visibility.
    output = _git(config, worktree, "-c", "core.quotepath=true", "count-objects", "-v")
    for alternate in re.findall(r"^alternate: (.*)$", output, re.MULTILINE):
        if alternate.startswith('"'):
            alternate = os.fsdecode(ast.literal_eval("b" + alternate))
        remedy = (f"Git object alternate {alternate!r} is not readable in the check sandbox. "
                  f"Run `git repack -a -d` here, delete {common_dir / 'objects/info/alternates'}, "
                  "then rerun loopzero check. "
                  "Transfer this task branch to a standalone clone without --shared/--reference, "
                  "restore .loopzero/task.md, and rerun loopzero check at the same commit.")
        command = ("/bin/sh", "-c", 'test -d "$1" && test -r "$1" && test -x "$1"', "loopzero-alternate", alternate)
        try:
            result = _run_bwrap(config, worktree, prefix, command, timeout=GIT_TIMEOUT)
        except _proc.ProcTimeout:
            raise SandboxUnavailable(f"Git alternate visibility probe timed out. {remedy}") from None
        if result.exit_code != 0:
            raise SandboxUnavailable(remedy)


def check_preflight(config: Config, worktree: Path) -> tuple[CheckResult | None, tuple[str, ...]]:
    # Recognize a deliberately small argv grammar, never interpret shell syntax.
    groups = None
    for command in config.checks:
        if not re.fullmatch(r"[A-Za-z0-9_./=-]+(?: +[A-Za-z0-9_./=-]+)*", command):
            continue
        words = command.split()
        python = len(words) > 1 and re.fullmatch(r"python(?:3(?:\.\d+)?)?", words[0])
        script = words[1] if python else words[0]
        path = Path(script)
        target = worktree / path
        if "=" in words[0] or any(path.is_relative_to(Path(entry)) for entry in config.scratch):
            continue
        if (not path.is_absolute() and "/" in script and ".." not in path.parts
                and not any(target.resolve().is_relative_to(Path(root)) for root in config.writable)
                and not (target.is_file() or (python and target.is_dir()))):
            tail = _check_tail(
                f"Check preflight: repo-local file {script!r} is absent from this task tree. "
                "If trusted-base checks advanced, integrate the current configured base "
                "into this task branch (resolve conflicts), then rerun loopzero check; "
                "otherwise restore the required file.")
            return CheckResult(command, 1, 0.0, tail), ()
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
            raise SandboxUnavailable(f"unsafe scratch destination {entry!r}. Use a relative path inside the worktree in `checks.scratch`.")
        destination = worktree.joinpath(*parts)
        if parts[0] == ".git" or any(
            destination == path or destination.is_relative_to(path) for path in (common_dir, git_dir)
        ):
            raise SandboxUnavailable(f"scratch destination touches Git metadata: {entry!r}. Choose a scratch path outside Git metadata in `checks.scratch`.")
        current = worktree
        for part in parts:
            current /= part
            if current.is_symlink():
                raise SandboxUnavailable(f"scratch destination has symlink component: {entry!r}. Choose a path with no symlink components in `checks.scratch`.")


def _cap_limit(requested: int, resource_limit: int) -> int:
    hard = resource.getrlimit(resource_limit)[1]
    return requested if hard == resource.RLIM_INFINITY else min(requested, hard)


def _private_check_value(name: str, value: str) -> bool:
    normalized = value.strip()
    if name in _CHECK_CONTEXT_ENV or value == SANDBOX_ENV.get(name) or len(normalized) < 6:
        return False
    mode = name == "ENV" or name.endswith(("_ENV", "_ENVIRONMENT", "_MODE"))
    return bool(_CREDENTIAL_ENV.search(name)) or not (mode and normalized.casefold() in _CHECK_MODES)


def _check_secrets(config: Config) -> list[str]:
    """Forwarded host values to hide, longest first; committed [checks] env is public."""
    return _proc.secret_forms(value for name, value in _proc.build_env(config.env_allowlist).items()
                              if _private_check_value(name, value))


def _check_tail(text: str) -> str:
    encoded = text.rstrip("\n").encode("utf-8", errors="replace")[-CHECK_TAIL_BYTES:]
    return _proc.tail(encoded.decode("utf-8", errors="ignore"))


def _run_one(
    config: Config, worktree: Path, prefix: list[str], command: str, timeout: float
) -> CheckResult:
    try:
        limits = config.limits
        argv = (
            "prlimit", f"--as={_cap_limit(limits.memory_mb * 1024 * 1024, resource.RLIMIT_AS)}",
            f"--fsize={_cap_limit(limits.file_mb * 1024 * 1024, resource.RLIMIT_FSIZE)}",
            "--", *CHECK_SHELL, command,
        )
        done = _run_bwrap(config, worktree, prefix, argv, timeout=timeout, tail_chars=CHECK_TAIL_BYTES)
    except _proc.ProcTimeout as exc:
        tail = _proc.redact(exc.stdout, _check_secrets(config), *_proc.SECRET_PATTERNS)
        return CheckResult(command, TIMEOUT_EXIT, timeout, _check_tail(f"{tail}\n[loopzero] timed out after {timeout:g}s"))
    return CheckResult(command, done.exit_code, done.duration_s, _check_tail(done.stdout))


def probe(config: Config, worktree: Path, prefix: list[str]) -> None:
    """Raise `SandboxUnavailable` unless bwrap can build the exact sandbox we will use."""
    try:
        probe = _run_bwrap(config, worktree, prefix, ("true",), timeout=GIT_TIMEOUT)
    except _proc.ToolMissing as exc:
        if "PATH" not in config.env_allowlist:
            raise SandboxUnavailable(
                "bwrap was found but cannot be started: PATH is not in checks.env_allowlist; add PATH to it"
            ) from exc
        raise SandboxUnavailable(
            f"bwrap binary missing while starting the namespace probe: {exc}\n"
            "sudo apt install bubblewrap"
        ) from exc
    except _proc.ProcTimeout:
        raise SandboxUnavailable(f"bwrap namespace creation timed out after {GIT_TIMEOUT:g}s. Retry once, then ask the host operator to check bubblewrap if it still times out.") from None
    if probe.exit_code != 0:
        remedy = "\nAsk the host operator to check bubblewrap before retrying."
        probe_error = probe.stderr.lower()
        if any(word in probe_error for word in ("permission", "user namespace", "userns")):
            remedy = ("\nRun outside the enclosing agent sandbox or ask the host operator "
                      "to allow user namespaces for bwrap.")
        detail = _proc.tail(probe.stderr, 1)
        raise SandboxUnavailable(f"bwrap namespace creation failed: exit {probe.exit_code}"
                                 f"{f': {detail}' if detail else ''}{remedy}")


def check_environment(config: Config) -> dict[str, str]:
    return SANDBOX_ENV | _proc.build_env(config.env_allowlist, dict(config.env)) | {"HOME": SANDBOX_HOME}


def _run_bwrap(
    config: Config, worktree: Path, prefix: list[str], command: Sequence[str],
    *, timeout: float, tail_chars: int | None = None,
) -> _proc.Completed:
    if not prefix or prefix[-1] != "--":
        raise ValueError("sandbox prefix must end at the command separator")
    # Keep the environment option stream out of the launcher argv.
    with tempfile.TemporaryFile() as options:
        for name, value in check_environment(config).items():
            if "\0" in value:
                raise ValueError("embedded null byte in check environment")
            options.write(os.fsencode(f"--setenv\0{name}\0{value}\0"))
        options.seek(0)
        fd = options.fileno()
        return _proc.run(
            [*prefix[:-1], "--args", str(fd), "--", *command],
            cwd=worktree, env_allowlist=("PATH",) if "PATH" in config.env_allowlist else (),
            timeout=timeout, merge_output=tail_chars is not None, tail_chars=tail_chars, pass_fds=(fd,),
            tail_secrets=_check_secrets(config) if tail_chars is not None else None,
        )


def bwrap_argv(  # noqa: C901 - one bind rule per sandbox mount class
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
    """Build mounts and environment options before the command separator.

    Reviewers inherit their filtered environment and apply sandbox defaults.
    """
    argv = ["bwrap", "--die-with-parent", "--new-session", "--unshare-all"]
    if config.network:
        argv.append("--share-net")
    argv += ["--cap-drop", "ALL"]
    for path in SYSTEM_RO:
        if Path(path).exists():
            argv += ["--ro-bind", path, path]
    argv += ["--dev", "/dev", "--proc", "/proc"]
    argv += ["--tmpfs", "/tmp", "--tmpfs", "/run", "--tmpfs", "/var/run"]
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
    for flag, paths in (("--ro-bind-try", config.sandbox_ro), ("--bind-try", config.writable)):
        for path in paths:
            argv += [flag, path, path]
    for source, destination in ((home, SANDBOX_HOME), *writable_binds):
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
    if not clearenv:
        for key, value in check_environment(config).items():
            argv += ["--setenv", key, value]
    return [*argv, "--"]


def _tracked_diff(config: Config, worktree: Path) -> tuple[str, ...]:
    args = ("diff", "--binary", "--no-color", "--no-ext-diff", "--no-textconv", "--no-renames", "--ignore-submodules=none")
    return tuple(hashlib.sha256(_git(config, worktree, *args, *staged).encode()).hexdigest()
                 for staged in (("--cached",), ()))


def _git(config: Config, worktree: Path, *args: str, paths: bool = False) -> str:
    try:
        return git(worktree, *args, env_allowlist=config.env_allowlist, timeout=GIT_TIMEOUT, paths=paths, raw=True)
    except GitError as exc:
        raise SandboxUnavailable(f"{' '.join(exc.command)} failed in {worktree}: {_git_reason(exc.tail)}") from None


def _git_reason(tail: str) -> str:
    if "ambiguous argument 'HEAD'" in tail:
        return "this branch has no commit yet; make the first commit before running checks"
    return next((line for line in tail.splitlines() if line.startswith("fatal:")), _proc.tail(tail, 1))
