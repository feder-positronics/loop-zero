import argparse
import importlib.util
import json
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


def test_context_boundary_event_is_idempotent(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(module, "repo_root", lambda: tmp_path)
    monkeypatch.setattr(
        module,
        "common_fields",
        lambda: {
            "ts": "2026-07-13T12:00:00Z",
            "session_id": "session-1",
            "git_branch": "feature/test",
        },
    )

    assert (
        module.write_context_boundary_event(
            run_id="sr_0123456789abcdef0123456789abcdef",
            skill="work-issue",
            disposition="rollover",
            issue=2644,
            pr=2700,
        )
        is True
    )
    assert (
        module.write_context_boundary_event(
            run_id="sr_0123456789abcdef0123456789abcdef",
            skill="work-issue",
            disposition="rollover",
            issue=2644,
            pr=2700,
        )
        is False
    )

    log_path = tmp_path / ".audit" / "agent-events" / "2026-07-13.jsonl"
    rows = [json.loads(line) for line in log_path.read_text().splitlines()]
    assert len(rows) == 1
    assert rows[0]["kind"] == "context_boundary"
    assert rows[0]["run_id"] == "sr_0123456789abcdef0123456789abcdef"


def test_context_boundary_parser_has_bounded_vocabulary() -> None:
    args = module.build_parser().parse_args(
        [
            "context-boundary",
            "--run-id",
            "sr_0123456789abcdef0123456789abcdef",
            "--skill",
            "execute-blueprint",
            "--disposition",
            "kept_inline",
            "--issue",
            "2644",
        ]
    )

    assert args.disposition == "kept_inline"


def test_context_boundary_common_field_failure_is_non_blocking(monkeypatch) -> None:
    monkeypatch.setattr(
        module, "common_fields", lambda: (_ for _ in ()).throw(OSError("offline"))
    )

    assert (
        module.write_context_boundary_event(
            run_id="sr_0123456789abcdef0123456789abcdef",
            skill="work-issue",
            disposition="rollover",
        )
        is False
    )


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


def test_friction_event_records_required_and_optional_fields(monkeypatch) -> None:
    captured: dict = {}
    monkeypatch.setattr(module, "common_fields", lambda: {"session_id": "session-1"})
    monkeypatch.setattr(module, "write_event", lambda event: captured.update(event))

    args = argparse.Namespace(
        skill="code-review",
        signal="repeated-finding",
        summary="Authorization finding survived a review pass",
        evidence_kind="review-finding",
        evidence_id="review-42",
        surface="backend-auth",
        issue_key="missing-user-filter",
        status="observed",
    )

    assert module.cmd_friction(args) == 0
    assert captured == {
        "session_id": "session-1",
        "kind": "friction",
        "skill": "code-review",
        "signal": "repeated-finding",
        "summary": "Authorization finding survived a review pass",
        "evidence_kind": "review-finding",
        "evidence_id": "review-42",
        "surface": "backend-auth",
        "issue_key": "missing-user-filter",
        "status": "observed",
    }


def test_friction_parser_supports_exact_signal_vocabulary() -> None:
    parser = module.build_parser()
    expected = {
        "owner-correction",
        "repeated-finding",
        "escape-hatch",
        "blocked-workflow",
        "unexpected-workaround",
        "useful-simplification",
    }

    for signal in expected:
        args = [
            "friction",
            "--skill",
            "code-review",
            "--signal",
            signal,
            "--summary",
            "Observed friction",
        ]
        if signal in module.KEYED_FRICTION_SIGNALS:
            args.extend(["--issue-key", f"test-{signal}"])
        assert parser.parse_args(args).signal == signal


def test_friction_requires_issue_key_for_recurrence_signals(capsys) -> None:
    for signal in ("repeated-finding", "unexpected-workaround"):
        try:
            module.main(
                [
                    "friction",
                    "--skill",
                    "code-review",
                    "--signal",
                    signal,
                    "--summary",
                    "A recurring problem",
                ]
            )
        except SystemExit as exc:
            assert exc.code == 2
        else:
            raise AssertionError(f"{signal} unexpectedly accepted without --issue-key")

    assert "--issue-key is required" in capsys.readouterr().err


def test_blocked_workflow_may_be_unkeyed(monkeypatch) -> None:
    captured: list[dict] = []
    monkeypatch.setattr(module, "common_fields", lambda: {"session_id": "session-1"})
    monkeypatch.setattr(module, "write_event", captured.append)

    assert (
        module.main(
            [
                "friction",
                "--skill",
                "work-issue",
                "--signal",
                "blocked-workflow",
                "--summary",
                "Merge gate could not inspect the final SHA",
            ]
        )
        == 0
    )
    assert "issue_key" not in captured[0]


def test_friction_resolution_requires_issue_key(capsys) -> None:
    try:
        module.main(
            [
                "friction",
                "--skill",
                "skill-health",
                "--signal",
                "blocked-workflow",
                "--summary",
                "Fixed the workflow blocker",
                "--status",
                "resolved",
            ]
        )
    except SystemExit as exc:
        assert exc.code == 2
    else:
        raise AssertionError("resolved friction unexpectedly accepted without a key")

    assert "--issue-key is required" in capsys.readouterr().err


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
