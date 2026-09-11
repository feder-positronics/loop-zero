import json
from pathlib import Path

import pytest

from loopzero.delivery import publish


CONTRACT = publish.BodyContract(("Context and goal", "Validation"))
BODY = """## Context and goal

Portable publication behavior.

## Validation

Unit checks passed.

Closes #42
"""


def artifact():
    return {
        "base_sha": "a" * 40,
        "head_sha": "b" * 40,
        "head_tree_sha": "c" * 40,
    }


def test_body_contract_is_supplied_by_configuration():
    assert publish.validate_contract(BODY, contract=CONTRACT) is None
    assert "## Validation" in publish.validate_contract(
        "## Context and goal\n\ntext\n\nCloses #1\n", contract=CONTRACT
    )


def test_issue_less_ready_body_needs_configured_standalone_authority():
    body = BODY.replace("Closes #42\n", "")
    assert "No work reference" in publish.validate_contract(body, contract=CONTRACT)
    assert publish.validate_contract(body, contract=CONTRACT, standalone=True) is None
    assert publish.validate_contract(
        body + "Standalone-Reason: maintenance\n", contract=CONTRACT
    ) is None


def test_evidence_binding_is_single_and_exact():
    bound = publish.bind_loopzero_evidence(
        BODY, artifact=artifact(), review_task_id="review-1", run_id="run-1"
    )
    assert bound.count(publish.EVIDENCE_START) == 1
    publish.validate_loopzero_evidence(
        bound, artifact=artifact(), review_task_id="review-1", run_id="run-1"
    )
    with pytest.raises(publish.PublicationError, match="does not match"):
        publish.validate_loopzero_evidence(
            bound, artifact=artifact(), review_task_id="review-2", run_id="run-1"
        )


def test_publication_receipt_is_idempotent_and_conflict_safe(tmp_path: Path):
    record = {
        "status": "published", "pr": 42, "base": "a", "head": "b",
        "expected_head": "b", "review_task_id": None, "review_exemption": "T0",
        "review_risk_json": "{}", "review_risk_sha256": "c", "review_risk_tier": "T0",
        "admission_tier_floor": "T0", "run_id": "run", "adopted": False,
    }
    first = publish.write_published_evidence_once(tmp_path, record, generation=0)
    second = publish.write_published_evidence_once(
        tmp_path, record | {"adopted": True}, generation=0
    )
    assert first == second
    rows = [json.loads(line) for line in first.read_text().splitlines()]
    assert rows == [record]
