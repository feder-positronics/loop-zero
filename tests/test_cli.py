"""cli.py end to end: real git with a bare origin, fake `gh`, `claude`, `codex` and `bwrap`."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from loopzero import cli, mergify
from loopzero.types import CheckReport, CheckResult, ReviewResult
from tests.conftest import git
from tests.test_github import FakeGh, pr_json, threads_json
from tests.test_runners import APPROVE, CHANGES, _claude_envelope, _fake_claude, _fake_codex
from tests.test_sandbox import FAKE_BWRAP

REPO = "acme/widgets"
URL = f"https://github.com/{REPO}/pull/7"
LOGIN = "lz-bot"
WORKFLOW = f"""
[repo]
name = "{REPO}"
base = "main"

[checks]
commands = ["echo ok", "test -f README.md"]
required_ci = ["checks"]

[delivery]
merge = "squash"
reviewers = ["claude", "codex"]
"""


@pytest.fixture
def repo(git_repo: Path, tmp_path: Path, fake_tool, fake_bin: Path) -> Path:
    """Repo with workflow.toml committed, a bare origin holding main, and a fake bwrap."""
    (git_repo / "workflow.toml").write_text(WORKFLOW)
    git(git_repo, "add", "workflow.toml")
    git(git_repo, "commit", "-q", "-m", "config")
    origin = tmp_path / "origin.git"
    git(git_repo, "init", "-q", "--bare", "-b", "main", str(origin))
    git(git_repo, "remote", "add", "origin", str(origin))
    git(git_repo, "push", "-q", "origin", "main")
    fake_tool("bwrap", FAKE_BWRAP.format(log=tmp_path / "bwrap.argv"))
    return git_repo


@pytest.fixture
def gh(fake_bin: Path, tmp_path: Path) -> FakeGh:
    fake = FakeGh(fake_bin, tmp_path)
    fake.respond("api user", {"login": LOGIN})
    fake.respond(f"api repos/{REPO}/pulls/7/files",
                 [{"filename": "feature.py", "patch": "@@ -0,0 +1 @@\n+print('hi')"}])
    return fake


@pytest.fixture
def wt(repo: Path, monkeypatch: pytest.MonkeyPatch, capsys) -> Path:
    """A started worktree with one commit on top of base; cwd is inside it."""
    monkeypatch.chdir(repo)
    assert cli.main(["start", "t1"]) == 0
    path = Path(capsys.readouterr().out.strip())
    assert path == repo / ".worktrees" / "t1"
    (path / "feature.py").write_text("print('hi')\n")
    task = path / ".loopzero" / "task.md"
    task.write_text(
        task.read_text()
        .replace(
            "<one or two sentences: where this sits, what exists today>",
            "The feature module is part of the sample application.",
        )
        .replace("<what is wrong or missing, observable>", "The feature is missing.")
        .replace("<what will be true when done>", "The feature prints its greeting.")
    )
    git(path, "add", "feature.py")
    git(path, "commit", "-q", "-m", "add feature")
    monkeypatch.chdir(path)
    return path


def head_of(path: Path) -> str:
    return git(path, "rev-parse", "HEAD").strip()


def marker(head: str, kind: str, source: str = "model") -> str:
    return cli.review_marker(head, kind, source)


def test_review_marker_parser_accepts_old_and_v1_forms() -> None:
    old = f"<!-- loopzero:review head={'a' * 40} kind=primary -->"
    new = cli.review_marker("b" * 40, "delta")
    assert cli.REVIEW_MARKER_RE.search(old).groups() == ("a" * 40, "primary")
    assert cli.REVIEW_MARKER_RE.search(new).groups() == ("b" * 40, "delta")


def rev(head: str, kind: str, *, state: str = "APPROVED", commit: str | None = None,
        login: str = LOGIN, verdict: str = "approve") -> dict:
    """A review object as `gh api .../reviews` returns it, carrying a loopzero marker."""
    return {"body": f"{marker(head, kind)}\nreview: **{verdict}**", "state": state,
            "commit_id": commit or head, "user": {"login": login}}


def reviews_key(page: int = 1) -> str:
    return f"api repos/{REPO}/pulls/7/reviews?per_page=100&page={page}"


def post_key() -> str:
    return f"api repos/{REPO}/pulls/7/reviews"


def arm_readiness(
    gh: FakeGh, head: str, wt: Path, *, threads=(), conclusion: str = "success",
    local_exit: int = 0, dirty: bool = False,
) -> None:
    gh.respond("api graphql", threads_json(*threads))
    gh.respond(f"api repos/{REPO}/commits/{head}/check-runs", {"check_runs": [
        {"name": "checks", "status": "completed", "conclusion": conclusion}]})
    gh.respond(f"api repos/{REPO}/commits/{head}/statuses", [])
    cli._save_report(
        wt, CheckReport(head, dirty, (CheckResult("echo ok", local_exit, 0.1, ""),))
    )


def run(capsys, *argv: str) -> tuple[int, str, str]:
    code = cli.main(list(argv))
    captured = capsys.readouterr()
    return code, captured.out, captured.err


# --- start / check --------------------------------------------------------------------


def test_start_prints_only_worktree_path(repo: Path, monkeypatch, capsys) -> None:
    monkeypatch.chdir(repo)
    code, out, err = run(capsys, "start", "hello")
    assert (code, err) == (0, "")
    assert out == f"{repo / '.worktrees' / 'hello'}\n"
    assert (repo / ".worktrees" / "hello" / ".loopzero" / "task.md").exists()


def test_start_rejects_bad_slug_on_stderr(repo: Path, monkeypatch, capsys) -> None:
    monkeypatch.chdir(repo)
    code, out, err = run(capsys, "start", "../x")
    assert code == 1 and out == "" and err.startswith("loopzero start: invalid slug")


def test_start_without_config(git_repo: Path, monkeypatch, capsys) -> None:
    monkeypatch.chdir(git_repo)
    code, _, err = run(capsys, "start", "x")
    assert code == 1 and "workflow.toml" in err and err.count("\n") == 1


def test_check_pass_writes_report(wt: Path, capsys) -> None:
    code, out, _ = run(capsys, "check")
    lines = out.splitlines()
    assert code == 0 and lines[-1] == "PASS"
    assert lines[0].startswith("checks pinned to origin/main@")
    assert lines[1].startswith("exit 0") and lines[1].endswith("  echo ok")
    assert lines[2].endswith("  test -f README.md")
    report = json.loads((wt / ".loopzero" / "checks.json").read_text())
    assert report["head"] == head_of(wt)
    assert report["dirty"] is False
    assert [(r["command"], r["exit_code"]) for r in report["results"]] == [
        ("echo ok", 0), ("test -f README.md", 0)]
    assert report["results"][0]["tail"] == "ok"


def test_check_with_open_pr_patches_validation_section(
    wt: Path, gh: FakeGh, capsys
) -> None:
    git(wt, "push", "-q", "-u", "origin", "lz/t1")
    head = head_of(wt)
    arm_pr(gh, head)
    gh.respond(f"api repos/{REPO}/pulls/7", {})

    code, out, err = run(capsys, "check")

    assert code == 0 and out.endswith("PASS\n") and err == ""
    assert [call["argv"][:2] for call in gh.calls] == [
        ["pr", "list"], ["api", f"repos/{REPO}/pulls/7"],
    ]
    body = json.loads(gh.calls[1]["--input"])["body"]
    assert body.count("## Validation") == 1
    assert f"Recorded for `{head[:12]}`" in body
    assert "- `echo ok`: exit 0" in body


@pytest.mark.parametrize("newline", ["\n", "\r\n"])
@pytest.mark.parametrize("command", ["check", "pr"])
def test_refresh_preserves_live_pr_evidence(
    wt: Path, gh: FakeGh, capsys, command: str, newline: str,
) -> None:
    git(wt, "push", "-q", "-u", "origin", "lz/t1")
    live = (
        "# Owner-edited title\n\n## Acceptance\n- Updated acceptance on GitHub.\n\n"
        "## Validation\nRecorded for `aaaaaaaaaaaa`\n"
        "- `old check`: exit 0 (1.0s)\n\n"
        "Browser: original failure reproduced; corrected image returns 200.\n\n"
        "## Review\n- primary, claude, aaaaaaaaaaaa, request_changes\n"
        "- delta, claude, bbbbbbbbbbbb, approve\n\n"
        "## Evidence\nProduction probe returned 100 records.\n"
    )
    gh.respond("pr list", [pr_json(headRefOid=head_of(wt), body=live.replace("\n", newline))])
    gh.respond(f"api repos/{REPO}/pulls/7", {})
    assert run(capsys, command)[0] == 0
    body = json.loads(gh.calls[-1]["--input"])["body"]
    if command == "check":
        assert body.startswith("# Owner-edited title\n\n## Acceptance\n")
        assert "- Updated acceptance on GitHub." in body
    else:
        assert "The feature prints its greeting." in body
        assert "Updated acceptance on GitHub" not in body
    assert "Browser: original failure reproduced; corrected image returns 200." in body
    assert "- primary, claude, aaaaaaaaaaaa, request_changes" in body
    assert "- delta, claude, bbbbbbbbbbbb, approve" in body
    assert "## Evidence\nProduction probe returned 100 records." in body
    assert "old check" not in body and "Recorded for `aaaaaaaaaaaa`" not in body


def test_initial_pr_keeps_human_validation(wt: Path, gh: FakeGh, capsys) -> None:
    task = wt / ".loopzero" / "task.md"
    task.write_text(task.read_text().replace(
        "(filled by `loopzero check`)", "- Regression failed before the fix.\n- Browser verified."
    ))
    gh.respond("pr list", [], [pr_json(headRefOid=head_of(wt))])
    gh.respond("pr create", URL + "\n")
    assert run(capsys, "pr")[0] == 0
    body = next(c["--body-file"] for c in gh.calls if "--body-file" in c)
    assert "- Regression failed before the fix." in body
    assert "- Browser verified." in body
    assert "(no `loopzero check` run recorded)" in body


def test_repeated_pr_does_not_rewrite_unchanged_body(wt: Path, gh: FakeGh, capsys) -> None:
    gh.respond("pr list", [pr_json(headRefOid=head_of(wt))])
    gh.respond(f"api repos/{REPO}/pulls/7", {})
    assert run(capsys, "pr")[0] == 0
    body = json.loads(gh.calls[-1]["--input"])["body"]
    gh.respond("pr list", [pr_json(headRefOid=head_of(wt), body=body)])
    before = len(gh.calls)
    assert run(capsys, "pr")[0] == 0
    assert [c["argv"][:2] for c in gh.calls[before:]] == [["pr", "list"]]


def test_check_does_not_rewrite_when_only_duration_changes(wt: Path, gh: FakeGh, capsys) -> None:
    git(wt, "push", "-q", "-u", "origin", "lz/t1")
    report = CheckReport(head_of(wt), False, (
        CheckResult("echo ok", 0, 999.0, "ok"),
        CheckResult("test -f README.md", 0, 999.0, ""),
    ))
    cli._save_report(wt, report)
    gh.respond("pr list", [pr_json(headRefOid=head_of(wt))])
    gh.respond(f"api repos/{REPO}/pulls/7", {})
    assert run(capsys, "pr")[0] == 0
    body = json.loads(gh.calls[-1]["--input"])["body"]
    gh.respond("pr list", [pr_json(headRefOid=head_of(wt), body=body)])
    before = len(gh.calls)
    assert run(capsys, "check")[0] == 0
    assert [c["argv"][:2] for c in gh.calls[before:]] == [["pr", "list"]]


@pytest.mark.parametrize("body", [
    "# PR\n\n## Evidence\nRetained browser proof.\n",
    "# PR\n\n## Validation\nHuman evidence.\n\n## Review\nApproved.\n",
    ("# PR\n\n## Checks\nRecorded for `aaaaaaaaaaaa`\n"
     "- `old check`: exit 1 (1.0s)\n\nHuman evidence.\n\n## Review\nApproved.\n"),
])
def test_check_refresh_replaces_previous_head_without_losing_evidence(
    wt: Path, gh: FakeGh, capsys, body: str,
) -> None:
    git(wt, "push", "-q", "-u", "origin", "lz/t1")
    gh.respond("pr list", [pr_json(headRefOid=head_of(wt), body=body)])
    gh.respond(f"api repos/{REPO}/pulls/7", {})
    assert run(capsys, "check")[0] == 0
    first = json.loads(gh.calls[-1]["--input"])["body"]
    old_head = head_of(wt)
    (wt / "feature.py").write_text("print('new')\n")
    git(wt, "add", "feature.py")
    git(wt, "commit", "-q", "-m", "new behavior")
    gh.respond("pr list", [pr_json(headRefOid=head_of(wt), body=first)])
    assert run(capsys, "check")[0] == 0
    updated = json.loads(gh.calls[-1]["--input"])["body"]
    assert old_head[:12] not in updated and head_of(wt)[:12] in updated
    assert updated.count("Recorded for") == 1
    for text in ["Retained browser proof.", "Human evidence.", "Approved."]:
        if text in body:
            assert text in updated


@pytest.mark.parametrize("command,exit_code", [("check", 0), ("pr", 1)])
def test_malformed_live_markers_do_not_mask_check_verdict(
    wt: Path, gh: FakeGh, capsys, command: str, exit_code: int,
) -> None:
    git(wt, "push", "-q", "-u", "origin", "lz/t1")
    body = "# PR\n\n## Validation\n<!-- loopzero:checks:start -->\nHuman evidence.\n"
    gh.respond("pr list", [pr_json(headRefOid=head_of(wt), body=body)])
    code, out, err = run(capsys, command)
    assert code == exit_code and "malformed loopzero checks block" in err
    assert all(c["argv"][:2] == ["pr", "list"] for c in gh.calls)
    if command == "check":
        assert out.endswith("PASS\n")
        assert json.loads((wt / ".loopzero/checks.json").read_text())["results"][0]["exit_code"] == 0


def test_check_without_pr_does_not_call_gh(wt: Path, gh: FakeGh, capsys) -> None:
    assert run(capsys, "check")[0] == 0
    assert gh.calls == []


def test_check_fail_exits_one(wt: Path, capsys) -> None:
    (wt / "workflow.toml").write_text(WORKFLOW.replace('"echo ok"', '"echo no; exit 3"'))
    code, out, err = run(capsys, "--config", str(wt / "workflow.toml"), "check")
    assert code == 1 and out.splitlines()[-1] == "FAIL"
    assert out.splitlines()[0] == "checks are unpinned because --config was provided"
    assert out.splitlines()[1].startswith("exit 3")
    assert "README.md" not in out
    assert err == "--- echo no; exit 3 (exit 3) ---\nno\n", "failing tail goes to stderr"
    report = json.loads((wt / ".loopzero" / "checks.json").read_text())
    assert [result["command"] for result in report["results"]] == ["echo no; exit 3"]


def test_check_failure_tail_is_capped_at_40_lines(wt: Path, capsys) -> None:
    cmd = "seq 1 50; exit 1"
    (wt / "workflow.toml").write_text(WORKFLOW.replace('"echo ok", "test -f README.md"', f'"{cmd}"'))
    code, _, err = run(capsys, "--config", str(wt / "workflow.toml"), "check")
    lines = err.splitlines()
    assert code == 1 and lines[0] == f"--- {cmd} (exit 1) ---"
    assert lines[1:] == [str(i) for i in range(11, 51)]


def test_check_offline_fetch_failure_explains_cache_remedy(
    wt: Path, repo: Path, capsys
) -> None:
    workflow = WORKFLOW.replace(
        'commands = ["echo ok", "test -f README.md"]',
        'commands = ["echo uv: Failed to fetch package; exit 1"]',
    ).replace("[delivery]", '[checks.env]\nUV_CACHE_DIR = "/host/cache/uv"\n\n[delivery]')
    (repo / "workflow.toml").write_text(workflow)
    git(repo, "add", "workflow.toml")
    git(repo, "commit", "-q", "-m", "offline check")
    git(repo, "push", "-q", "origin", "main")

    code, _, err = run(capsys, "check")

    assert code == 1
    tail, hint = err.split("\n\n", 1)
    assert tail.endswith("uv: Failed to fetch package")
    assert "`[checks] network = false`" in hint
    assert "effective UV_CACHE_DIR inside the sandbox is /host/cache/uv" in hint
    assert "`[checks] writable` is warm" in hint
    assert "set `[checks] env` UV_CACHE_DIR" in hint
    assert "network = true" not in hint


def test_check_sandbox_unavailable_exits_two(wt: Path, fake_tool, capsys) -> None:
    fake_tool("bwrap", "echo 'bwrap: No permissions' >&2\nexit 1\n")
    code, out, err = run(capsys, "check")
    assert code == 2 and out.startswith("checks pinned to origin/main@") and "No permissions" in err
    assert not (wt / ".loopzero" / "checks.json").exists()


def test_config_flag_overrides_root(wt: Path, tmp_path: Path, capsys) -> None:
    alt = tmp_path / "alt.toml"
    alt.write_text(WORKFLOW.replace('"echo ok", "test -f README.md"', '"echo alt"'))
    code, out, _ = run(capsys, "--config", str(alt), "check")
    assert code == 0 and "echo alt" in out and "README" not in out
    assert out.startswith("checks are unpinned because --config was provided\n")


def test_check_uses_base_commands_when_worktree_weakens_them(wt: Path, capsys) -> None:
    (wt / "workflow.toml").write_text(
        WORKFLOW.replace('"echo ok", "test -f README.md"', '"true"')
    )

    code, out, err = run(capsys, "check")

    assert code == 0 and err == ""
    assert "  echo ok" in out and "  test -f README.md" in out
    assert "  true" not in out


def test_check_uses_worktree_checks_when_base_has_no_workflow(
    wt: Path, repo: Path, capsys
) -> None:
    (wt / "workflow.toml").write_text(
        WORKFLOW.replace('"echo ok", "test -f README.md"', '"echo worktree"')
    )
    git(repo, "rm", "-q", "workflow.toml")
    git(repo, "commit", "-q", "-m", "remove workflow")
    git(repo, "push", "-q", "origin", "main")

    code, out, err = run(capsys, "check")

    assert code == 0 and err == ""
    assert "  echo worktree" in out
    assert not out.startswith("checks pinned")


def test_check_uses_task_base_when_remote_ref_is_missing(wt: Path, capsys) -> None:
    git(wt, "update-ref", "-d", "refs/remotes/origin/main")

    code, out, err = run(capsys, "check")

    assert code == 0 and err == ""
    assert "  echo ok" in out and out.startswith("checks pinned to origin/main@")


def test_primary_review_diffs_from_merge_base_not_base_tip(
    wt: Path, repo: Path, gh: FakeGh, fake_bin: Path, capsys
) -> None:
    (repo / "upstream.py").write_text("upstream = 1\n")
    git(repo, "add", "upstream.py")
    git(repo, "commit", "-q", "-m", "upstream change")
    git(repo, "push", "-q", "origin", "main")
    git(wt, "fetch", "-q", "origin")
    head = head_of(wt)
    arm_pr(gh, head)
    gh.respond(reviews_key(), [])
    gh.respond(post_key(), {"id": 4})
    _fake_claude(fake_bin, _claude_envelope(APPROVE))

    code, _, _ = run(capsys, "review")

    prompt = (fake_bin / "claude.stdin").read_text()
    assert code == 0 and "upstream = 1" not in prompt


def test_check_fails_when_base_workflow_is_malformed(
    wt: Path, repo: Path, capsys
) -> None:
    (repo / "workflow.toml").write_text("[checks\n")
    git(repo, "add", "workflow.toml")
    git(repo, "commit", "-q", "-m", "break workflow")
    git(repo, "push", "-q", "origin", "main")

    code, out, err = run(capsys, "check")

    assert code == 1 and out == ""
    assert "origin/main:workflow.toml: invalid TOML" in err


# --- pr ------------------------------------------------------------------------------------


def test_pr_pushes_and_creates_draft(
    wt: Path, repo: Path, gh: FakeGh, fake_tool, tmp_path: Path, capsys
) -> None:
    run(capsys, "check")
    head = head_of(wt)
    gh.respond("pr list", [], [pr_json(headRefOid=head)])
    gh.respond("pr create", URL + "\n")
    pushes = tmp_path / "pushes"
    fake_tool("git", f'''\
if [ "$1" = "push" ]; then printf '%s\\n' "$*" >> "{pushes}"; fi
exec /usr/bin/git "$@"
''')
    code, out, err = run(capsys, "pr")
    assert (code, out, err) == (0, URL + "\n", "")
    assert pushes.read_text().strip() == "push -u origin lz/t1"
    origin = repo.parent / "origin.git"
    assert git(origin, "rev-parse", "lz/t1").strip() == head
    create = next(c for c in gh.calls if c["argv"][:2] == ["pr", "create"])
    assert "--draft" in create["argv"]
    heading = (wt / ".loopzero" / "task.md").read_text().splitlines()[0].lstrip("# ")
    assert create["argv"][create["argv"].index("--title") + 1] == heading
    body = create["--body-file"]
    assert "## Context and goal" in body and git(repo, "rev-parse", "origin/main").strip() in body
    assert body.count("## Validation") == 1 and f"Recorded for `{head[:12]}`" in body
    assert "(filled by `loopzero check`)" not in body, "placeholder replaced in place"
    assert body.index("## Validation") < body.index("## Review") < body.index("## Notes")
    assert "- `echo ok`: exit 0" in body and "- `test -f README.md`: exit 0" in body


def test_pr_title_prefers_human_heading(wt: Path, gh: FakeGh, capsys) -> None:
    task = wt / ".loopzero" / "task.md"
    task.write_text("## Objective\nx\n\n# Add the feature flag\n")
    assert cli._pr_title(wt, "lz/t1") == "Add the feature flag"
    task.write_text("## Objective\nonly sections\n")
    assert cli._pr_title(wt, "lz/t1") == "lz/t1"


@pytest.mark.parametrize("name,value", [
    ("Context", ""),
    ("Problem", "<what is wrong or missing, observable>"),
    ("Goal", "<what will be true when done>"),
])
def test_pr_refuses_missing_context_problem_or_goal(
    wt: Path, gh: FakeGh, capsys, name: str, value: str
) -> None:
    task = wt / ".loopzero" / "task.md"
    task.write_text(re.sub(
        rf"^- \*\*{name}:\*\*.*$", f"- **{name}:** {value}", task.read_text(),
        flags=re.MULTILINE,
    ))

    code, out, err = run(capsys, "pr")

    assert code == 1 and out == "" and f"- **{name}:**" in err
    assert gh.calls == []


def test_pr_migrates_old_checks_heading_to_validation(wt: Path) -> None:
    task = wt / ".loopzero" / "task.md"
    task.write_text(task.read_text().replace("## Validation", "## Checks"))

    body = cli._pr_body(wt, head_of(wt))

    assert "## Checks" not in body
    assert body.count("## Validation") == 1
    assert "(no `loopzero check` run recorded)" in body


def test_pr_updates_existing_open_pr(wt: Path, gh: FakeGh, capsys) -> None:
    gh.respond("pr list", [pr_json(headRefOid=head_of(wt))])
    gh.respond(f"api repos/{REPO}/pulls/7", {})
    code, out, _ = run(capsys, "pr")
    assert code == 0 and out == URL + "\n"
    assert [c["argv"][:2] for c in gh.calls] == [
        ["pr", "list"], ["api", f"repos/{REPO}/pulls/7"]
    ]
    assert "(no `loopzero check` run recorded)" in gh.calls[1]["--input"]


def test_pr_uses_lease_for_rewritten_open_pr(
    wt: Path, repo: Path, gh: FakeGh, fake_tool, tmp_path: Path, capsys
) -> None:
    old = head_of(wt)
    git(wt, "push", "-q", "-u", "origin", "lz/t1")
    git(wt, "reset", "-q", "--soft", "HEAD~1")
    git(wt, "commit", "-q", "-m", "rebased feature")
    head = head_of(wt)
    pushes = tmp_path / "pushes"
    fake_tool("git", f'''\
if [ "$1" = "push" ]; then printf '%s\\n' "$*" >> "{pushes}"; fi
exec /usr/bin/git "$@"
''')
    gh.respond("pr list", [pr_json(headRefOid=old)])
    gh.respond(f"api repos/{REPO}/pulls/7", {})

    code, out, err = run(capsys, "pr")

    assert (code, out, err) == (0, URL + "\n", "")
    assert pushes.read_text().strip() == (
        f"push -u origin --force-with-lease=lz/t1:{old} lz/t1"
    )
    assert git(repo.parent / "origin.git", "rev-parse", "refs/heads/lz/t1").strip() == head


def test_pr_reports_rejected_lease_without_overwriting_remote(
    wt: Path, repo: Path, gh: FakeGh, tmp_path: Path, capsys
) -> None:
    old = head_of(wt)
    origin = repo.parent / "origin.git"
    git(wt, "push", "-q", "-u", "origin", "lz/t1")
    git(wt, "reset", "-q", "--soft", "HEAD~1")
    git(wt, "commit", "-q", "-m", "rebased feature")
    mover = tmp_path / "mover"
    git(tmp_path, "clone", "-q", str(origin), str(mover))
    git(mover, "checkout", "-q", "-b", "lz/t1", "origin/lz/t1")
    (mover / "remote.txt").write_text("moved\n")
    git(mover, "add", "remote.txt")
    git(mover, "commit", "-q", "-m", "remote move")
    git(mover, "push", "-q", "origin", "lz/t1")
    moved = git(origin, "rev-parse", "refs/heads/lz/t1").strip()
    gh.respond("pr list", [pr_json(headRefOid=old)])

    code, out, err = run(capsys, "pr")

    assert code == 1 and out == "" and "remote moved" in err
    assert git(origin, "rev-parse", "refs/heads/lz/t1").strip() == moved


def test_pr_refuses_to_overwrite_a_pr_head_it_has_never_seen(
    wt: Path, repo: Path, gh: FakeGh, capsys
) -> None:
    origin = repo.parent / "origin.git"
    git(wt, "push", "-q", "-u", "origin", "lz/t1")
    pushed = git(origin, "rev-parse", "refs/heads/lz/t1").strip()
    gh.respond("pr list", [pr_json(headRefOid="f" * 40)])  # head pushed by someone else

    code, out, err = run(capsys, "pr")

    assert code == 1 and out == "" and "is not in this worktree" in err
    assert git(origin, "rev-parse", "refs/heads/lz/t1").strip() == pushed


def test_version_names_the_installed_revision(capsys) -> None:
    with pytest.raises(SystemExit) as stop:
        cli.main(["--version"])
    out = capsys.readouterr().out
    assert stop.value.code == 0 and re.fullmatch(r"loopzero \S+ .+\n", out), out


def test_pr_refuses_dirty(wt: Path, gh: FakeGh, capsys) -> None:
    (wt / "scratch.txt").write_text("x")
    code, out, err = run(capsys, "pr")
    assert code == 1 and out == "" and "uncommitted or untracked" in err
    assert gh.calls == []


# --- review ---------------------------------------------------------------------------


def arm_pr(gh: FakeGh, head: str, **over) -> None:
    body = "# T1\n\n## Review\n(filled by `loopzero review`)\n\n## Notes\n"
    gh.respond("pr list", [pr_json(headRefOid=head, body=body, **over)])
    gh.respond(f"api repos/{REPO}/pulls/7", {})


def test_review_primary_posts_marker(wt: Path, gh: FakeGh, fake_bin: Path, capsys) -> None:
    head = head_of(wt)
    arm_pr(gh, head)
    gh.respond(reviews_key(), [])
    gh.respond(post_key(), {"id": 1})
    _fake_claude(fake_bin, _claude_envelope(CHANGES))
    code, out, err = run(capsys, "review")
    assert (code, err) == (5, "")  # request_changes: the exit code is the verdict
    assert out == (
        f"primary review by claude on {head[:12]}: request_changes "
        "(0 critical, 1 important, 1 suggestion)\n"
    )
    post = next(c for c in gh.calls if c["argv"][1] == post_key().split(" ")[1])
    payload = json.loads(post["--input"])
    assert payload["commit_id"] == head and payload["event"] == "REQUEST_CHANGES"
    assert marker(head, "primary") in payload["body"]
    prompt = (fake_bin / "claude.stdin").read_text()
    assert "+print('hi')" in prompt and "primary review" in prompt and "## Context and goal" in prompt
    updated = json.loads(gh.calls[-1]["--input"])["body"]
    assert f"- primary, claude, {head[:12]}, request_changes" in updated


@pytest.mark.parametrize(
    ("options", "model", "effort"),
    [((), "opus", "medium"), (("--model", "sonnet", "--effort", "high"), "sonnet", "high")],
    ids=["configured", "overridden"],
)
def test_review_passes_and_records_model_effort(
    wt: Path, gh: FakeGh, fake_bin: Path, capsys, options, model: str, effort: str
) -> None:
    workflow = wt / "workflow.toml"
    workflow.write_text(workflow.read_text() + """
