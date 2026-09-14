"""Content-addressed review generations and durable review-slot authority.

This module is deliberately storage agnostic.  A caller that changes slot state
must hold :func:`authority_store.authority_ledger_lock`, reload the authority
history while holding it, call the resolver/reservation operation, coordinator-
sign every returned record, append and fsync those records, and only then drop
the lock.  The lock must never be held while a model is running.  Reservations
have no time based expiry.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal, cast

from .canonical import canonical_json_bytes, canonical_record_digest
from .patch_identity import equivalence_receipt_is_valid
from ..runners.contract import ReviewOutcome

REVIEW_GENERATION_TYPE = "review-generation-v1"
GENERATION_CARRY_TYPE = "generation-carry-v1"
REVIEW_SLOT_RESERVATION_TYPE = "review-slot-reservation-v1"
REVIEW_SLOT_SETTLEMENT_TYPE = "review-slot-settlement-v1"

GenerationTransitionKind = Literal[
    "initial", "substantive", "supersession", "owner-requested"
]
GenerationResolutionKind = Literal["same", "new", "refused"]
ReviewFamily = Literal["delivery", "trust"]
ReviewSlotKind = Literal["primary", "delta"]

_OID_RE = re.compile(r"[0-9a-f]{40,64}")
_SHA256_RE = re.compile(r"[0-9a-f]{64}")


class ReviewStateError(ValueError):
    """A review-state record or state transition is invalid."""


class ReviewSlotError(ReviewStateError):
    """Typed slot admission failure used by the review admission facade."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _digest(value: object) -> str:
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def patch_identity_digest(identity: Mapping[str, object]) -> str:
    """Return the canonical digest used by generation and proof identities."""
    return _digest(dict(identity))


def _frozen_strings(values: Sequence[str], *, label: str) -> tuple[str, ...]:
    if isinstance(values, (str, bytes)) or not all(
        isinstance(value, str) and value for value in values
    ):
        raise ReviewStateError(f"{label} must be a string sequence")
    if len(values) != len(set(values)):
        raise ReviewStateError(f"{label} must be unique")
    return tuple(sorted(values))


def _validate_identity(identity: Mapping[str, object], tree: str) -> dict[str, object]:
    if not isinstance(identity, Mapping) or not identity:
        raise ReviewStateError("patch identity is missing")
    candidate_tree = identity.get("candidate_tree_sha")
    if not isinstance(candidate_tree, str) or candidate_tree != tree:
        raise ReviewStateError("patch identity does not bind the current tree")
    if _OID_RE.fullmatch(tree) is None:
        raise ReviewStateError("review generation tree is invalid")
    return dict(identity)


def generation_id_for(
    repository_binding: str,
    patch_identity: Mapping[str, object],
    tree: str,
    required_sections: Sequence[str],
) -> str:
    """Derive the D29 content-generation identifier exactly once."""
    sections = _frozen_strings(required_sections, label="required sections")
    return "cg_" + _digest(
        [
            "review-generation-v1",
            repository_binding,
            patch_identity_digest(patch_identity),
            tree,
            list(sections),
        ]
    )[:32]


def _lineage_id(
    repository_binding: str,
    patch_identity: Mapping[str, object],
    tree: str,
) -> str:
    return "rl_" + _digest(
        [
            "review-lineage-v1",
            repository_binding,
            patch_identity_digest(patch_identity),
            tree,
        ]
    )[:32]


def _policy_digest(required_sections: Sequence[str]) -> str:
    return _digest(["review-generation-policy-v1", list(required_sections)])


