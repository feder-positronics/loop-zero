"""Content-addressed trust-manifest claims and mechanical verdict reuse."""

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
        if not isinstance(self.evidence_digest, str) or _SHA256_RE.fullmatch(
            self.evidence_digest
        ) is None:
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
class TrustClaimReceiptV1:
    task_hash: str
    source_identity: str
    tree_sha: str
    manifest_sha256: str
    claim_set_digest: str
    claims: Mapping[str, ClaimVerdict]
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
        object.__setattr__(self, "claims", MappingProxyType(dict(sorted(normalized.items()))))
        if self._claim_set is not None:
            if self._claim_set.claim_set_digest != self.claim_set_digest:
                raise TrustClaimError("receipt claim set digest does not match")
            if set(self.claims) != set(self._claim_set.by_id):
                raise TrustClaimError("receipt claims do not match the claim set")

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
            "receipt_digest",
        }
        if set(raw) != expected or not isinstance(raw.get("claims"), Mapping):
            raise TrustClaimError("trust claim receipt fields are invalid")
        claims = {
            identity: ClaimVerdict.from_mapping(binding)
            for identity, binding in raw["claims"].items()  # type: ignore[union-attr]
            if isinstance(identity, str) and isinstance(binding, Mapping)
        }
        if len(claims) != len(raw["claims"]):  # type: ignore[arg-type]
            raise TrustClaimError("trust claim receipt bindings are invalid")
        receipt = cls(
            task_hash=raw["task_hash"],  # type: ignore[arg-type]
            source_identity=raw["source_identity"],  # type: ignore[arg-type]
            tree_sha=raw["tree_sha"],  # type: ignore[arg-type]
            manifest_sha256=raw["manifest_sha256"],  # type: ignore[arg-type]
            claim_set_digest=raw["claim_set_digest"],  # type: ignore[arg-type]
            claims=claims,
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

    def __post_init__(self) -> None:
        fresh = tuple(sorted(set(self.fresh_claim_ids)))
        retired = tuple(sorted(set(self.retired_claim_ids)))
        carried = MappingProxyType(dict(sorted(self.carried_claims.items())))
        causes = MappingProxyType(dict(sorted(self.causes.items())))
        expected = set(fresh) | set(retired)
        if set(causes) != expected or any(
            cause not in INVALIDATION_CAUSES for cause in causes.values()
        ):
            raise TrustClaimError("invalidation causes do not match fresh claims")
        if set(fresh) & set(carried) or set(retired) & set(carried):
            raise TrustClaimError("invalidation claim sets overlap")
        object.__setattr__(self, "fresh_claim_ids", fresh)
        object.__setattr__(self, "retired_claim_ids", retired)
        object.__setattr__(self, "carried_claims", carried)
        object.__setattr__(self, "causes", causes)
        object.__setattr__(
            self,
            "carried_receipt_digests",
            tuple(sorted(set(self.carried_receipt_digests))),
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
    if previous is not None and not previous.digest_is_valid():
        raise TrustClaimError("previous trust claim receipt digest is invalid")
    previous_claims = previous.claims if previous is not None else {}
    current_by_id = current.by_id
    fresh: list[str] = []
    carried: dict[str, ClaimVerdict] = {}
    causes: dict[str, str] = {}

    for identity, claim in current_by_id.items():
        prior = previous_claims.get(identity)
        if prior is None or prior.verdict == "inconclusive":
            fresh.append(identity)
            causes[identity] = "text-or-coverage-changed"
        elif claim.paths and set(claim.paths) & changed:
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
        tuple(fresh), carried, tuple(retired), causes, carried_digests
    )


@dataclass(frozen=True, slots=True)
class TrustClaimTaskV1:
    generation_ref: str
    source_identity: str
    tree_sha: str
    manifest_sha256: str
    claim_set_digest: str
    invalidated_claims: tuple[TrustClaim, ...]
    retirements: tuple[str, ...]
    carried_receipt_digests: tuple[str, ...]
    delta_from_tree_sha: str | None
    changed_paths: tuple[str, ...]
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
        retirements = tuple(sorted(set(self.retirements)))
        invalidated_ids = [claim.claim_id for claim in invalidated]
        if len(invalidated_ids) != len(set(invalidated_ids)):
            raise TrustClaimError("invalidated claims contain duplicate identities")
        if any(re.fullmatch(r"tc_[0-9a-f]{64}", value) is None for value in retirements):
            raise TrustClaimError("retirement claim identity is invalid")
        if set(retirements) & set(invalidated_ids):
            raise TrustClaimError("invalidated claims and retirements overlap")
        if self.delta_from_tree_sha is not None and (
            not isinstance(self.delta_from_tree_sha, str)
            or not self.delta_from_tree_sha
        ):
            raise TrustClaimError("delta_from_tree_sha is invalid")
        for digest in self.carried_receipt_digests:
            if not isinstance(digest, str) or _SHA256_RE.fullmatch(digest) is None:
                raise TrustClaimError("carried receipt digest is invalid")
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
            "retirements": list(self.retirements),
            "carried_receipt_digests": list(self.carried_receipt_digests),
            "delta_from_tree_sha": self.delta_from_tree_sha,
            "changed_paths": list(self.changed_paths),
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
            "task_hash",
        }
        if set(raw) != fields:
            raise TrustClaimError("trust claim task fields are invalid")
        raw_invalidated = raw["invalidated_claims"]
        list_fields = ("retirements", "carried_receipt_digests", "changed_paths")
        if not isinstance(raw_invalidated, list) or any(
            not isinstance(raw[field], list)
            or not all(isinstance(value, str) for value in raw[field])
            for field in list_fields
        ):
            raise TrustClaimError("trust claim task claims are invalid")
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
        carried = previous.claims if previous is not None else {}
        invalidated_ids = {claim.claim_id for claim in invalidated}
        carried = {
            identity: binding
            for identity, binding in carried.items()
            if identity in set(claim_set.by_id) - invalidated_ids
        }
        task = cls(
            generation_ref=raw["generation_ref"],  # type: ignore[arg-type]
            source_identity=raw["source_identity"],  # type: ignore[arg-type]
            tree_sha=raw["tree_sha"],  # type: ignore[arg-type]
            manifest_sha256=raw["manifest_sha256"],  # type: ignore[arg-type]
            claim_set_digest=raw["claim_set_digest"],  # type: ignore[arg-type]
            invalidated_claims=tuple(invalidated),
            retirements=tuple(raw["retirements"]),  # type: ignore[arg-type]
            carried_receipt_digests=tuple(
                raw["carried_receipt_digests"]  # type: ignore[arg-type]
            ),
            delta_from_tree_sha=raw["delta_from_tree_sha"],  # type: ignore[arg-type]
            changed_paths=tuple(raw["changed_paths"]),  # type: ignore[arg-type]
            _claim_set=claim_set,
            _carried_claims=carried,
        )
        if raw["task_hash"] != task.task_hash:
            raise TrustClaimError("trust claim task hash is invalid")
        expected_digests = (previous.receipt_digest,) if carried and previous else ()
        if task.carried_receipt_digests != expected_digests:
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
        retirements=invalidation.retired_claim_ids,
        carried_receipt_digests=invalidation.carried_receipt_digests,
        delta_from_tree_sha=delta_from_tree_sha,
        changed_paths=tuple(changed_paths),
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
        task.retirements
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
    retirement_results: Sequence[ClaimVerdict] = (),
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
    passing_claims = [claim_set.by_id[identity] for identity in passing]
    repository_wide = any(not claim.paths for claim in passing_claims)
    covered_paths = {
        path for claim in passing_claims for path in claim.paths
    }
    if not repository_wide and not set(claim_set.risk_paths) <= covered_paths:
        return "inconclusive"
    return "pass"


