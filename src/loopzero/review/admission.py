"""Shared, storage-agnostic admission for content-addressed reviews.

The import dependency closure intentionally is not computed in this release.
``closure_paths`` is the exact union of changed paths and security-trigger
paths; the repository-aware closure belongs in a later ``review/scope.py``.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from pathlib import PurePosixPath
from typing import Literal, cast

from ..kernel.authority_projection import slot_state
from ..kernel.review_state import (
    GenerationCarryV1,
    ReviewGenerationV1,
    ReviewSlotError,
    ReviewSlotReservation,
    _reserve_review_slot,
    resolve_generation,
)

BlockedCode = Literal[
    "stale-source",
    "invalid-proof",
    "missing-evidence",
    "slot-held",
    "slots-exhausted",
    "retries-exhausted",
    "oversized-delta",
    "reservation-conflict",
]
RequestedReview = Literal["review", "delta"]

MAX_DELTA_SCOPE_PATHS = 300


@dataclass(frozen=True, slots=True)
class Carry:
    generation: ReviewGenerationV1
    receipts: tuple[str, ...]
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


Admission = Carry | Reserved | Blocked


def _blocked(
    code: BlockedCode,
    message: str,
    *,
    records: Sequence[Mapping[str, object]] = (),
    **evidence: object,
) -> Blocked:
    return Blocked(
        code,
        {"message": message, **evidence},
        tuple(records),
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


def _review_family(task: Mapping[str, object]) -> Literal["delivery", "trust"]:
    explicit = task.get("review_family") or task.get("family")
    if explicit in {"delivery", "trust"}:
        return cast(Literal["delivery", "trust"], explicit)
    contract = task.get("task_contract")
    intent = (
        contract.get("review_intent")
        if isinstance(contract, Mapping)
        else task.get("review_intent")
    )
    return "trust" if intent == "trust-manifest-verification" else "delivery"


def admit_review(
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
    changed_paths: Sequence[str],
    security_trigger_paths: Sequence[str],
) -> Admission:
    """Resolve content, carry valid coverage, or reserve one bounded review."""
    if requested not in {"review", "delta"}:
        return _blocked("reservation-conflict", "unknown requested review kind")
    if not isinstance(task, Mapping) or not isinstance(
        current_source_identity, Mapping
    ):
        return _blocked("missing-evidence", "review source evidence is missing")
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
    if not isinstance(task_id, str) or not task_id or not isinstance(
        idempotency_key, str
    ) or not idempotency_key:
        return _blocked(
            "missing-evidence", "task_id and idempotency_key are required"
        )
    try:
        changed = _canonical_paths(changed_paths, label="changed paths")
        security = _canonical_paths(
            security_trigger_paths, label="security trigger paths"
        )
    except ValueError as exc:
        return _blocked("missing-evidence", str(exc))

    resolution = resolve_generation(
        records,
        repository_binding=repository_binding,
        patch_identity=patch_identity,
        tree_sha=current_tree_sha,
        required_sections=required_sections,
        equivalence_proof=equivalence_proof,
        format_only_proof=format_only_proof,
    )
    if resolution.kind == "refused" or resolution.generation is None:
        reason = str(resolution.reason or "missing-evidence")
        code: BlockedCode = (
            "invalid-proof" if reason == "invalid-proof" else "missing-evidence"
        )
        return _blocked(code, reason)
    generation = resolution.generation
    if resolution.kind == "new":
        generation = replace(
            generation,
            changed_paths=changed,
            dependency_paths=tuple(
                sorted(set(security).difference(changed))
            ),
        )
    family = _review_family(task)
    state = slot_state(
        cast(Sequence[dict[str, object]], records), generation.generation_id, family
    )
    inherited_primary = (
        generation.primary_origin_receipt if family == "delivery" else None
    )
    if (
        inherited_primary is None
        and resolution.kind == "new"
        and generation.predecessor_id is not None
    ):
        inherited_primary = slot_state(
            cast(Sequence[dict[str, object]], records),
            generation.predecessor_id,
            family,
        ).primary_terminal_ref
    has_primary = state.primary_consumed or inherited_primary is not None
    append_before_slot: list[Mapping[str, object]] = []
    carry_record = resolution.carry
    if (
        carry_record is not None
        and "security" in carry_record.sections
        and isinstance(equivalence_proof, Mapping)
        and isinstance(equivalence_proof.get("base_path_overlap"), list)
        and set(cast(list[object], equivalence_proof["base_path_overlap"]))
        .intersection(security)
    ):
        # Base motion across a security-trigger path keeps content lineage but
        # cannot carry that section's coverage.
        carry_record = GenerationCarryV1(
            generation_id=carry_record.generation_id,
            from_identity=carry_record.from_identity,
            to_identity=carry_record.to_identity,
            proof=carry_record.proof,
            sections=tuple(
                section for section in carry_record.sections
                if section != "security"
            ),
        )
    if resolution.kind == "new":
        append_before_slot.append(generation.to_dict())
    elif carry_record is not None:
        append_before_slot.append(carry_record.to_dict())

    # Equivalence, format-only, and exact-content lookup are all zero author
    # churn.  A standing consumed primary is therefore a content fact and no
    # dispatcher call is admitted.
    complete_carry = bool(
        carry_record is not None
        and set(generation.required_sections) <= set(carry_record.sections)
    )
    if resolution.kind == "same" and has_primary and complete_carry:
        receipts = state.receipts
        if inherited_primary is not None and inherited_primary not in receipts:
            receipts = (inherited_primary, *receipts)
        if not receipts:
            return _blocked(
                "missing-evidence",
                "generation primary verdict has no authenticated receipt",
                records=append_before_slot,
            )
        return Carry(
            generation=generation,
            receipts=receipts,
            carry_record=cast(GenerationCarryV1, carry_record),
            records_to_append=tuple(append_before_slot),
        )

    if has_primary and requested != "delta":
        return _blocked(
            "slots-exhausted",
            "generation already has a primary; only a delta is admissible",
            records=append_before_slot,
        )
    if not has_primary and requested == "delta":
        return _blocked(
            "missing-evidence",
            "delta review requires exactly one settled primary",
            records=append_before_slot,
        )

    slot_kind: Literal["primary", "delta"] = "delta" if has_primary else "primary"
    delta_scope: dict[str, object] | None = None
    if slot_kind == "delta":
        closure = tuple(sorted(set(changed).union(security)))
        if len(closure) > MAX_DELTA_SCOPE_PATHS:
            return _blocked(
                "oversized-delta",
                "delta closure exceeds the fixed path bound",
                records=append_before_slot,
                path_count=len(closure),
                maximum=MAX_DELTA_SCOPE_PATHS,
            )
        from_tree = generation.delta_from_tree
        if from_tree is None and carry_record is not None:
            candidate = carry_record.from_identity.get("candidate_tree_sha")
            from_tree = candidate if isinstance(candidate, str) else None
        if from_tree is None and resolution.kind == "same":
            from_tree = generation.tree
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
    try:
        reservation = _reserve_review_slot(
            records,
            generation_id=generation.generation_id,
            family=family,
            slot_kind=slot_kind,
            task_id=task_id,
            idempotency_key=idempotency_key,
            prospective_generation=(generation if resolution.kind == "new" else None),
        )
    except ReviewSlotError as exc:
        code = cast(BlockedCode, exc.code)
        return _blocked(code, str(exc), records=append_before_slot)
    if reservation.existing:
        outstanding = state.outstanding
        code: BlockedCode = (
            "slot-held"
            if outstanding is not None
            and outstanding.reservation_id == reservation.reservation_id
            else "reservation-conflict"
        )
        return _blocked(
            code,
            (
                "review family already has an outstanding reservation"
                if code == "slot-held"
                else "idempotent review reservation is already settled"
            ),
            records=append_before_slot,
            reservation_id=reservation.reservation_id,
        )

    scoped_task = {
        **dict(task),
        "source_identity": dict(current_source_identity),
        "snapshot_tree_sha": current_tree_sha,
        "patch_identity": dict(patch_identity),
        "required_sections": list(generation.required_sections),
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
    return Reserved(
        generation=generation,
        slot=reservation,
        scoped_task=scoped_task,
        records_to_append=tuple(to_append),
    )


__all__ = ["Admission", "Blocked", "Carry", "Reserved", "admit_review"]
