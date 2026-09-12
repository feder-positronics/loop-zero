"""Owning proofs for reentry recovery and publication invalidation."""

import importlib
import json
import os
import stat
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest

from loopzero.config import Alias
from loopzero.review import routing

reentry = importlib.import_module("loopzero.delivery.reentry")
RUN_ID = "sr_" + "1" * 32


@pytest.fixture(autouse=True)
def configured_source_routing():
    from types import SimpleNamespace

    routing.configure(
        SimpleNamespace(
            aliases={
                "opus": Alias("claude", "claude-opus-5", family="claude"),
                "astra": Alias("codex", "gpt-6-astra", family="gpt"),
            },
            tiers={}, routing_budgets={"medium": 5.0, "high": 10.0},
            routing_policy_version="2026-08-25-v12",
            telemetry_schema_version="dispatch-telemetry-v9",
            compatible_policy_versions=("2026-08-25-v12",),
            default_timeout_s=900, engine_cooldown_s=600,
            audit_root=Path(".audit"), env_prefix="INTELFLO",
            verifier_models={"astra": "gpt-6-astra"},
        )
    )


def test_generation_uses_authenticated_controls_and_counts_only_this_run(
    monkeypatch,
):
    authority = importlib.import_module("loopzero.review.authority")
    rows = [
        {"type": "delivery-control", "action": "review-reentry", "run_id": RUN_ID},
        {"type": "delivery-control", "action": "review-reentry", "run_id": "other"},
        {"type": "delivery-control", "action": "pause", "run_id": RUN_ID},
    ]
    monkeypatch.setattr(
        authority, "delivery_controller_records", lambda records: records[1:]
    )
    assert reentry.publication_generation(rows, RUN_ID) == 0
    monkeypatch.setattr(
        authority, "delivery_controller_records", lambda records: records
    )
    assert reentry.publication_generation(rows, RUN_ID) == 1


@pytest.mark.parametrize(
    ("before_ts", "decision_ts", "after_ts"),
    [
        ("2026-09-07T12:00:00Z", "2026-09-07T12:10:00Z", "2026-09-07T12:20:00Z"),
        ("2026-09-07T12:20:00Z", "2026-09-07T12:10:00Z", "2026-09-07T12:00:00Z"),
        ("2026-09-07T12:00:00Z",) * 3,
        (None, None, None),
    ],
)
def test_reminted_publication_requires_review_accepted_after_reentry(
    monkeypatch, before_ts, decision_ts, after_ts
):
    """Only ledger order matters, even when the wall clock moves backwards."""
    authority = importlib.import_module("loopzero.review.authority")
    decision = {
        "type": "delivery-control",
        "action": "review-reentry",
        "run_id": RUN_ID,
        "ts": decision_ts,
    }
    before = {"task_id": "before", "ts": before_ts}
    after = {"task_id": "after", "ts": after_ts}
    rows = [before, decision, after]
    monkeypatch.setattr(
        authority, "delivery_controller_records", lambda records: list(records)
    )
    monkeypatch.setattr(
        authority,
        "accepted_review_terminals",
        lambda records: {"before": before, "after": after, "absent": dict(after)},
    )
    reentry.require_review_after_reentry([], run_id=RUN_ID, review_task_ids=["before"])
    reentry.require_review_after_reentry(rows, run_id=RUN_ID, review_task_ids=["after"])
    for stale in (["before"], ["after", "before"], ["missing"], ["absent"]):
        with pytest.raises(ValueError, match="accepted after the reentry"):
            reentry.require_review_after_reentry(
                rows, run_id=RUN_ID, review_task_ids=stale
            )


