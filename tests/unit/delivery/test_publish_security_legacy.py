"""Security-boundary publication tests for ``scripts/util/pr_publish.py``.

Split from ``test_pr_publish.py`` to keep that module under the size guard.
The trusted-base security checks and their runner stub are self-contained;
the small ``_security_repo`` fixture builder is duplicated in the source
module for its single remaining caller.
"""

import importlib
import json
import subprocess
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest


def load_module() -> ModuleType:
    return importlib.import_module("loopzero.delivery.publish")


module = load_module()
publish_risk = importlib.import_module("loopzero.delivery._publish_risk")
authority = importlib.import_module("loopzero.kernel.authority")


def context_body(trailer: str = "Closes #2645") -> str:
    return (
        "## Context and goal\n\n"
        "- **Context:** Teammates use pull requests to understand a change.\n"
        "- **Problem:** The purpose can otherwise be unclear.\n"
        "- **Goal:** Make the intended outcome easy to understand.\n\n"
        + trailer
        + "\n\n## Validation\n\n- Focused publication tests passed.\n"
    )


VALID_BODY = context_body()


@pytest.fixture(autouse=True)
def isolated_process_memory_contract(
    monkeypatch: pytest.MonkeyPatch, isolated_ptrace_scope_path: Path
) -> None:
    """Keep authority fixtures independent of the runner's Yama configuration."""
    authority_globals = (
        authority.TerminalAuthority.generate.__func__.__globals__,
        authority.CoordinatorAuthority.from_local_state.__func__.__globals__,
    )
    for globals_ in {id(item): item for item in authority_globals}.values():
        monkeypatch.setitem(globals_, "PTRACE_SCOPE_PATH", isolated_ptrace_scope_path)


def _security_repo(tmp_path: Path, files: dict[str, str]) -> str:
    module.subprocess.run(
        ["git", "init", str(tmp_path)], check=True, capture_output=True
    )
    module.subprocess.run(
        ["git", "-C", str(tmp_path), "config", "user.email", "test@example.com"],
        check=True,
    )
    module.subprocess.run(
        ["git", "-C", str(tmp_path), "config", "user.name", "Test"], check=True
    )
    for path, content in files.items():
        target = tmp_path / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    module.subprocess.run(["git", "-C", str(tmp_path), "add", "."], check=True)
    module.subprocess.run(
        ["git", "-C", str(tmp_path), "commit", "-m", "base"],
        check=True,
        capture_output=True,
    )
    return module.subprocess.check_output(
        ["git", "-C", str(tmp_path), "rev-parse", "HEAD"], text=True
    ).strip()


def test_security_trigger_paths_skip_version_and_test_only_edits(
    tmp_path: Path,
) -> None:
    base = _security_repo(
        tmp_path,
        {
            "package.json": '{\n  "dependencies": {\n    "react": "18"\n  }\n}\n',
            "tests/action.ts": '"use server";\n',
        },
    )
    (tmp_path / "package.json").write_text(
        '{\n  "dependencies": {\n    "react": "19"\n  }\n}\n', encoding="utf-8"
    )
    (tmp_path / "tests/action.ts").write_text(
        '"use server";\nconst token = "test";\n', encoding="utf-8"
    )

    assert publish_risk.security_trigger_paths(tmp_path, base) == ()


def test_security_scope_errors_preserve_publication_error_contract(monkeypatch) -> None:
    def fail(*_args, **_kwargs):
        raise publish_risk.SecurityReviewScopeError("malformed scope")

    monkeypatch.setattr(publish_risk, "shared_security_trigger_paths_between", fail)

    with pytest.raises(module.PublicationError, match="malformed scope"):
        module._security_trigger_paths_between(Path("/repo"), "a" * 40, "b" * 40)


def test_security_trigger_paths_skip_prose_but_keep_machine_docs_triggers(
    tmp_path: Path,
) -> None:
    base = _security_repo(tmp_path, {"README.md": "base\n"})
    prose_paths = (
        "docs/architecture/middleware-notes.md",
        "docs/integrations/provider.md",
    )
    machine_paths = (
        "docs/policies/middleware-policy.yaml",
        "docs/policies/runtime.env.example",
    )
    for path in (*prose_paths, *machine_paths):
        target = tmp_path / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("Documentation only.\n", encoding="utf-8")
    module.subprocess.run(["git", "-C", str(tmp_path), "add", "docs"], check=True)
    module.subprocess.run(
        ["git", "-C", str(tmp_path), "commit", "-m", "docs"],
        check=True,
        capture_output=True,
    )

    assert publish_risk.security_trigger_paths(tmp_path, base) == machine_paths


