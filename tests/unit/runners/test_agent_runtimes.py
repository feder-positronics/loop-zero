import asyncio
import base64
import fcntl
import json
import os
import select
import signal
import subprocess
import sys
import threading
import time
from contextlib import contextmanager
from dataclasses import fields, replace
from pathlib import Path
from types import ModuleType
from unittest.mock import MagicMock

import pytest
from loopzero.runners.settings import RuntimeSettings


def load_package() -> (
    tuple[
        ModuleType,
        ModuleType,
        ModuleType,
        ModuleType,
        ModuleType,
        ModuleType,
        ModuleType,
    ]
):
    import loopzero.runners.claude as claude
    import loopzero.runners.codex as codex
    import loopzero.runners.contract as contracts
    import loopzero.runners.cursor as cursor
    import loopzero.runners.process as process
    import loopzero.runners.registry as registry
    import loopzero.runners.bridge as sdk_bridge

    return contracts, claude, codex, cursor, process, registry, sdk_bridge


contracts, claude, codex, cursor, process, registry, sdk_bridge = load_package()


def run_cli_unsandboxed(*args, **kwargs):
    """Exercise process mechanics without pretending the test owns a sandbox."""
    kwargs["unsandboxed"] = True
    kwargs["unsandboxed_reason"] = "unit test exercises process ownership mechanics"
    return process.run_cli(*args, **kwargs)


def test_result_failure_telemetry_vocabularies_are_closed_and_content_free() -> None:
    assert tuple(contracts.ModelResultReason) == (
        contracts.ModelResultReason.WORKER_REPORTED_FAILURE,
        contracts.ModelResultReason.MISSING_STRUCTURED_OUTPUT,
        contracts.ModelResultReason.POSTHOC_EXTRACTION_FAILURE,
        contracts.ModelResultReason.SCHEMA_SHAPE_OR_BOUNDS,
        contracts.ModelResultReason.TASK_ID_MISMATCH,
        contracts.ModelResultReason.UNSUPPORTED_STATUS,
    )
    assert tuple(contracts.ResultEnforcement) == (
        contracts.ResultEnforcement.NATIVE_SCHEMA,
        contracts.ResultEnforcement.POSTHOC_EXTRACTION,
    )
    assert "private" not in repr(contracts.ModelResultReason)
    assert "prompt" not in repr(contracts.ResultEnforcement)


class _ProgressRecorder:
    def __init__(self) -> None:
        self.phases: list[object] = []
        self.observations: list[tuple[object, object]] = []
        self.activity_count = 0

    def observe(self, phase: object, *, last_tool: object = None) -> None:
        self.activity_count += 1
        self.observations.append((phase, last_tool))
        if not self.phases or self.phases[-1] is not phase:
            self.phases.append(phase)

    def activity(self) -> None:
        self.activity_count += 1


def governed_schema(task_id: str) -> dict[str, object]:
    return {
        "type": "object",
        "properties": {
            "task_id": {"type": "string", "const": task_id},
            "status": {"type": "string", "enum": ["completed"]},
            "summary": {"type": "string", "maxLength": 100},
        },
        "required": ["task_id", "status", "summary"],
        "additionalProperties": False,
    }


def request(
    tmp_path: Path,
    *,
    eligibility: object = contracts.SubscriptionEligibility.APPROVED,
) -> object:
    return contracts.RuntimeRequest(
        vendor="cursor",
        transport="cursor/cli-stream-json",
        requested_model="cursor-auto",
        effort="high",
        prompt="this must never be retained as telemetry",
        cwd=tmp_path,
        timeout_s=5,
        read_only=True,
        attempt_id="attempt-1",
        eligibility=eligibility,
    )


def claude_request(
    tmp_path: Path,
    *,
    read_only: bool = True,
    commercial_mode: object = None,
) -> object:
    if commercial_mode is None:
        commercial_mode = contracts.RuntimeCommercialMode.SUBSCRIPTION_ONLY
    return contracts.RuntimeRequest(
        vendor="claude",
        transport="claude/agent-sdk",
        requested_model="claude-opus-5",
        effort="high",
        prompt="this prompt must remain private",
        cwd=tmp_path,
        timeout_s=5,
        read_only=read_only,
        attempt_id="attempt-claude",
        eligibility=contracts.SubscriptionEligibility.APPROVED,
        commercial_mode=commercial_mode,
        budget_usd=3.25,
        output_schema=governed_schema("attempt-claude"),
        capability_profile=contracts.RuntimeCapabilityProfile(
            read_roots=(tmp_path.resolve(),),
        ),
    )


def scoped_claude_request(tmp_path: Path) -> object:
    return contracts.RuntimeRequest(
        vendor="claude",
        transport="claude/agent-sdk",
        requested_model="claude-opus-5",
        effort="high",
        prompt="this prompt must remain private",
        cwd=tmp_path,
        timeout_s=5,
        read_only=True,
        attempt_id="attempt-claude-scoped",
        eligibility=contracts.SubscriptionEligibility.APPROVED,
        budget_usd=3.25,
        visible_tools=("Read", "Bash"),
        allowed_tools=(
            "Read(/evidence/**)",
            "Bash(sha256sum -- evidence/corpus.json)",
        ),
    )


def codex_request(tmp_path: Path, *, read_only: bool = True) -> object:
    return contracts.RuntimeRequest(
        vendor="codex",
        transport="codex/app-server",
        requested_model="gpt-5.6-sol",
        effort="high",
        prompt="this codex prompt must remain private",
        cwd=tmp_path,
        timeout_s=5,
        read_only=read_only,
        attempt_id="attempt-codex",
        eligibility=contracts.SubscriptionEligibility.APPROVED,
        output_schema=governed_schema("attempt-codex"),
        capability_profile=contracts.RuntimeCapabilityProfile(
            read_roots=(tmp_path.resolve(),),
            write_roots=(() if read_only else (tmp_path.resolve(),)),
        ),
    )


def test_contracts_are_immutable_and_exclude_prompt_from_repr(tmp_path: Path) -> None:
    runtime_request = request(tmp_path)

    with pytest.raises((AttributeError, TypeError)):
        runtime_request.timeout_s = 1

    assert "this must never be retained as telemetry" not in repr(runtime_request)
    assert contracts.RuntimeStatus.COMPLETED.value == "completed"
    assert (
        runtime_request.commercial_mode
        is contracts.RuntimeCommercialMode.SUBSCRIPTION_ONLY
    )


def test_runtime_progress_contract_is_closed_content_free_and_strict() -> None:
    assert contracts.RUNTIME_PROGRESS_PROTOCOL_VERSION == 1
    assert {phase.value for phase in contracts.RuntimePhase} == {
        "startup",
        "model-active",
        "tool-execution",
        "result-packaging",
    }
    assert {signal.value for signal in contracts.RuntimeProgressSignal} == {
        "transition",
        "heartbeat",
    }
    assert {tool.value for tool in contracts.RuntimeToolLabel} == {
        "command",
        "file",
        "search",
        "web",
        "tool",
    }

    progress = contracts.parse_runtime_progress_frame(
        {
            "version": 1,
            "phase": "model-active",
            "signal": "transition",
            "sequence": 7,
            "activity_age_s": 0.25,
            "last_tool": "command",
        }
    )

    assert [item.name for item in fields(progress)] == [
        "phase",
        "signal",
        "sequence",
        "activity_age_s",
        "last_tool",
    ]
    assert progress.phase is contracts.RuntimePhase.MODEL_ACTIVE
    assert progress.signal is contracts.RuntimeProgressSignal.TRANSITION
    assert progress.last_tool is contracts.RuntimeToolLabel.COMMAND
    assert "private prompt /secret/path tool-input model-output" not in repr(progress)
    with pytest.raises((AttributeError, TypeError)):
        progress.sequence = 8


@pytest.mark.parametrize(
    "override",
    [
        {"version": 2},
        {"version": True},
        {"phase": "reasoning"},
        {"signal": "update"},
        {"sequence": 0},
        {"sequence": True},
        {"activity_age_s": -0.1},
        {"activity_age_s": float("inf")},
        {"activity_age_s": True},
        {"last_tool": "DangerousMcpTool /private/path"},
        {"last_tool": True},
        {"unexpected": "private"},
    ],
)
def test_runtime_progress_parser_rejects_invalid_or_extra_fields(override) -> None:
    frame = {
        "version": 1,
        "phase": "startup",
        "signal": "transition",
        "sequence": 1,
        "activity_age_s": 0.0,
    }
    frame.update(override)

    with pytest.raises(contracts.RuntimeProgressProtocolError):
        contracts.parse_runtime_progress_frame(frame)


@pytest.mark.parametrize(
    ("adapter_factory", "request_factory", "executable", "expected_transport"),
    [
        (
            claude.ClaudeAdapter,
            claude_request,
            "/usr/bin/claude",
            claude.CLAUDE_SDK_TRANSPORT,
        ),
        (
            codex.CodexAdapter,
            codex_request,
            "/usr/bin/codex",
            codex.CODEX_SDK_TRANSPORT,
        ),
    ],
)
def test_sdk_probe_resolves_environment_from_tooling_root(
    tmp_path: Path,
    adapter_factory,
    request_factory,
    executable: str,
    expected_transport: str,
) -> None:
    snapshot = tmp_path / "snapshot"
    snapshot.mkdir()
    tooling_root = tmp_path / "live-worktree"
    python = tooling_root / "fastapi_backend/.venv/bin/python"
    python.parent.mkdir(parents=True)
    python.write_text("", encoding="utf-8")
    python.chmod(0o755)
    observed: list[Path] = []

    def sdk_available(candidate: Path, _cwd: Path) -> bool:
        observed.append(candidate)
        return True

    auth_result = (
        _codex_auth_process_result()
        if adapter_factory is codex.CodexAdapter
        else _auth_process_result()
    )

    def run_probe(command, **_kwargs):
        if adapter_factory is codex.CodexAdapter and command[0] == str(python):
            return process.ProcessResult(
                returncode=0,
                stdout='{"type":"readiness","status":"ready"}\n',
                stderr="private vendor output",
                duration_s=0.01,
                timed_out=False,
            )
        return auth_result

    adapter = adapter_factory(
        run_probe=run_probe,
        sdk_available=sdk_available,
        which=lambda _name: executable,
    )
    runtime_request = replace(
        request_factory(snapshot),
        tooling_root=tooling_root,
    )

    selected_request, readiness = adapter.select_transport(runtime_request)

    assert readiness.ready is True
    assert selected_request is not None
    assert selected_request.transport == expected_transport
    assert observed == [python]


@pytest.mark.parametrize(
    ("adapter", "request_factory"),
    [
        (claude.ClaudeAdapter, claude_request),
        (codex.CodexAdapter, codex_request),
    ],
)
def test_sdk_launch_resolves_bridge_from_tooling_root(
    tmp_path: Path, adapter, request_factory
) -> None:
    snapshot = tmp_path / "snapshot"
    snapshot.mkdir()
    tooling_root = tmp_path / "live-worktree"
    python = tooling_root / "fastapi_backend/.venv/bin/python"
    python.parent.mkdir(parents=True)
    python.write_text("", encoding="utf-8")
    observed: list[list[str]] = []

    def run_process(command, **_kwargs):
        observed.append(command)
        return process.ProcessResult(
            returncode=0,
            stdout=(
                '{"type":"result","status":"completed",'
                '"terminal_reason":"completed"}\n'
            ),
            stderr="",
            duration_s=0.01,
            timed_out=False,
        )

    runtime_request = replace(
        request_factory(snapshot),
        tooling_root=tooling_root,
    )

    adapter(run_process=run_process)._run_sdk(runtime_request)

    assert observed[0][0] == str(python)


@pytest.mark.parametrize(
    ("transport", "launch_method"),
    [
        (claude.CLAUDE_SDK_TRANSPORT, "_run_sdk"),
        (claude.CLAUDE_CLI_TRANSPORT, "_run_cli"),
    ],
)
def test_claude_launch_validates_runtime_cache_against_outer_write_root(
    tmp_path: Path,
    transport: str,
    launch_method: str,
) -> None:
    snapshot = tmp_path / "snapshot"
    snapshot.mkdir()
    runtime_root = tmp_path / "runtime-cache"
    cache_environment = tuple(
        (name, str(runtime_root / child))
        for name, child in (
            ("UV_CACHE_DIR", "uv"),
            ("XDG_CACHE_HOME", "xdg"),
            ("TMPDIR", "tmp"),
        )
    )
    observed_environment: list[dict[str, str]] = []

    def run_process(_command, **kwargs):
        observed_environment.append(kwargs["env"])
        return process.ProcessResult(
            returncode=0,
            stdout=(
                '{"type":"result","status":"completed",'
                '"terminal_reason":"completed"}\n'
            ),
            stderr="",
            duration_s=0.01,
            timed_out=False,
        )

    runtime_request = replace(
        claude_request(snapshot),
        transport=transport,
        capability_profile=contracts.RuntimeCapabilityProfile(
            read_roots=(snapshot,),
            write_roots=(runtime_root,),
            environment=cache_environment,
        ),
    )

    getattr(claude.ClaudeAdapter(run_process=run_process), launch_method)(
        runtime_request
    )

    assert len(observed_environment) == 1
    assert {
        name: observed_environment[0][name] for name, _value in cache_environment
    } == dict(cache_environment)


def test_runtime_contract_keeps_schema_and_structured_result_private(
    tmp_path: Path,
) -> None:
    runtime_request = claude_request(tmp_path)
    result = contracts.RuntimeResult(
        vendor="claude",
        transport="claude/agent-sdk",
        requested_model="claude-opus-5",
        status=contracts.RuntimeStatus.COMPLETED,
        terminal_reason=contracts.TerminalReason.COMPLETED,
        attempt_id="attempt-claude",
        structured_output={"summary": "private result"},
        cost_status=contracts.RuntimeCostStatus.ESTIMATED,
    )

    assert "properties" not in repr(runtime_request)
    assert "private result" not in repr(result)
    assert result.cost_status is contracts.RuntimeCostStatus.ESTIMATED


def test_process_result_excludes_vendor_output_from_repr() -> None:
    result = process.ProcessResult(
        returncode=1,
        stdout="private vendor stdout",
        stderr="private vendor stderr",
        duration_s=0.01,
        timed_out=False,
    )

    assert "private vendor stdout" not in repr(result)
    assert "private vendor stderr" not in repr(result)


def test_runtime_result_caps_separate_semantic_and_wire_byte_budgets() -> None:
    assert contracts.MAX_STRUCTURED_OUTPUT_BYTES == 8 * 1024 * 1024
    assert contracts.MAX_PROTOCOL_LINE_BYTES == (
        6 * contracts.MAX_STRUCTURED_OUTPUT_BYTES + 64 * 1024
    )
    assert process.DEFAULT_STDOUT_LIMIT_BYTES > contracts.MAX_PROTOCOL_LINE_BYTES
    assert {
        claude.MAX_FINAL_OUTPUT_BYTES,
        codex.MAX_FINAL_OUTPUT_BYTES,
        cursor.MAX_FINAL_OUTPUT_BYTES,
        sdk_bridge.MAX_FINAL_OUTPUT_BYTES,
    } == {contracts.MAX_STRUCTURED_OUTPUT_BYTES}
    assert {
        claude.MAX_BRIDGE_LINE_BYTES,
        codex.MAX_BRIDGE_LINE_BYTES,
        sdk_bridge.MAX_BRIDGE_LINE_BYTES,
    } == {contracts.MAX_PROTOCOL_LINE_BYTES}


def test_isolated_python_probe_ignores_worktree_and_pythonpath_modules(
    tmp_path: Path,
) -> None:
    marker = tmp_path / "shadow-imported"
    (tmp_path / "json.py").write_text(
        f"from pathlib import Path\nPath({str(marker)!r}).write_text('json')\n",
        encoding="utf-8",
    )
    (tmp_path / "sitecustomize.py").write_text(
        f"from pathlib import Path\nPath({str(marker)!r}).write_text('site')\n",
        encoding="utf-8",
    )

    available = process.isolated_python_import_available(
        Path(sys.executable),
        "json",
        timeout_s=5,
        env={**os.environ, "PYTHONPATH": str(tmp_path)},
        unsandboxed=True,
        unsandboxed_reason="unit test exercises isolated import mechanics",
    )

    assert available is True
    assert not marker.exists()


def test_native_runtime_registry_is_closed_and_constructs_supported_adapters() -> None:
    native_registry = registry.NATIVE_RUNTIME_REGISTRY

    assert native_registry.vendors == frozenset({"claude", "codex", "cursor"})
    assert native_registry.registration("claude").preferred_transport == (
        "claude/agent-sdk"
    )
    assert native_registry.registration("codex").cli_fallback_transport == ("codex/cli")
    assert isinstance(native_registry.create("cursor"), cursor.CursorAdapter)

    with pytest.raises(ValueError, match="unsupported native runtime"):
        native_registry.create("kimi")


def test_claude_auth_status_requires_subscription_backed_claude_ai_login() -> None:
    parsed = claude.parse_claude_auth_status(
        json.dumps(
            {
                "loggedIn": True,
                "authMethod": "claude.ai",
                "subscriptionType": "max",
                "email": "private@example.invalid",
                "orgName": "private-org",
            }
        )
    )

    assert parsed.logged_in is True
    assert parsed.auth_method == "claude.ai"
    assert parsed.has_subscription is True
    assert "private@example.invalid" not in repr(parsed)
    assert "private-org" not in repr(parsed)


@pytest.mark.parametrize(
    "status",
    [
        {"loggedIn": False, "authMethod": "claude.ai", "subscriptionType": "max"},
        {"loggedIn": True, "authMethod": "apiKey", "subscriptionType": "max"},
        {"loggedIn": True, "authMethod": "claude.ai", "subscriptionType": ""},
        {"loggedIn": True, "authMethod": "claude.ai", "subscriptionType": "free"},
    ],
)
def test_claude_auth_status_rejects_non_subscription_paths(
    status: dict[str, object],
) -> None:
    if status["subscriptionType"] in claude.ELIGIBLE_SUBSCRIPTION_TYPES:
        parsed = claude.parse_claude_auth_status(json.dumps(status))
        assert parsed.logged_in is False or parsed.auth_method != "claude.ai"
    else:
        with pytest.raises(claude.ClaudeProtocolError):
            claude.parse_claude_auth_status(json.dumps(status))


def test_claude_stream_normalizes_usage_events_and_keeps_output_private() -> None:
    parsed = claude.parse_claude_stream(
        "\n".join(
            [
                json.dumps(
                    {
                        "type": "event",
                        "kind": "system",
                        "subtype": "init",
                        "session_id": "session-claude",
                        "effective_model": "claude-opus-5",
                    }
                ),
                json.dumps(
                    {
                        "type": "event",
                        "kind": "assistant",
                        "subtype": "message",
                        "semantic": True,
                    }
                ),
                json.dumps(
                    {
                        "type": "result",
                        "status": "completed",
                        "terminal_reason": "completed",
                        "session_id": "session-claude",
                        "usage": {"input_tokens": 0, "output_tokens": 12},
                        "total_cost_usd": 0.42,
                        "stop_reason": "end_turn",
                        "api_error_status": 429,
                        "permission_denial_count": 2,
                        "permission_denial_tools": ["Read", "Grep"],
                        "structured_output": {
                            "task_id": "attempt-claude",
                            "status": "completed",
                            "summary": "private governed output",
                        },
                    }
                ),
            ]
        )
    )

    assert parsed.status is contracts.RuntimeStatus.COMPLETED
    assert parsed.session_id == "session-claude"
    assert parsed.effective_model == "claude-opus-5"
    assert parsed.usage == contracts.RuntimeUsage(input_tokens=0, output_tokens=12)
    assert parsed.cost_usd == 0.42
    assert parsed.diagnostics == (
        "Claude stop reason: end_turn",
        "Claude API error status: 429",
        "Claude permission denials: 2: Read, Grep",
    )
    assert parsed.structured_output == {
        "task_id": "attempt-claude",
        "status": "completed",
        "summary": "private governed output",
    }
    assert "private governed output" not in repr(parsed)
    assert all(
        event.kind in {"system", "assistant", "result"} for event in parsed.events
    )


@pytest.mark.parametrize(
    "terminal",
    [
        {
            "status": "completed",
            "terminal_reason": "completed",
            "structured_output": {"status": "completed"},
            "structured_output_recovery": "unknown",
        },
        {
            "status": "completed",
            "terminal_reason": "completed",
            "structured_output_recovery": "accepted-tool-result",
        },
        {
            "status": "failed",
            "terminal_reason": "process-exit",
            "structured_output": {"status": "completed"},
            "structured_output_recovery": "accepted-tool-result",
        },
    ],
)
def test_claude_stream_rejects_invalid_structured_output_recovery_metadata(
    terminal: dict[str, object],
) -> None:
    with pytest.raises(
        claude.ClaudeProtocolError,
        match="structured-output recovery metadata",
    ) as error:
        claude.parse_claude_stream(json.dumps({"type": "result", **terminal}))

    assert error.value.semantic_event is True


def test_claude_stream_classifies_budget_exhaustion_without_raw_error() -> None:
    parsed = claude.parse_claude_stream(
        json.dumps(
            {
                "type": "result",
                "status": "failed",
                "terminal_reason": "budget-exhausted",
                "subtype": "error_max_budget_usd",
                "result": "private vendor error",
                "total_cost_usd": 5.1,
            }
        )
    )

    assert parsed.status is contracts.RuntimeStatus.FAILED
    assert parsed.terminal_reason is contracts.TerminalReason.BUDGET_EXHAUSTED
    assert parsed.cost_usd == 5.1
    assert parsed.diagnostics == ("Claude budget exhausted",)
    assert "private vendor error" not in repr(parsed)


def test_claude_cli_recovers_structured_output_at_budget_terminal() -> None:
    governed_output = {
        "task_id": "attempt-claude",
        "status": "completed",
        "summary": "done",
    }
    parsed = claude.parse_claude_stream(
        json.dumps(
            {
                "type": "result",
                "subtype": "error_max_budget_usd",
                "is_error": True,
                "structured_output": governed_output,
                "total_cost_usd": 5.1,
            }
        )
    )

    assert parsed.status is contracts.RuntimeStatus.COMPLETED
    assert (
        parsed.terminal_reason is contracts.TerminalReason.BUDGET_EXHAUSTED_AFTER_RESULT
    )
    assert parsed.structured_output == governed_output
    assert parsed.diagnostics == (
        "Claude budget exhausted after accepted structured output",
    )


@pytest.mark.parametrize(
    ("status", "structured_output", "expected_status", "expected_reason"),
    [
        (
            "completed",
            None,
            contracts.RuntimeStatus.FAILED,
            contracts.TerminalReason.BUDGET_EXHAUSTED,
        ),
        (
            "failed",
            {
                "task_id": "attempt-claude",
                "status": "completed",
                "summary": "done",
            },
            contracts.RuntimeStatus.COMPLETED,
            contracts.TerminalReason.BUDGET_EXHAUSTED_AFTER_RESULT,
        ),
    ],
)
def test_claude_budget_subtype_owns_terminal_status_integrity(
    status,
    structured_output,
    expected_status,
    expected_reason,
) -> None:
    parsed = claude.parse_claude_stream(
        json.dumps(
            {
                "type": "result",
                "status": status,
                "terminal_reason": "completed",
                "subtype": "error_max_budget_usd",
                "structured_output": structured_output,
            }
        )
    )

    assert parsed.status is expected_status
    assert parsed.terminal_reason is expected_reason


@pytest.mark.parametrize(
    ("status", "reason"),
    [
        ("completed", "process-exit"),
        ("failed", "completed"),
        ("completed", "budget-exhausted-after-result"),
    ],
)
def test_claude_stream_rejects_inconsistent_non_budget_terminals(
    status: str,
    reason: str,
) -> None:
    with pytest.raises(claude.ClaudeProtocolError, match="terminal") as error:
        claude.parse_claude_stream(
            json.dumps(
                {
                    "type": "result",
                    "status": status,
                    "terminal_reason": reason,
                }
            )
        )
    assert error.value.semantic_event is True


@pytest.mark.parametrize(
    ("status", "reason"),
    [
        ("completed", "process-exit"),
        ("failed", "completed"),
        ("failed", "budget-exhausted"),
    ],
)
def test_codex_stream_rejects_inconsistent_terminals_as_semantic(
    status: str,
    reason: str,
) -> None:
    with pytest.raises(codex.CodexProtocolError, match="terminal") as error:
        codex.parse_codex_stream(
            json.dumps(
                {
                    "type": "result",
                    "status": status,
                    "terminal_reason": reason,
                }
            )
        )
    assert error.value.semantic_event is True


@pytest.mark.parametrize(
    ("parser", "reason", "expected"),
    [
        (
            claude.parse_claude_stream,
            "startup",
            contracts.TerminalReason.STARTUP_FAILURE,
        ),
        (
            claude.parse_claude_stream,
            "protocol",
            contracts.TerminalReason.PROTOCOL_FAILURE,
        ),
        (
            claude.parse_claude_stream,
            "disconnect",
            contracts.TerminalReason.TRANSPORT_DISCONNECT,
        ),
        (
            codex.parse_codex_stream,
            "startup",
            contracts.TerminalReason.STARTUP_FAILURE,
        ),
        (
            codex.parse_codex_stream,
            "protocol",
            contracts.TerminalReason.PROTOCOL_FAILURE,
        ),
        (
            codex.parse_codex_stream,
            "disconnect",
            contracts.TerminalReason.TRANSPORT_DISCONNECT,
        ),
    ],
)
def test_native_terminal_error_frames_normalize_without_a_later_result(
    parser,
    reason,
    expected,
) -> None:
    parsed = parser(json.dumps({"type": "error", "reason": reason}))

    assert parsed.status is contracts.RuntimeStatus.FAILED
    assert parsed.terminal_reason is expected
    assert parsed.semantic_event is False
    assert all("malformed" not in diagnostic for diagnostic in parsed.diagnostics)


@pytest.mark.parametrize(
    "parser", [claude.parse_claude_stream, codex.parse_codex_stream]
)
def test_native_terminal_error_frames_preserve_prior_semantic_work(parser) -> None:
    parsed = parser(
        "\n".join(
            [
                json.dumps(
                    {
                        "type": "event",
                        "kind": "assistant",
                        "semantic": True,
                    }
                ),
                json.dumps({"type": "error", "reason": "disconnect"}),
            ]
        )
    )

    assert parsed.terminal_reason is contracts.TerminalReason.TRANSPORT_DISCONNECT
    assert parsed.semantic_event is True


