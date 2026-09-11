#!/usr/bin/env python3
"""Acceptance-command engine for governed dispatch (#3944 decomposition).

Owns acceptance-command parsing and classification (pnpm/pytest/uv/make and
shell wrapper analysis), test-database targeting and locking, acceptance
tooling preflight and authority capture, governed acceptance execution with
secret redaction, and the review-acceptance receipt. Extracted move-only from
`agent_dispatch.py`; `agent_dispatch` re-exports every public name so external
callers are unaffected. This module must not import `agent_dispatch`.
"""

import fcntl
import hashlib
import json
import os
import pwd
import re
import shlex
import shutil
import socket
import stat
import subprocess
import sys
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from contextvars import ContextVar
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import IO, Literal, cast
from urllib.parse import urlparse

from ._acceptance_grammar import (  # noqa: F401
    _acceptance_command_segments,
    _acceptance_shell_tokens,
    _direct_pytest_arguments,
    _directory_operand,
    _escaping_directory_operand,
    _is_pytest_no_value_flag,
    _is_source_only_test_path_command,
    _make_targets_database,
    _pnpm_arguments,
    _pnpm_command_arguments,
    _pnpm_exec_executable,
    _pnpm_script_executables,
    _pnpm_script_name,
    _pnpm_uses_filter,
    _pytest_arguments,
    _pytest_database_decision,
    _pytest_targets_database,
    _script_executables,
    _segment_targets_database,
    _shell_command_operand,
    _ShellCommandOperand,
    _strictly_requires_database,
    _strictly_targets_database,
    _strip_environment_prefix,
    _timeout_wrapped_tokens,
    _uv_run_command_tokens,
    _wrapped_targets_database,
    acceptance_requires_database,
    DB_MAKE_TARGETS,
    ENV_FLAG_OPTIONS,
    ENV_OPTIONS_WITH_VALUE,
    pin_pnpm_commands,
    PNPM_DIRECTORY_OPTIONS,
    PNPM_FILTER_OPTIONS,
    PNPM_INVOKE_PATTERN,
    PNPM_OPTIONS_WITH_VALUE,
    PYTEST_FLAG_OPTIONS,
    PYTEST_OPTIONS_WITH_VALUE,
    PYTEST_POSITIONAL_TARGET_PATTERN,
    PYTEST_ROOT_TARGET_PATTERN,
    PYTEST_TARGET_PATTERN,
    SHELL_ASSIGNMENT_PATTERN,
    SHELL_BUILTINS,
    SHELL_COMMAND_SEPARATOR_CHARS,
    SHELL_OPTIONS_WITH_VALUE,
    SHELL_WRAPPER_NAMES,
    TIMEOUT_DURATION_PATTERN,
    TIMEOUT_LONG_FLAG_OPTIONS,
    TIMEOUT_LONG_OPTIONS_WITH_VALUE,
    TIMEOUT_SHORT_FLAG_OPTIONS,
    TIMEOUT_SHORT_OPTIONS_WITH_VALUE,
    UV_RUN_FLAG_OPTIONS,
    UV_RUN_OPTIONS_WITH_VALUE,
)
from ..kernel.gitscope import (
    DISPATCH_DIR,
    REVIEW_ACCEPTANCE_RECEIPT_SCHEMA_VERSION,
    DispatchError,
    ReviewSnapshot,
    primary_repo_root,
    resolved_record_worktree,
    task_contract_hash,
    trusted_git_command,
)
from ..kernel.sandbox import (
    SandboxError,
    current_boundary_reusable,
)
from ..kernel.sandbox import (
    command as sandbox_command,
)
from ..kernel.sandbox import (
    environment as sandbox_environment,
)
from ..kernel.trusted_exec import (
    TrustedExecutableError,
    system_executable,
)
from ..kernel.worktree_lease import (
    LEASE_BOUNDARY_ENV,
    LEASE_FD_ENV,
    LEASE_NONCE_ENV,
    LEASE_OWNER_PID_ENV,
)
from ..config import Profile
from ..kernel import settings as kernel_settings
from . import _acceptance_grammar

_PROFILE: ContextVar[Profile | None] = ContextVar("acceptance_profile", default=None)


def configure(profile: Profile) -> None:
    """Configure acceptance grammar and tool paths from a validated profile."""
    _acceptance_grammar.configure(profile)
    _PROFILE.set(profile)


def _toolchain() -> dict[str, object]:
    profile = _PROFILE.get()
    return profile.toolchain if profile else kernel_settings.settings.toolchain


def _audit_root() -> Path:
    profile = _PROFILE.get()
    return profile.audit_root if profile else kernel_settings.settings.audit_root


def _env_name(suffix: str) -> str:
    profile = _PROFILE.get()
    prefix = profile.env_prefix if profile else kernel_settings.settings.env_prefix
    return f"{prefix}_{suffix}"


def acceptance_environment_failure(*, exit_code: int, output: str) -> bool:
    """Return whether an acceptance command could not reach the test itself."""
    if exit_code == 127:
        return True
    lowered = output.lower()
    return bool(
        "local binary is missing:" in lowered
        or re.search(r'\bcommand ["`][^"`]+["`] not found\b', lowered)
        or re.search(
            r"(?:^|\n)(?:/bin/)?(?:ba|da|z)?sh: \d+: [^\n]+: not found\b",
            lowered,
        )
    )


