from loopzero.kernel import seams
from loopzero.review import authority


class Retained(dict):
    checkpoint_authenticated_retention = True


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