@dataclass(frozen=True, slots=True)
class ReviewGenerationV1:
    repository_binding: str
    lineage_id: str
    generation_id: str
    predecessor_id: str | None
    patch_identity: Mapping[str, object] = field(repr=False)
    tree: str
    required_sections: tuple[str, ...]
    policy_digest: str
    delta_from_tree: str | None
    delta_sha256: str | None
    changed_paths: tuple[str, ...]
    dependency_paths: tuple[str, ...]
    primary_origin_receipt: str | None
    inherited_coverage: tuple[str, ...]
    invalidated_sections: tuple[str, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.repository_binding, str) or not self.repository_binding:
            raise ReviewStateError("repository binding is invalid")
        if not isinstance(self.lineage_id, str) or not re.fullmatch(
            r"rl_[0-9a-f]{32}", self.lineage_id
        ):
            raise ReviewStateError("review lineage id is invalid")
        if not isinstance(self.generation_id, str) or not re.fullmatch(
            r"cg_[0-9a-f]{32}", self.generation_id
        ):
            raise ReviewStateError("review generation id is invalid")
        if self.predecessor_id is not None and not re.fullmatch(
            r"cg_[0-9a-f]{32}", self.predecessor_id
        ):
            raise ReviewStateError("review generation predecessor is invalid")
        identity = _validate_identity(self.patch_identity, self.tree)
        sections = _frozen_strings(self.required_sections, label="required sections")
        changed = _frozen_strings(self.changed_paths, label="changed paths")
        dependencies = _frozen_strings(
            self.dependency_paths, label="dependency paths"
        )
        inherited = _frozen_strings(
            self.inherited_coverage, label="inherited coverage"
        )
        invalidated = _frozen_strings(
            self.invalidated_sections, label="invalidated sections"
        )
        if not (set(inherited) | set(invalidated)) <= set(sections):
            raise ReviewStateError("generation coverage names an unrequired section")
        if not isinstance(self.policy_digest, str) or not _SHA256_RE.fullmatch(
            self.policy_digest
        ):
            raise ReviewStateError("generation policy digest is invalid")
        if self.delta_from_tree is not None and _OID_RE.fullmatch(
            self.delta_from_tree
        ) is None:
            raise ReviewStateError("generation delta source tree is invalid")
        if self.delta_sha256 is not None and _SHA256_RE.fullmatch(
            self.delta_sha256
        ) is None:
            raise ReviewStateError("generation delta digest is invalid")
        if self.primary_origin_receipt is not None and not isinstance(
            self.primary_origin_receipt, str
        ):
            raise ReviewStateError("generation primary receipt is invalid")
        if self.generation_id != generation_id_for(
            self.repository_binding, identity, self.tree, sections
        ):
            raise ReviewStateError("review generation id does not match content")
        object.__setattr__(self, "patch_identity", identity)
        object.__setattr__(self, "required_sections", sections)
        object.__setattr__(self, "changed_paths", changed)
        object.__setattr__(self, "dependency_paths", dependencies)
        object.__setattr__(self, "inherited_coverage", inherited)
        object.__setattr__(self, "invalidated_sections", invalidated)

    def to_dict(self) -> dict[str, object]:
        return {
            "type": REVIEW_GENERATION_TYPE,
            "repository_binding": self.repository_binding,
            "lineage_id": self.lineage_id,
            "generation_id": self.generation_id,
            "predecessor_id": self.predecessor_id,
            "patch_identity": dict(self.patch_identity),
            "tree": self.tree,
            "required_sections": list(self.required_sections),
            "policy_digest": self.policy_digest,
            "delta_from_tree": self.delta_from_tree,
            "delta_sha256": self.delta_sha256,
            "changed_paths": list(self.changed_paths),
            "dependency_paths": list(self.dependency_paths),
            "primary_origin_receipt": self.primary_origin_receipt,
            "inherited_coverage": list(self.inherited_coverage),
            "invalidated_sections": list(self.invalidated_sections),
        }

    to_json = to_dict
    to_record = to_dict

    def canonical_digest(self) -> str:
        return canonical_record_digest(self.to_dict())

    @classmethod
    def from_dict(cls, record: Mapping[str, object]) -> ReviewGenerationV1:
        expected = {
            "type",
            "repository_binding",
            "lineage_id",
            "generation_id",
            "predecessor_id",
            "patch_identity",
            "tree",
            "required_sections",
            "policy_digest",
            "delta_from_tree",
            "delta_sha256",
            "changed_paths",
            "dependency_paths",
            "primary_origin_receipt",
            "inherited_coverage",
            "invalidated_sections",
        }
        # Authority proof is outside the canonical review-state payload.
        if set(record) not in (expected, expected | {"terminal_authority_proof"}):
            raise ReviewStateError("review generation fields are invalid")
        if record.get("type") != REVIEW_GENERATION_TYPE:
            raise ReviewStateError("review generation type is invalid")
        identity = record.get("patch_identity")
        if not isinstance(identity, Mapping):
            raise ReviewStateError("patch identity is missing")
        sequence_fields = (
            "required_sections",
            "changed_paths",
            "dependency_paths",
            "inherited_coverage",
            "invalidated_sections",
        )
        if any(not isinstance(record.get(name), list) for name in sequence_fields):
            raise ReviewStateError("review generation sequence fields are invalid")
        return cls(
            repository_binding=cast(str, record.get("repository_binding")),
            lineage_id=cast(str, record.get("lineage_id")),
            generation_id=cast(str, record.get("generation_id")),
            predecessor_id=cast(str | None, record.get("predecessor_id")),
            patch_identity=dict(identity),
            tree=cast(str, record.get("tree")),
            required_sections=tuple(cast(list[str], record["required_sections"])),
            policy_digest=cast(str, record.get("policy_digest")),
            delta_from_tree=cast(str | None, record.get("delta_from_tree")),
            delta_sha256=cast(str | None, record.get("delta_sha256")),
            changed_paths=tuple(cast(list[str], record["changed_paths"])),
            dependency_paths=tuple(cast(list[str], record["dependency_paths"])),
            primary_origin_receipt=cast(
                str | None, record.get("primary_origin_receipt")
            ),
            inherited_coverage=tuple(
                cast(list[str], record["inherited_coverage"])
            ),
            invalidated_sections=tuple(
                cast(list[str], record["invalidated_sections"])
            ),
        )

    from_json = from_dict


