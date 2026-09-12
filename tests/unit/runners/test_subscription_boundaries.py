"""Adversarial coverage for live subscription and session boundaries."""

from __future__ import annotations

from contextlib import contextmanager
import json
import os
from pathlib import Path
import stat
import sys

import pytest

from loopzero.runners import bridge, claude, codex, cursor, process
from loopzero.runners.contract import (
    RuntimeCapabilityProfile,
    RuntimeRequest,
    SubscriptionEligibility,
)
from loopzero.runners.settings import RuntimeBudget, RuntimeSettings


def _descriptor(path: Path, payload: object) -> int:
    path.write_text(json.dumps(payload), encoding="utf-8")
    path.chmod(0o600)
    return os.open(path, os.O_RDONLY)


def _access_only_claude() -> dict[str, object]:
    return {
        "claudeAiOauth": {
            "accessToken": "access-only",
            "refreshToken": "access-only",
            "expiresAt": 2_000_000,
            "refreshTokenExpiresAt": 2_000_000,
            "scopes": sorted(claude.REQUIRED_SCOPES),
            "subscriptionType": "max",
        }
    }


def _request(tmp_path: Path, vendor: str) -> RuntimeRequest:
    return RuntimeRequest(
        vendor=vendor,
        transport={
            "claude": claude.CLAUDE_SDK_TRANSPORT,
            "codex": codex.CODEX_SDK_TRANSPORT,
            "cursor": cursor.CursorAdapter.transport,
        }[vendor],
        requested_model="test-model",
        effort="low",
        prompt="test",
        cwd=tmp_path,
        timeout_s=5,
        read_only=True,
        attempt_id="test",
        eligibility=SubscriptionEligibility.APPROVED,
        output_schema={"type": "object"},
        capability_profile=RuntimeCapabilityProfile(read_roots=(tmp_path,)),
    )


def _outcome(stdout: str, *, returncode: int = 0) -> process.ProcessResult:
    return process.ProcessResult(
        returncode=returncode,
        stdout=stdout,
        stderr="",
        duration_s=0.01,
        timed_out=False,
    )


