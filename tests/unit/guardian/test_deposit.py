import json
from pathlib import Path

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
