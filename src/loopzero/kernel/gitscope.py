#!/usr/bin/env python3
"""Shared primitives for the dispatch module family (#3944 decomposition).

Home for the dispatch error type, trusted git invocation, primary-repo
resolution, task-contract hashing, and small shared data types that
`agent_dispatch` and its extracted sibling modules (`dispatch_acceptance`, ...)
need without importing the dispatcher. Nothing here may import
`agent_dispatch`.
"""

import hashlib
import json
import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

# Sibling imports must survive PYTHONSAFEPATH=1 (job.sh) and python -I.
sys.path.insert(0, str(Path(__file__).resolve().parent))

from guardian_sandbox import (  # noqa: E402
    environment as sandbox_environment,
)
from trusted_executable import (  # noqa: E402
    TrustedExecutableError,
    system_executable,
)

DISPATCH_DIR = Path(".audit/dispatch")
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
