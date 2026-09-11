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


def _agent_tooling_lane_was_selected(config: pytest.Config) -> bool:
    for raw_argument in config.args:
        path_argument = Path(str(raw_argument).split("::", maxsplit=1)[0])
        candidate = (
            path_argument if path_argument.is_absolute() else Path.cwd() / path_argument
        ).resolve()
        if candidate == _AGENT_TOOLING_TEST_DIR or candidate.is_relative_to(
            _AGENT_TOOLING_TEST_DIR
        ):
            return True
    return False


def pytest_collection_modifyitems(
    config: pytest.Config,
    items: list[pytest.Item],
) -> None:
    """Skip this isolated suite unless its directory or a child was selected."""
    if _agent_tooling_lane_was_selected(config):
        return
    isolated_lane = pytest.mark.skip(
        reason="agent-tooling tests run on their explicitly selected lane"
    )
    for item in items:
        if Path(str(item.path)).resolve().is_relative_to(_AGENT_TOOLING_TEST_DIR):
            item.add_marker(isolated_lane)
