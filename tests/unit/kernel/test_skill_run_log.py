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
    repo_root = Path(__file__).resolve().parents[4]
    module_path = repo_root / "scripts" / "util" / "agent_event.py"
    spec = importlib.util.spec_from_file_location("agent_event", module_path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def load_module() -> ModuleType:
    repo_root = Path(__file__).resolve().parents[4]
    load_agent_event_module()
    module_path = repo_root / "scripts" / "util" / "skill_run_log.py"
    spec = importlib.util.spec_from_file_location("skill_run_log", module_path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


module = load_module()


def test_derive_duration_s_uses_earliest_matching_event(tmp_path: Path) -> None:
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
                        "kind": "phase",
                    }
                ),
                json.dumps(
                    {
                        "ts": "2026-05-14T10:03:00Z",
                        "session_id": "thread-1",
                        "skill": "work-issue",
                        "kind": "phase",
                    }
                ),
                json.dumps(
                    {
                        "ts": "2026-05-14T09:50:00Z",
                        "session_id": "thread-2",
                        "skill": "work-issue",
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
        datetime(2026, 5, 14, 10, 5, 0, tzinfo=UTC),
    )

    assert duration == 300


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
        lambda root, skill, session_id, end_ts: 300,
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


def test_in_progress_can_transition_to_terminal_from_another_session() -> None:
    run_id = "sr_0123456789abcdef0123456789abcdef"
    existing = [_entry(run_id, "in_progress", session_id="first")]

    should_append = module.validate_transition(
        existing,
        _entry(run_id, "merged", pr=2700, session_id="closeout"),
    )

    assert should_append is True


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
    assert entry["tokens_in"] == 1_200_000
    assert entry["tokens_out"] == 45_000
    assert entry["tokens_cached"] == 1_100_000


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

    script = _P(__file__).resolve().parents[4] / "scripts" / "util" / "skill_run_log.py"
    env = {
        "PATH": "/usr/bin:/bin",
        "HOME": str(repo.parent),
        "CODEX_HOME": str(repo.parent / "codex-home"),
    }
    if env_extra:
        env.update(env_extra)
    return subprocess.run(
        [sys.executable, str(script), *args],
        capture_output=True,
        text=True,
        cwd=repo,
        env=env,
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
            _row(
                RID_OLD, "backlog-drain", "in_progress", _iso(now - timedelta(hours=9))
            ),
            _row(
                RID_FRESH, "work-issue", "in_progress", _iso(now - timedelta(hours=1))
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


def test_reconcile_closes_stale_but_never_live(tmp_path):
    now = datetime.now(UTC)
    codex_home = tmp_path / "codex-home" / "sessions" / "2026" / "08" / "01"
    codex_home.mkdir(parents=True)
    # RID_LIVE's session has a freshly-touched rollout: owner shows life signs.
    (codex_home / "rollout-2026-08-01T00-00-00-sess-live.jsonl").write_text("{}\n")

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
                session_id="sess-live",
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
    assert "sess-live" in real.stdout and "recent activity" in real.stdout

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