def _database_unreachable_output(lowered: str) -> bool:
    """Shortlist output that suggests the test database was unreachable.

    Output text alone never classifies (a test may legitimately embed these
    strings); `run_acceptance_commands` confirms with a live probe before
    treating the failure as a harness gap (WF-2026-07-29-6, review I-1).
    """
    if "test db not ready at" in lowered:
        return True
    database_marker = any(
        marker in lowered
        for marker in ("asyncpg", "psycopg", "postgres", "sqlalchemy.exc.operational")
    )
    refusal = re.search(
        r"connection refused|could not connect|connection reset by peer"
        r"|no route to host|name or service not known",
        lowered,
    )
    return bool(database_marker and refusal)


def _structured_database_unavailable_claim(value: object) -> bool:
    """Recognize one bounded worker claim that DB acceptance was unavailable."""
    if not isinstance(value, str):
        return False
    lowered = value.lower()
    database_marker = any(
        marker in lowered
        for marker in (
            "asyncpg",
            "psycopg",
            "postgres",
            "sqlalchemy.exc.operational",
        )
    )
    unavailable_marker = any(
        marker in lowered
        for marker in (
            "connection refused",
            "could not connect",
            "connection reset by peer",
            "no route to host",
            "name or service not known",
            "test db not ready at",
            "test service is unavailable",
            "database acceptance unavailable",
        )
    )
    return database_marker and unavailable_marker


def blocked_result_reports_acceptance_environment(
    result: dict[str, object], *, task: dict[str, object]
) -> bool:
    """Return whether a blocked write deposit belongs in host revalidation.

    Worker prose alone is not enough. The result must pair a bounded DB
    environment claim with a failed test whose command exactly matches one of
    the task's declared DB-backed acceptance commands. Revalidation remains
    fail closed and reruns those commands outside the worker sandbox.
    """
    if result.get("status") != "blocked" or not (
        _structured_database_unavailable_claim(result.get("summary"))
        or _structured_database_unavailable_claim(result.get("escalation_reason"))
    ):
        return False
    raw_commands = [
        *cast(list[object], task.get("acceptance_commands") or []),
        *cast(list[object], task.get("db_acceptance_commands") or []),
    ]
    declared_db_commands = {
        command
        for command in raw_commands
        if isinstance(command, str) and acceptance_requires_database(command)
    }
    if not declared_db_commands:
        return False
    tests = result.get("tests")
    if not isinstance(tests, list):
        return False
    return any(
        isinstance(test, dict)
        and test.get("command") in declared_db_commands
        and isinstance(test.get("exit_code"), int)
        and not isinstance(test.get("exit_code"), bool)
        and test.get("exit_code") != 0
        and _structured_database_unavailable_claim(test.get("summary"))
        for test in tests
    )


def acceptance_failure_class(results: Sequence[dict[str, object]]) -> str:
    failed = [result for result in results if result.get("exit_code") != 0]
    if failed and all(
        result.get("failure_class") == "acceptance-environment" for result in failed
    ):
        return "acceptance-environment"
    return "acceptance"


def _acceptance_worktree(command: str, worktree: Path) -> Path:
    """Find the declared package directory without executing acceptance code.

    An absolute or ``..``-escaping operand clamps to the worktree root so
    nothing ever reads files (for example ``package.json``) from an
    arbitrary command-controlled path outside the worktree; preflight
    additionally reports the escaping operand as an error.
    """
    operand = _directory_operand(command)
    if operand is None or _escaping_directory_operand(command, worktree) is not None:
        return worktree
    return worktree / operand


@dataclass(frozen=True)
class AcceptanceAuthority:
    """Host-captured inputs shared by one acceptance transaction."""

    primary: Path
    test_database_url: str | None


def capture_acceptance_authority(
    primary: Path, *, db_bound: bool
) -> AcceptanceAuthority:
    """Capture mutable host configuration once, before candidate execution."""
    resolved_primary = primary.resolve()
    test_database_url = (
        _dotenv_test_database_url(
            resolved_primary / str(_toolchain().get("dotenv", ".env")),
            tuple(str(name) for name in _toolchain().get("db_url_vars", ("TEST_DATABASE_URL",))),
        )
        if db_bound
        else None
    )
    return AcceptanceAuthority(
        primary=resolved_primary,
        test_database_url=(
            test_database_url or str(_toolchain().get("db_default_url", "")) or None
            if db_bound
            else None
        ),
    )


def _acceptance_authority_for(worktree: Path, *, db_bound: bool) -> AcceptanceAuthority:
    try:
        primary = primary_repo_root(worktree)
    except DispatchError:
        primary = worktree
    return capture_acceptance_authority(primary, db_bound=db_bound)


