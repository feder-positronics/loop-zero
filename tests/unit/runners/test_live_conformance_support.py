"""Deterministic tests for the opt-in live harness mechanics."""

from __future__ import annotations

from contextlib import contextmanager
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
    SubscriptionEligibility,
    TerminalReason,
)
from loopzero.runners.fake import Scenario
from loopzero.runners.registry import RUNTIME_REGISTRY
from tests.conformance.live import live_conformance as live


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


def test_live_access_only_source_is_reused_without_copy_or_modification(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "source.json"
    original = b'{"tokens":{"access_token":"access","refresh_token":"access"}}'
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


def test_live_local_run_uses_normal_host_credential_discovery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = RuntimeSettings(state_root=str(tmp_path / "state"))
    monkeypatch.delenv("LOOPZERO_LIVE_CREDENTIAL_PATH", raising=False)
    observed: dict[str, object] = {}

    @contextmanager
    def broker(**kwargs):
        observed.update(kwargs)
        descriptor_path = tmp_path / "access-only.json"
        descriptor_path.write_text("{}x", encoding="utf-8")
        descriptor = os.open(descriptor_path, os.O_RDONLY)
        try:
            yield descriptor
        finally:
            os.close(descriptor)

    monkeypatch.setattr(live.codex, "codex_subscription_credential", broker)

    with live._credential("codex", 5, settings, lambda spec: spec.argv):
        pass

    assert "credential_path" not in observed


def test_live_adapter_run_transfers_a_duplicate_not_the_broker_descriptor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    credential = tmp_path / "credential.json"
    credential.write_text("{}x", encoding="utf-8")
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
    credential.write_text("{}x", encoding="utf-8")
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
    assert live._cursor_unsupported_reason("cursor", "success") is not None
    assert live._cursor_unsupported_reason("cursor", "restart-resume") is not None
    assert live._cursor_unsupported_reason("cursor", "permission-denial") is None


def test_suite_stops_at_default_killed_run_limit() -> None:
    assert live.DEFAULT_MAX_KILLED_RUNS == 3
    assert live._suite_stop_reason(
        charged_cost=0.2,
        ceiling=1.0,
        killed_runs=3,
        max_killed_runs=3,
    ) == "aborted-killed-limit"


def test_workflow_seals_with_release_wheel_and_deletes_source_before_bwrap() -> None:
    workflow = (REPO / ".github/workflows/nightly-conformance.yml").read_text(
        encoding="utf-8"
    )
    seal_step = workflow.index("- name: Seal access-only credential on the host")
    validation_step = workflow.index("- name: Run live scenarios in the validation bubble")
    source_delete = workflow.index('\n          rm -f -- "$SOURCE"\n', seal_step)
    bwrap = workflow.index("bwrap --unshare-user", validation_step)
    validation_body = workflow[validation_step:]

    assert "refs/remotes/origin/release/0.3" in workflow
    assert "$RUNNER_TEMP/loopzero-live/broker-venv" in workflow
    assert seal_step < source_delete < validation_step < bwrap
    assert '--ro-bind "$SNAPSHOT" /run/loopzero-credential.json' in validation_body
    assert '--ro-bind "$SOURCE"' not in validation_body
    assert "LIVE_CREDENTIAL:" not in validation_body
