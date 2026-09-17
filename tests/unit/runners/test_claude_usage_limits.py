"""Native SDK limit evidence must not become retry or account authority."""

import asyncio
import json

import claude_agent_sdk
import pytest
from claude_agent_sdk import AssistantMessage, RateLimitEvent, ResultMessage, TextBlock
from claude_agent_sdk.types import RateLimitInfo

from loopzero.runners import bridge, claude, contract
from tests.unit.runners.test_agent_runtimes import governed_schema


def result(*, error=True, status=429, structured=None, subtype=None, output=None):
    return ResultMessage(
        subtype=subtype or ("error_during_execution" if error else "success"),
        duration_ms=100,
        duration_api_ms=80,
        is_error=error,
        num_turns=1,
        session_id="session-limit",
        api_error_status=status,
        structured_output=structured,
        result=output,
    )


def event(*, status="rejected", scope="five_hour", reset=1_900_000_000, overage=None):
    return RateLimitEvent(
        rate_limit_info=RateLimitInfo(
            status=status,
            rate_limit_type=scope,
            resets_at=reset,
            overage_status=overage,
        ),
        uuid="limit-event",
        session_id="session-limit",
    )


def run(messages, monkeypatch, tmp_path, *, mode="subscription-only"):
    frames = []

    async def query(**_kwargs):
        for message in messages:
            yield message

    monkeypatch.setattr(claude_agent_sdk, "query", query)
    monkeypatch.setattr(bridge, "_write_frame", frames.append)
    asyncio.run(
        bridge._run_claude(
            {
                "vendor": "claude",
                "prompt": "private prompt",
                "cwd": str(tmp_path),
                "requested_model": "claude-opus-5",
                "effort": "high",
                "read_only": True,
                "budget_usd": None,
                "commercial_mode": mode,
                "output_schema": governed_schema("attempt-limit"),
                "read_roots": [str(tmp_path)],
            }
        )
    )
    parsed = claude.parse_claude_stream("\n".join(json.dumps(row) for row in frames))
    return parsed, frames


def test_native_rejected_window_and_unsuccessful_429_are_typed(monkeypatch, tmp_path):
    parsed, _ = run([event(), result()], monkeypatch, tmp_path)
    assert parsed.status.value == "limited"
    assert parsed.terminal_reason.value == "usage-limit"
    assert parsed.usage_limit.scope.value == "five_hour"
    assert parsed.usage_limit.resets_at == 1_900_000_000
    assert parsed.semantic_event  # Provider attempt evidence prevents fallback.


@pytest.mark.parametrize(
    "reset",
    [None, False, True, -1, 0, "1900000000", float("inf"), float("nan"), 10**100],
)
def test_invalid_reset_never_becomes_zero_or_cooldown(reset, monkeypatch, tmp_path):
    parsed, _ = run([event(reset=reset), result()], monkeypatch, tmp_path)
    assert parsed.status.value == "limited"
    assert parsed.usage_limit.resets_at is None


def test_assistant_typed_error_is_limited_with_unknown_scope(monkeypatch, tmp_path):
    error = AssistantMessage(
        content=[TextBlock(text="private provider text")],
        model="<synthetic>",
        error="rate_limit",
    )
    parsed, _ = run([error, result(status=None)], monkeypatch, tmp_path)
    assert parsed.status.value == "limited"
    assert parsed.usage_limit.scope.value == "unknown"
    assert parsed.usage_limit.resets_at is None
    assert parsed.semantic_event  # Provider attempt evidence prevents fallback.


def test_limit_preserves_partial_output_and_semantic_activity(monkeypatch, tmp_path):
    partial = {"task_id": "attempt-limit", "summary": "private partial"}
    parsed, _ = run(
        [
            AssistantMessage(
                content=[TextBlock(text="started")], model="claude-opus-5"
            ),
            event(),
            result(structured=partial, output="private partial text"),
        ],
        monkeypatch,
        tmp_path,
    )
    assert parsed.status.value == "limited"
    assert parsed.structured_output == partial
    assert parsed.final_output == "private partial text"
    assert parsed.semantic_event
    assert "private partial" not in repr(parsed)