@pytest.mark.parametrize(
    ("parser", "error_type", "vendor"),
    [
        (claude.parse_claude_stream, claude.ClaudeProtocolError, "Claude"),
        (codex.parse_codex_stream, codex.CodexProtocolError, "Codex"),
    ],
)
def test_native_stream_structured_output_has_an_independent_byte_ceiling(
    parser,
    error_type,
    vendor,
) -> None:
    empty_size = len(
        json.dumps(
            {"payload": ""},
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
    )
    at_limit = {
        "payload": "x" * (claude.MAX_FINAL_OUTPUT_BYTES - empty_size),
    }
    accepted = parser(
        json.dumps(
            {
                "type": "result",
                "status": "completed",
                "terminal_reason": "completed",
                "structured_output": at_limit,
            },
            ensure_ascii=False,
            separators=(",", ":"),
        )
    )
    assert accepted.structured_output == at_limit

    with pytest.raises(error_type, match=f"{vendor} structured output exceeds"):
        parser(
            json.dumps(
                {
                    "type": "result",
                    "status": "completed",
                    "terminal_reason": "completed",
                    "structured_output": {
                        "payload": "x"
                        * (claude.MAX_FINAL_OUTPUT_BYTES - empty_size + 1)
                    },
                },
                ensure_ascii=False,
                separators=(",", ":"),
            )
        )


@pytest.mark.parametrize(
    ("parser", "error_type", "vendor"),
    [
        (claude.parse_claude_stream, claude.ClaudeProtocolError, "Claude"),
        (codex.parse_codex_stream, codex.CodexProtocolError, "Codex"),
    ],
)
def test_native_stream_rejects_non_utf8_structured_output_as_semantic(
    parser,
    error_type,
    vendor,
) -> None:
    with pytest.raises(
        error_type, match=f"{vendor} structured output was not valid UTF-8"
    ) as error:
        parser(
            json.dumps(
                {
                    "type": "result",
                    "status": "completed",
                    "terminal_reason": "completed",
                    "structured_output": {"summary": "\ud800"},
                }
            )
        )
    assert error.value.semantic_event is True


@pytest.mark.parametrize(
    ("parser", "error_type", "vendor"),
    [
        (claude.parse_claude_stream, claude.ClaudeProtocolError, "Claude"),
        (codex.parse_codex_stream, codex.CodexProtocolError, "Codex"),
    ],
)
def test_native_stream_rejects_non_utf8_terminal_text_as_semantic(
    parser,
    error_type,
    vendor,
) -> None:
    with pytest.raises(
        error_type, match=f"{vendor} terminal output was not valid UTF-8"
    ) as error:
        parser(
            json.dumps(
                {
                    "type": "result",
                    "status": "completed",
                    "terminal_reason": "completed",
                    "result": "\ud800",
                }
            )
        )
    assert error.value.semantic_event is True


def test_claude_sdk_recovers_result_accepted_before_budget_terminal(
    monkeypatch, tmp_path: Path, capsys
) -> None:
    import claude_agent_sdk
    from claude_agent_sdk import (
        AssistantMessage,
        ResultMessage,
        ToolResultBlock,
        ToolUseBlock,
        UserMessage,
    )

    governed_output = {
        "task_id": "attempt-claude",
        "status": "completed",
        "summary": "done",
    }

    async def fake_query(**_kwargs):
        yield AssistantMessage(
            content=[
                ToolUseBlock(
                    id="structured-1",
                    name="StructuredOutput",
                    input=governed_output,
                )
            ],
            model="claude-opus-5",
            session_id="session-claude",
        )
        yield UserMessage(
            content=[
                ToolResultBlock(
                    tool_use_id="structured-1",
                    content="Structured output provided successfully",
                    is_error=False,
                )
            ]
        )
        yield ResultMessage(
            subtype="error_max_budget_usd",
            duration_ms=100,
            duration_api_ms=80,
            is_error=True,
            num_turns=2,
            session_id="session-claude",
            stop_reason="tool_use",
            total_cost_usd=5.1,
            result="private budget error",
            structured_output=None,
        )

    monkeypatch.setattr(claude_agent_sdk, "query", fake_query)

    asyncio.run(
        sdk_bridge._run_claude(
            {
                "vendor": "claude",
                "prompt": "private prompt",
                "cwd": str(tmp_path),
                "requested_model": "claude-opus-5",
                "effort": "high",
                "read_only": True,
                "budget_usd": 5.0,
                "commercial_mode": "promotional-credit",
                "output_schema": governed_schema("attempt-claude"),
                "read_roots": [str(tmp_path.resolve())],
            }
        )
    )

    frames = [
        json.loads(line)
        for line in capsys.readouterr().out.splitlines()
        if line.strip()
    ]
    terminal = frames[-1]
    assert terminal["status"] == "completed"
    assert terminal["terminal_reason"] == "budget-exhausted-after-result"
    assert terminal["structured_output"] == governed_output
    assert terminal["subtype"] == "error_max_budget_usd"
    assert terminal["total_cost_usd"] == 5.1
    assert all("private budget error" not in json.dumps(frame) for frame in frames)


def test_claude_sdk_recovers_accepted_result_omitted_from_success_terminal(
    monkeypatch, tmp_path: Path, capsys
) -> None:
    import claude_agent_sdk
    from claude_agent_sdk import (
        AssistantMessage,
        ResultMessage,
        ToolResultBlock,
        ToolUseBlock,
        UserMessage,
    )

    governed_output = {
        "task_id": "attempt-claude",
        "status": "completed",
        "summary": "done",
    }

    async def fake_query(**_kwargs):
        yield AssistantMessage(
            content=[
                ToolUseBlock(
                    id="denied-read",
                    name="Read",
                    input={"file_path": "/outside/evidence.txt"},
                )
            ],
            model="claude-opus-5",
            session_id="session-claude",
        )
        yield UserMessage(
            content=[
                ToolResultBlock(
                    tool_use_id="denied-read",
                    content="denied",
                    is_error=True,
                )
            ]
        )
        yield AssistantMessage(
            content=[
                ToolUseBlock(
                    id="structured-1",
                    name="StructuredOutput",
                    input=governed_output,
                )
            ],
            model="claude-opus-5",
            session_id="session-claude",
        )
        yield UserMessage(
            content=[
                ToolResultBlock(
                    tool_use_id="structured-1",
                    content="Structured output provided successfully",
                    is_error=False,
                )
            ]
        )
        yield ResultMessage(
            subtype="success",
            duration_ms=100,
            duration_api_ms=80,
            is_error=False,
            num_turns=3,
            session_id="session-claude",
            stop_reason="tool_use",
            total_cost_usd=0.1,
            result="",
            structured_output=None,
            permission_denials=[{"tool_name": "Read"}],
        )

    monkeypatch.setattr(claude_agent_sdk, "query", fake_query)

    asyncio.run(
        sdk_bridge._run_claude(
            {
                "vendor": "claude",
                "prompt": "private prompt",
                "cwd": str(tmp_path),
                "requested_model": "claude-opus-5",
                "effort": "high",
                "read_only": True,
                "budget_usd": None,
                "commercial_mode": "subscription-only",
                "output_schema": governed_schema("attempt-claude"),
                "read_roots": [str(tmp_path.resolve())],
            }
        )
    )

    terminal = json.loads(capsys.readouterr().out.splitlines()[-1])
    assert terminal["status"] == "completed"
    assert terminal["terminal_reason"] == "completed"
    assert terminal["structured_output"] == governed_output
    assert terminal["structured_output_recovery"] == "accepted-tool-result"
    assert terminal["subtype"] == "success"
    assert terminal["stop_reason"] == "tool_use"
    assert terminal["permission_denial_count"] == 1
    assert terminal["permission_denial_tools"] == ["Read"]
    parsed = claude.parse_claude_stream(json.dumps(terminal))
    assert parsed.status is contracts.RuntimeStatus.COMPLETED
    assert parsed.structured_output == governed_output
    assert parsed.diagnostics == (
        "Claude structured output recovered from accepted tool result",
        "Claude stop reason: tool_use",
        "Claude permission denials: 1: Read",
    )


def test_claude_sdk_does_not_promote_unaccepted_success_terminal(
    monkeypatch, tmp_path: Path, capsys
) -> None:
    import claude_agent_sdk
    from claude_agent_sdk import ResultMessage

    async def fake_query(**_kwargs):
        yield ResultMessage(
            subtype="success",
            duration_ms=100,
            duration_api_ms=80,
            is_error=False,
            num_turns=1,
            session_id="session-claude",
            stop_reason="end_turn",
            total_cost_usd=0.1,
            result="",
            structured_output=None,
        )

    monkeypatch.setattr(claude_agent_sdk, "query", fake_query)

    asyncio.run(
        sdk_bridge._run_claude(
            {
                "vendor": "claude",
                "prompt": "private prompt",
                "cwd": str(tmp_path),
                "requested_model": "claude-opus-5",
                "effort": "high",
                "read_only": True,
                "budget_usd": None,
                "commercial_mode": "subscription-only",
                "output_schema": governed_schema("attempt-claude"),
                "read_roots": [str(tmp_path.resolve())],
            }
        )
    )

    terminal = json.loads(capsys.readouterr().out.splitlines()[-1])
    assert terminal["status"] == "failed"
    assert terminal["terminal_reason"] == "model-result"
    assert "structured_output" not in terminal


def test_claude_sdk_progress_maps_phases_without_retaining_sensitive_content(
    monkeypatch, tmp_path: Path, capsys
) -> None:
    import claude_agent_sdk
    from claude_agent_sdk import AssistantMessage, ResultMessage, ToolUseBlock

    sensitive_prompt = "private prompt alpha"
    sensitive_command = "cat /secret/project/private.txt"
    sensitive_output = "private model output omega"

    async def fake_query(**_kwargs):
        yield AssistantMessage(
            content=[],
            model="claude-opus-5",
            session_id="private-session",
        )
        yield AssistantMessage(
            content=[
                ToolUseBlock(
                    id="private-tool-id",
                    name="Bash",
                    input={"command": sensitive_command},
                ),
                ToolUseBlock(
                    id="second-private-id",
                    name="Read",
                    input={"file_path": "/secret/file"},
                ),
            ],
            model="claude-opus-5",
            session_id="private-session",
        )
        yield ResultMessage(
            subtype="success",
            duration_ms=100,
            duration_api_ms=80,
            is_error=False,
            num_turns=2,
            session_id="private-session",
            stop_reason="end_turn",
            total_cost_usd=0.1,
            result=sensitive_output,
            structured_output={
                "task_id": "attempt-claude",
                "status": "completed",
                "summary": "done",
            },
        )

    monkeypatch.setattr(claude_agent_sdk, "query", fake_query)
    reporter = _ProgressRecorder()
    bridge_request = {
        "vendor": "claude",
        "prompt": sensitive_prompt,
        "cwd": str(tmp_path),
        "requested_model": "claude-opus-5",
        "effort": "high",
        "read_only": True,
        "budget_usd": 5.0,
        "commercial_mode": "subscription-only",
        "output_schema": governed_schema("attempt-claude"),
        "read_roots": [str(tmp_path.resolve())],
    }

    asyncio.run(sdk_bridge._run_claude(bridge_request, reporter=reporter))

    assert reporter.phases == [
        contracts.RuntimePhase.MODEL_ACTIVE,
        contracts.RuntimePhase.TOOL_EXECUTION,
        contracts.RuntimePhase.RESULT_PACKAGING,
    ]
    assert (
        contracts.RuntimePhase.TOOL_EXECUTION,
        contracts.RuntimeToolLabel.FILE,
    ) in reporter.observations
    retained = repr(reporter.phases)
    assert sensitive_prompt not in retained
    assert sensitive_command not in retained
    assert sensitive_output not in retained
    capsys.readouterr()


def test_claude_structured_output_tool_maps_directly_to_result_packaging(
    monkeypatch, tmp_path: Path, capsys
) -> None:
    import claude_agent_sdk
    from claude_agent_sdk import AssistantMessage, ResultMessage, ToolUseBlock

    async def fake_query(**_kwargs):
        yield AssistantMessage(
            content=[
                ToolUseBlock(
                    id="structured-private",
                    name="StructuredOutput",
                    input={
                        "task_id": "attempt-claude",
                        "status": "completed",
                        "summary": "done",
                    },
                )
            ],
            model="claude-opus-5",
            session_id="private-session",
        )
        yield ResultMessage(
            subtype="success",
            duration_ms=1,
            duration_api_ms=1,
            is_error=False,
            num_turns=1,
            session_id="private-session",
            stop_reason="end_turn",
            total_cost_usd=0.1,
            result=None,
            structured_output={
                "task_id": "attempt-claude",
                "status": "completed",
                "summary": "done",
            },
        )

    monkeypatch.setattr(claude_agent_sdk, "query", fake_query)
    reporter = _ProgressRecorder()

    asyncio.run(
        sdk_bridge._run_claude(
            {
                "vendor": "claude",
                "prompt": "private prompt",
                "cwd": str(tmp_path),
                "requested_model": "claude-opus-5",
                "effort": "high",
                "read_only": True,
                "budget_usd": None,
                "commercial_mode": "subscription-only",
                "output_schema": governed_schema("attempt-claude"),
                "read_roots": [str(tmp_path.resolve())],
            },
            reporter=reporter,
        )
    )

    assert reporter.phases == [
        contracts.RuntimePhase.MODEL_ACTIVE,
        contracts.RuntimePhase.RESULT_PACKAGING,
    ]
    assert all(tool is None for _phase, tool in reporter.observations)
    capsys.readouterr()


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("Bash", contracts.RuntimePhase.TOOL_EXECUTION),
        ("StructuredOutput", contracts.RuntimePhase.RESULT_PACKAGING),
    ],
)
def test_claude_stream_tool_start_maps_from_envelope_without_retaining_input(
    name: str,
    expected,
) -> None:
    sensitive = "cat /secret/stream-tool-input"

    phase = sdk_bridge._claude_stream_progress_phase(
        {
            "type": "content_block_start",
            "content_block": {
                "type": "tool_use",
                "name": name,
                "input": {"command": sensitive},
            },
        }
    )

    assert phase is expected
    assert sensitive not in repr(phase)


def test_claude_sdk_keeps_last_accepted_result_when_later_attempt_is_rejected(
    monkeypatch, tmp_path: Path, capsys
) -> None:
    import claude_agent_sdk
    from claude_agent_sdk import (
        AssistantMessage,
        ResultMessage,
        ToolResultBlock,
        ToolUseBlock,
        UserMessage,
    )

    accepted = {
        "task_id": "attempt-claude",
        "status": "completed",
        "summary": "accepted",
    }
    rejected = {**accepted, "summary": "rejected"}

    async def fake_query(**_kwargs):
        yield AssistantMessage(
            content=[
                ToolUseBlock(
                    id="structured-accepted",
                    name="StructuredOutput",
                    input=accepted,
                )
            ],
            model="claude-opus-5",
            session_id="session-claude",
        )
        yield UserMessage(
            content=[
                ToolResultBlock(
                    tool_use_id="structured-accepted",
                    content="accepted",
                    is_error=False,
                )
            ]
        )
        yield AssistantMessage(
            content=[
                ToolUseBlock(
                    id="structured-rejected",
                    name="StructuredOutput",
                    input=rejected,
                )
            ],
            model="claude-opus-5",
            session_id="session-claude",
        )
        yield UserMessage(
            content=[
                ToolResultBlock(
                    tool_use_id="structured-rejected",
                    content="rejected",
                    is_error=True,
                )
            ]
        )
        yield ResultMessage(
            subtype="error_max_budget_usd",
            duration_ms=100,
            duration_api_ms=80,
            is_error=True,
            num_turns=3,
            session_id="session-claude",
            stop_reason="tool_use",
            total_cost_usd=5.1,
            result="private budget error",
            structured_output=None,
        )

    monkeypatch.setattr(claude_agent_sdk, "query", fake_query)

    asyncio.run(
        sdk_bridge._run_claude(
            {
                "vendor": "claude",
                "prompt": "private prompt",
                "cwd": str(tmp_path),
                "requested_model": "claude-opus-5",
                "effort": "high",
                "read_only": True,
                "budget_usd": 5.0,
                "commercial_mode": "promotional-credit",
                "output_schema": governed_schema("attempt-claude"),
                "read_roots": [str(tmp_path.resolve())],
            }
        )
    )

    terminal = json.loads(capsys.readouterr().out.splitlines()[-1])
    assert terminal["status"] == "completed"
    assert terminal["terminal_reason"] == "budget-exhausted-after-result"
    assert terminal["structured_output"] == accepted


def test_claude_sdk_does_not_promote_unaccepted_budget_terminal_output(
    monkeypatch, tmp_path: Path, capsys
) -> None:
    import claude_agent_sdk
    from claude_agent_sdk import ResultMessage

    unaccepted = {
        "task_id": "attempt-claude",
        "status": "completed",
        "summary": "not accepted by the structured-output tool",
    }

    async def fake_query(**_kwargs):
        yield ResultMessage(
            subtype="error_max_budget_usd",
            duration_ms=100,
            duration_api_ms=80,
            is_error=True,
            num_turns=1,
            session_id="session-claude",
            stop_reason="tool_use",
            total_cost_usd=5.1,
            result="private budget error",
            structured_output=unaccepted,
        )

    monkeypatch.setattr(claude_agent_sdk, "query", fake_query)

    asyncio.run(
        sdk_bridge._run_claude(
            {
                "vendor": "claude",
                "prompt": "private prompt",
                "cwd": str(tmp_path),
                "requested_model": "claude-opus-5",
                "effort": "high",
                "read_only": True,
                "budget_usd": 5.0,
                "commercial_mode": "promotional-credit",
                "output_schema": governed_schema("attempt-claude"),
                "read_roots": [str(tmp_path.resolve())],
            }
        )
    )

    terminal = json.loads(capsys.readouterr().out.splitlines()[-1])
    assert terminal["status"] == "failed"
    assert terminal["terminal_reason"] == "budget-exhausted"
    assert "structured_output" not in terminal
    assert "private budget error" not in json.dumps(terminal)


def test_claude_sdk_emits_terminal_protocol_failure_for_malformed_result(
    monkeypatch, tmp_path: Path, capsys
) -> None:
    import claude_agent_sdk
    from claude_agent_sdk import ResultMessage

    async def fake_query(**_kwargs):
        yield ResultMessage(
            subtype="success",
            duration_ms=100,
            duration_api_ms=80,
            is_error=False,
            num_turns=1,
            session_id="session-claude",
            stop_reason="end_turn",
            total_cost_usd=0.1,
            result=None,
            structured_output={"summary": "\ud800"},
        )

    monkeypatch.setattr(claude_agent_sdk, "query", fake_query)

    asyncio.run(
        sdk_bridge._run_claude(
            {
                "vendor": "claude",
                "prompt": "private prompt",
                "cwd": str(tmp_path),
                "requested_model": "claude-opus-5",
                "effort": "high",
                "read_only": True,
                "budget_usd": 5.0,
                "commercial_mode": "promotional-credit",
                "output_schema": governed_schema("attempt-claude"),
                "read_roots": [str(tmp_path.resolve())],
            }
        )
    )

    terminal = json.loads(capsys.readouterr().out.splitlines()[-1])
    assert terminal == {
        "type": "result",
        "status": "failed",
        "terminal_reason": "protocol-failure",
        "session_id": "session-claude",
    }


def test_claude_stream_preserves_safe_rate_limit_commercial_boundary() -> None:
    parsed = claude.parse_claude_stream(
        "\n".join(
            [
                json.dumps(
                    {
                        "type": "event",
                        "kind": "rate_limit",
                        "subtype": "five_hour:allowed_warning",
                        "semantic": False,
                    }
                ),
                json.dumps(
                    {
                        "type": "result",
                        "status": "failed",
                        "terminal_reason": "commercial-boundary",
                    }
                ),
            ]
        )
    )

    assert parsed.status is contracts.RuntimeStatus.FAILED
    assert parsed.terminal_reason is contracts.TerminalReason.COMMERCIAL_BOUNDARY
    assert parsed.events[0] == contracts.RuntimeEvent(
        kind="rate_limit",
        subtype="five_hour:allowed_warning",
        semantic=False,
    )


@pytest.mark.parametrize(
    ("rate_type", "status", "overage_status", "commercial_mode", "expected"),
    [
        ("five_hour", "allowed_warning", None, "subscription-only", False),
        ("overage", "allowed", None, "subscription-only", True),
        ("seven_day", "allowed", "allowed_warning", "subscription-only", True),
        ("overage", "rejected", "rejected", "subscription-only", False),
        ("overage", "allowed", None, "promotional-credit", False),
        ("seven_day", "allowed", "allowed_warning", "promotional-credit", False),
        ("overage", "allowed", None, "owner-paid", False),
        ("seven_day", "allowed", "allowed_warning", "owner-paid", False),
    ],
)
def test_claude_rate_limit_commercial_boundary_is_fail_closed(
    rate_type, status, overage_status, commercial_mode, expected
) -> None:
    assert (
        sdk_bridge._commercial_boundary_from_rate_limit(
            rate_type=rate_type,
            status=status,
            overage_status=overage_status,
            commercial_mode=commercial_mode,
        )
        is expected
    )


def test_claude_environment_removes_gateway_and_raw_api_paths() -> None:
    environment = claude.filtered_claude_environment(
        {
            "ANTHROPIC_API_KEY": "secret",
            "ANTHROPIC_BASE_URL": "https://gateway.invalid",
            "CLAUDE_CODE_USE_BEDROCK": "1",
            "CLAUDE_CODE_USE_FOUNDRY": "1",
            "CLAUDE_CODE_SKIP_FOUNDRY_AUTH": "1",
            "AWS_BEARER_TOKEN_BEDROCK": "secret",
            "ANTHROPIC_FOUNDRY_API_KEY": "secret",
            "ANTHROPIC_FOUNDRY_BASE_URL": "https://foundry.invalid",
            "ANTHROPIC_CUSTOM_HEADERS": "Authorization: secret",
            "PYTHONPATH": "/secret/import/path",
            "NORMAL_LOGIN_MARKER": "kept",
        }
    )

    assert environment["NORMAL_LOGIN_MARKER"] == "kept"
    assert not claude.CLAUDE_BLOCKED_ENV_VARS.intersection(environment)
    assert not process.RAW_API_ENV_VARS.intersection(environment)
    assert not process.PYTHON_IMPORT_ENV_VARS.intersection(environment)


def test_claude_sdk_and_cli_receive_scoped_tools_and_native_schema(
    tmp_path: Path,
) -> None:
    import claude_agent_sdk
    runtime_request = claude_request(tmp_path)
    bridge_request = sdk_bridge._validate_request_payload(
        {
            "vendor": "claude",
            "prompt": runtime_request.prompt,
            "cwd": str(runtime_request.cwd),
            "requested_model": runtime_request.requested_model,
            "effort": runtime_request.effort,
            "read_only": True,
            "budget_usd": runtime_request.budget_usd,
            "output_schema": runtime_request.output_schema,
            "read_roots": [str(tmp_path.resolve())],
            "commercial_mode": "subscription-only",
        }
    )
    options = sdk_bridge._options(bridge_request)
    command = claude.build_claude_command(runtime_request)

    scoped = f"{tmp_path.resolve()}/**"
    assert options.tools == ["Read", "Grep", "Glob"]
    assert options.allowed_tools == []
    assert options.can_use_tool is not None
    assert options.output_format == {
        "type": "json_schema",
        "schema": runtime_request.output_schema,
    }
    assert command[
        command.index("--allowedTools") + 1 : command.index("--json-schema")
    ] == [f"Read({scoped}),Grep({scoped}),Glob({scoped})"]
    assert json.loads(command[command.index("--json-schema") + 1]) == (
        runtime_request.output_schema
    )


def test_claude_cli_builder_resumes_the_requested_session(tmp_path: Path) -> None:
    command = claude.build_claude_command(
        replace(claude_request(tmp_path), resume_session_id="session.valid-_1")
    )

    assert command[command.index("--resume") + 1] == "session.valid-_1"
    assert "--no-session-persistence" not in command


def test_claude_canonical_evidence_reaches_sdk_and_cli_with_bounded_reads(
    tmp_path: Path,
) -> None:
    evidence = tmp_path / "primary" / ".audit" / "dispatch" / "results"
    evidence.mkdir(parents=True)
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    runtime_request = replace(
        claude_request(worktree),
        capability_profile=contracts.RuntimeCapabilityProfile(
            read_roots=(worktree.resolve(),),
            evidence_read_roots=(evidence.resolve(),),
        ),
    )
    captured: list[dict[str, object]] = []

    def run_process(_command, **kwargs):
        captured.append(json.loads(kwargs["input_text"]))
        return process.ProcessResult(
            returncode=0,
            stdout=(
                '{"type":"result","status":"completed",'
                '"terminal_reason":"completed"}\n'
            ),
            stderr="",
            duration_s=0.01,
            timed_out=False,
        )

    result = claude.ClaudeAdapter(run_process=run_process)._run_sdk(runtime_request)
    command = claude.build_claude_command(runtime_request)
    allowed = command[command.index("--allowedTools") + 1]

    assert result.status is contracts.RuntimeStatus.COMPLETED
    assert captured[0]["read_roots"] == [str(worktree.resolve())]
    assert captured[0]["evidence_read_roots"] == [str(evidence.resolve())]
    assert command[command.index("--tools") + 1] == "Read,Grep,Glob"
    assert allowed.split(",") == [
        f"{tool}({root}/**)"
        for root in (worktree.resolve(), evidence.resolve())
        for tool in ("Read", "Grep", "Glob")
    ]


def test_claude_sdk_forwards_promotional_credit_mode(tmp_path: Path) -> None:
    captured: list[dict[str, object]] = []

    def run_process(command, **kwargs):
        del command
        captured.append(json.loads(kwargs["input_text"]))
        return process.ProcessResult(
            returncode=0,
            stdout=(
                '{"type":"result","status":"completed",'
                '"terminal_reason":"completed"}\n'
            ),
            stderr="",
            duration_s=0.01,
            timed_out=False,
        )

    runtime_request = claude_request(
        tmp_path,
        commercial_mode=contracts.RuntimeCommercialMode.PROMOTIONAL_CREDIT,
    )
    result = claude.ClaudeAdapter(run_process=run_process)._run_sdk(runtime_request)

    assert captured[0]["commercial_mode"] == "promotional-credit"
    assert result.commercial_mode is contracts.RuntimeCommercialMode.PROMOTIONAL_CREDIT


def test_claude_sdk_maps_vendor_cost_to_estimated_status(tmp_path: Path) -> None:
    def run_process(command, **kwargs):
        del command, kwargs
        return process.ProcessResult(
            returncode=0,
            stdout=(
                '{"type":"result","status":"completed",'
                '"terminal_reason":"completed","total_cost_usd":0.42}\n'
            ),
            stderr="",
            duration_s=0.01,
            timed_out=False,
        )

    result = claude.ClaudeAdapter(run_process=run_process)._run_sdk(
        claude_request(tmp_path)
    )

    assert result.cost_usd == 0.42
    assert result.cost_status is contracts.RuntimeCostStatus.ESTIMATED


def test_claude_sdk_maps_absent_vendor_cost_to_unknown_status(tmp_path: Path) -> None:
    def run_process(command, **kwargs):
        del command, kwargs
        return process.ProcessResult(
            returncode=0,
            stdout=(
                '{"type":"result","status":"completed",'
                '"terminal_reason":"completed"}\n'
            ),
            stderr="",
            duration_s=0.01,
            timed_out=False,
        )

    result = claude.ClaudeAdapter(run_process=run_process)._run_sdk(
        claude_request(tmp_path)
    )

    assert result.cost_usd is None
    assert result.cost_status is contracts.RuntimeCostStatus.UNKNOWN


def test_claude_cli_preserves_accepted_result_on_nonzero_budget_exit(
    tmp_path: Path,
) -> None:
    from dataclasses import replace

    governed_output = {
        "task_id": "attempt-claude",
        "status": "completed",
        "summary": "done",
    }

    def run_process(command, **kwargs):
        del command, kwargs
        return process.ProcessResult(
            returncode=1,
            stdout=json.dumps(
                {
                    "type": "result",
                    "subtype": "error_max_budget_usd",
                    "is_error": True,
                    "structured_output": governed_output,
                    "total_cost_usd": 5.1,
                }
            ),
            stderr="private vendor error",
            duration_s=0.01,
            timed_out=False,
        )

    runtime_request = replace(
        claude_request(tmp_path),
        transport=claude.CLAUDE_CLI_TRANSPORT,
    )
    result = claude.ClaudeAdapter(run_process=run_process)._run_cli(runtime_request)

    assert result.status is contracts.RuntimeStatus.COMPLETED
    assert (
        result.terminal_reason is contracts.TerminalReason.BUDGET_EXHAUSTED_AFTER_RESULT
    )
    assert result.structured_output == governed_output
    assert "private vendor error" not in repr(result)


@pytest.mark.parametrize(
    "payload",
    [
        {"vendor": "claude", "commercial_mode": "unbounded"},
        {"vendor": "codex", "commercial_mode": "promotional-credit"},
    ],
)
def test_sdk_bridge_rejects_invalid_commercial_modes(
    tmp_path: Path, payload: dict[str, object]
) -> None:
    base = {
        "prompt": "private",
        "cwd": str(tmp_path),
        "requested_model": "test-model",
        "effort": "high",
        "read_only": True,
        "budget_usd": None,
        "output_schema": governed_schema("attempt"),
        "read_roots": [str(tmp_path.resolve())],
    }

    with pytest.raises(sdk_bridge.BridgeInputError, match="commercial_mode"):
        sdk_bridge._validate_request_payload({**base, **payload})


def test_bridge_and_parent_filter_the_same_claude_billing_overrides() -> None:
    assert sdk_bridge.RAW_API_ENV_VARS == (
        process.RAW_API_ENV_VARS | claude.CLAUDE_BLOCKED_ENV_VARS
    )
    assert {
        "ANTHROPIC_FOUNDRY_API_KEY",
        "ANTHROPIC_FOUNDRY_AUTH_TOKEN",
        "ANTHROPIC_FOUNDRY_BASE_URL",
        "ANTHROPIC_BEDROCK_BASE_URL",
        "ANTHROPIC_VERTEX_BASE_URL",
        "ANTHROPIC_CUSTOM_HEADERS",
        "CLAUDE_CODE_USE_ANTHROPIC_AWS",
        "CLAUDE_CODE_USE_FOUNDRY",
        "CLAUDE_CODE_USE_MANTLE",
        "CLAUDE_CODE_SKIP_ANTHROPIC_AWS_AUTH",
        "CLAUDE_CODE_SKIP_BEDROCK_AUTH",
        "CLAUDE_CODE_SKIP_FOUNDRY_AUTH",
        "CLAUDE_CODE_SKIP_MANTLE_AUTH",
        "CLAUDE_CODE_SKIP_VERTEX_AUTH",
    } <= sdk_bridge.RAW_API_ENV_VARS
    assert sdk_bridge.PYTHON_IMPORT_ENV_VARS == process.PYTHON_IMPORT_ENV_VARS


def test_sdk_bridge_commands_use_isolated_python_mode(tmp_path: Path) -> None:
    claude_command = claude.build_sdk_bridge_command(tmp_path)
    codex_command = codex.build_sdk_bridge_command(tmp_path)
    expected_bridge = Path(sdk_bridge.__file__).resolve()

    assert claude_command[1] == "-I"
    assert codex_command[1] == "-I"
    assert claude_command[-1] == str(expected_bridge)
    assert codex_command[-1] == str(expected_bridge)


