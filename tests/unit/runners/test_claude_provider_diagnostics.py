import asyncio
import json
from pathlib import Path

import pytest

from loopzero.runners import bridge, claude, contract


@pytest.mark.parametrize(
    "secret",
    [
        "sk-ant-secret123456789",
        "person@example.com",
        "/home/person/private.txt",
        "https://example.com/?token=private",
        "password=private",
        "access_token=private",
        "Bearer abcdefghijklmnop",
        "Authorization: Bearer abcdefghijklmnop",
        'password="private secret words"',
        r"C:\Users\person\private.txt",
    ],
)
def test_provider_error_redacts_sensitive_values(secret):
    detail = contract.sanitized_provider_error(f"Authentication failed: {secret}")
    assert secret not in detail
    assert "<redacted>" in detail
    assert len(contract.sanitized_provider_error("é" * 500)) <= 200


def test_failed_terminal_retains_bounded_errors_before_stop_reason():
    parsed = claude.parse_claude_stream(
        json.dumps(
            {
                "type": "result",
                "is_error": True,
                "subtype": "success",
                "stop_reason": "stop_sequence",
                "model_output_seen": False,
                "usage": {"input_tokens": 0, "output_tokens": 0},
                "errors": ["Rate limit exceeded", "password=private /home/person/data"],
            }
        )
    )
    assert parsed.status is contract.RuntimeStatus.FAILED
    assert parsed.terminal_reason is contract.TerminalReason.PROCESS_EXIT
    assert parsed.usage.output_tokens == 0
    assert parsed.diagnostics[0] == "Claude provider error: Rate limit exceeded"
    assert "private" not in str(parsed.diagnostics)
    assert "Claude request ended before observed model output" in parsed.diagnostics


def test_synthetic_error_is_not_model_activity(monkeypatch, tmp_path: Path, capsys):
    import claude_agent_sdk as sdk

    async def query(**kwargs):
        yield sdk.SystemMessage(subtype="init", data={"model": "claude-fable-5-1"})
        yield sdk.AssistantMessage(
            content=[sdk.TextBlock(text="Authentication failed password=private")],
            model="<synthetic>",
            error="authentication_failed",
        )
        yield sdk.ResultMessage(
            subtype="success",
            duration_ms=1,
            duration_api_ms=0,
            is_error=True,
            num_turns=1,
            session_id="session",
            stop_reason="stop_sequence",
            usage={"input_tokens": 0, "output_tokens": 0},
            errors=["Account unavailable"],
        )

    phases = []
    monkeypatch.setattr(sdk, "query", query)
    monkeypatch.setattr(bridge, "_options", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        bridge,
        "_observe_progress",
        lambda reporter, phase, **kwargs: phases.append(phase),
    )
    asyncio.run(
        bridge._run_claude(
            {"prompt": "private prompt", "commercial_mode": "subscription-only"}
        )
    )
    wire = capsys.readouterr().out
    assert "private" not in wire
    assert contract.RuntimePhase.MODEL_ACTIVE not in phases
    frames = [json.loads(line) for line in wire.splitlines()]
    assert frames[-1]["model_output_seen"] is False
    assert frames[-1]["errors"] == ["Account unavailable"]
    assert all(not frame.get("semantic") for frame in frames[:-1])
    parsed = claude.parse_claude_stream(wire)
    assert parsed.effective_model != "<synthetic>"
    assert any("authentication_failed" in detail for detail in parsed.diagnostics)
    assert parsed.terminal_reason is contract.TerminalReason.PROCESS_EXIT


def test_provider_rejection_prevents_retry_after_disconnect():
    parsed = claude.parse_claude_stream(
        json.dumps(
            {
                "type": "event",
                "kind": "assistant",
                "subtype": "error",
                "semantic": False,
                "detail": "authentication_failed",
            }
        )
        + "\n"
        + json.dumps({"type": "error", "reason": "disconnect"})
    )
    assert parsed.semantic_event is True  # Retry barrier, not inference proof.
    assert parsed.events[0].semantic is False
    assert parsed.terminal_reason is contract.TerminalReason.TRANSPORT_DISCONNECT