def test_interrupted_phase_projection_can_be_repaired_once(monkeypatch, tmp_path):
    events = []
    record = {
        "type": "delivery-control",
        "action": "review-reentry",
        "run_id": RUN_ID,
        "reason": "gate demanded rebase",
    }
    phases = importlib.import_module("loopzero.kernel.run_log")
    agent_event = importlib.import_module("loopzero.kernel.events")
    monkeypatch.setattr(phases, "load_phase_events", lambda _: events)
    monkeypatch.setattr(phases, "phase_state", lambda *_, **__: ("ci-wait", "review"))
    monkeypatch.setattr(
        agent_event, "common_fields", lambda: {"ts": "2026-09-07T12:00:00Z"}
    )

    def append(event, **kwargs):
        events.append(event)
        return True

    monkeypatch.setattr(agent_event, "write_unique_event", append)
    assert reentry.pending_reentry([record], events, RUN_ID) == record
    for _ in range(2):
        reentry.append_reentry_phase(
            root=tmp_path, skill="work-issue", run_id=RUN_ID, record=record
        )
    assert len(events) == 1
    assert reentry.pending_reentry([record], events, RUN_ID) is None


def test_phase_history_replays_append_order_across_backward_utc_date(
    monkeypatch, tmp_path
):
    phases = importlib.import_module("loopzero.kernel.run_log")
    agent_event = importlib.import_module("loopzero.kernel.events")

    class Clock:
        current = datetime(2026, 9, 8, 0, 5, tzinfo=UTC)

        @classmethod
        def now(cls, _timezone):
            return cls.current

    monkeypatch.setattr(agent_event, "datetime", Clock)
    monkeypatch.setattr(agent_event, "repo_root", lambda: tmp_path)
    # Other test loaders replace agent_event in sys.modules during collection.
    # Bind the writer to the same module whose clock and root this test patches.
    monkeypatch.setattr(phases, "cmd_phase", agent_event.cmd_phase)

    for phase in ("implementation", "local-validation", "review", "ci-wait"):
        assert phases.transition_phase(
            root=tmp_path, skill="work-issue", run_id=RUN_ID, target=phase
        )
    assert phases.phase_state(
        phases.load_phase_events(tmp_path), skill="work-issue", run_id=RUN_ID
    ) == ("ci-wait", "review")

    Clock.current = datetime(2026, 9, 7, 23, 55, tzinfo=UTC)
    reentry.append_reentry_phase(
        root=tmp_path,
        skill="work-issue",
        run_id=RUN_ID,
        record={"reason": "clock moved backward across UTC midnight"},
    )
    assert phases.transition_phase(
        root=tmp_path, skill="work-issue", run_id=RUN_ID, target="ci-wait"
    )

    events = phases.load_phase_events(tmp_path)
    assert [event["status"] for event in events[-3:]] == [
        "review-reentry",
        "complete",
        "start",
    ]
    reentry_event = next(
        event for event in events if event["status"] == "review-reentry"
    )
    assert str(reentry_event["ts"]).startswith("2026-09-07")
    assert phases.phase_state(events, skill="work-issue", run_id=RUN_ID) == (
        "ci-wait",
        "review",
    )
    Clock.current = datetime(2026, 9, 9, 0, 5, tzinfo=UTC)
    assert phases.transition_phase(
        root=tmp_path, skill="work-issue", run_id=RUN_ID, target="closeout"
    )
    assert phases.phase_state(
        phases.load_phase_events(tmp_path), skill="work-issue", run_id=RUN_ID
    ) == ("closeout", "ci-wait")


