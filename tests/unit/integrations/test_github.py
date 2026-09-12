import ast
import re
import shutil
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


_PROCESS_CALL_ALLOWLIST = {
    "cli.py": "Runs the configured source-status observation command.",
    "config.py": "Reads repository identity through bounded Git queries.",
    "delivery/_obligations.py": "Computes obligation changes with Git.",
    "delivery/_publish_body.py": "Runs the isolated PR-body validator.",
    "delivery/closeout.py": "Runs audited Git and configured closeout tools.",
    "delivery/publish.py": "Reads the canonical Git worktree registration.",
    "guardian/state.py": "Resolves the canonical Git common directory.",
    "hooks/size_lint.py": "Measures committed and staged changes with Git.",
    "integrations/github.py": "Owns the sole gh boundary and audited Git access.",
    "kernel/authority.py": "Runs bounded Git and authority subprocess checks.",
    "kernel/authority_projection.py": "Projects repository facts through Git.",
    "kernel/authority_store.py": "Reads protected branch and revision state.",
    "kernel/capabilities.py": "Probes the configured sandbox executable.",
    "kernel/events.py": "Reads Git metadata for append-only event identity.",
    "kernel/git_config_security.py": "Parses canonical Git configuration.",
    "kernel/gitscope.py": "Implements the trusted Git command boundary.",
    "kernel/jobs.py": "Observes and controls governed worker processes.",
    "kernel/liveness.py": "Collects bounded process-liveness evidence.",
    "kernel/patch_identity.py": "Computes patch identity through trusted Git.",
    "kernel/run_log.py": "Reads Git identity for governed run records.",
    "kernel/sandbox.py": "Launches commands inside the kernel sandbox.",
    "kernel/validation.py": "Runs configured validation commands.",
    "kernel/worktree_claims.py": "Collects process and Git worktree claims.",
    "kernel/worktree_lease.py": "Manages Git worktrees and execs lease commands.",
    "kernel/worktree_list.py": "Reads registered Git worktrees.",
    "kernel/worktree_prune.py": "Performs bounded Git worktree pruning.",
    "review/_security_scope.py": "Reads security-sensitive deltas with Git.",
    "review/acceptance.py": "Collects acceptance evidence with trusted Git.",
    "review/authority.py": "Projects review authority from trusted Git state.",
    "review/evidence.py": "Materializes immutable review evidence with Git.",
    "review/findings.py": "Resolves the primary repository through Git.",
    "review/risk.py": "Computes review risk from trusted Git deltas.",
    "runners/process.py": "Owns the governed runtime process launcher.",
}

_SUBPROCESS_EXECUTION_NAMES = frozenset(
    {
        "Popen",
        "call",
        "check_call",
        "check_output",
        "getoutput",
        "getstatusoutput",
        "run",
    }
)


