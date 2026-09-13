"""Publication-side adapters for canonical review-risk evidence."""

from __future__ import annotations

import json
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol

from ..review import risk as delivery_review_risk
from .reentry import (
    publication_generation,
    require_review_after_reentry,
)
from ..kernel.authority_store import (
    load_authority_records,
)
from ..kernel.gitscope import (
    DispatchError,
)
from ..review.authority import (
    accepted_review_terminals,
)
from ..integrations.github import SecureGitRunner
from ._publish_paths import PublicationError
from ..review._security_scope import (
    SecurityReviewScopeError,
    security_trigger_paths_between as shared_security_trigger_paths_between,
)


class CommandRunner(Protocol):
    def run(
        self,
        args: list[str],
        *,
        check: bool = True,
        env: dict[str, str] | None = None,
        input_text: str | None = None,
    ) -> object: ...


_PUBLISHED_AUTHORITY_FIELDS = (
    "admission_tier_floor",
    "base",
    "expected_head",
    "head",
    "patch_identity_carry",
    "pr",
    "review_exemption",
    "review_risk_json",
    "review_risk_sha256",
    "review_risk_tier",
    "review_task_id",
    "run_id",
)


def write_evidence(evidence_dir: Path, record: dict[str, object]) -> Path:
    evidence_dir.mkdir(parents=True, exist_ok=True)
    path = evidence_dir / f"{datetime.now(UTC).date().isoformat()}.jsonl"
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, sort_keys=True) + "\n")
    return path


def bind_review_reentry(
    records: Sequence[dict[str, object]],
    *,
    run_id: str,
    review_task_ids: Sequence[str],
) -> int:
    """Return the run's reentry generation once every cited review postdates it.

    A reentry invalidates the prior receipt; the reminted one must cite review
    evidence accepted after that decision, never the pre-reentry review at an
    unchanged head.
    """
    generation = publication_generation(records, run_id)
    if generation:
        if not review_task_ids:
            raise PublicationError(
                "review exemption is unavailable after a review reentry; cite a "
                "review accepted after the reentry decision"
            )
        try:
            require_review_after_reentry(
                records, run_id=run_id, review_task_ids=review_task_ids
            )
        except ValueError as exc:
            raise PublicationError(str(exc)) from exc
    return generation


def write_published_evidence_once(
    evidence_dir: Path, record: dict[str, object], *, generation: int
) -> Path:
    """Append one publication authority row, or reuse its exact prior authority.

    ``generation`` is the run's authenticated review-reentry count, computed by
    the caller from frozen authority records; a reentry invalidates every
    earlier receipt even at an identical head.
    """
    if generation:
        record = {**record, "review_reentry_generation": generation}
    matches: list[tuple[Path, dict[str, object]]] = []
    for path in sorted(evidence_dir.glob("*.jsonl")) if evidence_dir.is_dir() else ():
        for line_number, line in enumerate(
            path.read_text(encoding="utf-8").splitlines(), start=1
        ):
            if not line.strip():
                continue
            try:
                candidate = json.loads(line)
            except json.JSONDecodeError as exc:
                raise PublicationError(
                    f"{path}:{line_number}: invalid publication evidence"
                ) from exc
            if not isinstance(candidate, dict):
                raise PublicationError(
                    f"{path}:{line_number}: publication evidence is not an object"
                )
            if (
                candidate.get("review_reentry_generation", 0) == generation
                and candidate.get("status") == "published"
                and candidate.get("pr") == record.get("pr")
                and candidate.get("run_id") == record.get("run_id")
                and candidate.get("expected_head") == record.get("expected_head")
            ):
                matches.append((path, candidate))
    if len(matches) > 1:
        raise PublicationError(
            "published review-risk evidence is duplicated for the PR/run/exact head"
        )
    if matches:
        path, existing = matches[0]
        existing_authority = {
            field: existing.get(field) for field in _PUBLISHED_AUTHORITY_FIELDS
        }
        requested_authority = {
            field: record.get(field) for field in _PUBLISHED_AUTHORITY_FIELDS
        }
        if existing_authority != requested_authority:
            raise PublicationError(
                "published review-risk evidence conflicts for the PR/run/exact head"
            )
        return path
    return write_evidence(evidence_dir, record)


def all_dispatch_records(repo: Path) -> list[dict[str, object]]:
    """Load publication authority while preserving its error contract."""
    try:
        return load_authority_records(repo, 30)
    except DispatchError as exc:
        raise PublicationError(f"invalid dispatch authority: {exc}") from exc


