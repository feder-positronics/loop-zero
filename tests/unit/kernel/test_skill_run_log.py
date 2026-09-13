
from .package_environment import package_environment
import argparse
import importlib.util
import json
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import ModuleType
from uuid import UUID

import pytest


def load_agent_event_module() -> ModuleType:
    return importlib.import_module('loopzero.kernel.events')


def load_module() -> ModuleType:
    return importlib.import_module('loopzero.kernel.run_log')


module = load_module()


def _install_closeout_lock(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsule_dir: Path
) -> Path:
    job_root = tmp_path / "account-private-jobs"
    lease_root = job_root / ".leases"
    lease_root.mkdir(parents=True)
    lock = lease_root / "delivery-closeout.lock"
    capsule_stat = capsule_dir.stat()
    lock.write_text(f"{capsule_stat.st_dev}:{capsule_stat.st_ino}\n", encoding="ascii")
    lock.chmod(0o600)
    monkeypatch.setattr(
        module,
        "canonical_job_root",
        lambda _root, *, configured=None: job_root,
    )
    return lock


def test_canonical_closeout_capsule_translates_job_store_failure_before_open(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    common_dir = tmp_path / ".git"
    common_dir.mkdir()
    capsule_dir = tmp_path / ".audit" / "delivery-closeout"
    capsule_dir.mkdir(parents=True)
    capsule = capsule_dir / "capsule.json"
    capsule.write_text("{}\n", encoding="utf-8")
    monkeypatch.setattr(
        module.subprocess,
        "run",
        lambda *_args, **_kwargs: module.subprocess.CompletedProcess(
            args=[], returncode=0, stdout=str(common_dir), stderr=""
        ),
    )
    monkeypatch.setattr(
        module,
        "canonical_job_root",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            module.JobStoreError("unavailable")
        ),
    )
    monkeypatch.setattr(
        module.os,
        "open",
        lambda *_args, **_kwargs: pytest.fail("opened before validating job root"),
    )

    with pytest.raises(ValueError, match="directory identity is unavailable"):
        module.load_canonical_closeout_capsule(tmp_path, capsule)


def test_derive_duration_s_uses_earliest_event_for_exact_run(tmp_path: Path) -> None:
    audit_dir = tmp_path / ".audit" / "agent-events"
    audit_dir.mkdir(parents=True)
    log_path = audit_dir / "2026-05-14.jsonl"
    log_path.write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "ts": "2026-05-14T10:00:00Z",
                        "session_id": "thread-1",
                        "skill": "work-issue",
                        "run_id": "sr_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
                        "kind": "phase",
                    }
                ),
                json.dumps(
                    {
                        "ts": "2026-05-14T10:03:00Z",
                        "session_id": "thread-1",
                        "skill": "work-issue",
                        "run_id": "sr_bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
                        "kind": "phase",
                    }
                ),
                json.dumps(
                    {
                        "ts": "2026-05-14T09:50:00Z",
                        "session_id": "thread-2",
                        "skill": "work-issue",
                        "run_id": "sr_bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
                        "kind": "phase",
                    }
                ),
            ]
        ),
        encoding="utf-8",
    )

    duration = module.derive_duration_s(
        tmp_path,
        "work-issue",
        "thread-1",
        "sr_bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
        datetime(2026, 5, 14, 10, 5, 0, tzinfo=UTC),
    )

    assert duration == 120


def test_derive_duration_s_omits_duration_without_an_exact_run(tmp_path: Path) -> None:
    audit_dir = tmp_path / ".audit" / "agent-events"
    audit_dir.mkdir(parents=True)
    (audit_dir / "2026-05-14.jsonl").write_text(
        json.dumps(
            {
                "ts": "2026-05-14T10:00:00Z",
                "session_id": "thread-1",
                "skill": "work-issue",
                "run_id": "sr_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
            }
        ),
        encoding="utf-8",
    )

    duration = module.derive_duration_s(
        tmp_path,
        "work-issue",
        "thread-1",
        "sr_bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
        datetime(2026, 5, 14, 10, 5, 0, tzinfo=UTC),
    )

    assert duration is None


def test_codex_turn_evidence_preserves_same_session_interruption(
    tmp_path: Path,
) -> None:
    rollout = tmp_path / "rollout-session-1.jsonl"
    rollout.write_text(
        "\n".join(
            json.dumps(row)
            for row in (
                {
                    "timestamp": "2026-08-26T21:00:00Z",
                    "type": "event_msg",
                    "payload": {"type": "task_started", "turn_id": "turn-1"},
                },
                {
                    "timestamp": "2026-08-26T21:19:24Z",
                    "type": "event_msg",
                    "payload": {"type": "token_count"},
                },
                {
                    "timestamp": "2026-08-27T06:14:55Z",
                    "type": "event_msg",
                    "payload": {"type": "thread_settings_applied"},
                },
                {
                    "timestamp": "2026-08-27T06:14:56Z",
                    "type": "event_msg",
                    "payload": {"type": "task_started", "turn_id": "turn-2"},
                },
            )
        ),
        encoding="utf-8",
    )
    # A same-session resume refreshes the file mtime. Structural timestamps,
    # not that mutable filesystem timestamp, must retain the historical gap.
    rollout.touch()

    evidence = module.codex_turn_evidence(
        rollout,
        run_started_at=datetime(2026, 8, 26, 20, 0, tzinfo=UTC),
        idle_s=2 * 3600,
        now=datetime(2026, 8, 27, 6, 20, tzinfo=UTC),
    )

    assert evidence["interruptions"] == [
        {
            "turn_id": "turn-1",
            "started_at": "2026-08-26T21:19:24Z",
            "resumed_at": "2026-08-27T06:14:56Z",
        }
    ]
    assert evidence["open_turn_id"] == "turn-2"
    assert evidence["first_turn_started_at"] == "2026-08-26T21:00:00Z"
    assert evidence["last_activity_at"] == "2026-08-27T06:14:56Z"


def test_codex_turn_evidence_selects_first_turn_after_resume_floor(
    tmp_path: Path,
) -> None:
    rollout = tmp_path / "rollout-session-1.jsonl"
    rollout.write_text(
        "\n".join(
            json.dumps(
                {
                    "timestamp": timestamp,
                    "type": "event_msg",
                    "payload": {"type": "task_started", "turn_id": turn_id},
                }
            )
            for timestamp, turn_id in (
                ("2026-08-26T20:00:00Z", "earlier-turn"),
                ("2026-08-27T06:14:56Z", "resume-turn"),
                ("2026-08-27T07:00:00Z", "later-turn"),
            )
        ),
        encoding="utf-8",
    )

    evidence = module.codex_turn_evidence(
        rollout,
        run_started_at=datetime(2026, 8, 26, 19, 0, tzinfo=UTC),
        first_turn_after=datetime(2026, 8, 26, 21, 19, 24, tzinfo=UTC),
        idle_s=2 * 3600,
        now=datetime(2026, 8, 27, 7, 1, tzinfo=UTC),
    )

    assert evidence["first_turn_started_at"] == "2026-08-27T06:14:56Z"