def test_database_unreachable(authority: AcceptanceAuthority) -> str | None:
    """Probe the captured test DB endpoint without executing candidate code."""
    raw_url = authority.test_database_url
    if not raw_url:
        return "test database configuration is unreadable"
    try:
        parsed = urlparse(raw_url)
        host = parsed.hostname or "localhost"
        port = parsed.port or 5433
    except ValueError:
        return "test database configuration is unreadable"
    try:
        connection = socket.create_connection((host, port), timeout=1.0)
        connection.close()
    except TimeoutError:
        return (
            "test database probe timed out after 1s; start it "
            "(make infra-start) or use an isolated stack (make start-isolated) "
            "before dispatching"
        )
    except OSError as exc:
        return (
            "test database unreachable for DB-dependent acceptance "
            f"({host}:{port}: {exc}); start it (make infra-start) or use an "
            "isolated stack (make start-isolated) before dispatching"
        )
    return None


def preflight_acceptance_tooling(
    commands: Sequence[str],
    *,
    worktree: Path,
    db_commands: Sequence[str] = (),
    authority: AcceptanceAuthority | None = None,
) -> list[str]:
    """Return actionable dependency gaps before a write worker is started."""
    errors: list[str] = []
    # WF-2026-07-29-5: a DB-dependent acceptance suite in a DB-less sandbox
    # must fail before model launch, never after a spent attempt. Explicitly
    # marked db_acceptance_commands (#3029) always require the probe.
    db_required = bool(db_commands) or any(
        acceptance_requires_database(command) for command in commands
    )
    if db_required:
        try:
            effective_authority = authority or _acceptance_authority_for(
                worktree, db_bound=True
            )
        except SandboxError as exc:
            errors.append(
                redact_acceptance_output(f"acceptance authority capture failed: {exc}")
            )
            effective_authority = None
        if effective_authority is not None:
            database_gap = test_database_unreachable(effective_authority)
            if database_gap is not None:
                errors.append(database_gap)
    for command in pin_pnpm_commands([*commands, *db_commands]):
        escaping = _escaping_directory_operand(command, worktree)
        if escaping is not None:
            errors.append(
                "acceptance command directory operand escapes the worktree: "
                f"{escaping}"
            )
            continue
        command_worktree = _acceptance_worktree(command, worktree)
        if re.search(PNPM_INVOKE_PATTERN, command):
            if "corepack pnpm" in command and shutil.which("corepack") is None:
                errors.append("Corepack is unavailable; install or enable Corepack")
                continue
            if _pnpm_uses_filter(command):
                errors.append(
                    "filtered pnpm acceptance cannot be preflighted to one package; "
                    "use -C/--dir with a concrete package directory"
                )
                continue
            exec_executable = _pnpm_exec_executable(command)
            executables = (
                [exec_executable]
                if exec_executable
                else _pnpm_script_executables(command, command_worktree)
            )
            for executable in executables:
                candidates = (
                    command_worktree / "node_modules" / ".bin" / executable,
                    worktree / "node_modules" / ".bin" / executable,
                )
                if not any(
                    binary.is_file() and os.access(binary, os.X_OK)
                    for binary in candidates
                ):
                    binary = candidates[0]
                    errors.append(
                        "frontend dependency missing: "
                        f"{_worktree_display(binary, worktree)}; run the frozen "
                        "pnpm install for this worktree"
                    )
        if re.search(r"\buv\s+run\s+pytest\b", command):
            if shutil.which("uv") is None:
                errors.append("uv is unavailable; install the backend toolchain")
                continue
            interpreter = Path(str(_toolchain().get("interpreter", ".venv/bin/python")))
            pytest_binary = worktree / interpreter.parent / "pytest"
            if not pytest_binary.is_file() or not os.access(pytest_binary, os.X_OK):
                errors.append(
                    "backend dependency missing: "
                    f"{_worktree_display(pytest_binary, worktree)}; run uv sync "
                    "in this worktree"
                )
    return errors


def _worktree_display(path: Path, worktree: Path) -> str:
    """Worktree-relative display form; absolute when a command's cd/-C operand
    escapes the worktree, so preflight reports instead of raising ValueError."""
    try:
        return str(path.relative_to(worktree))
    except ValueError:
        return str(path)


def test_db_lock_path(worktree: Path) -> Path:
    """Shared test-DB serialization lock, anchored at the primary repo.

    Worktrees share one local test database (`parallel-agents.mdc`), so the
    lock must live where every worktree resolves the same file. Outside a git
    checkout (unit tests) the worktree itself is the anchor.
    """
    configured = _toolchain().get("db_lock")
    if not isinstance(configured, str) or not Path(configured).is_absolute():
        raise DispatchError("acceptance DB lock is not configured")
    return Path(configured)


def _acquire_test_db_lock(worktree: Path, *, timeout_s: int) -> IO[str] | None:
    """Take the shared test-DB flock, or return None after timeout_s."""
    lock_path = test_db_lock_path(worktree)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    handle = lock_path.open("a")
    deadline = time.monotonic() + timeout_s
    while True:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            return handle
        except OSError:
            if time.monotonic() >= deadline:
                handle.close()
                return None
            time.sleep(0.5)