@dataclass(frozen=True, slots=True)
class GenerationCarryV1:
    generation_id: str
    from_identity: Mapping[str, object] = field(repr=False)
    to_identity: Mapping[str, object] = field(repr=False)
    proof: Mapping[str, object] = field(repr=False)
    sections: tuple[str, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.generation_id, str) or not re.fullmatch(
            r"cg_[0-9a-f]{32}", self.generation_id
        ):
            raise ReviewStateError("generation carry id is invalid")
        if not all(
            isinstance(value, Mapping) and value
            for value in (self.from_identity, self.to_identity, self.proof)
        ):
            raise ReviewStateError("generation carry evidence is invalid")
        sections = _frozen_strings(self.sections, label="carry sections")
        object.__setattr__(self, "from_identity", dict(self.from_identity))
        object.__setattr__(self, "to_identity", dict(self.to_identity))
        object.__setattr__(self, "proof", dict(self.proof))
        object.__setattr__(self, "sections", sections)

    def to_dict(self) -> dict[str, object]:
        return {
            "type": GENERATION_CARRY_TYPE,
            "generation_id": self.generation_id,
            "from_identity": dict(self.from_identity),
            "to_identity": dict(self.to_identity),
            "proof": dict(self.proof),
            "sections": list(self.sections),
        }

    to_json = to_dict
    to_record = to_dict

    def canonical_digest(self) -> str:
        return canonical_record_digest(self.to_dict())

    @classmethod
    def from_dict(cls, record: Mapping[str, object]) -> GenerationCarryV1:
        expected = {
            "type", "generation_id", "from_identity", "to_identity", "proof",
            "sections",
        }
        if set(record) not in (expected, expected | {"terminal_authority_proof"}):
            raise ReviewStateError("generation carry fields are invalid")
        if record.get("type") != GENERATION_CARRY_TYPE:
            raise ReviewStateError("generation carry type is invalid")
        source, target, proof, sections = (
            record.get("from_identity"), record.get("to_identity"),
            record.get("proof"), record.get("sections"),
        )
        if not all(isinstance(value, Mapping) for value in (source, target, proof)):
            raise ReviewStateError("generation carry evidence is invalid")
        if not isinstance(sections, list):
            raise ReviewStateError("generation carry sections are invalid")
        return cls(
            generation_id=cast(str, record.get("generation_id")),
            from_identity=cast(Mapping[str, object], source),
            to_identity=cast(Mapping[str, object], target),
            proof=cast(Mapping[str, object], proof),
            sections=tuple(cast(list[str], sections)),
        )

    from_json = from_dict


@dataclass(frozen=True, slots=True)
class GenerationTransition:
    kind: GenerationTransitionKind
    predecessor_id: str | None


@dataclass(frozen=True, slots=True)
class GenerationResolution:
    kind: GenerationResolutionKind
    generation: ReviewGenerationV1 | None = None
    carry: GenerationCarryV1 | None = None
    transition: GenerationTransition | None = None
    reason: str | None = None

    @property
    def status(self) -> GenerationResolutionKind:
        return self.kind