def test_codex_turn_evidence_streams_rollout_rows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rollout = tmp_path / "rollout-session-1.jsonl"
    rollout.write_text(
        json.dumps(
            {
                "timestamp": "2026-08-26T21:00:00Z",
                "type": "event_msg",
                "payload": {"type": "task_started", "turn_id": "turn-1"},
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        Path,
        "read_text",
        lambda *_args, **_kwargs: pytest.fail("rollout must be streamed"),
    )

    evidence = module.codex_turn_evidence(
        rollout,
        run_started_at=datetime(2026, 8, 26, 20, 0, tzinfo=UTC),
        idle_s=2 * 3600,
        now=datetime(2026, 8, 27, 21, 0, tzinfo=UTC),
    )

    assert evidence["open_turn_id"] == "turn-1"


def test_session_alive_treats_invalid_rollout_as_conservatively_alive(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    session_id = "019fe000-0000-7000-8000-000000000abc"
    rollout = tmp_path / "rollout-session-1.jsonl"
    rollout.write_text("{broken\n", encoding="utf-8")
    monkeypatch.setattr(module, "codex_rollout_path", lambda _session_id: rollout)

    assert module.session_alive(session_id, within_s=60) is True


def test_codex_rollout_path_rejects_glob_steering_without_lookup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    monkeypatch.setattr(
        Path,
        "glob",
        lambda *_args, **_kwargs: pytest.fail("invalid identity must not reach glob"),
    )

    assert module.codex_rollout_path("*crafted*") is None


def test_derive_codex_tokens_rejects_glob_steering_without_lookup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    monkeypatch.setattr(
        Path,
        "glob",
        lambda *_args, **_kwargs: pytest.fail("invalid identity must not reach glob"),
    )

    assert module.derive_codex_session_tokens("*crafted*") is None


def test_codex_turn_evidence_fails_closed_on_malformed_active_history(
    tmp_path: Path,
) -> None:
    rollout = tmp_path / "rollout-session-1.jsonl"
    rollout.write_text(
        json.dumps(
            {
                "timestamp": "2026-08-26T21:00:00Z",
                "type": "event_msg",
                "payload": {"type": "task_started", "turn_id": "turn-1"},
            }
        )
        + "\n{broken\n",
        encoding="utf-8",
    )

    with pytest.raises(module.RolloutEvidenceError, match="malformed"):
        module.codex_turn_evidence(
            rollout,
            run_started_at=datetime(2026, 8, 26, 20, 0, tzinfo=UTC),
            idle_s=2 * 3600,
            now=datetime(2026, 8, 27, 6, 20, tzinfo=UTC),
        )


def test_build_entry_auto_derives_duration(monkeypatch, tmp_path: Path) -> None:
    args = argparse.Namespace(
        skill="work-issue",
        outcome="merged",
        duration_s=None,
        review_passes=2,
        issue=12,
        pr=34,
        footgun_bypass=False,
        notes="done",
        run_id="sr_0123456789abcdef0123456789abcdef",
        git_branch=None,
    )
    monkeypatch.setattr(module, "repo_root", lambda: tmp_path)
    monkeypatch.setattr(
        module,
        "common_fields",
        lambda: {
            "ts": "2026-05-14T10:05:00Z",
            "session_id": "thread-1",
            "session_source": "codex_thread",
            "harness": "codex",
            "git_branch": "feature/x",
        },
    )
    monkeypatch.setattr(
        module,
        "derive_duration_s",
        lambda root, skill, session_id, run_id, end_ts: 300,
    )

    entry = module.build_entry(args)

    assert entry["duration_s"] == 300
    assert entry["session_id"] == "thread-1"
    assert entry["session_source"] == "codex_thread"
    assert entry["harness"] == "codex"
    assert entry["git_branch"] == "feature/x"
    assert entry["run_id"] == "sr_0123456789abcdef0123456789abcdef"
    assert entry["reentry_contract_version"] == 1


def _entry(run_id: str, outcome: str, **extra: object) -> dict[str, object]:
    return {
        "ts": "2026-07-13T10:00:00Z",
        "skill": "work-issue",
        "run_id": run_id,
        "outcome": outcome,
        "issue": 2639,
        "git_branch": "feat/2639-skill-run-lifecycle",
        **extra,
    }


def test_active_run_uses_the_exact_branch_identity_and_start() -> None:
    active = "sr_0123456789abcdef0123456789abcdef"
    completed = "sr_fedcba9876543210fedcba9876543210"
    entries = [
        _entry(completed, "in_progress", ts="2026-08-20T08:00:00Z"),
        _entry(completed, "merged", ts="2026-08-20T09:00:00Z"),
        _entry(active, "in_progress", ts="2026-08-20T10:00:00Z"),
        _entry(
            "sr_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
            "in_progress",
            ts="2026-08-20T07:00:00Z",
            git_branch="feat/another-task",
        ),
    ]

    assert module.active_run(entries, git_branch="feat/2639-skill-run-lifecycle") == (
        active,
        "2026-08-20T10:00:00Z",
    )


def test_active_run_returns_none_without_an_owner() -> None:
    assert module.active_run([], git_branch="feat/2639-skill-run-lifecycle") is None


def test_active_run_preserves_legacy_owner_without_a_start_timestamp() -> None:
    run_id = "sr_0123456789abcdef0123456789abcdef"
    entry = _entry(run_id, "in_progress")
    entry.pop("ts")

    assert module.active_run([entry], git_branch=entry["git_branch"]) == (
        run_id,
        None,
    )


def test_active_run_fails_closed_with_multiple_owners() -> None:
    entries = [
        _entry("sr_0123456789abcdef0123456789abcdef", "in_progress"),
        _entry("sr_fedcba9876543210fedcba9876543210", "in_progress"),
    ]

    with pytest.raises(ValueError, match="multiple active logical runs"):
        module.active_run(entries, git_branch="feat/2639-skill-run-lifecycle")


def test_start_reuses_exact_active_run_identity() -> None:
    run_id = "sr_0123456789abcdef0123456789abcdef"
    entries = [_entry(run_id, "in_progress")]

    resolved = module.resolve_start_run_id(
        entries,
        skill="work-issue",
        issue=2639,
        git_branch="feat/2639-skill-run-lifecycle",
    )

    assert resolved == run_id


@pytest.mark.parametrize(
    ("skill", "issue"),
    [("execute-blueprint", 2639), ("work-issue", 2640)],
)
def test_start_rejects_a_second_owner_for_the_same_branch(
    skill: str, issue: int
) -> None:
    run_id = "sr_0123456789abcdef0123456789abcdef"
    entries = [_entry(run_id, "in_progress")]

    with pytest.raises(ValueError, match="is already owned by active run"):
        module.resolve_start_run_id(
            entries,
            skill=skill,
            issue=issue,
            git_branch="feat/2639-skill-run-lifecycle",
        )


def test_start_generates_opaque_run_id_without_active_match() -> None:
    run_id = module.resolve_start_run_id(
        [],
        skill="work-issue",
        issue=2639,
        git_branch="feat/2639-skill-run-lifecycle",
    )

    assert run_id.startswith("sr_")
    assert len(run_id) == 35
    UUID(hex=run_id.removeprefix("sr_"))


def test_repeated_same_state_is_idempotent() -> None:
    run_id = "sr_0123456789abcdef0123456789abcdef"
    existing = [_entry(run_id, "merged", pr=2700)]

    assert (
        module.validate_transition(existing, _entry(run_id, "merged", pr=2700)) is False
    )


def test_merged_outcome_requires_verified_merge_flag(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "skill_run_log.py",
            "--skill",
            "work-issue",
            "--run-id",
            "sr_0123456789abcdef0123456789abcdef",
            "--outcome",
            "merged",
        ],
    )

    with pytest.raises(SystemExit, match="2"):
        module.main()


def test_verified_merged_replay_does_not_append_a_second_terminal_row(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    run_id = "sr_0123456789abcdef0123456789abcdef"
    audit_dir = tmp_path / ".audit" / "skill-runs"
    audit_dir.mkdir(parents=True)
    log_path = audit_dir / "2026-08-13.jsonl"
    log_path.write_text(json.dumps(_entry(run_id, "merged")) + "\n", encoding="utf-8")
    monkeypatch.setattr(module, "repo_root", lambda: tmp_path)
    monkeypatch.setattr(
        module,
        "complete_closeout_phase",
        lambda **_: pytest.fail("terminal replay must not mutate phase history"),
    )
    monkeypatch.setattr(
        module,
        "common_fields",
        lambda: {
            "ts": "2026-08-13T10:00:00Z",
            "session_id": "closeout-replay",
            "session_source": "codex_thread",
            "harness": "codex",
            "git_branch": "feat/2639-skill-run-lifecycle",
        },
    )
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "skill_run_log.py",
            "--skill",
            "work-issue",
            "--run-id",
            run_id,
            "--outcome",
            "merged",
            "--verified-merged",
        ],
    )

    assert module.main() == 0
    assert log_path.read_text(encoding="utf-8").splitlines() == [
        json.dumps(_entry(run_id, "merged"))
    ]


def test_check_owner_is_read_only_and_rejects_a_different_skill(tmp_path: Path) -> None:
    run_id = "sr_0123456789abcdef0123456789abcdef"
    repo = _repo_with_runs(
        tmp_path,
        [_row(run_id, "work-issue", "in_progress", "2026-08-13T10:00:00Z")],
    )
    log_path = repo / ".audit" / "skill-runs" / "2026-08-01.jsonl"
    before = log_path.read_bytes()

    accepted = _run_cli(
        repo,
        "--skill",
        "work-issue",
        "--run-id",
        run_id,
        "--check-owner",
    )
    rejected = _run_cli(
        repo,
        "--skill",
        "execute-blueprint",
        "--run-id",
        run_id,
        "--check-owner",
    )
    unknown = "sr_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
    missing_rejected = _run_cli(
        repo,
        "--skill",
        "work-issue",
        "--run-id",
        unknown,
        "--check-owner",
    )
    historical_recovery = _run_cli(
        repo,
        "--skill",
        "work-issue",
        "--run-id",
        unknown,
        "--check-owner",
        "--allow-missing-owner",
    )

    assert accepted.returncode == 0, accepted.stderr
    assert "Owned work-issue" in accepted.stdout
    assert rejected.returncode == 2
    assert "run_id is already owned by work-issue" in rejected.stderr
    assert missing_rejected.returncode == 2
    assert "unknown run_id" in missing_rejected.stderr
    assert historical_recovery.returncode == 0, historical_recovery.stderr
    assert "Unowned historical run" in historical_recovery.stdout
    assert log_path.read_bytes() == before


@pytest.mark.parametrize("history_present", [True, False])
def test_verified_merge_appends_terminal_row_when_phase_telemetry_raises(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, history_present: bool
) -> None:
    run_id = "sr_0123456789abcdef0123456789abcdef"
    audit_dir = tmp_path / ".audit" / "skill-runs"
    audit_dir.mkdir(parents=True)
    log_path = audit_dir / f"{datetime.now(UTC).strftime('%Y-%m-%d')}.jsonl"
    log_path.write_text(
        json.dumps(_entry(run_id, "in_progress")) + "\n" if history_present else "",
        encoding="utf-8",
    )
    monkeypatch.setattr(module, "repo_root", lambda: tmp_path)

    def fail_phase_completion(**kwargs) -> bool:
        raise ValueError("contradictory phase history")

    monkeypatch.setattr(module, "complete_closeout_phase", fail_phase_completion)
    monkeypatch.setattr(
        module,
        "common_fields",
        lambda: {
            "ts": f"{datetime.now(UTC).strftime('%Y-%m-%d')}T10:00:00Z",
            "session_id": "closeout-terminal",
            "session_source": "codex_thread",
            "harness": "codex",
            "git_branch": "feat/2639-skill-run-lifecycle",
        },
    )
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "skill_run_log.py",
            "--skill",
            "work-issue",
            "--run-id",
            run_id,
            "--outcome",
            "merged",
            "--verified-merged",
        ],
    )

    assert module.main() == 0
    rows = module.load_entries(audit_dir)
    assert [row["outcome"] for row in rows] == (
        ["in_progress", "merged"] if history_present else ["merged"]
    )
    assert rows[-1]["delivery_contract"] == "intelflo-v1"


