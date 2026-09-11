#!/usr/bin/env python3
"""Publish-time PR body validation against the trusted base revision (#4048).

Trusted-base PR Lint checks out ``scripts/util/pr_body_check.py`` from the
PR's base SHA, so a contract change that lands on the base while a branch is
in flight is enforced by CI but invisible to the worktree's own copy of the
checker. Running the base revision's checker before publication fails once,
locally, with the failing rule, instead of after a merge-gate round trip. A
base whose checker cannot be retrieved fails closed: a stale fetch or a missing
object is indistinguishable from legitimate absence, and every publication base
in this repository carries the checker.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
import tempfile
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

from ._body_check import has_standalone_reason, validate_contract
from ._visual_evidence import evaluate as evaluate_visual_evidence
from ..kernel.run_identity import extract_run_id_marker

if TYPE_CHECKING:
    from pr_publish import PublicationRequest

TRUSTED_BODY_CHECKER_PATH = "scripts/util/pr_body_check.py"
EVIDENCE_START = "<!-- loop-zero-evidence:start -->"
EVIDENCE_END = "<!-- loop-zero-evidence:end -->"


def _loopzero_evidence(
    *, artifact: Mapping[str, object], review_task_id: str | None, run_id: str
) -> str:
    """Render source binding from verified publication inputs, never a model claim."""
    payload = {
        "contract": "loop-zero-v1",
        "run_id": run_id,
        "base_sha": artifact["base_sha"],
        "head_sha": artifact["head_sha"],
        "head_tree_sha": artifact["head_tree_sha"],
        "review_task_id": review_task_id or None,
    }
    return (
        EVIDENCE_START
        + "\n```json\n"
        + json.dumps(payload, indent=2, sort_keys=True)
        + "\n```\n"
        + EVIDENCE_END
    )


def bind_loopzero_evidence(
    body: str,
    *,
    artifact: Mapping[str, object],
    review_task_id: str | None,
    run_id: str,
) -> str:
    """Maintain one current binding in the PR's existing Validation section."""
    block = _loopzero_evidence(
        artifact=artifact, review_task_id=review_task_id, run_id=run_id
    )
    starts, ends = body.count(EVIDENCE_START), body.count(EVIDENCE_END)
    if starts != ends or starts > 1:
        raise ValueError("loop-zero evidence block is malformed or duplicated")
    if starts:
        first, last = body.index(EVIDENCE_START), body.index(EVIDENCE_END)
        if last < first:
            raise ValueError("loop-zero evidence markers are out of order")
        binding = re.match(
            r"\n```json\n.*?\n```\n",
            body[first + len(EVIDENCE_START) : last],
            re.DOTALL,
        )
        if binding is None:
            raise ValueError("loop-zero evidence is missing its source binding")
        details = body[first + len(EVIDENCE_START) + binding.end() : last]
        block = block[: -len(EVIDENCE_END)] + details + EVIDENCE_END
        return body[:first] + block + body[last + len(EVIDENCE_END) :]
    validation = re.search(r"^## Validation[ \t]*$", body, re.MULTILINE)
    if validation is None:
        raise ValueError("loop-zero evidence requires the Validation section")
    remaining = body[validation.end() :]
    next_section = re.search(r"^## ", remaining, re.MULTILINE)
    end = validation.end() + next_section.start() if next_section else len(body)
    details = body[validation.end() : end].strip()
    block = (
        block[: -len(EVIDENCE_END)]
        + ("\n" + details + "\n\n" if details else "")
        + EVIDENCE_END
    )
    return body[: validation.end()] + "\n\n" + block + "\n\n" + body[end:]


def validate_loopzero_evidence(
    body: str,
    *,
    artifact: Mapping[str, object],
    review_task_id: str | None,
    run_id: str,
) -> None:
    """Reject PR edits that replace the admitted candidate's evidence."""
    if body.count(EVIDENCE_START) != 1 or body.count(EVIDENCE_END) != 1:
        raise ValueError("loop-zero requires exactly one PR evidence block")
    if (
        bind_loopzero_evidence(
            body, artifact=artifact, review_task_id=review_task_id, run_id=run_id
        )
        != body
    ):
        raise ValueError("loop-zero PR evidence does not match the admitted source")


