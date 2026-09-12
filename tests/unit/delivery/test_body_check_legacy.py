import importlib
import json
import re
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest


def load_module() -> ModuleType:
    return importlib.import_module("loopzero.delivery._body_check")


module = load_module()


def context_body(trailer: str = "Closes #123") -> str:
    return (
        "## Context and goal\n\n"
        "- **Context:** Teammates use pull requests to understand a change.\n"
        "- **Problem:** The purpose can otherwise be unclear.\n"
        "- **Goal:** Make the intended outcome easy to understand.\n\n"
        + trailer
        + "\n\n## Validation\n\n- Focused contract tests passed.\n"
    )


def test_validate_context_and_goal_accepts_complete_plain_language_opening() -> None:
    assert module.validate_context_and_goal(context_body()) is None


def test_validate_context_and_goal_accepts_crlf_body() -> None:
    assert (
        module.validate_context_and_goal(context_body().replace("\n", "\r\n")) is None
    )


def test_validate_context_and_goal_ignores_leading_html_comment() -> None:
    body = "<!-- publishing metadata -->\n" + context_body()

    assert module.validate_context_and_goal(body) is None


def test_validate_context_and_goal_accepts_an_optional_matched_glossary() -> None:
    body = context_body().replace(
        "\n\nCloses #123\n\n## Validation",
        "\n\n### Glossary\n\n"
        "- **Pull requests:** Proposed changes awaiting review.\n\n"
        "Closes #123\n\n## Validation",
    )

    assert module.validate_context_and_goal(body) is None


def test_validate_context_and_goal_rejects_glossary_term_absent_from_opening() -> None:
    body = context_body().replace(
        "\n\nCloses #123\n\n## Validation",
        "\n\n### Glossary\n\n"
        "- **Semantic verifier:** A check of intended meaning.\n\n"
        "Closes #123\n\n## Validation",
    )

    assert "Semantic verifier" in str(module.validate_context_and_goal(body))


def test_validate_context_and_goal_rejects_glossary_term_found_only_inside_word() -> (
    None
):
    body = (
        context_body()
        .replace(
            "The purpose can otherwise be unclear.",
            "The appropriate purpose can otherwise be unclear.",
        )
        .replace(
            "\n\nCloses #123",
            "\n\n### Glossary\n\n"
            "- **PR:** A proposed change awaiting review.\n\n"
            "Closes #123",
        )
    )

    assert "PR" in str(module.validate_context_and_goal(body))


def test_validate_context_and_goal_does_not_take_required_fields_from_glossary() -> (
    None
):
    body = context_body().replace(
        "- **Goal:** Make the intended outcome easy to understand.\n\nCloses #123",
        "### Glossary\n\n- **Goal:** A desired outcome.\n\nCloses #123",
    )

    assert (
        module.validate_context_and_goal(body)
        == "Context and goal has missing or empty field(s): Goal"
    )


@pytest.mark.parametrize(
    ("body", "message"),
    [
        ("Closes #123", "Missing required"),
        ("Closes #123\n\n" + context_body(), "must be the first"),
        (
            context_body().replace(
                "- **Problem:** The purpose can otherwise be unclear.\n", ""
            ),
            "Problem",
        ),
        (
            context_body().replace(
                "- **Goal:** Make the intended outcome easy to understand.",
                "- **Goal:**",
            ),
            "Goal",
        ),
    ],
)
def test_validate_context_and_goal_rejects_missing_or_empty_fields(
    body: str, message: str
) -> None:
    assert message in str(module.validate_context_and_goal(body))


def test_validate_context_and_goal_ignores_fields_hidden_in_html_comments() -> None:
    body = (
        "## Context and goal\n\n<!--\n"
        "- **Context:** Hidden context.\n"
        "- **Problem:** Hidden problem.\n"
        "- **Goal:** Hidden goal.\n"
        "-->\n\nCloses #123"
    )

    assert "Context, Problem, Goal" in str(module.validate_context_and_goal(body))


def test_validate_context_and_goal_rejects_fields_hidden_in_unclosed_comment() -> None:
    body = (
        "## Context and goal\n\n<!--\n"
        "- **Context:** Hidden context.\n"
        "- **Problem:** Hidden problem.\n"
        "- **Goal:** Hidden goal.\n\n"
        "Closes #123"
    )

    assert "unclosed HTML comment" in str(module.validate_context_and_goal(body))