def test_in_progress_can_transition_to_terminal_from_another_session() -> None:
    run_id = "sr_0123456789abcdef0123456789abcdef"
    existing = [_entry(run_id, "in_progress", session_id="first")]

    should_append = module.validate_transition(
        existing,
        _entry(run_id, "merged", pr=2700, session_id="closeout"),
    )

    assert should_append is True


def test_stale_abandoned_run_can_recover_only_from_matching_frozen_closeout() -> None:
    run_id = "sr_0123456789abcdef0123456789abcdef"
    prior = [
        _entry(
            run_id,
            "in_progress",
            pr=3702,
            issue=3691,
            git_branch="feat/test",
        ),
        _entry(
            run_id,
            "abandoned",
            issue=3691,
            git_branch="feat/test",
            notes="hygiene sweep 2026-08-28: session dead, no branch events",
        ),
    ]
    merged = _entry(run_id, "merged", pr=3702, issue=3691, git_branch="feat/test")
    operation_id = "delivery-closeout:" + "a" * 64
    plan = {
        "schema_version": "delivery-settlement-plan-v1",
        "run_id": run_id,
        "pr": 3702,
        "reviewed_head": "b" * 40,
        "merge_commit": "c" * 40,
        "operations": [
            {
                "operation_id": operation_id,
                "kind": "terminalize-run",
                "target": run_id,
                "details": {
                    "skill": "work-issue",
                    "pr": 3702,
                    "issue": 3691,
                    "branch": "feat/test",
                },
            }
        ],
    }
    plan["plan_digest"] = module._canonical_digest(plan)
    capsule = {
        "schema_version": "delivery-closeout-v2",
        "run_id": run_id,
        "pr": 3702,
        "reviewed_head": "b" * 40,
        "merge_commit": "c" * 40,
        "plan_digest": plan["plan_digest"],
        "plan": plan,
        "completed": [],
        "remaining": [operation_id],
        "receipts": {},
        "status": "in_progress",
    }
    capsule["capsule_digest"] = module._canonical_digest(capsule)

    assert (
        module.validate_stale_abandoned_merge_recovery(prior, merged, capsule) is True
    )
    assert (
        module.validate_transition(prior, merged, allow_stale_abandoned_merge=True)
        is True
    )

    takeover_id = "sr_fedcba9876543210fedcba9876543210"
    takeover_prior = [
        prior[0],
        {
            **prior[1],
            "notes": "rebased review repairs require fresh exact-head chain",
            "session_id": "recovery-session",
        },
        _entry(
            takeover_id,
            "in_progress",
            ts="2026-07-13T10:05:00Z",
            pr=None,
            issue=None,
            git_branch="feat/test",
            session_id="new-recovery-session",
        ),
        _entry(
            takeover_id,
            "merged",
            ts="2026-07-13T11:00:00Z",
            pr=4043,
            issue=None,
            git_branch="feat/test",
            session_id="recovery-session",
        ),
    ]
    assert (
        module.validate_stale_abandoned_merge_recovery(takeover_prior, merged, capsule)
        is True
    )
    with pytest.raises(ValueError, match="abandonment evidence"):
        module.validate_stale_abandoned_merge_recovery(
            takeover_prior[:-1], merged, capsule
        )

    candidate = takeover_prior[2]
    terminal = takeover_prior[3]
    rejected_histories = [
        [candidate, *takeover_prior[:2], terminal],
        [*takeover_prior[:2], {**candidate, "run_id": "legacy-run"}, terminal],
        [*takeover_prior[:2], {**candidate, "git_branch": "feat/other"}, terminal],
        [*takeover_prior[:2], {**candidate, "skill": "commit-autofix"}, terminal],
        [*takeover_prior[:3], {**terminal, "git_branch": "feat/other"}],
        [*takeover_prior[:3], {**terminal, "skill": "commit-autofix"}],
        [*takeover_prior[:3], {**terminal, "pr": None}],
    ]
    for rejected_history in rejected_histories:
        with pytest.raises(ValueError, match="abandonment evidence"):
            module.validate_stale_abandoned_merge_recovery(
                rejected_history, merged, capsule
            )

    mismatched_history = [{**prior[0], "pr": 3600}, prior[1]]
    with pytest.raises(ValueError, match="conflicts with run history"):
        module.validate_stale_abandoned_merge_recovery(
            mismatched_history, merged, capsule
        )


