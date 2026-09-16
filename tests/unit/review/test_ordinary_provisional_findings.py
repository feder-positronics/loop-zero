"""Native signed producer probes for ordinary prepublication finding capture."""
import copy

import pytest

from loopzero.kernel import authority as signing
from loopzero.kernel import authority_store, gitscope, policy
from loopzero.review import authority, provisional_findings as provisional
from tests.unit.review.test_provisional_findings_adversarial import configured, _signed_history


@pytest.fixture
def ordinary(configured, monkeypatch):
    coordinator, records, old_terminal = _signed_history(configured)
    monkeypatch.setattr(signing, "_trusted_coordinator_public_key", lambda: coordinator.public_key)
    dispatcher = signing.TerminalAuthority(coordinator.public_key, coordinator._private_key)
    start = {key: value for key, value in records[1].items() if key != "terminal_authority_proof"}
    start.pop("result_artifact")
    start.pop("result_sha256")
    start.pop("review_chain_receipt")
    start["delivery_contract"] = "loop-zero-v1"
    start["task_contract"] = {
        "task_id": "review-1", "review_intent": "delivery-code-review",
        "required_sections": ["code", "security"], "security_trigger_paths": ["owned.py"],
    }
    start["task_contract_hash"] = gitscope.task_contract_hash(start["task_contract"])
    start = coordinator.seal(start, authority_kind="coordinator")
    records = [records[0], start]
    completion = dispatcher.seal({
        **{key: start.get(key) for key in provisional._IDENTITY_FIELDS if key not in {"result_artifact", "result_sha256"}},
        "type": "finding-producer-completion-v1", "status": "completed",
        "result_artifact": old_terminal["result_artifact"],
        "result_sha256": old_terminal["result_sha256"],
        "schema_version": policy.TELEMETRY_SCHEMA_VERSION,
        "policy_version": policy.DISPATCH_POLICY_VERSION,
        "ts": "2026-09-16T12:00:00+00:00",
    }, authority_kind="dispatcher")
    return configured, coordinator, dispatcher, records, completion


def admit(case):
    repo, coordinator, _, records, completion = case
    payload = provisional.build_capture_admission(records, repo=repo, completion=completion)
    admission = coordinator.seal({
        **payload, "ts": "2026-09-16T12:00:00+00:00",
        "schema_version": policy.TELEMETRY_SCHEMA_VERSION,
        "policy_version": policy.DISPATCH_POLICY_VERSION,
    }, authority_kind="coordinator")
    with authority_store.authority_ledger_lock(repo), authority_store.attempt_lifecycle_lock(repo, "review-1"):
        assert provisional.authorize_capture_admission_append(repo, records, admission)
    records.append(admission)
    return admission


def test_first_capture_requires_no_failed_terminal_or_new_attempt(ordinary):
    repo, _, _, records, _ = ordinary
    admission = admit(ordinary)
    receipt = provisional.capture_provisional_findings(repo, authority_records=records, admission=admission)
    assert receipt["finding_count"] == 1
    assert provisional.capture_provisional_findings(repo, authority_records=records, admission=admission) == receipt
    assert not authority.accepted_review_terminals(records)
    assert not any(row["type"] in {"attempt-terminal", "attempt-recovery", "review-slot-settlement-v1"} for row in records)
    assert len([row for row in records if row["type"] == "attempt-start"]) == 1


@pytest.mark.parametrize("mutation", ["unsigned", "wrong-key", "run_id", "snapshot_sha", "result_sha256", "extra"])
def test_completion_forgery_or_identity_drift_cannot_admit(ordinary, mutation):
    repo, _, _, records, completion = ordinary
    changed = copy.deepcopy(completion)
    if mutation == "unsigned":
        changed.pop("terminal_authority_proof")
    elif mutation == "wrong-key":
        changed.pop("terminal_authority_proof")
        changed = signing.TerminalAuthority.generate().seal(changed, authority_kind="dispatcher")
    else:
        changed[mutation] = "forged"
    with pytest.raises(provisional.ProvisionalFindingError):
        provisional.build_capture_admission(records, repo=repo, completion=changed)
    assert not (repo / ".audit/provisional-findings").exists()


def test_signed_wrong_result_and_post_terminal_completion_rejected(ordinary):
    repo, _, dispatcher, records, completion = ordinary
    unsigned = {k: v for k, v in completion.items() if k != "terminal_authority_proof"}
    forged = dispatcher.seal({**unsigned, "result_sha256": "0" * 64}, authority_kind="dispatcher")
    with pytest.raises(provisional.ProvisionalFindingError):
        provisional.build_capture_admission(records, repo=repo, completion=forged)
    records.append(dispatcher.seal({**unsigned, "type": "attempt-terminal"}, authority_kind="dispatcher"))
    with pytest.raises(provisional.ProvisionalFindingError):
        provisional.build_capture_admission(records, repo=repo, completion=completion)


def test_ordinary_terminal_capture_binding_and_materialization(ordinary):
    repo, _, dispatcher, records, completion = ordinary
    admission = admit(ordinary)
    receipt = provisional.capture_provisional_findings(repo, authority_records=records, admission=admission)
    evidence = provisional.build_capture_terminal_evidence(records, repo=repo, admission=admission, capture_receipt=receipt)
    terminal = dispatcher.seal({
        **{k: v for k, v in records[1].items() if k not in {"terminal_authority_proof", "terminal_authority"}},
        **evidence, "type": "attempt-terminal", "status": "completed",
        "result_artifact": completion["result_artifact"], "result_sha256": completion["result_sha256"],
    }, authority_kind="dispatcher")
    records.append(terminal)
    assert authority.authenticated_review_terminals(records)["review-1"] == terminal
    assert provisional.authenticated_provisional_findings(records, repo, admission=admission)
    binding = provisional.build_publication_binding(records, repo=repo, admission=admission,
        capture_receipt=receipt, pr=18, head=completion["snapshot_sha"], base="main", repository="fixture/repo")
    coordinator = ordinary[1]
    binding = coordinator.seal({**binding, "ts": "2026-09-16T12:01:00+00:00", "schema_version": policy.TELEMETRY_SCHEMA_VERSION,
        "policy_version": policy.DISPATCH_POLICY_VERSION}, authority_kind="coordinator")
    records.append(binding)
    first = provisional.materialize_publication_binding(repo, authority_records=records, binding=binding)
    assert provisional.materialize_publication_binding(repo, authority_records=records, binding=binding) == first