@dataclass(frozen=True, slots=True)
class ReviewSlotReservation:
    generation_id: str
    family: ReviewFamily
    slot_kind: ReviewSlotKind
    task_id: str
    idempotency_key: str
    reservation_id: str
    existing: bool = field(default=False, compare=False)
    conflict: bool = field(default=False, compare=False)

    def to_dict(self) -> dict[str, object]:
        return {
            "type": REVIEW_SLOT_RESERVATION_TYPE,
            "generation_id": self.generation_id,
            "family": self.family,
            "slot_kind": self.slot_kind,
            "task_id": self.task_id,
            "idempotency_key": self.idempotency_key,
            "reservation_id": self.reservation_id,
        }

    to_json = to_dict
    to_record = to_dict

    def canonical_digest(self) -> str:
        return canonical_record_digest(self.to_dict())

    @classmethod
    def from_dict(cls, record: Mapping[str, object]) -> ReviewSlotReservation:
        expected = {
            "type", "generation_id", "family", "slot_kind", "task_id",
            "idempotency_key", "reservation_id",
        }
        if set(record) not in (expected, expected | {"terminal_authority_proof"}):
            raise ReviewStateError("review slot reservation fields are invalid")
        family = record.get("family")
        slot_kind = record.get("slot_kind")
        task_id = record.get("task_id")
        key = record.get("idempotency_key")
        generation_id = record.get("generation_id")
        if (
            record.get("type") != REVIEW_SLOT_RESERVATION_TYPE
            or family not in {"delivery", "trust"}
            or slot_kind not in {"primary", "delta"}
            or not isinstance(task_id, str) or not task_id
            or not isinstance(key, str) or not key
            or not isinstance(generation_id, str)
        ):
            raise ReviewStateError("review slot reservation is invalid")
        expected_id = _reservation_id(
            generation_id, cast(ReviewFamily, family),
            cast(ReviewSlotKind, slot_kind), task_id, key,
        )
        if record.get("reservation_id") != expected_id:
            raise ReviewStateError("review slot reservation id is invalid")
        return cls(
            generation_id=generation_id,
            family=cast(ReviewFamily, family),
            slot_kind=cast(ReviewSlotKind, slot_kind),
            task_id=task_id,
            idempotency_key=key,
            reservation_id=expected_id,
        )


@dataclass(frozen=True, slots=True)
class ReviewSlotSettlement:
    reservation_id: str
    generation_id: str
    family: ReviewFamily
    slot_kind: ReviewSlotKind
    task_id: str
    outcome: ReviewOutcome
    terminal_ref: str
    settlement_id: str
    existing: bool = field(default=False, compare=False)

    def to_dict(self) -> dict[str, object]:
        return {
            "type": REVIEW_SLOT_SETTLEMENT_TYPE,
            "reservation_id": self.reservation_id,
            "generation_id": self.generation_id,
            "family": self.family,
            "slot_kind": self.slot_kind,
            "task_id": self.task_id,
            "outcome": self.outcome.value,
            "terminal_ref": self.terminal_ref,
            "settlement_id": self.settlement_id,
        }

    to_json = to_dict
    to_record = to_dict

    def canonical_digest(self) -> str:
        return canonical_record_digest(self.to_dict())

    @classmethod
    def from_dict(cls, record: Mapping[str, object]) -> ReviewSlotSettlement:
        expected = {
            "type", "reservation_id", "generation_id", "family", "slot_kind",
            "task_id", "outcome", "terminal_ref", "settlement_id",
        }
        if set(record) not in (expected, expected | {"terminal_authority_proof"}):
            raise ReviewStateError("review slot settlement fields are invalid")
        try:
            outcome = ReviewOutcome(cast(str, record.get("outcome")))
        except (TypeError, ValueError) as exc:
            raise ReviewStateError("review slot settlement outcome is invalid") from exc
        values = [
            record.get("reservation_id"), record.get("generation_id"),
            record.get("task_id"), record.get("terminal_ref"),
        ]
        if (
            record.get("type") != REVIEW_SLOT_SETTLEMENT_TYPE
            or record.get("family") not in {"delivery", "trust"}
            or record.get("slot_kind") not in {"primary", "delta"}
            or not all(isinstance(value, str) and value for value in values)
        ):
            raise ReviewStateError("review slot settlement is invalid")
        expected_id = _settlement_id(cast(str, record["reservation_id"]), outcome,
                                     cast(str, record["terminal_ref"]))
        if record.get("settlement_id") != expected_id:
            raise ReviewStateError("review slot settlement id is invalid")
        return cls(
            reservation_id=cast(str, record["reservation_id"]),
            generation_id=cast(str, record["generation_id"]),
            family=cast(ReviewFamily, record["family"]),
            slot_kind=cast(ReviewSlotKind, record["slot_kind"]),
            task_id=cast(str, record["task_id"]),
            outcome=outcome,
            terminal_ref=cast(str, record["terminal_ref"]),
            settlement_id=expected_id,
        )


def _reservation_id(
    generation_id: str,
    family: ReviewFamily,
    slot_kind: ReviewSlotKind,
    task_id: str,
    idempotency_key: str,
) -> str:
    return "rr_" + _digest([
        "review-slot-reservation-v1", generation_id, family, slot_kind,
        task_id, idempotency_key,
    ])[:32]


def _settlement_id(
    reservation_id: str, outcome: ReviewOutcome, terminal_ref: str
) -> str:
    return "rs_" + _digest([
        "review-slot-settlement-v1", reservation_id, outcome.value, terminal_ref,
    ])[:32]


def _proof_for_exact_content(
    identity: Mapping[str, object], tree: str
) -> dict[str, object]:
    return {
        "kind": "seen-content-v1",
        "patch_identity_digest": patch_identity_digest(identity),
        "tree": tree,
    }


