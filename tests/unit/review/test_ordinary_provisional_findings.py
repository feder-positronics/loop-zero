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
    with (
        authority_store.authority_ledger_lock(repo),
        pytest.raises(Exception, match="lock"),
    ):
        provisional.authorize_capture_admission_append(repo, records, admission)
    unsigned = {k: v for k, v in admission.items() if k != "terminal_authority_proof"}
    with (
        authority_store.authority_ledger_lock(repo),
        authority_store.attempt_lifecycle_lock(repo, "review-1"),
        pytest.raises(provisional.ProvisionalFindingError),
    ):
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
    records.append(
        coordinator.seal(
            {
                "type": "verdict",
                "task_id": "review-1",
                "run_id": admission["run_id"],
                "verdict": "pass",
                "schema_version": policy.TELEMETRY_SCHEMA_VERSION,
                "policy_version": policy.DISPATCH_POLICY_VERSION,
            },
            authority_kind="coordinator",
        )
    )
    assert (
        authority.authenticated_review_verdict(
            admission, records, expected_family="delivery"
        )
        is None
    )
    assert (
        authority.classify_review_outcome(
            admission, records, expected_family="delivery"
        )
        is ReviewOutcome.UNRESOLVED
    )


def test_invalid_capture_join_cannot_gain_coordinator_verdict(ordinary):
    from loopzero.runners.contract import ReviewOutcome

    _, coordinator, dispatcher, records, _ = ordinary
    _, _, terminal = finish(ordinary)
    invalid = {
        key: value
        for key, value in terminal.items()
        if key != "terminal_authority_proof"
    }
    invalid["finding_capture_receipt_sha256"] = "0" * 64
    invalid = dispatcher.seal(invalid, authority_kind="dispatcher")
    records[-1] = invalid
    records.append(
        coordinator.seal(
            {
                "type": "verdict",
                "task_id": "review-1",
                "run_id": terminal["run_id"],
                "verdict": "pass",
                "schema_version": policy.TELEMETRY_SCHEMA_VERSION,
                "policy_version": policy.DISPATCH_POLICY_VERSION,
            },
            authority_kind="coordinator",
        )
    )
    assert (
        authority.classify_review_outcome(invalid, records, expected_family="delivery")
        is ReviewOutcome.UNRESOLVED
    )


@pytest.mark.parametrize("run_state", ["missing", "legacy", "changed"])
def test_native_run_contract_required_at_build_admission_and_terminal(
    ordinary, run_state
):
    import json

    from loopzero.runners.contract import ReviewOutcome

    repo, _, _, records, completion = ordinary
    admission, _, terminal = finish(ordinary)
    run_log = repo / ".audit/skill-runs/fixture.jsonl"
    if run_state == "missing":
        run_log.unlink()
    elif run_state == "legacy":
        run_log.write_text(
            json.dumps(
                {"run_id": admission["run_id"], "delivery_contract": "intelflo-v1"}
            )
            + "\n"
        )
    else:
        with run_log.open("a") as handle:
            handle.write(
                json.dumps(
                    {"run_id": admission["run_id"], "delivery_contract": "intelflo-v1"}
                )
                + "\n"
            )
    before_admission = records[
        : next(
            index
            for index, row in enumerate(records)
            if row.get("type") == "finding-capture-admission-v1"
        )
    ]
    with pytest.raises(provisional.ProvisionalFindingError, match="native"):
        provisional.build_producer_completion(
            before_admission,
            repo=repo,
            task_id="review-1",
            result_artifact=completion["result_artifact"],
            result_sha256=completion["result_sha256"],
        )
    with pytest.raises(provisional.ProvisionalFindingError, match="native"):
        provisional.build_capture_admission(
            before_admission, repo=repo, completion=completion
        )
    assert (
        authority.authenticated_review_verdict(
            terminal, records, expected_family="delivery"
        )
        is None
    )
    assert (
        authority.classify_review_outcome(terminal, records, expected_family="delivery")
        is ReviewOutcome.UNRESOLVED
    )


def test_verdict_consumes_original_slot_once_and_survives_compaction(ordinary):
    from loopzero.kernel import authority_projection
    from loopzero.runners.contract import ReviewOutcome

    repo, coordinator, _, records, _ = ordinary
    admission, _, terminal = finish(ordinary)
    records.append(
        coordinator.seal(
            {
                "type": "verdict",
                "task_id": "review-1",
                "run_id": terminal["run_id"],
                "verdict": "fail",
                "schema_version": policy.TELEMETRY_SCHEMA_VERSION,
                "policy_version": policy.DISPATCH_POLICY_VERSION,
            },
            authority_kind="coordinator",
        )
    )
    reservation = review_state.ReviewSlotReservation.from_dict(
        next(row for row in records if row["type"] == "review-slot-reservation-v1")
    )
    with authority_store.authority_ledger_lock(repo):
        settlement = review_state.settle_review_slot(
            repo,
            records,
            reservation=reservation,
            outcome=ReviewOutcome.CONSUMED,
            terminal_ref=terminal,
        )
    records.append(coordinator.seal(settlement.to_dict(), authority_kind="coordinator"))
    before = authority_projection.slot_state(
        records, admission["generation_id"], "delivery"
    )
    retained = authority_store.retained_authority_projection(records)
    compacted = authority_store._prospective_retained_view(
        retained, source_records=records
    )
    after = authority_projection.slot_state(
        compacted, admission["generation_id"], "delivery"
    )
    assert before.primary_consumed and after.primary_consumed
    assert before.attempt_count("primary") == after.attempt_count("primary") == 1
    assert not before.delta_consumed and not after.delta_consumed
    assert len(before.reservations) == len(after.reservations) == 1
    assert (
        authority.authenticated_review_verdict(
            terminal, compacted, expected_family="delivery"
        )
        == "fail"
    )


