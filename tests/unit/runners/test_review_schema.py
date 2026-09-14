from copy import deepcopy

import jsonschema
import pytest

from loopzero.kernel.gitscope import DispatchError
from loopzero.review.authority import normalize_review_result
from loopzero.runners._review_schema import (
    TrustClaimResultError,
    validate_trust_claim_result,
)
from loopzero.runners.contract import governed_result_schema


CLAIM_A = "tc_" + "a" * 64
CLAIM_B = "tc_" + "b" * 64


def result():
    return {
        "task_id": "task",
        "status": "completed",
        "summary": "verified",
        "files_changed": [],
        "commit": None,
        "tests": [],
        "requirements_met": [],
        "risks": [],
        "decisions_made": [],
        "escalation_reason": None,
        "recommended_followups": [],
        "findings": [],
        "verification_verdict": "pass",
    }


def schema():
    return governed_result_schema(
        "task",
        task={
            "work_kind": "review",
            "review_intent": "trust-manifest-verification",
        },
    )


def test_claim_verdicts_are_optional_and_other_result_fields_are_unchanged():
    jsonschema.validate(result(), schema())
    properties = schema()["properties"]
    assert "claim_verdicts" in properties
    assert "claim_verdicts" not in schema()["required"]
    assert schema()["additionalProperties"] is False


@pytest.mark.parametrize(
    "claim_verdicts,aggregate",
    [
        (["pass", "pass"], "pass"),
        (["pass", "fail"], "fail"),
        (["pass", "inconclusive"], "inconclusive"),
    ],
)
def test_claim_verdicts_must_match_aggregate(claim_verdicts, aggregate):
    payload = result()
    payload["verification_verdict"] = aggregate
    payload["claim_verdicts"] = [
        {"claim_id": identity, "verdict": verdict, "rationale": "checked"}
        for identity, verdict in zip((CLAIM_A, CLAIM_B), claim_verdicts, strict=True)
    ]
    jsonschema.validate(payload, schema())
    validate_trust_claim_result(payload)
    forged = deepcopy(payload)
    forged["verification_verdict"] = "fail" if aggregate != "fail" else "inconclusive"
    jsonschema.validate(forged, schema())
    with pytest.raises(TrustClaimResultError, match="contradicts"):
        validate_trust_claim_result(forged)


def test_all_passing_claims_allow_inconclusive_until_task_coverage_is_checked():
    payload = result()
    payload["verification_verdict"] = "inconclusive"
    payload["claim_verdicts"] = [
        {"claim_id": CLAIM_A, "verdict": "pass", "rationale": "checked"}
    ]
    jsonschema.validate(payload, schema())
    validate_trust_claim_result(payload)


def test_runner_normalization_rejects_a_contradictory_trust_aggregate():
    payload = result()
    payload["verification_verdict"] = "pass"
    payload["claim_verdicts"] = [
        {"claim_id": CLAIM_A, "verdict": "fail", "rationale": "failed"}
    ]
    jsonschema.validate(payload, schema())
    with pytest.raises(DispatchError, match="trust result is inconsistent"):
        normalize_review_result(
            payload,
            task={"review_intent": "trust-manifest-verification"},
        )


def test_claim_verdict_entries_are_strict_and_bounded():
    payload = result()
    payload["claim_verdicts"] = [
        {
            "claim_id": CLAIM_A,
            "verdict": "pass",
            "rationale": "checked",
            "unexpected": True,
        }
    ]
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(payload, schema())


def test_duplicate_claim_ids_are_rejected_after_provider_output():
    payload = result()
    row = {"claim_id": CLAIM_A, "verdict": "pass", "rationale": "checked"}
    payload["claim_verdicts"] = [row, dict(row)]
    jsonschema.validate(payload, schema())
    with pytest.raises(TrustClaimResultError, match="unique"):
        validate_trust_claim_result(payload)


def test_provider_schema_uses_only_the_established_keyword_subset():
    forbidden = {"allOf", "not", "contains", "uniqueItems"}

    def keys(value):
        if isinstance(value, dict):
            yield from value
            for child in value.values():
                yield from keys(child)
        elif isinstance(value, list):
            for child in value:
                yield from keys(child)

    assert not forbidden & set(keys(schema()))


def test_non_trust_review_does_not_accept_claim_verdicts():
    ordinary = governed_result_schema("task")
    payload = result()
    payload.pop("verification_verdict")
    payload["claim_verdicts"] = []
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(payload, ordinary)
