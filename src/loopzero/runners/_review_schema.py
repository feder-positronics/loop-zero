"""Trust-claim schemas and compatibility exports for shared section policy."""

import re
from collections.abc import Mapping


from ..review_contract import (
    ReviewChainError,
    required_review_sections as required_review_sections,
    review_sections_schema as review_sections_schema,
    validate_review_chain_task as validate_review_chain_task,
)


TRUST_CLAIM_ID_PATTERN = r"^tc_[0-9a-f]{64}$"
TRUST_CLAIM_VERDICTS = ("pass", "fail", "inconclusive")


class TrustClaimResultError(ReviewChainError):
    """Raised when returned trust-claim output is structurally inconsistent."""


def trust_claim_verdicts_schema(
    *, bounded_string: Mapping[str, object]
) -> dict[str, object]:
    """Return the optional strict per-claim trust-verification result field."""
    return {
        "type": "array",
        "minItems": 1,
        "maxItems": 64,
        "items": {
            "type": "object",
            "properties": {
                "claim_id": {
                    "type": "string",
                    "pattern": TRUST_CLAIM_ID_PATTERN,
                },
                "verdict": {
                    "type": "string",
                    "enum": list(TRUST_CLAIM_VERDICTS),
                },
                "rationale": dict(bounded_string),
            },
            "required": ["claim_id", "verdict", "rationale"],
            "additionalProperties": False,
        },
    }


def validate_trust_claim_result(
    result: Mapping[str, object],
) -> tuple[Mapping[str, object], ...] | None:
    """Validate rules omitted from the provider-compatible output schema.

    All-pass claim rows may mechanically aggregate to either pass or
    inconclusive: task-bound composition decides which after checking required
    risk-path coverage.
    """
    if "claim_verdicts" not in result:
        return None
    raw_rows = result.get("claim_verdicts")
    if (
        not isinstance(raw_rows, list)
        or not 1 <= len(raw_rows) <= 64
        or not all(isinstance(row, Mapping) for row in raw_rows)
    ):
        raise TrustClaimResultError("claim_verdicts must be a non-empty bounded list")
    rows = tuple(raw_rows)
    identities: list[str] = []
    verdicts: list[str] = []
    for row in rows:
        if set(row) != {"claim_id", "verdict", "rationale"}:
            raise TrustClaimResultError("claim verdict fields are invalid")
        identity = row.get("claim_id")
        verdict = row.get("verdict")
        rationale = row.get("rationale")
        if (
            not isinstance(identity, str)
            or re.fullmatch(TRUST_CLAIM_ID_PATTERN, identity) is None
        ):
            raise TrustClaimResultError("claim verdict identity is invalid")
        if verdict not in TRUST_CLAIM_VERDICTS:
            raise TrustClaimResultError("claim verdict is invalid")
        if not isinstance(rationale, str) or not rationale.strip():
            raise TrustClaimResultError("claim verdict rationale is invalid")
        identities.append(identity)
        verdicts.append(verdict)
    if len(identities) != len(set(identities)):
        raise TrustClaimResultError("claim verdict identities must be unique")

    aggregate = result.get("verification_verdict")
    permitted = (
        {"fail"}
        if "fail" in verdicts
        else {"inconclusive"}
        if "inconclusive" in verdicts
        else {"pass", "inconclusive"}
    )
    if aggregate not in permitted:
        raise TrustClaimResultError(
            "verification_verdict contradicts the returned claim verdicts"
        )
    return rows