def _equivalence_matches(
    proof: object,
    source: Mapping[str, object],
    target: Mapping[str, object],
    tree: str,
) -> bool:
    return bool(
        equivalence_receipt_is_valid(proof)
        and isinstance(proof, Mapping)
        and proof.get("left_identity_digest") == patch_identity_digest(source)
        and proof.get("right_identity_digest") == patch_identity_digest(target)
        and proof.get("result_tree_sha") == tree
    )


def _record_maps(records: Sequence[Mapping[str, object]]) -> list[dict[str, object]]:
    return [dict(record) for record in records if isinstance(record, Mapping)]


def _transition_kind(
    records: Sequence[Mapping[str, object]], predecessor_id: str
) -> GenerationTransitionKind:
    """Use authenticated authority facts, never caller labels, for transition type."""
    try:
        from .authority_projection import authenticated_coordinator_record_ids

        accepted = authenticated_coordinator_record_ids(
            cast(Sequence[dict[str, object]], records)
        )
    except (ImportError, RuntimeError, ValueError):
        accepted = frozenset()
    for record in reversed(records):
        if id(record) not in accepted:
            continue
        if (
            record.get("type") == "review-generation-owner-request"
            and record.get("generation_id") == predecessor_id
        ):
            return "owner-requested"
        if record.get("type") == "attempt-supersession":
            return "supersession"
    return "substantive"