@pytest.mark.skip(reason="(c) audit retention is owned by the kernel test corpus")
def test_forward_clock_excursion_does_not_pin_daily_rotation_or_retention(
    monkeypatch, tmp_path
):
    """A future-dated write must not absorb later ordinary daily events."""
    agent_event = importlib.import_module("loopzero.kernel.events")
    phases = importlib.import_module("loopzero.kernel.run_log")
    retention = importlib.import_module("audit_retention")

    class Clock:
        current = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)

        @classmethod
        def now(cls, _timezone):
            return cls.current

    monkeypatch.setattr(agent_event, "datetime", Clock)
    monkeypatch.setattr(agent_event, "repo_root", lambda: tmp_path)

    assert agent_event.write_event({"marker": "ordinary-before"})
    Clock.current = datetime(2126, 1, 1, 12, 0, tzinfo=UTC)
    assert agent_event.write_event({"marker": "forward-excursion"})
    Clock.current = datetime(2026, 1, 2, 12, 0, tzinfo=UTC)
    assert agent_event.write_event({"marker": "ordinary-after"})

    event_dir = tmp_path / ".audit" / "agent-events"
    assert sorted(path.name for path in event_dir.glob("*.jsonl")) == [
        "2026-01-01.jsonl",
        "2026-01-02.jsonl",
        "2126-01-01.jsonl",
    ]
    assert [event["marker"] for event in phases.load_phase_events(tmp_path)] == [
        "ordinary-before",
        "forward-excursion",
        "ordinary-after",
    ]

    policy = next(
        policy for policy in retention.POLICIES if policy.name == "agent-events"
    )
    compressed, deleted, _ = retention.sweep_directory(
        event_dir, policy, datetime(2026, 4, 4, tzinfo=UTC).date()
    )

    assert compressed == 0
    assert deleted == 2
    assert sorted(path.name for path in event_dir.glob("*.jsonl")) == [
        "2126-01-01.jsonl"
    ]
    assert "ordinary-after" not in (event_dir / "2126-01-01.jsonl").read_text()


def test_ordinary_event_append_does_not_read_retained_payloads(monkeypatch, tmp_path):
    """The hot append path must not scale with retained event volume."""
    agent_event = importlib.import_module("loopzero.kernel.events")
    event_dir = tmp_path / ".audit" / "agent-events"
    event_dir.mkdir(parents=True)
    (event_dir / "2026-09-07.jsonl").write_text('{"kind":"tool"}\n')
    # Cold initialization may inspect retained rows; ordinary appends must not.
    monkeypatch.setattr(agent_event, "repo_root", lambda: tmp_path)
    assert agent_event.write_event({"kind": "tool", "category": "initialize"})
    original_read = Path.read_text

    def forbid_history_read(path, *args, **kwargs):
        if path.parent == event_dir and path.suffix == ".jsonl":
            raise AssertionError("ordinary append read retained event payloads")
        return original_read(path, *args, **kwargs)

    monkeypatch.setattr(agent_event, "repo_root", lambda: tmp_path)
    monkeypatch.setattr(Path, "read_text", forbid_history_read)
    assert agent_event.write_event({"kind": "tool", "category": "regression"})
    assert any(
        '"regression"' in original_read(path) for path in event_dir.glob("*.jsonl")
    )


def test_interrupted_event_append_does_not_reuse_reserved_order(monkeypatch, tmp_path):
    """A failed payload append may leave a gap, never an ambiguous sequence."""
    agent_event = importlib.import_module("loopzero.kernel.events")
    event_dir = tmp_path / ".audit" / "agent-events"
    original_open = Path.open

    def interrupt_payload(path, mode="r", *args, **kwargs):
        if path.parent == event_dir and path.suffix == ".jsonl" and "a" in mode:
            raise OSError("interrupted payload append")
        return original_open(path, mode, *args, **kwargs)

    monkeypatch.setattr(agent_event, "repo_root", lambda: tmp_path)
    monkeypatch.setattr(Path, "open", interrupt_payload)
    assert not agent_event.write_event({"marker": "interrupted"})

    monkeypatch.setattr(Path, "open", original_open)
    assert agent_event.write_event({"marker": "retried"})
    log_path = next(event_dir.glob("*.jsonl"))
    event = json.loads(log_path.read_text())
    assert event["marker"] == "retried"
    assert event["append_sequence"] == 2


