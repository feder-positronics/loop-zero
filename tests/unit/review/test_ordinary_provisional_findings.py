"""Native signed producer probes for ordinary prepublication finding capture."""

import copy

import pytest

from loopzero.kernel import authority as signing
from loopzero.kernel import authority_store, gitscope, policy, review_state
from loopzero.review import authority
from loopzero.review import provisional_findings as provisional
from tests.unit.review.test_provisional_findings_adversarial import _signed_history
from tests.unit.review.test_provisional_findings_adversarial import (
    configured as native_repo_fixture,
)

configured = native_repo_fixture


@pytest.fixture
def ordinary(configured, monkeypatch):
    coordinator, records, old_terminal = _signed_history(configured)
    monkeypatch.setattr(
        signing, "_trusted_coordinator_public_key", lambda: coordinator.public_key
    )
    dispatcher = signing.TerminalAuthority.generate()
    start = {
        key: value
        for key, value in next(
            row for row in records if row["type"] == "attempt-start"
        ).items()
        if key != "terminal_authority_proof"
    }
    start["terminal_authority"] = dispatcher.registration()
    start.pop("result_artifact")
    start.pop("result_sha256")
    start.pop("review_chain_receipt")
    import json

    run_dir = configured / ".audit/skill-runs"
    run_dir.mkdir(parents=True)
    (run_dir / "fixture.jsonl").write_text(
        json.dumps({"run_id": start["run_id"], "delivery_contract": "loop-zero-v1"})
        + "\n"
    )
    records = records[:1]
    resolution = review_state.resolve_generation(
        records,
        repository_binding=start["repository_binding"],
        patch_identity=start["patch_identity"],
        tree_sha=start["snapshot_tree_sha"],
        required_sections=("code", "security"),
        equivalence_proof=None,
        format_only_proof=None,
        family="delivery",
    )
    records.extend(
        coordinator.seal(row.to_dict(), authority_kind="coordinator")
        for row in (resolution.generation, resolution.coverage)
    )
    with authority_store.authority_ledger_lock(configured):
        reservation = review_state.reserve_review_slot(
            configured,
            records,
            generation_id=resolution.generation.generation_id,
            family="delivery",
            slot_kind="primary",
            task_id="review-1",
            idempotency_key="review-1",
        )
    records.append(
        coordinator.seal(reservation.to_dict(), authority_kind="coordinator")
    )
    start["review_reservation_id"] = reservation.reservation_id
    start["review_generation_id"] = reservation.generation_id
    start["reservation_id"] = reservation.reservation_id
    start["generation_id"] = reservation.generation_id
    start["family"] = "delivery"
    start["task_contract"] = {
        "review_intent": "delivery-code-review",
        "required_sections": ["code", "security"],
        "security_trigger_paths": ["owned.py"],
    }
    start["task_contract_hash"] = gitscope.task_contract_hash(start["task_contract"])
    for field in ("reservation_id", "generation_id", "family", "repository_binding"):
        start.pop(field)
    start["source_identity"] = {
        "version": 2,
        "ref": "refs/heads/fix/18",
        "head": start["snapshot_sha"],
        "state_sha256": "a" * 64,
    }
    start = coordinator.seal(start, authority_kind="coordinator")
    records.append(start)
    payload = provisional.build_producer_completion(
        records,
        repo=configured,
        task_id="review-1",
        result_artifact=old_terminal["result_artifact"],
        result_sha256=old_terminal["result_sha256"],
    )
    completion = dispatcher.seal(payload, authority_kind="dispatcher")
    return configured, coordinator, dispatcher, records, completion


def admit(case):
    repo, coordinator, _, records, completion = case
    payload = provisional.build_capture_admission(
        records, repo=repo, completion=completion
    )
    admission = coordinator.seal(
        {
            **payload,
            "ts": "2026-09-16T12:00:00+00:00",
            "schema_version": policy.TELEMETRY_SCHEMA_VERSION,
            "policy_version": policy.DISPATCH_POLICY_VERSION,
        },
        authority_kind="coordinator",
    )
    with (
        authority_store.authority_ledger_lock(repo),
        authority_store.attempt_lifecycle_lock(repo, "review-1"),
    ):
        assert provisional.authorize_capture_admission_append(repo, records, admission)
    records.append(admission)
    return admission


