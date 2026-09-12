"""Deterministic tests for the opt-in live harness mechanics."""

from __future__ import annotations

from contextlib import contextmanager
import json
import os
from pathlib import Path
import sys

import pytest

from conftest import REPO
from loopzero.runners.settings import RuntimeBudget, RuntimeSettings
from loopzero.runners.contract import (
    RuntimeCapabilityProfile,
    RuntimeCostStatus,
    RuntimeEvent,
    RuntimeResult,
    RuntimeStatus,
    RuntimeTransportAttempt,
    SubscriptionEligibility,
    TerminalReason,
)
from loopzero.runners.fake import Scenario
from loopzero.runners.registry import RUNTIME_REGISTRY
from tests.conformance.live import live_conformance as live


def _codex_access_only() -> bytes:
    return json.dumps({
        "auth_mode": "chatgpt",
        "OPENAI_API_KEY": None,
        "tokens": {
            "access_token": "access-only",
            "id_token": "identity",
            "refresh_token": "access-only",
            "account_id": "account",
        },
        "last_refresh": "2026-09-09T17:58:29Z",
    }).encode()


def test_live_workspace_is_writable_beside_a_read_only_checkout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    checkout = tmp_path / "checkout"
    checkout.mkdir(mode=0o500)
    state_root = tmp_path / "state"
    settings = RuntimeSettings(tooling_root=checkout, state_root=str(state_root))
    monkeypatch.setattr(live.shutil, "which", lambda _name: "/usr/bin/bwrap")
    monkeypatch.setattr(
        live.subprocess,
        "run",
        lambda *_args, **_kwargs: type("Probe", (), {"returncode": 0})(),
    )

    live._sandbox_wrapper(settings)
    workspace_root = settings.workspace_root(checkout)
    probe = workspace_root / "writable"
    probe.write_text("ok", encoding="utf-8")

    assert not workspace_root.is_relative_to(checkout)
    assert probe.read_text(encoding="utf-8") == "ok"