def test_partial_mirror_write_does_not_brick_later_events(monkeypatch, tmp_path):
    agent_event = importlib.import_module("loopzero.kernel.events")
    phases = importlib.import_module("loopzero.kernel.run_log")
    monkeypatch.setattr(agent_event, "repo_root", lambda: tmp_path)
    for marker in range(9):
        assert agent_event.write_event({"marker": marker})
    original_dump = json.dump
    calls = 0

    def torn_dump(value, stream, *args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            stream.write(json.dumps(value)[:-1])
            stream.flush()
            raise OSError("simulated ENOSPC during mirror write")
        return original_dump(value, stream, *args, **kwargs)

    with monkeypatch.context() as failure:
        failure.setattr(json, "dump", torn_dump)
        assert not agent_event.write_event({"marker": "interrupted"})
    assert calls == 2
    assert agent_event.write_event({"marker": "after"})
    rows = phases.load_phase_events(tmp_path)
    assert [row["marker"] for row in rows] == [*range(9), "after"]
    assert rows[-1]["append_sequence"] > rows[-2]["append_sequence"] + 1


def test_order_reservation_syncs_directory_before_event_append(monkeypatch, tmp_path):
    """A persisted event must not outlive its counter rename after a crash."""
    agent_event = importlib.import_module("loopzero.kernel.events")
    original_sync, original_open = os.fsync, Path.open
    directory_synced = False

    def sync(descriptor):
        nonlocal directory_synced
        original_sync(descriptor)
        directory_synced |= stat.S_ISDIR(os.fstat(descriptor).st_mode)

    def open_path(path, mode="r", *args, **kwargs):
        if path.suffix == ".jsonl" and "a" in mode:
            assert directory_synced, "counter rename was not durable before append"
        return original_open(path, mode, *args, **kwargs)

    monkeypatch.setattr(agent_event, "repo_root", lambda: tmp_path)
    monkeypatch.setattr(os, "fsync", sync)
    monkeypatch.setattr(Path, "open", open_path)
    assert agent_event.write_event({"marker": "durable-order"})


@pytest.mark.parametrize(
    "damage", ["missing", "stale", "mirror-missing", "both-missing"]
)
def test_counter_recovery_preserves_retained_append_order(
    monkeypatch, tmp_path, damage
):
    agent_event = importlib.import_module("loopzero.kernel.events")
    phases = importlib.import_module("loopzero.kernel.run_log")
    monkeypatch.setattr(agent_event, "repo_root", lambda: tmp_path)
    assert agent_event.write_event({"marker": 1})
    directory = tmp_path / ".audit" / "agent-events"
    counter = directory / ".append-order.json"
    old_counter = counter.read_bytes()
    assert agent_event.write_event({"marker": 2})
    if damage in {"missing", "both-missing"}:
        counter.unlink()
    if damage == "stale":
        counter.write_bytes(old_counter)
    if damage in {"mirror-missing", "both-missing"}:
        (directory / ".append-order-mirror.json").unlink()
    assert agent_event.write_event({"marker": 3})
    assert [row["marker"] for row in phases.load_phase_events(tmp_path)] == [1, 2, 3]
    # A second loss must advance beyond every retained generation as well.
    counter.unlink()
    assert agent_event.write_event({"marker": 4})
    rows = phases.load_phase_events(tmp_path)
    assert [row["marker"] for row in rows] == [1, 2, 3, 4]
    assert rows[-1]["append_generation"] > rows[-2]["append_generation"]


@pytest.mark.parametrize("link_kind", ["symlink", "hardlink"])
def test_event_lock_links_cannot_overwrite_another_file(
    monkeypatch, tmp_path, link_kind
):
    agent_event = importlib.import_module("loopzero.kernel.events")
    monkeypatch.setattr(agent_event, "repo_root", lambda: tmp_path)
    directory = tmp_path / ".audit" / "agent-events"
    directory.mkdir(parents=True)
    victim = tmp_path / "unrelated-state"
    victim.write_text("preserve this state")
    lock = directory / ".write.lock"
    if link_kind == "symlink":
        lock.symlink_to(victim)
    else:
        lock.hardlink_to(victim)
    assert not agent_event.write_event({"marker": "refused"})
    assert victim.read_text() == "preserve this state"


@pytest.mark.parametrize("stem", [".append-order", ".append-order-mirror"])
@pytest.mark.parametrize("link_kind", ["symlink", "hardlink"])
@pytest.mark.parametrize("suffix", [".tmp", ".json"])
def test_order_state_links_cannot_redirect_access(
    monkeypatch, tmp_path, stem, link_kind, suffix
):
    agent_event = importlib.import_module("loopzero.kernel.events")
    monkeypatch.setattr(agent_event, "repo_root", lambda: tmp_path)
    directory = tmp_path / ".audit" / "agent-events"
    directory.mkdir(parents=True)
    victim = tmp_path / "unrelated-state"
    original = '{"generation": 1, "last_sequence": 0}'
    victim.write_text(original)
    planted = directory / (stem + suffix)
    if link_kind == "symlink":
        planted.symlink_to(victim)
    else:
        planted.hardlink_to(victim)
    result = agent_event.write_event({"marker": "safe"})
    assert victim.read_text() == original
    assert result is (suffix == ".tmp")
    if result:
        for name in (".append-order.json", ".append-order-mirror.json"):
            assert stat.S_IMODE((directory / name).stat().st_mode) == 0o600


@pytest.mark.skip(reason="(c) subprocess import-path guard is owned by kernel event tests")
@pytest.mark.parametrize("name", [".append-order.json", ".append-order-mirror.json"])
def test_order_state_fifo_is_rejected_without_blocking(tmp_path, name):
    directory = tmp_path / ".audit" / "agent-events"
    directory.mkdir(parents=True)
    os.mkfifo(directory / name)
    subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys; from pathlib import Path; sys.path.insert(0, sys.argv[1]); "
            "import agent_event; agent_event.repo_root = lambda: Path(sys.argv[2]); "
            "assert agent_event.write_event({}) is False",
            str(Path(__file__).resolve().parents[4] / "scripts" / "util"),
            str(tmp_path),
        ],
        check=True,
        capture_output=True,
        timeout=3,
    )


