"""Content-addressed trust-manifest claims and mechanical verdict reuse.

``changed_paths`` and ``risk_paths_added`` are trusted admission inputs.  D29
step 2 computes both from the previous receipt's tree; this module cannot
discover or authenticate a repository delta by itself.  It binds the previous
tree and the canonical changed-path digest into each task, then requires the
caller to present that same diff again when composing a receipt.
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import PurePosixPath
from types import MappingProxyType
from typing import Self

from ..kernel.canonical import canonical_json_bytes


TRUST_CLAIM_SECTIONS = (
    "actors_assets",
    "authority_writers_consumers",
    "credential_process_filesystem",
    "legacy_cutover",
    "race_replay_recovery",
    "adversarial_tests",
)
TRUST_MANIFEST_FIELDS = frozenset((*TRUST_CLAIM_SECTIONS, "risk_paths"))
TRUST_CLAIM_VERDICTS = frozenset({"pass", "fail", "inconclusive"})
INVALIDATION_CAUSES = frozenset(
    {
        "text-or-coverage-changed",
        "covered-path-changed",
        "repository-wide-dependency",
        "base-moved",
        "new-risk-path",
        "prior-inconclusive",
        "retired",
    }
)
_SHA256_RE = re.compile(r"[0-9a-f]{64}")


class TrustClaimError(ValueError):
    """A trust claim, task, result, or receipt is malformed or inconsistent."""


def _digest(value: object) -> str:
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def _normalized_text(value: object) -> str:
    if not isinstance(value, str):
        raise TrustClaimError("trust claim text must be a string")
    normalized = unicodedata.normalize("NFC", value.replace("\r\n", "\n")).strip()
    if not normalized:
        raise TrustClaimError("trust claim text must not be empty")
    return normalized


def _canonical_path(value: object, *, label: str = "trust claim path") -> str:
    if not isinstance(value, str) or not value:
        raise TrustClaimError(f"{label} must be a non-empty string")
    if value != unicodedata.normalize("NFC", value):
        raise TrustClaimError(f"{label} is not canonical")
    if (
        value.startswith("/")
        or "\\" in value
        or any(unicodedata.category(character) == "Cc" for character in value)
    ):
        raise TrustClaimError(f"{label} is not repository-relative and canonical")
    candidate = PurePosixPath(value)
    if (
        candidate.is_absolute()
        or value in {".", ".."}
        or any(part in {"", ".", ".."} for part in value.split("/"))
        or candidate.as_posix() != value
    ):
        raise TrustClaimError(f"{label} is not repository-relative and canonical")
    return value


def _canonical_paths(value: object, *, label: str) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)):
        raise TrustClaimError(f"{label} must be a list")
    return tuple(sorted({_canonical_path(path, label=label) for path in value}))


def _path_covers(declared_path: str, candidate_path: str) -> bool:
    """Return whether a declared path covers a path on component boundaries."""
    return candidate_path == declared_path or candidate_path.startswith(
        declared_path + "/"
    )


def _covered_by_any(candidate_path: str, declared_paths: Sequence[str]) -> bool:
    return any(_path_covers(path, candidate_path) for path in declared_paths)


def _changed_paths_digest(paths: Sequence[str]) -> str:
    normalized = _canonical_paths(paths, label="changed_paths")
    return _digest(["trust-claim-changed-paths-v1", list(normalized)])


def claim_id(section: str, text: str, paths: Sequence[str]) -> str:
    """Return the stable identity of one normalized trust claim."""
    if section not in TRUST_CLAIM_SECTIONS:
        raise TrustClaimError("trust claim section is invalid")
    normalized_text = _normalized_text(text)
    normalized_paths = tuple(
        sorted({_canonical_path(path) for path in paths})
    )
    return "tc_" + _digest(
        ["trust-claim-v1", section, normalized_text, list(normalized_paths)]
    )


@dataclass(frozen=True, slots=True)
class TrustClaim:
    section: str
    text: str
    paths: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.section not in TRUST_CLAIM_SECTIONS:
            raise TrustClaimError("trust claim section is invalid")
        normalized_text = _normalized_text(self.text)
        normalized_paths = tuple(
            sorted({_canonical_path(path) for path in self.paths})
        )
        object.__setattr__(self, "text", normalized_text)
        object.__setattr__(self, "paths", normalized_paths)

    @property
    def claim_id(self) -> str:
        return claim_id(self.section, self.text, self.paths)

    def to_dict(self) -> dict[str, object]:
        return {
            "claim_id": self.claim_id,
            "section": self.section,
            "text": self.text,
            "paths": list(self.paths),
        }


@dataclass(frozen=True, slots=True)
class ClaimSet:
    claims: tuple[TrustClaim, ...]
    risk_paths: tuple[str, ...]

    def __post_init__(self) -> None:
        claims = tuple(sorted(self.claims, key=lambda claim: claim.claim_id))
        identities = [claim.claim_id for claim in claims]
        if len(identities) != len(set(identities)):
            raise TrustClaimError("trust manifest contains duplicate claim identities")
        risk_paths = tuple(
            sorted({_canonical_path(path, label="risk path") for path in self.risk_paths})
        )
        object.__setattr__(self, "claims", claims)
        object.__setattr__(self, "risk_paths", risk_paths)

    @property
    def by_id(self) -> Mapping[str, TrustClaim]:
        return MappingProxyType({claim.claim_id: claim for claim in self.claims})

    @property
    def claim_set_digest(self) -> str:
        return _digest(
            [
                "trust-claim-set-v1",
                [claim.to_dict() for claim in self.claims],
                list(self.risk_paths),
            ]
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "claims": [claim.to_dict() for claim in self.claims],
            "risk_paths": list(self.risk_paths),
            "claim_set_digest": self.claim_set_digest,
        }


def normalize_manifest(manifest: Mapping[object, object]) -> ClaimSet:
    """Normalize one closed trust manifest into stable claims and risk paths."""
    if not isinstance(manifest, Mapping):
        raise TrustClaimError("trust manifest must be a mapping")
    if not all(isinstance(key, str) for key in manifest):
        raise TrustClaimError("trust manifest fields must be strings")
    unknown = set(manifest) - TRUST_MANIFEST_FIELDS
    if unknown:
        raise TrustClaimError(
            "trust manifest contains unknown fields: " + ", ".join(sorted(unknown))
        )
    if "risk_paths" not in manifest:
        raise TrustClaimError("trust manifest requires risk_paths")

    claims: list[TrustClaim] = []
    seen: set[str] = set()
    for section in TRUST_CLAIM_SECTIONS:
        raw_items = manifest.get(section, [])
        if not isinstance(raw_items, list):
            raise TrustClaimError(f"trust manifest section {section!r} must be a list")
        for item in raw_items:
            if isinstance(item, str):
                text = item
                paths: object = []
            elif isinstance(item, Mapping):
                if set(item) != {"text", "paths"}:
                    raise TrustClaimError(
                        f"trust claim in {section!r} must contain only text and paths"
                    )
                text = item["text"]
                paths = item["paths"]
                if not isinstance(paths, list):
                    raise TrustClaimError("trust claim paths must be a list")
            else:
                raise TrustClaimError(
                    f"trust claim in {section!r} must be a string or mapping"
                )
            claim = TrustClaim(
                section=section,
                text=_normalized_text(text),
                paths=_canonical_paths(paths, label="trust claim paths"),
            )
            if claim.claim_id in seen:
                raise TrustClaimError(
                    "trust manifest contains duplicate claim identities"
                )
            seen.add(claim.claim_id)
            claims.append(claim)
    risk_paths = _canonical_paths(manifest["risk_paths"], label="risk_paths")
    return ClaimSet(tuple(claims), risk_paths)


@dataclass(frozen=True, slots=True)
class ClaimVerdict:
    verdict: str
    verifier_run_id: str
    verifier_task_id: str
    evidence_digest: str
    verified_tree_sha: str
    carried_from: str | None = None

    def __post_init__(self) -> None:
        if self.verdict not in TRUST_CLAIM_VERDICTS:
            raise TrustClaimError("trust claim verdict is invalid")
        for label, value in (
            ("verifier_run_id", self.verifier_run_id),
            ("verifier_task_id", self.verifier_task_id),
            ("verified_tree_sha", self.verified_tree_sha),
        ):
            if not isinstance(value, str) or not value:
                raise TrustClaimError(f"{label} is invalid")
        if (
            not isinstance(self.evidence_digest, str)
            or _SHA256_RE.fullmatch(self.evidence_digest) is None
        ):
            raise TrustClaimError("evidence_digest is invalid")
        if self.carried_from is not None and (
            not isinstance(self.carried_from, str)
            or _SHA256_RE.fullmatch(self.carried_from) is None
        ):
            raise TrustClaimError("carried_from is invalid")

    def to_dict(self) -> dict[str, object]:
        return {
            "verdict": self.verdict,
            "verifier_run_id": self.verifier_run_id,
            "verifier_task_id": self.verifier_task_id,
            "evidence_digest": self.evidence_digest,
            "verified_tree_sha": self.verified_tree_sha,
            "carried_from": self.carried_from,
        }

    @classmethod
    def from_mapping(cls, raw: Mapping[str, object]) -> Self:
        expected = {
            "verdict",
            "verifier_run_id",
            "verifier_task_id",
            "evidence_digest",
            "verified_tree_sha",
            "carried_from",
        }
        if set(raw) != expected:
            raise TrustClaimError("trust claim receipt binding fields are invalid")
        return cls(**raw)  # type: ignore[arg-type]


@dataclass(frozen=True, slots=True)
class RetirementVerdict:
    verdict: str
    verifier_run_id: str
    verifier_task_id: str
    evidence_digest: str

    def __post_init__(self) -> None:
        if self.verdict not in TRUST_CLAIM_VERDICTS:
            raise TrustClaimError("trust claim retirement verdict is invalid")
        for label, value in (
            ("verifier_run_id", self.verifier_run_id),
            ("verifier_task_id", self.verifier_task_id),
        ):
            if not isinstance(value, str) or not value:
                raise TrustClaimError(f"retirement {label} is invalid")
        if (
            not isinstance(self.evidence_digest, str)
            or _SHA256_RE.fullmatch(self.evidence_digest) is None
        ):
            raise TrustClaimError("retirement evidence_digest is invalid")

    def to_dict(self) -> dict[str, object]:
        return {
            "verdict": self.verdict,
            "verifier_run_id": self.verifier_run_id,
            "verifier_task_id": self.verifier_task_id,
            "evidence_digest": self.evidence_digest,
        }

    @classmethod
    def from_mapping(cls, raw: Mapping[str, object]) -> Self:
        if set(raw) != {
            "verdict",
            "verifier_run_id",
            "verifier_task_id",
            "evidence_digest",
        }:
            raise TrustClaimError("trust claim retirement binding fields are invalid")
        return cls(**raw)  # type: ignore[arg-type]


def _uncovered_risk_paths(
    claim_set: ClaimSet, claims: Mapping[str, ClaimVerdict]
) -> tuple[str, ...]:
    passing_paths = tuple(
        path
        for identity, binding in claims.items()
        if binding.verdict == "pass" and identity in claim_set.by_id
        for path in claim_set.by_id[identity].paths
    )
    return tuple(
        risk_path
        for risk_path in claim_set.risk_paths
        if not _covered_by_any(risk_path, passing_paths)
    )


@dataclass(frozen=True, slots=True)
class TrustClaimReceiptV1:
    task_hash: str
    source_identity: str
    tree_sha: str
    manifest_sha256: str
    claim_set_digest: str
    claims: Mapping[str, ClaimVerdict]
    retirements: Mapping[str, RetirementVerdict]
    uncovered_risk_paths: tuple[str, ...]
    legacy_whole_manifest_pass: bool
    receipt_digest: str
    _claim_set: ClaimSet | None = field(default=None, repr=False, compare=False)

    def __post_init__(self) -> None:
        for label, value in (
            ("source_identity", self.source_identity),
            ("tree_sha", self.tree_sha),
        ):
            if not isinstance(value, str) or not value:
                raise TrustClaimError(f"{label} is invalid")
        for label, value in (
            ("task_hash", self.task_hash),
            ("manifest_sha256", self.manifest_sha256),
            ("claim_set_digest", self.claim_set_digest),
            ("receipt_digest", self.receipt_digest),
        ):
            if not isinstance(value, str) or _SHA256_RE.fullmatch(value) is None:
                raise TrustClaimError(f"{label} is invalid")
        normalized: dict[str, ClaimVerdict] = {}
        for identity, binding in self.claims.items():
            if (
                not isinstance(identity, str)
                or re.fullmatch(r"tc_[0-9a-f]{64}", identity) is None
            ):
                raise TrustClaimError("receipt claim identity is invalid")
            normalized[identity] = (
                binding
                if isinstance(binding, ClaimVerdict)
                else ClaimVerdict.from_mapping(binding)  # type: ignore[arg-type]
            )
        object.__setattr__(
            self, "claims", MappingProxyType(dict(sorted(normalized.items())))
        )
        normalized_retirements: dict[str, RetirementVerdict] = {}
        for identity, binding in self.retirements.items():
            if (
                not isinstance(identity, str)
                or re.fullmatch(r"tc_[0-9a-f]{64}", identity) is None
                or identity in normalized
            ):
                raise TrustClaimError("receipt retirement identity is invalid")
            normalized_retirements[identity] = (
                binding
                if isinstance(binding, RetirementVerdict)
                else RetirementVerdict.from_mapping(binding)  # type: ignore[arg-type]
            )
        object.__setattr__(
            self,
            "retirements",
            MappingProxyType(dict(sorted(normalized_retirements.items()))),
        )
        if not isinstance(self.legacy_whole_manifest_pass, bool):
            raise TrustClaimError("legacy whole-manifest pass marker is invalid")
        if self.legacy_whole_manifest_pass and self.retirements:
            raise TrustClaimError(
                "a legacy whole-manifest receipt cannot retire claims"
            )
        object.__setattr__(
            self,
            "uncovered_risk_paths",
            _canonical_paths(
                self.uncovered_risk_paths, label="uncovered_risk_paths"
            ),
        )
        if self._claim_set is not None:
            if self._claim_set.claim_set_digest != self.claim_set_digest:
                raise TrustClaimError("receipt claim set digest does not match")
            if set(self.claims) != set(self._claim_set.by_id):
                raise TrustClaimError("receipt claims do not match the claim set")
            if self.uncovered_risk_paths != _uncovered_risk_paths(
                self._claim_set, self.claims
            ):
                raise TrustClaimError(
                    "receipt uncovered risk paths do not match the claim verdicts"
                )

    def _payload(self) -> dict[str, object]:
        return {
            "task_hash": self.task_hash,
            "source_identity": self.source_identity,
            "tree_sha": self.tree_sha,
            "manifest_sha256": self.manifest_sha256,
            "claim_set_digest": self.claim_set_digest,
            "claims": {
                identity: binding.to_dict()
                for identity, binding in sorted(self.claims.items())
            },
            "retirements": {
                identity: binding.to_dict()
                for identity, binding in sorted(self.retirements.items())
            },
            "uncovered_risk_paths": list(self.uncovered_risk_paths),
            "legacy_whole_manifest_pass": self.legacy_whole_manifest_pass,
        }

    def to_dict(self) -> dict[str, object]:
        return {**self._payload(), "receipt_digest": self.receipt_digest}

    def digest_is_valid(self) -> bool:
        return self.receipt_digest == _digest(self._payload())

    @classmethod
    def from_mapping(
        cls, raw: Mapping[str, object], *, claim_set: ClaimSet | None = None
    ) -> Self:
        expected = {
            "task_hash",
            "source_identity",
            "tree_sha",
            "manifest_sha256",
            "claim_set_digest",
            "claims",
            "retirements",
            "uncovered_risk_paths",
            "legacy_whole_manifest_pass",
            "receipt_digest",
        }
        if (
            set(raw) != expected
            or not isinstance(raw.get("claims"), Mapping)
            or not isinstance(raw.get("retirements"), Mapping)
            or not isinstance(raw.get("uncovered_risk_paths"), list)
        ):
            raise TrustClaimError("trust claim receipt fields are invalid")
        claims = {
            identity: ClaimVerdict.from_mapping(binding)
            for identity, binding in raw["claims"].items()  # type: ignore[union-attr]
            if isinstance(identity, str) and isinstance(binding, Mapping)
        }
        if len(claims) != len(raw["claims"]):  # type: ignore[arg-type]
            raise TrustClaimError("trust claim receipt bindings are invalid")
        retirements = {
            identity: RetirementVerdict.from_mapping(binding)
            for identity, binding in raw["retirements"].items()  # type: ignore[union-attr]
            if isinstance(identity, str) and isinstance(binding, Mapping)
        }
        if len(retirements) != len(raw["retirements"]):  # type: ignore[arg-type]
            raise TrustClaimError("trust claim receipt retirement bindings are invalid")
        receipt = cls(
            task_hash=raw["task_hash"],  # type: ignore[arg-type]
            source_identity=raw["source_identity"],  # type: ignore[arg-type]
            tree_sha=raw["tree_sha"],  # type: ignore[arg-type]
            manifest_sha256=raw["manifest_sha256"],  # type: ignore[arg-type]
            claim_set_digest=raw["claim_set_digest"],  # type: ignore[arg-type]
            claims=claims,
            retirements=retirements,
            uncovered_risk_paths=tuple(
                raw["uncovered_risk_paths"]  # type: ignore[arg-type]
            ),
            legacy_whole_manifest_pass=raw["legacy_whole_manifest_pass"],  # type: ignore[arg-type]
            receipt_digest=raw["receipt_digest"],  # type: ignore[arg-type]
            _claim_set=claim_set,
        )
        if not receipt.digest_is_valid():
            raise TrustClaimError("trust claim receipt digest is invalid")
        return receipt

    @classmethod
    def build(
        cls,
        *,
        task_hash: str,
        source_identity: str,
        tree_sha: str,
        manifest_sha256: str,
        claim_set: ClaimSet,
        claims: Mapping[str, ClaimVerdict],
        retirements: Mapping[str, RetirementVerdict] = MappingProxyType({}),
        legacy_whole_manifest_pass: bool = False,
    ) -> Self:
        payload = {
            "task_hash": task_hash,
            "source_identity": source_identity,
            "tree_sha": tree_sha,
            "manifest_sha256": manifest_sha256,
            "claim_set_digest": claim_set.claim_set_digest,
            "claims": {
                identity: binding.to_dict()
                for identity, binding in sorted(claims.items())
            },
            "retirements": {
                identity: binding.to_dict()
                for identity, binding in sorted(retirements.items())
            },
            "uncovered_risk_paths": list(_uncovered_risk_paths(claim_set, claims)),
            "legacy_whole_manifest_pass": legacy_whole_manifest_pass,
        }
        return cls(
            **payload,
            receipt_digest=_digest(payload),
            _claim_set=claim_set,
        )  # type: ignore[arg-type]


TrustClaimReceipt = TrustClaimReceiptV1


@dataclass(frozen=True, slots=True)
class Invalidation:
    fresh_claim_ids: tuple[str, ...]
    carried_claims: Mapping[str, ClaimVerdict]
    retired_claim_ids: tuple[str, ...]
    causes: Mapping[str, str]
    carried_receipt_digests: tuple[str, ...] = ()
    changed_paths: tuple[str, ...] = ()
    base_moved: bool = False
    risk_paths_added: tuple[str, ...] = ()
    _retired_claims: tuple[TrustClaim, ...] = field(
        default=(), repr=False, compare=False
    )
    previous_receipt_digest: str | None = None

    def __post_init__(self) -> None:
        fresh = tuple(sorted(set(self.fresh_claim_ids)))
        retired = tuple(sorted(set(self.retired_claim_ids)))
        retired_claims = tuple(
            sorted(self._retired_claims, key=lambda claim: claim.claim_id)
        )
        carried = MappingProxyType(dict(sorted(self.carried_claims.items())))
        causes = MappingProxyType(dict(sorted(self.causes.items())))
        expected = set(fresh) | set(retired)
        if set(causes) != expected or any(
            cause not in INVALIDATION_CAUSES for cause in causes.values()
        ):
            raise TrustClaimError("invalidation causes do not match fresh claims")
        if set(fresh) & set(carried) or set(retired) & set(carried):
            raise TrustClaimError("invalidation claim sets overlap")
        if {claim.claim_id for claim in retired_claims} != set(retired):
            raise TrustClaimError("invalidation retirement content is incomplete")
        if not isinstance(self.base_moved, bool):
            raise TrustClaimError("base_moved must be a boolean")
        object.__setattr__(self, "fresh_claim_ids", fresh)
        object.__setattr__(self, "retired_claim_ids", retired)
        object.__setattr__(self, "_retired_claims", retired_claims)
        object.__setattr__(self, "carried_claims", carried)
        object.__setattr__(self, "causes", causes)
        object.__setattr__(
            self,
            "carried_receipt_digests",
            tuple(sorted(set(self.carried_receipt_digests))),
        )
        object.__setattr__(
            self,
            "changed_paths",
            _canonical_paths(self.changed_paths, label="changed_paths"),
        )
        object.__setattr__(
            self,
            "risk_paths_added",
            _canonical_paths(self.risk_paths_added, label="risk_paths_added"),
        )

    @property
    def fresh(self) -> tuple[str, ...]:
        return self.fresh_claim_ids

    @property
    def carried(self) -> Mapping[str, ClaimVerdict]:
        return self.carried_claims

    @property
    def retired(self) -> tuple[str, ...]:
        return self.retired_claim_ids


def invalidate_claims(
    previous: TrustClaimReceiptV1 | None,
    current: ClaimSet,
    *,
    changed_paths: Sequence[str],
    base_moved: bool,
    risk_paths_added: Sequence[str],
) -> Invalidation:
    """Classify current, carried, and retired claims using only trusted inputs."""
    changed = set(_canonical_paths(changed_paths, label="changed_paths"))
    added_risks = set(_canonical_paths(risk_paths_added, label="risk_paths_added"))
    if not isinstance(base_moved, bool):
        raise TrustClaimError("base_moved must be a boolean")
    if previous is not None:
        if not previous.digest_is_valid():
            raise TrustClaimError("previous trust claim receipt digest is invalid")
        if previous._claim_set is None:
            raise TrustClaimError("previous trust claim set is unavailable")
        if any(binding.verdict != "pass" for binding in previous.retirements.values()):
            raise TrustClaimError(
                "previous trust claim receipt has unresolved retirements"
            )
    previous_claims = previous.claims if previous is not None else {}
    current_by_id = current.by_id
    fresh: list[str] = []
    carried: dict[str, ClaimVerdict] = {}
    causes: dict[str, str] = {}

    for identity, claim in current_by_id.items():
        prior = previous_claims.get(identity)
        if prior is not None and prior.verdict == "inconclusive":
            fresh.append(identity)
            causes[identity] = "prior-inconclusive"
        elif previous is not None and previous.legacy_whole_manifest_pass:
            # Exact-source legacy reuse is handled by receipt_covers.  Once the
            # source changes, the first claim-level receipt must judge every
            # current claim before selective reuse can begin.
            fresh.append(identity)
            causes[identity] = "repository-wide-dependency"
        elif prior is None:
            fresh.append(identity)
            causes[identity] = "text-or-coverage-changed"
        elif claim.paths and any(
            _covered_by_any(path, claim.paths) for path in changed
        ):
            fresh.append(identity)
            causes[identity] = "covered-path-changed"
        elif not claim.paths and base_moved:
            fresh.append(identity)
            causes[identity] = "base-moved"
        elif not claim.paths and added_risks:
            fresh.append(identity)
            causes[identity] = "new-risk-path"
        elif not claim.paths and changed:
            fresh.append(identity)
            causes[identity] = "repository-wide-dependency"
        else:
            carried[identity] = prior

    retired = sorted(set(previous_claims) - set(current_by_id))
    causes.update({identity: "retired" for identity in retired})
    carried_digests = (
        (previous.receipt_digest,) if previous is not None and carried else ()
    )
    return Invalidation(
        tuple(fresh),
        carried,
        tuple(retired),
        causes,
        carried_digests,
        tuple(changed),
        base_moved,
        tuple(added_risks),
        tuple(previous._claim_set.by_id[identity] for identity in retired)
        if previous is not None
        else (),
        previous.receipt_digest if previous is not None else None,
    )


@dataclass(frozen=True, slots=True)
class TrustClaimTaskV1:
    generation_ref: str
    source_identity: str
    tree_sha: str
    manifest_sha256: str
    claim_set_digest: str
    invalidated_claims: tuple[TrustClaim, ...]
    retirements: tuple[TrustClaim, ...]
    carried_receipt_digests: tuple[str, ...]
    delta_from_tree_sha: str | None
    changed_paths: tuple[str, ...]
    changed_paths_digest: str
    base_moved: bool
    risk_paths_added: tuple[str, ...]
    previous_receipt_digest: str | None
    _claim_set: ClaimSet | None = field(default=None, repr=False, compare=False)
    _carried_claims: Mapping[str, ClaimVerdict] = field(
        default_factory=dict, repr=False, compare=False
    )

    def __post_init__(self) -> None:
        for label, value in (
            ("generation_ref", self.generation_ref),
            ("source_identity", self.source_identity),
            ("tree_sha", self.tree_sha),
        ):
            if not isinstance(value, str) or not value:
                raise TrustClaimError(f"{label} is invalid")
        for label, value in (
            ("manifest_sha256", self.manifest_sha256),
            ("claim_set_digest", self.claim_set_digest),
        ):
            if not isinstance(value, str) or _SHA256_RE.fullmatch(value) is None:
                raise TrustClaimError(f"{label} is invalid")
        invalidated = tuple(
            sorted(self.invalidated_claims, key=lambda claim: claim.claim_id)
        )
        retirements = tuple(sorted(self.retirements, key=lambda claim: claim.claim_id))
        invalidated_ids = [claim.claim_id for claim in invalidated]
        retirement_ids = [claim.claim_id for claim in retirements]
        if len(invalidated_ids) != len(set(invalidated_ids)):
            raise TrustClaimError("invalidated claims contain duplicate identities")
        if len(retirement_ids) != len(set(retirement_ids)):
            raise TrustClaimError("retirements contain duplicate identities")
        if set(retirement_ids) & set(invalidated_ids):
            raise TrustClaimError("invalidated claims and retirements overlap")
        if self.delta_from_tree_sha is not None and (
            not isinstance(self.delta_from_tree_sha, str)
            or not self.delta_from_tree_sha
        ):
            raise TrustClaimError("delta_from_tree_sha is invalid")
        for digest in self.carried_receipt_digests:
            if not isinstance(digest, str) or _SHA256_RE.fullmatch(digest) is None:
                raise TrustClaimError("carried receipt digest is invalid")
        if self.previous_receipt_digest is not None and (
            not isinstance(self.previous_receipt_digest, str)
            or _SHA256_RE.fullmatch(self.previous_receipt_digest) is None
        ):
            raise TrustClaimError("previous receipt digest is invalid")
        if (
            not isinstance(self.changed_paths_digest, str)
            or _SHA256_RE.fullmatch(self.changed_paths_digest) is None
        ):
            raise TrustClaimError("changed_paths_digest is invalid")
        if not isinstance(self.base_moved, bool):
            raise TrustClaimError("base_moved must be a boolean")
        object.__setattr__(self, "invalidated_claims", invalidated)
        object.__setattr__(self, "retirements", retirements)
        object.__setattr__(
            self,
            "carried_receipt_digests",
            tuple(sorted(set(self.carried_receipt_digests))),
        )
        object.__setattr__(
            self,
            "changed_paths",
            _canonical_paths(self.changed_paths, label="changed_paths"),
        )
        object.__setattr__(
            self,
            "risk_paths_added",
            _canonical_paths(self.risk_paths_added, label="risk_paths_added"),
        )
        if self.changed_paths_digest != _changed_paths_digest(self.changed_paths):
            raise TrustClaimError("changed_paths_digest does not match changed_paths")
        object.__setattr__(
            self, "_carried_claims", MappingProxyType(dict(self._carried_claims))
        )
        if self._claim_set is not None and (
            self._claim_set.claim_set_digest != self.claim_set_digest
            or set(self._claim_set.by_id)
            != {claim.claim_id for claim in invalidated} | set(self._carried_claims)
        ):
            raise TrustClaimError("task claim set does not match task claims")

    def hash_payload(self) -> dict[str, object]:
        return {
            "generation_ref": self.generation_ref,
            "source_identity": self.source_identity,
            "tree_sha": self.tree_sha,
            "manifest_sha256": self.manifest_sha256,
            "claim_set_digest": self.claim_set_digest,
            "invalidated_claims": [
                claim.to_dict() for claim in self.invalidated_claims
            ],
            "retirements": [claim.to_dict() for claim in self.retirements],
            "carried_receipt_digests": list(self.carried_receipt_digests),
            "delta_from_tree_sha": self.delta_from_tree_sha,
            "changed_paths": list(self.changed_paths),
            "changed_paths_digest": self.changed_paths_digest,
            "base_moved": self.base_moved,
            "risk_paths_added": list(self.risk_paths_added),
            "previous_receipt_digest": self.previous_receipt_digest,
        }

    @property
    def task_hash(self) -> str:
        return _digest(self.hash_payload())

    def to_dict(self) -> dict[str, object]:
        return {**self.hash_payload(), "task_hash": self.task_hash}

    @classmethod
    def from_mapping(
        cls,
        raw: Mapping[str, object],
        *,
        claim_set: ClaimSet,
        previous: TrustClaimReceiptV1 | None,
    ) -> Self:
        fields = {
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
            "task_hash",
            "previous_receipt_digest",
        }
        if set(raw) != fields:
            raise TrustClaimError("trust claim task fields are invalid")
        raw_invalidated = raw["invalidated_claims"]
        list_fields = (
            "retirements",
            "carried_receipt_digests",
            "changed_paths",
            "risk_paths_added",
        )
        if not isinstance(raw_invalidated, list) or any(
            not isinstance(raw[field], list)
            or (
                field != "retirements"
                and not all(isinstance(value, str) for value in raw[field])
            )
            for field in list_fields
        ):
            raise TrustClaimError("trust claim task claims are invalid")
        raw_hash_payload = {
            key: value for key, value in raw.items() if key != "task_hash"
        }
        if raw["task_hash"] != _digest(raw_hash_payload):
            raise TrustClaimError("trust claim task hash is invalid")
        invalidated: list[TrustClaim] = []
        for item in raw_invalidated:
            if not isinstance(item, Mapping) or set(item) != {
                "claim_id",
                "section",
                "text",
                "paths",
            }:
                raise TrustClaimError("trust claim task claim is invalid")
            paths = item["paths"]
            if not isinstance(paths, list):
                raise TrustClaimError("trust claim task claim paths are invalid")
            claim = TrustClaim(
                section=item["section"],  # type: ignore[arg-type]
                text=item["text"],  # type: ignore[arg-type]
                paths=tuple(paths),
            )
            if item["claim_id"] != claim.claim_id:
                raise TrustClaimError("trust claim task claim identity is forged")
            invalidated.append(claim)
        retirements: list[TrustClaim] = []
        for item in raw["retirements"]:  # type: ignore[union-attr]
            if not isinstance(item, Mapping) or set(item) != {
                "claim_id",
                "section",
                "text",
                "paths",
            }:
                raise TrustClaimError("trust claim task retirement is invalid")
            paths = item["paths"]
            if not isinstance(paths, list):
                raise TrustClaimError("trust claim task retirement paths are invalid")
            claim = TrustClaim(
                section=item["section"],  # type: ignore[arg-type]
                text=item["text"],  # type: ignore[arg-type]
                paths=tuple(paths),
            )
            if item["claim_id"] != claim.claim_id:
                raise TrustClaimError("trust claim task retirement identity is forged")
            retirements.append(claim)
        if previous is None:
            raise TrustClaimError(
                "previous trust claim receipt is required for restoration"
            )
        if not previous.digest_is_valid():
            raise TrustClaimError("previous trust claim receipt digest is invalid")
        if raw["previous_receipt_digest"] != previous.receipt_digest:
            raise TrustClaimError("previous receipt does not match the task binding")
        if previous._claim_set is None:
            raise TrustClaimError(
                "previous trust claim set is required for restoration"
            )
        expected_risks = tuple(
            sorted(set(claim_set.risk_paths) - set(previous._claim_set.risk_paths))
        )
        if tuple(sorted(raw["risk_paths_added"])) != expected_risks:  # type: ignore[arg-type]
            raise TrustClaimError("trust claim risk-path delta is invalid")
        expected = invalidate_claims(
            previous,
            claim_set,
            changed_paths=raw["changed_paths"],  # type: ignore[arg-type]
            base_moved=raw["base_moved"],  # type: ignore[arg-type]
            risk_paths_added=raw["risk_paths_added"],  # type: ignore[arg-type]
        )
        if {claim.claim_id for claim in invalidated} != set(expected.fresh_claim_ids):
            raise TrustClaimError("trust claim invalidated set is invalid")
        expected_retired = {
            identity: previous._claim_set.by_id[identity]
            for identity in expected.retired_claim_ids
        }
        if {claim.claim_id: claim for claim in retirements} != expected_retired:
            raise TrustClaimError("trust claim retirements are invalid")
        carried = expected.carried_claims
        task = cls(
            generation_ref=raw["generation_ref"],  # type: ignore[arg-type]
            source_identity=raw["source_identity"],  # type: ignore[arg-type]
            tree_sha=raw["tree_sha"],  # type: ignore[arg-type]
            manifest_sha256=raw["manifest_sha256"],  # type: ignore[arg-type]
            claim_set_digest=raw["claim_set_digest"],  # type: ignore[arg-type]
            invalidated_claims=tuple(invalidated),
            retirements=tuple(retirements),
            carried_receipt_digests=tuple(
                raw["carried_receipt_digests"]  # type: ignore[arg-type]
            ),
            delta_from_tree_sha=raw["delta_from_tree_sha"],  # type: ignore[arg-type]
            changed_paths=tuple(raw["changed_paths"]),  # type: ignore[arg-type]
            changed_paths_digest=raw["changed_paths_digest"],  # type: ignore[arg-type]
            base_moved=raw["base_moved"],  # type: ignore[arg-type]
            risk_paths_added=tuple(raw["risk_paths_added"]),  # type: ignore[arg-type]
            previous_receipt_digest=raw["previous_receipt_digest"],  # type: ignore[arg-type]
            _claim_set=claim_set,
            _carried_claims=carried,
        )
        if raw["task_hash"] != task.task_hash:
            raise TrustClaimError("trust claim task hash is invalid")
        if task.delta_from_tree_sha != previous.tree_sha:
            raise TrustClaimError(
                "trust claim delta tree does not match the previous receipt"
            )
        if task.carried_receipt_digests != expected.carried_receipt_digests:
            raise TrustClaimError("trust claim carried receipt binding is invalid")
        return task


def build_trust_claim_task(
    current: ClaimSet,
    invalidation: Invalidation,
    *,
    generation_ref: str,
    source_identity: str,
    tree_sha: str,
    manifest_sha256: str,
    delta_from_tree_sha: str | None,
    changed_paths: Sequence[str],
) -> TrustClaimTaskV1 | None:
    """Build a hash-bound delta task, or return ``None`` for no work."""
    normalized_changed = _canonical_paths(changed_paths, label="changed_paths")
    if normalized_changed != invalidation.changed_paths:
        raise TrustClaimError("task changed paths do not match the invalidation diff")
    if not invalidation.fresh_claim_ids and not invalidation.retired_claim_ids:
        return None
    by_id = current.by_id
    try:
        invalidated = tuple(by_id[identity] for identity in invalidation.fresh_claim_ids)
    except KeyError as exc:
        raise TrustClaimError("invalidation references a non-current claim") from exc
    if set(by_id) != set(invalidation.fresh_claim_ids) | set(
        invalidation.carried_claims
    ):
        raise TrustClaimError("invalidation does not partition current claims")
    return TrustClaimTaskV1(
        generation_ref=generation_ref,
        source_identity=source_identity,
        tree_sha=tree_sha,
        manifest_sha256=manifest_sha256,
        claim_set_digest=current.claim_set_digest,
        invalidated_claims=invalidated,
        retirements=invalidation._retired_claims,
        carried_receipt_digests=invalidation.carried_receipt_digests,
        delta_from_tree_sha=delta_from_tree_sha,
        changed_paths=normalized_changed,
        changed_paths_digest=_changed_paths_digest(normalized_changed),
        base_moved=invalidation.base_moved,
        risk_paths_added=invalidation.risk_paths_added,
        previous_receipt_digest=invalidation.previous_receipt_digest,
        _claim_set=current,
        _carried_claims=invalidation.carried_claims,
    )


build_claim_task = build_trust_claim_task


def _fresh_result_rows(fresh_results: object) -> list[Mapping[str, object]]:
    if isinstance(fresh_results, Mapping) and "claim_verdicts" in fresh_results:
        rows = fresh_results.get("claim_verdicts")
        if not isinstance(rows, list):
            raise TrustClaimError("fresh claim verdicts must be a list")
        common = {
            field: fresh_results.get(field)
            for field in (
                "verifier_run_id",
                "verifier_task_id",
                "evidence_digest",
                "verified_tree_sha",
                "task_hash",
            )
            if field in fresh_results
        }
        if not all(isinstance(row, Mapping) for row in rows):
            raise TrustClaimError("fresh claim verdict entry is invalid")
        return [{**common, **row} for row in rows]  # type: ignore[misc]
    if isinstance(fresh_results, Mapping):
        rows = []
        for identity, raw in fresh_results.items():
            if not isinstance(identity, str) or not isinstance(raw, Mapping):
                raise TrustClaimError("fresh claim verdict mapping is invalid")
            row = dict(raw)
            if "claim_id" in row and row["claim_id"] != identity:
                raise TrustClaimError("fresh claim result identity is forged")
            row["claim_id"] = identity
            rows.append(row)
        return rows
    if isinstance(fresh_results, Sequence) and not isinstance(
        fresh_results, (str, bytes)
    ):
        if not all(isinstance(row, Mapping) for row in fresh_results):
            raise TrustClaimError("fresh claim verdict entry is invalid")
        return list(fresh_results)  # type: ignore[return-value]
    raise TrustClaimError("fresh claim results are invalid")


def _validated_fresh_results(
    task: TrustClaimTaskV1, fresh_results: object
) -> dict[str, ClaimVerdict]:
    expected = {claim.claim_id for claim in task.invalidated_claims} | set(
        claim.claim_id for claim in task.retirements
    )
    rows = _fresh_result_rows(fresh_results)
    parsed: dict[str, ClaimVerdict] = {}
    required = {
        "claim_id",
        "verdict",
        "verifier_run_id",
        "verifier_task_id",
        "evidence_digest",
        "verified_tree_sha",
    }
    allowed = required | {"rationale", "task_hash"}
    for row in rows:
        if set(row) - allowed or not required <= set(row):
            raise TrustClaimError("fresh claim result fields are invalid")
        identity = row["claim_id"]
        if not isinstance(identity, str) or identity in parsed:
            raise TrustClaimError("fresh claim result identity is invalid or duplicate")
        if identity not in expected:
            raise TrustClaimError("fresh result references a claim not in the task")
        if "task_hash" in row and row["task_hash"] != task.task_hash:
            raise TrustClaimError("fresh claim result task binding is forged")
        if row["verified_tree_sha"] != task.tree_sha:
            raise TrustClaimError("fresh claim result tree binding is forged")
        rationale = row.get("rationale")
        if rationale is not None and (
            not isinstance(rationale, str) or not rationale.strip()
        ):
            raise TrustClaimError("fresh claim result rationale is invalid")
        parsed[identity] = ClaimVerdict(
            verdict=row["verdict"],  # type: ignore[arg-type]
            verifier_run_id=row["verifier_run_id"],  # type: ignore[arg-type]
            verifier_task_id=row["verifier_task_id"],  # type: ignore[arg-type]
            evidence_digest=row["evidence_digest"],  # type: ignore[arg-type]
            verified_tree_sha=row["verified_tree_sha"],  # type: ignore[arg-type]
            carried_from=None,
        )
    missing = expected - set(parsed)
    if missing:
        raise TrustClaimError("fresh results are missing claims in the task")
    return parsed


def _aggregate(
    claim_set: ClaimSet,
    claims: Mapping[str, ClaimVerdict],
    retirement_results: Sequence[RetirementVerdict] = (),
) -> str:
    verdicts = [binding.verdict for binding in claims.values()] + [
        binding.verdict for binding in retirement_results
    ]
    if "fail" in verdicts:
        return "fail"
    if "inconclusive" in verdicts:
        return "inconclusive"
    passing = {
        identity
        for identity, binding in claims.items()
        if binding.verdict == "pass"
    }
    if passing != set(claim_set.by_id):
        return "inconclusive"
    if _uncovered_risk_paths(claim_set, claims):
        return "inconclusive"
    return "pass"


def _validate_task_obligations(
    previous: TrustClaimReceiptV1 | None,
    task: TrustClaimTaskV1,
) -> Invalidation:
    if task._claim_set is None:
        raise TrustClaimError("claim task is not bound to its normalized claim set")
    if task.previous_receipt_digest != (
        previous.receipt_digest if previous is not None else None
    ):
        raise TrustClaimError("previous receipt does not match the task binding")
    if previous is not None:
        if not previous.digest_is_valid():
            raise TrustClaimError("previous trust claim receipt digest is invalid")
        if previous._claim_set is None:
            raise TrustClaimError("previous trust claim set is unavailable")
        if task.delta_from_tree_sha != previous.tree_sha:
            raise TrustClaimError(
                "trust claim delta tree does not match the previous receipt"
            )
        expected_risks = tuple(
            sorted(
                set(task._claim_set.risk_paths) - set(previous._claim_set.risk_paths)
            )
        )
        if task.risk_paths_added != expected_risks:
            raise TrustClaimError("trust claim risk-path delta is invalid")
    expected = invalidate_claims(
        previous,
        task._claim_set,
        changed_paths=task.changed_paths,
        base_moved=task.base_moved,
        risk_paths_added=task.risk_paths_added,
    )
    if {claim.claim_id for claim in task.invalidated_claims} != set(
        expected.fresh_claim_ids
    ):
        raise TrustClaimError("trust claim invalidated set is invalid")
    if {claim.claim_id: claim for claim in task.retirements} != {
        claim.claim_id: claim for claim in expected._retired_claims
    }:
        raise TrustClaimError("trust claim retirements are invalid")
    if dict(task._carried_claims) != dict(expected.carried_claims):
        raise TrustClaimError("trust claim carried set is invalid")
    if task.carried_receipt_digests != expected.carried_receipt_digests:
        raise TrustClaimError("trust claim carried receipt binding is invalid")
    return expected


def compose_claim_verdict(
    previous: TrustClaimReceiptV1 | None,
    task: TrustClaimTaskV1,
    fresh_results: object,
    *,
    changed_paths: Sequence[str],
) -> tuple[TrustClaimReceiptV1, str]:
    """Merge exact carried bindings and complete fresh results into a receipt."""
    if isinstance(fresh_results, Mapping) and "claim_verdicts" in fresh_results:
        from ..runners._review_schema import (
            TrustClaimResultError,
            validate_trust_claim_result,
        )

        try:
            validate_trust_claim_result(fresh_results)
        except TrustClaimResultError as exc:
            raise TrustClaimError(str(exc)) from exc
    if _changed_paths_digest(changed_paths) != task.changed_paths_digest:
        raise TrustClaimError("composition changed paths do not match the task diff")
    _validate_task_obligations(previous, task)
    assert task._claim_set is not None
    expected_carried_receipts = (
        (previous.receipt_digest,)
        if previous is not None and task._carried_claims
        else ()
    )
    if task.carried_receipt_digests != expected_carried_receipts:
        raise TrustClaimError("previous receipt does not match the carried binding")
    fresh = _validated_fresh_results(task, fresh_results)
    claims: dict[str, ClaimVerdict] = {}
    for identity, binding in task._carried_claims.items():
        if binding.verdict == "inconclusive":
            raise TrustClaimError("an inconclusive claim verdict cannot be carried")
        if previous is None or previous.claims.get(identity) != binding:
            raise TrustClaimError("carried claim binding is not in the prior receipt")
        claims[identity] = ClaimVerdict(
            verdict=binding.verdict,
            verifier_run_id=binding.verifier_run_id,
            verifier_task_id=binding.verifier_task_id,
            evidence_digest=binding.evidence_digest,
            verified_tree_sha=binding.verified_tree_sha,
            carried_from=previous.receipt_digest,
        )
    current_ids = set(task._claim_set.by_id)
    for identity in current_ids - set(claims):
        binding = fresh.get(identity)
        if binding is None:
            raise TrustClaimError("fresh current claim result is missing")
        claims[identity] = binding
    retirement_results = {
        claim.claim_id: RetirementVerdict(
            verdict=fresh[claim.claim_id].verdict,
            verifier_run_id=fresh[claim.claim_id].verifier_run_id,
            verifier_task_id=fresh[claim.claim_id].verifier_task_id,
            evidence_digest=fresh[claim.claim_id].evidence_digest,
        )
        for claim in task.retirements
    }
    aggregate = _aggregate(task._claim_set, claims, tuple(retirement_results.values()))
    if isinstance(fresh_results, Mapping) and "verification_verdict" in fresh_results:
        claimed_aggregate = fresh_results["verification_verdict"]
        if (
            claimed_aggregate not in TRUST_CLAIM_VERDICTS
            or claimed_aggregate != aggregate
        ):
            raise TrustClaimError(
                "verification_verdict does not match the claim aggregate"
            )
    receipt = TrustClaimReceiptV1.build(
        task_hash=task.task_hash,
        source_identity=task.source_identity,
        tree_sha=task.tree_sha,
        manifest_sha256=task.manifest_sha256,
        claim_set=task._claim_set,
        claims=claims,
        retirements=retirement_results,
    )
    return receipt, aggregate


def carry_trust_claim_receipt(
    previous: TrustClaimReceiptV1,
    current: ClaimSet,
    invalidation: Invalidation,
    *,
    source_identity: str,
    tree_sha: str,
    manifest_sha256: str,
    delta_from_tree_sha: str,
    changed_paths: Sequence[str],
) -> tuple[TrustClaimReceiptV1, str]:
    """Bind an all-carried claim set to a new head without launching a task."""
    if not previous.digest_is_valid():
        raise TrustClaimError("previous trust claim receipt digest is invalid")
    if delta_from_tree_sha != previous.tree_sha:
        raise TrustClaimError(
            "trust claim delta tree does not match the previous receipt"
        )
    if _changed_paths_digest(changed_paths) != _changed_paths_digest(
        invalidation.changed_paths
    ):
        raise TrustClaimError("carry changed paths do not match the invalidation diff")
    if previous._claim_set is None:
        raise TrustClaimError("previous trust claim set is unavailable")
    expected = invalidate_claims(
        previous,
        current,
        changed_paths=changed_paths,
        base_moved=invalidation.base_moved,
        risk_paths_added=tuple(
            sorted(set(current.risk_paths) - set(previous._claim_set.risk_paths))
        ),
    )
    if invalidation != expected:
        raise TrustClaimError("carry invalidation does not match the previous receipt and diff")
    if invalidation.fresh_claim_ids or invalidation.retired_claim_ids:
        raise TrustClaimError("carry-only receipt requires zero invalidations")
    if set(invalidation.carried_claims) != set(current.by_id):
        raise TrustClaimError("carry-only invalidation does not cover every claim")
    if invalidation.carried_receipt_digests != expected.carried_receipt_digests:
        raise TrustClaimError("previous receipt does not match the carried binding")

    carried: dict[str, ClaimVerdict] = {}
    for identity, binding in invalidation.carried_claims.items():
        if binding.verdict == "inconclusive":
            raise TrustClaimError("an inconclusive claim verdict cannot be carried")
        if previous.claims.get(identity) != binding:
            raise TrustClaimError("carried claim binding is not in the prior receipt")
        carried[identity] = ClaimVerdict(
            verdict=binding.verdict,
            verifier_run_id=binding.verifier_run_id,
            verifier_task_id=binding.verifier_task_id,
            evidence_digest=binding.evidence_digest,
            verified_tree_sha=binding.verified_tree_sha,
            carried_from=previous.receipt_digest,
        )
    task_hash = _digest(
        [
            "trust-claim-carry-v1",
            previous.receipt_digest,
            source_identity,
            tree_sha,
            manifest_sha256,
            current.claim_set_digest,
            delta_from_tree_sha,
            _changed_paths_digest(changed_paths),
        ]
    )
    receipt = TrustClaimReceiptV1.build(
        task_hash=task_hash,
        source_identity=source_identity,
        tree_sha=tree_sha,
        manifest_sha256=manifest_sha256,
        claim_set=current,
        claims=carried,
    )
    return receipt, _aggregate(current, carried)


def receipt_covers(
    receipt: TrustClaimReceiptV1,
    *,
    source_identity: str,
    tree_sha: str,
    manifest_sha256: str,
    risk_paths: Sequence[str],
) -> bool:
    """Mechanically decide whether one complete passing receipt is reusable."""
    try:
        normalized_risks = _canonical_paths(risk_paths, label="risk_paths")
    except TrustClaimError:
        return False
    return bool(
        receipt.digest_is_valid()
        and receipt._claim_set is not None
        and receipt.source_identity == source_identity
        and receipt.tree_sha == tree_sha
        and receipt.manifest_sha256 == manifest_sha256
        and receipt._claim_set.risk_paths == normalized_risks
        and receipt._claim_set.claim_set_digest == receipt.claim_set_digest
        and (
            (
                receipt.legacy_whole_manifest_pass
                and all(
                    binding.verdict == "pass" for binding in receipt.claims.values()
                )
            )
            or _aggregate(
                receipt._claim_set,
                receipt.claims,
                tuple(receipt.retirements.values()),
            )
            == "pass"
        )
    )


def legacy_receipt(
    manifest: Mapping[object, object],
    *,
    source_identity: str,
    tree_sha: str,
    manifest_sha256: str,
    verifier_run_id: str,
    verifier_task_id: str,
) -> TrustClaimReceiptV1:
    """Project one historical whole-manifest pass into conservative claims."""
    claim_set = normalize_manifest(manifest)
    task_hash = _digest(
        [
            "trust-claim-legacy-v1",
            source_identity,
            tree_sha,
            manifest_sha256,
            verifier_run_id,
            verifier_task_id,
        ]
    )
    bindings = {
        claim.claim_id: ClaimVerdict(
            verdict="pass",
            verifier_run_id=verifier_run_id,
            verifier_task_id=verifier_task_id,
            evidence_digest=manifest_sha256,
            verified_tree_sha=tree_sha,
            carried_from=None,
        )
        for claim in claim_set.claims
    }
    return TrustClaimReceiptV1.build(
        task_hash=task_hash,
        source_identity=source_identity,
        tree_sha=tree_sha,
        manifest_sha256=manifest_sha256,
        claim_set=claim_set,
        claims=bindings,
        legacy_whole_manifest_pass=True,
    )


__all__ = [
    "ClaimSet",
    "ClaimVerdict",
    "Invalidation",
    "RetirementVerdict",
    "TrustClaim",
    "TrustClaimError",
    "TrustClaimReceipt",
    "TrustClaimReceiptV1",
    "TrustClaimTaskV1",
    "build_claim_task",
    "build_trust_claim_task",
    "carry_trust_claim_receipt",
    "claim_id",
    "compose_claim_verdict",
    "invalidate_claims",
    "legacy_receipt",
    "normalize_manifest",
    "receipt_covers",
]
