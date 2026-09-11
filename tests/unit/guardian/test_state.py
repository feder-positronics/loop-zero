"""Strict persistence and timing tests for Guardian observations."""

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

def load_state():
    from loopzero.guardian import state

    return state


SOURCE = "a" * 40
CLAIM = {
    "id": "QG-1",
    "claim": "quality remains bounded",
    "type": "metric_max",
    "command": "echo 1",
    "threshold": 5,
    "headroom": 4,
    "repair_scope": ["scripts/util/*.py"],
    "acceptance_command": "pytest guardian",
    "candidate_command": "printf 'scripts/util/guardian_tick.py\\n'",
}


def observation(state, transition: str = "quiet", identity: str = "2026-08-11T06:00Z"):
    return state.build_observation(
        identity=identity,
        claim=CLAIM,
        measured_source_sha=SOURCE,
        transition=transition,
        verified_source_sha=SOURCE if transition == "resolved" else None,
        issue_number=42 if transition == "escalated" else None,
        charter=(
            "Fix the exact frozen Guardian ticket and stop."
            if transition in {"ticket", "paused"}
            else None
        ),
        value=1.0,
        ts="2026-08-11T06:01:00Z",
    )


def test_append_is_source_bound_fsynced_reread_and_idempotent(tmp_path: Path) -> None:
    state = load_state()
    event = observation(state)

    assert state.append_observation(event, tmp_path) is True
    assert state.append_observation(event, tmp_path) is False
    retry = {**event, "ts": "2026-08-11T06:02:00Z"}
    assert state.append_observation(retry, tmp_path) is False

    rows = state.read_observations(tmp_path)
    assert rows == (event,)
    assert rows[0]["command_digest"] == state.command_digest(CLAIM["command"])
    assert rows[0]["claim_command"] == CLAIM["command"]
    assert rows[0]["policy_digest"] == state.policy_digest(CLAIM)
    assert rows[0]["headroom"] == 4
    assert rows[0]["acceptance_command"] == "pytest guardian"
    assert rows[0]["repair_scope"] == ["scripts/util/*.py"]
    assert rows[0]["candidate_command"].startswith("printf")


def test_observation_and_boundary_outcome_fsync_audit_directory(
    tmp_path: Path, monkeypatch
) -> None:
    state = load_state()
    audit_dir = tmp_path / "new" / "agent-events"
    synced = []
    original = state._fsync_directory

    def record(directory):
        synced.append(directory)
        original(directory)

    monkeypatch.setattr(state, "_fsync_directory", record)
    assert state.append_observation(observation(state), audit_dir)
    assert state.append_dispatch_broken(
        failure_class="GuardianStateError",
        audit_dir=audit_dir,
        ts="2026-08-11T06:02:00Z",
    )

    assert tmp_path in synced
    assert audit_dir.parent in synced
    assert synced.count(audit_dir) == 2


@pytest.mark.parametrize("boundary", [False, True])
def test_directory_fsync_failure_is_not_reported_as_durable(
    tmp_path: Path, monkeypatch, boundary: bool
) -> None:
    state = load_state()
    audit_dir = tmp_path / "agent-events"
    original = state._fsync_directory

    def fail_audit_directory(directory):
        if directory == audit_dir:
            raise OSError("directory fsync failed")
        original(directory)

    monkeypatch.setattr(state, "_fsync_directory", fail_audit_directory)
    if boundary:
        with pytest.raises(state.GuardianStateError, match="dispatcher outcome"):
            state.append_dispatch_broken(
                failure_class="GuardianStateError",
                audit_dir=audit_dir,
                ts="2026-08-11T06:03:00Z",
            )
    else:
        with pytest.raises(state.GuardianStateError, match="durably append"):
            state.append_observation(observation(state), audit_dir)


def test_corrupt_existing_guardian_row_fails_closed(tmp_path: Path) -> None:
    state = load_state()
    (tmp_path / "2026-08-11.jsonl").write_text('{"kind":"claim_observation"}\n')

    with pytest.raises(state.GuardianStateError, match="missing required fields"):
        state.append_observation(observation(state), tmp_path)


