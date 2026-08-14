"""Keep agent-tooling tests on their explicitly selected test lane."""

from pathlib import Path

import pytest

_AGENT_TOOLING_TEST_DIR = Path(__file__).resolve().parent


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
