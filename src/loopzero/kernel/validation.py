#!/usr/bin/env python3
"""Run a base-governed hook and authenticate its terminal result host-side."""

from __future__ import annotations

import argparse
import json
import os
import shlex
import stat
import subprocess
import sys
from pathlib import Path

from loopzero.config import ConfigError, PRIVILEGED_HOOKS, effective_hooks, load_profile, resolve_base
from loopzero.trust import allowed_path, resolve_executable

from .authority import PROOF_FIELD, TerminalAuthorityError, verify_terminal_authority
from .sandbox import SandboxError, run_validation_child
from .settings import settings

RESULT_SCHEMA = "loopzero-validation-result-v1"
MAX_RESULT_BYTES = 1024 * 1024
MAX_PUBLIC_KEY_BYTES = 2048


class ValidationHookError(RuntimeError):
    """The requested hook is absent, ambiguous, or not safely executable."""


class UnsignedResultError(ValidationHookError):
    """The hook did not produce a coordinator-signed terminal result."""


class TamperedResultError(ValidationHookError):
    """The artifact has an invalid coordinator signature."""


class UnboundResultError(ValidationHookError):
    """The signed artifact belongs to a different invocation."""


def _command_argv(root: Path, command: str, extra: list[str]) -> list[str]:
    try:
        argv = shlex.split(command, posix=True)
    except ValueError as exc:
        raise ValidationHookError(f"approved hook command is invalid: {exc}") from exc
    if not argv:
        raise ValidationHookError("approved hook command is empty")
    executable = resolve_executable(root, argv[0], allowed_path(()))
    if executable is None:
        raise ValidationHookError(
            f"approved hook executable {argv[0]!r} is outside the shared allowlist"
        )
    # Only argv[0] is host-selected. Remaining operands intentionally name
    # candidate inputs and tests; their output is authenticated separately.
    return [str(executable), *argv[1:], *extra]


def _read_regular(path: Path, *, maximum: int, label: str) -> bytes:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise ValidationHookError(f"{label} is unavailable") from exc
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_size <= 0 or metadata.st_size > maximum:
            raise ValidationHookError(f"{label} must be a bounded regular file")
        payload = os.read(descriptor, maximum + 1)
        if len(payload) != metadata.st_size:
            raise ValidationHookError(f"{label} changed while being read")
        return payload
    finally:
        os.close(descriptor)


def read_coordinator_public_key(path: Path) -> bytes:
    """Snapshot caller-selected authority before candidate code executes."""
    return _read_regular(path, maximum=MAX_PUBLIC_KEY_BYTES, label="coordinator public key")


def verify_result_artifact(
    path: Path,
    *,
    coordinator_public_key: bytes,
    task_id: str,
    base_sha: str,
    head_sha: str,
    hook: str,
    hook_commands: list[str],
) -> dict[str, object]:
    try:
        artifact = json.loads(
            _read_regular(path, maximum=MAX_RESULT_BYTES, label="validation result artifact")
        )
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise UnsignedResultError("validation result artifact is not canonical JSON") from exc
    if not isinstance(artifact, dict) or not isinstance(artifact.get(PROOF_FIELD), dict):
        raise UnsignedResultError("validation result artifact is unsigned")
    try:
        verify_terminal_authority(
            artifact,
            registration=None,
            expected_kind="coordinator",
            coordinator_public_key=coordinator_public_key,
        )
    except TerminalAuthorityError as exc:
        raise TamperedResultError(f"validation result signature is invalid: {exc}") from exc
    expected = {
        "schema_version": RESULT_SCHEMA,
        "task_id": task_id,
        "base_sha": base_sha,
        "head_sha": head_sha,
        "hook": hook,
        "hook_commands": hook_commands,
        "status": "completed",
        "exit_code": 0,
    }
    mismatches = [name for name, value in expected.items() if artifact.get(name) != value]
    if set(artifact) != {*expected, PROOF_FIELD}:
        mismatches.append("fields")
    if mismatches:
        raise UnboundResultError(
            "validation result is not bound to this invocation: "
            + ", ".join(dict.fromkeys(mismatches))
        )
    return artifact


def run_hook(
    *,
    worktree: Path,
    hook: str,
    base_sha: str,
    head_sha: str,
    task_id: str,
    result_artifact: Path,
    coordinator_public_key: bytes,
    extra: list[str],
) -> int:
    root = worktree.resolve()
    if hook not in PRIVILEGED_HOOKS:
        raise ValidationHookError(f"{hook!r} is not a privileged validation hook")
    approved_base = resolve_base(root, base=base_sha)
    approved_head = resolve_base(root, base=head_sha)
    profile = load_profile(root)
    commands = effective_hooks(profile, approved_base).get(hook)
    if not commands:
        raise ValidationHookError(f"approved {hook!r} hook is unavailable at {approved_base}")
    if extra and len(commands) != 1:
        raise ValidationHookError("hook arguments require exactly one approved command")
    timeout = settings.toolchain.get("validation_timeout_s", 1800)
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or timeout <= 0:
        raise ValidationHookError("validation timeout setting is invalid")
    actual_commands: list[str] = []
    for command in commands:
        argv = _command_argv(root, command, extra)
        actual_commands.append(shlex.join(argv))
        result = run_validation_child(argv, worktree=root, timeout=float(timeout))
        if result.stdout:
            sys.stdout.write(result.stdout)
        if result.stderr:
            sys.stderr.write(result.stderr)
        if result.returncode != 0:
            return result.returncode
    verify_result_artifact(
        result_artifact,
        coordinator_public_key=coordinator_public_key,
        task_id=task_id,
        base_sha=approved_base,
        head_sha=approved_head,
        hook=hook,
        hook_commands=actual_commands,
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--worktree", type=Path, required=True)
    parser.add_argument("--base", required=True)
    parser.add_argument("--head", required=True)
    parser.add_argument("--task-id", required=True)
    parser.add_argument("--result-artifact", type=Path, required=True)
    parser.add_argument("--coordinator-public-key", type=Path, required=True)
    parser.add_argument("--hook", required=True)
    parser.add_argument("extra", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    extra = args.extra[1:] if args.extra[:1] == ["--"] else args.extra
    try:
        public_key = read_coordinator_public_key(args.coordinator_public_key)
        return run_hook(
            worktree=args.worktree,
            hook=args.hook,
            base_sha=args.base,
            head_sha=args.head,
            task_id=args.task_id,
            result_artifact=args.result_artifact,
            coordinator_public_key=public_key,
            extra=extra,
        )
    except subprocess.TimeoutExpired as exc:
        print(f"validation hook timed out after {exc.timeout}s", file=sys.stderr)
        return 124
    except UnsignedResultError as exc:
        print(f"validation result rejected (unsigned): {exc}", file=sys.stderr)
        return 125
    except TamperedResultError as exc:
        print(f"validation result rejected (signature): {exc}", file=sys.stderr)
        return 126
    except UnboundResultError as exc:
        print(f"validation result rejected (binding): {exc}", file=sys.stderr)
        return 127
    except (ConfigError, SandboxError, ValidationHookError) as exc:
        print(f"validation hook unavailable: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