def test_claude_subscription_environment_consumes_fd_and_access_only_shape(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fd = _descriptor(tmp_path / "claude.json", _access_only_claude())
    descriptor_name = bridge.get_settings().env_name("CLAUDE_AUTH_FD")
    monkeypatch.setenv(descriptor_name, str(fd))

    environment = bridge._claude_subscription_environment()

    assert environment["CLAUDE_CODE_OAUTH_TOKEN"] == "access-only"
    assert descriptor_name not in environment
    assert descriptor_name not in os.environ
    with pytest.raises(OSError):
        os.fstat(fd)


def test_claude_subscription_environment_rejects_refresh_capability_and_closes_fd(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    payload = _access_only_claude()
    payload["claudeAiOauth"]["refreshToken"] = "refresh-capability"  # type: ignore[index]
    fd = _descriptor(tmp_path / "claude.json", payload)
    monkeypatch.setenv(bridge.get_settings().env_name("CLAUDE_AUTH_FD"), str(fd))

    with pytest.raises(bridge.BridgeInputError):
        bridge._claude_subscription_environment()
    with pytest.raises(OSError):
        os.fstat(fd)


@pytest.mark.parametrize("valid", [True, False])
def test_protected_claude_credential_ready_checks_access_only_shape(
    tmp_path: Path, valid: bool
) -> None:
    payload = _access_only_claude()
    if not valid:
        payload["claudeAiOauth"]["refreshTokenExpiresAt"] = 3_000_000  # type: ignore[index]
    fd = _descriptor(tmp_path / "claude.json", payload)
    try:
        assert claude.protected_claude_credential_ready(fd) is valid
    finally:
        os.close(fd)


def test_cursor_subscription_environment_is_private_and_cleans_on_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state_root = tmp_path / "state"
    settings = RuntimeSettings(state_root=str(state_root))
    fd = _descriptor(
        tmp_path / "cursor.json",
        {"accessToken": "access-only", "refreshToken": "access-only"},
    )
    monkeypatch.setenv(settings.env_name("CURSOR_AUTH_FD"), str(fd))

    with settings.use(), pytest.raises(RuntimeError, match="synthetic failure"):
        with cursor._cursor_subscription_environment() as (environment, mounts):
            home = Path(environment["HOME"])
            auth_path = home / ".config/cursor/auth.json"
            assert mounts == (home,)
            assert stat.S_IMODE(home.stat().st_mode) == 0o700
            assert stat.S_IMODE(auth_path.stat().st_mode) == 0o600
            raise RuntimeError("synthetic failure")

    assert not home.exists()
    os.close(fd)


def test_cursor_rejects_oversized_descriptor_before_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    name = cursor.get_settings().env_name("CURSOR_AUTH_FD")
    monkeypatch.setenv(name, "123")
    metadata = type("Metadata", (), {"st_size": 1024 * 1024 + 1})()
    monkeypatch.setattr(cursor.os, "fstat", lambda _fd: metadata)
    monkeypatch.setattr(
        cursor.os,
        "pread",
        lambda *_args: pytest.fail("oversized descriptor must not be read"),
    )

    with pytest.raises(cursor.CursorProtocolError):
        with cursor._cursor_subscription_environment():
            pass


def test_codex_subscription_auth_scrubs_session_home_on_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state_root = tmp_path / "state"
    state_root.mkdir(mode=0o700)
    session_home = state_root / "session"
    session_home.mkdir(mode=0o700)
    settings = RuntimeSettings(
        state_root=str(state_root), session_home=session_home
    )
    fd = _descriptor(tmp_path / "codex.json", {"tokens": {"access_token": "access"}})
    monkeypatch.setenv(settings.env_name("CODEX_AUTH_FD"), str(fd))

    with settings.use(), pytest.raises(RuntimeError, match="synthetic failure"):
        with bridge._codex_subscription_environment() as (environment, auth_path):
            assert environment == {"CODEX_HOME": str(session_home)}
            assert auth_path is not None and auth_path.is_file()
            raise RuntimeError("synthetic failure")

    assert not (session_home / "auth.json").exists()
    with pytest.raises(OSError):
        os.fstat(fd)


def test_session_home_is_mounted_in_each_launch_and_removed_at_suite_end(
    tmp_path: Path,
) -> None:
    state_root = tmp_path / "state"
    base = RuntimeSettings(state_root=str(state_root))
    launches: list[process.LaunchSpec] = []

    def wrapper(spec: process.LaunchSpec):
        launches.append(spec)
        return spec.argv

    with base.use(), process.private_temporary_directory("suite-session") as home:
        settings = RuntimeSettings(state_root=str(state_root), session_home=home)
        with settings.use():
            for _ in range(2):
                process.run_cli(
                    [sys.executable, "-c", "pass"],
                    cwd=tmp_path,
                    input_text="",
                    timeout_s=2,
                    env={"PATH": os.environ["PATH"]},
                    sandbox_wrapper=wrapper,
                )
        assert all(home in launch.private_mounts for launch in launches)

    assert len(launches) == 2
    assert not home.exists()


@pytest.mark.parametrize("vendor", ["claude", "codex", "cursor"])
def test_explicit_cli_version_mismatch_fails_closed(
    tmp_path: Path, vendor: str
) -> None:
    executable = tmp_path / vendor
    executable.write_text("", encoding="utf-8")
    executable.chmod(0o700)
    settings = RuntimeSettings(
        state_root=str(tmp_path / "state"),
        claude_cli_path=executable if vendor == "claude" else None,
        codex_cli_path=executable if vendor == "codex" else None,
        cursor_cli_path=executable if vendor == "cursor" else None,
    )
    outputs = {
        "claude": "2.1.268\n",
        "codex": "codex-cli 0.153.0\n",
        "cursor": "2026.09.08-deadbeef\n",
    }
    with settings.use():
        adapter = {
            "claude": claude.ClaudeAdapter,
            "codex": codex.CodexAdapter,
            "cursor": cursor.CursorAdapter,
        }[vendor](
            run_probe=lambda *_args, **_kwargs: _outcome(outputs[vendor]),
            **(
                {"sdk_available": lambda *_args: True}
                if vendor in {"claude", "codex"}
                else {}
            ),
        )
    readiness = (
        adapter.probe_sdk(_request(tmp_path, vendor))
        if vendor in {"claude", "codex"}
        else adapter.probe(_request(tmp_path, vendor))
    )

    assert readiness.ready is False
    assert readiness.failure is not None
    assert readiness.failure.value == "sdk-version-mismatch"


def test_codex_output_token_limit_is_in_both_pinned_config_layers() -> None:
    settings = RuntimeSettings(
        budget=RuntimeBudget(max_tokens=32_768, max_turns=2, max_usd=0.25)
    )
    with settings.use():
        assert "output_token_limit=32768" in bridge._codex_budget_overrides()
        assert "output_token_limit=32768" in codex.codex_runtime_overrides()