def load_review_risk_envelope(
    dispatch_records: Sequence[dict[str, object]], task_id: str
) -> tuple[str, str] | None:
    """Load candidate-bound risk bytes from one accepted review contract."""
    terminal = accepted_review_terminals(dispatch_records).get(task_id)
    if terminal is None:
        raise PublicationError(f"final review task {task_id!r} is not accepted")
    contract = terminal.get("task_contract") if isinstance(terminal, dict) else None
    risk_json = (
        contract.get("delivery_review_risk_json")
        if isinstance(contract, dict)
        else None
    )
    risk_digest = (
        contract.get("delivery_review_risk_sha256")
        if isinstance(contract, dict)
        else None
    )
    if risk_json is None and risk_digest is None:
        # Authenticated reviews admitted before the risk-artifact cutover have
        # no envelope. The publisher derives one from the exact candidate and
        # retains the existing T1 admission floor for these tasks.
        return None
    if not isinstance(risk_json, str) or not isinstance(risk_digest, str):
        raise PublicationError(
            f"final review task {task_id!r} has no candidate-bound review-risk artifact"
        )
    return risk_json, risk_digest


def review_risk_envelope_for_publication(
    dispatch_records: Sequence[dict[str, object]],
    task_id: str,
    worktree: Path,
    base_ref: str,
    head_ref: str,
    runner: CommandRunner,
    *,
    patch_identity_covers_head: bool = False,
) -> tuple[str, str]:
    """Load exact review risk or safely rederive it for legacy/carried reviews."""
    envelope = load_review_risk_envelope(dispatch_records, task_id)
    if envelope is not None and not patch_identity_covers_head:
        try:
            accepted_artifact = delivery_review_risk.parse_review_risk(envelope[0])
        except delivery_review_risk.ReviewRiskError as exc:
            raise PublicationError(str(exc)) from exc
        if (
            accepted_artifact.get("base_sha") == base_ref
            and accepted_artifact.get("head_sha") == head_ref
        ):
            return envelope
    derived = delivery_review_risk.compute_review_risk(
        worktree, base_ref, head_ref, runner=runner
    )
    if envelope is not None:
        try:
            accepted = delivery_review_risk.verify_review_risk(
                worktree, envelope[0], envelope[1], runner=runner
            )
            current = delivery_review_risk.verify_review_risk(
                worktree,
                derived["review_risk_json"],
                derived["review_risk_sha256"],
                runner=runner,
            )
        except delivery_review_risk.ReviewRiskError as exc:
            raise PublicationError(str(exc)) from exc
        accepted_tier = str(accepted["effective_tier"])
        current_tier = str(current["effective_tier"])
        if (
            delivery_review_risk.strictest_tier(accepted_tier, current_tier)
            != current_tier
        ):
            raise PublicationError("patch-carried review-risk tier became weaker")
        accepted_security = set(accepted["effective_security_trigger_paths"])
        current_security = set(current["effective_security_trigger_paths"])
        if not accepted_security.issubset(current_security):
            raise PublicationError(
                "patch-carried review-risk security scope became weaker"
            )
    return derived["review_risk_json"], derived["review_risk_sha256"]


def load_review_risk_file(path: Path) -> tuple[str, str]:
    """Load canonical risk bytes from a deterministic T0 preflight result."""
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise PublicationError(f"cannot read review-risk input: {exc}") from exc
    risk_json = record.get("review_risk_json") if isinstance(record, dict) else None
    risk_digest = record.get("review_risk_sha256") if isinstance(record, dict) else None
    if not isinstance(risk_json, str) or not isinstance(risk_digest, str):
        raise PublicationError(
            "review-risk input requires canonical review_risk_json and digest"
        )
    return risk_json, risk_digest


def _security_trigger_paths_between(
    worktree: Path,
    base_ref: str,
    head_ref: str,
    *,
    runner: CommandRunner | None = None,
) -> tuple[str, ...]:
    """Preserve publication's error contract around the shared policy."""
    try:
        return shared_security_trigger_paths_between(
            worktree,
            base_ref,
            head_ref,
            runner=runner or SecureGitRunner(worktree),
        )
    except SecurityReviewScopeError as exc:
        raise PublicationError(str(exc)) from exc


def security_trigger_paths(
    worktree: Path,
    merge_base: str,
    *,
    runner: CommandRunner | None = None,
) -> tuple[str, ...]:
    """Evaluate canonical path/content triggers on the frozen diff."""
    return _security_trigger_paths_between(
        worktree,
        merge_base,
        "HEAD",
        runner=runner,
    )
