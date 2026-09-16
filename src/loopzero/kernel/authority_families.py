"""Closed registry of governed authority record families and retention anchors."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Literal

RetentionAnchor = Literal[
    "policy", "generation", "trust-generation", "reservation", "attempt"
]


@dataclass(frozen=True, slots=True)
class GovernedRecordFamily:
    """Retention policy for one coordinator-governed record type."""

    record_type: str
    anchor: RetentionAnchor
    reference_path: tuple[str, ...] = ()


def _family(
    record_type: str,
    anchor: RetentionAnchor = "policy",
    *reference_path: str,
) -> tuple[str, GovernedRecordFamily]:
    return record_type, GovernedRecordFamily(record_type, anchor, reference_path)


_POLICY_FAMILIES = (
    "alias-availability",
    "attempt-abort",
    "attempt-checkpoint",
    "attempt-cleanup-failure",
    "attempt-owner",
    "attempt-patch-identity-carry",
    "attempt-progress",
    "attempt-recovery",
    "attempt-start",
    "attempt-supersession",
    "attempt-terminal",
    "coordinator-authority-cutover",
    "delivery-control",
    "deposit-adoption",
    "deposit-verification",
    "evidence-cleanup",
    "evidence-cleanup-friction",
    "finding-publication-binding-v1",
    "finding-recovery-admission-v1",
    "inline",
    "inconclusive-retry-authorization",
    "provider-outage-recovery-authorization-v1",
    "review-chain-advisory",
    "review-generation-v1",
    "review-generation-proof-v1",
    "review-generation-link-v1",
    "review-recovery-verification",
    "review-slot-reservation-v1",
    "review-slot-settlement-v1",
    "retained-authority-state",
    "route",
    "scratch-cleanup",
    "verdict",
    "generation-carry-v1",
)

GOVERNED_RECORD_FAMILIES: Mapping[str, GovernedRecordFamily] = MappingProxyType(
    dict(
        [*(_family(record_type) for record_type in _POLICY_FAMILIES)]
        + [
            _family("review-launch-v1", "reservation", "reservation_id"),
            _family("review-launch-outcome-v1", "reservation", "reservation_id"),
            _family("review-family-coverage-v1", "generation", "generation_id"),
            _family(
                "trust-claim-receipt-v1",
                "trust-generation",
            ),
            _family("review-nonverdict-launch-v1", "attempt"),
        ]
    )
)


def governed_record_family(record_type: object) -> GovernedRecordFamily | None:
    """Return package retention policy for a governed record type."""
    return (
        GOVERNED_RECORD_FAMILIES.get(record_type)
        if isinstance(record_type, str)
        else None
    )


__all__ = [
    "GOVERNED_RECORD_FAMILIES",
    "GovernedRecordFamily",
    "RetentionAnchor",
    "governed_record_family",
]