def test_security_trigger_paths_detect_semantic_and_new_package_edits(
    tmp_path: Path,
) -> None:
    base = _security_repo(tmp_path, {"README.md": "base\n"})
    files = {
        "app/submit.ts": '"use server";\nexport async function submit() {}\n',
        "fastapi_backend/app/config.py": "SECRET = None\n",
        "package.json": '{\n  "dependencies": {\n    "new-client": "1"\n  }\n}\n',
    }
    for path, content in files.items():
        target = tmp_path / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    module.subprocess.run(["git", "-C", str(tmp_path), "add", "."], check=True)
    module.subprocess.run(
        ["git", "-C", str(tmp_path), "commit", "-m", "change"],
        check=True,
        capture_output=True,
    )

    assert publish_risk.security_trigger_paths(tmp_path, base) == tuple(sorted(files))


def test_security_trigger_paths_detect_unicode_python_paths(tmp_path: Path) -> None:
    base = _security_repo(tmp_path, {"README.md": "base\n"})
    path = "fastapi_backend/app/\u0430uth.py"
    target = tmp_path / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("current_user = require_session()\n", encoding="utf-8")
    module.subprocess.run(["git", "-C", str(tmp_path), "add", path], check=True)
    module.subprocess.run(
        ["git", "-C", str(tmp_path), "commit", "-m", "unicode auth"],
        check=True,
        capture_output=True,
    )

    assert publish_risk.security_trigger_paths(tmp_path, base) == (path,)


def test_security_trigger_paths_parse_unicode_name_status_without_quoting(
    tmp_path: Path,
) -> None:
    path = "fastapi_backend/app/\u0430uth.py"

    class Runner:
        def run(self, args, **kwargs):
            if "--name-status" in args:
                return SimpleNamespace(returncode=0, stdout=f"A\0{path}\0", stderr="")
            assert "--unified=0" in args
            assert args[-1] == path
            return SimpleNamespace(
                returncode=0,
                stdout="@@ -0,0 +1 @@\n+current_user = require_session()\n",
                stderr="",
            )

    assert module._security_trigger_paths_between(
        tmp_path,
        "a" * 40,
        "b" * 40,
        runner=Runner(),
    ) == (path,)


def test_security_trigger_paths_inspect_source_of_rename_into_prose_docs(
    tmp_path: Path,
) -> None:
    source = "app/auth_middleware.py"
    destination = "docs/architecture/auth-middleware.md"

    class Runner:
        def run(self, args, **kwargs):
            if "--name-status" in args:
                return SimpleNamespace(
                    returncode=0,
                    stdout=f"R100\0{source}\0{destination}\0",
                    stderr="",
                )
            assert "--unified=0" in args
            assert args[-1] == source
            return SimpleNamespace(
                returncode=0,
                stdout="@@ -1 +0,0 @@\n-Depends(get_current_user)\n",
                stderr="",
            )

    assert module._security_trigger_paths_between(
        tmp_path,
        "a" * 40,
        "b" * 40,
        runner=Runner(),
    ) == (source,)


@pytest.mark.parametrize(
    "ownership_addition",
    (
        "record.user_id = actor.id",
        "record.created_by_owner_id = owner.id",
        "query = query.where(Model.tenant_id == tenant.id)",
        "account_id = request.account_id",
        "actor_ids = [actor.id]",
        "actor_kind = 'user'",
        "stored_file.uploader_id = actor.id",
        "record.created_by = actor.id",
    ),
)
def test_security_trigger_paths_detect_ownership_changes_in_ordinary_python(
    tmp_path: Path,
    ownership_addition: str,
) -> None:
    path = "fastapi_backend/app/services/articles/article_service.py"
    base = _security_repo(tmp_path, {path: "def update(record):\n    return record\n"})
    (tmp_path / path).write_text(
        "def update(record):\n" f"    {ownership_addition}\n" "    return record\n",
        encoding="utf-8",
    )
    module.subprocess.run(["git", "-C", str(tmp_path), "add", path], check=True)
    module.subprocess.run(
        ["git", "-C", str(tmp_path), "commit", "-m", "change"],
        check=True,
        capture_output=True,
    )

    assert publish_risk.security_trigger_paths(tmp_path, base) == (path,)