def test_late_projection_revalidates_phase_before_appending(monkeypatch, tmp_path):
    """A decision projected after the run left ci-wait is refused, not appended."""
    phases = importlib.import_module("loopzero.kernel.run_log")
    agent_event = importlib.import_module("loopzero.kernel.events")
    writes = []
    monkeypatch.setattr(phases, "load_phase_events", lambda _: [])
    monkeypatch.setattr(phases, "phase_state", lambda *_, **__: ("closeout", "ci-wait"))
    monkeypatch.setattr(agent_event, "common_fields", lambda: {})
    monkeypatch.setattr(
        agent_event, "write_unique_event", lambda event, **_: writes.append(event)
    )
    with pytest.raises(reentry.StaleReentryProjection, match="left ci-wait"):
        reentry.append_reentry_phase(
            root=tmp_path,
            skill="work-issue",
            run_id=RUN_ID,
            record={"reason": "late repair"},
        )
    assert writes == []


def test_projection_write_failure_is_not_success(monkeypatch, tmp_path):
    phases = importlib.import_module("loopzero.kernel.run_log")
    agent_event = importlib.import_module("loopzero.kernel.events")
    monkeypatch.setattr(phases, "load_phase_events", lambda _: [])
    monkeypatch.setattr(phases, "phase_state", lambda *_, **__: ("ci-wait", "review"))
    monkeypatch.setattr(agent_event, "common_fields", lambda: {})
    monkeypatch.setattr(agent_event, "write_unique_event", lambda *_, **__: False)
    with pytest.raises(ValueError, match="durably"):
        reentry.append_reentry_phase(
            root=tmp_path,
            skill="work-issue",
            run_id=RUN_ID,
            record={"reason": "repair"},
        )