@pytest.mark.parametrize(
    "resolver", [claude.repository_python, codex.repository_python]
)
def test_repository_python_does_not_escape_tooling_root(
    resolver, tmp_path: Path
) -> None:
    parent_python = tmp_path / "fastapi_backend" / ".venv" / "bin" / "python"
    parent_python.parent.mkdir(parents=True)
    parent_python.touch()
    tooling_root = tmp_path / "trusted-tooling"
    tooling_root.mkdir()

    assert resolver(tooling_root) == (
        tooling_root / "fastapi_backend" / ".venv" / "bin" / "python"
    )


@pytest.mark.parametrize(
    "resolver", [claude.repository_python, codex.repository_python]
)
def test_repository_python_keeps_missing_tooling_root_authority(
    resolver, tmp_path: Path
) -> None:
    parent_python = tmp_path / "fastapi_backend" / ".venv" / "bin" / "python"
    parent_python.parent.mkdir(parents=True)
    parent_python.touch()
    missing_tooling_root = tmp_path / "missing-trusted-tooling"

    assert resolver(missing_tooling_root) == (
        missing_tooling_root / "fastapi_backend" / ".venv" / "bin" / "python"
    )


@pytest.mark.parametrize(
    "resolver", [claude.repository_python, codex.repository_python]
)
def test_repository_python_normalizes_direct_backend_directory(
    resolver, tmp_path: Path
) -> None:
    backend_root = tmp_path / "fastapi_backend"
    backend_root.mkdir()

    assert resolver(backend_root) == backend_root / ".venv" / "bin" / "python"


@pytest.mark.parametrize(
    "command_builder",
    [claude.build_sdk_bridge_command, codex.build_sdk_bridge_command],
)
def test_sdk_bridge_command_normalizes_direct_backend_directory(
    command_builder, tmp_path: Path
) -> None:
    backend_root = tmp_path / "fastapi_backend"
    backend_root.mkdir()

    command = command_builder(backend_root)

    assert command[0] == str(backend_root / ".venv" / "bin" / "python")
    assert command[-1] == str(
        Path(sdk_bridge.__file__).resolve()
    )


@pytest.mark.parametrize(
    "command_builder",
    [claude.build_sdk_bridge_command, codex.build_sdk_bridge_command],
)
def test_sdk_bridge_starts_under_isolated_python_mode(
    command_builder, tmp_path: Path
) -> None:
    repo_root = Path(__file__).resolve().parents[4]
    marker = tmp_path / "shadow-imported"
    (tmp_path / "codex_isolation.py").write_text(
        f"from pathlib import Path\nPath({str(marker)!r}).write_text('shadow')\n",
        encoding="utf-8",
    )

    with RuntimeSettings(
        env_prefix="INTELFLO", toolchain_interpreter=Path(sys.executable)
    ).use():
        command = command_builder(repo_root)
        result = run_cli_unsandboxed(
            command,
            cwd=tmp_path,
            input_text="not-json",
            timeout_s=5,
            env={**os.environ, "PYTHONPATH": str(tmp_path)},
        )

    assert result.returncode == 2
    assert json.loads(result.stdout) == {"type": "error", "reason": "protocol"}
    assert result.stderr == ""
    assert not marker.exists()


def _auth_process_result() -> object:
    return process.ProcessResult(
        returncode=0,
        stdout=json.dumps(
            {
                "loggedIn": True,
                "authMethod": "claude.ai",
                "subscriptionType": "max",
                "email": "private@example.invalid",
                "orgName": "private-org",
            }
        ),
        stderr="",
        duration_s=0.01,
        timed_out=False,
    )


def test_claude_selects_cli_before_launch_when_sdk_is_unavailable(
    tmp_path: Path,
) -> None:
    calls: list[list[str]] = []

    def run_cli(command, **kwargs):
        del kwargs
        calls.append(list(command))
        return process.ProcessResult(
            returncode=0,
            stdout=json.dumps(
                {
                    "type": "result",
                    "subtype": "success",
                    "is_error": False,
                    "session_id": "cli-session",
                    "result": "cli output",
                }
            ),
            stderr="",
            duration_s=0.01,
            timed_out=False,
        )

    adapter = claude.ClaudeAdapter(
        run_cli=run_cli,
        run_probe=lambda *args, **kwargs: _auth_process_result(),
        sdk_available=lambda *_args: False,
        which=lambda _name: "/usr/bin/claude",
    )
    result = adapter.run(claude_request(tmp_path))

    assert result.transport == claude.CLAUDE_CLI_TRANSPORT
    assert result.status is contracts.RuntimeStatus.COMPLETED
    assert result.fallback_from is None
    assert result.attempt_id == "attempt-claude"
    assert [attempt.transport for attempt in result.transport_attempts] == [
        claude.CLAUDE_SDK_TRANSPORT,
        claude.CLAUDE_CLI_TRANSPORT,
    ]
    assert result.transport_attempts[0].failure_class == "sdk-unavailable"
    assert result.transport_attempts[0].selected_next is True
    assert calls == [claude.build_claude_command(claude_request(tmp_path))]


def test_claude_sdk_readiness_rejects_unsupported_model_before_auth_or_launch(
    tmp_path: Path,
) -> None:
    probe_calls: list[list[str]] = []
    launches: list[list[str]] = []

    def model_probe(command, **_kwargs):
        probe_calls.append(list(command))
        return process.ProcessResult(
            returncode=1,
            stdout=(
                '{"type":"readiness","status":"failed",'
                '"failure":"model-unsupported"}\n'
            ),
            stderr="private vendor details must not be retained",
            duration_s=0.01,
            timed_out=False,
        )

    def unexpected_launch(command, **_kwargs):
        launches.append(list(command))
        raise AssertionError("an incompatible governed runtime must not launch")

    runtime_request = replace(
        claude_request(tmp_path),
        requested_model="claude-fable-5-1",
    )
    adapter = claude.ClaudeAdapter(
        run_process=unexpected_launch,
        run_probe=model_probe,
        sdk_available=lambda *_args: True,
        which=lambda _name: "/usr/bin/claude",
    )

    result = adapter.run(runtime_request)

    assert result.status is contracts.RuntimeStatus.SUBSCRIPTION_UNAVAILABLE
    assert result.diagnostics == (
        "upgrade the governed Claude SDK runtime for the configured model",
    )
    assert probe_calls == [claude.build_sdk_bridge_command(tmp_path)]
    assert launches == []
    assert "private vendor" not in repr(result)


def test_selected_claude_sdk_bundle_supports_fable_5_1_without_model_launch(
    tmp_path: Path,
) -> None:
    import claude_agent_sdk
    repo_root = Path(__file__).resolve().parents[4]
    runtime_request = replace(
        claude_request(tmp_path),
        requested_model="claude-fable-5-1",
        tooling_root=repo_root,
    )

    with RuntimeSettings(
        env_prefix="INTELFLO", toolchain_interpreter=Path(sys.executable)
    ).use():
        command = claude.build_sdk_bridge_command(repo_root)
        result = run_cli_unsandboxed(
            command,
            cwd=tmp_path,
            input_text=claude._sdk_model_readiness_payload(runtime_request),
            timeout_s=5,
            env=claude.filtered_claude_environment(),
        )

    assert result.returncode == 0
    assert result.stdout == '{"type":"readiness","status":"ready"}\n'
    assert result.stderr == ""


def test_fable_5_1_sdk_probe_failure_never_selects_system_cli(
    tmp_path: Path,
) -> None:
    launches: list[list[str]] = []

    def malformed_model_probe(command, **_kwargs):
        return process.ProcessResult(
            returncode=1,
            stdout="not a readiness frame",
            stderr="private vendor details must not be retained",
            duration_s=0.01,
            timed_out=False,
        )

    def unexpected_launch(command, **_kwargs):
        launches.append(list(command))
        raise AssertionError("Fable 5.1 must stay on the governed SDK bundle")

    runtime_request = replace(
        claude_request(tmp_path),
        requested_model="claude-fable-5-1",
    )
    adapter = claude.ClaudeAdapter(
        run_process=unexpected_launch,
        run_probe=malformed_model_probe,
        sdk_available=lambda *_args: True,
        which=lambda _name: "/usr/bin/claude",
    )

    result = adapter.run(runtime_request)

    assert result.status is contracts.RuntimeStatus.SUBSCRIPTION_UNAVAILABLE
    assert result.transport == claude.CLAUDE_SDK_TRANSPORT
    assert result.transport_attempts[0].failure_class == "protocol-incompatible"
    assert result.transport_attempts[0].selected_next is False
    assert launches == []
    assert "private vendor" not in repr(result)


def test_fable_5_1_explicit_cli_request_is_rejected_without_probe_or_launch(
    tmp_path: Path,
) -> None:
    calls: list[list[str]] = []

    def unexpected_call(command, **_kwargs):
        calls.append(list(command))
        raise AssertionError("Fable 5.1 must never invoke the mutable CLI")

    runtime_request = replace(
        claude_request(tmp_path),
        requested_model="claude-fable-5-1",
        transport=claude.CLAUDE_CLI_TRANSPORT,
    )
    adapter = claude.ClaudeAdapter(
        run_process=unexpected_call,
        run_probe=unexpected_call,
        sdk_available=lambda *_args: True,
        which=lambda _name: "/usr/bin/claude",
    )

    result = adapter.run(runtime_request)

    assert result.status is contracts.RuntimeStatus.SUBSCRIPTION_UNAVAILABLE
    assert result.transport == claude.CLAUDE_CLI_TRANSPORT
    assert result.transport_attempts[0].failure_class == "model-unsupported"
    assert calls == []


def test_claude_write_run_never_relaunches_cli_after_bridge_start(
    tmp_path: Path,
) -> None:
    calls: list[list[str]] = []

    def run_process(command, **kwargs):
        del kwargs
        calls.append(list(command))
        return process.ProcessResult(
            returncode=1,
            stdout="not json",
            stderr="vendor response must not be retained",
            duration_s=0.01,
            timed_out=False,
        )

    adapter = claude.ClaudeAdapter(
        run_process=run_process,
        run_probe=lambda *args, **kwargs: _auth_process_result(),
        sdk_available=lambda *_args: True,
        which=lambda _name: "/usr/bin/claude",
    )
    result = adapter.run(claude_request(tmp_path, read_only=False))

    assert result.status is contracts.RuntimeStatus.FAILED
    assert result.terminal_reason is contracts.TerminalReason.STARTUP_FAILURE
    assert len(calls) == 1
    assert calls[0][0].endswith("/bin/python")
    assert calls[0][1] == "-I"
    assert "vendor response must not be retained" not in repr(result)


def test_claude_read_only_falls_back_only_after_presemantic_bridge_failure(
    tmp_path: Path,
) -> None:
    calls: list[list[str]] = []
    outcomes = iter(
        [
            process.ProcessResult(
                returncode=1,
                stdout='{"type":"error","reason":"startup"}\n',
                stderr="",
                duration_s=0.01,
                timed_out=False,
                progress_diagnostic=True,
            ),
            process.ProcessResult(
                returncode=0,
                stdout=json.dumps(
                    {
                        "type": "result",
                        "subtype": "success",
                        "is_error": False,
                        "session_id": "fallback-session",
                        "result": "fallback output",
                    }
                ),
                stderr="",
                duration_s=0.01,
                timed_out=False,
            ),
        ]
    )

    def run_process(command, **kwargs):
        del kwargs
        calls.append(list(command))
        return next(outcomes)

    adapter = claude.ClaudeAdapter(
        run_process=run_process,
        run_probe=lambda *args, **kwargs: _auth_process_result(),
        sdk_available=lambda *_args: True,
        which=lambda _name: "/usr/bin/claude",
    )
    result = adapter.run(claude_request(tmp_path))

    assert result.transport == claude.CLAUDE_CLI_TRANSPORT
    assert result.fallback_from is None
    assert result.attempt_id == "attempt-claude"
    assert result.status is contracts.RuntimeStatus.COMPLETED
    assert [attempt.transport for attempt in result.transport_attempts] == [
        claude.CLAUDE_SDK_TRANSPORT,
        claude.CLAUDE_CLI_TRANSPORT,
    ]
    assert result.transport_attempts[0].selected_next is True
    assert result.transport_attempts[0].duration_s == 0.01
    assert result.transport_attempts[1].selected_next is False
    assert len(calls) == 2
    assert "Native runtime progress was dropped" in result.diagnostics


def test_scoped_claude_authorization_never_falls_back_to_unhooked_cli(
    tmp_path: Path,
) -> None:
    calls: list[list[str]] = []

    def run_process(command, **kwargs):
        del kwargs
        calls.append(list(command))
        return process.ProcessResult(
            returncode=1,
            stdout='{"type":"error","reason":"startup"}\n',
            stderr="",
            duration_s=0.01,
            timed_out=False,
        )

    adapter = claude.ClaudeAdapter(
        run_process=run_process,
        run_probe=lambda *args, **kwargs: _auth_process_result(),
        sdk_available=lambda *_args: True,
        which=lambda _name: "/usr/bin/claude",
    )

    result = adapter.run(scoped_claude_request(tmp_path))

    assert result.status is contracts.RuntimeStatus.FAILED
    assert result.terminal_reason is contracts.TerminalReason.STARTUP_FAILURE
    assert len(calls) == 1
    assert calls[0][0].endswith("/bin/python")


def test_scoped_claude_sdk_unavailable_never_selects_or_launches_cli(
    tmp_path: Path,
) -> None:
    calls: list[list[str]] = []

    def run_process(command, **kwargs):
        del kwargs
        calls.append(list(command))
        return _auth_process_result()

    adapter = claude.ClaudeAdapter(
        run_process=run_process,
        run_probe=lambda *args, **kwargs: _auth_process_result(),
        sdk_available=lambda *_args: False,
        which=lambda _name: "/usr/bin/claude",
    )

    result = adapter.run(scoped_claude_request(tmp_path))

    assert result.status is contracts.RuntimeStatus.SUBSCRIPTION_UNAVAILABLE
    assert result.terminal_reason is contracts.TerminalReason.SUBSCRIPTION_UNAVAILABLE
    assert result.diagnostics == (
        "restore the repository Claude Agent SDK tooling dependency",
    )
    assert calls == []


def test_claude_read_only_does_not_fallback_after_semantic_event(
    tmp_path: Path,
) -> None:
    calls: list[list[str]] = []

    def run_process(command, **kwargs):
        del kwargs
        calls.append(list(command))
        return process.ProcessResult(
            returncode=1,
            stdout=('{"type":"event","kind":"assistant","semantic":true}\n'),
            stderr="",
            duration_s=0.01,
            timed_out=False,
        )

    adapter = claude.ClaudeAdapter(
        run_process=run_process,
        run_probe=lambda *args, **kwargs: _auth_process_result(),
        sdk_available=lambda *_args: True,
        which=lambda _name: "/usr/bin/claude",
    )
    result = adapter.run(claude_request(tmp_path))

    assert result.status is contracts.RuntimeStatus.FAILED
    assert result.terminal_reason is contracts.TerminalReason.PROTOCOL_FAILURE
    assert len(calls) == 1


def test_claude_retains_sdk_failure_when_post_start_cli_is_unavailable(
    tmp_path: Path,
) -> None:
    probe_outcomes = iter(
        [
            _auth_process_result(),
            process.ProcessResult(
                returncode=1,
                stdout="",
                stderr="",
                duration_s=0.01,
                timed_out=False,
            ),
        ]
    )
    adapter = claude.ClaudeAdapter(
        run_process=lambda *args, **kwargs: process.ProcessResult(
            returncode=1,
            stdout='{"type":"error","reason":"startup"}\n',
            stderr="",
            duration_s=0.25,
            timed_out=False,
        ),
        run_probe=lambda *args, **kwargs: next(probe_outcomes),
        sdk_available=lambda *_args: True,
        which=lambda _name: "/usr/bin/claude",
    )

    result = adapter.run(claude_request(tmp_path))

    assert result.status is contracts.RuntimeStatus.SUBSCRIPTION_UNAVAILABLE
    assert [attempt.transport for attempt in result.transport_attempts] == [
        claude.CLAUDE_SDK_TRANSPORT,
        claude.CLAUDE_CLI_TRANSPORT,
    ]
    assert result.transport_attempts[0].phase == "run"
    assert result.transport_attempts[0].failure_class == "startup"
    assert result.transport_attempts[0].duration_s == 0.25
    assert result.transport_attempts[0].selected_next is False
    assert result.transport_attempts[1].phase == "readiness"
    assert result.transport_attempts[1].failure_class == "authentication"


def test_claude_ineligible_policy_fails_closed_without_launch(tmp_path: Path) -> None:
    calls: list[list[str]] = []

    def unexpected_process(command, **kwargs):
        del kwargs
        calls.append(list(command))
        raise AssertionError("an ineligible request must not launch a process")

    runtime_request = claude_request(tmp_path)
    runtime_request = contracts.RuntimeRequest(
        vendor=runtime_request.vendor,
        transport=runtime_request.transport,
        requested_model=runtime_request.requested_model,
        effort=runtime_request.effort,
        prompt=runtime_request.prompt,
        cwd=runtime_request.cwd,
        timeout_s=runtime_request.timeout_s,
        read_only=runtime_request.read_only,
        attempt_id=runtime_request.attempt_id,
        eligibility=contracts.SubscriptionEligibility.UNAVAILABLE,
    )
    adapter = claude.ClaudeAdapter(
        run_process=unexpected_process,
        run_probe=unexpected_process,
        sdk_available=lambda *_args: True,
        which=lambda _name: "/usr/bin/claude",
    )

    result = adapter.run(runtime_request)

    assert result.status is contracts.RuntimeStatus.SUBSCRIPTION_UNAVAILABLE
    assert result.terminal_reason is contracts.TerminalReason.SUBSCRIPTION_UNAVAILABLE
    assert result.diagnostics == (
        "confirm the approved Claude subscription login path",
    )
    assert calls == []


def test_explicit_claude_cli_ineligible_policy_never_launches(tmp_path: Path) -> None:
    calls: list[list[str]] = []
    runtime_request = claude_request(tmp_path)
    runtime_request = contracts.RuntimeRequest(
        vendor="claude",
        transport=claude.CLAUDE_CLI_TRANSPORT,
        requested_model=runtime_request.requested_model,
        effort=runtime_request.effort,
        prompt=runtime_request.prompt,
        cwd=runtime_request.cwd,
        timeout_s=runtime_request.timeout_s,
        read_only=runtime_request.read_only,
        attempt_id=runtime_request.attempt_id,
        eligibility=contracts.SubscriptionEligibility.UNAVAILABLE,
    )

    def unexpected_process(command, **kwargs):
        del kwargs
        calls.append(list(command))
        raise AssertionError("an ineligible CLI request must not launch")

    adapter = claude.ClaudeAdapter(
        run_process=unexpected_process,
        run_probe=unexpected_process,
        which=lambda _name: "/usr/bin/claude",
    )

    result = adapter.run(runtime_request)

    assert result.status is contracts.RuntimeStatus.SUBSCRIPTION_UNAVAILABLE
    assert result.terminal_reason is contracts.TerminalReason.SUBSCRIPTION_UNAVAILABLE
    assert calls == []


def test_explicit_claude_cli_api_key_auth_never_launches(tmp_path: Path) -> None:
    launches: list[list[str]] = []
    runtime_request = claude_request(tmp_path)
    runtime_request = contracts.RuntimeRequest(
        vendor="claude",
        transport=claude.CLAUDE_CLI_TRANSPORT,
        requested_model=runtime_request.requested_model,
        effort=runtime_request.effort,
        prompt=runtime_request.prompt,
        cwd=runtime_request.cwd,
        timeout_s=runtime_request.timeout_s,
        read_only=runtime_request.read_only,
        attempt_id=runtime_request.attempt_id,
        eligibility=contracts.SubscriptionEligibility.APPROVED,
    )

    def auth_probe(*_args, **_kwargs):
        return process.ProcessResult(
            returncode=0,
            stdout=json.dumps(
                {
                    "loggedIn": True,
                    "authMethod": "apiKey",
                    "subscriptionType": "max",
                }
            ),
            stderr="",
            duration_s=0.01,
            timed_out=False,
        )

    def unexpected_launch(command, **kwargs):
        del kwargs
        launches.append(list(command))
        raise AssertionError("API-key auth must not launch the Claude CLI")

    adapter = claude.ClaudeAdapter(
        run_process=unexpected_launch,
        run_probe=auth_probe,
        which=lambda _name: "/usr/bin/claude",
    )

    result = adapter.run(runtime_request)

    assert result.status is contracts.RuntimeStatus.SUBSCRIPTION_UNAVAILABLE
    assert launches == []


def test_claude_write_cli_remains_default_deny(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="not an approved route"):
        claude.build_claude_command(claude_request(tmp_path, read_only=False))


def test_claude_both_transports_unavailable_returns_safe_terminal_result(
    tmp_path: Path,
) -> None:
    adapter = claude.ClaudeAdapter(
        sdk_available=lambda *_args: False,
        which=lambda _name: None,
    )

    result = adapter.run(claude_request(tmp_path))

    assert result.status is contracts.RuntimeStatus.SUBSCRIPTION_UNAVAILABLE
    assert result.terminal_reason is contracts.TerminalReason.SUBSCRIPTION_UNAVAILABLE
    assert result.diagnostics == ("install or restore the approved Claude CLI",)
    assert "API" not in result.diagnostics[0]


def test_claude_timeout_never_falls_back(tmp_path: Path) -> None:
    calls: list[list[str]] = []

    def run_process(command, **kwargs):
        del kwargs
        calls.append(list(command))
        return process.ProcessResult(
            returncode=-signal.SIGKILL,
            stdout="",
            stderr="",
            duration_s=5,
            timed_out=True,
        )

    adapter = claude.ClaudeAdapter(
        run_process=run_process,
        run_probe=lambda *args, **kwargs: _auth_process_result(),
        sdk_available=lambda *_args: True,
        which=lambda _name: "/usr/bin/claude",
    )

    result = adapter.run(claude_request(tmp_path))

    assert result.status is contracts.RuntimeStatus.TIMED_OUT
    assert result.terminal_reason is contracts.TerminalReason.TIMEOUT
    assert len(calls) == 1


def test_sdk_bridge_options_keep_prompt_out_of_options_repr(tmp_path: Path) -> None:
    import claude_agent_sdk
    options = sdk_bridge._options(
        {
            "vendor": "claude",
            "prompt": "private prompt",
            "cwd": str(tmp_path),
            "requested_model": "claude-opus-5",
            "effort": "high",
            "read_only": True,
            "budget_usd": 3.25,
            "output_schema": governed_schema("attempt-claude"),
            "read_roots": [str(tmp_path.resolve())],
        }
    )

    assert options.model == "claude-opus-5"
    assert options.permission_mode == "default"
    assert options.tools == ["Read", "Grep", "Glob"]
    assert options.setting_sources == []
    assert options.skills == []
    assert options.mcp_servers == {}
    assert options.strict_mcp_config is True
    assert options.max_budget_usd == 3.25
    assert options.hooks is not None
    assert options.hooks["PreToolUse"][0].matcher == ("Read|Grep|Glob|StructuredOutput")
    assert options.can_use_tool is not None
    assert "private prompt" not in repr(options)


def test_claude_structured_output_packaging_latches_before_message_consumption(
    monkeypatch, tmp_path: Path, capsys
) -> None:
    import claude_agent_sdk
    from claude_agent_sdk import ResultMessage

    query_count = 0

    async def fake_query(**kwargs):
        nonlocal query_count
        query_count += 1
        options = kwargs["options"]
        assert options.hooks is not None
        hook = options.hooks["PreToolUse"][0].hooks[0]

        first_attempt = await hook(
            {
                "hook_event_name": "PreToolUse",
                "tool_name": "StructuredOutput",
                "tool_input": {"invalid": "first schema attempt"},
            },
            "structured-1",
            {"signal": None},
        )
        blocked_read = await hook(
            {
                "hook_event_name": "PreToolUse",
                "tool_name": "Read",
                "tool_input": {"file_path": "."},
            },
            "read-after-packaging",
            {"signal": None},
        )
        correction_attempt = await hook(
            {
                "hook_event_name": "PreToolUse",
                "tool_name": "StructuredOutput",
                "tool_input": {"corrected": "second schema attempt"},
            },
            "structured-2",
            {"signal": None},
        )

        assert first_attempt["hookSpecificOutput"]["permissionDecision"] == "allow"
        assert correction_attempt["hookSpecificOutput"]["permissionDecision"] == (
            "allow"
        )
        assert blocked_read["hookSpecificOutput"] == {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": (
                "result packaging has started; finish StructuredOutput using the "
                "collected evidence"
            ),
        }
        yield ResultMessage(
            subtype="error_max_structured_output_retries",
            duration_ms=1,
            duration_api_ms=1,
            is_error=True,
            num_turns=1,
            session_id="session-claude",
            result="private provider diagnostic",
        )

    monkeypatch.setattr(claude_agent_sdk, "query", fake_query)

    asyncio.run(
        sdk_bridge._run_claude(
            {
                "vendor": "claude",
                "prompt": "private prompt",
                "cwd": str(tmp_path),
                "requested_model": "claude-opus-5",
                "effort": "high",
                "read_only": True,
                "budget_usd": None,
                "commercial_mode": "subscription-only",
                "output_schema": governed_schema("attempt-claude"),
                "read_roots": [str(tmp_path.resolve())],
            }
        )
    )

    terminal = json.loads(capsys.readouterr().out.splitlines()[-1])
    assert query_count == 1
    assert terminal["status"] == "failed"
    assert terminal["terminal_reason"] == "process-exit"
    assert terminal["subtype"] == "error_max_structured_output_retries"


def test_claude_structured_output_packaging_stops_scoped_exploration(
    tmp_path: Path,
) -> None:
    import claude_agent_sdk
    evidence = tmp_path / "evidence"
    evidence.mkdir()
    corpus = evidence / "corpus.json"
    corpus.write_text("{}", encoding="utf-8")
    options = sdk_bridge._options(
        {
            "vendor": "claude",
            "prompt": "private prompt",
            "cwd": str(tmp_path),
            "requested_model": "claude-opus-5",
            "effort": "high",
            "read_only": True,
            "budget_usd": 3.25,
            "output_schema": governed_schema("attempt-claude"),
            "visible_tools": ["Read", "Bash"],
            "allowed_tools": [
                "Read(/evidence/**)",
                "Bash(sha256sum -- evidence/corpus.json)",
            ],
        }
    )
    assert options.hooks is not None
    assert options.hooks["PreToolUse"][0].matcher == "Read|Bash|StructuredOutput"
    hook = options.hooks["PreToolUse"][0].hooks[0]

    assert (
        asyncio.run(
            hook(
                {
                    "hook_event_name": "PreToolUse",
                    "tool_name": "Bash",
                    "tool_input": {"command": "sha256sum -- evidence/corpus.json"},
                },
                "bash-before-packaging",
                {"signal": None},
            )
        )
        == {}
    )
    packaging = asyncio.run(
        hook(
            {
                "hook_event_name": "PreToolUse",
                "tool_name": "StructuredOutput",
                "tool_input": {"first": "attempt"},
            },
            "structured-1",
            {"signal": None},
        )
    )
    blocked_bash = asyncio.run(
        hook(
            {
                "hook_event_name": "PreToolUse",
                "tool_name": "Bash",
                "tool_input": {"command": "sha256sum -- evidence/corpus.json"},
            },
            "bash-after-packaging",
            {"signal": None},
        )
    )

    assert packaging["hookSpecificOutput"]["permissionDecision"] == "allow"
    assert blocked_bash["hookSpecificOutput"]["permissionDecision"] == "deny"


