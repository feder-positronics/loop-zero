from pathlib import Path
from types import SimpleNamespace

import pytest

from loopzero.kernel.gitscope import DispatchError
from loopzero.review import evidence
from loopzero.review.trust_claims import (
    build_trust_claim_task,
    invalidate_claims,
    legacy_receipt,
    normalize_manifest,
)


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


def test_trust_claim_evidence_stages_only_invalidated_and_changed_paths(configured):
    for name in ("covered.py", "changed.py", "outside.py"):
        (configured / name).write_text(name, encoding="utf-8")
    raw = {
        "risk_paths": ["covered.py"],
        "actors_assets": [{"text": "claim", "paths": ["covered.py"]}],
    }
    claims = normalize_manifest(raw)
    previous = legacy_receipt(
        raw,
        source_identity="source",
        tree_sha="1" * 40,
        manifest_sha256="a" * 64,
        verifier_run_id="run",
        verifier_task_id="task",
    )
    invalidation = invalidate_claims(
        previous,
        claims,
        changed_paths=["covered.py", "changed.py"],
        base_moved=False,
        risk_paths_added=[],
    )
    task = build_trust_claim_task(
        claims,
        invalidation,
        generation_ref="generation",
        source_identity="source-2",
        tree_sha="2" * 40,
        manifest_sha256="b" * 64,
        delta_from_tree_sha="1" * 40,
        changed_paths=["covered.py", "changed.py"],
    )
    assert task is not None
    snapshot = evidence.stage_trust_claim_evidence(
        worktree=configured,
        primary_repo=configured,
        task_id="trust-task",
        task=task,
    )
    assert {entry["origin"] for entry in snapshot.manifest} == {
        "worktree/covered.py",
        "worktree/changed.py",
    }
    with pytest.raises(DispatchError, match="exceeds task scope"):
        evidence.stage_trust_claim_evidence(
            worktree=configured,
            primary_repo=configured,
            task_id="forged-task",
            task=task,
            evidence_paths=["outside.py"],
        )