def resolve_generation(
    records: Sequence[Mapping[str, object]],
    *,
    repository_binding: str,
    patch_identity: Mapping[str, object],
    tree_sha: str,
    required_sections: Sequence[str],
    equivalence_proof: object | None,
    format_only_proof: object | None,
) -> GenerationResolution:
    """Resolve content to an authenticated generation without running Git.

    ``equivalence_proof`` must be the receipt returned by
    :func:`patch_identity.prove_patch_equivalence`; ``format_only_proof`` is the
    boolean returned by :func:`patch_identity.prove_format_only`.  This boundary
    validates those existing receipt types and never re-runs either proof.
    """
    try:
        identity = _validate_identity(patch_identity, tree_sha)
        sections = _frozen_strings(required_sections, label="required sections")
        if not sections:
            raise ReviewStateError("required sections cannot be empty")
        if not isinstance(repository_binding, str) or not repository_binding:
            raise ReviewStateError("repository binding is invalid")
    except ReviewStateError as exc:
        return GenerationResolution("refused", reason=f"missing-evidence:{exc}")
    if equivalence_proof is not None and not (
        isinstance(equivalence_proof, Mapping)
        and equivalence_receipt_is_valid(equivalence_proof)
    ):
        return GenerationResolution("refused", reason="invalid-proof")
    if format_only_proof is not None and type(format_only_proof) is not bool:
        return GenerationResolution("refused", reason="invalid-proof")

    from .authority_projection import generations, generation_carries, slot_state

    projected = generations(cast(Sequence[dict[str, object]], records))
    repository_generations = [
        generation for generation in projected.values()
        if generation.repository_binding == repository_binding
    ]
    exact_generations = [
        generation
        for generation in projected.values()
        if generation.repository_binding == repository_binding
        or generation.repository_binding.startswith("legacy-")
    ]
    current_digest = patch_identity_digest(identity)

    # Exact content is an authenticated lookup, not a caller assertion.  It is
    # what prevents a revert or a branch rename from replenishing review slots.
    exact = [
        generation for generation in exact_generations
        if generation.tree == tree_sha
        and patch_identity_digest(generation.patch_identity) == current_digest
        and generation.required_sections == sections
    ]
    if len(exact) > 1:
        return GenerationResolution("refused", reason="ambiguous-lineage")
    if exact:
        generation = exact[0]
        carry = GenerationCarryV1(
            generation_id=generation.generation_id,
            from_identity=generation.patch_identity,
            to_identity=identity,
            proof=_proof_for_exact_content(identity, tree_sha),
            sections=sections,
        )
        return GenerationResolution("same", generation=generation, carry=carry)

    # A prior carry endpoint is equally content-addressed and cannot mint slots.
    carried_exact: list[tuple[ReviewGenerationV1, Mapping[str, object]]] = []
    for carry in generation_carries(cast(Sequence[dict[str, object]], records)):
        generation = projected.get(carry.generation_id)
        if (
            generation is not None
            and (
                generation.repository_binding == repository_binding
                or generation.repository_binding.startswith("legacy-")
            )
            and generation.required_sections == sections
            and carry.to_identity.get("candidate_tree_sha") == tree_sha
            and patch_identity_digest(carry.to_identity) == current_digest
        ):
            carried_exact.append((generation, carry.to_identity))
    if len({item[0].generation_id for item in carried_exact}) > 1:
        return GenerationResolution("refused", reason="ambiguous-lineage")
    if carried_exact:
        generation, source = carried_exact[-1]
        carry = GenerationCarryV1(
            generation_id=generation.generation_id,
            from_identity=source,
            to_identity=identity,
            proof=_proof_for_exact_content(identity, tree_sha),
            sections=sections,
        )
        return GenerationResolution("same", generation=generation, carry=carry)

    source_generation: ReviewGenerationV1 | None = None
    source_identity: Mapping[str, object] | None = None
    if equivalence_proof is not None:
        left_digest = equivalence_proof.get("left_identity_digest")
        candidates: list[tuple[ReviewGenerationV1, Mapping[str, object]]] = []
        for generation in repository_generations:
            if generation.required_sections != sections:
                continue
            identities: list[Mapping[str, object]] = [generation.patch_identity]
            identities.extend(
                carry.to_identity
                for carry in generation_carries(
                    cast(Sequence[dict[str, object]], records)
                )
                if carry.generation_id == generation.generation_id
            )
            candidates.extend(
                (generation, candidate)
                for candidate in identities
                if patch_identity_digest(candidate) == left_digest
                and _equivalence_matches(
                    equivalence_proof, candidate, identity, tree_sha
                )
            )
        if len({candidate[0].generation_id for candidate in candidates}) > 1:
            return GenerationResolution("refused", reason="ambiguous-lineage")
        if candidates:
            source_generation, source_identity = candidates[-1]
        else:
            return GenerationResolution("refused", reason="invalid-proof")
    elif format_only_proof is True:
        # The existing API's proof is a boolean, so it can only carry the one
        # authenticated current head; accepting an arbitrary older head would
        # let the caller choose lineage.
        if repository_generations:
            source_generation = repository_generations[-1]
            source_identity = source_generation.patch_identity
        else:
            return GenerationResolution("refused", reason="invalid-proof")

    if source_generation is not None and source_identity is not None:
        proof = (
            dict(cast(Mapping[str, object], equivalence_proof))
            if equivalence_proof is not None
            else {
                "kind": "format-only-v1",
                "from_tree": source_identity.get("candidate_tree_sha"),
                "to_tree": tree_sha,
                "verified": True,
            }
        )
        return GenerationResolution(
            "same",
            generation=source_generation,
            carry=GenerationCarryV1(
                generation_id=source_generation.generation_id,
                from_identity=source_identity,
                to_identity=identity,
                proof=proof,
                sections=sections,
            ),
        )

    predecessor = repository_generations[-1] if repository_generations else None
    predecessor_id = predecessor.generation_id if predecessor is not None else None
    lineage_id = (
        predecessor.lineage_id
        if predecessor is not None
        else _lineage_id(repository_binding, identity, tree_sha)
    )
    primary_receipt: str | None = None
    inherited: tuple[str, ...] = ()
    if predecessor is not None:
        predecessor_slots = slot_state(
            cast(Sequence[dict[str, object]], records),
            predecessor.generation_id,
            "delivery",
        )
        primary_receipt = (
            predecessor_slots.primary_terminal_ref
            or predecessor.primary_origin_receipt
        )
        if primary_receipt is not None:
            inherited = tuple(
                sorted(set(predecessor.required_sections).intersection(sections))
            )
    generation = ReviewGenerationV1(
        repository_binding=repository_binding,
        lineage_id=lineage_id,
        generation_id=generation_id_for(
            repository_binding, identity, tree_sha, sections
        ),
        predecessor_id=predecessor_id,
        patch_identity=identity,
        tree=tree_sha,
        required_sections=sections,
        policy_digest=_policy_digest(sections),
        delta_from_tree=predecessor.tree if predecessor is not None else None,
        delta_sha256=(
            cast(str, identity.get("diff_sha256"))
            if isinstance(identity.get("diff_sha256"), str)
            and _SHA256_RE.fullmatch(cast(str, identity["diff_sha256"]))
            else None
        ),
        changed_paths=(),
        dependency_paths=(),
        primary_origin_receipt=primary_receipt,
        inherited_coverage=inherited,
        invalidated_sections=sections if predecessor is not None else (),
    )
    transition_kind: GenerationTransitionKind = (
        "initial"
        if predecessor is None
        else _transition_kind(records, predecessor.generation_id)
    )
    return GenerationResolution(
        "new",
        generation=generation,
        transition=GenerationTransition(transition_kind, predecessor_id),
    )