def _canonical_db_lock_transport_candidate(
    command: str, *, worktree: Path
) -> str | None:
    """Return the inner command for one exact, redundant host-lock wrapper."""
    dotenv = Path(str(_toolchain().get("dotenv", ".env")))
    root = dotenv.parent.as_posix()
    cd_prefix = f"cd {root} && " if root != "." else ""
    transport_prefix = cd_prefix if cd_prefix and command.startswith(cd_prefix) else ""
    wrapped = command[len(transport_prefix) :]
    lock_path = test_db_lock_path(worktree)
    inner: str | None = None
    for executable in ("flock", "/usr/bin/flock"):
        prefix = f"{executable} -x {lock_path} "
        if wrapped.startswith(prefix):
            inner = wrapped[len(prefix) :]
            break
    if inner is None or not inner:
        return None
    if any(character in inner for character in ";&|\n\r<>`") or "$" in inner:
        return None
    segments = _acceptance_command_segments(inner)
    if (
        segments is None
        or len(segments) != 1
        or (
            bool(inner_tokens := _strip_environment_prefix(segments[0]))
            and Path(inner_tokens[0]).name in SHELL_WRAPPER_NAMES
        )
        or not _strictly_targets_database(segments[0])
    ):
        return None
    return f"{transport_prefix}{inner}"


def _canonical_db_lock_transport_command(
    command: str, *, worktree: Path, db_lock: IO[str] | None
) -> str | None:
    """Strip a redundant canonical wrapper only while its host lock is held."""
    if db_lock is None:
        return None
    lock_path = test_db_lock_path(worktree)
    try:
        lock_stat = lock_path.stat()
        handle_stat = os.fstat(db_lock.fileno())
    except (OSError, ValueError):
        return None
    if (lock_stat.st_dev, lock_stat.st_ino) != (handle_stat.st_dev, handle_stat.st_ino):
        return None
    return _canonical_db_lock_transport_candidate(command, worktree=worktree)


def exact_repair_file_bindings(worktree: Path, paths: tuple[str, ...]) -> list[str]:
    """Pin existing regular source files; readonly parents prevent replacement escapes."""
    bindings: list[str] = []
    if not paths or len(set(paths)) != len(paths):
        raise SandboxError("review repair requires distinct exact files")
    for relative in paths:
        parsed = PurePosixPath(relative)
        if (parsed.is_absolute() or str(parsed) != relative or not parsed.parts
            or any(part in {".", ".."} for part in parsed.parts)
            or parsed.parts[0] in {".git", _audit_root().parts[0]}
            or any(char in relative for char in "*?\\:\x00\n\r")):
            raise SandboxError("unsafe review repair file")
        target = worktree
        for part in parsed.parts:
            target /= part
            if target.is_symlink():
                raise SandboxError("review repair file has a symlink component")
        try:
            info = target.stat()
        except OSError as exc:
            raise SandboxError("review repair requires existing regular files") from exc
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise SandboxError("review repair file is not an unaliased regular file")
        bindings.extend(("--bind", str(target), str(target)))
    completed = subprocess.run(
        trusted_git_command(worktree, "ls-files", "--cached", "-z"),
        capture_output=True,
        check=False,
        env=sandbox_environment(os.environ),
    )
    if completed.returncode != 0:
        raise SandboxError("cannot verify tracked source files for review repair")
    tracked = {os.fsdecode(path) for path in completed.stdout.split(b"\0") if path}
    if not set(paths).issubset(tracked):
        raise SandboxError("review repair requires tracked source files")
    return bindings


