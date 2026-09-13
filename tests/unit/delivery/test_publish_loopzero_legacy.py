"""New-contract evidence binding and PR-scoped finding lifetime."""

import importlib
import json
import sys
from pathlib import Path

import pytest

module = importlib.import_module("loopzero.delivery.publish")

VALID_BODY = "## Summary\nCutover.\n\n## Validation\nOwning tests passed.\n"


def test_loopzero_body_has_one_candidate_binding_and_rejects_stale_or_duplicate_evidence():
    helper = importlib.import_module("loopzero.delivery._publish_body")
    artifact = {"base_sha": "a" * 40, "head_sha": "b" * 40, "head_tree_sha": "c" * 40}
    binding = dict(
        artifact=artifact, review_task_id="review-4367", run_id="sr_" + "d" * 32
    )
    body = helper.bind_loopzero_evidence(VALID_BODY, **binding)
    helper.validate_loopzero_evidence(body, **binding)
    assert helper.bind_loopzero_evidence(body, **binding) == body
    assert VALID_BODY.split("## Validation", 1)[1].strip() in body
    with pytest.raises(ValueError, match="evidence"):
        helper.validate_loopzero_evidence(body + body, **binding)
    with pytest.raises(ValueError, match="evidence"):
        helper.validate_loopzero_evidence(body.replace("b" * 40, "e" * 40), **binding)
    with pytest.raises(ValueError, match="evidence"):
        helper.validate_loopzero_evidence(VALID_BODY, **binding)


@pytest.mark.parametrize("contract", ["intelflo-v1", "loop-zero-v1"])
def test_loopzero_accepted_delta_replaces_closure_bookkeeping(
    tmp_path, monkeypatch, contract
):
    run_id = "sr_" + "a" * 32
    rows = tmp_path / ".audit/skill-runs"
    rows.mkdir(parents=True)
    (rows / "2026-09-10.jsonl").write_text(
        json.dumps({"run_id": run_id, "delivery_contract": contract}) + "\n"
    )
    prior = {
        "run_id": run_id,
        "snapshot_sha": "a" * 40,
        "source_identity": {"ref": "refs/heads/feature/x"},
        "task_contract": {"review_chain_id": "chain"},
    }
    current = {
        **prior,
        "snapshot_sha": "b" * 40,
        "task_contract": {"review_chain_id": "chain", "delta_from_snapshot": "a" * 40},
    }
    terminals = {"prior": prior, "current": current}
    monkeypatch.setattr(
        module, "authenticated_review_terminals", lambda *a, **k: terminals
    )
    verdicts = {"current": {"verdict": "pass"}}
    monkeypatch.setattr(module, "authenticated_verdicts", lambda *a, **k: verdicts)
    findings = [
        {
            "finding_id": "old",
            "state": "open",
            "severity": "important",
            "review_task_id": "prior",
        },
        {
            "finding_id": "new",
            "state": "open",
            "severity": "critical",
            "review_task_id": "current",
        },
    ]
    args = dict(
        dispatch_records=[], finding_records=findings, current_review_task_id="current"
    )
    blockers = module.open_important_finding_ids(
        tmp_path, "refs/heads/feature/x", **args
    )
    assert blockers == (("new",) if contract == "loop-zero-v1" else ("new", "old"))
    assert findings[0]["state"] == "open"  # Raw history is never rewritten.
    verdicts.clear()
    assert module.open_important_finding_ids(
        tmp_path, "refs/heads/feature/x", **args
    ) == ("new", "old")
    verdicts["current"] = {"verdict": "pass"}
    current["task_contract"]["review_chain_id"] = "unrelated"
    assert module.open_important_finding_ids(
        tmp_path, "refs/heads/feature/x", **args
    ) == ("new", "old")