[delivery.review.claude]
model = "opus"
effort = "medium"
allowed_efforts = ["medium", "high"]
""")
    git(wt, "add", "workflow.toml")
    git(wt, "commit", "-q", "-m", "configure review")
    head = head_of(wt)
    arm_pr(gh, head)
    gh.respond(reviews_key(), [])
    gh.respond(post_key(), {"id": 1})
    _fake_claude(fake_bin, _claude_envelope(APPROVE))

    code, _, err = run(capsys, "review", *options)

    assert (code, err) == (0, "")
    argv = (fake_bin / "claude.argv").read_text().split("\0")
    assert argv[argv.index("--model") + 1] == model
    assert argv[argv.index("--effort") + 1] == effort
    saved = json.loads((wt / ".loopzero" / f"review-{head[:12]}-primary.json").read_text())
    assert (saved["model"], saved["effort"]) == (model, effort)
    payload = json.loads(next(c["--input"] for c in gh.calls if c["argv"][1] == post_key()[4:]))
    assert f"model {model} (effort {effort})" in payload["body"]


def test_codex_config_is_recorded_without_banner(
    wt: Path, gh: FakeGh, fake_bin: Path, capsys
) -> None:
    workflow = wt / "workflow.toml"
    text = workflow.read_text().replace(
        'reviewers = ["claude", "codex"]', 'reviewers = ["codex"]'
    )
    workflow.write_text(text + """
