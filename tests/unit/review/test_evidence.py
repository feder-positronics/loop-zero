from pathlib import Path
from types import SimpleNamespace

import pytest

from loopzero.kernel.gitscope import DispatchError
from loopzero.review import evidence


@pytest.fixture
def configured(tmp_path):
    profile = SimpleNamespace(
        root=tmp_path,
        audit_root=Path("private-state"),
        github=SimpleNamespace(ref_namespace="refs/consumer-snapshots"),
    )
    evidence.configure(profile)
    return tmp_path


def test_review_findings_keep_material_severities_only():
    rows = [
        {"severity": "critical", "claim": "a"},
        {"severity": "important", "claim": "b"},
        {"severity": "suggestion", "claim": "c"},
    ]
    assert evidence.persisted_review_findings(rows, review_intent="discovery") == rows[:2]
    assert evidence.persisted_review_findings(
        rows, review_intent="trust-manifest-verification"
    ) == []


def test_evidence_snapshot_uses_configured_private_root_and_detects_drift(configured):
    source = configured / "proof.txt"
    source.write_text("proof", encoding="utf-8")
    snapshot = evidence.stage_evidence_snapshot(
        worktree=configured,
        primary_repo=configured,
        task_id="review/17",
        evidence_paths=["proof.txt"],
    )
    assert snapshot.directory.is_relative_to(configured / "private-state")
    evidence.verify_evidence_snapshot(snapshot)
    snapshot.files[0].chmod(0o600)
    snapshot.files[0].write_text("changed", encoding="utf-8")
    with pytest.raises(DispatchError, match="evidence-drift"):
        evidence.verify_evidence_snapshot(snapshot)


def test_evidence_paths_cannot_escape_configured_roots(configured, tmp_path_factory):
    outside = tmp_path_factory.mktemp("outside") / "proof.txt"
    outside.write_text("proof", encoding="utf-8")
    with pytest.raises(DispatchError, match="outside allowed roots"):
        evidence.validate_evidence_inputs(
            worktree=configured, primary_repo=configured, evidence_paths=[str(outside)]
        )