def run_acceptance_commands(
    commands: Sequence[str],
    *,
    worktree: Path,
    timeout_s: int,
    db_commands: Sequence[str] = (),
    authority: AcceptanceAuthority | None = None,
    source_write_files: tuple[str, ...] | None = None,
) -> list[dict[str, object]]:
    """Janitor-lite: the dispatcher runs acceptance commands itself.

    Worker-reported test results are informational only; the deterministic
    gate is this run (research: merge gates on concrete signals, never on
    worker self-reports). Explicitly marked `db_commands` (#3029) run last;
    every DB-bound command — explicit or heuristic — executes under the shared
    test-DB flock so parallel dispatches serialize on the one local database.
    """
    # (command, db_bound, network_grant): classify raw authority first. The only
    # additional grant below is for a strict DB command inside the validated
    # canonical transport while the host owns its shared database lock.
    ordered = [
        (
            command,
            acceptance_requires_database(command),
            _acceptance_network_grant(command, explicit_db=False),
        )
        for command in commands
    ] + [(command, True, True) for command in db_commands]
    try:
        effective_authority = authority or _acceptance_authority_for(
            worktree, db_bound=True
        )
    except SandboxError as exc:
        tail = redact_acceptance_output(f"acceptance authority capture failed: {exc}")
        return [
            {
                "command": redact_acceptance_output(command),
                "exit_code": 125,
                "tail": tail,
                "failure_class": "acceptance-environment",
                **({"db_bound": True} if db_bound else {}),
            }
            for command, db_bound, _ in zip(
                pin_pnpm_commands([raw for raw, _, _ in ordered]),
                (db_bound for _, db_bound, _ in ordered),
                (grant for _, _, grant in ordered),
                strict=True,
            )
        ]
    results: list[dict[str, object]] = []
    db_lock: IO[str] | None = None
    db_lock_timed_out = False
    try:
        for command, db_bound, network_grant in zip(
            pin_pnpm_commands([raw for raw, _, _ in ordered]),
            (db_bound for _, db_bound, _ in ordered),
            (grant for _, _, grant in ordered),
            strict=True,
        ):
            if db_bound and db_lock is None and not db_lock_timed_out:
                db_lock = _acquire_test_db_lock(worktree, timeout_s=timeout_s)
                db_lock_timed_out = db_lock is None
            if db_bound and db_lock_timed_out:
                results.append(
                    {
                        # Redacted like every other persisted result: this
                        # branch bypasses _run_one_acceptance_command and its
                        # secret-bearing rejection.
                        "command": redact_acceptance_output(command),
                        "exit_code": 124,
                        "tail": (
                            "test-DB serialization lock timed out; another "
                            "DB-bound acceptance run holds the shared database"
                        ),
                        "failure_class": "acceptance-environment",
                        "db_bound": True,
                    }
                )
                continue
            transport_command = (
                _canonical_db_lock_transport_command(
                    command, worktree=worktree, db_lock=db_lock
                )
                if db_bound
                else None
            )
            # This bounded authority extension applies only after the canonical
            # host lock and exact transport were validated. Raw wrapper grammar
            # alone remains insufficient to grant database network access.
            if transport_command is not None:
                network_grant = network_grant or _acceptance_network_grant(
                    transport_command, explicit_db=False
                )
            result = _run_one_acceptance_command(
                transport_command or command,
                worktree=worktree,
                timeout_s=timeout_s,
                db_bound=db_bound,
                db_network_grant=network_grant,
                authority=effective_authority,
                **(
                    {"source_write_files": source_write_files}
                    if source_write_files is not None
                    else {}
                ),
            )
            if transport_command is not None:
                result["command"] = redact_acceptance_output(command)
                result["transport_command"] = redact_acceptance_output(
                    transport_command
                )
            if db_bound:
                result["db_bound"] = True
            results.append(result)
    finally:
        if db_lock is not None:
            fcntl.flock(db_lock.fileno(), fcntl.LOCK_UN)
            db_lock.close()
    return results


