"""Keep agent-tooling tests on their explicitly selected test lane."""

import os
from pathlib import Path

import pytest

_AGENT_TOOLING_TEST_DIR = Path(__file__).resolve().parent


@pytest.fixture(scope="session")
def isolated_ptrace_scope_path(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Provide a stable Yama contract outside per-test Git repositories."""
    ptrace_scope = tmp_path_factory.mktemp("kernel-contract") / "ptrace_scope"
    ptrace_scope.write_text("1\n", encoding="ascii")
    return ptrace_scope


@pytest.fixture(autouse=True)
def _isolate_git_config_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """Give nested secure Git runners no inherited outer configuration."""
    for name in tuple(os.environ):
        if name == "GIT_CONFIG" or name.startswith("GIT_CONFIG_"):
            monkeypatch.delenv(name)



# Imported behavior tests use the original consumer namespace in-process and in
# subprocesses. New settings tests explicitly exercise a second namespace.
os.environ["LOOPZERO_ENV_PREFIX"] = "INTELFLO"
from loopzero.kernel.settings import KernelSettings, configure
configure(KernelSettings(env_prefix="INTELFLO", toolchain={"interpreter": "fastapi_backend/.venv/bin/python"}))

@pytest.fixture(autouse=True)
def kernel_settings(monkeypatch):
    import sys
    monkeypatch.setenv("LOOPZERO_ENV_PREFIX", "INTELFLO")
    monkeypatch.setenv("LOOPZERO_PYTHON", sys.executable)
    yield