def test_first_capture_requires_no_failed_terminal_or_new_attempt(ordinary):
    repo, _, _, records, _ = ordinary
    admission = admit(ordinary)
    receipt = provisional.capture_provisional_findings(
        repo, authority_records=records, admission=admission
    )
    assert receipt["finding_count"] == 1
    assert (
        provisional.capture_provisional_findings(
            repo, authority_records=records, admission=admission
        )
        == receipt
    )
    assert not authority.accepted_review_terminals(records)
    assert not any(
        row["type"]
        in {"attempt-terminal", "attempt-recovery", "review-slot-settlement-v1"}
        for row in records
    )
    assert len([row for row in records if row["type"] == "attempt-start"]) == 1


@pytest.mark.parametrize(
    "mutation",
    ["unsigned", "wrong-key", "run_id", "snapshot_sha", "result_sha256", "extra"],
)
def test_completion_forgery_or_identity_drift_cannot_admit(ordinary, mutation):
    repo, _, _, records, completion = ordinary
    changed = copy.deepcopy(completion)
    if mutation == "unsigned":
        changed.pop("terminal_authority_proof")
    elif mutation == "wrong-key":
        changed.pop("terminal_authority_proof")
        changed = signing.TerminalAuthority.generate().seal(
            changed, authority_kind="dispatcher"
        )
    else:
        changed[mutation] = "forged"
    with pytest.raises(provisional.ProvisionalFindingError):
        provisional.build_capture_admission(records, repo=repo, completion=changed)
    assert not (repo / ".audit/provisional-findings").exists()


def test_signed_wrong_result_and_post_terminal_completion_rejected(ordinary):
    repo, _, dispatcher, records, completion = ordinary
    unsigned = {k: v for k, v in completion.items() if k != "terminal_authority_proof"}
    forged = dispatcher.seal(
        {**unsigned, "result_sha256": "0" * 64}, authority_kind="dispatcher"
    )
    with pytest.raises(provisional.ProvisionalFindingError):
        provisional.build_capture_admission(records, repo=repo, completion=forged)
    records.append(
        dispatcher.seal(
            {**unsigned, "type": "attempt-terminal"}, authority_kind="dispatcher"
        )
    )
    with pytest.raises(provisional.ProvisionalFindingError):
        provisional.build_capture_admission(records, repo=repo, completion=completion)


def test_ordinary_terminal_capture_binding_and_materialization(ordinary):
    repo, _, dispatcher, records, completion = ordinary
    admission = admit(ordinary)
    receipt = provisional.capture_provisional_findings(
        repo, authority_records=records, admission=admission
    )
    evidence = provisional.build_capture_terminal_evidence(
        records, repo=repo, admission=admission, capture_receipt=receipt
    )
    terminal = dispatcher.seal(
        {
            **{
                k: v
                for k, v in next(
                    row for row in records if row["type"] == "attempt-start"
                ).items()
                if k not in {"terminal_authority_proof", "terminal_authority"}
            },
            **evidence,
            "type": "attempt-terminal",
            "status": "completed",
            "result_artifact": completion["result_artifact"],
            "result_sha256": completion["result_sha256"],
        },
        authority_kind="dispatcher",
    )
    records.append(terminal)
    assert authority.authenticated_review_terminals(records)["review-1"] == terminal
    assert provisional.authenticated_provisional_findings(
        records, repo, admission=admission
    )
    binding = provisional.build_publication_binding(
        records,
        repo=repo,
        admission=admission,
        capture_receipt=receipt,
        pr=18,
        head=completion["snapshot_sha"],
        base="main",
        repository="fixture/repo",
    )
    coordinator = ordinary[1]
    binding = coordinator.seal(
        {
            **binding,
            "ts": "2026-09-16T12:01:00+00:00",
            "schema_version": policy.TELEMETRY_SCHEMA_VERSION,
            "policy_version": policy.DISPATCH_POLICY_VERSION,
        },
        authority_kind="coordinator",
    )
    records.append(binding)
    first = provisional.materialize_publication_binding(
        repo, authority_records=records, binding=binding
    )
    assert (
        provisional.materialize_publication_binding(
            repo, authority_records=records, binding=binding
        )
        == first
    )