[delivery.review.codex]
model = "gpt-5.6-luna"
effort = "high"
allowed_efforts = ["high"]
""")
    git(wt, "add", "workflow.toml")
    git(wt, "commit", "-q", "-m", "configure codex review")
    head = head_of(wt)
    arm_pr(gh, head)
    gh.respond(reviews_key(), [])
    gh.respond(post_key(), {"id": 1})
    _fake_codex(fake_bin, json.dumps(APPROVE))

    code, _, err = run(capsys, "review")

    assert (code, err) == (0, "")
    saved = json.loads((wt / ".loopzero" / f"review-{head[:12]}-primary.json").read_text())
    assert (saved["model"], saved["effort"]) == ("gpt-5.6-luna", "high")
    payload = json.loads(next(c["--input"] for c in gh.calls if c["argv"][1] == post_key()[4:]))
    assert "model gpt-5.6-luna (effort high)" in payload["body"]


@pytest.mark.parametrize("options,configured", [((), "max"), (("--effort", "max"), "medium")])
def test_review_rejects_disallowed_effort_before_launch(
    wt: Path, gh: FakeGh, fake_bin: Path, capsys, options, configured: str
) -> None:
    workflow = wt / "workflow.toml"
    workflow.write_text(workflow.read_text() + f"""
[delivery.review.claude]
effort = "{configured}"
allowed_efforts = ["medium"]
""")
    git(wt, "add", "workflow.toml")
    git(wt, "commit", "-q", "-m", "configure review")
    head = head_of(wt)
    arm_pr(gh, head)
    gh.respond(reviews_key(), [])

    code, out, err = run(capsys, "review", *options)

    assert code == 1 and out == "" and "effort 'max' is not allowed for claude" in err
    assert not (fake_bin / "claude.argv").exists()


def test_review_refuses_when_reviewer_dirties_worktree(
    wt: Path, gh: FakeGh, fake_bin: Path, capsys
) -> None:
    head = head_of(wt)
    arm_pr(gh, head)
    gh.respond(reviews_key(), [])
    reviewer = _fake_claude(fake_bin, _claude_envelope(APPROVE))
    reviewer.write_text(reviewer.read_text().replace("#!/bin/sh\n", "#!/bin/sh\ntouch drift.txt\n"))

    code, out, err = run(capsys, "review")

    saved = wt / ".loopzero" / f"review-{head[:12]}-primary.json"
    assert code == 1 and out == "" and "worktree changed during review" in err
    assert err.count(head) == 2 and not saved.exists()
    assert all(call["argv"][:2] != ["api", post_key().split(" ")[1]] for call in gh.calls)


@pytest.mark.parametrize("padding", ["", "x" * 574_264], ids=["short", "long"])
def test_review_saves_result_and_repost_skips_model(wt: Path, gh: FakeGh, fake_bin: Path,
                                                    capsys, padding: str) -> None:
    head = head_of(wt)
    arm_pr(gh, head)
    gh.respond(reviews_key(), [])
    gh.fail(post_key(), "HTTP 500 server error")
    envelope = {"padding": padding,
                **_claude_envelope(CHANGES, modelUsage={"claude-sonnet-4-6": {}})}
    _fake_claude(fake_bin, envelope)
    saved = wt / ".loopzero" / f"review-{head[:12]}-primary.json"
    code, out, err = run(capsys, "review")
    assert code == 1 and out == "" and saved.exists()
    assert f"review saved at {saved}" in err and "loopzero review --repost" in err
    saved_bytes = saved.read_bytes()
    data = json.loads(saved_bytes)
    assert json.loads(data["raw"]) == envelope
    assert data["head"] == head and data["kind"] == "primary" and data["family"] == "claude"
    assert data["verdict"] == "request_changes" and len(data["findings"]) == 2
    assert data["model"] == "claude-sonnet-4-6" and data["duration_s"] >= 0
    first_body = json.loads(gh.calls[-1]["--input"])["body"]
    assert "model claude-sonnet-4-6" in first_body
    assert len(first_body.encode("utf-8")) < 65_536
    assert ("Transcript truncated" in first_body) == bool(padding)
    (fake_bin / "claude.stdin").unlink()
    gh.respond(post_key(), {"id": 5})
    code, out, err = run(capsys, "review", "--repost")
    assert (code, err) == (5, "") and out.startswith("primary review by claude"), err
    assert not (fake_bin / "claude.stdin").exists(), "no model invoked"
    payload = json.loads(next(
        c["--input"] for c in reversed(gh.calls)
        if c["argv"][1] == post_key().split(" ")[1]
    ))
    assert marker(head, "primary", "repost") in payload["body"] and "Nit" in payload["body"]
    assert "claude-session-123" in payload["body"]
    assert len(payload["body"].encode("utf-8")) < 65_536
    assert ("Transcript truncated" in payload["body"]) == bool(padding)
    assert saved.read_bytes() == saved_bytes
    assert "model claude-sonnet-4-6" in payload["body"]
    assert [c["body"].split("\n")[-1] for c in payload["comments"]] == ["Off by one."]


def test_repost_refuses_missing_or_stale_file(wt: Path, gh: FakeGh, fake_bin: Path,
                                              capsys) -> None:
    head = head_of(wt)
    arm_pr(gh, head)
    gh.respond(reviews_key(), [])
    code, _, err = run(capsys, "review", "--repost")
    assert code == 1 and "no saved primary review" in err
    saved = wt / ".loopzero" / f"review-{head[:12]}-primary.json"
    saved.write_text(json.dumps({"family": "claude", "head": "0" * 40, "kind": "primary",
                                 "verdict": "approve", "findings": [], "raw": ""}))
    code, _, err = run(capsys, "review", "--repost")
    assert code == 1 and f"not HEAD {head[:12]}" in err
    assert all(c["argv"][1] != post_key().split(" ")[1] for c in gh.calls)


def test_repost_refuses_hand_written_review(wt: Path, gh: FakeGh, capsys) -> None:
    head = head_of(wt)
    arm_pr(gh, head)
    gh.respond(reviews_key(), [])
    saved = wt / ".loopzero" / f"review-{head[:12]}-primary.json"
    saved.write_text(json.dumps({
        "family": "claude", "head": head, "kind": "primary", "verdict": "approve",
        "findings": [], "raw": json.dumps(_claude_envelope(APPROVE)),
    }))

    code, out, err = run(capsys, "review", "--repost")

    assert code == 1 and out == ""
    assert "not runner-produced" in err
    assert all(c["argv"][1] != post_key().split(" ")[1] for c in gh.calls)


def test_review_chunks_oversized_diff_with_same_family(
    wt: Path, gh: FakeGh, fake_tool, capsys
) -> None:
    workflow = (wt / "workflow.toml").read_text().replace(
        'reviewers = ["claude", "codex"]',
        'reviewers = ["claude", "codex"]\nreview_chunk_bytes = 160',
    )
    (wt / "workflow.toml").write_text(workflow)
    for name in ("large_a.py", "large_b.py"):
        (wt / name).write_text("x = 1\n" * 30)
    git(wt, "add", "workflow.toml", "large_a.py", "large_b.py")
    git(wt, "commit", "-q", "-m", "large review")
    head = head_of(wt)
    arm_pr(gh, head)
    gh.respond(reviews_key(), [])
    gh.respond(post_key(), {"id": 8})
    envelope = json.dumps(_claude_envelope(APPROVE))
    fake_tool("claude", f"""
        input=$(mktemp)
        cat > "$input"
        if [ "$(grep -c '^diff --git ' "$input")" -gt 1 ]; then
            echo 'Prompt is too long' >&2
            exit 1
        fi
        echo '{envelope}'
    """)

    code, out, err = run(capsys, "review")

    assert code == 0 and "review by claude" in out
    assert "reviewing" in err and "chunks" in err
    payload = json.loads(next(
        c["--input"] for c in reversed(gh.calls)
        if c["argv"][1] == post_key().split(" ")[1]
    ))
    assert "Reviewed in " in payload["body"] and " chunks" in payload["body"]
    saved = json.loads(
        (wt / ".loopzero" / f"review-{head[:12]}-primary.json").read_text()
    )
    assert saved["chunk_count"] > 1
    assert len(saved["provenance"]["session_ids"]) == saved["chunk_count"]


def test_review_without_independent_family_exits_four_and_repost_cannot_bypass(
    wt: Path, gh: FakeGh, capsys
) -> None:
    workflow = (wt / "workflow.toml").read_text().replace(
        'reviewers = ["claude", "codex"]', 'reviewers = ["claude"]'
    )
    (wt / "workflow.toml").write_text(workflow)
    git(wt, "add", "workflow.toml")
    git(
        wt, "commit", "-q", "-m",
        "claude-authored\n\nCo-Authored-By: Claude <noreply@anthropic.com>",
    )
    head = head_of(wt)
    arm_pr(gh, head)
    gh.respond(reviews_key(), [])

    code, out, err = run(capsys, "review")

    assert code == 4 and out == ""
    assert "author family: claude" in err and "excluded families: claude" in err
    assert "configure another family, log in" in err

    code, out, err = run(capsys, "review", "--repost")
    assert code == 4 and out == "" and "--repost does not apply" in err


def test_review_delta_diffs_since_primary(wt: Path, gh: FakeGh, fake_bin: Path, capsys) -> None:
    first = head_of(wt)
    (wt / "second.py").write_text("x = 2\n")
    git(wt, "add", "second.py")
    git(wt, "commit", "-q", "-m", "second")
    head = head_of(wt)
    arm_pr(gh, head)
    gh.respond(reviews_key(), [rev(first, "primary", state="CHANGES_REQUESTED", verdict="request_changes")])
    gh.respond(post_key(), {"id": 2})
    _fake_claude(fake_bin, _claude_envelope(APPROVE))
    code, out, _ = run(capsys, "review")
    assert code == 0 and out.startswith(f"delta review by claude on {head[:12]}: approve")
    prompt = (fake_bin / "claude.stdin").read_text()
    assert "+x = 2" in prompt and "print('hi')" not in prompt and "delta review" in prompt
    payload = json.loads(next(
        c["--input"] for c in reversed(gh.calls)
        if c["argv"][1] == post_key().split(" ")[1]
    ))
    assert marker(head, "delta") in payload["body"]


def test_review_appends_line_after_existing_review(wt: Path, gh: FakeGh, fake_bin, capsys) -> None:
    head = head_of(wt)
    prior = "- primary, codex, 123456789abc, request_changes"
    body = f"# T1\n\n## Review\n{prior}\n\n## Notes\n"
    gh.respond("pr list", [pr_json(headRefOid=head, body=body)])
    gh.respond(f"api repos/{REPO}/pulls/7", {})
    gh.respond(reviews_key(), [])
    gh.respond(post_key(), {"id": 3})
    _fake_claude(fake_bin, _claude_envelope(APPROVE))

    assert run(capsys, "review")[0] == 0

    updated = json.loads(gh.calls[-1]["--input"])["body"]
    assert prior in updated
    assert f"- primary, claude, {head[:12]}, approve" in updated
    assert updated.index(prior) < updated.index(head[:12]) < updated.index("## Notes")


def test_review_ignores_markers_outside_lineage(wt: Path, gh: FakeGh, fake_bin, capsys) -> None:
    head = head_of(wt)
    arm_pr(gh, head)
    gh.respond(reviews_key(), [rev("f" * 40, "primary"),
                               rev("e" * 40, "delta")])
    gh.respond(post_key(), {"id": 3})
    _fake_claude(fake_bin, _claude_envelope(APPROVE))
    code, out, _ = run(capsys, "review")
    assert code == 0 and out.startswith("primary review")


def test_review_refuses_when_budget_exhausted(wt: Path, gh: FakeGh, fake_bin, capsys) -> None:
    base = git(wt, "rev-parse", "HEAD~1").strip()
    head = head_of(wt)
    arm_pr(gh, head)
    gh.respond(reviews_key(), [rev(base, "primary"), rev(head, "delta")])
    _fake_claude(fake_bin, _claude_envelope(APPROVE))
    code, out, err = run(capsys, "review")
    assert code == 1 and out == "" and "review budget exhausted" in err
    assert not (fake_bin / "claude.stdin").exists()


@pytest.mark.parametrize("publisher", [LOGIN, "OTHER"])
def test_review_refuses_same_head_twice(wt: Path, gh: FakeGh, capsys, publisher) -> None:
    (wt / "workflow.toml").write_text(WORKFLOW + '\nreview_publishers = ["lz-bot", "other"]\n')
    git(wt, "add", "workflow.toml")
    git(wt, "commit", "-qm", "configure trusted publishers")
    git(wt, "update-ref", "refs/remotes/origin/main", "HEAD")
    head = head_of(wt)
    arm_pr(gh, head)
    gh.respond(reviews_key(), [rev(head, "primary", login=publisher)])
    code, _, err = run(capsys, "review")
    assert code == 1 and "already has a primary review" in err


def test_review_refuses_without_pr(wt: Path, gh: FakeGh, capsys) -> None:
    gh.respond("pr list", [])
    code, out, err = run(capsys, "review")
    assert code == 1 and out == "" and "no pull request for lz/t1" in err


def test_review_refuses_retargeted_pr(wt: Path, gh: FakeGh, fake_bin: Path, capsys) -> None:
    arm_pr(gh, head_of(wt), baseRefName="release")

    code, out, err = run(capsys, "review")

    assert code == 1 and out == ""
    assert "PR targets release, configured base is main" in err
    assert not (fake_bin / "claude.stdin").exists()


def test_review_refuses_dirty_and_unpushed(wt: Path, gh: FakeGh, capsys) -> None:
    (wt / "scratch.txt").write_text("x")
    code, _, err = run(capsys, "review")
    assert code == 1 and "uncommitted" in err and gh.calls == []
    (wt / "scratch.txt").unlink()
    arm_pr(gh, "0" * 40)
    code, _, err = run(capsys, "review")
    assert code == 1 and "push or pull first" in err


def test_review_falls_through_when_preferred_family_fails(
    wt: Path, gh: FakeGh, fake_bin: Path, fake_tool, capsys
) -> None:
    git(wt, "commit", "-q", "--allow-empty", "-m", "by claude\n\nCo-Authored-By: Claude <n@a.c>")
    head = head_of(wt)
    arm_pr(gh, head)
    gh.respond(reviews_key(), [])
    gh.respond(post_key(), {"id": 4})
    _fake_claude(fake_bin, _claude_envelope(APPROVE))
    fake_tool("codex", "echo 'Please run codex login' >&2\nexit 1\n")  # RunnerAuthFailed
    code, out, err = run(capsys, "review")
    assert code == 4 and out == ""
    assert "codex: CLI is not authenticated" in err


def test_review_fails_with_three_line_errors_and_remedies_without_spending_budget(
    wt: Path, gh: FakeGh, capsys, monkeypatch
) -> None:
    head = head_of(wt)
    arm_pr(gh, head)
    gh.respond(reviews_key(), [])

    def fail(family, **kwargs):
        if family == "claude":
            raise cli.runners.RunnerAuthFailed(
                "claude: CLI is not authenticated\nfirst detail\nsecond detail\nnot printed"
            )
        raise cli.runners.RunnerMissing("codex: 'codex' not found on PATH")

    monkeypatch.setattr(cli.runners, "review_with", fail)
    code, out, err = run(capsys, "review")
    assert code == 4 and out == ""
    assert "no independent reviewer can run" in err
    assert "author family: unknown" in err and "excluded families: none" in err
    assert "configure another family, log in" in err
    assert "claude: CLI is not authenticated\nfirst detail\nsecond detail" in err
    assert "not printed" not in err
    assert "Fix: run `claude auth login`." in err
    assert "codex: 'codex' not found on PATH" in err
    assert "npm install -g @openai/codex" in err
    assert all(c["argv"][1] != post_key().split(" ")[1] for c in gh.calls)


def test_review_reports_missing_bwrap_like_check(
    wt: Path, gh: FakeGh, fake_bin: Path, capsys, monkeypatch
) -> None:
    head = head_of(wt)
    arm_pr(gh, head)
    gh.respond(reviews_key(), [])
    _fake_claude(fake_bin, _claude_envelope(APPROVE))
    original = cli.runners.shutil.which
    monkeypatch.setattr(
        cli.runners.shutil, "which", lambda name: None if name == "bwrap" else original(name)
    )
    code, out, err = run(capsys, "review")
    assert (code, out) == (2, "")
    assert err == "sandbox unavailable: bwrap not found on PATH (install bubblewrap)\n"


def test_review_falls_back_after_timeout(wt: Path, gh: FakeGh, capsys, monkeypatch) -> None:
    head = head_of(wt)
    arm_pr(gh, head)
    gh.respond(reviews_key(), [])
    gh.respond(post_key(), {"id": 4})

    def review_with(family, **kwargs):
        if family == "claude":
            raise cli.runners.RunnerBadOutput("claude: timed out", "partial")
        return ReviewResult("codex", head, "primary", "approve", (), "{}")

    monkeypatch.setattr(cli.runners, "review_with", review_with)
    code, out, err = run(capsys, "review")
    assert code == 0 and out.startswith("primary review by codex")
    assert "claude: timed out" in err


# --- ready / merge / status ----------------------------------------------------------


def test_ready_marks_draft_ready(wt: Path, gh: FakeGh, capsys) -> None:
    head = head_of(wt)
    arm_pr(gh, head)
    gh.respond(reviews_key(), [rev(head, "primary", state="APPROVED")])
    arm_readiness(gh, head, wt)
    gh.respond("pr ready", "")
    code, out, err = run(capsys, "ready")
    assert (code, err) == (0, "") and out == f"ready: {URL}\n"
    assert ["pr", "ready", "7", "--repo", REPO] in [c["argv"] for c in gh.calls]


def test_ready_not_ready_lists_reasons(wt: Path, gh: FakeGh, capsys) -> None:
    head = head_of(wt)
    arm_pr(gh, head)
    gh.respond(reviews_key(), [])
    arm_readiness(gh, head, wt, conclusion="failure")
    code, out, _ = run(capsys, "ready")
    assert code == 1
    assert out.splitlines() == [
        "not ready: no review recorded for the current head",
        "not ready: required check 'checks' is failure",
    ]
    assert all(c["argv"][:2] != ["pr", "ready"] for c in gh.calls)


def test_ready_lists_retargeted_pr_reason(wt: Path, gh: FakeGh, capsys) -> None:
    head = head_of(wt)
    arm_pr(gh, head, baseRefName="release")
    gh.respond(reviews_key(), [rev(head, "primary")])
    arm_readiness(gh, head, wt)

    code, out, err = run(capsys, "ready")

    assert (code, err) == (1, "")
    assert out == "not ready: PR targets release, configured base is main\n"


def test_ready_uses_base_required_ci_when_worktree_empties_it(
    wt: Path, gh: FakeGh, capsys
) -> None:
    (wt / "workflow.toml").write_text(WORKFLOW.replace('["checks"]', "[]"))
    head = head_of(wt)
    arm_pr(gh, head, isDraft=False)
    gh.respond(reviews_key(), [rev(head, "primary")])
    arm_readiness(gh, head, wt, conclusion="failure")

    code, out, err = run(capsys, "ready")

    assert (code, err) == (1, "")
    assert out == "not ready: required check 'checks' is failure\n"


def test_pending_required_check_names_the_wait(wt: Path, gh: FakeGh, capsys) -> None:
    head = head_of(wt)
    arm_pr(gh, head, isDraft=False)
    gh.respond(reviews_key(), [rev(head, "primary")])
    arm_readiness(gh, head, wt)
    gh.respond(f"api repos/{REPO}/commits/{head}/check-runs",
               {"check_runs": [{"name": "checks", "status": "in_progress"}]})

    code, out, err = run(capsys, "ready")

    assert (code, err) == (1, "")
    assert out.splitlines() == [
        "not ready: required check 'checks' is pending",
        "not ready: required checks pending or missing; next: loopzero ready --wait",
    ]

    arm_pr(gh, head, isDraft=False)
    arm_readiness(gh, head, wt)
    gh.respond(f"api repos/{REPO}/commits/{head}/check-runs",
               {"check_runs": [{"name": "checks", "status": "in_progress"}]})
    code, out, err = run(capsys, "merge")

    assert (code, out) == (1, "")
    assert "required check 'checks' is pending; next: loopzero ready --wait" in err


def test_ready_marks_draft_with_skipped_required_check_then_waits(
    wt: Path, gh: FakeGh, capsys
) -> None:
    head = head_of(wt)
    arm_pr(gh, head)
    gh.respond(reviews_key(), [rev(head, "primary")])
    arm_readiness(gh, head, wt, conclusion="skipped")
    gh.respond("pr ready", "")

    code, out, err = run(capsys, "ready")

    assert (code, err) == (3, "")
    assert out.splitlines() == [
        "marked ready; waiting for required checks: checks",
        "waiting for required checks; next: loopzero ready --wait",
    ]
    assert ["pr", "ready", "7", "--repo", REPO] in [c["argv"] for c in gh.calls]


def test_ready_waits_for_checks_on_original_head(
    wt: Path, gh: FakeGh, capsys, monkeypatch
) -> None:
    head = head_of(wt)
    arm_pr(gh, head)
    gh.respond(reviews_key(), [rev(head, "primary")])
    arm_readiness(gh, head, wt)
    gh.respond(f"api repos/{REPO}/commits/{head}/check-runs",
               {"check_runs": []},
               {"check_runs": [{"name": "checks", "status": "in_progress"}]},
               {"check_runs": [{"name": "checks", "status": "completed",
                                  "conclusion": "success"}]})
    gh.respond(f"api repos/{REPO}/commits/{'f' * 40}/check-runs",
               {"check_runs": [{"name": "checks", "conclusion": "success"}]})
    gh.respond("pr ready", "")
    monkeypatch.setattr(cli, "_sleep", lambda _: None)

    code, out, err = run(capsys, "ready", "--wait")

    assert (code, err) == (0, "") and out.endswith(f"ready: {URL}\n")
    assert "required check 'checks' is pending" in out
    assert all("f" * 40 not in " ".join(c["argv"]) for c in gh.calls)
    assert sum(c["argv"][:2] == ["pr", "ready"] for c in gh.calls) == 1


def test_ready_wait_timeout_is_exit_three(wt: Path, gh: FakeGh, capsys) -> None:
    head = head_of(wt)
    arm_pr(gh, head, isDraft=False)
    gh.respond(reviews_key(), [rev(head, "primary")])
    arm_readiness(gh, head, wt, conclusion="pending")

    code, out, err = run(capsys, "ready", "--wait=0")

    assert (code, err) == (3, "")
    assert f"timed out waiting for required checks on {head[:12]}" in out


def test_ready_wait_stops_on_failed_check(wt: Path, gh: FakeGh, capsys) -> None:
    head = head_of(wt)
    arm_pr(gh, head, isDraft=False)
    gh.respond(reviews_key(), [rev(head, "primary")])
    arm_readiness(gh, head, wt, conclusion="pending")
    gh.respond(f"api repos/{REPO}/commits/{head}/check-runs",
               {"check_runs": [{"name": "checks", "status": "in_progress"}]},
               {"check_runs": [{"name": "checks", "status": "completed",
                                  "conclusion": "failure"}]})

    code, out, err = run(capsys, "ready", "--wait")

    assert (code, err) == (1, "")
    assert out == "not ready: required check 'checks' is failure\n"


def test_ready_wait_stops_if_pr_head_moves(wt: Path, gh: FakeGh, capsys) -> None:
    head = head_of(wt)
    body = "# T1\n\n## Review\n"
    gh.respond("pr list", [pr_json(headRefOid=head, body=body, isDraft=False)],
               [pr_json(headRefOid="f" * 40, body=body, isDraft=False)])
    gh.respond(reviews_key(), [rev(head, "primary")])
    arm_readiness(gh, head, wt, conclusion="pending")

    code, out, err = run(capsys, "ready", "--wait")

    assert code == 1 and out == ""
    assert f"PR head changed while waiting: {head[:12]} to {'f' * 12}" in err


def test_ready_wait_reports_a_check_run_that_never_started(
    wt: Path, gh: FakeGh, capsys, monkeypatch
) -> None:
    """#181: a required check still missing after the grace period is not a plain timeout."""
    head = head_of(wt)
    arm_pr(gh, head, isDraft=False)
    gh.respond(reviews_key(), [rev(head, "primary")])
    arm_readiness(gh, head, wt)
    gh.respond(f"api repos/{REPO}/commits/{head}/check-runs", {"check_runs": []})
    monkeypatch.setattr(cli, "MISSING_RUN_GRACE", 0.0)

    code, out, err = run(capsys, "ready", "--wait")

    assert code == 1 and "waiting:" in out and "GitHub created no run for this head" in err
    assert "gh pr close 7" in err and "gh pr reopen 7" in err