def _prospective_generations(
    records: Sequence[Mapping[str, object]],
    prospective_generation: ReviewGenerationV1 | None,
) -> dict[str, ReviewGenerationV1]:
    from .authority_projection import generations

    projected = generations(cast(Sequence[dict[str, object]], records))
    if prospective_generation is not None:
        projected = {**projected, prospective_generation.generation_id: prospective_generation}
    return projected


def _reserve_review_slot(
    records: Sequence[Mapping[str, object]],
    *,
    generation_id: str,
    family: ReviewFamily,
    slot_kind: ReviewSlotKind,
    task_id: str,
    idempotency_key: str,
    prospective_generation: ReviewGenerationV1 | None = None,
) -> ReviewSlotReservation:
    from .authority_projection import slot_state
    from ..review.chain import ReviewChainError, enforce_review_budget

    if family not in {"delivery", "trust"} or slot_kind not in {"primary", "delta"}:
        raise ReviewSlotError("reservation-conflict", "review slot identity is invalid")
    if not task_id or not idempotency_key:
        raise ReviewSlotError(
            "reservation-conflict", "review reservation identity is missing"
        )
    if generation_id not in _prospective_generations(records, prospective_generation):
        raise ReviewSlotError("missing-evidence", "review generation is not authenticated")

    state = slot_state(
        cast(Sequence[dict[str, object]], records), generation_id, family
    )
    prospective_primary = bool(
        prospective_generation is not None
        and (
            (
                family == "delivery"
                and prospective_generation.primary_origin_receipt is not None
            )
            or (
                prospective_generation.predecessor_id is not None
                and slot_state(
                    cast(Sequence[dict[str, object]], records),
                    prospective_generation.predecessor_id,
                    family,
                ).primary_consumed
            )
        )
    )
    primary_consumed = state.primary_consumed or prospective_primary
    for existing in state.reservations:
        if existing.idempotency_key != idempotency_key:
            continue
        same = (
            existing.generation_id == generation_id
            and existing.family == family
            and existing.slot_kind == slot_kind
            and existing.task_id == task_id
        )
        if not same:
            raise ReviewSlotError(
                "reservation-conflict",
                "idempotency key is already bound to another review obligation",
            )
        return ReviewSlotReservation(
            **{
                name: getattr(existing, name)
                for name in (
                    "generation_id", "family", "slot_kind", "task_id",
                    "idempotency_key", "reservation_id",
                )
            },
            existing=True,
        )
    if state.outstanding is not None:
        existing = state.outstanding
        return ReviewSlotReservation(
            **{
                name: getattr(existing, name)
                for name in (
                    "generation_id", "family", "slot_kind", "task_id",
                    "idempotency_key", "reservation_id",
                )
            },
            existing=True,
            conflict=True,
        )
    if slot_kind == "primary" and primary_consumed:
        raise ReviewSlotError("slots-exhausted", "primary review slot is consumed")
    if slot_kind == "delta" and not primary_consumed:
        raise ReviewSlotError(
            "missing-evidence", "delta review requires exactly one settled primary"
        )
    if slot_kind == "delta" and state.delta_consumed:
        raise ReviewSlotError("slots-exhausted", "delta review slot is consumed")
    if state.attempt_count(slot_kind) >= 2:
        raise ReviewSlotError("retries-exhausted", "review slot retry is exhausted")

    primary_count = int(primary_consumed)
    delta_count = int(state.delta_consumed)
    try:
        enforce_review_budget(
            completed_reviews=primary_count,
            completed_delta_reviews=delta_count,
            requested="review" if slot_kind == "primary" else "delta",
            generation=generation_id,
        )
    except ReviewChainError as exc:
        raise ReviewSlotError("slots-exhausted", str(exc)) from exc
    return ReviewSlotReservation(
        generation_id=generation_id,
        family=family,
        slot_kind=slot_kind,
        task_id=task_id,
        idempotency_key=idempotency_key,
        reservation_id=_reservation_id(
            generation_id, family, slot_kind, task_id, idempotency_key
        ),
    )


def reserve_review_slot(
    records: Sequence[Mapping[str, object]],
    *,
    generation_id: str,
    family: ReviewFamily,
    slot_kind: ReviewSlotKind,
    task_id: str,
    idempotency_key: str,
) -> ReviewSlotReservation:
    """Reserve one slot from authenticated ledger state.

    The caller must follow the module-level lock/reload/sign/append/fsync
    contract.  Returning a reservation does not itself launch inference.
    """
    return _reserve_review_slot(
        records,
        generation_id=generation_id,
        family=family,
        slot_kind=slot_kind,
        task_id=task_id,
        idempotency_key=idempotency_key,
    )


