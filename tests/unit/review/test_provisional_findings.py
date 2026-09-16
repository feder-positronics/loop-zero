import hashlib
import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from loopzero.kernel import authority as kernel_authority
from loopzero.kernel import authority_projection, authority_store, policy, review_state
from loopzero.review import authority as review_authority
from loopzero.review import findings, provisional_findings


def _review_result():
    return {
        "findings": [
            {"severity": "important", "claim": "fix it", "path": "a.py"},
            {"severity": "suggestion", "claim": "rename local", "path": "a.py"},
        ]
    }


def _signed_history(
    tmp_path: Path,
    *,
    result: dict[str, object] | None = None,
    task_contract: dict[str, object] | None = None,
):
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
    artifact = tmp_path / ".audit" / "dispatch" / "results" / "review-1.json"
    artifact.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(result or _review_result(), sort_keys=True).encode("utf-8")
    artifact.write_bytes(payload)
    snapshot_sha = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    snapshot_tree_sha = subprocess.run(
        ["git", "rev-parse", "HEAD^{tree}"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    identity = {
        **common,
        "task_id": "review-1",
        "work_unit_id": "review-1",
        "attempt_index": 0,
        "run_id": "sr_" + "1" * 32,
        "worktree": str(tmp_path),
        "read_only": True,
        "work_kind": "review",
        "advisory": False,
        "review_intent": "delivery-code-review",
        "snapshot_sha": snapshot_sha,
        "snapshot_tree_sha": snapshot_tree_sha,
        "result_artifact": ".audit/dispatch/results/review-1.json",
        "result_sha256": hashlib.sha256(payload).hexdigest(),
        "reservation_id": "rr_" + "2" * 32,
        "generation_id": "cg_" + "3" * 32,
        "family": "gpt",
        "repository_binding": authority_store.authority_repository_binding(tmp_path),
        "source_identity": {"head": "a" * 40, "ref": "refs/heads/fix/18"},
        "task_contract_hash": "d" * 64,
    }
    if task_contract is not None:
        identity["task_contract"] = task_contract
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
    (tmp_path / "a.py").write_text("value = 1\n", encoding="utf-8")
    subprocess.run(["git", "add", "a.py"], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-qm", "fixture"], cwd=tmp_path, check=True)
    profile = SimpleNamespace(
        root=tmp_path,
        audit_root=Path(".audit"),
        finding_severities=("critical", "important", "suggestion"),
    )
    provisional_findings.configure(profile)
    findings.configure(profile)
    return tmp_path


def _snapshot_identity(repo: Path) -> tuple[str, str]:
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    tree = subprocess.run(
        ["git", "rev-parse", "HEAD^{tree}"],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    return head, tree


def _seal_admission(coordinator, payload, **changes):
    return coordinator.seal(
        {
            "ts": "2026-09-16T12:00:00+00:00",
            **payload,
            **changes,
            "schema_version": policy.TELEMETRY_SCHEMA_VERSION,
            "policy_version": policy.DISPATCH_POLICY_VERSION,
        },
        authority_kind="coordinator",
    )


def test_admission_is_derived_from_the_exact_registered_failure(
    configured, monkeypatch
):
    coordinator, records, terminal = _signed_history(configured)
    monkeypatch.setattr(
        kernel_authority,
        "_trusted_coordinator_public_key",
        lambda: coordinator.public_key,
    )

    unsigned = provisional_findings.build_recovery_admission(
        records, task_id="review-1"
    )
    admission = _seal_admission(coordinator, unsigned)
    projected = provisional_findings.authenticated_recovery_admissions(
        [*records, admission]
    )

    assert projected["review-1"] == admission
    assert admission["result_sha256"] == terminal["result_sha256"]
    assert admission["provisional_owner_id"].startswith("pfo_")
    assert "pr" not in admission

    forged = _seal_admission(coordinator, unsigned, snapshot_sha="f" * 40)
    assert (
        provisional_findings.authenticated_recovery_admissions([*records, forged]) == {}
    )


def test_capture_replays_exactly_and_rejects_owner_or_content_reuse(
    configured, monkeypatch
):
    coordinator, records, _terminal = _signed_history(configured)
    monkeypatch.setattr(
        kernel_authority,
        "_trusted_coordinator_public_key",
        lambda: coordinator.public_key,
    )
    admission = _seal_admission(
        coordinator,
        provisional_findings.build_recovery_admission(records, task_id="review-1"),
    )
    records.append(admission)
    result = _review_result()

    first = provisional_findings.capture_provisional_findings(
        configured, authority_records=records, admission=admission, result=result
    )
    assert (
        provisional_findings.capture_provisional_findings(
            configured, authority_records=records, admission=admission, result=result
        )
        == first
    )
    assert len(provisional_findings.load_provisional_findings(configured)) == 1

    with pytest.raises(provisional_findings.ProvisionalFindingError, match="payload"):
        provisional_findings.capture_provisional_findings(
            configured,
            authority_records=records,
            admission=admission,
            result={"findings": [{"severity": "important", "claim": "different"}]},
        )
    forged = {**admission, "provisional_owner_id": "pfo_" + "9" * 64}
    with pytest.raises(
        provisional_findings.ProvisionalFindingError, match="authenticated"
    ):
        provisional_findings.capture_provisional_findings(
            configured, authority_records=records, admission=forged, result=result
        )


def test_authenticated_owner_without_capture_fails_closed(configured, monkeypatch):
    coordinator, records, _terminal = _signed_history(configured)
    monkeypatch.setattr(
        kernel_authority,
        "_trusted_coordinator_public_key",
        lambda: coordinator.public_key,
    )
    admission = _seal_admission(
        coordinator,
        provisional_findings.build_recovery_admission(records, task_id="review-1"),
    )

    with pytest.raises(
        provisional_findings.ProvisionalFindingError, match="no complete capture"
    ):
        provisional_findings.load_provisional_findings(
            configured, owner_id=admission["provisional_owner_id"]
        )


def test_capture_rejects_changed_or_symlinked_result_artifact(configured, monkeypatch):
    coordinator, records, _terminal = _signed_history(configured)
    monkeypatch.setattr(
        kernel_authority,
        "_trusted_coordinator_public_key",
        lambda: coordinator.public_key,
    )
    admission = _seal_admission(
        coordinator,
        provisional_findings.build_recovery_admission(records, task_id="review-1"),
    )
    records.append(admission)
    artifact = configured / str(admission["result_artifact"])
    original = artifact.read_bytes()
    artifact.write_bytes(b"{}")
    with pytest.raises(
        provisional_findings.ProvisionalFindingError, match="digest changed"
    ):
        provisional_findings.capture_provisional_findings(
            configured, authority_records=records, admission=admission
        )

    target = configured / ".audit" / "dispatch" / "results" / "target.json"
    target.write_bytes(original)
    artifact.unlink()
    artifact.symlink_to(target)
    with pytest.raises(provisional_findings.ProvisionalFindingError, match="symlink"):
        provisional_findings.capture_provisional_findings(
            configured, authority_records=records, admission=admission
        )


def test_capture_interruption_before_replace_is_cleanly_retryable(
    configured, monkeypatch
):
    coordinator, records, _terminal = _signed_history(configured)
    monkeypatch.setattr(
        kernel_authority,
        "_trusted_coordinator_public_key",
        lambda: coordinator.public_key,
    )
    admission = _seal_admission(
        coordinator,
        provisional_findings.build_recovery_admission(records, task_id="review-1"),
    )
    records.append(admission)
    real_replace = provisional_findings.os.replace
    monkeypatch.setattr(
        provisional_findings.os,
        "replace",
        lambda *_args: (_ for _ in ()).throw(OSError("simulated crash")),
    )
    with pytest.raises(findings.LedgerReadError, match="simulated crash"):
        provisional_findings.capture_provisional_findings(
            configured, authority_records=records, admission=admission
        )
    assert provisional_findings._stream_records(configured) == []

    monkeypatch.setattr(provisional_findings.os, "replace", real_replace)
    receipt = provisional_findings.capture_provisional_findings(
        configured, authority_records=records, admission=admission
    )
    assert receipt["finding_count"] == 1


def test_binding_is_permanent_idempotent_and_cannot_move_to_another_pr(
    configured, monkeypatch
):
    coordinator, records, _terminal = _signed_history(configured)
    monkeypatch.setattr(
        kernel_authority,
        "_trusted_coordinator_public_key",
        lambda: coordinator.public_key,
    )
    admission = _seal_admission(
        coordinator,
        provisional_findings.build_recovery_admission(records, task_id="review-1"),
    )
    records.append(admission)
    receipt = provisional_findings.capture_provisional_findings(
        configured,
        authority_records=records,
        admission=admission,
        result=_review_result(),
    )
    unsigned = provisional_findings.build_publication_binding(
        records,
        repo=configured,
        admission=admission,
        capture_receipt=receipt,
        pr=18,
        head="e" * 40,
        base="main",
        repository="feder-positronics/loop-zero",
    )
    binding = coordinator.seal(unsigned, authority_kind="coordinator")
    records.append(binding)

    assert provisional_findings.authenticated_publication_bindings(records) == {
        admission["provisional_owner_id"]: binding
    }
    forged_repository = coordinator.seal(
        {**unsigned, "repository": ""}, authority_kind="coordinator"
    )
    assert (
        provisional_findings.authenticated_publication_bindings(
            [*records[:-1], forged_repository]
        )
        == {}
    )
    assert (
        provisional_findings.build_publication_binding(
            records,
            repo=configured,
            admission=admission,
            capture_receipt=receipt,
            pr=18,
            head="e" * 40,
            base="main",
            repository="feder-positronics/loop-zero",
        )
        == unsigned
    )
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

    materialized = provisional_findings.materialize_publication_binding(
        configured, authority_records=records, binding=binding
    )
    assert (
        provisional_findings.materialize_publication_binding(
            configured, authority_records=records, binding=binding
        )
        == materialized
    )
    projected = findings.load_finding_records(configured, pr=18)
    assert [(row["severity"], row["delivery_run_id"]) for row in projected] == [
        ("important", admission["run_id"])
    ]


def test_truncated_capture_stream_fails_closed(configured):
    stream = configured / ".audit" / "provisional-findings" / "2026-09-16.jsonl"
    stream.parent.mkdir(parents=True)
    stream.write_text(
        json.dumps({"type": "provisional-finding"}) + "\n{", encoding="utf-8"
    )

    with pytest.raises(
        provisional_findings.ProvisionalFindingError, match="invalid JSON"
    ):
        provisional_findings.load_provisional_findings(configured)


def test_capture_receipt_must_name_the_exact_persisted_findings(configured):
    stream = configured / ".audit" / "provisional-findings" / "2026-09-16.jsonl"
    stream.parent.mkdir(parents=True)
    rows = [
        {
            "type": provisional_findings.FINDING_TYPE,
            "provisional_owner_id": "pfo_test",
            "finding_id": "pf_real",
        },
        {
            "type": provisional_findings.CAPTURE_TYPE,
            "provisional_owner_id": "pfo_test",
            "finding_count": 1,
            "finding_ids": ["pf_forged"],
        },
    ]
    stream.write_text(
        "".join(json.dumps(row, sort_keys=True) + "\n" for row in rows),
        encoding="utf-8",
    )

    with pytest.raises(
        provisional_findings.ProvisionalFindingError, match="complete receipt"
    ):
        provisional_findings.load_provisional_findings(configured)


def test_recovery_is_authenticated_before_but_accepted_only_after_verdict(
    configured, monkeypatch
):
    coordinator, records, _terminal = _signed_history(configured)
    monkeypatch.setattr(
        kernel_authority,
        "_trusted_coordinator_public_key",
        lambda: coordinator.public_key,
    )
    admission = _seal_admission(
        coordinator,
        provisional_findings.build_recovery_admission(records, task_id="review-1"),
    )
    records.append(admission)
    receipt = provisional_findings.capture_provisional_findings(
        configured,
        authority_records=records,
        admission=admission,
        result=_review_result(),
    )
    recovery = coordinator.seal(
        provisional_findings.build_recovery_evidence(
            records, repo=configured, admission=admission, capture_receipt=receipt
        ),
        authority_kind="coordinator",
    )
    records.append(recovery)
    assert "invalid-review-recovery-verification" not in (
        review_authority.review_terminal_acceptance_reasons(recovery)
    )
    monkeypatch.setattr(
        review_authority, "review_terminal_acceptance_reasons", lambda *_a, **_k: ()
    )
    monkeypatch.setattr(
        review_authority, "verifier_identity_is_independent", lambda **_kwargs: True
    )

    assert (
        review_authority.authenticated_review_terminals(records)["review-1"] == recovery
    )
    assert review_authority.accepted_review_terminals(records) == {}

    verdict = coordinator.seal(
        {
            "schema_version": policy.TELEMETRY_SCHEMA_VERSION,
            "policy_version": policy.DISPATCH_POLICY_VERSION,
            "type": "verdict",
            "task_id": "review-1",
            "run_id": recovery["run_id"],
            "verdict": "pass",
            "target_worker_identity": recovery.get("worker_identity"),
            "verifier_identity": "independent-verifier",
        },
        authority_kind="coordinator",
    )
    records.append(verdict)
    assert review_authority.accepted_review_terminals(records)["review-1"] == recovery


def test_recovery_rejects_wrong_capture_and_unsigned_admission(configured, monkeypatch):
    coordinator, records, _terminal = _signed_history(configured)
    monkeypatch.setattr(
        kernel_authority,
        "_trusted_coordinator_public_key",
        lambda: coordinator.public_key,
    )
    unsigned = provisional_findings.build_recovery_admission(
        records, task_id="review-1"
    )
    with pytest.raises(
        provisional_findings.ProvisionalFindingError, match="authenticated"
    ):
        provisional_findings.build_recovery_evidence(
            [*records, unsigned],
            repo=configured,
            admission=unsigned,
            capture_receipt={"type": provisional_findings.CAPTURE_TYPE},
        )


def test_enveloped_admission_projects_and_unknown_fields_do_not(
    configured, monkeypatch
):
    coordinator, records, _terminal = _signed_history(configured)
    monkeypatch.setattr(
        kernel_authority,
        "_trusted_coordinator_public_key",
        lambda: coordinator.public_key,
    )
    payload = provisional_findings.build_recovery_admission(records, task_id="review-1")
    unenveloped = coordinator.seal(payload, authority_kind="coordinator")
    assert (
        provisional_findings.authenticated_recovery_admissions([*records, unenveloped])
        == {}
    )
    admission = coordinator.seal(
        {
            "ts": "2026-09-16T12:00:00+00:00",
            **payload,
            "schema_version": policy.TELEMETRY_SCHEMA_VERSION,
            "policy_version": policy.DISPATCH_POLICY_VERSION,
        },
        authority_kind="coordinator",
    )
    assert (
        provisional_findings.authenticated_recovery_admissions([*records, admission])[
            "review-1"
        ]
        == admission
    )

    forged = coordinator.seal(
        {
            "ts": "2026-09-16T12:00:00+00:00",
            **payload,
            "schema_version": policy.TELEMETRY_SCHEMA_VERSION,
            "policy_version": policy.DISPATCH_POLICY_VERSION,
            "unexpected": True,
        },
        authority_kind="coordinator",
    )
    assert (
        provisional_findings.authenticated_recovery_admissions([*records, forged]) == {}
    )


def test_recovery_reloads_the_persisted_capture_receipt(configured, monkeypatch):
    coordinator, records, _terminal = _signed_history(configured)
    monkeypatch.setattr(
        kernel_authority,
        "_trusted_coordinator_public_key",
        lambda: coordinator.public_key,
    )
    admission = _seal_admission(
        coordinator,
        provisional_findings.build_recovery_admission(records, task_id="review-1"),
    )
    records.append(admission)
    receipt = provisional_findings.capture_provisional_findings(
        configured, authority_records=records, admission=admission
    )
    forged = {
        **receipt,
        "capture_request_sha256": "0" * 64,
        "finding_ids": ["pf_forged"],
    }

    with pytest.raises(
        provisional_findings.ProvisionalFindingError, match="persisted capture receipt"
    ):
        provisional_findings.build_recovery_evidence(
            records,
            repo=configured,
            admission=admission,
            capture_receipt=forged,
        )


def test_capture_anchors_findings_to_the_authenticated_predecessor(
    configured, monkeypatch
):
    coordinator, records, _terminal = _signed_history(configured)
    monkeypatch.setattr(
        kernel_authority,
        "_trusted_coordinator_public_key",
        lambda: coordinator.public_key,
    )
    admission = _seal_admission(
        coordinator,
        provisional_findings.build_recovery_admission(records, task_id="review-1"),
    )
    records.append(admission)

    provisional_findings.capture_provisional_findings(
        configured, authority_records=records, admission=admission
    )
    captured = provisional_findings.load_provisional_findings(
        configured, owner_id=str(admission["provisional_owner_id"])
    )
    expected_blob = subprocess.run(
        ["git", "rev-parse", f"{admission['snapshot_sha']}:a.py"],
        cwd=configured,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    assert captured[0]["content_anchor"] == {
        "path": "a.py",
        "blob_sha": expected_blob,
    }


def test_recovery_reconstructs_missing_review_chain_receipt(configured, monkeypatch):
    material = {
        "severity": "important",
        "claim": "fix it",
        "path": "a.py",
    }
    result = {
        "findings": [material],
        "review_sections": {
            "code": {
                "completion": "completed",
                "verdict": "findings",
                "findings": [material],
            },
            "security": {
                "completion": "completed",
                "verdict": "clean",
                "findings": [],
                "threat_model_summary": "No changed trust boundary.",
            },
        },
    }
    task_contract = {
        "task_id": "review-1",
        "review_intent": "delivery-code-review",
        "security_trigger_paths": ["a.py"],
        "required_sections": ["code", "security"],
    }
    coordinator, records, _terminal = _signed_history(
        configured, result=result, task_contract=task_contract
    )
    monkeypatch.setattr(
        kernel_authority,
        "_trusted_coordinator_public_key",
        lambda: coordinator.public_key,
    )
    admission = _seal_admission(
        coordinator,
        provisional_findings.build_recovery_admission(records, task_id="review-1"),
    )
    records.append(admission)
    receipt = provisional_findings.capture_provisional_findings(
        configured, authority_records=records, admission=admission
    )

    recovery = provisional_findings.build_recovery_evidence(
        records,
        repo=configured,
        admission=admission,
        capture_receipt=receipt,
    )

    assert (
        recovery["review_chain_receipt"]["sections"]["code"]["finding_ids"]
        == receipt["finding_ids"]
    )
    assert (
        recovery["review_chain_receipt"]["snapshot_tree_sha"]
        == admission["snapshot_tree_sha"]
    )


def test_classified_recovery_append_requires_both_locks_and_replays_once(
    configured, monkeypatch
):
    coordinator, records, _terminal = _signed_history(configured)
    monkeypatch.setattr(
        kernel_authority,
        "_trusted_coordinator_public_key",
        lambda: coordinator.public_key,
    )
    admission = _seal_admission(
        coordinator,
        provisional_findings.build_recovery_admission(records, task_id="review-1"),
    )
    records.append(admission)
    receipt = provisional_findings.capture_provisional_findings(
        configured, authority_records=records, admission=admission
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
    with pytest.raises(review_state.ReviewStateError, match="authority ledger lock"):
        provisional_findings.authorize_classified_recovery_append(
            configured, records, recovery
        )
    with authority_store.authority_ledger_lock(configured):
        with pytest.raises(RuntimeError, match="attempt lifecycle lock"):
            provisional_findings.authorize_classified_recovery_append(
                configured, records, recovery
            )
        with authority_store.attempt_lifecycle_lock(configured, "review-1"):
            forged = coordinator.seal(
                {
                    **{
                        key: value
                        for key, value in recovery.items()
                        if key != "terminal_authority_proof"
                    },
                    "recovered_result_sha256": "9" * 64,
                },
                authority_kind="coordinator",
            )
            with pytest.raises(
                provisional_findings.ProvisionalFindingError,
                match="evidence is invalid",
            ):
                provisional_findings.authorize_classified_recovery_append(
                    configured, records, forged
                )
            assert provisional_findings.authorize_classified_recovery_append(
                configured, records, recovery
            )
            assert not provisional_findings.authorize_classified_recovery_append(
                configured, [*records, recovery], recovery
            )
