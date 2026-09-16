"""Synthetic native credentials only: billing observations never contact vendors."""

import base64
import json
import os
import time
from dataclasses import replace
from pathlib import Path

import pytest

from loopzero.runners import bridge, claude, codex, contract, cursor
from loopzero.runners.settings import RuntimeSettings
from tests.conformance.live import live_conformance as live


def _native_snapshot(kind):
    token = (
        "header."
        + base64.urlsafe_b64encode(
            json.dumps({"exp": int(time.time()) + 864000}).encode()
        )
        .decode()
        .rstrip("=")
        + ".signature"
    )
    if kind == "claude-setup":
        return {
            "claudeCodeOauthToken": "sk-ant-oat01-" + "s" * 80,
            "source": "setup-token-file",
        }
    if kind == "claude-oauth":
        expiry = (int(time.time()) + 864000) * 1000
        return {
            "claudeAiOauth": {
                "accessToken": "synthetic-oauth-access",
                "refreshToken": "synthetic-oauth-access",
                "expiresAt": expiry,
                "refreshTokenExpiresAt": expiry,
                "scopes": sorted(claude.REQUIRED_SCOPES),
                "subscriptionType": "max",
            }
        }
    if kind == "codex":
        return {
            "auth_mode": "chatgpt",
            "OPENAI_API_KEY": None,
            "tokens": {
                "access_token": token,
                "refresh_token": token,
                "id_token": "opaque-id",
                "account_id": "synthetic-account",
            },
            "last_refresh": "2026-09-16T00:00:00Z",
        }
    return {"accessToken": token, "refreshToken": token}


def _context(tmp_path, monkeypatch, kind):
    vendor = kind.split("-", 1)[0]
    source_dir = tmp_path / "sealed"
    source_dir.mkdir(mode=0o700)
    source = source_dir / "snapshot.json"
    payload = _native_snapshot(kind)
    source.write_text(json.dumps(payload))
    source.chmod(0o600)
    monkeypatch.setenv("LOOPZERO_LIVE_CREDENTIAL_PATH", str(source))
    home = tmp_path / "state" / "session"
    home.parent.mkdir(mode=0o700)
    home.mkdir(mode=0o700)
    settings = RuntimeSettings(
        tooling_root=tmp_path / "checkout",
        state_root=str(home.parent),
        session_home=home,
    )
    request = live._request(vendor, "success", tmp_path, 5)
    return vendor, source, payload, settings, request


def _result(request):
    return contract.RuntimeResult(
        vendor=request.vendor,
        transport=request.transport,
        requested_model=request.requested_model,
        attempt_id=request.attempt_id,
        status=contract.RuntimeStatus.COMPLETED,
        terminal_reason=contract.TerminalReason.COMPLETED,
        cost_usd=0.01,
        cost_source=contract.RuntimeCostSource.VENDOR,
    )


@pytest.mark.parametrize("kind", ["claude-setup", "claude-oauth", "codex", "cursor"])
def test_actual_brokers_and_bridge_consumers_bind_subscription_snapshot(
    tmp_path, monkeypatch, kind
):
    vendor, source, payload, settings, request = _context(tmp_path, monkeypatch, kind)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "synthetic-wrong-api-key")
    monkeypatch.setenv("OPENAI_API_KEY", "synthetic-wrong-api-key")
    monkeypatch.setenv("CURSOR_API_KEY", "synthetic-wrong-api-key")
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "synthetic-wrong-oauth-token")
    monkeypatch.setenv("BILLING_MODE", "metered")

    # Already access-only inputs must never trigger renewal, fallback, or a token probe.
    def forbidden(*_args, **_kwargs):
        pytest.fail(
            "billing evidence attempted credential discovery or network refresh"
        )

    monkeypatch.setattr(claude, "_validate_remote_token", forbidden)
    monkeypatch.setattr(claude, "_refresh_credential", forbidden)
    monkeypatch.setattr(codex, "_refresh_credential", forbidden)

    class NativeConsumer:
        def run(self, actual_request):
            assert actual_request is request
            # Replace the source after the broker lent its snapshot. Consumers
            # must still use the held credential, never reopen this pathname.
            source.write_text('{"billing_mode":"metered"}')
            if vendor == "claude":
                environment = bridge._claude_subscription_environment()
                expected = (
                    payload.get("claudeCodeOauthToken")
                    or payload["claudeAiOauth"]["accessToken"]
                )
                assert environment["CLAUDE_CODE_OAUTH_TOKEN"] == expected
            elif vendor == "codex":
                with bridge._codex_subscription_environment() as (
                    auth_environment,
                    auth_path,
                ):
                    assert (
                        json.loads(auth_path.read_bytes())["tokens"]
                        == payload["tokens"]
                    )
                    environment = bridge._codex_sdk_environment(
                        auth_environment, auth_path
                    )
                    assert (
                        environment["HOME"]
                        == environment["CODEX_HOME"]
                        == str(auth_path.parent)
                    )
            else:
                with cursor._cursor_subscription_environment() as (
                    environment,
                    _mounts,
                ):
                    assert (
                        json.loads(
                            (
                                Path(environment["HOME"]) / ".config/cursor/auth.json"
                            ).read_bytes()
                        )
                        == payload
                    )
            assert (
                not {"ANTHROPIC_API_KEY", "OPENAI_API_KEY", "CURSOR_API_KEY"}
                & environment.keys()
            )
            return _result(request)

    with settings.use():
        result = live._run_with_credential(
            vendor,
            NativeConsumer(),
            request,
            timeout_s=5,
            settings=settings,
            wrapper=None,
        )
    assert result.billing_mode is contract.RuntimeBillingMode.SUBSCRIPTION
    normalized = live._normalized(result)
    assert normalized["billing_mode"] == "subscription"
    assert "synthetic" not in json.dumps(normalized)
    assert settings.env_name(f"{vendor.upper()}_AUTH_FD") not in os.environ


