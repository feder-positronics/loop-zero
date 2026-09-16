"""Adversarial contract tests for finding-deposition recovery.

This file is intentionally kept outside the package writer's checkout.  It may
be copied into ``tests/unit/review/`` only after the package API is frozen.  The
tests exercise public package schemas and authority primitives; they never
append live authority.
"""

from __future__ import annotations

import copy
import hashlib
import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from loopzero.kernel import authority as kernel_authority
from loopzero.kernel import (
    authority_projection,
    authority_store,
    patch_identity,
    policy,
    review_state,
)
from loopzero.review import authority as review_authority
from loopzero.review import chain, findings, provisional_findings
from loopzero.runners.contract import ReviewOutcome


def _result(*, claim: str = "close the descriptor") -> dict[str, object]:
    finding = {
        "severity": "critical",
        "claim": claim,
        "path": "owned.py",
        "line_start": 1,
        "line_end": 1,
    }
    return {
        "task_id": "review-1",
        "status": "completed",
        "summary": "one material finding",
        "files_changed": [],
        "commit": None,
        "tests": [],
        "requirements_met": [],
        "risks": [],
        "decisions_made": [],
        "escalation_reason": None,
        "recommended_followups": [],
        "findings": [finding],
        "review_sections": {
            "code": {
                "completion": "completed",
                "verdict": "findings",
                "findings": [finding],
            },
            "security": {
                "completion": "completed",
                "verdict": "clean",
                "findings": [],
                "threat_model_summary": "No changed trust boundary.",
            },
        },
    }


def _canonical(value: object) -> bytes:
    return json.dumps(
        value, sort_keys=True, ensure_ascii=False, separators=(",", ":")
    ).encode("utf-8")


