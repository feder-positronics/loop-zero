from loopzero.kernel import authority as kernel_authority, authority_store, seams
from loopzero.review import authority
from loopzero.runners.contract import ReviewOutcome
import copy
import pytest


class Retained(dict):
    checkpoint_authenticated_retention = True


@pytest.fixture(autouse=True)
def isolated_archive_authority(monkeypatch, tmp_path, isolated_ptrace_scope_path):
    state = tmp_path / "dispatch-authority"
    monkeypatch.setattr(kernel_authority, "PTRACE_SCOPE_PATH", isolated_ptrace_scope_path)
    monkeypatch.setattr(kernel_authority, "_coordinator_state_directory", lambda: state)


def test_latest_settlement_keeps_highest_attempt_and_last_equal_attempt():
    rows = [
        {"type": "attempt-terminal", "task_id": "a", "attempt_index": 1},
        {"type": "attempt-abort", "task_id": "a", "attempt_index": 2},
        {"type": "attempt-terminal", "task_id": "b", "attempt_index": 1},
        {"type": "attempt-terminal", "task_id": "a", "attempt_index": 2},
    ]
    assert authority._latest_attempt_settlement_indices(rows) == frozenset({2, 3})


def test_archive_anchor_is_accepted_history_only():
    row = Retained(
        type="attempt-terminal", task_id="review-1", status="completed",
        read_only=True, work_kind="review", review_gate_terminal=True,
        review_acceptance_verified=True, accepted_verdict="pass",
    )
    assert authority.passing_archive_anchor([row], "review-1") is row
    assert authority.passing_archive_anchor([{**row, "accepted_verdict": "fail"}], "review-1") is None
    assert authority.passing_archive_anchor([row, row], "review-1") is None


def test_checkpoint_retained_retry_outcome_remains_classifiable():
    released = Retained(
        type="attempt-terminal",
        task_id="retry",
        status="infrastructure-failure",
        terminal_reason="transport-disconnect",
    )
    consumed = Retained(
        type="attempt-terminal",
        task_id="review",
        status="completed",
        review_acceptance_verified=True,
        accepted_verdict="pass",
    )
    assert authority.classify_review_outcome(released) is ReviewOutcome.RELEASED
    assert authority.classify_review_outcome(consumed) is ReviewOutcome.CONSUMED


def test_archive_deposits_fail_closed_without_excluded_controller_validator():
    authority.configure(uncarryable_delta_authority_is_valid=None)
    assert authority.archived_supersession_deposits([], []) == {}


def test_kernel_a4_seams_are_bound_to_mechanisms():
    authority.configure_kernel_seams()
    rows = [
        {"type": "inline", "task_id": "one"},
        {"type": "inline", "task_id": "one"},
    ]
    assert seams._latest_attempt_settlement_indices(rows) == frozenset({1})


def _signed_archived_review():
    coordinator = authority_store.create_coordinator_authority()
    dispatcher = kernel_authority.TerminalAuthority.generate()
    common = {
        "task_id": "archived-review",
        "attempt_index": 0,
        "run_id": "sr_" + "1" * 32,
        "work_unit_id": "review-unit",
        "worktree": "/repo",
        "read_only": True,
        "work_kind": "review",
        "root_work_unit_id": "root-unit",
        "lineage": {"task_id": "archived-review", "root_work_unit_id": "root-unit"},
        "task_contract": {"work_kind": "review", "category": "code-review"},
        "budget_usd": 10,
        "cost_usd": 2,
        "cost_status": "measured",
        "tokens": 200,
        "snapshot_sha": "a" * 40,
        "source_identity": {"ref": "refs/heads/feature/archive", "head": "b" * 40},
        "result_artifact": ".audit/dispatch/results/review.json",
        "result_sha256": "c" * 64,
    }
    start = coordinator.seal(
        {
            **common,
            "type": "attempt-start",
            "terminal_authority": dispatcher.registration(),
        },
        authority_kind="coordinator",
    )
    terminal = dispatcher.seal(
        {**common, "type": "attempt-terminal", "status": "completed"},
        authority_kind="dispatcher",
    )
    anchor = Retained(
        terminal,
        review_gate_terminal=True,
        review_acceptance_verified=True,
        accepted_verdict="pass",
    )
    return anchor, {"start": start, "terminal": terminal}