@pytest.mark.parametrize(
    "messages",
    [
        [result(output="rate limit exceeded")],
        [event(status="allowed_warning"), result()],
        [event(), result(status=500)],
        [event(), result(subtype="error_max_budget_usd")],
    ],
)
def test_unrelated_failures_and_local_budget_are_not_window_limits(
    messages, monkeypatch, tmp_path
):
    parsed, _ = run(messages, monkeypatch, tmp_path)
    assert parsed.status.value == "failed"
    assert parsed.terminal_reason.value != "usage-limit"


@pytest.mark.parametrize(
    "mode,expected",
    [
        ("subscription-only", "commercial-boundary"),
        ("owner-paid", "process-exit"),
        ("promotional-credit", "process-exit"),
    ],
)
def test_allowed_overage_preserves_commercial_boundary(
    mode, expected, monkeypatch, tmp_path
):
    parsed, _ = run(
        [event(overage="allowed"), result()], monkeypatch, tmp_path, mode=mode
    )
    assert parsed.terminal_reason.value == expected


def test_rejected_advisory_does_not_downgrade_accepted_completion(
    monkeypatch, tmp_path
):
    accepted = {"task_id": "attempt-limit", "status": "completed", "summary": "done"}
    parsed, _ = run(
        [event(), result(error=False, status=None, structured=accepted)],
        monkeypatch,
        tmp_path,
    )
    assert parsed.status is contract.RuntimeStatus.COMPLETED
    assert parsed.structured_output == accepted
    assert getattr(parsed, "usage_limit", None) is None


def test_accepted_tool_result_survives_later_limit(monkeypatch, tmp_path):
    from claude_agent_sdk import ToolUseBlock, ToolResultBlock, UserMessage
    accepted = {"task_id": "attempt-limit", "status": "completed", "summary": "done"}
    parsed, _ = run([
        AssistantMessage(content=[ToolUseBlock(id="accepted", name="StructuredOutput", input=accepted)], model="claude-opus-5"),
        UserMessage(content=[ToolResultBlock(tool_use_id="accepted", content="Structured output provided successfully", is_error=False)]),
        event(), result(),
    ], monkeypatch, tmp_path)
    assert parsed.status is contract.RuntimeStatus.COMPLETED
    assert parsed.structured_output == accepted
    assert parsed.usage_limit is None


@pytest.mark.parametrize("overrides", [
    {"terminal_reason": "process-exit"}, {"status": "failed"},
    {"usage_limit": None}, {"usage_limit": {"scope": "invented"}},
    {"subtype": "error_max_budget_usd"},
])
def test_limit_wire_contract_rejects_inconsistent_terminals(overrides):
    frame = {"type": "result", "status": "limited", "terminal_reason": "usage-limit",
             "usage_limit": {"scope": "five_hour", "resets_at": 1_900_000_000}}
    frame.update(overrides)
    with pytest.raises(claude.ClaudeProtocolError):
        claude.parse_claude_stream(json.dumps(frame))


def test_limit_adapter_retains_metadata_without_retry(tmp_path):
    from loopzero.runners import process
    from tests.unit.runners.test_agent_runtimes import claude_request, _auth_process_result
    calls = []
    def execute(command, **kwargs):
        calls.append(command)
        return process.ProcessResult(returncode=1, stdout=json.dumps({
            "type": "result", "status": "limited", "terminal_reason": "usage-limit",
            "usage_limit": {"scope": "seven_day", "resets_at": 1_900_000_000},
        }), stderr="", duration_s=.01, timed_out=False)
    adapter = claude.ClaudeAdapter(run_process=execute,
        run_probe=lambda *args, **kwargs: _auth_process_result(),
        sdk_available=lambda *_args: True, which=lambda _name: "/usr/bin/claude")
    outcome = adapter.run(claude_request(tmp_path))
    assert outcome.status is contract.RuntimeStatus.LIMITED
    assert outcome.usage_limit.scope is contract.RuntimeLimitScope.SEVEN_DAY
    assert len(calls) == 1


def test_later_success_cannot_replace_a_terminal_limit():
    frames = [
        {"type": "result", "status": "limited", "terminal_reason": "usage-limit",
         "usage_limit": {"scope": "five_hour", "resets_at": 1_900_000_000}},
        {"type": "result", "status": "completed", "terminal_reason": "completed"},
    ]
    with pytest.raises(claude.ClaudeProtocolError, match="after its terminal"):
        claude.parse_claude_stream("\n".join(map(json.dumps, frames)))