@pytest.mark.skip(reason="(a) delivery-control front controller stays with the consumer")
def test_cli_reentry_reads_actual_phase_and_appends_decision_before_projection(
    monkeypatch, tmp_path
):
    from argparse import Namespace
    from contextlib import nullcontext

    dispatch = importlib.import_module("agent_dispatch")
    phases = importlib.import_module("loopzero.kernel.run_log")
    rows = []
    writes = []
    evidence = tmp_path / "reason.json"
    evidence.write_text('{"reason": "gate demanded rebase", "phase": "closeout"}')
    monkeypatch.setattr(dispatch, "primary_repo_root", lambda _: tmp_path)
    monkeypatch.setattr(
        dispatch,
        "load_skill_run_entries",
        lambda _: [{"run_id": RUN_ID, "skill": "work-issue"}],
    )
    monkeypatch.setattr(dispatch, "active_outer_run_id", lambda _: RUN_ID)
    monkeypatch.setattr(dispatch, "load_authority_records", lambda *_: rows)
    monkeypatch.setattr(
        dispatch, "delivery_controller_records", lambda records: records
    )
    monkeypatch.setattr(dispatch, "authority_ledger_lock", lambda _: nullcontext())
    monkeypatch.setattr(dispatch, "source_identity", lambda _: {"head": "a" * 40})
    monkeypatch.setattr(dispatch, "create_coordinator_authority", lambda: object())
    monkeypatch.setattr(phases, "load_phase_events", lambda _: [])
    monkeypatch.setattr(phases, "phase_state", lambda *_, **__: ("ci-wait", "review"))

    def append(_repo, record, **kwargs):
        writes.append("decision")
        rows.append(record)

    monkeypatch.setattr(dispatch, "append_authoritative_record", append)
    monkeypatch.setattr(
        reentry, "append_reentry_phase", lambda **_: writes.append("phase")
    )
    args = Namespace(
        worktree=tmp_path,
        run_id=RUN_ID,
        control_action="review-reentry",
        evidence_json=evidence,
        verifier_task_id=None,
        pr=None,
    )
    assert dispatch.cmd_delivery_control(args) == 0
    assert writes == ["decision", "phase"]
    assert rows[0]["invalidates_publication"] is True
    # Crash recovery reuses the one durable decision and repairs only its projection.
    assert dispatch.cmd_delivery_control(args) == 0
    assert writes == ["decision", "phase", "phase"]
    monkeypatch.setattr(
        phases,
        "load_phase_events",
        lambda _: [{"run_id": RUN_ID, "status": "review-reentry"}],
    )
    with pytest.raises(dispatch.DispatchError, match="once"):
        dispatch.cmd_delivery_control(args)


@pytest.fixture
def isolated_recovery_authority(monkeypatch, tmp_path, isolated_ptrace_scope_path):
    """Real signing and trusted-key lookup scoped to this test process."""
    authority_store = importlib.import_module("loopzero.kernel.authority_store")
    globals_ = authority_store.create_coordinator_authority.__globals__[
        "CoordinatorAuthority"
    ].from_local_state.__func__.__globals__
    monkeypatch.setitem(globals_, "PTRACE_SCOPE_PATH", isolated_ptrace_scope_path)
    monkeypatch.setitem(
        globals_, "_coordinator_state_directory", lambda: tmp_path / "authority"
    )
    monkeypatch.setitem(
        globals_, "GUARDIAN_COORDINATOR_PUBLIC_KEY_PATH", tmp_path / "public.der"
    )
    return authority_store.create_coordinator_authority()


