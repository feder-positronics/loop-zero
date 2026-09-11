#!/usr/bin/env python3
"""Run an approved-base workflow hook as a contained validation child."""

from __future__ import annotations

import argparse
import shlex
import subprocess
import sys
from pathlib import Path

from loopzero.config import ConfigError, PRIVILEGED_HOOKS, hooks_from_base

from .sandbox import SandboxError, run_validation_child
from .settings import settings
from .trusted_exec import TrustedExecutableError, system_executable


class ValidationHookError(RuntimeError):
    """The requested hook is absent, ambiguous, or not safely executable."""


def _command_argv(command: str, extra: list[str]) -> list[str]:
    try:
        argv = shlex.split(command, posix=True)
    except ValueError as exc:
        raise ValidationHookError(f"approved hook command is invalid: {exc}") from exc
    if not argv:
        raise ValidationHookError("approved hook command is empty")
    executable = Path(argv[0])
    if executable.name != argv[0]:
        raise ValidationHookError(
            "approved hook executable must be a protected system basename"
        )
    try:
        argv[0] = str(system_executable(argv[0]))
    except TrustedExecutableError as exc:
        raise ValidationHookError(str(exc)) from exc
    return [*argv, *extra]


def run_hook(*, worktree: Path, hook: str, extra: list[str]) -> int:
    root = worktree.resolve()
    if hook not in PRIVILEGED_HOOKS:
        raise ValidationHookError(
            f"{hook!r} is not a privileged validation hook"
        )
    base_ref = str(settings.toolchain.get("approved_base", "origin/main"))
    commands = hooks_from_base(root, base_ref).get(hook)
    if not commands:
        raise ValidationHookError(
            f"approved {hook!r} hook is unavailable at {base_ref}"
        )
    if extra and len(commands) != 1:
        raise ValidationHookError(
            "hook arguments require exactly one approved command"
        )
    timeout = settings.toolchain.get("validation_timeout_s", 1800)
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or timeout <= 0:
        raise ValidationHookError("validation timeout setting is invalid")
    for command in commands:
        result = run_validation_child(
            _command_argv(command, extra),
            worktree=root,
            timeout=float(timeout),
        )
        if result.stdout:
            sys.stdout.write(result.stdout)
        if result.stderr:
            sys.stderr.write(result.stderr)
        if result.returncode != 0:
            return result.returncode
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--worktree", type=Path, required=True)
    parser.add_argument("--hook", required=True)
    parser.add_argument("extra", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    extra = args.extra[1:] if args.extra[:1] == ["--"] else args.extra
    try:
        return run_hook(worktree=args.worktree, hook=args.hook, extra=extra)
    except subprocess.TimeoutExpired as exc:
        print(f"validation hook timed out after {exc.timeout}s", file=sys.stderr)
        return 124
    except (ConfigError, SandboxError, ValidationHookError) as exc:
        print(f"validation hook unavailable: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