def test_validate_context_and_goal_rejects_fields_hidden_in_details() -> None:
    body = (
        "## Context and goal\n\n<details>\n<summary>Technical details</summary>\n\n"
        "- **Context:** Hidden context.\n"
        "- **Problem:** Hidden problem.\n"
        "- **Goal:** Hidden goal.\n\n"
        "</details>\n\nCloses #123"
    )

    assert "HTML elements" in str(module.validate_context_and_goal(body))


@pytest.mark.parametrize("fence", ["```", "~~~"])
def test_validate_context_and_goal_rejects_fields_hidden_in_code_fence(
    fence: str,
) -> None:
    body = (
        f"## Context and goal\n\n{fence}markdown\n"
        "- **Context:** Hidden context.\n"
        "- **Problem:** Hidden problem.\n"
        "- **Goal:** Hidden goal.\n"
        f"{fence}\n\nCloses #123"
    )

    assert "Markdown code fences" in str(module.validate_context_and_goal(body))


@pytest.mark.parametrize("indent", ["    ", "\t"])
def test_validate_context_and_goal_rejects_fields_hidden_in_indented_code_block(
    indent: str,
) -> None:
    body = (
        "## Context and goal\n\n"
        f"{indent}- **Context:** Hidden context.\n"
        f"{indent}- **Problem:** Hidden problem.\n"
        f"{indent}- **Goal:** Hidden goal.\n\n"
        "Closes #123"
    )

    assert "Markdown-indented" in str(module.validate_context_and_goal(body))


@pytest.mark.parametrize("value", ["&nbsp;", "&#8203;", "&#x200B;"])
def test_validate_context_and_goal_rejects_visually_empty_html_entity_value(
    value: str,
) -> None:
    body = context_body().replace(
        "Make the intended outcome easy to understand.",
        value,
    )

    assert "Goal" in str(module.validate_context_and_goal(body))


def test_validate_contract_rejects_unclosed_html_comment_before_hidden_content() -> (
    None
):
    body = "<!-- publishing metadata -->\n" + context_body().replace(
        "- **Context:**", "<!--\n- **Context:**", 1
    )

    assert "Unclosed HTML comment" in str(module.validate_contract(body))


@pytest.mark.parametrize(
    ("body", "message"),
    [
        (
            "```markdown\n" + context_body() + "```\n",
            "Context and goal",
        ),
        (
            context_body().replace(
                "## Validation\n\n- Focused contract tests passed.",
                "```markdown\n## Validation\n\n- Focused contract tests passed.\n```",
            ),
            "Validation",
        ),
        (
            context_body(trailer="`Closes #123`"),
            "No work reference found",
        ),
        (
            context_body(trailer="```text\nStandalone-Reason: hidden\n```"),
            "No work reference found",
        ),
    ],
)
def test_validate_contract_ignores_markdown_code_as_contract_content(
    body: str, message: str
) -> None:
    assert message in str(module.validate_contract(body))


def test_machine_readable_contract_names_required_and_optional_content() -> None:
    assert module.PR_BODY_CONTRACT["version"] == 1
    assert module.PR_BODY_CONTRACT["required_sections"] == (
        "Context and goal",
        "Validation",
    )
    assert module.PR_BODY_CONTRACT["required_context_fields"] == (
        "Context",
        "Problem",
        "Goal",
    )
    assert module.PR_BODY_CONTRACT["optional_sections"] == (
        "Glossary",
        "Decisions taken",
    )


def test_validate_contract_accepts_optional_sections_being_omitted() -> None:
    assert module.validate_contract(context_body()) is None


@pytest.mark.parametrize(
    ("body", "message"),
    [
        ("Closes #123\n\n## Validation\n\n- Passed.\n", "Context and goal"),
        (
            context_body().split("\n\n## Validation", 1)[0],
            "Missing required '## Validation' section",
        ),
        (
            context_body().replace(
                "## Validation\n\n- Focused contract tests passed.",
                "## Validation\n\n<!-- add validation -->\n-",
            ),
            "must contain a result",
        ),
        (
            context_body().replace(
                "## Validation\n\n- Focused contract tests passed.",
                "## Validation\n\n- <result>",
            ),
            "must contain a result",
        ),
        (
            context_body().replace(
                "## Validation\n\n- Focused contract tests passed.",
                "## Validation\n\n- TODO",
            ),
            "must contain a result",
        ),
        (
            context_body().replace(
                "## Validation\n\n- Focused contract tests passed.",
                "## Validation\n\n- not run",
            ),
            "must contain a result",
        ),
        (
            context_body().replace(
                "## Validation\n\n- Focused contract tests passed.",
                "## Validation\n\nResult: TODO",
            ),
            "must contain a result",
        ),
    ],
)
def test_validate_contract_rejects_missing_mandatory_content(
    body: str, message: str
) -> None:
    assert message in str(module.validate_contract(body))