@pytest.mark.parametrize("producer_after", [False, True])
@pytest.mark.parametrize("proof", ["valid", "unsigned", "unmatched"])
@pytest.mark.parametrize("clock", ["forward", "backward", "equal", "missing"])
def test_reentry_recovery_freshness_binds_original_authenticated_producer(
    tmp_path, isolated_recovery_authority, producer_after, proof, clock
):
    """A signed recovery accepts old work; it does not create new review work."""
    from loopzero.delivery.reentry import require_review_after_reentry
    from loopzero.review.authority import accepted_review_terminals

    policy = importlib.import_module("loopzero.kernel.policy")
    authority_projection = importlib.import_module("loopzero.kernel.authority_projection")

    def current_dispatch_record(record):
        return {
            "schema_version": policy.TELEMETRY_SCHEMA_VERSION,
            "policy_version": policy.DISPATCH_POLICY_VERSION,
            **record,
        }

    times = {
        "forward": ("2026-09-07T10:00:00Z", "2026-09-07T11:00:00Z"),
        "backward": ("2026-09-07T12:00:00Z", "2026-09-07T09:00:00Z"),
        "equal": ("2026-09-07T10:00:00Z",) * 2,
        "missing": (None, None),
    }[clock]
    deposit = current_dispatch_record(
        {
            "type": "attempt-terminal",
            "task_id": "recovered-review",
            "work_unit_id": "recovered-review",
            "attempt_index": 0,
            "run_id": "sr_" + "1" * 32,
            "worktree": str(tmp_path),
            "status": "completed",
            "read_only": True,
            "work_kind": "review",
            "source_identity": {"head": "a" * 40},
            "result_artifact": "original.json",
            "result_sha256": "a" * 64,
            "worker_identity": "claude:opus",
            "effective_alias": "opus",
            "ts": times[0],
        }
    )
    decision = current_dispatch_record(
        {
            "type": "delivery-control",
            "action": "review-reentry",
            "run_id": deposit["run_id"],
            "ts": times[1],
        }
    )
    prefix = [decision, deposit] if producer_after else [deposit, decision]
    coordinator = isolated_recovery_authority
    cutover = coordinator.seal(
        current_dispatch_record(
            {
                "type": "coordinator-authority-cutover",
                "status": "active",
                "ledger_prefix": authority_projection.coordinator_ledger_prefix(prefix),
            }
        ),
        authority_kind="coordinator",
    )
    recovery = {
        **deposit,
        "type": "attempt-recovery",
        "ts": "2099-01-01T00:00:00Z",
        "recovered_terminal_status": "completed",
        "recovered_result_artifact": deposit["result_artifact"],
        "recovered_result_sha256": deposit["result_sha256"],
        "recovery_classification": "acceptance-receipt-only",
        "recovery_verification": {
            "type": "review-recovery-verification",
            "task_id": deposit["task_id"],
            "source_identity": deposit["source_identity"],
            "result_artifact": deposit["result_artifact"],
            "result_sha256": deposit["result_sha256"],
            "semantic_verdict": "pass",
            "blocker_class": "acceptance-receipt-only",
            "unresolved_findings": 0,
            "notes": "independent verification",
            "verifier_identity": "codex:gpt-6-astra",
            "verifier_alias": "astra",
        },
    }
    if proof == "unmatched":
        recovery["source_identity"] = {"head": "b" * 40}
    if proof != "unsigned":
        recovery = coordinator.seal(recovery, authority_kind="coordinator")
    rows = [*prefix, cutover, recovery]
    assert (accepted_review_terminals(rows).get("recovered-review") is recovery) == (
        proof == "valid"
    )
    if producer_after and proof == "valid":
        require_review_after_reentry(
            rows, run_id=deposit["run_id"], review_task_ids=["recovered-review"]
        )
    else:
        with pytest.raises(ValueError, match="after the reentry"):
            require_review_after_reentry(
                rows, run_id=deposit["run_id"], review_task_ids=["recovered-review"]
            )
    # The ordinary authenticated producer retains its original ledger position.
    if producer_after:
        require_review_after_reentry(
            [*prefix, cutover],
            run_id=deposit["run_id"],
            review_task_ids=["recovered-review"],
        )
