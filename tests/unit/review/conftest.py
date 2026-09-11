from pathlib import Path

import pytest


@pytest.fixture(scope="session")
def isolated_ptrace_scope_path(tmp_path_factory: pytest.TempPathFactory) -> Path:
    path = tmp_path_factory.mktemp("review-ptrace") / "ptrace_scope"
    path.write_text("1\n", encoding="utf-8")
    return path
