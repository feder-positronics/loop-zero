"""cli.py end to end: real git with a bare origin, fake `gh`, `claude`, `codex` and `bwrap`."""

from __future__ import annotations

import base64
import errno
import json
import os
import re
import shutil
import signal
import subprocess
import sys
from contextlib import suppress
from dataclasses import asdict, replace
from pathlib import Path
from select import select
from types import SimpleNamespace
from unittest.mock import Mock
from urllib.parse import quote

import pytest

from loopzero import cli, config, github, hosted, mergify
from loopzero._proc import Completed, build_env
from loopzero.types import CheckReport, CheckResult, Finding, ReviewResult
from tests.conftest import commit_file, git, init_git_repo
from tests.fake_gh import replay
from tests.test_github import (
    FakeGh,
    branch_head_key,
    branch_history_key,
    closed_history_row,
    merge_json,
    pr_json,
    pr_list_key,
    rest_pr_json,
    threads_json,
)
from tests.test_runners import (
    APPROVE,
    CHANGES,
    _claude_envelope,
    _fake_claude,
    _fake_codex,
    _native_codex_result,
)
from tests.test_sandbox import FAKE_BWRAP, inject_check_timeout

REPO = "acme/widgets"
URL = f"https://github.com/{REPO}/pull/7"
FAILED_CHECK_HINT = f". Inspect {URL}/checks. Fix reproducible failures before retrying. Rerun the failed job only for a transient failure. Once green, run `loopzero ready --wait`."
MERGIFY_QUEUE_FIXTURE = json.loads((Path(__file__).with_name("fixtures") / "mergify-queue.json").read_text())
BRANCH_READ = branch_head_key().split()
PR_LIST_READ = pr_list_key().split()
LOOKUP_CALLS = [BRANCH_READ, PR_LIST_READ, ["api", f"repos/{REPO}/pulls/7"]]
HISTORY_CALLS = [BRANCH_READ, branch_history_key().split()]
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


@pytest.fixture(scope="session")
def git_seed(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Prepare the clean and task checkouts and their bare origin."""
    seed = tmp_path_factory.mktemp("git-seed")
    checkout = init_git_repo(seed / "repo")
    commit_file(checkout, "workflow.toml", WORKFLOW, "config")
    origin = seed / "origin.git"
    git(checkout, "init", "-q", "--bare", "-b", "main", str(origin))
    git(checkout, "remote", "add", "origin", str(origin))
    git(checkout, "push", "-q", "origin", "main")
    git(checkout, "remote", "set-head", "origin", "main")
    prepared = seed / "prepared"
    shutil.copytree(checkout, prepared)
    path = cli.worktree.start(prepared, "t1", "main")
    (path / "feature.py").write_text("print('hi')\n")
    task = cli.worktree.task_file(path)
    for field, value in (
        ("Goal", "The feature prints its greeting."),
        ("Problem", "The feature is missing."),
        ("Context", "The feature module is part of the sample application."),
    ):
        task.write_text(re.sub(rf"(?m)^(- \*\*{field}:\*\* )<.*>$", rf"\g<1>{value}", task.read_text()))
    shutil.move(task.parent, seed / ".loopzero")
    git(path, "add", "feature.py")
    git(path, "commit", "-q", "-m", "add feature")
    git(prepared, "worktree", "remove", str(path))
    return seed


@pytest.fixture
def repo(git_seed: Path, tmp_path: Path, fake_tool, monkeypatch, request: pytest.FixtureRequest) -> Path:
    """A private copy of both Git repositories and a per-case fake bwrap."""
    checkout = tmp_path / "repo"
    seed_checkout = "prepared" if "wt" in request.fixturenames else "repo"
    shutil.copytree(git_seed / seed_checkout, checkout)
    origin = shutil.copytree(git_seed / "origin.git", tmp_path / "origin.git")
    git(checkout, "remote", "set-url", "origin", str(origin))
    run_command, git_command = github.run, cli._git

    def local_push_url(cwd, *args, **kwargs):
        output = git_command(cwd, *args, **kwargs)
        if args == ("remote", "get-url", "--push", "--all", "origin"):
            return "\n".join(f"https://github.com/{REPO}.git" if url == str(origin) else url
                             for url in output.splitlines())
        return output

    def local_transport(argv, **kwargs):
        if argv[0] == "git" and "push" in argv:
            assert argv[-2] == f"https://github.com/{REPO}.git"
            assert Path(kwargs["cwd"]) != Path.cwd()
            assert kwargs["extra_env"]["GIT_CONFIG_NOSYSTEM"] == "1"
            assert kwargs["extra_env"]["GIT_CONFIG_GLOBAL"] == "/dev/null"
            argv = (*argv[:-2], str(origin), argv[-1])
        return run_command(argv, **kwargs)

    monkeypatch.setattr(github, "run", local_transport)
    monkeypatch.setattr(cli, "_git", local_push_url)
    fake_tool("bwrap", FAKE_BWRAP.format(log=tmp_path / "bwrap.argv"))
    return checkout


@pytest.fixture
def gh(fake_bin: Path, tmp_path: Path) -> FakeGh:
    fake = FakeGh(fake_bin, tmp_path)
    fake.respond(branch_history_key(), [])
    fake.respond("api graphql viewer", {"data": {"viewer": {"login": LOGIN}}})
    fake.respond(f"api repos/{REPO}/pulls/7/files",
                 [{"filename": "feature.py", "patch": "@@ -0,0 +1 @@\n+print('hi')"}])
    return fake


@pytest.mark.parametrize("link_error", [errno.EOPNOTSUPP, errno.EPERM, errno.EEXIST])
def test_start_reports_handoff_publication_failure(repo, monkeypatch, capsys, link_error):
    monkeypatch.chdir(repo)
    task = repo / ".worktrees" / "t1" / ".loopzero" / "task.md"

    def fail_link(path, source):
        if link_error == errno.EEXIST:
            path.write_text("existing handoff")
        raise OSError(link_error, "link failed")

    monkeypatch.setattr(Path, "hardlink_to", fail_link)
    assert cli.main(["start", "t1"]) == 1
    error = capsys.readouterr().err
    assert str(task) in error
    if link_error == errno.EEXIST:
        assert "task file already exists" in error
        assert task.read_text() == "existing handoff"
    else:
        assert "from a checkout on a filesystem that supports hard links" in error
        assert not task.exists()
    assert not list(task.parent.glob(".task.md-*"))


@pytest.fixture
def replay_gh(gh: FakeGh, monkeypatch: pytest.MonkeyPatch) -> None:
    """Replay GitHub CLI requests in-process while keeping real Git commands."""
    run_process = github.run

    def run(argv, *, cwd, env_allowlist, extra_env=None, **kwargs):
        if argv[0] != "gh":
            return run_process(argv, cwd=cwd, env_allowlist=env_allowlist,
                               extra_env=extra_env, **kwargs)
        token = build_env(env_allowlist, extra_env).get("GH_TOKEN")
        code, stdout, stderr = replay(argv[1:], str(gh.calls_file), str(gh.scenario_file), str(cwd), token)
        return Completed(code, stdout, stderr, 0)

    monkeypatch.setattr(github, "run", run)


@pytest.fixture
def wt(repo: Path, git_seed: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = cli.worktree.worktree_dir(repo, "t1")
    git(repo, "worktree", "add", "-q", str(path), "lz/t1")
    shutil.copytree(git_seed / ".loopzero", cli.worktree.task_file(path).parent)
    monkeypatch.chdir(path)
    return path


def head_of(path: Path) -> str:
    return git(path, "rev-parse", "HEAD").strip()


@pytest.fixture
def head(wt: Path) -> str:
    return head_of(wt)


def marker(head: str, kind: str, source: str = "model") -> str:
    return cli.review_marker(head, kind, source)


@pytest.mark.parametrize("source", ["model", "pair"])
def test_review_marker_quoted_in_prose_neither_voids_nor_hides_a_review(gh: FakeGh, source: str) -> None:
    head = "a" * 40
    quote = cli.review_marker("b" * 40, "primary", source)
    result = ReviewResult("claude", head, "primary", "approve",
                          (Finding("suggestion", None, None, "Fixture", f"The fixture quotes {quote}."),),
                          "raw", provenance={"session_ids": ["s"]})
    gh.respond(f"api repos/{REPO}/pulls/7/files", [])
    (payload,) = github.review_payloads(REPO, 7, head, result, [(marker(head, "primary"), "current", False)])
    review = {"body": payload["body"], "state": "COMMENTED", "commit_id": head, "user": {"login": LOGIN}}
    assert quote in review["body"]

    assert cli.eligibility.eligible_review([review], head, (LOGIN,)) is review
    assert cli.eligibility.readiness_review(
        [review], head, (LOGIN,), repo=REPO, number=7, base_branch="main") is review
    assert cli._pr_markers([review], head, (LOGIN,)) == [cli.Marker(head, "primary", "COMMENTED")]


def test_second_review_marker_in_the_leading_block_voids_the_review() -> None:
    head = "a" * 40
    body = f"{marker(head, 'primary')}\n{marker('b' * 40, 'primary')}\nreview: **approve**"
    review = {"body": body, "state": "APPROVED", "commit_id": head, "user": {"login": LOGIN}}
    assert cli.eligibility.eligible_review([review], head, (LOGIN,)) is None
    assert cli.eligibility.readiness_review(
        [review], head, (LOGIN,), repo=REPO, number=7, base_branch="main") is None
    assert cli._pr_markers([review], head, (LOGIN,)) == []


def test_publication_recovery_ignores_markers_quoted_on_other_commits(wt: Path, gh: FakeGh, head) -> None:
    config = cli.config_mod.load(wt / "workflow.toml")
    result = ReviewResult("claude", head, "primary", "approve", (), "raw", provenance={"session_ids": ["s"]})
    other = rev("f" * 40, "primary")
    other["body"] += f"\nA finding quotes {marker(head, 'primary')}"
    gh.respond_pr_view(pr_json(headRefOid=head))
    pr = github.PR(7, URL, head, "main", False, "OPEN", "MERGEABLE")
    assert cli._confirm_review(wt, config, pr, result, LOGIN, [other]) is False


def rev(head: str, kind: str, *, state: str = "APPROVED", commit: str | None = None,
        login: str = LOGIN, verdict: str = "approve") -> dict:
    """A review object as `gh api .../reviews` returns it, carrying a loopzero marker."""
    return {"body": f"{marker(head, kind)}\nreview: **{verdict}**", "state": state,
            "commit_id": commit or head, "user": {"login": login}}


def reviews_key(page: int = 1) -> str:
    return f"api repos/{REPO}/pulls/7/reviews?per_page=100&page={page}"


def post_key() -> str:
    return f"api repos/{REPO}/pulls/7/reviews"


def arm_check_runs(gh: FakeGh, head: str, *runs) -> None:
    gh.respond(f"api repos/{REPO}/commits/{head}/check-runs",
               *({"check_runs": result} for result in runs))


def arm_check_progress(gh: FakeGh, head: str, *conclusions: str) -> None:
    snapshots = [
        [{"name": "checks", "status": "in_progress"}] if conclusion == "pending"
        else [{"name": "checks", "status": "completed", "conclusion": conclusion}]
        for conclusion in conclusions
    ]
    arm_check_runs(gh, head, *snapshots)


def arm_readiness(
    gh: FakeGh, head: str, wt: Path, *, threads=(), conclusion: str = "success",
    local_exit: int | None = 0, dirty: bool = False,
) -> None:
    gh.respond("api graphql", threads_json(*threads))
    arm_check_runs(gh, head, [
        {"name": "checks", "status": "completed", "conclusion": conclusion}])
    gh.respond(f"api repos/{REPO}/commits/{head}/statuses", [])
    if local_exit is not None:
        config = cli._load_config(SimpleNamespace(config=None, command="ready"), wt, pin_checks=True)
        results = tuple(CheckResult(command, local_exit, 0.1, "") for command in config.checks)
        cli._save_report(
            wt, CheckReport(head, dirty, results, cli.sandbox.settings_fingerprint(config, wt))
        )


def without_accepted_receipt(out: str) -> str:
    return "".join(line for line in out.splitlines(keepends=True)
                   if not line.startswith("receipt accepted:"))


@pytest.fixture
def git_calls(monkeypatch):
    calls = []
    original = subprocess.Popen

    def record(argv, *args, **kwargs):
        if argv[0] == "git":
            calls.append(argv[1:])
        return original(argv, *args, **kwargs)

    monkeypatch.setattr(subprocess, "Popen", record)
    return calls


def assert_git_cost(calls, total, workflow_reads):
    assert len(calls) == total, calls
    assert sum(call[0] == "show" and call[-1].endswith(":workflow.toml") for call in calls) == workflow_reads


def run(capsys, *argv: str) -> tuple[int, str, str]:
    code = cli.main(list(argv))
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def poll_clock(monkeypatch, *, start=0.0, jitter=None, after_sleep=None, patch_sleep=True):
    clock = SimpleNamespace(now=start, sleeps=[])

    def sleep(seconds):
        clock.sleeps.append(seconds)
        clock.now += seconds
        if after_sleep is not None:
            after_sleep()

    monkeypatch.setattr(cli.time, "monotonic", lambda: clock.now)
    if patch_sleep:
        monkeypatch.setattr(cli, "_sleep", sleep)
    if jitter is not None:
        monkeypatch.setattr(cli.random, "uniform", lambda *_: jitter)
    return clock


def on_ready_transition(gh, monkeypatch, callback):
    gh.respond("pr ready", "")
    mark_ready = github.mark_ready

    def transition(*args):
        mark_ready(*args)
        callback()

    monkeypatch.setattr(github, "mark_ready", transition)


def expect_cli(capsys, exit_code, *argv, stdout=None, stderr=None, stdout_contains=(), stderr_contains=()):
    code, out, err = run(capsys, *argv)
    assert code == exit_code, out + err
    if stdout is not None:
        assert out == stdout
    if stderr is not None:
        assert err == stderr
    for fragment in stdout_contains:
        assert fragment in out
    for fragment in stderr_contains:
        assert fragment in err
    return out, err


# --- start / check --------------------------------------------------------------------


@pytest.mark.parametrize("detached", [False, True])
def test_start_prints_only_worktree_path(repo: Path, monkeypatch, capsys, detached: bool) -> None:
    if detached:
        git(repo, "switch", "-q", "--detach")
    subdir = repo / "subdir"
    subdir.mkdir()
    monkeypatch.chdir(subdir)
    out, _err = expect_cli(capsys, 0, "start", "hello", stderr="")
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


def test_check_pass_writes_report(wt: Path, gh: FakeGh, capsys) -> None:
    code, out, _ = run(capsys, "check")
    assert gh.calls == []
    lines = out.splitlines()
    assert code == 0 and lines[-1] == "PASS"
    assert lines[0].startswith("checks pinned to origin/main@")
    assert lines[1] == "running: echo ok"
    assert lines[2].startswith("stage 1/2: exit 0 (")
    assert lines[3] == "running: test -f README.md"
    assert lines[4].startswith("stage 2/2: exit 0 (")
    assert sum("exit 0" in line for line in lines) == 2
    report = json.loads((wt / ".loopzero" / "checks.json").read_text())
    assert report["head"] == head_of(wt)
    assert report["dirty"] is False
    assert f"receipt produced: head={report['head']} content=" in out
    assert "config=commands-sha256:" in out and "environment=unknown" in out
    assert [(r["command"], r["exit_code"]) for r in report["results"]] == [
        ("echo ok", 0), ("test -f README.md", 0)]
    assert report["results"][0]["tail"] == "ok"


@pytest.mark.parametrize("state", [None, "CLOSED", "MERGED"])
def test_check_skips_closed_pr_history(wt, gh, capsys, head, state):
    git(wt, "push", "-q", "-u", "origin", "lz/t1")
    associated = (
        [dict(rest_pr_json(pr_json(headRefOid=head, state=state)),
              created_at="2026-11-25T00:00:00Z")] if state else []
    )
    commit_read = f"api repos/{REPO}/commits/{head}/pulls?per_page=100&page=1"
    gh.respond(branch_head_key(), {"commit": {"sha": head}})
    gh.respond(commit_read, associated)
    gh.respond_pr_list([pr_json(headRefOid=head, state=state)] if state else [])

    code, out, err = run(capsys, "check")

    assert (code, err) == (0, "") and out.endswith("PASS\n")
    assert [c["argv"][:2] for c in gh.calls] == [
        BRANCH_READ, commit_read.split(), PR_LIST_READ,
    ]


@pytest.mark.parametrize("pushed,refresh_timeout,stage_exit", [
    (False, False, 0), (True, False, 0), (True, True, 0), (True, True, 124),
])
def test_check_refresh_preserves_local_verdict(wt, gh, capsys, monkeypatch, pushed, refresh_timeout, stage_exit):
    git(wt, "push", "-q", "-u", "origin", "lz/t1")
    arm_pr(gh, head_of(wt))
    if not pushed:
        git(wt, "commit", "-q", "--allow-empty", "-m", "unpushed change")
    if stage_exit:
        inject_check_timeout(monkeypatch, b"local check output")
    endpoint = f"repos/{REPO}/pulls/7"
    gh.respond(f"api {endpoint} --method PATCH", {})
    if refresh_timeout:
        timeout = cli._proc.ProcTimeout(f"PATCH {endpoint} timed out")
        monkeypatch.setattr(github, "update_body", Mock(side_effect=timeout))
    code, out, err = run(capsys, "check")
    assert (code, out.splitlines()[-1]) == ((1, "FAIL") if stage_exit else (0, "PASS"))
    assert [call["argv"][:2] for call in gh.calls] == LOOKUP_CALLS + (
        [["api", endpoint]] if pushed and not refresh_timeout else [])
    report = json.loads((wt / ".loopzero/checks.json").read_text())
    assert report["head"] == head_of(wt) and report["dirty"] is False
    assert report["results"][0]["exit_code"] == stage_exit
    assert bool(err) == refresh_timeout
    if refresh_timeout:
        assert f"note: could not refresh PR checks: PATCH {endpoint} timed out" in err
    if pushed and not refresh_timeout:
        body = json.loads(gh.calls[len(LOOKUP_CALLS)]["--input"])["body"]
        assert body.count("## Validation") == 1
        assert f"Recorded for `{head_of(wt)[:12]}`" in body
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
    arm_pr_body_patch(gh, head_of(wt), body=live.replace("\n", newline))
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
    gh.respond_pr_list([])
    gh.respond_pr_create(pr_json(headRefOid=head_of(wt)))
    assert run(capsys, "pr")[0] == 0
    body = next(json.loads(c["--input"])["body"] for c in gh.calls
                if c["argv"][:2] == ["api", f"repos/{REPO}/pulls"])
    assert "- Regression failed before the fix." in body
    assert "- Browser verified." in body
    assert "(no `loopzero check` run recorded)" in body


@pytest.mark.parametrize("command", ["pr", "check"])
def test_pr_body_is_unchanged_when_checks_are_unchanged(wt: Path, gh: FakeGh, capsys, command) -> None:
    head = head_of(wt)
    if command == "check":
        config = cli._load_config(SimpleNamespace(config=None, command="ready"), wt, pin_checks=True)
        cli._save_report(wt, CheckReport(head, False, (
            CheckResult("echo ok", 0, 999.0, "ok"),
            CheckResult("test -f README.md", 0, 999.0, ""),
        ), cli.sandbox.settings_fingerprint(config, wt)))
    arm_pr_body_patch(gh, head)
    assert run(capsys, "pr")[0] == 0
    body = json.loads(gh.calls[-1]["--input"])["body"]
    gh.respond_pr_list([pr_json(headRefOid=head, body=body)])
    before = len(gh.calls)
    assert run(capsys, command)[0] == 0
    history = HISTORY_CALLS if command == "pr" else []
    refresh = [["api", f"repos/{REPO}/pulls/7"]] if command == "pr" else []
    assert [c["argv"][:2] for c in gh.calls[before:]] == history + LOOKUP_CALLS + refresh


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
    arm_pr_body_patch(gh, head_of(wt), body=body)
    assert run(capsys, "check")[0] == 0
    first = json.loads(gh.calls[-1]["--input"])["body"]
    old_head = head_of(wt)
    commit_file(wt, "feature.py", "print('new')\n", "new behavior")
    gh.respond_pr_list([pr_json(headRefOid=head_of(wt), body=first)])
    assert run(capsys, "check")[0] == 0
    updated = json.loads(gh.calls[-1]["--input"])["body"]
    assert old_head[:12] not in updated and head_of(wt)[:12] in updated
    assert updated.count("Recorded for") == 1
    for text in ["Retained browser proof.", "Human evidence.", "Approved."]:
        if text in body:
            assert text in updated


def test_delayed_check_preserves_review_summary(wt, gh, fake_bin, capsys, monkeypatch):
    head = prepare_review(wt, gh, fake_bin)
    body = "# PR\n\n## Notes\nHuman evidence.\n"
    gh.respond_pr_list([pr_json(headRefOid=head, body=body)])
    git(wt, "push", "-q", "-u", "origin", "lz/t1")
    refresh = cli._refresh_pr_checks

    def delay(path, config):
        assert run(capsys, "review")[0] == 0
        live = json.loads(gh.calls[-1]["--input"])["body"]
        gh.respond(f"api repos/{REPO}/pulls/7", rest_pr_json(pr_json(headRefOid=head, body=live)))
        refresh(path, config)

    monkeypatch.setattr(cli, "_refresh_pr_checks", delay)
    assert run(capsys, "check")[0] == 0
    final = json.loads(gh.calls[-1]["--input"])["body"]
    assert all(text in final for text in ("Human evidence.", "Recorded for", "- primary, claude"))


@pytest.mark.parametrize("command,exit_code", [("check", 0), ("pr", 1)])
def test_malformed_live_markers_do_not_mask_check_verdict(
    wt: Path, gh: FakeGh, capsys, command: str, exit_code: int,
) -> None:
    git(wt, "push", "-q", "-u", "origin", "lz/t1")
    body = "# PR\n\n## Validation\n<!-- loopzero:checks:start -->\nHuman evidence.\n"
    gh.respond_pr_list([pr_json(headRefOid=head_of(wt), body=body)])
    code, out, err = run(capsys, command)
    assert code == exit_code and "malformed loopzero checks block" in err
    history = HISTORY_CALLS if command == "pr" else []
    assert [c["argv"][:2] for c in gh.calls] == history + LOOKUP_CALLS
    if command == "check":
        assert out.endswith("PASS\n")
        assert json.loads((wt / ".loopzero/checks.json").read_text())["results"][0]["exit_code"] == 0


@pytest.mark.parametrize("command,exit_code,tail", [
    ("echo no; exit 3", 3, "no\n"),
    ("seq 1 50; exit 1", 1, "".join(f"{i}\n" for i in range(11, 51)))])
def test_check_failure_reports_verdict_and_capped_tail(wt, capsys, command, exit_code, tail):
    (wt / "workflow.toml").write_text(WORKFLOW.replace(
        '["echo ok", "test -f README.md"]', json.dumps([command, "test -f README.md"])))
    code, out, err = run(capsys, "--config", str(wt / "workflow.toml"), "check")
    assert code == 1 and out.endswith("FAIL\n")
    assert out.startswith("checks are unpinned because --config was provided\n")
    assert sum(line.startswith(("stage 1/2: exit ", "exit "))
               for line in out.splitlines()) == 1
    assert f"stage 1/2: exit {exit_code} (" in out and "README.md" not in out
    assert err == f"--- {command} (exit {exit_code}) ---\n{tail}"
    report = json.loads((wt / ".loopzero/checks.json").read_text())
    assert [result["command"] for result in report["results"]] == [command]


@pytest.mark.parametrize("cut", ["second stage", "final validation", "changed inputs"])
def test_check_reuses_checkpoint_after_interruption(wt, gh, capsys, monkeypatch, cut):
    original_context, original_git = cli._context, cli.sandbox._git
    changed_inputs = cut == "changed inputs"
    calls, interrupt = [], not changed_inputs
    previous_commands = ("first", "second", "third")

    def context(*args, **kwargs):
        path, settings = original_context(*args, **kwargs)
        commands = previous_commands if changed_inputs else settings.checks
        inputs = {command: ("feature.py",) for command in commands}
        if changed_inputs:
            inputs["third"] = ("README.md",)
        return path, replace(settings, check_runner=(sys.executable,), checks=commands, inputs=inputs)

    def dispatch(_config, _wt, _head, _base, command, _timeout):
        calls.append(command)
        if interrupt and cut != "final validation" and len(calls) == (5 if changed_inputs else 2):
            raise KeyboardInterrupt
        return CheckResult(command, 0, 0.0, "")

    def check_git(config, path, *args):
        if interrupt and len(calls) == 2 and args == ("rev-parse", "HEAD"):
            raise KeyboardInterrupt
        return original_git(config, path, *args)

    monkeypatch.setattr(cli, "_context", context)
    monkeypatch.setattr(cli.sandbox, "_dispatch_one", dispatch)
    if cut == "final validation":
        monkeypatch.setattr(cli.sandbox, "_git", check_git)
    if changed_inputs:
        expect_cli(capsys, 0, "check")
        prior_head = cli._load_report(wt).head
        commit_file(wt, "feature.py", "updated = True\n", "Change the first two stages' inputs")
        interrupt = True

    expect_cli(capsys, 130, "check")
    report = cli._load_report(wt)
    assert report is not None and not report.ok
    if not changed_inputs:
        assert [result.command for result in report.results if result.input_key] == (
            calls[:1] if cut == "second stage" else calls
        )

    interrupt = False
    expect_cli(capsys, 0, "check")
    report = cli._load_report(wt)
    assert report.ok
    if changed_inputs:
        assert calls == [*previous_commands, "first", "second", "second"]
        assert report.results[-1].reused_from == prior_head
    else:
        assert calls == ["echo ok", "test -f README.md"] + (
            ["test -f README.md"] if cut == "second stage" else []
        )


def advance_checks(repo, commands):
    (repo / "workflow.toml").write_text(WORKFLOW.replace(
        '["echo ok", "test -f README.md"]', json.dumps(commands)))
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "trusted checks")
    git(repo, "push", "-q", "origin", "main")


@pytest.mark.parametrize("command,missing", [
    ("python3 scripts/gate.py", True), ("scripts/gate.py", True), ("python3 feature.py", False),
    ("test ! -f scripts/absent.py && echo fine", False), (".venv/bin/tool", False), ("VAR=./x echo fine", False)])
def test_check_preflights_missing_trusted_script(wt, repo, capsys, command, missing):
    advance_checks(repo, ["mkdir -p .venv/bin && cp /bin/true .venv/bin/tool; echo expensive-check", command, "rm -rf .venv/bin"])
    code, out, err = run(capsys, "check")
    assert code == int(missing) and out.endswith("FAIL\n" if missing else "PASS\n"), err
    assert ("expensive-check" in out) != missing
    if missing:
        assert "scripts/gate.py" in err and "integrate" in err and "trusted" in err

@pytest.mark.parametrize("uv,failed", [(True, True), (True, False), (False, True)])
def test_check_offline_cache_preflight(wt, repo, fake_tool, capsys, uv, failed):
    (repo / "pyproject.toml").write_text('[project]\nname = "sample"\nversion = "0"\n')
    command = "uv run --group dev pytest -q" if uv else "echo Failed to fetch package; exit 1"
    advance_checks(repo, ["echo expensive-check", command])
    git(wt, "merge", "-q", "origin/main")
    fake_tool("uv", 'echo "hatchling not found in cache"; exit 1' if failed else "exit 0")
    code, out, err = run(capsys, "check")
    assert code == int(failed) and out.endswith("FAIL\n" if failed else "PASS\n")
    assert ("expensive-check" in out) == (not uv or not failed) and ("uv sync --offline --locked --group dev" in out) == uv
    assert failed or [r["command"] for r in json.loads((wt / ".loopzero/checks.json").read_text())["results"]] == ["echo expensive-check", command]
    if failed:
        assert "effective UV_CACHE_DIR inside the sandbox is /tmp/uv-cache" in err and "uv sync --group dev" in err
        assert "`[checks] network = false`" in err
        assert "`[checks] writable` is warm" in err and "network = true" not in err


def test_check_sandbox_unavailable_exits_two(wt: Path, fake_tool, capsys) -> None:
    fake_tool("bwrap", "echo 'bwrap: No permissions' >&2\nexit 1\n")
    code, out, err = run(capsys, "check")
    assert code == 2 and out.startswith("checks pinned to origin/main@")
    assert "namespace creation failed: exit 1" in err
    assert "kernel.apparmor_restrict_unprivileged_userns=0" in err
    assert not (wt / ".loopzero" / "checks.json").exists()


@pytest.mark.parametrize("bootstrap", [False, True])
def test_config_flag_overrides_root(wt: Path, tmp_path: Path, capsys, bootstrap) -> None:
    if bootstrap:
        git(wt, "symbolic-ref", "--delete", "refs/remotes/origin/HEAD")
    alt = tmp_path / "alt.toml"
    alt.write_text(WORKFLOW.replace('"echo ok", "test -f README.md"', '"echo alt"'))
    code, out, _ = run(capsys, "--config", str(alt), "check")
    assert code == 0 and "echo alt" in out and "README" not in out
    assert out.startswith("checks are unpinned because --config was provided\n")


@pytest.mark.parametrize("trusted", [False, True])
def test_invalid_workflow_utf8_names_file(wt, capsys, trusted):
    data = WORKFLOW.encode().replace(b"echo ok", b"echo \xff")
    filename = "origin/main:workflow.toml" if trusted else "broken-workflow.toml"
    if trusted:
        (wt / "workflow.toml").write_bytes(data)
        git(wt, "add", "workflow.toml")
        git(wt, "commit", "-qm", "invalid workflow bytes")
        git(wt, "update-ref", "refs/remotes/origin/main", "HEAD")
    else:
        (wt / filename).write_bytes(data)
    args = ["status"] if trusted else ["--config", filename, "status"]

    _, err = expect_cli(capsys, 1, *args)
    assert filename in err and "utf-8" in err.lower() and "position " in err
    assert "Traceback" not in err


def test_check_uses_base_commands_when_worktree_weakens_them(wt: Path, capsys) -> None:
    (wt / "workflow.toml").write_text(WORKFLOW.replace('"echo ok", "test -f README.md"', '"true"'))
    out, _ = expect_cli(capsys, 0, "check", stderr="")
    assert "running: echo ok\n" in out and "running: test -f README.md\n" in out
    assert "  true" not in out


def test_check_reads_workflow_from_reported_base_commit(wt, repo, capsys):
    older_base = head_of(repo)
    advance_checks(repo, ["echo current-base"])
    base = head_of(repo)
    git(wt, "tag", "origin/main", older_base)

    out, _err = expect_cli(capsys, 0, "check", stderr="")
    lines = out.splitlines()
    assert f"checks pinned to origin/main@{base}" in lines
    assert "running: echo current-base" in lines
    assert "running: echo ok" not in lines


@pytest.mark.parametrize("command", ["check", "review", "ready", "merge"])
def test_privileged_commands_resolve_policy_independently_of_task(wt, command):
    (wt / "workflow.toml").write_text(WORKFLOW.replace('base = "main"', 'base = "topic"'))
    (wt / ".loopzero/task.md").write_text(f"Base: {head_of(wt)}\n")

    config = cli._load_config(SimpleNamespace(config=None, command=command), wt, pin_checks=True)

    assert config.base_branch == "main"
    assert config.checks == ("echo ok", "test -f README.md")


def test_trusted_default_workflow_can_select_delivery_branch(wt, repo, capsys):
    release_policy = WORKFLOW.replace('base = "main"', 'base = "release"')
    commit_file(repo, "workflow.toml", release_policy, "select release")
    git(repo, "push", "-q", "origin", "main")
    commit_file(repo, "workflow.toml", release_policy.replace("echo ok", "echo release"), "release checks")
    git(repo, "push", "-q", "origin", "HEAD:refs/heads/release")

    out, _err = expect_cli(capsys, 0, "check", stderr="")
    assert f"checks pinned to origin/release@{head_of(repo)}" in out
    assert "running: echo release" in out


def publish_upstream_change(repo):
    (repo / "upstream.py").write_text("upstream = 1\n")
    git(repo, "add", "upstream.py")
    git(repo, "commit", "-qm", "upstream change")
    git(repo, "push", "-q", "origin", "main")


def test_primary_review_diffs_from_merge_base_not_base_tip(
    wt: Path, repo: Path, gh: FakeGh, fake_bin: Path, capsys
) -> None:
    publish_upstream_change(repo)
    git(wt, "fetch", "-q", "origin")
    prepare_review(wt, gh, fake_bin, post_id=4)

    code, _, _ = run(capsys, "review")

    prompt = (fake_bin / "claude.stdin").read_text()
    assert code == 0 and "upstream = 1" not in prompt


def test_primary_review_stops_when_shallow_history_hides_merge_base(
    wt: Path, repo: Path, gh: FakeGh, fake_bin: Path, tmp_path: Path, monkeypatch, capsys
) -> None:
    publish_upstream_change(repo)
    git(wt, "push", "-q", "origin", "HEAD:refs/heads/lz/t1")
    shallow = tmp_path / "shallow"
    origin = tmp_path / "origin.git"
    git(repo, "clone", "-q", "--depth=1", "--no-single-branch", "--branch", "lz/t1",
        origin.as_uri(), str(shallow))
    git(shallow, "remote", "set-head", "origin", "main")
    (shallow / ".git/info/exclude").write_text(".loopzero/\n")
    shutil.copytree(wt / ".loopzero", shallow / ".loopzero")
    monkeypatch.chdir(shallow)
    prepare_review(shallow, gh, fake_bin)

    expect_cli(capsys, 1, "review", stderr_contains=("no merge base", "deepen or unshallow"))
    assert not (fake_bin / "claude.stdin").exists()
    assert all("--method" not in call["argv"] for call in gh.calls)


@pytest.mark.parametrize("workflow,error", [
    ("[checks\n", "invalid TOML"),
    ("checks = []\n", "[checks] must be a table"),
    ("delivery = []\n", "[delivery] must be a table"),
    (WORKFLOW.replace("[checks]", "[check]"), "unknown key(s) in top level: check"),
    pytest.param(None, "checks.commands", id="empty-commands"),
])
def test_check_rejects_invalid_base_workflow(wt, repo, capsys, workflow, error):
    if workflow is None:
        advance_checks(repo, [])
    else:
        commit_file(repo, "workflow.toml", workflow, "break workflow")
        git(repo, "push", "-q", "origin", "main")

    out, _ = expect_cli(capsys, 1, "check", stderr_contains=(f"origin/main:workflow.toml: {error}",))
    assert "PASS" not in out if workflow is None else out == ""
    assert not (wt / ".loopzero" / "checks.json").exists()


def test_readiness_rejects_existing_empty_check_receipt(wt: Path, repo: Path) -> None:
    advance_checks(repo, [])
    args = SimpleNamespace(config=None, command="ready")
    config = cli._load_config(args, wt, pin_checks=True)
    head = head_of(wt)
    cli._save_report(wt, CheckReport(head, False, (), cli.sandbox.settings_fingerprint(config, wt)))

    assert cli._current_check_report(wt, config, head) is None


# --- pr ------------------------------------------------------------------------------------


@pytest.mark.parametrize("token, status, installation_hint", [
    ("app-token", 404, True),
    ("", 404, False),
    ("app-token", 403, False),
])
def test_review_pr_lookup_failure(gh, wt, fake_bin, monkeypatch, capsys,
                                  token, status, installation_hint):
    monkeypatch.setattr(github, "app_token", lambda: token)
    gh.fail(pr_list_key(), f"gh: request failed (HTTP {status})")

    _, err = expect_cli(capsys, 1, "review")
    assert REPO in err and f"HTTP {status}" in err
    assert ("GitHub App installation" in err) == installation_hint
    if installation_hint:
        assert "rerun `loopzero review`" in err
    assert [call["token"] or "" for call in gh.calls] == [token, token]
    assert not (fake_bin / "claude.stdin").exists()


@pytest.mark.parametrize("command, key", [("pr", branch_history_key()), ("status", pr_list_key())])
@pytest.mark.parametrize("use_app", [True, False])
def test_pr_list_404_explains_app_installation(
    wt, repo, gh, fake_tool, monkeypatch, capsys, command, key, use_app,
):
    helper = fake_tool("app-token", "echo app-token") if use_app else wt / "missing-token"
    monkeypatch.setattr(github, "APP_TOKEN_HELPER", helper)
    monkeypatch.setenv("GH_TOKEN", "personal-token")
    gh.respond(key, {"message": "Not Found"}, exit=1, stderr="gh: Not Found (HTTP 404)")
    run(capsys, "check")

    _, err = expect_cli(capsys, 1, command)
    assert key.removeprefix("api ") in err and "gh: Not Found (HTTP 404)" in err
    assert '"message": "Not Found"' in err
    assert ("Check that the GitHub App installation includes this repository" in err) == use_app
    token = "app-token" if use_app else "personal-token"
    assert [(call["argv"], call["token"]) for call in gh.calls] == [
        ([*BRANCH_READ, "--include"], token), ([*key.split(), "--include"], token),
    ]
    assert git(repo.parent / "origin.git", "branch", "--list", "lz/t1").strip() == ""


@pytest.mark.parametrize("options", [(), ("--repost",)])
def test_review_pr_detail_404(gh, wt, monkeypatch, capsys, options):
    monkeypatch.setattr(github, "app_token", lambda: "app-token")
    endpoint = f"repos/{REPO}/pulls/7"
    gh.respond(pr_list_key(), [{"number": 7}])
    gh.fail(f"api {endpoint}", "gh: Not Found (HTTP 404)")

    _out, err = expect_cli(capsys, 1, "review", *options, stdout="")
    assert "PR #7" in err and endpoint in err and "HTTP 404" in err
    assert "branch" in err and "lz/t1" in err
    assert "rerun `" + " ".join(("loopzero", "review", *options)) + "`" in err
    assert [call["argv"] for call in gh.calls] == [
        [*BRANCH_READ, "--include"], [*PR_LIST_READ, "--include"], ["api", endpoint, "--include"],
    ]


def trace_pushes(fake_tool, tmp_path):
    pushes = tmp_path / "pushes"
    fake_tool("git", f'''\
if [ "$1" = "push" ]; then printf '%s\\n' "$*" >> "{pushes}"; fi
exec /usr/bin/git "$@"
''')
    return pushes


@pytest.mark.parametrize("branch", [
    "lz/t1", "t3/archive-review-receipts", "od183-fable-5-2", "codex/task", "lz/zażółć\u00a0",
])
def test_pr_pushes_and_creates_draft(
    wt: Path, repo: Path, gh: FakeGh, fake_tool, tmp_path: Path, capsys, branch
) -> None:
    git(wt, "branch", "-m", branch)
    if branch.endswith("\u00a0"):
        git(wt, "branch", branch.removesuffix("\u00a0"), "origin/main")
    history_key = branch_history_key().replace("lz%2Ft1", quote(branch, safe=""))
    gh.respond(history_key, [])
    run(capsys, "check")
    head = head_of(wt)
    gh.respond_pr_list([], branch=branch)
    gh.respond_pr_create(pr_json(headRefOid=head), branch=branch)
    pushes = trace_pushes(fake_tool, tmp_path)
    code, out, err = run(capsys, "pr")
    assert (code, out, err) == (0, URL + "\n", "")
    assert pushes.read_text() == f"push -u origin {branch}\n"
    origin = repo.parent / "origin.git"
    assert git(origin, "rev-parse", branch).strip() == head
    assert [c["argv"][:2] for c in gh.calls] == [
        branch_head_key(branch).split(), history_key.split(),
        branch_head_key(branch).split(), pr_list_key(branch).split(), ["api", f"repos/{REPO}/pulls"],
    ]
    create = gh.calls[-1]
    payload = json.loads(create["--input"])
    assert payload["draft"] is True and payload["head"] == branch
    heading = (wt / ".loopzero" / "task.md").read_text().splitlines()[0].lstrip("# ")
    assert payload["title"] == heading
    body = payload["body"]
    assert "## Context and goal" in body and git(repo, "rev-parse", "origin/main").strip() in body
    assert body.count("## Validation") == 1 and f"Recorded for `{head[:12]}`" in body
    assert "(filled by `loopzero check`)" not in body, "placeholder replaced in place"
    assert body.index("## Notes") < body.index("## Validation") < body.index("## Review"), "evidence trails the narrative"
    assert "- `echo ok`: exit 0" in body and "- `test -f README.md`: exit 0" in body


@pytest.mark.parametrize("refresh", [False, True])
def test_pr_validation_redacts_private_check_commands(wt, gh, capsys, tmp_path, refresh):
    command = (
        "git ls-remote 'https://x-access-token:ghs_FAKE0123@example.invalid/repo.git' "
        "&& curl -H 'Authorization: Bearer opaque-fake-header' https://example.invalid/ "
        r"&& curl -H Authorization:\ Bot\ opaque-fake-header https://example.invalid/ "
        """&& curl -d '{"Authorization": "Bearer opaque-fake-header"}' https://example.invalid/ """
        r'''&& curl -d "{\"Authorization\": \"Bearer opaque-fake-header\"}" https://example.invalid/ '''
        """&& python -c "request(headers={'Authorization': 'Basic opaque-fake-header'})" """
        """&& curl -H 'Authorization: Digest username="fake-user", response="opaque-fake-header"' """
        "https://example.invalid/"
    )
    private = tmp_path / "private.toml"
    private.write_text(WORKFLOW.replace(json.dumps("echo ok"), json.dumps(command)))
    cfg = config.load(private)
    head = head_of(wt)
    results = (CheckResult(command, 7, 0.1, ""), CheckResult("test -f README.md", 0, 0.1, ""))
    cli._save_report(wt, CheckReport(head, False, results, cli.sandbox.settings_fingerprint(cfg, wt)))

    if refresh:
        git(wt, "push", "-q", "-u", "origin", "lz/t1")
        gh.respond_pr_list([pr_json(headRefOid=head)])
        gh.respond(f"api repos/{REPO}/pulls/7 --method PATCH", {})
        cli._refresh_pr_checks(wt, cfg)
    else:
        gh.respond(branch_history_key(), [])
        gh.respond_pr_list([])
        gh.respond_pr_create(pr_json(headRefOid=head))
        assert run(capsys, "--config", str(private), "pr")[0] == 0

    body = json.loads(gh.calls[-1]["--input"])["body"]
    assert "ghs_FAKE0123" not in body and "opaque-fake-header" not in body
    assert "git ls-remote 'https://[REDACTED]@example.invalid/repo.git'" in body
    assert "curl -H 'Authorization: [REDACTED]' https://example.invalid/" in body
    assert "curl -H Authorization:[REDACTED] https://example.invalid/" in body
    assert """curl -d '{"Authorization": "[REDACTED]"}' https://example.invalid/""" in body
    assert r'''curl -d "{\"Authorization\": \"[REDACTED]\"}" https://example.invalid/''' in body
    assert """python -c "request(headers={'Authorization': '[REDACTED]'})" """ in body
    assert "'Authorization: [REDACTED]' https://example.invalid/`: exit 7" in body
    assert f"Recorded for `{head[:12]}`" in body and "(unverified" not in body
    assert "- `test -f README.md`: exit 0" in body
    assert cli._load_report(wt).results == results