def finish(case):
    repo, _, dispatcher, records, completion = case
    admission = admit(case)
    receipt = provisional.capture_provisional_findings(
        repo, authority_records=records, admission=admission
    )
    evidence = provisional.build_capture_terminal_evidence(
        records, repo=repo, admission=admission, capture_receipt=receipt
    )
    terminal = dispatcher.seal(
        {
            **{
                k: v
                for k, v in next(
                    row for row in records if row["type"] == "attempt-start"
                ).items()
                if k not in {"terminal_authority_proof", "terminal_authority"}
            },
            **evidence,
            "type": "attempt-terminal",
            "status": "completed",
            "result_artifact": completion["result_artifact"],
            "result_sha256": completion["result_sha256"],
        },
        authority_kind="dispatcher",
    )
    records.append(terminal)
    return admission, receipt, terminal


def test_compaction_preserves_ordinary_proof_capture_and_closed_replay(ordinary):
    repo, _, _, records, completion = ordinary
    admission, receipt, terminal = finish(ordinary)
    retained = authority_store.retained_authority_projection(records)
    compacted = authority_store._prospective_retained_view(
        retained, source_records=records
    )
    assert (
        provisional.authenticated_capture_admissions(compacted)["review-1"] == admission
    )
    assert authority.authenticated_review_terminals(compacted)["review-1"] == terminal
    assert (
        provisional.capture_provisional_findings(
            repo, authority_records=compacted, admission=admission
        )
        == receipt
    )
    with (
        authority_store.authority_ledger_lock(repo),
        authority_store.attempt_lifecycle_lock(repo, "review-1"),
    ):
        assert not provisional.authorize_capture_admission_append(
            repo, compacted, admission
        )
    assert (
        provisional.build_capture_admission(
            compacted, repo=repo, completion=completion
        )["provisional_owner_id"]
        == admission["provisional_owner_id"]
    )


@pytest.mark.parametrize(
    "field",
    [
        "provisional_owner_id",
        "finding_capture_receipt_sha256",
        "review_chain_receipt",
        "result_sha256",
    ],
)
def test_signed_terminal_cannot_change_capture_or_result(ordinary, field):
    _, _, dispatcher, records, _ = ordinary
    _, _, terminal = finish(ordinary)
    altered = {k: v for k, v in terminal.items() if k != "terminal_authority_proof"}
    altered[field] = "changed"
    records[-1] = dispatcher.seal(altered, authority_kind="dispatcher")
    assert "review-1" not in authority.authenticated_review_terminals(records)
    assert "review-1" not in authority.accepted_review_terminals(records)


def test_pending_admission_does_not_publish_and_changed_artifact_cannot_capture(
    ordinary,
):
    repo, _, _, records, completion = ordinary
    admission = admit(ordinary)
    with pytest.raises(provisional.ProvisionalFindingError):
        provisional.authenticated_provisional_findings(
            records, repo, admission=admission
        )
    (repo / completion["result_artifact"]).write_text("{}")
    with pytest.raises(provisional.ProvisionalFindingError):
        provisional.capture_provisional_findings(
            repo, authority_records=records, admission=admission
        )