def test_wait_keeps_waiting_when_a_sibling_required_check_is_running(
    monkeypatch, tmp_path: Path
) -> None:
    """A missing check next to a pending one means CI is alive: time out, do not abort."""
    (tmp_path / "workflow.toml").write_text(WORKFLOW.replace('["checks"]', '["a", "b"]'))
    config = cli.config_mod.load(tmp_path / "workflow.toml")
    pr = cli.github.PR(7, URL, "a" * 40, "main", False, "OPEN", "MERGEABLE")
    reasons = (f"required check 'a' missing on {'a' * 12}", "required check 'b' is pending")
    monkeypatch.setattr(cli, "_require_pr", lambda *a, **k: pr)
    monkeypatch.setattr(cli, "_readiness", lambda *a, **k: cli.github.Readiness(False, reasons))
    monkeypatch.setattr(cli.worktree, "branch", lambda wt: "lz/t1")
    monkeypatch.setattr(cli, "MISSING_RUN_GRACE", 0.0)

    assert cli._wait_for_checks(Path("."), config, pr, 0.0, kicked=False) is None


def test_ready_wait_tolerates_a_late_required_job_of_a_running_workflow(
    wt: Path, gh: FakeGh, capsys, monkeypatch
) -> None:
    """#189: the required job has no check run until its `needs` finish; CI is alive."""
    head = head_of(wt)
    arm_pr(gh, head, isDraft=False)
    gh.respond(reviews_key(), [rev(head, "primary")])
    arm_readiness(gh, head, wt)
    gh.respond(f"api repos/{REPO}/commits/{head}/check-runs",
               {"check_runs": [{"name": "Backend Tests", "status": "in_progress"}]})
    monkeypatch.setattr(cli, "MISSING_RUN_GRACE", 0.0)

    code, out, err = run(capsys, "ready", "--wait=0")

    assert (code, err) == (3, "") and "timed out waiting for required checks" in out