@pytest.mark.parametrize("failure", ["merged", "CLOSED", "OPEN", "read", "malformed", "incomplete", "repeated", "detail"])
def test_pr_refuses_closed_or_unproven_branch_history(wt, gh, fake_tool, tmp_path, capsys, failure):
    head = head_of(wt)
    gh.respond_pr_create(pr_json(headRefOid=head))
    completed = failure in {"merged", "CLOSED", "OPEN"}
    if completed:
        merged = pr_json(state="MERGED", headRefOid=head)
        newer = pr_json(number=8, state=failure, headRefOid=head)
        gh.respond_pr_list([merged if failure == "merged" else newer])
        gh.respond(f"api repos/{REPO}/pulls/8 --method PATCH", {})
        history = [closed_history_row(merged)]
        if failure == "CLOSED":
            history.insert(0, closed_history_row(newer))
        gh.respond(branch_history_key(), history)
        gh.respond(f"api repos/{REPO}/pulls/7", rest_pr_json(merged))
    else:
        gh.respond_pr_list([])
        arm_unproven_history(gh, failure)
    pushes = trace_pushes(fake_tool, tmp_path)

    _, err = expect_cli(capsys, 1, "pr", stdout="")
    expected = ("#7", "merged", "loopzero start") if completed else ("loopzero pr:",)
    assert all(fragment in err for fragment in expected)
    assert not pushes.exists(), "branch was pushed"
    assert all("--method" not in c["argv"] for c in gh.calls), "GitHub was written"


def arm_unproven_history(gh, failure):
    forbidden = "HTTP 403 forbidden"
    if failure == "read":
        gh.fail(branch_history_key(), forbidden)
    elif failure == "malformed":
        gh.respond(branch_history_key(), [{"number": 7}])
    elif failure == "detail":
        gh.respond(branch_history_key(), [closed_history_row(pr_json(state="MERGED"))])
        gh.fail(f"api repos/{REPO}/pulls/7", forbidden)
    else:
        page = [closed_history_row(pr_json(number=n, state="CLOSED")) for n in range(1, 101)]
        gh.respond(branch_history_key(), page)
        if failure == "incomplete":
            gh.fail(branch_history_key(2), forbidden)
        else:
            gh.respond(branch_history_key(2), page)


def test_pr_replaces_closed_never_merged_pr(wt: Path, gh: FakeGh, capsys) -> None:
    closed = pr_json(state="CLOSED", headRefOid=head_of(wt))
    gh.respond(branch_history_key(), [closed_history_row(closed)])
    gh.respond_pr_list([closed])
    gh.respond_pr_create(pr_json(headRefOid=head_of(wt)))

    assert run(capsys, "pr") == (0, URL + "\n", "")


def test_pr_title_prefers_human_heading(wt: Path, gh: FakeGh, capsys) -> None:
    task = wt / ".loopzero" / "task.md"
    task.write_text("## Objective\nx\n\n# Add the feature flag\n")
    assert cli._pr_title(wt, "lz/t1") == "Add the feature flag"
    task.write_text("## Objective\nonly sections\n")
    assert cli._pr_title(wt, "lz/t1") == "lz/t1"


@pytest.mark.parametrize("name,value", [
    ("Context", ""),
    ("Problem", "<one or two plain sentences before code names: what goes wrong (one problem only), who notices, and evidence/cost; refs in brackets at the end>"),
    ("Goal", "<one plain sentence: what the owner or users now notice (and cost/saving if known), because this change fixes the problem>"),
])
def test_pr_refuses_missing_context_problem_or_goal(
    wt: Path, gh: FakeGh, capsys, name: str, value: str
) -> None:
    task = wt / ".loopzero" / "task.md"
    task.write_text(re.sub(
        rf"^- \*\*{name}:\*\*.*$", f"- **{name}:** {value}", task.read_text(),
        flags=re.MULTILINE,
    ))

    expect_cli(capsys, 1, "pr", stdout="", stderr_contains=(f"- **{name}:**",))
    assert gh.calls == []


@pytest.mark.parametrize("missing", [False, True], ids=["unfilled", "missing"])
def test_pr_explains_incomplete_task(wt, gh, capsys, missing):
    task = wt / ".loopzero" / "task.md"
    if missing:
        task.unlink()
        message = (
            f"loopzero pr: missing {task}. Run `loopzero start <slug>` for a new task worktree, "
            "or recreate the file using the handoff template at "
            "https://github.com/feder-positronics/loop-zero/blob/main/core/HANDOFF.md\n"
        )
    else:
        task.write_text("- **Context:**\n- **Problem:** <describe the problem>\n")
        message = (
            f"loopzero pr: fill '- **Context:**', '- **Problem:**', '- **Goal:**' in {task}"
            " (Context, Problem and Goal become the PR description)\n"
        )
    expect_cli(capsys, 1, "pr", stdout="", stderr=message)
    assert gh.calls == []


def test_pr_accepts_angle_brackets_and_sublists_as_filled_text(wt: Path) -> None:
    task = wt / ".loopzero" / "task.md"
    task.write_text(task.read_text().replace("The feature", "\n  - maps <PMID> via `Pick<T>`, then"))
    cli._require_pr_details(wt)


def test_progress_output_precedes_the_stderr_verdict_in_one_file(tmp_path: Path) -> None:
    script = ("import sys\nfrom loopzero import cli\ndef wait(args):\n"
              "    print('waiting')\n    raise cli.CliError('dequeued')\n"
              "cli.cmd_merge = wait\nsys.exit(cli.main(['merge']))\n")
    with (tmp_path / "log").open("w") as log:
        subprocess.run([sys.executable, "-c", script], stdout=log, stderr=subprocess.STDOUT, check=False)
    assert (tmp_path / "log").read_text().splitlines() == ["waiting", "loopzero merge: dequeued"]


@pytest.mark.parametrize("corrupt_report", [False, True], ids=["no-report", "corrupt-report"])
def test_pr_updates_existing_open_pr(wt, gh, capsys, corrupt_report, git_calls):
    if corrupt_report:
        (wt / ".loopzero" / "checks.json").write_text('{"results": "nope"}')
    arm_pr_body_patch(gh, head_of(wt))

    git_calls.clear()
    code, out, err = run(capsys, "pr")
    assert not any(call[:2] == ["merge-base", "--is-ancestor"] for call in git_calls)

    assert code == 0 and out == URL + "\n"
    assert ("warning: ignoring unreadable" in err) == corrupt_report
    assert [c["argv"][:2] for c in gh.calls] == (
        HISTORY_CALLS + LOOKUP_CALLS + [["api", f"repos/{REPO}/pulls/7"]] * 2)
    assert "(no `loopzero check` run recorded)" in gh.calls[-1]["--input"]


@pytest.mark.parametrize("probe,diagnostic", [
    (None, "fetch and integrate it first"),
    ("kill -TERM $$", "exit -15"),
    ("echo 'fatal: unable to read commit' >&2; exit 128", "unable to read commit"),
], ids=["missing-remote-head", "interrupted-probe", "failed-probe"])
def test_pr_aborts_when_ancestry_cannot_be_verified(wt, gh, fake_tool, tmp_path, capsys, probe, diagnostic):
    if probe is None:
        gh.respond_pr_list([pr_json(headRefOid="a" * 40)])
        pushes = trace_pushes(fake_tool, tmp_path)
    else:
        arm_pr_body_patch(gh, head_of(wt))
        git(wt, "commit", "--allow-empty", "-m", "Advance local head")
        pushes = tmp_path / "pushes"
        fake_tool("git", f'''\
if [ "$1" = "merge-base" ] && [ "$2" = "--is-ancestor" ]; then {probe}; fi
if [ "$1" = "push" ]; then touch "{pushes}"; exit 0; fi
exec /usr/bin/git "$@"
''')

    _, err = expect_cli(capsys, 1, "pr", stdout="")
    assert diagnostic in err
    if probe is not None:
        assert "git merge-base --is-ancestor" in err
    assert not pushes.exists()
    assert all("--method" not in call["argv"] for call in gh.calls)


@pytest.mark.parametrize("case", ["rewrite", "hook-fails", "wrong-destination"])
def test_pr_uses_lease_for_rewritten_open_pr(
    wt: Path, repo: Path, gh: FakeGh, fake_tool, tmp_path: Path, capsys, case: str
) -> None:
    old = head_of(wt)
    git(wt, "push", "-q", "-u", "origin", "lz/t1")
    git(wt, "reset", "-q", "--soft", "HEAD~1")
    git(wt, "commit", "-q", "-m", "rebased feature")
    head = head_of(wt)
    origin, other = repo.parent / "origin.git", tmp_path / "other.git"
    if case == "wrong-destination":
        shutil.copytree(origin, other)
        git(wt, "remote", "set-url", "--push", "origin", str(other))
    if case == "hook-fails":
        hook = fake_tool("pre-push", "echo 'lint failed in widget.py' >&2\nexit 1\n")
        git(wt, "config", "core.hooksPath", str(hook.parent))
    pushes = trace_pushes(fake_tool, tmp_path)
    gh.respond_pr_list([pr_json(headRefOid=old)])
    gh.respond(f"api repos/{REPO}/pulls/7 --method PATCH", {})

    code, out, err = run(capsys, "pr")

    if case == "wrong-destination":
        assert (code, out) == (1, "") and "origin push URL does not target" in err
        assert not pushes.exists()
        assert all(git(remote, "rev-parse", "refs/heads/lz/t1").strip() == old for remote in (origin, other))
        return
    if case == "hook-fails":
        assert (code, out) == (1, "") and "lint failed in widget.py" in err
        assert "remote moved" not in err and all("PATCH" not in call["argv"] for call in gh.calls)
    else:
        assert (code, out, err) == (0, URL + "\n", "")
    assert pushes.read_text().strip() == (
        f"push -u origin --porcelain --force-with-lease=lz/t1:{old} lz/t1"
    )
    assert git(origin, "rev-parse", "refs/heads/lz/t1").strip() == (old if case == "hook-fails" else head)


@pytest.mark.parametrize("shared_object", [False, True])
def test_pr_reports_rejected_lease_without_overwriting_remote(
    wt: Path, repo: Path, gh: FakeGh, tmp_path: Path, capsys, shared_object: bool
) -> None:
    old = head_of(wt)
    origin = repo.parent / "origin.git"
    git(wt, "push", "-q", "-u", "origin", "lz/t1")
    git(wt, "reset", "-q", "--soft", "HEAD~1")
    git(wt, "commit", "-q", "-m", "rebased feature")
    mover = tmp_path / "mover"
    git(tmp_path, "clone", "-q", str(origin), str(mover))
    git(mover, "checkout", "-q", "-b", "lz/t1", "origin/lz/t1")
    commit_file(mover, "remote.txt", "moved\n", "remote move")
    git(mover, "push", "-q", "origin", "lz/t1")
    moved = git(origin, "rev-parse", "refs/heads/lz/t1").strip()
    if shared_object:
        git(repo, "fetch", "-q", "origin", "lz/t1")
    gh.respond_pr_list([pr_json(headRefOid=moved if shared_object else old)])

    diagnostic = "absent from this task branch's history" if shared_object else "remote moved"
    expect_cli(capsys, 1, "pr", stdout="", stderr_contains=(diagnostic,))
    assert git(origin, "rev-parse", "refs/heads/lz/t1").strip() == moved


def test_pr_refuses_to_overwrite_a_pr_head_it_has_never_seen(
    wt: Path, repo: Path, gh: FakeGh, capsys
) -> None:
    origin = repo.parent / "origin.git"
    git(wt, "push", "-q", "-u", "origin", "lz/t1")
    pushed = git(origin, "rev-parse", "refs/heads/lz/t1").strip()
    gh.respond_pr_list([pr_json(headRefOid="f" * 40)])  # head pushed by someone else

    expect_cli(capsys, 1, "pr", stdout="", stderr_contains=("is not in this worktree",))
    assert git(origin, "rev-parse", "refs/heads/lz/t1").strip() == pushed


@pytest.mark.parametrize("flag, pattern", [("--version", r"loopzero \S+ .+\n\Z"), ("--help", r"usage: loopzero ")])
def test_cli_info_leaves_delivery_backends_unloaded(flag, pattern):
    for entry in (("-m", "loopzero.cli"), ("-c", "from loopzero._cli_entry import main; main()")):
        done = subprocess.run([sys.executable, "-X", "importtime", *entry, flag], capture_output=True, text=True)
        assert done.returncode == 0 and re.match(pattern, done.stdout), done
        assert not re.search(r"loopzero\.(github|mergify|runners|sandbox)\b", done.stderr)


def test_pr_refuses_dirty(wt: Path, gh: FakeGh, capsys) -> None:
    (wt / "scratch.txt").write_text("x")
    expect_cli(capsys, 1, "pr", stdout="", stderr_contains=("uncommitted or untracked",))
    assert gh.calls == []


# --- review ---------------------------------------------------------------------------


def arm_pr_body_patch(gh: FakeGh, head: str, **fields) -> None:
    gh.respond_pr_list([pr_json(headRefOid=head, **fields)])
    gh.respond(f"api repos/{REPO}/pulls/7 --method PATCH", {})


def arm_pr(gh: FakeGh, head: str, *, branch: str = "lz/t1", **over) -> None:
    body = "# T1\n\n## Review\n(filled by `loopzero review`)\n\n## Notes\n"
    gh.respond_pr_list([pr_json(headRefOid=head, body=body, **over)], branch=branch)
    gh.fail(f"api repos/{REPO}/git/ref/heads/{branch}", "gh: Not Found (HTTP 404)")
    gh.respond(f"api repos/{REPO}/pulls/7 --method PATCH", {})


