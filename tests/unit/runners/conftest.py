"""Legacy-name fixtures for the unchanged consumer tests."""

from pathlib import Path
import sys

import pytest

from loopzero.runners.settings import RuntimeSettings, DEFAULT_SETTINGS


@pytest.fixture(autouse=True)
def runtime_settings(monkeypatch, tmp_path):
    settings = RuntimeSettings(
        env_prefix="INTELFLO",
        toolchain_interpreter=Path("fastapi_backend/.venv/bin/python"),
        state_root=str(tmp_path / "runner-state"),
    )
    # Exported environment-name constants remain strings. Legacy callers that
    # use those names explicitly get their consumer's names in this fixture.
    for name, module in tuple(sys.modules.items()):
        if not name.startswith("loopzero.runners.") or name.endswith("settings"):
            continue
        for key, value in tuple(vars(module).items()):
            if isinstance(value, str) and value.startswith("LOOPZERO_"):
                monkeypatch.setattr(module, key, settings.env_name(value.removeprefix("LOOPZERO_")))
            if key == "BROKER_LOCK_NAME" and value == DEFAULT_SETTINGS.lock_name("codex-refresh"):
                monkeypatch.setattr(module, key, settings.lock_name("codex-refresh"))
    with settings.use():
        yield settings