def test_append_requires_both_locks_and_original_coordinator(ordinary):
    repo, coordinator, _, records, completion = ordinary
    payload = provisional.build_capture_admission(
        records, repo=repo, completion=completion
    )
    admission = coordinator.seal(
        {
            **payload,
            "ts": "2026-09-16T12:00:00+00:00",
            "schema_version": policy.TELEMETRY_SCHEMA_VERSION,
            "policy_version": policy.DISPATCH_POLICY_VERSION,
        },
        authority_kind="coordinator",
    )
    with pytest.raises(Exception, match="lock"):
        provisional.authorize_capture_admission_append(repo, records, admission)
    with authority_store.authority_ledger_lock(repo):
        with pytest.raises(Exception, match="lock"):
            provisional.authorize_capture_admission_append(repo, records, admission)
    unsigned = {k: v for k, v in admission.items() if k != "terminal_authority_proof"}
    with (
        authority_store.authority_ledger_lock(repo),
        authority_store.attempt_lifecycle_lock(repo, "review-1"),
    ):
        with pytest.raises(provisional.ProvisionalFindingError):
            provisional.authorize_capture_admission_append(repo, records, unsigned)


@pytest.mark.parametrize("change", ["missing", "symlink", "sections", "wrong-kind"])
def test_native_artifact_and_registration_fail_closed(ordinary, change):
    import hashlib
    import json

    repo, coordinator, dispatcher, records, completion = ordinary
    artifact = repo / completion["result_artifact"]
    if change == "missing":
        artifact.unlink()
    elif change == "symlink":
        target = artifact.with_name("outside.json")
        artifact.rename(target)
        artifact.symlink_to(target)
    elif change == "sections":
        payload = json.loads(artifact.read_text())
        payload.pop("review_sections")
        artifact.write_text(json.dumps(payload))
        completion = {
            k: v for k, v in completion.items() if k != "terminal_authority_proof"
        }
        completion["result_sha256"] = hashlib.sha256(artifact.read_bytes()).hexdigest()
        completion = dispatcher.seal(completion, authority_kind="dispatcher")
    else:
        start = {
            k: v
            for k, v in next(
                row for row in records if row["type"] == "attempt-start"
            ).items()
            if k != "terminal_authority_proof"
        }
        start["advisory"] = True
        records[
            records.index(
                next(row for row in records if row["type"] == "attempt-start")
            )
        ] = coordinator.seal(start, authority_kind="coordinator")
    with pytest.raises(provisional.ProvisionalFindingError):
        provisional.build_capture_admission(records, repo=repo, completion=completion)


def test_admission_never_inherits_a_later_task_verdict(ordinary):
    from loopzero.runners.contract import ReviewOutcome
    _, coordinator, _, records, _ = ordinary
    admission = admit(ordinary)
    records.append(coordinator.seal({
        "type": "verdict", "task_id": "review-1", "run_id": admission["run_id"],
        "verdict": "pass", "schema_version": policy.TELEMETRY_SCHEMA_VERSION,
        "policy_version": policy.DISPATCH_POLICY_VERSION,
    }, authority_kind="coordinator"))
    assert authority.authenticated_review_verdict(admission, records, expected_family="delivery") is None
    assert authority.classify_review_outcome(admission, records, expected_family="delivery") is ReviewOutcome.UNRESOLVED


def test_invalid_capture_join_cannot_gain_coordinator_verdict(ordinary):
    from loopzero.runners.contract import ReviewOutcome
    _, coordinator, dispatcher, records, _ = ordinary
    _, _, terminal = finish(ordinary)
    invalid = {key: value for key, value in terminal.items() if key != "terminal_authority_proof"}
    invalid["finding_capture_receipt_sha256"] = "0" * 64
    invalid = dispatcher.seal(invalid, authority_kind="dispatcher")
    records[-1] = invalid
    records.append(coordinator.seal({
        "type": "verdict", "task_id": "review-1", "run_id": terminal["run_id"],
        "verdict": "pass", "schema_version": policy.TELEMETRY_SCHEMA_VERSION,
        "policy_version": policy.DISPATCH_POLICY_VERSION,
    }, authority_kind="coordinator"))
    assert authority.classify_review_outcome(invalid, records, expected_family="delivery") is ReviewOutcome.UNRESOLVED
