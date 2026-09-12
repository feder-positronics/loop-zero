"""Deterministic tests for the opt-in live harness mechanics."""

from __future__ import annotations

from contextlib import contextmanager
import os
from pathlib import Path

import pytest

from loopzero.runners.settings import RuntimeBudget, RuntimeSettings
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


@pytest.mark.parametrize(
    "vendor,expected",
    [("claude", 0.25), ("codex", 0.065536), ("cursor", None)],
)
def test_killed_run_vendor_cap_charge(
    vendor: str, expected: float | None
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
    assert (charge, accounting) == (0.0, "harness-only-unpriced")


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


def test_live_credential_source_is_resealed_and_never_modified(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "source.json"
    original = b'{"tokens":{"access_token":"access","refresh_token":"refresh"}}'
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
        credential_path.write_text('{"rotated":true}', encoding="utf-8")
        fd = os.open(credential_path, os.O_RDONLY)
        try:
            yield fd
        finally:
            os.close(fd)

    monkeypatch.setattr(live.codex, "codex_subscription_credential", broker)

    with live._credential("codex", 5, settings, lambda spec: spec.argv):
        pass

    assert source.read_bytes() == original
    assert observed["path"] != source
    assert not observed["path"].exists()
