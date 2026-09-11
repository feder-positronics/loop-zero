"""Keep agent-tooling tests on their explicitly selected test lane."""

import os
from pathlib import Path

import pytest

from loopzero.kernel import authority

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


@pytest.fixture(autouse=True)
def _isolate_delivery_authority_state(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(
        authority,
        "_coordinator_state_directory",
        lambda: tmp_path / "dispatch-authority",
    )