def test_stale_abandoned_recovery_rejects_mismatched_capsule() -> None:
    run_id = "sr_0123456789abcdef0123456789abcdef"
    prior = [
        _entry(run_id, "in_progress", issue=3691, git_branch="feat/test"),
        _entry(
            run_id,
            "abandoned",
            issue=3691,
            git_branch="feat/test",
            notes="hygiene sweep 2026-08-28: session dead, no branch events",
        ),
    ]
    merged = _entry(run_id, "merged", pr=3702, issue=3691, git_branch="feat/test")

    with pytest.raises(ValueError, match="frozen closeout capsule"):
        module.validate_stale_abandoned_merge_recovery(
            prior,
            merged,
            {
                "schema_version": "delivery-closeout-v2",
                "run_id": run_id,
                "pr": 9999,
            },
        )


def test_canonical_closeout_capsule_rejects_hard_link_alias(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    common_dir = tmp_path / ".git"
    common_dir.mkdir()
    capsule_dir = tmp_path / ".audit" / "delivery-closeout"
    capsule_dir.mkdir(parents=True)
    capsule = capsule_dir / "pr-3702-sr_0123456789abcdef0123456789abcdef.json"
    capsule.write_text("{}\n", encoding="utf-8")
    _install_closeout_lock(monkeypatch, tmp_path, capsule_dir)
    (tmp_path / "capsule-alias.json").hardlink_to(capsule)
    monkeypatch.setattr(
        module.subprocess,
        "run",
        lambda *_args, **_kwargs: module.subprocess.CompletedProcess(
            args=[], returncode=0, stdout=str(common_dir), stderr=""
        ),
    )

    with pytest.raises(ValueError, match="hard links"):
        module.load_canonical_closeout_capsule(tmp_path, capsule)


def test_canonical_closeout_capsule_rejects_symlinked_audit_ancestor(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    common_dir = tmp_path / ".git"
    common_dir.mkdir()
    outside_audit = tmp_path / "outside-audit"
    capsule_dir = outside_audit / "delivery-closeout"
    capsule_dir.mkdir(parents=True)
    capsule = capsule_dir / "capsule.json"
    (tmp_path / ".audit").symlink_to(outside_audit, target_is_directory=True)
    capsule.write_text("{}\n", encoding="utf-8")
    monkeypatch.setattr(
        module.subprocess,
        "run",
        lambda *_args, **_kwargs: module.subprocess.CompletedProcess(
            args=[], returncode=0, stdout=str(common_dir), stderr=""
        ),
    )

    with pytest.raises(ValueError, match="canonical primary artifact"):
        module.load_canonical_closeout_capsule(tmp_path, capsule)


def test_canonical_closeout_capsule_rejects_replaced_locked_directory(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    common_dir = tmp_path / ".git"
    common_dir.mkdir()
    capsule_dir = tmp_path / ".audit" / "delivery-closeout"
    capsule_dir.mkdir(parents=True)
    _install_closeout_lock(monkeypatch, tmp_path, capsule_dir)
    moved = tmp_path / ".audit" / "delivery-closeout-moved"
    capsule_dir.rename(moved)
    capsule_dir.mkdir()
    capsule = capsule_dir / "pr-3702-sr_0123456789abcdef0123456789abcdef.json"
    capsule.write_text("{}\n", encoding="utf-8")
    monkeypatch.setattr(
        module.subprocess,
        "run",
        lambda *_args, **_kwargs: module.subprocess.CompletedProcess(
            args=[], returncode=0, stdout=str(common_dir), stderr=""
        ),
    )

    with pytest.raises(ValueError, match="directory identity changed"):
        module.load_canonical_closeout_capsule(tmp_path, capsule)


def test_resolved_no_change_is_a_truthful_terminal_outcome() -> None:
    run_id = "sr_0123456789abcdef0123456789abcdef"

    assert (
        module.validate_transition(
            [_entry(run_id, "in_progress")],
            _entry(run_id, "resolved_no_change"),
        )
        is True
    )


def test_conflicting_terminal_transition_is_rejected() -> None:
    run_id = "sr_0123456789abcdef0123456789abcdef"

    with pytest.raises(ValueError, match="contradictory terminal"):
        module.validate_transition(
            [_entry(run_id, "merged")],
            _entry(run_id, "blocked"),
        )


def test_phase_transition_is_forward_only_and_idempotent(
    monkeypatch, tmp_path: Path
) -> None:
    run_id = "sr_0123456789abcdef0123456789abcdef"
    events = [
        {
            "kind": "phase",
            "run_id": run_id,
            "skill": "work-issue",
            "phase": "1",
            "status": "start",
        }
    ]
    emitted: list[tuple[str, str, str]] = []
    monkeypatch.setattr(module, "load_phase_events", lambda root: events)
    monkeypatch.setattr(
        module,
        "emit_phase_event",
        lambda *, skill, run_id, phase, status: emitted.append((phase, status, run_id)),
    )

    assert (
        module.transition_phase(
            root=tmp_path,
            skill="work-issue",
            run_id=run_id,
            target="local-validation",
        )
        is True
    )
    assert emitted == [
        ("implementation", "complete", run_id),
        ("local-validation", "start", run_id),
    ]

    events.extend(
        {
            "kind": "phase",
            "run_id": run_id,
            "skill": "work-issue",
            "phase": str(module.PHASES.index(phase) + 1),
            "status": status,
        }
        for phase, status, _ in emitted
    )
    assert (
        module.transition_phase(
            root=tmp_path,
            skill="work-issue",
            run_id=run_id,
            target="local-validation",
        )
        is False
    )
    with pytest.raises(ValueError, match="immediately next"):
        module.transition_phase(
            root=tmp_path,
            skill="work-issue",
            run_id=run_id,
            target="ci-wait",
        )
    with pytest.raises(ValueError, match="unknown phase"):
        module.transition_phase(
            root=tmp_path,
            skill="work-issue",
            run_id=run_id,
            target="invented-phase",
        )


def test_phase_state_exposes_public_active_and_completed_names() -> None:
    run_id = "sr_0123456789abcdef0123456789abcdef"
    events = [
        {
            "kind": "phase",
            "run_id": run_id,
            "skill": "work-issue",
            "phase": "1",
            "name": "implementation",
            "status": status,
        }
        for status in ("start", "complete")
    ]
    events.append(
        {
            "kind": "phase",
            "run_id": run_id,
            "skill": "work-issue",
            "phase": "2",
            "name": "local-validation",
            "status": "start",
        }
    )

    assert module.phase_state(events, skill="work-issue", run_id=run_id) == (
        "local-validation",
        "implementation",
    )


def test_phase_transition_starts_implementation_for_a_new_run(
    monkeypatch, tmp_path: Path
) -> None:
    run_id = "sr_0123456789abcdef0123456789abcdef"
    emitted: list[tuple[str, str]] = []
    monkeypatch.setattr(module, "load_phase_events", lambda root: [])
    monkeypatch.setattr(
        module,
        "emit_phase_event",
        lambda *, skill, run_id, phase, status: emitted.append((phase, status)),
    )

    assert (
        module.transition_phase(
            root=tmp_path,
            skill="work-issue",
            run_id=run_id,
            target="implementation",
        )
        is True
    )
    assert emitted == [("implementation", "start")]


def test_phase_transition_resumes_half_written_boundary(
    monkeypatch, tmp_path: Path
) -> None:
    run_id = "sr_0123456789abcdef0123456789abcdef"
    events = [
        {
            "kind": "phase",
            "run_id": run_id,
            "skill": "work-issue",
            "phase": "1",
            "status": status,
        }
        for status in ("start", "complete")
    ]
    emitted: list[tuple[str, str]] = []
    monkeypatch.setattr(module, "load_phase_events", lambda root: events)
    monkeypatch.setattr(
        module,
        "emit_phase_event",
        lambda *, skill, run_id, phase, status: emitted.append((phase, status)),
    )

    assert module.transition_phase(
        root=tmp_path,
        skill="work-issue",
        run_id=run_id,
        target="local-validation",
    )
    assert emitted == [("local-validation", "start")]


def test_completed_closeout_transition_is_an_idempotent_noop(
    monkeypatch, tmp_path: Path
) -> None:
    run_id = "sr_0123456789abcdef0123456789abcdef"
    events = [
        event
        for phase in range(1, 6)
        for event in (
            {
                "kind": "phase",
                "run_id": run_id,
                "skill": "work-issue",
                "phase": str(phase),
                "status": "start",
            },
            {
                "kind": "phase",
                "run_id": run_id,
                "skill": "work-issue",
                "phase": str(phase),
                "status": "complete",
            },
        )
    ]
    monkeypatch.setattr(module, "load_phase_events", lambda root: events)
    monkeypatch.setattr(
        module,
        "emit_phase_event",
        lambda **kwargs: pytest.fail("completed closeout must not emit another event"),
    )

    assert (
        module.transition_phase(
            root=tmp_path,
            skill="work-issue",
            run_id=run_id,
            target="closeout",
        )
        is False
    )


def test_emit_phase_event_fails_when_storage_is_not_durable(monkeypatch) -> None:
    monkeypatch.setattr(module, "cmd_phase", lambda args: 1)

    with pytest.raises(RuntimeError, match="not written durably"):
        module.emit_phase_event(
            skill="work-issue",
            run_id="sr_0123456789abcdef0123456789abcdef",
            phase="implementation",
            status="start",
        )


def test_only_verified_merged_outcome_completes_closeout(
    monkeypatch, tmp_path: Path
) -> None:
    run_id = "sr_0123456789abcdef0123456789abcdef"
    events = [
        event
        for phase in range(1, 5)
        for event in (
            {
                "kind": "phase",
                "run_id": run_id,
                "skill": "work-issue",
                "phase": str(phase),
                "status": "start",
            },
            {
                "kind": "phase",
                "run_id": run_id,
                "skill": "work-issue",
                "phase": str(phase),
                "status": "complete",
            },
        )
    ] + [
        {
            "kind": "phase",
            "run_id": run_id,
            "skill": "work-issue",
            "phase": "5",
            "status": "start",
        }
    ]
    emitted: list[tuple[str, str]] = []
    monkeypatch.setattr(module, "load_phase_events", lambda root: events)
    monkeypatch.setattr(
        module,
        "emit_phase_event",
        lambda *, skill, run_id, phase, status: emitted.append((phase, status)),
    )

    assert (
        module.complete_closeout_phase(
            root=tmp_path,
            skill="work-issue",
            run_id=run_id,
            outcome="blocked",
            verified_merged=False,
        )
        is False
    )
    assert emitted == []
    assert (
        module.complete_closeout_phase(
            root=tmp_path,
            skill="work-issue",
            run_id=run_id,
            outcome="merged",
            verified_merged=True,
        )
        is True
    )
    assert emitted == [("closeout", "complete")]


@pytest.mark.parametrize("early_phase_statuses", [("start",), ("start", "complete")])
def test_verified_merge_terminalizes_without_synthesizing_missing_phase_history(
    monkeypatch, tmp_path: Path, early_phase_statuses: tuple[str, ...]
) -> None:
    run_id = "sr_0123456789abcdef0123456789abcdef"
    events = [
        {
            "kind": "phase",
            "run_id": run_id,
            "skill": "work-issue",
            "phase": "1",
            "status": status,
        }
        for status in early_phase_statuses
    ]
    monkeypatch.setattr(module, "load_phase_events", lambda root: events)
    monkeypatch.setattr(
        module,
        "emit_phase_event",
        lambda **kwargs: pytest.fail("missing phase history must not be synthesized"),
    )

    assert (
        module.complete_closeout_phase(
            root=tmp_path,
            skill="work-issue",
            run_id=run_id,
            outcome="merged",
            verified_merged=True,
        )
        is False
    )


def test_verified_merge_terminalizes_despite_contradictory_phase_history(
    monkeypatch, tmp_path: Path
) -> None:
    run_id = "sr_0123456789abcdef0123456789abcdef"
    events = [
        {
            "kind": "phase",
            "run_id": run_id,
            "skill": "work-issue",
            "phase": "1",
            "status": "start",
        },
        {
            "kind": "phase",
            "run_id": run_id,
            "skill": "work-issue",
            "phase": "2",
            "status": "start",
        },
    ]
    monkeypatch.setattr(module, "load_phase_events", lambda root: events)

    assert (
        module.complete_closeout_phase(
            root=tmp_path,
            skill="work-issue",
            run_id=run_id,
            outcome="merged",
            verified_merged=True,
        )
        is False
    )


def test_verified_merge_terminalizes_when_phase_completion_write_fails(
    monkeypatch, tmp_path: Path
) -> None:
    run_id = "sr_0123456789abcdef0123456789abcdef"
    events = [
        {
            "kind": "phase",
            "run_id": run_id,
            "skill": "work-issue",
            "phase": str(index),
            "name": name,
            "status": status,
        }
        for index, name in enumerate(module.PHASES, start=1)
        for status in ("start", "complete")
        if name != "closeout" or status == "start"
    ]
    monkeypatch.setattr(module, "load_phase_events", lambda root: events)

    def fail_phase_write(**kwargs) -> None:
        raise RuntimeError("write failed")

    monkeypatch.setattr(module, "emit_phase_event", fail_phase_write)

    assert (
        module.complete_closeout_phase(
            root=tmp_path,
            skill="work-issue",
            run_id=run_id,
            outcome="merged",
            verified_merged=True,
        )
        is False
    )


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        (
            "Refs #2639\n\n<!-- skill-run-id: sr_0123456789abcdef0123456789abcdef -->",
            "sr_0123456789abcdef0123456789abcdef",
        ),
        ("Refs #2639", None),
        (
            "<!-- skill-run-id: sr_0123456789abcdef0123456789abcdef -->\n"
            "<!-- skill-run-id: sr_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa -->",
            None,
        ),
    ],
)
def test_extract_run_id_marker(body: str, expected: str | None) -> None:
    assert module.extract_run_id_marker(body) == expected


def test_build_entry_records_provider_reported_tokens(monkeypatch, tmp_path):
    args = argparse.Namespace(
        skill="work-issue",
        outcome="merged",
        duration_s=120,
        review_passes=None,
        issue=None,
        pr=None,
        footgun_bypass=False,
        notes=None,
        run_id="sr_0123456789abcdef0123456789abcdef",
        git_branch=None,
        tokens_in=1_200_000,
        tokens_out=45_000,
        tokens_cached=1_100_000,
    )
    monkeypatch.setattr(module, "repo_root", lambda: tmp_path)
    monkeypatch.setattr(
        module,
        "common_fields",
        lambda: {
            "ts": "2026-08-09T10:05:00Z",
            "session_id": "thread-1",
            "session_source": "codex_thread",
            "harness": "codex",
            "git_branch": "feature/x",
        },
    )

    entry = module.build_entry(args)

    assert entry["tokens_in"] == 1_200_000
    assert entry["tokens_out"] == 45_000
    assert entry["tokens_cached"] == 1_100_000
    assert entry["token_scope"] == "run"


def test_build_entry_omits_token_fields_when_unmetered(monkeypatch, tmp_path):
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "empty-codex-home"))
    args = argparse.Namespace(
        skill="work-issue",
        outcome="merged",
        duration_s=120,
        review_passes=None,
        issue=None,
        pr=None,
        footgun_bypass=False,
        notes=None,
        run_id="sr_0123456789abcdef0123456789abcdef",
        git_branch=None,
    )
    monkeypatch.setattr(module, "repo_root", lambda: tmp_path)
    monkeypatch.setattr(
        module,
        "common_fields",
        lambda: {
            "ts": "2026-08-09T10:05:00Z",
            "session_id": "thread-1",
            "session_source": "codex_thread",
            "harness": "codex",
            "git_branch": "feature/x",
        },
    )

    entry = module.build_entry(args)

    assert "tokens_in" not in entry
    assert "tokens_out" not in entry
    assert "tokens_cached" not in entry


