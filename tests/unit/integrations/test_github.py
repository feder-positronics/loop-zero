import ast
import re
import subprocess
from pathlib import Path

import pytest

from loopzero.integrations.github import (
    GitHub,
    GitHubError,
    GitHubSettings,
    repository_from_origin,
)


@pytest.mark.parametrize(
    "origin", ["git@github.com:acme/widget.git", "https://github.com/acme/widget.git"]
)
def test_repository_identity_comes_from_origin(origin: str):
    assert repository_from_origin(origin).slug == "acme/widget"


def test_version_floor_retries_and_label_vocabulary(tmp_path: Path):
    calls = []

    def run(argv, **kwargs):
        calls.append(argv)
        if len(calls) == 1:
            return subprocess.CompletedProcess(argv, 1, "", "temporary")
        return subprocess.CompletedProcess(argv, 0, "gh version 2.40.1 (test)\n", "")

    client = GitHub(
        tmp_path,
        GitHubSettings(labels={"blocked": "needs-attention"}, retries=1),
        run=run,
        sleep=lambda _: None,
    )
    assert client.check_version() == (2, 40, 1)
    assert client.labels(["blocked", "blocked"]) == ("needs-attention",)
    assert len(calls) == 2


def test_unknown_label_and_old_cli_fail_closed(tmp_path: Path):
    client = GitHub(
        tmp_path,
        GitHubSettings(labels={}, version_floor=(3, 0, 0), retries=0),
        run=lambda argv, **kwargs: subprocess.CompletedProcess(
            argv, 0, "gh version 2.99.0 (test)\n", ""
        ),
    )
    with pytest.raises(GitHubError, match="newer"):
        client.check_version()
    with pytest.raises(GitHubError, match="unknown configured label"):
        client.labels(["standalone"])


def test_non_idempotent_api_mutation_is_never_retried(tmp_path: Path):
    calls = []

    def run(argv, **kwargs):
        calls.append(argv)
        if argv[-1] == "version":
            return subprocess.CompletedProcess(argv, 0, "gh version 2.40.1\n", "")
        return subprocess.CompletedProcess(argv, 1, "", "connection reset")

    client = GitHub(
        tmp_path,
        GitHubSettings(labels={}, retries=5),
        run=run,
        sleep=lambda _: pytest.fail("mutation retry slept"),
    )
    client.check_version()
    with pytest.raises(GitHubError, match="connection reset"):
        client.api("repos/acme/widget/pulls", method="POST", fields={"title": "x"})
    assert len(calls) == 2


def test_github_integration_is_the_only_subprocess_gh_boundary() -> None:
    package = Path(__file__).resolve().parents[3] / "src" / "loopzero"
    violations: list[str] = []
    for path in package.rglob("*.py"):
        if path == package / "integrations" / "github.py":
            continue
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
                continue
            owner = node.func.value
            if (
                not isinstance(owner, ast.Name)
                or owner.id != "subprocess"
                or node.func.attr not in {"run", "Popen", "check_call", "check_output"}
            ):
                continue
            invocation = ast.get_source_segment(source, node) or ""
            if re.search(r"[\"']gh[\"']", invocation):
                violations.append(f"{path.relative_to(package)}:{node.lineno}")
    assert violations == []
