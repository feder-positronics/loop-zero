import argparse
import importlib.util
import os
import sys
from pathlib import Path
from types import ModuleType


def load_module() -> ModuleType:
    repo_root = Path(__file__).resolve().parents[4]
    module_path = repo_root / "scripts" / "util" / "agent_event.py"
    spec = importlib.util.spec_from_file_location("agent_event", module_path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


module = load_module()


def test_detect_harness_prefers_codex_env(monkeypatch) -> None:
    monkeypatch.delenv("AGENT_HARNESS", raising=False)
    monkeypatch.setenv("CODEX_THREAD_ID", "thread-123")

    assert module.detect_harness() == "codex"


def test_resolve_session_prefers_codex_thread_id(monkeypatch) -> None:
    monkeypatch.delenv("AGENT_SESSION_ID", raising=False)
    monkeypatch.setenv("CODEX_THREAD_ID", "thread-xyz")

    session_id, source = module.resolve_session(Path("/tmp/repo"), "feature/test")

    assert session_id == "thread-xyz"
    assert source == "codex_thread"


def test_output_event_records_decision_counts_and_ids(monkeypatch) -> None:
    captured: dict = {}
    monkeypatch.setattr(
        module,
        "common_fields",
        lambda: {
            "ts": "2026-07-02T10:00:00Z",
            "session_id": "session-1",
            "session_source": "env",
            "harness": "codex",
            "git_branch": "feature/test",
        },
    )
    monkeypatch.setattr(module, "write_event", lambda event: captured.update(event))

    args = argparse.Namespace(
        skill="resolve-findings",
        pass_num=None,
        findings_critical=None,
        findings_high=None,
        findings_medium=None,
        findings_low=None,
        docs_created=None,
        docs_updated=None,
        drift_fixes=None,
        decisions_made=None,
        decisions_locked=1,
        decisions_assumed=2,
        decisions_deferred=3,
        decision_locked_ids=["DR-1"],
        decision_assumed_ids=["DR-2", "DR-3"],
        decision_deferred_ids=["DR-4"],
        reverses_decision_ids=["DR-2"],
        notes=None,
    )

    assert module.cmd_output(args) == 0

    assert captured["kind"] == "output"
    assert captured["skill"] == "resolve-findings"
    assert captured["decisions"] == {
        "locked": 1,
        "assumed": 2,
        "deferred": 3,
        "ids": {
            "locked": ["DR-1"],
            "assumed": ["DR-2", "DR-3"],
            "deferred": ["DR-4"],
        },
    }
    assert captured["reverses_decision_ids"] == ["DR-2"]


def test_tool_event_records_cross_harness_cost_fields(monkeypatch) -> None:
    captured: dict = {}
    monkeypatch.setattr(module, "common_fields", lambda: {"ts": "2026-07-10T10:00:00Z"})
    monkeypatch.setattr(module, "write_event", lambda event: captured.update(event))

    args = argparse.Namespace(
        category="cross-harness-review",
        duration_ms=19_000,
        result="pass",
        skill="code-review",
        cost_usd=0.37,
        budget_usd=1.0,
        provider="claude",
        scope_digest="abc123",
        notes=None,
    )

    assert module.cmd_tool(args) == 0
    assert captured["cost_usd"] == 0.37
    assert captured["budget_usd"] == 1.0
    assert captured["provider"] == "claude"
    assert captured["scope_digest"] == "abc123"


def test_decision_event_records_calibration_envelope(monkeypatch) -> None:
    captured: dict = {}
    monkeypatch.setattr(module, "common_fields", lambda: {"ts": "2026-07-11T10:00:00Z"})
    monkeypatch.setattr(module, "write_event", lambda event: captured.update(event))

    args = argparse.Namespace(
        decision_id="ADC-05",
        state="assumed",
        skill="resolve-findings",
        category="workflow-policy",
        weight="normal",
        authority="decide-notify",
        confidence="high",
        reversibility="single-pr",
        calibration_deadline="2026-07-18",
        challenger_verdict="agree",
        rejected_alternative="ask-first",
        grounding="D-16; issue #2466",
        expected_outcome="fewer owner interruptions",
        notes=None,
    )

    assert module.cmd_decision(args) == 0
    assert captured == {
        "ts": "2026-07-11T10:00:00Z",
        "kind": "decision",
        "decision_id": "ADC-05",
        "state": "assumed",
        "owner_verdict": "pending",
        "outcome_verdict": "unknown",
        "skill": "resolve-findings",
        "category": "workflow-policy",
        "weight": "normal",
        "authority": "decide-notify",
        "confidence": "high",
        "reversibility": "single-pr",
        "calibration_deadline": "2026-07-18",
        "challenger_verdict": "agree",
        "rejected_alternative": "ask-first",
        "grounding": "D-16; issue #2466",
        "expected_outcome": "fewer owner interruptions",
    }


def test_verdict_and_outcome_events_are_addressed_by_decision_id(monkeypatch) -> None:
    captured: list[dict] = []
    monkeypatch.setattr(module, "common_fields", lambda: {"ts": "2026-07-18T10:00:00Z"})
    monkeypatch.setattr(module, "write_event", captured.append)

    assert (
        module.cmd_verdict(
            argparse.Namespace(
                decision_id="ADC-05", owner_verdict="no-veto", notes="weekly digest"
            )
        )
        == 0
    )
    assert (
        module.cmd_outcome(
            argparse.Namespace(
                decision_id="ADC-05", outcome_verdict="supported", notes=None
            )
        )
        == 0
    )

    assert captured[0]["kind"] == "decision-verdict"
    assert captured[0]["owner_verdict"] == "no-veto"
    assert captured[1]["kind"] == "decision-outcome"
    assert captured[1]["outcome_verdict"] == "supported"