def test_build_entry_derives_codex_tokens_from_rollout(monkeypatch, tmp_path):
    session_id = "019fe000-0000-7000-8000-000000000abc"
    rollout_dir = tmp_path / "codex-home" / "sessions" / "2026" / "08" / "09"
    rollout_dir.mkdir(parents=True)
    rollout = rollout_dir / f"rollout-2026-08-09T10-00-00-{session_id}.jsonl"
    lines = [
        json.dumps({"type": "session_meta", "payload": {"id": session_id}}),
        json.dumps(
            {
                "type": "event_msg",
                "payload": {
                    "type": "token_count",
                    "info": {
                        "total_token_usage": {
                            "input_tokens": 500,
                            "output_tokens": 50,
                            "cached_input_tokens": 400,
                        }
                    },
                },
            }
        ),
        json.dumps(
            {
                "type": "event_msg",
                "payload": {
                    "type": "token_count",
                    "info": {
                        "total_token_usage": {
                            "input_tokens": 1_200_000,
                            "output_tokens": 45_000,
                            "cached_input_tokens": 1_100_000,
                        }
                    },
                },
            }
        ),
    ]
    rollout.write_text("\n".join(lines) + "\n", encoding="utf-8")
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex-home"))
    monkeypatch.setattr(module, "repo_root", lambda: tmp_path)
    monkeypatch.setattr(
        module,
        "common_fields",
        lambda: {
            "ts": "2026-08-09T10:05:00Z",
            "session_id": session_id,
            "session_source": "codex_thread",
            "harness": "codex",
            "git_branch": "feature/x",
        },
    )
    args = argparse.Namespace(
        skill="work-issue",
        outcome="merged",
        duration_s=120,
        review_passes=None,
        issue=None,
        pr=None,
        footgun_bypass=False,
        notes=None,
        run_id="sr_0123456789abcdef0123456789abcdef",
        git_branch=None,
    )

    entry = module.build_entry(args)

    # Last cumulative token_count wins — provider-reported, never estimated.
    assert entry["session_tokens_in_cumulative"] == 1_200_000
    assert entry["session_tokens_out_cumulative"] == 45_000
    assert entry["session_tokens_cached_cumulative"] == 1_100_000
    assert "tokens_in" not in entry
    assert "tokens_out" not in entry
    assert "tokens_cached" not in entry
    assert "token_scope" not in entry