@pytest.mark.parametrize(
    "authorization",
    [
        "Authorization: Basic dXNlcjpwYXNz",
        "Authorization: Basic\r\n dXNlcjpwYXNz",
        "Authorization: Bearer short",
        'Authorization: Digest username="private", response="sensitive"',
        '"Authorization": "Basic dXNlcjpwYXNz"',
    ],
)
def test_authorization_value_never_reaches_wire_or_diagnostics(
    authorization, monkeypatch, capsys
):
    import claude_agent_sdk as sdk

    async def query(**kwargs):
        yield sdk.AssistantMessage(
            content=[sdk.TextBlock(text=f"Authentication failed: {authorization}")],
            model="<synthetic>",
            error="authentication_failed",
        )
        yield sdk.ResultMessage(
            subtype="success",
            duration_ms=1,
            duration_api_ms=0,
            is_error=True,
            num_turns=1,
            session_id="session",
            errors=[f"Provider rejected {authorization}"],
        )

    monkeypatch.setattr(sdk, "query", query)
    monkeypatch.setattr(bridge, "_options", lambda *args, **kwargs: None)
    asyncio.run(
        bridge._run_claude({"prompt": "test", "commercial_mode": "subscription-only"})
    )
    wire = capsys.readouterr().out
    parsed = claude.parse_claude_stream(wire)
    hostile_terminal = claude.parse_claude_stream(
        json.dumps(
            {
                "type": "result",
                "is_error": True,
                "errors": [authorization],
            }
        )
    )
    for secret in ("dXNlcjpwYXNz", "short", "private", "sensitive"):
        assert secret not in wire
        assert secret not in str(parsed.diagnostics)
        assert secret not in str(hostile_terminal.diagnostics)


@pytest.mark.parametrize(
    "evidence", ["none", "assistant", "stream", "tool", "structured", "usage", "partial_result"]
)
def test_typed_authentication_diagnostic_echo_is_not_model_output(
    monkeypatch, capsys, evidence
):
    """Constructed SDK shape; historical failures did not retain their raw stream."""
    import claude_agent_sdk as sdk

    diagnostic = "Not logged in · Please run /login"

    async def query(**kwargs):
        if evidence == "assistant":
            yield sdk.AssistantMessage(content=[sdk.TextBlock(text="Actual partial answer")], model="claude-fable-5-1")
        elif evidence == "stream":
            yield sdk.StreamEvent(uuid="event", session_id="session", event={
                "type": "content_block_delta", "delta": {"type": "text_delta", "text": "Partial"},
            })
        elif evidence == "tool":
            yield sdk.AssistantMessage(content=[sdk.ToolUseBlock(id="tool", name="Read", input={})], model="claude-fable-5-1")
        yield sdk.AssistantMessage(
            content=[sdk.TextBlock(text=diagnostic)], model="<synthetic>",
            error="authentication_failed",
        )
        yield sdk.ResultMessage(
            subtype="success", duration_ms=1, duration_api_ms=0, is_error=True,
            num_turns=1, session_id="session", stop_reason="stop_sequence",
            result="Actual partial result" if evidence == "partial_result" else diagnostic,
            structured_output={} if evidence == "structured" else None,
            usage={"input_tokens": 0, "output_tokens": 1 if evidence == "usage" else 0},
            errors=["Authentication failed"],
        )

    monkeypatch.setattr(sdk, "query", query)
    monkeypatch.setattr(bridge, "_options", lambda *args, **kwargs: None)
    asyncio.run(bridge._run_claude({"prompt": "test", "commercial_mode": "subscription-only"}))
    wire = capsys.readouterr().out
    frame = json.loads(wire.splitlines()[-1])
    assert frame["model_output_seen"] is (evidence != "none")
    parsed = claude.parse_claude_stream(wire, sdk_bridge=True)
    assert parsed.model_output_seen is (evidence != "none")
    assert parsed.semantic_event is True  # The provider request still occurred.
    assert parsed.status is contract.RuntimeStatus.FAILED


@pytest.mark.parametrize("model,error,is_error", [
    ("<synthetic>", "unknown", True),
    ("claude-fable-5-1", "authentication_failed", True),
    ("<synthetic>", "authentication_failed", False),
])
def test_unknown_or_successful_diagnostic_shaped_result_still_counts_output(
    monkeypatch, capsys, model, error, is_error
):
    import claude_agent_sdk as sdk
    async def query(**kwargs):
        yield sdk.AssistantMessage(content=[sdk.TextBlock(text="Not logged in")], model=model, error=error)
        yield sdk.ResultMessage(
            subtype="success", duration_ms=1, duration_api_ms=0, is_error=is_error,
            num_turns=1, session_id="session", result="Not logged in",
            usage={"output_tokens": 0},
        )
    monkeypatch.setattr(sdk, "query", query)
    monkeypatch.setattr(bridge, "_options", lambda *args, **kwargs: None)
    asyncio.run(bridge._run_claude({"prompt": "test", "commercial_mode": "subscription-only"}))
    assert json.loads(capsys.readouterr().out.splitlines()[-1])["model_output_seen"] is True