def test_existing_row_with_unbound_evaluation_identity_fails_closed(
    tmp_path: Path,
) -> None:
    state = load_state()
    event = observation(state)
    event["evaluation_id"] = "b" * 64
    event["idempotency_key"] = state.transition_key(
        event["evaluation_id"], event["transition"]
    )
    (tmp_path / "2026-08-11.jsonl").write_text(json.dumps(event) + "\n")

    with pytest.raises(state.GuardianStateError, match="does not bind"):
        state.read_observations(tmp_path)


def test_changed_repair_authority_with_stale_policy_digest_fails_closed() -> None:
    state = load_state()
    event = observation(state, "ticket")
    event["repair_scope"] = ["**/*"]

    with pytest.raises(state.GuardianStateError, match="policy digest"):
        state.validate_observation(event)


def test_conflicting_duplicate_transition_fails_closed(tmp_path: Path) -> None:
    state = load_state()
    event = observation(state)
    assert state.append_observation(event, tmp_path)
    conflict = {
        **event,
        "quality_verdicts": [
            {
                "claim_id": "QG-1",
                "status": "VERIFIED",
                "value": 1.0,
                "threshold": 5,
            }
        ],
    }

    with pytest.raises(state.GuardianStateError, match="conflicting observation"):
        state.append_observation(conflict, tmp_path)


def test_known_malformed_dispatch_outcome_blocks_observation_admission(
    tmp_path: Path,
) -> None:
    state = load_state()
    (tmp_path / "2026-08-11.jsonl").write_text(
        json.dumps(
            {
                "kind": "guardian_dispatch_outcome",
                "schema_version": 1,
                "ts": "2026-08-11T06:01:00Z",
                "day": "2026-08-11",
                "transition": "broken",
                "stage": "dispatcher-boundary",
                "failure_class": "secret message is invalid",
                "idempotency_key": "f" * 64,
            }
        )
        + "\n",
        encoding="utf-8",
    )

    with pytest.raises(state.GuardianStateError, match="dispatcher outcome"):
        state.read_observations(tmp_path)
    with pytest.raises(state.GuardianStateError, match="dispatcher outcome"):
        state.append_observation(observation(state), tmp_path)


def test_contradictory_initial_and_ticket_transitions_fail_closed(
    tmp_path: Path,
) -> None:
    state = load_state()
    ticket = observation(state, "ticket")
    quiet = observation(state, "quiet")

    with pytest.raises(state.GuardianStateError, match="transition lifecycle"):
        state.reduce_observations((ticket, quiet))

    assert state.append_observation(ticket, tmp_path)
    with pytest.raises(state.GuardianStateError, match="transition lifecycle"):
        state.append_observation(quiet, tmp_path)


def test_ticket_lifecycle_allows_pause_failure_escalation_and_resolution() -> None:
    state = load_state()
    ticket = observation(state, "ticket")
    for suffix in (
        ("paused",),
        ("failed", "paused"),
        ("failed", "escalated"),
        ("paused", "resolved"),
    ):
        events = (ticket, *(observation(state, transition) for transition in suffix))
        state.reduce_observations(events)


@pytest.mark.parametrize(
    "transitions",
    (
        ("ticket", "failed", "resolved"),
        ("ticket", "escalated", "resolved"),
        ("ticket", "failed", "escalated", "resolved"),
    ),
)
def test_ticket_lifecycle_rejects_success_after_failure_or_escalation(
    transitions: tuple[str, ...],
) -> None:
    state = load_state()
    events = tuple(observation(state, transition) for transition in transitions)

    with pytest.raises(state.GuardianStateError, match="transition lifecycle"):
        state.reduce_observations(events)


def test_denied_or_non_directory_storage_fails_closed(tmp_path: Path) -> None:
    state = load_state()
    blocked = tmp_path / "not-a-directory"
    blocked.write_text("blocked")

    with pytest.raises(state.GuardianStateError, match="cannot durably append"):
        state.append_observation(observation(state), blocked)


def test_missing_row_after_flush_reread_is_broken(tmp_path: Path, monkeypatch) -> None:
    state = load_state()
    original = state.read_observations
    calls = 0

    def disappear(directory):
        nonlocal calls
        calls += 1
        return original(directory) if calls == 1 else ()

    monkeypatch.setattr(state, "read_observations", disappear)
    with pytest.raises(state.GuardianStateError, match="fsync/reread"):
        state.append_observation(observation(state), tmp_path)