def arm_empty_primary_review_history(gh: FakeGh, head: str, *, branch="lz/t1") -> None:
    arm_pr(gh, head, branch=branch)
    gh.respond(reviews_key(), [])


def arm_empty_primary_review_post(
    gh: FakeGh, head: str, post_id: int = 1, *, branch="lz/t1"
) -> None:
    arm_empty_primary_review_history(gh, head, branch=branch)
    gh.respond(post_key(), {"id": post_id})


def prepare_review(wt: Path, gh: FakeGh, fake_bin: Path, *, payload=APPROVE,
                   history=(), post_id: int = 1, branch="lz/t1") -> str:
    head = head_of(wt)
    arm_empty_primary_review_post(gh, head, post_id, branch=branch)
    gh.respond(reviews_key(), list(history))
    _fake_claude(fake_bin, _claude_envelope(payload))
    return head


def posted_review(gh: FakeGh) -> dict:
    return json.loads(next(
        call["--input"] for call in reversed(gh.calls)
        if call["argv"][1] == post_key().split(" ")[1]
    ))


def arm_approved_ready_pr(gh: FakeGh, head: str, wt: Path, **pr_overrides) -> None:
    arm_approved_pr_with_check_result(gh, head, wt, "success", **pr_overrides)


def arm_squash_merge(gh: FakeGh, head: str, wt: Path, *snapshots, branch: str = "lz/t1") -> None:
    arm_approved_ready_pr(gh, head, wt, isDraft=False, branch=branch)
    gh.respond("pr merge", "")
    if snapshots:
        gh.append(f"api repos/{REPO}/pulls/7", *snapshots)


def arm_approved_pr_with_check_result(
    gh: FakeGh, head: str, wt: Path, conclusion: str, *, review_login: str = LOGIN,
    reviews=None, readiness=None, **pr_overrides,
) -> None:
    arm_pr(gh, head, **pr_overrides)
    gh.respond(reviews_key(), reviews if reviews is not None else [rev(head, "primary", login=review_login)])
    arm_readiness(gh, head, wt, conclusion=conclusion, **(readiness or {}))


def publish_workflow(wt: Path, policy: str, message: str = "configure delivery") -> None:
    repo = wt.parent.parent
    commit_file(repo, "workflow.toml", policy, message)
    git(repo, "push", "-q", "origin", "main")


def stub_mergify_admission(monkeypatch, *, configured=True):
    if configured is not None:
        monkeypatch.setattr(mergify, "configured", lambda *_: configured)
    monkeypatch.setattr(mergify, "markers", lambda *_: (False, False))
    monkeypatch.setattr(mergify, "membership", lambda *_: None)


def commit_mergify_configuration(wt: Path, message: str = "use mergify") -> str:
    policy = (wt.parent.parent / "workflow.toml").read_text().replace(
        'merge = "squash"', 'merge = "mergify"\nmergify_queue = "main"'
    )
    publish_workflow(wt, policy, message)
    return head_of(wt)


@pytest.mark.parametrize("branch", [
    "lz/t1", "t3/archive-review-receipts", "od183-fable-5-2", "codex/task",
])
def test_review_primary_posts_marker(wt: Path, gh: FakeGh, fake_bin: Path, capsys, branch, git_calls) -> None:
    git(wt, "branch", "-m", branch)
    head = prepare_review(wt, gh, fake_bin, payload=CHANGES, branch=branch)
    task = (wt / ".loopzero/task.md").read_text()
    expected_diff = git(wt, "diff", "--no-color", "origin/main", head).encode("utf-8")
    git_calls.clear()
    code, out, err = run(capsys, "review")
    assert_git_cost(git_calls, 14, 1)
    assert (code, err) == (5, "")  # request_changes: the exit code is the verdict
    assert out.splitlines()[0].startswith("reviewer paths pinned to origin/main@")
    assert out.splitlines()[-1] + "\n" == (
        f"primary review by claude on {head[:12]}: request_changes "
        "(0 critical, 1 important, 1 suggestion)\n"
    )
    post = next(c for c in gh.calls if c["argv"][1] == post_key().split(" ")[1])
    payload = json.loads(post["--input"])
    assert payload["commit_id"] == head and payload["event"] == "REQUEST_CHANGES"
    assert marker(head, "primary") in payload["body"]
    header, diff = (fake_bin / "claude.stdin").read_bytes().split(b"\n", 1)
    assert json.loads(header) == {
        "head": head, "kind": "primary", "task_text": task, "diff_utf8_bytes": len(expected_diff),
    }
    assert diff == expected_diff and b"+print('hi')" in diff
    argv = (fake_bin / "claude.argv").read_bytes().decode("utf-8").split("\0")[:-1]
    policy = argv[argv.index("--append-system-prompt") + 1]
    assert "primary review of the whole change" in policy and policy.encode("utf-8") not in header + diff
    updated = json.loads(gh.calls[-1]["--input"])["body"]
    assert f"- primary, claude, {head[:12]}, request_changes" in updated


def test_overlapping_review_dispatches_and_publishes_once(wt, gh, fake_bin, capsys, monkeypatch):
    prepare_review(wt, gh, fake_bin)
    review = cli._run_review
    dispatched, overlapping = [], []

    def overlap(*args, **kwargs):
        dispatched.append(args[2])
        if len(dispatched) == 1:
            overlapping.append(run(capsys, "review"))
        return review(*args, **kwargs)

    monkeypatch.setattr(cli, "_run_review", overlap)
    assert run(capsys, "review")[0] == 0
    assert len(dispatched) == 1
    assert overlapping[0][0] == 1 and "review already running" in overlapping[0][2]
    assert sum(call["argv"][1] == post_key().split()[1] and "--method" in call["argv"]
               for call in gh.calls) == 1


@pytest.mark.parametrize("refresh_fails", [False, True])
def test_review_summary_uses_body_edited_during_review(wt, gh, fake_bin, capsys, monkeypatch, refresh_fails):
    head = prepare_review(wt, gh, fake_bin)
    detail_key = f"api repos/{REPO}/pulls/7"
    live = "# T1\n\n## Notes\nEdited during review\n\n## Validation\nNew check results\n"
    run_review = cli._run_review

    def change_body_during_review(*args, **kwargs):
        result = run_review(*args, **kwargs)
        if refresh_fails:
            gh.respond(detail_key, {}, exit=1, stderr="body read failed")
        else:
            detail = rest_pr_json({**pr_json(headRefOid=head), "body": live})
            gh.respond(detail_key, detail)
        return result

    monkeypatch.setattr(cli, "_run_review", change_body_during_review)
    _, err = expect_cli(capsys, 0, "review", stdout_contains=(f"primary review by claude on {head[:12]}: approve",))
    posted_review(gh)
    post_index = next(i for i, call in enumerate(gh.calls) if call["argv"][:2] == post_key().split())
    assert gh.calls[post_index + 1]["argv"] == [*detail_key.split(), "--include"]
    patches = [call for call in gh.calls if "PATCH" in call["argv"]]
    if refresh_fails:
        assert not patches
        assert "could not append PR review summary" in err and "body read failed" in err
    else:
        assert len(patches) == 1 and not err
        assert json.loads(patches[0]["--input"])["body"] == (
            f"{live}\n## Review\n- primary, claude, {head[:12]}, approve\n"
        )


@pytest.mark.parametrize("source", ["task", "base", "override", "missing_ref", "bootstrap"])
def test_review_runtime_paths_use_only_verified_base(
    wt: Path, gh: FakeGh, fake_bin: Path, capsys, tmp_path: Path, source: str,
) -> None:
    workflow = wt / "workflow.toml"
    exposed = tmp_path / "host-state"
    exposed.mkdir()
    (exposed / "history.txt").write_text("private host history")
    directive = f'reviewer_ro_paths = ["{exposed}"]\n'
    workflow.write_text(workflow.read_text().replace('[delivery]\n', '[delivery]\n' + directive))
    git(wt, "add", "workflow.toml")
    git(wt, "commit", "-qm", "change task runtime paths")
    if source == "base":
        git(wt, "update-ref", "refs/remotes/origin/main", head_of(wt))
    elif source == "missing_ref":
        git(wt, "update-ref", "-d", "refs/remotes/origin/main")
    elif source == "bootstrap":
        git(wt, "update-ref", "refs/remotes/origin/main",
            git(wt, "rev-list", "--max-parents=0", "HEAD").strip())
    prepare_review(wt, gh, fake_bin)
    code, out, err = run(capsys, *(["--config", str(workflow)] if source == "override" else []),
                         "review")
    if source in {"missing_ref", "bootstrap"}:
        assert code == 1 and "--config" in err
        assert not (fake_bin / "claude.stdin").exists()
        assert not any(c["argv"][1] == post_key().split(" ")[1] for c in gh.calls)
    else:
        assert code == 0 and err == ""
        mounts = (tmp_path / "bwrap.argv").read_text().splitlines()
        assert (str(exposed) in mounts) == (source != "task")
        assert "reviewer paths" in out


@pytest.mark.parametrize("command", ["check", "review", "ready", "merge", "status"])
@pytest.mark.parametrize("missing", ["default", "remote", "workflow"])
def test_runtime_pinning_requires_trusted_workflow(
    wt: Path, gh: FakeGh, fake_bin: Path, capsys, command: str, missing: str,
) -> None:
    if missing == "default":
        git(wt, "symbolic-ref", "--delete", "refs/remotes/origin/HEAD")
    elif missing == "remote":
        git(wt, "update-ref", "-d", "refs/remotes/origin/main")
    else:
        git(wt, "update-ref", "refs/remotes/origin/main",
            git(wt, "rev-list", "--max-parents=0", "HEAD").strip())
    prepare_review(wt, gh, fake_bin)
    code, out, err = run(capsys, command)
    assert "reviewer paths" not in out + err
    assert "--config" in err
    assert code == 1
    assert not gh.calls


@pytest.mark.parametrize("family,options,model,effort", [
    ("claude", (), "opus", "medium"),
    ("claude", ("--model", "sonnet", "--effort", "high"), "sonnet", "high"),
    ("codex", (), "gpt-5.6-luna", "high"),
])
def test_review_passes_and_records_model_effort(
    wt: Path, gh: FakeGh, fake_bin: Path, capsys, family, options, model, effort
) -> None:
    configured_model = "opus" if family == "claude" else model
    configured_effort = "medium" if family == "claude" else effort
    reviewers = [family] if family == "codex" else ["claude", "codex"]
    publish_workflow(wt, WORKFLOW.replace('["claude", "codex"]', json.dumps(reviewers)) + f"""
[delivery.review.{family}]
model = "{configured_model}"
effort = "{configured_effort}"
allowed_efforts = {json.dumps(["high"] if family == "codex" else ["medium", "high"])}
""")
    head = prepare_review(wt, gh, fake_bin)
    if family == "codex":
        _fake_codex(fake_bin, json.dumps(APPROVE))
    _, _err = expect_cli(capsys, 0, "review", *options, stderr="")
    if family == "claude":
        argv = (fake_bin / "claude.argv").read_text().split("\0")
        assert argv[argv.index("--model") + 1] == model
        assert argv[argv.index("--effort") + 1] == effort
    saved = json.loads((wt / ".loopzero" / f"review-{head[:12]}-primary.json").read_text())
    assert (saved["model"], saved["effort"]) == (model, effort)
    assert f"model {model} (effort {effort})" in posted_review(gh)["body"]


@pytest.mark.parametrize("options,configured", [((), "max"), (("--effort", "max"), "medium")])
def test_review_rejects_disallowed_effort_before_launch(
    wt: Path, gh: FakeGh, fake_bin: Path, capsys, options, configured: str
) -> None:
    publish_workflow(wt, WORKFLOW + f"""
[delivery.review.claude]
effort = "{configured}"
allowed_efforts = ["medium"]
""")
    head = head_of(wt)
    arm_empty_primary_review_history(gh, head)

    expect_cli(capsys, 1, "review", *options, stdout="", stderr_contains=("effort 'max' is not allowed for claude",))
    assert not (fake_bin / "claude.argv").exists()


def test_review_refuses_when_reviewer_dirties_worktree(
    wt: Path, gh: FakeGh, fake_bin: Path, capsys, head
) -> None:
    arm_empty_primary_review_history(gh, head)
    reviewer = _fake_claude(fake_bin, _claude_envelope(APPROVE))
    reviewer.write_text(reviewer.read_text().replace("#!/bin/sh\n", "#!/bin/sh\ntouch drift.txt\n"))

    code, out, err = run(capsys, "review")

    saved = wt / ".loopzero" / f"review-{head[:12]}-primary.json"
    assert code == 1 and out.startswith("reviewer paths pinned") and "worktree changed during review" in err
    assert err.count(head) == 2 and not saved.exists()
    assert all(call["argv"][:2] != ["api", post_key().split(" ")[1]] for call in gh.calls)


@pytest.mark.parametrize("option", ["--model", "--effort"])
@pytest.mark.parametrize("repost_first", [True, False])
def test_review_repost_rejects_reviewer_overrides(monkeypatch, capsys, option, repost_first):
    monkeypatch.setattr(cli, "_context", lambda *_, **_kw: pytest.fail("loaded context for invalid review"))
    override = [option, "override"]
    options = ["--repost", *override] if repost_first else [*override, "--repost"]
    with pytest.raises(SystemExit) as stop:
        cli.main(["review", *options])

    assert stop.value.code == 2
    assert f"--repost cannot be combined with {option}" in capsys.readouterr().err


@pytest.mark.parametrize("padding", ["", "x" * 574_264], ids=["short", "long"])
def test_review_saves_result_and_repost_skips_model(wt: Path, gh: FakeGh, fake_bin: Path,
                                                    capsys, padding: str, head) -> None:
    arm_empty_primary_review_history(gh, head)
    gh.fail(post_key(), "HTTP 500 server error")
    envelope = {"padding": padding, "synthetic_metadata": "private-runner-metadata",
                **_claude_envelope(CHANGES, modelUsage={"claude-sonnet-4-6": {}})}
    _fake_claude(fake_bin, envelope)
    saved = wt / ".loopzero" / f"review-{head[:12]}-primary.json"
    code, out, err = run(capsys, "review")
    assert code == 1 and out.startswith("reviewer paths pinned") and saved.exists()
    assert f"review saved at {saved}" in err and "loopzero review --repost" in err
    saved_bytes = saved.read_bytes()
    data = json.loads(saved_bytes)
    assert json.loads(data["raw"]) == envelope
    assert saved.stat().st_mode & 0o777 == 0o600
    assert data["head"] == head and data["kind"] == "primary" and data["family"] == "claude"
    assert data["verdict"] == "request_changes" and len(data["findings"]) == 2
    assert data["model"] == "claude-sonnet-4-6" and data["duration_s"] >= 0
    first_body = posted_review(gh)["body"]
    assert "model claude-sonnet-4-6" in first_body
    assert len(first_body.encode("utf-8")) < 65_536
    assert "private-runner-metadata" not in first_body and "Raw reviewer output" not in first_body
    # Repost can still read ordinary receipts containing the old pair defaults.
    data.update(members=[], reconciliation="", pair_authority=None, pair_prompt_sha256=None)
    saved.write_text(json.dumps(data))
    (fake_bin / "claude.stdin").unlink()
    gh.respond(post_key(), {"id": 5})
    code, out, err = run(capsys, "review", "--repost")
    assert (code, err) == (5, "") and out.splitlines()[-1].startswith("primary review by claude"), err
    assert not (fake_bin / "claude.stdin").exists(), "no model invoked"
    payload = posted_review(gh)
    assert marker(head, "primary", "repost") in payload["body"] and "Nit" in payload["body"]
    assert "claude-session-123" in payload["body"]
    assert len(payload["body"].encode("utf-8")) < 65_536
    assert "private-runner-metadata" not in payload["body"] and "Raw reviewer output" not in payload["body"]
    assert saved.read_bytes() == saved_bytes
    assert "model claude-sonnet-4-6" in payload["body"]
    assert [c["body"].split("\n")[-1] for c in payload["comments"]] == ["Off by one."]