def test_ready_draft_with_open_blocking_finding_does_not_mark_ready(
    wt: Path, gh: FakeGh, capsys
) -> None:
    head = head_of(wt)
    arm_pr(gh, head)
    gh.respond(reviews_key(), [rev(head, "primary")])
    finding = {
        "isResolved": False, "path": "feature.py", "line": 1,
        "comments": {"nodes": [{"body":
            f"<!-- loopzero:finding severity=important head={head} -->\n"
            "**important: Fix this**"}]},
    }
    arm_readiness(gh, head, wt, threads=(finding,), conclusion="skipped")

    code, out, err = run(capsys, "ready")

    assert code == 1 and err == ""
    assert "not ready: open important finding at feature.py:1: Fix this" in out
    assert all(c["argv"][:2] != ["pr", "ready"] for c in gh.calls)


def test_ready_non_draft_with_skipped_required_check_refuses(
    wt: Path, gh: FakeGh, capsys
) -> None:
    head = head_of(wt)
    arm_pr(gh, head, isDraft=False)
    gh.respond(reviews_key(), [rev(head, "primary")])
    arm_readiness(gh, head, wt, conclusion="skipped")

    code, out, err = run(capsys, "ready")

    assert (code, err) == (1, "")
    assert out == "not ready: required check 'checks' is skipped\n"
    assert all(c["argv"][:2] != ["pr", "ready"] for c in gh.calls)