def test_validate_contract_keeps_only_issue_linkage_optional_for_drafts() -> None:
    draft_body = context_body(trailer="No issue link yet")

    assert module.validate_contract(draft_body, draft=True) is None
    assert "No work reference found" in str(module.validate_contract(draft_body))
    assert "Context and goal" in str(module.validate_contract("Summary", draft=True))


@pytest.mark.parametrize(
    ("body", "message"),
    [
        (
            context_body().replace(
                "Make the intended outcome easy to understand.",
                "[](https://example.test/concealed-goal)",
            ),
            "Goal",
        ),
        (
            context_body().replace(
                "- Focused contract tests passed.",
                "- [](https://example.test/concealed-validation)",
            ),
            "must contain a result",
        ),
        (
            context_body().replace(
                "- Focused contract tests passed.",
                "- [][concealed-validation]\n\n"
                "[concealed-validation]: https://example.test/result",
            ),
            "must contain a result",
        ),
        (
            context_body().replace(
                "Make the intended outcome easy to understand.",
                "[](https://example.test/a(b(c)))",
            ),
            "Goal",
        ),
    ],
)
def test_validate_contract_rejects_content_only_in_markdown_link_destinations(
    body: str, message: str
) -> None:
    assert message in str(module.validate_contract(body))


def test_validate_contract_accepts_visible_markdown_link_labels() -> None:
    body = (
        context_body()
        .replace(
            "Make the intended outcome easy to understand.",
            "[Make the outcome clear](https://example.test/goal)",
        )
        .replace(
            "Focused contract tests passed.",
            "[Focused tests passed](https://example.test/validation)",
        )
    )

    assert module.validate_contract(body) is None


def test_validate_accepts_reference_variants_and_exemptions() -> None:
    assert module.validate("Closes #123") is None
    assert module.validate("Refs #42") is None
    assert module.validate("Fixes #7") is None
    assert module.validate("Reverts #2630") is None
    assert module.validate("No issue yet", draft=True) is None
    assert module.validate("Deliberately standalone", standalone=True) is None
    assert module.validate("Standalone-Reason: recursive component audit") is None
    assert module.validate("  standalone-reason:\tCI-only maintenance") is None


def test_validate_rejects_missing_reference() -> None:
    assert module.validate("Validation and screenshots only") is not None
    assert module.validate("UI prefs #12 are not a policy keyword") is not None
    assert module.validate("Standalone-Reason:") is not None
    assert module.validate("Standalone-Reason:   \nNo issue") is not None
    assert module.validate("<!-- Closes #123 -->") is not None
    assert module.validate("<!-- Standalone-Reason: hidden -->") is not None


_LEGACY_ISSUE_REFERENCE = re.compile(
    r"(^|[^A-Za-z0-9_])"
    r"(close[sd]?|fix(e[sd])?|resolve[sd]?|refs?|revert(s|ed|ing)?)"
    r"[ \t]+#[0-9]+",
    re.IGNORECASE | re.MULTILINE,
)
_LEGACY_STANDALONE = re.compile(
    r"^[ \t]*Standalone-Reason:[ \t]+\S", re.IGNORECASE | re.MULTILINE
)


def _legacy_two_step_accepts(body: str, *, standalone: bool = False) -> bool:
    """Model the removed shell precheck followed by the canonical checker."""
    shell_accepts = bool(
        standalone
        or _LEGACY_STANDALONE.search(body)
        or _LEGACY_ISSUE_REFERENCE.search(body)
    )
    return (
        shell_accepts and module.validate_contract(body, standalone=standalone) is None
    )