@pytest.mark.parametrize("repost", [False, True], ids=["first-save", "repost"])
def test_interrupted_review_save_preserves_previous_receipt(wt, gh, fake_bin, capsys, monkeypatch, repost):
    head = prepare_review(wt, gh, fake_bin)
    saved = wt / ".loopzero" / f"review-{head[:12]}-primary.json"
    if repost:
        gh.fail(post_key(), "HTTP 500 server error")
        code, _, err = run(capsys, "review")
        assert code == 1 and f"review saved at {saved}" in err
        original = saved.read_bytes()
        (fake_bin / "claude.stdin").unlink()
        gh.respond(post_key(), {"id": 5})
        args = ["review", "--repost"]
    else:
        args = ["review"]
    calls_before = len(gh.calls)
    encode = json.dumps

    def interrupt_encoding(value, *args, **kwargs):
        if isinstance(value, dict) and "publication" in value:
            raise KeyboardInterrupt
        return encode(value, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(json, "dumps", interrupt_encoding)
        assert cli.main(args) == 130
    assert all(call["argv"][:2] != post_key().split() for call in gh.calls[calls_before:])

    if repost:
        assert saved.read_bytes() == original
        assert run(capsys, "review", "--repost")[0] == 0
        assert saved.read_bytes() == original
        assert saved.stat().st_mode & 0o777 == 0o600
        assert not (fake_bin / "claude.stdin").exists()
    else:
        assert not saved.exists()


def test_repost_refuses_retired_paired_review(
    wt: Path, gh: FakeGh, fake_bin: Path, capsys
) -> None:
    head = prepare_review(wt, gh, fake_bin)
    code, _, err = run(capsys, "review")
    assert (code, err) == (0, "")
    saved = wt / ".loopzero" / f"review-{head[:12]}-primary.json"
    data = json.loads(saved.read_text())
    data["members"] = [{"family": "claude"}]
    saved.write_text(json.dumps(data))
    (fake_bin / "claude.stdin").unlink()
    _fake_codex(fake_bin, json.dumps(APPROVE))
    prior_calls = len(gh.calls)

    _, err = expect_cli(capsys, 1, "review", "--repost")
    assert "invalid saved review" in err
    assert not (fake_bin / "claude.stdin").exists()
    assert not (fake_bin / "codex.stdin").exists()
    assert not any(
        call["argv"][:2] == post_key().split() for call in gh.calls[prior_calls:]
    )


@pytest.mark.parametrize("saved_state", ["missing", "stale", "hand-written"])
def test_repost_refuses_missing_stale_or_hand_written_review(
    wt: Path, gh: FakeGh, capsys, saved_state: str, head
) -> None:
    arm_empty_primary_review_history(gh, head)
    if saved_state != "missing":
        saved = wt / ".loopzero" / f"review-{head[:12]}-primary.json"
        saved.write_text(json.dumps({
            "family": "claude", "head": "0" * 40 if saved_state == "stale" else head,
            "kind": "primary", "verdict": "approve", "findings": [],
            "raw": "" if saved_state == "stale" else json.dumps(_claude_envelope(APPROVE)),
        }))
    code, out, err = run(capsys, "review", "--repost")
    expected = {"missing": "no saved primary review", "stale": f"not HEAD {head[:12]}",
                "hand-written": "not runner-produced"}
    assert code == 1 and out == "" and expected[saved_state] in err
    assert all(c["argv"][1] != post_key().split(" ")[1] for c in gh.calls)


@pytest.mark.parametrize("chunk_auth", [False, True])
def test_review_chunks_oversized_diff_with_same_family(
    wt: Path, gh: FakeGh, fake_tool, fake_bin, capsys, chunk_auth, git_calls
) -> None:
    workflow = (wt / "workflow.toml").read_text().replace(
        'reviewers = ["claude", "codex"]',
        'reviewers = ["claude", "codex"]\nreview_chunk_bytes = 4096',
    )
    publish_workflow(wt, workflow)
    for name in ("large_a.py", "large_b.py"):
        (wt / name).write_text("x = 1\n" * 900)
    git(wt, "add", "large_a.py", "large_b.py")
    git(wt, "commit", "-q", "-m", "large review")
    head = head_of(wt)
    arm_empty_primary_review_post(gh, head, 8)
    envelope = json.dumps(_claude_envelope(APPROVE))
    _fake_codex(fake_bin, json.dumps(APPROVE))
    fake_tool("claude", f"""
        input=$(mktemp "{fake_bin}/claude.chunk.XXXXXX")
        echo "$input" >> "{fake_bin}/claude.calls"
        printf '%s\\0' "$@" > "$input.argv"
        cat > "$input"
        if [ "$(grep -c '^diff --git ' "$input")" -gt 1 ]; then
            echo 'Prompt is too long' >&2
            exit 1
        fi
        {"echo 'not authenticated' >&2; exit 1" if chunk_auth else ""}
        echo '{envelope}'
    """)

    git_calls.clear()
    code, out, err = run(capsys, "review")
    probes = [cmd for cmd in git_calls if "--git-common-dir" in cmd or "--git-dir" in cmd]
    assert len(probes) == (3 if chunk_auth else 6), probes

    assert code == 0 and f"review by {'codex' if chunk_auth else 'claude'}" in out
    materials, policies, tasks, fragments = [], [], [], []
    for name in (fake_bin / "claude.calls").read_bytes().split(b"\n")[:-1]:
        path = Path(name.decode())
        argv = path.with_suffix(path.suffix + ".argv").read_bytes().decode().split("\0")[:-1]
        policies.append(argv[argv.index("--append-system-prompt") + 1])
        header, diff = path.read_bytes().split(b"\n", 1)
        metadata = json.loads(header)
        assert metadata["head"] == head and metadata["kind"] == "primary"
        assert metadata["diff_utf8_bytes"] == len(diff)
        tasks.append(metadata["task_text"])
        fragments.append(metadata.get("fragment"))
        materials.append(diff)
    assert len(set(tasks)) == 1 and len(materials) > 1
    assert "fragment" not in policies[0] and len(set(policies[1:])) == 1
    assert "do not judge whether the whole change is complete" in policies[1]
    full_diff, *chunks = materials
    assert all(len(part) <= 4096 for part in chunks)
    assert fragments[0] is None
    if chunk_auth:
        assert len(chunks) == 1 and full_diff.startswith(chunks[0])
        assert re.fullmatch(r"chunk 1 of (?:[2-9]|[1-9][0-9]+)", fragments[1])
        header, fallback_diff = (fake_bin / "codex.stdin").read_bytes().split(b"\n", 1)
        fallback = json.loads(header)
        assert fallback_diff == full_diff and fallback["task_text"] == tasks[0]
        assert "fragment" not in fallback
    else:
        assert fragments[1:] == [f"chunk {i} of {len(chunks)}" for i in range(1, len(chunks) + 1)]
        assert b"".join(chunks) == full_diff
    if chunk_auth:
        assert "reviewer claude unavailable:" in err and "CLI is not authenticated" in err
        return
    assert "reviewing" in err and "chunks" in err
    payload = posted_review(gh)
    assert "Reviewed in " in payload["body"] and " chunks" in payload["body"]
    saved = json.loads(
        (wt / ".loopzero" / f"review-{head[:12]}-primary.json").read_text()
    )
    assert saved["chunk_count"] > 1
    assert len(saved["provenance"]["session_ids"]) == saved["chunk_count"]


@pytest.mark.parametrize("family,configured,note,code,diagnostic,no_post", [
    pytest.param("claude", None, "", 4, "saved reviewer family claude contributed", False, id="missing-fallback"),
    pytest.param("claude", None, "independent reviewers failed: codex: unavailable",
                 0, "", False, id="disclosed-fallback"),
    pytest.param("codex", None, "independent reviewers failed: claude: unavailable",
                 1, "codex is not a configured reviewer family", False, id="independent-family"),
    pytest.param("claude", None, "independent reviewers failed: /home/u/secret: boom",
                 1, "not in the published form", True, id="legacy-raw-note"),
    pytest.param("claude", '["codex"]', "independent reviewers failed: codex: unavailable",
                 1, "claude is not a configured reviewer family", True, id="unconfigured-author"),
    pytest.param("claude", None, "every configured family contributed (claude)",
                 1, "not in the published form", True, id="wrong-contributors"),
    pytest.param("claude", None, "independent reviewers failed: claude: unavailable",
                 1, "not in the published form", True, id="wrong-failed-family"),
])
def test_review_repost_requires_valid_fallback(wt, gh, fake_bin, capsys, family, configured,
                                              note, code, diagnostic, no_post):
    from dataclasses import replace

    if configured:
        publish_workflow(wt, WORKFLOW.replace('reviewers = ["claude", "codex"]', f'reviewers = {configured}'))
    git(wt, "commit", "-q", "--allow-empty", "-m", "by claude\n\nCo-Authored-By: Claude <n@a.c>")
    head = prepare_review(wt, gh, fake_bin)
    if family == "codex":
        _fake_codex(fake_bin, json.dumps(APPROVE))
    result = cli.runners.review_with(family, cwd=wt, head=head, kind="primary",
                                     task_text="task", diff="diff")
    cli._save_review(wt, replace(result, fallback=note))
    actual, _out, err = run(capsys, "review", "--repost")
    assert actual == code
    if diagnostic:
        assert diagnostic in err
    if code == 0:
        assert "Same-family fallback:" in posted_review(gh)["body"]
    if no_post:
        assert not any(call["argv"][1] == post_key().split(" ")[1] for call in gh.calls)


def test_review_delta_diffs_since_primary(wt: Path, gh: FakeGh, fake_bin: Path, capsys) -> None:
    first = head_of(wt)
    commit_file(wt, "second.py", "x = 2\n", "second")
    head = prepare_review(wt, gh, fake_bin, post_id=2, history=[
        rev(first, "primary", state="CHANGES_REQUESTED", verdict="request_changes")])
    task = (wt / ".loopzero/task.md").read_text()
    expected_diff = git(wt, "diff", "--no-color", first, head).encode("utf-8")
    code, out, _ = run(capsys, "review")
    assert code == 0 and out.splitlines()[-1].startswith(f"delta review by claude on {head[:12]}: approve")
    header, diff = (fake_bin / "claude.stdin").read_bytes().split(b"\n", 1)
    assert json.loads(header) == {
        "head": head, "kind": "delta", "task_text": task, "diff_utf8_bytes": len(expected_diff),
    }
    assert diff == expected_diff and b"+x = 2" in diff and b"print('hi')" not in diff
    argv = (fake_bin / "claude.argv").read_bytes().decode("utf-8").split("\0")[:-1]
    policy = argv[argv.index("--append-system-prompt") + 1]
    assert "delta review: only review the diff in the material message" in policy
    assert policy.encode("utf-8") not in header + diff
    payload = posted_review(gh)
    assert marker(head, "delta") in payload["body"]


def test_review_delta_refuses_base_merge_since_primary(wt, repo, gh, fake_bin, capsys) -> None:
    reviewed = head_of(wt)  # intelflo#8393: primary-reviewed head, a merge of main, the delta
    (repo / "upstream.py").write_text("y = 1\n")
    git(repo, "add", "upstream.py")
    git(repo, "commit", "-q", "-m", "landed on main")
    git(wt, "merge", "-q", "--no-edit", "main")
    merge = head_of(wt)
    git(wt, "commit", "-q", "--allow-empty", "-m", "fix after review")
    prepare_review(wt, gh, fake_bin, history=[rev(reviewed, "primary", state="CHANGES_REQUESTED")])
    code, out, err = run(capsys, "review")
    assert code != 0 and out == "" and not (fake_bin / "claude.stdin").exists()
    assert f"merge commit {merge[:12]} since the reviewed head {reviewed[:12]}" in err
    assert f"`git reset --hard {reviewed}`" in err
    assert "cherry-pick any later fixes" in err
    assert "run `loopzero pr`, then `loopzero review` before merging main again" in err


def test_review_delta_replays_fixes_when_reviewed_head_is_second_parent(
    wt: Path, gh: FakeGh, fake_bin: Path, capsys
) -> None:
    reviewed = head_of(wt)
    git(wt, "reset", "--hard", "main")
    git(wt, "commit", "-q", "--allow-empty", "-m", "independent branch work")
    git(wt, "merge", "-q", "--no-edit", reviewed)
    prepare_review(wt, gh, fake_bin, history=[rev(reviewed, "primary", state="CHANGES_REQUESTED")])
    _fake_codex(fake_bin, json.dumps(APPROVE))

    _, err = expect_cli(capsys, 1, "review")
    assert f"replay the fixes on the reviewed head {reviewed[:12]}" in err
    assert "git reset --hard" not in err
    assert not (fake_bin / "claude.stdin").exists()
    assert not (fake_bin / "codex.stdin").exists()


def test_review_delta_refuses_rewritten_primary(wt, gh, fake_bin, capsys):
    reviewed = head_of(wt)
    git(wt, "commit", "--amend", "-q", "-m", "rewritten feature")
    prepare_review(wt, gh, fake_bin, history=[rev(reviewed, "primary")])
    code, out, err = run(capsys, "review")
    assert code == 1 and not out and not (fake_bin / "claude.stdin").exists()
    assert f"reviewed head {reviewed[:12]} is not an ancestor" in err
    assert "git reflog" in err and "open a new PR" in err
    assert "run `loopzero pr`, then `loopzero review` before merging main" in err


def test_review_appends_line_after_existing_review(wt: Path, gh: FakeGh, fake_bin, capsys, head) -> None:
    prior = "- primary, codex, 123456789abc, request_changes"
    body = f"# T1\n\n## Review\n{prior}\n\n## Notes\n"
    arm_pr_body_patch(gh, head, body=body)
    gh.respond(reviews_key(), [])
    gh.respond(post_key(), {"id": 3})
    _fake_claude(fake_bin, _claude_envelope(APPROVE))

    assert run(capsys, "review")[0] == 0

    updated = json.loads(gh.calls[-1]["--input"])["body"]
    assert prior in updated
    assert f"- primary, claude, {head[:12]}, approve" in updated
    assert updated.index(prior) < updated.index(head[:12]) < updated.index("## Notes")


def test_review_ignores_forged_history(wt: Path, gh: FakeGh, fake_bin, capsys, head) -> None:
    history = [rev(head, "primary", login="stranger"), rev(head, "delta", commit="2" * 40)]
    prepare_review(wt, gh, fake_bin, history=history)
    code, out, _ = run(capsys, "review")
    assert code == 0 and out.splitlines()[-1].startswith("primary review by claude")


def test_review_refuses_when_budget_exhausted(wt: Path, gh: FakeGh, fake_bin, capsys) -> None:
    primary = head_of(wt)
    git(wt, "commit", "--allow-empty", "-qm", "repair after primary")
    delta = head_of(wt)
    git(wt, "reset", "--soft", primary + "^")
    git(wt, "commit", "-qm", "squashed work")
    prepare_review(wt, gh, fake_bin, history=[rev(primary, "primary"), rev(delta, "delta")])
    _, err = expect_cli(capsys, 1, "review", stdout="", stderr_contains=("review cap reached (2 rounds per PR)",))
    assert "resolve-findings" in err and "split the PR" in err
    assert not (fake_bin / "claude.stdin").exists()


@pytest.mark.parametrize("publisher, allowed", [("app[bot]", True), (LOGIN, False)])
def test_review_publisher_check_uses_app_identity(
    wt: Path, gh: FakeGh, fake_bin: Path, app_token_helper, capsys,
    publisher: str, allowed: bool,
) -> None:
    app_token_helper()
    (wt / "workflow.toml").write_text(WORKFLOW + f'\nreview_publishers = ["{publisher}"]\n')
    git(wt, "commit", "-qam", "configure publisher")
    git(wt, "update-ref", "refs/remotes/origin/main", "HEAD")
    prepare_review(wt, gh, fake_bin, post_id=3)
    gh.respond("api graphql viewer", {"data": {"viewer": {"login": "app[bot]"}}})
    code, _, err = run(capsys, "review")
    assert code == (0 if allowed else 1)
    if not allowed:
        assert "not a configured review publisher" in err


@pytest.mark.parametrize("after_review", ["app[bot]", "other[bot]", "helper fails"])
def test_review_reminted_app_token_keeps_publisher(
    wt: Path, gh: FakeGh, fake_bin: Path, tmp_path: Path, monkeypatch, capsys, after_review: str, app_token_helper,
) -> None:
    counter = tmp_path / "helper-calls"
    fail = f"[ $(wc -l < {counter}) -eq 1 ] || exit 1\n" if after_review == "helper fails" else ""
    app_token_helper(f"echo called >> {counter}\n{fail}echo synthetic-token-$(wc -l < {counter})\n")
    boot, run_review = [1_000_000.0], cli._run_review
    monkeypatch.setattr(github, "_app_token_clock", lambda: boot[0])
    monkeypatch.setattr(cli, "_run_review", lambda *a, **k: (run_review(*a, **k), boot.append(boot.pop() + 3601))[0])
    (wt / "workflow.toml").write_text(WORKFLOW + '\nreview_publishers = ["app[bot]"]\n')
    git(wt, "commit", "-qam", "configure publisher")
    git(wt, "update-ref", "refs/remotes/origin/main", "HEAD")
    head = prepare_review(wt, gh, fake_bin, post_id=3)
    gh.respond("api graphql viewer", {"data": {"viewer": {"login": "app[bot]"}}},
               {"data": {"viewer": {"login": after_review}}})
    code, _, err = run(capsys, "review")
    posts = [c["token"] for c in gh.calls if "/reviews" in " ".join(c["argv"]) and "POST" in c["argv"]]
    if after_review == "app[bot]":
        assert code == 0 and posts == ["synthetic-token-2"]
    else:
        assert code == 1 and "could not refresh the app token" in err and "--repost" in err and posts == []
        assert cli._review_file(wt, head, "primary").is_file()


@pytest.mark.parametrize("tokens, publisher", [(["", "app"], LOGIN)])  # app -> unavailable: reminted test
def test_review_posts_as_checked_identity(wt, gh, fake_bin, monkeypatch, capsys, tokens, publisher):
    monkeypatch.setattr(github, "app_token", lambda: tokens[0])  # flips after the publisher check
    monkeypatch.setattr(github, "login", lambda token=None, f=github.login: (
        f(token), len(tokens) > 1 and tokens.pop(0))[0])
    (wt / "workflow.toml").write_text(WORKFLOW + f'\nreview_publishers = ["{publisher}"]\n')
    git(wt, "commit", "-qam", "configure publisher")
    git(wt, "update-ref", "refs/remotes/origin/main", "HEAD")
    prepare_review(wt, gh, fake_bin, post_id=3)
    gh.respond("api graphql viewer", {"data": {"viewer": {"login": publisher}}})
    assert run(capsys, "review")[0] == 0
    assert len(gh.calls) == 8  # Includes the body refresh after review publication.
    assert sum(c["argv"][:2] == ["api", "graphql"] for c in gh.calls) == 1
    assert not any("/files" in c["argv"][1] for c in gh.calls)
    assert posted_review(gh)["event"] == ("APPROVE" if publisher == LOGIN else "COMMENT")
    assert [c["token"] or "" for c in gh.calls if "/reviews" in " ".join(c["argv"])
            and "POST" in c["argv"]] == ["" if publisher == LOGIN else "app"]


@pytest.mark.parametrize("publisher", [LOGIN, "OTHER"])
def test_review_refuses_same_head_twice(wt: Path, gh: FakeGh, capsys, publisher) -> None:
    commit_file(wt, "workflow.toml", WORKFLOW + '\nreview_publishers = ["lz-bot", "other"]\n', "configure trusted publishers")
    git(wt, "update-ref", "refs/remotes/origin/main", "HEAD")
    head = head_of(wt)
    arm_pr(gh, head)
    gh.respond(reviews_key(), [rev(head, "primary", login=publisher)])
    expect_cli(capsys, 1, "review", stderr_contains=("already has a primary review; run `loopzero ready`",))


@pytest.mark.parametrize("condition,message", [
    ("no-pr", "no pull request for lz/t1"),
    ("retargeted", "PR targets release, configured base is main"),
    ("dirty", "uncommitted"),
    ("unpushed", "push or pull first"),
])
def test_review_refuses_invalid_preflight(wt: Path, gh: FakeGh, fake_bin: Path,
                                         capsys, condition, message) -> None:
    if condition == "no-pr":
        gh.respond_pr_list([])
    elif condition == "dirty":
        (wt / "scratch.txt").write_text("x")
    elif condition == "retargeted":
        arm_pr(gh, head_of(wt), baseRefName="release")
    else:
        arm_pr(gh, "0" * 40)
    code, out, err = run(capsys, "review")
    assert code == 1 and out == "" and message in err
    assert not (fake_bin / "claude.stdin").exists()
    if condition == "dirty":
        assert gh.calls == []


@pytest.mark.parametrize("private_path", ["", " /home/private/secret-path"], ids=["auth", "redaction"])
def test_review_falls_through_when_preferred_family_fails(
    wt, gh, fake_bin, fake_tool, capsys, private_path
):
    git(wt, "commit", "-q", "--allow-empty", "-m", "by claude\n\nCo-Authored-By: Claude <n@a.c>")
    prepare_review(wt, gh, fake_bin, post_id=4)
    fake_tool("codex", "if [ \"$1\" = --version ]; then echo 'codex-cli 0.161.0'; exit 0; fi\n"
              f"echo 'Please run codex login{private_path}' >&2\nexit 1\n")
    code, out, err = run(capsys, "review")
    body = posted_review(gh)["body"]
    assert code == 0 and "review by claude" in out
    assert "reviewer codex unavailable:" in err
    assert "same-family fallback: independent reviewers failed: codex:" in err
    assert "Same-family fallback:" in body
    if private_path:
        assert "independent reviewers failed: codex: not authenticated." in body
        assert "secret-path" not in body


@pytest.mark.parametrize("codex_ok", [False, True])
def test_review_unusable_fallback_effort_waits_for_independent_result(
    wt: Path, gh: FakeGh, fake_bin: Path, fake_tool, capsys, codex_ok
) -> None:
    publish_workflow(wt, WORKFLOW + """
[delivery.review.claude]
allowed_efforts = ["medium"]
""")
    git(wt, "commit", "-q", "--allow-empty", "-m", "by claude\n\nCo-Authored-By: Claude <n@a.c>")
    prepare_review(wt, gh, fake_bin, post_id=4)
    if codex_ok:
        _fake_codex(fake_bin, json.dumps(APPROVE))
    else:
        fake_tool("codex", "if [ \"$1\" = --version ]; then echo 'codex-cli 0.161.0'; exit 0; fi\n"
                  "echo 'Please run codex login' >&2\nexit 1\n")
    code, out, err = run(capsys, "review", "--effort", "high")
    if codex_ok:
        assert code == 0 and "review by codex" in out
        return
    assert code == 4 and "same-family fallback:" not in err
    assert "Please run codex login" in err and "effort 'high' is not allowed for claude" in err
    assert "reviewer codex unavailable:" not in err
    assert not (fake_bin / "claude.argv").exists()


def test_review_observes_new_codex_version_in_saved_receipt(
    wt: Path, gh: FakeGh, fake_bin: Path, capsys,
) -> None:
    publish_workflow(wt, WORKFLOW.replace(
        'reviewers = ["claude", "codex"]', 'reviewers = ["codex", "claude"]',
    ))
    prepare_review(wt, gh, fake_bin)
    version = "codex-cli 0.168.0"
    _fake_codex(fake_bin, json.dumps(APPROVE), version=version)
    _, err = expect_cli(capsys, 0, "review", stdout_contains=("review by codex",))
    assert "reviewer codex unavailable:" not in err
    result = cli._load_review(wt, git(wt, "rev-parse", "HEAD").strip(), "primary")
    assert result.provenance["cli_version"] == version


@pytest.mark.parametrize("mixed_authors", [False, True])
def test_review_fails_with_three_line_errors_and_remedies_without_spending_budget(
    wt: Path, gh: FakeGh, capsys, monkeypatch, head, mixed_authors
) -> None:
    if mixed_authors:
        git(wt, "commit", "-q", "--allow-empty", "-m",
            "mixed\n\nCo-Authored-By: Claude <n@a.c>\nCo-Authored-By: Codex <n@o.c>")
        head = head_of(wt)
    arm_empty_primary_review_history(gh, head)

    def fail(family, **kwargs):
        if family == "claude":
            raise cli.runners.RunnerAuthFailed(
                "claude: CLI is not authenticated\nfirst detail\nsecond detail\nnot printed"
            )
        raise cli.runners.RunnerMissing("codex: 'codex' not found on PATH")

    monkeypatch.setattr(cli.runners, "review_with", fail)
    code, out, err = run(capsys, "review")
    assert code == 4 and out.startswith("reviewer paths pinned")
    assert "no configured reviewer can run" in err
    assert ("author family: claude, codex" if mixed_authors else "author family: unknown") in err
    assert "Fix the reviewer failures below and rerun" in err
    assert "delivery.reviewers" not in err and "exception" not in err
    assert "claude: CLI is not authenticated\nfirst detail\nsecond detail" in err
    assert "not printed" not in err
    assert "Fix: run `claude auth login`." in err
    assert err.count("codex: 'codex' not found on PATH") == 1
    assert "npm install -g @openai/codex" in err
    assert all(c["argv"][1] != post_key().split(" ")[1] for c in gh.calls)


@pytest.mark.parametrize(("platform", "message"), [
    ("linux", "bwrap not found on PATH (install bubblewrap)"),
    ("darwin", "fresh local reviews use Bubblewrap and require Linux. Run loopzero review on a Linux host"),
])
def test_review_reports_unavailable_sandbox(
    wt: Path, gh: FakeGh, fake_bin: Path, capsys, monkeypatch, platform, message
) -> None:
    monkeypatch.setattr(sys, "platform", platform)
    prepare_review(wt, gh, fake_bin)
    original = cli.runners.shutil.which
    monkeypatch.setattr(
        cli.runners.shutil, "which", lambda name: None if name == "bwrap" else original(name)
    )
    code, out, err = run(capsys, "review")
    assert code == 2 and out.startswith("reviewer paths pinned")
    assert err == f"sandbox unavailable: {message}\n"


def test_review_falls_back_after_timeout(wt: Path, gh: FakeGh, capsys, monkeypatch, head) -> None:
    arm_empty_primary_review_post(gh, head, 4)

    def review_with(family, **kwargs):
        if family == "claude":
            raise cli.runners.RunnerBadOutput("claude: timed out", "partial")
        return _native_codex_result(head, publisher=LOGIN, cwd=str(wt))

    monkeypatch.setattr(cli.runners, "review_with", review_with)
    code, out, err = run(capsys, "review")
    assert code == 0 and out.splitlines()[-1].startswith("primary review by codex")
    assert "claude: timed out" in err


# --- ready / merge / status ----------------------------------------------------------


@pytest.mark.parametrize("reposted", [False, True], ids=["published", "reposted"])
@pytest.mark.parametrize("kind,state,verdict,comparison,resolved,ready,history", [
    ("primary", "APPROVED", "approve", "ahead", True, True, "clean"),
    ("primary", "COMMENTED", "approve", "ahead", True, True, "clean"),
    ("primary", "APPROVED", "approve", "ahead", True, False, "own commit"),
    ("primary", "APPROVED", "approve", "ahead", True, False, "altered merge"),
    ("primary", "APPROVED", "approve", "ahead", True, False, "missing history"),
    ("primary", "CHANGES_REQUESTED", "request_changes", "ahead", True, True, "clean"),
    ("primary", "CHANGES_REQUESTED", "approve", "ahead", True, False, "clean"),
    ("primary", "COMMENTED", "request_changes", "ahead", True, True, "clean"),
    ("primary", "COMMENTED", "request_changes", "ahead", False, False, "clean"),
    ("primary", "COMMENTED", "request_changes", "ahead", True, False, "own commit"),
    ("primary", "COMMENTED", "request_changes", "ahead", True, False, "altered merge"),
    ("primary", "APPROVED", "request_changes", "ahead", True, False, "clean"),
    ("primary", "COMMENTED", "unknown", "ahead", True, False, "clean"),
    ("primary", "APPROVED", "approve", "diverged", True, False, "clean"),
    ("primary", "APPROVED", "approve", "ahead", False, False, "clean"),
    ("delta", "APPROVED", "approve", "ahead", True, True, "clean"),
    ("delta", "APPROVED", "approve", "ahead", True, False, "own commit"),
    ("delta", "APPROVED", "approve", "ahead", True, False, "altered merge"),
    ("delta", "APPROVED", "approve", "ahead", True, False, "missing history"),
    ("delta", "APPROVED", "approve", "ahead", False, False, "clean"),
    ("delta", "APPROVED", "approve", "diverged", True, False, "clean"),
])
def test_ready_after_reviewed_head(
    wt, gh, capsys, monkeypatch, fake_tool, replay_gh,
    kind, state, verdict, comparison, resolved, ready, history, reposted,
):
    reviewed = head_of(wt)
    git(wt, "switch", "-c", "updated-base", "HEAD^")
    commit_file(wt, "base.txt", "new base content\n", "update base")
    base = head_of(wt)
    git(wt, "switch", "-")
    if comparison == "ahead":
        git(wt, "merge", "--no-edit", "updated-base")
        if history == "own commit":
            git(wt, "commit", "--allow-empty", "-m", "own change")
        elif history == "altered merge":
            (wt / "base.txt").write_text("manual merge resolution\n")
            git(wt, "commit", "--amend", "-am", "merge updated base with edits")
    else:
        git(wt, "rebase", "updated-base")
    head = head_of(wt)
    review = rev(reviewed, kind, state=state, verdict=verdict)
    result = ReviewResult("claude", reviewed, kind, verdict, (), "Quoted verdict: **approve**",
                          provenance={"session_ids": ["test-session"]})
    prefix = cli._repost_prefix(result) if reposted else marker(reviewed, kind)
    review["body"] = github.review_payload(REPO, 7, reviewed, result, prefix)["body"]
    thread = {"isResolved": resolved, "isOutdated": True, "path": "feature.py", "line": 1,
              "comments": {"nodes": [{"author": {"login": LOGIN}, "body": f"<!-- loopzero:finding v=1 severity=important head={reviewed} id=0b9b59e7 -->"}]}}
    reviews = [rev("f" * 40, "primary"), review] if kind == "delta" else [review]
    arm_approved_pr_with_check_result(gh, head, wt, "success", reviews=reviews,
                                      readiness={"threads": (thread,)}, isDraft=False)
    detail = rest_pr_json(pr_json(headRefOid=head, isDraft=False))
    detail["base"]["sha"] = base
    gh.respond(f"api repos/{REPO}/pulls/7", detail)
    fake_tool("prlimit", "exit 127\n")
    popen = subprocess.Popen

    def isolated_fetch(argv, **kwargs):
        if "fetch" in argv:
            if history == "missing history":
                raise cli.eligibility.candidate.CandidateError("history unavailable")
            argv = [wt.as_uri() if arg == f"https://github.com/{REPO}.git" else arg for arg in argv]
        return popen(argv, **kwargs)

    monkeypatch.setenv("GH_TOKEN", "test-token")
    monkeypatch.setattr(subprocess, "Popen", isolated_fetch)
    code, out, _ = run(capsys, "ready")
    assert code == (0 if ready else 1)
    if not resolved:
        assert ("no review recorded" in out) is (verdict == "request_changes")
        if verdict == "approve":
            assert "open important finding" in out
    if history != "clean":
        assert ("no eligible review" if kind == "delta" else "no review recorded") in out
    assert cli.eligibility.evaluate(REPO, 7, head, "main", (LOGIN,)).ready is ready


def test_exact_head_delta_ignores_outdated_blocker(wt, gh, capsys, head):
    previous = git(wt, "rev-parse", "HEAD^").strip()
    history = [rev("f" * 40, "primary"), rev(head, "delta")]
    thread = {"isResolved": False, "isOutdated": True, "path": "feature.py", "line": 1,
              "comments": {"nodes": [{"author": {"login": LOGIN}, "body": f"<!-- loopzero:finding v=1 severity=critical head={previous} id=0b9b59e7 -->"}]}}
    arm_approved_pr_with_check_result(gh, head, wt, "success", reviews=history,
                                      readiness={"threads": (thread,)}, isDraft=False)
    code, _, _ = run(capsys, "ready")
    gh.respond_pr_view(pr_json(headRefOid=head))
    hosted = cli.eligibility.evaluate(REPO, 7, head, "main", (LOGIN,))
    assert code == 0 and hosted.ready


@pytest.mark.parametrize("branch", [
    "lz/t1", "t3/archive-review-receipts", "od183-fable-5-2", "codex/task",
])
def test_ready_marks_draft_ready(wt: Path, gh: FakeGh, capsys, head, branch, git_calls) -> None:
    git(wt, "branch", "-m", branch)
    arm_approved_ready_pr(gh, head, wt, branch=branch)
    gh.respond("pr ready", "")
    git_calls.clear()
    code, out, err = run(capsys, "ready")
    assert_git_cost(git_calls, 7, 1)
    assert (code, err) == (0, "") and out.endswith(f"ready: {URL}\n")
    assert ["pr", "ready", "7", "--repo", REPO] in [c["argv"] for c in gh.calls]

    assert out.count("receipt accepted:") == 1
    assert f"head={head}" in out and "environment=unknown" in out


@pytest.mark.parametrize("phase", ["stage", "save", "sandbox returned"])
def test_late_check_preserves_receipt_when_head_moves(wt, gh, capsys, monkeypatch, phase):
    old_head, receipt = head_of(wt), wt / ".loopzero/checks.json"
    refreshed = []
    monkeypatch.setattr(cli, "_refresh_pr_checks", lambda *_: refreshed.append(head_of(wt)))
    if phase != "stage":
        expect_cli(capsys, 0, "check")
        saved_receipt = receipt.read_bytes()
    run_stage, run_checks, save_report = cli.sandbox._run_one, cli.sandbox.run_checks, cli._save_report

    def advance_head(*, rerun=True):
        nonlocal saved_receipt
        git(wt, "commit", "--allow-empty", "-qm", "advance HEAD")
        if rerun:
            expect_cli(capsys, 0, "check")
            saved_receipt = receipt.read_bytes()

    def during_stage(*args):
        monkeypatch.setattr(cli.sandbox, "_run_one", run_stage)
        advance_head()
        return run_stage(*args)

    def before_save(path, report):
        if report.head == old_head:
            advance_head()
        return save_report(path, report)

    def after_checks(*args, **kwargs):
        report = run_checks(*args, **kwargs)
        assert report.ok
        advance_head(rerun=False)
        return report

    if phase == "stage":
        monkeypatch.setattr(cli.sandbox, "_run_one", during_stage)
    elif phase == "save":
        monkeypatch.setattr(cli, "_save_report", before_save)
    else:
        monkeypatch.setattr(cli.sandbox, "run_checks", after_checks)

    out, err = expect_cli(capsys, 1, "check")
    head = head_of(wt)
    assert receipt.read_bytes() == saved_receipt
    if phase == "stage":
        assert f"HEAD before: {old_head}; HEAD after: {head}" in err
        assert refreshed == [head]
        arm_approved_ready_pr(gh, head, wt, readiness={"local_exit": None})
        gh.respond("pr ready", "")
        ready, _ = expect_cli(capsys, 0, "ready", stderr="")
        assert "receipt accepted:" in ready
    else:
        assert "PASS" not in out
        assert "discarded" in err and old_head in err and head in err
        assert refreshed == ([old_head, head] if phase == "save" else [old_head])


def test_ready_not_ready_lists_reasons(wt: Path, gh: FakeGh, capsys, head) -> None:
    reviews = [rev("a" * 40, "primary"), rev("b" * 40, "delta", state="CHANGES_REQUESTED")]
    arm_approved_pr_with_check_result(gh, head, wt, "failure", reviews=reviews)
    arm_check_runs(gh, head, [{
        "name": "checks", "status": "completed", "conclusion": "failure",
        "check_suite": {"id": 10}}])
    out, _ = expect_cli(capsys, 1, "ready")
    out = out.replace(FAILED_CHECK_HINT, "")
    assert without_accepted_receipt(out).splitlines() == [
        f"not ready: Both review rounds are spent (primary + delta) and {head[:12]} has no eligible review. Open a replacement PR for changes that need review. Another `loopzero review` on this PR will be refused.",
        "not ready: required check 'checks' is failure",
    ]
    assert all(c["argv"][:2] != ["pr", "ready"] for c in gh.calls)
    assert not any("actions/runs" in " ".join(c["argv"]) for c in gh.calls)


@pytest.mark.parametrize("retargeted,reason", [
    (True, "PR targets release, configured base is main"),
    (False, f"required check 'checks' is failure{FAILED_CHECK_HINT}"),
], ids=["retargeted", "trust-base-required-ci"])
def test_ready_explains_trusted_policy_blocker(wt, gh, capsys, head, retargeted, reason):
    if retargeted:
        arm_approved_ready_pr(gh, head, wt, baseRefName="release")
    else:
        (wt / "workflow.toml").write_text(WORKFLOW.replace('["checks"]', "[]"))
        arm_approved_pr_with_check_result(gh, head, wt, "failure", isDraft=False)

    out, _err = expect_cli(capsys, 1, "ready", stderr="")
    assert without_accepted_receipt(out) == f"not ready: {reason}\n"


def test_pending_required_check_names_the_wait(wt: Path, gh: FakeGh, capsys, head) -> None:
    arm_approved_ready_pr(gh, head, wt, isDraft=False)
    arm_check_runs(gh, head, [{"name": "checks", "status": "in_progress"}])

    code, out, err = run(capsys, "ready")

    out = without_accepted_receipt(out)
    assert (code, err) == (1, "")
    assert out.splitlines() == [
        "not ready: required check 'checks' is pending",
        "next: loopzero ready --wait",
    ]

    arm_pr(gh, head, isDraft=False)
    arm_readiness(gh, head, wt)
    arm_check_runs(gh, head, [{"name": "checks", "status": "in_progress"}])
    code, out, err = run(capsys, "merge")

    assert (code, without_accepted_receipt(out)) == (1, "")
    assert "required check 'checks' is pending; next: loopzero merge --wait" in err


@pytest.mark.parametrize("draft", [True, False])
@pytest.mark.parametrize("command", ["ready", "merge"])
def test_skipped_required_check_activates_only_on_ready(wt, gh, capsys, head, draft, command):
    arm_approved_pr_with_check_result(gh, head, wt, "skipped", isDraft=draft)
    if command == "merge":
        code, out, err = run(capsys, "merge", "--wait=0")
        assert (code, without_accepted_receipt(out)) == (1, "")
        hint = "; next: loopzero ready --wait" if draft else ""
        assert err == f"loopzero merge: not ready to merge: required check 'checks' is skipped{hint}\n"
    else:
        gh.respond("pr ready", "")
        code, out, err = run(capsys, "ready")
        out = without_accepted_receipt(out)
        assert (code, err) == (3 if draft else 1, "")
        if draft:
            assert out.splitlines() == [
                "marked ready; waiting for required checks: checks; next: loopzero ready --wait",
            ]
            assert ["pr", "ready", "7", "--repo", REPO] in [c["argv"] for c in gh.calls]
            return
        assert out == "not ready: required check 'checks' is skipped\n"
    assert all(c["argv"][:2] != ["pr", "ready"] for c in gh.calls)


def test_ready_wait_reports_initial_draft_blocker_once(wt, gh, capsys, monkeypatch, head, git_calls):
    arm_approved_pr_with_check_result(gh, head, wt, "skipped", isDraft=True)
    arm_check_progress(gh, head, "skipped", "skipped", "pending", "success")
    gh.respond("pr ready", "")
    git_calls.clear()
    monkeypatch.setattr(cli, "_sleep", lambda _: None)
    out, _err = expect_cli(capsys, 0, "ready", "--wait", stderr="")
    assert sum(call[0] == "show" and call[-1].endswith(":workflow.toml") for call in git_calls) == 1
    assert out.count("receipt accepted:") == 1
    assert without_accepted_receipt(out).splitlines() == [
        "marked ready; required check 'checks' is skipped",
        "waiting: required check 'checks' is pending",
        f"ready: {URL}",
    ]


@pytest.mark.parametrize("settles,fault,error", [(True, "interrupt", ""), (False, "interrupt", ""), (True, "lost", ""), (False, "lost_zero", ""), (True, "retry", ""), (False, "moved", "head changed"), (False, "throttle", "HTTP 403")])
def test_ready_wait_resumes_after_draft_transition(wt, gh, capsys, monkeypatch, head, settles, fault, error):
    arm_approved_pr_with_check_result(gh, head, wt, "skipped", isDraft=True)
    gh.respond("pr ready", "")
    original, attempts, pauses = github.mark_ready, [], []
    def activate(*args):
        original(*args)
        attempts.append(args)
        if fault not in {"retry", "throttle"} or len(attempts) == 2:
            arm_pr(gh, "b" * 40 if fault == "moved" else head, isDraft=False)
        if len(attempts) == 1:
            raise {"interrupt": KeyboardInterrupt(), "throttle": github.GhUnavailable(("gh", "pr", "ready"), "HTTP 403 rate limit")}.get(fault, github.ProcTimeout("lost activation reply"))
    monkeypatch.setattr(github, "mark_ready", activate)
    monkeypatch.setattr(github, "_retry_sleep", pauses.append)
    arm_check_progress(gh, head, *(("skipped", "skipped", "skipped", "pending", "success") if settles else ("skipped",)))
    monkeypatch.setattr(cli, "WAIT_INTERVAL", 1.0)
    monkeypatch.setattr(cli, "_sleep", lambda _: None)
    wait = "--wait=30" if settles or fault == "moved" else "--wait=0"
    if fault == "interrupt":
        assert run(capsys, "ready", wait)[0] == 130
    code, out, err = run(capsys, "ready", wait)
    assert code == (1 if error else (0 if settles else 3)) and error in err
    assert ("waiting: required check 'checks' is pending" in out) == settles
    assert ("timed out waiting for required checks" in out) == (not settles and fault in {"interrupt", "lost_zero"})
    assert sum(c["argv"][:2] == ["pr", "ready"] for c in gh.calls) == len(attempts) == (2 if fault == "retry" else 1)
    assert pauses == ([5] if fault == "retry" else [])


@pytest.mark.parametrize("settled", ["success", "skipped"])
def test_ready_waits_for_checks_on_original_head(
    wt: Path, gh: FakeGh, capsys, monkeypatch, settled: str, head
) -> None:
    (wt / "workflow.toml").write_text(WORKFLOW.replace('["checks"]', '["a", "b"]'))
    arm_approved_ready_pr(gh, head, wt)
    pending = {"check_runs": [{"name": "a", "status": "in_progress"}]}
    gh.respond(f"api repos/{REPO}/commits/{head}/check-runs", pending, pending,
               {"check_runs": [{"name": "a", "conclusion": settled}]},
               {"check_runs": [{"name": name, "conclusion": "success"}
                               for name in ("a", "b")]})
    gh.respond("pr ready", "")
    monkeypatch.setattr(cli, "_sleep", lambda _: None)
    monkeypatch.setattr(cli, "MISSING_RUN_GRACE", 0.0)
    code, out, err = run(capsys, "--config", "workflow.toml", "ready", "--wait")
    assert (code, err) == (0, "") and out.endswith(f"ready: {URL}\n")
    assert out.count("receipt accepted:") == 1
    assert "required check 'a' is pending" in out
    assert all("f" * 40 not in " ".join(c["argv"]) for c in gh.calls)
    assert sum(c["argv"][:2] == ["pr", "ready"] for c in gh.calls) == 1


@pytest.mark.parametrize("wait", [False, True])
def test_pending_mergeability_does_not_delay_draft_checks(wt, gh, capsys, head, wait):
    arm_approved_pr_with_check_result(gh, head, wt, "skipped", mergeable="UNKNOWN")
    gh.respond("pr ready", "")

    out, _err = expect_cli(capsys, 3, "ready", *(["--wait=0"] if wait else []), stderr="")
    assert "marked ready;" in out and "checks" in out
    assert "GitHub is computing mergeability" in out
    assert sum(call["argv"][:2] == ["pr", "ready"] for call in gh.calls) == 1


@pytest.fixture
def source_queue_api(gh, monkeypatch):
    repo = f"repos/{REPO}"
    config_path = f"{repo}/contents/.mergify.yml?ref=refs%2Fheads%2Fmain"
    gh.respond(f"api {repo}", {"default_branch": "main"})
    gh.respond(f"api {config_path}", {
        "type": "file", "encoding": "base64", "sha": "1" * 40,
        "content": base64.b64encode(
            b"merge_queue: {mode: serial, max_parallel_checks: 3}\nqueue_rules: [{name: main}]\n"
        ).decode(),
    })
    vendor_calls = []

    def get(_repo, path):
        vendor_calls.append(path)
        return {
            "/merge-queue/status?branch=main": {"batches": [], "mode": "serial"},
            "/queues/configuration": {"configuration": [{"name": "main", "config": {}}]},
        }[path]

    monkeypatch.setattr(mergify, "api_get", get)
    return repo, config_path, vendor_calls


@pytest.mark.parametrize("command", ["ready", "merge"])
def test_source_wait_reads_once_before_first_interval(
    wt, gh, capsys, monkeypatch, source_queue_api, command,
):
    head = commit_mergify_configuration(wt)
    arm_approved_pr_with_check_result(gh, head, wt, "pending", isDraft=False)
    repo, config_path, vendor_calls = source_queue_api
    observations = []
    gh.respond("api -X", "")

    def after_sleep():
        observations.append(([call["argv"][1] for call in gh.calls], len(vendor_calls)))
        arm_check_runs(gh, head, [{"name": "checks", "conclusion": "success"}])

    clock = poll_clock(monkeypatch, jitter=1.0, after_sleep=after_sleep)
    monkeypatch.setattr(mergify, "markers", lambda *_: (True, False))
    monkeypatch.setattr(mergify, "membership", lambda *_: None)
    monkeypatch.setattr(cli, "_wait_for_mergify", lambda *_: "e" * 40)

    _out, _err = expect_cli(capsys, 0, command, "--wait=90", stderr="")
    assert clock.now == cli.WAIT_INTERVAL
    [(before_sleep, vendor_count)] = observations
    assert before_sleep.count(f"{repo}/pulls/7") == 1
    assert before_sleep.count(reviews_key().split(" ", 1)[1]) == 1
    assert before_sleep.count(f"{repo}/commits/{head}/check-runs") == 1
    assert before_sleep.count(f"{repo}/commits/{head}/statuses") == 1
    assert before_sleep.count(repo) == before_sleep.count(config_path) == 2
    assert vendor_count == 2
    assert sum(call["argv"][1] == f"{repo}/commits/{head}/check-runs" for call in gh.calls) == 2


@pytest.mark.parametrize("command", ["ready", "merge"])
@pytest.mark.parametrize("settled", ["CONFLICTING", "MERGEABLE", "UNKNOWN"])
def test_wait_for_github_mergeability(wt, gh, capsys, monkeypatch, command, settled):
    head = commit_mergify_configuration(wt)
    arm_approved_ready_pr(gh, head, wt, mergeable="UNKNOWN", isDraft=False)
    gh.respond_pr_view(pr_json(headRefOid=head, mergeable=settled, isDraft=False,
                               mergeStateStatus="BEHIND"))
    requests = []
    stub_mergify_admission(monkeypatch)
    monkeypatch.setattr(mergify, "request", lambda *args: requests.append(args))
    monkeypatch.setattr(cli, "_wait_for_mergify", lambda *_: None)

    poll_clock(monkeypatch)
    code, out, err = run(capsys, command, "--wait=1")

    if settled == "CONFLICTING":
        assert code == 1 and "has merge conflicts" in out + err
    elif settled == "UNKNOWN":
        assert code == 3 and out.endswith(f"timed out waiting for GitHub to compute mergeability on {head[:12]}. Run `loopzero {command} --wait` to resume on this head.\n")
    else:
        assert code == (0 if command == "ready" else 3)
    assert requests == ([(REPO, 7, head, "main")]
                        if command == "merge" and settled == "MERGEABLE" else [])


@pytest.mark.parametrize("command,check,hold_at_admission", [
    ("ready", "success", False), ("ready", "skipped", False),
    ("merge", "success", False), ("merge", "success", True),
])
def test_hold_blocks_ready_and_merge(wt, gh, capsys, monkeypatch, command, check, hold_at_admission):
    head = commit_mergify_configuration(wt)
    labels = [{"name": "hold"}]
    arm_approved_pr_with_check_result(
        gh, head, wt, check, isDraft=command == "ready", labels=[] if hold_at_admission else labels
    )
    if hold_at_admission:
        gh.append(f"api repos/{REPO}/pulls/7", {**merge_json(head=head), "labels": labels})
    stub_mergify_admission(monkeypatch)
    requests = []
    monkeypatch.setattr(mergify, "request", lambda *args: requests.append(args))

    code, out, err = run(capsys, command, "--wait=0")

    assert code == 1 and "merge-authority hold" in out + err
    assert not requests and all(c["argv"][:2] != ["pr", "ready"] for c in gh.calls)

    arm_approved_pr_with_check_result(gh, head, wt, check, isDraft=command == "ready")
    gh.respond("pr ready", "")
    monkeypatch.setattr(cli, "_wait_for_mergify", lambda *_: None)
    code, _, _ = run(capsys, command, "--wait=0")
    assert code == (0 if command == "ready" and check == "success" else 3)
    assert requests == ([(REPO, 7, head, "main")] if command == "merge" else [])
    assert any(c["argv"][:2] == ["pr", "ready"] for c in gh.calls) == (command == "ready")


def test_hold_label_refresh_matches_local_readiness(wt, gh, head, tmp_path, monkeypatch, capsys):
    trusted = tmp_path / "trusted"
    trusted.mkdir()
    (trusted / "workflow.toml").write_text(WORKFLOW + f'review_publishers = ["{LOGIN}"]\n')
    event = trusted / "event.json"
    monkeypatch.setenv("GITHUB_REPOSITORY", REPO)
    monkeypatch.setenv("GITHUB_EVENT_PATH", str(event))
    gh.respond(f"api repos/{REPO}/statuses/{head}", {})

    for action, labels, expected in (
        ("labeled", [{"name": "hold"}], 1), ("unlabeled", [], 0),
    ):
        arm_approved_ready_pr(gh, head, wt, isDraft=False, labels=labels)
        gh.respond(f"api repos/{REPO}/pulls/7", rest_pr_json(
            pr_json(headRefOid=head, isDraft=False, labels=labels)))
        event.write_text(json.dumps({"action": action, "pull_request": {"number": 7}}))
        code, out, _ = run(capsys, "ready")
        assert code == expected
        assert ("merge-authority hold" in out) == bool(labels)
        with monkeypatch.context() as hosted_env:
            hosted_env.chdir(trusted)
            assert hosted.main() == expected
        capsys.readouterr()
        payload = json.loads(gh.calls[-1]["--input"])
        assert gh.calls[-1]["argv"][1] == f"repos/{REPO}/statuses/{head}"
        assert payload["state"] == ("failure" if labels else "success")


@pytest.mark.parametrize("wait,elapsed,conclusion,expected", [
    (0, 0, None, 3), (1, 0, "failure", 1), (1, 0.25, "success", 0), (1, 6, "success", 3),
])
def test_ready_wait_verdict(wt, gh, capsys, monkeypatch, head, wait, elapsed, conclusion, expected):
    arm_approved_pr_with_check_result(gh, head, wt, "pending", isDraft=False)
    clock = poll_clock(monkeypatch)
    pr_view = github.pr_view

    def observe(repo, number):
        clock.now += elapsed
        if conclusion:
            arm_check_runs(gh, head, [{"name": "checks", "conclusion": conclusion}])
        return pr_view(repo, number)

    monkeypatch.setattr(github, "pr_view", observe)

    (wt / "-retry config.toml").write_bytes((wt / "workflow.toml").read_bytes())
    out, _ = expect_cli(capsys, expected, "--config", "./-retry config.toml", "ready", f"--wait={wait}", stderr="")
    assert out.endswith({
        0: f"ready: {URL}\n", 1: f"not ready: required check 'checks' is failure{FAILED_CHECK_HINT}\n",
        3: f"timed out waiting for required checks on {head[:12]}. Run `loopzero '--config=-retry config.toml' ready --wait` to resume on this head.\n",
    }[expected])


def arm_replacement_workflow(gh, head, conclusion):
    arm_check_runs(gh, head, [{
        "id": 100, "name": "checks", "status": "completed", "conclusion": conclusion,
        "check_suite": {"id": 10}, "completed_at": "2026-09-23T21:54:41Z"}])
    gh.respond(f"api repos/{REPO}/actions/runs?head_sha={head}", {"workflow_runs": [
        {"id": 1, "check_suite_id": 10, "workflow_id": 7, "head_sha": head,
         "status": "completed", "created_at": "2026-09-23T21:53:00Z"},
        {"id": 2, "check_suite_id": 20, "workflow_id": 7, "head_sha": head,
         "status": "in_progress", "created_at": "2026-09-23T21:54:21Z"},
    ]})


@pytest.mark.parametrize("replacement,expected", [("success", 0), ("failure", 1), ("pending", 3)])
def test_ready_wait_tracks_the_replacement_workflow(
    wt: Path, gh: FakeGh, capsys, monkeypatch, replacement: str, expected: int, head
) -> None:
    arm_approved_ready_pr(gh, head, wt, isDraft=False)
    old_conclusion = "cancelled" if replacement == "pending" else "failure"
    arm_replacement_workflow(gh, head, old_conclusion)
    elapsed = 0.0
    monkeypatch.setattr(cli.time, "monotonic", lambda: elapsed)
    if replacement != "pending":
        def sleep(seconds: float) -> None:
            nonlocal elapsed
            elapsed += seconds
            arm_check_runs(gh, head, [
                {"id": 101, "name": "checks", "status": "completed", "conclusion": replacement}])

        monkeypatch.setattr(cli, "_sleep", sleep)
    out, _err = expect_cli(capsys, expected, "ready", "--wait=0" if replacement == "pending" else "--wait=1", stderr="")
    assert f"waiting: required check 'checks' is {old_conclusion}" in out
    if replacement == "pending":
        assert "timed out waiting for required checks" in out
    else:
        assert out.endswith(f"ready: {URL}\n" if expected == 0 else
                            f"not ready: required check 'checks' is failure{FAILED_CHECK_HINT}\n")


@pytest.mark.usefixtures("replay_gh")
@pytest.mark.parametrize("replacement,expected", [("success", 0), ("failure", 1), ("pending", 3)])
@pytest.mark.parametrize("delivery", ["squash", "mergify"])
@pytest.mark.parametrize("initial_pending", [False, True])
def test_ready_wait_revalidates_its_ready_transition(
    wt, gh, capsys, monkeypatch, replacement, expected, delivery, initial_pending
):
    if delivery == "mergify":
        commit_mergify_configuration(wt)
        monkeypatch.setattr(mergify, "configured", lambda *_: True)
    head = head_of(wt)
    arm_approved_pr_with_check_result(gh, head, wt, "pending" if initial_pending else "success")
    transitioned = False

    def transition():
        nonlocal transitioned
        transitioned = True
        gh.respond_pr_view(pr_json(headRefOid=head, isDraft=False))
        arm_replacement_workflow(gh, head, "cancelled")

    def advance():
        if not transitioned or replacement != "pending":
            settled = replacement if transitioned else "success"
            arm_check_runs(gh, head, [{"name": "checks", "conclusion": settled}])
    clock = poll_clock(monkeypatch, jitter=1.0, after_sleep=advance)
    on_ready_transition(gh, monkeypatch, transition)

    out, _err = expect_cli(capsys, expected, "ready", "--wait=40", stderr="")
    assert "waiting: required check 'checks' is cancelled" in out
    assert clock.sleeps == ([30.0, 5.0, 5.0] if replacement == "pending" else
                      [30.0, 5.0] if initial_pending else [30.0])
    assert out.endswith(f"ready: {URL}\n" if expected == 0 else
                        f"not ready: required check 'checks' is failure{FAILED_CHECK_HINT}\n" if expected == 1 else
                        f"timed out waiting for required checks on {head[:12]}. Run `loopzero ready --wait` to resume on this head.\n")
    assert sum(c["argv"][:2] == ["pr", "ready"] for c in gh.calls) == 1


@pytest.mark.parametrize("after", ["pending", "late_success", "head_move", "blocker", "cancelled"])
def test_ready_transition_preserves_deadline_and_gates(wt, gh, capsys, monkeypatch, head, after, replay_gh):
    arm_approved_pr_with_check_result(gh, head, wt, "pending")
    transitioned = False

    def transition():
        nonlocal transitioned
        transitioned = True
        gh.respond_pr_view(pr_json(headRefOid="f" * 40 if after == "head_move" else head,
                                   isDraft=False, baseRefName="release" if after == "blocker" else "main"))
        arm_check_runs(gh, head, [{"name": "checks",
                                   "conclusion": "cancelled" if after == "cancelled" else "pending"}])
        gh.respond(f"api repos/{REPO}/actions/runs?head_sha={head}", {"workflow_runs": []})

    def advance():
        if not transitioned:
            arm_check_runs(gh, head, [{"name": "checks", "conclusion": "success"}])
    clock = poll_clock(monkeypatch, jitter=1.0, after_sleep=advance)
    on_ready_transition(gh, monkeypatch, transition)
    if after == "late_success":
        def late_read(*args, **kwargs):
            current = original_view(*args, **kwargs)
            if not current.is_draft:
                clock.now = 41.0
                arm_check_runs(gh, head, [{"name": "checks", "conclusion": "success"}])
            return current
        original_view = github.pr_view
        monkeypatch.setattr(github, "pr_view", late_read)

    code, out, err = run(capsys, "ready", "--wait=40")

    assert code == (1 if after in {"head_move", "blocker", "cancelled"} else 3)
    assert clock.sleeps == ([30.0, 5.0, 5.0] if after == "pending" else [30.0])
    assert "ready: https" not in out
    assert ("PR head changed while waiting" in err) == (after == "head_move")
    if after == "cancelled":
        assert "not ready: required check 'checks' is cancelled" in out
    if after == "blocker":
        assert "PR targets release, configured base is main" in out
    assert sum(c["argv"][:2] == ["pr", "ready"] for c in gh.calls) == 1


@pytest.mark.usefixtures("replay_gh")
@pytest.mark.parametrize("run_appears", [True, False])
def test_ready_transition_missing_run_gets_observation_grace(
    wt, gh, capsys, monkeypatch, head, run_appears
):
    arm_approved_pr_with_check_result(gh, head, wt, "pending")
    marked_at = []

    def transition():
        marked_at.append(clock.now)
        gh.respond_pr_view(pr_json(headRefOid=head, isDraft=False))

    def advance():
        if marked_at and run_appears:
            arm_check_runs(gh, head, [{"name": "checks", "conclusion": "success"}])
        elif clock.now >= 210:
            arm_check_runs(gh, head, [])
    clock = poll_clock(monkeypatch, jitter=1.0, after_sleep=advance)
    on_ready_transition(gh, monkeypatch, transition)

    code, out, err = run(capsys, "ready", "--wait=300")

    assert (code, err) == (0 if run_appears else 3, "")
    assert marked_at == [210.0]
    assert clock.sleeps == [30.0, 60.0, 120.0, 30.0] + ([] if run_appears else [30.0, 30.0])
    assert out.endswith(f"ready: {URL}\n" if run_appears else
                        f"timed out waiting for required checks on {head[:12]}. Run `loopzero ready --wait` to resume on this head.\n")
    assert sum(c["argv"][:2] == ["pr", "ready"] for c in gh.calls) == 1


@pytest.mark.parametrize("wait,draft", [(True, True), (False, True), (True, False)])
def test_ready_transition_immediate_green_is_best_effort(wt, gh, capsys, head, wait, draft, replay_gh):
    arm_approved_ready_pr(gh, head, wt, isDraft=draft)
    gh.respond("pr ready", "")

    code, out, err = run(capsys, "ready", *(["--wait"] if wait else []))

    assert (code, without_accepted_receipt(out), err) == (0, f"ready: {URL}\n", "")
    assert sum(c["argv"][:2] == ["pr", "ready"] for c in gh.calls) == int(draft)
    observations = sum(c["argv"][:2] == ["api", f"repos/{REPO}/commits/{head}/check-runs"]
                       for c in gh.calls)
    assert observations == (2 if wait and draft else 1)


@pytest.mark.parametrize("name", ["checks", "Loop-zero Eligibility"])
@pytest.mark.parametrize("pending,age,new_failure,wait,expected,waited", [
    (True, 180, False, True, 0, True),
    (False, 30, False, True, 0, True),
    (False, -30, False, True, 0, True),
    (False, -3600, False, True, 1, False),
    (False, -30, True, True, 1, False),
    (False, -30, "tie", True, 1, False),
    (True, 30, True, True, 1, True),
    (False, 30, True, True, 0, True),
    (False, 120, False, True, 0, True),
    (False, 119, False, True, 0, True),
    (False, 300, False, True, 0, True),
    (False, 899, False, True, 0, True),
    (False, 900, False, True, 1, False),
    (False, 901, False, True, 1, False),
    (False, 300, True, True, 1, False),
    (True, 300, True, True, 1, False),
    (False, 300, "tie", True, 1, False),
    (False, 300, False, False, 1, False),
    (False, 300, "missing", True, 1, False),
])
def test_ready_wait_review_failure_freshness(
    wt, gh, capsys, monkeypatch, name, pending, age, new_failure, wait, expected, waited, head, replay_gh,
):
    (wt / "workflow.toml").write_text(WORKFLOW.replace('["checks"]', json.dumps([name])))
    arm_approved_pr_with_check_result(
        gh,
        head,
        wt,
        "success",
        reviews=[rev(head, "primary") | {"submitted_at": "2026-09-23T12:00:00Z"}],
        isDraft=False,
    )
    epoch = 1790164800 + age  # 2026-09-23T12:00:00Z plus review age
    failed_at = {
        "tie": "2026-09-23T12:00:00Z",
        "missing": None,
    }.get(
        new_failure,
        "2026-09-23T12:00:10Z" if new_failure else "2026-09-23T11:59:00Z",
    )
    active_at = "2026-09-23T12:00:20Z"
    if name == "checks":
        runs = [{"name": name, "status": "completed", "conclusion": "failure", "completed_at": failed_at}]
        statuses = [{"context": name, "state": "pending", "created_at": active_at}] if pending else []
    else:
        runs = [{"name": name, "status": "in_progress", "started_at": active_at}] if pending else []
        statuses = [{"context": name, "state": "failure", "created_at": failed_at}]
    arm_check_runs(gh, head, runs)
    status_key = f"api repos/{REPO}/commits/{head}/statuses"
    gh.respond(status_key, statuses)

    def advance():
        if expected == 0 and (age != 300 or len(clock.sleeps) > 1):
            arm_check_runs(gh, head, [{"name": name, "conclusion": "success"}])
            gh.respond(status_key, [{"context": name, "state": "success"}])

    clock = poll_clock(monkeypatch, after_sleep=advance)
    monkeypatch.setattr(cli.time, "time", lambda: epoch + clock.now)
    wait_args = ["--wait=60" if age < 0 else "--wait"] if wait else []
    out, _ = expect_cli(capsys, expected, "--config", "workflow.toml", "ready", *wait_args, stderr="")
    assert bool(clock.sleeps) == waited
    assert out.endswith(f"ready: {URL}\n" if expected == 0 else
                        f"not ready: required check '{name}' is failure{FAILED_CHECK_HINT}\n")
    assert (f"waiting: required check '{name}' is failure" in out) == waited
    assert out.count("predates the review; awaiting refresh") == int(
        waited and (age < 0 or age >= 120) and not pending
    )


def test_ready_wait_stops_if_pr_head_moves(wt: Path, gh: FakeGh, capsys, monkeypatch, head) -> None:
    body = "# T1\n\n## Review\n"
    gh.respond_pr_list([pr_json(headRefOid=head, body=body, isDraft=False)],
               [pr_json(headRefOid="f" * 40, body=body, isDraft=False)])
    gh.respond_pr_view( pr_json(headRefOid="f" * 40, body=body, isDraft=False))
    gh.respond(reviews_key(), [rev(head, "primary")])
    arm_readiness(gh, head, wt, conclusion="pending")

    monkeypatch.setattr(cli, "_sleep", lambda _: None)
    code, out, err = run(capsys, "ready", "--wait")

    out = without_accepted_receipt(out)
    assert code == 1 and "waiting:" in out
    assert f"PR head changed while waiting: {head[:12]} to {'f' * 12}" in err


@pytest.mark.parametrize("running", [False, True], ids=["missing-ci", "late-required-job"])
def test_ready_wait_with_missing_required_check(wt, gh, capsys, monkeypatch, head, running):
    # #189: an upstream job can be running before the required job appears.
    arm_approved_ready_pr(gh, head, wt, isDraft=False)
    arm_check_runs(gh, head, [{"name": "Backend Tests", "status": "in_progress"}] if running else [])
    monkeypatch.setattr(cli, "MISSING_RUN_GRACE", 0.0)

    wait = "--wait=0" if running else "--wait"
    code, out, err = run(capsys, "ready", wait)

    if running:
        assert (code, err) == (3, "") and "timed out waiting for required checks" in out
    else:
        assert code == 1 and "waiting:" in out and f"head {head[:12]}" in err
        assert "workflow scheduling, triggers and permissions" in err
        assert "after correcting the cause, rerun the same `--wait` command" in err
        assert not any(s in err for s in ("close", "reopen", "retrigger", "created no run"))
    for endpoint in ("check-runs", "statuses"):
        assert sum(
            c["argv"][:2] == ["api", f"repos/{REPO}/commits/{head}/{endpoint}"]
            for c in gh.calls
        ) == 1


def test_wait_keeps_waiting_when_a_sibling_required_check_is_running(
    monkeypatch, tmp_path: Path
) -> None:
    """A missing check next to a pending one means CI is alive: time out, do not abort."""
    (tmp_path / "workflow.toml").write_text(WORKFLOW.replace('["checks"]', '["a", "b"]'))
    config = cli.config_mod.load(tmp_path / "workflow.toml")
    pr = cli.github.PR(7, URL, "a" * 40, "main", False, "OPEN", "MERGEABLE")
    reasons = (f"required check 'a' missing on {'a' * 12}", "required check 'b' is pending")
    monkeypatch.setattr(cli.github, "pr_view", lambda *a, **k: pr)
    monkeypatch.setattr(cli, "_readiness", lambda *a, **k: cli.github.Readiness(False, reasons))
    monkeypatch.setattr(cli, "MISSING_RUN_GRACE", 0.0)

    assert cli._wait_for_checks(Path("."), config, pr, 0.0, kicked=False) is None


def test_ready_draft_with_open_blocking_finding_does_not_mark_ready(
    wt: Path, gh: FakeGh, capsys, head
) -> None:
    finding = {
        "isResolved": False, "path": "feature.py", "line": 1,
        "comments": {"nodes": [{"author": {"login": LOGIN}, "body":
            f"<!-- loopzero:finding v=1 severity=important head={head} id=0b9b59e7 -->\n"
            "**important: Fix this**"}]},
    }
    arm_approved_pr_with_check_result(gh, head, wt, "skipped", readiness={"threads": (finding,)})

    out, _ = expect_cli(capsys, 1, "ready", stderr="")
    assert "not ready: open important finding at feature.py:1: Fix this" in out
    assert all(c["argv"][:2] != ["pr", "ready"] for c in gh.calls)


@pytest.mark.parametrize("draft,condition,reason", [
    (False, "missing", "run loopzero check at this head"),
    *((False, condition, "check receipt") for condition in ("wrong-head", "failed", "dirty")),
    (False, "wrong-commands", "check receipt's plan differs from the trusted workflow."),
    (False, "legacy", "check receipt is missing its settings fingerprint."),
    (False, "incompatible", "check receipt has an incompatible settings fingerprint."),
    (False, "matching", None),
    (True, "stale", "check receipt is for aaaaaaaaaaaa."),
    (True, "red", "check receipt records a failed stage."),
    (True, "dirty", "check receipt was recorded in a dirty worktree."),
    (True, "commands", "check receipt's plan differs from the trusted workflow."),
    *((True, condition, "check receipt is incomplete.") for condition in ("empty", "checkpoint")),
])
def test_ready_requires_clean_successful_local_report_at_head(wt, gh, capsys, condition, head, draft, reason):
    arm_approved_pr_with_check_result(gh, head, wt, "success", isDraft=draft, readiness={
        "local_exit": int(condition == "failed"), "dirty": condition == "dirty",
    })
    path = wt / ".loopzero/checks.json"
    if condition == "missing":
        path.unlink()
    elif condition not in {"failed", "dirty", "matching"}:
        report = json.loads(path.read_text())
        if condition in {"wrong-head", "stale"}:
            report["head"] = ("a" if draft else "0") * 40
        elif condition == "wrong-commands":
            report["results"].pop()
        elif condition == "commands":
            report["results"][0]["command"] = "changed"
        elif condition == "empty":
            report["results"] = []
        elif condition == "checkpoint":
            report["results"][1:] = [asdict(CheckResult("<checks incomplete>", 1, 0.0, ""))]
        elif condition == "red":
            report["results"][0]["exit_code"] = 1
        elif condition == "legacy":
            del report["settings_sha256"]
        else:
            report["settings_sha256"] = "0" * 64
        path.write_text(json.dumps(report))

    out, _ = expect_cli(capsys, int(reason is not None), "ready")
    if reason:
        assert f"not ready: {reason}" in out
    if draft:
        assert "receipt accepted:" not in out
    if condition in {"legacy", "incompatible"}:
        assert ("Run loopzero check in this worktree at this head with the same current CLI and effective "
                "settings as ready, using a local coordinator for delegated stages." in out)


@pytest.fixture
def check_environment(wt, gh, head, tmp_path):
    arm_approved_ready_pr(gh, head, wt, readiness={"local_exit": None}, isDraft=False)
    path = tmp_path / "check-policy.toml"
    policy = WORKFLOW.replace('"echo ok"', json.dumps('test "$CHECK_POLICY" = old'))
    return path, policy, ("--config", str(path))


@pytest.mark.parametrize("wait_for", [None, "ready", "merge"])
def test_ready_requires_receipt_from_current_check_environment(gh, capsys, head, monkeypatch, wait_for, check_environment):
    config_path, config, args = check_environment
    config = config.replace("[checks]", '[checks]\nenv = { CHECK_POLICY = "old" }')
    config_path.write_text(config)

    assert run(capsys, *args, "check")[0] == 0
    assert run(capsys, *args, "ready")[0] == 0

    new_config = config.replace('CHECK_POLICY = "old"', 'CHECK_POLICY = "new"')
    if wait_for:
        arm_check_runs(gh, head, [{"name": "checks", "status": "in_progress"}])

        def change_policy(_seconds):
            assert config_path.read_text() == config
            config_path.write_text(new_config)

        monkeypatch.setattr(cli, "_sleep", change_policy)
        code, _, err = run(capsys, *args, wait_for, "--wait")
        assert code == 1 and "workflow settings changed while waiting; run loopzero check" in err
        arm_check_runs(gh, head, [
            {"name": "checks", "status": "completed", "conclusion": "success"}])
    else:
        config_path.write_text(new_config)
    out, _ = expect_cli(capsys, 1, *args, "ready")
    assert "not ready: check receipt has an incompatible settings fingerprint." in out
    assert run(capsys, *args, "check")[0] == 1


def test_check_refuses_unverified_receipt_after_runner_retarget(wt, gh, capsys, head, tmp_path):
    runner = tmp_path / "runner"
    original = tmp_path / "runner-a"
    replacement = tmp_path / "runner-b"
    stage = (
        f"#!{sys.executable}\n"
        "import json, sys\n"
        "frame = json.load(sys.stdin)\n"
        "del frame['env']\n"
        "frame.update(exit_code=0, duration_s=0, not_started=False, tail='')\n"
    )
    for target, extra in (
        (original, f"from pathlib import Path\np = Path({str(runner)!r})\n"
                   f"p.unlink()\np.symlink_to({str(replacement)!r})\n"),
        (replacement, ""),
    ):
        target.write_text(stage + extra + "print(json.dumps(frame))\n")
        target.chmod(0o755)
    runner.symlink_to(original)
    publish_workflow(wt, WORKFLOW.replace("[checks]", f"[checks]\nrunner = {json.dumps([str(runner)])}"))
    arm_approved_ready_pr(gh, head, wt, readiness={"local_exit": None}, isDraft=False)

    out, err = expect_cli(capsys, 1, "check")
    assert "receipt produced:" not in out and "PASS" not in out
    assert "stages passed but the receipt is unverified" in err
    assert "rerun loopzero check from the task worktree with a stable, readable configured runner" in err
    report = cli._load_report(wt)
    assert report.head == head and not report.dirty and report.ok
    assert report.settings_sha256 is None
    assert run(capsys, "ready")[0] == 1

    expect_cli(capsys, 0, "check", stdout_contains=("receipt produced:",))
    assert run(capsys, "ready")[0] == 0


@pytest.mark.parametrize("base_sensitive", [False, True])
def test_wait_snapshot_handles_trusted_base_advance(wt, repo, base_sensitive):
    if base_sensitive:
        publish_workflow(wt, WORKFLOW.replace('[delivery]', '[checks.inputs]\n"*" = ["."]\n\n[delivery]'))
    args = SimpleNamespace(command="ready", config=None)
    config = cli._load_config(args, wt, pin_checks=True)
    head = head_of(wt)
    git(repo, "commit", "-qm", "advance base without editing policy", "--allow-empty")
    git(repo, "push", "-q", "origin", "main")

    if base_sensitive:
        with pytest.raises(cli.CliError, match="check base changed while waiting; run loopzero check"):
            cli._verify_wait_snapshot(args, wt, config, head, head)
    else:
        cli._verify_wait_snapshot(args, wt, config, head, head)


@pytest.mark.parametrize("fixed", [False, True])
def test_ready_matches_effective_inherited_check_environment(capsys, monkeypatch, fixed, check_environment):
    config_path, config, args = check_environment
    config = config.replace("[checks]", '[checks]\nenv_allowlist = ["PATH", "CHECK_POLICY"]')
    if fixed:
        config = config.replace("[checks]", '[checks]\nenv = { CHECK_POLICY = "old" }')
    config_path.write_text(config)

    monkeypatch.setenv("CHECK_POLICY", "old")
    assert run(capsys, *args, "check")[0] == 0
    monkeypatch.setenv("CHECK_POLICY", "new")
    code, out, _ = run(capsys, *args, "ready")
    if fixed:
        assert code == 0
    else:
        assert code == 1 and "not ready: check receipt has an incompatible settings fingerprint." in out


@pytest.mark.parametrize("worker", ["legacy", "different-path", "matching"])
def test_ready_checks_transferred_whole_job_receipt(wt, gh, capsys, head, tmp_path, monkeypatch, worker):
    arm_approved_ready_pr(gh, head, wt, readiness={"local_exit": None}, isDraft=False)
    with monkeypatch.context() as worker_env:
        if worker == "different-path":
            worker_env.setenv("PATH", str(tmp_path / "worker-bin"), prepend=":")
        assert run(capsys, "check")[0] == 0

    receipt = wt / ".loopzero/checks.json"
    payload = json.loads(receipt.read_bytes())
    if worker == "legacy":
        # Older producers serialize these results without a settings fingerprint.
        del payload["settings_sha256"]
    # Whole-job transport preserves the producer's fields.
    receipt.write_text(json.dumps(payload))
    assert run(capsys, "ready")[0] == (0 if worker == "matching" else 1)

    if worker != "matching":
        assert run(capsys, "check")[0] == 0
        assert run(capsys, "ready")[0] == 0


def test_ready_ignores_forged_markers(wt: Path, gh: FakeGh, capsys, head) -> None:
    arm_approved_pr_with_check_result(gh, head, wt, "success", reviews=[rev(head, "primary", login="stranger"), rev(head, "primary", commit="1" * 40)])
    code, out, _ = run(capsys, "ready")
    out = without_accepted_receipt(out)
    assert code == 1 and out == "not ready: no review recorded for the current head\n"
    assert sum(1 for c in gh.calls if c["argv"][:2] == ["api", "graphql"]) == 2


@pytest.mark.parametrize("state", ["DISMISSED", "PENDING", "COMMENTED"])
def test_ready_respects_latest_review_state(wt: Path, gh: FakeGh, capsys, state, head) -> None:
    arm_approved_pr_with_check_result(gh, head, wt, "success", reviews=[rev(head, "primary"), rev(head, "delta", state=state, verdict="request_changes")])
    gh.respond("pr ready", "")
    code, out, _ = run(capsys, "ready")
    out = without_accepted_receipt(out)
    assert (code, out) == ((0, f"ready: {URL}\n") if state == "COMMENTED" else
                           (1, "not ready: no review recorded for the current head\n"))


def test_merge_refuses_when_not_ready(wt: Path, gh: FakeGh, capsys, head) -> None:
    arm_pr(gh, head, isDraft=False)
    gh.respond(reviews_key(), [rev(head, "primary")])
    arm_readiness(gh, head, wt, conclusion="failure", threads=(
        {"isResolved": False, "isOutdated": False, "path": "feature.py", "line": 1,
         "comments": {"nodes": [{"author": {"login": LOGIN}, "body": f"<!-- loopzero:finding v=1 severity=critical head={head} id=0b9b59e7 -->"
                                         "\n**critical: Bad**"}]}},))
    code, out, err = run(capsys, "merge")
    assert code == 1 and without_accepted_receipt(out) == ""
    assert f"not ready to merge: open critical finding at feature.py:1: Bad; required check 'checks' is failure{FAILED_CHECK_HINT}" in err
    assert wt.exists()


@pytest.mark.parametrize("branch,changed", [
    ("lz/t1", False),
    ("t3/archive-review-receipts", False),
    ("od183-fable-5-2", False),
    ("codex/task", False),
    ("lz/t1", True),
])
def test_merge_prints_sha_and_cleans_up(
    wt: Path, repo: Path, gh: FakeGh, capsys, changed, head, branch
) -> None:
    git(wt, "branch", "-m", branch)
    arm_squash_merge(gh, head, wt, merge_json(state='MERGED', head='f' * 40 if changed else head_of(wt), sha='c' * 40), branch=branch)
    gh.respond("api -X", "")
    code, out, err = run(capsys, "merge")
    out = without_accepted_receipt(out)
    if changed:
        assert code == 1 and "head changed" in err and wt.exists()
        assert all(c["argv"][:2] != ["api", "-X"] for c in gh.calls)
        return
    assert (code, out, err) == (0, f"{'c' * 40}\n", "")
    merge = next(c["argv"] for c in gh.calls if c["argv"][:2] == ["pr", "merge"])
    assert "--squash" in merge and merge[merge.index("--match-head-commit") + 1] == head
    assert not wt.exists()
    assert git(repo, "branch", "--list", branch) == ""


@pytest.mark.parametrize("state", ["clean", "dirty", "wrong_primary"])
def test_merged_bare_primary_cleans_only_clean_linked_worktree(
    repo: Path, gh: FakeGh, capsys, monkeypatch, state
) -> None:
    primary = repo.parent / "origin.git"
    linked = repo.parent / "linked task"
    git(primary, "worktree", "add", "-q", "-b", "lz/bare", str(linked), "main")
    monkeypatch.chdir(linked)
    head = head_of(linked)
    git(primary, "update-ref", "refs/remotes/origin/main", head)
    git(primary, "symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/main")
    arm_pr(gh, head, branch="lz/bare", isDraft=False, state="MERGED")
    gh.append(f"api repos/{REPO}/pulls/7", merge_json(state='MERGED', head=head, sha='c' * 40))
    gh.respond("api -X", "")
    if state == "dirty":
        (linked / "README.md").write_text("uncommitted work\n")
    original, cwds = cli._git, []  # #439: no cleanup git in a primary checkout
    monkeypatch.setattr(cli, "_git", lambda cwd, *args, **kwargs: cwds.append(Path(cwd).resolve()) or (
        f"worktree {repo}\0" if state == "wrong_primary" and args == ("worktree", "list", "--porcelain", "-z")
        else original(cwd, *args, **kwargs)))

    code, out, err = run(capsys, "merge")
    assert {primary.resolve(), repo.resolve()}.isdisjoint(cwds)

    if state == "wrong_primary":
        assert code == 1 and "does not own this linked worktree" in err
    else:
        assert code == 0 and out.endswith(f"{'c' * 40}\n") and "cd " not in out, (out, err)
        assert ("worktree is dirty" in err) if state == "dirty" else not err
    assert linked.exists() is (state != "clean")
    assert bool(git(primary, "branch", "--list", "lz/bare")) is (state != "clean")
    assert "bare" in git(primary, "worktree", "list", "--porcelain")


@pytest.mark.parametrize("already_landed", [False, True])
def test_merge_wait_waits_out_a_pending_check_then_merges(
    wt: Path, gh: FakeGh, capsys, monkeypatch, head, already_landed
) -> None:
    arm_approved_pr_with_check_result(gh, head, wt, "pending", isDraft=False)
    arm_check_runs(gh, head, *([{"name": "checks", "status": "in_progress"}],) * 2,
                   [{"name": "checks", "status": "completed", "conclusion": "success"}])
    gh.respond("pr merge", "")
    open_pr = rest_pr_json(pr_json(headRefOid=head, isDraft=False))
    landed = merge_json(state="MERGED", head=head, sha="c" * 40)
    polls = ({**open_pr, **landed, "head": open_pr["head"]}, landed) if already_landed else (open_pr, open_pr, open_pr, landed)
    gh.append(f"api repos/{REPO}/pulls/7", *polls)
    gh.respond("api -X", "")

    monkeypatch.setattr(cli, "_sleep", lambda _: None)
    out, _err = expect_cli(capsys, 0, "merge", "--wait", stderr="")
    if already_landed:
        assert "was already merged" in out
        assert all(c["argv"][:2] != ["pr", "merge"] for c in gh.calls)
    else:
        assert "waiting: required check 'checks' is pending" in out
    assert out.splitlines()[-1] == "c" * 40 and not wt.exists()


def test_merge_wait_times_out_on_pending_checks_with_exit_three(
    wt: Path, gh: FakeGh, capsys, head
) -> None:
    arm_approved_pr_with_check_result(gh, head, wt, "pending", isDraft=False)

    out, _ = expect_cli(capsys, 3, "merge", "--wait=0", stderr="")
    assert f"timed out waiting for required checks on {head[:12]}. Run `loopzero merge --wait` to resume on this head." in out
    assert wt.exists() and all(c["argv"][:2] != ["pr", "merge"] for c in gh.calls)


def test_merge_wait_bounds_slow_source_check_read(wt, gh, capsys, monkeypatch, head, replay_gh):
    arm_approved_pr_with_check_result(gh, head, wt, "pending", isDraft=False)
    budgets = []
    initial_check_seen = False
    run_gh = github.run
    clock = poll_clock(monkeypatch, start=1000.0)
    monkeypatch.setattr(github, "_retry_sleep", lambda _: None)

    def slow_check(argv, *, timeout, **kwargs):
        nonlocal initial_check_seen
        if len(argv) > 2 and argv[2].endswith("/check-runs"):
            if initial_check_seen:
                budgets.append(timeout)
                clock.now += timeout
                raise cli._proc.ProcTimeout("gh api timed out", "")
            initial_check_seen = True
        return run_gh(argv, timeout=timeout, **kwargs)

    monkeypatch.setattr(github, "run", slow_check)
    out, _err = expect_cli(capsys, 3, "merge", "--wait=16", stderr="")
    assert budgets == pytest.approx([8.0])
    assert f"timed out waiting for required checks on {head[:12]}. Run `loopzero merge --wait` to resume on this head." in out
    assert all(c["argv"][:2] != ["pr", "merge"] for c in gh.calls)


@pytest.mark.parametrize("strategy", ["squash", "mergify"])
@pytest.mark.parametrize("ready", [True, False], ids=["green", "terminal-failure"])
def test_merge_wait_rejects_verdicts_after_deadline(wt, gh, capsys, monkeypatch, strategy, ready):
    if strategy == "mergify":
        commit_mergify_configuration(wt)
        monkeypatch.setattr(mergify, "configured", lambda *_: True)
        if ready:
            monkeypatch.setattr(mergify, "markers", lambda *_: (False, False))
            monkeypatch.setattr(mergify, "membership", lambda *_: None)
        monkeypatch.setattr(mergify, "request", lambda *_: pytest.fail("queued after the deadline"))
    arm_approved_pr_with_check_result(gh, head_of(wt), wt, "pending", isDraft=False)
    clock = poll_clock(monkeypatch, start=1000.0, patch_sleep=False)
    failed = "required check 'checks' is failure"

    def check_after_deadline(_wt, _config, pr, *_args, **_kwargs):
        clock.now += 11
        return pr, github.Readiness(ready=ready, reasons=() if ready else (failed,)), False

    monkeypatch.setattr(cli, "_wait_for_checks", check_after_deadline)
    code, out, err = run(capsys, "merge", "--wait=10")
    assert wt.exists()
    assert (code, err) == (3, "")
    assert "timed out waiting for required checks" in out
    assert all(c["argv"][:2] != ["pr", "merge"] for c in gh.calls)


@pytest.mark.parametrize("pending", [False, True])
@pytest.mark.parametrize("safe", [False, True])
def test_merge_uses_one_queue_proof_for_admission(
    wt, gh, capsys, monkeypatch, source_queue_api, pending, safe,
):
    head = commit_mergify_configuration(wt)
    arm_approved_pr_with_check_result(gh, head, wt, "pending" if pending else "success", isDraft=False)
    repo, config_path, vendor_calls = source_queue_api
    requests = []
    before_final = 0

    def set_unsafe():
        gh.respond(f"api {config_path}", {"type": "symlink"})

    def after_sleep():
        nonlocal before_final
        before_final = len(gh.calls)
        vendor_calls.clear()
        arm_check_runs(gh, head, [{"name": "checks", "conclusion": "success"}])
        if not safe:
            set_unsafe()

    if not pending and not safe:
        set_unsafe()
    clock = poll_clock(monkeypatch, after_sleep=after_sleep)
    stub_mergify_admission(monkeypatch, configured=None)
    monkeypatch.setattr(mergify, "request", lambda *args: requests.append(args))
    monkeypatch.setattr(cli, "_wait_for_mergify", lambda *_: None)

    code, out, err = run(capsys, "merge", "--wait=90")

    assert (clock.now > 0) == pending
    assert requests == ([(REPO, 7, head, "main")] if safe else [])
    assert code == (3 if safe else 1), out + err
    assert ("Mergify queue configuration is unverified" in err) == (not safe)
    paths = [call["argv"][1] for call in gh.calls[before_final:]]
    assert paths.count(repo) == paths.count(config_path) == (2 if safe else 1)
    assert vendor_calls == ["/merge-queue/status?branch=main", "/queues/configuration"]


@pytest.mark.parametrize("wait", [False, True])
@pytest.mark.parametrize("outcome", ["open", "changed", "closed", "invalid", "merged"])
def test_mergify_admission_rechecks_only_the_selected_pr(wt, gh, capsys, monkeypatch, wait, outcome):
    head = commit_mergify_configuration(wt)
    arm_approved_ready_pr(gh, head, wt, isDraft=False)
    detail = f"api repos/{REPO}/pulls/7"
    merged_sha = "e" * 40
    snapshots = {
        "open": rest_pr_json(pr_json(headRefOid=head, isDraft=False)),
        "changed": merge_json(head="f" * 40),
        "closed": merge_json(state="CLOSED", head=head),
        "invalid": {"state": "open", "head": {"sha": head}},
        "merged": merge_json(state="MERGED", head=head, sha=merged_sha),
    }
    monkeypatch.setattr(mergify, "configured", lambda *_: True)
    monkeypatch.setattr(mergify, "markers", lambda *_: (True, outcome == "open"))

    def membership(*_):
        gh.append(detail, snapshots[outcome])
        return mergify.Membership()

    monkeypatch.setattr(mergify, "membership", membership)
    monkeypatch.setattr(mergify, "mark_confirmed", lambda *_: pytest.fail("unexpected confirmation"))
    monkeypatch.setattr(mergify, "request", lambda *_: pytest.fail("unexpected request"))
    monkeypatch.setattr(cli, "_wait_for_mergify", lambda *_: None)
    gh.respond("api -X", "")

    code, out, err = run(capsys, "merge", *(["--wait=1"] if wait else []))

    calls = [c["argv"][:2] for c in gh.calls]
    assert calls.count(LOOKUP_CALLS[0]) == 1
    assert calls.count(detail.split()) == 2
    if outcome == "merged":
        assert (code, err) == (0, "") and out.splitlines()[-1] == merged_sha
        assert not wt.exists()
    elif outcome == "open":
        assert (code, err) == (3 if wait else 0, "")
        assert wt.exists()
    else:
        diagnostic = {"changed": "head changed", "closed": "closed without", "invalid": "invalid PR"}
        assert code == 1 and diagnostic[outcome] in err
        assert wt.exists()


@pytest.mark.parametrize("admission_expires", [True, False], ids=["admission-expiry", "landing-budget"])
def test_mergify_uses_one_deadline_for_checks_admission_and_landing(
    wt, gh, capsys, monkeypatch, admission_expires
):
    head = commit_mergify_configuration(wt)
    arm_approved_pr_with_check_result(gh, head, wt, "pending", isDraft=False)
    stub_mergify_admission(monkeypatch)
    budgets = []
    clock = poll_clock(monkeypatch, start=1000.0, patch_sleep=False)

    def spend_admission_time(*_args):
        clock.now += 2

    def finish_checks(_wt, _config, pr, *_args, **_kwargs):
        clock.now += 9 if admission_expires else 4
        return pr, github.Readiness(ready=True, reasons=()), False

    if admission_expires:
        monkeypatch.setattr(mergify, "membership", spend_admission_time)
        monkeypatch.setattr(mergify, "request", lambda *_: pytest.fail("queued after the deadline"))
    else:
        monkeypatch.setattr(mergify, "request", spend_admission_time)
        monkeypatch.setattr(cli, "_wait_for_mergify", lambda _c, _p, timeout, _f: budgets.append(timeout))
    monkeypatch.setattr(cli, "_wait_for_checks", finish_checks)

    out, err = expect_cli(capsys, 3, "merge", "--wait=10")
    if admission_expires:
        assert err == "" and "timed out before requesting Mergify for PR #7" in out
    else:
        assert budgets == [4.0]


@pytest.mark.parametrize("removed,error,hint", [
    (False, "worktree is dirty",
     "; fix the cause, then rerun `loopzero merge` in this worktree to finish cleanup"),
    (True, "cannot lock ref",
     "; worktree removed; delete local branch 'lz/t1' if it still points at the merged head"),
], ids=["worktree-remains", "branch-remains"])
def test_merge_warns_when_cleanup_fails(wt, gh, capsys, monkeypatch, head, removed, error, hint):
    arm_squash_merge(gh, head, wt, merge_json(state="MERGED", head=head, sha="d" * 40))
    gh.respond("api -X", "")

    def fail_cleanup(path, _expected_head):
        if removed:
            shutil.rmtree(path)
        raise cli.worktree.WorktreeError(error)

    monkeypatch.setattr(cli.worktree, "cleanup", fail_cleanup)
    code, out, err = run(capsys, "merge")

    assert code == 0 and out.splitlines()[-1] == "d" * 40
    assert err.startswith(f"warning: merged but worktree cleanup failed: {error}")
    assert err.rstrip().endswith(hint)


def test_cleanup_hints_name_branch_and_configured_repo_without_commands(
    tmp_path: Path, capsys, monkeypatch
) -> None:
    branch = "lz/fix$version"
    monkeypatch.setattr(cli, "_repo_root", lambda _wt: tmp_path)
    monkeypatch.setattr(cli.worktree, "branch", lambda _wt: branch)
    monkeypatch.setattr(cli.worktree, "cleanup", lambda *_: (_ for _ in ()).throw(
        cli.worktree.WorktreeError("cannot lock ref")))
    cli._cleanup(tmp_path / "gone", SimpleNamespace(head_sha="a" * 40))
    assert capsys.readouterr().err.rstrip().endswith(
        "delete local branch 'lz/fix$version' if it still points at the merged head")

    monkeypatch.setattr(cli.github, "delete_remote_branch", lambda *_: (_ for _ in ()).throw(
        cli.github.GhError(("gh", "api"), "HTTP 500")))
    cli._cleanup_remote_refs(tmp_path / "gone", SimpleNamespace(repo="acme/widgets"), branch, "a" * 40)
    assert capsys.readouterr().err.rstrip().endswith(
        "delete 'lz/fix$version' in acme/widgets if it still points at the merged head")


@pytest.mark.parametrize("remote", ["deleted", "absent", "moved", "failed"])
@pytest.mark.parametrize("tracking_matches", [True, False])
def test_merge_cleans_only_stale_task_tracking_ref(
    wt, repo, gh, capsys, head, remote, tracking_matches
):
    branch = "lz/t1"
    tracking = f"refs/remotes/origin/{branch}"
    git(wt, "push", "-u", "origin", branch)
    other = git(repo, "rev-parse", "origin/main").strip()
    if not tracking_matches:
        git(repo, "update-ref", tracking, other)
    reflog = repo / ".git" / "logs" / tracking
    assert reflog.is_file()

    arm_pr(gh, head, state="MERGED", isDraft=False)
    gh.append(f"api repos/{REPO}/pulls/7", merge_json(state="MERGED", head=head, sha="e" * 40))
    ref = f"api repos/{REPO}/git/ref/heads/{branch}"
    git(repo, "config", "push.followTags", "true")
    git(wt, "tag", "-am", "local only", "local-only", "origin/main")
    if remote == "absent":
        git(repo, "push", "--no-follow-tags", "--delete", "origin", branch)
        git(repo, "update-ref", "--create-reflog", tracking, head if tracking_matches else other)
        gh.fail(ref, "gh: Not Found (HTTP 404)")
    else:
        gh.respond(ref, {"object": {"sha": other if remote == "moved" else head}})
        if remote == "failed":
            git(repo.parent / "origin.git", "config", "receive.denyDeletes", "true")

    _, _ = expect_cli(capsys, 0, "merge")
    if remote in {"deleted", "absent"} and tracking_matches:
        assert not git(repo, "for-each-ref", tracking)
        assert not reflog.exists()
    else:
        assert git(repo, "rev-parse", tracking).strip() == (head if tracking_matches else other)
        assert reflog.is_file()
    assert not git(repo, "ls-remote", "--tags", "origin")
    assert git(repo, "rev-parse", "origin/main").strip() == other


@pytest.mark.parametrize("raced", [False, True])
def test_merge_warns_when_remote_branch_moves(wt, repo, gh, capsys, monkeypatch, head, raced):
    landed_sha = "e" * 40
    branch = "refs/heads/lz/t1"
    remote_ref = f"repos/{REPO}/git/ref/heads/lz/t1"
    arm_pr(gh, head, state="MERGED", isDraft=False)
    gh.append(f"api repos/{REPO}/pulls/7", merge_json(state="MERGED", head=head, sha=landed_sha))
    git(wt, "push", "origin", f"{head}:{branch}")
    newer = git(wt, "commit-tree", "HEAD^{tree}", "-p", head, "-m", "new work").strip()
    read = github.api_get

    def observe(endpoint, *args, **kwargs):
        if endpoint == remote_ref:
            current = git(wt, "ls-remote", "origin", branch).split()[0]
            if current == head:
                git(wt, "push", "origin", f"{newer}:{branch}")
                current = head if raced else newer
            return {"object": {"sha": current}}
        return read(endpoint, *args, **kwargs)

    monkeypatch.setattr(github, "api_get", observe)
    out, err = expect_cli(capsys, 0, "merge")
    assert out.splitlines()[-1] == landed_sha
    assert f"warning: merged; remote branch lz/t1 moved from {head[:12]}; skipping deletion" in err
    assert git(repo, "ls-remote", "origin", branch).split() == [newer, branch]


def test_merge_warns_when_remote_cleanup_times_out(
    wt: Path, gh: FakeGh, capsys, monkeypatch, head
) -> None:
    arm_squash_merge(gh, head, wt, merge_json(state='MERGED', head=head_of(wt), sha='f' * 40))
    monkeypatch.setattr(cli.github, "delete_remote_branch", lambda *_: (_ for _ in ()).throw(
        cli._proc.ProcTimeout("gh api timed out", "")))
    monkeypatch.setattr(cli, "_cleanup", lambda *_: None)
    code, out, err = run(capsys, "merge")
    out = without_accepted_receipt(out)
    assert code == 0 and out.splitlines()[0] == "f" * 40
    assert "remote branch cleanup failed for lz/t1" in err and "timed out" in err
    assert err.rstrip().endswith(
        f"; delete 'lz/t1' in {REPO} if it still points at the merged head")


def test_second_merge_retries_remote_branch_cleanup(
    wt: Path, repo: Path, gh: FakeGh, capsys, monkeypatch, head
) -> None:
    gh.respond(f"api repos/{REPO}/git/ref/heads/lz/t1", {"object": {"sha": head}})
    gh.respond_pr_list(
        [pr_json(headRefOid=head, isDraft=False)],
        [pr_json(headRefOid=head, isDraft=False, state="MERGED")],
    )
    gh.respond(reviews_key(), [rev(head, "primary")])
    arm_readiness(gh, head, wt)
    gh.respond("pr merge", "")
    gh.respond(f"api repos/{REPO}/pulls/7",
               rest_pr_json(pr_json(headRefOid=head, isDraft=False)),
               merge_json(state='MERGED', head=head, sha='e' * 40),
               rest_pr_json(pr_json(headRefOid=head, isDraft=False, state="MERGED")),
               merge_json(state='MERGED', head=head, sha='e' * 40))
    origin = repo.parent / "origin.git"
    git(wt, "push", "origin", "HEAD:refs/heads/lz/t1")
    git(origin, "config", "receive.denyDeletes", "true")
    monkeypatch.setattr(cli, "_cleanup", lambda *_: None)

    first = run(capsys, "merge")
    git(origin, "config", "receive.denyDeletes", "false")
    second = run(capsys, "merge")

    assert first[0] == 0 and "remote branch cleanup failed for lz/t1" in first[2]
    assert second[0] == 0 and second[2] == ""
    assert not git(repo, "ls-remote", "origin", "refs/heads/lz/t1")



def test_status_names_base_workflow_for_invalid_pinned_checks(git_repo, monkeypatch, capsys):
    monkeypatch.chdir(git_repo)
    workflow = git_repo / "workflow.toml"
    contents = '[repo]\nname = "example/repo"\n[checks]\ncommands = false\n'
    commit_file(git_repo, workflow.name, contents, "invalid base workflow")
    git(git_repo, "update-ref", "refs/remotes/origin/main", "HEAD")
    git(git_repo, "symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/main")
    contents = contents.replace("false", '["true"]')
    for active in (contents, contents.replace('"example/repo"', "false")):
        workflow.write_text(active)
        expect_cli(capsys, 1, "status", stderr_contains=("origin/main:workflow.toml: checks.commands must be a list",))


def test_status_rejects_scratch_path_naming_worktree_root(git_repo, monkeypatch, capsys):
    monkeypatch.chdir(git_repo)
    bad_config = git_repo / "bad.toml"
    bad_config.write_text('[repo]\nname = "example/repo"\n[checks]\nscratch = ["./"]\n')

    _, err = expect_cli(capsys, 1, "--config", "bad.toml", "status")
    assert "bad.toml: checks.scratch entries must be relative, got './'" in err

    bad_config.write_text(bad_config.read_text().replace('"./"', '"./.venv"'))
    assert config.load(bad_config).scratch == ("./.venv",)


def test_status_explains_detached_head(wt: Path, capsys) -> None:
    git(wt, "switch", "--detach")

    _out, err = expect_cli(capsys, 1, "status", stdout="")
    assert err == "loopzero status: HEAD is detached. Switch to a branch and retry.\n"


def test_status_full(wt: Path, gh: FakeGh, capsys, git_calls) -> None:
    head = head_of(wt)
    gh.respond_pr_list([])
    out, _ = expect_cli(capsys, 0, "status")
    assert "branch:   lz/t1" in out and f"head:     {head}" in out and "dirty:    no" in out
    assert "pr:       none (commit changes and pass `loopzero check` before `loopzero pr`)" in out
    arm_approved_pr_with_check_result(gh, head, wt, "pending")
    (wt / "scratch").write_text("x")
    git_calls.clear()
    code, out, _ = run(capsys, "status")
    assert_git_cost(git_calls, 7, 1)
    assert code == 0 and "dirty:    yes" in out
    assert f"pr:       #7 {URL} (draft, OPEN)" in out
    assert f"review:   primary on {head[:12]} (approved)" in out
    assert "ready:    no" in out and "  - required check 'checks' is pending" in out
    assert sum(1 for c in gh.calls if c["argv"][:2] == ["api", reviews_key()[4:]]) == 1


@pytest.mark.parametrize(("signum", "status"), [
    (signal.SIGTERM, -signal.SIGTERM),
    (signal.SIGINT, 130),
])
def test_status_reaps_gh_after_signal(wt, fake_tool, signum, status):
    fake_tool("gh", "exec /bin/sleep 30")
    driver = """
import subprocess
import sys
from loopzero import cli, github

github.app_token = lambda: ""
communicate = subprocess.Popen.communicate

def announce(process, *args, **kwargs):
    if process.args[0] == "gh":
        print(process.pid, flush=True)
    return communicate(process, *args, **kwargs)

subprocess.Popen.communicate = announce
sys.exit(cli.main(["status"]))
"""
    env = {**os.environ, "PYTHONPATH": str(Path(cli.__file__).resolve().parents[1])}
    with subprocess.Popen(
        [sys.executable, "-c", driver], cwd=wt, env=env, stdout=subprocess.PIPE, bufsize=0,
    ) as process:
        child = None
        try:
            # Read until the supervisor enters communicate on the offline gh child.
            for _ in range(6):
                assert select([process.stdout], [], [], 5)[0]
                line = process.stdout.readline().strip()
                if line.isdigit():
                    child = int(line)
                    break
            assert child is not None
            process.send_signal(signum)
            assert process.wait(timeout=5) == status
            with pytest.raises(ProcessLookupError):
                os.kill(child, 0)
        finally:
            process.kill()
            if child is not None:
                with suppress(ProcessLookupError):
                    os.killpg(child, signal.SIGKILL)


def test_status_silently_ignores_legacy_report_without_dirty(
    wt: Path, gh: FakeGh, capsys, head
) -> None:
    arm_approved_ready_pr(gh, head, wt, isDraft=False)
    report_path = wt / ".loopzero" / "checks.json"
    report = json.loads(report_path.read_text())
    del report["dirty"]
    report_path.write_text(json.dumps(report))

    out, _ = expect_cli(capsys, 0, "status", stderr="")
    assert "ready:    no" in out
    assert "  - check receipt was recorded in a dirty worktree." in out


def test_gh_errors_are_one_line_on_stderr(wt: Path, gh: FakeGh, capsys) -> None:
    gh.fail(pr_list_key(), "To get started with GitHub CLI, please run:  gh auth login\n", exit=4)
    code, out, err = run(capsys, "status")
    assert code == 1 and out.count("\n") >= 4 and err.count("\n") == 1
    assert err.startswith("loopzero status: gh api") and "gh auth login" in err


@pytest.mark.parametrize("command", ["ready", "merge"])
@pytest.mark.parametrize("budget,conclusion", [
    ("-1", "success"), ("NaN", "pending"), ("inf", "success"), ("-inf", "success"),
])
def test_invalid_wait_does_not_read_pr_state(wt: Path, gh: FakeGh, capsys, head, command, budget, conclusion):
    arm_approved_pr_with_check_result(gh, head, wt, conclusion, isDraft=False)

    with pytest.raises(SystemExit) as error:
        cli.main([command, f"--wait={budget}"])

    out, err = capsys.readouterr()
    assert error.value.code == 2 and out == ""
    assert "argument --wait: expected a finite, nonnegative number of seconds" in err
    assert gh.calls == []


# --- review findings -------------------------------------------------------------------


@pytest.mark.parametrize("base", ["main", "staging"])
def test_commands_refuse_base_branch(wt: Path, gh: FakeGh, capsys, base) -> None:
    workflow = wt / "workflow.toml"
    workflow.write_text(workflow.read_text().replace('base = "main"', f'base = "{base}"'))
    if base == "main":
        git(wt, "checkout", "-q", "--ignore-other-worktrees", base)
    else:
        publish_workflow(wt, workflow.read_text())
        git(wt, "update-ref", f"refs/remotes/origin/{base}", "refs/remotes/origin/main")
        git(wt, "checkout", "-q", "-b", base)
    for command in ("pr", "review", "ready", "merge"):
        code, out, err = run(capsys, command)
        assert code == 1 and out == "" and "configured base" in err, command
    assert gh.calls == []
    git(wt, "checkout", "-q", "--detach")
    for command in ("pr", "review", "ready", "merge"):
        code, out, err = run(capsys, command)
        assert code == 1 and out == "" and "HEAD is detached" in err, command
    assert gh.calls == []


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


@pytest.mark.parametrize("changed", [False, True])
def test_merge_of_externally_merged_pr_verifies_sha_and_cleans_up(
    wt: Path, repo: Path, gh: FakeGh, capsys, changed, monkeypatch
) -> None:
    collision, wt = wt, wt.with_name(wt.name + " ")
    git(repo, "worktree", "move", str(collision), str(wt))
    git(repo, "worktree", "add", "-qb", "lz/other", str(collision), "lz/t1")
    monkeypatch.chdir(wt)
    for branch, number in (("lz/t1", 7), ("lz/other", 8)):
        arm_pr(gh, head_of(wt), branch=branch, number=number, state="MERGED", isDraft=False)
        gh.append(f"api repos/{REPO}/pulls/{number}", merge_json(state='MERGED', head='f' * 40 if changed else head_of(wt), sha='e' * 40))
    code, out, err = run(capsys, "merge")
    assert (code, collision.exists(), wt.exists()) == (int(changed), True, changed)
    if changed:
        assert "head changed" in err
    else:
        assert not err and f"PR #7 was already merged as {'e' * 12}" in out and out.endswith("e" * 40 + "\n")


def test_merge_queued_prints_and_keeps_worktree(wt: Path, gh: FakeGh, capsys, head) -> None:
    arm_squash_merge(gh, head, wt, merge_json(state='OPEN', head=head_of(wt), sha=None))
    gh.respond("api graphql", threads_json(),
               {"data": {"repository": {"pullRequest": {"isInMergeQueue": True}}}})
    out, _err = expect_cli(capsys, 0, "merge", stderr="")
    assert "queued for merge into main" in out and out.endswith(
        "next: loopzero merge --wait follows the queue, verifies and cleans up\n"
    )
    assert wt.exists()
    assert all(c["argv"][:2] != ["api", "-X"] for c in gh.calls)


@pytest.mark.parametrize("attested", [True, False])
@pytest.mark.parametrize("merge_state", ["BEHIND", "CLEAN"])
def test_mergify_merge_requests_once_for_behind_head(
    wt: Path, gh: FakeGh, capsys, monkeypatch, attested, merge_state, git_calls
) -> None:
    head = commit_mergify_configuration(wt)
    arm_approved_ready_pr(gh, head, wt, isDraft=False, mergeStateStatus=merge_state)
    monkeypatch.setattr(mergify, "configured", lambda *_: attested)
    requested = []
    stub_mergify_admission(monkeypatch, configured=None)
    monkeypatch.setattr(mergify, "request", lambda *args: requested.append(args))

    git_calls.clear()
    code, out, err = run(capsys, "merge")
    assert git_calls.count(["branch", "--show-current"]) == 1

    if attested:
        assert (code, err) == (0, "")
        assert requested == [(REPO, 7, head, "main")]
        assert "requested from Mergify queue main" in out
    else:
        assert code == 1 and "queue configuration is unverified" in err and not requested


@pytest.mark.parametrize("wait", [False, True])
def test_mergify_merge_waits_for_pending_checks_only_with_wait(
    wt: Path, gh: FakeGh, capsys, monkeypatch, wait
) -> None:
    head = commit_mergify_configuration(wt)
    arm_approved_pr_with_check_result(gh, head, wt, "pending", isDraft=False)
    arm_check_runs(gh, head, *([{"name": "checks", "status": "in_progress"}],) * 2,
                   [{"name": "checks", "status": "completed", "conclusion": "success"}])
    stub_mergify_admission(monkeypatch)
    requested = []
    monkeypatch.setattr(mergify, "request", lambda *args: requested.append(args))
    monkeypatch.setattr(cli, "_sleep", lambda _: None)
    monkeypatch.setattr(cli, "_wait_for_mergify", lambda *_: "e" * 40)
    open_pr = rest_pr_json(pr_json(headRefOid=head, isDraft=False))
    gh.append(f"api repos/{REPO}/pulls/7", open_pr, open_pr, open_pr, open_pr)
    gh.respond("api -X", "")

    code, out, err = run(capsys, "merge", *(["--wait"] if wait else []))

    if wait:
        assert (code, err) == (0, "") and requested == [(REPO, 7, head, "main")]
        assert "waiting: required check 'checks' is pending" in out
        assert out.splitlines()[-1] == "e" * 40
    else:
        assert code == 1 and not requested
        assert "required check 'checks' is pending; next: loopzero merge --wait" in err


@pytest.mark.parametrize("command", ["ready", "status"])
@pytest.mark.parametrize("merge_state", ["CLEAN", "BEHIND"])
@pytest.mark.parametrize("attested", [True, False])
def test_ready_allows_behind_only_after_configured_mergify_attestation(
    wt: Path, gh: FakeGh, capsys, monkeypatch, command, merge_state, attested
) -> None:
    head = commit_mergify_configuration(wt)
    arm_approved_ready_pr(gh, head, wt, isDraft=False, mergeStateStatus=merge_state)
    monkeypatch.setattr(mergify, "configured", lambda *args: attested)

    gh.respond(f"api repos/{REPO}/issues/7/comments", [])
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
    head = commit_mergify_configuration(wt)
    arm_approved_ready_pr(gh, head, wt, isDraft=False)
    gh.append(f"api repos/{REPO}/pulls/7", rest_pr_json(pr_json(headRefOid=head, isDraft=False)))
    gh.append(f"api repos/{REPO}/pulls/7", merge_json(state='OPEN', head=head, sha=None))
    monkeypatch.setattr(mergify, "configured", lambda *_: True)
    monkeypatch.setattr(mergify, "markers", lambda *_: (True, True))
    monkeypatch.setattr(mergify, "membership", lambda *_: None)
    monkeypatch.setattr(
        mergify, "request", lambda *_: pytest.fail("resume must not submit another command")
    )

    code, _out, err = run(capsys, "merge", "--wait=1")

    assert 'Read Mergify\'s "Left the queue" comment' in err
    assert code == 1 and "post `@mergifyio queue` as the whole PR comment (same login), then rerun `loopzero merge --wait`" in err


@pytest.mark.parametrize("outcome", ["merged", "ejected", "landed-on-ejection",
                                      "head-changed-early", "head-changed-late"])
def test_merge_wait_preserves_reviewed_head_and_cleans_only_landed_work(
    wt: Path, gh: FakeGh, capsys, monkeypatch, head, outcome
) -> None:
    arm_squash_merge(gh, head, wt)
    open_pr = merge_json(state="OPEN", head=head, sha=None)
    changed = outcome.startswith("head-changed")
    landed = outcome in {"merged", "landed-on-ejection"}
    if changed:
        snapshots = [merge_json(head="f" * 40 if outcome.endswith("early") else head),
                     merge_json(state="MERGED", head="f" * 40, sha="e" * 40)]
    else:
        snapshots = [open_pr, open_pr,
                     merge_json(state="MERGED", head=head, sha="e" * 40) if landed else open_pr]
    gh.append(f"api repos/{REPO}/pulls/7", *snapshots)
    def queue(value: bool) -> dict:
        return {"data": {"repository": {"pullRequest": {"isInMergeQueue": value}}}}
    ejected = outcome in {"ejected", "landed-on-ejection"}
    gh.respond("api graphql", threads_json(), queue(True), queue(not ejected))
    gh.respond("api -X", "")
    monkeypatch.setattr(cli, "_sleep", lambda _: None)
    code, out, err = run(capsys, "merge", "--wait=0" if ejected else "--wait")
    if changed:
        assert code == 1 and "PR head changed" in err
        assert wt.exists(), "no cleanup after an unreviewed head"
    elif ejected:
        assert (code, wt.exists()) == ((0, False) if landed else (1, True))
        assert ("e" * 40 if landed else "left the merge queue unmerged") in out + err
    else:
        assert (code, err) == (0, "") and "e" * 40 in out
        assert "PR #7 is queued for merge into main; waiting" in out
        assert out.splitlines()[-1] == "e" * 40 and not wt.exists()


def _finding_thread(marker_id: str, head: str) -> dict:
    body = (f"<!-- loopzero:finding v=1 severity=important head={head} id={marker_id} -->\n"
            "**important: Off by one**\n\nThe loop skips the last row.")
    return {"id": "THREAD_1", "isResolved": False, "isOutdated": False, "path": "src/a.py",
            "line": 3, "comments": {"nodes": [{"author": {"login": LOGIN}, "body": body}]}}


@pytest.fixture
def finding_pr(wt, gh, head):
    gh.respond_pr_list([pr_json(headRefOid=head)])
    return _finding_thread("0b9b59e7", head)


REPLY_ACCEPTED = {"data": {"addPullRequestReviewThreadReply": {"comment": {"id": "C1"}}}}
THREAD_RESOLVED = {"data": {"resolveReviewThread": {"thread": {"isResolved": True}}}}


@pytest.mark.parametrize("outcome", ["refreshed", "remaining", "dispatch failed"])
def test_resolve_lists_then_replies_and_resolves_one_finding(finding_pr, gh: FakeGh, capsys, outcome) -> None:
    listing = threads_json(finding_pr)
    remaining = listing if outcome == "remaining" else threads_json()
    gh.respond("api graphql", listing, listing, REPLY_ACCEPTED, THREAD_RESOLVED, remaining)
    gh.respond("api -X", "", exit=int(outcome == "dispatch failed"), stderr="HTTP 403")

    expect_cli(capsys, 0, "resolve", stderr="", stdout="0b9b59e7  important  src/a.py:3  Off by one\n")
    assert not any("eligibility.yml/dispatches" in " ".join(c["argv"]) for c in gh.calls)

    code, out, err = run(capsys, "resolve", "0b9b59e7", "Fixed in abc1234: loop bound corrected.")
    assert out.splitlines()[0] == "resolved 0b9b59e7: Off by one"
    calls = [c["argv"] for c in gh.calls
             if c["argv"][:2] == ["api", "graphql"] and "viewer {" not in c["argv"][3]][2:]
    assert "addPullRequestReviewThreadReply" in calls[0][3] and "resolveReviewThread" in calls[1][3]
    assert "body=Fixed in abc1234: loop bound corrected." in calls[0] and "thread=THREAD_1" in calls[1]
    endpoint = f"repos/{REPO}/actions/workflows/eligibility.yml/dispatches"
    dispatches = [c["argv"] for c in gh.calls if endpoint in c["argv"]]
    if outcome == "remaining":
        assert (code, err, dispatches) == (0, "", [])
        assert len(out.splitlines()) == 1
    else:
        assert dispatches == [["api", "-X", "POST", endpoint, "-f", "ref=main", "-f", "inputs[pr]=7", "--include"]]
        if outcome == "refreshed":
            assert (code, err) == (0, "") and out.splitlines()[-1] == "refreshed Loop-zero Eligibility for PR #7"
        else:
            assert code == 1 and "HTTP 403" in err
            assert f"gh api -X POST {endpoint} -f ref=main -f 'inputs[pr]=7'" in err


@pytest.mark.parametrize("cut", ["reply", "resolution"])
def test_resolve_resumes_after_github_accepts_mutation(gh, finding_pr, capsys, monkeypatch, cut):
    node = finding_pr
    reply = "Fixed the loop bound."
    accepted = {"reply": 0, "resolution": 0}

    def github_call(*args, **kwargs):
        query = args[3]
        if "viewer {" in query:
            return {"data": {"viewer": {"login": LOGIN}}}
        if "reviewThreads(" in query:
            node["isResolved"] = bool(accepted["resolution"])
            node["comments"]["pageInfo"] = {
                "hasNextPage": bool(accepted["reply"]), "endCursor": "ROOT",
            }
            return threads_json(node)
        if "comments(first: 100" in query:
            if "after=ROOT" in args:
                return {"data": {"node": {"comments": {
                    "nodes": [{"body": reply, "viewerDidAuthor": False},
                              {"body": "Different text", "viewerDidAuthor": True}],
                    "pageInfo": {"hasNextPage": True, "endCursor": "PAGE"},
                }}}}
            assert "after=PAGE" in args
            return {"data": {"node": {"comments": {
                "nodes": [{"body": reply, "viewerDidAuthor": True}],
                "pageInfo": {"hasNextPage": False},
            }}}}
        assert "addPullRequestReviewThreadReply" in query
        accepted["reply"] += 1
        if cut == "reply" and accepted["reply"] == 1:
            raise KeyboardInterrupt
        return REPLY_ACCEPTED

    original_gh = github._gh
    original_json = github._gh_json

    def read_or_reply(*args, **kwargs):
        if args[:2] == ("api", "graphql"):
            return github_call(*args, **kwargs)
        return original_json(*args, **kwargs)

    def resolve_or_dispatch(*args, **kwargs):
        if args[:2] == ("api", "graphql") and "resolveReviewThread" in args[3]:
            accepted["resolution"] += 1
            if cut == "resolution" and accepted["resolution"] == 1:
                raise KeyboardInterrupt
            return json.dumps(THREAD_RESOLVED)
        return original_gh(*args, **kwargs)

    monkeypatch.setattr(github, "_gh_json", read_or_reply)
    monkeypatch.setattr(github, "_gh", resolve_or_dispatch)
    gh.respond("api -X", "")

    assert run(capsys, "resolve", "0b9b59e7", reply)[0] == 130
    out, _err = expect_cli(capsys, 0, "resolve", "0b9b59e7", reply, stderr="")
    assert accepted == {"reply": 1, "resolution": 1}
    assert "refreshed Loop-zero Eligibility for PR #7" in out
    assert sum("eligibility.yml/dispatches" in " ".join(c["argv"]) for c in gh.calls) == 1
    assert run(capsys, "resolve") == (0, "", "")


def test_resolve_takes_review_publishers_from_the_base(wt: Path, gh: FakeGh, capsys, head) -> None:
    (wt / "workflow.toml").write_text(WORKFLOW + '\nreview_publishers = ["outsider"]\n')
    planted, genuine = _finding_thread("0b9b59e7", head), _finding_thread("1c2d3e4f", head)
    planted["comments"]["nodes"][0]["author"] = {"__typename": "User", "login": "outsider"}
    gh.respond_pr_list([pr_json(headRefOid=head)])
    gh.respond("api graphql", threads_json(planted, genuine))
    assert run(capsys, "resolve") == (0, "1c2d3e4f  important  src/a.py:3  Off by one\n", "")


@pytest.mark.parametrize("argv", [("resolve",), ("resolve", "0b9b59e7", "Fixed.")])
def test_resolve_refuses_a_pr_targeting_another_base(wt: Path, gh: FakeGh, capsys, head, argv) -> None:
    gh.respond_pr_list([pr_json(headRefOid=head, baseRefName="release")])
    code, out, err = run(capsys, *argv)
    assert (code, out) == (1, "") and "PR #7 targets release, configured base is main" in err
    assert not any("reviewThreads" in " ".join(c["argv"]) for c in gh.calls)


def test_resolve_and_ready_ignore_findings_from_non_publishers(wt: Path, gh: FakeGh, capsys, head) -> None:
    planted = _finding_thread("0b9b59e7", head)
    planted["comments"]["nodes"][0]["author"] = {"__typename": "User", "login": "outsider"}
    gh.respond_pr_list([pr_json(headRefOid=head)])
    gh.respond("api graphql", threads_json(planted))
    assert run(capsys, "resolve") == (0, "", "")

    arm_approved_pr_with_check_result(gh, head, wt, "success", reviews=[rev(head, "primary")],
                                      readiness={"threads": (planted,)}, isDraft=False)
    code, out, _ = run(capsys, "ready")
    gh.respond_pr_view(pr_json(headRefOid=head))
    hosted = cli.eligibility.evaluate(REPO, 7, head, "main", (LOGIN,))
    assert (code, hosted.ready) == (0, True), out


@pytest.mark.parametrize("response", [
    {"data": {"resolveReviewThread": {"thread": {"isResolved": False}}}},
    {"data": {"resolveReviewThread": {"thread": {}}}},
    "invalid JSON",
])
def test_resolve_requires_confirmation_from_github(finding_pr, gh: FakeGh, capsys, response) -> None:
    listing = threads_json(finding_pr)
    gh.respond("api graphql", listing, REPLY_ACCEPTED, response, listing)

    _out, err = expect_cli(capsys, 1, "resolve", "0b9b59e7", "Fixed the loop bound.", stdout="")
    assert "reply posted to finding 0b9b59e7" in err and "PR #7" in err
    assert "Retry loopzero resolve with the same reply" in err
    assert not any("eligibility.yml/dispatches" in " ".join(call["argv"]) for call in gh.calls)
    assert sum("addPullRequestReviewThreadReply" in " ".join(call["argv"]) for call in gh.calls) == 1
    assert run(capsys, "resolve") == (0, "0b9b59e7  important  src/a.py:3  Off by one\n", "")


def test_resolve_reports_recovery_when_followup_read_fails(finding_pr, gh, capsys):
    listing = threads_json(finding_pr)
    gh.respond("api graphql", listing, REPLY_ACCEPTED, THREAD_RESOLVED,
               {"stdout": "", "stderr": "gh: Forbidden (HTTP 403)", "exit_code": 1})

    _, err = expect_cli(capsys, 1, "resolve", "0b9b59e7", "Fixed the loop bound.", stdout_contains=("resolved 0b9b59e7",))
    assert "Resolved finding 0b9b59e7 on PR #7" in err
    assert "could not refresh Loop-zero Eligibility" in err and "HTTP 403" in err
    assert "run `loopzero resolve`" in err and "if no findings remain" in err
    endpoint = f"repos/{REPO}/actions/workflows/eligibility.yml/dispatches"
    assert f"gh api -X POST {endpoint} -f ref=main -f 'inputs[pr]=7'" in err

    gh.respond("api graphql", threads_json())
    assert run(capsys, "resolve") == (0, "", "")
    assert not any(endpoint in call["argv"] for call in gh.calls)


@pytest.mark.parametrize("recovers", [True, False])
def test_resolve_retries_only_thread_resolution(finding_pr, gh: FakeGh, capsys, monkeypatch, recovers) -> None:
    failure = {"stdout": "", "stderr": "gh: Something went wrong while executing your query", "exit_code": 1}
    outcomes = [failure, THREAD_RESOLVED] if recovers else [failure] * 2
    gh.respond("api graphql", threads_json(finding_pr), REPLY_ACCEPTED, *outcomes)
    if recovers:
        gh.append("api graphql", threads_json())
        gh.respond("api -X", "")
    monkeypatch.setattr(github, "_retry_sleep", lambda _: None)

    code, out, err = run(capsys, "resolve", "0b9b59e7", "Fixed.")

    if recovers:
        assert (code, err) == (0, "")
        assert out.splitlines() == [
            "resolved 0b9b59e7: Off by one", "refreshed Loop-zero Eligibility for PR #7",
        ]
    else:
        assert code == 1
        assert "reply posted to finding 0b9b59e7" in err
        assert "Retry loopzero resolve with the same reply" in err
    queries = [c["argv"][3] for c in gh.calls
               if c["argv"][:2] == ["api", "graphql"] and "viewer {" not in c["argv"][3]]
    assert len(queries) == 2 + len(outcomes) + recovers
    assert "addPullRequestReviewThreadReply" in queries[1]
    assert all("resolveReviewThread" in query for query in queries[2:2 + len(outcomes)])


@pytest.mark.parametrize("finding_id,reply,reason", [
    ("0b9b59e7", "Fixed in abc1234.", "reply was not posted; not resolving"),
    ("deadbeef", "Fixed.", "no open blocking finding deadbeef"),
], ids=["reply-rejected", "unknown-finding"])
def test_resolve_leaves_thread_open_without_reply(finding_pr, gh, capsys, finding_id, reply, reason):
    rejected = {"data": {"addPullRequestReviewThreadReply": None}, "errors": [{"message": "no"}]}
    snapshots = (rejected,) if finding_id == "0b9b59e7" else ()
    gh.respond("api graphql", threads_json(finding_pr), *snapshots)

    code, _, err = run(capsys, "resolve", finding_id, reply)

    assert code == 1 and reason in err
    assert not any("resolveReviewThread" in a for c in gh.calls for a in c["argv"])


@pytest.mark.parametrize("reply", [(), ("  ",)])
def test_resolve_requires_reply_before_context(monkeypatch, capsys, reply):
    monkeypatch.setattr(cli, "_context", lambda *_: pytest.fail("loaded context for invalid resolve"))
    with pytest.raises(SystemExit) as stop:
        cli.main(["resolve", "0b9b59e7", *reply])

    assert stop.value.code == 2
    assert 'resolve [<finding-id> "<reply>"]' in capsys.readouterr().err


def test_resolve_help_groups_arguments(capsys):
    with pytest.raises(SystemExit) as stop:
        cli.main(["resolve", "--help"])

    assert stop.value.code == 0
    assert 'resolve [<finding-id> "<reply>"]' in capsys.readouterr().out


def test_unexpected_and_interrupt_exit_codes(wt: Path, capsys, monkeypatch) -> None:
    def boom(*_):
        raise KeyError("headRefOid")
    monkeypatch.setattr(cli.github, "pr_for_branch", boom)
    expect_cli(capsys, 1, "status", stderr="loopzero status: unexpected response: 'headRefOid'\n")

    def interrupt(*_):
        raise KeyboardInterrupt
    monkeypatch.setattr(cli.github, "pr_for_branch", interrupt)
    assert run(capsys, "status")[0] == 130


@pytest.mark.parametrize("detached", [False, True])
def test_start_refuses_inside_task_worktree(wt: Path, repo: Path, monkeypatch, capsys, detached: bool) -> None:
    if detached:
        git(wt, "switch", "-q", "--detach")
    monkeypatch.chdir(wt / ".loopzero")
    _, err = expect_cli(capsys, 1, "start", "nested", stdout="", stderr_contains=("already inside task worktree",))
    assert "run start from the main checkout" in err
    assert not (repo / ".worktrees" / "nested").exists()
    assert not (wt / ".worktrees" / "nested").exists()
    assert "lz/nested" not in git(repo, "branch", "--list", "lz/nested")


def test_reviews_are_paginated(wt: Path, gh: FakeGh, capsys, head) -> None:
    arm_approved_pr_with_check_result(gh, head, wt, "success", reviews=[rev("f" * 40, "primary", login="x")] * 100)
    gh.respond(reviews_key(2), [rev(head, "primary")])
    gh.respond("pr ready", "")
    code, out, _ = run(capsys, "ready")
    assert code == 0 and out.endswith(f"ready: {URL}\n")


@pytest.mark.parametrize("state,verdict,expected", [
    ("APPROVED", "approve", True), ("COMMENTED", "request_changes", True),
    ("DISMISSED", "approve", False), ("PENDING", "approve", False),
    ("COMMENTED", "", False),
])
def test_hosted_eligibility_uses_publisher_not_token(wt, gh, state, verdict, expected, head):
    from loopzero import eligibility
    gh.respond_pr_view( pr_json(headRefOid=head, mergeStateStatus="BEHIND"))
    gh.respond("api graphql viewer", {"data": {"viewer": {"login": "ci-service"}}})
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
    gh.respond_pr_view( pr_json(headRefOid="b" * 40))
    assert not eligibility.evaluate(REPO, 7, head, "main", (LOGIN,)).ready


@pytest.mark.parametrize("publisher", ["stranger", "lz-bot"])
def test_ready_pins_review_publishers_to_base(wt, gh, capsys, publisher, head):
    (wt / "workflow.toml").write_text(WORKFLOW + f'\nreview_publishers = ["{publisher}"]\n')
    arm_approved_pr_with_check_result(
        gh, head, wt, "success", review_login=publisher, isDraft=False
    )
    assert run(capsys, "ready")[0] == (0 if publisher == LOGIN else 1)


def test_mergify_confirmed_wait_reads_pr_once_per_steady_poll(wt, gh, monkeypatch):
    head = commit_mergify_configuration(wt)
    config = cli.config_mod.load_trusted(wt)
    pr = github.PR(7, URL, head, "main", False, "OPEN", "MERGEABLE")
    detail = ["api", f"repos/{REPO}/pulls/7"]
    gh.respond(" ".join(detail), merge_json(state="OPEN", head=head))
    clock = poll_clock(monkeypatch, jitter=1.0)
    reads_at_membership = []
    configurations = []

    def membership(*_):
        reads_at_membership.append(len(gh.calls))
        if clock.now == 60:
            gh.respond(" ".join(detail), merge_json(state="MERGED", head=head, sha="e" * 40))
        return mergify.Membership()

    def configured(*args):
        configurations.append(args)
        return True

    monkeypatch.setattr(mergify, "configured", configured)
    monkeypatch.setattr(mergify, "membership", membership)

    assert cli._wait_for_mergify(config, pr, 60, confirmed=True) == "e" * 40
    assert clock.sleeps == [30, 30]
    assert configurations == [(REPO, "main", "main")] * 3
    assert reads_at_membership == [1, 2, 3]
    assert [c["argv"][:2] for c in gh.calls] == [detail] * 4


@pytest.mark.parametrize("outcome,jitter", [
    ("merged", 1.0),
    ("worktree_removed", 0.9),
    ("worktree_removed", 1.0),
    ("ejected", 1.0),
    ("head_changed", 1.0),
    ("timeout", 0.9),
    ("timeout", 1.0),
    ("api_error", 1.0),
    ("unsafe_config", 1.0),
    ("config_error", 1.0),
    ("merged_head_changed", 1.0),
    ("ejection_head_changed", 1.0),
    ("already_merged", 1.0),
    ("already_merged_changed", 1.0),
    ("unsafe_pending", 1.0),
    ("requested_head_changed", 1.0),
])
def test_mergify_wait_outcomes_preserve_unmerged_work(wt, repo, gh, capsys, monkeypatch, outcome, jitter):
    head = commit_mergify_configuration(wt, "mergify delivery")
    arm_approved_pr_with_check_result(
        gh, head, wt, "success", isDraft=False,
        state="MERGED" if outcome.startswith("already_") else "OPEN",
    )
    gh.respond("api -X", "")
    landed = outcome in {
        "merged", "worktree_removed", "merged_head_changed", "already_merged", "already_merged_changed",
    }
    snapshots = []
    if not outcome.startswith("already_"):
        second_head = "f" * 40 if outcome == "head_changed" else head
        snapshots.append(rest_pr_json(pr_json(headRefOid=second_head, isDraft=False)))
    changed = outcome in {"merged_head_changed", "already_merged_changed", "requested_head_changed"}
    snapshots.append(merge_json(state="MERGED" if landed else "OPEN", sha="e" * 40 if landed else None,
                                head="f" * 40 if changed else head))
    if outcome == "ejection_head_changed":
        snapshots.extend((merge_json(head=head), merge_json(state="MERGED", head="f" * 40, sha="e" * 40)))
    gh.append(f"api repos/{REPO}/pulls/7", *snapshots)
    configurations = []

    def configured(*_):
        configurations.append(True)
        if len(configurations) > 1:
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
        return mergify.Membership()
    monkeypatch.setattr(mergify, "membership", membership)
    clock = poll_clock(monkeypatch, jitter=jitter)
    if outcome == "worktree_removed":
        poll_until = cli._poll_until

        def remove_worktree_then_poll(*args):
            git(repo, "worktree", "remove", "--force", str(wt))
            return poll_until(*args)

        monkeypatch.setattr(cli, "_poll_until", remove_worktree_then_poll)
    try:
        code, out, err = run(capsys, "merge", "--wait=250" if outcome == "timeout" else "--wait=0")
    finally:
        if outcome == "worktree_removed":
            monkeypatch.chdir(repo)
    if outcome == "timeout":
        assert f"following Mergify queue main; head {head[:12]}; waiting up to 250s" in out
        assert clock.sleeps == ([30, 60, 120, 40] if jitter == 1 else [27, 54, 108, 61])
        assert sum(c["argv"][:2] == PR_LIST_READ for c in gh.calls) == 1
        assert sum(c["argv"][:2] == ["api", "graphql"] for c in gh.calls) == 2
    assert requests == ([(REPO, 7, head, "main")] if outcome == "requested_head_changed" else [])
    if outcome in {"merged", "already_merged", "worktree_removed"}:
        assert code == 0 and "e" * 40 in out and not wt.exists(), err
        if outcome == "worktree_removed":
            assert "warning: merged but worktree cleanup failed" in err
            assert f"working directory {wt} does not exist" in err
    else:
        assert wt.exists() and code == (3 if outcome == "timeout" else 1)
        if outcome == "ejected":
            assert err.index('"Left the queue" comment') < err.index("post `@mergifyio queue`")
        message = {"ejected": "post `@mergifyio queue` as the whole PR comment (same login), then rerun `loopzero merge --wait`", "head_changed": "head changed",
                   "timeout": "timed out", "api_error": "HTTP 503",
                   "unsafe_config": "unsafe or unavailable", "unsafe_pending": "unsafe or unavailable", "config_error": "HTTP 503",
                   "requested_head_changed": "head changed", "merged_head_changed": "head changed", "already_merged_changed": "head changed", "ejection_head_changed": "head changed"}[outcome]
        assert message in out + err


@pytest.mark.parametrize("confirmed,kind", [
    (False, "current"), (False, "stale"), (False, "missing"),
    (False, "stacked"), (True, "stacked"), (True, "isolated"), (True, "unknown"), (True, "conflict"),
    (False, "head"), (False, "requeue"), (False, "author"), (False, "landed"),
])
def test_mergify_reports_current_queue_attempt(wt, gh, capsys, monkeypatch, confirmed, kind):
    head = commit_mergify_configuration(wt)
    arm_approved_ready_pr(gh, head, wt, isDraft=False)
    admission = kind in MERGIFY_QUEUE_FIXTURE["admission"]
    comments = [{"id": 42, "user": {"login": LOGIN}, "created_at": "2026-11-25T19:00:00Z",
                 "body": MERGIFY_QUEUE_FIXTURE["request"].replace("@head@", head)}]
    if kind != "missing":
        template = MERGIFY_QUEUE_FIXTURE.get("admission_report" if admission else f"{kind}_report", MERGIFY_QUEUE_FIXTURE["report"])
        body = template.replace("@url@", URL).replace("@sha@", "f" * 7 if kind == "head" else head[:7])
        comments.append({"id": 43, "user": {"login": "someone-else" if kind == "author" else "mergify[bot]"},
                         "updated_at": MERGIFY_QUEUE_FIXTURE["admission"].get(kind),
                         "body": body + MERGIFY_QUEUE_FIXTURE["attribution"].get(kind, "")})
    if kind == "requeue":
        comments.append({"id": 44, "user": {"login": LOGIN}, "body": "@mergifyio queue"})
    gh.respond(f"api repos/{REPO}/issues/7/comments", comments)
    monkeypatch.setattr(mergify, "configured", lambda *_: True)
    membership = iter([mergify.Membership()] if confirmed else [])
    monkeypatch.setattr(mergify, "membership", lambda *_: next(membership, None))
    monkeypatch.setattr(mergify, "mark_confirmed", lambda *_: None)
    monkeypatch.setattr(mergify, "request", lambda *_: pytest.fail("already requested"))
    if kind == "landed":
        opened = rest_pr_json(pr_json(headRefOid=head, isDraft=False))
        gh.append(f"api repos/{REPO}/pulls/7", *([opened] * 3), merge_json(state="MERGED", head=head, sha="e" * 40))
        gh.respond("api -X", "")
    if admission:
        status, summary, error = run(capsys, "status")
        assert (status, error) == (0, "") and "waiting for admission" in summary
        assert (f"{URL}#issuecomment-43" in summary) is (kind == "current")
    code, out, err = run(capsys, "merge", "--wait=0")
    if kind == "landed":
        assert (code, err) == (0, "") and not wt.exists() and out.splitlines()[-1] == "e" * 40
    elif kind in MERGIFY_QUEUE_FIXTURE["expected"]:
        assert code == 1 and wt.exists() and f"{URL}#issuecomment-43" in err and ("'checks'" in err) == (kind != "conflict")
        assert all(part in err for part in [*MERGIFY_QUEUE_FIXTURE["requeue_guidance"], *MERGIFY_QUEUE_FIXTURE["expected"][kind]])
    else:
        assert code == 3 and wt.exists() and "waiting for admission" in out
        assert (f"{URL}#issuecomment-43" in out) is (kind == "current")


@pytest.mark.parametrize("result", ["blocking", "neutral", "skipped", None, "long", "many"])
def test_mergify_timeout_reports_evaluated_candidate_conditions(wt, gh, capsys, monkeypatch, result):
    head = commit_mergify_configuration(wt)
    arm_approved_ready_pr(gh, head, wt, isDraft=False)
    monkeypatch.setattr(mergify, "configured", lambda *_: True)
    monkeypatch.setattr(mergify, "markers", lambda *_: (True, True))
    payload = MERGIFY_QUEUE_FIXTURE["candidate_responses"]["blocking" if result in {"long", "many"} else result or "missing"]
    if extra := {"long": 1, "many": 30}.get(result, 0):
        conditions = payload["mergeability_check"]["conditions_evaluation"]
        monkeypatch.setitem(conditions, "subconditions", conditions["subconditions"] + [{"match": False, "label": "界" * 3000}] * extra)
    monkeypatch.setattr(mergify, "api_get", lambda *_: payload)
    out, _ = expect_cli(capsys, 3, "merge", "--wait=0", stderr="")
    assert f"https://github.com/{REPO}/pull/42" in out
    assert (MERGIFY_QUEUE_FIXTURE["candidate_condition"] in out) == (result in {"blocking", "long", "many"})
    assert "failed" not in out and "check-success = checks" not in out
    assert len(out.encode()) < 2048 and ("27 more" in out) == (extra == 30)


@pytest.mark.parametrize("queue_state", ["unsafe", "error", "safe", "becomes_unsafe"])
@pytest.mark.parametrize("check_state", ["missing", "skipped"])
def test_mergify_ready_draft_requires_safe_queue_before_ci_activation(
    wt, gh, capsys, monkeypatch, queue_state, check_state
):
    commit_mergify_configuration(wt, "mergify delivery")
    head = head_of(wt)
    arm_approved_ready_pr(gh, head, wt)
    arm_check_runs(gh, head, [] if check_state == "missing" else [
                   {"name": "checks", "status": "completed", "conclusion": "skipped"}],
               [{"name": "checks", "status": "completed", "conclusion": "success"}])
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
    commit_mergify_configuration(wt)
    arm_pr(gh, "a" * 40)
    monkeypatch.setattr(mergify, "configured", lambda *_: pytest.fail("cancel must remain available"))
    removals = []
    monkeypatch.setattr(mergify, "dequeue", lambda *args: removals.append(args))
    code, _out, err = run(capsys, "merge", "--cancel")
    assert (code, err) == (0, "") and removals == [(REPO, 7)] and wt.exists()


@pytest.mark.parametrize("lineage", ["two-head-trailers", "earlier-child", "delta", "mixed-case-trailers"])
def test_review_discloses_fallback_for_all_contributing_families(wt, gh, fake_bin, capsys, lineage):
    codex = "Co-Authored-By: Codex <codex@openai.com>"
    claude = "Co-Authored-By: Claude <noreply@anthropic.com>"
    history = []
    if lineage == "mixed-case-trailers":
        git(wt, "commit", "-q", "--allow-empty", "-m",
            f"mixed-family\n\n{claude}\nCo-authored-by: Codex gpt-6.2-sol <noreply@openai.com>")
    else:
        git(wt, "commit", "--amend", "-qm", f"child patch\n\n{codex}")
        if lineage == "two-head-trailers":
            git(wt, "commit", "--amend", "-qm", f"patch\n\n{codex}\n{claude}")
        else:
            if lineage == "delta":
                history = [rev(head_of(wt), "primary", state="CHANGES_REQUESTED", verdict="request_changes")]
            git(wt, "commit", "--allow-empty", "-qm", f"repair\n\n{claude}")
        _fake_codex(fake_bin, json.dumps(APPROVE))
    head = prepare_review(wt, gh, fake_bin, history=history)

    code, out, err = run(capsys, "review")

    reason = "every configured family contributed (claude, codex)"
    assert code == 0 and "review by claude" in out, err
    assert f"same-family fallback: {reason}" in err
    assert (fake_bin / "claude.stdin").exists()
    assert not (fake_bin / "codex.stdin").exists()
    assert f"Same-family fallback: {reason}. Cross-family review is preferred." in posted_review(gh)["body"]
    kind = "delta" if lineage == "delta" else "primary"
    assert cli._load_review(wt, head, kind).fallback == reason


def test_repost_rejects_saved_contributor_review(
    wt: Path, gh: FakeGh, fake_bin: Path, capsys
) -> None:
    from loopzero import runners

    git(wt, "commit", "--amend", "-q", "-m",
        "patch\n\nCo-Authored-By: Claude <noreply@anthropic.com>")
    head = prepare_review(wt, gh, fake_bin)
    result = runners.review_with("claude", cwd=wt, head=head, kind="primary",
                                 diff="", task_text="")
    cli._save_review(wt, result)
    (fake_bin / "claude.stdin").unlink()

    _, err = expect_cli(capsys, 4, "review", "--repost", stdout="")
    assert "saved reviewer family claude" in err
    assert not (fake_bin / "claude.stdin").exists()
    assert not any(call["argv"][1] == post_key()[4:] for call in gh.calls)


@pytest.mark.parametrize("author,expected", [("Claude", "codex"), ("Jane", "claude")])
def test_review_uses_lineage_author_after_unattributed_repair(
    wt: Path, gh: FakeGh, fake_bin: Path, capsys, author: str, expected: str
) -> None:
    git(wt, "commit", "--amend", "-q", "-m",
        f"child patch\n\nCo-Authored-By: {author} <bot@example.com>")
    git(wt, "commit", "--allow-empty", "-q", "-m", "unattributed repair")
    prepare_review(wt, gh, fake_bin)
    _fake_codex(fake_bin, json.dumps(APPROVE))

    code, out, err = run(capsys, "review")

    assert code == 0 and out.splitlines()[-1].startswith(f"primary review by {expected}"), err
    other = "claude" if expected == "codex" else "codex"
    assert not (fake_bin / f"{other}.stdin").exists()


@pytest.mark.parametrize("command", ["check", "ready"])
def test_receipt_print_failure_preserves_verdict(wt, gh, capsys, head, monkeypatch, command):
    import builtins

    original = builtins.print

    def broken(*args, **kwargs):
        if args and str(args[0]).startswith("receipt "):
            raise OSError("capture unavailable")
        return original(*args, **kwargs)

    if command == "ready":
        arm_approved_ready_pr(gh, head, wt)
        gh.respond("pr ready", "")
    monkeypatch.setattr(builtins, "print", broken)
    assert run(capsys, command)[0] == 0


@pytest.mark.parametrize("exit_code,ok", [(False, None), (True, None), (0, True), (1, False)])
def test_load_report_requires_integer_exit_code(wt, head, capsys, exit_code, ok):
    path = wt / ".loopzero/checks.json"
    path.parent.mkdir(exist_ok=True)
    path.write_text(json.dumps({
        "head": head, "dirty": False,
        "results": [{"command": "true", "exit_code": exit_code, "duration_s": 0.1, "tail": ""}],
    }))

    report = cli._load_report(wt)
    if ok is None:
        assert report is None
        warning = capsys.readouterr().err
        assert "warning: ignoring unreadable" in warning
        assert str(path) in warning and "exit_code" in warning
    else:
        assert report is not None and report.ok is ok
        assert report.head == head


def test_passive_receipt_load_is_silent(wt, gh, capsys, head):
    arm_approved_ready_pr(gh, head, wt)
    cli._load_report(wt)
    cli._validation_section(wt, head, cli.config_mod.load(wt / "workflow.toml"))
    assert "receipt " not in capsys.readouterr().out


def test_dirty_check_does_not_claim_committed_receipt(wt, capsys):
    (wt / "README.md").write_text("uncommitted content")
    code, out, _ = run(capsys, "check")
    assert code == 0 and out.endswith("PASS\n")
    assert "receipt produced:" not in out


@pytest.mark.parametrize("case", ["dirty", "unpushed", "after-head-read"])
def test_cleanup_preserves_local_work(wt, case, capsys, monkeypatch):
    pr = SimpleNamespace(head_sha=cli.worktree.head(wt))
    if case == "dirty":
        (wt / "untracked").write_text("keep me")
    else:
        git(wt, "commit", "--allow-empty", "-m", "local after merge")
    if case == "after-head-read":
        monkeypatch.setattr(cli.worktree, "head", lambda _wt: pr.head_sha)
    cli._cleanup(wt, pr)
    assert wt.exists()
    diagnostic = {"dirty": "worktree is dirty", "unpushed": "local HEAD differs", "after-head-read": "but expected"}
    assert diagnostic[case] in capsys.readouterr().err


def _closed_unmerged_wait(monkeypatch, reads, steps=0):
    """Run _wait_for_merge against scripted merged_sha reads; return its result."""
    argv = ("gh", "api", "repos/o/r/pulls/7")
    seen = iter(reads)

    def merged_sha(*_a, **_k):
        read = next(seen)
        if read == "closed":
            raise github.ClosedUnmerged(argv, "PR closed without a verified merge or invalid merge evidence")
        return read

    monkeypatch.setattr(cli, "_sleep", lambda _: None)
    monkeypatch.setattr(cli.github, "merged_sha", merged_sha)
    monkeypatch.setattr(cli.github, "merge_pending", lambda *_: True)
    ticks = iter(range(steps + 1))
    monkeypatch.setattr(cli, "_poll_until", lambda *_: (None for _ in ticks))
    config = SimpleNamespace(repo="o/r", base_branch="main")
    pr = github.PR(7, URL, "a" * 40, "main", False, "OPEN", "MERGEABLE")
    return cli._wait_for_merge(config, pr, 60)


@pytest.mark.parametrize("final", [
    pytest.param("c" * 40, id="merged"),
    pytest.param("closed", id="still-closed"),
])
def test_wait_rechecks_closed_unmerged_pr(monkeypatch, final) -> None:
    reads = ["closed", final]
    if final == "closed":
        with pytest.raises(github.GhError, match="closed without a verified merge"):
            _closed_unmerged_wait(monkeypatch, reads, steps=1)
    else:
        assert _closed_unmerged_wait(monkeypatch, reads, steps=1) == final


@pytest.mark.parametrize("redirected", [False, True])
def test_checks_report_survives_failed_publication(tmp_path, monkeypatch, redirected):
    path = tmp_path / cli.CHECKS_FILE
    monkeypatch.setattr(cli.worktree, "head", lambda _: next(heads))
    heads = iter(("a" * 40, "b" * 40))
    cli._save_report(tmp_path, CheckReport("a" * 40, False, ()))

    def interrupt(staged, dest, *, src_dir_fd, dst_dir_fd):
        assert src_dir_fd == dst_dir_fd and dest == path.name
        assert json.loads(path.with_name(staged).read_text())["head"] == "b" * 40
        assert json.loads(path.read_text())["head"] == "a" * 40
        raise OSError("publication interrupted")

    if redirected:
        external = path.parent.rename(tmp_path / "external")
        path.parent.symlink_to(external, target_is_directory=True)
    monkeypatch.setattr(cli.os, "replace", interrupt)
    with pytest.raises(OSError, match="Not a directory" if redirected else "publication interrupted"):
        cli._save_report(tmp_path, CheckReport("b" * 40, False, ()))
    assert json.loads(path.read_text())["head"] == "a" * 40
    assert list(path.parent.iterdir()) == [path]
    if redirected:
        external.chmod(0o755)
        review = ReviewResult("claude", "a" * 40, "primary", "approve", (), "")
        receipt = cli._review_file(tmp_path, review.head, review.kind)
        receipt.write_text("user file")
        with pytest.raises(OSError, match="Not a directory"):
            cli._save_review(tmp_path, review)
        assert receipt.read_text() == "user file" and external.stat().st_mode & 0o777 == 0o755


@pytest.mark.parametrize("moved", [False, True])
def test_already_merged_finishes_partial_cleanup(wt, repo, gh, capsys, monkeypatch, head, moved):
    cli._review_file(wt, head, "primary").write_text("{broken")
    arm_pr(gh, head, isDraft=False, state="MERGED")
    gh.append(f"api repos/{REPO}/pulls/7", merge_json(state="MERGED", head=head, sha="c" * 40))
    gh.fail(f"api repos/{REPO}/git/ref/heads/lz/t1", "gh: Not Found (HTTP 404)")
    original = cli.worktree._git
    advanced = git(wt, "commit-tree", f"{head}^{{tree}}", "-p", head, "-m", "newer").strip()

    def cleanup_git(cwd, *args, **kwargs):
        if args[1:3] == ("update-ref", "--no-deref"):
            update = ("update-ref", "refs/heads/lz/t1", advanced) if moved else ("branch", "-D", "lz/t1")
            git(repo, *update)
        return original(cwd, *args, **kwargs)

    monkeypatch.setattr(cli.worktree, "_git", cleanup_git)
    code, out, err = run(capsys, "merge")
    assert code == 0 and "was already merged" in out and not wt.exists()
    assert ("but expected" in err) if moved else err == ""
    assert git(repo, "branch", "--list", "lz/t1", "--format=%(objectname)").strip() == (advanced if moved else "")
    assert not any(call["argv"][:2] == ["api", "-X"] for call in gh.calls)


@pytest.mark.parametrize("extra", [0, 1])
@pytest.mark.parametrize("create", [True, False])
def test_generated_pr_body_limit(wt, gh, capsys, monkeypatch, create, extra):
    monkeypatch.setattr(cli, "_validation_section", lambda *_: "Recorded validation")
    suffix = ("\n\n## Validation\n<!-- loopzero:checks:start -->\n"
              "Recorded validation\n<!-- loopzero:checks:end -->\n")
    task = wt / ".loopzero/task.md"
    prefix = task.read_text().split("## Validation")[0] if create else ""
    prose = prefix + "x" * (65536 + extra - len(prefix) - len(suffix))
    head = head_of(wt)
    if create:
        task.write_text(prose)
        gh.respond(branch_history_key(), [])
        gh.respond_pr_list([])
        gh.respond_pr_create(pr_json(headRefOid=head))
        code, _, err = run(capsys, "pr")
        assert code == int(bool(extra))
        if extra:
            assert not git(wt, "ls-remote", "origin", "refs/heads/lz/t1").strip()
    else:
        git(wt, "push", "-q", "-u", "origin", "lz/t1")
        gh.respond_pr_list([pr_json(headRefOid=head, body=prose)])
        gh.respond(f"api repos/{REPO}/pulls/7 --method PATCH", {})
        code, out, err = run(capsys, "check")
        assert code == 0 and out.endswith("PASS\n")

    writes = [call for call in gh.calls if "--input" in call]
    if extra:
        assert "65537" in err and "65536" in err
        assert not writes
    else:
        assert err == ""
        assert len(writes) == 1
        assert json.loads(writes[0]["--input"])["body"] == prose + suffix


@pytest.mark.parametrize("refresh", ["resync", "automatic"])
def test_pr_marks_legacy_same_head_check_unverified(wt, gh, capsys, refresh):
    head = head_of(wt)
    config = cli._load_config(SimpleNamespace(command="check", config=None), wt, pin_checks=True)
    live = ("# PR\n\n## Validation\nManual validation evidence.\n\n"
            "<!-- loopzero:checks:start -->\n"
            f"Recorded for `{head[:12]}`\n- `echo ok`: exit 0\n"
            "<!-- loopzero:checks:end -->\n")
    arm_pr_body_patch(gh, head, body=live)
    stages = tuple(CheckResult(command, 0, 0.0, "") for command in config.checks)
    cli._save_report(wt, CheckReport(head, False, stages))
    git(wt, "push", "-q", "-u", "origin", "lz/t1")

    if refresh == "resync":
        assert run(capsys, "pr")[0] == 0
    else:
        cli._refresh_pr_checks(wt, config)
    draft = json.loads(gh.calls[-1]["--input"])["body"]
    assert "unverified" in draft and "run `loopzero check`" in draft
    assert "Manual validation evidence." in draft

    gh.respond_pr_list([pr_json(headRefOid=head, body=draft)])
    assert run(capsys, "check")[0] == 0
    draft = json.loads(gh.calls[-1]["--input"])["body"]
    assert "unverified" not in draft
    assert f"Recorded for `{head[:12]}`\n- `echo ok`: exit 0" in draft
    assert "Manual validation evidence." in draft