def compose_claim_verdict(
    previous: TrustClaimReceiptV1 | None,
    task: TrustClaimTaskV1,
    fresh_results: object,
) -> tuple[TrustClaimReceiptV1, str]:
    """Merge exact carried bindings and complete fresh results into a receipt."""
    if task._claim_set is None:
        raise TrustClaimError("claim task is not bound to its normalized claim set")
    if previous is not None and not previous.digest_is_valid():
        raise TrustClaimError("previous trust claim receipt digest is invalid")
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
    retirement_results = [fresh[identity] for identity in task.retirements]
    aggregate = _aggregate(task._claim_set, claims, retirement_results)
    if isinstance(fresh_results, Mapping) and "verification_verdict" in fresh_results:
        claimed_aggregate = fresh_results["verification_verdict"]
        if claimed_aggregate not in TRUST_CLAIM_VERDICTS or claimed_aggregate != aggregate:
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
    )
    return receipt, aggregate


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
        and _aggregate(receipt._claim_set, receipt.claims) == "pass"
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
    )


__all__ = [
    "ClaimSet",
    "ClaimVerdict",
    "Invalidation",
    "TrustClaim",
    "TrustClaimError",
    "TrustClaimReceipt",
    "TrustClaimReceiptV1",
    "TrustClaimTaskV1",
    "build_claim_task",
    "build_trust_claim_task",
    "claim_id",
    "compose_claim_verdict",
    "invalidate_claims",
    "legacy_receipt",
    "normalize_manifest",
    "receipt_covers",
]