@pytest.mark.parametrize(
    "field",
    ("task_contract", "lineage", "budget_usd", "cost_usd", "cost_status", "tokens"),
)
def test_archived_supersession_binds_full_terminal_record(field):
    anchor, witness = _signed_archived_review()
    terminal = witness["terminal"]
    current = {**terminal["source_identity"], "head": "d" * 40}
    supersession = {
        "type": "attempt-supersession",
        "task_id": terminal["task_id"],
        "archived_review_witness": witness,
        "supersession_reason": "stale-source",
        "superseding_source_identity": current,
        "discarded_snapshot_bound_reason": "cumulative author delta exceeds cap",
        "uncarryable_delta_authority": {
            "schema_version": "uncarryable-delta-authority-v1",
            "reason": "delta-churn-exceeded",
            "predecessor_task_id": terminal["task_id"],
            "predecessor_snapshot_sha": terminal["snapshot_sha"],
            "superseding_source_identity": current,
        },
        **{name: terminal[name] for name in (
            "task_contract", "root_work_unit_id", "lineage", "budget_usd",
            "cost_usd", "cost_status", "tokens",
        )},
    }

    assert authority.archived_supersession_deposits([anchor], [supersession]) == {
        ("archived-review", 0): [terminal]
    }
    damaged = copy.deepcopy(supersession)
    damaged[field] = "forged"
    assert authority.archived_supersession_deposits([anchor], [damaged]) == {}


@pytest.mark.parametrize(
    "damage",
    ("result_artifact", "task_contract", "registration", "terminal-signature"),
)
def test_archived_review_witness_rejects_identity_or_signature_damage(damage):
    anchor, witness = _signed_archived_review()
    damaged = copy.deepcopy(witness)
    if damage == "registration":
        damaged["start"]["terminal_authority"] = kernel_authority.TerminalAuthority.generate().registration()
    elif damage == "terminal-signature":
        damaged["terminal"]["status"] = "failed"
    else:
        damaged["terminal"][damage] = "forged"

    with pytest.raises(authority.DispatchError):
        authority.validate_archived_review_witness(damaged, anchor)


@pytest.fixture
def outcome_authentication(monkeypatch):
    monkeypatch.setattr(
        authority,
        "_authenticated_attempt_terminal_ids",
        lambda rows, **kwargs: frozenset(
            id(row) for row in rows if row.get("ledger_authenticated") is True
        ),
    )
    monkeypatch.setattr(
        authority,
        "_authenticated_coordinator_record_ids",
        lambda rows: frozenset(
            id(row) for row in rows if row.get("coordinator_authenticated") is True
        ),
    )


@pytest.mark.parametrize(
    "reason",
    ("model-result", "transport-disconnect", "budget-exhausted"),
)
def test_authenticated_infrastructure_outcomes_release(
    outcome_authentication, reason
):
    terminal = {
        "type": "attempt-terminal",
        "task_id": "review",
        "status": "failed",
        "terminal_reason": reason,
        "ledger_authenticated": True,
    }
    assert authority.classify_review_outcome(terminal, [terminal]) is ReviewOutcome.RELEASED


@pytest.mark.parametrize(
    "reason", (None, "model-result", "budget-exhausted-after-result")
)
def test_coordinator_verdict_consumes(outcome_authentication, reason):
    terminal = {
        "type": "attempt-terminal",
        "task_id": "review",
        "run_id": "run",
        "status": "completed" if reason is None else "failed",
        "ledger_authenticated": True,
    }
    if reason is not None:
        terminal["terminal_reason"] = reason
    verdict = {
        "type": "verdict",
        "task_id": "review",
        "run_id": "run",
        "verdict": "fail",
        "coordinator_authenticated": True,
    }
    assert authority.classify_review_outcome(
        terminal, [terminal, verdict]
    ) is ReviewOutcome.CONSUMED


def test_self_asserted_authentication_without_ledger_verdict_is_unresolved(
    outcome_authentication,
):
    forged = {
        "type": "attempt-terminal",
        "task_id": "review",
        "status": "completed",
        "ledger_authenticated": True,
        "review_acceptance_verified": True,
        "accepted_verdict": "pass",
        "verdict_authenticated": True,
        "authenticated_verdict": {"verdict": "pass", "authenticated": True},
    }
    assert authority.classify_review_outcome(forged, [forged]) is ReviewOutcome.UNRESOLVED


def test_unknown_or_unrecorded_terminal_is_unresolved(outcome_authentication):
    terminal = {"status": "forged", "terminal_reason": "invented"}
    assert authority.classify_review_outcome(terminal, [terminal]) is ReviewOutcome.UNRESOLVED