def test_security_trigger_paths_detect_removed_user_isolation_filter(
    tmp_path: Path,
) -> None:
    path = "fastapi_backend/app/services/sources/source_write_service.py"
    base = _security_repo(
        tmp_path,
        {
            path: (
                "def owned(query, current_user):\n"
                "    return query.where(Model.user_id == current_user.id)\n"
            )
        },
    )
    (tmp_path / path).write_text(
        "def owned(query, current_user):\n    return query\n",
        encoding="utf-8",
    )
    module.subprocess.run(["git", "-C", str(tmp_path), "add", path], check=True)
    module.subprocess.run(
        ["git", "-C", str(tmp_path), "commit", "-m", "change"],
        check=True,
        capture_output=True,
    )

    assert publish_risk.security_trigger_paths(tmp_path, base) == (path,)


@pytest.mark.parametrize(
    "removed_dependency",
    (
        "user: CurrentUser",
        "admin: CurrentAdmin",
        "user = Depends(current_active_user)",
    ),
)
def test_security_trigger_paths_detect_removed_router_auth_dependency(
    tmp_path: Path,
    removed_dependency: str,
) -> None:
    path = "fastapi_backend/app/routers/articles.py"
    base = _security_repo(
        tmp_path,
        {path: f"def read({removed_dependency}):\n    return []\n"},
    )
    (tmp_path / path).write_text(
        "def read():\n    return []\n",
        encoding="utf-8",
    )
    module.subprocess.run(["git", "-C", str(tmp_path), "add", path], check=True)
    module.subprocess.run(
        ["git", "-C", str(tmp_path), "commit", "-m", "change"],
        check=True,
        capture_output=True,
    )

    assert publish_risk.security_trigger_paths(tmp_path, base) == (path,)


@pytest.mark.parametrize("scope_name", ("access_scope", "dedup_scope"))
def test_security_trigger_paths_detect_isolation_scope_change(
    tmp_path: Path,
    scope_name: str,
) -> None:
    path = "fastapi_backend/app/services/storage/attachment_service.py"
    base = _security_repo(tmp_path, {path: f'{scope_name} = "user"\n'})
    (tmp_path / path).write_text(f'{scope_name} = "public"\n', encoding="utf-8")
    module.subprocess.run(["git", "-C", str(tmp_path), "add", path], check=True)
    module.subprocess.run(
        ["git", "-C", str(tmp_path), "commit", "-m", "change"],
        check=True,
        capture_output=True,
    )

    assert publish_risk.security_trigger_paths(tmp_path, base) == (path,)


def test_security_trigger_paths_detect_ownership_change_in_existing_integration(
    tmp_path: Path,
) -> None:
    path = "fastapi_backend/app/integrations/vendor/client.py"
    base = _security_repo(tmp_path, {path: "def map_row(row):\n    return row\n"})
    (tmp_path / path).write_text(
        "def map_row(row):\n    row.user_id = actor.id\n    return row\n",
        encoding="utf-8",
    )
    module.subprocess.run(["git", "-C", str(tmp_path), "add", path], check=True)
    module.subprocess.run(
        ["git", "-C", str(tmp_path), "commit", "-m", "change"],
        check=True,
        capture_output=True,
    )

    assert publish_risk.security_trigger_paths(tmp_path, base) == (path,)


def test_security_trigger_paths_detect_removed_auth_from_existing_server_action(
    tmp_path: Path,
) -> None:
    path = "nextjs-frontend/app/actions/article.ts"
    base = _security_repo(
        tmp_path,
        {
            path: (
                '"use server";\n\n'
                "export async function update() {\n"
                "  const user = await auth();\n"
                "  return user;\n"
                "}\n"
            )
        },
    )
    (tmp_path / path).write_text(
        '"use server";\n\n'
        "export async function update() {\n"
        "  return null;\n"
        "}\n",
        encoding="utf-8",
    )
    module.subprocess.run(["git", "-C", str(tmp_path), "add", path], check=True)
    module.subprocess.run(
        ["git", "-C", str(tmp_path), "commit", "-m", "change"],
        check=True,
        capture_output=True,
    )

    assert publish_risk.security_trigger_paths(tmp_path, base) == (path,)


# --- #4048: publication validates the body with the trusted-base checker