@pytest.mark.parametrize("profile", ["default", "scoped"])
def test_claude_structured_output_packaging_hook_and_callback_agree(
    tmp_path: Path,
    profile: str,
) -> None:
    import claude_agent_sdk
    payload = {
        "vendor": "claude",
        "prompt": "private prompt",
        "cwd": str(tmp_path),
        "requested_model": "claude-opus-5",
        "effort": "high",
        "read_only": True,
        "budget_usd": 3.25,
        "output_schema": governed_schema("attempt-claude"),
        "read_roots": [str(tmp_path.resolve())],
    }
    tool_name = "Read"
    tool_input = {"file_path": "."}
    if profile == "scoped":
        evidence = tmp_path / "evidence"
        evidence.mkdir()
        corpus = evidence / "corpus.json"
        corpus.write_text("{}", encoding="utf-8")
        payload.update(
            {
                "visible_tools": ["Read", "Bash"],
                "allowed_tools": [
                    "Read(/evidence/**)",
                    "Bash(sha256sum -- evidence/corpus.json)",
                ],
            }
        )
        tool_name = "Bash"
        tool_input = {"command": "sha256sum -- evidence/corpus.json"}

    options = sdk_bridge._options(payload)
    assert options.hooks is not None
    hook = options.hooks["PreToolUse"][0].hooks[0]
    assert options.can_use_tool is not None
    permission = options.can_use_tool

    assert (
        asyncio.run(
            hook(
                {
                    "hook_event_name": "PreToolUse",
                    "tool_name": tool_name,
                    "tool_input": tool_input,
                },
                "exploration-before-packaging",
                {"signal": None},
            )
        )
        == {}
    )
    assert asyncio.run(permission(tool_name, tool_input, {})).behavior == "allow"

    packaging = asyncio.run(
        permission(
            "StructuredOutput",
            {"first": "schema attempt"},
            {},
        )
    )
    blocked_hook = asyncio.run(
        hook(
            {
                "hook_event_name": "PreToolUse",
                "tool_name": tool_name,
                "tool_input": tool_input,
            },
            "exploration-after-packaging",
            {"signal": None},
        )
    )
    blocked_permission = asyncio.run(permission(tool_name, tool_input, {}))
    correction = asyncio.run(
        permission(
            "StructuredOutput",
            {"corrected": "schema attempt"},
            {},
        )
    )

    assert packaging.behavior == "allow"
    assert blocked_hook["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert blocked_permission.behavior == "deny"
    assert blocked_permission.message == (
        "result packaging has started; finish StructuredOutput using the "
        "collected evidence"
    )
    assert correction.behavior == "allow"


@pytest.mark.parametrize(
    ("tool_name", "tool_input"),
    [
        ("Read", {"file_path": "sample.py"}),
        ("Grep", {"pattern": "needle", "path": "."}),
        ("Glob", {"pattern": "**/*.py", "path": "."}),
    ],
)
def test_sdk_bridge_default_read_hook_allows_in_worktree_calls(
    tmp_path: Path,
    tool_name: str,
    tool_input: dict[str, str],
) -> None:
    import claude_agent_sdk
    (tmp_path / "sample.py").write_text("needle\n", encoding="utf-8")
    options = sdk_bridge._options(
        {
            "vendor": "claude",
            "prompt": "private prompt",
            "cwd": str(tmp_path),
            "requested_model": "claude-opus-5",
            "effort": "high",
            "read_only": True,
            "budget_usd": 3.25,
            "output_schema": governed_schema("attempt-claude"),
            "read_roots": [str(tmp_path.resolve())],
        }
    )
    assert options.hooks is not None
    callback = options.hooks["PreToolUse"][0].hooks[0]

    result = asyncio.run(
        callback(
            {
                "hook_event_name": "PreToolUse",
                "tool_name": tool_name,
                "tool_input": tool_input,
            },
            "allowed-1",
            {"signal": None},
        )
    )

    assert result == {}
    assert options.can_use_tool is not None
    permission = asyncio.run(options.can_use_tool(tool_name, tool_input, {}))
    assert permission.behavior == "allow"


@pytest.mark.parametrize("tool_name", ["Read", "Grep", "Glob"])
def test_sdk_bridge_canonical_evidence_read_hook_and_callback_agree(
    monkeypatch, tmp_path: Path, tool_name: str
) -> None:
    import claude_agent_sdk
    installation = tmp_path / "installation"
    bridge = installation / "scripts" / "util" / "agent_runtimes" / "sdk_bridge.py"
    bridge.parent.mkdir(parents=True)
    bridge.touch()
    evidence = installation / ".audit" / "dispatch" / "results"
    evidence.mkdir(parents=True)
    sample = evidence / "review.json"
    sample.write_text('{"claim":"trusted"}\n', encoding="utf-8")
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    monkeypatch.setattr(sdk_bridge, "__file__", str(bridge))
    payload = {
        "vendor": "claude",
        "prompt": "private prompt",
        "cwd": str(worktree),
        "requested_model": "claude-opus-5",
        "effort": "high",
        "read_only": True,
        "budget_usd": 3.25,
        "output_schema": governed_schema("attempt-claude"),
        "read_roots": [str(worktree.resolve())],
        "evidence_read_roots": [str(evidence)],
    }
    request = sdk_bridge._validate_request_payload(payload)
    options = sdk_bridge._options(request)
    tool_input = (
        {"file_path": str(sample)}
        if tool_name == "Read"
        else {"pattern": "trusted", "path": str(evidence)}
        if tool_name == "Grep"
        else {"pattern": "*.json", "path": str(evidence)}
    )
    assert options.hooks is not None
    hook = options.hooks["PreToolUse"][0].hooks[0]

    hook_result = asyncio.run(
        hook(
            {
                "hook_event_name": "PreToolUse",
                "tool_name": tool_name,
                "tool_input": tool_input,
            },
            "evidence-read",
            {"signal": None},
        )
    )
    assert hook_result == {}
    assert options.can_use_tool is not None
    permission = asyncio.run(options.can_use_tool(tool_name, tool_input, {}))
    assert permission.behavior == "allow"


@pytest.mark.parametrize(
    ("payload_update", "message"),
    [
        ({"read_only": False}, "read-only Claude"),
        ({"vendor": "codex"}, "read-only Claude"),
        ({"visible_tools": ["Read"]}, "scoped Claude"),
    ],
)
def test_sdk_bridge_rejects_canonical_evidence_outside_default_read_only_claude(
    monkeypatch, tmp_path: Path, payload_update: dict[str, object], message: str
) -> None:
    installation = tmp_path / "installation"
    bridge = installation / "scripts" / "util" / "agent_runtimes" / "sdk_bridge.py"
    bridge.parent.mkdir(parents=True)
    bridge.touch()
    evidence = installation / ".audit" / "dispatch" / "results"
    evidence.mkdir(parents=True)
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    monkeypatch.setattr(sdk_bridge, "__file__", str(bridge))
    payload: dict[str, object] = {
        "vendor": "claude",
        "prompt": "private prompt",
        "cwd": str(worktree),
        "requested_model": "claude-opus-5",
        "effort": "high",
        "read_only": True,
        "budget_usd": 3.25,
        "output_schema": governed_schema("attempt-claude"),
        "read_roots": [str(worktree.resolve())],
        "evidence_read_roots": [str(evidence)],
    }
    payload.update(payload_update)

    with pytest.raises(sdk_bridge.BridgeInputError, match=message):
        sdk_bridge._validate_request_payload(payload)


@pytest.mark.parametrize("variant", ["arbitrary", "duplicate", "traversal", "symlink"])
def test_sdk_bridge_rejects_unsafe_canonical_evidence_roots(
    monkeypatch, tmp_path: Path, variant: str
) -> None:
    installation = tmp_path / "installation"
    bridge = installation / "scripts" / "util" / "agent_runtimes" / "sdk_bridge.py"
    bridge.parent.mkdir(parents=True)
    bridge.touch()
    evidence = installation / ".audit" / "dispatch" / "results"
    evidence.mkdir(parents=True)
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    monkeypatch.setattr(sdk_bridge, "__file__", str(bridge))
    roots = [str(evidence)]
    if variant == "arbitrary":
        arbitrary = tmp_path / "results"
        arbitrary.mkdir()
        roots = [str(arbitrary)]
    elif variant == "duplicate":
        roots.append(str(evidence))
    elif variant == "traversal":
        roots = [str(evidence.parent / "ignored" / ".." / "results")]
    else:
        evidence.rmdir()
        target = tmp_path / "external-results"
        target.mkdir()
        evidence.symlink_to(target, target_is_directory=True)

    with pytest.raises(sdk_bridge.BridgeInputError, match="evidence_read_roots"):
        sdk_bridge._validate_request_payload(
            {
                "vendor": "claude",
                "prompt": "private prompt",
                "cwd": str(worktree),
                "requested_model": "claude-opus-5",
                "effort": "high",
                "read_only": True,
                "budget_usd": 3.25,
                "output_schema": governed_schema("attempt-claude"),
                "read_roots": [str(worktree.resolve())],
                "evidence_read_roots": roots,
            }
        )


@pytest.mark.parametrize(
    ("tool_name", "tool_input"),
    [
        ("Read", {"file_path": "../outside.py"}),
        ("Grep", {"pattern": "needle", "path": "../outside"}),
        ("Glob", {"pattern": "../**/*.py", "path": "."}),
        ("Write", {"file_path": "sample.py", "content": "changed"}),
    ],
)
def test_sdk_bridge_default_read_hook_denies_out_of_scope_calls(
    tmp_path: Path,
    tool_name: str,
    tool_input: dict[str, str],
) -> None:
    import claude_agent_sdk
    options = sdk_bridge._options(
        {
            "vendor": "claude",
            "prompt": "private prompt",
            "cwd": str(tmp_path),
            "requested_model": "claude-opus-5",
            "effort": "high",
            "read_only": True,
            "budget_usd": 3.25,
            "output_schema": governed_schema("attempt-claude"),
            "read_roots": [str(tmp_path.resolve())],
        }
    )
    assert options.hooks is not None
    callback = options.hooks["PreToolUse"][0].hooks[0]

    result = asyncio.run(
        callback(
            {
                "hook_event_name": "PreToolUse",
                "tool_name": tool_name,
                "tool_input": tool_input,
            },
            "blocked-1",
            {"signal": None},
        )
    )

    assert result["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert options.can_use_tool is not None
    permission = asyncio.run(options.can_use_tool(tool_name, tool_input, {}))
    assert permission.behavior == "deny"


def test_sdk_bridge_keeps_visibility_separate_from_authorization(
    tmp_path: Path,
) -> None:
    import claude_agent_sdk
    options = sdk_bridge._options(
        {
            "vendor": "claude",
            "prompt": "private prompt",
            "cwd": str(tmp_path),
            "requested_model": "claude-opus-5",
            "effort": "high",
            "read_only": True,
            "budget_usd": 3.25,
            "visible_tools": ["Read"],
            "allowed_tools": [],
        }
    )

    assert options.tools == ["Read"]
    assert options.allowed_tools == []
    assert options.permission_mode == "default"


def test_sdk_bridge_exact_read_and_hash_scope_allows_only_declared_calls(
    tmp_path: Path,
) -> None:
    import claude_agent_sdk
    evidence = tmp_path / "evidence"
    evidence.mkdir()
    corpus = evidence / "corpus.json"
    corpus.write_text("{}", encoding="utf-8")
    options = sdk_bridge._options(
        {
            "vendor": "claude",
            "prompt": "private prompt",
            "cwd": str(tmp_path),
            "requested_model": "claude-opus-5",
            "effort": "high",
            "read_only": True,
            "budget_usd": 3.25,
            "visible_tools": ["Read", "Bash"],
            "allowed_tools": [
                "Read(/evidence/**)",
                "Bash(sha256sum -- evidence/corpus.json)",
            ],
        }
    )

    assert options.tools == ["Read", "Bash"]
    assert options.allowed_tools == []
    assert options.hooks is not None
    callback = options.hooks["PreToolUse"][0].hooks[0]

    assert (
        asyncio.run(
            callback(
                {
                    "hook_event_name": "PreToolUse",
                    "tool_name": "Read",
                    "tool_input": {"file_path": str(corpus)},
                },
                "read-1",
                {"signal": None},
            )
        )
        == {}
    )
    assert options.can_use_tool is not None
    read_permission = asyncio.run(
        options.can_use_tool("Read", {"file_path": str(corpus)}, {})
    )
    hash_permission = asyncio.run(
        options.can_use_tool(
            "Bash", {"command": "sha256sum -- evidence/corpus.json"}, {}
        )
    )
    assert read_permission.behavior == "allow"
    assert hash_permission.behavior == "allow"
    assert (
        asyncio.run(
            callback(
                {
                    "hook_event_name": "PreToolUse",
                    "tool_name": "Bash",
                    "tool_input": {"command": "sha256sum -- evidence/corpus.json"},
                },
                "bash-1",
                {"signal": None},
            )
        )
        == {}
    )


@pytest.mark.parametrize(
    ("tool_name", "tool_input"),
    [
        ("Bash", {"command": "curl https://example.invalid"}),
        ("Bash", {"command": "printf compromised > evidence/corpus.json"}),
        ("Bash", {"command": "sha256sum -- evidence/other.json"}),
        ("Grep", {"pattern": "secret", "path": "evidence"}),
        ("Write", {"file_path": "evidence/new.txt", "content": "x"}),
        ("Read", {"file_path": "outside.json"}),
        ("Read", {"file_path": "../other-worktree/secret.txt"}),
    ],
)
def test_sdk_bridge_scope_hook_denies_unrelated_tools_and_cross_worktree_paths(
    tmp_path: Path,
    tool_name: str,
    tool_input: dict[str, str],
) -> None:
    import claude_agent_sdk
    evidence = tmp_path / "evidence"
    evidence.mkdir()
    (evidence / "corpus.json").write_text("{}", encoding="utf-8")
    (evidence / "other.json").write_text("{}", encoding="utf-8")
    (tmp_path / "outside.json").write_text("{}", encoding="utf-8")
    options = sdk_bridge._options(
        {
            "vendor": "claude",
            "prompt": "private prompt",
            "cwd": str(tmp_path),
            "requested_model": "claude-opus-5",
            "effort": "high",
            "read_only": True,
            "budget_usd": 3.25,
            "visible_tools": ["Read", "Grep", "Bash"],
            "allowed_tools": [
                "Read(/evidence/**)",
                "Bash(sha256sum -- evidence/corpus.json)",
            ],
        }
    )
    assert options.hooks is not None
    callback = options.hooks["PreToolUse"][0].hooks[0]

    result = asyncio.run(
        callback(
            {
                "hook_event_name": "PreToolUse",
                "tool_name": tool_name,
                "tool_input": tool_input,
            },
            "blocked-1",
            {"signal": None},
        )
    )

    assert result["hookSpecificOutput"]["permissionDecision"] == "deny"
    serialized = json.dumps(result)
    assert all(value not in serialized for value in tool_input.values())


def test_sdk_bridge_scope_hook_fails_closed_for_unresolvable_read_path(
    tmp_path: Path,
) -> None:
    import claude_agent_sdk
    loop = tmp_path / "loop"
    loop.symlink_to(loop)
    options = sdk_bridge._options(
        {
            "vendor": "claude",
            "prompt": "private prompt",
            "cwd": str(tmp_path),
            "requested_model": "claude-opus-5",
            "effort": "high",
            "read_only": True,
            "budget_usd": 3.25,
            "visible_tools": ["Read"],
            "allowed_tools": ["Read(/evidence/**)"],
        }
    )
    assert options.hooks is not None
    callback = options.hooks["PreToolUse"][0].hooks[0]

    result = asyncio.run(
        callback(
            {
                "hook_event_name": "PreToolUse",
                "tool_name": "Read",
                "tool_input": {"file_path": str(loop)},
            },
            "blocked-loop",
            {"signal": None},
        )
    )

    assert result["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_sdk_bridge_scope_hook_fails_closed_when_hash_path_becomes_unresolvable(
    tmp_path: Path,
) -> None:
    import claude_agent_sdk
    evidence = tmp_path / "evidence"
    evidence.mkdir()
    corpus = evidence / "corpus.json"
    corpus.write_text("{}", encoding="utf-8")
    options = sdk_bridge._options(
        {
            "vendor": "claude",
            "prompt": "private prompt",
            "cwd": str(tmp_path),
            "requested_model": "claude-opus-5",
            "effort": "high",
            "read_only": True,
            "budget_usd": 3.25,
            "visible_tools": ["Bash"],
            "allowed_tools": ["Bash(sha256sum -- evidence/corpus.json)"],
        }
    )
    corpus.unlink()
    corpus.symlink_to(corpus)
    assert options.hooks is not None
    callback = options.hooks["PreToolUse"][0].hooks[0]

    result = asyncio.run(
        callback(
            {
                "hook_event_name": "PreToolUse",
                "tool_name": "Bash",
                "tool_input": {"command": "sha256sum -- evidence/corpus.json"},
            },
            "blocked-loop",
            {"signal": None},
        )
    )

    assert result["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_sdk_bridge_scoped_options_do_not_leak_to_later_default_request(
    tmp_path: Path,
) -> None:
    import claude_agent_sdk
    sdk_bridge._options(
        {
            "vendor": "claude",
            "prompt": "private prompt",
            "cwd": str(tmp_path),
            "requested_model": "claude-opus-5",
            "effort": "high",
            "read_only": True,
            "budget_usd": 3.25,
            "visible_tools": ["Read", "Bash"],
            "allowed_tools": ["Read(/evidence/**)"],
        }
    )

    default_options = sdk_bridge._options(
        {
            "vendor": "claude",
            "prompt": "private prompt",
            "cwd": str(tmp_path),
            "requested_model": "claude-opus-5",
            "effort": "high",
            "read_only": True,
            "budget_usd": 3.25,
        }
    )

    assert default_options.tools == ["Read", "Grep", "Glob"]
    assert default_options.allowed_tools == []
    assert default_options.hooks is not None


@pytest.mark.parametrize(
    ("visible_tools", "allowed_tools"),
    [
        (["Write"], []),
        (["Read"], ["Read"]),
        (["Read"], ["Read(//tmp/**)"]),
        (["Read"], ["Read(/../other/**)"]),
        (["Bash"], ["Bash(curl https://example.invalid)"]),
        (["Bash"], ["Bash(sha256sum -- ../other/file)"]),
        (["Bash"], ["Bash(sha256sum -- evidence/file >> evidence/hash)"]),
        (["Bash"], ["Bash(sha256sum -- evidence/*.json)"]),
        (["Bash"], ["Bash(sha256sum -- ~/secret)"]),
        (["Read"], ["Bash(sha256sum -- evidence/corpus.json)"]),
    ],
)
def test_sdk_bridge_rejects_broad_or_cross_worktree_authorization_rules(
    tmp_path: Path,
    visible_tools: list[str],
    allowed_tools: list[str],
) -> None:
    with pytest.raises(sdk_bridge.BridgeInputError):
        sdk_bridge.validate_scoped_tool_configuration(
            cwd=tmp_path,
            read_only=True,
            visible_tools=visible_tools,
            allowed_tools=allowed_tools,
        )


def test_codex_login_status_requires_chatgpt_subscription() -> None:
    parsed = codex.parse_codex_login_status("Logged in using ChatGPT\n")

    assert parsed.logged_in is True
    assert parsed.auth_method == "chatgpt"
    assert "private@example.invalid" not in repr(parsed)
    assert parsed.has_subscription is True


@pytest.mark.parametrize(
    "status",
    [
        "Logged in using an API key - sk-test",
        "Logged in using Amazon Bedrock API key",
        "Logged in using personal access token",
        "Logged in using access token",
        "Not logged in",
        "Not logged in. Run codex login to use ChatGPT",
        "",
    ],
)
def test_codex_login_status_rejects_non_chatgpt_paths(status: str) -> None:
    with pytest.raises(codex.CodexProtocolError):
        codex.parse_codex_login_status(status)


@pytest.mark.parametrize(
    "payload",
    [
        {"account": {"type": "apiKey"}, "requires_openai_auth": True},
        {
            "account": {"type": "amazonBedrock", "credential_source": "awsManaged"},
            "requires_openai_auth": False,
        },
        {"account": None, "requires_openai_auth": True},
    ],
)
def test_codex_account_rejects_api_key_and_bedrock(payload: dict) -> None:
    with pytest.raises(codex.CodexProtocolError):
        codex.parse_codex_account(payload)


def test_codex_account_accepts_chatgpt_without_retaining_identity() -> None:
    parsed = codex.parse_codex_account(
        {
            "account": {
                "type": "chatgpt",
                "email": "private@example.invalid",
                "plan_type": "pro",
            },
            "requires_openai_auth": True,
        }
    )

    assert parsed.auth_method == "chatgpt"


def test_sdk_bridge_consumes_guardian_codex_auth_fd(monkeypatch) -> None:
    read_fd, write_fd = os.pipe()
    os.write(write_fd, b'{"tokens":{"access_token":"bounded-test"}}')
    os.close(write_fd)
    monkeypatch.setenv(sdk_bridge.CODEX_AUTH_FD_ENV, str(read_fd))

    directory, environment, auth_path = (
        sdk_bridge._materialize_codex_subscription_auth()
    )

    assert directory is not None
    assert auth_path is not None
    assert environment == {"CODEX_HOME": directory.name}
    assert json.loads(auth_path.read_text(encoding="utf-8"))["tokens"]
    assert auth_path.stat().st_mode & 0o077 == 0
    with pytest.raises(OSError):
        os.fstat(read_fd)
    directory.cleanup()


def test_codex_stream_normalizes_thread_turn_usage_and_keeps_output_private() -> None:
    parsed = codex.parse_codex_stream(
        "\n".join(
            [
                json.dumps(
                    {
                        "type": "event",
                        "kind": "system",
                        "subtype": "thread_started",
                        "session_id": "thread-1",
                        "effective_model": "gpt-5.6-sol",
                        "semantic": False,
                    }
                ),
                json.dumps(
                    {
                        "type": "event",
                        "kind": "system",
                        "subtype": "turn_started",
                        "session_id": "thread-1",
                        "request_id": "turn-1",
                        "semantic": False,
                    }
                ),
                json.dumps(
                    {
                        "type": "event",
                        "kind": "assistant",
                        "subtype": "message",
                        "session_id": "thread-1",
                        "request_id": "turn-1",
                        "semantic": True,
                    }
                ),
                json.dumps(
                    {
                        "type": "result",
                        "status": "completed",
                        "terminal_reason": "completed",
                        "session_id": "thread-1",
                        "request_id": "turn-1",
                        "effective_model": "gpt-5.6-sol",
                        "output": "secret codex answer",
                        "usage": {
                            "input_tokens": 11,
                            "output_tokens": 7,
                            "cachedInputTokens": 3,
                            "reasoningOutputTokens": 2,
                        },
                    }
                ),
            ]
        )
    )

    assert parsed.session_id == "thread-1"
    assert parsed.request_id == "turn-1"
    assert parsed.effective_model == "gpt-5.6-sol"
    assert parsed.usage is not None
    assert parsed.usage.input_tokens == 11
    assert parsed.usage.output_tokens == 7
    assert parsed.usage.cache_read_tokens == 3
    assert parsed.usage.reasoning_tokens == 2
    assert parsed.final_output == "secret codex answer"
    assert "secret codex answer" not in repr(parsed)


def test_codex_environment_removes_openai_gateway_and_raw_api_paths() -> None:
    environment = codex.filtered_codex_environment(
        {
            "OPENAI_API_KEY": "raw-openai-key",
            "OPENAI_BASE_URL": "https://gateway.invalid",
            "OPENAI_API_BASE": "https://other-gateway.invalid",
            "CODEX_API_KEY": "raw-codex-key",
            "NORMAL_LOGIN_MARKER": "kept",
        }
    )

    assert environment["NORMAL_LOGIN_MARKER"] == "kept"
    assert not codex.CODEX_BLOCKED_ENV_VARS.intersection(environment)
    assert "OPENAI_API_KEY" not in environment


def test_codex_bridge_thread_kwargs_force_first_party_provider_and_deny_all(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import openai_codex
    kwargs = sdk_bridge._codex_thread_kwargs(
        {
            "vendor": "codex",
            "prompt": "private prompt",
            "cwd": str(tmp_path),
            "requested_model": "gpt-5.6-sol",
            "effort": "max",
            "read_only": True,
            "budget_usd": None,
            "output_schema": governed_schema("attempt-codex"),
            "read_roots": [str(tmp_path.resolve())],
        }
    )
    turn_kwargs = sdk_bridge._codex_turn_kwargs(
        {
            "vendor": "codex",
            "prompt": "private prompt",
            "cwd": str(tmp_path),
            "requested_model": "gpt-5.6-sol",
            "effort": "max",
            "read_only": False,
            "budget_usd": None,
            "output_schema": governed_schema("attempt-codex"),
            "read_roots": [str(tmp_path.resolve())],
        }
    )

    assert kwargs["model_provider"] == "openai"
    assert kwargs["service_tier"] == "default"
    assert kwargs["ephemeral"] is True
    assert kwargs["approval_mode"].value == "deny_all"
    assert kwargs["sandbox"].value == "read-only"
    assert turn_kwargs["service_tier"] == "default"
    assert turn_kwargs["approval_mode"].value == "deny_all"
    assert turn_kwargs["sandbox"].value == "workspace-write"
    assert turn_kwargs["effort"].value == "xhigh"
    assert "private prompt" not in repr(kwargs)
    assert "private prompt" not in repr(turn_kwargs)

    monkeypatch.setenv(sdk_bridge.OUTER_WORKER_SANDBOX_ENV, "1")
    isolated_kwargs = sdk_bridge._codex_thread_kwargs(
        {
            "vendor": "codex",
            "prompt": "private prompt",
            "cwd": str(tmp_path),
            "requested_model": "gpt-5.6-sol",
            "effort": "high",
            "read_only": True,
            "budget_usd": None,
            "output_schema": governed_schema("attempt-codex"),
            "read_roots": [str(tmp_path.resolve())],
        }
    )
    assert isolated_kwargs["sandbox"].value == "full-access"

    isolated_write_kwargs = sdk_bridge._codex_thread_kwargs(
        {
            "vendor": "codex",
            "prompt": "private prompt",
            "cwd": str(tmp_path),
            "requested_model": "gpt-5.6-sol",
            "effort": "high",
            "read_only": False,
            "budget_usd": None,
            "output_schema": governed_schema("attempt-codex"),
            "read_roots": [str(tmp_path.resolve())],
        }
    )
    assert isolated_write_kwargs["sandbox"].value == "full-access"


def test_codex_sdk_progress_maps_fake_stream_without_retaining_sensitive_content(
    monkeypatch, tmp_path: Path, capsys
) -> None:
    import openai_codex
    from openai_codex.generated.v2_all import (
        AgentMessageThreadItem,
        CommandExecutionThreadItem,
        ItemCompletedNotification,
        ItemStartedNotification,
        ThreadItem,
        Turn,
        TurnCompletedNotification,
    )
    from openai_codex.models import Notification

    sensitive_prompt = "private codex prompt alpha"
    sensitive_command = "cat /secret/codex/private.txt"
    sensitive_path = "/secret/codex"
    sensitive_output = "private codex model output omega"
    structured_output = {
        "task_id": "attempt-codex",
        "status": "completed",
        "summary": sensitive_output,
    }
    command_item = ThreadItem(
        root=CommandExecutionThreadItem(
            id="private-command-id",
            command=sensitive_command,
            commandActions=[],
            cwd=sensitive_path,
            status="inProgress",
            type="commandExecution",
        )
    )
    message_item = ThreadItem(
        root=AgentMessageThreadItem(
            id="private-message-id",
            text=json.dumps(structured_output),
            phase="final_answer",
            type="agentMessage",
        )
    )
    notifications = [
        Notification(
            "item/started",
            ItemStartedNotification(
                item=command_item,
                startedAtMs=1,
                threadId="private-thread",
                turnId="private-turn",
            ),
        ),
        Notification(
            "item/completed",
            ItemCompletedNotification(
                completedAtMs=2,
                item=message_item,
                threadId="private-thread",
                turnId="private-turn",
            ),
        ),
        Notification(
            "turn/completed",
            TurnCompletedNotification(
                threadId="private-thread",
                turn=Turn(
                    id="private-turn",
                    items=[],
                    status="completed",
                ),
            ),
        ),
    ]

    class FakeStream(list):
        def close(self) -> None:
            return None

    class FakeTurn:
        id = "private-turn"

        def stream(self):
            return FakeStream(notifications)

    class FakeThread:
        id = "private-thread"

        def turn(self, prompt, **_kwargs):
            assert prompt == sensitive_prompt
            return FakeTurn()

    class FakeCodex:
        def __init__(self, _config) -> None:
            return None

        def __enter__(self):
            return self

        def __exit__(self, *_args) -> None:
            return None

        def thread_start(self, **_kwargs):
            return FakeThread()

    @contextmanager
    def fake_config_lock(_request):
        path = tmp_path / "runtime.config.lock.toml"
        path.write_text("version = 1\n", encoding="utf-8")
        yield path

    monkeypatch.setattr(openai_codex, "Codex", FakeCodex)
    monkeypatch.setattr(sdk_bridge, "_codex_effective_config_lock", fake_config_lock)
    monkeypatch.setattr(sdk_bridge, "_enforce_codex_chatgpt_login", lambda _codex: None)
    reporter = _ProgressRecorder()

    sdk_bridge._run_codex(
        {
            "vendor": "codex",
            "prompt": sensitive_prompt,
            "cwd": str(tmp_path),
            "requested_model": "gpt-5.6-sol",
            "effort": "high",
            "read_only": True,
            "budget_usd": None,
            "commercial_mode": "subscription-only",
            "output_schema": governed_schema("attempt-codex"),
            "read_roots": [str(tmp_path.resolve())],
        },
        reporter=reporter,
    )

    assert reporter.phases == [
        contracts.RuntimePhase.MODEL_ACTIVE,
        contracts.RuntimePhase.TOOL_EXECUTION,
        contracts.RuntimePhase.MODEL_ACTIVE,
        contracts.RuntimePhase.RESULT_PACKAGING,
    ]
    assert (
        contracts.RuntimePhase.TOOL_EXECUTION,
        contracts.RuntimeToolLabel.COMMAND,
    ) in reporter.observations
    retained = repr(reporter.phases)
    assert sensitive_prompt not in retained
    assert sensitive_command not in retained
    assert sensitive_path not in retained
    assert sensitive_output not in retained
    capsys.readouterr()


def test_codex_completed_item_retention_is_constant_and_size_bounded() -> None:
    import openai_codex
    from openai_codex.generated.v2_all import (
        AgentMessageThreadItem,
        CommandExecutionThreadItem,
        ThreadItem,
    )

    final_text: str | None = None
    latest_text: str | None = None
    for index in range(100):
        item = ThreadItem(
            root=AgentMessageThreadItem(
                id=f"message-{index}",
                text=f"message {index}",
                phase="commentary",
                type="agentMessage",
            )
        )
        final_text, latest_text = sdk_bridge._retain_codex_response_text(
            final_text,
            latest_text,
            item,
        )

    tool_item = ThreadItem(
        root=CommandExecutionThreadItem(
            id="tool",
            command="true",
            commandActions=[],
            cwd="/tmp",
            status="completed",
            type="commandExecution",
        )
    )
    assert sdk_bridge._retain_codex_response_text(
        final_text,
        latest_text,
        tool_item,
    ) == (None, "message 99")

    oversized = ThreadItem(
        root=AgentMessageThreadItem(
            id="oversized",
            text="x" * (sdk_bridge.MAX_FINAL_OUTPUT_BYTES + 1),
            phase="final_answer",
            type="agentMessage",
        )
    )
    with pytest.raises(sdk_bridge.BridgeInputError, match="safe limit"):
        sdk_bridge._retain_codex_response_text(final_text, latest_text, oversized)


def test_codex_bootstrap_thread_kwargs_replace_project_working_directory(
    tmp_path: Path,
) -> None:
    import openai_codex
    project = tmp_path / "project"
    project.mkdir()
    isolated = tmp_path / "isolated"
    isolated.mkdir()
    bridge_request = {
        "vendor": "codex",
        "prompt": "private prompt",
        "cwd": str(project),
        "requested_model": "gpt-5.6-sol",
        "effort": "high",
        "read_only": True,
        "budget_usd": None,
    }

    kwargs = sdk_bridge._codex_bootstrap_thread_kwargs(
        bridge_request,
        isolated_cwd=isolated,
    )

    assert kwargs["cwd"] == str(isolated)
    assert str(project) not in repr(kwargs)


def test_codex_app_server_probe_consumes_protected_auth_and_checks_chatgpt_account(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    import openai_codex

    credential = tmp_path / "auth.json"
    credential.write_text(json.dumps(_protected_codex_auth()), encoding="utf-8")
    read_fd = os.open(credential, os.O_RDONLY)
    monkeypatch.setenv(sdk_bridge.CODEX_AUTH_FD_ENV, str(read_fd))
    invoked: list[object] = []
    observed_homes: list[Path] = []
    account_calls: list[None] = []
    thread_calls: list[None] = []

    class FakeConfig:
        def __init__(self, **kwargs) -> None:
            self.env = kwargs["env"]

    class FakeAccountResponse:
        def model_dump(self) -> dict[str, object]:
            return {
                "account": {
                    "type": "chatgpt",
                    "email": "private@example.invalid",
                }
            }

    class FakeCodex:
        def __init__(self, config) -> None:
            home = Path(config.env["CODEX_HOME"])
            observed_homes.append(home)
            assert json.loads((home / "auth.json").read_text(encoding="utf-8"))[
                "tokens"
            ]

        def __enter__(self):
            return self

        def __exit__(self, *_args) -> None:
            return None

        def account(self):
            account_calls.append(None)
            return FakeAccountResponse()

        def thread_start(self, **_kwargs):
            thread_calls.append(None)
            raise AssertionError("readiness probe must not start a model thread")

    @contextmanager
    def fake_config_lock(request):
        invoked.append(request)
        yield tmp_path / "runtime.config.lock.toml"

    monkeypatch.setattr(openai_codex, "Codex", FakeCodex)
    monkeypatch.setattr(openai_codex, "CodexConfig", FakeConfig)
    monkeypatch.setattr(sdk_bridge, "_codex_effective_config_lock", fake_config_lock)
    request = {
        "vendor": "codex-probe",
        "prompt": "",
        "cwd": str(tmp_path),
        "requested_model": "gpt-5.6-sol",
        "effort": "high",
        "read_only": True,
        "budget_usd": None,
        "commercial_mode": "subscription-only",
        "output_schema": {},
        "read_roots": [str(tmp_path.resolve())],
    }

    sdk_bridge._probe_codex_app_server(request)

    assert invoked == [request]
    assert account_calls == [None]
    assert thread_calls == []
    assert sdk_bridge.CODEX_AUTH_FD_ENV not in os.environ
    with pytest.raises(OSError):
        os.fstat(read_fd)
    assert observed_homes and not observed_homes[0].exists()
    assert capsys.readouterr().out == '{"type":"readiness","status":"ready"}\n'


def test_codex_app_server_probe_rejects_non_chatgpt_account_without_a_turn(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    import openai_codex

    credential = tmp_path / "auth.json"
    credential.write_text(json.dumps(_protected_codex_auth()), encoding="utf-8")
    read_fd = os.open(credential, os.O_RDONLY)
    monkeypatch.setenv(sdk_bridge.CODEX_AUTH_FD_ENV, str(read_fd))
    observed_homes: list[Path] = []
    thread_calls: list[None] = []

    class FakeConfig:
        def __init__(self, **kwargs) -> None:
            self.env = kwargs["env"]

    class FakeAccountResponse:
        def model_dump(self) -> dict[str, object]:
            return {
                "account": {
                    "type": "apiKey",
                    "email": "private@example.invalid",
                }
            }

    class FakeCodex:
        def __init__(self, config) -> None:
            observed_homes.append(Path(config.env["CODEX_HOME"]))

        def __enter__(self):
            return self

        def __exit__(self, *_args) -> None:
            return None

        def account(self):
            return FakeAccountResponse()

        def thread_start(self, **_kwargs):
            thread_calls.append(None)

    @contextmanager
    def fake_config_lock(_request):
        yield tmp_path / "runtime.config.lock.toml"

    monkeypatch.setattr(openai_codex, "Codex", FakeCodex)
    monkeypatch.setattr(openai_codex, "CodexConfig", FakeConfig)
    monkeypatch.setattr(sdk_bridge, "_codex_effective_config_lock", fake_config_lock)

    sdk_bridge._probe_codex_app_server(
        {
            "vendor": "codex-probe",
            "prompt": "",
            "cwd": str(tmp_path),
            "requested_model": "gpt-5.6-sol",
            "effort": "high",
            "read_only": True,
            "budget_usd": None,
            "commercial_mode": "subscription-only",
            "output_schema": {},
            "read_roots": [str(tmp_path.resolve())],
        }
    )

    assert thread_calls == []
    assert sdk_bridge.CODEX_AUTH_FD_ENV not in os.environ
    with pytest.raises(OSError):
        os.fstat(read_fd)
    assert observed_homes and not observed_homes[0].exists()
    output = capsys.readouterr().out
    assert output == ('{"type":"readiness","status":"failed","failure":"bootstrap"}\n')
    assert "private@example.invalid" not in output


def _codex_auth_process_result() -> object:
    return process.ProcessResult(
        returncode=0,
        stdout="Logged in using ChatGPT\n",
        stderr="",
        duration_s=0.01,
        timed_out=False,
    )


def _test_jwt(*, expires_at: int) -> str:
    def segment(payload: dict[str, object]) -> str:
        encoded = base64.urlsafe_b64encode(
            json.dumps(payload, separators=(",", ":")).encode()
        )
        return encoded.rstrip(b"=").decode()

    return f"{segment({'alg': 'none'})}.{segment({'exp': expires_at})}.signature"


def _protected_codex_auth(*, access_exp: int = 10**12, id_exp: int = 10**12):
    return {
        "auth_mode": "chatgpt",
        "OPENAI_API_KEY": None,
        "tokens": {
            "access_token": _test_jwt(expires_at=access_exp),
            "id_token": _test_jwt(expires_at=id_exp),
            "refresh_token": "refresh-through-pinned-app-server",
        },
    }


def test_codex_readiness_accepts_chatgpt_status_from_stderr(tmp_path: Path) -> None:
    adapter = codex.CodexAdapter(
        run_probe=lambda command, **_kwargs: (
            process.ProcessResult(
                returncode=0,
                stdout='{"type":"readiness","status":"ready"}\n',
                stderr="private vendor output",
                duration_s=0.01,
                timed_out=False,
            )
            if str(command[0]).endswith("python")
            else process.ProcessResult(
                returncode=0,
                stdout="",
                stderr="Logged in using ChatGPT\n",
                duration_s=0.01,
                timed_out=False,
            )
        ),
        sdk_available=lambda *_args: True,
        which=lambda _name: "/usr/bin/codex",
    )

    readiness = adapter.probe_sdk(codex_request(tmp_path))

    assert readiness.ready is True
    assert readiness.eligibility is contracts.SubscriptionEligibility.APPROVED


def test_codex_sdk_readiness_accepts_protected_subscription_descriptor(
    monkeypatch, tmp_path: Path
) -> None:
    credential = tmp_path / "auth.json"
    credential.write_text(json.dumps(_protected_codex_auth()))
    fd = os.open(credential, os.O_RDONLY)
    monkeypatch.setenv(codex.CODEX_AUTH_FD_ENV, str(fd))
    calls: list[tuple[str, ...]] = []

    def probe(command, **_kwargs):
        calls.append(tuple(map(str, command)))
        return process.ProcessResult(
            returncode=0,
            stdout='{"type":"readiness","status":"ready"}\n',
            stderr="",
            duration_s=0.01,
            timed_out=False,
        )

    adapter = codex.CodexAdapter(
        run_probe=probe,
        sdk_available=lambda *_args: True,
        which=lambda _name: "/usr/bin/codex",
    )
    try:
        readiness = adapter.probe_sdk(codex_request(tmp_path))
        cli = adapter.probe_cli(codex_request(tmp_path))
    finally:
        os.close(fd)

    assert readiness.ready is True
    assert len(calls) == 1
    assert cli.ready is False
    assert cli.transport == codex.CODEX_CLI_TRANSPORT


def test_codex_sdk_readiness_allows_app_server_to_refresh_identity_token(
    monkeypatch, tmp_path: Path
) -> None:
    credential = tmp_path / "auth.json"
    credential.write_text(json.dumps(_protected_codex_auth(id_exp=1)))
    fd = os.open(credential, os.O_RDONLY)
    monkeypatch.setenv(codex.CODEX_AUTH_FD_ENV, str(fd))
    probe = MagicMock(
        return_value=process.ProcessResult(
            returncode=0,
            stdout='{"type":"readiness","status":"ready"}\n',
            stderr="",
            duration_s=0.01,
            timed_out=False,
        )
    )
    adapter = codex.CodexAdapter(
        run_probe=probe,
        sdk_available=lambda *_args: True,
        which=lambda _name: "/usr/bin/codex",
    )
    try:
        readiness = adapter.probe_sdk(codex_request(tmp_path))
    finally:
        os.close(fd)

    assert readiness.ready is True
    assert readiness.transport == codex.CODEX_SDK_TRANSPORT
    probe.assert_called_once()


def test_codex_protected_credential_allows_app_server_to_refresh_expired_tokens() -> (
    None
):
    payload = json.dumps(
        {
            "auth_mode": "chatgpt",
            "OPENAI_API_KEY": None,
            "tokens": {
                "access_token": _test_jwt(expires_at=1_059),
                "id_token": _test_jwt(expires_at=1_061),
                "refresh_token": "refresh-through-pinned-app-server",
            },
        }
    ).encode()

    assert codex.protected_codex_credential_ready(payload, now_s=1_000) is True


@pytest.mark.parametrize(
    "mutation",
    [
        {"auth_mode": "apikey"},
        {"OPENAI_API_KEY": "raw-api-path-is-forbidden"},
        {"tokens": {"refresh_token": ""}},
    ],
)
def test_codex_protected_credential_rejects_non_subscription_shape(
    mutation: dict[str, object],
) -> None:
    credential = _protected_codex_auth()
    credential.update(mutation)

    assert (
        codex.protected_codex_credential_ready(json.dumps(credential).encode()) is False
    )


def test_codex_protected_credential_rejects_non_utf8_token() -> None:
    credential = _protected_codex_auth()
    credential["tokens"]["access_token"] = "invalid-surrogate-\ud800"

    assert (
        codex.protected_codex_credential_ready(json.dumps(credential).encode()) is False
    )


@pytest.mark.parametrize("error", [ValueError("invalid"), RecursionError("nested")])
def test_codex_protected_credential_parse_failures_are_closed(
    monkeypatch: pytest.MonkeyPatch,
    error: Exception,
) -> None:
    def failing_loads(_payload):
        raise error

    monkeypatch.setattr(codex.json, "loads", failing_loads)
    payload = b'{"auth_mode":"chatgpt"}'

    assert codex.protected_codex_credential_ready(payload) is False


def test_codex_protected_credential_keeps_sdk_failure_when_cli_is_forbidden(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    credential = tmp_path / "auth.json"
    credential.write_text(json.dumps(_protected_codex_auth()))
    fd = os.open(credential, os.O_RDONLY)
    monkeypatch.setenv(codex.CODEX_AUTH_FD_ENV, str(fd))
    adapter = codex.CodexAdapter(
        run_probe=MagicMock(),
        sdk_available=lambda *_args: False,
        which=lambda _name: "/usr/bin/codex",
    )
    try:
        selected, readiness = adapter.select_transport(codex_request(tmp_path))
    finally:
        os.close(fd)

    assert selected is None
    assert readiness.failure is contracts.ReadinessFailure.SDK_UNAVAILABLE
    assert readiness.repair == "restore the repository openai-codex tooling dependency"


def test_codex_protected_credential_never_falls_back_to_cli(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    credential = tmp_path / "auth.json"
    credential.write_text(json.dumps(_protected_codex_auth()))
    fd = os.open(credential, os.O_RDONLY)
    monkeypatch.setenv(codex.CODEX_AUTH_FD_ENV, str(fd))
    launches: list[list[str]] = []

    def run_process(command, **_kwargs):
        launches.append(list(command))
        return process.ProcessResult(
            returncode=1,
            stdout='{"type":"error","reason":"startup"}\n',
            stderr="",
            duration_s=0.01,
            timed_out=False,
        )

    adapter = codex.CodexAdapter(
        run_process=run_process,
        run_probe=lambda *_args, **_kwargs: process.ProcessResult(
            returncode=0,
            stdout='{"type":"readiness","status":"ready"}\n',
            stderr="",
            duration_s=0.01,
            timed_out=False,
        ),
        sdk_available=lambda *_args: True,
        which=lambda _name: "/usr/bin/codex",
    )
    try:
        result = adapter.run(codex_request(tmp_path))
    finally:
        os.close(fd)

    assert result.status is contracts.RuntimeStatus.FAILED
    assert result.transport == codex.CODEX_SDK_TRANSPORT
    assert result.terminal_reason is contracts.TerminalReason.STARTUP_FAILURE
    assert [attempt.transport for attempt in result.transport_attempts] == [
        codex.CODEX_SDK_TRANSPORT,
        codex.CODEX_CLI_TRANSPORT,
    ]
    assert result.transport_attempts[1].failure_class == "eligibility"
    assert len(launches) == 1
    assert launches[0][0].endswith("/bin/python")
    assert all("exec" not in command for command in launches)


@pytest.mark.parametrize(
    ("frame", "failure"),
    [
        (
            '{"type":"readiness","status":"failed","failure":"sdk-version"}\n',
            contracts.ReadinessFailure.SDK_VERSION_MISMATCH,
        ),
        (
            '{"type":"readiness","status":"failed","failure":"bootstrap"}\n',
            contracts.ReadinessFailure.CONFIG_BOOTSTRAP,
        ),
        ("private vendor protocol\n", contracts.ReadinessFailure.PROTOCOL_INCOMPATIBLE),
    ],
)
def test_codex_sdk_readiness_classifies_only_sanitized_boundary_failures(
    tmp_path: Path,
    frame: str,
    failure: contracts.ReadinessFailure,
) -> None:
    adapter = codex.CodexAdapter(
        run_probe=lambda command, **_kwargs: (
            process.ProcessResult(
                returncode=1,
                stdout=frame,
                stderr="private vendor stderr and account identity",
                duration_s=0.01,
                timed_out=False,
            )
            if str(command[0]).endswith("python")
            else _codex_auth_process_result()
        ),
        sdk_available=lambda *_args: True,
        which=lambda _name: "/usr/bin/codex",
    )

    readiness = adapter.probe_sdk(codex_request(tmp_path))

    assert readiness.ready is False
    assert readiness.failure is failure
    assert "private vendor" not in repr(readiness)


def test_codex_cli_readiness_requires_an_isolated_config_lock(tmp_path: Path) -> None:
    observed: list[tuple[list[str], Path, dict[str, str]]] = []

    def run_process(command, *, cwd, env, **_kwargs):
        observed.append((list(command), cwd, env))
        export_override = next(
            part
            for part in command
            if str(part).startswith("debug.config_lockfile.export_dir=")
        )
        export_dir = Path(json.loads(export_override.split("=", 1)[1]))
        (export_dir / "runtime.config.lock.toml").write_text(
            "version = 1\n", encoding="utf-8"
        )
        return process.ProcessResult(
            returncode=0,
            stdout="[]\n",
            stderr="private vendor bootstrap output",
            duration_s=0.01,
            timed_out=False,
        )

    adapter = codex.CodexAdapter(
        run_process=run_process,
        run_probe=lambda *_args, **_kwargs: _codex_auth_process_result(),
        sdk_available=lambda *_args: False,
        which=lambda _name: "/usr/bin/codex",
    )

    readiness = adapter.probe_cli(codex_request(tmp_path))

    assert readiness.ready is True
    assert readiness.transport == codex.CODEX_CLI_TRANSPORT
    assert "debug" in observed[0][0]
    assert observed[0][1] != tmp_path
    assert observed[0][1].parent == tmp_path
    assert observed[0][2]["CODEX_HOME"].startswith(str(observed[0][1]))


def test_codex_runtime_keeps_only_code_mode_host_enabled_for_terminal_tools(
    tmp_path: Path,
) -> None:
    config_lock = tmp_path / "runtime.config.lock.toml"
    output_schema = tmp_path / "output-schema.json"
    command = codex.build_codex_command(
        codex_request(tmp_path),
        config_lock=config_lock,
        output_schema_path=output_schema,
    )
    feature_overrides = {
        part.removeprefix("features.").split("=", 1)[0]: part.rsplit("=", 1)[1]
        for part in command
        if part.startswith("features.")
    }

    assert feature_overrides.pop("code_mode_host") == "true"
    assert feature_overrides["code_mode"] == "false"
    assert feature_overrides
    assert set(feature_overrides.values()) == {"false"}
    assert "--sandbox" in command
    assert command[command.index("--sandbox") + 1] == "read-only"


def test_codex_cli_builder_resumes_the_requested_session(tmp_path: Path) -> None:
    command = codex.build_codex_command(
        replace(codex_request(tmp_path), resume_session_id="thread.valid-._1"),
        config_lock=tmp_path / "runtime.config.lock.toml",
        output_schema_path=tmp_path / "result.schema.json",
    )

    exec_index = command.index("exec")
    assert command[exec_index] == "exec"
    assert command[-3:] == ["resume", "thread.valid-._1", "-"]


@pytest.mark.parametrize("read_only", [True, False])
def test_codex_selects_cli_before_launch_when_sdk_is_unavailable(
    tmp_path: Path, read_only: bool
) -> None:
    calls: list[list[str]] = []
    call_cwds: list[Path] = []
    call_inputs: list[str] = []

    def run_cli(command, **kwargs):
        calls.append(list(command))
        call_cwds.append(kwargs["cwd"])
        call_inputs.append(kwargs["input_text"])
        if "debug" in command:
            export_override = next(
                part
                for part in command
                if str(part).startswith("debug.config_lockfile.export_dir=")
            )
            export_dir = Path(json.loads(export_override.split("=", 1)[1]))
            (export_dir / "runtime.config.lock.toml").write_text(
                'version = 1\ncodex_version = "test"\n[config]\n',
                encoding="utf-8",
            )
            return process.ProcessResult(
                returncode=0,
                stdout="[]\n",
                stderr="",
                duration_s=0.01,
                timed_out=False,
            )
        return process.ProcessResult(
            returncode=0,
            stdout=json.dumps(
                {
                    "task_id": "attempt-codex",
                    "status": "completed",
                    "summary": "cli output",
                }
            ),
            stderr="",
            duration_s=0.01,
            timed_out=False,
        )

    adapter = codex.CodexAdapter(
        run_cli=run_cli,
        run_probe=lambda *args, **kwargs: _codex_auth_process_result(),
        sdk_available=lambda *_args: False,
        which=lambda _name: "/usr/bin/codex",
    )
    result = adapter.run(codex_request(tmp_path, read_only=read_only))

    assert result.transport == codex.CODEX_CLI_TRANSPORT
    assert result.status is contracts.RuntimeStatus.COMPLETED
    assert result.fallback_from is None
    assert result.attempt_id == "attempt-codex"
    assert [attempt.transport for attempt in result.transport_attempts] == [
        codex.CODEX_SDK_TRANSPORT,
        codex.CODEX_CLI_TRANSPORT,
    ]
    assert result.transport_attempts[0].failure_class == "sdk-unavailable"
    assert result.transport_attempts[0].selected_next is True
    assert result.usage is None
    assert result.effective_model is None
    assert len(calls) == 2
    assert calls[0][0] == "codex"
    assert "debug" in calls[0]
    assert "prompt-input" in calls[0]
    assert not any(str(part).endswith("sdk_bridge.py") for part in calls[0])
    assert call_inputs[0] == ""
    assert calls[1][0] == "codex"
    assert "exec" in calls[1]
    assert not any(str(part).endswith("sdk_bridge.py") for part in calls[1])
    assert "debug.config_lockfile.load_path" in " ".join(calls[1])
    assert "mcp_servers={}" in calls[1]
    assert "hooks={}" in calls[1]
    assert "plugins={}" in calls[1]
    assert "skills={}" in calls[1]
    assert "features.code_mode=false" in calls[1]
    assert "features.multi_agent=false" in calls[1]
    assert "--add-dir" in calls[1]
    assert calls[1][calls[1].index("--add-dir") + 1] == str(tmp_path)
    assert "--skip-git-repo-check" in calls[1]
    assert "--ephemeral" in calls[1]
    assert "--ignore-user-config" in calls[1]
    assert "--ignore-rules" in calls[1]
    assert call_cwds[0] != tmp_path
    assert call_cwds[0].parent == tmp_path
    assert call_cwds[1] == (call_cwds[0] if read_only else tmp_path)
    assert str(tmp_path) in call_inputs[1]
    assert "this codex prompt must remain private" in call_inputs[1]


def test_codex_bridge_normalizes_native_cached_and_reasoning_usage() -> None:
    usage = sdk_bridge._usage_payload(
        {
            "input_tokens": 17,
            "cached_input_tokens": 5,
            "output_tokens": 9,
            "reasoning_output_tokens": 4,
            "total_tokens": 26,
        }
    )

    assert usage == {
        "input_tokens": 17,
        "output_tokens": 9,
        "cache_read_tokens": 5,
        "reasoning_tokens": 4,
    }


def test_codex_write_run_never_relaunches_cli_after_bridge_start(
    tmp_path: Path,
) -> None:
    calls: list[list[str]] = []

    def run_process(command, **kwargs):
        del kwargs
        calls.append(list(command))
        return process.ProcessResult(
            returncode=1,
            stdout="not json",
            stderr="vendor response must not be retained",
            duration_s=0.01,
            timed_out=False,
        )

    adapter = codex.CodexAdapter(
        run_process=run_process,
        run_probe=lambda *args, **kwargs: _codex_auth_process_result(),
        sdk_available=lambda *_args: True,
        which=lambda _name: "/usr/bin/codex",
    )
    result = adapter.run(codex_request(tmp_path, read_only=False))

    assert result.status is contracts.RuntimeStatus.FAILED
    assert result.terminal_reason is contracts.TerminalReason.STARTUP_FAILURE
    assert len(calls) == 1
    assert calls[0][0].endswith("/bin/python")
    assert calls[0][1] == "-I"
    assert "vendor response must not be retained" not in repr(result)


def test_codex_read_only_falls_back_only_after_presemantic_bridge_failure(
    tmp_path: Path,
) -> None:
    calls: list[list[str]] = []
    outcomes = iter(
        [
            process.ProcessResult(
                returncode=1,
                stdout='{"type":"error","reason":"startup"}\n',
                stderr="",
                duration_s=0.01,
                timed_out=False,
                progress_diagnostic=True,
            ),
            process.ProcessResult(
                returncode=0,
                stdout="[]\n",
                stderr="",
                duration_s=0.01,
                timed_out=False,
            ),
            process.ProcessResult(
                returncode=0,
                stdout=json.dumps(
                    {
                        "task_id": "attempt-codex",
                        "status": "completed",
                        "summary": "fallback output",
                    }
                ),
                stderr="",
                duration_s=0.01,
                timed_out=False,
            ),
        ]
    )

    def run_process(command, **kwargs):
        calls.append(list(command))
        if len(calls) == 2:
            export_override = next(
                part
                for part in command
                if str(part).startswith("debug.config_lockfile.export_dir=")
            )
            export_dir = Path(json.loads(export_override.split("=", 1)[1]))
            (export_dir / "runtime.config.lock.toml").write_text(
                'version = 1\ncodex_version = "test"\n[config]\n',
                encoding="utf-8",
            )
        del kwargs
        return next(outcomes)

    adapter = codex.CodexAdapter(
        run_process=run_process,
        run_probe=lambda *args, **kwargs: _codex_auth_process_result(),
        sdk_available=lambda *_args: True,
        which=lambda _name: "/usr/bin/codex",
    )
    result = adapter.run(codex_request(tmp_path))

    assert result.transport == codex.CODEX_CLI_TRANSPORT
    assert result.fallback_from is None
    assert result.attempt_id == "attempt-codex"
    assert result.status is contracts.RuntimeStatus.COMPLETED
    assert [attempt.transport for attempt in result.transport_attempts] == [
        codex.CODEX_SDK_TRANSPORT,
        codex.CODEX_CLI_TRANSPORT,
    ]
    assert result.transport_attempts[0].selected_next is True
    assert result.transport_attempts[0].duration_s == 0.01
    assert result.transport_attempts[1].selected_next is False
    assert len(calls) == 3
    assert "Native runtime progress was dropped" in result.diagnostics
    assert "debug" in calls[1]
    assert "exec" in calls[2]


def test_codex_read_only_does_not_fallback_after_semantic_event(
    tmp_path: Path,
) -> None:
    calls: list[list[str]] = []

    def run_process(command, **kwargs):
        del kwargs
        calls.append(list(command))
        return process.ProcessResult(
            returncode=1,
            stdout=('{"type":"event","kind":"assistant","semantic":true}\n'),
            stderr="",
            duration_s=0.01,
            timed_out=False,
        )

    adapter = codex.CodexAdapter(
        run_process=run_process,
        run_probe=lambda *args, **kwargs: _codex_auth_process_result(),
        sdk_available=lambda *_args: True,
        which=lambda _name: "/usr/bin/codex",
    )
    result = adapter.run(codex_request(tmp_path))

    assert result.status is contracts.RuntimeStatus.FAILED
    assert result.terminal_reason is contracts.TerminalReason.PROTOCOL_FAILURE
    assert len(calls) == 1


def test_codex_retains_sdk_failure_when_post_start_cli_is_unavailable(
    tmp_path: Path,
) -> None:
    probe_outcomes = iter(
        [
            _codex_auth_process_result(),
            process.ProcessResult(
                returncode=1,
                stdout="",
                stderr="",
                duration_s=0.01,
                timed_out=False,
            ),
        ]
    )
    adapter = codex.CodexAdapter(
        run_process=lambda *args, **kwargs: process.ProcessResult(
            returncode=1,
            stdout='{"type":"error","reason":"startup"}\n',
            stderr="",
            duration_s=0.25,
            timed_out=False,
        ),
        run_probe=lambda *args, **kwargs: next(probe_outcomes),
        sdk_available=lambda *_args: True,
        which=lambda _name: "/usr/bin/codex",
    )

    result = adapter.run(codex_request(tmp_path))

    assert result.status is contracts.RuntimeStatus.SUBSCRIPTION_UNAVAILABLE
    assert [attempt.transport for attempt in result.transport_attempts] == [
        codex.CODEX_SDK_TRANSPORT,
        codex.CODEX_CLI_TRANSPORT,
    ]
    assert result.transport_attempts[0].phase == "run"
    assert result.transport_attempts[0].failure_class == "startup"
    assert result.transport_attempts[0].duration_s == 0.25
    assert result.transport_attempts[0].selected_next is False
    assert result.transport_attempts[1].phase == "readiness"
    assert result.transport_attempts[1].failure_class == "eligibility"


def test_codex_timeout_never_falls_back(tmp_path: Path) -> None:
    calls: list[list[str]] = []

    def run_process(command, **kwargs):
        del kwargs
        calls.append(list(command))
        return process.ProcessResult(
            returncode=-1,
            stdout="",
            stderr="",
            duration_s=0.01,
            timed_out=True,
        )

    adapter = codex.CodexAdapter(
        run_process=run_process,
        run_probe=lambda *args, **kwargs: _codex_auth_process_result(),
        sdk_available=lambda *_args: True,
        which=lambda _name: "/usr/bin/codex",
    )
    result = adapter.run(codex_request(tmp_path))

    assert result.status is contracts.RuntimeStatus.TIMED_OUT
    assert result.terminal_reason is contracts.TerminalReason.TIMEOUT
    assert len(calls) == 1


def test_codex_ineligible_policy_fails_closed_without_launch(tmp_path: Path) -> None:
    calls: list[list[str]] = []

    def unexpected_process(command, **kwargs):
        del kwargs
        calls.append(list(command))
        raise AssertionError("an ineligible request must not launch a process")

    runtime_request = codex_request(tmp_path)
    runtime_request = contracts.RuntimeRequest(
        vendor=runtime_request.vendor,
        transport=runtime_request.transport,
        requested_model=runtime_request.requested_model,
        effort=runtime_request.effort,
        prompt=runtime_request.prompt,
        cwd=runtime_request.cwd,
        timeout_s=runtime_request.timeout_s,
        read_only=runtime_request.read_only,
        attempt_id=runtime_request.attempt_id,
        eligibility=contracts.SubscriptionEligibility.UNAVAILABLE,
    )
    adapter = codex.CodexAdapter(
        run_process=unexpected_process,
        run_probe=unexpected_process,
        sdk_available=lambda *_args: True,
        which=lambda _name: "/usr/bin/codex",
    )
    result = adapter.run(runtime_request)

    assert result.status is contracts.RuntimeStatus.SUBSCRIPTION_UNAVAILABLE
    assert calls == []


def test_codex_write_cli_forces_openai_provider_and_default_tier(
    tmp_path: Path,
) -> None:
    schema_path = tmp_path / "result.schema.json"
    command = codex.build_codex_command(
        codex_request(tmp_path, read_only=False),
        config_lock=tmp_path / "runtime.config.lock.toml",
        output_schema_path=schema_path,
    )

    assert command[command.index("--sandbox") + 1] == "workspace-write"
    assert "model_provider=openai" in command
    assert "service_tier=default" in command
    assert "--disable" in command
    assert "fast_mode" in command
    assert command[command.index("--output-schema") + 1] == str(schema_path)


def test_codex_cli_defers_to_declared_outer_worker_sandbox(tmp_path: Path) -> None:
    runtime_request = codex_request(tmp_path)
    isolated_request = replace(
        runtime_request,
        capability_profile=replace(
            runtime_request.capability_profile,
            environment=((codex.OUTER_WORKER_SANDBOX_ENV, "1"),),
        ),
    )

    command = codex.build_codex_command(
        isolated_request,
        config_lock=tmp_path / "runtime.config.lock.toml",
        output_schema_path=tmp_path / "result.schema.json",
    )

    assert command[command.index("--sandbox") + 1] == "danger-full-access"

    write_command = codex.build_codex_command(
        replace(isolated_request, read_only=False),
        config_lock=tmp_path / "runtime.config.lock.toml",
        output_schema_path=tmp_path / "result.schema.json",
    )

    assert write_command[write_command.index("--sandbox") + 1] == ("danger-full-access")


def test_codex_sdk_turn_receives_native_output_schema(tmp_path: Path) -> None:
    import openai_codex
    runtime_request = codex_request(tmp_path)
    bridge_request = sdk_bridge._validate_request_payload(
        {
            "vendor": "codex",
            "prompt": runtime_request.prompt,
            "cwd": str(runtime_request.cwd),
            "requested_model": runtime_request.requested_model,
            "effort": runtime_request.effort,
            "read_only": True,
            "budget_usd": None,
            "output_schema": runtime_request.output_schema,
            "read_roots": [str(tmp_path.resolve())],
        }
    )

    assert sdk_bridge._codex_turn_kwargs(bridge_request)["output_schema"] == (
        runtime_request.output_schema
    )


def test_filtered_child_environment_removes_raw_api_and_endpoint_values() -> None:
    environment = process.filtered_child_environment(
        {
            "ANTHROPIC_API_KEY": "raw-anthropic-key",
            "ANTHROPIC_BASE_URL": "https://gateway.invalid",
            "OPENAI_API_KEY": "raw-openai-key",
            "OPENAI_BASE_URL": "https://gateway.invalid",
            "CURSOR_API_KEY": "raw-cursor-key",
            process.RUNTIME_PROGRESS_FD_ENV_VAR: "999",
            "NORMAL_LOGIN_MARKER": "kept",
        },
        extra={"OPENAI_API_BASE": "https://other-gateway.invalid"},
    )

    assert environment["NORMAL_LOGIN_MARKER"] == "kept"
    assert not process.RAW_API_ENV_VARS.intersection(environment)
    assert process.RUNTIME_PROGRESS_FD_ENV_VAR not in environment


def test_runtime_cache_environment_is_limited_to_the_write_root(
    tmp_path: Path,
) -> None:
    cache = tmp_path / ".audit" / "runtime-cache" / "attempt" / "uv"
    cache.mkdir(parents=True)

    environment = process.merge_runtime_cache_environment(
        {"PATH": "/bin"},
        (("UV_CACHE_DIR", str(cache)),),
        write_root=tmp_path,
    )

    assert environment["UV_CACHE_DIR"] == str(cache)
    isolated_environment = process.merge_runtime_cache_environment(
        {"PATH": "/bin"},
        ((process.OUTER_WORKER_SANDBOX_ENV, "1"),),
        write_root=tmp_path,
    )
    assert isolated_environment[process.OUTER_WORKER_SANDBOX_ENV] == "1"
    with pytest.raises(ValueError, match="invalid environment"):
        process.merge_runtime_cache_environment(
            {"PATH": "/bin"},
            (("TMPDIR", "/tmp/outside"),),
            write_root=tmp_path,
        )
    with pytest.raises(ValueError, match="invalid environment"):
        process.merge_runtime_cache_environment(
            {"PATH": "/bin"},
            ((process.OUTER_WORKER_SANDBOX_ENV, "0"),),
            write_root=tmp_path,
        )


def test_progress_reporter_enforces_heartbeat_cadence_dedupe_rate_cap_and_sequence() -> (
    None
):
    now = [0.0]
    wire_frames: list[dict[str, object]] = []
    reporter = sdk_bridge._ProgressReporter(
        wire_frames.append,
        clock=lambda: now[0],
    )
    reporter.start(start_thread=False)

    now[0] = 0.25
    reporter.observe(contracts.RuntimePhase.MODEL_ACTIVE)
    reporter.observe(contracts.RuntimePhase.MODEL_ACTIVE)
    now[0] = 0.30
    reporter.observe(contracts.RuntimePhase.TOOL_EXECUTION)
    now[0] = 0.49
    reporter.report_due()
    now[0] = 0.50
    reporter.report_due()
    now[0] = 29.99
    reporter.report_due()
    now[0] = 30.0
    reporter.report_due()
    reporter.close()

    progress = [contracts.parse_runtime_progress_frame(frame) for frame in wire_frames]
    assert [(item.phase, item.signal) for item in progress] == [
        (
            contracts.RuntimePhase.STARTUP,
            contracts.RuntimeProgressSignal.TRANSITION,
        ),
        (
            contracts.RuntimePhase.MODEL_ACTIVE,
            contracts.RuntimeProgressSignal.TRANSITION,
        ),
        (
            contracts.RuntimePhase.TOOL_EXECUTION,
            contracts.RuntimeProgressSignal.TRANSITION,
        ),
        (
            contracts.RuntimePhase.TOOL_EXECUTION,
            contracts.RuntimeProgressSignal.HEARTBEAT,
        ),
    ]
    assert [item.sequence for item in progress] == [1, 2, 3, 4]
    assert progress[-1].activity_age_s == pytest.approx(29.7)


def test_progress_reporter_emits_changed_closed_tool_within_one_phase() -> None:
    now = [0.0]
    wire_frames: list[dict[str, object]] = []
    reporter = sdk_bridge._ProgressReporter(wire_frames.append, clock=lambda: now[0])
    reporter.start(start_thread=False)

    now[0] = 0.25
    reporter.observe(
        contracts.RuntimePhase.TOOL_EXECUTION,
        last_tool=contracts.RuntimeToolLabel.COMMAND,
    )
    now[0] = 0.3
    reporter.observe(
        contracts.RuntimePhase.TOOL_EXECUTION,
        last_tool=contracts.RuntimeToolLabel.FILE,
    )
    now[0] = 0.5
    reporter.report_due()
    reporter.close()

    progress = [contracts.parse_runtime_progress_frame(frame) for frame in wire_frames]
    assert [(item.phase, item.last_tool) for item in progress] == [
        (contracts.RuntimePhase.STARTUP, None),
        (contracts.RuntimePhase.TOOL_EXECUTION, contracts.RuntimeToolLabel.COMMAND),
        (contracts.RuntimePhase.TOOL_EXECUTION, contracts.RuntimeToolLabel.FILE),
    ]


def test_unknown_claude_and_codex_tools_are_projected_as_generic_labels() -> None:
    sensitive_name = "mcp://private-server/DangerousTool /secret/path"
    claude_phase, claude_tool = sdk_bridge._claude_stream_progress(
        {
            "type": "content_block_start",
            "content_block": {
                "type": "tool_use",
                "name": sensitive_name,
                "input": {"payload": "private"},
            },
        }
    )

    assert claude_phase is contracts.RuntimePhase.TOOL_EXECUTION
    assert claude_tool is contracts.RuntimeToolLabel.TOOL
    assert sensitive_name not in repr((claude_phase, claude_tool))

    class DangerousMcpToolThreadItem:
        pass

    codex_tool = sdk_bridge._codex_tool_label(DangerousMcpToolThreadItem())
    assert codex_tool is contracts.RuntimeToolLabel.TOOL
    assert "Dangerous" not in repr(codex_tool)


def test_run_cli_delivers_progress_before_exit_without_forwarding_stdout(
    tmp_path: Path,
) -> None:
    finished = tmp_path / "child-finished"
    observed: list[tuple[object, bool]] = []
    private_stdout = "private native stdout must not become progress"
    frame = json.dumps(
        {
            "version": 1,
            "phase": "startup",
            "signal": "transition",
            "sequence": 1,
            "activity_age_s": 0.0,
        },
        separators=(",", ":"),
    )
    child_code = (
        "import os, pathlib, time; "
        f"os.write(int(os.environ[{process.RUNTIME_PROGRESS_FD_ENV_VAR!r}]), "
        f"{(frame + chr(10)).encode()!r}); "
        f"print({private_stdout!r}, flush=True); "
        "time.sleep(0.1); "
        f"pathlib.Path({str(finished)!r}).write_text('done')"
    )

    result = run_cli_unsandboxed(
        [sys.executable, "-c", child_code],
        cwd=tmp_path,
        input_text="",
        timeout_s=2,
        env={"PATH": os.environ["PATH"]},
        on_progress=lambda progress: observed.append((progress, finished.exists())),
    )

    assert result.returncode == 0
    assert result.progress_diagnostic is False
    assert len(observed) == 1
    assert observed[0][1] is False
    assert observed[0][0].phase is contracts.RuntimePhase.STARTUP
    assert private_stdout not in repr(observed)
    assert result.stdout.strip() == private_stdout


def test_run_cli_drops_malformed_and_oversized_progress(
    tmp_path: Path,
) -> None:
    child_code = (
        "import json, os; "
        f"fd=int(os.environ[{process.RUNTIME_PROGRESS_FD_ENV_VAR!r}]); "
        "os.write(fd, b'not-json\\n'); "
        f"os.write(fd, b'x' * {process.MAX_PROGRESS_FRAME_BYTES + 1} + b'\\n'); "
        "os.write(fd, (json.dumps({'version':1,'phase':'startup',"
        "'signal':'heartbeat','sequence':1,'activity_age_s':0.0},"
        "separators=(',',':'))+'\\n').encode())"
    )
    observed: list[object] = []

    result = run_cli_unsandboxed(
        [sys.executable, "-c", child_code],
        cwd=tmp_path,
        input_text="",
        timeout_s=2,
        env={"PATH": os.environ["PATH"]},
        on_progress=observed.append,
    )

    assert result.returncode == 0
    assert result.progress_diagnostic is True
    assert len(observed) == 1


def test_run_cli_drops_flooded_progress(tmp_path: Path) -> None:
    child_code = (
        "import json, os; "
        f"fd=int(os.environ[{process.RUNTIME_PROGRESS_FD_ENV_VAR!r}]); "
        f"[os.write(fd, (json.dumps({{'version':1,'phase':'startup',"
        "'signal':'heartbeat','sequence':index+1,'activity_age_s':0.0},"
        "separators=(',',':'))+'\\n').encode()) "
        f"for index in range({process.MAX_PROGRESS_FRAMES + 16})]"
    )
    observed: list[object] = []

    result = run_cli_unsandboxed(
        [sys.executable, "-c", child_code],
        cwd=tmp_path,
        input_text="",
        timeout_s=2,
        env={"PATH": os.environ["PATH"]},
        on_progress=observed.append,
    )

    assert result.returncode == 0
    assert result.progress_diagnostic is True
    assert len(observed) == process.MAX_PROGRESS_FRAMES


def test_progress_drain_delivers_frame_when_read_fd_at_or_above_fd_setsize() -> None:
    """Progress must still deliver when the read FD is beyond select.select's ceiling."""
    fd_ceiling = getattr(select, "FD_SETSIZE", 1024)
    low_read_fd, write_fd = os.pipe()
    read_fd = fcntl.fcntl(low_read_fd, fcntl.F_DUPFD, fd_ceiling)
    os.close(low_read_fd)
    assert read_fd >= fd_ceiling
    with pytest.raises(ValueError):
        select.select([read_fd], [], [], 0)

    diagnostic = threading.Event()
    stop = threading.Event()
    observed: list[object] = []
    delivered = threading.Event()
    frame = (
        b'{"version":1,"phase":"startup","signal":"transition",'
        b'"sequence":1,"activity_age_s":0.0}\n'
    )

    def observe(progress: object) -> None:
        observed.append(progress)
        delivered.set()

    reader = threading.Thread(
        target=process._drain_progress_pipe,
        args=(read_fd, observe, diagnostic, stop),
        name=process.PROGRESS_READER_THREAD_NAME,
    )
    os.write(write_fd, frame)
    reader.start()
    try:
        os.close(write_fd)
        write_fd = -1
        assert delivered.wait(timeout=1)
        reader.join(timeout=1)
        assert not reader.is_alive()
        assert diagnostic.is_set() is False
        assert len(observed) == 1
        assert observed[0].phase is contracts.RuntimePhase.STARTUP
    finally:
        stop.set()
        if write_fd >= 0:
            try:
                os.close(write_fd)
            except OSError:
                pass
        reader.join(timeout=1)


def test_progress_drain_accepts_frames_again_after_rate_window(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    read_fd, write_fd = os.pipe()
    diagnostic = threading.Event()
    stop = threading.Event()
    first_window_drained = threading.Event()
    observed: list[object] = []
    now = [0.0]
    frame = (
        b'{"version":1,"phase":"startup","signal":"heartbeat",'
        b'"sequence":1,"activity_age_s":0.0}\n'
    )

    def observe(progress: object) -> None:
        observed.append(progress)
        if len(observed) == process.MAX_PROGRESS_FRAMES:
            first_window_drained.set()

    monkeypatch.setattr(process.time, "monotonic", lambda: now[0])
    reader = threading.Thread(
        target=process._drain_progress_pipe,
        args=(read_fd, observe, diagnostic, stop),
    )
    reader.start()
    try:
        for _ in range(process.MAX_PROGRESS_FRAMES):
            os.write(write_fd, frame)
        assert first_window_drained.wait(timeout=1)

        now[0] = process.PROGRESS_FRAME_WINDOW_S + 0.01
        os.write(write_fd, frame)
        os.close(write_fd)
        reader.join(timeout=1)

        assert not reader.is_alive()
        assert diagnostic.is_set() is False
        assert len(observed) == process.MAX_PROGRESS_FRAMES + 1
    finally:
        stop.set()
        try:
            os.close(write_fd)
        except OSError:
            pass
        reader.join(timeout=1)


def test_run_cli_drops_callback_failures_and_broken_progress_pipe(
    tmp_path: Path,
) -> None:
    frame = json.dumps(
        {
            "version": 1,
            "phase": "startup",
            "signal": "transition",
            "sequence": 1,
            "activity_age_s": 0.0,
        }
    )
    callback_result = run_cli_unsandboxed(
        [
            sys.executable,
            "-c",
            (
                "import os; "
                f"os.write(int(os.environ[{process.RUNTIME_PROGRESS_FD_ENV_VAR!r}]), "
                f"{(frame + chr(10)).encode()!r})"
            ),
        ],
        cwd=tmp_path,
        input_text="",
        timeout_s=2,
        env={"PATH": os.environ["PATH"]},
        on_progress=lambda _progress: (_ for _ in ()).throw(RuntimeError("private")),
    )
    broken_result = run_cli_unsandboxed(
        [
            sys.executable,
            "-c",
            (
                "import os; "
                f"os.close(int(os.environ[{process.RUNTIME_PROGRESS_FD_ENV_VAR!r}]))"
            ),
        ],
        cwd=tmp_path,
        input_text="",
        timeout_s=2,
        env={"PATH": os.environ["PATH"]},
        on_progress=lambda _progress: None,
    )

    assert callback_result.returncode == 0
    assert callback_result.progress_diagnostic is True
    assert "private" not in repr(callback_result)
    assert broken_result.returncode == 0


def test_progress_heartbeats_never_extend_the_process_timeout(tmp_path: Path) -> None:
    child_code = (
        "import json, os, time; "
        f"fd=int(os.environ[{process.RUNTIME_PROGRESS_FD_ENV_VAR!r}]); "
        "sequence=1; time.sleep(0.15); "
        "\nwhile True:\n"
        " os.write(fd, (json.dumps({'version':1,'phase':'model-active',"
        "'signal':'heartbeat','sequence':sequence,'activity_age_s':0.0})+'\\n').encode())\n"
        " sequence += 1\n"
        " time.sleep(0.005)"
    )
    observed: list[object] = []
    timeout_s = 1.0
    terminate_grace_s = 0.05

    result = run_cli_unsandboxed(
        [sys.executable, "-c", child_code],
        cwd=tmp_path,
        input_text="",
        timeout_s=timeout_s,
        env={"PATH": os.environ["PATH"]},
        terminate_grace_s=terminate_grace_s,
        on_progress=observed.append,
    )

    assert result.timed_out is True
    assert timeout_s <= result.duration_s < timeout_s + terminate_grace_s + 0.25
    assert len(observed) >= 2


def test_bridge_progress_fd_and_environment_are_not_inherited_by_descendants(
    monkeypatch, tmp_path: Path
) -> None:
    read_fd, write_fd = os.pipe()
    monkeypatch.setenv(process.RUNTIME_PROGRESS_FD_ENV_VAR, str(write_fd))
    reporter = sdk_bridge._open_progress_reporter_from_env()
    assert reporter is not None
    child_code = (
        "import os; "
        f"marker={process.RUNTIME_PROGRESS_FD_ENV_VAR!r} in os.environ; "
        "inherited=True; "
        f"\ntry: os.fstat({write_fd})\n"
        "except OSError: inherited=False\n"
        "print(f'{marker} {inherited}')"
    )
    try:
        descendant = subprocess.run(
            [sys.executable, "-c", child_code],
            check=True,
            capture_output=True,
            text=True,
            close_fds=False,
        )
    finally:
        reporter.close()
        os.close(read_fd)

    assert descendant.stdout.strip() == "False False"


@pytest.mark.parametrize(
    ("terminal", "expected_returncode"),
    [("normal", 0), ("error", 1), ("interruption", 130)],
)
def test_bridge_reporter_stops_and_joins_on_every_terminal_path(
    monkeypatch,
    terminal: str,
    expected_returncode: int,
) -> None:
    class FakeReporter:
        closed = False

        def close(self) -> None:
            self.closed = True

    reporter = FakeReporter()

    async def fake_run(_request, *, reporter):
        assert reporter is not None
        if terminal == "error":
            raise RuntimeError("protocol")
        if terminal == "interruption":
            raise asyncio.CancelledError

    monkeypatch.setattr(
        sdk_bridge,
        "_open_progress_reporter_from_env",
        lambda: reporter,
    )
    monkeypatch.setattr(sdk_bridge, "_read_request", lambda: {"vendor": "claude"})
    monkeypatch.setattr(sdk_bridge, "_run", fake_run)
    monkeypatch.setattr(sdk_bridge, "_error_frame", lambda _reason: None)

    assert asyncio.run(sdk_bridge.main()) == expected_returncode
    assert reporter.closed is True


@pytest.mark.parametrize("mode", ["normal", "timeout"])
def test_progress_reader_reporter_is_cleaned_up_on_terminal_paths(
    tmp_path: Path,
    mode: str,
) -> None:
    code = "pass" if mode == "normal" else "import time; time.sleep(60)"

    result = run_cli_unsandboxed(
        [sys.executable, "-c", code],
        cwd=tmp_path,
        input_text="",
        timeout_s=2 if mode == "normal" else 0.05,
        env={"PATH": os.environ["PATH"]},
        terminate_grace_s=0.05,
        on_progress=lambda _progress: None,
    )

    assert result.timed_out is (mode == "timeout")
    assert not any(
        thread.name == process.PROGRESS_READER_THREAD_NAME
        for thread in threading.enumerate()
    )


def test_run_cli_lingering_progress_writer_teardown_closes_read_fd_once(
    monkeypatch, tmp_path: Path
) -> None:
    """Teardown must not race-close the read FD when a progress writer still lingers."""
    pipes: list[tuple[int, int]] = []
    held_write_fd: int | None = None
    closed_fds: list[int] = []
    real_pipe = process.os.pipe
    real_close = process.os.close
    real_dup = process.os.dup

    def capturing_pipe() -> tuple[int, int]:
        nonlocal held_write_fd
        pair = real_pipe()
        pipes.append(pair)
        # run_cli creates the progress pipe before Popen; hold only that writer.
        if held_write_fd is None:
            held_write_fd = real_dup(pair[1])
        return pair

    def tracking_close(fd: int) -> None:
        closed_fds.append(fd)
        real_close(fd)

    monkeypatch.setattr(process.os, "pipe", capturing_pipe)
    monkeypatch.setattr(process.os, "close", tracking_close)

    try:
        result = run_cli_unsandboxed(
            [sys.executable, "-c", "pass"],
            cwd=tmp_path,
            input_text="",
            timeout_s=2,
            env={"PATH": os.environ["PATH"]},
            terminate_grace_s=0.05,
            on_progress=lambda _progress: None,
        )
    finally:
        if held_write_fd is not None:
            try:
                real_close(held_write_fd)
            except OSError:
                pass

    assert pipes, "progress pipe was not created"
    progress_read_fd = pipes[0][0]
    assert result.returncode == 0
    assert result.progress_diagnostic is True
    assert closed_fds.count(progress_read_fd) == 1
    assert not any(
        thread.name == process.PROGRESS_READER_THREAD_NAME
        for thread in threading.enumerate()
    )


@pytest.mark.parametrize("vendor", ["claude", "codex"])
def test_sdk_adapters_forward_progress_only_to_bridge_launches(
    tmp_path: Path,
    vendor: str,
) -> None:
    observed_kwargs: list[dict[str, object]] = []

    def callback(_progress) -> None:
        return None

    structured = {
        "task_id": f"attempt-{vendor}",
        "status": "completed",
        "summary": "done",
    }

    def run_process(_command, **kwargs):
        observed_kwargs.append(kwargs)
        return process.ProcessResult(
            returncode=0,
            stdout=json.dumps(
                {
                    "type": "result",
                    "status": "completed",
                    "terminal_reason": "completed",
                    "structured_output": structured,
                }
            ),
            stderr="",
            duration_s=0.01,
            timed_out=False,
            progress_diagnostic=True,
        )

    if vendor == "claude":
        adapter = claude.ClaudeAdapter(
            run_process=run_process,
            run_probe=lambda *args, **kwargs: _auth_process_result(),
            sdk_available=lambda *_args: True,
            which=lambda _name: "/usr/bin/claude",
        )
        runtime_request = claude_request(tmp_path)
    else:
        adapter = codex.CodexAdapter(
            run_process=run_process,
            run_probe=lambda *args, **kwargs: _codex_auth_process_result(),
            sdk_available=lambda *_args: True,
            which=lambda _name: "/usr/bin/codex",
        )
        runtime_request = codex_request(tmp_path)

    result = adapter.run(runtime_request, on_progress=callback)

    assert result.status is contracts.RuntimeStatus.COMPLETED
    assert observed_kwargs[0]["on_progress"] is callback
    assert result.diagnostics == ("Native runtime progress was dropped",)
    assert all(event.kind != "progress" for event in result.events)


def test_direct_cli_and_cursor_paths_accept_but_do_not_forward_progress(
    tmp_path: Path,
) -> None:
    claude_kwargs: list[dict[str, object]] = []
    codex_kwargs: list[dict[str, object]] = []
    cursor_kwargs: list[dict[str, object]] = []

    def run_claude(_command, **kwargs):
        claude_kwargs.append(kwargs)
        return process.ProcessResult(
            returncode=0,
            stdout=json.dumps(
                {
                    "type": "result",
                    "is_error": False,
                    "structured_output": {
                        "task_id": "attempt-claude",
                        "status": "completed",
                        "summary": "done",
                    },
                }
            ),
            stderr="",
            duration_s=0.01,
            timed_out=False,
        )

    claude_adapter = claude.ClaudeAdapter(
        run_process=run_claude,
        run_probe=lambda *args, **kwargs: _auth_process_result(),
        sdk_available=lambda *_args: True,
        which=lambda _name: "/usr/bin/claude",
    )
    claude_result = claude_adapter.run(
        replace(claude_request(tmp_path), transport=claude.CLAUDE_CLI_TRANSPORT),
        on_progress=lambda _progress: None,
    )

    def run_codex(command, **kwargs):
        codex_kwargs.append(kwargs)
        if "debug" in command:
            export_override = next(
                part
                for part in command
                if str(part).startswith("debug.config_lockfile.export_dir=")
            )
            export_dir = Path(json.loads(export_override.split("=", 1)[1]))
            (export_dir / "runtime.config.lock.toml").write_text(
                'version = 1\ncodex_version = "test"\n[config]\n',
                encoding="utf-8",
            )
            stdout = "[]\n"
        else:
            stdout = json.dumps(
                {
                    "task_id": "attempt-codex",
                    "status": "completed",
                    "summary": "done",
                }
            )
        return process.ProcessResult(
            returncode=0,
            stdout=stdout,
            stderr="",
            duration_s=0.01,
            timed_out=False,
        )

    codex_adapter = codex.CodexAdapter(
        run_process=run_codex,
        run_probe=lambda *args, **kwargs: _codex_auth_process_result(),
        sdk_available=lambda *_args: True,
        which=lambda _name: "/usr/bin/codex",
    )
    codex_result = codex_adapter.run(
        replace(codex_request(tmp_path), transport=codex.CODEX_CLI_TRANSPORT),
        on_progress=lambda _progress: None,
    )

    def run_cursor(_command, **kwargs):
        cursor_kwargs.append(kwargs)
        return process.ProcessResult(
            returncode=0,
            stdout="\n".join(
                [
                    json.dumps({"type": "system", "subtype": "init"}),
                    json.dumps(
                        {
                            "type": "result",
                            "subtype": "success",
                            "is_error": False,
                            "result": "done",
                        }
                    ),
                ]
            ),
            stderr="",
            duration_s=0.01,
            timed_out=False,
        )

    cursor_adapter = cursor.CursorAdapter(
        run_cli=run_cursor,
        run_probe=_cursor_auth_probe,
        which=lambda _name: "/usr/bin/cursor-agent",
    )
    cursor_result = cursor_adapter.run(
        request(tmp_path),
        on_progress=lambda _progress: None,
    )

    assert claude_result.status is contracts.RuntimeStatus.COMPLETED
    assert codex_result.status is contracts.RuntimeStatus.COMPLETED
    assert cursor_result.status is contracts.RuntimeStatus.COMPLETED
    assert "on_progress" not in claude_kwargs[0]
    assert all("on_progress" not in kwargs for kwargs in codex_kwargs)
    assert "on_progress" not in cursor_kwargs[0]


@pytest.mark.parametrize("vendor", ["claude", "codex"])
def test_semantic_terminal_bridge_error_forbids_read_only_fallback(
    tmp_path: Path,
    vendor: str,
) -> None:
    calls: list[list[str]] = []
    stdout = "\n".join(
        [
            json.dumps({"type": "event", "kind": "assistant", "semantic": True}),
            json.dumps({"type": "error", "reason": "disconnect"}),
        ]
    )

    def run_process(command, **_kwargs):
        calls.append(list(command))
        return process.ProcessResult(
            returncode=1,
            stdout=stdout,
            stderr="private vendor failure",
            duration_s=0.01,
            timed_out=False,
        )

    if vendor == "claude":
        adapter = claude.ClaudeAdapter(
            run_process=run_process,
            run_probe=lambda *args, **kwargs: _auth_process_result(),
            sdk_available=lambda *_args: True,
            which=lambda _name: "/usr/bin/claude",
        )
        runtime_request = claude_request(tmp_path)
    else:
        adapter = codex.CodexAdapter(
            run_process=run_process,
            run_probe=lambda *args, **kwargs: _codex_auth_process_result(),
            sdk_available=lambda *_args: True,
            which=lambda _name: "/usr/bin/codex",
        )
        runtime_request = codex_request(tmp_path)

    result = adapter.run(runtime_request)

    assert result.status is contracts.RuntimeStatus.FAILED
    assert result.terminal_reason is contracts.TerminalReason.TRANSPORT_DISCONNECT
    assert len(calls) == 1
    assert "private vendor failure" not in repr(result)


def test_run_cli_uses_a_new_process_group_and_collects_normal_exit(
    tmp_path: Path,
) -> None:
    result = run_cli_unsandboxed(
        [sys.executable, "-c", "import sys; print(sys.stdin.read())"],
        cwd=tmp_path,
        input_text="safe input",
        timeout_s=2,
        env={"PATH": os.environ["PATH"]},
    )

    assert result.returncode == 0
    assert result.timed_out is False
    assert result.stdout == "safe input\n"


def test_run_cli_reaps_descendants_after_normal_parent_exit(tmp_path: Path) -> None:
    child_pid_path = tmp_path / "normal-exit-child.pid"
    parent_code = (
        "import pathlib, subprocess, sys; "
        "child = subprocess.Popen([sys.executable, '-c', "
        "'import signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); "
        "time.sleep(60)'], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, "
        "stderr=subprocess.DEVNULL, close_fds=True); "
        f"pathlib.Path({str(child_pid_path)!r}).write_text(str(child.pid))"
    )

    result = run_cli_unsandboxed(
        [sys.executable, "-c", parent_code],
        cwd=tmp_path,
        input_text="",
        timeout_s=2,
        env={"PATH": os.environ["PATH"]},
        terminate_grace_s=0.05,
    )

    assert result.returncode == 0
    child_pid = int(child_pid_path.read_text())
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        try:
            os.kill(child_pid, 0)
        except ProcessLookupError:
            break
        time.sleep(0.01)
    else:
        pytest.fail("runtime descendant survived normal parent collection")


def test_run_cli_preserves_dispatch_depth_and_declared_file_descriptors(
    tmp_path: Path,
) -> None:
    read_fd, write_fd = os.pipe()
    try:
        result = run_cli_unsandboxed(
            [
                sys.executable,
                "-c",
                (
                    "import os; "
                    f"os.fstat({read_fd}); "
                    "print(os.environ['AGENT_DISPATCH_DEPTH'])"
                ),
            ],
            cwd=tmp_path,
            input_text="",
            timeout_s=2,
            env={"PATH": os.environ["PATH"]},
            pass_fds=(read_fd,),
        )
    finally:
        os.close(read_fd)
        os.close(write_fd)

    assert result.returncode == 0
    assert result.stdout == "1\n"


def test_run_cli_timeout_terminates_the_owned_process_group(tmp_path: Path) -> None:
    result = run_cli_unsandboxed(
        [sys.executable, "-c", "import time; time.sleep(60)"],
        cwd=tmp_path,
        input_text="",
        timeout_s=0.05,
        env={"PATH": os.environ["PATH"]},
        terminate_grace_s=0.05,
    )

    assert result.timed_out is True
    assert result.returncode != 0


def test_run_cli_reports_timeout_within_bound_when_leader_ignores_sigterm(
    tmp_path: Path,
) -> None:
    started = time.monotonic()
    result = run_cli_unsandboxed(
        [
            sys.executable,
            "-c",
            "import signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(60)",
        ],
        cwd=tmp_path,
        input_text="",
        timeout_s=0.1,
        env={"PATH": os.environ["PATH"]},
        terminate_grace_s=0.1,
    )
    elapsed = time.monotonic() - started

    assert result.timed_out is True
    assert result.returncode == -signal.SIGKILL
    assert 0.1 <= elapsed < 0.45


def test_run_cli_reports_exact_launch_identity_before_writing_input(
    tmp_path: Path,
) -> None:
    observed: list[process.ProcessLaunchIdentity] = []

    def capture(identity: process.ProcessLaunchIdentity) -> None:
        observed.append(identity)
        assert identity.pid == identity.pgid
        assert identity.start_ticks >= identity.prelaunch_tick_lower_bound - 1

    result = run_cli_unsandboxed(
        [sys.executable, "-c", "import sys; print(sys.stdin.read())"],
        cwd=tmp_path,
        input_text="semantic input",
        timeout_s=2,
        env={"PATH": os.environ["PATH"]},
        on_launch=capture,
    )

    assert result.returncode == 0
    assert result.stdout == "semantic input\n"
    assert len(observed) == 1
    identity = observed[0]
    assert identity.boot_id
    assert not hasattr(identity, "command")
    assert "semantic input" not in repr(identity)


def test_launch_callback_failure_cancels_and_reaps_before_input(
    tmp_path: Path,
) -> None:
    launched_pid: list[int] = []

    def reject(identity: process.ProcessLaunchIdentity) -> None:
        launched_pid.append(identity.pid)
        raise RuntimeError("ownership append unavailable")

    with pytest.raises(process.ProcessIdentityError):
        run_cli_unsandboxed(
            [
                sys.executable,
                "-c",
                "import sys, time; sys.stdin.read(); time.sleep(60)",
            ],
            cwd=tmp_path,
            input_text="must-not-be-written",
            timeout_s=2,
            env={"PATH": os.environ["PATH"]},
            terminate_grace_s=0.05,
            on_launch=reject,
        )

    assert len(launched_pid) == 1
    with pytest.raises(ProcessLookupError):
        os.kill(launched_pid[0], 0)


def _write_proc_stat(
    proc_root: Path, *, pid: int, pgid: int, start_ticks: int, state: str = "S"
) -> None:
    fields = [state, *(["0"] * 19)]
    fields[2] = str(pgid)
    fields[19] = str(start_ticks)
    root = proc_root / str(pid)
    root.mkdir(parents=True, exist_ok=True)
    (root / "stat").write_text(f"{pid} (runtime) {' '.join(fields)}\n")


def test_process_identity_revalidation_distinguishes_live_reused_and_gone(
    tmp_path: Path,
) -> None:
    proc_root = tmp_path / "proc"
    proc_root.mkdir()
    boot_id_path = tmp_path / "boot_id"
    boot_id_path.write_text("boot-a\n")
    identity = process.ProcessLaunchIdentity(
        pid=123,
        pgid=123,
        boot_id="boot-a",
        start_ticks=456,
        prelaunch_tick_lower_bound=455,
    )
    _write_proc_stat(proc_root, pid=123, pgid=123, start_ticks=456)

    assert (
        process.classify_process_identity(
            identity, proc_root=proc_root, boot_id_path=boot_id_path
        )
        == "active"
    )

    (proc_root / "123" / "stat").unlink()
    _write_proc_stat(proc_root, pid=123, pgid=999, start_ticks=999)
    assert (
        process.classify_process_identity(
            identity, proc_root=proc_root, boot_id_path=boot_id_path
        )
        == "gone"
    )

    (proc_root / "123" / "stat").unlink()
    assert (
        process.classify_process_identity(
            identity, proc_root=proc_root, boot_id_path=boot_id_path
        )
        == "gone"
    )

    boot_id_path.write_text("boot-b\n")
    assert (
        process.classify_process_identity(
            identity, proc_root=proc_root, boot_id_path=boot_id_path
        )
        == "gone"
    )


def test_reused_leader_with_surviving_original_group_is_unverifiable(
    tmp_path: Path,
) -> None:
    proc_root = tmp_path / "proc"
    proc_root.mkdir()
    boot_id_path = tmp_path / "boot_id"
    boot_id_path.write_text("boot-a\n")
    identity = process.ProcessLaunchIdentity(123, 123, "boot-a", 456, 455)
    _write_proc_stat(proc_root, pid=123, pgid=999, start_ticks=999)
    _write_proc_stat(proc_root, pid=124, pgid=123, start_ticks=457)

    assert (
        process.classify_process_identity(
            identity, proc_root=proc_root, boot_id_path=boot_id_path
        )
        == "unverifiable"
    )


def test_missing_leader_with_surviving_group_is_unverifiable(tmp_path: Path) -> None:
    proc_root = tmp_path / "proc"
    proc_root.mkdir()
    boot_id_path = tmp_path / "boot_id"
    boot_id_path.write_text("boot-a\n")
    identity = process.ProcessLaunchIdentity(123, 123, "boot-a", 456, 455)
    _write_proc_stat(proc_root, pid=124, pgid=123, start_ticks=457)

    assert (
        process.classify_process_identity(
            identity, proc_root=proc_root, boot_id_path=boot_id_path
        )
        == "unverifiable"
    )


def test_non_owner_identity_ignores_surviving_process_group(tmp_path: Path) -> None:
    proc_root = tmp_path / "proc"
    proc_root.mkdir()
    boot_id_path = tmp_path / "boot_id"
    boot_id_path.write_text("boot-a\n")
    identity = process.ProcessLaunchIdentity(123, 900, "boot-a", 456, 455)
    _write_proc_stat(proc_root, pid=124, pgid=900, start_ticks=457)

    assert (
        process.classify_process_identity(
            identity,
            proc_root=proc_root,
            boot_id_path=boot_id_path,
            require_group_gone=False,
        )
        == "gone"
    )


@pytest.mark.parametrize("require_group_gone", [True, False])
def test_unreadable_owner_identity_is_unverifiable(
    tmp_path: Path, require_group_gone: bool
) -> None:
    proc_root = tmp_path / "proc"
    proc_root.mkdir()
    boot_id_path = tmp_path / "boot_id"
    boot_id_path.write_text("boot-a\n")
    identity = process.ProcessLaunchIdentity(123, 123, "boot-a", 456, 455)
    owner = proc_root / "123"
    owner.mkdir()
    (owner / "stat").write_text("malformed proc stat\n")

    assert (
        process.classify_process_identity(
            identity,
            proc_root=proc_root,
            boot_id_path=boot_id_path,
            require_group_gone=require_group_gone,
        )
        == "unverifiable"
    )


def test_unreadable_group_member_is_unverifiable(tmp_path: Path) -> None:
    proc_root = tmp_path / "proc"
    proc_root.mkdir()
    boot_id_path = tmp_path / "boot_id"
    boot_id_path.write_text("boot-a\n")
    identity = process.ProcessLaunchIdentity(123, 123, "boot-a", 456, 455)
    member = proc_root / "124"
    member.mkdir()
    (member / "stat").write_text("malformed proc stat\n")

    assert (
        process.classify_process_identity(
            identity, proc_root=proc_root, boot_id_path=boot_id_path
        )
        == "unverifiable"
    )


def test_run_cli_caps_output_and_reaps_the_writer(tmp_path: Path) -> None:
    result = run_cli_unsandboxed(
        [
            sys.executable,
            "-c",
            "import sys, time; sys.stdout.write('x' * 1048576); "
            "sys.stdout.flush(); time.sleep(60)",
        ],
        cwd=tmp_path,
        input_text="",
        timeout_s=2,
        env={"PATH": os.environ["PATH"]},
        terminate_grace_s=0.05,
        max_stdout_bytes=1024,
    )

    assert result.output_limited is True
    assert result.timed_out is False
    assert result.returncode != 0
    assert len(result.stdout.encode("utf-8")) <= 1024


def test_run_cli_cancels_owned_process_group_when_parent_is_interrupted(
    monkeypatch, tmp_path: Path
) -> None:
    child = MagicMock()
    child.poll.return_value = None
    child.stdout.read.return_value = ""
    child.stderr.read.return_value = ""
    handle = process.ProcessHandle(process=child, pid=123, pgid=123)
    cancelled: list[object] = []
    monkeypatch.setattr(process, "launch_cli", lambda *args, **kwargs: handle)
    monkeypatch.setattr(process, "_process_exited_without_reaping", lambda owned: False)
    monkeypatch.setattr(
        process, "cancel_cli", lambda owned, **kwargs: cancelled.append(owned)
    )

    def interrupt(_delay: float) -> None:
        raise KeyboardInterrupt

    monkeypatch.setattr(process.time, "sleep", interrupt)

    with pytest.raises(KeyboardInterrupt):
        run_cli_unsandboxed(
            ["agent"],
            cwd=tmp_path,
            input_text="",
            timeout_s=2,
            on_progress=lambda _progress: None,
        )

    assert cancelled == [handle]
    assert not any(
        thread.name == process.PROGRESS_READER_THREAD_NAME
        for thread in threading.enumerate()
    )


def test_cancel_cli_terminates_descendants_after_direct_child_exits(
    tmp_path: Path,
) -> None:
    child_pid_path = tmp_path / "child.pid"
    parent_code = (
        "import pathlib, subprocess, sys, time; "
        "child = subprocess.Popen([sys.executable, '-c', "
        "'import signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); "
        "time.sleep(60)']); "
        f"pathlib.Path({str(child_pid_path)!r}).write_text(str(child.pid))"
    )
    result = run_cli_unsandboxed(
        [sys.executable, "-c", parent_code],
        cwd=tmp_path,
        input_text="",
        timeout_s=2,
        env={"PATH": os.environ["PATH"]},
        terminate_grace_s=0.05,
    )
    child_pid = int(child_pid_path.read_text())

    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        try:
            os.kill(child_pid, 0)
        except ProcessLookupError:
            break
        time.sleep(0.01)
    else:
        pytest.fail("runtime descendant survived process-group cancellation")
    assert result.returncode == 0


def test_cancel_cli_never_signals_after_group_leader_was_reaped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class ReapedProcess:
        returncode = 0

        def poll(self) -> int:
            return 0

        def wait(self, timeout: float | None = None) -> int:
            del timeout
            return 0

    signals: list[tuple[int, int]] = []
    monkeypatch.setattr(
        process.os,
        "killpg",
        lambda pgid, sig: signals.append((pgid, sig)),
    )
    handle = process.ProcessHandle(
        process=ReapedProcess(),  # type: ignore[arg-type]
        pid=123,
        pgid=123,
    )

    process.cancel_cli(handle, grace_s=0)

    assert signals == []


def test_cancel_cli_skips_grace_delay_when_unreaped_group_is_already_gone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    child = MagicMock()
    child.returncode = None
    handle = process.ProcessHandle(process=child, pid=123, pgid=123)
    signals: list[tuple[int, int]] = []
    monkeypatch.setattr(
        process.os,
        "killpg",
        lambda pgid, sig: signals.append((pgid, sig)),
    )
    monkeypatch.setattr(process, "_process_group_has_live_member", lambda _pgid: False)
    monkeypatch.setattr(
        process.time,
        "sleep",
        lambda _delay: pytest.fail("grace delay should be skipped"),
    )

    process.cancel_cli(handle)

    assert signals == [(123, signal.SIGTERM)]
    child.wait.assert_called_once()
    assert child.wait.call_args.kwargs["timeout"] > 0
    child.communicate.assert_called_once()
    assert child.communicate.call_args.kwargs["timeout"] > 0


def test_runtime_launch_transfers_and_closes_protected_codex_descriptor(
    tmp_path: Path,
) -> None:
    credential = tmp_path / "auth.json"
    credential.write_text('{"tokens":{"access_token":"bounded-test"}}')
    fd = os.open(credential, os.O_RDONLY)

    environment = {
        "PATH": os.environ["PATH"],
        process.CODEX_AUTH_FD_ENV_VAR: str(fd),
    }
    result = run_cli_unsandboxed(
        [sys.executable, "-c", "import time; time.sleep(0.05)"],
        cwd=tmp_path,
        env=environment,
        pass_fds=(fd,),
        input_text="",
        timeout_s=2,
    )

    with pytest.raises(OSError):
        os.fstat(fd)
    assert process.CODEX_AUTH_FD_ENV_VAR not in environment
    assert result.returncode == 0


def test_cursor_command_uses_structured_output_and_existing_permissions(
    tmp_path: Path,
) -> None:
    isolated_workspace = tmp_path / "isolated"
    command = cursor.build_cursor_command(
        request(tmp_path),
        isolated_workspace=isolated_workspace,
    )

    assert command == [
        "cursor-agent",
        "-p",
        "--model",
        "cursor-auto",
        "--output-format",
        "stream-json",
        "--workspace",
        str(isolated_workspace),
        "--add-dir",
        str(tmp_path),
        "--skip-worktree-setup",
        "--trust",
        "--mode",
        "ask",
    ]


@pytest.mark.parametrize(
    "resume_session_id",
    ["control\ncharacter", "../path-component", "nested/session", "--last", "x" * 129],
)
def test_resume_identifiers_are_rejected_before_bridge_or_argv_use(
    tmp_path: Path,
    resume_session_id: str,
) -> None:
    base = claude_request(tmp_path)
    payload = {
        "vendor": "claude",
        "prompt": base.prompt,
        "cwd": str(base.cwd),
        "requested_model": base.requested_model,
        "effort": base.effort,
        "read_only": True,
        "budget_usd": base.budget_usd,
        "output_schema": base.output_schema,
        "read_roots": [str(tmp_path.resolve())],
        "commercial_mode": "subscription-only",
        "resume_session_id": resume_session_id,
    }
    with pytest.raises(sdk_bridge.BridgeInputError, match="resume_session_id"):
        sdk_bridge._validate_request_payload(payload)
    with pytest.raises(ValueError, match="resume_session_id"):
        cursor.build_cursor_command(
            replace(request(tmp_path), resume_session_id=resume_session_id),
            isolated_workspace=tmp_path,
        )
    with pytest.raises(ValueError, match="resume_session_id"):
        claude.build_claude_command(
            replace(claude_request(tmp_path), resume_session_id=resume_session_id)
        )
    with pytest.raises(ValueError, match="resume_session_id"):
        codex.build_codex_command(
            replace(codex_request(tmp_path), resume_session_id=resume_session_id),
            config_lock=tmp_path / "runtime.config.lock.toml",
            output_schema_path=tmp_path / "result.schema.json",
        )


def _cursor_auth_probe(command, **kwargs):
    del kwargs
    if "status" in command:
        stdout = json.dumps(
            {
                "isAuthenticated": True,
                "hasAccessToken": True,
                "hasRefreshToken": True,
                "userInfo": {"email": "must-not-be-retained@example.invalid"},
            }
        )
    else:
        stdout = json.dumps(
            {
                "subscriptionTier": "Pro+",
                "userEmail": "must-not-be-retained@example.invalid",
            }
        )
    return process.ProcessResult(
        returncode=0,
        stdout=stdout,
        stderr="",
        duration_s=0.01,
        timed_out=False,
    )


def test_cursor_auth_requires_browser_refresh_token_and_paid_tier() -> None:
    accepted = cursor.parse_cursor_auth_status(
        json.dumps(
            {
                "isAuthenticated": True,
                "hasAccessToken": True,
                "hasRefreshToken": True,
                "userInfo": {"email": "must-not-be-retained@example.invalid"},
            }
        ),
        json.dumps(
            {
                "subscriptionTier": "Pro+",
                "userEmail": "must-not-be-retained@example.invalid",
            }
        ),
    )

    assert accepted.auth_method == "browser-login"
    assert accepted.has_subscription is True
    assert "must-not-be-retained" not in repr(accepted)

    with pytest.raises(cursor.CursorProtocolError):
        cursor.parse_cursor_auth_status(
            json.dumps(
                {
                    "isAuthenticated": True,
                    "hasAccessToken": True,
                    "hasRefreshToken": False,
                }
            ),
            json.dumps({"subscriptionTier": "Pro+"}),
        )
    with pytest.raises(cursor.CursorProtocolError):
        cursor.parse_cursor_auth_status(
            json.dumps(
                {
                    "isAuthenticated": True,
                    "hasAccessToken": True,
                    "hasRefreshToken": True,
                }
            ),
            json.dumps({"subscriptionTier": "Hobby"}),
        )


@pytest.mark.parametrize("separator", ["\u0085", "\u2028", "\u2029"])
@pytest.mark.parametrize("line_ending", ["\n", "\r\n"])
def test_cursor_stream_preserves_unicode_separators_inside_json_strings(
    separator: str, line_ending: str
) -> None:
    payload = f"left{separator}right"
    stream = line_ending.join(
        json.dumps(event, ensure_ascii=False)
        for event in (
            {"type": "assistant", "message": payload},
            {
                "type": "result",
                "subtype": "success",
                "is_error": False,
                "result": payload,
            },
        )
    )

    parsed = cursor.parse_cursor_stream(stream)

    assert parsed.status is contracts.RuntimeStatus.COMPLETED
    assert parsed.final_output == payload
    assert [event.kind for event in parsed.events] == ["assistant", "result"]


def test_cursor_stream_extracts_terminal_metadata_without_payload_telemetry() -> None:
    parsed = cursor.parse_cursor_stream(
        "\n".join(
            [
                '{"type":"system","subtype":"init","session_id":"session-1",'
                '"model":"Cursor Auto","cwd":"/secret/path","unknown":true}',
                '{"type":"tool_call","subtype":"completed","tool_call":'
                '{"writeToolCall":{"args":{"fileText":"secret tool payload"}}}}',
                '{"type":"future-event","detail":"unknown fields are tolerated"}',
                '{"type":"result","subtype":"success","is_error":false,'
                '"result":"secret final answer","session_id":"session-1",'
                '"request_id":"request-1"}',
            ]
        )
    )

    assert parsed.session_id == "session-1"
    assert parsed.effective_model == "Cursor Auto"
    assert parsed.request_id == "request-1"
    assert parsed.status is contracts.RuntimeStatus.COMPLETED
    assert [(event.kind, event.subtype) for event in parsed.events] == [
        ("system", "init"),
        ("tool_call", "completed"),
        ("future-event", None),
        ("result", "success"),
    ]
    assert "secret tool payload" not in repr(parsed)
    assert "secret final answer" not in repr(parsed)
    assert parsed.final_output == "secret final answer"


def test_cursor_stream_rejects_events_after_terminal_result() -> None:
    with pytest.raises(cursor.CursorProtocolError):
        cursor.parse_cursor_stream(
            "\n".join(
                [
                    '{"type":"result","subtype":"success",'
                    '"is_error":false,"result":"done"}',
                    '{"type":"assistant","message":{"content":[]}}',
                ]
            )
        )


def test_cursor_stream_accepts_large_governed_output_without_repr_leak() -> None:
    final_output = "x" * 10_000
    parsed = cursor.parse_cursor_stream(
        json.dumps(
            {
                "type": "result",
                "subtype": "success",
                "is_error": False,
                "result": final_output,
            }
        )
    )

    assert parsed.final_output == final_output
    assert final_output not in repr(parsed)


def test_cursor_stream_bounds_sanitized_event_metadata() -> None:
    events = [json.dumps({"type": "assistant"}) for _ in range(600)]
    events.append(
        json.dumps(
            {
                "type": "result",
                "subtype": "success",
                "is_error": False,
                "result": "done",
            }
        )
    )

    parsed = cursor.parse_cursor_stream("\n".join(events))

    assert len(parsed.events) == cursor.MAX_RETAINED_EVENTS
    assert parsed.events[-1].kind == "result"


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("type", "assistant output must not become metadata"),
        ("type", "event\nprivate-output"),
        ("type", "x" * (cursor.MAX_EVENT_LABEL_CHARS + 1)),
        ("subtype", "private output"),
    ],
)
def test_cursor_stream_rejects_unsafe_event_labels(field: str, value: str) -> None:
    event = {
        "type": "result",
        "subtype": "success",
        "is_error": False,
        "result": "done",
    }
    event[field] = value

    with pytest.raises(cursor.CursorProtocolError):
        cursor.parse_cursor_stream(json.dumps(event))


@pytest.mark.parametrize(
    "stream",
    [
        "not json",
        '{"type": 1}',
        '{"type":"result","subtype":"success"}',
        '{"type":"system","subtype":"init"}',
    ],
)
def test_cursor_stream_malformed_events_fail_closed(stream: str) -> None:
    with pytest.raises(cursor.CursorProtocolError):
        cursor.parse_cursor_stream(stream)


@pytest.mark.parametrize(
    ("stdout", "expected_cause"),
    [
        ("hostile invalid json: secret-token\n", "invalid-json"),
        (
            json.dumps(["secret-model-output"]),
            "event-not-object",
        ),
        (
            json.dumps({"subtype": "secret-subtype"}),
            "required-field-missing; field=type",
        ),
        (
            json.dumps({"type": "assistant output contains secret-token"}),
            "field-not-identifier; field=type",
        ),
        (
            json.dumps({"type": "assistant", "session_id": "x" * 257}),
            "field-too-long; field=session_id",
        ),
        (
            json.dumps({"type": "assistant", "session_id": {"secret": "secret-token"}}),
            "field-not-string; field=session_id",
        ),
        (
            '{"type":"result","subtype":"success","is_error":"secret",'
            '"result":"secret-model-output"}',
            "terminal-incomplete",
        ),
        (
            '{"type":"result","subtype":"success","is_error":false}',
            "terminal-output-missing",
        ),
        ("\n", "empty-stream"),
        ('{"type":"assistant","message":"secret-model-output"}', "terminal-missing"),
        (
            '{"type":"result","subtype":"success","is_error":false,'
            '"result":"done"}\n'
            '{"type":"assistant","message":"secret-after-terminal"}',
            "event-after-terminal",
        ),
    ],
)
def test_cursor_adapter_returns_typed_protocol_failure_without_payload_disclosure(
    tmp_path: Path,
    stdout: str,
    expected_cause: str,
) -> None:
    def run_bad_output(*args, **kwargs):
        del args, kwargs
        return process.ProcessResult(
            returncode=0,
            stdout=stdout,
            stderr="hostile-stderr credential-token request-id-123",
            duration_s=0.01,
            timed_out=False,
        )

    result = cursor.CursorAdapter(
        run_cli=run_bad_output,
        run_probe=_cursor_auth_probe,
        which=lambda _name: "/usr/bin/cursor-agent",
    ).run(request(tmp_path))

    assert result.status is contracts.RuntimeStatus.FAILED
    assert result.terminal_reason is contracts.TerminalReason.MALFORMED_EVENT
    assert result.diagnostics == (
        "cursor emitted malformed stream-json",
        f"Cursor parser cause: {expected_cause}",
    )
    for secret in (
        "secret-token",
        "secret-model-output",
        "secret-subtype",
        "secret-after-terminal",
        "hostile-stderr",
        "credential-token",
        "request-id-123",
    ):
        assert secret not in repr(result.diagnostics)


def test_cursor_adapter_reports_bounded_output_cause_without_output_disclosure(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(cursor, "MAX_FINAL_OUTPUT_BYTES", 4)

    def run_oversized_output(*args, **kwargs):
        del args, kwargs
        return process.ProcessResult(
            returncode=0,
            stdout=(
                '{"type":"result","subtype":"success","is_error":false,'
                '"result":"secret-model-output"}'
            ),
            stderr="secret-stderr",
            duration_s=0.01,
            timed_out=False,
        )

    result = cursor.CursorAdapter(
        run_cli=run_oversized_output,
        run_probe=_cursor_auth_probe,
        which=lambda _name: "/usr/bin/cursor-agent",
    ).run(request(tmp_path))

    assert result.status is contracts.RuntimeStatus.FAILED
    assert result.terminal_reason is contracts.TerminalReason.MALFORMED_EVENT
    assert result.diagnostics == (
        "cursor emitted malformed stream-json",
        "Cursor parser cause: terminal-output-too-large",
    )
    assert "secret" not in repr(result.diagnostics)


def test_cursor_probe_fails_closed_when_subscription_policy_is_ambiguous(
    tmp_path: Path,
) -> None:
    readiness = cursor.CursorAdapter().probe(
        request(
            tmp_path,
            eligibility=contracts.SubscriptionEligibility.AMBIGUOUS,
        )
    )

    assert readiness.ready is False
    assert readiness.failure is contracts.ReadinessFailure.SUBSCRIPTION_UNAVAILABLE


def test_cursor_adapter_keeps_governed_output_out_of_result_repr(
    tmp_path: Path,
) -> None:
    launches: list[tuple[list[str], Path, str]] = []

    def run_success(command, *, cwd, input_text, **kwargs):
        del kwargs
        launches.append((list(command), cwd, input_text))
        return process.ProcessResult(
            returncode=0,
            stdout=(
                '{"type":"result","subtype":"success","is_error":false,'
                '"result":"secret governed output"}\n'
            ),
            stderr="",
            duration_s=0.01,
            timed_out=False,
        )

    result = cursor.CursorAdapter(
        run_cli=run_success,
        run_probe=_cursor_auth_probe,
        which=lambda _name: "/usr/bin/cursor-agent",
    ).run(request(tmp_path))

    assert result.final_output == "secret governed output"
    assert "secret governed output" not in repr(result)
    assert len(launches) == 1
    command, isolated_cwd, input_text = launches[0]
    assert isolated_cwd != tmp_path
    assert command[command.index("--workspace") + 1] == str(isolated_cwd)
    assert command[command.index("--add-dir") + 1] == str(tmp_path)
    assert str(tmp_path) in input_text


def test_cursor_run_workspace_is_inside_documented_settings_root(
    tmp_path: Path,
) -> None:
    tooling_root = tmp_path / "tooling"
    worktree = tmp_path / "worktree"
    tooling_root.mkdir()
    worktree.mkdir()
    settings = RuntimeSettings(tooling_root=tooling_root)
    workspace_root = settings.workspace_root(tooling_root)
    observed_workspace: Path | None = None

    def run_success(command, *, cwd, **kwargs):
        nonlocal observed_workspace
        del command, kwargs
        observed_workspace = cwd
        return process.ProcessResult(
            returncode=0,
            stdout=(
                '{"type":"result","subtype":"success","is_error":false,'
                '"result":"done"}'
            ),
            stderr="",
            duration_s=0.01,
            timed_out=False,
        )

    with settings.use():
        adapter = cursor.CursorAdapter(
            run_cli=run_success,
            run_probe=_cursor_auth_probe,
            which=lambda _name: "/usr/bin/cursor-agent",
        )
    outcome = adapter.run(replace(request(worktree), tooling_root=tooling_root))

    assert outcome.status is contracts.RuntimeStatus.COMPLETED
    assert observed_workspace is not None
    assert observed_workspace.is_relative_to(workspace_root)


def test_cursor_run_fails_closed_before_launch_when_eligibility_is_ambiguous(
    tmp_path: Path,
) -> None:
    launches: list[list[str]] = []

    def unexpected_launch(command, **kwargs):
        del kwargs
        launches.append(list(command))
        raise AssertionError("ambiguous subscription eligibility must not launch")

    result = cursor.CursorAdapter(
        run_cli=unexpected_launch,
        which=lambda _name: "/usr/bin/cursor-agent",
    ).run(
        request(
            tmp_path,
            eligibility=contracts.SubscriptionEligibility.AMBIGUOUS,
        )
    )

    assert result.status is contracts.RuntimeStatus.SUBSCRIPTION_UNAVAILABLE
    assert result.terminal_reason is contracts.TerminalReason.SUBSCRIPTION_UNAVAILABLE
    assert launches == []


def test_cursor_run_normalizes_executable_startup_race(tmp_path: Path) -> None:
    def missing_at_launch(*args, **kwargs):
        del args, kwargs
        raise OSError("executable disappeared")

    result = cursor.CursorAdapter(
        run_cli=missing_at_launch,
        run_probe=_cursor_auth_probe,
        which=lambda _name: "/usr/bin/cursor-agent",
    ).run(request(tmp_path))

    assert result.status is contracts.RuntimeStatus.FAILED
    assert result.terminal_reason is contracts.TerminalReason.STARTUP_FAILURE
    assert result.diagnostics == ("Cursor CLI startup failed",)


@pytest.mark.parametrize(
    "error_type", [BlockingIOError, InterruptedError, BrokenPipeError]
)
def test_progress_reporter_recovers_after_transient_pipe_errors(error_type) -> None:
    now = [0.0]
    frames = []
    writes = 0

    def write_frame(frame):
        nonlocal writes
        writes += 1
        if writes == 2:
            raise error_type()
        frames.append(frame)

    reporter = sdk_bridge._ProgressReporter(write_frame, clock=lambda: now[0])
    reporter.start(start_thread=False)
    now[0] = 0.25
    reporter.observe(
        contracts.RuntimePhase.TOOL_EXECUTION,
        last_tool=contracts.RuntimeToolLabel.COMMAND,
    )
    now[0] = 30.0
    reporter.report_due()
    reporter.close()

    if error_type is BrokenPipeError:
        assert writes == 2
        assert len(frames) == 1
    else:
        assert writes == 3
        recovered = contracts.parse_runtime_progress_frame(frames[-1])
        assert recovered.sequence == 3
        assert recovered.signal is contracts.RuntimeProgressSignal.HEARTBEAT
        assert recovered.phase is contracts.RuntimePhase.TOOL_EXECUTION
        assert recovered.last_tool is contracts.RuntimeToolLabel.COMMAND


def test_claude_failed_sdk_retains_safe_permission_diagnostics(
    monkeypatch, tmp_path, capsys
):
    import claude_agent_sdk
    from claude_agent_sdk import ResultMessage

    evidence = tmp_path / ".audit" / "dispatch-inputs" / "copy.json"
    evidence.parent.mkdir(parents=True)
    evidence.write_text("{}")
    external = tmp_path.parent / "secret-token-original.json"
    (tmp_path / "escape").symlink_to(external)

    async def fake_query(**kwargs):
        options = kwargs["options"]
        callback = options.can_use_tool
        assert callback is not None
        for path in (str(evidence), ".audit/dispatch-inputs/copy.json"):
            result = await callback("Read", {"file_path": path}, None)
            assert result.behavior == "allow"
        for path in (".audit/secret-original.json", str(external), "escape"):
            result = await callback("Read", {"file_path": path}, None)
            assert result.behavior == "deny"
        hook = options.hooks["PreToolUse"][0].hooks[0]
        result = await hook(
            {
                "tool_name": "Read",
                "tool_input": {"file_path": ".audit/secret-original.json"},
            },
            None,
            None,
        )
        assert result["hookSpecificOutput"]["permissionDecision"] == "deny"
        yield ResultMessage(
            subtype="error_max_structured_output_retries",
            duration_ms=1,
            duration_api_ms=1,
            is_error=True,
            num_turns=1,
            session_id="session-claude",
            result="secret provider diagnostic",
        )

    monkeypatch.setattr(claude_agent_sdk, "query", fake_query)
    asyncio.run(
        sdk_bridge._run_claude(
            {
                "vendor": "claude",
                "prompt": "private prompt",
                "cwd": str(tmp_path),
                "requested_model": "claude-opus-5",
                "effort": "high",
                "read_only": True,
                "budget_usd": None,
                "commercial_mode": "subscription-only",
                "output_schema": governed_schema("attempt-claude"),
                "read_roots": [str(tmp_path.resolve())],
            }
        )
    )
    output = capsys.readouterr().out
    terminal = json.loads(output.splitlines()[-1])
    assert terminal["permission_check_denials"] == {
        "missing-path": 2,
        "outside-root": 2,
    }
    parsed = claude.parse_claude_stream(output)
    assert parsed.status is contracts.RuntimeStatus.FAILED
    assert (
        "Claude SDK result subtype: error_max_structured_output_retries"
        in parsed.diagnostics
    )
    assert (
        "Claude denied permission checks: missing-path=2, outside-root=2"
        in parsed.diagnostics
    )
    for secret in ("secret-token", "secret-original", "secret provider", str(tmp_path)):
        assert secret not in output
        assert secret not in repr(parsed.diagnostics)


def test_claude_permission_diagnostics_reject_untrusted_values():
    parsed = claude.parse_claude_stream(
        json.dumps(
            {
                "type": "result",
                "status": "failed",
                "terminal_reason": "process-exit",
                "subtype": "secret-provider-error",
                "permission_check_denials": {
                    "secret-path": 1,
                    "missing-path": True,
                    "outside-root": 10001,
                    "invalid-input": "secret-value",
                    "unresolvable-path": 2,
                },
            }
        )
    )
    assert parsed.diagnostics == (
        "Claude denied permission checks: unresolvable-path=2",
    )
    assert "secret" not in repr(parsed.diagnostics)


def test_cursor_grok_preserves_explicit_model(tmp_path: Path) -> None:
    runtime_request = replace(request(tmp_path), requested_model="cursor-grok-4.6-high")
    command = cursor.build_cursor_command(runtime_request, isolated_workspace=tmp_path)
    assert command[command.index("--model") + 1] == "cursor-grok-4.6-high"
    assert "auto" not in command


@pytest.mark.parametrize(
    ("stream", "extra"),
    [
        ('{"type":"error","reason":"startup"}\n', ("Codex SDK startup failed",)),
        ('{"type":"error","reason":"protocol"}\n', ("Codex SDK protocol failed",)),
        (
            '{"type":"error","reason":"disconnect"}\n',
            ("Codex SDK transport disconnected",),
        ),
        (
            '{"type":"error","reason":"startup","message":"Authorization: Bearer synthetic-token; Cookie: private; https://example.invalid/?secret=private; ENV=private; person@example.invalid", "session_id":"private-path"}\n',
            ("Codex SDK startup failed",),
        ),
        (
            '{"type":"error","reason":"startup"}\n{"type":"result","status":"completed","terminal_reason":"completed","output":"private partial"}\n',
            ("Codex SDK startup failed",),
        ),
        (
            '{"type":"result","status":"completed","terminal_reason":"completed","output":"private partial"}\n',
            (),
        ),
        ('{"type":"error","reason":"startup"}', ()),
        ('{"type":"error","reason":', ()),
        ("not JSON\n[]\n", ()),
        ('{"type":"error","reason":"startup\\u001b[31mprivate"}\n', ()),
        ('{"type":"error","reason":["startup"]}\n', ()),
        ('{"type":"event","kind":"private","session_id":"private-checkpoint"}\n', ()),
        ('{"type":"error","reason":"startup","message":"' + "x" * 1024 + '"}\n', ()),
        ('{"type":"error","reason":"startup"}\n' * 20, ()),
        ("", ()),
        (
            '{"type":"error","reason":"startup"}\n' * 4,
            ("Codex SDK startup failed",) * 3,
        ),
        (
            '{"type":"error","reason":"startup"}\n{"type":',
            ("Codex SDK startup failed",),
        ),
    ],
    ids=[
        "startup",
        "protocol",
        "disconnect",
        "secret-fields",
        "success-after-error",
        "success-only",
        "unterminated",
        "truncated",
        "malformed",
        "control-reason",
        "malformed-reason",
        "unattested-event",
        "oversized-line",
        "oversized-stream",
        "empty",
        "retention-bound",
        "truncated-tail",
    ],
)
def test_codex_timeout_retains_only_fixed_diagnostics(
    monkeypatch, tmp_path: Path, stream: str, extra: tuple[str, ...]
) -> None:
    monkeypatch.setattr(codex, "MAX_BRIDGE_LINE_BYTES", 512)
    calls = []

    def run_process(command, **kwargs):
        calls.append(command)
        return process.ProcessResult(
            returncode=124,
            stdout=stream,
            stderr="private stderr",
            duration_s=5.0,
            timed_out=True,
        )

    result = codex.CodexAdapter(run_process=run_process)._run_sdk(
        codex_request(tmp_path)
    )
    assert result.status is contracts.RuntimeStatus.TIMED_OUT
    assert result.terminal_reason is contracts.TerminalReason.TIMEOUT
    assert result.returncode == 124
    assert result.diagnostics == ("Codex SDK bridge timed out", *extra)
    assert result.final_output is None
    assert result.structured_output is None
    assert result.session_id is None
    assert result.request_id is None
    assert result.effective_model is None
    assert result.usage is None
    assert result.cost_usd is None
    assert result.events == ()
    assert len(calls) == 1
    assert result.transport_attempts[0].selected_next is False
    assert (
        result.transport_attempts[0].status == contracts.RuntimeStatus.TIMED_OUT.value
    )


def test_codex_complete_result_still_parses_normally(tmp_path: Path) -> None:
    stream = '{"type":"result","status":"completed","terminal_reason":"completed","output":"normal result"}\n'
    result = codex.CodexAdapter(
        run_process=lambda *args, **kwargs: process.ProcessResult(
            returncode=0,
            stdout=stream,
            stderr="",
            duration_s=0.1,
            timed_out=False,
        )
    )._run_sdk(replace(codex_request(tmp_path), output_schema=None))
    assert result.status is contracts.RuntimeStatus.COMPLETED
    assert result.returncode == 0
    assert result.final_output == "normal result"
    assert result.diagnostics == ()


@pytest.fixture
def command_completion_stream(monkeypatch, tmp_path):
    """Replay real SDK notifications through the producer, never a provider."""
    import io
    from contextlib import redirect_stdout

    import openai_codex
    from openai_codex.generated.v2_all import (
        AgentMessageThreadItem,
        CommandExecutionThreadItem,
        ItemCompletedNotification,
        ItemStartedNotification,
        ThreadItem,
        Turn,
        TurnCompletedNotification,
    )
    from openai_codex.models import Notification

    events = []

    class Stream(list):
        def close(self):
            pass

    class FakeTurn:
        id = "private-turn"

        def stream(self):
            return Stream(events)

    class FakeThread:
        id = "private-thread"

        def turn(self, *args, **kwargs):
            return FakeTurn()

    class FakeCodex:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def thread_start(self, **kwargs):
            return FakeThread()

    @contextmanager
    def config_lock(_request):
        yield tmp_path / "unused.config.lock.toml"

    monkeypatch.setattr(openai_codex, "Codex", FakeCodex)
    monkeypatch.setattr(sdk_bridge, "_codex_effective_config_lock", config_lock)
    monkeypatch.setattr(sdk_bridge, "_enforce_codex_chatgpt_login", lambda *args: None)
    monkeypatch.setattr(
        sdk_bridge, "_materialize_codex_subscription_auth", lambda: (None, {}, None)
    )

    def replay(status="completed", exit_code=0, output="żółć", *, completed=True):
        def item(state, code=None, text=None):
            return ThreadItem(
                root=CommandExecutionThreadItem(
                    id="private-command-id",
                    command="PRIVATE_COMMAND",
                    commandActions=[],
                    cwd="/PRIVATE_PATH",
                    status=state,
                    type="commandExecution",
                    exitCode=code,
                    aggregatedOutput=text,
                )
            )

        events[:] = [
            Notification(
                "item/started",
                ItemStartedNotification(
                    item=item("inProgress"),
                    startedAtMs=1,
                    threadId="private-thread",
                    turnId="private-turn",
                ),
            )
        ]
        if completed:
            events.append(
                Notification(
                    "item/completed",
                    ItemCompletedNotification(
                        item=item(status, exit_code, output),
                        completedAtMs=2,
                        threadId="private-thread",
                        turnId="private-turn",
                    ),
                )
            )
        events.extend(
            [
                Notification(
                    "item/completed",
                    ItemCompletedNotification(
                        item=ThreadItem(
                            root=AgentMessageThreadItem(
                                id="private-message",
                                text=json.dumps({"ok": True}),
                                phase="final_answer",
                                type="agentMessage",
                            )
                        ),
                        completedAtMs=3,
                        threadId="private-thread",
                        turnId="private-turn",
                    ),
                ),
                Notification(
                    "turn/completed",
                    TurnCompletedNotification(
                        threadId="private-thread",
                        turn=Turn(id="private-turn", items=[], status="completed"),
                    ),
                ),
            ]
        )
        captured = io.StringIO()
        with redirect_stdout(captured):
            sdk_bridge._run_codex(
                {
                    "vendor": "codex",
                    "prompt": "private prompt",
                    "cwd": str(tmp_path),
                    "requested_model": "gpt-5.6-sol",
                    "effort": "high",
                    "read_only": True,
                    "commercial_mode": "subscription-only",
                    "output_schema": {"type": "object"},
                    "read_roots": [str(tmp_path)],
                }
            )
        return captured.getvalue()

    return replay


@pytest.mark.parametrize("timed_out", [False, True])
@pytest.mark.parametrize(
    ("status", "code", "output", "size"),
    [
        ("completed", 0, "", 0),
        ("completed", 0, "żółć", 8),
        ("failed", 2, "PRIVATE_ERROR", 13),
        ("declined", None, None, None),
    ],
)
def test_command_completion_producer_to_adapters(
    command_completion_stream,
    tmp_path,
    timed_out,
    status,
    code,
    output,
    size,
):
    stream = command_completion_stream(status, code, output)
    diagnostic = f"Codex command completed: status={status}; exit_code={code}; observed_output_bytes={size}"
    result = codex.CodexAdapter(
        run_process=lambda *args, **kwargs: process.ProcessResult(
            returncode=124 if timed_out else 0,
            stdout=stream,
            stderr="",
            duration_s=1,
            timed_out=timed_out,
        )
    )._run_sdk(codex_request(tmp_path))
    assert result.diagnostics == (
        ("Codex SDK bridge timed out", diagnostic) if timed_out else (diagnostic,)
    )
    assert result.status is (
        contracts.RuntimeStatus.TIMED_OUT
        if timed_out
        else contracts.RuntimeStatus.COMPLETED
    )
    assert result.structured_output == (None if timed_out else {"ok": True})
    if timed_out:
        assert result.final_output is result.usage is result.cost_usd is None
        assert result.events == ()
    frames = [json.loads(line) for line in stream.splitlines()]
    completion = [f for f in frames if f.get("kind") == "command_completion"]
    assert len(completion) == 1
    assert set(completion[0]) == {
        "type",
        "kind",
        "semantic",
        "status",
        "exit_code",
        "output_bytes",
    }
    assert not completion[0]["semantic"]
    assert all(
        secret not in repr(completion) for secret in ("PRIVATE", "private", "żółć")
    )


def test_command_started_only_has_no_completion(command_completion_stream):
    stream = command_completion_stream(completed=False)
    assert codex.parse_codex_stream(stream).diagnostics == ()
    assert codex._timeout_diagnostics(stream) == ()


@pytest.mark.parametrize(
    "mutation",
    [
        {"status": "inProgress"},
        {"status": "failed\nSECRET"},
        {"status": True},
        {"exit_code": True},
        {"exit_code": "SECRET"},
        {"exit_code": 10**12 + 1},
        {"output_bytes": True},
        {"output_bytes": -1},
        {"output_bytes": 10**12 + 1},
        {"output_bytes": "SECRET"},
        {"semantic": True},
        {"command": "SECRET"},
        {"output": "SECRET"},
        {"id": "SECRET"},
        {"path": "SECRET"},
    ],
)
def test_command_completion_invalid_metadata_denied(mutation):
    frame = {
        "type": "event",
        "kind": "command_completion",
        "semantic": False,
        "status": "completed",
        "exit_code": 0,
        "output_bytes": 0,
    }
    valid = json.dumps(frame) + "\n"
    assert codex._timeout_diagnostics(valid), "nonvacuous valid sibling"
    frame.update(mutation)
    stream = json.dumps(frame) + "\n"
    assert codex._timeout_diagnostics(stream) == ()
    terminal = '{"type":"result","status":"completed","terminal_reason":"completed","structured_output":{"ok":true}}\n'
    parsed = codex.parse_codex_stream(stream + terminal)
    assert parsed.diagnostics == ()
    assert parsed.structured_output == {"ok": True}


def test_command_completion_partial_oversized_and_repetition(
    command_completion_stream, monkeypatch
):
    stream = command_completion_stream()
    frame = next(
        line
        for line in stream.splitlines()
        if json.loads(line).get("kind") == "command_completion"
    )
    assert codex._timeout_diagnostics(frame + "\n")
    assert codex._timeout_diagnostics(frame) == ()
    assert codex._timeout_diagnostics(frame[:-5] + "\n") == ()
    repeated = (frame + "\n") * 20
    assert len(codex._timeout_diagnostics(repeated)) == codex.MAX_DIAGNOSTICS - 1
    result = codex.parse_codex_stream(repeated + stream)
    assert len(result.diagnostics) == codex.MAX_DIAGNOSTICS
    assert result.structured_output == {"ok": True}
    monkeypatch.setattr(codex, "MAX_BRIDGE_LINE_BYTES", len(frame) - 1)
    assert codex._timeout_diagnostics(frame + "\n") == ()
    with pytest.raises(codex.CodexProtocolError):
        codex.parse_codex_stream(frame + "\n")


@pytest.mark.parametrize("output", ["\ud800", "x" * 17])
def test_command_completion_producer_unknown_size_is_not_total(
    monkeypatch, capsys, output
):
    from types import SimpleNamespace

    monkeypatch.setattr(sdk_bridge, "MAX_FINAL_OUTPUT_BYTES", 16)
    sdk_bridge._codex_command_completion(
        SimpleNamespace(
            status="completed",
            exit_code=True,
            aggregated_output=output,
        )
    )
    frame = json.loads(capsys.readouterr().out)
    assert frame["exit_code"] is None
    assert frame["output_bytes"] is None
    assert codex._timeout_diagnostics(json.dumps(frame) + "\n")


def test_command_completion_latest_is_bounded_and_does_not_evict_errors(
    command_completion_stream,
):
    frame = next(
        json.loads(line)
        for line in command_completion_stream().splitlines()
        if json.loads(line).get("kind") == "command_completion"
    )
    rows = []
    for index in range(20):
        rows.append(json.dumps({**frame, "output_bytes": index}) + "\n")
    rows.insert(0, '{"type":"error","reason":"startup"}\n')
    diagnostics = codex._timeout_diagnostics("".join(rows))
    assert len(diagnostics) == codex.MAX_DIAGNOSTICS - 1
    assert diagnostics[0] == "Codex SDK startup failed"
    assert diagnostics[-1].endswith("observed_output_bytes=19")
    with pytest.raises(codex.CodexProtocolError):
        codex.parse_codex_stream("".join(rows[1:]))


@pytest.mark.parametrize("remove", ["status", "exit_code", "output_bytes", "semantic"])
def test_command_completion_missing_fields_are_not_inferred(
    command_completion_stream, remove
):
    frame = next(
        json.loads(line)
        for line in command_completion_stream().splitlines()
        if json.loads(line).get("kind") == "command_completion"
    )
    assert codex._timeout_diagnostics(json.dumps(frame) + "\n")
    del frame[remove]
    assert codex._timeout_diagnostics(json.dumps(frame) + "\n") == ()


@pytest.mark.parametrize("timed_out", [False, True])
@pytest.mark.parametrize("order", ["completions-first", "interleaved"])
def test_command_completion_later_bridge_errors_take_priority(
    command_completion_stream,
    tmp_path,
    timed_out,
    order,
):
    frame = next(
        json.loads(line)
        for line in command_completion_stream().splitlines()
        if json.loads(line).get("kind") == "command_completion"
    )
    completions = [json.dumps({**frame, "output_bytes": i}) + "\n" for i in range(4)]
    # Nonvacuity: completion diagnostics really fill the available slots first.
    assert (
        len(codex._timeout_diagnostics("".join(completions)))
        == codex.MAX_DIAGNOSTICS - 1
    )
    startup = '{"type":"error","reason":"startup"}\n'
    protocol = '{"type":"error","reason":"protocol"}\n'
    started = (
        '{"type":"event","kind":"item","subtype":"commandExecution","semantic":true}\n'
    )
    if order == "completions-first":
        rows = [started, *completions, startup]
    elif timed_out:
        rows = [started, *completions, startup, *completions, protocol, *completions]
    else:
        # A normal error frame is terminal: interleave activity/completions
        # before that error, never append invalid frames after the terminal.
        rows = [started, *completions[:2], started, *completions[2:], startup]
    result = codex.CodexAdapter(
        run_process=lambda *args, **kwargs: process.ProcessResult(
            returncode=124 if timed_out else 1,
            stdout="".join(rows),
            stderr="",
            duration_s=1,
            timed_out=timed_out,
        )
    )._run_sdk(codex_request(tmp_path))
    assert "Codex SDK startup failed" in result.diagnostics
    if order == "interleaved" and timed_out:
        assert "Codex SDK protocol failed" in result.diagnostics
    assert any(
        item.startswith("Codex command completed:") for item in result.diagnostics
    )
    assert len(result.diagnostics) <= codex.MAX_DIAGNOSTICS
    assert result.status is (
        contracts.RuntimeStatus.TIMED_OUT
        if timed_out
        else contracts.RuntimeStatus.FAILED
    )
    assert result.terminal_reason is (
        contracts.TerminalReason.TIMEOUT
        if timed_out
        else contracts.TerminalReason.STARTUP_FAILURE
    )
    assert result.final_output is result.structured_output is None


@pytest.mark.parametrize("buffer_kind", ["saturated", "empty", "mixed", "error-only"])
@pytest.mark.parametrize("progress_lost", [False, True])
def test_command_completion_result_progress_priority(
    tmp_path, buffer_kind, progress_lost
):
    completion = "Codex command completed: status=completed; exit_code=0; observed_output_bytes=0"
    errors = (
        "Codex SDK startup failed",
        "Codex SDK protocol failed",
        "Codex SDK bridge timed out",
        "Codex SDK failed",
    )
    before = {
        "saturated": (completion,) * 4,
        "empty": (),
        "mixed": (errors[0], completion, errors[1], completion),
        "error-only": errors,
    }[buffer_kind]
    result = codex.CodexAdapter()._result(
        codex_request(tmp_path),
        outcome=process.ProcessResult(
            returncode=1,
            stdout="",
            stderr="",
            duration_s=1,
            timed_out=False,
            progress_diagnostic=progress_lost,
        ),
        status=contracts.RuntimeStatus.FAILED,
        reason=contracts.TerminalReason.STARTUP_FAILURE,
        diagnostics=before,
    )
    warning = "Native runtime progress was dropped"
    assert (warning in result.diagnostics) is (
        progress_lost and buffer_kind != "error-only"
    )
    assert all(error in result.diagnostics for error in before if error in errors)
    assert len(result.diagnostics) <= codex.MAX_DIAGNOSTICS
    assert result.status is contracts.RuntimeStatus.FAILED
    assert result.terminal_reason is contracts.TerminalReason.STARTUP_FAILURE
    assert (
        result.structured_output
        is result.final_output
        is result.usage
        is result.cost_usd
        is None
    )
    if not progress_lost or buffer_kind == "error-only":
        assert result.diagnostics == before


@pytest.mark.parametrize("timed_out", [False, True])
def test_command_completion_adapter_callback_loss_priority(
    command_completion_stream, tmp_path, timed_out
):
    frames = command_completion_stream().splitlines()
    completion = next(
        line for line in frames if json.loads(line).get("kind") == "command_completion"
    )
    index = frames.index(completion)
    frames[index : index + 1] = [completion] * 4
    stream = "\n".join(frames) + "\n"
    assert (
        sum(json.loads(line).get("kind") == "command_completion" for line in frames)
        == 4
    )
    calls = []

    def callback(_event):
        raise RuntimeError("private callback error")

    def run_process(*args, **kwargs):
        try:
            kwargs["on_progress"]({"kind": "item-completed"})
        except RuntimeError:
            calls.append("lost")
        return process.ProcessResult(
            returncode=124 if timed_out else 0,
            stdout=stream,
            stderr="",
            duration_s=1,
            timed_out=timed_out,
            progress_diagnostic=True,
        )

    result = codex.CodexAdapter(run_process=run_process)._run_sdk(
        codex_request(tmp_path), on_progress=callback
    )
    assert calls == ["lost"]
    assert "Native runtime progress was dropped" in result.diagnostics
    assert len(result.diagnostics) == codex.MAX_DIAGNOSTICS
    assert "private callback error" not in repr(result)
    assert result.status is (
        contracts.RuntimeStatus.TIMED_OUT
        if timed_out
        else contracts.RuntimeStatus.COMPLETED
    )
    assert result.structured_output == (None if timed_out else {"ok": True})
    if timed_out:
        assert "Codex SDK bridge timed out" in result.diagnostics
        assert result.events == ()
        assert result.final_output is result.usage is result.cost_usd is None


def test_command_completion_fallback_progress_priority(tmp_path, monkeypatch):
    adapter = codex.CodexAdapter()
    request = codex_request(tmp_path)
    completion = "Codex command completed: status=completed; exit_code=0; observed_output_bytes=0"
    baseline = adapter._result(
        request,
        status=contracts.RuntimeStatus.COMPLETED,
        reason=contracts.TerminalReason.COMPLETED,
        diagnostics=(completion,) * 4,
        structured_output={"ok": True},
    )
    monkeypatch.setattr(
        adapter,
        "_cli_launch_readiness",
        lambda _request: contracts.RuntimeReadiness(
            ready=True, eligibility=request.eligibility
        ),
    )
    monkeypatch.setattr(adapter, "_run_cli", lambda _request: baseline)
    result = adapter._run_read_only_fallback(
        request,
        reason=contracts.TerminalReason.STARTUP_FAILURE,
        progress_diagnostic=True,
    )
    assert "Native runtime progress was dropped" in result.diagnostics
    assert len(result.diagnostics) == codex.MAX_DIAGNOSTICS
    assert result.status is baseline.status
    assert result.structured_output == baseline.structured_output


def test_command_completion_unknown_frame_priority(command_completion_stream):
    frames = command_completion_stream().splitlines()
    completion = next(
        line for line in frames if json.loads(line).get("kind") == "command_completion"
    )
    index = frames.index(completion)
    frames[index : index + 1] = [completion] * 4 + ['{"type":"unknown"}']
    result = codex.parse_codex_stream("\n".join(frames) + "\n")
    assert "Codex emitted an unknown protocol frame" in result.diagnostics
    assert len(result.diagnostics) == codex.MAX_DIAGNOSTICS
    assert result.status is contracts.RuntimeStatus.COMPLETED
    assert result.structured_output == {"ok": True}
