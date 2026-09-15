"""Shared, storage-agnostic admission for content-addressed reviews.

The import dependency closure intentionally is not computed in this release.
``closure_paths`` is the exact union of changed paths and security-trigger
paths; the repository-aware closure belongs in a later ``review/scope.py``.

Released-retry paths are rare and deliberately fail closed.  If their
obligations cannot be proved complete, admission spends one full primary
verification instead of carrying earlier authority.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Literal, cast

from ..kernel.authority_projection import (
    ReviewSlotState,
    authenticated_review_state_records,
    family_coverages,
    generations,
    slot_state,
)
from ..kernel.canonical import canonical_record_digest
from ..kernel.gitscope import DispatchError
from ..kernel.patch_identity import PatchIdentityError, tree_diff_paths
from ..kernel.policy import (
    DISPATCH_POLICY_VERSION,
    RUNTIME_CONTRACT_VERSION,
    TELEMETRY_SCHEMA_VERSION,
)
from ..kernel.review_state import (
    GenerationCarryV1,
    ReviewFamilyCoverageV1,
    ReviewGenerationV1,
    ReviewSlotError,
    ReviewSlotReservation,
    ReviewStateError,
    _reserve_review_slot,
    assert_authority_ledger_lock_held,
    fresh_review_generation,
    generation_id_for,
    patch_content_digest,
    resolve_generation,
)
from ._security_scope import (
    SecurityReviewScopeError,
    security_trigger_paths_between,
)
from .routing import review_family_for_intent

BlockedCode = Literal[
    "stale-source",
    "invalid-proof",
    "missing-evidence",
    "slot-held",
    "slots-exhausted",
    "retries-exhausted",
    "oversized-delta",
    "reservation-conflict",
    "invalid-launch-label",
    "unknown-intent",
]
RequestedReview = Literal["review", "delta"]
LaunchReason = Literal[
    "initial",
    "bounded-delta",
    "supersession",
    "trust-verification",
    "trust-delta",
    "security-path",
    "owner-requested",
    "infrastructure-retry",
]

MAX_DELTA_SCOPE_PATHS = 300


@dataclass(frozen=True, slots=True)
class Carry:
    generation: ReviewGenerationV1
    receipts: tuple[str, ...]
    verdicts: tuple[tuple[str, Literal["pass", "fail"]], ...]
    carry_record: GenerationCarryV1
    records_to_append: tuple[Mapping[str, object], ...]
    dispatch: bool = field(default=False, init=False)
    kind: str = field(default="carry", init=False)


@dataclass(frozen=True, slots=True)
class Reserved:
    generation: ReviewGenerationV1
    slot: ReviewSlotReservation
    scoped_task: Mapping[str, object]
    records_to_append: tuple[Mapping[str, object], ...]
    launch_reason: LaunchReason
    secondary_triggers: tuple[LaunchReason, ...] = ()
    dispatch: bool = field(default=True, init=False)
    kind: str = field(default="reserved", init=False)

    @property
    def reservation(self) -> ReviewSlotReservation:
        return self.slot


@dataclass(frozen=True, slots=True)
class Blocked:
    code: BlockedCode
    evidence: Mapping[str, object]
    records_to_append: tuple[Mapping[str, object], ...] = ()
    dispatch: bool = field(default=False, init=False)
    kind: str = field(default="blocked", init=False)


@dataclass(frozen=True, slots=True)
class NonVerdictReviewLaunch:
    """A package-declared no-family launch with no verdict authority."""

    scoped_task: Mapping[str, object]
    launch_record: Mapping[str, object]
    records_to_append: tuple[Mapping[str, object], ...]
    family: None = field(default=None, init=False)
    dispatch: bool = field(default=True, init=False)
    kind: str = field(default="non-verdict", init=False)


Admission = Carry | Reserved | NonVerdictReviewLaunch | Blocked


def _launch_reasons(
    *,
    family: Literal["delivery", "trust"],
    slot_kind: Literal["primary", "delta"],
    transition_kind: str | None,
    security_triggered: bool,
    infrastructure_retry: bool,
) -> tuple[LaunchReason, tuple[LaunchReason, ...]]:
    """Derive the bounded explanatory vocabulary from admission authority."""
    triggers: list[LaunchReason] = []
    if infrastructure_retry:
        triggers.append("infrastructure-retry")
    if transition_kind == "owner-requested":
        triggers.append("owner-requested")
    elif transition_kind == "supersession":
        triggers.append("supersession")
    if family == "trust":
        triggers.append("trust-delta" if slot_kind == "delta" else "trust-verification")
    elif slot_kind == "delta":
        triggers.append("bounded-delta")
    elif security_triggered:
        triggers.append("security-path")
    else:
        triggers.append("initial")
    if security_triggered and "security-path" not in triggers:
        triggers.append("security-path")
    if transition_kind == "initial" and "initial" not in triggers:
        triggers.append("initial")
    return triggers[0], tuple(dict.fromkeys(triggers[1:]))


def _blocked(
    code: BlockedCode,
    message: str,
    *,
    records: Sequence[Mapping[str, object]] = (),
    **evidence: object,
) -> Blocked:
    safe_records = tuple(
        record for record in records if record.get("type") == "generation-carry-v1"
    )
    safe_evidence = {
        key: value
        for key, value in evidence.items()
        if key in {"path_count", "maximum", "fields"}
    }
    cause_class = (
        message
        if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*(?:Error)?", message)
        else "".join(part.capitalize() for part in code.split("-")) + "Error"
    )
    return Blocked(
        code,
        {
            "message": "Blocked",
            "cause_class": cause_class,
            **safe_evidence,
        },
        safe_records,
    )


def _task_source(task: Mapping[str, object]) -> Mapping[str, object] | None:
    source = task.get("source_identity")
    if isinstance(source, Mapping):
        return source
    contract = task.get("task_contract")
    if isinstance(contract, Mapping) and isinstance(
        contract.get("source_identity"), Mapping
    ):
        return cast(Mapping[str, object], contract["source_identity"])
    return None


def _canonical_paths(values: Sequence[str], *, label: str) -> tuple[str, ...]:
    if isinstance(values, (str, bytes)):
        raise ValueError(f"{label} must be a sequence")
    normalized: set[str] = set()
    for value in values:
        if not isinstance(value, str) or not value:
            raise ValueError(f"{label} contains an invalid path")
        path = PurePosixPath(value)
        if path.is_absolute() or ".." in path.parts or str(path) != value:
            raise ValueError(f"{label} contains a non-canonical path")
        normalized.add(value)
    return tuple(sorted(normalized))


def _review_intent(task: Mapping[str, object]) -> object:
    contract = task.get("task_contract")
    contracted = contract.get("review_intent") if isinstance(contract, Mapping) else None
    supplied = task.get("review_intent")
    if contracted is not None and supplied is not None and contracted != supplied:
        raise ValueError("review intent conflicts with the immutable task contract")
    return contracted if contracted is not None else supplied


_UNBOUND_TASK = object()


def _bound_task_family(
    records: Sequence[Mapping[str, object]], task_id: str
) -> object:
    """Return the family already authenticated for a task identity, if any."""
    from ..kernel.authority_projection import authenticated_coordinator_record_ids

    authenticated = authenticated_coordinator_record_ids(
        cast(Sequence[dict[str, object]], records)
    )
    families: set[str | None] = set()
    for record in records:
        if id(record) not in authenticated or record.get("task_id") != task_id:
            continue
        if record.get("type") == "review-slot-reservation-v1":
            family = record.get("family")
            if family in {"delivery", "trust"}:
                families.add(cast(str, family))
            continue
        intent = None
        if record.get("type") == "review-nonverdict-launch-v1":
            intent = record.get("intent")
        else:
            contract = record.get("task_contract")
            if isinstance(contract, Mapping):
                intent = contract.get("review_intent")
        if intent is not None:
            try:
                families.add(review_family_for_intent(intent))
            except DispatchError:
                continue
    if len(families) > 1:
        raise ValueError("task identity has conflicting authenticated review families")
    return next(iter(families)) if families else _UNBOUND_TASK


def _trust_obligation(
    task: Mapping[str, object],
) -> tuple[
    str,
    str,
    tuple[str, ...],
    tuple[str, ...],
    tuple[str, ...],
    Mapping[str, object],
]:
    contract = task.get("task_contract")
    raw = (
        contract.get("trust_claim_task")
        if isinstance(contract, Mapping) and "trust_claim_task" in contract
        else task.get("trust_claim_task")
    )
    if not isinstance(raw, Mapping):
        raise ValueError("trust review requires a package trust claim task")
    raw_manifest = (
        contract.get("trust_claim_manifest")
        if isinstance(contract, Mapping) and "trust_claim_manifest" in contract
        else task.get("trust_claim_manifest")
    )
    if not isinstance(raw_manifest, Mapping):
        raise ValueError("trust review requires normalized manifest evidence")
    from .trust_claims import (
        TrustClaim,
        TrustClaimError,
        TrustClaimTaskV1,
        normalize_manifest,
    )

    try:
        normalized_manifest = normalize_manifest(raw_manifest)
    except TrustClaimError as exc:
        raise ValueError("trust manifest evidence is invalid") from exc
    manifest = canonical_record_digest(
        {
            "scheme": "trust-claim-manifest-v1",
            "claim_set": normalized_manifest.to_dict(),
        }
    )
    claim_set = normalized_manifest.claim_set_digest
    stated_manifest = raw.get("manifest_sha256")
    stated_claim_set = raw.get("claim_set_digest")
    task_hash = raw.get("task_hash")
    task_fields = {
        "generation_ref",
        "source_identity",
        "tree_sha",
        "manifest_sha256",
        "claim_set_digest",
        "invalidated_claims",
        "retirements",
        "carried_receipt_digests",
        "delta_from_tree_sha",
        "changed_paths",
        "changed_paths_digest",
        "base_moved",
        "risk_paths_added",
        "previous_receipt_digest",
        "task_hash",
    }
    if set(raw) != task_fields:
        raise ValueError("trust claim task fields are invalid")
    if any(
        not isinstance(value, str) or len(value) != 64 or re.fullmatch(r"[0-9a-f]{64}", value) is None
        for value in (stated_manifest, stated_claim_set, task_hash)
    ):
        raise ValueError("trust claim obligation digest is invalid")
    if stated_claim_set != claim_set:
        raise ValueError("trust claim set digest is not package-derived")
    parsed_claims: dict[str, tuple[TrustClaim, ...]] = {}

    def identities(name: str, *, current: bool) -> tuple[str, ...]:
        rows = raw.get(name)
        if not isinstance(rows, list) or not all(isinstance(row, Mapping) for row in rows):
            raise ValueError("trust claim scope is invalid")
        values: list[str] = []
        claims: list[TrustClaim] = []
        for row in rows:
            if set(row) != {"claim_id", "section", "text", "paths"} or not isinstance(
                row.get("paths"), list
            ):
                raise ValueError("trust claim scope is invalid")
            try:
                claim = TrustClaim(
                    section=cast(str, row.get("section")),
                    text=cast(str, row.get("text")),
                    paths=tuple(cast(list[str], row.get("paths"))),
                )
            except (TrustClaimError, TypeError, ValueError) as exc:
                raise ValueError("trust claim scope is invalid") from exc
            if row.get("claim_id") != claim.claim_id:
                raise ValueError("trust claim scope is invalid")
            if current and normalized_manifest.by_id.get(claim.claim_id) != claim:
                raise ValueError("trust invalidation is outside the current manifest")
            if not current and claim.claim_id in normalized_manifest.by_id:
                raise ValueError("trust retirement remains in the current manifest")
            values.append(claim.claim_id)
            claims.append(claim)
        values_tuple = tuple(sorted(values))
        if len(values_tuple) != len(set(values_tuple)) or any(
            re.fullmatch(r"tc_[0-9a-f]{64}", value) is None for value in values_tuple
        ):
            raise ValueError("trust claim scope is invalid")
        parsed_claims[name] = tuple(claims)
        return values_tuple

    invalidated = identities("invalidated_claims", current=True)
    retirements = identities("retirements", current=False)
    try:
        typed_task = TrustClaimTaskV1(
            generation_ref=cast(str, raw.get("generation_ref")),
            source_identity=cast(str, raw.get("source_identity")),
            tree_sha=cast(str, raw.get("tree_sha")),
            manifest_sha256=cast(str, stated_manifest),
            claim_set_digest=cast(str, stated_claim_set),
            invalidated_claims=parsed_claims["invalidated_claims"],
            retirements=parsed_claims["retirements"],
            carried_receipt_digests=tuple(
                cast(Sequence[str], raw.get("carried_receipt_digests"))
            ),
            delta_from_tree_sha=cast(str | None, raw.get("delta_from_tree_sha")),
            changed_paths=tuple(cast(Sequence[str], raw.get("changed_paths"))),
            changed_paths_digest=cast(str, raw.get("changed_paths_digest")),
            base_moved=cast(bool, raw.get("base_moved")),
            risk_paths_added=tuple(
                cast(Sequence[str], raw.get("risk_paths_added"))
            ),
            previous_receipt_digest=cast(
                str | None, raw.get("previous_receipt_digest")
            ),
        )
    except (KeyError, TrustClaimError, TypeError, ValueError) as exc:
        raise ValueError("trust claim task is invalid") from exc
    if typed_task.task_hash != task_hash:
        raise ValueError("trust claim task hash is invalid")

    return (
        cast(str, manifest),
        cast(str, claim_set),
        invalidated,
        retirements,
        tuple(sorted(normalized_manifest.by_id)),
        typed_task.to_dict(),
    )


def _released_retry_guard(
    records: Sequence[Mapping[str, object]],
    generation: ReviewGenerationV1,
    state: ReviewSlotState,
    family: Literal["delivery", "trust"],
    current_claim_ids: Sequence[str] = (),
) -> tuple[bool, tuple[str, ...], tuple[str, ...]]:
    """Return whether released work still requires a full primary boundary."""
    from ..runners.contract import ReviewOutcome

    coverages: dict[str, ReviewFamilyCoverageV1] = {}
    for record in authenticated_review_state_records(
        cast(Sequence[dict[str, object]], records)
    ):
        if record.get("type") != "review-family-coverage-v1":
            continue
        try:
            parsed = ReviewFamilyCoverageV1.from_dict(record)
        except (TypeError, ValueError):
            continue
        coverages[canonical_record_digest(parsed.to_dict())] = parsed

    projected_generations = generations(cast(Sequence[dict[str, object]], records))
    lineage: list[ReviewGenerationV1] = []
    cursor: ReviewGenerationV1 | None = generation
    seen: set[str] = set()
    while cursor is not None and cursor.generation_id not in seen:
        seen.add(cursor.generation_id)
        lineage.append(cursor)
        predecessor = projected_generations.get(cursor.predecessor_id)
        cursor = (
            cast(ReviewGenerationV1, predecessor)
            if predecessor is not None
            else None
        )
    entries: list[tuple[ReviewSlotState, ReviewSlotReservation]] = []
    for member in reversed(lineage):
        member_state = (
            state
            if member.generation_id == generation.generation_id
            else slot_state(
                cast(Sequence[dict[str, object]], records),
                member.generation_id,
                family,
            )
        )
        entries.extend(
            (member_state, reservation)
            for reservation in cast(
                Sequence[ReviewSlotReservation], member_state.reservations
            )
        )

    current_ids = set(current_claim_ids)
    unfinished_retirements: set[str] = set()
    terminal_refs = tuple(
        dict.fromkeys(
            settlement.terminal_ref
            for entry_state, reservation in entries
            if (
                settlement := entry_state.settlement_for(
                    reservation.reservation_id
                )
            )
            is not None
        )
    )
    guard = False
    for index, (reservation_state, reservation) in enumerate(entries):
        settlement = reservation_state.settlement_for(reservation.reservation_id)
        if settlement is None or reservation_state._effective_outcome(
            settlement
        ) is not (
            ReviewOutcome.RELEASED
        ):
            continue
        released_coverage = coverages.get(reservation.coverage_digest)
        released_scope = (
            set(released_coverage.invalidated_claim_ids).union(
                released_coverage.retirement_claim_ids
            )
            if released_coverage is not None
            else set()
        )
        later_primary_scopes: list[set[str]] = []
        later_consumed_claims: set[str] = set()
        for later_state, later in entries[index + 1 :]:
            later_settlement = later_state.settlement_for(later.reservation_id)
            if later_settlement is None or later_state._effective_outcome(
                later_settlement
            ) is not ReviewOutcome.CONSUMED:
                continue
            later_coverage = coverages.get(later.coverage_digest)
            later_scope = (
                set(later_coverage.invalidated_claim_ids).union(
                    later_coverage.retirement_claim_ids
                )
                if later_coverage is not None
                else set()
            )
            later_consumed_claims.update(later_scope)
            if later.slot_kind == "primary":
                later_primary_scopes.append(later_scope)

        primary_discharged = bool(
            later_primary_scopes
            and (
                family == "delivery"
                or (
                    released_coverage is not None
                    and released_coverage._claim_scope_bound
                    and any(
                        released_scope <= scope for scope in later_primary_scopes
                    )
                )
            )
        )
        if primary_discharged:
            continue
        if family == "delivery" or reservation.slot_kind == "delta":
            guard = True
        if family == "trust":
            unfinished = released_scope.difference(later_consumed_claims)
            if unfinished or released_coverage is None:
                guard = True
            unfinished_retirements.update(unfinished.difference(current_ids))
    return guard, tuple(sorted(unfinished_retirements)), terminal_refs


def admit_review(
    repository: Path,
    records: Sequence[Mapping[str, object]],
    *,
    repository_binding: str,
    task: Mapping[str, object],
    current_source_identity: Mapping[str, object],
    current_tree_sha: str,
    patch_identity: Mapping[str, object],
    required_sections: Sequence[str],
    equivalence_proof: object | None,
    format_only_proof: object | None,
    requested: RequestedReview,
    changed_paths: Sequence[str] | None,
    security_trigger_paths: Sequence[str],
    changed_paths_digest: str | None = None,
    diff_sha256: str | None = None,
    attempt_index: int = 0,
) -> Admission:
    """Resolve content, carry valid coverage, or reserve one bounded review."""
    assert_authority_ledger_lock_held(repository)
    if requested not in {"review", "delta"}:
        return _blocked("reservation-conflict", "unknown requested review kind")
    if not isinstance(task, Mapping) or not isinstance(
        current_source_identity, Mapping
    ):
        return _blocked("missing-evidence", "review source evidence is missing")
    try:
        patch_content_digest(patch_identity)
    except (AttributeError, ReviewStateError) as exc:
        return _blocked("missing-evidence", type(exc).__name__)
    task_source = _task_source(task)
    if task_source is not None and dict(task_source) != dict(current_source_identity):
        return _blocked(
            "stale-source",
            "task source no longer matches the current source identity",
        )
    identity_tree = patch_identity.get("candidate_tree_sha")
    current_head = current_source_identity.get("head")
    identity_head = patch_identity.get("candidate_sha")
    if identity_tree != current_tree_sha or (
        isinstance(current_head, str)
        and isinstance(identity_head, str)
        and current_head != identity_head
    ):
        return _blocked(
            "stale-source", "patch identity no longer matches the current source"
        )
    task_id = task.get("task_id")
    idempotency_key = task.get("idempotency_key")
    if (
        not isinstance(task_id, str)
        or not task_id
        or not isinstance(idempotency_key, str)
        or not idempotency_key
    ):
        return _blocked("missing-evidence", "task_id and idempotency_key are required")
    forbidden_labels = {
        name for name in ("launch_reason", "review_launch_reason") if name in task
    }
    if forbidden_labels:
        return _blocked(
            "invalid-launch-label",
            "review launch reasons are package-derived, not caller labels",
            fields=sorted(forbidden_labels),
        )
    try:
        intent = _review_intent(task)
        family = review_family_for_intent(intent)
        bound_family = _bound_task_family(records, task_id)
    except (ValueError, DispatchError) as exc:
        # DispatchError is intentionally reported through a content-free
        # admission class; caller labels must not appear in authority errors.
        return _blocked("unknown-intent", type(exc).__name__)
    if bound_family is not _UNBOUND_TASK and bound_family != family:
        return _blocked("unknown-intent", "ReviewIntentRelabelError")
    if family is None:
        if not isinstance(task.get("task_contract"), Mapping):
            return _blocked("missing-evidence", "ReviewIntentAuthorityError")
        if (
            isinstance(attempt_index, bool)
            or not isinstance(attempt_index, int)
            or attempt_index < 0
        ):
            return _blocked("missing-evidence", "review attempt index is invalid")
        scoped_task = {
            **dict(task),
            "source_identity": dict(current_source_identity),
            "snapshot_tree_sha": current_tree_sha,
            "patch_identity": dict(patch_identity),
            "attempt_index": attempt_index,
        }
        reason: LaunchReason = (
            "owner-requested" if intent == "resolution-adjudication" else "initial"
        )
        launch_record = {
            "type": "review-nonverdict-launch-v1",
            "schema_version": TELEMETRY_SCHEMA_VERSION,
            "policy_version": DISPATCH_POLICY_VERSION,
            "runtime_contract_version": RUNTIME_CONTRACT_VERSION,
            "task_id": task_id,
            "attempt_index": attempt_index,
            "attempt_id": f"{task_id}:{attempt_index}",
            "intent": intent,
            "reason": reason,
            "secondary_triggers": [],
            "admitted_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
            "source_identity_digest": canonical_record_digest(
                dict(current_source_identity)
            ),
            "patch_identity_digest": canonical_record_digest(dict(patch_identity)),
        }
        run_id = scoped_task.get("run_id")
        if not isinstance(run_id, str):
            contract = scoped_task.get("task_contract")
            run_id = contract.get("run_id") if isinstance(contract, Mapping) else None
        if isinstance(run_id, str) and run_id:
            launch_record["run_id"] = run_id
        return NonVerdictReviewLaunch(
            scoped_task=scoped_task,
            launch_record=launch_record,
            records_to_append=(launch_record,),
        )

    manifest_digest: str | None = None
    claim_set_digest: str | None = None
    invalidated_claim_ids: tuple[str, ...] = ()
    retirement_claim_ids: tuple[str, ...] = ()
    current_claim_ids: tuple[str, ...] | None = None
    trust_task: Mapping[str, object] | None = None
    if family == "trust":
        try:
            (
                manifest_digest,
                claim_set_digest,
                invalidated_claim_ids,
                retirement_claim_ids,
                current_claim_ids,
                trust_task,
            ) = _trust_obligation(task)
        except ValueError as exc:
            return _blocked("missing-evidence", type(exc).__name__)
    try:
        supplied_changed = (
            None
            if changed_paths is None
            else _canonical_paths(changed_paths, label="changed paths")
        )
        # Validate the legacy caller field, but never use it as authority. The
        # exact Git objects are classified below by the package-owned policy.
        _canonical_paths(security_trigger_paths, label="security trigger paths")
    except ValueError as exc:
        return _blocked("missing-evidence", str(exc))
    if (
        changed_paths_digest is not None
        and diff_sha256 is not None
        and changed_paths_digest != diff_sha256
    ):
        return _blocked("invalid-proof", "conflicting caller diff digests")
    supplied_diff_digest = (
        changed_paths_digest if changed_paths_digest is not None else diff_sha256
    )

    resolution = resolve_generation(
        records,
        repository_binding=repository_binding,
        patch_identity=patch_identity,
        tree_sha=current_tree_sha,
        required_sections=required_sections,
        equivalence_proof=equivalence_proof,
        format_only_proof=format_only_proof,
        family=family,
        manifest_digest=manifest_digest,
        claim_set_digest=claim_set_digest,
        invalidated_claim_ids=invalidated_claim_ids,
        retirement_claim_ids=retirement_claim_ids,
        current_claim_ids=current_claim_ids,
    )
    if resolution.kind == "refused" or resolution.generation is None:
        reason = str(resolution.reason or "missing-evidence")
        code: BlockedCode = (
            "invalid-proof" if reason == "invalid-proof" else "missing-evidence"
        )
        return _blocked(code, reason)
    generation = resolution.generation
    if trust_task is not None and (
        trust_task.get("generation_ref")
        != generation_id_for(
            repository_binding, patch_identity, current_tree_sha, required_sections
        )
        or trust_task.get("tree_sha") != current_tree_sha
        or trust_task.get("source_identity")
        != canonical_record_digest(dict(current_source_identity))
    ):
        return _blocked("invalid-proof", "trust claim task does not bind admission")
    requested_sections = tuple(sorted(set(required_sections)))
    prior_coverage = family_coverages(
        cast(Sequence[dict[str, object]], records)
    ).get((generation.generation_id, family))
    if (
        resolution.kind == "same"
        and prior_coverage is not None
        and prior_coverage.required_sections != requested_sections
    ):
        return _blocked(
            "missing-evidence",
            "required sections conflict with the existing content generation",
            generation_id=generation.generation_id,
        )
    carry_record = resolution.carry
    # A carry endpoint is the immediate reviewed content boundary.  Diffing a
    # later delta against the generation root would reintroduce base motion and
    # can falsely inflate a bounded repair into an oversized delta.
    diff_from_tree = None
    if carry_record is not None:
        candidate = carry_record.from_identity.get("candidate_tree_sha")
        diff_from_tree = candidate if isinstance(candidate, str) else None
    if diff_from_tree is None:
        diff_from_tree = generation.delta_from_tree
    if diff_from_tree is None:
        base_tree = patch_identity.get("base_tree_sha")
        diff_from_tree = base_tree if isinstance(base_tree, str) else None
    if diff_from_tree is None:
        if supplied_changed not in {None, ()} or supplied_diff_digest is not None:
            return _blocked(
                "invalid-proof", "changed paths have no kernel-verifiable source tree"
            )
        changed: tuple[str, ...] = ()
        actual_diff_digest: str | None = None
    else:
        try:
            changed, actual_diff_digest = tree_diff_paths(
                repository, diff_from_tree, current_tree_sha
            )
        except PatchIdentityError as exc:
            return _blocked("invalid-proof", str(exc))
        if supplied_changed is not None and (
            supplied_diff_digest != actual_diff_digest or supplied_changed != changed
        ):
            return _blocked(
                "invalid-proof",
                "caller path scope does not match the kernel tree diff",
                expected_diff_digest=actual_diff_digest,
            )
    security_from_tree = patch_identity.get("base_tree_sha")
    if not isinstance(security_from_tree, str):
        return _blocked("missing-evidence", "patch identity has no recorded base tree")
    try:
        _identity_paths, identity_diff_digest = tree_diff_paths(
            repository, security_from_tree, current_tree_sha
        )
        if identity_diff_digest != patch_identity.get("diff_sha256"):
            return _blocked(
                "invalid-proof",
                "patch identity diff does not match its recorded base tree",
                expected_diff_digest=identity_diff_digest,
            )
        security = _canonical_paths(
            security_trigger_paths_between(
                repository, security_from_tree, current_tree_sha
            ),
            label="kernel security trigger paths",
        )
    except (SecurityReviewScopeError, ValueError) as exc:
        return _blocked("invalid-proof", f"security classification failed: {exc}")
    if resolution.kind == "new":
        generation = replace(
            generation,
            delta_sha256=actual_diff_digest,
            changed_paths=changed,
            dependency_paths=tuple(sorted(set(security).difference(changed))),
        )
    coverage = resolution.coverage
    if coverage is None:
        return _blocked("missing-evidence", "review family coverage is missing")
    state = slot_state(
        cast(Sequence[dict[str, object]], records), generation.generation_id, family
    )
    coverage_digest = canonical_record_digest(coverage.to_dict())

    def current_coverage_receipts(slot_kind: str) -> tuple[str, ...]:
        from ..runners.contract import ReviewOutcome

        values: list[str] = []
        for reservation in state.reservations:
            if (
                reservation.slot_kind != slot_kind
                or reservation.coverage_digest != coverage_digest
            ):
                continue
            settlement = state.settlement_for(reservation.reservation_id)
            if settlement is not None and state._effective_outcome(settlement) is (
                ReviewOutcome.CONSUMED
            ):
                values.append(cast(str, settlement.terminal_ref))
        return tuple(dict.fromkeys(values))

    current_primary_receipts = current_coverage_receipts("primary")
    current_delta_receipts = current_coverage_receipts("delta")
    requested_obligation = (
        manifest_digest,
        claim_set_digest,
        invalidated_claim_ids,
        retirement_claim_ids,
    )
    if (
        family == "trust"
        and prior_coverage is not None
        and coverage.obligation_key != requested_obligation
        and not current_primary_receipts
        and not current_delta_receipts
    ):
        if coverage.obligation_key[:2] != (manifest_digest, claim_set_digest):
            return _blocked(
                "invalid-proof", "trust claim obligation does not match reservation"
            )
        prior_coverage_digest = canonical_record_digest(prior_coverage.to_dict())
        manifest_changed = prior_coverage.obligation_key[:2] != (
            manifest_digest,
            claim_set_digest,
        )
        current_ids = set(current_claim_ids or ())
        unfinished = set(prior_coverage.invalidated_claim_ids).union(
            prior_coverage.retirement_claim_ids
        )
        carried_invalidated = unfinished.intersection(current_ids)
        carried_retirements = unfinished.difference(current_ids)
        expected_invalidated = set(invalidated_claim_ids).union(
            carried_invalidated
        )
        expected_retirements = set(retirement_claim_ids).union(
            carried_retirements
        )
        changed_manifest_reconciliation = bool(
            manifest_changed
            and set(coverage.invalidated_claim_ids) == expected_invalidated
            and set(coverage.retirement_claim_ids) == expected_retirements
        )
        empty_same_manifest_retry = bool(
            not manifest_changed
            and not invalidated_claim_ids
            and not retirement_claim_ids
            and coverage.obligation_key == prior_coverage.obligation_key
        )
        if not (changed_manifest_reconciliation or empty_same_manifest_retry):
            return _blocked(
                "invalid-proof", "trust claim scope does not match obligation"
            )

        # An empty released retry inherits the exact persisted obligation.
        # Coverage supplies the claim identities and the normalized current
        # manifest supplies invalidated claim payloads. A releasing terminal's
        # package task is consulted only for retirement payloads, which cannot
        # be reconstructed from the current manifest. Pending retries remain
        # blocked rather than silently inheriting caller-omitted scope.
        current_trust_task = trust_task
        terminal_by_digest = {
            canonical_record_digest(record): record
            for record in records
            if isinstance(record, Mapping)
        }
        persisted_task: Mapping[str, object] | None = None
        released_reservation_found = False
        released_coverage_digest = (
            prior_coverage_digest
            if changed_manifest_reconciliation
            else coverage_digest
        )
        for persisted_reservation in reversed(state.reservations):
            if persisted_reservation.coverage_digest != released_coverage_digest:
                continue
            settlement = state.settlement_for(
                persisted_reservation.reservation_id
            )
            if settlement is None or state._effective_outcome(settlement).value != (
                "released"
            ):
                continue
            released_reservation_found = True
            released_terminal = terminal_by_digest.get(settlement.terminal_ref)
            released_contract = (
                released_terminal.get("task_contract")
                if isinstance(released_terminal, Mapping)
                else None
            )
            if not isinstance(released_contract, Mapping):
                continue
            try:
                (
                    persisted_manifest,
                    persisted_claim_set,
                    persisted_invalidated,
                    persisted_retirements,
                    _persisted_current_claim_ids,
                    candidate_task,
                ) = _trust_obligation(released_contract)
            except ValueError:
                continue
            persisted_obligation = (
                persisted_manifest,
                persisted_claim_set,
                persisted_invalidated,
                persisted_retirements,
            )
            expected_persisted_obligation = (
                prior_coverage.obligation_key
                if changed_manifest_reconciliation
                else coverage.obligation_key
            )
            if persisted_obligation == expected_persisted_obligation:
                persisted_task = candidate_task
                break
        raw_manifest = (
            task.get("task_contract", {}).get("trust_claim_manifest")
            if isinstance(task.get("task_contract"), Mapping)
            and "trust_claim_manifest" in task["task_contract"]
            else task.get("trust_claim_manifest")
        )
        try:
            from .trust_claims import normalize_manifest

            current_claims = (
                normalize_manifest(cast(Mapping[object, object], raw_manifest)).by_id
                if isinstance(raw_manifest, Mapping)
                else {}
            )
        except (TypeError, ValueError):
            current_claims = {}
        invalidated_rows = [
            current_claims[claim_id].to_dict()
            for claim_id in coverage.invalidated_claim_ids
            if claim_id in current_claims
        ]
        current_retirement_rows = (
            current_trust_task.get("retirements", [])
            if isinstance(current_trust_task, Mapping)
            else []
        )
        persisted_retirements = {
            row.get("claim_id"): dict(row)
            for row in (
                *cast(Sequence[object], current_retirement_rows),
                *cast(
                    Sequence[object],
                    persisted_task.get("retirements", [])
                    if isinstance(persisted_task, Mapping)
                    else [],
                ),
                *cast(
                    Sequence[object],
                    persisted_task.get("invalidated_claims", [])
                    if isinstance(persisted_task, Mapping)
                    else [],
                ),
            )
            if isinstance(row, Mapping) and isinstance(row.get("claim_id"), str)
        }
        retirement_rows = [
            persisted_retirements[claim_id]
            for claim_id in coverage.retirement_claim_ids
            if claim_id in persisted_retirements
        ]
        if (
            not released_reservation_found
            or current_trust_task is None
            or len(invalidated_rows) != len(coverage.invalidated_claim_ids)
            or len(retirement_rows) != len(coverage.retirement_claim_ids)
        ):
            return _blocked(
                "invalid-proof", "trust claim scope does not match obligation"
            )
        rebuilt_task = {
            **dict(current_trust_task),
            "invalidated_claims": invalidated_rows,
            "retirements": retirement_rows,
        }
        if set(coverage.invalidated_claim_ids) == set(current_claim_ids or ()):
            rebuilt_task["carried_receipt_digests"] = []
        rebuilt_task.pop("task_hash", None)
        rebuilt_task["task_hash"] = canonical_record_digest(rebuilt_task)
        invalidated_claim_ids = coverage.invalidated_claim_ids
        retirement_claim_ids = coverage.retirement_claim_ids
        trust_task = rebuilt_task
    inherited_primary = coverage.primary_origin_receipt
    has_primary = state.primary_consumed or inherited_primary is not None
    append_before_slot: list[Mapping[str, object]] = []
    carry_proof = carry_record.proof if carry_record is not None else None
    if isinstance(carry_proof, Mapping) and isinstance(
        carry_proof.get("proof"), Mapping
    ):
        carry_proof = cast(Mapping[str, object], carry_proof["proof"])
    trust_base_overlap = bool(
        family == "trust"
        and isinstance(carry_proof, Mapping)
        and isinstance(carry_proof.get("base_path_overlap"), list)
        and carry_proof["base_path_overlap"]
    )
    trust_claim_invalidation = bool(
        family == "trust" and (invalidated_claim_ids or retirement_claim_ids)
    )
    if (
        carry_record is not None
        and "security" in carry_record.sections
        and isinstance(carry_proof, Mapping)
        and isinstance(carry_proof.get("base_path_overlap"), list)
        and set(cast(list[object], carry_proof["base_path_overlap"])).intersection(
            security
        )
    ):
        # Base motion across a security-trigger path keeps content lineage but
        # cannot carry that section's coverage.
        carry_record = GenerationCarryV1(
            generation_id=carry_record.generation_id,
            from_identity=carry_record.from_identity,
            to_identity=carry_record.to_identity,
            proof=carry_record.proof,
            sections=tuple(
                section for section in carry_record.sections if section != "security"
            ),
            family=carry_record.family,
        )
    if carry_record is not None and (
        trust_base_overlap or trust_claim_invalidation
    ):
        # Trust coverage includes the current claim obligation. Base motion and
        # an explicit invalidation both require a fresh claim-scoped delta even
        # when patch equivalence proves identical candidate bytes.
        carry_record = GenerationCarryV1(
            generation_id=carry_record.generation_id,
            from_identity=carry_record.from_identity,
            to_identity=carry_record.to_identity,
            proof=carry_record.proof,
            sections=(),
            family=carry_record.family,
        )
    if resolution.kind == "new":
        append_before_slot.append(generation.to_dict())
    elif (
        carry_record is not None
        and carry_record.from_identity != carry_record.to_identity
    ):
        append_before_slot.append(carry_record.to_dict())
    if prior_coverage != coverage:
        append_before_slot.append(coverage.to_dict())

    # Equivalence, format-only, and exact-content lookup are all zero author
    # churn.  A standing consumed primary is therefore a content fact and no
    # dispatcher call is admitted.
    complete_carry = bool(
        carry_record is not None
        and set(coverage.required_sections) <= set(carry_record.sections)
    )
    own_primary_consumed = bool(
        (
            bool(current_primary_receipts)
            if family == "trust"
            else state.own_primary_consumed
        )
        and (
            not coverage.invalidated_sections
            or (
                bool(current_delta_receipts)
                if family == "trust"
                else state.delta_consumed
            )
        )
    )
    inherited_coverage_complete = bool(
        inherited_primary is not None
        and (
            not coverage.invalidated_sections
            or (
                bool(current_delta_receipts)
                if family == "trust"
                else state.delta_consumed
            )
        )
    )
    carry_eligible = bool(
        resolution.kind == "same"
        and (own_primary_consumed or inherited_coverage_complete)
        and complete_carry
        and state.outstanding is None
    )
    guard_eligible = bool(
        carry_eligible
        or (
            resolution.kind == "new"
            and inherited_primary is not None
            and state.outstanding is None
        )
    )
    released_retry_reverification = False
    if guard_eligible:
        (
            released_retry_reverification,
            unfinished_retirement_ids,
            obligation_terminal_refs,
        ) = _released_retry_guard(
            records,
            generation,
            state,
            family,
            current_claim_ids or (),
        )
        if released_retry_reverification:
            if resolution.kind == "new":
                generation = replace(
                    generation,
                    primary_origin_receipt=None,
                    inherited_coverage=(),
                    invalidated_sections=(),
                )
                append_before_slot = [
                    generation.to_dict()
                    if record.get("type") == "review-generation-v1"
                    and record.get("generation_id") == generation.generation_id
                    else record
                    for record in append_before_slot
                ]
            reset_coverage = replace(
                coverage,
                primary_origin_receipt=None,
                inherited_coverage=(),
                invalidated_sections=(),
            )
            if family == "delivery" and reset_coverage != coverage:
                coverage = reset_coverage
                append_before_slot.append(coverage.to_dict())
                coverage_digest = canonical_record_digest(coverage.to_dict())
        if released_retry_reverification and family == "trust":
            if trust_task is None or current_claim_ids is None:
                return _blocked(
                    "missing-evidence",
                    "full trust reverification has no current claim set",
                    records=append_before_slot,
                )
            raw_manifest = (
                task.get("task_contract", {}).get("trust_claim_manifest")
                if isinstance(task.get("task_contract"), Mapping)
                and "trust_claim_manifest" in task["task_contract"]
                else task.get("trust_claim_manifest")
            )
            try:
                from .trust_claims import normalize_manifest

                current_claims = normalize_manifest(
                    cast(Mapping[object, object], raw_manifest)
                ).by_id
            except (TypeError, ValueError):
                current_claims = {}
            invalidated_rows = [
                current_claims[claim_id].to_dict()
                for claim_id in current_claim_ids
                if claim_id in current_claims
            ]
            terminal_by_digest = {
                canonical_record_digest(record): record
                for record in records
                if isinstance(record, Mapping)
            }
            retirement_rows_by_id = {
                row.get("claim_id"): dict(row)
                for row in cast(Sequence[object], trust_task.get("retirements", []))
                if isinstance(row, Mapping) and isinstance(row.get("claim_id"), str)
            }
            for terminal_ref in obligation_terminal_refs:
                terminal = terminal_by_digest.get(terminal_ref)
                contract = (
                    terminal.get("task_contract")
                    if isinstance(terminal, Mapping)
                    else None
                )
                if not isinstance(contract, Mapping):
                    continue
                try:
                    *_, persisted_task = _trust_obligation(contract)
                except ValueError:
                    continue
                for row in (
                    *cast(
                        Sequence[object],
                        persisted_task.get("invalidated_claims", []),
                    ),
                    *cast(
                        Sequence[object], persisted_task.get("retirements", [])
                    ),
                ):
                    if isinstance(row, Mapping) and isinstance(
                        row.get("claim_id"), str
                    ):
                        retirement_rows_by_id[cast(str, row["claim_id"])] = dict(row)
            retirement_rows = [
                retirement_rows_by_id[claim_id]
                for claim_id in unfinished_retirement_ids
                if claim_id in retirement_rows_by_id
            ]
            if (
                len(invalidated_rows) != len(current_claim_ids)
                or len(retirement_rows) != len(unfinished_retirement_ids)
            ):
                return _blocked(
                    "missing-evidence",
                    "full trust reverification scope is incomplete",
                    records=append_before_slot,
                )
            rebuilt_task = {
                **dict(trust_task),
                "invalidated_claims": invalidated_rows,
                "retirements": retirement_rows,
                "carried_receipt_digests": [],
            }
            rebuilt_task.pop("task_hash", None)
            rebuilt_task["task_hash"] = canonical_record_digest(rebuilt_task)
            trust_task = rebuilt_task
            invalidated_claim_ids = current_claim_ids
            retirement_claim_ids = unfinished_retirement_ids
            full_coverage = replace(
                reset_coverage,
                invalidated_claim_ids=current_claim_ids,
                retirement_claim_ids=unfinished_retirement_ids,
            )
            if full_coverage != coverage:
                coverage = full_coverage
                append_before_slot.append(coverage.to_dict())
                coverage_digest = canonical_record_digest(coverage.to_dict())
        if carry_eligible and not released_retry_reverification:
            receipts = (
                tuple(
                    dict.fromkeys(
                        (
                            *((inherited_primary,) if inherited_primary is not None else ()),
                            *current_primary_receipts,
                            *current_delta_receipts,
                        )
                    )
                )
                if family == "trust"
                else state.receipts
            )
            if inherited_primary is not None and inherited_primary not in receipts:
                receipts = (inherited_primary, *receipts)
            if not receipts:
                return _blocked(
                    "missing-evidence",
                    "generation primary verdict has no authenticated receipt",
                    records=append_before_slot,
                )
            from .authority import authenticated_review_verdict

            terminal_by_digest = {
                canonical_record_digest(record): record
                for record in records
                if isinstance(record, Mapping)
            }
            verdicts: list[tuple[str, Literal["pass", "fail"]]] = []
            native_generation_ids = {
                record.get("generation_id")
                for record in authenticated_review_state_records(
                    cast(Sequence[dict[str, object]], records)
                )
                if record.get("type") == "review-generation-v1"
            }
            legacy_delivery = (
                family == "delivery"
                and generation.generation_id not in native_generation_ids
            )
            for receipt in receipts:
                verdict = authenticated_review_verdict(
                    terminal_by_digest.get(receipt),
                    records,
                    expected_family=family,
                    allow_missing_intent=legacy_delivery,
                )
                if verdict not in {"pass", "fail"}:
                    return _blocked(
                        "missing-evidence",
                        "generation receipt has no authenticated verdict",
                        records=append_before_slot,
                        terminal_ref=receipt,
                    )
                verdicts.append((receipt, cast(Literal["pass", "fail"], verdict)))
            return Carry(
                generation=generation,
                receipts=receipts,
                verdicts=tuple(verdicts),
                carry_record=cast(GenerationCarryV1, carry_record),
                records_to_append=tuple(append_before_slot),
            )

    closure = tuple(sorted(set(changed).union(security)))
    trust_obligation_delta = bool(
        family == "trust"
        and (
            (
                prior_coverage is not None
                and prior_coverage.obligation_key != coverage.obligation_key
            )
            or trust_base_overlap
            or trust_claim_invalidation
        )
    )
    if (
        has_primary
        and requested == "review"
        and resolution.kind == "new"
        and len(closure) > MAX_DELTA_SCOPE_PATHS
    ):
        # A bounded delta cannot cover this successor.  An explicit full-review
        # request may detach it into a fresh lineage, but only after the kernel
        # has measured the same oversized scope; small repairs cannot widen.
        generation = fresh_review_generation(generation)
        coverage = replace(
            coverage,
            primary_origin_receipt=None,
            inherited_coverage=(),
            invalidated_sections=(),
        )
        state = slot_state(
            cast(Sequence[dict[str, object]], records),
            generation.generation_id,
            family,
        )
        inherited_primary = None
        has_primary = state.primary_consumed
        append_before_slot = [generation.to_dict(), coverage.to_dict()]
        coverage_digest = canonical_record_digest(coverage.to_dict())

    if (
        not released_retry_reverification
        and has_primary
        and requested != "delta"
        and not trust_obligation_delta
    ):
        return _blocked(
            "slots-exhausted",
            "generation already has a primary; only a delta is admissible",
            records=append_before_slot,
        )
    if (
        not released_retry_reverification
        and not has_primary
        and requested == "delta"
    ):
        return _blocked(
            "missing-evidence",
            "delta review requires exactly one settled primary",
            records=append_before_slot,
        )

    slot_kind: Literal["primary", "delta"] = (
        "primary"
        if released_retry_reverification
        else "delta"
        if has_primary
        else "primary"
    )
    delta_scope: dict[str, object] | None = None
    if slot_kind == "delta":
        if len(closure) > MAX_DELTA_SCOPE_PATHS:
            return _blocked(
                "oversized-delta",
                "delta closure exceeds the fixed path bound",
                records=append_before_slot,
                path_count=len(closure),
                maximum=MAX_DELTA_SCOPE_PATHS,
            )
        from_tree = diff_from_tree
        if not isinstance(from_tree, str):
            return _blocked(
                "missing-evidence",
                "delta generation has no predecessor tree",
                records=append_before_slot,
            )
        delta_scope = {
            "from_tree": from_tree,
            "to_tree": current_tree_sha,
            "changed_paths": list(changed),
            "closure_paths": list(closure),
        }
        if family == "trust":
            delta_scope["invalidated_claim_ids"] = list(invalidated_claim_ids)
            delta_scope["retirement_claim_ids"] = list(retirement_claim_ids)
    try:
        reservation = _reserve_review_slot(
            records,
            generation_id=generation.generation_id,
            family=family,
            slot_kind=slot_kind,
            task_id=task_id,
            idempotency_key=idempotency_key,
            coverage_digest=coverage_digest,
            prospective_generation=(generation if resolution.kind == "new" else None),
            prospective_inherited_primary=(inherited_primary is not None),
            released_retry_reverification=released_retry_reverification,
        )
    except ReviewSlotError as exc:
        code = cast(BlockedCode, exc.code)
        return _blocked(code, str(exc), records=append_before_slot)
    if reservation.existing and reservation.conflict:
        return _blocked(
            "slot-held",
            "review family already has an outstanding reservation",
            records=append_before_slot,
            reservation_id=reservation.reservation_id,
        )

    scoped_task_values = dict(task)
    if trust_task is not None:
        contract = scoped_task_values.get("task_contract")
        if isinstance(contract, Mapping) and "trust_claim_task" in contract:
            scoped_task_values["task_contract"] = {
                **dict(contract),
                "trust_claim_task": dict(trust_task),
            }
        scoped_task_values["trust_claim_task"] = dict(trust_task)
    scoped_task = {
        **scoped_task_values,
        "source_identity": dict(current_source_identity),
        "snapshot_tree_sha": current_tree_sha,
        "patch_identity": dict(patch_identity),
        "required_sections": list(coverage.required_sections),
        "review_lineage_id": generation.lineage_id,
        "review_generation_id": generation.generation_id,
        "review_family": family,
        "review_slot_kind": slot_kind,
        "review_reservation_id": reservation.reservation_id,
    }
    if delta_scope is not None:
        scoped_task["delta_scope"] = delta_scope
    to_append = list(append_before_slot)
    if not reservation.existing:
        to_append.append(reservation.to_dict())
    transition_kind = (
        resolution.transition.kind if resolution.transition is not None else None
    )
    prior_release = any(
        settlement.slot_kind == slot_kind
        and state._effective_outcome(settlement).value == "released"
        for settlement in state.settlements
    )
    launch_reason, secondary_triggers = _launch_reasons(
        family=family,
        slot_kind=slot_kind,
        transition_kind=transition_kind,
        security_triggered=bool(security),
        infrastructure_retry=prior_release,
    )
    return Reserved(
        generation=generation,
        slot=reservation,
        scoped_task=scoped_task,
        records_to_append=tuple(to_append),
        launch_reason=launch_reason,
        secondary_triggers=secondary_triggers,
    )


__all__ = [
    "Admission",
    "Blocked",
    "Carry",
    "LaunchReason",
    "NonVerdictReviewLaunch",
    "Reserved",
    "admit_review",
]