@pytest.mark.parametrize("condition", ["missing", "wrong-head", "failed", "dirty"])
def test_ready_requires_clean_successful_local_report_at_head(
    wt: Path, gh: FakeGh, capsys, condition: str
) -> None:
    head = head_of(wt)
    arm_pr(gh, head, isDraft=False)
    gh.respond(reviews_key(), [rev(head, "primary")])
    arm_readiness(
        gh, head, wt, local_exit=1 if condition == "failed" else 0,
        dirty=condition == "dirty",
    )
    report_path = wt / ".loopzero" / "checks.json"
    if condition == "missing":
        report_path.unlink()
    elif condition == "wrong-head":
        report = json.loads(report_path.read_text())
        report["head"] = "0" * 40
        report_path.write_text(json.dumps(report))
    code, out, _ = run(capsys, "ready")
    assert code == 1
    assert out.endswith("not ready: run loopzero check at this head\n")


def test_ready_ignores_forged_markers(wt: Path, gh: FakeGh, capsys) -> None:
    head = head_of(wt)
    arm_pr(gh, head)
    gh.respond(reviews_key(), [
        rev(head, "primary", login="stranger"),          # not the token's account
        rev(head, "primary", commit="1" * 40),           # commit_id does not match marker
    ])
    arm_readiness(gh, head, wt)
    code, out, _ = run(capsys, "ready")
    assert code == 1 and out == "not ready: no review recorded for the current head\n"
    assert sum(1 for c in gh.calls if c["argv"][:2] == ["api", "user"]) == 1


def test_review_treats_forged_marker_as_fresh_lineage(wt: Path, gh: FakeGh, fake_bin,
                                                      capsys) -> None:
    head = head_of(wt)
    arm_pr(gh, head)
    gh.respond(reviews_key(), [rev(head, "primary", login="stranger"),
                               rev(head, "delta", commit="2" * 40)])
    gh.respond(post_key(), {"id": 9})
    _fake_claude(fake_bin, _claude_envelope(APPROVE))
    code, out, _ = run(capsys, "review")
    assert code == 0 and out.startswith("primary review by claude")


@pytest.mark.parametrize("state", ["DISMISSED", "PENDING", "COMMENTED"])
def test_ready_respects_latest_review_state(wt: Path, gh: FakeGh, capsys, state) -> None:
    head = head_of(wt)
    arm_pr(gh, head)
    gh.respond(reviews_key(), [rev(head, "primary"), rev(head, "delta", state=state, verdict="request_changes")])
    gh.respond("pr ready", "")
    arm_readiness(gh, head, wt)
    code, out, _ = run(capsys, "ready")
    assert (code, out) == ((0, f"ready: {URL}\n") if state == "COMMENTED" else
                           (1, "not ready: no review recorded for the current head\n"))


def test_merge_refuses_when_not_ready(wt: Path, gh: FakeGh, capsys) -> None:
    head = head_of(wt)
    arm_pr(gh, head, isDraft=False)
    gh.respond(reviews_key(), [rev(head, "primary")])
    arm_readiness(gh, head, wt, threads=(
        {"isResolved": False, "isOutdated": False, "path": "feature.py", "line": 1,
         "comments": {"nodes": [{"body": f"<!-- loopzero:finding severity=critical head={head} -->"
                                         "\n**critical: Bad**"}]}},))
    code, out, err = run(capsys, "merge")
    assert code == 1 and out == ""
    assert "not ready to merge: open critical finding at feature.py:1: Bad" in err
    assert wt.exists()


def test_merge_prints_sha_and_cleans_up(wt: Path, repo: Path, gh: FakeGh, capsys) -> None:
    head = head_of(wt)
    arm_pr(gh, head, isDraft=False)
    gh.respond(reviews_key(), [rev(head, "primary")])
    arm_readiness(gh, head, wt)
    gh.respond("pr merge", "")
    gh.respond("pr view", {"state": "MERGED", "mergeCommit": {"oid": "c" * 40}})
    gh.respond("api -X", "")
    code, out, err = run(capsys, "merge")
    assert (code, out, err) == (0, f"{'c' * 40}\ncd {repo}\n", "")
    merge = next(c["argv"] for c in gh.calls if c["argv"][:2] == ["pr", "merge"])
    assert "--squash" in merge and merge[merge.index("--match-head-commit") + 1] == head
    assert not wt.exists()
    assert git(repo, "branch", "--list", "lz/t1") == ""


def test_merge_warns_when_cleanup_fails(wt: Path, gh: FakeGh, capsys, monkeypatch) -> None:
    head = head_of(wt)
    arm_pr(gh, head, isDraft=False)
    gh.respond(reviews_key(), [rev(head, "primary")])
    arm_readiness(gh, head, wt)
    gh.respond("pr merge", "")
    gh.respond("pr view", {"state": "MERGED", "mergeCommit": {"oid": "d" * 40}})
    gh.respond("api -X", "")
    monkeypatch.setattr(cli.worktree, "cleanup", lambda *_: (_ for _ in ()).throw(
        cli.worktree.WorktreeError("worktree is dirty")))
    code, out, err = run(capsys, "merge")
    assert code == 0 and out.splitlines()[0] == "d" * 40
    assert err.startswith("warning: merged but worktree cleanup failed")


def test_merge_warns_when_remote_cleanup_times_out(
    wt: Path, gh: FakeGh, capsys, monkeypatch
) -> None:
    head = head_of(wt)
    arm_pr(gh, head, isDraft=False)
    gh.respond(reviews_key(), [rev(head, "primary")])
    arm_readiness(gh, head, wt)
    gh.respond("pr merge", "")
    gh.respond("pr view", {"state": "MERGED", "mergeCommit": {"oid": "f" * 40}})
    monkeypatch.setattr(cli.github, "delete_remote_branch", lambda *_: (_ for _ in ()).throw(
        cli._proc.ProcTimeout("gh api timed out", "")))
    monkeypatch.setattr(cli, "_cleanup", lambda _: None)
    code, out, err = run(capsys, "merge")
    assert code == 0 and out.splitlines()[0] == "f" * 40
    assert "remote branch cleanup failed for lz/t1" in err and "timed out" in err


def test_second_merge_retries_remote_branch_cleanup(
    wt: Path, gh: FakeGh, capsys, monkeypatch
) -> None:
    head = head_of(wt)
    gh.respond(
        "pr list",
        [pr_json(headRefOid=head, isDraft=False)],
        [pr_json(headRefOid=head, isDraft=False, state="MERGED")],
    )
    gh.respond(reviews_key(), [rev(head, "primary")])
    arm_readiness(gh, head, wt)
    gh.respond("pr merge", "")
    gh.respond("pr view", {"state": "MERGED", "mergeCommit": {"oid": "e" * 40}})
    gh.respond(
        "api -X",
        {"stdout": "", "stderr": "HTTP 500: delete failed", "exit": 1},
        "",
    )
    monkeypatch.setattr(cli, "_cleanup", lambda _: None)

    first = run(capsys, "merge")
    second = run(capsys, "merge")

    assert first[0] == 0 and "remote branch cleanup failed for lz/t1" in first[2]
    assert second[0] == 0 and second[2] == ""
    deletes = [call for call in gh.calls if call["argv"][:2] == ["api", "-X"]]
    assert len(deletes) == 2


def test_status_full_and_path(wt: Path, gh: FakeGh, capsys) -> None:
    code, out, _ = run(capsys, "status", "--path")
    assert code == 0 and out == f"{wt}\n"
    head = head_of(wt)
    gh.respond("pr list", [])
    code, out, _ = run(capsys, "status")
    assert code == 0
    assert "branch:   lz/t1" in out and f"head:     {head}" in out and "dirty:    no" in out
    assert "pr:       none" in out
    arm_pr(gh, head)
    gh.respond(reviews_key(), [rev(head, "primary", state="APPROVED")])
    arm_readiness(gh, head, wt, conclusion="pending")
    (wt / "scratch").write_text("x")
    code, out, _ = run(capsys, "status")
    assert code == 0 and "dirty:    yes" in out
    assert f"pr:       #7 {URL} (draft, OPEN)" in out
    assert f"review:   primary on {head[:12]} (approved)" in out
    assert "ready:    no" in out and "  - required check 'checks' is pending" in out
    assert sum(1 for c in gh.calls if c["argv"][:2] == ["api", reviews_key()[4:]]) == 1


def test_status_silently_ignores_legacy_report_without_dirty(
    wt: Path, gh: FakeGh, capsys
) -> None:
    head = head_of(wt)
    arm_pr(gh, head, isDraft=False)
    gh.respond(reviews_key(), [rev(head, "primary")])
    arm_readiness(gh, head, wt)
    report_path = wt / ".loopzero" / "checks.json"
    report = json.loads(report_path.read_text())
    del report["dirty"]
    report_path.write_text(json.dumps(report))

    code, out, err = run(capsys, "status")

    assert code == 0 and err == ""
    assert "ready:    no" in out
    assert "  - run loopzero check at this head" in out


def test_gh_errors_are_one_line_on_stderr(wt: Path, gh: FakeGh, capsys) -> None:
    gh.fail("pr list", "To get started with GitHub CLI, please run:  gh auth login\n", exit=4)
    code, out, err = run(capsys, "status")
    assert code == 1 and out.count("\n") >= 4 and err.count("\n") == 1
    assert err.startswith("loopzero status: gh pr list") and "gh auth login" in err


def test_help_lists_all_commands(capsys) -> None:
    with pytest.raises(SystemExit) as info:
        cli.main(["--help"])
    assert info.value.code == 0
    out = capsys.readouterr().out
    assert all(word in out for word in ("start", "check", "pr", "review", "ready", "merge", "status"))


# --- review findings -------------------------------------------------------------------


def test_commands_refuse_non_task_branch(wt: Path, gh: FakeGh, capsys) -> None:
    git(wt, "checkout", "-q", "-b", "feature")
    for command in ("pr", "review", "ready", "merge"):
        code, out, err = run(capsys, command)
        assert code == 1 and out == "" and "not a loopzero task branch" in err, command
    assert gh.calls == []
    git(wt, "symbolic-ref", "HEAD", "refs/heads/main")  # base branch itself
    assert run(capsys, "pr")[0] == 1 and gh.calls == []


