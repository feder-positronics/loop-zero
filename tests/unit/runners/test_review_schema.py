from copy import deepcopy

import jsonschema
import pytest

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
    forged = deepcopy(payload)
    forged["verification_verdict"] = (
        "fail" if aggregate != "fail" else "inconclusive"
    )
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(forged, schema())


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


def test_non_trust_review_does_not_accept_claim_verdicts():
    ordinary = governed_result_schema("task")
    payload = result()
    payload.pop("verification_verdict")
    payload["claim_verdicts"] = []
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(payload, ordinary)
