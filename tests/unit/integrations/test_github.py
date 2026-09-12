import ast
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


def test_repository_identity_rejects_same_path_on_another_host():
    with pytest.raises(GitHubError, match="does not match configured GitHub host"):
        repository_from_origin("https://attacker.invalid/acme/widget.git")


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


def test_gh_child_environment_is_allowlisted_and_host_is_argv_bound(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured = {}
    for name, value in {
        "GH_TOKEN": "configured-token",
        "GITHUB_TOKEN": "alternate-token",
        "GH_HOST": "attacker.invalid",
        "GH_REPO": "attacker/project",
        "GH_ENTERPRISE_TOKEN": "enterprise-token",
        "GH_CONFIG_DIR": "/tmp/attacker-gh",
        "GH_FUTURE_OVERRIDE": "future-redirect",
        "GITHUB_FUTURE_OVERRIDE": "future-redirect",
        "UNRELATED_SECRET": "not-for-gh",
    }.items():
        monkeypatch.setenv(name, value)

    def run(argv, **kwargs):
        captured["argv"] = argv
        captured["env"] = kwargs["env"]
        return subprocess.CompletedProcess(argv, 0, "{}", "")

    client = GitHub(
        tmp_path,
        GitHubSettings(labels={}, retries=0),
        run=run,
    )
    client._checked_version = True
    assert client.api("repos/acme/widget/pulls/1") == {}
    assert captured["argv"][1:] == [
        "api",
        "--hostname",
        "github.com",
        "--method",
        "GET",
        "repos/acme/widget/pulls/1",
    ]
    assert captured["env"]["GH_TOKEN"] == "configured-token"
    assert not {
        name
        for name in captured["env"]
        if name.startswith(("GH_", "GITHUB_")) and name != "GH_TOKEN"
    }
    assert "UNRELATED_SECRET" not in captured["env"]


_PROCESS_CALL_ALLOWLIST = frozenset(
    {
        "cli.py",
        "config.py",
        "delivery/_body_check.py",
        "delivery/_obligations.py",
        "delivery/_publish_body.py",
        "delivery/closeout.py",
        "delivery/publish.py",
        "guardian/state.py",
        "hooks/size_lint.py",
        "integrations/github.py",
        "review/_security_scope.py",
        "review/acceptance.py",
        "review/authority.py",
        "review/evidence.py",
        "review/findings.py",
        "review/risk.py",
        "runners/claude.py",
        "runners/codex.py",
        "runners/process.py",
        "delivery/_publish_threads.py",
    }
)


def _process_call_lines(source: str, *, filename: str) -> list[int]:
    """Find process creation regardless of how its argv is constructed."""
    tree = ast.parse(source, filename=filename)
    module_aliases = {"subprocess": "subprocess", "os": "os", "asyncio": "asyncio"}
    direct_calls: dict[str, tuple[str, str]] = {}
    for node in tree.body:
        if isinstance(node, ast.Import):
            for imported in node.names:
                if imported.name in module_aliases:
                    module_aliases[imported.asname or imported.name] = imported.name
        elif isinstance(node, ast.ImportFrom) and node.module in {
            "subprocess", "os", "asyncio"
        }:
            for imported in node.names:
                direct_calls[imported.asname or imported.name] = (
                    node.module,
                    imported.name,
                )

    def is_execution(module: str, name: str) -> bool:
        return module == "subprocess" or (
            module == "os"
            and (
                name == "system"
                or name.startswith(("exec", "spawn", "posix_spawn"))
            )
        ) or (module == "asyncio" and name.startswith("create_subprocess"))

    violations = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        if isinstance(node.func, ast.Name):
            imported = direct_calls.get(node.func.id)
            if imported is not None and is_execution(*imported):
                violations.append(node.lineno)
        elif (
            isinstance(node.func, ast.Attribute)
            and isinstance(node.func.value, ast.Name)
            and (module := module_aliases.get(node.func.value.id)) is not None
            and is_execution(module, node.func.attr)
        ):
            violations.append(node.lineno)
    return sorted(violations)


def test_github_integration_is_the_only_subprocess_gh_boundary() -> None:
    package = Path(__file__).resolve().parents[3] / "src" / "loopzero"
    violations: list[str] = []
    for path in package.rglob("*.py"):
        relative = path.relative_to(package).as_posix()
        if relative.startswith("kernel/") or relative in _PROCESS_CALL_ALLOWLIST:
            continue
        source = path.read_text(encoding="utf-8")
        violations.extend(
            f"{relative}:{line}"
            for line in _process_call_lines(source, filename=str(path))
        )
    assert violations == []


@pytest.mark.parametrize(
    ("source", "expected"),
    (
        ('import subprocess\nsubprocess.run(["g" + "h", "api"])\n', [2]),
        (
            'import shutil, subprocess\ngh = shutil.which("gh")\n'
            'subprocess.run([gh, "api"])\n',
            [3],
        ),
    ),
)
def test_process_boundary_scan_rejects_hidden_gh_argv(
    source: str, expected: list[int]
) -> None:
    assert _process_call_lines(source, filename="future.py") == expected