@pytest.mark.parametrize(
    ("body", "standalone"),
    [
        (context_body("Closes #123"), False),
        (context_body("Closed #123"), False),
        (context_body("Fix #7"), False),
        (context_body("Fixed #7"), False),
        (context_body("Resolves #42"), False),
        (context_body("Refs #42"), False),
        (context_body("Reverting #2630"), False),
        (context_body("Standalone-Reason: bounded CI maintenance"), False),
        (context_body("No issue reference"), True),
        (context_body("No issue reference"), False),
        (context_body("<!-- Closes #123 -->"), False),
        (context_body("```text\nCloses #123\n```"), False),
        (context_body("`Closes #123`"), False),
        (context_body("Standalone-Reason:"), False),
        (context_body("<!-- Standalone-Reason: hidden -->"), False),
        ("Closes #123", False),
        ("## Context and goal\n\n- **Context:** malformed", False),
        ("<!-- unclosed\n" + context_body(), False),
    ],
)
def test_python_only_body_contract_matches_legacy_two_step_outcome(
    body: str, standalone: bool
) -> None:
    python_only_accepts = module.validate_contract(body, standalone=standalone) is None

    assert _legacy_two_step_accepts(body, standalone=standalone) is python_only_accepts


@pytest.mark.skip(reason="(a) consumer workflow composition stays outside loopzero")
def test_ci_workflow_runs_trusted_pr_body_checker() -> None:
    repo_root = Path(__file__).resolve().parents[4]
    workflow = (repo_root / ".github" / "workflows" / "pr-lint.yml").read_text()

    assert "  pr-policy:" in workflow
    assert "name: PR Policy" in workflow
    assert "ref: ${{ github.event.pull_request.base.sha }}" in workflow
    assert "scripts/util/pr_body_check.py" in workflow
    assert "python3 scripts/util/pr_body_check.py" in workflow
    assert 'standalone_args+=("--standalone")' in workflow
    assert '--body-file /tmp/pr-body.txt "${standalone_args[@]}"' in workflow
    assert "grep -qiE" not in workflow


@pytest.mark.skip(reason="(a) consumer workflow composition stays outside loopzero")
def test_ci_workflow_pins_every_checkout_to_the_trusted_digest() -> None:
    repo_root = Path(__file__).resolve().parents[4]
    workflow = (repo_root / ".github" / "workflows" / "pr-lint.yml").read_text()
    pinned_revision = "d23441a48e516b6c34aea4fa41551a30e30af803"
    checkout_revisions = re.findall(r"actions/checkout@([^\s]+)", workflow)

    assert checkout_revisions == [pinned_revision]
    assert workflow.count("persist-credentials: false") == 1


@pytest.mark.skip(reason="(a) consumer workflow composition stays outside loopzero")
def test_ci_workflow_matches_standalone_label_by_exact_array_membership() -> None:
    repo_root = Path(__file__).resolve().parents[4]
    workflow = (repo_root / ".github" / "workflows" / "pr-lint.yml").read_text()
    exact_membership = (
        "${{ contains(github.event.pull_request.labels.*.name, 'standalone') }}"
    )
    assert workflow.count(exact_membership) == 1
    assert "join(github.event.pull_request.labels.*.name, ',')" not in workflow
    assert 'if [ "${HAS_STANDALONE_LABEL}" = "true" ]; then' in workflow


@pytest.mark.skip(reason="(a) consumer workflow composition stays outside loopzero")
def test_ci_workflow_admits_only_ready_and_semantically_relevant_events() -> None:
    repo_root = Path(__file__).resolve().parents[4]
    workflow = (repo_root / ".github" / "workflows" / "pr-lint.yml").read_text()

    assert "github.event.pull_request.draft == false" in workflow
    assert "github.event.action != 'labeled'" in workflow
    assert "github.event.action != 'unlabeled'" in workflow
    assert "github.event.label.name == 'standalone'" in workflow
    assert "closed" not in workflow.split("permissions:", 1)[0]
    assert "ci-final" not in workflow
    assert "gate-merged" not in workflow


@pytest.mark.skip(reason="(a) consumer workflow composition stays outside loopzero")
def test_ci_workflow_reads_pr_files_once_and_never_executes_the_head() -> None:
    repo_root = Path(__file__).resolve().parents[4]
    workflow = (repo_root / ".github" / "workflows" / "pr-lint.yml").read_text()

    assert workflow.count("pulls/${PR_NUMBER}/files") == 1
    assert "--paginate --slurp" in workflow
    assert "git checkout" not in workflow
    assert "git switch" not in workflow
    assert "github.event.pull_request.head.sha" in workflow
    assert "python3 scripts/util/check_obligation_changes.py" in workflow
    assert "pip install" not in workflow
    assert "uv sync" not in workflow


