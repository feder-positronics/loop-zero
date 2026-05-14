import argparse
import importlib.util
import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from types import ModuleType


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