# --- session ceiling + stale reconcile (#3418 P3) ---------------------------


def _iso(dt) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _repo_with_runs(tmp_path, rows):
    import subprocess

    repo = tmp_path / "repo"
    (repo / ".audit" / "skill-runs").mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    log = repo / ".audit" / "skill-runs" / "2026-08-01.jsonl"
    log.write_text("\n".join(json.dumps(r) for r in rows) + "\n")
    return repo


def _run_cli(repo, *args, env_extra=None):
    import subprocess
    from pathlib import Path as _P

    script = _P(__file__).resolve().parents[3] / "scripts" / "util" / "skill_run_log.py"
    env = {
        "PATH": "/usr/bin:/bin",
        "HOME": str(repo.parent),
        "CODEX_HOME": str(repo.parent / "codex-home"),
    }
    if env_extra:
        env.update(env_extra)
    return subprocess.run(
        [sys.executable, "-m", "loopzero.kernel.run_log", *args],
        capture_output=True,
        text=True,
        cwd=repo,
        env=package_environment(env),
        timeout=60,
    )


def _row(run_id, skill, outcome, ts, session_id="sess-x"):
    return {
        "ts": ts,
        "skill": skill,
        "outcome": outcome,
        "run_id": run_id,
        "session_id": session_id,
        "session_source": "codex_thread",
        "harness": "codex",
        "git_branch": "feature/x",
    }


RID_OLD = "sr_" + "a" * 32
RID_LIVE = "sr_" + "b" * 32
RID_FRESH = "sr_" + "c" * 32


def test_check_ceiling_under_and_over(tmp_path):
    now = datetime.now(UTC)
    repo = _repo_with_runs(
        tmp_path,
        [
            # 25h > the 12h default ceiling; 9h remains under it.
            _row(
                RID_OLD, "backlog-drain", "in_progress", _iso(now - timedelta(hours=25))
            ),
            _row(
                RID_FRESH, "work-issue", "in_progress", _iso(now - timedelta(hours=9))
            ),
        ],
    )
    over = _run_cli(repo, "--skill", "backlog-drain", "--check-ceiling")
    assert over.returncode == 3, over.stdout + over.stderr
    assert "take-up brief" in over.stdout

    under = _run_cli(repo, "--skill", "work-issue", "--check-ceiling")
    assert under.returncode == 0, under.stdout + under.stderr

    none = _run_cli(repo, "--skill", "guardian", "--check-ceiling")
    assert none.returncode == 2


def test_ceiling_and_reconcile_are_decoupled(tmp_path):
    """A 9h dead session is under the 12h ceiling but past the 8h idle window:
    check-ceiling lets it run, reconcile reaps it (decoupled thresholds)."""
    now = datetime.now(UTC)
    # No codex-home rollout for RID_OLD → no life signs → dead.
    (tmp_path / "codex-home").mkdir(exist_ok=True)
    repo = _repo_with_runs(
        tmp_path,
        [_row(RID_OLD, "backlog-drain", "in_progress", _iso(now - timedelta(hours=9)))],
    )
    # Ceiling (12h default): a 9h run is fine to keep working.
    under = _run_cli(repo, "--skill", "backlog-drain", "--check-ceiling")
    assert under.returncode == 0, under.stdout + under.stderr
    # Reconcile (8h idle default): the same dead 9h run is reaped.
    reaped = _run_cli(repo, "--reconcile-stale")
    assert reaped.returncode == 0, reaped.stdout + reaped.stderr
    assert f"close {RID_OLD}" in reaped.stdout


def test_reconcile_closes_stale_but_never_live(tmp_path):
    now = datetime.now(UTC)
    live_session = "019fe000-0000-7000-8000-000000000abc"
    codex_home = tmp_path / "codex-home" / "sessions" / "2026" / "08" / "01"
    codex_home.mkdir(parents=True)
    # RID_LIVE's session has a recent structural provider event. File mtime is
    # deliberately not the authority because a later resume can refresh it.
    (codex_home / f"rollout-2026-08-01T00-00-00-{live_session}.jsonl").write_text(
        json.dumps(
            {
                "timestamp": _iso(now - timedelta(minutes=1)),
                "type": "event_msg",
                "payload": {"type": "task_started", "turn_id": "live-turn"},
            }
        )
        + "\n"
    )

    repo = _repo_with_runs(
        tmp_path,
        [
            _row(
                RID_OLD, "backlog-drain", "in_progress", _iso(now - timedelta(hours=20))
            ),
            _row(
                RID_LIVE,
                "work-issue",
                "in_progress",
                _iso(now - timedelta(hours=20)),
                session_id=live_session,
            ),
            _row(
                RID_FRESH, "work-issue", "in_progress", _iso(now - timedelta(hours=2))
            ),
        ],
    )

    dry = _run_cli(repo, "--reconcile-stale", "--dry-run")
    assert dry.returncode == 0
    assert RID_OLD in dry.stdout and "dry-run" in dry.stdout

    real = _run_cli(repo, "--reconcile-stale")
    assert real.returncode == 0, real.stdout + real.stderr
    assert f"close {RID_OLD}" in real.stdout
    assert live_session[:8] in real.stdout and "recent activity" in real.stdout

    # Latest states after reconcile: OLD abandoned(stale); LIVE + FRESH untouched.
    day_files = sorted((repo / ".audit" / "skill-runs").glob("*.jsonl"))
    rows = [
        json.loads(line)
        for f in day_files
        for line in f.read_text().splitlines()
        if line.strip()
    ]
    latest = {}
    for r in rows:
        latest[r["run_id"]] = r
    assert latest[RID_OLD]["outcome"] == "abandoned"
    assert "stale-reconcile" in latest[RID_OLD]["notes"]
    assert latest[RID_LIVE]["outcome"] == "in_progress"
    assert latest[RID_FRESH]["outcome"] == "in_progress"

    again = _run_cli(repo, "--reconcile-stale")
    assert "no stale runs to reconcile" in again.stdout