@pytest.mark.skip(reason="(a) consumer workflow composition stays outside loopzero")
def test_ci_workflow_aggregates_all_policy_outcomes() -> None:
    repo_root = Path(__file__).resolve().parents[4]
    workflow = (repo_root / ".github" / "workflows" / "pr-lint.yml").read_text()

    assert workflow.count("continue-on-error: true") == 3
    assert "if: always()" in workflow
    assert "PR body" in workflow
    assert "Visual evidence" in workflow
    assert "Obligation change" in workflow
    assert "not applicable" in workflow


def _github_client(tmp_path: Path, payload: dict, captured: list[list[str]]):
    subprocess.run(["/usr/bin/git", "init", "-q", str(tmp_path)], check=True)
    subprocess.run(
        [
            "/usr/bin/git",
            "-C",
            str(tmp_path),
            "remote",
            "add",
            "origin",
            "https://github.com/example/project.git",
        ],
        check=True,
    )

    def fake_run(command, **_kwargs):
        captured.append(command)
        if command[-1] == "version":
            return subprocess.CompletedProcess(
                command, 0, "gh version 2.40.1 (test)\n", ""
            )
        return subprocess.CompletedProcess(command, 0, json.dumps(payload), "")

    return module.GitHub(
        tmp_path,
        module.GitHubSettings(labels={}, retries=0),
        run=fake_run,
    )


def test_load_pr_reads_live_policy_fields_through_github_integration(tmp_path) -> None:
    payload = {
        "body": "Issue-less by design",
        "draft": False,
        "labels": [{"name": "standalone"}],
        "base": {"sha": "a" * 40},
        "head": {"sha": "b" * 40},
    }

    captured: list[list[str]] = []
    github = _github_client(tmp_path, payload, captured)

    assert module.load_pr("123", github=github) == (
        "Issue-less by design",
        False,
        True,
        "a" * 40,
        "b" * 40,
    )
    assert captured[0][1:] == ["version"]
    assert captured[1][1:] == [
        "api", "--method", "GET", "repos/example/project/pulls/123"
    ]


def test_load_pr_rejects_url_outside_repository_binding(tmp_path) -> None:
    payload = {
        "body": "Issue-less by design",
        "draft": False,
        "labels": [{"name": "standalone"}],
        "base": {"sha": "a" * 40},
        "head": {"sha": "b" * 40},
    }
    captured: list[list[str]] = []
    github = _github_client(tmp_path, payload, captured)

    with pytest.raises(ValueError, match="cannot load PR metadata"):
        module.load_pr(
            "https://github.com/other/project/pull/123", github=github
        )
    assert captured == []


def test_ready_check_does_not_keep_draft_exemption(monkeypatch, capsys) -> None:
    monkeypatch.setattr(
        module,
        "load_pr",
        lambda _pr: (
            context_body("No reference yet"),
            True,
            False,
            "a" * 40,
            "b" * 40,
        ),
    )

    try:
        module.main(["--pr", "123", "--ready"])
    except SystemExit as exc:
        assert exc.code == 2
    else:
        raise AssertionError("ready policy unexpectedly kept the draft exemption")

    assert "No work reference found" in capsys.readouterr().err


def test_ready_cli_requires_context_and_goal(tmp_path: Path, capsys) -> None:
    body = tmp_path / "pr.md"
    body.write_text("Closes #123\n", encoding="utf-8")

    with pytest.raises(SystemExit, match="2"):
        module.main(["--body-file", str(body)])

    assert "Missing required '## Context and goal' opening" in capsys.readouterr().err


def test_draft_cli_still_requires_the_body_contract(tmp_path: Path, capsys) -> None:
    body = tmp_path / "pr.md"
    body.write_text("Draft summary only\n", encoding="utf-8")

    with pytest.raises(SystemExit, match="2"):
        module.main(["--body-file", str(body), "--draft"])

    assert "Missing required '## Context and goal' opening" in capsys.readouterr().err


@pytest.mark.skip(reason="(a) consumer product prose stays outside loopzero")
def test_supported_direct_draft_workflow_validates_before_create() -> None:
    repo_root = Path(__file__).resolve().parents[4]
    guide = (
        repo_root / "docs" / "guides" / "dev-workflow" / "backlog-workflow.md"
    ).read_text(encoding="utf-8")

    validation = guide.index("pr_body_check.py --body-file")
    creation = guide.index("gh pr create --draft --body-file")
    assert validation < creation
    assert 'gh pr create --body "Closes #' not in guide