@pytest.mark.parametrize("claim", ["subscription", "metered", "unknown"])
def test_adapter_billing_claim_is_overridden_by_broker_evidence(
    tmp_path, monkeypatch, claim
):
    vendor, _source, _payload, settings, request = _context(
        tmp_path, monkeypatch, "codex"
    )

    class Adapter:
        def run(self, _request):
            return replace(
                _result(request), billing_mode=contract.RuntimeBillingMode(claim)
            )

    with settings.use():
        result = live._run_with_credential(
            vendor, Adapter(), request, timeout_s=5, settings=settings, wrapper=None
        )
    assert result.billing_mode is contract.RuntimeBillingMode.SUBSCRIPTION


def test_direct_result_defaults_unknown_and_cost_accounting_is_mode_independent(
    tmp_path,
):
    request = live._request("codex", "success", tmp_path, 5)
    original = _result(request)
    assert original.billing_mode is contract.RuntimeBillingMode.UNKNOWN
    budget = live._remaining_budget("codex", 1)
    for mode in contract.RuntimeBillingMode:
        result = replace(original, billing_mode=mode)
        assert live._normalized(result)["billing_mode"] == mode.value
        assert live._account_invocations(
            "codex",
            "success",
            [result.cost_usd],
            budget,
            cost_sources=[result.cost_source],
        ) == (0.01, "known", 0)


@pytest.mark.parametrize("change", ["vendor", "transport", "attempt"])
def test_nonmatching_result_route_cannot_acquire_subscription_label(
    tmp_path, monkeypatch, change
):
    vendor, _source, _payload, settings, request = _context(
        tmp_path, monkeypatch, "codex"
    )

    class Adapter:
        def run(self, _request):
            updates = (
                {"vendor": "fake"}
                if change == "vendor"
                else {"transport": "codex/cli"}
                if change == "transport"
                else {"attempt_id": "other-attempt"}
            )
            return replace(_result(request), **updates)

    with settings.use():
        result = live._run_with_credential(
            vendor, Adapter(), request, timeout_s=5, settings=settings, wrapper=None
        )
    assert result.billing_mode is contract.RuntimeBillingMode.UNKNOWN


@pytest.mark.parametrize("invalid", ["missing", "api-key", "refresh-capable"])
def test_invalid_snapshot_cannot_supply_billing_evidence(
    tmp_path, monkeypatch, invalid
):
    vendor, source, payload, settings, request = _context(
        tmp_path, monkeypatch, "codex"
    )
    if invalid == "missing":
        source.unlink()
    elif invalid == "api-key":
        payload["OPENAI_API_KEY"] = "synthetic-metered-key"
        source.write_text(json.dumps(payload))
    else:
        payload["tokens"]["refresh_token"] = "synthetic-refresh-capability"
        source.write_text(json.dumps(payload))

    class ForbiddenAdapter:
        def run(self, _request):
            pytest.fail("invalid credential reached adapter")

    with (
        settings.use(),
        pytest.raises((RuntimeError, live.credential_seal.CredentialSealError)),
    ):
        live._run_with_credential(
            vendor,
            ForbiddenAdapter(),
            request,
            timeout_s=5,
            settings=settings,
            wrapper=None,
        )