def test_start_rejects_reuse_on_a_different_branch(tmp_path):
    now = datetime.now(UTC)
    repo = _repo_with_runs(
        tmp_path,
        [_row(RID_LIVE, "resolve-findings", "in_progress", _iso(now))],
    )

    same = _run_cli(
        repo,
        "--start",
        "--skill",
        "resolve-findings",
        "--run-id",
        RID_LIVE,
        "--git-branch",
        "feature/x",
    )
    assert same.returncode == 0, same.stdout + same.stderr
    assert same.stdout.strip() == RID_LIVE

    other = _run_cli(
        repo,
        "--start",
        "--skill",
        "resolve-findings",
        "--run-id",
        RID_LIVE,
        "--git-branch",
        "feature/y",
    )
    assert other.returncode == 2, other.stdout + other.stderr
    assert "bound to branch 'feature/x'" in other.stderr


def test_start_rejects_an_explicit_second_run_on_an_owned_branch(tmp_path):
    now = datetime.now(UTC)
    repo = _repo_with_runs(
        tmp_path,
        [_row(RID_LIVE, "resolve-findings", "in_progress", _iso(now))],
    )

    result = _run_cli(
        repo,
        "--start",
        "--skill",
        "work-issue",
        "--run-id",
        RID_FRESH,
        "--git-branch",
        "feature/x",
    )

    assert result.returncode == 2, result.stdout + result.stderr
    assert f"already owned by active run {RID_LIVE}" in result.stderr


def test_review_reentry_preserves_forward_order_and_is_once_per_run():
    run_id = "sr_" + "1" * 32

    def row(phase, status):
        return {
            "kind": "phase",
            "skill": "work-issue",
            "run_id": run_id,
            "phase": str(phase),
            "status": status,
            "reason": "gate demanded rebase",
        }

    events = [row(p, status) for p in (1, 2, 3) for status in ("start", "complete")]
    events += [row(4, "start"), row(3, "review-reentry")]
    assert module.phase_state(events, skill="work-issue", run_id=run_id) == (
        "review",
        "local-validation",
    )
    resumed = [*events, row(3, "complete"), row(4, "start")]
    assert module.phase_state(resumed, skill="work-issue", run_id=run_id) == (
        "ci-wait",
        "review",
    )
    with pytest.raises(ValueError, match="reentry"):
        module.phase_state(
            [*resumed, row(3, "review-reentry")], skill="work-issue", run_id=run_id
        )
    with pytest.raises(ValueError, match="contradictory"):
        module.phase_state(
            [*events, row(5, "start")], skill="work-issue", run_id=run_id
        )


@pytest.fixture
def resume_cli(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    run_id = "sr_0123456789abcdef0123456789abcdef"
    rows = [
        _entry(run_id, "in_progress", ts="2026-09-08T10:00:00Z", issue=123, pr=456),
        _entry(
            run_id, "blocked", ts="2026-09-09T10:00:00Z", review_passes=2, tokens_in=400
        ),
    ]
    rows[-1].pop("issue")
    audit = tmp_path / ".audit" / "skill-runs"
    audit.mkdir(parents=True)
    log = audit / "2026-09-09.jsonl"
    log.write_text("".join(json.dumps(row) + "\n" for row in rows))
    fields = {
        "ts": "2026-09-09T11:00:00Z",
        "session_id": "resuming-session",
        "session_source": "test",
        "harness": "test",
        "git_branch": rows[0]["git_branch"],
    }
    monkeypatch.setattr(module, "repo_root", lambda: tmp_path)
    monkeypatch.setattr(module, "common_fields", lambda: fields)
    monkeypatch.setattr(
        module, "transition_phase", lambda **_: pytest.fail("resume reset phase")
    )

    def invoke(*extra):
        monkeypatch.setattr(
            sys,
            "argv",
            [
                "skill_run_log.py",
                "--skill",
                "work-issue",
                "--run-id",
                run_id,
                "--resume",
                "--notes",
                "Owner resumed delivery after blocker repair",
                *extra,
            ],
        )
        return module.main()

    return run_id, rows, log, fields, invoke


def test_resume_preserves_identity_history_and_counters(resume_cli):
    run_id, prior, log, fields, invoke = resume_cli
    assert invoke() == 0
    rows = [json.loads(line) for line in log.read_text().splitlines()]
    assert rows[:2] == prior
    assert rows[-1]["outcome"] == "in_progress"
    assert rows[-1]["issue"] == 123 and rows[-1]["pr"] == 456
    assert prior[-1]["review_passes"] == 2 and prior[-1]["tokens_in"] == 400
    assert "review_passes" not in rows[-1] and "tokens_in" not in rows[-1]
    assert rows[-1]["session_id"] == "resuming-session"
    assert module.active_run(rows, git_branch=fields["git_branch"]) == (
        run_id,
        "2026-09-08T10:00:00Z",
    )
    assert module.validate_transition(rows, _entry(run_id, "merged")) is True
    original = log.read_bytes()
    assert invoke() == 0
    assert log.read_bytes() == original


@pytest.mark.parametrize(
    "outcome", ["merged", "abandoned", "resolved_no_change", "in_progress"]
)
def test_resume_rejects_other_states(resume_cli, outcome):
    _, prior, log, _, invoke = resume_cli
    prior[-1]["outcome"] = outcome
    log.write_text("".join(json.dumps(row) + "\n" for row in prior))
    before = log.read_bytes()
    with pytest.raises(SystemExit, match="2"):
        invoke()
    assert log.read_bytes() == before


@pytest.mark.parametrize(
    "extra",
    [
        ("--notes", "  "),
        ("--skill", "implement-backend"),
        ("--issue", "999"),
        ("--pr", "999"),
        ("--git-branch", "another-branch"),
        ("--start",),
        ("--outcome", "in_progress"),
        ("--transition", "review"),
        ("--review-passes", "0"),
        ("--tokens-in", "0"),
    ],
)
def test_resume_rejects_conflicting_inputs(resume_cli, extra):
    _, _, log, _, invoke = resume_cli
    before = log.read_bytes()
    with pytest.raises(SystemExit, match="2"):
        invoke(*extra)
    assert log.read_bytes() == before


def test_resume_requires_actual_branch_and_exclusive_owner(resume_cli):
    _, prior, log, fields, invoke = resume_cli
    fields["git_branch"] = "different-checkout"
    with pytest.raises(SystemExit, match="2"):
        invoke("--git-branch", prior[0]["git_branch"])
    fields["git_branch"] = prior[0]["git_branch"]
    other = _entry("sr_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa", "in_progress")
    with log.open("a") as stream:
        stream.write(json.dumps(other) + "\n")
    with pytest.raises(SystemExit, match="2"):
        invoke()


def test_resume_rejects_missing_run(resume_cli):
    _, _, log, _, invoke = resume_cli
    log.write_text("")
    with pytest.raises(SystemExit, match="2"):
        invoke()


def test_start_cannot_resume_blocked_run(resume_cli, monkeypatch):
    run_id, _, _, _, _ = resume_cli
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "skill_run_log.py",
            "--skill",
            "work-issue",
            "--run-id",
            run_id,
            "--start",
        ],
    )
    with pytest.raises(ValueError, match="contradictory terminal"):
        module.main()


