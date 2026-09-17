"""Prospective absence evidence must survive normalization and fail closed."""

import asyncio
import json
from types import SimpleNamespace

import pytest

from loopzero.runners import bridge, claude, contract


def terminal(**overrides):
    return {
        "type": "result",
        "status": "failed",
        "terminal_reason": "process-exit",
        "model_output_seen": False,
        "model_output_evidence_version": 1,
        **overrides,
    }


def parse(*frames, sdk_bridge=True):
    return claude.parse_claude_stream(
        "\n".join(map(json.dumps, frames)), sdk_bridge=sdk_bridge
    )


def test_only_prospective_sdk_terminal_can_establish_empty():
    assert parse(terminal()).model_output_seen is False
    assert parse(terminal(), sdk_bridge=False).model_output_seen is None
    for field in ("model_output_seen", "model_output_evidence_version"):
        frame = terminal()
        del frame[field]
        assert parse(frame).model_output_seen is None
    for value in (0, "false", None):
        assert parse(terminal(model_output_seen=value)).model_output_seen is None


@pytest.mark.parametrize(
    "extra",
    [
        {"output": "partial"},
        {"structured_output": {}},
        {"usage": {"output_tokens": 1}},
        {"model_output_seen": True},
        {"usage": {"output_tokens": 0, "outputTokens": 1}},
        {"usage": {"output_tokens": 0}, "modelUsage": {"model": {"outputTokens": 1}}},
        {"usage": {"reasoning_tokens": 1}},
    ],
)
def test_positive_terminal_evidence_overrides_empty_claim(extra):
    assert parse(terminal(**extra)).model_output_seen is True


@pytest.mark.parametrize(
    "kind,subtype",
    [
        ("assistant", "message"),
        ("tool_use", None),
        ("tool_result", None),
        ("stream", "content_block_delta"),
    ],
)
def test_positive_events_override_empty_claim_even_with_false_semantic(kind, subtype):
    assert (
        parse(
            {"type": "event", "kind": kind, "subtype": subtype, "semantic": False},
            terminal(),
        ).model_output_seen
        is True
    )


def test_unknown_and_error_streams_cannot_prove_empty():
    assert parse({"type": "unknown"}, terminal()).model_output_seen is None
    assert parse({"type": "error", "reason": "disconnect"}).model_output_seen is None
    with pytest.raises(claude.ClaudeProtocolError):
        parse({"type": "event", "kind": "system"})
    with pytest.raises(claude.ClaudeProtocolError):
        parse(terminal(), terminal())


@pytest.mark.parametrize("counter", ["5", True, -1, None, float("nan")])
@pytest.mark.parametrize("through_bridge", [False, True])
def test_malformed_raw_counter_cannot_prove_empty(
    counter, through_bridge, monkeypatch, capsys
):
    import claude_agent_sdk as sdk

    if not through_bridge:
        assert (
            parse(terminal(usage={"output_tokens": counter})).model_output_seen is None
        )
        return

    async def query(**kwargs):
        yield sdk.ResultMessage(
            subtype="error_max_budget_usd",
            duration_ms=1,
            duration_api_ms=0,
            is_error=True,
            num_turns=1,
            session_id="session",
            usage={"output_tokens": counter},
        )

    monkeypatch.setattr(sdk, "query", query)
    monkeypatch.setattr(bridge, "_options", lambda *args, **kwargs: None)
    asyncio.run(
        bridge._run_claude({"prompt": "test", "commercial_mode": "subscription-only"})
    )
    assert (
        claude.parse_claude_stream(
            capsys.readouterr().out, sdk_bridge=True
        ).model_output_seen
        is None
    )


@pytest.mark.parametrize(
    "result,structured,usage",
    [
        ("partial", None, {}),
        (None, {}, {}),
        (None, None, {"output_tokens": 1}),
        (None, None, {"output_tokens": 0}),
    ],
)
def test_bridge_observes_before_discarding_result(
    monkeypatch, capsys, result, structured, usage
):
    import claude_agent_sdk as sdk

    async def query(**kwargs):
        yield sdk.ResultMessage(
            subtype="error_max_budget_usd",
            duration_ms=1,
            duration_api_ms=0,
            is_error=True,
            num_turns=1,
            session_id="session",
            result=result,
            structured_output=structured,
            usage=usage,
        )

    monkeypatch.setattr(sdk, "query", query)
    monkeypatch.setattr(bridge, "_options", lambda *args, **kwargs: None)
    asyncio.run(
        bridge._run_claude({"prompt": "test", "commercial_mode": "subscription-only"})
    )
    parsed = claude.parse_claude_stream(capsys.readouterr().out, sdk_bridge=True)
    assert parsed.model_output_seen is (
        result is not None
        or structured is not None
        or usage.get("output_tokens", 0) > 0
    )


def test_normalized_budget_cannot_erase_observation(monkeypatch):
    monkeypatch.setattr(
        claude,
        "get_settings",
        lambda: SimpleNamespace(budget=SimpleNamespace(max_tokens=0, max_usd=0)),
    )
    request = SimpleNamespace(
        requested_model="claude-fable-5-1",
        transport="claude-sdk",
        attempt_id="attempt",
        eligibility=contract.SubscriptionEligibility.AMBIGUOUS,
        commercial_mode=contract.RuntimeCommercialMode.SUBSCRIPTION_ONLY,
        fallback_from=None,
    )
    result = claude.ClaudeAdapter()._result(
        request,
        status=contract.RuntimeStatus.FAILED,
        reason=contract.TerminalReason.PROCESS_EXIT,
        usage=contract.RuntimeUsage(output_tokens=1),
        final_output="discarded",
        model_output_seen=False,
    )
    assert result.final_output is None
    assert result.model_output_seen is True


def test_unrecognized_sdk_message_prevents_absence_evidence(monkeypatch, capsys):
    import claude_agent_sdk as sdk

    async def query(**kwargs):
        yield object()
        yield sdk.ResultMessage(
            subtype="success",
            duration_ms=1,
            duration_api_ms=0,
            is_error=True,
            num_turns=1,
            session_id="session",
        )

    monkeypatch.setattr(sdk, "query", query)
    monkeypatch.setattr(bridge, "_options", lambda *args, **kwargs: None)
    asyncio.run(
        bridge._run_claude({"prompt": "test", "commercial_mode": "subscription-only"})
    )
    assert (
        claude.parse_claude_stream(
            capsys.readouterr().out, sdk_bridge=True
        ).model_output_seen
        is None
    )


@pytest.mark.parametrize("output", [None, "partial review"])
def test_merged_usage_limit_preserves_output_evidence(output, monkeypatch, tmp_path):
    from tests.unit.runners.test_claude_usage_limits import event, result, run

    _, frames = run([event(), result(output=output)], monkeypatch, tmp_path)
    parsed = claude.parse_claude_stream(
        "\n".join(json.dumps(frame) for frame in frames), sdk_bridge=True
    )
    assert parsed.status is contract.RuntimeStatus.LIMITED
    assert parsed.terminal_reason is contract.TerminalReason.USAGE_LIMIT
    assert parsed.usage_limit.scope is contract.RuntimeLimitScope.FIVE_HOUR
    assert parsed.model_output_seen is (output is not None)
