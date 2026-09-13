"""Review-terminal, verdict, supersession, and delta-chain authority projections.

Move-only extraction: this sibling must not import ``agent_dispatch``.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
from collections.abc import Callable, Collection, Mapping, Sequence
from contextvars import ContextVar
from pathlib import Path
from typing import cast


# delivery controller constants are the preserved cutover-A contract
from ..kernel import patch_identity
from ..kernel.worktree_lease import identities_match, source_identity
from .acceptance import review_acceptance_receipt_reasons
from ..kernel.authority_projection import (
    _authenticated_attempt_terminal_ids,
    _authenticated_attempt_terminal_registrations,
    _authenticated_coordinator_record_ids,
    _authenticated_open_before_record_projections,
    _authority_record_list,
    _legacy_compatibility_record_ids,
    _recovery_matches_deposit,
    _registered_authority_before_record_ids,
    _terminal_authority_attempt_key,
    authenticated_supersessions,
    current_telemetry,
    retained_retry_outcomes,
    worktree_status_paths,
)
from ..kernel.gitscope import (
    DispatchError,
    ReviewSnapshot,
    primary_repo_root,
    trusted_git_command,
)
from .evidence import (
    _finding_capture_id,
    _validate_result_findings,
    persisted_review_findings,
)
from .routing import (
    DISPATCH_OUTCOME_TYPES,
    ESCALATION_TARGETS,
    WORK_UNIT_HISTORY_DAYS,
    supersession_reason_matches_terminal,
    verifier_identity_is_independent,
)
from .findings import (
    LedgerConflict as FindingLedgerConflict,
    LedgerReadError,
    build_finding_capture_request,
    canonical_record_digest,
    finding_capture_operation_id,
    load_finding_records,
    replay_finding_capture,
)
from ..kernel.sandbox import environment as sandbox_environment
from .chain import (
    review_chain_receipt_reasons,
    review_record_lens as review_gate_lens,
)

DELTA_CUMULATIVE_LINE_CAP = 300
DELTA_REVIEW_PATCH_MAX_BYTES = 225_000
CONTROL_TYPE = "delivery-control"
_ARCHIVE_VALIDATOR: ContextVar[Callable[..., bool] | None]


def uncarryable_delta_authority_is_valid(
    authority: object,
    *,
    predecessor_task_id: str,
    predecessor_snapshot_sha: str,
    current_source: Mapping[str, object],
) -> bool:
    """Validate recovery shape after the caller authenticates authority."""
    if (
        not isinstance(authority, Mapping)
        or authority.get("schema_version") != "uncarryable-delta-authority-v1"
        or authority.get("predecessor_task_id") != predecessor_task_id
        or authority.get("predecessor_snapshot_sha") != predecessor_snapshot_sha
        or authority.get("superseding_source_identity") != dict(current_source)
    ):
        return False
    reason = authority.get("reason")
    if reason in {"author-patch-ambiguous", "delta-churn-exceeded"}:
        return True
    current_head = current_source.get("head")
    patch_bytes = authority.get("delta_patch_bytes")
    cumulative_churn = authority.get("cumulative_churn")
    return (
        reason == "delta-byte-cap-exceeded"
        and type(patch_bytes) is int
        and patch_bytes > DELTA_REVIEW_PATCH_MAX_BYTES
        and authority.get("delta_patch_max_bytes") == DELTA_REVIEW_PATCH_MAX_BYTES
        and isinstance(authority.get("delta_patch_sha256"), str)
        and re.fullmatch(r"[0-9a-f]{64}", str(authority["delta_patch_sha256"]))
        is not None
        and authority.get("delta_patch_from_sha") == predecessor_snapshot_sha
        and isinstance(current_head, str)
        and re.fullmatch(r"[0-9a-f]{40}", current_head) is not None
        and authority.get("delta_patch_to_sha") == current_head
        and type(cumulative_churn) is int
        and 0 <= cumulative_churn <= DELTA_CUMULATIVE_LINE_CAP
        and authority.get("delta_churn_ceiling") == DELTA_CUMULATIVE_LINE_CAP
    )


_ARCHIVE_VALIDATOR = ContextVar(
    "archive_delta_authority_validator",
    default=None,
)
_DEFAULT_ARCHIVE_VALIDATOR = uncarryable_delta_authority_is_valid


def configure(
    *, uncarryable_delta_authority_is_valid: Callable[..., bool] | None = None
) -> None:
    """Install an optional consumer-narrowed archived-delta validator."""
    global _DEFAULT_ARCHIVE_VALIDATOR
    configured = (
        uncarryable_delta_authority_is_valid
        or globals()["uncarryable_delta_authority_is_valid"]
    )
    _DEFAULT_ARCHIVE_VALIDATOR = configured
    _ARCHIVE_VALIDATOR.set(configured)


def _archive_validator() -> Callable[..., bool]:
    return _ARCHIVE_VALIDATOR.get() or _DEFAULT_ARCHIVE_VALIDATOR


def _latest_attempt_settlement_indices(
    records: Sequence[dict[str, object]],
    record_types: Collection[str] = ("attempt-terminal", "attempt-abort", "inline"),
) -> frozenset[int]:
    """Select the highest durable settlement watermark for every task."""
    latest: dict[str, tuple[int, int]] = {}
    for index, record in enumerate(records):
        if record.get("type") not in record_types:
            continue
        task_id = record.get("task_id")
        attempt_index = record.get("attempt_index")
        if attempt_index is None:
            attempt_index = 0 if record.get("type") == "inline" else -1
        if (
            not isinstance(task_id, str)
            or not task_id
            or type(attempt_index) is not int
            or attempt_index < -1
        ):
            continue
        previous = latest.get(task_id)
        if previous is None or attempt_index >= previous[0]:
            latest[task_id] = (attempt_index, index)
    return frozenset(index for _attempt_index, index in latest.values())


def fold_in_governed_review_findings(
    *, worktree: Path, task_id: str, unit_attempt_number: int
) -> dict[str, object]:
    """Resolve and return one governed review's existing capture receipt.

    The caller supplies only immutable governed identity. Primary dispatch
    telemetry selects the accepted terminal and its result artifact; this path
    never snapshots the caller's worktree, reruns acceptance, or writes Finding
    Ledger rows.
    """
    from ..kernel.authority_store import load_authority_records

    if not task_id.strip():
        raise DispatchError("governed finding fold-in requires a task_id")
    if (
        not isinstance(unit_attempt_number, int)
        or isinstance(unit_attempt_number, bool)
        or unit_attempt_number < 1
    ):
        raise DispatchError(
            "governed finding fold-in requires a positive unit_attempt_number"
        )
    resolved = worktree.resolve()
    repo = primary_repo_root(resolved)
    authority_history = load_authority_records(repo, WORK_UNIT_HISTORY_DAYS)
    terminal = latest_accepted_review_terminal(authority_history, task_id)
    if terminal is None:
        raise DispatchError(
            f"governed review {task_id!r} is not accepted as a non-superseded terminal"
        )
    if terminal.get("unit_attempt_number") != unit_attempt_number:
        raise DispatchError(
            "governed finding fold-in attempt does not match the accepted terminal"
        )
    receipt = terminal.get("finding_capture_receipt")
    if receipt is not None and not isinstance(receipt, dict):
        raise DispatchError("governed capture receipt is invalid")

    artifact_field = "result_artifact"
    digest_field = "result_sha256"
    if terminal.get("type") == "attempt-recovery":
        artifact_field = "recovered_result_artifact"
        digest_field = "recovered_result_sha256"
    artifact_bytes = _result_artifact_bytes(repo, terminal.get(artifact_field))
    artifact_digest = hashlib.sha256(artifact_bytes).hexdigest()
    if artifact_digest != terminal.get(digest_field):
        raise DispatchError("governed fold-in result artifact digest changed")
    try:
        result = json.loads(artifact_bytes)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise DispatchError("governed fold-in result artifact is invalid") from exc
    if not isinstance(result, dict) or not isinstance(result.get("findings"), list):
        raise DispatchError("governed fold-in result artifact has no findings")
    findings = _validate_result_findings(cast(list[object], result["findings"]))
    contract = terminal.get("task_contract")
    intent = contract.get("review_intent") if isinstance(contract, dict) else None
    review_intent = str(intent or terminal.get("review_intent") or "discovery")
    if receipt is None:
        # Governed intake persists only material findings, so an accepted
        # terminal whose review returned nothing persistable carries no receipt
        # and has nothing to fold in; a material finding without one is drift.
        if persisted_review_findings(findings, review_intent=review_intent):
            raise DispatchError(
                "governed capture receipt is missing from the accepted terminal"
            )
        return {
            "status": "folded-in",
            "task_id": task_id,
            "unit_attempt_number": unit_attempt_number,
            "finding_ids": [],
            "receipt": None,
        }
    if not findings:
        raise DispatchError("governed fold-in result artifact has no findings")

    category = terminal.get("category")
    source_identity_record = terminal.get("source_identity")
    snapshot_sha = terminal.get("snapshot_sha")
    snapshot_tree_sha = terminal.get("snapshot_tree_sha")
    if not isinstance(category, str) or not category.strip():
        raise DispatchError("governed capture receipt category is invalid")
    if (
        not isinstance(source_identity_record, dict)
        or not isinstance(source_identity_record.get("head"), str)
        or not isinstance(snapshot_sha, str)
        or not isinstance(snapshot_tree_sha, str)
    ):
        raise DispatchError("governed capture receipt source identity is invalid")
    request = build_finding_capture_request(
        pr=receipt.get("pr"),
        producer_kind="governed-review",
        producer_id=task_id,
        unit_attempt_number=unit_attempt_number,
        producer_skill="governed-review",
        category=category,
        advisory=False,
        findings=findings,
    )
    # Older receipts include suggestions. Match their complete request first;
    # new receipts bind only the shared intake selection.
    if receipt.get("operation_request_digest") != canonical_record_digest(request):
        findings = persisted_review_findings(findings, review_intent=review_intent)
        request = build_finding_capture_request(
            pr=receipt.get("pr"),
            producer_kind="governed-review",
            producer_id=task_id,
            unit_attempt_number=unit_attempt_number,
            producer_skill="governed-review",
            category=category,
            advisory=False,
            findings=findings,
        )
    expected_source_identity = {
        "snapshot_sha": snapshot_sha,
        "snapshot_tree_sha": snapshot_tree_sha,
        "source_head": source_identity_record["head"],
        "patch_identity": terminal.get("patch_identity"),
    }
    expected_finding_ids = [
        _finding_capture_id(snapshot_tree_sha, finding) for finding in findings
    ]
    expected_fields: dict[str, object] = {
        "type": "finding-capture-operation",
        "operation_id": finding_capture_operation_id(request),
        "operation_request_digest": canonical_record_digest(request),
        "producer_kind": "governed-review",
        "producer_id": task_id,
        "unit_attempt_number": unit_attempt_number,
        "producer_skill": "governed-review",
        "category": category,
        "advisory": False,
        "source_identity": expected_source_identity,
        "result_digest": canonical_record_digest({"findings": request["findings"]}),
        "finding_ids": expected_finding_ids,
    }
    mismatches = [
        field
        for field, expected in expected_fields.items()
        if receipt.get(field) != expected
    ]
    if mismatches:
        raise DispatchError(
            "governed capture receipt mismatch: " + ", ".join(mismatches)
        )
    outcomes = receipt.get("outcomes")
    if (
        not isinstance(outcomes, list)
        or [
            outcome.get("finding_id") if isinstance(outcome, dict) else None
            for outcome in outcomes
        ]
        != expected_finding_ids
        or any(
            not isinstance(outcome, dict)
            or outcome.get("status")
            not in {"created", "replayed", "promoted", "reopened"}
            for outcome in outcomes
        )
    ):
        raise DispatchError("governed capture receipt outcomes are invalid")
    expected_write_count = sum(
        1
        for outcome in outcomes
        if isinstance(outcome, dict) and outcome.get("status") != "replayed"
    )
    if receipt.get("write_count") != expected_write_count:
        raise DispatchError("governed capture receipt write count is invalid")
    try:
        persisted_receipt = replay_finding_capture(repo, request=request)
        durable_records = load_finding_records(repo, strict_malformed=True)
    except (LedgerReadError, FindingLedgerConflict) as exc:
        raise DispatchError(f"governed capture evidence is invalid: {exc}") from exc
    if persisted_receipt is None or persisted_receipt != receipt:
        raise DispatchError(
            "governed capture receipt does not match the primary Finding Ledger"
        )
    durable_by_id = {
        record.get("finding_id"): record
        for record in durable_records
        if isinstance(record.get("finding_id"), str)
    }
    valid_severities = {"suggestion", "important", "critical"}
    for finding_id, finding in zip(expected_finding_ids, findings, strict=True):
        durable = durable_by_id.get(finding_id)
        durable_severity = durable.get("severity") if durable is not None else None
        invalid = (
            durable is None
            or durable.get("claim") != finding.get("claim")
            or durable.get("snapshot_tree_sha") != snapshot_tree_sha
            or durable.get("advisory") is True
            or durable_severity not in valid_severities
        )
        path = finding.get("path")
        anchor = durable.get("anchor") if durable is not None else None
        if isinstance(path, str) and path and anchor is not None:
            invalid = (
                invalid or not isinstance(anchor, dict) or anchor.get("path") != path
            )
            line_start = finding.get("line_start")
            if isinstance(line_start, int) and not isinstance(line_start, bool):
                line_end = finding.get("line_end")
                expected_span = [
                    line_start,
                    (
                        line_end
                        if isinstance(line_end, int) and not isinstance(line_end, bool)
                        else line_start
                    ),
                ]
                invalid = invalid or anchor.get("line_span") != expected_span
        if invalid:
            raise DispatchError(
                f"governed durable finding {finding_id!r} is missing or inconsistent"
            )
    return {
        "status": "folded-in",
        "task_id": task_id,
        "unit_attempt_number": unit_attempt_number,
        "finding_ids": expected_finding_ids,
        "receipt": receipt,
    }


def _resolve_delta_chain(
    history: Sequence[dict[str, object]],
    delta_from: str,
    *,
    category: str,
    source_ref: str,
    new_tree_sha: str,
    alias: str | None = None,
    allow_superseded_ancestry: bool = False,
) -> tuple[dict[str, object], dict[str, object]]:
    """Return the full-review root and immediate same-lens predecessor."""
    authority_history = _authority_record_list(history)
    superseded = (
        frozenset()
        if allow_superseded_ancestry
        else frozenset(
            str(record.get("superseded_task_id") or record.get("task_id"))
            for record in authenticated_supersessions(authority_history)
        )
    )
    accepted_by_task = accepted_review_terminals(
        authority_history, _superseded_task_ids=superseded
    )
    verdicts = authenticated_verdicts(
        authority_history, _accepted_terminals=accepted_by_task
    )
    accepted_reviews = [
        accepted
        for accepted in accepted_by_task.values()
        if isinstance(accepted.get("snapshot_sha"), str)
    ]
    eligible = [
        record
        for record in accepted_reviews
        if verdicts.get(str(record.get("task_id")), {}).get("verdict") == "pass"
    ]

    def record_ref(record: dict[str, object]) -> str:
        identity = record.get("source_identity")
        return str(identity.get("ref") or "") if isinstance(identity, dict) else ""

    def select(snapshot_sha: str) -> dict[str, object]:
        candidates = [
            record
            for record in eligible
            if str(record.get("snapshot_sha") or "") == snapshot_sha
        ]
        if not candidates:
            raise DispatchError(
                f"delta_from_snapshot {snapshot_sha[:12]} has no completed, "
                "non-superseded review with an independent pass; dispatch a "
                "full review instead"
            )
        same_lens = [
            record for record in candidates if review_gate_lens(record) == category
        ]
        if not category or not same_lens:
            raise DispatchError(
                "delta predecessor review lens does not match the continuing "
                "review lens; dispatch a fresh full review instead"
            )
        same_branch = [
            record for record in same_lens if record_ref(record) == source_ref
        ]
        if not source_ref.startswith("refs/heads/") or not same_branch:
            raise DispatchError(
                "delta predecessor review branch does not match the continuing "
                "review branch; dispatch a fresh full review instead"
            )
        return same_branch[-1]

    seen: set[str] = set()
    current_sha = delta_from
    predecessor: dict[str, object] | None = None
    while True:
        if current_sha in seen:
            raise DispatchError("delta review chain contains a cycle")
        seen.add(current_sha)
        record = select(current_sha)
        if predecessor is None:
            predecessor = record
        contract = record.get("task_contract")
        prior_delta = (
            contract.get("delta_from_snapshot") if isinstance(contract, dict) else None
        )
        if not isinstance(prior_delta, str):
            same_tree_reviews = [
                candidate
                for candidate in accepted_reviews
                if candidate is not predecessor
                and review_gate_lens(candidate) == category
                and record_ref(candidate) == source_ref
                and candidate.get("snapshot_tree_sha") == new_tree_sha
            ]
            standing_reviews = [
                candidate
                for candidate in same_tree_reviews
                if verdicts.get(str(candidate.get("task_id")), {}).get("verdict")
                != "fail"
            ]
            if standing_reviews:
                raise DispatchError(
                    "the current source tree already has standing completed "
                    "review evidence for this branch and lens; replace "
                    "stale passing evidence with supersede-first"
                )
            failed_reviews = [
                candidate
                for candidate in same_tree_reviews
                if verdicts.get(str(candidate.get("task_id")), {}).get("verdict")
                == "fail"
            ]
            # Publication can discharge failed-review debt only through the
            # declared escalation edge, so refuse ad-hoc alternate reviewers.
            requested_alias = str(alias or "")
            if failed_reviews and any(
                requested_alias
                not in ESCALATION_TARGETS.get(_effective_alias(candidate), frozenset())
                for candidate in failed_reviews
            ):
                raise DispatchError(
                    "a verified-fail delta review may reroute only to its "
                    "designated escalation alias; reuse the failed review's "
                    "delta predecessor with the declared reviewer"
                )
            return record, predecessor
        current_sha = prior_delta


def validate_delta_review(
    history: Sequence[dict[str, object]],
    *,
    delta_from: str,
    worktree: Path,
    new_snapshot: ReviewSnapshot,
    category: str,
    source_ref: str,
    alias: str | None = None,
) -> dict[str, object]:
    """Enforce FL-2 and return the delta edge fields to record."""
    resolved = worktree.resolve()
    prior, predecessor = _resolve_delta_chain(
        history,
        delta_from,
        category=category,
        source_ref=source_ref,
        new_tree_sha=new_snapshot.tree_sha,
        alias=alias,
    )
    predecessor_contract = predecessor.get("task_contract")
    if isinstance(predecessor_contract, Mapping) and isinstance(
        predecessor_contract.get("delta_from_snapshot"), str
    ):
        raise DispatchError(
            "delta review budget exhausted; dispatch a fresh full review instead"
        )
    predecessor_tree = predecessor.get("snapshot_tree_sha")
    if not isinstance(predecessor_tree, str) or not predecessor_tree:
        raise DispatchError(
            "delta predecessor has no snapshot_tree_sha; dispatch a fresh full "
            "review instead"
        )
    if predecessor_tree == new_snapshot.tree_sha:
        raise DispatchError(
            "delta review source tree has not advanced beyond the predecessor "
            "snapshot; the completed same-source review remains terminal, and "
            "a verified-fail reroute must reuse that failed review's delta "
            "predecessor snapshot"
        )
    prior_identity = prior.get("patch_identity")
    if not isinstance(prior_identity, Mapping) or new_snapshot.patch_identity is None:
        raise DispatchError(
            "delta review patch identity is unavailable; dispatch a fresh full "
            "review instead"
        )
    delta = patch_identity.patch_delta_churn(
        resolved,
        prior_identity,
        new_snapshot.patch_identity,
        churn_ceiling=DELTA_CUMULATIVE_LINE_CAP,
    )
    if delta is None:
        raise DispatchError(
            "delta review author-patch comparison is ambiguous; dispatch a fresh "
            "full review instead"
        )
    cumulative, equivalence_receipt = delta
    if cumulative > DELTA_CUMULATIVE_LINE_CAP:
        raise DispatchError(
            f"cumulative delta churn ({cumulative} lines since the chain's "
            f"full review) exceeds the {DELTA_CUMULATIVE_LINE_CAP}-line floor; "
            "dispatch a fresh full review (FL-2)"
        )
    changed = subprocess.run(
        trusted_git_command(
            resolved,
            "diff",
            "--name-only",
            delta_from,
            new_snapshot.commit_sha,
        ),
        capture_output=True,
        text=True,
        env=sandbox_environment(os.environ),
    )
    if changed.returncode != 0:
        raise DispatchError(
            f"delta review pair diff failed: {changed.stderr.strip()[:200]}"
        )
    return {
        "delta_from_snapshot_sha": delta_from,
        "delta_from_tree_sha": predecessor_tree,
        "patch_equivalence": equivalence_receipt,
        "cumulative_patch_churn": cumulative,
    }


def _review_worktree_dirty(worktree: Path) -> bool:
    """Reject all tracked dirt, including audit files captured by Git snapshots."""
    if worktree_status_paths(worktree):
        return True
    try:
        tracked = subprocess.run(
            trusted_git_command(worktree, "status", "--porcelain=v1", "-z", "-uno"),
            capture_output=True,
            env=sandbox_environment(os.environ),
        )
    except OSError as exc:
        raise DispatchError("delta review tracked status is unavailable") from exc
    if tracked.returncode != 0 or not isinstance(tracked.stdout, bytes):
        raise DispatchError("delta review tracked status is unavailable")
    return bool(tracked.stdout)


def validate_delta_review_patch_admission(
    history: Sequence[dict[str, object]],
    *,
    delta_from: str,
    worktree: Path,
    current_source: Mapping[str, object],
    category: str,
    alias: str | None = None,
) -> None:
    """Authenticate and byte-bound the real Git pair before provider runtime."""
    if not identities_match(source_identity(worktree), current_source):
        raise DispatchError("delta review current source changed before byte admission")
    if _review_worktree_dirty(worktree):
        raise DispatchError("delta review byte admission requires a clean worktree")
    current_head = current_source.get("head")
    source_ref = current_source.get("ref")
    if (
        not isinstance(current_head, str)
        or re.fullmatch(r"[0-9a-f]{40}", current_head) is None
        or not isinstance(source_ref, str)
    ):
        raise DispatchError("delta review current source identity is invalid")
    tree = subprocess.run(
        trusted_git_command(worktree.resolve(), "rev-parse", f"{current_head}^{{tree}}"),
        capture_output=True,
        text=True,
        env=sandbox_environment(os.environ),
    )
    current_tree_sha = tree.stdout.strip()
    if tree.returncode != 0 or re.fullmatch(r"[0-9a-f]{40}", current_tree_sha) is None:
        raise DispatchError("delta review current source tree is unavailable")
    _root, predecessor = _resolve_delta_chain(
        history,
        delta_from,
        category=category,
        source_ref=source_ref,
        new_tree_sha=current_tree_sha,
        alias=alias,
    )
    predecessor_contract = predecessor.get("task_contract")
    if isinstance(predecessor_contract, Mapping) and isinstance(
        predecessor_contract.get("delta_from_snapshot"), str
    ):
        raise DispatchError(
            "delta review budget exhausted; dispatch a fresh full review instead"
        )
    bounded_delta_review_patch_bytes(
        worktree,
        delta_from=delta_from,
        current_sha=current_head,
    )


def uncarryable_delta_authority(
    target: Mapping[str, object],
    *,
    worktree: Path,
    current_source: Mapping[str, object],
    history: Sequence[dict[str, object]] = (),
) -> dict[str, object] | None:
    """Prove that a snapshot-bound review cannot continue as a delta."""
    snapshot_sha = target.get("snapshot_sha")
    task_id = target.get("task_id")
    current_head = current_source.get("head")
    if (
        not isinstance(snapshot_sha, str)
        or re.fullmatch(r"[0-9a-f]{40}", snapshot_sha) is None
        or not isinstance(task_id, str)
        or not task_id
        or not isinstance(current_head, str)
        or re.fullmatch(r"[0-9a-f]{40}", current_head) is None
        or _review_worktree_dirty(worktree)
    ):
        return None
    prior_identity = target.get("patch_identity")
    if not isinstance(prior_identity, Mapping):
        return None
    cumulative_churn: int | None = None
    patch_bytes: bytes | None = None
    try:
        current_identity = patch_identity.capture_patch_identity(
            worktree, candidate_sha=current_head
        )
        if not isinstance(current_identity, Mapping):
            return None
        comparison_identity = prior_identity
        contract = target.get("task_contract")
        delta_from = (
            contract.get("delta_from_snapshot")
            if isinstance(contract, Mapping)
            else None
        )
        if isinstance(delta_from, str):
            current_tree_sha = current_identity.get("candidate_tree_sha")
            source_ref = current_source.get("ref")
            if (
                not isinstance(current_tree_sha, str)
                or not current_tree_sha
                or not isinstance(source_ref, str)
                or not source_ref
            ):
                return None
            target_record = dict(target)
            root, _predecessor = _resolve_delta_chain(
                history,
                delta_from,
                category=review_gate_lens(target_record),
                source_ref=source_ref,
                new_tree_sha=current_tree_sha,
                alias=_effective_alias(target_record),
                allow_superseded_ancestry=True,
            )
            root_identity = root.get("patch_identity")
            if not isinstance(root_identity, Mapping):
                return None
            comparison_identity = root_identity
        delta = patch_identity.patch_delta_churn(
            worktree,
            comparison_identity,
            current_identity,
            churn_ceiling=DELTA_CUMULATIVE_LINE_CAP,
        )
        if delta is None:
            reason = "author-patch-ambiguous"
        else:
            cumulative_churn = delta[0]
            if cumulative_churn > DELTA_CUMULATIVE_LINE_CAP:
                reason = "delta-churn-exceeded"
            else:
                # Existing ambiguity/churn authority remains unchanged. Byte
                # recovery additionally authenticates the exact pair we measure.
                if not identities_match(source_identity(worktree), current_source):
                    return None
                _root, predecessor = _resolve_delta_chain(
                    history,
                    snapshot_sha,
                    category=review_gate_lens(dict(target)),
                    source_ref=str(current_source.get("ref") or ""),
                    new_tree_sha=str(current_identity.get("candidate_tree_sha") or ""),
                    alias=_effective_alias(dict(target)),
                    allow_superseded_ancestry=True,
                )
                if predecessor.get("task_id") != task_id:
                    return None
                patch_bytes = delta_review_patch_bytes(
                    worktree, delta_from=snapshot_sha, current_sha=current_head
                )
                if len(patch_bytes) <= DELTA_REVIEW_PATCH_MAX_BYTES:
                    return None
                reason = "delta-byte-cap-exceeded"
    except (DispatchError, OSError, RuntimeError, patch_identity.PatchIdentityError):
        return None
    authority: dict[str, object] = {
        "schema_version": "uncarryable-delta-authority-v1",
        "reason": reason,
        "predecessor_task_id": task_id,
        "predecessor_snapshot_sha": snapshot_sha,
        "superseding_source_identity": dict(current_source),
    }
    if cumulative_churn is not None:
        authority["cumulative_churn"] = cumulative_churn
        authority["delta_churn_ceiling"] = DELTA_CUMULATIVE_LINE_CAP
    if patch_bytes is not None:
        authority.update(
            delta_patch_bytes=len(patch_bytes),
            delta_patch_max_bytes=DELTA_REVIEW_PATCH_MAX_BYTES,
            delta_patch_sha256=hashlib.sha256(patch_bytes).hexdigest(),
            delta_patch_from_sha=snapshot_sha,
            delta_patch_to_sha=current_head,
        )
    return authority


def delta_review_patch_bytes(
    worktree: Path,
    *,
    delta_from: str,
    current_sha: str,
) -> bytes:
    """Return the canonical real Git pair patch used by all byte-cap decisions."""
    completed = subprocess.run(
        trusted_git_command(
            worktree.resolve(),
            "diff",
            "--no-ext-diff",
            "--binary",
            delta_from,
            current_sha,
        ),
        capture_output=True,
        env=sandbox_environment(os.environ),
    )
    if completed.returncode != 0:
        error = completed.stderr.decode("utf-8", errors="replace").strip()[:200]
        raise DispatchError(f"delta review patch materialization failed: {error}")
    return completed.stdout


def bounded_delta_review_patch_bytes(
    worktree: Path,
    *,
    delta_from: str,
    current_sha: str,
) -> bytes:
    """Return the canonical pair patch only when it fits the immutable cap."""
    patch = delta_review_patch_bytes(
        worktree,
        delta_from=delta_from,
        current_sha=current_sha,
    )
    if len(patch) > DELTA_REVIEW_PATCH_MAX_BYTES:
        raise DispatchError(
            "delta review pair patch exceeds the bounded prompt size; dispatch "
            "a fresh full review instead"
        )
    return patch


def materialize_delta_review_patch(
    worktree: Path,
    *,
    delta_from: str,
    new_snapshot: ReviewSnapshot,
) -> str:
    """Return a bounded exact patch for embedding in the review prompt."""
    return bounded_delta_review_patch_bytes(
        worktree,
        delta_from=delta_from,
        current_sha=new_snapshot.commit_sha,
    ).decode("utf-8", errors="replace")


def review_terminal_acceptance_reasons(
    record: dict[str, object], *, allow_advisory: bool = False
) -> tuple[str, ...]:
    """Return why one review terminal cannot serve as accepted evidence."""
    reasons: list[str] = []
    if record.get("type") not in {"attempt-terminal", "attempt-recovery"}:
        reasons.append("not-review-terminal")
    if record.get("status") != "completed":
        reasons.append("review-not-completed")
    if record.get("read_only") is not True:
        reasons.append("review-not-read-only")
    if record.get("work_kind") != "review":
        reasons.append("not-review-work-kind")
    if record.get("advisory") is True and not allow_advisory:
        reasons.append("advisory-review")
    receipt_reasons = review_acceptance_receipt_reasons(record)
    reasons.extend(receipt_reasons)
    receipt = record.get("acceptance_receipt")
    if isinstance(receipt, dict):
        expected_provenance = (
            "recovery-time" if record.get("type") == "attempt-recovery" else "pre-model"
        )
        if receipt.get("provenance") != expected_provenance:
            reasons.append("review-acceptance-wrong-phase")
    contract = record.get("task_contract")
    if isinstance(contract, dict) and "required_sections" in contract:
        chain_receipt = record.get("review_chain_receipt")
        if not isinstance(chain_receipt, dict):
            reasons.append("missing-review-chain-receipt")
        else:
            reasons.extend(
                f"review-chain-{reason}"
                for reason in review_chain_receipt_reasons(
                    chain_receipt,
                    task={**contract, "task_id": record.get("task_id")},
                    snapshot_sha=str(record.get("snapshot_sha") or ""),
                    snapshot_tree_sha=str(record.get("snapshot_tree_sha") or ""),
                    patch_identity=(
                        cast(dict[str, object], record["patch_identity"])
                        if isinstance(record.get("patch_identity"), dict)
                        else None
                    ),
                )
            )
    if record.get("type") == "attempt-recovery":
        verification = record.get("recovery_verification")
        recovered_terminal_status = record.get("recovered_terminal_status")
        recorded_classification = record.get("recovery_classification")
        legacy_capability_recovery = (
            recovered_terminal_status is None
            and recorded_classification is None
            and isinstance(verification, dict)
            and verification.get("blocker_class") == "acceptance-capability-only"
        )
        if recovered_terminal_status == "blocked" or legacy_capability_recovery:
            expected_classification = "acceptance-capability-only"
        elif recovered_terminal_status == "completed":
            expected_classification = "acceptance-receipt-only"
        else:
            expected_classification = None
        if (
            not isinstance(verification, dict)
            or verification.get("type") != "review-recovery-verification"
            or verification.get("task_id") != record.get("task_id")
            or verification.get("task_contract_hash")
            != record.get("task_contract_hash")
            or verification.get("source_identity") != record.get("source_identity")
            or verification.get("snapshot_sha") != record.get("snapshot_sha")
            or verification.get("snapshot_tree_sha") != record.get("snapshot_tree_sha")
            or verification.get("result_artifact")
            != record.get("recovered_result_artifact")
            or verification.get("result_sha256")
            != record.get("recovered_result_sha256")
            or verification.get("semantic_verdict") != "pass"
            or verification.get("blocker_class") != expected_classification
            or (
                not legacy_capability_recovery
                and recorded_classification != expected_classification
            )
            or verification.get("unresolved_findings") != 0
            or not str(verification.get("notes") or "").strip()
            or not verifier_identity_is_independent(
                worker_identity=record.get("worker_identity"),
                worker_alias=_effective_alias(record),
                worker_model=record.get("runtime_effective_model") or record.get("model"),
                verifier_identity=verification.get("verifier_identity"),
                verifier_alias=verification.get("verifier_alias"),
            )
        ):
            reasons.append("invalid-review-recovery-verification")
    return tuple(dict.fromkeys(reasons))


def _review_terminal_authority_projection(
    records: Sequence[dict[str, object]],
    *,
    _superseded_task_ids: Collection[str] | None = None,
) -> tuple[Collection[str], frozenset[int], frozenset[int]]:
    """Index authenticated terminals, supersessions, and recoveries."""
    authority_history = _authority_record_list(records)
    governed_records = current_telemetry(authority_history)
    open_before_ids, supersession_open_before_ids = (
        _authenticated_open_before_record_projections(authority_history)
    )
    registered_before_ids = _registered_authority_before_record_ids(authority_history)
    authenticated_terminal_ids = _authenticated_attempt_terminal_ids(
        authority_history,
        _open_before_record_ids=open_before_ids,
        _registered_before_record_ids=registered_before_ids,
    )
    authenticated_coordinator_ids = _authenticated_coordinator_record_ids(
        authority_history
    )
    legacy_compatibility_ids = _legacy_compatibility_record_ids(authority_history)
    superseded_task_ids = _superseded_task_ids
    if superseded_task_ids is None:
        superseded_task_ids = {
            str(record.get("superseded_task_id") or record.get("task_id"))
            for record in authenticated_supersessions(
                authority_history,
                _open_before_record_ids=open_before_ids,
                _supersession_open_before_record_ids=supersession_open_before_ids,
                _registered_before_record_ids=registered_before_ids,
                _authenticated_terminal_ids=authenticated_terminal_ids,
                _authenticated_coordinator_ids=authenticated_coordinator_ids,
                _legacy_compatibility_ids=legacy_compatibility_ids,
            )
            if record.get("superseded_task_id") or record.get("task_id")
        }
    deposits_by_key: dict[tuple[str, int], list[dict[str, object]]] = {}
    authenticated_recovery_ids: set[int] = set()
    for record in governed_records:
        key = _terminal_authority_attempt_key(record)
        if (
            record.get("type") == "attempt-terminal"
            and key is not None
            and id(record) in authenticated_terminal_ids
        ):
            deposits_by_key.setdefault(key, []).append(record)
            continue
        if record.get("type") != "attempt-recovery":
            continue
        if id(record) in registered_before_ids:
            continue
        deposits = deposits_by_key.get(key, []) if key is not None else []
        if len(deposits) != 1 or id(record) in open_before_ids:
            continue
        deposit = deposits[0]
        if isinstance(
            deposit.get("terminal_authority_proof"), dict
        ) or not _recovery_matches_deposit(record, deposit):
            continue
        if not isinstance(record.get("terminal_authority_proof"), dict):
            if id(record) in legacy_compatibility_ids:
                authenticated_recovery_ids.add(id(record))
            continue
        if id(record) not in authenticated_coordinator_ids:
            continue
        authenticated_recovery_ids.add(id(record))
    return (
        superseded_task_ids,
        authenticated_terminal_ids,
        frozenset(authenticated_recovery_ids),
    )


def authenticated_review_terminals(
    records: Sequence[dict[str, object]],
    *,
    _superseded_task_ids: Collection[str] | None = None,
) -> dict[str, dict[str, object]]:
    """Project standing review terminals without trusting recovery rows."""
    superseded_task_ids, authenticated_terminal_ids, authenticated_recovery_ids = (
        _review_terminal_authority_projection(
            records, _superseded_task_ids=_superseded_task_ids
        )
    )
    latest_by_task: dict[str, dict[str, object]] = {}
    for record in current_telemetry(records):
        task_id = record.get("task_id")
        if (
            isinstance(task_id, str)
            and task_id not in superseded_task_ids
            and (
                (
                    record.get("type") == "attempt-terminal"
                    and id(record) in authenticated_terminal_ids
                )
                or (
                    record.get("type") == "attempt-recovery"
                    and id(record) in authenticated_recovery_ids
                )
            )
        ):
            latest_by_task[task_id] = record
    return latest_by_task


def accepted_review_terminals(
    records: Sequence[dict[str, object]],
    *,
    allow_advisory: bool = False,
    _superseded_task_ids: Collection[str] | None = None,
) -> dict[str, dict[str, object]]:
    """Project every latest accepted review terminal in one authenticated pass."""
    superseded_task_ids, authenticated_terminal_ids, authenticated_recovery_ids = (
        _review_terminal_authority_projection(
            records, _superseded_task_ids=_superseded_task_ids
        )
    )

    latest_by_task: dict[str, dict[str, object]] = {}
    for record in current_telemetry(records):
        task_id = record.get("task_id")
        if (
            isinstance(task_id, str)
            and task_id not in superseded_task_ids
            and record.get("type") in {"attempt-terminal", "attempt-recovery"}
        ):
            latest_by_task[task_id] = record
    accepted: dict[str, dict[str, object]] = {}
    for task_id, terminal in latest_by_task.items():
        if (
            (
                terminal.get("type") == "attempt-terminal"
                and id(terminal) in authenticated_terminal_ids
            )
            or (
                terminal.get("type") == "attempt-recovery"
                and id(terminal) in authenticated_recovery_ids
            )
        ) and not review_terminal_acceptance_reasons(
            terminal, allow_advisory=allow_advisory
        ):
            accepted[task_id] = terminal
    return accepted


def accepted_review_producers(
    records: Sequence[dict[str, object]],
) -> dict[str, dict[str, object]]:
    """Bind accepted recovery evidence to its authenticated original producer."""
    accepted = accepted_review_terminals(records)
    terminal_ids = _authenticated_attempt_terminal_ids(records)
    producers: dict[str, dict[str, object]] = {}
    for task_id, accepted_row in accepted.items():
        if accepted_row.get("type") != "attempt-recovery":
            producers[task_id] = accepted_row
            continue
        deposits = [
            row
            for row in records
            if row.get("type") == "attempt-terminal"
            and id(row) in terminal_ids
            and _recovery_matches_deposit(accepted_row, row)
        ]
        if len(deposits) == 1:
            producers[task_id] = deposits[0]
    return producers


def authenticated_verdicts(
    records: Sequence[dict[str, object]],
    *,
    _accepted_terminals: Mapping[str, dict[str, object]] | None = None,
) -> dict[str, dict[str, object]]:
    """Project verdicts appended after their accepted review terminal."""
    accepted_terminals = _accepted_terminals
    if accepted_terminals is None:
        accepted_terminals = accepted_review_terminals(records)
    authority_history = _authority_record_list(records)
    authenticated_coordinator_ids = _authenticated_coordinator_record_ids(
        authority_history
    )
    legacy_compatibility_ids = _legacy_compatibility_record_ids(authority_history)
    seen_terminals: set[str] = set()
    latest: dict[str, dict[str, object]] = {}
    for record in current_telemetry(authority_history):
        task_id = record.get("task_id")
        if not isinstance(task_id, str):
            continue
        terminal = accepted_terminals.get(task_id)
        if record is terminal:
            seen_terminals.add(task_id)
            continue
        if record.get("type") != "verdict" or task_id not in seen_terminals:
            continue
        terminal_is_registered = isinstance(
            terminal.get("terminal_authority_proof"), dict
        )
        verdict_is_signed = isinstance(record.get("terminal_authority_proof"), dict)
        if (
            not terminal_is_registered
            and not verdict_is_signed
            and id(record) in legacy_compatibility_ids
            and record.get("verdict") in {"pass", "fail"}
        ):
            # Preserve historical proofless verdicts only inside the legacy
            # pre-cutover prefix; later appends must pass signature checks.
            latest[task_id] = record
            continue
        if (
            record.get("run_id") != terminal.get("run_id")
            or record.get("target_worker_identity") != terminal.get("worker_identity")
            or not isinstance(record.get("verifier_identity"), str)
            or not str(record.get("verifier_identity") or "").strip()
            or not verifier_identity_is_independent(
                worker_identity=terminal.get("worker_identity"),
                worker_alias=_effective_alias(terminal),
                worker_model=terminal.get("runtime_effective_model") or terminal.get("model"),
                verifier_identity=record.get("verifier_identity"),
                verifier_alias=record.get("verifier_alias"),
            )
            or record.get("verdict") not in {"pass", "fail"}
        ):
            continue
        if not verdict_is_signed:
            continue
        if verdict_is_signed and id(record) not in authenticated_coordinator_ids:
            continue
        latest[task_id] = record
    return latest


def delivery_controller_records(
    records: Sequence[dict[str, object]],
) -> list[dict[str, object]]:
    """Return only authority-bearing records the delivery controller may trust.

    Delivery-control decisions share the coordinator authority stream, while
    review budget and trust-verifier evidence share the authenticated review
    projection. Authenticated supersession rows remain visible as lineage
    evidence even though their retired review terminals are omitted. Keeping
    this adapter here avoids a circular import from the storage-agnostic
    controller back into the dispatcher.
    """
    authority_history = _authority_record_list(records)
    governed = current_telemetry(authority_history)
    coordinator_ids = _authenticated_coordinator_record_ids(authority_history)
    legacy_ids = _legacy_compatibility_record_ids(authority_history)
    _superseded_task_ids, terminal_ids, recovery_ids = (
        _review_terminal_authority_projection(authority_history)
    )
    controller_supersessions = [
        record
        for record in authenticated_supersessions(authority_history)
        if id(record) in coordinator_ids
        if isinstance(record.get("terminal_authority_proof"), Mapping)
        and record["terminal_authority_proof"].get("authority_kind") == "coordinator"
        if record.get("superseded_task_id") or record.get("task_id")
    ]
    controller_superseded_task_ids = {
        str(record.get("superseded_task_id") or record.get("task_id"))
        for record in controller_supersessions
    }
    controller_supersession_ids = {id(record) for record in controller_supersessions}
    accepted = accepted_review_terminals(authority_history)
    verdicts = authenticated_verdicts(authority_history, _accepted_terminals=accepted)
    verdict_ids = {id(record) for record in verdicts.values()}
    authenticated_settlements = [
        record
        for record in governed
        if id(record) in terminal_ids
        and record.get("type") in {"attempt-terminal", "attempt-abort"}
    ]
    settlement_indices = _latest_attempt_settlement_indices(
        authenticated_settlements,
        record_types=("attempt-terminal", "attempt-abort"),
    )
    latest_settlement_ids = {
        id(authenticated_settlements[index]) for index in settlement_indices
    }
    return [
        record
        for record in governed
        if (
            record.get("type") == CONTROL_TYPE
            and (id(record) in coordinator_ids or id(record) in legacy_ids)
        )
        or (
            record.get("type") in {"attempt-terminal", "attempt-abort"}
            and id(record) in terminal_ids
            and id(record) in latest_settlement_ids
            and str(record.get("task_id") or "") not in controller_superseded_task_ids
        )
        or (
            record.get("type") == "attempt-recovery"
            and id(record) in recovery_ids
            and str(record.get("task_id") or "") not in controller_superseded_task_ids
        )
        or id(record) in controller_supersession_ids
        or id(record) in verdict_ids
    ]


def registered_dispatcher_delivery_controller_records(
    records: Sequence[dict[str, object]],
) -> list[dict[str, object]]:
    """Project controller terminals proved by a registered dispatcher.

    A dispatcher key only proves that the registered worker settled its own
    attempt; it does not prove which PR that attempt delivered.  The PR is
    therefore bound to the coordinator-signed registration (attempt-start) of
    the very attempt the terminal completes, and a terminal claiming a
    different PR than its registration is excluded from the projection.
    """
    authority_history = _authority_record_list(records)
    registered_before_ids = _registered_authority_before_record_ids(authority_history)
    registrations = _authenticated_attempt_terminal_registrations(authority_history)
    return [
        record
        for record in delivery_controller_records(authority_history)
        if record.get("type") == "attempt-terminal"
        and id(record) in registered_before_ids
        and isinstance((proof := record.get("terminal_authority_proof")), Mapping)
        and proof.get("authority_kind") == "dispatcher"
        and (registration := registrations.get(id(record))) is not None
        and _registration_binds_terminal_pr(registration, record)
    ]


def _registration_binds_terminal_pr(
    registration: Mapping[str, object], terminal: Mapping[str, object]
) -> bool:
    """Accept a terminal PR only when its registration carries the same PR."""
    registered_pr = registration.get("pr")
    return (
        type(registered_pr) is int
        and registered_pr > 0
        and terminal.get("pr") == registered_pr
        and type(terminal.get("pr")) is int
    )


def latest_accepted_review_terminal(
    records: Sequence[dict[str, object]],
    task_id: str,
    *,
    allow_advisory: bool = False,
) -> dict[str, object] | None:
    """Select the latest non-superseded terminal satisfying review acceptance."""
    return accepted_review_terminals(records, allow_advisory=allow_advisory).get(
        task_id
    )


def _effective_alias(record: dict[str, object]) -> str:
    explicit = record.get("effective_alias")
    if isinstance(explicit, str) and explicit:
        return explicit
    if record.get("engine") == "cursor" and record.get("model") == "auto":
        return "auto"
    return str(record.get("alias"))


def authenticated_retry_outcomes(
    records: Sequence[dict[str, object]],
) -> list[dict[str, object]]:
    """Project retry outcomes without trusting registered raw clones."""
    _, authenticated_terminal_ids, authenticated_recovery_ids = (
        _review_terminal_authority_projection(records)
    )
    accepted = retained_retry_outcomes(records)
    for record in current_telemetry(records):
        record_type = record.get("type")
        if record_type not in DISPATCH_OUTCOME_TYPES:
            continue
        if (
            record_type in {"attempt-terminal", "attempt-abort"}
            and id(record) in authenticated_terminal_ids
        ) or (
            record_type == "attempt-recovery"
            and id(record) in authenticated_recovery_ids
        ):
            accepted.append(record)
    latest_indices = _latest_attempt_settlement_indices(
        accepted, DISPATCH_OUTCOME_TYPES
    )
    return [record for index, record in enumerate(accepted) if index in latest_indices]


def latest_explicit_alias_availability(
    records: Sequence[dict[str, object]], *, alias: str
) -> dict[str, object] | None:
    """Return the newest explicit operator/provider-UI observation for an alias."""
    for record in reversed(current_telemetry(records)):
        effective_alias = str(record.get("effective_alias") or record.get("alias"))
        if record.get("type") == "alias-availability" and effective_alias == alias:
            return record
    return None


def _result_artifact_bytes(repo: Path, artifact: object) -> bytes:
    """Read one repo-contained immutable result artifact, rejecting path escape."""
    if not isinstance(artifact, str) or not artifact or Path(artifact).is_absolute():
        raise DispatchError("review recovery requires a relative result artifact")
    root = repo.resolve()
    unresolved = root / artifact
    if unresolved.is_symlink():
        raise DispatchError("review recovery result artifact cannot be a symlink")
    path = unresolved.resolve()
    try:
        path.relative_to(root)
    except ValueError as exc:
        raise DispatchError(
            "review recovery result artifact escapes the repository"
        ) from exc
    if not path.is_file():
        raise DispatchError("review recovery result artifact is unavailable")
    return path.read_bytes()


# Imported after the authority functions above so the archive validator can
# share the same configurable cutover seam without an import cycle.
from ._archived import (  # noqa: E402
    archived_supersession_deposits,
    passing_archive_anchor,
    passing_archive_ancestry,
    validate_archived_review_witness,
)
from ..kernel import seams  # noqa: E402
from ..kernel.authority_store import load_authority_records  # noqa: E402


def configure_kernel_seams() -> None:
    """Bind the kernel's declared A4 injection points to this package."""
    seams.configure(
        _latest_attempt_settlement_indices=_latest_attempt_settlement_indices,
        accepted_review_terminals=accepted_review_terminals,
        archived_supersession_deposits=archived_supersession_deposits,
        authenticated_retry_outcomes=authenticated_retry_outcomes,
        authenticated_review_terminals=authenticated_review_terminals,
        authenticated_supersessions=authenticated_supersessions,
        authenticated_verdicts=authenticated_verdicts,
        delivery_controller_records=delivery_controller_records,
        latest_explicit_alias_availability=latest_explicit_alias_availability,
        load_authority_records=load_authority_records,
        passing_archive_ancestry=passing_archive_ancestry,
        passing_archive_anchor=passing_archive_anchor,
        supersession_reason_matches_terminal=supersession_reason_matches_terminal,
        validate_archived_review_witness=validate_archived_review_witness,
    )


# Preserve the source modules' directly-importable behavior.  Package
# configuration refreshes these bindings after applying a consumer profile.
configure_kernel_seams()