def _process_calls(source: str, *, filename: str) -> tuple[
    ast.AST,
    list[tuple[ast.Call, str, str]],
    dict[str, str],
    dict[str, tuple[str, str]],
]:
    """Find process creation regardless of import aliases or argv construction."""
    tree = ast.parse(source, filename=filename)
    module_aliases: dict[str, str] = {}
    direct_calls: dict[str, tuple[str, str]] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for imported in node.names:
                if imported.name in {"subprocess", "os", "asyncio"}:
                    module_aliases[imported.asname or imported.name] = imported.name
        elif isinstance(node, ast.ImportFrom) and node.module in {
            "subprocess",
            "os",
            "asyncio",
        }:
            for imported in node.names:
                direct_calls[imported.asname or imported.name] = (
                    node.module,
                    imported.name,
                )

    def is_execution(module: str, name: str) -> bool:
        return (
            module == "subprocess" and name in _SUBPROCESS_EXECUTION_NAMES
        ) or (
            module == "os"
            and (
                name == "system"
                or name.startswith(("exec", "spawn", "posix_spawn"))
            )
        ) or (module == "asyncio" and name.startswith("create_subprocess"))

    calls: list[tuple[ast.Call, str, str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        target: tuple[str, str] | None = None
        if isinstance(node.func, ast.Name):
            target = direct_calls.get(node.func.id)
        elif isinstance(node.func, ast.Attribute) and isinstance(
            node.func.value, ast.Name
        ):
            module = module_aliases.get(node.func.value.id)
            if module is not None:
                target = module, node.func.attr
        if target is not None and is_execution(*target):
            calls.append((node, *target))
    return (
        tree,
        sorted(calls, key=lambda item: item[0].lineno),
        module_aliases,
        direct_calls,
    )


def _process_call_lines(source: str, *, filename: str) -> list[int]:
    _tree, calls, _aliases, _direct = _process_calls(source, filename=filename)
    return [node.lineno for node, _module, _name in calls]


def _gh_process_call_lines(source: str, *, filename: str) -> list[int]:
    tree, calls, module_aliases, direct_calls = _process_calls(
        source, filename=filename
    )
    assignments: dict[str, list[ast.AST]] = {}
    for node in ast.walk(tree):
        if isinstance(node, (ast.Assign, ast.AnnAssign, ast.NamedExpr)):
            value = node.value
            if not isinstance(value, ast.AST):
                continue
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for target in targets:
                for child in ast.walk(target):
                    if isinstance(child, ast.Name):
                        assignments.setdefault(child.id, []).append(value)

    def literal_string(
        node: ast.AST, resolving: frozenset[str] = frozenset()
    ) -> str | None:
        """Resolve str, bytes, concatenation and f-string literals statically."""
        if isinstance(node, ast.Constant):
            if isinstance(node.value, str):
                return node.value
            if isinstance(node.value, bytes):
                return node.value.decode("latin-1")
            return None
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
            left = literal_string(node.left, resolving)
            right = literal_string(node.right, resolving)
            if left is not None and right is not None:
                return left + right
            return None
        if isinstance(node, ast.JoinedStr):
            parts = [literal_string(value, resolving) for value in node.values]
            return None if any(part is None for part in parts) else "".join(parts)
        if isinstance(node, ast.FormattedValue):
            return literal_string(node.value, resolving)
        if isinstance(node, ast.Name) and node.id not in resolving:
            values = assignments.get(node.id, [])
            resolved = {
                literal_string(value, resolving | {node.id}) for value in values
            }
            if len(resolved) == 1 and None not in resolved:
                return resolved.pop()
        return None

    def mentions_gh(text: str) -> bool:
        words = text.lower().replace("/", " ").split()
        return text.lower() == "gh" or "gh" in words

    def environment_key_mentions_gh(text: str) -> bool:
        return "gh" in re.split(r"[^a-z0-9]+", text.lower())

    def is_environ_object(node: ast.AST) -> bool:
        if isinstance(node, ast.Attribute) and node.attr == "environ":
            return (
                isinstance(node.value, ast.Name)
                and module_aliases.get(node.value.id) == "os"
            )
        if isinstance(node, ast.Name):
            return direct_calls.get(node.id) == ("os", "environ")
        return False

    def environment_lookup_key(node: ast.AST) -> tuple[bool, ast.AST | None]:
        """Return (is_lookup, key) for os.environ[...]/.get(...)/os.getenv(...)."""
        if isinstance(node, ast.Subscript) and is_environ_object(node.value):
            return True, node.slice
        if isinstance(node, ast.Call):
            func = node.func
            key = node.args[0] if node.args else None
            if isinstance(func, ast.Attribute):
                if func.attr == "get" and is_environ_object(func.value):
                    return True, key
                if (
                    func.attr == "getenv"
                    and isinstance(func.value, ast.Name)
                    and module_aliases.get(func.value.id) == "os"
                ):
                    return True, key
            if isinstance(func, ast.Name) and direct_calls.get(func.id) == (
                "os",
                "getenv",
            ):
                return True, key
        return False, None

    def contains_environment_lookup(node: ast.AST, env_tainted: frozenset[str]) -> bool:
        for child in ast.walk(node):
            if environment_lookup_key(child)[0]:
                return True
            if isinstance(child, ast.Name) and child.id in env_tainted:
                return True
        return False

    def references_gh(node: ast.AST, tainted_names: frozenset[str]) -> bool:
        literal = literal_string(node)
        if literal is not None:
            return mentions_gh(literal)
        is_lookup, key = environment_lookup_key(node)
        if is_lookup:
            # A gh-named or unauditable environment key is a hidden gh argv.
            key_literal = None if key is None else literal_string(key)
            return key_literal is None or environment_key_mentions_gh(key_literal)
        if isinstance(node, ast.Name):
            return node.id.lower() == "gh" or node.id in tainted_names
        if isinstance(node, ast.JoinedStr):
            return any(
                references_gh(value, tainted_names) for value in node.values
            ) or mentions_gh(
                "".join(literal_string(value) or " " for value in node.values)
            )
        return any(
            references_gh(child, tainted_names)
            for child in ast.iter_child_nodes(node)
        )

    tainted: set[str] = set()
    while True:
        discovered = {
            name
            for name, values in assignments.items()
            if name not in tainted
            and any(references_gh(value, frozenset(tainted)) for value in values)
        }
        if not discovered:
            break
        tainted.update(discovered)

    environment_tainted: set[str] = set()
    while True:
        discovered = {
            name
            for name, values in assignments.items()
            if name not in environment_tainted
            and any(
                contains_environment_lookup(value, frozenset(environment_tainted))
                for value in values
            )
        }
        if not discovered:
            break
        environment_tainted.update(discovered)

    def executable_nodes(call: ast.Call) -> list[ast.AST]:
        """Return the nodes that decide which program a process call launches."""
        candidates: list[ast.AST] = []
        positional = call.args[0] if call.args else None
        for keyword in call.keywords:
            if keyword.arg == "args" and positional is None:
                positional = keyword.value
            if keyword.arg == "executable":
                candidates.append(keyword.value)
        if positional is not None:
            if isinstance(positional, (ast.List, ast.Tuple)) and positional.elts:
                candidates.append(positional.elts[0])
            else:
                candidates.append(positional)
        return candidates

    def executable_from_environment(node: ast.AST) -> bool:
        # The program itself resolved from the process environment cannot be
        # audited statically, whatever the key is called.
        if isinstance(node, ast.Starred):
            return executable_from_environment(node.value)
        if isinstance(node, ast.Subscript) and not environment_lookup_key(node)[0]:
            return executable_from_environment(node.value)
        if isinstance(node, ast.Name):
            return node.id in environment_tainted
        return contains_environment_lookup(node, frozenset(environment_tainted))

    violations: list[int] = []
    for node, _module, _name in calls:
        argv = list(node.args)
        argv.extend(
            keyword.value
            for keyword in node.keywords
            if keyword.arg in {"args", "executable"}
        )
        if any(references_gh(value, frozenset(tainted)) for value in argv) or any(
            executable_from_environment(value) for value in executable_nodes(node)
        ):
            violations.append(node.lineno)
    return violations


def _process_boundary_violations(package: Path) -> list[str]:
    """Scan every package module against the audited process policy."""
    violations: list[str] = []
    modules_with_calls: set[str] = set()
    for path in sorted(package.rglob("*.py")):
        relative = path.relative_to(package).as_posix()
        source = path.read_text(encoding="utf-8")
        call_lines = _process_call_lines(source, filename=str(path))
        if call_lines:
            modules_with_calls.add(relative)
        if relative not in _PROCESS_CALL_ALLOWLIST:
            violations.extend(
                f"{relative}:{line}: process creation outside audited allowlist"
                for line in call_lines
            )
        elif relative != "integrations/github.py":
            violations.extend(
                f"{relative}:{line}: gh outside the sole GitHub boundary"
                for line in _gh_process_call_lines(source, filename=str(path))
            )
    violations.extend(
        f"{relative}: stale process allowlist entry"
        for relative in sorted(_PROCESS_CALL_ALLOWLIST.keys() - modules_with_calls)
    )
    return violations


def test_github_integration_is_the_only_subprocess_gh_boundary() -> None:
    package = Path(__file__).resolve().parents[3] / "src" / "loopzero"
    assert _process_boundary_violations(package) == []


@pytest.mark.parametrize(
    ("source", "expected"),
    (
        ('import subprocess\nsubprocess.run(["g" + "h", "api"])\n', [2]),
        (
            'import shutil, subprocess\ngh = shutil.which("gh")\n'
            'subprocess.run([gh, "api"])\n',
            [3],
        ),
        # f-string assembled across a formatted value and a literal tail.
        (
            'import subprocess\ntool = "g"\nsubprocess.run([f"{tool}h", "api"])\n',
            [3],
        ),
        # bytes argv never reaches the str literal path.
        ('import subprocess\nsubprocess.run([b"gh", b"api"])\n', [2]),
        # executable resolved from the process environment.
        (
            'import os, subprocess\n'
            'subprocess.run([os.environ["GH_BIN"], "api"])\n',
            [2],
        ),
        (
            'import os, subprocess\n'
            'subprocess.run([os.getenv("LOOPZERO_GH"), "api"])\n',
            [2],
        ),
        (
            'from os import environ\nimport subprocess\n'
            'subprocess.run([environ.get("TOOL"), "api"])\n',
            [3],
        ),
    ),
)
def test_process_boundary_scan_rejects_hidden_gh_argv(
    source: str, expected: list[int]
) -> None:
    assert _process_call_lines(source, filename="future.py") == expected
    assert _gh_process_call_lines(source, filename="future.py") == expected


@pytest.mark.parametrize(
    "counterexample",
    (
        'import subprocess\nsubprocess.run(["gh", "api"])\n',
        'import subprocess\nsubprocess.run(["g" + "h", "api"])\n',
        (
            'import shutil, subprocess\ngh = shutil.which("gh")\n'
            'subprocess.run([gh, "api"])\n'
        ),
        'import os\nos.posix_spawn("gh", ["gh", "api"], {})\n',
        'import asyncio\nasyncio.create_subprocess_exec("gh", "api")\n',
        'import subprocess as sp\nsp.run(["gh", "api"])\n',
        'import subprocess\ntool = "g"\nsubprocess.run([f"{tool}h", "api"])\n',
        'import subprocess\nsubprocess.run([b"gh", b"api"])\n',
        'import os, subprocess\nsubprocess.run([os.environ["GH_BIN"], "api"])\n',
    ),
)
def test_package_scan_rejects_process_counterexample_in_body_check(
    tmp_path: Path, counterexample: str
) -> None:
    package = Path(__file__).resolve().parents[3] / "src" / "loopzero"
    copied_package = tmp_path / "loopzero"
    shutil.copytree(package, copied_package)
    body_check = copied_package / "delivery" / "_body_check.py"
    body_check.write_text(
        body_check.read_text(encoding="utf-8") + "\n" + counterexample,
        encoding="utf-8",
    )

    assert any(
        violation.startswith("delivery/_body_check.py:")
        for violation in _process_boundary_violations(copied_package)
    )