def test_slot_boundaries_and_pending_catchup_are_stable() -> None:
    state = load_state()
    assert (
        state.scheduled_slot(datetime(2026, 8, 11, 6, 0, tzinfo=UTC))
        == "2026-08-11T06:00Z"
    )
    assert (
        state.scheduled_slot(datetime(2026, 8, 11, 5, 59, tzinfo=UTC))
        == "2026-08-10T18:00Z"
    )
    assert (
        state.scheduled_slot(datetime(2026, 8, 11, 11, 59, tzinfo=UTC))
        == "2026-08-11T06:00Z"
    )
    assert (
        state.scheduled_slot(datetime(2026, 8, 11, 12, 0, tzinfo=UTC))
        == "2026-08-11T12:00Z"
    )
    assert (
        state.scheduled_slot(datetime(2026, 8, 11, 17, 59, tzinfo=UTC))
        == "2026-08-11T12:00Z"
    )
    assert (
        state.scheduled_slot(datetime(2026, 8, 11, 18, 0, tzinfo=UTC))
        == "2026-08-11T18:00Z"
    )
    assert (
        state.scheduled_slot(datetime.fromisoformat("2026-08-11T08:04:00+02:00"))
        == "2026-08-11T06:00Z"
    )
    ticket = observation(state, "ticket")
    reduced = state.reduce_observations((ticket,))
    assert reduced.last_consumed_slot is None
    assert (
        state.select_scheduled_identity(
            datetime(2026, 8, 11, 18, 1, tzinfo=UTC), reduced
        )
        == "2026-08-11T06:00Z"
    )
    final = observation(state, "resolved")
    reduced = state.reduce_observations((ticket, final))
    assert reduced.last_consumed_slot == "2026-08-11T06:00Z"
    assert (
        state.select_scheduled_identity(
            datetime(2026, 8, 11, 6, 30, tzinfo=UTC), reduced
        )
        is None
    )
    assert (
        state.select_scheduled_identity(
            datetime(2026, 8, 11, 12, 1, tzinfo=UTC), reduced
        )
        == "2026-08-11T12:00Z"
    )


def test_manual_recording_requires_explicit_qualification_identity() -> None:
    state = load_state()
    assert state.manual_evaluation_id("qualification:2026-08-11:gate-a")
    with pytest.raises(state.GuardianStateError, match="manual recording"):
        state.manual_evaluation_id("manual-run")


def test_paused_slot_remains_pending_and_core_metadata_cannot_be_overridden() -> None:
    state = load_state()
    paused = observation(state, "paused")
    reduced = state.reduce_observations((paused,))
    assert reduced.pending_slots == ("2026-08-11T06:00Z",)
    assert reduced.last_consumed_slot is None

    with pytest.raises(state.GuardianStateError, match="cannot replace core fields"):
        state.build_observation(
            identity="2026-08-11T06:00Z",
            claim=CLAIM,
            measured_source_sha=SOURCE,
            transition="quiet",
            kind="forged",
        )


def test_observation_timestamp_must_be_utc() -> None:
    state = load_state()
    with pytest.raises(state.GuardianStateError, match="must be UTC"):
        state.build_observation(
            identity="2026-08-11T06:00Z",
            claim=CLAIM,
            measured_source_sha=SOURCE,
            transition="quiet",
            ts="2026-08-11T08:00:00+02:00",
        )


def test_resolved_provider_token_evidence_is_strict_and_complete() -> None:
    state = load_state()
    resolved = state.build_observation(
        identity="2026-08-11T06:00Z",
        claim=CLAIM,
        measured_source_sha=SOURCE,
        verified_source_sha=SOURCE,
        transition="resolved",
        tokens_in=100,
        tokens_out=20,
        token_coverage="partial",
        token_tasks_total=4,
        token_tasks_covered=2,
        run_id="sr_" + "1" * 32,
        implementation_task_id="guardian-implementation-1",
        review_task_id="guardian-review-1",
        security_review_task_id=None,
        delivery_commit_sha="b" * 40,
        pr_number=42,
    )
    state.validate_observation(resolved)

    with pytest.raises(state.GuardianStateError, match="conflicts with coverage"):
        state.validate_observation({**resolved, "token_coverage": "full"})
    missing = {**resolved}
    missing.pop("token_tasks_total")
    with pytest.raises(state.GuardianStateError, match="complete and resolved-only"):
        state.validate_observation(missing)
    with pytest.raises(state.GuardianStateError, match="resolved-only"):
        state.validate_observation(
            {
                **observation(state, "quiet"),
                "tokens_in": None,
                "tokens_out": None,
                "token_coverage": "none",
                "token_tasks_total": 4,
                "token_tasks_covered": 0,
            }
        )
    with pytest.raises(state.GuardianStateError, match="delivery lineage"):
        state.validate_observation({**resolved, "pr_number": 0})


