"""Content-addressed review generations and durable review-slot authority.

Content identity includes the base tree, candidate tree, and exact diff digest.
The same candidate tree and diff on a different base therefore rejoins an
existing generation only through an authenticated exact-pair equivalence
proof.  Without that proof, the changed base is substantive review context: it
starts a detached generation with no inherited primary authority.

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
from .patch_identity import (
    PATCH_DIFF_FORMAT,
    PATCH_IDENTITY_SCHEMA,
    equivalence_receipt_is_valid,
)
from ..runners.contract import ReviewOutcome

REVIEW_GENERATION_TYPE = "review-generation-v1"
GENERATION_PROOF_TYPE = "review-generation-proof-v1"
GENERATION_LINK_TYPE = "review-generation-link-v1"
GENERATION_CARRY_TYPE = "generation-carry-v1"
REVIEW_SLOT_RESERVATION_TYPE = "review-slot-reservation-v1"
REVIEW_SLOT_SETTLEMENT_TYPE = "review-slot-settlement-v1"

GenerationTransitionKind = Literal[
    "initial", "substantive", "supersession", "owner-requested"
]
GenerationResolutionKind = Literal["same", "new", "refused"]
ReviewFamily = Literal["delivery", "trust"]
ReviewSlotKind = Literal["primary", "delta"]
GenerationProofKind = Literal["patch-equivalence", "format-only"]

_OID_RE = re.compile(r"[0-9a-f]{40,64}")
_SHA256_RE = re.compile(r"[0-9a-f]{64}")
_PATCH_IDENTITY_FIELDS = {
    "schema_version",
    "base_sha",
    "base_tree_sha",
    "candidate_sha",
    "candidate_tree_sha",
    "diff_format",
    "diff_sha256",
    "patch_id_verbatim",
}


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
    if set(identity) != _PATCH_IDENTITY_FIELDS:
        raise ReviewStateError("patch identity fields are invalid")
    if (
        identity.get("schema_version") != PATCH_IDENTITY_SCHEMA
        or identity.get("diff_format") != PATCH_DIFF_FORMAT
    ):
        raise ReviewStateError("patch identity schema or diff format is invalid")
    for field_name in (
        "base_sha",
        "base_tree_sha",
        "candidate_sha",
        "candidate_tree_sha",
        "patch_id_verbatim",
    ):
        value = identity.get(field_name)
        if not isinstance(value, str) or _OID_RE.fullmatch(value) is None:
            raise ReviewStateError(f"patch identity {field_name} is invalid")
    diff_digest = identity.get("diff_sha256")
    if not isinstance(diff_digest, str) or _SHA256_RE.fullmatch(diff_digest) is None:
        raise ReviewStateError("patch identity diff digest is invalid")
    candidate_tree = identity.get("candidate_tree_sha")
    if not isinstance(candidate_tree, str) or candidate_tree != tree:
        raise ReviewStateError("patch identity does not bind the current tree")
    return dict(identity)


def patch_content_digest(identity: Mapping[str, object]) -> str:
    """Identify patch content without commit metadata or caller extensions."""
    tree = identity.get("candidate_tree_sha")
    if not isinstance(tree, str):
        raise ReviewStateError("patch identity is missing a candidate tree")
    validated = _validate_identity(identity, tree)
    return _digest(
        [
            "review-patch-content-v1",
            validated["base_tree_sha"],
            validated["candidate_tree_sha"],
            validated["diff_sha256"],
        ]
    )


def generation_id_for(
    repository_binding: str,
    patch_identity: Mapping[str, object],
    tree: str,
    required_sections: Sequence[str],
) -> str:
    """Derive one identifier from content, never commit or policy metadata."""
    _frozen_strings(required_sections, label="required sections")
    validated = _validate_identity(patch_identity, tree)
    return "cg_" + _digest(
        [
            "review-generation-v1",
            repository_binding,
            patch_content_digest(validated),
            tree,
        ]
    )[:32]


def _lineage_id(
    repository_binding: str,
    patch_identity: Mapping[str, object],
    tree: str,
) -> str:
    validated = _validate_identity(patch_identity, tree)
    return "rl_" + _digest(
        [
            "review-lineage-v1",
            repository_binding,
            patch_content_digest(validated),
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
class GenerationProofV1:
    """Coordinator-authenticated provenance from a kernel proof operation."""

    proof_kind: GenerationProofKind
    from_identity: Mapping[str, object] = field(repr=False)
    from_tree: str
    to_identity: Mapping[str, object] = field(repr=False)
    to_tree: str
    proof: Mapping[str, object] = field(repr=False)

    def __post_init__(self) -> None:
        if self.proof_kind not in {"patch-equivalence", "format-only"}:
            raise ReviewStateError("generation proof kind is invalid")
        source = _validate_identity(self.from_identity, self.from_tree)
        target = _validate_identity(self.to_identity, self.to_tree)
        proof = dict(self.proof)
        source_digest = patch_identity_digest(source)
        target_digest = patch_identity_digest(target)
        if self.proof_kind == "patch-equivalence":
            valid = bool(
                equivalence_receipt_is_valid(proof)
                and proof.get("left_identity_digest") == source_digest
                and proof.get("right_identity_digest") == target_digest
                and proof.get("result_tree_sha") == self.to_tree
            )
        else:
            valid = proof == {
                "schema_version": "format-only-v1",
                "from_identity_digest": source_digest,
                "from_tree": self.from_tree,
                "to_identity_digest": target_digest,
                "to_tree": self.to_tree,
                "verified": True,
            }
        if not valid:
            raise ReviewStateError("generation proof does not bind its identities")
        object.__setattr__(self, "from_identity", source)
        object.__setattr__(self, "to_identity", target)
        object.__setattr__(self, "proof", proof)

    def to_dict(self) -> dict[str, object]:
        return {
            "type": GENERATION_PROOF_TYPE,
            "proof_kind": self.proof_kind,
            "from_identity": dict(self.from_identity),
            "from_tree": self.from_tree,
            "to_identity": dict(self.to_identity),
            "to_tree": self.to_tree,
            "proof": dict(self.proof),
        }

    to_json = to_dict
    to_record = to_dict

    def canonical_digest(self) -> str:
        return canonical_record_digest(self.to_dict())

    @classmethod
    def from_dict(cls, record: Mapping[str, object]) -> GenerationProofV1:
        expected = {
            "type",
            "proof_kind",
            "from_identity",
            "from_tree",
            "to_identity",
            "to_tree",
            "proof",
        }
        if set(record) not in (expected, expected | {"terminal_authority_proof"}):
            raise ReviewStateError("generation proof fields are invalid")
        if record.get("type") != GENERATION_PROOF_TYPE:
            raise ReviewStateError("generation proof type is invalid")
        source = record.get("from_identity")
        target = record.get("to_identity")
        proof = record.get("proof")
        if not all(isinstance(value, Mapping) for value in (source, target, proof)):
            raise ReviewStateError("generation proof evidence is invalid")
        return cls(
            proof_kind=cast(GenerationProofKind, record.get("proof_kind")),
            from_identity=cast(Mapping[str, object], source),
            from_tree=cast(str, record.get("from_tree")),
            to_identity=cast(Mapping[str, object], target),
            to_tree=cast(str, record.get("to_tree")),
            proof=cast(Mapping[str, object], proof),
        )

    from_json = from_dict


@dataclass(frozen=True, slots=True)
class GenerationLinkV1:
    """Coordinator-authorized content link for one substantive successor.

    Record order is never ancestry: a successor inherits review authority only
    when a preceding authenticated link binds the exact known source identity
    and exact target identity.
    """

    repository_binding: str
    predecessor_generation_id: str
    from_identity: Mapping[str, object] = field(repr=False)
    to_identity: Mapping[str, object] = field(repr=False)
    transition_kind: Literal["substantive", "supersession", "owner-requested"]

    def __post_init__(self) -> None:
        if not isinstance(self.repository_binding, str) or not self.repository_binding:
            raise ReviewStateError("generation link repository binding is invalid")
        if not re.fullmatch(r"cg_[0-9a-f]{32}", self.predecessor_generation_id):
            raise ReviewStateError("generation link predecessor is invalid")
        if self.transition_kind not in {
            "substantive", "supersession", "owner-requested"
        }:
            raise ReviewStateError("generation link transition is invalid")
        source_tree = self.from_identity.get("candidate_tree_sha")
        target_tree = self.to_identity.get("candidate_tree_sha")
        if not isinstance(source_tree, str) or not isinstance(target_tree, str):
            raise ReviewStateError("generation link identity is missing a tree")
        source = _validate_identity(self.from_identity, source_tree)
        target = _validate_identity(self.to_identity, target_tree)
        object.__setattr__(self, "from_identity", source)
        object.__setattr__(self, "to_identity", target)

    def to_dict(self) -> dict[str, object]:
        return {
            "type": GENERATION_LINK_TYPE,
            "repository_binding": self.repository_binding,
            "predecessor_generation_id": self.predecessor_generation_id,
            "from_identity": dict(self.from_identity),
            "to_identity": dict(self.to_identity),
            "transition_kind": self.transition_kind,
        }

    to_json = to_dict
    to_record = to_dict

    def canonical_digest(self) -> str:
        return canonical_record_digest(self.to_dict())

    @classmethod
    def from_dict(cls, record: Mapping[str, object]) -> GenerationLinkV1:
        expected = {
            "type", "repository_binding", "predecessor_generation_id",
            "from_identity", "to_identity", "transition_kind",
        }
        if set(record) not in (expected, expected | {"terminal_authority_proof"}):
            raise ReviewStateError("generation link fields are invalid")
        source = record.get("from_identity")
        target = record.get("to_identity")
        if (
            record.get("type") != GENERATION_LINK_TYPE
            or not isinstance(source, Mapping)
            or not isinstance(target, Mapping)
        ):
            raise ReviewStateError("generation link is invalid")
        return cls(
            repository_binding=cast(str, record.get("repository_binding")),
            predecessor_generation_id=cast(
                str, record.get("predecessor_generation_id")
            ),
            from_identity=source,
            to_identity=target,
            transition_kind=cast(
                Literal["substantive", "supersession", "owner-requested"],
                record.get("transition_kind"),
            ),
        )


def prove_generation_carry(
    repo: Path,
    *,
    from_identity: Mapping[str, object],
    to_identity: Mapping[str, object],
    proof_kind: GenerationProofKind,
) -> GenerationProofV1 | None:
    """Run the kernel prover and return the exact record to authenticate.

    The coordinator must seal and append this record before passing it to
    :func:`resolve_generation`; an unrecorded result grants no carry.
    """
    from . import patch_identity as patch_identity_kernel

    from_tree = from_identity.get("candidate_tree_sha")
    to_tree = to_identity.get("candidate_tree_sha")
    if not isinstance(from_tree, str) or not isinstance(to_tree, str):
        return None
    if proof_kind == "patch-equivalence":
        receipt = patch_identity_kernel.prove_patch_equivalence(
            repo, from_identity, to_identity
        )
        if receipt is None:
            return None
        proof = receipt
    elif proof_kind == "format-only":
        if not patch_identity_kernel.prove_format_only(repo, from_tree, to_tree):
            return None
        proof = {
            "schema_version": "format-only-v1",
            "from_identity_digest": patch_identity_digest(from_identity),
            "from_tree": from_tree,
            "to_identity_digest": patch_identity_digest(to_identity),
            "to_tree": to_tree,
            "verified": True,
        }
    else:
        raise ReviewStateError("generation proof kind is invalid")
    return GenerationProofV1(
        proof_kind=proof_kind,
        from_identity=from_identity,
        from_tree=from_tree,
        to_identity=to_identity,
        to_tree=to_tree,
        proof=proof,
    )


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
        from_tree = self.from_identity.get("candidate_tree_sha")
        to_tree = self.to_identity.get("candidate_tree_sha")
        if not isinstance(from_tree, str) or not isinstance(to_tree, str):
            raise ReviewStateError("generation carry identity is missing a tree")
        source = _validate_identity(self.from_identity, from_tree)
        target = _validate_identity(self.to_identity, to_tree)
        sections = _frozen_strings(self.sections, label="carry sections")
        object.__setattr__(self, "from_identity", source)
        object.__setattr__(self, "to_identity", target)
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
        "content_digest": patch_content_digest(identity),
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


def _supplied_generation_proof(
    records: Sequence[Mapping[str, object]],
    supplied: object,
    *,
    proof_kind: GenerationProofKind,
    source: Mapping[str, object],
    target: Mapping[str, object],
) -> GenerationProofV1 | None:
    """Resolve only a preceding coordinator-authenticated exact-pair proof."""
    from .authority_projection import generation_proofs

    for candidate in reversed(
        generation_proofs(cast(Sequence[dict[str, object]], records))
    ):
        if (
            candidate.proof_kind == proof_kind
            and candidate.from_identity == dict(source)
            and candidate.to_identity == dict(target)
            and (
                supplied == candidate.proof
                or supplied == candidate.to_dict()
                or (
                    isinstance(supplied, Mapping)
                    and {
                        key: value
                        for key, value in supplied.items()
                        if key != "terminal_authority_proof"
                    }
                    == candidate.to_dict()
                )
            )
        ):
            return candidate
    return None


def _record_maps(records: Sequence[Mapping[str, object]]) -> list[dict[str, object]]:
    return [dict(record) for record in records if isinstance(record, Mapping)]


def _transition_kind(
    records: Sequence[Mapping[str, object]],
    predecessor_id: str,
    target_identity: Mapping[str, object],
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
        contract = record.get("task_contract")
        recorded_generation = record.get("review_generation_id")
        if recorded_generation is None and isinstance(contract, Mapping):
            recorded_generation = contract.get("review_generation_id")
        superseding_source = record.get("superseding_source_identity")
        if (
            record.get("type") == "attempt-supersession"
            and recorded_generation == predecessor_id
            and isinstance(superseding_source, Mapping)
            and superseding_source.get("head")
            == target_identity.get("candidate_sha")
        ):
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

    Each supplied proof must match an exact-pair :class:`GenerationProofV1`
    already authenticated in ``records``. The proof operation runs separately
    through :func:`prove_generation_carry`; this lock-held boundary never runs
    Git and never accepts a bare boolean or self-digested receipt.
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
    if equivalence_proof is not None and not isinstance(equivalence_proof, Mapping):
        return GenerationResolution("refused", reason="invalid-proof")
    if format_only_proof is not None and not isinstance(format_only_proof, Mapping):
        return GenerationResolution("refused", reason="invalid-proof")
    accumulator = getattr(records, "accumulator_head", None)
    ledger_binding = getattr(accumulator, "repository_binding", None)
    if isinstance(ledger_binding, str) and ledger_binding != repository_binding:
        return GenerationResolution(
            "refused", reason="missing-evidence:repository binding mismatch"
        )

    from .authority_projection import (
        generation_carries,
        generation_links,
        generations,
        slot_state,
    )

    from .seams import MissingAdapter

    try:
        projected = generations(cast(Sequence[dict[str, object]], records))
    except MissingAdapter:
        return GenerationResolution(
            "refused", reason="missing-evidence:legacy projection unavailable"
        )
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
    current_content = patch_content_digest(identity)

    # Exact content is an authenticated lookup, not a caller assertion.  It is
    # what prevents an amend, re-commit, revert, or branch rename from
    # replenishing review slots. Commit metadata and policy sections are not
    # content identity.
    exact = [
        generation for generation in exact_generations
        if generation.tree == tree_sha
        and patch_content_digest(generation.patch_identity) == current_content
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
            sections=tuple(
                sorted(set(generation.required_sections).intersection(sections))
            ),
        )
        return GenerationResolution("same", generation=generation, carry=carry)

    # A prior carry endpoint is equally content-addressed and cannot mint slots.
    # Preserve the sections that can actually reach that endpoint through the
    # authenticated carry graph.  Reconstructing the generation's full policy
    # here would regrant coverage an earlier carry deliberately withheld.
    projected_carries = generation_carries(
        cast(Sequence[dict[str, object]], records)
    )
    reachable_sections: dict[tuple[str, str], set[str]] = {
        (generation.generation_id, patch_identity_digest(generation.patch_identity)):
        set(generation.required_sections)
        for generation in projected.values()
    }
    carry_identities: dict[tuple[str, str], Mapping[str, object]] = {}
    for carry in projected_carries:
        source_key = (
            carry.generation_id,
            patch_identity_digest(carry.from_identity),
        )
        target_key = (
            carry.generation_id,
            patch_identity_digest(carry.to_identity),
        )
        carried_sections = reachable_sections.get(source_key, set()).intersection(
            carry.sections
        )
        reachable_sections.setdefault(target_key, set()).update(carried_sections)
        carry_identities[target_key] = carry.to_identity

    carried_exact: list[
        tuple[ReviewGenerationV1, Mapping[str, object], tuple[str, ...]]
    ] = []
    for key, source in carry_identities.items():
        generation = projected.get(key[0])
        if (
            generation is not None
            and (
                generation.repository_binding == repository_binding
                or generation.repository_binding.startswith("legacy-")
            )
            and source.get("candidate_tree_sha") == tree_sha
            and patch_content_digest(source) == current_content
        ):
            carried_exact.append(
                (
                    generation,
                    source,
                    tuple(sorted(reachable_sections.get(key, set()))),
                )
            )
    if len({item[0].generation_id for item in carried_exact}) > 1:
        return GenerationResolution("refused", reason="ambiguous-lineage")
    if carried_exact:
        generation, source, carried_sections = carried_exact[-1]
        carry = GenerationCarryV1(
            generation_id=generation.generation_id,
            from_identity=source,
            to_identity=identity,
            proof=_proof_for_exact_content(identity, tree_sha),
            sections=tuple(sorted(set(carried_sections).intersection(sections))),
        )
        return GenerationResolution("same", generation=generation, carry=carry)

    source_generation: ReviewGenerationV1 | None = None
    source_identity: Mapping[str, object] | None = None
    if equivalence_proof is not None:
        candidates: list[tuple[ReviewGenerationV1, Mapping[str, object]]] = []
        for generation in repository_generations:
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
                if _supplied_generation_proof(
                    records,
                    equivalence_proof,
                    proof_kind="patch-equivalence",
                    source=candidate,
                    target=identity,
                )
                is not None
            )
        if len({candidate[0].generation_id for candidate in candidates}) > 1:
            return GenerationResolution("refused", reason="ambiguous-lineage")
        if candidates:
            source_generation, source_identity = candidates[-1]
        else:
            return GenerationResolution("refused", reason="invalid-proof")
    elif format_only_proof is not None:
        candidates = []
        carries = generation_carries(cast(Sequence[dict[str, object]], records))
        for generation in repository_generations:
            identities = [generation.patch_identity]
            identities.extend(
                carry.to_identity
                for carry in carries
                if carry.generation_id == generation.generation_id
            )
            candidates.extend(
                (generation, candidate)
                for candidate in identities
                if _supplied_generation_proof(
                    records,
                    format_only_proof,
                    proof_kind="format-only",
                    source=candidate,
                    target=identity,
                )
                is not None
            )
        if len({candidate[0].generation_id for candidate in candidates}) > 1:
            return GenerationResolution("refused", reason="ambiguous-lineage")
        if not candidates:
            return GenerationResolution("refused", reason="invalid-proof")
        source_generation, source_identity = candidates[-1]

    if source_generation is not None and source_identity is not None:
        authenticated_proof = _supplied_generation_proof(
            records,
            equivalence_proof if equivalence_proof is not None else format_only_proof,
            proof_kind=(
                "patch-equivalence"
                if equivalence_proof is not None
                else "format-only"
            ),
            source=source_identity,
            target=identity,
        )
        if authenticated_proof is None:
            return GenerationResolution("refused", reason="invalid-proof")
        return GenerationResolution(
            "same",
            generation=source_generation,
            carry=GenerationCarryV1(
                generation_id=source_generation.generation_id,
                from_identity=source_identity,
                to_identity=identity,
                proof=authenticated_proof.to_dict(),
                sections=tuple(
                    sorted(
                        set(source_generation.required_sections).intersection(
                            sections
                        )
                    )
                ),
            ),
        )

    matching_links = [
        link
        for link in generation_links(cast(Sequence[dict[str, object]], records))
        if link.repository_binding == repository_binding
        and link.to_identity == identity
        and link.predecessor_generation_id in projected
        and link.from_identity in (
            [projected[link.predecessor_generation_id].patch_identity]
            + [
                carry.to_identity
                for carry in projected_carries
                if carry.generation_id == link.predecessor_generation_id
            ]
        )
    ]
    if len(matching_links) > 1:
        return GenerationResolution("refused", reason="ambiguous-lineage")
    link = matching_links[0] if matching_links else None
    predecessor = (
        projected.get(link.predecessor_generation_id) if link is not None else None
    )
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
        predecessor_is_covered = bool(
            predecessor_slots.own_primary_consumed
            or (
                predecessor_slots.inherited_primary_ref is not None
                and (
                    not predecessor.invalidated_sections
                    or predecessor_slots.delta_consumed
                )
            )
        )
        primary_receipt = (
            predecessor_slots.primary_terminal_ref
            if predecessor_is_covered
            else None
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
        delta_from_tree=(
            cast(str, link.from_identity.get("candidate_tree_sha"))
            if link is not None
            else None
        ),
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
    transition_kind: GenerationTransitionKind = "initial"
    if link is not None:
        transition_kind = link.transition_kind
        if transition_kind in {"supersession", "owner-requested"} and (
            _transition_kind(records, predecessor.generation_id, identity)
            != transition_kind
        ):
            return GenerationResolution("refused", reason="invalid-proof")
    elif any(
        generation.tree == tree_sha
        and generation.patch_identity.get("diff_sha256")
        == identity.get("diff_sha256")
        and generation.patch_identity.get("base_tree_sha")
        != identity.get("base_tree_sha")
        for generation in repository_generations
    ):
        # The candidate bytes and patch bytes match, but a different base is a
        # different contextual claim until an exact-pair proof authenticates
        # equivalence.  Keep this generation detached so no prior primary or
        # retry budget is inherited, while labelling the change substantive.
        transition_kind = "substantive"
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


def fresh_review_generation(generation: ReviewGenerationV1) -> ReviewGenerationV1:
    """Detach an oversized successor from inherited authority for a full review."""
    return ReviewGenerationV1(
        repository_binding=generation.repository_binding,
        lineage_id=_lineage_id(
            generation.repository_binding,
            generation.patch_identity,
            generation.tree,
        ),
        generation_id=generation.generation_id,
        predecessor_id=None,
        patch_identity=generation.patch_identity,
        tree=generation.tree,
        required_sections=generation.required_sections,
        policy_digest=generation.policy_digest,
        delta_from_tree=None,
        delta_sha256=generation.delta_sha256,
        changed_paths=generation.changed_paths,
        dependency_paths=generation.dependency_paths,
        primary_origin_receipt=None,
        inherited_coverage=(),
        invalidated_sections=(),
    )


def _reserve_review_slot(
    records: Sequence[Mapping[str, object]],
    *,
    generation_id: str,
    family: ReviewFamily,
    slot_kind: ReviewSlotKind,
    task_id: str,
    idempotency_key: str,
    prospective_generation: ReviewGenerationV1 | None = None,
    prospective_inherited_primary: bool = False,
) -> ReviewSlotReservation:
    from .authority_projection import authenticated_review_state_records, slot_state
    from ..review.chain import ReviewChainError, enforce_review_budget

    if family not in {"delivery", "trust"} or slot_kind not in {"primary", "delta"}:
        raise ReviewSlotError("reservation-conflict", "review slot identity is invalid")
    if not task_id or not idempotency_key:
        raise ReviewSlotError(
            "reservation-conflict", "review reservation identity is missing"
        )
    if generation_id not in _prospective_generations(records, prospective_generation):
        raise ReviewSlotError("missing-evidence", "review generation is not authenticated")

    for record in authenticated_review_state_records(
        cast(Sequence[dict[str, object]], records)
    ):
        if (
            record.get("type") != REVIEW_SLOT_RESERVATION_TYPE
            or record.get("idempotency_key") != idempotency_key
        ):
            continue
        try:
            bound = ReviewSlotReservation.from_dict(record)
        except (TypeError, ValueError):
            continue
        if (
            bound.generation_id,
            bound.family,
            bound.slot_kind,
            bound.task_id,
        ) != (generation_id, family, slot_kind, task_id):
            raise ReviewSlotError(
                "reservation-conflict",
                "idempotency key is already bound to another review obligation",
            )

    state = slot_state(
        cast(Sequence[dict[str, object]], records), generation_id, family
    )
    prospective_primary = bool(
        prospective_generation is not None
        and (
            prospective_inherited_primary
            or (
                family == "delivery"
                and prospective_generation.primary_origin_receipt is not None
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
        settlement = state.settlement_for(existing.reservation_id)
        if settlement is not None:
            raise ReviewSlotError(
                "reservation-conflict",
                "idempotency key belongs to an already settled review reservation",
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
    repository: Path,
    records: Sequence[Mapping[str, object]],
    *,
    generation_id: str,
    family: ReviewFamily,
    slot_kind: ReviewSlotKind,
    task_id: str,
    idempotency_key: str,
) -> ReviewSlotReservation:
    """Reserve one slot from authenticated ledger state.

    The caller must reload before this call and sign/append/fsync the returned
    record while retaining the same authority-ledger lock. Returning a
    reservation does not itself launch inference.
    """
    assert_authority_ledger_lock_held(repository)
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
    repository: Path,
    records: Sequence[Mapping[str, object]],
    *,
    reservation: ReviewSlotReservation,
    outcome: ReviewOutcome,
    terminal_ref: object,
) -> ReviewSlotSettlement:
    """Build an idempotent settlement bound to an authenticated terminal."""
    from .authority_projection import (
        authenticated_review_state_records,
        generation_carries,
        generations,
    )
    from ..review.authority import classify_review_outcome

    assert_authority_ledger_lock_held(repository)
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
    generation = generations(cast(Sequence[dict[str, object]], records)).get(
        reservation.generation_id
    )
    valid_trees = {generation.tree} if generation is not None else set()
    valid_trees.update(
        cast(str, carry.to_identity["candidate_tree_sha"])
        for carry in generation_carries(cast(Sequence[dict[str, object]], records))
        if carry.generation_id == reservation.generation_id
        and isinstance(carry.to_identity.get("candidate_tree_sha"), str)
    )
    if terminal.get("snapshot_tree_sha") not in valid_trees:
        raise ReviewSlotError(
            "reservation-conflict",
            "terminal tree is outside the reserved content generation",
        )
    classified = classify_review_outcome(terminal, records)
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
        if len(existing) == 1 and (
            existing[0].to_dict() == proposed.to_dict()
            or (
                existing[0].outcome is ReviewOutcome.UNRESOLVED
                and normalized_outcome is ReviewOutcome.CONSUMED
                and existing[0].terminal_ref == digest
            )
        ):
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
    "GenerationLinkV1",
    "GenerationProofV1",
    "GenerationResolution",
    "GenerationTransition",
    "ReviewGenerationV1",
    "ReviewSlotError",
    "ReviewSlotReservation",
    "ReviewSlotSettlement",
    "ReviewStateError",
    "assert_authority_ledger_lock_held",
    "fresh_review_generation",
    "generation_id_for",
    "patch_content_digest",
    "patch_identity_digest",
    "prove_generation_carry",
    "reserve_review_slot",
    "resolve_generation",
    "settle_review_slot",
]
