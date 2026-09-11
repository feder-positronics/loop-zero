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
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

from loopzero.config import ConfigError, PRIVILEGED_HOOKS, effective_hooks, load_profile, resolve_base
from loopzero.trust import allowed_path, git_environment, resolve_executable

from .authority import (
    PROOF_FIELD,
    TerminalAuthorityError,
    TerminalAuthorityOperationalError,
    verify_terminal_authority,
)
from .sandbox import SandboxError, run_validation_child
from .settings import settings

RESULT_SCHEMA = "loopzero-validation-result-v2"
MAX_RESULT_BYTES = 1024 * 1024
MAX_PUBLIC_KEY_BYTES = 2048
RESULT_READ_TIMEOUT_S = 2.0


class ValidationHookError(RuntimeError):
    """The requested hook is absent, ambiguous, or not safely executable."""


class UnsignedResultError(ValidationHookError):
    """The hook did not produce a coordinator-signed terminal result."""


class TamperedResultError(ValidationHookError):
    """The artifact has an invalid coordinator signature."""


class UnboundResultError(ValidationHookError):
    """The signed artifact belongs to a different invocation."""


@dataclass(frozen=True)
class SourceIdentity:
    """The exact commit or candidate tree presented to validation."""

    kind: str
    sha: str


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


def _read_regular(
    path: Path,
    *,
    maximum: int,
    label: str,
    error_type: type[ValidationHookError] = ValidationHookError,
    timeout: float = RESULT_READ_TIMEOUT_S,
) -> bytes:
    flags = (
        os.O_RDONLY
        | os.O_NONBLOCK
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    deadline = time.monotonic() + timeout
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise error_type(f"{label} is unavailable") from exc
    try:
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_size <= 0
            or metadata.st_size > maximum
        ):
            raise error_type(f"{label} must be a bounded regular file")
        payload = bytearray()
        while len(payload) < metadata.st_size:
            if time.monotonic() >= deadline:
                raise error_type(f"{label} read timed out")
            try:
                chunk = os.read(
                    descriptor,
                    min(64 * 1024, metadata.st_size - len(payload)),
                )
            except BlockingIOError:
                time.sleep(min(0.01, max(0.0, deadline - time.monotonic())))
                continue
            if not chunk:
                break
            payload.extend(chunk)
        current = os.fstat(descriptor)
        identity = ("st_dev", "st_ino", "st_size", "st_mtime_ns", "st_ctime_ns")
        if len(payload) != metadata.st_size or any(
            getattr(current, field) != getattr(metadata, field) for field in identity
        ):
            raise error_type(f"{label} changed while being read")
        return bytes(payload)
    finally:
        os.close(descriptor)


def _git(
    root: Path, *arguments: str, index: Path | None = None
) -> subprocess.CompletedProcess[str]:
    environment = git_environment({"HOME": "/nonexistent", "PATH": "/usr/bin:/bin"})
    if index is not None:
        environment["GIT_INDEX_FILE"] = str(index)
    return subprocess.run(
        ["/usr/bin/git", "-C", str(root), *arguments],
        capture_output=True,
        text=True,
        check=False,
        env=environment,
    )


def _git_output(
    root: Path, *arguments: str, index: Path | None = None, label: str
) -> str:
    completed = _git(root, *arguments, index=index)
    if completed.returncode != 0:
        raise ValidationHookError(f"validation worktree {label} is unavailable")
    return completed.stdout.strip()


def _dirty_tree_sha(root: Path) -> str:
    """Materialize the worktree in an isolated index without changing its index."""
    with tempfile.TemporaryDirectory(prefix="loopzero-validation-index-") as raw:
        index = Path(raw) / "index"
        _git_output(root, "read-tree", "HEAD", index=index, label="tree")
        _git_output(root, "add", "-A", "--", ".", index=index, label="tree")
        tree = _git_output(root, "write-tree", index=index, label="tree")
    if len(tree) != 40 or any(character not in "0123456789abcdef" for character in tree):
        raise ValidationHookError("validation worktree tree identity is invalid")
    return tree


def source_identity(
    root: Path, *, approved_head: str, allow_dirty_tree: bool
) -> SourceIdentity:
    """Prove HEAD and bind either its clean commit or an intentional dirty tree."""
    head = _git_output(root, "rev-parse", "HEAD", label="HEAD")
    commit = _git_output(
        root, "rev-parse", "--verify", "HEAD^{commit}", label="HEAD commit"
    )
    if head != approved_head or commit != approved_head:
        raise ValidationHookError(
            "validation worktree HEAD does not match the approved head"
        )

    status = _git(root, "status", "--porcelain=v1", "--untracked-files=all")
    difference = _git(root, "diff", "--quiet", "HEAD", "--")
    if status.returncode != 0 or difference.returncode not in {0, 1}:
        raise ValidationHookError("validation worktree cleanliness is unavailable")
    clean = not status.stdout and difference.returncode == 0
    if clean:
        return SourceIdentity("commit", approved_head)
    if not allow_dirty_tree:
        raise ValidationHookError(
            "validation worktree is dirty; --allow-dirty-tree is required"
        )
    tree = _dirty_tree_sha(root)
    head_tree = _git_output(root, "rev-parse", "HEAD^{tree}", label="HEAD tree")
    if tree == head_tree:
        raise ValidationHookError(
            "validation worktree dirt cannot be represented by a Git tree"
        )
    return SourceIdentity("tree", tree)


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
    source: SourceIdentity,
) -> dict[str, object]:
    try:
        artifact = json.loads(
            _read_regular(
                path,
                maximum=MAX_RESULT_BYTES,
                label="validation result artifact",
                error_type=UnsignedResultError,
            )
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
    except TerminalAuthorityOperationalError as exc:
        raise ValidationHookError(
            f"validation result verifier is unavailable: {exc}"
        ) from exc
    except TerminalAuthorityError as exc:
        raise TamperedResultError(f"validation result signature is invalid: {exc}") from exc
    expected = {
        "schema_version": RESULT_SCHEMA,
        "task_id": task_id,
        "base_sha": base_sha,
        "head_sha": head_sha,
        "source_kind": source.kind,
        "source_sha": source.sha,
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
    allow_dirty_tree: bool = False,
) -> int:
    root = worktree.resolve()
    if hook not in PRIVILEGED_HOOKS:
        raise ValidationHookError(f"{hook!r} is not a privileged validation hook")
    approved_base = resolve_base(root, base=base_sha)
    approved_head = resolve_base(root, base=head_sha)
    source = source_identity(
        root, approved_head=approved_head, allow_dirty_tree=allow_dirty_tree
    )
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
    try:
        after = source_identity(
            root, approved_head=approved_head, allow_dirty_tree=allow_dirty_tree
        )
    except ValidationHookError as exc:
        raise UnboundResultError(
            "validation worktree source changed during hook execution"
        ) from exc
    if after != source:
        raise UnboundResultError(
            "validation worktree source changed during hook execution"
        )
    verify_result_artifact(
        result_artifact,
        coordinator_public_key=coordinator_public_key,
        task_id=task_id,
        base_sha=approved_base,
        head_sha=approved_head,
        hook=hook,
        hook_commands=actual_commands,
        source=source,
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
    parser.add_argument("--allow-dirty-tree", action="store_true")
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
            allow_dirty_tree=args.allow_dirty_tree,
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