def test_escalation_ownership_is_tracked_eligible_or_broken() -> None:
    state = load_state()
    claim = {**CLAIM, "tracked": "#42"}
    open_issue = [{"number": 42, "state": "open"}]
    closed_issue = [{"number": 42, "state": "closed"}]
    assert state.classify_escalation(claim, open_issue) == "tracked"
    assert state.classify_escalation(claim, closed_issue) == "eligible"
    assert state.classify_escalation(claim, None) == "broken"


def test_latest_escalation_is_policy_bound_and_requires_issue_number() -> None:
    state = load_state()
    event = observation(state, "escalated")
    assert state.latest_escalation((event,), "QG-1", event["policy_digest"]) == event
    assert state.latest_escalation((event,), "QG-1", "f" * 64) is None

    broken = {**event}
    broken.pop("issue_number")
    with pytest.raises(state.GuardianStateError, match="issue_number"):
        state.latest_escalation((broken,), "QG-1", event["policy_digest"])


def test_non_guardian_rows_remain_compatible_with_shared_event_stream(
    tmp_path: Path,
) -> None:
    state = load_state()
    (tmp_path / "2026-08-11.jsonl").write_text(
        json.dumps({"kind": "tool", "x": 1}) + "\n"
    )
    assert state.append_observation(observation(state), tmp_path)


def test_dispatch_broken_outcome_is_private_durable_and_idempotent(
    tmp_path: Path,
) -> None:
    state = load_state()
    assert state.append_dispatch_broken(
        failure_class="GuardianStateError",
        audit_dir=tmp_path,
        ts="2026-08-11T06:01:00Z",
    )
    assert not state.append_dispatch_broken(
        failure_class="GuardianStateError",
        audit_dir=tmp_path,
        ts="2026-08-11T06:02:00Z",
    )
    rows = [
        json.loads(line)
        for line in (tmp_path / "2026-08-11.jsonl").read_text().splitlines()
    ]
    assert len(rows) == 1
    assert rows[0]["kind"] == "guardian_dispatch_outcome"
    assert rows[0]["transition"] == "broken"
    assert rows[0]["failure_class"] == "GuardianStateError"
    assert "message" not in rows[0]

    assert state.read_dispatch_outcomes(tmp_path) == (rows[0],)


def test_qualification_dispatch_outcome_is_bound_to_unique_systemd_invocation(
    tmp_path: Path,
) -> None:
    state = load_state()
    first = "1" * 32
    second = "2" * 32

    assert state.append_dispatch_broken(
        failure_class="GuardianDispatchError",
        qualification_invocation_id=first,
        audit_dir=tmp_path,
        ts="2026-08-11T06:01:00Z",
    )
    assert state.append_dispatch_broken(
        failure_class="GuardianDispatchError",
        qualification_invocation_id=second,
        audit_dir=tmp_path,
        ts="2026-08-11T06:02:00Z",
    )

    rows = state.read_dispatch_outcomes(tmp_path)
    assert [row["qualification_invocation_id"] for row in rows] == [first, second]


def test_dispatch_outcome_reader_rejects_malformed_guardian_boundary_row(
    tmp_path: Path,
) -> None:
    state = load_state()
    (tmp_path / "2026-08-11.jsonl").write_text(
        json.dumps(
            {
                "kind": "guardian_dispatch_outcome",
                "schema_version": 1,
                "ts": "2026-08-11T06:01:00Z",
                "transition": "broken",
                "stage": "dispatcher-boundary",
                "failure_class": "contains a secret message",
                "idempotency_key": "f" * 64,
            }
        )
        + "\n",
        encoding="utf-8",
    )

    with pytest.raises(state.GuardianStateError, match="dispatcher outcome"):
        state.read_dispatch_outcomes(tmp_path)