def _signed_history(
    repo: Path,
    *,
    task_id: str = "review-1",
    run_id: str = "sr_" + "1" * 32,
    result_artifact: str = ".audit/dispatch/results/review-1.json",
    result: dict[str, object] | None = None,
    repository_binding: str | None = None,
) -> tuple[
    kernel_authority.CoordinatorAuthority,
    list[dict[str, object]],
    dict[str, object],
]:
    payload = result or _result()
    relative_artifact = Path(result_artifact)
    artifact = repo / relative_artifact
    if ".." not in relative_artifact.parts and not artifact.is_symlink():
        artifact.parent.mkdir(parents=True, exist_ok=True)
        artifact.write_bytes(_canonical(payload))
    result_sha256 = hashlib.sha256(_canonical(payload)).hexdigest()
    dispatcher = kernel_authority.TerminalAuthority.generate()
    coordinator = kernel_authority.CoordinatorAuthority(
        dispatcher.public_key, dispatcher._private_key
    )
    common = {
        "schema_version": policy.TELEMETRY_SCHEMA_VERSION,
        "policy_version": policy.DISPATCH_POLICY_VERSION,
    }
    cutover = coordinator.seal(
        {
            **common,
            "type": "coordinator-authority-cutover",
            "status": "active",
            "ledger_prefix": authority_projection.coordinator_ledger_prefix([]),
        },
        authority_kind="coordinator",
    )
    snapshot_sha = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    snapshot_tree_sha = subprocess.run(
        ["git", "rev-parse", "HEAD^{tree}"],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    base_sha = subprocess.run(
        ["git", "rev-parse", f"{snapshot_sha}^"],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    identity_patch = patch_identity.compute_patch_identity(
        repo, base_sha=base_sha, candidate_sha=snapshot_sha
    )
    repository_binding = (
        repository_binding or authority_store.authority_repository_binding(repo)
    )
    generation_id = review_state.generation_id_for(
        repository_binding, identity_patch, snapshot_tree_sha, ("code", "security")
    )
    reservation_id = review_state._reservation_id(
        generation_id, "delivery", "primary", task_id, task_id
    )
    identity = {
        **common,
        "task_id": task_id,
        "work_unit_id": task_id,
        "attempt_index": 0,
        "run_id": run_id,
        "worktree": str(repo),
        "read_only": True,
        "work_kind": "review",
        "advisory": False,
        "review_intent": "delivery-code-review",
        "snapshot_sha": snapshot_sha,
        "snapshot_tree_sha": snapshot_tree_sha,
        "result_artifact": result_artifact,
        "result_sha256": result_sha256,
        "reservation_id": reservation_id,
        "generation_id": generation_id,
        "family": "gpt",
        "review_family": "delivery",
        "review_reservation_id": reservation_id,
        "review_generation_id": generation_id,
        "repository_binding": repository_binding,
        "patch_identity": identity_patch,
        "source_identity": {
            "head": snapshot_sha,
            "tree": snapshot_tree_sha,
            "ref": "refs/heads/fix/18",
            "repository_binding": repository_binding,
        },
        "task_contract_hash": "d" * 64,
        "review_chain_receipt": {
            "schema_version": "ReviewChainReceiptV1",
            "task_id": task_id,
            "snapshot_sha": snapshot_sha,
            "snapshot_tree_sha": snapshot_tree_sha,
            "patch_identity": identity_patch,
            "trigger_classifier": {
                "security_trigger_paths": ["owned.py"],
                "security_required": True,
            },
            "required_sections": ["code", "security"],
            "sections": {
                "code": {
                    "completion": "completed",
                    "verdict": "findings",
                    "finding_ids": ["f_1"],
                },
                "security": {
                    "completion": "completed",
                    "verdict": "clean",
                    "finding_ids": [],
                    "threat_model_summary": "No changed trust boundary.",
                },
            },
        },
    }
    start = coordinator.seal(
        {
            **identity,
            "type": "attempt-start",
            "registration_authority_version": 1,
            "terminal_authority": dispatcher.registration(),
        },
        authority_kind="coordinator",
    )
    terminal = dispatcher.seal(
        {
            **identity,
            "type": "attempt-terminal",
            "status": "infrastructure-failure",
            "failure_class": "finding-deposition-failed",
            "deposit_state": "none",
        },
        authority_kind="dispatcher",
    )
    return coordinator, [cutover, start, terminal], terminal


@pytest.fixture
def configured(tmp_path: Path):
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(
        ["git", "config", "user.email", "test@example.invalid"],
        cwd=tmp_path,
        check=True,
    )
    subprocess.run(["git", "config", "user.name", "Test"], cwd=tmp_path, check=True)
    (tmp_path / "owned.py").write_text("owned = True\n", encoding="utf-8")
    subprocess.run(["git", "commit", "--allow-empty", "-qm", "base"], cwd=tmp_path, check=True)
    subprocess.run(["git", "add", "owned.py"], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-qm", "fixture"], cwd=tmp_path, check=True)
    profile = SimpleNamespace(
        root=tmp_path,
        audit_root=Path(".audit"),
        finding_severities=("critical", "important", "suggestion"),
        max_reviews_per_pr=1,
        max_delta_reviews=1,
        required_sections=("code", "security"),
    )
    chain.configure(profile)
    provisional_findings.configure(profile)
    findings.configure(profile)
    return tmp_path


def _admit(coordinator, records):
    admission = coordinator.seal(
        {
            "ts": "2026-09-16T12:00:00+00:00",
            **provisional_findings.build_recovery_admission(
                records, task_id="review-1"
            ),
            "schema_version": policy.TELEMETRY_SCHEMA_VERSION,
            "policy_version": policy.DISPATCH_POLICY_VERSION,
        },
        authority_kind="coordinator",
    )
    records.append(admission)
    return admission


def _append_recovery(coordinator, records, repo, admission, receipt):
    recovery = coordinator.seal(
        {
            "ts": "2026-09-16T12:00:01+00:00",
            **provisional_findings.build_recovery_evidence(
                records, repo=repo, admission=admission, capture_receipt=receipt
            ),
            "schema_version": policy.TELEMETRY_SCHEMA_VERSION,
            "policy_version": policy.DISPATCH_POLICY_VERSION,
        },
        authority_kind="coordinator",
    )
    records.append(recovery)
    return recovery


def test_admission_binds_repository_and_patch_identity(configured, monkeypatch):
    coordinator, records, terminal = _signed_history(configured)
    monkeypatch.setattr(
        kernel_authority,
        "_trusted_coordinator_public_key",
        lambda: coordinator.public_key,
    )

    admission = _admit(coordinator, records)

    assert (
        admission["source_identity"]["repository_binding"]
        == terminal["source_identity"]["repository_binding"]
    )
    assert admission["patch_identity"] == terminal["patch_identity"]


def test_cross_repository_admission_does_not_project(configured, monkeypatch):
    coordinator, records, _terminal = _signed_history(configured)
    monkeypatch.setattr(
        kernel_authority,
        "_trusted_coordinator_public_key",
        lambda: coordinator.public_key,
    )
    unsigned = provisional_findings.build_recovery_admission(
        records, task_id="review-1"
    )
    forged = coordinator.seal(
        {
            "ts": "2026-09-16T12:00:00+00:00",
            **unsigned,
            "repository_binding": "repo_" + "9" * 64,
            "schema_version": policy.TELEMETRY_SCHEMA_VERSION,
            "policy_version": policy.DISPATCH_POLICY_VERSION,
        },
        authority_kind="coordinator",
    )

    assert (
        provisional_findings.authenticated_recovery_admissions([*records, forged]) == {}
    )


def test_result_tamper_fails_before_provisional_write(configured, monkeypatch):
    coordinator, records, _terminal = _signed_history(configured)
    monkeypatch.setattr(
        kernel_authority,
        "_trusted_coordinator_public_key",
        lambda: coordinator.public_key,
    )
    admission = _admit(coordinator, records)
    tampered = _result(claim="different bytes and finding")

    with pytest.raises(
        provisional_findings.ProvisionalFindingError, match="digest|artifact"
    ):
        provisional_findings.capture_provisional_findings(
            configured,
            authority_records=records,
            admission=admission,
            result=tampered,
        )
    assert not (configured / ".audit" / "provisional-findings").exists()


@pytest.mark.parametrize("escape_kind", ["parent", "symlink"])
def test_result_path_escape_or_symlink_fails_closed(
    configured, monkeypatch, escape_kind
):
    outside = configured.parent / "outside-result.json"
    outside.write_bytes(_canonical(_result()))
    if escape_kind == "parent":
        artifact = "../outside-result.json"
    else:
        link = configured / ".audit" / "dispatch" / "results" / "review-1.json"
        link.parent.mkdir(parents=True, exist_ok=True)
        link.symlink_to(outside)
        artifact = ".audit/dispatch/results/review-1.json"
    coordinator, records, _terminal = _signed_history(
        configured, result_artifact=artifact
    )
    monkeypatch.setattr(
        kernel_authority,
        "_trusted_coordinator_public_key",
        lambda: coordinator.public_key,
    )
    admission = _admit(coordinator, records)

    with pytest.raises(
        provisional_findings.ProvisionalFindingError, match="path|symlink|artifact"
    ):
        provisional_findings.capture_provisional_findings(
            configured,
            authority_records=records,
            admission=admission,
            result=_result(),
        )
    assert not (configured / ".audit" / "provisional-findings").exists()


def test_distinct_authenticated_runs_may_capture_identical_result_bytes(
    configured, monkeypatch
):
    coordinator, first_records, _terminal = _signed_history(configured)
    monkeypatch.setattr(
        kernel_authority,
        "_trusted_coordinator_public_key",
        lambda: coordinator.public_key,
    )
    first = _admit(coordinator, first_records)
    provisional_findings.capture_provisional_findings(
        configured,
        authority_records=first_records,
        admission=first,
        result=_result(),
    )

    other_coordinator, other_records, _other_terminal = _signed_history(
        configured,
        task_id="review-2",
        run_id="sr_" + "8" * 32,
        result_artifact=".audit/dispatch/results/review-2.json",
    )
    monkeypatch.setattr(
        kernel_authority,
        "_trusted_coordinator_public_key",
        lambda: other_coordinator.public_key,
    )
    other_admission = other_coordinator.seal(
        {
            "ts": "2026-09-16T12:00:00+00:00",
            **provisional_findings.build_recovery_admission(
                other_records, task_id="review-2"
            ),
            "schema_version": policy.TELEMETRY_SCHEMA_VERSION,
            "policy_version": policy.DISPATCH_POLICY_VERSION,
        },
        authority_kind="coordinator",
    )
    other_records.append(other_admission)

    provisional_findings.capture_provisional_findings(
        configured,
        authority_records=other_records,
        admission=other_admission,
        result=_result(),
    )
    captured = provisional_findings.load_provisional_findings(configured)
    assert {row["provisional_owner_id"] for row in captured} == {
        first["provisional_owner_id"],
        other_admission["provisional_owner_id"],
    }


def test_retry_after_finding_fsync_observes_one_capture(configured, monkeypatch):
    coordinator, records, _terminal = _signed_history(configured)
    monkeypatch.setattr(
        kernel_authority,
        "_trusted_coordinator_public_key",
        lambda: coordinator.public_key,
    )
    admission = _admit(coordinator, records)
    original_append = provisional_findings._append_atomic
    crashed = False

    def append_then_interrupt(repo, rows):
        nonlocal crashed
        original_append(repo, rows)
        if not crashed:
            crashed = True
            raise RuntimeError("simulated interruption after durable finding fsync")

    monkeypatch.setattr(provisional_findings, "_append_atomic", append_then_interrupt)
    with pytest.raises(RuntimeError, match="simulated interruption"):
        provisional_findings.capture_provisional_findings(
            configured,
            authority_records=records,
            admission=admission,
            result=_result(),
        )

    receipt = provisional_findings.capture_provisional_findings(
        configured,
        authority_records=records,
        admission=admission,
        result=_result(),
    )
    assert receipt["finding_count"] == 1
    assert (
        len(
            provisional_findings.load_provisional_findings(
                configured, owner_id=str(admission["provisional_owner_id"])
            )
        )
        == 1
    )


def test_recovery_reloads_capture_receipt_instead_of_trusting_caller(
    configured, monkeypatch
):
    coordinator, records, _terminal = _signed_history(configured)
    monkeypatch.setattr(
        kernel_authority,
        "_trusted_coordinator_public_key",
        lambda: coordinator.public_key,
    )
    admission = _admit(coordinator, records)
    receipt = provisional_findings.capture_provisional_findings(
        configured,
        authority_records=records,
        admission=admission,
        result=_result(),
    )
    forged = {
        **receipt,
        "capture_request_sha256": "9" * 64,
        "finding_count": 0,
        "finding_ids": [],
    }

    try:
        unsigned = provisional_findings.build_recovery_evidence(
            records,
            repo=configured,
            admission=admission,
            capture_receipt=forged,
        )
    except provisional_findings.ProvisionalFindingError:
        return
    recovery = coordinator.seal(unsigned, authority_kind="coordinator")
    records.append(recovery)
    assert (
        review_authority.authenticated_review_terminals(records).get("review-1")
        != recovery
    )


def test_recovery_replay_is_exact_and_does_not_create_second_owner(
    configured, monkeypatch
):
    coordinator, records, _terminal = _signed_history(configured)
    monkeypatch.setattr(
        kernel_authority,
        "_trusted_coordinator_public_key",
        lambda: coordinator.public_key,
    )
    admission = _admit(coordinator, records)
    receipt = provisional_findings.capture_provisional_findings(
        configured,
        authority_records=records,
        admission=admission,
        result=_result(),
    )

    first = provisional_findings.build_recovery_evidence(
        records, repo=configured, admission=admission, capture_receipt=receipt
    )
    replay = provisional_findings.build_recovery_evidence(
        records, repo=configured, admission=admission, capture_receipt=receipt
    )

    assert replay == first
    assert replay["provisional_owner_id"] == admission["provisional_owner_id"]


def test_unvalidated_coordinator_verdict_cannot_accept_recovery(
    configured, monkeypatch
):
    coordinator, records, _terminal = _signed_history(configured)
    monkeypatch.setattr(
        kernel_authority,
        "_trusted_coordinator_public_key",
        lambda: coordinator.public_key,
    )
    admission = _admit(coordinator, records)
    receipt = provisional_findings.capture_provisional_findings(
        configured,
        authority_records=records,
        admission=admission,
        result=_result(),
    )
    recovery = coordinator.seal(
        {
            "ts": "2026-09-16T12:00:01+00:00",
            **provisional_findings.build_recovery_evidence(
                records,
                repo=configured,
                admission=admission,
                capture_receipt=receipt,
            ),
            "schema_version": policy.TELEMETRY_SCHEMA_VERSION,
            "policy_version": policy.DISPATCH_POLICY_VERSION,
        },
        authority_kind="coordinator",
    )
    records.append(recovery)
    # Deliberately lacks the independently verified target/verifier contract.
    forged_verdict = coordinator.seal(
        {
            "schema_version": policy.TELEMETRY_SCHEMA_VERSION,
            "policy_version": policy.DISPATCH_POLICY_VERSION,
            "type": "verdict",
            "task_id": "review-1",
            "run_id": recovery["run_id"],
            "verdict": "pass",
        },
        authority_kind="coordinator",
    )
    records.append(forged_verdict)
    monkeypatch.setattr(
        review_authority,
        "review_terminal_acceptance_reasons",
        lambda *_args, **_kwargs: (),
    )

    assert review_authority.authenticated_verdicts(records) == {}
    assert review_authority.accepted_review_terminals(records) == {}


def test_binding_cannot_move_to_second_pr_after_replay(configured, monkeypatch):
    coordinator, records, _terminal = _signed_history(configured)
    monkeypatch.setattr(
        kernel_authority,
        "_trusted_coordinator_public_key",
        lambda: coordinator.public_key,
    )
    admission = _admit(coordinator, records)
    receipt = provisional_findings.capture_provisional_findings(
        configured,
        authority_records=records,
        admission=admission,
        result=_result(),
    )
    _append_recovery(coordinator, records, configured, admission, receipt)
    first = provisional_findings.build_publication_binding(
        records,
        repo=configured,
        admission=admission,
        capture_receipt=receipt,
        pr=18,
        head="e" * 40,
        base="main",
        repository="feder-positronics/loop-zero",
    )
    records.append(coordinator.seal(first, authority_kind="coordinator"))

    with pytest.raises(
        provisional_findings.ProvisionalFindingError, match="different PR"
    ):
        provisional_findings.build_publication_binding(
            records,
            repo=configured,
            admission=admission,
            capture_receipt=receipt,
            pr=19,
            head="e" * 40,
            base="main",
            repository="feder-positronics/loop-zero",
        )


def test_compaction_retains_recovery_lineage_and_unique_binding(
    configured, monkeypatch
):
    coordinator, records, terminal = _signed_history(configured)
    monkeypatch.setattr(
        kernel_authority,
        "_trusted_coordinator_public_key",
        lambda: coordinator.public_key,
    )
    monkeypatch.setattr(
        review_authority, "verifier_identity_is_independent", lambda **_kwargs: True
    )
    admission = _admit(coordinator, records)
    receipt = provisional_findings.capture_provisional_findings(
        configured,
        authority_records=records,
        admission=admission,
        result=_result(),
    )
    resolution = review_state.resolve_generation(
        records,
        repository_binding=str(terminal["repository_binding"]),
        patch_identity=terminal["patch_identity"],
        tree_sha=str(terminal["snapshot_tree_sha"]),
        required_sections=("code", "security"),
        equivalence_proof=None,
        format_only_proof=None,
        family="delivery",
    )
    assert resolution.generation is not None
    assert resolution.coverage is not None
    records.append(
        coordinator.seal(resolution.generation.to_dict(), authority_kind="coordinator")
    )
    records.append(
        coordinator.seal(resolution.coverage.to_dict(), authority_kind="coordinator")
    )
    with authority_store.authority_ledger_lock(configured):
        reservation = review_state.reserve_review_slot(
            configured,
            records,
            generation_id=resolution.generation.generation_id,
            family="delivery",
            slot_kind="primary",
            task_id=str(terminal["task_id"]),
            idempotency_key=str(terminal["task_id"]),
        )
        records.append(
            coordinator.seal(reservation.to_dict(), authority_kind="coordinator")
        )
        settlement = review_state.settle_review_slot(
            configured,
            records,
            reservation=reservation,
            outcome=ReviewOutcome.UNRESOLVED,
            terminal_ref=terminal,
        )
        records.append(
            coordinator.seal(settlement.to_dict(), authority_kind="coordinator")
        )
    pending_retained = authority_store.retained_authority_projection(records)
    pending_compacted = authority_store._prospective_retained_view(
        pending_retained, source_records=records
    )
    assert provisional_findings.authenticated_recovery_admissions(
        pending_compacted
    ) == {"review-1": admission}
    recovery = coordinator.seal(
        {
            "ts": "2026-09-16T12:00:01+00:00",
            **provisional_findings.build_recovery_evidence(
                records,
                repo=configured,
                admission=admission,
                capture_receipt=receipt,
            ),
            "schema_version": policy.TELEMETRY_SCHEMA_VERSION,
            "policy_version": policy.DISPATCH_POLICY_VERSION,
        },
        authority_kind="coordinator",
    )
    records.append(recovery)
    records.append(
        coordinator.seal(
            {
                "schema_version": policy.TELEMETRY_SCHEMA_VERSION,
                "policy_version": policy.DISPATCH_POLICY_VERSION,
                "type": "verdict",
                "task_id": terminal["task_id"],
                "run_id": terminal["run_id"],
                "verdict": "pass",
                "target_worker_identity": recovery.get("worker_identity"),
                "verifier_identity": "independent-verifier",
            },
            authority_kind="coordinator",
        )
    )
    binding = coordinator.seal(
        provisional_findings.build_publication_binding(
            records,
            repo=configured,
            admission=admission,
            capture_receipt=receipt,
            pr=18,
            head="e" * 40,
            base="main",
            repository="feder-positronics/loop-zero",
        ),
        authority_kind="coordinator",
    )
    records.append(binding)

    before = authority_projection.slot_state(
        records, reservation.generation_id, "delivery"
    )
    assert before.reservations == (reservation,)
    assert before.primary_consumed
    assert before.attempt_count("primary") == 1
    assert not before.delta_consumed
    retained = authority_store.retained_authority_projection(records)
    retained_types = [row.get("type") for row in retained]
    compacted = authority_store._prospective_retained_view(
        retained, source_records=records
    )
    after = authority_projection.slot_state(
        compacted, reservation.generation_id, "delivery"
    )

    assert after.reservations == (reservation,), retained_types
    assert after.primary_consumed, retained_types
    assert after.attempt_count("primary") == 1
    assert not after.delta_consumed
    assert provisional_findings.authenticated_recovery_admissions(compacted), retained_types
    assert review_authority.authenticated_review_terminals(compacted)["review-1"] == recovery
    assert provisional_findings.authenticated_publication_bindings(compacted) == {
        admission["provisional_owner_id"]: binding
    }
    assert any(row == terminal for row in compacted)
    replay_payload = provisional_findings.build_recovery_evidence(
        compacted,
        repo=configured,
        admission=admission,
        capture_receipt=receipt,
    )
    assert replay_payload["type"] == "attempt-recovery"
    assert replay_payload["finding_capture_receipt_sha256"] == recovery[
        "finding_capture_receipt_sha256"
    ]
    with authority_store.authority_ledger_lock(configured):
        with authority_store.attempt_lifecycle_lock(configured, "review-1"):
            assert not provisional_findings.authorize_classified_recovery_append(
                configured,
                compacted,
                recovery,
            )


def test_fixture_mutation_does_not_accidentally_authenticate(configured, monkeypatch):
    coordinator, records, _terminal = _signed_history(configured)
    monkeypatch.setattr(
        kernel_authority,
        "_trusted_coordinator_public_key",
        lambda: coordinator.public_key,
    )
    admission = _admit(coordinator, records)
    for field, forged_value in (
        ("run_id", "sr_" + "9" * 32),
        ("task_id", "other-task"),
        ("attempt_index", 2),
        ("reservation_id", "rr_" + "9" * 32),
        ("generation_id", "cg_" + "9" * 32),
        ("family", "other-family"),
        ("snapshot_sha", "f" * 40),
        ("snapshot_tree_sha", "e" * 40),
    ):
        forged = copy.deepcopy(admission)
        forged[field] = forged_value
        assert (
            provisional_findings.authenticated_recovery_admissions(
                [*records[:-1], forged]
            )
            == {}
        ), field