@pytest.mark.parametrize(
    "scenario,fail_second",
    [
        ("success", False),
        ("restart-resume", False),
        ("restart-resume", True),
        ("malformed-output", False),
    ],
)
def test_scenario_artifacts_preserve_each_invocation_mode(
    tmp_path, monkeypatch, scenario, fail_second
):
    from contextlib import nullcontext
    from types import SimpleNamespace

    executable = tmp_path / "codex"
    executable.touch()
    output = tmp_path / "result.json"
    monkeypatch.setenv("LOOPZERO_LIVE_CLI_PATH", str(executable))
    monkeypatch.setenv("LOOPZERO_LIVE_RESULTS", str(output))
    monkeypatch.setattr(live, "SCENARIOS", (scenario,))
    monkeypatch.setattr(live, "_suite_access_only_credential", lambda *_: nullcontext())
    monkeypatch.setattr(
        live, "private_temporary_directory", lambda *_: nullcontext(tmp_path)
    )
    monkeypatch.setattr(live, "_sandbox_wrapper", lambda *_: None)
    monkeypatch.setattr(live, "_version", lambda *_args, **_kwargs: live.PINS["codex"])
    monkeypatch.setattr(
        live,
        "_probe_with_credential",
        lambda *_args, **_kwargs: SimpleNamespace(ready=True),
    )
    monkeypatch.setattr(live, "_normalized_readiness", lambda *_: {"ready": True})
    runner = SimpleNamespace(
        scenario_started=False, scenario_invocations=0, killed=False, metered_cost=None
    )
    monkeypatch.setattr(live, "RealProcess", lambda *_: runner)
    monkeypatch.setattr(
        live.RUNTIME_REGISTRY,
        "create",
        lambda *_args, **_kwargs: SimpleNamespace(cancel=lambda: None),
    )

    def run(_vendor, _adapter, request, **_kwargs):
        runner.scenario_started = True
        runner.scenario_invocations += 1
        if fail_second and runner.scenario_invocations == 2:
            raise RuntimeError("synthetic terminal failure")
        return replace(
            _result(request),
            session_id="synthetic-session",
            structured_output={"ok": True},
            billing_mode=contract.RuntimeBillingMode.SUBSCRIPTION
            if runner.scenario_invocations == 1
            else contract.RuntimeBillingMode.UNKNOWN,
        )

    monkeypatch.setattr(live, "_run_with_credential", run)
    expected_failure = fail_second or scenario == "malformed-output"
    with pytest.raises(AssertionError) if expected_failure else nullcontext():
        live.test_live_runtime_contract(
            "codex", tmp_path, SimpleNamespace(getoption=lambda _: False)
        )
    record = json.loads(output.read_text())["scenarios"][0]
    assert record["billing_modes"] == (
        ["subscription", "unknown"]
        if scenario == "restart-resume"
        else ["subscription"]
    )
    assert record["outcome"] == (
        "failed" if fail_second or scenario == "malformed-output" else "passed"
    )
    assert record["charged_cost_usd"] >= 0.01
    if not fail_second:
        assert record["charged_cost_usd"] == (
            0.02 if scenario == "restart-resume" else 0.01
        )


def test_workflow_summary_displays_modes_and_unknown_for_older_artifacts(
    tmp_path, monkeypatch
):
    import sys

    import yaml
    from conftest import REPO

    workflow = yaml.safe_load(
        (REPO / ".github/workflows/nightly-conformance.yml").read_text()
    )
    step = next(
        step
        for step in workflow["jobs"]["runtime"]["steps"]
        if step.get("name") == "Add normalized summary"
    )
    script = step["run"].split("<<'PY'\n", 1)[1].rsplit("\nPY", 1)[0]
    result = tmp_path / "result.json"
    output = tmp_path / "summary.md"
    result.write_text(
        json.dumps(
            {
                "runtime": "codex",
                "version": "test",
                "scenarios": [
                    {
                        "scenario": "restart-resume",
                        "outcome": "passed",
                        "billing_modes": ["subscription", "unknown"],
                        "charged_cost_usd": 0.02,
                    },
                    {"scenario": "legacy", "outcome": "failed"},
                ],
                "charged_cost_usd": 0.02,
                "killed_runs": 0,
                "max_killed_runs": 3,
                "unaccounted_runs": 0,
                "max_unaccounted_runs": 3,
                "spend_bound": "bounded",
            }
        )
    )
    monkeypatch.setattr(sys, "argv", ["summary", str(result), str(output)])
    exec(compile(script, "nightly-summary", "exec"), {})
    summary = output.read_text()
    assert "Billing mode(s)" in summary
    assert "| restart-resume | passed | n/a | subscription, unknown |" in summary
    assert "| legacy | failed | n/a | unknown |" in summary
    assert "Total budget debit (API-equivalent USD): 0.020000" in summary