def test_ready_and_merge_refuse_when_pr_head_differs(wt: Path, gh: FakeGh, capsys) -> None:
    arm_pr(gh, "0" * 40, isDraft=False)
    for command in ("ready", "merge"):
        code, out, err = run(capsys, command)
        assert code == 1 and out == "" and "push or pull first" in err, command
    assert wt.exists() and all(c["argv"][:2] not in (["pr", "merge"], ["pr", "ready"])
                               for c in gh.calls)


def test_review_and_ready_refuse_closed_pr(wt: Path, gh: FakeGh, capsys) -> None:
    arm_pr(gh, head_of(wt), state="CLOSED")
    for command in ("review", "ready", "merge"):
        code, _, err = run(capsys, command)
        assert code == 1 and "PR #7 is CLOSED, not open" in err, command


def test_merge_of_externally_merged_pr_verifies_sha_and_cleans_up(
    wt: Path, repo: Path, gh: FakeGh, capsys
) -> None:
    arm_pr(gh, head_of(wt), state="MERGED", isDraft=False)
    gh.respond("pr view", {"state": "MERGED", "mergeCommit": {"oid": "e" * 40}})
    gh.respond("api -X", "")
    code, out, err = run(capsys, "merge")
    assert (code, err) == (0, "")
    assert out.splitlines()[-1] == f"cd {repo}"
    assert f"PR #7 was already merged as {'e' * 12}" in out and "e" * 40 in out
    assert not wt.exists()
    assert all(c["argv"][:2] != ["pr", "merge"] for c in gh.calls)


def test_merge_queued_prints_and_keeps_worktree(wt: Path, gh: FakeGh, capsys) -> None:
    head = head_of(wt)
    arm_pr(gh, head, isDraft=False)
    gh.respond(reviews_key(), [rev(head, "primary")])
    arm_readiness(gh, head, wt)
    gh.respond("pr merge", "")
    gh.respond("pr view", {"state": "OPEN", "mergeCommit": None, "headRefName": "lz/t1"})
    gh.respond("api graphql", threads_json(),
               {"data": {"repository": {"pullRequest": {"isInMergeQueue": True}}}})
    code, out, err = run(capsys, "merge")
    assert (code, err) == (0, "")
    assert "queued for merge into main" in out and out.endswith(
        "next: loopzero merge --wait follows the queue, verifies and cleans up\n"
    )
    assert wt.exists()
    assert all(c["argv"][:2] != ["api", "-X"] for c in gh.calls)


@pytest.mark.parametrize("attested", [True, False])
@pytest.mark.parametrize("merge_state", ["BEHIND", "CLEAN"])
def test_mergify_merge_requests_once_for_behind_head(
    wt: Path, gh: FakeGh, capsys, monkeypatch, attested, merge_state
) -> None:
    workflow = (wt / "workflow.toml").read_text().replace(
        'merge = "squash"', 'merge = "mergify"\nmergify_queue = "main"'
    )
    (wt / "workflow.toml").write_text(workflow)
    git(wt, "add", "workflow.toml")
    git(wt, "commit", "-q", "-m", "use mergify")
    head = head_of(wt)
    arm_pr(gh, head, isDraft=False, mergeStateStatus=merge_state)
    gh.respond(reviews_key(), [rev(head, "primary")])
    arm_readiness(gh, head, wt)
    monkeypatch.setattr(mergify, "configured", lambda *_: attested)
    requested = []
    monkeypatch.setattr(mergify, "markers", lambda *_: (False, False))
    monkeypatch.setattr(mergify, "membership", lambda *_: None)
    monkeypatch.setattr(mergify, "request", lambda *args: requested.append(args))

    code, out, err = run(capsys, "merge")

    if attested:
        assert (code, err) == (0, "")
        assert requested == [(REPO, 7, head, "main")]
        assert "requested from Mergify queue main" in out
    else:
        assert code == 1 and "queue configuration is unverified" in err and not requested


@pytest.mark.parametrize("command", ["ready", "status"])
@pytest.mark.parametrize("merge_state", ["CLEAN", "BEHIND"])
@pytest.mark.parametrize("attested", [True, False])
def test_ready_allows_behind_only_after_configured_mergify_attestation(
    wt: Path, gh: FakeGh, capsys, monkeypatch, command, merge_state, attested
) -> None:
    workflow = (wt / "workflow.toml").read_text().replace(
        'merge = "squash"', 'merge = "mergify"\nmergify_queue = "main"'
    )
    (wt / "workflow.toml").write_text(workflow)
    git(wt, "add", "workflow.toml")
    git(wt, "commit", "-q", "-m", "use mergify")
    head = head_of(wt)
    arm_pr(gh, head, isDraft=False, mergeStateStatus=merge_state)
    gh.respond(reviews_key(), [rev(head, "primary")])
    arm_readiness(gh, head, wt)
    monkeypatch.setattr(mergify, "configured", lambda *args: attested)

    monkeypatch.setattr(mergify, "markers", lambda *_: (False, False))
    monkeypatch.setattr(mergify, "membership", lambda *_: None)
    code, out, err = run(capsys, command)

    assert not err
    if command == "ready":
        assert code == (0 if attested else 1)
    else:
        assert f"ready:    {'yes' if attested else 'no'}" in out


def test_mergify_wait_resume_does_not_resubmit_and_reports_ejection(
    wt: Path, gh: FakeGh, capsys, monkeypatch
) -> None:
    workflow = (wt / "workflow.toml").read_text().replace(
        'merge = "squash"', 'merge = "mergify"\nmergify_queue = "main"'
    )
    (wt / "workflow.toml").write_text(workflow)
    git(wt, "add", "workflow.toml")
    git(wt, "commit", "-q", "-m", "use mergify")
    head = head_of(wt)
    arm_pr(gh, head, isDraft=False)
    gh.respond(reviews_key(), [rev(head, "primary")])
    arm_readiness(gh, head, wt)
    gh.respond("pr view", {"state": "OPEN", "mergeCommit": None, "headRefOid": head})
    monkeypatch.setattr(mergify, "configured", lambda *_: True)
    monkeypatch.setattr(mergify, "markers", lambda *_: (True, True))
    monkeypatch.setattr(mergify, "membership", lambda *_: None)
    monkeypatch.setattr(
        mergify, "request", lambda *_: pytest.fail("resume must not submit another command")
    )

    code, _out, err = run(capsys, "merge", "--wait=1")

    assert code == 1 and "left Mergify queue main unmerged" in err


def test_merge_waits_for_queue_then_cleans_up(
    wt: Path, repo: Path, gh: FakeGh, capsys, monkeypatch
) -> None:
    head = head_of(wt)
    arm_pr(gh, head, isDraft=False)
    gh.respond(reviews_key(), [rev(head, "primary")])
    arm_readiness(gh, head, wt)
    gh.respond("pr merge", "")
    open_pr = {"state": "OPEN", "mergeCommit": None, "headRefName": "lz/t1"}
    gh.respond("pr view", open_pr, open_pr,
               {"state": "MERGED", "mergeCommit": {"oid": "e" * 40}})
    queued = {"data": {"repository": {"pullRequest": {"isInMergeQueue": True}}}}
    gh.respond("api graphql", threads_json(), queued, queued)
    gh.respond("api -X", "")
    monkeypatch.setattr(cli, "_sleep", lambda _: None)

    code, out, err = run(capsys, "merge", "--wait")

    assert (code, err) == (0, "") and "e" * 40 in out
    assert "PR #7 is queued for merge into main; waiting" in out and out.splitlines()[-1] == f"cd {repo}"
    assert not wt.exists()


def test_merge_wait_reports_queue_ejection(wt: Path, gh: FakeGh, capsys) -> None:
    head = head_of(wt)
    arm_pr(gh, head, isDraft=False)
    gh.respond(reviews_key(), [rev(head, "primary")])
    arm_readiness(gh, head, wt)
    gh.respond("pr merge", "")
    open_pr = {"state": "OPEN", "mergeCommit": None, "headRefName": "lz/t1"}
    gh.respond("pr view", open_pr, open_pr)
    def queue(value: bool) -> dict:
        return {"data": {"repository": {"pullRequest": {"isInMergeQueue": value}}}}
    gh.respond("api graphql", threads_json(), queue(True), queue(False))

    code, out, err = run(capsys, "merge", "--wait")

    assert code == 1 and "left the merge queue unmerged" in out + err
    assert wt.exists()


def test_merge_wait_refuses_a_head_that_changed_in_the_queue(wt: Path, gh: FakeGh, capsys) -> None:
    head = head_of(wt)
    body = "# T1\n\n## Review\n"
    gh.respond("pr list", [pr_json(headRefOid=head, body=body, isDraft=False)],
               [pr_json(headRefOid="f" * 40, body=body, isDraft=False)])
    gh.respond(reviews_key(), [rev(head, "primary")])
    arm_readiness(gh, head, wt)
    gh.respond("pr merge", "")
    gh.respond("pr view", {"state": "OPEN", "mergeCommit": None, "headRefName": "lz/t1"})
    queued = {"data": {"repository": {"pullRequest": {"isInMergeQueue": True}}}}
    gh.respond("api graphql", threads_json(), queued)

    code, _out, err = run(capsys, "merge", "--wait")

    assert code == 1 and f"PR head changed while waiting: {head[:12]} to {'f' * 12}" in err
    assert wt.exists(), "no cleanup after an unreviewed head"


def _finding_thread(marker_id: str, head: str) -> dict:
    body = (f"<!-- loopzero:finding v=1 severity=important head={head} id={marker_id} -->\n"
            "**important: Off by one**\n\nThe loop skips the last row.")
    return {"id": "THREAD_1", "isResolved": False, "isOutdated": False, "path": "src/a.py",
            "line": 3, "comments": {"nodes": [{"body": body}]}}


def test_resolve_lists_then_replies_and_resolves_one_finding(wt: Path, gh: FakeGh, capsys) -> None:
    head = head_of(wt)
    gh.respond("pr list", [pr_json(headRefOid=head)])
    posted = {"data": {"addPullRequestReviewThreadReply": {"comment": {"id": "C1"}}}}
    listing = threads_json(_finding_thread("0b9b59e7", head))
    gh.respond("api graphql", listing, listing, posted, {"data": {}})

    code, out, err = run(capsys, "resolve")
    assert (code, err) == (0, "") and out == "0b9b59e7  important  src/a.py:3  Off by one\n"

    code, out, err = run(capsys, "resolve", "0b9b59e7", "Fixed in abc1234: loop bound corrected.")
    assert (code, err) == (0, "") and out == "resolved 0b9b59e7: Off by one\n"
    calls = [c["argv"] for c in gh.calls if c["argv"][:2] == ["api", "graphql"]][2:]
    assert "addPullRequestReviewThreadReply" in calls[0][3] and "resolveReviewThread" in calls[1][3]
    assert "body=Fixed in abc1234: loop bound corrected." in calls[0] and "thread=THREAD_1" in calls[1]


def test_resolve_leaves_the_thread_open_when_the_reply_was_not_posted(
    wt: Path, gh: FakeGh, capsys
) -> None:
    head = head_of(wt)
    gh.respond("pr list", [pr_json(headRefOid=head)])
    rejected = {"data": {"addPullRequestReviewThreadReply": None}, "errors": [{"message": "no"}]}
    gh.respond("api graphql", threads_json(_finding_thread("0b9b59e7", head)), rejected)

    code, _out, err = run(capsys, "resolve", "0b9b59e7", "Fixed in abc1234.")

    assert code == 1 and "reply was not posted; not resolving" in err
    assert not any("resolveReviewThread" in a for c in gh.calls for a in c["argv"])