def _declared_commands_sha256(commands: Sequence[str]) -> str:
    payload = json.dumps(
        list(commands), ensure_ascii=False, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def build_review_acceptance_receipt(
    *,
    task: dict[str, object],
    source_identity: dict[str, object],
    snapshot: ReviewSnapshot,
    worktree: Path,
    results: Sequence[dict[str, object]],
    provenance: str,
    duration_s: float = 0.0,
) -> dict[str, object]:
    """Bind dispatcher-run acceptance to one immutable review snapshot."""
    raw_commands = task.get("acceptance_commands")
    if not isinstance(raw_commands, list) or not all(
        isinstance(command, str) and command for command in raw_commands
    ):
        raise DispatchError("review acceptance commands are invalid")
    declared_commands = [str(command) for command in raw_commands]
    executed_commands = pin_pnpm_commands(declared_commands)
    if provenance not in {"pre-model", "recovery-time"}:
        raise DispatchError("review acceptance receipt provenance is invalid")
    if (
        not isinstance(duration_s, (int, float))
        or isinstance(duration_s, bool)
        or duration_s < 0
    ):
        raise DispatchError("review acceptance duration is invalid")
    if len(results) != len(declared_commands):
        raise DispatchError("review acceptance result count does not match commands")
    receipt_results: list[dict[str, object]] = []
    for declared, executed, result in zip(
        declared_commands, executed_commands, results, strict=True
    ):
        if result.get("command") != executed:
            raise DispatchError(
                "review acceptance executed command does not match normalization"
            )
        exit_code = result.get("exit_code")
        if not isinstance(exit_code, int) or isinstance(exit_code, bool):
            raise DispatchError("review acceptance result exit_code is invalid")
        receipt_result = {
            "declared_command": declared,
            "executed_command": executed,
            "exit_code": exit_code,
            "tail": str(result.get("tail") or ""),
        }
        expected_transport = _canonical_db_lock_transport_candidate(
            executed, worktree=worktree
        )
        if expected_transport is None:
            if "transport_command" in result:
                raise DispatchError(
                    "review acceptance result has unexpected transport command"
                )
        elif (
            result.get("transport_command") != expected_transport
        ):
            raise DispatchError(
                "review acceptance transport command does not match normalization"
            )
        else:
            receipt_result["transport_command"] = expected_transport
        for key in ("failure_class", "db_bound"):
            if key in result:
                receipt_result[key] = result[key]
        receipt_results.append(receipt_result)
    return {
        "schema_version": REVIEW_ACCEPTANCE_RECEIPT_SCHEMA_VERSION,
        "owner": "dispatcher",
        "provenance": provenance,
        "validated_at": datetime.now(UTC).isoformat(),
        "duration_s": round(float(duration_s), 3),
        "validation_worktree": str(worktree.resolve()),
        "task_contract_hash": task_contract_hash(task),
        "review_intent": task.get("review_intent"),
        "review_lens": task.get("review_lens"),
        "source_identity": dict(source_identity),
        "snapshot_sha": snapshot.commit_sha,
        "snapshot_tree_sha": snapshot.tree_sha,
        "patch_identity": snapshot.patch_identity,
        "declared_commands": declared_commands,
        "declared_commands_sha256": _declared_commands_sha256(declared_commands),
        "results": receipt_results,
    }


def review_acceptance_receipt_reasons(
    record: dict[str, object],
    receipt: dict[str, object] | None = None,
) -> tuple[str, ...]:
    """Return fail-closed gaps in one read-only review acceptance receipt."""
    contract = record.get("task_contract")
    if isinstance(contract, dict) and task_contract_hash(contract) != record.get(
        "task_contract_hash"
    ):
        return ("invalid-review-acceptance-contract",)
    raw_commands: object
    if isinstance(contract, dict):
        raw_commands = contract.get("acceptance_commands", [])
    else:
        raw_commands = record.get("acceptance_commands", [])
    if not isinstance(raw_commands, list) or not all(
        isinstance(command, str) and command for command in raw_commands
    ):
        return ("invalid-review-acceptance-contract",)
    declared_commands = [str(command) for command in raw_commands]
    if not declared_commands:
        return ()
    candidate = receipt if receipt is not None else record.get("acceptance_receipt")
    if not isinstance(candidate, dict):
        return ("missing-review-acceptance-receipt",)
    reasons: list[str] = []
    if (
        candidate.get("schema_version")
        not in {"review-acceptance-v1", REVIEW_ACCEPTANCE_RECEIPT_SCHEMA_VERSION}
        or candidate.get("owner") != "dispatcher"
        or candidate.get("provenance") not in {"pre-model", "recovery-time"}
        or not isinstance(candidate.get("validated_at"), str)
        or not candidate.get("validated_at")
        or not isinstance(candidate.get("duration_s"), (int, float))
        or isinstance(candidate.get("duration_s"), bool)
        or cast(float, candidate.get("duration_s")) < 0
    ):
        reasons.append("invalid-review-acceptance-provenance")
    expected_worktree = record.get("worktree")
    resolved_worktree = resolved_record_worktree(expected_worktree)
    if resolved_worktree is None or candidate.get("validation_worktree") != str(
        resolved_worktree
    ):
        reasons.append("review-acceptance-worktree-mismatch")
    expected_contract_hash = record.get("task_contract_hash")
    if not isinstance(expected_contract_hash, str) and isinstance(contract, dict):
        expected_contract_hash = task_contract_hash(contract)
    if candidate.get("task_contract_hash") != expected_contract_hash:
        reasons.append("review-acceptance-contract-mismatch")
    expected_review_intent = (
        contract.get("review_intent") if isinstance(contract, dict) else None
    )
    if (
        expected_review_intent is not None
        and candidate.get("review_intent") != expected_review_intent
    ):
        reasons.append("review-acceptance-intent-mismatch")
    expected_review_lens = (
        contract.get("review_lens") if isinstance(contract, dict) else None
    )
    if (
        expected_review_lens is not None
        and candidate.get("review_lens") != expected_review_lens
    ):
        reasons.append("review-acceptance-lens-mismatch")
    for field in ("source_identity", "snapshot_sha", "snapshot_tree_sha"):
        expected = record.get(field)
        if expected is None or candidate.get(field) != expected:
            reasons.append(f"review-acceptance-{field.replace('_', '-')}-mismatch")
    if record.get("patch_identity") is not None and (
        candidate.get("schema_version") != REVIEW_ACCEPTANCE_RECEIPT_SCHEMA_VERSION
        or candidate.get("patch_identity") != record.get("patch_identity")
    ):
        reasons.append("review-acceptance-patch-identity-mismatch")
    if candidate.get("declared_commands") != declared_commands or candidate.get(
        "declared_commands_sha256"
    ) != _declared_commands_sha256(declared_commands):
        reasons.append("review-acceptance-command-mismatch")
    raw_results = candidate.get("results")
    expected_executed = pin_pnpm_commands(declared_commands)
    if not isinstance(raw_results, list) or len(raw_results) != len(declared_commands):
        reasons.append("review-acceptance-result-mismatch")
    else:
        for declared, executed, result in zip(
            declared_commands, expected_executed, raw_results, strict=True
        ):
            expected_transport = (
                _canonical_db_lock_transport_candidate(
                    executed, worktree=resolved_worktree
                )
                if resolved_worktree is not None
                else None
            )
            transport_matches = (
                "transport_command" not in result
                if expected_transport is None and isinstance(result, dict)
                else isinstance(result, dict)
                and result.get("transport_command") == expected_transport
            )
            if (
                not isinstance(result, dict)
                or result.get("declared_command") != declared
                or result.get("executed_command") != executed
                or not transport_matches
                or not isinstance(result.get("exit_code"), int)
                or isinstance(result.get("exit_code"), bool)
                or result.get("exit_code") != 0
            ):
                reasons.append("review-acceptance-result-mismatch")
                break
    terminal_acceptance = record.get("acceptance")
    if not isinstance(terminal_acceptance, list) or len(terminal_acceptance) != len(
        declared_commands
    ):
        reasons.append("review-acceptance-terminal-mismatch")
    else:
        for executed, result in zip(
            expected_executed, terminal_acceptance, strict=True
        ):
            expected_transport = (
                _canonical_db_lock_transport_candidate(
                    executed, worktree=resolved_worktree
                )
                if resolved_worktree is not None
                else None
            )
            transport_matches = (
                "transport_command" not in result
                if expected_transport is None and isinstance(result, dict)
                else isinstance(result, dict)
                and result.get("transport_command") == expected_transport
            )
            if (
                not isinstance(result, dict)
                or result.get("command") != executed
                or not transport_matches
                or not isinstance(result.get("exit_code"), int)
                or isinstance(result.get("exit_code"), bool)
                or result.get("exit_code") != 0
            ):
                reasons.append("review-acceptance-terminal-mismatch")
                break
    return tuple(dict.fromkeys(reasons))


def _dotenv_test_database_url(path: Path, variable_names: Sequence[str]) -> str | None:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise SandboxError("test database configuration is unreadable") from exc
    selected: str | None = None
    for line in lines:
        alternatives = "|".join(re.escape(name) for name in variable_names)
        match = re.match(rf"\s*(?:export\s+)?(?:{alternatives})\s*=\s*(.*)\s*$", line)
        if match is None:
            continue
        value = match.group(1).strip()
        if value[:1] in {"'", '"'}:
            closing_quote = value.find(value[0], 1)
            if closing_quote >= 1:
                value = value[1:closing_quote]
        else:
            value = re.split(r"\s+#", value, maxsplit=1)[0].rstrip()
        selected = value or None
    return selected


def _acceptance_environment(
    source: Mapping[str, str], *, authority: AcceptanceAuthority, db_bound: bool
) -> dict[str, str]:
    environment = sandbox_environment(source)
    account_name = pwd.getpwuid(os.getuid()).pw_name
    environment.update(USER=account_name, LOGNAME=account_name)
    for name in (
        LEASE_BOUNDARY_ENV,
        LEASE_FD_ENV,
        LEASE_NONCE_ENV,
        LEASE_OWNER_PID_ENV,
    ):
        environment.pop(name, None)
    if not db_bound:
        return environment
    test_database_url = authority.test_database_url
    if not test_database_url:
        raise SandboxError("test database configuration is unreadable")
    for name in _toolchain().get("db_url_vars", ("TEST_DATABASE_URL",)):
        environment[str(name)] = test_database_url
    environment[_env_name("DISABLE_DOTENV")] = "1"
    return environment


_ACCEPTANCE_SECRET_NAME = (
    r"api[_-]?key|access[_-]?key(?:[_-]?id)?|private[_-]?key|"
    r"credential|password|pwd|secret|token|key|"
    r"database[_-]?url|test[_-]?database[_-]?url"
)
_ACCEPTANCE_SECRET_VALUE = r"(?:'[^']*'|\"[^\"]*\"|\S+)"
_ACCEPTANCE_SECRET_ASSIGNMENT = re.compile(
    rf"(?i)(?<![a-z0-9])([a-z0-9_-]*(?:{_ACCEPTANCE_SECRET_NAME}))"
    rf"([\"']?\s*[:=]\s*)"
    rf"({_ACCEPTANCE_SECRET_VALUE})"
)
_ACCEPTANCE_SECRET_CLI = re.compile(
    rf"(?i)(?<![\w-])(--[a-z0-9_-]*"
    rf"(?:{_ACCEPTANCE_SECRET_NAME})(?:\s*=\s*|\s+))"
    rf"({_ACCEPTANCE_SECRET_VALUE})"
)
_ACCEPTANCE_BEARER = re.compile(r"(?i)(authorization\s*[:=]\s*bearer\s+)\S+")
_ACCEPTANCE_URL_CREDENTIAL = re.compile(
    r"([A-Za-z][A-Za-z0-9+.-]*://)[^/@:\s]+(?::[^/@\s]*)?@"
)


def redact_acceptance_output(value: str) -> str:
    """Keep bounded diagnostics while removing common secret-bearing forms."""
    redacted = _ACCEPTANCE_SECRET_ASSIGNMENT.sub(r"\1\2<redacted>", value)
    redacted = _ACCEPTANCE_BEARER.sub(r"\1<redacted>", redacted)
    redacted = _ACCEPTANCE_SECRET_CLI.sub(r"\1<redacted>", redacted)
    return _ACCEPTANCE_URL_CREDENTIAL.sub(r"\1<redacted>@", redacted)


def _same_directory_identity(source: Path, destination: Path) -> bool:
    """Prove that an active namespace already presents one trusted directory."""
    try:
        return source.is_dir() and destination.is_dir() and source.samefile(destination)
    except OSError:
        return False


def _acceptance_network_grant(command: str, *, explicit_db: bool) -> bool:
    """Containment fails closed: only a caller's explicit db_commands entry or
    strictly recognized DB grammar opens the acceptance sandbox network;
    everything else stays isolated even when serialized as DB-bound."""
    return explicit_db or _strictly_requires_database(command)


def _run_one_acceptance_command(
    command: str,
    *,
    worktree: Path,
    timeout_s: int,
    db_bound: bool = False,
    db_network_grant: bool | None = None,
    authority: AcceptanceAuthority | None = None,
    source_write_files: tuple[str, ...] | None = None,
) -> dict[str, object]:
    persisted_command = redact_acceptance_output(command)
    if persisted_command != command:
        return {
            "command": persisted_command,
            "exit_code": 125,
            "tail": "acceptance command rejected: literal secret-bearing form",
            "failure_class": "acceptance-policy",
        }
    # Assigned before the sandbox try block so the failure-classification
    # expression below can never hit UnboundLocalError when authority
    # resolution itself raises SandboxError.
    effective_authority = authority
    try:
        # Captured once, DB-capable, before the candidate command executes:
        # the URL is always present so the probe can genuinely confirm, and
        # host configuration is never re-read after candidate code ran.
        effective_authority = authority or _acceptance_authority_for(
            worktree, db_bound=True
        )
        primary = effective_authority.primary
        git_metadata = primary / ".git"
        artifacts = tuple(Path(str(path)) for path in _toolchain().get("shared_artifacts", ()))
        toolchain_identities = [
            (primary / path, worktree / path)
            for path in artifacts
            if (primary / path).exists() or (worktree / path).exists()
        ]
        if any(not source.is_dir() for source, _ in toolchain_identities):
            raise SandboxError("trusted primary acceptance toolchain is unavailable")
        toolchain_mounts = [
            (source, destination)
            for source, destination in toolchain_identities
            if not (
                destination.is_symlink()
                and _same_directory_identity(source, destination)
            )
        ]
        read_only_tool_roots = tuple(
            source.resolve() for source, _destination in toolchain_identities
        )
        if db_network_grant is None:
            db_network_grant = db_bound and _acceptance_network_grant(
                command, explicit_db=False
            )
        deny_network = not db_network_grant
        acceptance_argv = [str(system_executable("bash")), "-c", command]
        needs_corepack = bool(
            re.search(r"(?<![A-Za-z0-9_.-])corepack\s+pnpm\b", command)
        )
        if source_write_files is None and current_boundary_reusable(deny_network=deny_network):
            if not all(
                _same_directory_identity(source, destination)
                for source, destination in toolchain_identities
            ):
                raise SandboxError(
                    "active Guardian sandbox lacks trusted toolchain mounts"
                )
            if needs_corepack and (
                shutil.which("corepack") is None
                or shutil.which("node") is None
                or (
                    not os.environ.get("COREPACK_HOME")
                    and not Path("/run/guardian-corepack-home").is_dir()
                )
            ):
                raise SandboxError("active Guardian sandbox lacks Corepack runtime")
            wrapped = acceptance_argv
        else:
            wrapped = sandbox_command(
                acceptance_argv,
                worktree=worktree,
                writable_worktree=source_write_files is None,
                audit_source=None,
                audit_destination=None,
                git_source=git_metadata if git_metadata.is_dir() else None,
                git_destination=git_metadata if git_metadata.is_dir() else None,
                writable_git=False,
                read_only_roots=(*read_only_tool_roots, Path("/etc/alternatives")),
                read_only_mounts=tuple(toolchain_mounts),
                preserve_fds=(),
                deny_network=deny_network,
                include_model_runtime=False,
                include_corepack_runtime=needs_corepack,
            )
        if source_write_files is not None:
            split = len(wrapped) - len(acceptance_argv) - 1
            if wrapped[split] != "--":
                raise SandboxError("acceptance sandbox command boundary is invalid")
            wrapped[split:split] = exact_repair_file_bindings(worktree, source_write_files)
        child_environment = _acceptance_environment(
            os.environ, authority=effective_authority, db_bound=db_bound
        )
        child_environment["COREPACK_ENABLE_PROJECT_SPEC"] = "1"
        completed = subprocess.run(
            wrapped,
            cwd=worktree,
            env=child_environment,
            capture_output=True,
            text=True,
            timeout=timeout_s,
            check=False,
            close_fds=True,
        )
        exit_code = completed.returncode
        output = completed.stdout + completed.stderr
        tail = [
            redact_acceptance_output(line) for line in output.strip().splitlines()[-12:]
        ]
        sandbox_setup_failed = (
            source_write_files is not None
            and exit_code == 1
            and completed.stderr.startswith("bwrap: No permissions to create a new namespace")
        )
    except (SandboxError, TrustedExecutableError) as exc:
        exit_code = 125
        output = str(exc)
        tail = [redact_acceptance_output(f"acceptance sandbox unavailable: {exc}")]
        sandbox_setup_failed = True
    except subprocess.TimeoutExpired:
        exit_code = 124
        output = ""
        tail = ["acceptance command timed out"]
        sandbox_setup_failed = False
    result: dict[str, object] = {
        "command": persisted_command,
        "exit_code": exit_code,
        "tail": " | ".join(tail),
    }
    if exit_code != 0 and (
        sandbox_setup_failed
        or acceptance_environment_failure(exit_code=exit_code, output=output)
        or (
            _database_unreachable_output(output.lower())
            and effective_authority is not None
            and test_database_unreachable(effective_authority) is not None
        )
    ):
        result["failure_class"] = "acceptance-environment"
    return result
