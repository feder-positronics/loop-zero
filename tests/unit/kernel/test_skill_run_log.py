import argparse
import importlib.util
import json
import sys
from datetime import UTC, datetime
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