def test_live_sandbox_probe_reports_bubblewraps_last_error_line(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = RuntimeSettings(state_root=str(tmp_path / "state"))
    monkeypatch.setattr(live.shutil, "which", lambda _name: "/usr/bin/bwrap")
    monkeypatch.setattr(
        live.subprocess,
        "run",
        lambda *_args, **_kwargs: type(
            "Probe",
            (),
            {
                "returncode": 1,
                "stderr": b"first diagnostic\nactual namespace failure\n",
            },
        )(),
    )

    with pytest.raises(
        pytest.skip.Exception,
        match="bubblewrap user-namespace probe failed: actual namespace failure",
    ):
        live._sandbox_wrapper(settings)


def test_live_wrapper_argv_for_scenarios_does_not_unshare_network(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = RuntimeSettings(
        tooling_root=REPO,
        state_root=str(tmp_path / "state"),
    )
    monkeypatch.setattr(live.shutil, "which", lambda _name: "/usr/bin/bwrap")
    monkeypatch.setattr(
        live.subprocess,
        "run",
        lambda *_args, **_kwargs: type("Probe", (), {"returncode": 0})(),
    )
    wrapper = live._sandbox_wrapper(settings)
    private_tmpdir = tmp_path / "private-tmp"
    private_tmpdir.mkdir()
    argv = wrapper(live.LaunchSpec(
        argv=("/usr/bin/true",),
        cwd=tmp_path,
        private_mounts=(),
        private_tmpdir=private_tmpdir,
    ))

    assert "--unshare-net" not in argv
    assert "--unshare-user" in argv
    assert "--cap-drop" in argv


def test_live_wrapper_models_uv_alias_and_binds_resolved_interpreter_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    checkout = tmp_path / "checkout"
    venv_bin = checkout / ".venv" / "bin"
    uv_root = tmp_path / "uv"
    alias_root = uv_root / "cpython-3.13-linux-x86_64-gnu"
    install_root = uv_root / "cpython-3.13.14-linux-x86_64-gnu"
    install_bin = install_root / "bin"
    venv_bin.mkdir(parents=True)
    install_bin.mkdir(parents=True)
    resolved_python = install_bin / "python3.13"
    resolved_python.write_bytes(b"python")
    alias_root.symlink_to(install_root.name, target_is_directory=True)
    (venv_bin / "python").symlink_to(alias_root / "bin" / "python3.13")
    (venv_bin / "python3").symlink_to("python")
    settings = RuntimeSettings(
        tooling_root=checkout,
        toolchain_interpreter=venv_bin / "python",
        state_root=str(tmp_path / "state"),
    )
    monkeypatch.setattr(live.shutil, "which", lambda _name: "/usr/bin/bwrap")
    monkeypatch.setattr(
        live.subprocess,
        "run",
        lambda *_args, **_kwargs: type("Probe", (), {"returncode": 0})(),
    )

    wrapper = live._sandbox_wrapper(settings)
    private_tmpdir = tmp_path / "private-tmp"
    private_tmpdir.mkdir()
    argv = wrapper(live.LaunchSpec(
        argv=(str(venv_bin / "python"),),
        cwd=tmp_path,
        private_mounts=(),
        private_tmpdir=private_tmpdir,
    ))
    bound_roots = {
        Path(argv[index + 2])
        for index, value in enumerate(argv[:-2])
        if value == "--ro-bind"
    }
    modeled_links = {
        Path(argv[index + 2]): argv[index + 1]
        for index, value in enumerate(argv[:-2])
        if value == "--symlink"
    }

    assert (checkout / ".venv") in bound_roots
    assert install_root in bound_roots
    assert modeled_links[alias_root] == install_root.name
    assert argv.index(str(alias_root)) < argv.index(str(install_root))

    # Walk the namespace described by argv: links beneath a bind come from the
    # host tree, while every link crossed outside a bind must be explicit.
    remaining = list((venv_bin / "python").parts[1:])
    current = Path("/")
    while remaining:
        current /= remaining.pop(0)
        containing_root = next(
            (
                root
                for root in bound_roots
                if current == root or current.is_relative_to(root)
            ),
            None,
        )
        target = (
            os.readlink(current)
            if containing_root is not None and current.is_symlink()
            else modeled_links.get(current)
        )
        if target is None:
            continue
        target_path = Path(target)
        current = Path("/") if target_path.is_absolute() else current.parent
        target_parts = (
            target_path.parts[1:] if target_path.is_absolute() else target_path.parts
        )
        remaining[0:0] = target_parts
    assert current == resolved_python


def test_live_wrapper_rejects_modeled_interpreter_symlink_escape(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    checkout = tmp_path / "checkout"
    venv_bin = checkout / ".venv" / "bin"
    uv_root = tmp_path / "uv"
    alias_root = uv_root / "cpython-3.13-linux-x86_64-gnu"
    install_root = uv_root / "cpython-3.13.14-linux-x86_64-gnu"
    install_bin = install_root / "bin"
    outside = tmp_path / "outside"
    venv_bin.mkdir(parents=True)
    install_bin.mkdir(parents=True)
    outside.mkdir()
    resolved_python = install_bin / "python3.13"
    resolved_python.write_bytes(b"python")
    alias_root.symlink_to(install_root.name, target_is_directory=True)
    (venv_bin / "python").symlink_to(alias_root / "bin" / "python3.13")
    settings = RuntimeSettings(
        tooling_root=checkout,
        toolchain_interpreter=venv_bin / "python",
        state_root=str(tmp_path / "state"),
    )
    monkeypatch.setattr(live.shutil, "which", lambda _name: "/usr/bin/bwrap")
    monkeypatch.setattr(
        live.subprocess,
        "run",
        lambda *_args, **_kwargs: type("Probe", (), {"returncode": 0})(),
    )
    real_readlink = live.os.readlink
    alias_reads = 0

    def retarget_alias_after_resolution(path: os.PathLike[str] | str) -> str:
        nonlocal alias_reads
        if Path(path) != alias_root:
            return real_readlink(path)
        alias_reads += 1
        return real_readlink(path) if alias_reads == 1 else str(outside)

    monkeypatch.setattr(
        live.os,
        "readlink",
        retarget_alias_after_resolution,
    )

    with pytest.raises(RuntimeError, match="escapes read-only roots"):
        live._sandbox_wrapper(settings)


@pytest.mark.parametrize(
    "vendor,expected",
    [("claude", 0.25), ("codex", 0.065536), ("cursor", 0.1)],
)
def test_killed_run_vendor_cap_charge(
    vendor: str, expected: float
) -> None:
    budget = RuntimeBudget(max_tokens=32_768, max_turns=2, max_usd=0.25)
    assert live._vendor_cap_charge(vendor, budget) == expected


@pytest.mark.parametrize("scenario", sorted(live.KILLED_SCENARIOS))
def test_missing_killed_usage_is_known_or_conservatively_bounded(
    scenario: str,
) -> None:
    budget = RuntimeBudget(max_tokens=32_768, max_turns=2, max_usd=0.25)
    charge, accounting = live._accounted_cost("claude", scenario, None, budget)
    assert (charge, accounting) == (0.25, "conservative-vendor-cap")

    charge, accounting = live._accounted_cost("cursor", scenario, None, budget)
    assert (charge, accounting) == (0.1, "fixed-conservative-killed-charge")
    charge, accounting = live._accounted_cost("cursor", scenario, 0.001, budget)
    assert (charge, accounting) == (0.1, "fixed-conservative-killed-charge")


@pytest.mark.parametrize(
    "vendor,expected,expected_accounting",
    [
        ("claude", 0.25, "conservative-vendor-cap"),
        ("codex", 0.065536, "conservative-vendor-cap"),
        ("cursor", 0.1, "fixed-conservative-charge"),
    ],
)
def test_non_killed_exception_without_usage_increases_aggregate(
    vendor: str, expected: float, expected_accounting: str
) -> None:
    budget = RuntimeBudget(max_tokens=32_768, max_turns=2, max_usd=0.25)
    aggregate = 0.125

    charge, accounting, unaccounted = live._account_invocations(
        vendor, "permission-denial", [None], budget
    )
    aggregate += charge

    assert aggregate == 0.125 + expected
    assert accounting == expected_accounting
    assert unaccounted == 1


def test_codex_killed_charge_includes_conservative_prompt_input() -> None:
    budget = RuntimeBudget(max_tokens=32_768, max_turns=2, max_usd=0.25)
    assert live._vendor_cap_charge("codex", budget, prompt_bytes=100) == 0.065561


def test_remaining_allowance_narrows_next_vendor_cap() -> None:
    budget = live._remaining_budget("claude", 0.1)
    assert budget.max_usd == 0.1
    assert budget.max_tokens == live.LIVE_MAX_OUTPUT_TOKENS

    codex_budget = live._remaining_budget("codex", 0.01)
    assert live._vendor_cap_charge("codex", codex_budget) <= 0.01


def test_live_version_probe_uses_nested_wrapper(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    executable = tmp_path / "codex"
    executable.touch(mode=0o700)
    state_root = tmp_path / "state"
    settings = RuntimeSettings(state_root=str(state_root))
    wrapper = lambda spec: spec.argv
    observed: dict[str, object] = {}

    def fake_run(command, **kwargs):
        observed["command"] = command
        observed.update(kwargs)
        return live.ProcessResult(
            returncode=0,
            stdout="codex-cli 0.154.0\n",
            stderr="",
            duration_s=0.01,
            timed_out=False,
        )

    monkeypatch.setattr(live, "run_cli", fake_run)

    assert live._version(
        executable, "codex", settings=settings, wrapper=wrapper
    ) == "0.154.0"
    assert observed["sandbox_wrapper"] is wrapper
    assert observed["command"] == [str(executable), "--version"]


def test_live_process_lends_codex_credential_only_to_sdk_bridge(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    credential = tmp_path / "credential.json"
    credential.write_bytes(_codex_access_only())
    credential.chmod(0o600)
    descriptor = os.open(credential, os.O_RDONLY)
    observed: list[tuple[list[str], tuple[int, ...]]] = []

    def fake_run(command, **kwargs):
        observed.append((list(command), kwargs["pass_fds"]))
        return live.ProcessResult(
            returncode=0,
            stdout="",
            stderr="",
            duration_s=0.01,
            timed_out=False,
        )

    monkeypatch.setattr(live, "run_cli", fake_run)
    runner = live.RealProcess(lambda spec: spec.argv, "codex", "success")
    environment = {"LOOPZERO_CODEX_AUTH_FD": str(descriptor)}
    try:
        runner(
            ["/opt/runtime/codex", "--version"],
            cwd=tmp_path,
            input_text="",
            timeout_s=1,
            env=environment,
        )
        runner(
            [sys.executable, "-I", str(RuntimeSettings().bridge_path)],
            cwd=tmp_path,
            input_text="",
            timeout_s=1,
            env=environment,
        )
    finally:
        os.close(descriptor)

    assert observed == [
        (["/opt/runtime/codex", "--version"], ()),
        (
            [sys.executable, "-I", str(RuntimeSettings().bridge_path)],
            (descriptor,),
        ),
    ]


def test_live_access_only_source_is_reused_without_copy_or_modification(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "source.json"
    original = _codex_access_only()
    source.write_bytes(original)
    source.chmod(0o400)
    state_root = tmp_path / "state"
    settings = RuntimeSettings(state_root=str(state_root))
    monkeypatch.setenv("LOOPZERO_LIVE_CREDENTIAL_PATH", str(source))
    observed: dict[str, Path] = {}

    @contextmanager
    def broker(**kwargs):
        credential_path = kwargs["credential_path"]
        observed["path"] = credential_path
        fd = os.open(credential_path, os.O_RDONLY)
        try:
            yield fd
        finally:
            os.close(fd)

    monkeypatch.setattr(live.codex, "codex_subscription_credential", broker)

    with live._credential("codex", 5, settings, lambda spec: spec.argv):
        pass

    assert source.read_bytes() == original
    assert observed["path"] == source


def test_live_preflight_seals_once_and_scenarios_use_access_only_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tmp_path.chmod(0o700)
    settings = RuntimeSettings(state_root=str(tmp_path / "state"))
    monkeypatch.delenv("LOOPZERO_LIVE_CREDENTIAL_PATH", raising=False)
    seal_binary = tmp_path / "loopzero-credential-seal"
    seal_binary.touch(mode=0o700)
    monkeypatch.setattr(live, "_seal_command", lambda: seal_binary)
    seal_calls: list[list[str]] = []
    scenario_sources: list[Path] = []

    def seal(command, **_kwargs):
        seal_calls.append(command)
        output = Path(command[command.index("--out") + 1])
        output.write_bytes(_codex_access_only())
        output.chmod(0o600)
        return type("Completed", (), {"returncode": 0})()

    monkeypatch.setattr(live.subprocess, "run", seal)

    @contextmanager
    def broker(**kwargs):
        assert "sandbox_wrapper" not in kwargs
        credential_path = kwargs["credential_path"]
        scenario_sources.append(credential_path)
        assert credential_path.read_bytes() == _codex_access_only()
        descriptor = os.open(credential_path, os.O_RDONLY)
        try:
            yield descriptor
        finally:
            os.close(descriptor)

    monkeypatch.setattr(live.codex, "codex_subscription_credential", broker)

    with live._suite_access_only_credential("codex", tmp_path) as sealed:
        assert sealed.name == "access-only.json"
        for _scenario in ("success", "disconnect"):
            with live._credential("codex", 5, settings, lambda spec: spec.argv):
                pass

    assert len(seal_calls) == 1
    assert scenario_sources == [sealed, sealed]
    assert not sealed.exists()
    assert "LOOPZERO_LIVE_CREDENTIAL_PATH" not in os.environ


def test_live_failed_seal_stops_before_readiness_or_scenario_broker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tmp_path.chmod(0o700)
    monkeypatch.delenv("LOOPZERO_LIVE_CREDENTIAL_PATH", raising=False)
    seal_binary = tmp_path / "loopzero-credential-seal"
    seal_binary.touch(mode=0o700)
    monkeypatch.setattr(live, "_seal_command", lambda: seal_binary)
    monkeypatch.setattr(
        live.subprocess,
        "run",
        lambda *_args, **_kwargs: type("Completed", (), {"returncode": 1})(),
    )

    broker_calls: list[dict[str, object]] = []

    @contextmanager
    def broker(**kwargs):
        broker_calls.append(kwargs)
        yield -1

    monkeypatch.setattr(live.claude, "claude_subscription_credential", broker)

    with pytest.raises(RuntimeError, match="live credential sealing failed"):
        with live._suite_access_only_credential("claude", tmp_path):
            pytest.fail("failed sealing must not enter live readiness")

    assert broker_calls == []
    assert "LOOPZERO_LIVE_CREDENTIAL_PATH" not in os.environ
    assert not (tmp_path / "access-only.json").exists()


def test_live_adapter_run_transfers_a_duplicate_not_the_broker_descriptor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    credential = tmp_path / "credential.json"
    credential.write_bytes(_codex_access_only())
    credential.chmod(0o600)
    monkeypatch.setenv("LOOPZERO_LIVE_CREDENTIAL_PATH", str(credential))
    settings = RuntimeSettings(state_root=str(tmp_path / "state"))
    broker_descriptor: int | None = None

    @contextmanager
    def broker(**_kwargs):
        nonlocal broker_descriptor
        broker_descriptor = os.open(credential, os.O_RDONLY)
        try:
            yield broker_descriptor
        finally:
            os.close(broker_descriptor)

    class ConsumingAdapter:
        def run(self, request):
            assert broker_descriptor is not None
            child_descriptor = int(
                os.environ[settings.env_name("CODEX_AUTH_FD")]
            )
            assert child_descriptor != broker_descriptor
            os.fstat(broker_descriptor)
            return live.run_cli(
                [sys.executable, "-c", "pass"],
                cwd=request.cwd,
                input_text="",
                timeout_s=2,
                env={
                    "PATH": os.environ["PATH"],
                    settings.env_name("CODEX_AUTH_FD"): str(child_descriptor),
                },
                pass_fds=(child_descriptor,),
                sandbox_wrapper=lambda spec: spec.argv,
            )

    monkeypatch.setattr(live.codex, "codex_subscription_credential", broker)

    # Lending the broker's descriptor itself reproduces the original cleanup
    # failure after a launch path consumes it.
    with settings.use(), pytest.raises(OSError, match="Bad file descriptor"):
        with broker() as descriptor:
            live.run_cli(
                [sys.executable, "-c", "pass"],
                cwd=tmp_path,
                input_text="",
                timeout_s=2,
                env={
                    "PATH": os.environ["PATH"],
                    settings.env_name("CODEX_AUTH_FD"): str(descriptor),
                },
                pass_fds=(descriptor,),
                sandbox_wrapper=lambda spec: spec.argv,
            )

    request = live.RuntimeRequest(
        vendor="fake",
        transport=RUNTIME_REGISTRY.registration("fake").preferred_transport,
        requested_model="test-model",
        effort="low",
        prompt="test",
        cwd=tmp_path,
        timeout_s=1,
        read_only=True,
        attempt_id="fd-ownership",
        eligibility=SubscriptionEligibility.APPROVED,
        output_schema={"type": "object"},
        capability_profile=RuntimeCapabilityProfile(read_roots=(tmp_path,)),
    )

    result = live._run_with_credential(
        "codex",
        ConsumingAdapter(),  # type: ignore[arg-type]
        request,
        timeout_s=5,
        settings=settings,
        wrapper=lambda spec: spec.argv,
    )

    assert result.returncode == 0
    assert broker_descriptor is not None
    with pytest.raises(OSError):
        os.fstat(broker_descriptor)
    assert settings.env_name("CODEX_AUTH_FD") not in os.environ


@pytest.mark.parametrize("scenario", tuple(Scenario))
def test_live_credential_launch_path_runs_all_registry_fake_scenarios(
    scenario: Scenario, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    credential = tmp_path / "credential.json"
    credential.write_bytes(_codex_access_only())
    credential.chmod(0o600)
    monkeypatch.setenv("LOOPZERO_LIVE_CREDENTIAL_PATH", str(credential))
    settings = RuntimeSettings(state_root=str(tmp_path / "state"))

    @contextmanager
    def broker(**_kwargs):
        descriptor = os.open(credential, os.O_RDONLY)
        try:
            yield descriptor
        finally:
            os.close(descriptor)

    monkeypatch.setattr(live.codex, "codex_subscription_credential", broker)
    adapter = RUNTIME_REGISTRY.create("fake", scenario=scenario)
    request = live.RuntimeRequest(
        vendor="fake",
        transport=RUNTIME_REGISTRY.registration("fake").preferred_transport,
        requested_model="test-model",
        effort="low",
        prompt="test",
        cwd=tmp_path,
        timeout_s=1,
        read_only=True,
        attempt_id=f"fake-{scenario.value}",
        eligibility=SubscriptionEligibility.APPROVED,
        output_schema={"type": "object"},
        capability_profile=RuntimeCapabilityProfile(read_roots=(tmp_path,)),
    )

    result = live._run_with_credential(
        "codex",
        adapter,
        request,
        timeout_s=5,
        settings=settings,
        wrapper=lambda spec: spec.argv,
    )

    assert result.status is not None
    assert settings.env_name("CODEX_AUTH_FD") not in os.environ


def _permission_result(*, events=(), diagnostics=()) -> RuntimeResult:
    return RuntimeResult(
        vendor="codex",
        transport="codex/app-server",
        requested_model="gpt-5.6-luna",
        status=RuntimeStatus.FAILED,
        terminal_reason=TerminalReason.PROCESS_EXIT,
        attempt_id="permission-test",
        cost_status=RuntimeCostStatus.UNKNOWN,
        eligibility=SubscriptionEligibility.APPROVED,
        events=events,
        diagnostics=diagnostics,
    )


def test_live_result_json_includes_sdk_fallback_readiness_diagnostics(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    result = _permission_result(diagnostics=("safe CLI diagnostic",))
    result = live.replace(
        result,
        transport="codex/cli",
        transport_attempts=(
            RuntimeTransportAttempt(
                transport="codex/app-server",
                requested_model="gpt-5.6-luna",
                phase="readiness",
                status="subscription-unavailable",
                terminal_reason="subscription-unavailable",
                failure_class="sdk-version-mismatch",
                duration_s=None,
                semantic=False,
                selected_next=True,
            ),
        ),
    )

    normalized = live._normalized(result)
    output = tmp_path / "result.json"
    monkeypatch.setenv("LOOPZERO_LIVE_RESULTS", str(output))
    live._write_results(
        "codex",
        "0.154.0",
        [{"scenario": "success", "result": normalized}],
        0.0,
        killed_runs=0,
        max_killed_runs=3,
        unaccounted_runs=0,
        max_unaccounted_runs=3,
    )
    serialized = json.loads(output.read_text())["scenarios"][0]["result"]

    assert serialized["diagnostics"] == ["safe CLI diagnostic"]
    assert serialized["transport_attempts"] == [
        {
            "transport": "codex/app-server",
            "requested_model": "gpt-5.6-luna",
            "phase": "readiness",
            "status": "subscription-unavailable",
            "terminal_reason": "subscription-unavailable",
            "failure_class": "sdk-version-mismatch",
            "duration_s": None,
            "semantic": False,
            "selected_next": True,
        }
    ]


def test_permission_denial_requires_an_explicit_denial_signal() -> None:
    ordinary_tool = _permission_result(
        events=(RuntimeEvent(kind="command", subtype="completed"),)
    )
    explicit_event = _permission_result(
        events=(RuntimeEvent(kind="permission", subtype="denied"),)
    )
    explicit_counter = _permission_result(
        diagnostics=("Claude permission denials: 1: Read",)
    )

    assert live._permission_denial_evident(ordinary_tool) is False
    assert live._permission_denial_evident(explicit_event) is True
    assert live._permission_denial_evident(explicit_counter) is True


def test_cursor_schema_scenarios_are_unsupported_not_free_text_passes() -> None:
    assert live._unsupported_reason("cursor", "success") is not None
    assert live._unsupported_reason("cursor", "restart-resume") is not None
    assert live._unsupported_reason("cursor", "permission-denial") is None


def test_codex_permission_denial_is_classified_only_after_launch() -> None:
    assert live._unsupported_reason("codex", "permission-denial") is None
    result = _permission_result(diagnostics=(
        "Codex command completed: status=completed; exit_code=1; observed_output_bytes=42",
    ))
    assert live._post_launch_unsupported_reason("permission-denial", result)
    assert live._observed_tool_outcomes(result) == list(result.diagnostics)
    assert live._observed_tool_outcomes(_permission_result()) == [
        "no command completion observed"
    ]
    with pytest.raises(AssertionError):
        live._assert_contract("permission-denial", result)
    denial = _permission_result(events=(
        RuntimeEvent(kind="tool", subtype="denied:/etc/shadow"),
    ))
    assert live._post_launch_unsupported_reason("permission-denial", denial) is None
    live._assert_contract("permission-denial", denial)


@pytest.mark.parametrize("reason", [TerminalReason.PROTOCOL_FAILURE,
                                   TerminalReason.TRANSPORT_DISCONNECT,
                                   TerminalReason.STARTUP_FAILURE])
def test_codex_permission_exception_does_not_waive_runtime_failure(reason) -> None:
    from dataclasses import replace

    result = replace(_permission_result(), terminal_reason=reason)
    assert live._post_launch_unsupported_reason("permission-denial", result) is None
    with pytest.raises(AssertionError):
        live._assert_contract("permission-denial", result)


def test_suite_stops_at_default_killed_run_limit() -> None:
    assert live.DEFAULT_MAX_KILLED_RUNS == 3
    assert live._suite_stop_reason(
        charged_cost=0.2,
        ceiling=1.0,
        killed_runs=3,
        max_killed_runs=3,
        unaccounted_runs=0,
        max_unaccounted_runs=3,
    ) == "aborted-killed-limit"


def test_suite_stops_at_default_unaccounted_run_limit() -> None:
    assert live.DEFAULT_MAX_UNACCOUNTED_RUNS == 3
    assert live._suite_stop_reason(
        charged_cost=0.2,
        ceiling=1.0,
        killed_runs=0,
        max_killed_runs=3,
        unaccounted_runs=3,
        max_unaccounted_runs=3,
    ) == "aborted-unaccounted-limit"


def test_workflow_seals_with_release_wheel_and_deletes_source_before_bwrap() -> None:
    workflow = (REPO / ".github/workflows/nightly-conformance.yml").read_text(
        encoding="utf-8"
    )
    seal_step = workflow.index("- name: Seal access-only credential on the host")
    validation_step = workflow.index("- name: Run live scenarios in the validation bubble")
    source_delete = workflow.index('\n          rm -f -- "$SOURCE"\n', seal_step)
    bwrap = workflow.index("bwrap --unshare-user", validation_step)
    validation_body = workflow[validation_step:]
    bwrap_command = workflow[bwrap:workflow.index("--chdir", bwrap)]

    assert "refs/remotes/origin/release/0.3" in workflow
    assert "$RUNNER_TEMP/loopzero-live/broker-venv" in workflow
    assert seal_step < source_delete < validation_step < bwrap
    assert '--ro-bind "$SNAPSHOT" /run/loopzero-credential.json' in validation_body
    assert '--ro-bind "$SOURCE"' not in validation_body
    assert "LIVE_CREDENTIAL:" not in validation_body
    assert "--unshare-net" not in bwrap_command
    assert "--diagnose" in validation_body
    assert validation_body.index("--diagnose") < validation_body.index(
        "--basetemp=/tmp/pytest tests/conformance/live/live_conformance.py"
    )


def test_live_name_resolution_binds_resolver_symlinked_into_run(tmp_path: Path) -> None:
    etc = tmp_path / "etc"
    run = tmp_path / "run"
    etc.mkdir()
    (run / "systemd" / "resolve").mkdir(parents=True)
    target = run / "systemd" / "resolve" / "stub-resolv.conf"
    target.write_text("nameserver 127.0.0.53\n", encoding="utf-8")
    (etc / "resolv.conf").symlink_to(target)

    assert live._name_resolution_binds(etc, run) == (target,)

    (etc / "resolv.conf").unlink()
    (etc / "resolv.conf").write_text("nameserver 1.1.1.1\n", encoding="utf-8")
    assert live._name_resolution_binds(etc, run) == ()

    (etc / "resolv.conf").unlink()
    (etc / "resolv.conf").symlink_to(tmp_path / "elsewhere.conf")
    assert live._name_resolution_binds(etc, run) == ()


def test_live_wrapper_binds_resolver_target_after_private_run_tmpfs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = RuntimeSettings(
        tooling_root=REPO,
        state_root=str(tmp_path / "state"),
    )
    monkeypatch.setattr(live.shutil, "which", lambda _name: "/usr/bin/bwrap")
    monkeypatch.setattr(
        live.subprocess,
        "run",
        lambda *_args, **_kwargs: type("Probe", (), {"returncode": 0})(),
    )
    resolver = Path("/run/systemd/resolve/stub-resolv.conf")
    monkeypatch.setattr(live, "_name_resolution_binds", lambda: (resolver,))
    wrapper = live._sandbox_wrapper(settings)
    private_tmpdir = tmp_path / "private-tmp"
    private_tmpdir.mkdir()
    argv = wrapper(live.LaunchSpec(
        argv=("/usr/bin/true",),
        cwd=tmp_path,
        private_mounts=(),
        private_tmpdir=private_tmpdir,
    ))

    tmpfs_run = argv.index("/run", argv.index("--tmpfs"))
    bind = argv.index(str(resolver))
    assert argv[bind - 1] == "--ro-bind"
    assert argv[bind + 1] == str(resolver)
    assert bind > tmpfs_run
    assert "--unshare-net" not in argv


@pytest.mark.parametrize("denied,tool_observed", [(True, True), (False, True), (False, False)])
def test_codex_permission_scenario_launches_and_records_evidence_and_cost(
    monkeypatch, tmp_path, denied, tool_observed
) -> None:
    from contextlib import nullcontext
    from dataclasses import replace
    from types import SimpleNamespace

    executable = tmp_path / "codex"
    executable.touch()
    output = tmp_path / "results.json"
    monkeypatch.setenv("LOOPZERO_LIVE_CLI_PATH", str(executable))
    monkeypatch.setenv("LOOPZERO_LIVE_RESULTS", str(output))
    monkeypatch.setattr(live, "SCENARIOS", ("permission-denial",))
    monkeypatch.setattr(live, "_suite_access_only_credential", lambda *_: nullcontext())
    monkeypatch.setattr(live, "private_temporary_directory", lambda *_: nullcontext(tmp_path))
    monkeypatch.setattr(live, "_sandbox_wrapper", lambda *_: None)
    monkeypatch.setattr(live, "_version", lambda *_args, **_kwargs: live.PINS["codex"])
    monkeypatch.setattr(live, "_probe_with_credential", lambda *_args, **_kwargs: SimpleNamespace(ready=True))
    monkeypatch.setattr(live, "_normalized_readiness", lambda *_: {"ready": True})
    monkeypatch.setattr(live, "RealProcess", lambda _wrapper, _vendor, scenario: SimpleNamespace(
        scenario_started=scenario != "diagnose", scenario_invocations=1,
        killed=False, metered_cost=None,
    ))
    monkeypatch.setattr(live.RUNTIME_REGISTRY, "create", lambda *_args, **_kwargs: SimpleNamespace(cancel=lambda: None))
    launches = []
    result = replace(_permission_result(
        events=(RuntimeEvent(kind="tool", subtype="denied:/etc/shadow"),) if denied else (),
        diagnostics=("Codex command completed: status=completed; exit_code=1; observed_output_bytes=42",) if tool_observed else (),
    ), cost_usd=0.01)

    def run(_vendor, _adapter, request, **_kwargs):
        launches.append(request)
        return result

    monkeypatch.setattr(live, "_run_with_credential", run)
    live.test_live_runtime_contract("codex", tmp_path, SimpleNamespace(getoption=lambda _: False))
    assert len(launches) == 1
    assert "/etc/shadow" in launches[0].prompt
    record = json.loads(output.read_text())["scenarios"][0]
    assert record["outcome"] == ("passed" if denied else "unsupported")
    assert record["accounting"] == "known"
    assert record["charged_cost_usd"] == 0.01
    assert record["known_cost_usd"] == 0.01
    assert record["observed_tool_outcomes"] == (
        list(result.diagnostics) if tool_observed else ["no command completion observed"]
    )
    if not denied:
        assert "no observable denial" in record["reason"]
        assert record["status"] == "unsupported"