class _TrustedShowRunner:
    def __init__(self, *, checker_source: str | None) -> None:
        self.checker_source = checker_source
        self.calls: list[list[str]] = []

    def run(self, args, *, check=True, env=None):
        del check, env
        self.calls.append(list(args))
        assert args[:2] == ["git", "show"], args
        if self.checker_source is None:
            return SimpleNamespace(
                returncode=128, stdout="", stderr="fatal: path does not exist"
            )
        return SimpleNamespace(returncode=0, stdout=self.checker_source, stderr="")


def test_trusted_base_body_check_rejects_a_base_whose_checker_is_unavailable() -> None:
    """A missing object and legitimate absence are indistinguishable: fail closed."""
    runner = _TrustedShowRunner(checker_source=None)
    invoked: list[list[str]] = []

    with pytest.raises(
        module.PublicationError,
        match=r"checker unavailable at c{12}.*path does not exist.*fetch the pinned",
    ):
        module.validate_body_against_trusted_base(
            runner,
            trusted_base_head="c" * 40,
            body="anything",
            standalone=False,
            error_type=module.PublicationError,
            run_checker=lambda argv, _source: invoked.append(list(argv)),
        )

    assert invoked == []
    assert runner.calls == [
        ["git", "show", "c" * 40 + ":scripts/util/pr_body_check.py"]
    ]


def test_trusted_base_body_check_reports_the_base_checkers_failing_rule() -> None:
    runner = _TrustedShowRunner(checker_source="print('base checker')")
    seen: dict[str, object] = {}

    def fake_checker(argv, source):
        seen["argv"] = list(argv)
        seen["source"] = source
        seen["body"] = Path(argv[argv.index("--body-file") + 1]).read_text(
            encoding="utf-8"
        )
        return subprocess.CompletedProcess(
            list(argv),
            2,
            stdout="",
            stderr=(
                "usage: pr_body_check.py [-h] ...\n"
                "pr_body_check.py: error: Missing required '## Validation' section."
            ),
        )

    with pytest.raises(
        module.PublicationError,
        match=r"trusted-base contract at c{12}.*Missing required '## Validation'",
    ):
        module.validate_body_against_trusted_base(
            runner,
            trusted_base_head="c" * 40,
            body=VALID_BODY,
            standalone=True,
            error_type=module.PublicationError,
            run_checker=fake_checker,
        )

    argv = seen["argv"]
    assert seen["source"] == "print('base checker')"
    assert seen["body"] == VALID_BODY
    assert argv[:3] == [sys.executable, "-I", "-"]
    assert "--ready" in argv and "--standalone" in argv
    assert not Path(argv[argv.index("--body-file") + 1]).exists()


def test_trusted_base_body_check_runs_the_real_checker_source() -> None:
    """The base revision's checker executes from stdin, exactly as PR Lint would."""
    checker_source = (
        Path(module.__file__).resolve().parent / "pr_body_check.py"
    ).with_name("_body_check.py").read_text(encoding="utf-8")
    runner = _TrustedShowRunner(checker_source=checker_source)

    module.validate_body_against_trusted_base(
        runner,
        trusted_base_head="c" * 40,
        body=VALID_BODY,
        standalone=False,
        error_type=module.PublicationError,
    )

    with pytest.raises(module.PublicationError, match="trusted-base contract"):
        module.validate_body_against_trusted_base(
            runner,
            trusted_base_head="c" * 40,
            body="Closes #1",
            standalone=False,
            error_type=module.PublicationError,
        )


@pytest.mark.parametrize(
    "consumer",
    [
        pytest.param(
            "delivery_pipeline",
            marks=pytest.mark.skip(reason="(a) delivery pipeline is on the deletion list"),
        ),
        pytest.param(
            "finding_anchor",
            marks=pytest.mark.skip(reason="(a) consumer finding composition stays outside loopzero"),
        ),
        "loopzero.delivery.publish",
        "loopzero.delivery._publish_risk",
        "loopzero.review.harness",
    ],
)
def test_delivery_consumers_load_without_dispatcher_executable(consumer: str) -> None:
    """Fresh isolated imports must not transitively load the CLI facade."""
    probe = """
import importlib.abc
import sys

class NoDispatcher(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname == "agent_dispatch":
            raise AssertionError("consumer loaded the dispatcher executable")

sys.meta_path.insert(0, NoDispatcher())
sys.path.insert(0, sys.argv[1])
name = sys.argv[2]
__import__(name)
assert "agent_dispatch" not in sys.modules
"""
    result = subprocess.run(
        [
            sys.executable,
            "-I",
            "-c",
            probe,
            str(Path(__file__).resolve().parents[3] / "src"),
            consumer,
        ],
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