@pytest.mark.parametrize("future_other_run", [False, True])
def test_resume_rejects_clock_rollback_without_writes(resume_cli, future_other_run):
    _, _, log, fields, invoke = resume_cli
    fields["ts"] = "2026-09-08T11:00:00Z"
    if future_other_run:
        (log.parent / "2026-09-10.jsonl").write_text(
            json.dumps(
                _entry(
                    "sr_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
                    "blocked",
                    git_branch="other-delivery",
                    ts="2026-09-10T10:00:00Z",
                )
            )
            + "\n"
        )
    before = {p.name: p.read_bytes() for p in log.parent.glob("*.jsonl")}
    with pytest.raises(SystemExit, match="2"):
        invoke()
    assert {p.name: p.read_bytes() for p in log.parent.glob("*.jsonl")} == before


def test_resume_does_not_clone_historical_measurements(resume_cli):
    _, prior, log, _, invoke = resume_cli
    prior[-1].update(
        duration_s=500,
        review_passes=2,
        tokens_in=400,
        session_tokens_in_cumulative=9000,
        session_tokens_cached_cumulative=8000,
    )
    log.write_text("".join(json.dumps(row) + "\n" for row in prior))
    assert invoke() == 0
    rows = module.load_entries(log.parent)
    assert rows[:2] == prior
    assert not any(
        key in rows[-1]
        for key in (
            "duration_s",
            "review_passes",
            "tokens_in",
            "session_tokens_in_cumulative",
            "session_tokens_cached_cumulative",
        )
    )


@pytest.mark.parametrize("fresh_id", [None, "sr_bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"])
def test_start_cannot_replace_blocked_branch_owner(resume_cli, monkeypatch, fresh_id):
    _, _, log, _, _ = resume_cli
    args = ["skill_run_log.py", "--skill", "work-issue", "--start"]
    if fresh_id:
        args.extend(["--run-id", fresh_id])
    monkeypatch.setattr(sys, "argv", args)
    before = log.read_bytes()
    with pytest.raises(SystemExit, match="2"):
        module.main()
    assert log.read_bytes() == before


@pytest.mark.parametrize(
    "session,source,valid,ceiling,expected",
    [
        ("fresh", "codex_thread", True, 43200, 0),
        ("sess-x", "codex_thread", True, 43200, 3),
        ("fresh", "derived", True, 43200, 3),
        ("fresh", "codex_thread", False, 43200, 3),
        ("fresh", "codex_thread", True, 30, 3),
        ("", "codex_thread", True, 43200, 3),
        (" ", "codex_thread", True, 43200, 3),
        (123, "codex_thread", True, 43200, 3),
    ],
)
def test_session_ceiling_uses_only_fresh_valid_resume(
    session, source, valid, ceiling, expected
):
    now = datetime.now(UTC)
    start = _row(RID_OLD, "work-issue", "in_progress", _iso(now - timedelta(days=1)))
    blocked = {**start, "outcome": "blocked"}
    resume = {
        **start,
        "ts": _iso(now - timedelta(minutes=1)),
        "session_id": session,
        "session_source": source,
        "notes": "Continue",
        "resumed_from_sha256": module._canonical_digest(blocked) if valid else "0" * 64,
    }
    rows = [start, blocked, resume]
    args = argparse.Namespace(run_id=RID_OLD, skill="work-issue", ceiling_s=ceiling)
    assert module.cmd_check_ceiling(args, rows) == expected
    # Neither replay nor another block/resume within this context renews its clock.
    repeated_blocker = {**resume, "outcome": "blocked"}
    repeated = {
        **resume,
        "ts": _iso(now),
        "resumed_from_sha256": module._canonical_digest(repeated_blocker),
    }
    assert (
        module.cmd_check_ceiling(args, [*rows, resume, repeated_blocker, repeated])
        == expected
    )
    assert rows[0]["ts"] == start["ts"]

    if valid and session == "fresh" and source == "codex_thread":
        for returning_session in ("sess-x", "fresh"):
            blocker = {**rows[-1], "outcome": "blocked"}
            returning = {
                **resume,
                "ts": _iso(now),
                "session_id": returning_session,
                "resumed_from_sha256": module._canonical_digest(blocker),
            }
            rows.extend([blocker, returning])
            assert module.cmd_check_ceiling(args, rows) == (
                3 if returning_session == "sess-x" else expected
            )


def test_delivery_contract_is_fixed_by_first_run_row() -> None:
    run_id = "sr_" + "9" * 32
    old = [{"run_id": run_id, "skill": "work-issue"}]
    assert module.run_delivery_contract(old, run_id) == "intelflo-v1"
    new = [{"run_id": run_id, "delivery_contract": "loop-zero-v1"}]
    assert module.run_delivery_contract(new, run_id) == "loop-zero-v1"
    with pytest.raises(ValueError, match="contract"):
        module.run_delivery_contract(old + new, run_id)
    with pytest.raises(ValueError, match="contract"):
        module.run_delivery_contract(
            [{"run_id": run_id, "delivery_contract": "unknown"}], run_id
        )


def test_start_binds_new_contract_without_migrating_resumed_runs() -> None:
    run_id = "sr_" + "9" * 32
    new = {"run_id": run_id}
    module.bind_delivery_contract(new, [], start=True)
    assert new["delivery_contract"] == "loop-zero-v1"
    old = {"run_id": run_id}
    module.bind_delivery_contract(old, [{"run_id": run_id}], start=True)
    assert old["delivery_contract"] == "intelflo-v1"


def test_missing_history_terminal_recovery_keeps_legacy_contract() -> None:
    recovered = {"run_id": "sr_" + "9" * 32, "outcome": "merged"}
    module.bind_delivery_contract(recovered, [])
    assert recovered["delivery_contract"] == "intelflo-v1"


def test_fresh_cli_entry_selects_loopzero_and_resume_does_not_create_phases(tmp_path):
    repo = _repo_with_runs(tmp_path, [])
    started = _run_cli(
        repo, "--start", "--skill", "work-issue", "--git-branch", "feature/new"
    )
    assert started.returncode == 0, started.stderr
    run_id = started.stdout.strip()
    assert (
        module.run_delivery_contract(
            module.load_entries(repo / ".audit/skill-runs"), run_id
        )
        == "loop-zero-v1"
    )
    repeated = _run_cli(
        repo, "--start", "--skill", "work-issue", "--git-branch", "feature/new"
    )
    assert repeated.stdout.strip() == run_id
    transition = _run_cli(
        repo, "--skill", "work-issue", "--run-id", run_id, "--transition", "review"
    )
    assert transition.returncode == 0, transition.stderr
    assert "no phase transitions" in transition.stdout
    assert module.load_phase_events(repo) == []
    owner = _run_cli(
        repo,
        "--skill",
        "work-issue",
        "--run-id",
        run_id,
        "--check-owner",
        "--print-delivery-contract",
    )
    assert owner.returncode == 0 and owner.stdout.strip() == "loop-zero-v1"


def test_delivery_contract_query_is_read_only_and_rejects_unknown_run(tmp_path):
    repo = _repo_with_runs(tmp_path, [])
    started = _run_cli(
        repo, "--start", "--skill", "work-issue", "--git-branch", "feature/query"
    )
    run_id = started.stdout.strip()
    before = module.load_entries(repo / ".audit/skill-runs")
    query = _run_cli(repo, "--run-id", run_id, "--print-delivery-contract")
    assert query.returncode == 0 and query.stdout.strip() == "loop-zero-v1"
    assert module.load_entries(repo / ".audit/skill-runs") == before
    missing = _run_cli(repo, "--run-id", "sr_" + "f" * 32, "--print-delivery-contract")
    assert missing.returncode != 0
