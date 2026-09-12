import json
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from loopzero.runners.fake import FakeAdapter, ScenarioSpec
from loopzero.runners.registry import RuntimeRegistration, RuntimeRegistry
from loopzero.review import harness


def _profile(tmp_path):
    return SimpleNamespace(
        aliases={
            "alternate": SimpleNamespace(runner="fake", model="review-model", write=False)
        },
        tiers={"review": SimpleNamespace(alias="alternate", effort="low")},
        routing_budgets={"low": 1.0},
        routing_policy_version="policy-v1",
        telemetry_schema_version="telemetry-v1",
        compatible_policy_versions=(),
        default_timeout_s=30,
        engine_cooldown_s=10,
        audit_root=Path("private"),
        env_prefix="CONSUMER",
        toolchain={}, state_root="unused", state_root_explicit=False,
        root=tmp_path,
    )


def _flow_profile(tmp_path):
    profile = _profile(tmp_path)
    profile.aliases["alternate"] = SimpleNamespace(
        runner="claude",
        model="review-model",
        write=False,
        default_effort="low",
    )
    profile.cross_harness_routes = {"codex": "alternate"}
    return profile


def _fake_claude_registry(factory):
    return RuntimeRegistry(
        {
            "claude": RuntimeRegistration(
                preferred_transport="fake/scenario",
                cli_fallback_transport=None,
                factory=factory,
            )
        }
    )


def test_harness_uses_registry_and_normalized_runtime_contract(tmp_path):
    result = harness.run_review(
        _profile(tmp_path), worktree=tmp_path, alias="alternate", effort="low",
        prompt="review this", attempt_id="attempt-1",
        adapter_options={
            "scenario": ScenarioSpec(
                output=json.dumps(
                    {"findings": [{"severity": "important", "claim": "fix it"}]}
                )
            )
        },
    )
    assert result.finding_count == 1
    assert result.max_severity == "important"
    assert result.runtime.vendor == "fake"


def test_harness_rejects_malformed_or_failed_runtime_result(tmp_path):
    with pytest.raises(harness.CrossHarnessError, match="not JSON"):
        harness.run_review(
            _profile(tmp_path), worktree=tmp_path, alias="alternate", effort="low",
            prompt="review", attempt_id="attempt-1",
            adapter_options={"scenario": ScenarioSpec(output="not json")},
        )
    with pytest.raises(harness.CrossHarnessError, match="did not complete"):
        harness.run_review(
            _profile(tmp_path), worktree=tmp_path, alias="alternate", effort="low",
            prompt="review", attempt_id="attempt-2",
            adapter_options={"scenario": "timeout/expiry"},
        )


def test_harness_contains_no_native_command_shapes():
    source = Path(harness.__file__).read_text(encoding="utf-8")
    assert "subprocess" not in source
    assert "_command(" not in source


def test_prompt_cap_skips_before_fake_runner_launch(monkeypatch, tmp_path):
    created = []
    events = []

    def forbidden_factory(**kwargs):
        created.append(kwargs)
        raise AssertionError("oversized prompt reached the runtime")

    monkeypatch.setattr(harness, "collect_scope", lambda *a, **k: "x" * 200)
    monkeypatch.setattr(harness, "CLAUDE_MAX_PROMPT_CHARS", 100)
    monkeypatch.setattr(
        harness, "_emit_event", lambda _repo, **event: events.append(event)
    )

    result = harness.run_review(
        SimpleNamespace(),
        repo=tmp_path,
        skill="code-review",
        current_harness="codex",
        request_reason="Check the unresolved scope boundary",
        scope="diff",
        diff_range="HEAD",
        paths=[],
        profile=_flow_profile(tmp_path),
        registry=_fake_claude_registry(forbidden_factory),
    )

    assert result == 0
    assert created == []
    assert events[0]["notes"].startswith("scope too large for Claude (")
    assert events[0]["prompt_chars"] > 100
    assert events[0]["timeout_seconds"] == 120


def test_successful_fake_review_records_prompt_size_and_timeout(
    monkeypatch, tmp_path
):
    events = []
    requests = []

    class RecordingFakeAdapter(FakeAdapter):
        def run(self, request, *, on_progress=None):
            requests.append(request)
            return super().run(request, on_progress=on_progress)

    monkeypatch.setattr(harness, "collect_scope", lambda *a, **k: "exact scope")
    monkeypatch.setattr(
        harness, "_emit_event", lambda _repo, **event: events.append(event)
    )
    registry = _fake_claude_registry(
        lambda **_kwargs: RecordingFakeAdapter(
            scenario=ScenarioSpec(output='{"findings":[],"limitations":[]}')
        )
    )

    result = harness.run_review(
        SimpleNamespace(),
        repo=tmp_path,
        skill="code-review",
        current_harness="codex",
        request_reason="Check the unresolved scope boundary",
        scope="diff",
        diff_range="HEAD",
        paths=[],
        profile=_flow_profile(tmp_path),
        registry=registry,
    )

    assert result == 0
    assert len(requests) == 1
    assert events[0]["result"] == "pass"
    assert events[0]["provider"] == "claude"
    assert events[0]["prompt_chars"] == len(requests[0].prompt)
    assert events[0]["timeout_seconds"] == requests[0].timeout_s == 120


def test_fake_timeout_is_terminal_for_the_same_scope(monkeypatch, tmp_path, capsys):
    events = []
    launches = []

    class RecordingTimeoutAdapter(FakeAdapter):
        def run(self, request, *, on_progress=None):
            launches.append(request.attempt_id)
            return super().run(request, on_progress=on_progress)

    def emit(repo, **event):
        events.append(event)
        audit = repo / ".audit" / "agent-events"
        audit.mkdir(parents=True, exist_ok=True)
        payload = {
            **event,
            "kind": "tool",
            "category": "cross-harness-review",
            "ts": datetime.now(UTC).isoformat(),
        }
        (audit / "events.jsonl").write_text(
            json.dumps(payload) + "\n", encoding="utf-8"
        )

    monkeypatch.setattr(harness, "collect_scope", lambda *a, **k: "exact scope")
    monkeypatch.setattr(harness, "_emit_event", emit)
    registry = _fake_claude_registry(
        lambda **_kwargs: RecordingTimeoutAdapter(scenario="timeout/expiry")
    )
    kwargs = {
        "repo": tmp_path,
        "skill": "code-review",
        "current_harness": "codex",
        "request_reason": "Check the unresolved scope boundary",
        "scope": "files",
        "diff_range": "HEAD",
        "paths": ["app.py"],
        "profile": _flow_profile(tmp_path),
        "registry": registry,
    }

    assert harness.run_review(SimpleNamespace(), **kwargs) == 0
    assert events[0]["notes"].startswith("timeout; request: ")
    assert harness.run_review(SimpleNamespace(), **kwargs) == 0
    assert len(launches) == 1
    assert "one attempt per logical gate" in capsys.readouterr().err