class _CompletedLike(Protocol):
    returncode: int
    stdout: str
    stderr: str


class _GitRunner(Protocol):
    def run(self, args: list[str], *, check: bool = ...) -> _CompletedLike: ...


CheckerRunner = Callable[[Sequence[str], str], _CompletedLike]


def _run_trusted_body_checker(
    argv: Sequence[str], checker_source: str
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        list(argv),
        input=checker_source,
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )


def validate_body_against_trusted_base(
    runner: _GitRunner,
    *,
    trusted_base_head: str,
    body: str,
    standalone: bool,
    error_type: type[Exception] = RuntimeError,
    run_checker: CheckerRunner | None = None,
) -> None:
    """Validate ``body`` with the checker PR Lint will actually run.

    The checker source is read from ``trusted_base_head`` and executed from
    stdin under ``python -I -`` exactly as the sparse-checkout PR Lint job
    would run it from a file. A checker that cannot be retrieved from the base
    revision fails closed, and a contract failure raises ``error_type`` naming
    the base revision and the failing rule.
    """
    shown = runner.run(
        ["git", "show", f"{trusted_base_head}:{TRUSTED_BODY_CHECKER_PATH}"],
        check=False,
    )
    if shown.returncode != 0:
        detail = (shown.stderr or shown.stdout or "").strip().splitlines()
        raise error_type(
            "trusted-base PR body checker unavailable at "
            f"{trusted_base_head[:12]} ({TRUSTED_BODY_CHECKER_PATH}): "
            f"{detail[-1].strip() if detail else 'git show failed'}; fetch the "
            "pinned base before publication"
        )
    handle = tempfile.NamedTemporaryFile(
        "w", suffix=".md", delete=False, encoding="utf-8"
    )
    try:
        with handle:
            handle.write(body)
        argv = [
            sys.executable,
            "-I",
            "-",
            "--body-file",
            handle.name,
            "--ready",
            *(["--standalone"] if standalone else []),
        ]
        checker = run_checker or _run_trusted_body_checker
        completed = checker(argv, shown.stdout)
    finally:
        Path(handle.name).unlink(missing_ok=True)
    if completed.returncode != 0:
        lines = (completed.stderr or completed.stdout or "").strip().splitlines()
        rule = lines[-1].strip() if lines else "unknown rule"
        raise error_type(
            "PR body violates the trusted-base contract at "
            f"{trusted_base_head[:12]} ({TRUSTED_BODY_CHECKER_PATH}): {rule}"
        )


def validate_adopted_body(
    request: PublicationRequest,
    *,
    number: int,
    body: object,
    error_type: type[Exception] = RuntimeError,
) -> None:
    if "standalone" in request.labels and (
        not isinstance(body, str) or not has_standalone_reason(body)
    ):
        raise error_type(
            f"adopted PR #{number} cannot receive the standalone label: its "
            "live body lacks a non-empty Standalone-Reason"
        )
    if not isinstance(body, str):
        raise error_type(f"adopted PR #{number} has no live body")
    if error := validate_contract(body):
        raise error_type(
            f"adopted PR #{number} live body violates publication policy: {error}"
        )
    live_visual_evidence = evaluate_visual_evidence(list(request.changed_paths), body)
    if not live_visual_evidence.ok:
        print(
            f"warning: adopted PR #{number} visual-evidence advisory: "
            + live_visual_evidence.reason,
            file=sys.stderr,
        )
    expected_run_id = extract_run_id_marker(request.body)
    if body != request.body or extract_run_id_marker(body) != expected_run_id:
        raise error_type(
            f"PR #{number} skill-run marker/body differs from requested body"
        )
