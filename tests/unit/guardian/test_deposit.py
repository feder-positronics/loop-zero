import json
from pathlib import Path

import pytest

from loopzero.guardian import deposit


def test_prepare_uses_injected_audit_root(tmp_path: Path):
    repo = tmp_path / "repo"
    state = tmp_path / "state"
    (repo / "records" / "dispatch").mkdir(parents=True)
    (repo / "records" / "dispatch" / "events.jsonl").write_text("", encoding="utf-8")
    prepared = deposit.prepare(
        repo=repo,
        state_dir=state,
        task_id="task-1",
        audit_root=Path("records"),
    )
    assert prepared.canonical_audit_root == repo / "records"
    assert prepared.audit_root.joinpath("dispatch/events.jsonl").is_file()
    assert json.loads(prepared.manifest_path.read_text())["task_id"] == "task-1"


def test_prepare_rejects_malformed_task_identity(tmp_path: Path):
    try:
        deposit.prepare(repo=tmp_path, state_dir=tmp_path, task_id="bad/task")
    except deposit.AuditDepositError as exc:
        assert "identity" in str(exc)
    else:  # pragma: no cover - makes the security property explicit
        raise AssertionError("malformed task identity was accepted")


def test_promotion_rejects_contradictory_trust_result_before_import(tmp_path: Path):
    repo = tmp_path / "repo"
    prepared = deposit.prepare(
        repo=repo, state_dir=tmp_path / "state", task_id="trust-1",
        audit_root=Path("records"),
    )
    deposit.bind_launch(
        prepared,
        task={
            "task_id": "trust-1", "work_unit_id": "trust-1",
            "objective": "Verify trust claims", "role": "review", "size_points": 1,
            "work_kind": "review", "category": "quality-maintenance",
            "review_intent": "trust-manifest-verification",
        },
        run_id="sr_" + "a" * 32, worktree=repo, read_only=True,
    )
    prepared.result_path.parent.mkdir(parents=True, exist_ok=True)
    prepared.result_path.write_text(json.dumps({
        "task_id": "trust-1", "findings": [], "verification_verdict": "pass",
        "claim_verdicts": [{
            "claim_id": "tc_" + "a" * 64, "verdict": "fail", "rationale": "failed",
        }],
    }))
    imported = []
    with pytest.raises(deposit.AuditDepositError, match="review result is invalid") as raised:
        deposit.promote(prepared, append_authority_records=lambda *args: imported.append(args))
    assert "trust result is inconsistent" in str(raised.value.__cause__)
    assert not imported
    assert not (repo / "records" / "dispatch" / "results" / "trust-1.json").exists()
