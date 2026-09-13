#!/usr/bin/env python3
"""Shared primitives for the dispatch module family (#3944 decomposition).

Home for the dispatch error type, trusted git invocation, primary-repo
resolution, task-contract hashing, and small shared data types that
`agent_dispatch` and its extracted sibling modules (`dispatch_acceptance`, ...)
need without importing the dispatcher. Nothing here may import
`agent_dispatch`.
"""

from .settings import settings

import fnmatch
import hashlib
import json
import os
import subprocess
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path


from .sandbox import (  # noqa: E402
    environment as sandbox_environment,
)
from .trusted_exec import (  # noqa: E402
    TrustedExecutableError,
    system_executable,
)

DISPATCH_DIR = settings.audit_root / "dispatch"
REVIEW_ACCEPTANCE_RECEIPT_SCHEMA_VERSION = "review-acceptance-v2"


class DispatchError(RuntimeError):
    """Raised when a dispatch cannot be prepared or executed safely."""


def trusted_git_command(worktree: Path, *args: str) -> list[str]:
    try:
        git = system_executable("git")
    except TrustedExecutableError as exc:
        raise DispatchError(f"cannot resolve trusted git: {exc}") from exc
    return [str(git), "-C", str(worktree), *args]


def primary_repo_root(path: Path) -> Path:
    """Resolve the PRIMARY repo root for a checkout (worktree-safe).

    Telemetry must anchor here, not at the worktree: `.audit/` inside a
    worktree is deleted with it, which would silently destroy the model
    learning-loop data (same anchoring rule as agent_event.py).
    """
    completed = subprocess.run(
        trusted_git_command(
            path,
            "rev-parse",
            "--path-format=absolute",
            "--git-common-dir",
        ),
        capture_output=True,
        text=True,
        check=False,
        env=sandbox_environment(os.environ),
    )
    if completed.returncode != 0:
        raise DispatchError(f"not a git checkout: {path}")
    return Path(completed.stdout.strip()).parent


def resolved_record_worktree(raw_worktree: object) -> Path | None:
    """Resolve an untrusted ledger worktree without allowing path-shaped DoS."""
    if not isinstance(raw_worktree, str) or not raw_worktree:
        return None
    try:
        return Path(raw_worktree).resolve()
    except (OSError, RuntimeError, ValueError):
        return None


@dataclass(frozen=True)
class ReviewSnapshot:
    """Immutable reviewed-state commit plus its ephemeral checkout."""

    commit_sha: str
    tree_sha: str
    directory: Path
    patch_identity: dict[str, object] | None = None


IMMUTABLE_TASK_EXCLUDED_KEYS = frozenset(
    {
        "task_id",
        "preferred_model",
        "reasoning_level",
        "experiment_name",
        "experiment_arm",
        "experiment_width_bucket",
        "evidence_manifest",
    }
)


def immutable_task_contract(task: dict[str, object]) -> dict[str, object]:
    """Return the complete stable task contract available to the dispatcher.

    Task and reasoning identity intentionally stay outside this value so a
    failed unit can be rerouted to a different alias/effort. Everything that
    defines the requested work, its scope, and its acceptance gate remains
    bound and is persisted with each governed attempt.
    """
    return {
        key: task[key]
        for key in sorted(task)
        if key not in IMMUTABLE_TASK_EXCLUDED_KEYS
    }


def task_contract_hash(task: dict[str, object]) -> str:
    serialized = json.dumps(
        immutable_task_contract(task), sort_keys=True, separators=(",", ":")
    )
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def _deterministic_identifier_sha256(value: object) -> str:
    """Hash untrusted identifiers without failing on JSON-valid lone surrogates."""
    if isinstance(value, str):
        payload = value.encode("utf-8", "surrogatepass")
    else:
        try:
            payload = json.dumps(
                value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
            ).encode("utf-8", "surrogatepass")
        except (TypeError, ValueError):
            payload = type(value).__qualname__.encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _literal_bracket_glob(glob: str) -> str:
    """Keep framework route segments such as ``[slug]`` literal."""
    return "".join(
        "[[]" if char == "[" else "[]]" if char == "]" else char for char in glob
    )



def _path_allowed(path: str, glob: str) -> bool:
    if fnmatch.fnmatchcase(path, _literal_bracket_glob(glob)):
        return True
    # A literal directory entry ("app/routers" or "app/routers/") allows
    # everything under it — callers routinely pass directories, and fnmatch
    # alone flags every in-scope file as a violation (2026-07-10
    # usage-capture-rollout false scope-violation).
    prefix = glob.rstrip("/")
    if prefix and not any(ch in prefix for ch in "*?"):
        return path == prefix or path.startswith(prefix + "/")
    return False



def scope_violations(changed: set[str], allowed_globs: Sequence[str]) -> list[str]:
    return sorted(
        path
        for path in changed
        if not any(_path_allowed(path, glob) for glob in allowed_globs)
    )