def _terminal_reference(
    records: Sequence[Mapping[str, object]], terminal_ref: object
) -> tuple[str, Mapping[str, object]]:
    from .authority_projection import (
        _authenticated_attempt_terminal_ids,
        authenticated_coordinator_record_ids,
    )

    if isinstance(terminal_ref, Mapping):
        digest = canonical_record_digest(terminal_ref)
    elif isinstance(terminal_ref, str) and _SHA256_RE.fullmatch(terminal_ref):
        digest = terminal_ref
    else:
        raise ReviewSlotError("reservation-conflict", "terminal reference is invalid")
    matches = [
        record for record in records
        if canonical_record_digest(record) == digest
    ]
    if len(matches) != 1:
        raise ReviewSlotError(
            "reservation-conflict", "terminal reference is not uniquely present"
        )
    mapped = cast(Sequence[dict[str, object]], records)
    authenticated = (
        set(authenticated_coordinator_record_ids(mapped))
        | set(_authenticated_attempt_terminal_ids(mapped))
    )
    if id(matches[0]) not in authenticated:
        raise ReviewSlotError("reservation-conflict", "terminal reference is unauthenticated")
    return digest, matches[0]


def settle_review_slot(
    records: Sequence[Mapping[str, object]],
    *,
    reservation: ReviewSlotReservation,
    outcome: ReviewOutcome,
    terminal_ref: object,
) -> ReviewSlotSettlement:
    """Build an idempotent settlement bound to an authenticated terminal."""
    from .authority_projection import authenticated_review_state_records
    from ..review.authority import classify_review_outcome

    try:
        normalized_outcome = ReviewOutcome(outcome)
    except (TypeError, ValueError) as exc:
        raise ReviewSlotError("reservation-conflict", "review outcome is invalid") from exc
    authenticated = authenticated_review_state_records(
        cast(Sequence[dict[str, object]], records)
    )
    reservations = [
        ReviewSlotReservation.from_dict(record)
        for record in authenticated
        if record.get("type") == REVIEW_SLOT_RESERVATION_TYPE
    ]
    persisted = [
        candidate for candidate in reservations
        if candidate.reservation_id == reservation.reservation_id
    ]
    if len(persisted) != 1 or persisted[0].to_dict() != reservation.to_dict():
        raise ReviewSlotError(
            "reservation-conflict", "review reservation is not authenticated"
        )
    digest, terminal = _terminal_reference(records, terminal_ref)
    if terminal.get("task_id") != reservation.task_id:
        raise ReviewSlotError(
            "reservation-conflict", "terminal reference belongs to another task"
        )
    classified = classify_review_outcome(terminal)
    if classified is not normalized_outcome:
        raise ReviewSlotError(
            "reservation-conflict", "settlement outcome does not match terminal"
        )
    proposed = ReviewSlotSettlement(
        reservation_id=reservation.reservation_id,
        generation_id=reservation.generation_id,
        family=reservation.family,
        slot_kind=reservation.slot_kind,
        task_id=reservation.task_id,
        outcome=normalized_outcome,
        terminal_ref=digest,
        settlement_id=_settlement_id(
            reservation.reservation_id, normalized_outcome, digest
        ),
    )
    existing = [
        ReviewSlotSettlement.from_dict(record)
        for record in authenticated
        if record.get("type") == REVIEW_SLOT_SETTLEMENT_TYPE
        and record.get("reservation_id") == reservation.reservation_id
    ]
    if existing:
        if len(existing) == 1 and existing[0].to_dict() == proposed.to_dict():
            return ReviewSlotSettlement(
                **{
                    name: getattr(proposed, name)
                    for name in (
                        "reservation_id", "generation_id", "family", "slot_kind",
                        "task_id", "outcome", "terminal_ref", "settlement_id",
                    )
                },
                existing=True,
            )
        raise ReviewSlotError(
            "reservation-conflict", "review reservation has a conflicting settlement"
        )
    return proposed


def assert_authority_ledger_lock_held(repo: Path) -> None:
    """Assert the in-process lock contract when the ledger exposes lock state."""
    from . import authority_store

    held_provider = getattr(authority_store, "_held_authority_ledger_locks", None)
    if callable(held_provider) and str(repo.resolve()) not in held_provider():
        raise ReviewStateError("authority ledger lock must be held")


__all__ = [
    "GenerationCarryV1",
    "GenerationResolution",
    "GenerationTransition",
    "ReviewGenerationV1",
    "ReviewSlotError",
    "ReviewSlotReservation",
    "ReviewSlotSettlement",
    "ReviewStateError",
    "assert_authority_ledger_lock_held",
    "generation_id_for",
    "patch_identity_digest",
    "reserve_review_slot",
    "resolve_generation",
    "settle_review_slot",
]