@pytest.mark.skip(reason="(a) consumer product prose stays outside loopzero")
def test_template_marks_only_contract_sections_as_required() -> None:
    repo_root = Path(__file__).resolve().parents[4]
    template = (repo_root / ".github" / "pull_request_template.md").read_text(
        encoding="utf-8"
    )

    assert "Validation is mandatory" in template
    assert "Decisions taken is optional" in template
    assert "\n## Decisions taken\n" not in module.HTML_COMMENT_RE.sub("", template)


@pytest.mark.skip(reason="(a) consumer Makefile composition stays outside loopzero")
def test_make_pr_body_check_uses_local_range_only_for_body_files() -> None:
    repo_root = Path(__file__).resolve().parents[4]
    makefile = (repo_root / "Makefile").read_text(encoding="utf-8")
    recipe = makefile.split("pr-body-check:", 1)[1].split("\n\n", 1)[0]

    assert (
        '$(if $(PR),,--base "$(if $(BASE),$(BASE),origin/main)" '
        '--head "$(if $(HEAD),$(HEAD),HEAD)")' in recipe
    )


def test_pr_cli_checks_obligations_against_live_pr_oids(monkeypatch) -> None:
    base_oid = "a" * 40
    head_oid = "b" * 40
    calls: list[tuple[str, str, str]] = []
    checker = ModuleType("check_obligation_changes")

    class ObligationCheckError(RuntimeError):
        pass

    checker.ObligationCheckError = ObligationCheckError
    checker.check_obligation_changes = (
        lambda base, head, *, acknowledgment_text: calls.append(
            (base, head, acknowledgment_text)
        )
        or 0
    )
    monkeypatch.setitem(sys.modules, "check_obligation_changes", checker)
    monkeypatch.setattr(
        module,
        "load_pr",
        lambda _pr: (context_body(), False, False, base_oid, head_oid),
    )
    materialized: list[tuple[str, str, str]] = []
    monkeypatch.setattr(
        module,
        "materialize_pr_commits",
        lambda pr, base, head: materialized.append((pr, base, head)),
    )

    assert module.main(["--pr", "123"]) == 0
    assert materialized == [("123", base_oid, head_oid)]
    assert calls == [(base_oid, head_oid, context_body())]


def test_materialize_pr_commits_fetches_missing_live_objects(monkeypatch) -> None:
    base_oid = "a" * 40
    head_oid = "b" * 40
    commands: list[list[str]] = []
    available = {base_oid: False, head_oid: False}

    class Completed:
        returncode = 0

    def fake_run(command, **_kwargs):
        commands.append(command)
        if command[:3] == ["git", "cat-file", "-e"]:
            oid = command[3].removesuffix("^{commit}")
            result = Completed()
            result.returncode = 0 if available[oid] else 1
            return result
        assert command == [
            "git",
            "fetch",
            "--no-tags",
            "--quiet",
            "origin",
            base_oid,
            "refs/pull/123/head",
        ]
        available[base_oid] = True
        available[head_oid] = True
        return Completed()

    monkeypatch.setattr(module.subprocess, "run", fake_run)

    module.materialize_pr_commits("123", base_oid, head_oid)

    assert commands[-1] == ["git", "cat-file", "-e", f"{head_oid}^{{commit}}"]


def test_obligation_git_failure_is_reported_as_contract_error(
    monkeypatch, tmp_path: Path, capsys
) -> None:
    body = tmp_path / "pr.md"
    body.write_text(context_body(), encoding="utf-8")
    checker = ModuleType("check_obligation_changes")

    class ObligationCheckError(RuntimeError):
        pass

    checker.ObligationCheckError = ObligationCheckError
    checker.check_obligation_changes = lambda *_args, **_kwargs: (_ for _ in ()).throw(
        ObligationCheckError("cannot resolve merge base")
    )
    monkeypatch.setitem(sys.modules, "check_obligation_changes", checker)

    with pytest.raises(SystemExit, match="2"):
        module.main(
            [
                "--body-file",
                str(body),
                "--base",
                "origin/main",
                "--head",
                "HEAD",
            ]
        )

    assert "cannot resolve merge base" in capsys.readouterr().err