def test_publication_blocks_pending_or_open_ordinary_debt(ordinary):
    from loopzero.delivery.publish import open_important_finding_ids
    from loopzero.review.findings import load_finding_records

    repo, _, _, records, _ = ordinary
    admission, receipt, terminal = finish(ordinary)
    assert not load_finding_records(repo, pr=18)
    assert open_important_finding_ids(
        repo, "refs/heads/fix/18", dispatch_records=records, finding_records=[]
    ) == tuple(receipt["finding_ids"])
    assert not open_important_finding_ids(
        repo, "refs/heads/other", dispatch_records=records, finding_records=[]
    )
    records.remove(terminal)
    with pytest.raises(provisional.ProvisionalFindingError):
        open_important_finding_ids(
            repo, "refs/heads/fix/18", dispatch_records=records, finding_records=[]
        )
    assert (
        provisional.authenticated_capture_admissions(records)["review-1"] == admission
    )


def test_binding_cannot_reassign_ordinary_owner(ordinary):
    repo, coordinator, _, records, completion = ordinary
    admission, receipt, _ = finish(ordinary)
    args = {
        "repo": repo,
        "admission": admission,
        "capture_receipt": receipt,
        "head": completion["snapshot_sha"],
        "base": "main",
        "repository": "fixture/repo",
    }
    binding = provisional.build_publication_binding(records, pr=18, **args)
    records.append(coordinator.seal(binding, authority_kind="coordinator"))
    assert provisional.build_publication_binding(records, pr=18, **args) == binding
    with pytest.raises(provisional.ProvisionalFindingError, match="different PR"):
        provisional.build_publication_binding(records, pr=19, **args)


def test_signed_supersession_prevents_first_admission_without_releasing_slot(ordinary):
    from loopzero.kernel import authority_projection

    repo, coordinator, _, records, completion = ordinary
    records.append(
        coordinator.seal(
            {
                "type": "attempt-supersession",
                "status": "superseded",
                "task_id": "review-1",
                "superseded_task_id": "review-1",
                "run_id": completion["run_id"],
                "schema_version": policy.TELEMETRY_SCHEMA_VERSION,
                "policy_version": policy.DISPATCH_POLICY_VERSION,
            },
            authority_kind="coordinator",
        )
    )
    with pytest.raises(provisional.ProvisionalFindingError, match="superseded"):
        provisional.build_capture_admission(records, repo=repo, completion=completion)
    state = authority_projection.slot_state(
        records, completion["generation_id"], "delivery"
    )
    assert len(state.reservations) == 1
    assert state.settlement_for(completion["reservation_id"]) is None


def _first_deposition_failure(case):
    _, _, dispatcher, records, completion = case
    start = next(row for row in records if row["type"] == "attempt-start")
    failure = dispatcher.seal(
        {
            **{k: v for k, v in start.items()
               if k not in {"terminal_authority_proof", "terminal_authority"}},
            **{key: completion[key] for key in (
                "result_artifact", "result_sha256", "reservation_id",
                "generation_id", "family", "repository_binding")},
            "type": "attempt-terminal",
            "status": "infrastructure-failure",
            "failure_class": "finding-deposition-failed",
            "deposit_state": "none",
        },
        authority_kind="dispatcher",
    )
    records.append(failure)
    return failure


def test_pending_ordinary_owner_rejects_recovery_after_first_failure(ordinary):
    repo, _, _, records, _ = ordinary
    admission = admit(ordinary)
    receipt = provisional.capture_provisional_findings(
        repo, authority_records=records, admission=admission
    )
    assert receipt["finding_ids"]
    _first_deposition_failure(ordinary)
    assert sum(row["type"] == "attempt-terminal" for row in records) == 1
    with pytest.raises(provisional.ProvisionalFindingError, match="conflict"):
        provisional.build_recovery_admission(records, task_id="review-1")
    assert provisional.authenticated_capture_admissions(records)["review-1"] == admission


def test_historical_conflicting_owners_cannot_erase_publication_debt(ordinary):
    from loopzero.delivery.publish import open_important_finding_ids

    repo, coordinator, _, records, _ = ordinary
    admission = admit(ordinary)
    receipt = provisional.capture_provisional_findings(
        repo, authority_records=records, admission=admission
    )
    assert receipt["finding_ids"]
    failure = _first_deposition_failure(ordinary)
    # Construct the historically accepted signed record directly: a fixed
    # builder must not prevent this probe from testing already-persisted debt.
    recovery = coordinator.seal(
        {
            **provisional._admission_payload(failure),
            "ts": "2026-09-16T13:00:00+00:00",
            "schema_version": policy.TELEMETRY_SCHEMA_VERSION,
            "policy_version": policy.DISPATCH_POLICY_VERSION,
        },
        authority_kind="coordinator",
    )
    records.append(recovery)
    assert provisional.authenticated_recovery_admissions(records)["review-1"] == recovery
    assert provisional.authenticated_capture_admissions(records)["review-1"] == admission
    with pytest.raises(provisional.ProvisionalFindingError, match="conflict"):
        provisional.authenticated_provisional_admissions(records)
    with pytest.raises(provisional.ProvisionalFindingError, match="conflict"):
        open_important_finding_ids(
            repo, "refs/heads/fix/18", dispatch_records=records, finding_records=[]
        )
    assert recovery in records and admission in records