def test_resolve_refuses_silent_or_unknown(wt: Path, gh: FakeGh, capsys) -> None:
    head = head_of(wt)
    gh.respond("pr list", [pr_json(headRefOid=head)])
    gh.respond("api graphql", threads_json(_finding_thread("0b9b59e7", head)))

    code, _out, err = run(capsys, "resolve", "0b9b59e7", "  ")
    assert code == 1 and "a reply saying what changed" in err
    code, _out, err = run(capsys, "resolve", "deadbeef", "Fixed.")
    assert code == 1 and "no open blocking finding deadbeef" in err
    assert not any("resolveReviewThread" in a for c in gh.calls for a in c["argv"])


def test_pr_ignores_corrupt_checks_report(wt: Path, gh: FakeGh, capsys) -> None:
    (wt / ".loopzero" / "checks.json").write_text('{"results": "nope"}')
    gh.respond("pr list", [pr_json(headRefOid=head_of(wt))])
    gh.respond(f"api repos/{REPO}/pulls/7", {})
    code, _, err = run(capsys, "pr")
    assert code == 0 and "warning: ignoring unreadable" in err
    assert "(no `loopzero check` run recorded)" in gh.calls[-1]["--input"]


def test_unexpected_and_interrupt_exit_codes(wt: Path, capsys, monkeypatch) -> None:
    def boom(*_):
        raise KeyError("headRefOid")
    monkeypatch.setattr(cli.github, "pr_for_branch", boom)
    code, _, err = run(capsys, "status")
    assert code == 1 and err == "loopzero status: unexpected response: 'headRefOid'\n"

    def interrupt(*_):
        raise KeyboardInterrupt
    monkeypatch.setattr(cli.github, "pr_for_branch", interrupt)
    assert run(capsys, "status")[0] == 130


def test_start_refuses_inside_task_worktree(wt: Path, repo: Path, capsys) -> None:
    code, out, err = run(capsys, "start", "nested")
    assert code == 1 and out == "" and "already inside task worktree" in err
    assert not (repo / ".worktrees" / "nested").exists()
    assert "lz/nested" not in git(repo, "branch", "--list", "lz/nested")


def test_reviews_are_paginated(wt: Path, gh: FakeGh, capsys) -> None:
    head = head_of(wt)
    arm_pr(gh, head)
    gh.respond(reviews_key(1), [rev("f" * 40, "primary", login="x")] * 100)
    gh.respond(reviews_key(2), [rev(head, "primary")])
    arm_readiness(gh, head, wt)
    gh.respond("pr ready", "")
    code, out, _ = run(capsys, "ready")
    assert code == 0 and out == f"ready: {URL}\n"


def test_budget_message_explains_rewrite(wt: Path, gh: FakeGh, capsys) -> None:
    base, head = git(wt, "rev-parse", "HEAD~1").strip(), head_of(wt)
    arm_pr(gh, head)
    gh.respond(reviews_key(), [rev(base, "primary"), rev(head, "delta")])
    code, _, err = run(capsys, "review")
    assert code == 1 and "rewrite the reviewed commits (squash/amend)" in err


@pytest.mark.parametrize("state,verdict,expected", [
    ("APPROVED", "approve", True), ("COMMENTED", "request_changes", True),
    ("DISMISSED", "approve", False), ("PENDING", "approve", False),
    ("COMMENTED", "", False),
])
def test_hosted_eligibility_uses_publisher_not_token(wt, gh, state, verdict, expected):
    from loopzero import eligibility
    head = head_of(wt)
    gh.respond("pr view", pr_json(headRefOid=head, mergeStateStatus="BEHIND"))
    gh.respond("api user", {"login": "ci-service"})
    gh.respond(reviews_key(), [rev(head, "primary"), rev(head, "delta", state=state, verdict=verdict)])
    gh.respond("api graphql", threads_json())
    assert eligibility.evaluate(REPO, 7, head, "main", (LOGIN,)).ready is expected
    cfg = wt / ".loopzero" / "hosted.toml"
    cfg.write_text(WORKFLOW + f'\nreview_publishers = ["{LOGIN}"]\n')
    gh.respond(f"api repos/{REPO}/statuses/{head}", {})
    assert eligibility.main(["--config", str(cfg), "--pr", "7", "--head", head,
                             "--publish"]) == (0 if expected else 1)
    states = [json.loads(c["--input"])["state"] for c in gh.calls if "--input" in c]
    assert states == ["pending", "success" if expected else "failure"]
    gh.respond("pr view", pr_json(headRefOid="b" * 40))
    assert not eligibility.evaluate(REPO, 7, head, "main", (LOGIN,)).ready


@pytest.mark.parametrize("publisher", ["stranger", "lz-bot"])
def test_ready_pins_review_publishers_to_base(wt, gh, capsys, publisher):
    (wt / "workflow.toml").write_text(WORKFLOW + f'\nreview_publishers = ["{publisher}"]\n')
    head = head_of(wt)
    arm_pr(gh, head, isDraft=False)
    gh.respond(reviews_key(), [rev(head, "primary", login=publisher)])
    arm_readiness(gh, head, wt)
    assert run(capsys, "ready")[0] == (0 if publisher == LOGIN else 1)


@pytest.mark.parametrize("outcome", ["merged", "ejected", "head_changed", "timeout", "api_error",
    "unsafe_config", "config_error", "merged_head_changed", "ejection_head_changed", "already_merged",
    "already_merged_changed", "unsafe_pending", "requested_head_changed"])
def test_mergify_wait_outcomes_preserve_unmerged_work(wt, repo, gh, capsys, monkeypatch, outcome):
    (wt / "workflow.toml").write_text(WORKFLOW.replace(
        'merge = "squash"', 'merge = "mergify"\nmergify_queue = "main"'))
    git(wt, "add", "workflow.toml")
    git(wt, "commit", "-qm", "mergify delivery")
    head = head_of(wt)
    arm_pr(gh, head, isDraft=False, state="MERGED" if outcome.startswith("already_") else "OPEN")
    gh.respond(reviews_key(), [rev(head, "primary")])
    arm_readiness(gh, head, wt)
    gh.respond("api -X", "")
    gh.respond("pr view", {"state": "MERGED" if outcome in {"merged", "merged_head_changed", "already_merged", "already_merged_changed"} else "OPEN",
                           "mergeCommit": {"oid": "e" * 40} if outcome in {"merged", "merged_head_changed", "already_merged", "already_merged_changed"} else None,
                           "headRefOid": "f" * 40 if outcome in {"merged_head_changed", "already_merged_changed"} else head})
    if outcome == "ejection_head_changed":
        gh.respond("pr view", {"state": "OPEN", "headRefOid": head},
                   {"state": "MERGED", "headRefOid": "f" * 40,
                    "mergeCommit": {"oid": "e" * 40}})
    if outcome in {"head_changed", "requested_head_changed"}:
        gh.respond("pr list", [pr_json(headRefOid=head, isDraft=False)],
                   [pr_json(headRefOid=head, isDraft=False)],
                   [pr_json(headRefOid="f" * 40, isDraft=False)])
    configurations = []
    def configured(*_):
        configurations.append(True)
        if len(configurations) > 2:
            if outcome == "config_error":
                raise mergify.MergifyError("configuration HTTP 503")
            if outcome in {"unsafe_config", "unsafe_pending"}:
                return False
        return True
    monkeypatch.setattr(mergify, "configured", configured)
    monkeypatch.setattr(mergify, "markers", lambda *_: (outcome != "requested_head_changed",
                                                        outcome not in {"unsafe_pending", "requested_head_changed"}))
    requests = []
    monkeypatch.setattr(mergify, "request", lambda *args: requests.append(args))
    monkeypatch.setattr(mergify, "mark_confirmed", lambda *_: pytest.fail("unsafe confirmation"))
    reads = []
    def membership(*_):
        reads.append(True)
        if outcome in {"unsafe_pending", "requested_head_changed"} and len(reads) == 1:
            return None
        if len(reads) > 1:
            if outcome == "api_error":
                raise mergify.MergifyError("HTTP 503")
            if outcome in {"ejected", "ejection_head_changed"}:
                return None
        return mergify.Membership("main", "2026-09-22T12:00:00Z", 1)
    monkeypatch.setattr(mergify, "membership", membership)
    code, out, err = run(capsys, "merge", "--wait=0")
    assert requests == ([(REPO, 7, head, "main")] if outcome == "requested_head_changed" else [])
    if outcome in {"merged", "already_merged"}:
        assert code == 0 and "e" * 40 in out and not wt.exists()
    else:
        assert wt.exists() and code == (3 if outcome == "timeout" else 1)
        message = {"ejected": "left Mergify queue", "head_changed": "head changed",
                   "timeout": "timed out", "api_error": "HTTP 503",
                   "unsafe_config": "unsafe or unavailable", "unsafe_pending": "unsafe or unavailable", "config_error": "HTTP 503",
                   "requested_head_changed": "head changed", "merged_head_changed": "head changed", "already_merged_changed": "head changed", "ejection_head_changed": "head changed"}[outcome]
        assert message in out + err


@pytest.mark.parametrize("queue_state", ["unsafe", "error", "safe", "becomes_unsafe"])
@pytest.mark.parametrize("check_state", ["missing", "skipped"])
def test_mergify_ready_draft_requires_safe_queue_before_ci_activation(
    wt, gh, capsys, monkeypatch, queue_state, check_state
):
    (wt / "workflow.toml").write_text(WORKFLOW.replace(
        'merge = "squash"', 'merge = "mergify"\nmergify_queue = "main"'))
    git(wt, "add", "workflow.toml")
    git(wt, "commit", "-qm", "mergify delivery")
    head = head_of(wt)
    arm_pr(gh, head)
    gh.respond(reviews_key(), [rev(head, "primary")])
    arm_readiness(gh, head, wt)
    gh.respond(f"api repos/{REPO}/commits/{head}/check-runs",
               {"check_runs": [] if check_state == "missing" else [
                   {"name": "checks", "status": "completed", "conclusion": "skipped"}]},
               {"check_runs": [{"name": "checks", "status": "completed", "conclusion": "success"}]})
    gh.respond("pr ready", "")
    calls = []
    def configured(*_):
        calls.append(True)
        if queue_state == "error":
            raise mergify.MergifyError("configuration HTTP 503")
        return queue_state == "safe" or (queue_state == "becomes_unsafe" and len(calls) == 1)
    monkeypatch.setattr(mergify, "configured", configured)
    code, out, err = run(capsys, "ready", "--wait=0")
    assert code == (0 if queue_state == "safe" else 1)
    writes = sum(c["argv"][:2] == ["pr", "ready"] for c in gh.calls)
    assert writes == (1 if queue_state in {"safe", "becomes_unsafe"} else 0)
    if queue_state != "safe":
        assert "configuration" in out + err


def test_mergify_cancel_does_not_require_healthy_queue_or_review(wt, gh, capsys, monkeypatch):
    (wt / "workflow.toml").write_text(WORKFLOW.replace(
        'merge = "squash"', 'merge = "mergify"\nmergify_queue = "main"'))
    arm_pr(gh, "a" * 40)
    monkeypatch.setattr(mergify, "configured", lambda *_: pytest.fail("cancel must remain available"))
    removals = []
    monkeypatch.setattr(mergify, "dequeue", lambda *args: removals.append(args))
    code, _out, err = run(capsys, "merge", "--cancel")
    assert (code, err) == (0, "") and removals == [(REPO, 7)] and wt.exists()
