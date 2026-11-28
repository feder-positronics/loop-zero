"""cli.py end to end: real git with a bare origin, fake `gh`, `claude`, `codex` and `bwrap`."""

from __future__ import annotations

import base64
import errno
import itertools
import json
import os
import re
import shutil
import signal
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from contextlib import suppress
from functools import partial
from dataclasses import asdict, replace
from email.utils import formatdate
from pathlib import Path
from select import select
from threading import Event, current_thread, main_thread
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from loopzero import cli, config, github, hosted, mergify
from loopzero._proc import Completed
from loopzero.types import CheckReport, CheckResult, Finding, ReviewResult
from tests.conftest import commit_file, git, init_git_repo, review_summary
from tests.test_github import (
    FakeGh,
    branch_head_key,
    branch_history_key,
    check_run,
    closed_history_row,
    merge_json,
    pr_json,
    pr_list_key,
    producing_run,
    replacement_run,
    rest_pr_json,
    thread as review_thread,
    threads_json,
)
from tests.test_runners import (
    _argv,
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
def commit_pr_read(head: str) -> list[str]:
    return ["api", f"repos/{REPO}/commits/{head}/pulls"]


BRANCH_READ = branch_head_key().split()
PR_LIST_READ = pr_list_key().split()
LOOKUP_CALLS = [BRANCH_READ, PR_LIST_READ, ["api", f"repos/{REPO}/pulls/7"]]
HISTORY_CALLS = [BRANCH_READ, branch_history_key().split()]
LOGIN = "lz-bot"
TASK_BRANCHES = ("lz/t1", "t3/archive-review-receipts", "od183-fable-5-2", "codex/task")

WORKFLOW_BASE = f"""
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
WORKFLOW = WORKFLOW_BASE + f'review_publishers = ["{LOGIN}"]\n'


@pytest.fixture(scope="session")
def git_seed(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Prepare the clean and task checkouts and their bare origin."""
    seed = tmp_path_factory.mktemp("git-seed")
    checkout = init_git_repo(seed / "repo")
    commit_file(checkout, "workflow.toml", WORKFLOW, "config")
    origin = seed / "origin.git"
    git(checkout, "init", "-q", "--bare", "-b", "main", str(origin))
    git(origin, "config", "receive.autogc", "false")
    git(checkout, "remote", "add", "origin", str(origin))
    git(checkout, "push", "-q", "origin", "main")
    git(checkout, "remote", "set-head", "origin", "main")
    prepared = seed / "prepared"
    shutil.copytree(checkout, prepared)
    path = cli.worktree.start(prepared, "t1", "main")
    task = cli.worktree.task_file(path)
    for field, value in (
        ("Goal", "The feature prints its greeting."),
        ("Problem", "The feature is missing."),
        ("Context", "The feature module is part of the sample application."),
    ):
        task.write_text(re.sub(rf"(?m)^(- \*\*{field}:\*\* )<.*>$", rf"\g<1>{value}", task.read_text()))
    shutil.move(task.parent, seed / ".loopzero")
    commit_file(path, "feature.py", "print('hi')\n", "add feature")
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
def gh(fake_bin: Path, tmp_path: Path, monkeypatch) -> FakeGh:
    fake = FakeGh(fake_bin, tmp_path)
    fake.replay_in_process(monkeypatch)
    fake.respond(branch_history_key(), [])
    fake.respond("api graphql viewer", {"data": {"viewer": {"login": LOGIN}}})
    fake.respond(f"api repos/{REPO}/pulls/7/files",
                 [{"filename": "feature.py", "patch": "@@ -0,0 +1 @@\n+print('hi')"}])
    return fake


@pytest.mark.parametrize("link_error", [errno.EOPNOTSUPP, errno.EPERM, errno.EEXIST])
def test_start_reports_handoff_publication_failure(repo, monkeypatch, capsys, link_error):
    monkeypatch.chdir(repo)
    task = repo / ".worktrees" / "t1" / ".loopzero" / "task.md"

    def fail_link(*_args, **_kwargs):
        if link_error == errno.EEXIST:
            task.write_text("existing handoff")
        raise OSError(link_error, "link failed")

    monkeypatch.setattr(cli.worktree.os, "link", fail_link)
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
def wt(repo: Path, git_seed: Path, monkeypatch: pytest.MonkeyPatch, request) -> Path:
    path = cli.worktree.worktree_dir(repo, getattr(request, "param", "t1"))
    git(repo, "worktree", "add", "-q", str(path), "lz/t1")
    shutil.copytree(git_seed / ".loopzero", cli.worktree.task_file(path).parent)
    monkeypatch.chdir(path)
    return path


def head_of(path: Path) -> str:
    return git(path, "rev-parse", "HEAD").strip()


@pytest.fixture
def isolated_git(wt, monkeypatch, fake_tool):
    """Use real isolated Git fetches against the test worktree."""
    fake_tool("prlimit", "exit 127\n")
    popen = subprocess.Popen

    def fetch_from_worktree(argv, **kwargs):
        if "fetch" in argv:
            argv = [wt.as_uri() if arg == f"https://github.com/{REPO}.git" else arg for arg in argv]
        return popen(argv, **kwargs)

    monkeypatch.setenv("GH_TOKEN", "test-token")
    monkeypatch.setattr(subprocess, "Popen", fetch_from_worktree)


@pytest.fixture
def head(wt: Path) -> str:
    return head_of(wt)


def marker(head: str, kind: str, source: str = "model") -> str:
    return cli.review_marker(head, kind, source)


@pytest.mark.parametrize("source", ["model", "pair", "second-leading"])
def test_review_marker_counts_only_in_the_leading_block(gh: FakeGh, source: str) -> None:
    head = "a" * 40
    if source == "second-leading":
        review = rev(head, "primary")
        review["body"] = f"{marker(head, 'primary')}\n{marker('b' * 40, 'primary')}\nreview: **approve**"
        eligible, markers = None, []
    else:
        quote = marker("b" * 40, "primary", source)
        result = ReviewResult("claude", head, "primary", "approve",
                              (Finding("suggestion", None, None, "Fixture", f"The fixture quotes {quote}."),),
                              "raw", provenance={"session_ids": ["s"]})
        gh.respond(f"api repos/{REPO}/pulls/7/files", [])
        (payload,) = github.review_payloads(REPO, 7, head, result, [(marker(head, "primary"), "current", False)])
        review = rev(head, "primary", state="COMMENTED") | payload
        assert quote in review["body"]
        eligible, markers = review, [cli.Marker(head, "primary", "COMMENTED")]

    assert cli.eligibility.eligible_review([review], head, (LOGIN,)) is eligible
    assert cli.eligibility.readiness_review(
        [review], head, (LOGIN,), repo=REPO, number=7, base_branch="main") is eligible
    assert cli._pr_markers([review], head, (LOGIN,)) == markers


@pytest.mark.parametrize("changed, publisher", [
    ({"headRefOid": "b" * 40}, LOGIN),
    ({"state": "CLOSED"}, LOGIN),
    ({"baseRefName": "release"}, LOGIN),
    ({}, "untrusted-publisher"),
    ({}, LOGIN),
])
def test_publication_recovery_rechecks_pr_and_publisher(wt, gh, head, changed, publisher):
    cfg = replace(config.load(wt / "workflow.toml"), review_publishers=(LOGIN,))
    result = ReviewResult("claude", head, "primary", "approve", (), "raw", provenance={"session_ids": ["s"]})
    pr = github.PR(7, URL, head, "main", False, "OPEN", "MERGEABLE")
    gh.respond_pr_view(pr_json(**({"headRefOid": head} | changed)))
    if not changed and publisher == LOGIN:
        other = rev("f" * 40, "primary")
        other["body"] += f"\nA finding quotes {marker(head, 'primary')}"
        assert cli._confirm_review(wt, cfg, pr, result, publisher, [other]) is False
        return
    reviews = [] if changed else [rev(head, "primary")]
    error = "PR changed during publication recovery" if changed else "publication is revoked, pending or untrusted"

    with pytest.raises(cli.CliError, match=error):
        cli._confirm_review(wt, cfg, pr, result, publisher, reviews)


def rev(head: str, kind: str, *, state: str = "APPROVED", commit: str | None = None,
        login: str = LOGIN, verdict: str = "approve") -> dict:
    """A review object as `gh api .../reviews` returns it, carrying a loopzero marker."""
    return {"body": f"{marker(head, kind)}\n{review_summary(head, kind, verdict=verdict)}", "state": state,
            "commit_id": commit or head, "user": {"login": login}}


def reviews_key(page: int = 1) -> str:
    return f"api repos/{REPO}/pulls/7/reviews " + ("--method GET" if page == 1 else f"page={page}")


def post_key() -> str:
    return f"api repos/{REPO}/pulls/7/reviews"


def posted(call: dict) -> bool:
    return call["argv"][1] == post_key().split()[1] and "--input" in call


def wrote(call: dict) -> bool:
    argv = call["argv"]
    return "--method" in argv and argv[argv.index("--method") + 1] != "GET"


def arm_check_runs(gh: FakeGh, head: str, *runs) -> None:
    gh.respond(f"api repos/{REPO}/commits/{head}/check-runs",
               *({"check_runs": result} for result in runs))


def arm_named_check(gh, head, **fields):
    arm_check_runs(gh, head, [{"name": "checks", **fields}])


def arm_check_progress(gh: FakeGh, head: str, *conclusions: str) -> None:
    arm_check_runs(gh, head, *(
        [{"name": "checks", "status": "in_progress"}] if value == "pending" else [check_run(value)]
        for value in conclusions
    ))


def arm_readiness(
    gh: FakeGh, head: str, wt: Path, *, threads=(), conclusion: str = "success",
    local_exit: int | None = 0, dirty: bool = False,
) -> None:
    gh.respond("api graphql", threads_json(*threads))
    arm_named_check(gh, head, status="completed", conclusion=conclusion)
    gh.respond(f"api repos/{REPO}/commits/{head}/statuses", [])
    if local_exit is not None:
        save_check_receipt(wt, head, local_exit, dirty)


def save_check_receipt(wt: Path, head: str, local_exit: int = 0, dirty: bool = False, *, config_path=None) -> None:
    config = cli.config_mod.load(config_path) if config_path else cli.config_mod.load_trusted(wt)
    results = tuple(CheckResult(command, local_exit, 0.1, "") for command in config.checks)
    cli._save_report(wt, CheckReport(head, dirty, results, cli.sandbox.settings_fingerprint(config)))


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


def assert_one_workflow_read(calls):
    assert sum(call[0] == "show" and call[-1].endswith(":workflow.toml") for call in calls) == 1


def test_config_help_identifies_explicit_policy_override():
    assert "override trusted-base settings" in " ".join(cli.build_parser().format_help().split())


@pytest.mark.parametrize("command", ["ready", "merge"])
def test_wait_defaults_to_the_documented_budget(command):
    """CONTRACT step 5: bare `--wait` is 5400 s; `--wait=N` and `--wait N` set it."""
    parse = cli.build_parser().parse_args
    assert parse([command]).wait is None
    assert parse([command, "--wait"]).wait == 5400.0
    assert parse([command, "--wait=0"]).wait == 0.0
    assert parse([command, "--wait", "30"]).wait == 30.0


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


def is_ready_call(call):
    argv = call["argv"]
    return argv[:2] == ["pr", "ready"] or any("markPullRequestReadyForReview(" in arg for arg in argv)


def on_ready_transition(gh, monkeypatch, callback):
    gh.respond("api graphql ready", "")
    mark_ready = github.mark_ready
    observation = SimpleNamespace(marked=False)

    def transition(*args):
        mark_ready(*args)
        observation.marked = True
        callback()

    monkeypatch.setattr(github, "mark_ready", transition)
    return observation


def assert_contains(text, *fragments):
    for fragment in fragments:
        assert fragment in text


def expect_cli(
    capsys, exit_code, *argv, stdout=None, stderr=None, stdout_contains=(), stderr_contains=(),
    stdout_endswith=None, stderr_startswith=None,
):
    code, out, err = run(capsys, *argv)
    assert code == exit_code, out + err
    if stdout is not None:
        assert out == stdout
    if stderr is not None:
        assert err == stderr
    if stdout_endswith is not None:
        assert out.endswith(stdout_endswith)
    if stderr_startswith is not None:
        assert err.startswith(stderr_startswith)
    assert_contains(out, *stdout_contains)
    assert_contains(err, *stderr_contains)
    return out, err


@pytest.mark.parametrize("command", ["ready", "merge"])
def test_wait_reports_only_check_changes(wt, gh, head, capsys, monkeypatch, command):
    names = ["checks"] + [f"verify deployment / integration shard {n:02d}" for n in range(23)]
    cfg = cli.dataclasses.replace(config.load(wt / "workflow.toml"), required_ci=tuple(names))
    monkeypatch.setattr(cli, "_context", lambda *a, **k: (wt, cfg))
    monkeypatch.setattr(cli, "_verify_wait_snapshot", lambda *a: None)
    monkeypatch.setattr(cli, "WAIT_INTERVAL", 0.1)
    arm_approved_pr(gh, head, wt, "pending", isDraft=False)
    runs = [
        [{"name": name, "status": "completed", "conclusion": "success"}
         if i < cleared else {"name": name, "status": "in_progress"}
         for i, name in enumerate(names)]
        for cleared in (0, *range(25))
    ]
    arm_check_runs(gh, head, *runs)
    poll_clock(monkeypatch)
    if command == "merge":
        monkeypatch.setattr(github, "merge", lambda *a, **k: "c" * 40)
        monkeypatch.setattr(cli, "_cleanup_remote_refs", lambda *a: None)
        monkeypatch.setattr(cli, "_cleanup", lambda *a: None)
    ending = f"ready: {URL}\n" if command == "ready" else "c" * 40 + "\n"
    out, _ = expect_cli(capsys, 0, command, "--wait=1800", stderr="", stdout_endswith=ending)
    assert len(out.encode()) < 3200 and out.count("waiting:") == 1
    assert all(f"required check '{name}' is pending" in out for name in names)
    assert all(f"cleared: {name}\n" in out for name in names)


# --- start / check --------------------------------------------------------------------


def test_start_rejects_bad_slug_on_stderr(repo: Path, monkeypatch, capsys) -> None:
    monkeypatch.chdir(repo)
    expect_cli(capsys, 1, "start", "../x", stdout="", stderr_startswith="loopzero start: invalid slug")


def test_start_without_config(git_repo: Path, monkeypatch, capsys) -> None:
    monkeypatch.chdir(git_repo)
    _, err = expect_cli(capsys, 1, "start", "x", stderr_contains=("workflow.toml",))
    assert err.count("\n") == 1


@pytest.mark.parametrize("dirty", [False, True])
def test_check_pass_writes_report(wt: Path, gh: FakeGh, capsys, dirty) -> None:
    if dirty:
        (wt / "README.md").write_text("uncommitted content")
    out, _ = expect_cli(capsys, 0, "check")
    lines = out.splitlines()
    assert gh.calls == [] and lines[-1] == "PASS"
    if dirty:
        assert "receipt produced:" not in out
        return
    assert lines[0].startswith("checks pinned to origin/main@")
    for stage, command in enumerate(("echo ok", "test -f README.md"), 1):
        assert lines[2 * stage - 1] == f"running: {command}"
        assert lines[2 * stage].startswith(f"stage {stage}/2: exit 0 (")
    assert sum("exit 0" in line for line in lines) == 2
    report = json.loads((wt / ".loopzero" / "checks.json").read_text())
    assert report["head"] == head_of(wt)
    assert report["dirty"] is False
    assert f"receipt produced: head={report['head']} content=" in out
    assert "config=commands-sha256:" in out and "environment=unknown" in out
    assert [(r["command"], r["exit_code"]) for r in report["results"]] == [
        ("echo ok", 0), ("test -f README.md", 0)]
    assert report["results"][0]["tail"] == "ok"
    cli._load_report(wt)
    cli._validation_section(wt, report["head"], config.load(wt / "workflow.toml"))
    assert "receipt " not in capsys.readouterr().out


@pytest.mark.parametrize("state", [None, "CLOSED", "MERGED"])
def test_check_skips_closed_pr_history(wt, gh, capsys, head, state):
    git(wt, "push", "-q", "-u", "origin", "lz/t1")
    associated = (
        [dict(rest_pr_json(pr_json(headRefOid=head, state=state)),
              created_at="2026-11-25T00:00:00Z")] if state else []
    )
    commit_read = f"api repos/{REPO}/commits/{head}/pulls --method GET"
    gh.respond(commit_read, associated)
    gh.respond_pr_list([pr_json(headRefOid=head, state=state)] if state else [])

    out, _ = expect_cli(capsys, 0, "check", stderr="")
    assert out.endswith("PASS\n")
    assert [c["argv"][:2] for c in gh.calls] == [
        commit_pr_read(head), PR_LIST_READ,
    ]


@pytest.mark.parametrize("pushed,refresh_timeout,stage_exit", [
    (False, False, 0), (True, False, 0), (True, True, 0), (True, True, 124),
])
def test_check_refresh_preserves_local_verdict(wt, gh, capsys, monkeypatch, tmp_path, pushed, refresh_timeout, stage_exit):
    git(wt, "push", "-q", "-u", "origin", "lz/t1")
    head = head_of(wt)
    arm_pr(gh, head)
    gh.respond(" ".join([*commit_pr_read(head), "--method", "GET"]),
               [dict(rest_pr_json(pr_json(headRefOid=head)), created_at="2026-11-25T00:00:00Z")])
    if not pushed:
        git(wt, "commit", "-q", "--allow-empty", "-m", "unpushed change")
    options = ()
    if stage_exit:
        monkeypatch.setenv("AUDIT_TOKEN", "ghs_FAKE0123")
        private = tmp_path / "private.toml"
        private.write_text(WORKFLOW.replace("[checks]", '[checks]\nenv_allowlist = ["PATH", "AUDIT_TOKEN"]'))
        options = ("--config", str(private))
        inject_check_timeout(monkeypatch, b"local check ghs_FAKE0123 output")
    endpoint = f"repos/{REPO}/pulls/7"
    gh.respond(f"api {endpoint} --method PATCH", {})
    if refresh_timeout:
        timeout = cli._proc.ProcTimeout(f"PATCH {endpoint} timed out")
        monkeypatch.setattr(github, "update_body", Mock(side_effect=timeout))
    code, out, err = run(capsys, *options, "check")
    assert (code, out.splitlines()[-1]) == ((1, "FAIL") if stage_exit else (0, "PASS"))
    extra = [["api", endpoint]] * (2 - refresh_timeout) if pushed else []
    lookup = LOOKUP_CALLS[2:] if pushed else LOOKUP_CALLS[1:]
    assert [call["argv"][:2] for call in gh.calls] == [commit_pr_read(head_of(wt)), *lookup, *extra]
    report = json.loads((wt / ".loopzero/checks.json").read_text())
    assert report["head"] == head_of(wt) and report["dirty"] is False
    assert report["results"][0]["exit_code"] == stage_exit
    assert bool(err) == refresh_timeout
    if stage_exit:
        assert "local check [REDACTED] output" in err
        assert "local check [REDACTED] output" in report["results"][0]["tail"]
        assert "ghs_FAKE0123" not in err
    if refresh_timeout:
        assert f"warning: could not refresh PR checks: PATCH {endpoint} timed out" in err
    if pushed and not refresh_timeout:
        body = json.loads(gh.calls[-1]["--input"])["body"]
        assert body.count("## Validation") == 1
        assert_contains(body, f"Recorded for `{head_of(wt)[:12]}`", "- `echo ok`: exit 0")


LIVE_PR_BODY = (
    "# Owner-edited title\n\n## Acceptance\n- Updated acceptance on GitHub.\n\n"
    "## Validation\n"
    "Browser: original failure reproduced; corrected image returns 200.\n\n"
    "## Review\n- primary, claude, aaaaaaaaaaaa, request_changes\n"
    "- delta, claude, bbbbbbbbbbbb, approve\n\n"
    "## Evidence\nProduction probe returned 100 records.\n"
)


@pytest.mark.parametrize("command,live,advance", [
    *((command, LIVE_PR_BODY.replace("\n", newline), False)
      for command in ("check", "pr") for newline in ("\n", "\r\n")),
    ("check", "# PR\n\n## Evidence\nRetained browser proof.\n", True),
    ("check", "# PR\n\n## Validation\nHuman evidence.\n\n## Review\nApproved.\n", True),
    ("check", "# PR\n\n## Checks\nLegacy summary.\n\n## Validation\nHuman evidence.\n", True),
], ids=["check-lf", "check-crlf", "pr-lf", "pr-crlf", "new-head-evidence", "new-head-validation", "new-head-legacy"])
def test_refresh_preserves_live_pr_evidence(wt, gh, capsys, command, live, advance):
    git(wt, "push", "-q", "-u", "origin", "lz/t1")
    original_head = head_of(wt)
    arm_pr_body_patch(gh, original_head, body=live)
    expect_cli(capsys, 0, command)
    body = json.loads(gh.calls[-1]["--input"])["body"]
    if advance:
        commit_file(wt, "feature.py", "print('new')\n", "new behavior")
        head = head_of(wt)
        gh.respond_pr_list([pr_json(headRefOid=head, body=body)])
        expect_cli(capsys, 0, command)
        updated = json.loads(gh.calls[-1]["--input"])["body"]
        assert original_head[:12] not in updated and head[:12] in updated
        assert updated.count("Recorded for") == 1 and updated.count("## Validation") == 1
        for text in ("Retained browser proof.", "Human evidence.", "Approved.", "## Checks\nLegacy summary."):
            if text in live:
                assert text in updated
        return

    if command == "check":
        assert body.startswith("# Owner-edited title\n\n## Acceptance\n")
        assert "- Updated acceptance on GitHub." in body
        report = cli._load_report(wt)
        results = tuple(replace(stage, duration_s=999.0) for stage in report.results)
        cli._save_report(wt, replace(report, results=results))
    else:
        assert "The feature prints its greeting." in body
        assert "Updated acceptance on GitHub" not in body
    assert_contains(body, "Browser: original failure reproduced; corrected image returns 200.",
                    "- primary, claude, aaaaaaaaaaaa, request_changes", "- delta, claude, bbbbbbbbbbbb, approve",
                    "## Evidence\nProduction probe returned 100 records.")
    gh.respond_pr_list([pr_json(headRefOid=original_head, body=body)])
    before = len(gh.calls)
    expect_cli(capsys, 0, command)
    history = HISTORY_CALLS if command == "pr" else []
    lookup = LOOKUP_CALLS if command == "pr" else [commit_pr_read(original_head), *LOOKUP_CALLS[1:]]
    assert [c["argv"][:2] for c in gh.calls[before:]] == history + lookup + LOOKUP_CALLS[-1:]


def test_delayed_check_preserves_review_summary(wt, gh, fake_bin, tmp_path, monkeypatch):
    head = prepare_review(wt, gh, fake_bin)
    detail = f"api repos/{REPO}/pulls/7"
    gh.respond(detail, rest_pr_json(pr_json(headRefOid=head, body="# PR\n\nHuman evidence.\n")))
    git(wt, "push", "-q", "-u", "origin", "lz/t1")
    check = tmp_path / "check"
    git(wt, "clone", "-q", "-b", "lz/t1", git(wt, "remote", "get-url", "origin").strip(), str(check))
    context = cli._context
    monkeypatch.setattr(cli, "_context", lambda args, **kw: (check, config.load_trusted(check))
                        if args.command == "check" else context(args, **kw))
    proceed, flock, update = Event(), github.fcntl.flock, github.update_body

    def acquire(fd, op):
        if current_thread() is not main_thread() and op == github.fcntl.LOCK_EX:
            try:
                return flock(fd, op | github.fcntl.LOCK_NB)
            except BlockingIOError:
                proceed.set()
        return flock(fd, op)

    pending = []
    with ThreadPoolExecutor(1) as pool:
        def patch(repo, number, body):
            if "## Review" not in body:
                pending.append(pool.submit(cli.main, ["review"]))
                assert proceed.wait(10)
            update(repo, number, body)
            gh.respond(detail, rest_pr_json(pr_json(headRefOid=head, body=body)))
            proceed.set()

        monkeypatch.setattr(github.fcntl, "flock", acquire)
        monkeypatch.setattr(github, "update_body", patch)
        assert cli.main(["check"]) == pending[0].result(timeout=10) == 0
    final = json.loads(gh.calls[-1]["--input"])["body"]
    assert_contains(final, "Human evidence.", "Recorded for", "- primary, claude")


@pytest.mark.parametrize("command,exit_code", [("check", 0), ("pr", 1)])
def test_malformed_live_markers_do_not_mask_check_verdict(
    wt: Path, gh: FakeGh, capsys, command: str, exit_code: int,
) -> None:
    git(wt, "push", "-q", "-u", "origin", "lz/t1")
    body = "# PR\n\n## Validation\n<!-- loopzero:checks:start -->\nHuman evidence.\n"
    gh.respond_pr_list([pr_json(headRefOid=head_of(wt), body=body)])
    out, _ = expect_cli(capsys, exit_code, command, stderr_contains=("malformed loopzero checks block", "PR description on GitHub"))
    history = HISTORY_CALLS if command == "pr" else []
    calls = history + LOOKUP_CALLS if command == "pr" else [
        commit_pr_read(head_of(wt)), *LOOKUP_CALLS[1:], LOOKUP_CALLS[-1],
    ]
    assert [c["argv"][:2] for c in gh.calls] == calls
    if command == "check":
        assert out.endswith("PASS\n")
        assert json.loads((wt / ".loopzero/checks.json").read_text())["results"][0]["exit_code"] == 0


@pytest.mark.parametrize("receipt", ["saved", "head moved", "write failed"])
def test_check_bounds_large_failure_and_preserves_receipt(wt, head, capsys, monkeypatch, receipt):
    tail = 'TEST_FAILURES={"detail":"' + "é" * 7500 + '","error":"Failed to fetch package"}\n'
    tail += "migration failed: relation widgets already exists\nAssertionError: assert " + "x" * 1000
    monkeypatch.setattr(cli.sandbox, "run_checks", lambda *a, **k: CheckReport(
        "a" * 40 if receipt == "head moved" else head, False,
        (CheckResult("apply migrations", 7, 0.1, tail),),
    ))
    if receipt == "write failed":
        monkeypatch.setattr(cli, "_save_report", Mock(side_effect=PermissionError("receipt write denied")))
    out, err = expect_cli(capsys, 1, "check", stderr_contains=("apply migrations (exit 7)", "uv sync --group dev",))
    assert len((out + err).encode()) < 2500
    assert all(text in err for text in ("migration failed: relation widgets already exists", "AssertionError: assert", "[...]"))
    if receipt == "saved":
        assert out.endswith("FAIL\n") and ".loopzero/checks.json" in err and head in err
        assert json.loads((wt / ".loopzero/checks.json").read_text())["results"][0]["tail"] == tail
    else:
        assert ".loopzero/checks.json" not in err
        assert ("current HEAD" if receipt == "head moved" else "receipt write denied") in err


def test_check_hour_long_stage_transcript(wt, head, gh, capsys, monkeypatch):
    sandbox = cli.sandbox
    clock = poll_clock(monkeypatch)
    deadline = 0.0

    def wait(seconds):
        clock.now += seconds
        return clock.now >= deadline

    monkeypatch.setattr(sandbox.threading, "Event", lambda: Mock(wait=wait))
    monkeypatch.setattr(sandbox.threading, "Thread", lambda *, target, daemon: Mock(start=target))

    def run_checks(cfg, path, *, progress, status, checkpoint, workflow):
        nonlocal deadline
        results = []
        for ordinal in range(1, 25):
            command = f"run tool {ordinal:02d}"
            duration = 3600.0 if ordinal == 1 else 0.1
            deadline = clock.now + duration
            progress(command)
            with sandbox._stage_status(status, ordinal, 24):
                pass
            status(f"stage {ordinal}/24: exit 0 ({duration:.1f}s)")
            results.append(CheckResult(command, 0, duration, ""))
        return CheckReport(head, False, tuple(results), sandbox.settings_fingerprint(cfg))

    monkeypatch.setattr(sandbox, "run_checks", run_checks)
    out, _ = expect_cli(capsys, 0, "check", stderr="", stdout_endswith="PASS\n")
    assert len(out.encode()) < 2400 and not gh.calls
    assert f"receipt produced: head={head}" in out and "stage 24/24: exit 0" in out
    assert out.count("running: run tool") == 24
    assert [float(seconds) for seconds in re.findall(r"running \(([\d.]+)s elapsed\)", out)] == list(
        range(30, 3600, 300)
    )
    assert out.rindex("s elapsed)") < out.index("stage 1/24: exit 0 (3600.0s)")


@pytest.mark.parametrize("command,exit_code,tail", [
    ("echo no; exit 3 # https://fake-heading-secret@example.invalid", 3, "no\n"),
    ("seq 1 50; exit 1", 1, "".join(f"{i}\n" for i in range(11, 51)))])
def test_check_failure_reports_verdict_and_capped_tail(wt, capsys, command, exit_code, tail):
    (wt / "workflow.toml").write_text(WORKFLOW.replace(
        '["echo ok", "test -f README.md"]', json.dumps([command, "test -f README.md"])))
    out, err = expect_cli(capsys, 1, "--config", str(wt / "workflow.toml"), "check", stdout_endswith="FAIL\n")
    assert out.startswith("checks are unpinned because --config was provided\n")
    heading = command.replace("fake-heading-secret", "[REDACTED]")
    assert out.count(f"running: {heading}\n") == out.count("stage 1/2: exit ") == 1
    assert f"stage 1/2: exit {exit_code} (" in out and "README.md" not in out
    assert err == f"--- {heading} (exit {exit_code}) ---\n{tail}Fix the failed stage, then rerun `loopzero check`.\n"
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
        return path, replace(settings, checks=commands, inputs=inputs)

    def dispatch(_config, _wt, _prefix, command, _timeout):
        calls.append(command)
        if interrupt and cut != "final validation" and len(calls) == (5 if changed_inputs else 2):
            raise KeyboardInterrupt
        return CheckResult(command, 0, 0.0, "")

    def check_git(config, path, *args, **kwargs):
        if interrupt and len(calls) == 2 and args == ("rev-parse", "HEAD"):
            raise KeyboardInterrupt
        return original_git(config, path, *args, **kwargs)

    monkeypatch.setattr(cli, "_context", context)
    monkeypatch.setattr(cli.sandbox, "_run_one", dispatch)
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


def advance_checks(wt, commands):
    workflow = WORKFLOW.replace('["echo ok", "test -f README.md"]', json.dumps(commands))
    publish_workflow(wt, workflow, "trusted checks")


@pytest.mark.parametrize("command,missing", [
    ("python3 scripts/gate.py", True), ("scripts/gate.py", True), ("python3 feature.py", False),
    ("test ! -f scripts/absent.py && echo fine", False), (".venv/bin/tool", False), ("VAR=./x echo fine", False)])
def test_check_preflights_missing_trusted_script(wt, capsys, command, missing):
    advance_checks(wt, ["mkdir -p .venv/bin && cp /bin/true .venv/bin/tool; echo expensive-check", command, "rm -rf .venv/bin"])
    out, err = expect_cli(capsys, int(missing), "check", stdout_endswith="FAIL\n" if missing else "PASS\n")
    assert ("expensive-check" in out) != missing
    if missing:
        assert "scripts/gate.py" in err and "integrate" in err and "trusted" in err

@pytest.mark.parametrize("uv,failed", [(True, True), (True, False), (False, True)])
def test_check_offline_cache_preflight(wt, repo, fake_tool, capsys, uv, failed):
    (repo / "pyproject.toml").write_text('[project]\nname = "sample"\nversion = "0"\n')
    git(repo, "add", "pyproject.toml")
    command = "uv run --group dev pytest -q" if uv else "echo Failed to fetch package; exit 1"
    advance_checks(wt, ["echo expensive-check", command])
    git(wt, "merge", "-q", "origin/main")
    fake_tool("uv", 'echo "hatchling not found in cache"; exit 1' if failed else "exit 0")
    out, err = expect_cli(capsys, int(failed), "check", stdout_endswith="FAIL\n" if failed else "PASS\n")
    assert ("expensive-check" in out) == (not uv or not failed) and ("uv sync --offline --locked --group dev" in out) == uv
    assert failed or [r["command"] for r in json.loads((wt / ".loopzero/checks.json").read_text())["results"]] == ["echo expensive-check", command]
    if failed:
        assert "effective UV_CACHE_DIR inside the sandbox is /tmp/uv-cache" in err and "uv sync --group dev" in err
        assert "`[checks] network = false`" in err
        assert "list it in `[checks] writable`" in err and "network = true" not in err


def test_check_sandbox_unavailable_exits_two(wt: Path, fake_tool, capsys) -> None:
    fake_tool("bwrap", "echo 'bwrap: No permissions' >&2\nexit 1\n")
    out, err = expect_cli(capsys, 2, "check")
    assert out.startswith("checks pinned to origin/main@")
    assert_contains(err, "namespace creation failed: exit 1", "ask the host operator to allow user namespaces for bwrap")
    assert not (wt / ".loopzero" / "checks.json").exists()


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


@pytest.mark.parametrize("command,state_file", [
    ("check", None), ("review", None), ("repost", None), ("ready", None),
    ("check", "checks.json"), ("review", "task.md"), ("ready", "checks.json"), ("check", "tracked"),
    ("review", "review-{head}-primary.json"), ("repost", "review-{head}-primary.json"),
])
def test_commands_refuse_linked_local_state(wt, gh, capsys, tmp_path, command, state_file, head):
    if command == "ready":
        arm_approved_pr(gh, head, wt)
    state, sentinel = wt / ".loopzero", tmp_path / "external"
    tracked = state_file == "tracked"
    if tracked:
        sentinel = state / "task.md"
        git(wt, "add", "-f", str(sentinel))
    else:
        target = state / state_file.format(head=head[:12]) if state_file else state
        if state_file and not target.exists():
            target.write_text("local state")
        target.rename(sentinel)
        target.symlink_to(sentinel, target_is_directory=state_file is None)
    directory = state if state_file else sentinel
    directory.chmod(0o755)

    def snapshot():
        return {f.name: f.read_bytes() for f in (directory.iterdir() if not state_file else (sentinel,))}

    original = snapshot()
    args = ("review", "--repost") if command == "repost" else (command,)
    diagnostic = "repository-tracked local state" if tracked else "symbolic link"
    expect_cli(capsys, 1, *args, stderr_contains=(diagnostic,))
    assert snapshot() == original and (tracked or target.is_symlink())
    assert directory.stat().st_mode & 0o777 == 0o755


@pytest.mark.parametrize("policy", ["task", "tag-collision", "release", "override", "bootstrap-override"])
def test_check_selects_and_reports_workflow(wt, repo, tmp_path, capsys, policy):
    options = ()
    if policy == "task":
        (wt / "workflow.toml").write_text(WORKFLOW.replace('"echo ok", "test -f README.md"', '"true"'))
        expected, absent = ("running: echo ok", "running: test -f README.md"), "  true"
    elif policy == "tag-collision":
        older_base = head_of(repo)
        advance_checks(wt, ["echo current-base"])
        git(wt, "tag", "origin/main", older_base)
        expected, absent = (f"checks pinned to origin/main@{head_of(repo)}", "running: echo current-base"), "running: echo ok"
    elif policy == "release":
        release_policy = WORKFLOW.replace('base = "main"', 'base = "release"')
        publish_workflow(wt, release_policy, "select release")
        commit_file(repo, "workflow.toml", release_policy.replace("echo ok", "echo release"), "release checks")
        git(repo, "push", "-q", "origin", "HEAD:refs/heads/release")
        expected, absent = (f"checks pinned to origin/release@{head_of(repo)}", "running: echo release"), None
    else:
        if policy == "bootstrap-override":
            git(wt, "symbolic-ref", "--delete", "refs/remotes/origin/HEAD")
        alt = tmp_path / "alt.toml"
        alt.write_text(WORKFLOW.replace('"echo ok", "test -f README.md"', '"echo alt"'))
        options = ("--config", str(alt))
        expected, absent = ("checks are unpinned because --config was provided", "running: echo alt"), "README"
    stderr = "note: dirty worktree; this check cannot satisfy loopzero ready\n" if policy == "task" else ""
    out, _ = expect_cli(capsys, 0, *options, "check", stderr=stderr)
    assert_contains(out.splitlines(), *expected)
    assert absent is None or absent not in out
    assert not options or out.startswith("checks are unpinned because --config was provided\n")


@pytest.mark.parametrize("command", ["check", "review", "ready", "merge"])
def test_privileged_commands_resolve_policy_independently_of_task(wt, command):
    (wt / "workflow.toml").write_text(WORKFLOW.replace('base = "main"', 'base = "topic"'))
    (wt / ".loopzero/task.md").write_text(f"## Base\nmain @ {head_of(wt)}\n")

    config = cli._load_config(SimpleNamespace(config=None, command=command), wt)

    assert config.base_branch == "main"
    assert config.checks == ("echo ok", "test -f README.md")


@pytest.mark.parametrize("shallow", [False, True], ids=["merge-base", "missing-history"])
def test_primary_review_requires_merge_base(wt, repo, gh, fake_bin, tmp_path, monkeypatch, capsys, shallow):
    commit_file(repo, "upstream.py", "upstream = 1\n", "upstream change")
    git(repo, "push", "-q", "origin", "main")
    checkout = wt
    if shallow:
        git(wt, "push", "-q", "origin", "HEAD:refs/heads/lz/t1")
        checkout = tmp_path / "shallow"
        git(repo, "clone", "-q", "--depth=1", "--no-single-branch", "--branch", "lz/t1",
            (tmp_path / "origin.git").as_uri(), str(checkout))
        git(checkout, "remote", "set-head", "origin", "main")
        (checkout / ".git/info/exclude").write_text(".loopzero/\n")
        shutil.copytree(wt / ".loopzero", checkout / ".loopzero")
        monkeypatch.chdir(checkout)
    else:
        git(wt, "fetch", "-q", "origin")
    prepare_review(checkout, gh, fake_bin, post_id=1 if shallow else 4)
    material = fake_bin / "claude.stdin"
    if shallow:
        expect_cli(capsys, 1, "review", stderr_contains=("no merge base", "deepen or unshallow"))
        assert not material.exists() and not any(map(wrote, gh.calls))
    else:
        expect_cli(capsys, 0, "review")
        assert "upstream = 1" not in material.read_text()


@pytest.mark.parametrize("workflow,error", [
    ("[checks\n", "invalid TOML"),
    ("checks = []\n", "[checks] must be a table"),
    ("delivery = []\n", "[delivery] must be a table"),
    (WORKFLOW.replace("[checks]", "[check]"), "unknown key(s) in top level: check"),
    pytest.param(None, "checks.commands", id="empty-commands"),
])
def test_check_rejects_invalid_base_workflow(wt, capsys, workflow, error):
    if workflow is None:
        advance_checks(wt, [])
    else:
        publish_workflow(wt, workflow, "break workflow")

    diagnostic = error if workflow is None else f"origin/main:workflow.toml: {error}"
    out, _ = expect_cli(capsys, 1, "check", stderr_contains=(diagnostic,))
    assert "PASS" not in out if workflow is None else out == ""
    assert not (wt / ".loopzero" / "checks.json").exists()
    if workflow is None:
        cfg = cli._load_config(SimpleNamespace(config=None, command="ready"), wt)
        head = head_of(wt)
        cli._save_report(wt, CheckReport(head, False, (), cli.sandbox.settings_fingerprint(cfg)))
        assert cli._current_check_report(wt, cfg, head) is None


# --- pr ------------------------------------------------------------------------------------


@pytest.mark.parametrize("token,status,detail,options", [
    ("app-token", 404, False, ()), ("", 404, False, ()), ("app-token", 403, False, ()),
    ("app-token", 404, True, ()), ("app-token", 404, True, ("--repost",)),
])
def test_review_pr_lookup_failure(gh, wt, fake_bin, monkeypatch, capsys, token, status, detail, options):
    monkeypatch.setattr(github, "app_token", lambda: token)
    endpoint = f"repos/{REPO}/pulls/7"
    if detail:
        gh.respond(pr_list_key(), [{"number": 7}])
        gh.fail(f"api {endpoint}", "gh: Not Found (HTTP 404)")
    else:
        gh.fail(pr_list_key(), f"gh: request failed (HTTP {status})")
    _, err = expect_cli(capsys, 1, "review", *options, stdout="" if detail else None)
    command = " ".join(("loopzero", "review", *options))
    assert_contains(err, REPO, f"HTTP {status}")
    if detail:
        assert_contains(err, "PR #7", endpoint, "branch", "lz/t1", f"rerun `{command}`")
        assert gh.calls[0]["argv"][:2] == commit_pr_read(head_of(wt))
        assert [call["argv"] for call in gh.calls[1:]] == [
            [*PR_LIST_READ, "--include"], ["api", endpoint, "--include"],
        ]
    else:
        hint = token == "app-token" and status == 404
        assert ("GitHub App installation" in err) == hint
        if hint:
            assert f"rerun `{command}`" in err
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


def trace_pushes(fake_tool, tmp_path, *, ancestry_probe=None):
    pushes = tmp_path / "pushes"
    probe = f'if [ "$1" = "merge-base" ] && [ "$2" = "--is-ancestor" ]; then {ancestry_probe}; fi' if ancestry_probe else ""
    skip_push = " exit 0;" if ancestry_probe else ""
    fake_tool("git", f'''\
{probe}
if [ "$1" = "push" ]; then printf '%s\\n' "$*" >> "{pushes}";{skip_push} fi
exec /usr/bin/git "$@"
''')
    return pushes


@pytest.mark.parametrize("branch,checked", [
    *((branch, True) for branch in (*TASK_BRANCHES, "lz/zażółć\u00a0")),
    ("lz/t1", False),
])
def test_pr_pushes_and_creates_draft(
    wt: Path, repo: Path, gh: FakeGh, fake_tool, tmp_path: Path, capsys, branch, checked,
) -> None:
    git(wt, "branch", "-m", branch)
    if branch.endswith("\u00a0"):
        git(wt, "branch", branch.removesuffix("\u00a0"), "origin/main")
    history_key = branch_history_key(branch=branch)
    if checked:
        expect_cli(capsys, 0, "check")
    head = head_of(wt)
    arm_new_draft(gh, head, branch=branch)
    task = (wt / ".loopzero" / "task.md").read_text()
    narrative = task.split("## Validation\n", 1)[0].replace("## Notes\n", "## Notes\nEdited on GitHub.\n")
    (wt / ".loopzero" / "task.md").write_text(
        task.replace("(filled by `loopzero check`)", "- Regression failed before the fix.\n- Browser verified.")
        .replace("(filled by `loopzero review`)", "Saved review.")
    )
    gh.respond_pr_view(pr_json(headRefOid=head, body=narrative), branch=branch)
    gh.respond(f"api repos/{REPO}/pulls/7 --method PATCH", {})
    pushes = trace_pushes(fake_tool, tmp_path)
    expect_cli(capsys, 0, "pr", stdout=URL + "\n", stderr="")
    assert pushes.read_text() == f"push origin {head}:refs/heads/{branch}\n"
    assert git(wt, "rev-parse", "--abbrev-ref", "@{upstream}").rstrip("\n") == f"origin/{branch}"
    origin = repo.parent / "origin.git"
    assert git(origin, "rev-parse", branch).strip() == head
    assert [c["argv"][:2] for c in gh.calls] == [
        branch_head_key(branch).split(), history_key.split(),
        branch_head_key(branch).split(), pr_list_key(branch).split(), ["api", f"repos/{REPO}/pulls"],
        ["api", f"repos/{REPO}/pulls/7"], ["api", f"repos/{REPO}/pulls/7"],
    ]
    payload = json.loads(gh.calls[-3]["--input"])
    assert "## Validation" not in payload["body"] and "## Review" not in payload["body"]
    assert payload["draft"] is True and payload["head"] == branch
    heading = (wt / ".loopzero" / "task.md").read_text().splitlines()[0].lstrip("# ")
    assert payload["title"] == heading
    body = json.loads(gh.calls[-1]["--input"])["body"]
    assert "## Context and goal" in body and git(repo, "rev-parse", "origin/main").strip() in body
    assert body.count("## Validation") == 1
    assert "Browser verified." in body and "Saved review." in body and "Edited on GitHub." in body
    assert body.index("## Notes") < body.index("## Validation") < body.index("## Review"), "evidence trails the narrative"
    if checked:
        assert_contains(body, f"Recorded for `{head[:12]}`", "- `echo ok`: exit 0", "- `test -f README.md`: exit 0")
    else:
        assert_contains(body, "Regression failed before the fix.", "(no `loopzero check` run recorded)")


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
    cli._save_report(wt, CheckReport(head, False, results, cli.sandbox.settings_fingerprint(cfg)))

    if refresh:
        git(wt, "push", "-q", "-u", "origin", "lz/t1")
        arm_pr_body_patch(gh, head)
        cli._refresh_pr_checks(wt, cfg)
    else:
        arm_new_draft(gh, head)
        arm_draft_validation(gh, wt, head)
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


@pytest.mark.parametrize("failure", ["merged", "CLOSED", "OPEN", "read", "malformed", "incomplete",
                                     "repeated", "detail", "closed-unmerged"])
def test_pr_requires_proven_branch_history(wt, gh, fake_tool, tmp_path, capsys, head, failure):
    gh.respond_pr_create(pr_json(headRefOid=head))
    if failure == "closed-unmerged":
        closed = pr_json(state="CLOSED", headRefOid=head)
        gh.respond(branch_history_key(), [closed_history_row(closed)])
        gh.respond_pr_list([closed])
        arm_draft_validation(gh, wt, head)
        expect_cli(capsys, 0, "pr", stdout=URL + "\n", stderr="")
        return
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
    assert not any(map(wrote, gh.calls)), "GitHub was written"


def test_pr_pushes_the_captured_commit_not_the_moved_branch(wt, gh, fake_tool, tmp_path, capsys):
    head = head_of(wt)
    arm_new_draft(gh, head)
    arm_draft_validation(gh, wt, head)
    git(wt, "config", "remote.origin.fetch", "+refs/heads/main:refs/remotes/origin/main")  # single-branch clone
    pushes = tmp_path / "pushes"  # another writer moves the branch name just before the push
    fake_tool("git", f'''if [ "$1" = "push" ]; then printf '%s\\n' "$*" >> "{pushes}"; /usr/bin/git commit -q --allow-empty -m moved; fi
exec /usr/bin/git "$@"
''')
    expect_cli(capsys, 0, "pr", stdout=URL + "\n")
    assert pushes.read_text() == f"push origin {head}:refs/heads/lz/t1\n"
    assert git(wt.parent.parent.parent / "origin.git", "rev-parse", "lz/t1").strip() == head
    assert git(wt, "config", "branch.lz/t1.merge").strip() == "refs/heads/lz/t1"


def test_pr_does_not_publish_validation_on_a_draft_created_at_another_head(wt, gh, capsys):
    head = head_of(wt)
    task = wt / ".loopzero" / "task.md"
    task.write_text(
        task.read_text().replace("(filled by `loopzero check`)", "Browser verified.")
        .replace("(filled by `loopzero review`)", "Saved review.")
    )
    arm_new_draft(gh, "b" * 40)  # another writer pushed B after the pinned push
    gh.respond_pr_view(pr_json(headRefOid="b" * 40))
    _, err = expect_cli(capsys, 1, "pr", stdout="")
    assert "rerun loopzero pr" in err and head not in err
    assert not any("PATCH" in call["argv"] for call in gh.calls)
    body = json.loads(gh.calls[-2]["--input"])["body"]
    assert "## Context and goal" in body
    assert not any(text in body for text in ("## Validation", "Browser verified.", "## Review", "Saved review."))

    # The branch is back at the evaluated head, but the draft still has only prose.
    arm_pr_body_patch(gh, head, body=body)
    expect_cli(capsys, 0, "pr", stdout=URL + "\n", stderr="")
    patch = gh.calls[-1]
    assert "PATCH" in patch["argv"]
    body = json.loads(patch["--input"])["body"]
    assert "Browser verified." in body and "Saved review." in body


@pytest.mark.parametrize("command,live", [
    ("pr", {"headRefOid": "b" * 40}),
    ("pr", {"baseRefName": "release"}),
    ("check", {"baseRefName": "release"}),
])
def test_pr_body_mutation_rechecks_live_head_and_target(wt, gh, capsys, head, command, live):
    arm_pr_body_patch(gh, head)
    gh.respond_pr_view(pr_json(**({"headRefOid": head} | live)))
    if command == "check":
        git(wt, "push", "-q", "-u", "origin", "lz/t1")
        cli._refresh_pr_checks(wt, config.load(wt / "workflow.toml"))
    else:
        expect_cli(capsys, 1, "pr", stdout="", stderr_contains=("rerun loopzero pr",))
    assert not any(map(wrote, gh.calls))


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


@pytest.mark.parametrize(("heading", "expected"), [("", "lz/t1"), ("\n# Zażółć gęślą jaźń\n", "Zażółć gęślą jaźń")])
def test_pr_title_uses_only_top_level_heading(wt: Path, monkeypatch, heading, expected) -> None:
    task, read_text = wt / ".loopzero" / "task.md", Path.read_text
    monkeypatch.setattr(Path, "read_text", lambda path, encoding="ascii", **kw: read_text(path, encoding, **kw))
    task.write_text(f"## Summary\nZażółć gęślą jaźń\n{heading}", encoding="utf-8")
    assert cli._pr_title(wt, "lz/t1") == expected


@pytest.mark.parametrize("name,value", [
    ("Context", ""),
    ("Problem", "<one or two plain sentences before code names: what goes wrong (one problem only), who notices, and evidence/cost; refs in brackets at the end>"),
    ("Goal", "<one plain sentence: what the owner or users now notice (and cost/saving if known), because this change fixes the problem>"),
    pytest.param(None, "dirty", id="dirty-worktree"),
    pytest.param(None, None, id="missing-task"),
    pytest.param(None, "- **Context:**\n- **Problem:** <describe the problem>\n", id="unfilled-task"),
])
def test_pr_refuses_invalid_local_state(wt, gh, capsys, name, value):
    task = wt / ".loopzero/task.md"
    if name:
        task.write_text(re.sub(rf"(?m)^- \*\*{name}:\*\*.*$", f"- **{name}:** {value}", task.read_text()))
        message = f"- **{name}:**"
    elif value == "dirty":
        (wt / "scratch.txt").write_text("x")
        message = "uncommitted or untracked"
    elif value is None:
        task.unlink()
        message = (
            f"loopzero pr: missing {task}. Run `loopzero start <slug>` for a new task worktree, "
            "or recreate the file using the handoff template at "
            "https://github.com/feder-positronics/loop-zero/blob/main/core/HANDOFF.md\n"
        )
    else:
        task.write_text(value)
        message = (
            f"loopzero pr: fill '- **Context:**', '- **Problem:**', '- **Goal:**' in {task}"
            " (Context, Problem and Goal become the PR description)\n"
        )
    _, err = expect_cli(capsys, 1, "pr", stdout="")
    assert (message in err) if name or value == "dirty" else (err == message)
    assert gh.calls == []


def test_pr_accepts_angle_brackets_and_sublists_as_filled_text(wt: Path) -> None:
    task = wt / ".loopzero" / "task.md"
    task.write_text(task.read_text().replace("The feature", "\n  - maps <PMID> via `Pick<T>`, then"))
    cli._require_pr_details(wt, (wt / '.loopzero/task.md').read_text())


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
    _, err = expect_cli(capsys, 0, "pr", stdout=URL + "\n")
    assert not any(call[:2] == ["merge-base", "--is-ancestor"] for call in git_calls)
    assert ("warning: ignoring unreadable" in err) == corrupt_report
    assert [c['argv'][:2] for c in gh.calls] == HISTORY_CALLS + LOOKUP_CALLS + [['api', f'repos/{REPO}/pulls/7']] * 2
    assert "(no `loopzero check` run recorded)" in gh.calls[-1]["--input"]


@pytest.mark.parametrize("probe,diagnostic", [
    (None, "fetch and integrate it first"),
    ("kill -TERM $$", "exit -15"),
    ("echo 'fatal: unable to read commit' >&2; exit 128", "unable to read commit"),
], ids=["missing-remote-head", "interrupted-probe", "failed-probe"])
def test_pr_aborts_when_ancestry_cannot_be_verified(wt, gh, fake_tool, tmp_path, capsys, probe, diagnostic):
    if probe is None:
        gh.respond_pr_list([pr_json(headRefOid="a" * 40)])
    else:
        arm_pr_body_patch(gh, head_of(wt))
        git(wt, "commit", "--allow-empty", "-m", "Advance local head")
    pushes = trace_pushes(fake_tool, tmp_path, ancestry_probe=probe)

    _, err = expect_cli(capsys, 1, "pr", stdout="")
    assert diagnostic in err
    if probe is not None:
        assert "git merge-base --is-ancestor" in err
    assert not pushes.exists()
    assert not any(map(wrote, gh.calls))


@pytest.fixture
def rewritten_branch(wt):
    old = head_of(wt)
    git(wt, "push", "-q", "-u", "origin", "lz/t1")
    git(wt, "reset", "-q", "--soft", "HEAD~1")
    git(wt, "commit", "-q", "-m", "rebased feature")
    return old


@pytest.mark.parametrize("case", ["rewrite", "hook-fails", "wrong-destination", "lease-moved", "shared-object"])
def test_pr_uses_lease_for_rewritten_open_pr(wt, repo, gh, fake_tool, tmp_path, capsys, case, rewritten_branch):
    old, head = rewritten_branch, head_of(wt)
    origin, other = repo.parent / "origin.git", tmp_path / "other.git"
    moved = None
    if case in {"lease-moved", "shared-object"}:
        mover = tmp_path / "mover"
        git(tmp_path, "clone", "-q", str(origin), str(mover))
        git(mover, "checkout", "-q", "-b", "lz/t1", "origin/lz/t1")
        commit_file(mover, "remote.txt", "moved\n", "remote move")
        git(mover, "push", "-q", "origin", "lz/t1")
        moved = head_of(mover)
        if case == "shared-object":
            git(repo, "fetch", "-q", "origin", "lz/t1")
    if case == "wrong-destination":
        shutil.copytree(origin, other)
        git(wt, "remote", "set-url", "--push", "origin", str(other))
    if case == "hook-fails":
        hook = fake_tool("pre-push", "echo 'lint failed in widget.py' >&2\nexit 1\n")
        git(wt, "config", "core.hooksPath", str(hook.parent))
    gh.respond_pr_list([pr_json(headRefOid=moved if case == "shared-object" else old)])
    if moved is None:
        pushes = trace_pushes(fake_tool, tmp_path)
        gh.respond(f"api repos/{REPO}/pulls/7 --method PATCH", {})
        gh.respond_pr_view(pr_json(headRefOid=head))
    code, out, err = run(capsys, "pr")
    if moved:
        diagnostic = "absent from this task branch's history" if case == "shared-object" else "remote moved"
        assert (code, out) == (1, "") and diagnostic in err
        assert git(origin, "rev-parse", "refs/heads/lz/t1").strip() == moved
        return
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
    assert pushes.read_text().strip() == f"push origin --porcelain --force-with-lease=lz/t1:{old} {head}:refs/heads/lz/t1"
    assert git(origin, "rev-parse", "refs/heads/lz/t1").strip() == (old if case == "hook-fails" else head)


@pytest.mark.parametrize("wt", ["Łódź"], indirect=True)
def test_legacy_locale_status_reflog_push_and_cleanup(wt, repo, gh, monkeypatch, rewritten_branch):
    branch, head = "lz/Łódź", head_of(wt)
    ref, origin = f"refs/heads/{branch}", repo.parent / "origin.git"
    git(wt, "branch", "-m", branch)
    git(origin, "update-ref", ref, rewritten_branch)
    assert git(wt, "remote", "get-url", "origin").strip() == str(origin)
    monkeypatch.chdir(repo)

    gh.respond(branch_history_key(branch=branch), [])
    arm_pr_body_patch(gh, rewritten_branch, branch=branch)
    gh.respond(reviews_key(), [])
    gh.respond_pr_view(pr_json(headRefOid=rewritten_branch), pr_json(headRefOid=head), branch=branch)
    program = """
from pathlib import Path
from loopzero import cli, worktree

cli._readiness = lambda *_, **__: cli.github.Readiness(False, ("check \\u0141 is pending",))
assert cli.main(["status"]) == 0
# The fixture verifies and uses a local bare remote.
cli._require_push_target = lambda *_: None
assert cli.main(["pr"]) == 0
wt = Path.cwd()
head = worktree.head(wt)
cli._delete_tracking_ref(wt, worktree.branch(wt), head)
worktree.cleanup(wt, head)
"""
    result = subprocess.run(
        [sys.executable, "-X", "utf8=0", "-c", program], cwd=wt,
        env={**os.environ, "LC_ALL": "C", "PYTHONCOERCECLOCALE": "0", "PYTHONIOENCODING": "ascii:strict"},
        capture_output=True, encoding="ascii",
    )
    assert result.returncode == 0, result.stderr
    escaped = branch.encode("ascii", "backslashreplace").decode("ascii")
    assert f"branch:   {escaped}" in result.stdout
    assert r"check \u0141 is pending" in result.stdout
    assert any(pr_list_key(branch).removeprefix("api ") in call["argv"] for call in gh.calls)
    assert git(origin, "rev-parse", ref).strip() == head
    assert not wt.exists() and not git(repo, "branch", "-a", "--list", f"*{branch}")


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


def test_merge_cancel_and_wait_conflict_is_a_usage_error(capsys):
    with pytest.raises(SystemExit) as exc:
        cli.main(["merge", "--cancel", "--wait"])
    assert exc.value.code == 2
    assert "merge --cancel cannot be combined with --wait" in capsys.readouterr().err


# --- review ---------------------------------------------------------------------------


def arm_new_draft(gh: FakeGh, head: str, *, branch="lz/t1") -> None:
    gh.respond(branch_history_key(branch=branch), [])
    gh.respond_pr_list([], branch=branch)
    gh.respond_pr_create(pr_json(headRefOid=head), branch=branch)


def arm_draft_validation(gh: FakeGh, wt: Path, head: str) -> None:
    """The created draft is reread, then receives its validation by PATCH."""
    gh.respond(f"api repos/{REPO}/pulls/7",
               rest_pr_json(pr_json(headRefOid=head, body=(wt / ".loopzero" / "task.md").read_text())))
    gh.respond(f"api repos/{REPO}/pulls/7 --method PATCH", {})


def arm_pr_body_patch(gh: FakeGh, head: str, *, branch: str = "lz/t1", **fields) -> None:
    gh.respond_pr_list([pr_json(headRefOid=head, **fields)], branch=branch)
    gh.respond(f"api repos/{REPO}/pulls/7 --method PATCH", {})


def arm_pr(gh: FakeGh, head: str, *, branch: str = "lz/t1", **over) -> None:
    body = "# T1\n\n## Review\n(filled by `loopzero review`)\n\n## Notes\n"
    arm_pr_body_patch(gh, head, branch=branch, body=body, **over)
    gh.fail(f"api repos/{REPO}/git/ref/heads/{branch}", "gh: Not Found (HTTP 404)")


def arm_pr_base(gh, head, base):
    detail = rest_pr_json(pr_json(headRefOid=head, isDraft=False))
    detail["base"]["sha"] = base
    gh.respond(f"api repos/{REPO}/pulls/7", detail)


def arm_review_pr(wt, gh, head, *, branch="lz/t1", history=(), post_id=None, receipt=True, config_path=None):
    if receipt:
        save_check_receipt(wt, head, config_path=config_path)
    arm_pr(gh, head, branch=branch)
    gh.respond(reviews_key(), list(history))
    if post_id is not None:
        gh.respond(post_key(), {"id": post_id})


def test_review_context_mismatch_names_the_recovery(wt, gh, capsys, head, monkeypatch):
    arm_review_pr(wt, gh, head)
    result = ReviewResult("claude", head, "primary", "approve", (), "")
    monkeypatch.setattr(cli, "_run_review", lambda *_args, **_kwargs: result)
    monkeypatch.setattr(cli, "_matches_publication", lambda *_: False)
    expect_cli(capsys, 1, "review", stderr_contains=("run loopzero review", "configured publisher"))
    assert not any(posted(call) for call in gh.calls)


@pytest.mark.parametrize(("local_exit", "hosted"), [(None, "success"), (1, "failure"), (1, None)])
def test_review_blocks_without_passing_checks(wt, gh, capsys, head, monkeypatch, local_exit, hosted):
    arm_review_pr(wt, gh, head, receipt=False)
    arm_readiness(gh, head, wt, local_exit=local_exit)
    if hosted is None:
        arm_check_runs(gh, head, [])
    else:
        arm_named_check(gh, head, status="completed", conclusion=hosted)
    launch = Mock(side_effect=AssertionError("review launched before checks passed"))
    monkeypatch.setattr(cli, "_run_review", launch)
    expect_cli(capsys, 1, "review", stderr_contains=("loopzero check",))
    launch.assert_not_called()


@pytest.mark.parametrize("failure", ["check", "preflight", "preparation", "other plan"])
def test_review_uses_hosted_checks_with_valid_failed_receipt(wt, gh, fake_bin, fake_tool, capsys, failure):
    commands = {
        "preflight": ["echo ok", "scripts/gate.py"],
        "preparation": ["echo ok", "uv run --group dev pytest -q"],
        "other plan": ["echo ok", "test -f README.md"],
        "check": ["echo ok", "test -f README.md"],
    }
    workflow = WORKFLOW.replace('["checks"]', '["checks", "Loop-zero Eligibility"]')
    publish_workflow(wt, workflow.replace('["echo ok", "test -f README.md"]', json.dumps(commands[failure])))
    if failure == "preparation":
        commit_file(wt, "pyproject.toml", '[project]\nname = "fixture"\nversion = "0"\n', "add project")
        fake_tool("uv", "exit 1")
    elif failure == "check":
        (wt / "README.md").unlink()
        git(wt, "commit", "-qam", "remove check input")
    head = prepare_review(wt, gh, fake_bin, post_id=1, receipt=False)
    arm_readiness(gh, head, wt, local_exit=None)
    if failure == "other plan":
        config = cli.config_mod.load_trusted(wt)
        stage = CheckResult("echo obsolete", 1, 0.1, "")
        cli._save_report(wt, CheckReport(head, False, (stage,), cli.sandbox.settings_fingerprint(config)))
        expect_cli(capsys, 1, "review", stderr_contains=("plan differs",))
        assert not any(posted(call) for call in gh.calls)
    else:
        expect_cli(capsys, 1, "check")
        expect_cli(capsys, 0, "review")
        assert posted_review(gh)["commit_id"] == head


def prepare_review(wt: Path, gh: FakeGh, fake_bin: Path, *, payload=APPROVE,
                   history=(), post_id: int = 1, branch="lz/t1", receipt=True, config_path=None) -> str:
    head = head_of(wt)
    arm_review_pr(wt, gh, head, branch=branch, history=history, post_id=post_id,
                  receipt=receipt, config_path=config_path)
    _fake_claude(fake_bin, _claude_envelope(payload))
    return head


def posted_review(gh: FakeGh) -> dict:
    return json.loads(next(call["--input"] for call in reversed(gh.calls) if posted(call)))


def arm_squash_merge(gh: FakeGh, head: str, wt: Path, *snapshots, branch: str = "lz/t1") -> None:
    arm_approved_pr(gh, head, wt, isDraft=False, branch=branch)
    gh.respond("pr merge", "")
    if snapshots:
        gh.append(f"api repos/{REPO}/pulls/7", *snapshots)


def arm_approved_pr(
    gh: FakeGh, head: str, wt: Path, conclusion: str = "success", *, review_login: str = LOGIN,
    reviews=None, readiness=None, **pr_overrides,
) -> None:
    arm_pr(gh, head, **pr_overrides)
    gh.respond("api graphql ready", "")
    gh.respond(reviews_key(), reviews if reviews is not None else [rev(head, "primary", login=review_login)])
    arm_readiness(gh, head, wt, conclusion=conclusion, **(readiness or {}))


def publish_workflow(wt: Path, policy: str, message: str = "configure delivery") -> None:
    repo = wt.parent.parent
    commit_file(repo, "workflow.toml", policy, message)
    git(repo, "push", "-q", "origin", "main")


def stub_mergify_admission(monkeypatch, *, configured=True, markers=(False, False)):
    if configured is not None:
        monkeypatch.setattr(mergify, "configured", lambda *_: configured)
    monkeypatch.setattr(mergify, "queue_attempt", lambda *_: mergify.QueueAttempt(*markers))
    monkeypatch.setattr(mergify, "membership", lambda *_: None)


def commit_mergify_configuration(wt: Path, message: str = "use mergify") -> str:
    policy = (wt.parent.parent / "workflow.toml").read_text().replace(
        'merge = "squash"', 'merge = "mergify"\nmergify_queue = "main"'
    )
    publish_workflow(wt, policy, message)
    return head_of(wt)


def arm_mergify_pr(wt, gh, conclusion="success", *, message="use mergify", **overrides):
    head = commit_mergify_configuration(wt, message)
    arm_approved_pr(gh, head, wt, conclusion, **({"isDraft": False} | overrides))
    return head


def assert_claude_review_material(fake_bin, head, kind, task, expected_diff):
    header, diff = (fake_bin / "claude.stdin").read_bytes().split(b"\n", 1)
    assert json.loads(header) == dict(head=head, kind=kind, task_text=task, diff_utf8_bytes=len(expected_diff))
    assert diff == expected_diff
    return header, diff


@pytest.mark.parametrize("branch", TASK_BRANCHES)
def test_review_primary_posts_marker(wt: Path, gh: FakeGh, fake_bin: Path, capsys, branch, git_calls) -> None:
    git(wt, "branch", "-m", branch)
    head = prepare_review(wt, gh, fake_bin, payload=CHANGES, branch=branch)
    lookup = commit_pr_read(head)
    gh.respond(" ".join([*lookup, "--method", "GET"]),
               [dict(rest_pr_json(pr_json(headRefOid=head), branch=branch),
                     created_at="2026-11-25T00:00:00Z")])
    task = (wt / ".loopzero/task.md").read_text()
    expected_diff = git(wt, "diff", "--no-color", "origin/main", head).encode("utf-8")
    git_calls.clear()
    out, _ = expect_cli(capsys, 5, "review", stderr="")
    assert_one_workflow_read(git_calls)
    assert [call["argv"][:2] for call in gh.calls[:2]] == [lookup, LOOKUP_CALLS[-1]]
    assert all("/branches/" not in call["argv"][1] for call in gh.calls)
    assert out.splitlines()[0].startswith("reviewer paths pinned to origin/main@")
    assert out.splitlines()[-1] + "\n" == (
        f"primary review by claude on {head[:12]}: request_changes "
        "(0 critical, 1 important, 1 suggestion). Inspect the PR review and run `loopzero resolve` to list open findings.\n"
    )
    post = next(filter(posted, gh.calls))
    payload = json.loads(post["--input"])
    assert payload["commit_id"] == head and payload["event"] == "REQUEST_CHANGES"
    assert marker(head, "primary") in payload["body"]
    header, diff = assert_claude_review_material(fake_bin, head, "primary", task, expected_diff)
    assert b"+print('hi')" in diff
    argv = _argv(fake_bin, "claude")
    policy = argv[argv.index("--append-system-prompt") + 1]
    assert "primary review of the whole change" in policy and policy.encode("utf-8") not in header + diff
    updated = json.loads(gh.calls[-1]["--input"])["body"]
    assert f"- primary, claude, {head[:12]}, request_changes" in updated


@pytest.mark.parametrize("separate_checkout", [False, True])
def test_overlapping_review_dispatches_and_publishes_once(
    wt, repo, gh, fake_bin, capsys, monkeypatch, separate_checkout,
):
    head = prepare_review(wt, gh, fake_bin)
    other = wt
    if separate_checkout:
        other = wt.parent / "other"
        git(repo, "worktree", "add", "--force", str(other), "lz/t1")
        shutil.copytree(wt / ".loopzero", other / ".loopzero")
    post = github.post_review

    def publish(*args, **kwargs):
        post(*args, **kwargs)
        gh.respond(reviews_key(), [rev(head, "primary")])

    monkeypatch.setattr(github, "post_review", publish)
    review = cli._run_review
    dispatched, overlapping = [], []

    def overlap(*args, **kwargs):
        dispatched.append(args[2])
        if len(dispatched) == 1:
            with monkeypatch.context() as nested:
                nested.chdir(other)
                overlapping.append(run(capsys, "review"))
        return review(*args, **kwargs)

    monkeypatch.setattr(cli, "_run_review", overlap)
    code, _, err = run(capsys, "review")
    if separate_checkout:
        assert code == 1 and "primary review round is already taken" in err, overlapping
        assert len(dispatched) == 2 and overlapping[0][0] == 0
    else:
        assert code == 0 and len(dispatched) == 1
        assert overlapping[0][0] == 1 and "review already running" in overlapping[0][2]
    assert sum(map(posted, gh.calls)) == 1


@pytest.mark.parametrize("body_case", ["edited", "read-fails", "existing-review", "receipt", "closed"])
def test_review_summary_preserves_live_body(wt, gh, fake_bin, capsys, monkeypatch, body_case):
    head = prepare_review(wt, gh, fake_bin)
    prior = "- primary, codex, 123456789abc, request_changes"
    line = f"- primary, claude, {head[:12]}, approve"
    live = "# T1\n\n## Notes\nEdited during review\n\n## Validation\nNew check results\n"
    run_review = cli._run_review

    def change_body_during_review(*args, **kwargs):
        result = run_review(*args, **kwargs)
        if body_case == "edited":
            (wt / cli.CHECKS_FILE).unlink()
        if body_case == "read-fails":  # the pre-post checks read the PR twice before the summary does
            done = sum(call["argv"][:2] == f"api repos/{REPO}/pulls/7".split() for call in gh.calls)
            gh.respond(f"api repos/{REPO}/pulls/7", *[rest_pr_json(pr_json(headRefOid=head))] * (done + 2),
                       {"stdout": "", "stderr": "body read failed", "exit": 1})
        else:
            gh.respond(f"api repos/{REPO}/pulls/7", rest_pr_json(pr_json(headRefOid=head, body=live)))
        return result

    def close_after_post(*args, **kwargs):
        post(*args, **kwargs)
        gh.respond(f"api repos/{REPO}/pulls/7", rest_pr_json(pr_json(headRefOid=head, state="CLOSED")))

    if body_case == "closed":
        post = github.post_review
        monkeypatch.setattr(github, "post_review", close_after_post)
    elif body_case == "receipt":
        assert cli.main(["check"]) == 0
        stale = "# T1\n\n## Validation\n<!-- loopzero:checks:start -->\nRecorded for `aaaaaaaaaaaa`\n<!-- loopzero:checks:end -->\n"
        gh.respond(f"api repos/{REPO}/pulls/7", rest_pr_json(pr_json(headRefOid=head, body=stale)))
    elif body_case == "existing-review":
        arm_pr_body_patch(gh, head, body=f"# T1\n\n## Review\n{prior}\n\n## Notes\n")
    else:
        monkeypatch.setattr(cli, "_run_review", change_body_during_review)
    _, err = expect_cli(capsys, 0, "review", stdout_contains=(f"primary review by claude on {head[:12]}: approve",))
    posted_review(gh)
    post_index = next(i for i, call in enumerate(gh.calls) if posted(call))
    assert gh.calls[post_index + 1]["argv"] == [*f"api repos/{REPO}/pulls/7".split(), "--include"]
    patches = [call for call in gh.calls if "PATCH" in call["argv"]]
    if body_case == "closed":
        assert not patches and not err
    elif body_case == "read-fails":
        assert not patches
        assert_contains(err, "could not append PR review summary", "body read failed")
    else:
        assert len(patches) == 1 and not err
        updated = json.loads(patches[0]["--input"])["body"]
        if body_case == "existing-review":
            assert updated.index(prior) < updated.index(line) < updated.index("## Notes")
        elif body_case == "receipt":
            assert f"Recorded for `{head[:12]}`" in updated and "aaaaaaaaaaaa" not in updated
        else:
            assert updated == f"{live}\n## Review\n{line}\n"  # No receipt here: the live record is kept.


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
    git(wt, "commit", "-qam", "change task runtime paths")
    if source == "base":
        git(wt, "update-ref", "refs/remotes/origin/main", head_of(wt))
    elif source == "missing_ref":
        git(wt, "update-ref", "-d", "refs/remotes/origin/main")
    elif source == "bootstrap":
        git(wt, "update-ref", "refs/remotes/origin/main",
            git(wt, "rev-list", "--max-parents=0", "HEAD").strip())
    prepare_review(wt, gh, fake_bin, receipt=source not in {"missing_ref", "bootstrap"},
                   config_path=workflow if source == "override" else None)
    code, out, err = run(capsys, *(["--config", str(workflow)] if source == "override" else []),
                         "review")
    if source in {"missing_ref", "bootstrap"}:
        assert code == 1 and "--config" in err
        assert not (fake_bin / "claude.stdin").exists()
        assert not any(map(posted, gh.calls))
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
    origin = Path(git(wt, "remote", "get-url", "origin").strip())
    if missing == "default":
        git(wt, "symbolic-ref", "--delete", "refs/remotes/origin/HEAD")
    else:
        options = ("-d",) if missing == "remote" else ()
        revision = () if options else (git(wt, "rev-list", "--max-parents=0", "HEAD").strip(),)
        for checkout, ref in ((wt, "refs/remotes/origin/main"), (origin, "refs/heads/main")):
            git(checkout, "update-ref", *options, ref, *revision)
    prepare_review(wt, gh, fake_bin, receipt=False)
    out, err = expect_cli(capsys, 1, command, stderr_contains=("--config",))
    assert "reviewer paths" not in out + err
    assert not gh.calls


@pytest.mark.parametrize("family,options,configured,expected", [
    ("claude", (), "medium", ("opus", "medium")),
    ("claude", ("--model", "sonnet", "--effort", "high"), "medium", ("sonnet", "high")),
    ("codex", (), "high", ("gpt-5.6-luna", "high")),
    pytest.param("claude", (), "max", None, id="disallowed-configured-effort"),
    pytest.param("claude", ("--effort", "max"), "medium", None, id="disallowed-override-effort"),
])
def test_review_model_and_effort(wt, gh, fake_bin, capsys, family, options, configured, expected):
    configured_model = "opus" if family == "claude" else "gpt-5.6-luna"
    reviewers = ["codex", "claude"] if family == "codex" else ["claude", "codex"]
    allowed = ["medium"] if expected is None else ["high"] if family == "codex" else ["medium", "high"]
    model_setting = f'model = "{configured_model}"\n' if expected else ""
    publish_workflow(wt, WORKFLOW.replace('["claude", "codex"]', json.dumps(reviewers)) + f"""
[delivery.review.{family}]
{model_setting}effort = "{configured}"
allowed_efforts = {json.dumps(allowed)}
""")
    if expected is None:
        arm_review_pr(wt, gh, head_of(wt))
        expect_cli(capsys, 1, "review", *options, stdout="", stderr_contains=("effort 'max' is not allowed for claude",))
        assert not (fake_bin / "claude.argv").exists()
        return
    model, effort = expected
    head = prepare_review(wt, gh, fake_bin)
    if family == "codex":
        _fake_codex(fake_bin, json.dumps(APPROVE))
    expect_cli(capsys, 0, "review", *options, stdout_contains=(f"review by {family}",), stderr="")
    if family == "claude":
        argv = _argv(fake_bin, "claude")
        assert (argv[argv.index("--model") + 1], argv[argv.index("--effort") + 1]) == expected
    saved, _ = cli._load_review(wt, head, "primary")
    assert (saved.model, saved.effort) == expected
    assert f"model {model} (effort {effort})" in posted_review(gh)["body"]


def test_review_refuses_when_reviewer_dirties_worktree(
    wt: Path, gh: FakeGh, fake_bin: Path, capsys, head
) -> None:
    arm_review_pr(wt, gh, head)
    reviewer = _fake_claude(fake_bin, _claude_envelope(APPROVE))
    reviewer.write_text(reviewer.read_text().replace("#!/bin/sh\n", "#!/bin/sh\ntouch drift.txt\n"))

    code, out, err = run(capsys, "review")

    saved = wt / ".loopzero" / f"review-{head[:12]}-primary.json"
    assert code == 1 and out.startswith("reviewer paths pinned") and "worktree changed during review" in err
    assert err.count(head) == 2 and not saved.exists()
    assert not any(map(posted, gh.calls))


@pytest.mark.parametrize("option", ["--model", "--effort"])
def test_review_repost_rejects_reviewer_overrides(monkeypatch, capsys, option):
    monkeypatch.setattr(cli, "_context", lambda *_, **_kw: pytest.fail("loaded context for invalid review"))
    with pytest.raises(SystemExit) as stop:
        cli.main(["review", "--repost", option, "override"])

    assert stop.value.code == 2
    assert f"--repost cannot be combined with {option}" in capsys.readouterr().err


def reject_review_post(gh, capsys, saved):
    gh.fail(post_key(), "HTTP 500 server error")
    return expect_cli(capsys, 1, "review", stderr_contains=(f"review saved at {saved}",))


def arm_saved_repost(gh, fake_bin):
    (fake_bin / "claude.stdin").unlink()
    gh.respond(post_key(), {"id": 5})


def test_review_saves_result_and_repost_skips_model(wt: Path, gh: FakeGh, fake_bin: Path,
                                                    capsys, head) -> None:
    arm_review_pr(wt, gh, head)
    envelope = {"padding": "x" * 574_264, "synthetic_metadata": "private-runner-metadata",
                **_claude_envelope(CHANGES, modelUsage={"claude-sonnet-4-6": {}})}
    _fake_claude(fake_bin, envelope)
    saved = wt / ".loopzero" / f"review-{head[:12]}-primary.json"
    out, err = reject_review_post(gh, capsys, saved)
    assert out.startswith("reviewer paths pinned") and "loopzero review --repost" in err
    saved_bytes = saved.read_bytes()
    data = json.loads(saved_bytes)
    assert json.loads(data["raw"]) == envelope
    assert saved.stat().st_mode & 0o777 == 0o600
    assert data["head"] == head and data["kind"] == "primary" and data["family"] == "claude"
    assert data["verdict"] == "request_changes" and len(data["findings"]) == 2
    assert data["model"] == "claude-sonnet-4-6" and data["duration_s"] >= 0
    first_body = posted_review(gh)["body"]
    # Repost can still read ordinary receipts containing the old pair defaults.
    data.update(members=[], reconciliation="", pair_authority=None, pair_prompt_sha256=None)
    saved.write_text(json.dumps(data))
    arm_saved_repost(gh, fake_bin)
    out, _ = expect_cli(capsys, 5, "review", "--repost", stderr="")
    assert out.splitlines()[-1].startswith("primary review by claude")
    assert not (fake_bin / "claude.stdin").exists(), "no model invoked"
    payload = posted_review(gh)
    assert marker(head, "primary", "repost") in payload["body"] and "Nit" in payload["body"]
    assert "claude-session-123" in payload["body"]
    for body in (first_body, payload["body"]):
        assert "model claude-sonnet-4-6" in body and len(body.encode("utf-8")) < 65_536
        assert "private-runner-metadata" not in body and "Raw reviewer output" not in body
    assert saved.read_bytes() == saved_bytes
    assert [c["body"].split("\n")[-1] for c in payload["comments"]] == ["Off by one."]


@pytest.mark.parametrize("repost", [False, True], ids=["first-save", "repost"])
def test_interrupted_review_save_preserves_previous_receipt(wt, gh, fake_bin, capsys, monkeypatch, repost):
    head = prepare_review(wt, gh, fake_bin)
    saved = wt / ".loopzero" / f"review-{head[:12]}-primary.json"
    if repost:
        reject_review_post(gh, capsys, saved)
        original = saved.read_bytes()
        arm_saved_repost(gh, fake_bin)
    args = ["review", "--repost"] if repost else ["review"]
    calls_before = len(gh.calls)
    encode = json.dumps

    def interrupt_encoding(value, *args, **kwargs):
        if isinstance(value, dict) and "publication" in value:
            raise KeyboardInterrupt
        return encode(value, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(json, "dumps", interrupt_encoding)
        assert cli.main(args) == 130
    assert not any(map(posted, gh.calls[calls_before:]))

    if repost:
        assert saved.read_bytes() == original
        assert run(capsys, "review", "--repost")[0] == 0
        assert saved.read_bytes() == original
        assert saved.stat().st_mode & 0o777 == 0o600
        assert not (fake_bin / "claude.stdin").exists()
    else:
        assert not saved.exists()


def test_repost_refuses_a_saved_review_for_the_wrong_round(wt, gh, capsys, head, monkeypatch):
    arm_review_pr(wt, gh, head)
    cli._review_file(wt, head, "primary").touch()
    result = ReviewResult("claude", head, "primary", "approve", (), "")
    monkeypatch.setattr(cli, "_load_review", lambda *_args: (result, None))
    monkeypatch.setattr(cli, "_select_review_round", lambda *_args: ("delta", None))
    monkeypatch.setattr(cli, "_delta_patch", lambda *_args: pytest.fail("processed a mismatched review"))

    expect_cli(capsys, 1, "review", "--repost", stderr_contains=("saved review kind differs",))


@pytest.mark.parametrize("saved_state", ["missing", "stale", "hand-written", "retired"])
def test_repost_refuses_invalid_saved_review(wt, gh, fake_bin, capsys, saved_state, head):
    saved = cli._review_file(wt, head, "primary")
    if saved_state == "retired":
        prepare_review(wt, gh, fake_bin)
        expect_cli(capsys, 0, "review", stderr="")
        saved.write_text(json.dumps(json.loads(saved.read_text()) | {"members": [{"family": "claude"}]}))
        (fake_bin / "claude.stdin").unlink()
        _fake_codex(fake_bin, json.dumps(APPROVE))
    else:
        arm_review_pr(wt, gh, head)
        if saved_state != "missing":
            saved.write_text(json.dumps({
                "family": "claude", "head": "0" * 40 if saved_state == "stale" else head,
                "kind": "primary", "verdict": "approve", "findings": [],
                "raw": "" if saved_state == "stale" else json.dumps(_claude_envelope(APPROVE)),
            }))
    prior_calls = len(gh.calls)
    expected = {"missing": "no saved primary review", "stale": f"not HEAD {head[:12]}",
                "hand-written": "not runner-produced", "retired": "invalid saved review"}
    expect_cli(capsys, 1, "review", "--repost", stdout="", stderr_contains=(expected[saved_state], "Run `loopzero review` without `--repost`"))
    assert all(not (fake_bin / f"{family}.stdin").exists() for family in ("claude", "codex"))
    assert not any(map(posted, gh.calls[prior_calls:]))


@pytest.mark.parametrize("case", ["single", "multiple", "chunk-auth"])
def test_review_chunks_oversized_diff_with_same_family(
    wt, gh, fake_tool, fake_bin, capsys, case, git_calls, monkeypatch,
):
    single, chunk_auth = case == "single", case == "chunk-auth"
    if not single:
        split_diff = cli.runners.split_diff
        monkeypatch.setattr(cli.runners, "split_diff", lambda diff: split_diff(diff, 4096))
        for name in ("large_a.py", "large_b.py"):
            (wt / name).write_text("x = 1\n" * 900)
        git(wt, "add", "large_a.py", "large_b.py")
        git(wt, "commit", "-qm", "large review")

    head = head_of(wt)
    arm_review_pr(wt, gh, head, post_id=8)
    envelope = json.dumps(_claude_envelope(APPROVE))
    _fake_codex(fake_bin, json.dumps(APPROVE))
    fake_tool("claude", f"""
        input=$(mktemp "{fake_bin}/claude.chunk.XXXXXX")
        first=0
        [ -e "{fake_bin}/claude.calls" ] || first=1
        echo "$input" >> "{fake_bin}/claude.calls"
        printf '%s\\0' "$@" > "$input.argv"
        cat > "$input"
        if [ "$first" = 1 ]; then echo 'Prompt is too long' >&2; exit 1; fi
        {"echo 'not authenticated' >&2; exit 1" if chunk_auth else ""}
        echo '{envelope}'
    """)

    git_calls.clear()
    _, err = expect_cli(capsys, 0, "review", stdout_contains=(f"review by {'codex' if chunk_auth else 'claude'}",))
    calls = (fake_bin / "claude.calls").read_bytes().splitlines()
    if single:
        assert len(calls) == 2
        saved, _ = cli._load_review(wt, head, "primary")
        assert saved.chunk_count == 1 and cli.runners.validate_runner_result(saved)
        return
    probes = [cmd for cmd in git_calls if "--git-common-dir" in cmd or "--git-dir" in cmd]
    assert len(probes) == (3 if chunk_auth else 6), probes
    materials, policies, tasks, fragments = [], [], [], []
    for name in calls:
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
    pytest.param("claude", None, "", 4, "saved reviewer family claude contributed", True, id="missing-fallback"),
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
    if configured:
        publish_workflow(wt, WORKFLOW.replace('reviewers = ["claude", "codex"]', f'reviewers = {configured}'))
    git(wt, "commit", "-q", "--allow-empty", "-m", "by claude\n\nCo-Authored-By: Claude <n@a.c>")
    head = prepare_review(wt, gh, fake_bin)
    if family == "codex":
        _fake_codex(fake_bin, json.dumps(APPROVE))
    result = cli.runners.review_with(family, cwd=wt, head=head, kind="primary",
                                     task_text="task", diff="diff")
    cli._save_review(wt, replace(result, fallback=note))
    if code == 4:
        (fake_bin / "claude.stdin").unlink()
    _, err = expect_cli(capsys, code, "review", "--repost", stdout="" if code == 4 else None)
    if code == 4:
        assert not (fake_bin / "claude.stdin").exists()
    if diagnostic:
        assert diagnostic in err
    if code == 0:
        assert "Same-family fallback:" in posted_review(gh)["body"]
    if no_post:
        assert not any(map(posted, gh.calls))


@pytest.mark.parametrize("kind", ["primary", "delta"])
def test_current_dismissed_review_names_recovery(kind, head):
    previous = [cli.Marker("e" * 40, "primary", "APPROVED")] if kind == "delta" else []
    with pytest.raises(cli.CliError, match=r"dismissed.*open a replacement PR"):
        cli._decide_kind([*previous, cli.Marker(head, kind, "DISMISSED")], head)


def test_empty_non_merge_delta_keeps_blocking_findings(wt, monkeypatch):
    since = head_of(wt)
    git(wt, "commit", "--allow-empty", "-qm", "empty fix")
    monkeypatch.setattr(cli.github, "login", lambda **_: LOGIN)
    monkeypatch.setattr(cli.github, "open_blocking_findings", lambda *_: ["open finding"])
    with pytest.raises(cli.CliError, match="empty delta leaves blocking findings open"):
        cli._delta_patch(wt, cli.config_mod.load_trusted(wt), since, head_of(wt), 7)


def test_review_delta_diffs_since_primary(wt: Path, gh: FakeGh, fake_bin: Path, capsys) -> None:
    first = head_of(wt)
    commit_file(wt, "second.py", "x = 2\n", "second")
    head = prepare_review(wt, gh, fake_bin, post_id=2, history=[
        rev(first, "primary", state="CHANGES_REQUESTED", verdict="request_changes")])
    task = (wt / ".loopzero/task.md").read_text()
    expected_diff = git(wt, "diff", "--no-color", first, head).encode("utf-8")
    code, out, _ = run(capsys, "review")
    assert code == 0 and out.splitlines()[-1].startswith(f"delta review by claude on {head[:12]}: approve")
    header, diff = assert_claude_review_material(fake_bin, head, "delta", task, expected_diff)
    assert b"+x = 2" in diff and b"print('hi')" not in diff
    argv = _argv(fake_bin, "claude")
    policy = argv[argv.index("--append-system-prompt") + 1]
    assert "delta review: only review the diff in the material message" in policy
    assert policy.encode("utf-8") not in header + diff
    payload = posted_review(gh)
    assert marker(head, "delta") in payload["body"]


def conflict_merge(wt, repo):
    commit_file(wt, "README.md", "branch edit\n", "edit on branch")
    reviewed = head_of(wt)
    (repo / "upstream.py").write_text("upstream = 1\n")
    git(repo, "add", "upstream.py")
    commit_file(repo, "README.md", "base edit\n", "landed on main")
    with pytest.raises(subprocess.CalledProcessError) as exc:
        git(wt, "merge", "--no-edit", "main")
    assert "CONFLICT" in exc.value.stdout
    commit_file(wt, "README.md", "distinct resolution\n", "resolve base merge")
    return reviewed


@pytest.mark.parametrize("case", [
    "verified", "unverified", "second parent", "rewritten", "missing", "empty", "empty commit", "empty with blocker",
])
def test_review_delta_after_conflict_merge(wt, repo, gh, fake_bin, capsys, isolated_git, case):
    if case == "second parent":
        reviewed = head_of(wt)
        git(wt, "reset", "--hard", "main")
        git(wt, "commit", "--allow-empty", "-qm", "independent branch work")
        git(wt, "merge", "--no-edit", reviewed)
        base = reviewed
    elif case == "missing":
        base, reviewed = head_of(wt), "f" * 40
    elif case == "rewritten":
        base = reviewed = head_of(wt)
        git(wt, "commit", "--amend", "-qm", "rewritten feature")
    elif case in {"empty commit", "empty with blocker"}:
        gh.respond("api graphql", threads_json())
        reviewed, base = head_of(wt), head_of(repo)
        git(wt, "commit", "--allow-empty", "-qm", "empty topic commit")
    elif case == "empty":
        gh.respond("api graphql", threads_json())
        reviewed = head_of(wt)
        commit_file(repo, "new-base.txt", "new base\n", "advance base cleanly")
        base = head_of(repo)
        git(wt, "merge", "--no-ff", "--no-edit", base)
    else:
        reviewed = conflict_merge(wt, repo)
        (wt / ".gitmodules").write_text('[submodule "dep"]\npath = dep\nurl = ../dep\nignore = all\n')
        git(wt, "add", ".gitmodules")
        git(wt, "update-index", "--add", "--cacheinfo", f"160000,{reviewed},dep")
        commit_file(wt, "topic.py", "topic = 2\n", "topic repair")
        base = git(repo, "rev-parse", "main" if case == "verified" else "main^").strip()
    blocker = case in {"second parent", "empty with blocker"}
    history = [rev(reviewed, "primary", state="CHANGES_REQUESTED" if blocker else "APPROVED")]
    if case == "empty with blocker":
        history[0]["body"] = github.BODY_BLOCKERS + history[0]["body"]
    head = prepare_review(wt, gh, fake_bin, history=history)
    arm_pr_base(gh, head, base)
    code, _, err = run(capsys, "review")
    if case == "empty commit":
        assert code == 0, err
        assert marker(head, "delta") in posted_review(gh)["body"]
        return
    if case != "verified":
        assert code == 1 and not list(fake_bin.glob("*.stdin"))
        if case == "missing":
            assert f"git fetch origin {reviewed}" in err
        elif case == "rewritten":
            assert f"reviewed head {reviewed[:12]} is not an ancestor" in err
            assert "git reflog" in err and "open a new PR" in err
            assert "merge `origin/main` into the restored branch" in err and "`loopzero check`, `loopzero pr` and `loopzero review`" in err
        elif case == "empty with blocker":
            assert "empty delta leaves blocking findings in the review body" in err
        elif case == "empty":
            assert f"nothing to review since {reviewed[:12]}" in err and "`loopzero ready`" in err
        else:
            assert f"{reviewed[:12]} must be on the first-parent history" in err
            assert "GitHub objects may be unavailable or exceed verification limits" in err
            assert "Open a new PR from the current base" in err and "replay the fixes" not in err
        return
    assert code == 0, err
    header, diff = (fake_bin / "claude.stdin").read_bytes().split(b"\n", 1)
    assert json.loads(header)["head"] == head
    assert b"+distinct resolution" in diff and b"+topic = 2" in diff and b"upstream.py" not in diff
    assert f"+Subproject commit {reviewed}".encode() in diff and diff.endswith(b"+topic = 2\n")
    posted = posted_review(gh)
    assert posted["commit_id"] == head and marker(head, "delta") in posted["body"]

    exact = rev(head, "delta") | {"body": posted["body"]}
    for reviews, ready in ((history, False), ([*history, exact], True)):
        arm_approved_pr(gh, head, wt, reviews=reviews, isDraft=False)
        arm_pr_base(gh, head, base)
        assert run(capsys, "ready")[0] == (0 if ready else 1)
        assert cli.eligibility.evaluate(REPO, 7, head, "main", (LOGIN,)).ready is ready


@pytest.mark.parametrize("case", [
    "conflict", "repost", "own commit", "rewritten", "unverified", "blocking", "rejected", "clean", "edited clean",
])
def test_review_extra_delta_after_base_conflict(wt, repo, gh, fake_bin, capsys, isolated_git, case):
    if case in {"clean", "edited clean"}:
        reviewed = head_of(wt)
        commit_file(repo, "base.py", "upstream = 1\n", "landed on main")
        git(wt, "merge", "--no-edit", "main")
        if case == "edited clean":
            (wt / "base.py").write_text("manual = 2\n")
            git(wt, "commit", "-qam", "edited base merge", "--amend")
    else:
        reviewed = conflict_merge(wt, repo)
        (wt / "merge-edit.py").write_text("manual = 2\n")
        git(wt, "add", "merge-edit.py")
        git(wt, "commit", "--amend", "--no-edit")
    if case == "conflict":
        commit_file(repo, "later.py", "unrelated = True\n", "landed after the conflict")
        git(wt, "merge", "--no-edit", "main")
    primary = git(wt, "rev-parse", f"{reviewed}^").strip()
    base_ref = "main^" if case == "unverified" else "main"
    base = git(repo, "rev-parse", base_ref).strip()
    if case == "rewritten":
        reviewed = base  # Reaching the reviewed commit through the merge's second parent is insufficient.
    if case == "own commit":
        commit_file(wt, "extra.py", "added = True\n", "new topic change")
    verdict = "request_changes" if case == "rejected" else "approve"
    previous = ReviewResult("claude", reviewed, "delta", verdict, (), "Quoted: **approve**")
    history = [rev(primary, "primary"), rev(reviewed, "delta", state="COMMENTED")]
    history[-1]["body"] = github.review_payload(REPO, 7, reviewed, previous, marker(reviewed, "delta"))["body"]
    head = prepare_review(wt, gh, fake_bin, history=history)
    finding = f"<!-- loopzero:finding v=1 severity=important head={reviewed} id=0b9b59e7 -->"
    readiness = {"threads": (review_thread(finding, author=LOGIN, outdated=True),)} if case == "blocking" else {}
    arm_approved_pr(gh, head, wt, reviews=history, readiness=readiness, isDraft=False)
    arm_pr_base(gh, head, base)

    allowed = case in {"conflict", "repost"}
    out, _ = expect_cli(capsys, int(case != "clean"), "ready")
    assert ("available conflict-resolution delta" in out) is allowed
    assert cli.eligibility.evaluate(REPO, 7, head, "main", (LOGIN,)).ready is (case == "clean")

    material = fake_bin / "claude.stdin"
    if case == "repost":
        gh.fail(post_key(), "HTTP 500 server error")
        assert run(capsys, "review")[0] == 1
        material.unlink()
        gh.respond(post_key(), {"id": 5})
    code, _, err = run(capsys, "review", *(["--repost"] if case == "repost" else []))
    if not allowed:
        assert code == 1 and "review cap reached" in err and not material.exists()
        return
    assert code == 0, err
    if case == "repost":
        assert not material.exists()
    else:
        patch = material.read_bytes()
        assert b"+distinct resolution" in patch and b"+manual = 2" in patch
        assert b"upstream.py" not in patch and b"later.py" not in patch
    posted = posted_review(gh)
    assert posted["commit_id"] == head
    exact = rev(head, "delta") | {"body": posted["body"]}
    gh.respond(reviews_key(), [*history, exact])
    assert run(capsys, "ready")[0] == 0
    assert cli.eligibility.evaluate(REPO, 7, head, "main", (LOGIN,)).ready
    expect_cli(capsys, 1, "review", stderr_contains=("review cap reached",))


def test_review_ignores_forged_history(wt: Path, gh: FakeGh, fake_bin, capsys, head) -> None:
    history = [rev(head, "primary", login="stranger"), rev(head, "delta", commit="2" * 40)]
    assert cli._pr_markers([*history, rev("b" * 40, "primary", commit=head)], head, (LOGIN,)) == []
    prepare_review(wt, gh, fake_bin, history=history)
    out, _ = expect_cli(capsys, 0, "review")
    assert out.splitlines()[-1].startswith("primary review by claude")


def test_review_refuses_when_budget_exhausted(wt: Path, gh: FakeGh, fake_bin, capsys) -> None:
    primary = head_of(wt)
    git(wt, "commit", "--allow-empty", "-qm", "repair after primary")
    delta = head_of(wt)
    git(wt, "reset", "--soft", primary + "^")
    git(wt, "commit", "-qm", "squashed work")
    prepare_review(wt, gh, fake_bin, history=[rev(primary, "primary"), rev(delta, "delta")])
    _, err = expect_cli(capsys, 1, "review", stdout="", stderr_contains=("review cap reached",))
    assert "resolve-findings" in err and "split the PR" in err
    assert not (fake_bin / "claude.stdin").exists()


def configure_review_publisher(wt, publisher):
    commit_file(wt, "workflow.toml", WORKFLOW_BASE + f'\nreview_publishers = ["{publisher}"]\n', "configure publisher")
    git(wt, "update-ref", "refs/remotes/origin/main", "HEAD")


@pytest.mark.parametrize("publisher, allowed", [("app[bot]", True), (LOGIN, False), ("other[bot]", False)])
def test_review_publisher_check_uses_app_identity(
    wt: Path, gh: FakeGh, fake_bin: Path, app_token_helper, capsys,
    publisher: str, allowed: bool,
) -> None:
    app_token_helper("exit 1" if publisher == "other[bot]" else "echo synthetic-app-token")
    configure_review_publisher(wt, publisher)
    prepare_review(wt, gh, fake_bin, post_id=3)
    gh.respond("api graphql viewer", {"data": {"viewer": {"login": "app[bot]"}}})
    _, err = expect_cli(capsys, 0 if allowed else 1, "review")
    if not allowed:
        assert "GitHub login 'app[bot]' is not a configured review publisher" in err
        assert f"Configured publishers: {publisher}" in err
        assert ("Restore the App token helper" in err) is (publisher == "other[bot]") and "rerun `loopzero review`" in err


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
    configure_review_publisher(wt, "app[bot]")
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


def test_review_posts_as_checked_identity(wt, gh, fake_bin, monkeypatch, capsys):
    tokens = ["", "app"]
    monkeypatch.setattr(github, "app_token", lambda: tokens[0])  # flips after the publisher check
    monkeypatch.setattr(github, "login", lambda token=None, f=github.login: (
        f(token), len(tokens) > 1 and tokens.pop(0))[0])
    configure_review_publisher(wt, LOGIN)
    prepare_review(wt, gh, fake_bin, post_id=3)
    gh.respond("api graphql viewer", {"data": {"viewer": {"login": LOGIN}}})
    assert run(capsys, "review")[0] == 0
    assert sum(c["argv"][:2] == ["api", "graphql"] for c in gh.calls) == 1
    assert not any("/files" in c["argv"][1] for c in gh.calls)
    assert posted_review(gh)["event"] == "APPROVE"
    assert [c["token"] or "" for c in gh.calls if "/reviews" in " ".join(c["argv"])
            and "POST" in c["argv"]] == [""]


@pytest.mark.parametrize("publisher", [LOGIN, "OTHER"])
def test_review_refuses_same_head_twice(wt: Path, gh: FakeGh, capsys, publisher) -> None:
    commit_file(wt, "workflow.toml", WORKFLOW_BASE + '\nreview_publishers = ["lz-bot", "other"]\n', "configure trusted publishers")
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
    expect_cli(capsys, 1, "review", stdout="", stderr_contains=(message,))
    assert not (fake_bin / "claude.stdin").exists()
    if condition == "dirty":
        assert gh.calls == []


@pytest.mark.parametrize("case", ["auth", "redaction", "bad-effort-codex-ok", "bad-effort-codex-unavailable"])
def test_review_fallback_after_independent_attempt(wt, gh, fake_bin, fake_tool, capsys, case):
    bad_effort = case.startswith("bad-effort")
    codex_ok = case == "bad-effort-codex-ok"
    if bad_effort:
        publish_workflow(wt, WORKFLOW + '\n[delivery.review.claude]\nallowed_efforts = ["medium"]\n')
    git(wt, "commit", "-q", "--allow-empty", "-m", "by claude\n\nCo-Authored-By: Claude <n@a.c>")
    prepare_review(wt, gh, fake_bin, post_id=4)
    if codex_ok:
        _fake_codex(fake_bin, json.dumps(APPROVE))
    else:
        private_path = " /home/private/secret-path" if case == "redaction" else ""
        fake_tool("codex", "if [ \"$1\" = --version ]; then echo 'codex-cli 0.161.0'; exit 0; fi\n"
                  f"echo 'Please run codex login{private_path}' >&2\nexit 1\n")
    code, out, err = run(capsys, "review", *(("--effort", "high") if bad_effort else ()))
    if bad_effort:
        if codex_ok:
            assert code == 0 and "review by codex" in out
            return
        assert code == 4 and "same-family fallback:" not in err and "reviewer codex unavailable:" not in err
        assert_contains(err, "Please run codex login", "effort 'high' is not allowed for claude")
        assert not (fake_bin / "claude.argv").exists()
        return
    body = posted_review(gh)["body"]
    assert code == 0 and "review by claude" in out and "Same-family fallback:" in body
    assert_contains(err, "reviewer codex unavailable:", "same-family fallback: independent reviewers failed: codex:")
    if case == "redaction":
        assert "independent reviewers failed: codex: not authenticated." in body
        assert "secret-path" not in body


def test_review_redacts_native_authentication_diagnostics(wt, gh, fake_tool, capsys, monkeypatch, head):
    arm_review_pr(wt, gh, head)
    for family, env in (("claude", "ANTHROPIC_API_KEY"), ("codex", "OPENAI_API_KEY")):
        monkeypatch.setenv(env, f"opaque-fake-{family}-credential")
        fake_tool(family, "if [ \"$1\" = --version ]; then echo 'codex-cli 0.161.0'; exit 0; fi\n"
                  f'printf "Not logged in with %s (server echoed sk-FakeToken0123)\\n" "${env}" >&2\nexit 1\n')
    _, err = expect_cli(capsys, 4, "review")
    assert err.count("Not logged in with [REDACTED] (server echoed [REDACTED])") == 2
    assert "opaque-fake-" not in err and "sk-FakeToken0123" not in err
    for family in ("claude", "codex"):
        assert f"{family}: CLI is not authenticated" in err
    assert_contains(err, "Fix: replace or unset `ANTHROPIC_API_KEY`", "Fix: run `codex login`.")


@pytest.mark.parametrize("save", [True, False])
def test_review_malformed_fragments_stay_out_of_terminal(wt, gh, fake_bin, monkeypatch, capsys, save):
    prepare_review(wt, gh, fake_bin)
    envelopes = {family: json.dumps({"unexpected": family * 700}) for family in ("claude", "codex")}

    def malformed(family, *, head, kind, **kwargs):
        raw = envelopes[family]
        cli.runners.parse_review(json.loads(raw), family=family, head=head, kind=kind, raw=raw)

    monkeypatch.setattr(cli.runners, "review_with", malformed)
    if not save:
        monkeypatch.setattr(cli.worktree, "write_local_state", Mock(side_effect=PermissionError("diagnostic write denied")))
    code, out, err = run(capsys, "review")
    size = len((out + err).encode())
    assert size < 650, size
    assert code == 4 and "Fix the reviewer failures below and rerun." in err
    for family in envelopes:
        assert f"{family}: review has unexpected or missing keys" in err
        assert family * 10 not in err
    if save:
        path = next((wt / ".loopzero").glob("reviewer-output-*.json"))
        assert path.name in err and path.stat().st_mode & 0o777 == 0o600
        assert json.loads(path.read_text()) == {family: raw[-2000:] for family, raw in envelopes.items()}
    else:
        assert "diagnostic write denied" in err


@pytest.mark.parametrize("mixed_authors", [False, True])
def test_review_fails_with_three_line_errors_and_remedies_without_spending_budget(
    wt: Path, gh: FakeGh, capsys, monkeypatch, head, mixed_authors
) -> None:
    if mixed_authors:
        git(wt, "commit", "-q", "--allow-empty", "-m",
            "mixed\n\nCo-Authored-By: Claude <n@a.c>\nCo-Authored-By: Codex <n@o.c>")
        head = head_of(wt)
    arm_review_pr(wt, gh, head)

    failures = {
        "claude": cli.runners.RunnerAuthFailed("claude: CLI is not authenticated\nnot printed\nsecond detail\nlast cause"),
        "codex": cli.runners.RunnerMissing("codex: 'codex' not found on PATH"),
    }
    def fail(family, **kwargs):
        raise failures[family]

    monkeypatch.setattr(cli.runners, "review_with", fail)
    out, err = expect_cli(capsys, 4, "review")
    assert out.startswith("reviewer paths pinned")
    author = "claude, codex" if mixed_authors else "unknown"
    assert_contains(err, "no configured reviewer can run", f"author family: {author}",
                    "Fix the reviewer failures below and rerun")
    assert "delivery.reviewers" not in err and "exception" not in err
    assert "claude: CLI is not authenticated\nsecond detail\nlast cause" in err  # The cause ends the tail.
    assert "not printed" not in err
    assert_contains(err, "Fix: run `claude auth login`.", "npm install -g @openai/codex")
    assert err.count("codex: 'codex' not found on PATH") == 1
    assert not any(map(posted, gh.calls))


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
    out, _ = expect_cli(capsys, 2, "review", stderr=f"sandbox unavailable: {message}\n")
    assert out.startswith("reviewer paths pinned")


def test_review_falls_back_after_timeout(wt: Path, gh: FakeGh, capsys, monkeypatch, head) -> None:
    arm_review_pr(wt, gh, head, post_id=4)

    def review_with(family, **kwargs):
        if family == "claude":
            raise cli.runners.RunnerBadOutput("claude: timed out", "partial")
        return _native_codex_result(head, publisher=LOGIN, cwd=str(wt))

    monkeypatch.setattr(cli.runners, "review_with", review_with)
    out, _ = expect_cli(capsys, 0, "review", stderr_contains=("claude: timed out",))
    assert out.splitlines()[-1].startswith("primary review by codex")


# --- ready / merge / status ----------------------------------------------------------


@pytest.mark.parametrize("reposted", [False, True], ids=["published", "reposted"])
@pytest.mark.parametrize("kind,state,verdict,comparison,resolved,ready,history", [
    *[(kind, "APPROVED", "approve", "ahead", True, history == "clean", history)
      for kind in ("primary", "delta") for history in ("clean", "own commit", "altered merge", "missing history")],
    *[(kind, "APPROVED", "approve", comparison, resolved, False, "clean")
      for kind in ("primary", "delta") for comparison, resolved in (("ahead", False), ("diverged", True))],
    ("primary", "COMMENTED", "approve", "ahead", True, True, "clean"),
    ("primary", "CHANGES_REQUESTED", "request_changes", "ahead", True, True, "clean"),
    ("primary", "CHANGES_REQUESTED", "approve", "ahead", True, False, "clean"),
    ("primary", "COMMENTED", "request_changes", "ahead", True, True, "clean"),
    ("primary", "COMMENTED", "request_changes", "ahead", False, False, "clean"),
    *(("primary", "COMMENTED", "request_changes", "ahead", True, False, history)
      for history in ("own commit", "altered merge")),
    ("primary", "APPROVED", "request_changes", "ahead", True, False, "clean"),
    ("primary", "COMMENTED", "unknown", "ahead", True, False, "clean"),
    ("delta", "APPROVED", "approve", "exact", False, True, "clean"),
])
def test_ready_after_reviewed_head(
    wt, gh, capsys, monkeypatch, isolated_git,
    kind, state, verdict, comparison, resolved, ready, history, reposted,
):
    reviewed = head_of(wt)
    git(wt, "switch", "-c", "updated-base", "HEAD^")
    commit_file(wt, "base.txt", "new base content\n", "update base")
    base = head_of(wt)
    git(wt, "switch", "-")
    if comparison != "diverged":
        git(wt, "merge", "--no-edit", "updated-base")
        if history == "own commit":
            git(wt, "commit", "--allow-empty", "-m", "own change")
        elif history == "altered merge":
            (wt / "base.txt").write_text("manual merge resolution\n")
            git(wt, "commit", "--amend", "-am", "merge updated base with edits")
    else:
        git(wt, "rebase", "updated-base")
    head = head_of(wt)
    anchor = head if comparison == "exact" else reviewed
    review = rev(anchor, kind, state=state, verdict=verdict)
    result = ReviewResult("claude", anchor, kind, verdict, (), "Quoted verdict: **approve**",
                          provenance={"session_ids": ["test-session"]})
    prefix = cli._repost_prefix(result) if reposted else marker(anchor, kind)
    review["body"] = github.review_payload(REPO, 7, anchor, result, prefix)["body"]
    severity = "critical" if comparison == "exact" else "important"
    finding = f"<!-- loopzero:finding v=1 severity={severity} head={reviewed} id=0b9b59e7 -->"
    thread = review_thread(finding, resolved=resolved, author=LOGIN, outdated=True)
    reviews = [rev("f" * 40, "primary"), review] if kind == "delta" else [review]
    arm_approved_pr(gh, head, wt, reviews=reviews,
                                      readiness={"threads": (thread,)}, isDraft=False)
    arm_pr_base(gh, head, base)
    if history == "missing history":
        monkeypatch.setattr(cli.eligibility.candidate, "fetched_git",
                            Mock(side_effect=cli.eligibility.candidate.CandidateError("history unavailable")))
    out, _ = expect_cli(capsys, 0 if ready else 1, "ready")
    if not resolved and comparison != "exact":
        assert ("no review recorded" in out) is (verdict == "request_changes")
        assert "open important finding" in out
        if verdict == "request_changes":
            with pytest.raises(cli.CliError, match="loopzero resolve"):
                cli._delta_patch(wt, cli.config_mod.load_trusted(wt), reviewed, head, 7)
    if history != "clean":
        assert ("no eligible review" if kind == "delta" else "no review recorded") in out
    assert cli.eligibility.evaluate(REPO, 7, head, "main", (LOGIN,)).ready is ready


@pytest.mark.parametrize("phase", ["stage", "save", "sandbox returned"])
def test_late_check_preserves_receipt_when_head_moves(wt, gh, capsys, monkeypatch, phase):
    old_head, receipt = head_of(wt), wt / ".loopzero/checks.json"
    refreshed = []
    monkeypatch.setattr(cli, "_refresh_pr_checks", lambda *_: refreshed.append(head_of(wt)))
    expect_cli(capsys, 0, "check")
    saved_receipt = receipt.read_bytes()
    run_stage, run_checks, save_report = cli.sandbox._run_one, cli.sandbox.run_checks, cli._save_report

    def advance_head():
        git(wt, "commit", "--allow-empty", "-qm", "advance HEAD")

    def during_stage(*args):
        monkeypatch.setattr(cli.sandbox, "_run_one", run_stage)
        advance_head()
        return run_stage(*args)

    def before_save(path, report, **kwargs):
        if report.head == old_head:
            advance_head()
        return save_report(path, report, **kwargs)

    def after_checks(*args, **kwargs):
        report = run_checks(*args, **kwargs)
        assert report.ok
        advance_head()
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
    assert "PASS" not in out and "discarded" in err and old_head in err and head in err
    assert refreshed == [old_head]
    if phase == "stage":
        assert f"HEAD before: {old_head}; HEAD after: {head}" in err
        expect_cli(capsys, 0, "check")
        arm_approved_pr(gh, head, wt, readiness={"local_exit": None})
        ready, _ = expect_cli(capsys, 0, "ready", stderr="")
        assert "receipt accepted:" in ready


@pytest.mark.parametrize("mode", ["commands", "settings", "same", "override"])
def test_check_stops_stale_trusted_plan_between_stages(repo, wt, gh, capsys, monkeypatch, mode):
    """Replay the old-plan runs from #1988 without waiting for their long stages."""
    new_workflow = {
        "settings": WORKFLOW.replace("[checks]", "[checks]\nnetwork = true"),
        "same": WORKFLOW + "\n# unchanged settings",
    }.get(mode, WORKFLOW.replace("echo ok", "echo new plan"))
    commit_file(repo, "workflow.toml", new_workflow, "advance trusted plan")
    seen = []

    def stage(config, path, prefix, command, timeout):
        seen.append(command)
        git(wt, "update-ref", "refs/remotes/origin/main", head_of(repo))
        return CheckResult(command, 0, 0.0, "")

    monkeypatch.setattr(cli.sandbox, "_run_one", stage)
    args = ("--config", "workflow.toml", "check") if mode == "override" else ("check",)
    if mode in {"commands", "settings"}:
        expect_cli(capsys, 1, *args, stderr_contains=("trusted check plan changed", "Run loopzero check"))
        assert seen == ["echo ok"]
        assert not (wt / ".loopzero/checks.json").exists()
    else:
        expect_cli(capsys, 0, *args, stdout_endswith="PASS\n")
        assert seen == ["echo ok", "test -f README.md"]


def overlap_calls(monkeypatch, entered, overlap, first, second=None, *, blocking_only=False):
    flock = cli.fcntl.flock
    def lock(fd, flags):
        if entered.is_set() and (not blocking_only or flags == cli.fcntl.LOCK_EX):
            overlap.set()
        return flock(fd, flags)
    monkeypatch.setattr(cli.fcntl, "flock", lock)
    with ThreadPoolExecutor(2) as pool:
        caller = pool.submit(first)
        assert entered.wait(10)
        contender = pool.submit(second or first)
        return caller.result(timeout=15), contender.result(timeout=15)


@pytest.mark.parametrize("first_exit", [0, 1])
def test_overlapping_checks_share_only_a_pass(wt, capsys, monkeypatch, first_exit):
    started, waiting = Event(), Event()

    def stage(_config, _wt, _prefix, command, _timeout):
        started.set()
        assert waiting.wait(5)
        return CheckResult(command, first_exit if run_stage.call_count == 1 else 0, 1.0, "")
    run_stage = Mock(side_effect=stage)
    monkeypatch.setattr(cli.sandbox, "_run_one", run_stage)
    monkeypatch.setattr(cli, "_refresh_pr_checks", Mock())
    check = partial(cli.main, ["check"])
    assert overlap_calls(monkeypatch, started, waiting, check) == (first_exit, 0)
    assert [call.args[3] for call in run_stage.call_args_list] == ["echo ok"] * (1 + first_exit) + ["test -f README.md"]
    assert ("receipt reused:" in capsys.readouterr().out) == (first_exit == 0)


@pytest.mark.parametrize("body_blocker", [False, True])
def test_ready_not_ready_lists_reasons(wt: Path, gh: FakeGh, capsys, head, body_blocker: bool) -> None:
    delta = rev(head if body_blocker else "b" * 40, "delta", state="CHANGES_REQUESTED")
    if body_blocker:
        delta["body"] = github.BODY_BLOCKERS + delta["body"]
    reviews = [rev("a" * 40, "primary"), delta]
    arm_approved_pr(gh, head, wt, "failure", reviews=reviews)
    arm_named_check(gh, head, status="completed", conclusion="failure", check_suite={"id": 10})
    out, _ = expect_cli(capsys, 1, "ready")
    out = out.replace(FAILED_CHECK_HINT, "")
    context = ("Blocking findings in the review body need fixes. The review budget is exhausted." if body_blocker
               else f"The review budget is exhausted and {head[:12]} has no eligible review.")
    assert without_accepted_receipt(out).splitlines() == [
        f"not ready: {context} Open a replacement PR for changes that need review. "
        "Another `loopzero review` on this PR will be refused.",
        "not ready: required check 'checks' is failure",
    ]
    assert not any(is_ready_call(call) for call in gh.calls)
    assert not any("actions/runs" in " ".join(c["argv"]) for c in gh.calls)


@pytest.mark.parametrize("command,required,code,override", [
    ("ready", "", 1, False), ("merge", "", 1, False), ("ready", "", 1, True),
    ("ready", "required_ci = []", 0, False), ("ready", 'required_ci = ["checks"]', 1, False),
])
def test_delivery_requires_explicit_ci_policy(wt, gh, capsys, head, tmp_path, command, required, code, override):
    publish_workflow(wt, WORKFLOW.replace('required_ci = ["checks"]', required) + "\n")
    if required:
        arm_approved_pr(gh, head, wt, "failure", isDraft=False)
    source, options = "origin/main:workflow.toml", ()
    if override:
        path = tmp_path / "override.toml"
        path.write_text(WORKFLOW.replace('required_ci = ["checks"]', required))
        source, options = str(path), ("--config", str(path))
    out, err = expect_cli(capsys, code, *options, command)
    if not required:
        assert f"{source}: checks.required_ci must list the required contexts" in err
        assert "required_ci = []" in err and not gh.calls
    elif code:
        assert "required check 'checks' is failure" in out
    else:
        assert not any(call["argv"][1].endswith(("/check-runs", "/statuses")) for call in gh.calls)


@pytest.mark.parametrize("declared", [False, True])
def test_ready_reads_required_ci_from_selected_workflow(wt, repo, declared):
    redirect = WORKFLOW.replace('base = "main"', 'base = "release"').replace('required_ci = ["checks"]', "")
    publish_workflow(wt, redirect)
    if declared:
        policy = redirect.replace('commands = ', 'required_ci = []\ncommands = ')
        commit_file(repo, "workflow.toml", policy, "set release policy")
    git(repo, "push", "-q", "origin", "HEAD:refs/heads/release")
    args = SimpleNamespace(config=None, command="ready")
    if declared:
        config = cli._load_config(args, wt, refresh=True)
        assert config.base_branch == "release" and config.required_ci == ()
    else:
        with pytest.raises(cli.config_mod.ConfigError, match=r"origin/release:workflow\.toml: checks\.required_ci"):
            cli._load_config(args, wt, refresh=True)


@pytest.mark.parametrize("retargeted,reason", [
    (True, "PR targets release, configured base is main. Retarget the PR on GitHub and retry."),
    (False, f"required check 'checks' is failure{FAILED_CHECK_HINT}"),
], ids=["retargeted", "trust-base-required-ci"])
def test_ready_explains_trusted_policy_blocker(wt, gh, capsys, head, retargeted, reason):
    if retargeted:
        arm_approved_pr(gh, head, wt, baseRefName="release")
    else:
        (wt / "workflow.toml").write_text(WORKFLOW.replace('["checks"]', "[]"))
        arm_approved_pr(gh, head, wt, "failure", isDraft=False)

    out, _err = expect_cli(capsys, 1, "ready", stderr="")
    assert without_accepted_receipt(out) == f"not ready: {reason}\n"


@pytest.mark.parametrize("wait", [False, True])
def test_ready_waitability_ignores_reason_wording(wt, gh, capsys, monkeypatch, head, wait):
    arm_approved_pr(gh, head, wt, isDraft=False)
    arm_check_runs(gh, head, [])
    monkeypatch.setattr(cli.github, "_missing_check_reason", lambda name, *_: f"no {name} run yet")
    if wait:
        monkeypatch.setattr(cli, "MISSING_RUN_GRACE", 0.0)
        gh.respond(f"api repos/{REPO}/actions/runs?head_sha={head}", {"workflow_runs": []})
        expect_cli(capsys, 1, "ready", "--wait", stderr_contains=("workflow scheduling, triggers and permissions",))
    else:
        out, _ = expect_cli(capsys, 3, "ready", stderr="")
        assert without_accepted_receipt(out) == "not ready: no checks run yet\nnext: loopzero ready --wait\n"


def test_repeated_required_check_keeps_each_reason(wt, gh, capsys, head):
    publish_workflow(wt, WORKFLOW.replace('["checks"]', '["checks", "checks"]'))
    arm_approved_pr(gh, head, wt, isDraft=False)
    arm_check_runs(gh, head, [])
    _, out, _ = run(capsys, "ready")
    assert out.count(f"not ready: required check 'checks' missing on {head[:12]}") == 2


def test_check_state_drives_failure_hints_and_wait_keys(capsys):
    unknown, capped = "required check 'a' is unknown", "cannot determine required check 'b'"
    readiness = github.Readiness(False, (unknown, capped), checks={unknown: ("a", "unknown"), capped: ("b", "capped")})
    hinted, plain = cli._readiness_messages(github.PR(7, URL, "a" * 40, "main", False, "OPEN", "MERGEABLE"), readiness)
    assert "Inspect" in hinted and plain == capped
    name = github.MERGEABILITY_PENDING  # a required check may share a non-check reason's text
    check = f"required check '{name}' is pending"
    both = github.Readiness(False, (check, name), checks={check: (name, "pending")})
    only_check = github.Readiness(False, (check,), checks=both.checks)
    cli._print_wait_changes(cli._by_check(both, both.reasons), cli._by_check(only_check, only_check.reasons))
    assert capsys.readouterr().out == f"cleared: {name}\n"


@pytest.mark.parametrize("conclusion,draft", [("pending", False), ("skipped", False), ("skipped", True)])
@pytest.mark.parametrize("command", ["ready", "merge"])
def test_required_check_blocks_delivery_or_activates_draft(wt, gh, capsys, head, conclusion, draft, command):
    arm_approved_pr(gh, head, wt, conclusion, isDraft=draft)
    if conclusion == "pending":
        arm_check_progress(gh, head, conclusion)
    reason = f"required check 'checks' is {conclusion}"
    if command == "merge":
        wait = ("--wait=0",) if conclusion == "skipped" else ()
        out, err = expect_cli(capsys, 1, "merge", *wait)
        assert without_accepted_receipt(out) == ""
        if conclusion == "pending":
            assert f"{reason}; next: loopzero merge --wait" in err
        else:
            hint = "; next: loopzero ready --wait" if draft else ""
            assert err == f"loopzero merge: not ready to merge: {reason}{hint}\n"
    else:
        out, _ = expect_cli(capsys, 3 if draft or conclusion == "pending" else 1, "ready", stderr="")
        if draft:
            assert without_accepted_receipt(out).splitlines() == [
                "marked ready; waiting for required checks: checks; next: loopzero ready --wait",
            ]
            assert any(is_ready_call(c) for c in gh.calls)
            return
        hint = "\nnext: loopzero ready --wait" if conclusion == "pending" else ""
        assert without_accepted_receipt(out) == f"not ready: {reason}{hint}\n"
    assert not any(is_ready_call(call) for call in gh.calls)


@pytest.mark.parametrize("scenario,wait,snapshots", [
    ("uninterrupted", "--wait", ("skipped", "skipped", "pending", "success")),
    ("resume", "--wait=30", ("skipped", "skipped", "skipped", "pending", "success")),
    ("resume-timeout", "--wait=0", ("skipped",)),
], ids=["uninterrupted", "resume", "resume-timeout"])
def test_ready_wait_after_draft_transition(wt, gh, capsys, monkeypatch, head, git_calls, scenario, wait, snapshots):
    arm_approved_pr(gh, head, wt, "skipped")
    if scenario != "uninterrupted":
        def interrupt_after_mark_ready():
            arm_pr(gh, head, isDraft=False)
            raise KeyboardInterrupt
        on_ready_transition(gh, monkeypatch, interrupt_after_mark_ready)
        monkeypatch.setattr(cli, "WAIT_INTERVAL", 1.0)
    arm_check_progress(gh, head, *snapshots)
    monkeypatch.setattr(cli, "_sleep", lambda _: None)
    if scenario != "uninterrupted":
        assert run(capsys, "ready", wait)[0] == 130
    git_calls.clear()
    timed_out = scenario == "resume-timeout"
    out, _ = expect_cli(capsys, 3 if timed_out else 0, "ready", wait, stderr="")
    if scenario == "uninterrupted":
        assert_one_workflow_read(git_calls)
        assert out.count("receipt accepted:") == 1
        assert without_accepted_receipt(out).splitlines() == [
            "marked ready; required check 'checks' is skipped",
            "waiting: required check 'checks' is pending",
            "cleared: checks",
            f"ready: {URL}",
        ]
    else:
        assert ("waiting: required check 'checks' is pending" in out) is not timed_out
        assert ("timed out waiting for required checks" in out) is timed_out
        assert sum(is_ready_call(call) for call in gh.calls) == 1


@pytest.mark.parametrize("settled", ["success", "skipped"])
def test_ready_waits_for_checks_on_original_head(wt, gh, capsys, monkeypatch, settled, head):
    (wt / "workflow.toml").write_text(WORKFLOW.replace('["checks"]', '["a", "b"]'))
    arm_approved_pr(gh, head, wt)
    pending = [{"name": "a", "status": "in_progress"}]
    success = [{"name": name, "conclusion": "success"} for name in ("a", "b")]
    arm_check_runs(gh, head, pending, pending, [{"name": "a", "conclusion": settled}], success)
    monkeypatch.setattr(cli, "_sleep", lambda _: None)
    monkeypatch.setattr(cli, "MISSING_RUN_GRACE", 0.0)
    out, _ = expect_cli(capsys, 0, "--config", "workflow.toml", "ready", "--wait", stderr="", stdout_endswith=f"ready: {URL}\n")
    assert out.count("receipt accepted:") == 1
    assert "required check 'a' is pending" in out
    assert all("f" * 40 not in " ".join(c["argv"]) for c in gh.calls)
    assert sum(is_ready_call(call) for call in gh.calls) == 1


@pytest.mark.parametrize("wait", [False, True])
def test_pending_mergeability_does_not_delay_draft_checks(wt, gh, capsys, head, wait):
    arm_approved_pr(gh, head, wt, "skipped", mergeable="UNKNOWN")

    expect_cli(capsys, 3, "ready", *(["--wait=0"] if wait else []), stderr="",
               stdout_contains=("marked ready;", "checks", "GitHub is computing mergeability"))
    assert sum(is_ready_call(call) for call in gh.calls) == 1


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

    def get(_repo, path, *_):
        vendor_calls.append(path)
        return {
            "/merge-queue/status?branch=main": {"batches": [], "mode": "serial"},
            "/queues/configuration": {"configuration": [{"name": "main", "config": {}}]},
        }[path]

    monkeypatch.setattr(mergify, "api_get", get)
    return repo, config_path, vendor_calls


@pytest.mark.parametrize("command", ["ready", "merge"])
def test_source_wait_reads_once_before_first_interval(wt, gh, capsys, monkeypatch, source_queue_api, command):
    head = arm_mergify_pr(wt, gh, "pending")
    repo, config_path, vendor_calls = source_queue_api
    observations = []
    gh.respond("api -X", "")

    def after_sleep():
        observations.append(([call["argv"][1] for call in gh.calls], len(vendor_calls)))
        if clock.now >= 6:
            arm_named_check(gh, head, conclusion="success")

    clock = poll_clock(monkeypatch, jitter=1.0, after_sleep=after_sleep)
    stub_mergify_admission(monkeypatch, configured=None, markers=(True, False))
    monkeypatch.setattr(cli, "_wait_for_mergify", lambda *_: "e" * 40)

    _out, _err = expect_cli(capsys, 0, command, "--wait=8", stderr="")
    assert clock.now == pytest.approx(7.2)
    [(before_sleep, vendor_count)] = observations
    assert before_sleep.count(f"{repo}/pulls/7") == 1
    assert before_sleep.count(reviews_key().split()[1]) == 1
    assert before_sleep.count(f"{repo}/commits/{head}/check-runs") == 1
    assert before_sleep.count(f"{repo}/commits/{head}/statuses") == 1
    assert before_sleep.count(repo) == 1
    assert before_sleep.count(config_path) == 2
    assert vendor_count == 2
    assert sum(call["argv"][1] == f"{repo}/commits/{head}/check-runs" for call in gh.calls) == 2


@pytest.fixture
def queue_requests(monkeypatch):
    requests = []
    monkeypatch.setattr(mergify, "request", lambda *args: requests.append(args))
    return requests


@pytest.mark.parametrize("command", ["ready", "merge"])
@pytest.mark.parametrize("settled", ["CONFLICTING", "MERGEABLE", "UNKNOWN"])
def test_wait_for_github_mergeability(wt, gh, capsys, monkeypatch, queue_requests, command, settled):
    head = arm_mergify_pr(wt, gh, mergeable="UNKNOWN")
    gh.respond_pr_view(pr_json(headRefOid=head, mergeable=settled, isDraft=False,
                               mergeStateStatus="BEHIND"))
    stub_mergify_admission(monkeypatch)
    monkeypatch.setattr(cli, "_wait_for_mergify", lambda *_: None)

    poll_clock(monkeypatch)
    code, out, err = run(capsys, command, "--wait=60")

    if settled == "CONFLICTING":
        assert code == 1 and "has merge conflicts" in out + err
    elif settled == "UNKNOWN":
        assert code == 3 and " ".join(out.splitlines()).endswith(f"timed out waiting for GitHub to compute mergeability on {head[:12]}. Run `loopzero {command} --wait` to resume on this head.")
    else:
        assert code == (0 if command == "ready" else 3)
    assert queue_requests == ([(REPO, 7, head, "main")]
                        if command == "merge" and settled == "MERGEABLE" else [])


@pytest.mark.parametrize("command,check,hold_at_admission", [
    ("ready", "success", False), ("ready", "skipped", False),
    ("merge", "success", False), ("merge", "success", True),
])
def test_hold_blocks_ready_and_merge(wt, gh, capsys, monkeypatch, queue_requests, command, check, hold_at_admission):
    head = commit_mergify_configuration(wt)
    labels = [{"name": "hold"}]
    arm_approved_pr(
        gh, head, wt, check, isDraft=command == "ready", labels=[] if hold_at_admission else labels
    )
    if hold_at_admission:
        gh.append(f"api repos/{REPO}/pulls/7", {**merge_json(head=head), "labels": labels})
    stub_mergify_admission(monkeypatch)
    out, err = expect_cli(capsys, 1, command, "--wait=0")
    assert "merge-authority hold" in out + err
    assert not queue_requests and all(not is_ready_call(c) for c in gh.calls)
    arm_approved_pr(gh, head, wt, check, isDraft=command == "ready")
    monkeypatch.setattr(cli, "_wait_for_mergify", lambda *_: None)
    expect_cli(capsys, 0 if command == "ready" and check == "success" else 3, command, "--wait=0")
    assert queue_requests == ([(REPO, 7, head, "main")] if command == "merge" else [])
    assert any(is_ready_call(c) for c in gh.calls) == (command == "ready")


def test_hold_label_refresh_matches_local_readiness(wt, gh, head, tmp_path, monkeypatch, capsys):
    trusted = tmp_path / "trusted"
    trusted.mkdir()
    (trusted / "workflow.toml").write_text(WORKFLOW_BASE + f'review_publishers = ["{LOGIN}"]\n')
    event = trusted / "event.json"
    monkeypatch.setenv("GITHUB_REPOSITORY", REPO)
    monkeypatch.setenv("GITHUB_EVENT_PATH", str(event))
    gh.respond(f"api repos/{REPO}/statuses/{head}", {})

    for action, labels, expected in (
        ("labeled", [{"name": "hold"}], 1), ("unlabeled", [], 0),
    ):
        arm_approved_pr(gh, head, wt, isDraft=False, labels=labels)
        event.write_text(json.dumps({"action": action, "pull_request": {"number": 7}}))
        out, _ = expect_cli(capsys, expected, "ready")
        assert ("merge-authority hold" in out) == bool(labels)
        with monkeypatch.context() as hosted_env:
            hosted_env.chdir(trusted)
            assert hosted.main() == 0
        capsys.readouterr()
        payload = json.loads(gh.calls[-1]["--input"])
        assert gh.calls[-1]["argv"][1] == f"repos/{REPO}/statuses/{head}"
        assert payload["state"] == ("failure" if labels else "success")


@pytest.mark.parametrize("wait,elapsed,conclusion,expected", [
    (0, 0, None, 3), (1, 0, "failure", 1), (1, 0.25, "success", 0), (1, 6, "success", 3),
])
def test_ready_wait_verdict(wt, gh, capsys, monkeypatch, head, wait, elapsed, conclusion, expected):
    arm_approved_pr(gh, head, wt, "pending", isDraft=False)
    clock = poll_clock(monkeypatch)
    pr_view = github.pr_view

    def observe(repo, number):
        clock.now += elapsed
        if conclusion:
            arm_named_check(gh, head, conclusion=conclusion)
        return pr_view(repo, number)

    monkeypatch.setattr(github, "pr_view", observe)

    (wt / "-retry config.toml").write_bytes((wt / "workflow.toml").read_bytes())
    out, _ = expect_cli(capsys, expected, "--config", "./-retry config.toml", "ready", f"--wait={wait}", stderr="")
    assert out.endswith(ready_wait_tail(head, expected, "loopzero '--config=-retry config.toml' ready --wait"))


def arm_replacement_workflow(gh, head, conclusion):
    arm_named_check(gh, head, id=100, status="completed", conclusion=conclusion,
                    check_suite={"id": 10}, completed_at="2026-09-23T21:54:41Z")
    gh.respond(f"api repos/{REPO}/actions/runs?head_sha={head}", {"workflow_runs": [
        producing_run(head_sha=head), dict(replacement_run(), head_sha=head),
    ]})


def ready_wait_tail(head, status, resume="loopzero ready --wait"):
    if status == 0:
        return f"ready: {URL}\n"
    if status == 1:
        return f"not ready: required check 'checks' is failure{FAILED_CHECK_HINT}\n"
    return f"timed out waiting for required checks on {head[:12]}. Run `{resume}` to resume on this head.\n"


@pytest.mark.parametrize("rerun", [False, True])
def test_ready_keeps_required_check_signals_from_both_apps(wt, gh, capsys, head, rerun):
    arm_approved_pr(gh, head, wt, isDraft=False)
    runs = [
        {"id": 1, "app": {"id": 10}, "name": "checks", "conclusion": "failure"},
        {"id": 2, "app": {"id": 20}, "name": "checks", "conclusion": "success"},
    ]
    if rerun:
        runs.append({**runs[0], "id": 3, "conclusion": "success"})
    arm_check_runs(gh, head, runs)
    out, _ = expect_cli(capsys, 0 if rerun else 1, "ready", stderr="")
    assert ("required check 'checks' is failure" in out) is not rerun


@pytest.mark.parametrize("outcome", ["retry", "applied", "blocker", "moved", "exhausted", "quota", "short-applied", "write-deadline"])
def test_ready_recovers_ambiguous_activation(wt, gh, capsys, monkeypatch, head, outcome):
    arm_approved_pr(gh, head, wt, "skipped")
    clock, attempts = poll_clock(monkeypatch), []
    def activate(*_):
        attempts.append(clock.now)
        clock.now += {"short-applied": 59, "write-deadline": 60}.get(outcome, 0)
        if outcome == "write-deadline":
            raise github.ProcTimeout("ready write reached deadline")
        if outcome.endswith("applied") or (outcome == "retry" and len(attempts) == 2):
            arm_pr(gh, head, isDraft=False)
            arm_named_check(gh, head, conclusion="success")
        if outcome == "blocker":
            arm_named_check(gh, head, conclusion="failure")
        if outcome == "moved":
            arm_pr(gh, "f" * 40)
        if not (outcome == "retry" and len(attempts) == 2):
            detail = "secondary rate limit" if outcome == "quota" else "HTTP 502"
            raise github.GhUnavailable(("gh", "pr", "ready"), detail)
    monkeypatch.setattr(github, "mark_ready", activate)
    receipt = wt / ".loopzero/checks.json"
    before = receipt.read_bytes()
    code, out, err = run(capsys, "ready", "--wait=60")
    assert attempts == ([0, 5] if outcome in {"retry", "exhausted"} else [0])
    assert receipt.read_bytes() == before
    assert code == {"blocker": 1, "moved": 1, "exhausted": 2, "quota": 2, "write-deadline": 3}.get(outcome, 0)
    assert code != 2 or "Run `loopzero ready --wait`" in err
    assert code != 0 or f"ready: {URL}" in out


@pytest.mark.parametrize("command,budget,activation", [
    ("ready", 0, False), ("ready", 1, False), ("ready", 1, True), ("merge", 1, False),
])
def test_wait_bounds_initial_reads(wt, gh, capsys, monkeypatch, head, budget, activation, command):
    arm_approved_pr(gh, head, wt, isDraft=bool(budget))
    clock = poll_clock(monkeypatch)
    read = github.pr_view if activation else github.pr_for_branch
    limits = []
    def slow_read(*args, **kwargs):
        limits.append(github.read_timeout(120))
        pr = read(*args, **kwargs)
        clock.now = 1.0
        return pr
    monkeypatch.setattr(github, read.__name__, slow_read)
    out, _ = expect_cli(capsys, 3 if budget else 0, command, f"--wait={budget}", stderr="")
    assert limits == [budget or 120]
    assert ("timed out" in out) is bool(budget)
    assert not any(is_ready_call(call) for call in gh.calls)


@pytest.mark.parametrize("replacement,expected", [("success", 0), ("failure", 1), ("pending", 3)])
def test_ready_wait_tracks_the_replacement_workflow(
    wt: Path, gh: FakeGh, capsys, monkeypatch, replacement: str, expected: int, head
) -> None:
    arm_approved_pr(gh, head, wt, isDraft=False)
    old_conclusion = "cancelled" if replacement == "pending" else "failure"
    arm_replacement_workflow(gh, head, old_conclusion)
    replace_checks = partial(arm_named_check, gh, head, id=101, status="completed", conclusion=replacement)
    poll_clock(monkeypatch, after_sleep=replace_checks, patch_sleep=replacement != "pending")
    out, _err = expect_cli(capsys, expected, "ready", "--wait=0" if replacement == "pending" else "--wait=60", stderr="")
    assert f"waiting: required check 'checks' is {old_conclusion}" in out
    assert out.endswith(ready_wait_tail(head, expected))


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
    arm_approved_pr(gh, head, wt, "pending" if initial_pending else "success")

    def transition():
        gh.respond_pr_view(pr_json(headRefOid=head, isDraft=False))
        arm_replacement_workflow(gh, head, "cancelled")

    def advance():
        if not observation.marked or replacement != "pending":
            settled = replacement if observation.marked else "success"
            arm_named_check(gh, head, conclusion=settled)
    clock = poll_clock(monkeypatch, jitter=1.0, after_sleep=advance)
    observation = on_ready_transition(gh, monkeypatch, transition)
    out, _err = expect_cli(capsys, expected, "ready", "--wait=40", stderr="")
    assert "waiting: required check 'checks' is cancelled" in out
    assert clock.sleeps == ([30.0, 9.0, 1.0] if replacement == "pending" else
                      [30.0, 9.0] if initial_pending else [30.0])
    assert out.endswith(ready_wait_tail(head, expected))
    assert sum(is_ready_call(call) for call in gh.calls) == 1


@pytest.mark.parametrize("after", ["pending", "late_success", "head_move", "blocker", "cancelled"])
def test_ready_transition_preserves_deadline_and_gates(wt, gh, capsys, monkeypatch, head, after):
    arm_approved_pr(gh, head, wt, "pending")

    def transition():
        gh.respond_pr_view(pr_json(headRefOid="f" * 40 if after == "head_move" else head,
                                   isDraft=False, baseRefName="release" if after == "blocker" else "main"))
        arm_named_check(gh, head, conclusion="cancelled" if after == "cancelled" else "pending")
        gh.respond(f"api repos/{REPO}/actions/runs?head_sha={head}", {"workflow_runs": []})

    def advance():
        if not observation.marked:
            arm_named_check(gh, head, conclusion="success")
    clock = poll_clock(monkeypatch, jitter=1.0, after_sleep=advance)
    observation = on_ready_transition(gh, monkeypatch, transition)
    if after == "late_success":
        def late_read(*args, **kwargs):
            current = original_view(*args, **kwargs)
            if not current.is_draft:
                clock.now = 41.0
                arm_named_check(gh, head, conclusion="success")
            return current
        original_view = github.pr_view
        monkeypatch.setattr(github, "pr_view", late_read)
    out, err = expect_cli(capsys, 1 if after in {"head_move", "blocker", "cancelled"} else 3, "ready", "--wait=40")
    assert clock.sleeps == ([30.0, 9.0, 1.0] if after == "pending" else [30.0])
    assert "ready: https" not in out
    assert ("PR head changed while waiting" in err) == (after == "head_move")
    if message := {"cancelled": "not ready: required check 'checks' is cancelled",
                   "blocker": "PR targets release, configured base is main"}.get(after):
        assert message in out
    assert sum(is_ready_call(call) for call in gh.calls) == 1


@pytest.mark.parametrize("run_appears", [True, False])
def test_ready_transition_missing_run_gets_observation_grace(wt, gh, capsys, monkeypatch, head, run_appears):
    arm_approved_pr(gh, head, wt, "pending")
    marked_at = []

    def transition():
        marked_at.append(clock.now)
        gh.respond_pr_view(pr_json(headRefOid=head, isDraft=False))

    def advance():
        if marked_at and run_appears:
            arm_named_check(gh, head, conclusion="success")
        elif clock.now >= 210:
            arm_check_runs(gh, head, [])
    clock = poll_clock(monkeypatch, jitter=1.0, after_sleep=advance)
    on_ready_transition(gh, monkeypatch, transition)
    out, _ = expect_cli(capsys, 0 if run_appears else 3, "ready", "--wait=300", stderr="")
    assert marked_at == [210.0]
    assert clock.sleeps == [30.0, 60.0, 120.0, 30.0] + ([] if run_appears else [55.0, 5.0])
    assert out.endswith(ready_wait_tail(head, 0 if run_appears else 3))
    assert sum(is_ready_call(call) for call in gh.calls) == 1


@pytest.mark.parametrize("branch,wait,draft", [
    *((branch, False, True) for branch in TASK_BRANCHES),
    ("lz/t1", True, True), ("lz/t1", True, False),
])
def test_ready_transition_immediate_green_is_best_effort(wt, gh, capsys, head, branch, wait, draft, git_calls):
    git(wt, "branch", "-m", branch)
    arm_approved_pr(gh, head, wt, branch=branch, isDraft=draft)
    git_calls.clear()
    code, out, err = run(capsys, "ready", *(["--wait"] if wait else []))
    assert (code, without_accepted_receipt(out), err) == (0, f"ready: {URL}\n", "")
    ready_calls = [c["argv"] for c in gh.calls if is_ready_call(c)]
    assert len(ready_calls) == int(draft)
    assert all(argv[:2] == ["api", "graphql"] and "id=PR_7" in argv for argv in ready_calls)
    assert sum(c["argv"][:2] == ["api", f"repos/{REPO}/commits/{head}/check-runs"]
               for c in gh.calls) == (2 if wait and draft else 1)
    if not wait:
        assert_one_workflow_read(git_calls)
        assert ["ls-files", "-z", "--", ".loopzero"] in git_calls
        assert out.count("receipt accepted:") == 1
        assert f"head={head}" in out and "environment=unknown" in out


@pytest.mark.parametrize("name", ["checks", "Loop-zero Eligibility"])
@pytest.mark.parametrize("pending,age,new_failure,wait,expected,waited", [
    (True, 180, False, True, 0, True),
    *((False, age, False, True, 0, True) for age in (30, -30, 120, 119, 300, 899)),
    *((False, age, False, True, 1, False) for age in (-3600, 900, 901)),
    *((False, -30, failure, True, 1, False) for failure in (True, "tie")),
    *((pending, 30, True, True, int(pending), True) for pending in (True, False)),
    *((pending, 300, failure, True, 1, False)
      for pending, failure in ((False, True), (True, True), (False, "tie"), (False, "missing"))),
    (False, 300, False, False, 1, False),
    *[(False, 180, failure, True, 1, False) for failure in
      ("missing", "fractional-newer", "offset-tie", "naive-check", "malformed-check")],
    (False, 180, "offset-older", True, 0, True),
    *[(False, 5, failure, True, 1, False) for failure in ("naive-review", "malformed-review", "missing-review")],
    *[(False, 5, sample, True, 0, True) for sample in
      ("server-synced", "server-host-behind", "server-host-ahead", "invalid-date", "fractional-review")],
    (False, 900, "server-old-review", True, 1, False),
])
def test_ready_wait_review_failure_freshness(
    wt: Path, gh: FakeGh, capsys, monkeypatch, name, pending, age, new_failure, wait, expected, waited, head,
) -> None:
    (wt / "workflow.toml").write_text(WORKFLOW.replace('["checks"]', json.dumps([name])))
    current, earlier = "12:00:00Z", "11:59:59Z"
    refresh = name == "Loop-zero Eligibility" and new_failure == "server-old-review"
    expected, waited = (0, True) if refresh else (expected, waited)
    if refresh:
        gh.respond(f"api {ELIGIBILITY_DISPATCH.removesuffix('/dispatches')}/runs?event=workflow_dispatch&branch=main&per_page=100", {"workflow_runs": [{"display_title": "Refresh eligibility for PR #7", "status": "queued"}]})
    review_time, failure_time = {
        True: (current, "12:00:10Z"), "tie": (current, current), "missing": (current, None),
        "fractional-review": ("12:00:00.1234569Z", "12:00:00.1234561Z"),
        "fractional-newer": (current, "12:00:00.250Z"),
        "offset-tie": ("12:00:00+00:00", "11:00:00-01:00"),
        "offset-older": ("13:00:00+01:00", "13:59:59+02:00"),
        "naive-check": (current, "11:59:00"), "malformed-check": (current, "12:00:00+00:60"),
        "naive-review": ("12:00:00", earlier), "malformed-review": ("bad", earlier),
        "missing-review": (None, earlier),
    }.get(new_failure, (current, earlier))
    review_at, failed_at = (f"2026-09-23T{value}" if value else None for value in (review_time, failure_time))
    reviews = [rev(head, "primary") | {"submitted_at": review_at}]
    arm_approved_pr(gh, head, wt, reviews=reviews, isDraft=False)
    server_now = 1790164800 + age
    server_timeline = str(new_failure).startswith("server-")
    if server_timeline or new_failure == "invalid-date":
        date = (formatdate(server_now, usegmt=True) if server_timeline
                else "Wed, 23 Sep 2026 12:00:05 +0060")
        gh.respond(reviews_key(), f"HTTP/2 200 OK\nDate: {date}\n\n{json.dumps(reviews)}")
    offsets = {"server-host-behind": -40, "server-host-ahead": 1000, "server-old-review": -40}
    start = server_now + offsets.get(new_failure, 0)
    pending_at = "2026-09-23T12:00:20Z"
    if name == "checks":
        runs = [{"name": name, "conclusion": "failure", "status": "completed",
                 "completed_at": failed_at, "started_at": "2026-09-23T11:59:59Z"}]
        statuses = [{"context": name, "state": "pending", "created_at": pending_at}] if pending else []
    else:
        runs = [{"name": name, "status": "in_progress", "started_at": pending_at}] if pending else []
        statuses = [{"context": name, "state": "failure", "created_at": failed_at}]
    arm_check_runs(gh, head, runs)
    status_key = f"api repos/{REPO}/commits/{head}/statuses"
    gh.respond(status_key, statuses)
    def advance():
        if expected == 0 and (age != 300 or len(clock.sleeps) > 1):
            arm_check_runs(gh, head, [{"name": name, "conclusion": "success"}])
            gh.respond(status_key, [{"context": name, "state": "success"}])
    clock = poll_clock(monkeypatch, after_sleep=advance)
    monkeypatch.setattr(cli.time, "time", lambda: start + clock.now)
    wait_flag = "--wait=10" if server_timeline else "--wait=60" if age < 0 else "--wait"
    out, _ = expect_cli(capsys, expected, "--config", "workflow.toml", "ready",
                        *([wait_flag] if wait else []), stderr="")
    assert bool(clock.sleeps) is waited
    assert not (server_timeline and waited) or clock.sleeps == [9.0]
    assert out.endswith(f"ready: {URL}\n" if expected == 0 else
                        f"not ready: required check '{name}' is failure{FAILED_CHECK_HINT}\n")
    assert (f"waiting: required check '{name}' is failure" in out) == waited
    assert out.count("predates the review; awaiting refresh") == int(waited and (age < 0 or age >= 120) and not pending and not refresh)


def test_ready_wait_stops_if_pr_head_moves(wt, gh, capsys, monkeypatch, head):
    moved = "f" * 40
    arm_approved_pr(gh, head, wt, "pending", isDraft=False)
    gh.respond_pr_view(pr_json(headRefOid=moved, isDraft=False))
    monkeypatch.setattr(cli, "_sleep", lambda _: None)
    expect_cli(capsys, 1, "ready", "--wait", stdout_contains=("waiting:",),
               stderr_contains=(f"PR head changed while waiting: {head[:12]} to {moved[:12]}",))


@pytest.mark.parametrize("running", [None, "upstream", "required-sibling", "workflow", "skipped"])
def test_ready_wait_with_missing_required_check(wt, gh, capsys, monkeypatch, head, running):
    # #189: an upstream job can be running before the required job appears.
    if running == "required-sibling":
        publish_workflow(wt, WORKFLOW.replace('["checks"]', '["checks", "Backend Tests"]'))
    arm_approved_pr(gh, head, wt, isDraft=False)
    checks = [{"name": "Backend Tests", "status": "in_progress"}] if running in ("upstream", "required-sibling") else []
    if running == "skipped":
        checks = [{"name": "checks", "status": "completed", "conclusion": "skipped"}]
    arm_check_runs(gh, head, checks)
    gh.respond(f"api repos/{REPO}/actions/runs?head_sha={head}", {
        "workflow_runs": [{"head_sha": head, "status": "queued"}] if running == "workflow" else [],
    })
    monkeypatch.setattr(cli, "MISSING_RUN_GRACE", 0.0)

    wait = "--wait=0" if running and running != "skipped" else "--wait=90"
    poll_clock(monkeypatch, jitter=1.0)
    code, out, err = run(capsys, "ready", wait)

    if running == "skipped":
        assert code == 1 and "required check 'checks' is skipped after" in err
    elif running:
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


@pytest.mark.parametrize("change,diagnostic", [
    ({"headRefOid": "b" * 40}, ("rerun loopzero ready",)),
    ({"labels": [{"name": "hold"}]}, ("rerun loopzero ready",)),
    ({"baseRefName": "release"}, ("rerun loopzero ready",)),
    (None, ("run loopzero check at this head", cli.OWNER_ROUTE)),
], ids=["head", "hold", "target", "lost-receipt"])
def test_ready_rechecks_the_live_pr_before_the_first_activation(wt, gh, capsys, head, monkeypatch, change, diagnostic):
    arm_approved_pr(gh, head, wt)
    if change is None:
        pr = github.PR(7, URL, head, "main", True, "OPEN", "MERGEABLE")

        def snapshot(*_args, **_kwargs):
            (wt / ".loopzero/checks.json").unlink()
            return pr, github.Readiness(True, ())
        monkeypatch.setattr(cli, "_pr_readiness", snapshot)
    else:
        gh.respond_pr_view(pr_json(**{"headRefOid": head, **change}))
    expect_cli(capsys, 1, "ready", stderr_contains=diagnostic)
    assert not any(is_ready_call(call) for call in gh.calls)


@pytest.mark.parametrize("command", ["ready", "merge"])
@pytest.mark.parametrize("condition,reason", [
    *((case, "run loopzero check at this head") for case in ("missing", "during-gates", "transient")),
    ("head", "check receipt is for aaaaaaaaaaaa."),
    ("failed", "check receipt records a failed stage."), ("dirty", "check receipt was recorded in a dirty worktree."),
    ("plan", "check receipt's plan differs from the trusted workflow."),
    ("legacy", "check receipt is missing its settings fingerprint. Rerun `loopzero check`"),
    ("incompatible", "check receipt has an incompatible settings fingerprint. Rerun `loopzero check`"),
    ("empty", "check receipt is incomplete."), ("checkpoint", "check receipt is incomplete."),
])
def test_delivery_requires_current_receipt(wt, gh, capsys, head, monkeypatch, command, condition, reason):
    head = commit_mergify_configuration(wt) if command == "merge" else head
    monkeypatch.setattr(mergify, "configured", lambda *_: condition == "during-gates" or pytest.fail("remote gate read"))
    arm_approved_pr(gh, head, wt, isDraft=command == "ready")
    commit_lookup = f"api repos/{REPO}/commits/{head}/pulls --method GET"
    associated = dict(rest_pr_json(pr_json(headRefOid=head)), created_at="2026-10-06T00:00:00Z")
    gh.respond(commit_lookup, [associated])
    path = wt / ".loopzero/checks.json"
    report = json.loads(path.read_text())
    if condition == "during-gates":
        readiness = github.readiness

        def replace_receipt(*args, **kwargs):
            path.unlink()
            return readiness(*args, **kwargs)

        monkeypatch.setattr(github, "readiness", replace_receipt)
    elif condition in {"missing", "transient"}:
        path.unlink()
        if condition == "transient":
            monkeypatch.setattr(github, "_retry_sleep", lambda _: None)
            gh.respond(commit_lookup, {"stdout": "", "exit_code": 1, "stderr": "HTTP 503"}, [associated])
    else:
        results = report["results"]
        report.update({
            "head": {"head": "a" * 40}, "dirty": {"dirty": True},
            "failed": {"results": [dict(results[0], exit_code=1), *results[1:]]},
            "plan": {"results": results[:-1]}, "empty": {"results": []},
            "legacy": {"settings_sha256": None}, "incompatible": {"settings_sha256": "0" * 64},
            "checkpoint": {"results": [results[0], asdict(CheckResult("<checks incomplete>", 1, 0, ""))]},
        }[condition])
        path.write_text(json.dumps(report))
    out, err = expect_cli(capsys, 1, command, *(["--wait"] if command == "merge" or condition == "during-gates" else []))
    assert reason in out + err and "receipt accepted:" not in out
    assert ("owner queue route applies, post its one reason" in out) == (command == "ready")
    assert all(github._read_only(tuple(c["argv"])) for c in gh.calls)
    if condition != "during-gates":
        expected = [commit_lookup.split()[:2]] * (2 if condition == "transient" else 1) + [LOOKUP_CALLS[-1]]
        assert [c["argv"][:2] for c in gh.calls] == expected


@pytest.fixture
def check_environment(wt, gh, head, tmp_path):
    arm_approved_pr(gh, head, wt, readiness={"local_exit": None}, isDraft=False)
    path = tmp_path / "check-policy.toml"
    policy = WORKFLOW.replace('"echo ok"', json.dumps('test "$CHECK_POLICY" = old'))
    return path, policy, ("--config", str(path))


@pytest.mark.parametrize("case", ["explicit", "ready", "merge", "fresh", "inherited", "fixed"])
def test_ready_checks_effective_receipt_environment(wt, gh, capsys, head, monkeypatch, case, check_environment):
    config_path, config, args = check_environment
    inherited = case in {"inherited", "fixed"}
    if inherited:
        config = config.replace("[checks]", '[checks]\nenv_allowlist = ["PATH", "CHECK_POLICY"]')
        monkeypatch.setenv("CHECK_POLICY", "old")
    if case != "inherited":
        config = config.replace("[checks]", '[checks]\nenv = { CHECK_POLICY = "old" }')
    config_path.write_text(config)
    expect_cli(capsys, 0, *args, "check")
    expect_cli(capsys, 0, *args, "ready")

    if inherited:
        monkeypatch.setenv("CHECK_POLICY", "new")
    else:
        new_config = config.replace('CHECK_POLICY = "old"', 'CHECK_POLICY = "new"')
        if case in {"ready", "merge", "fresh"}:
            arm_check_progress(gh, head, "pending")
            command = "merge" if case == "fresh" else case

            def change_policy(_seconds):
                config_path.write_text(new_config)
                if case == "fresh":
                    cfg = cli._load_config(SimpleNamespace(config=config_path, command=command), wt)
                    cli._save_report(wt, cli.dataclasses.replace(cli._load_report(wt),
                                     settings_sha256=cli.sandbox.settings_fingerprint(cfg)))

            monkeypatch.setattr(cli, "_sleep", change_policy)
            hint = "retry with the current settings" if case == "fresh" else "run loopzero check"
            expect_cli(capsys, 1, *args, command, "--wait", stderr_contains=(f"workflow settings changed while waiting; {hint}",))
            arm_check_progress(gh, head, "success")
        else:
            config_path.write_text(new_config)
    passing = case in {"fixed", "fresh"}
    out, _ = expect_cli(capsys, int(not passing), *args, "ready")
    assert passing or "not ready: check receipt has an incompatible settings fingerprint." in out
    if not (passing or inherited):
        expect_cli(capsys, 1, *args, "check")


@pytest.mark.parametrize("change,exit_code", [("plain", 0), ("unrelated", 0), ("order", 0), ("allowlist", 0), ("policy", 1), ("slow", 3)])
def test_ready_receipt_after_base_advance(wt, repo, gh, head, capsys, monkeypatch, tmp_path, change, exit_code):
    env = 'CHECK_A = "first", CHECK_B = "second"'
    policy = WORKFLOW.replace("[checks]", f"[checks]\nenv = {{ {env} }}")
    policy = policy.replace("[delivery]", '[checks.inputs]\n"*" = ["."]\n\n[delivery]')
    if change != "plain":
        publish_workflow(wt, policy)
    arm_approved_pr(gh, head, wt, "pending", isDraft=False, readiness={"local_exit": None})
    expect_cli(capsys, 0, "check")
    external = tmp_path / "external"
    git(tmp_path, "clone", "-q", "--branch", "main", str(repo.parent / "origin.git"), str(external))
    clock, original = poll_clock(monkeypatch, jitter=1.0), cli._proc.run

    def fetch(argv, *, timeout, **kwargs):
        if argv[:2] == ["git", "fetch"] and clock.now:
            clock.fetch_timeout = timeout
            if change == "slow":
                clock.now += timeout
                raise cli._proc.ProcTimeout("origin timed out")
            edits = {
                "policy": policy.replace('CHECK_A = "first"', 'CHECK_A = "changed"'),
                "order": policy.replace(env, ", ".join(reversed(env.split(", ")))),
                "allowlist": policy.replace("[checks]", '[checks]\nenv_allowlist = ["TERM", "LC_ALL", "LANG", "HOME", "PATH", "PATH"]'),
            }
            commit_file(external, "workflow.toml" if change in edits else "unrelated.txt", edits.get(change, ""), "advance main")
            git(external, "push", "-q", "origin", "main")
            arm_check_progress(gh, head, "success")
        return original(argv, timeout=timeout, **kwargs)

    monkeypatch.setattr(cli._proc, "run", fetch)
    assert run(capsys, "ready", "--wait=60")[0] == exit_code and clock.fetch_timeout == 60 - clock.sleeps[0]


@pytest.mark.parametrize("prior_hash", [None, "55d37760fc170d550b75a2d25d8e1c5bee0e519724b2c1b19aeda5a2713bbb47"])
def test_ready_accepts_receipt_after_forwarding_worker_cpu_grant(wt, capsys, monkeypatch, check_environment, prior_hash):
    path, _, args = check_environment
    monkeypatch.delenv("ZZ_LZ_UNUSED", raising=False)
    allowlist = ("HOME", "PATH", "LANG", "LC_ALL", "TERM", "ZZ_LZ_UNUSED")
    policy = WORKFLOW.replace("[checks]", "[checks]\nenv_allowlist = LZ_ALLOWLIST")
    path.write_text(policy.replace("LZ_ALLOWLIST", json.dumps(allowlist)))
    expect_cli(capsys, 0, *args, "check")
    if prior_hash:
        cli._save_report(wt, cli.dataclasses.replace(cli._load_report(wt), settings_sha256=prior_hash))
    monkeypatch.setenv("HEAVY_SLOT_GRANTED_CORES", "2")
    path.write_text(policy.replace("LZ_ALLOWLIST", json.dumps((*allowlist, "HEAVY_SLOT_GRANTED_CORES"))))
    expect_cli(capsys, 0, *args, "ready")


@pytest.mark.parametrize("worker", ["legacy", "different-environment", "matching"])
def test_ready_checks_transferred_whole_job_receipt(wt, gh, capsys, head, tmp_path, monkeypatch, worker):
    policy = WORKFLOW.replace(
        '[checks]', '[checks]\nenv_allowlist = ["PATH", "LANG", "LC_ALL", "TERM", "HEAVY_SLOT_GRANTED_CORES"]')
    publish_workflow(wt, policy.replace('[delivery]', '[checks.inputs]\n"*" = ["."]\n\n[delivery]'))
    arm_approved_pr(gh, head, wt, readiness={"local_exit": None}, isDraft=False)
    with monkeypatch.context() as worker_env:
        if worker == "different-environment":
            worker_env.setenv("PATH", str(tmp_path / "worker-bin"), prepend=":")
            for name, value in {"LANG": "en_US.UTF-8", "LC_ALL": "en_US.UTF-8",
                                "TERM": "dumb", "HEAVY_SLOT_GRANTED_CORES": "2"}.items():
                worker_env.setenv(name, value)
        assert run(capsys, "check")[0] == 0

    if worker == "legacy":
        receipt = wt / ".loopzero/checks.json"
        payload = json.loads(receipt.read_bytes())
        del payload["settings_sha256"]
        receipt.write_text(json.dumps(payload))
    assert run(capsys, "ready")[0] == int(worker == "legacy")
    assert run(capsys, "check")[0] == 0
    assert all(bool(stage.reused_from) == (worker == "matching") for stage in cli._load_report(wt).results)
    assert run(capsys, "ready")[0] == 0


@pytest.mark.parametrize("state", ["forged", "quoted", "DISMISSED", "PENDING", "COMMENTED"])
def test_ready_respects_latest_review_state(wt, gh, capsys, state, head):
    reviews = [rev(head, "primary"), rev(head, "delta", state=state, verdict="request_changes")]
    if state == "forged":
        reviews = [rev(head, "primary", login="stranger"), rev(head, "primary", commit="1" * 40)]
    elif state == "quoted":
        reviews[-1].update(state="COMMENTED", body=f"{marker(head, 'delta')}\n\nProse quoted **approve**.")
    arm_approved_pr(gh, head, wt, reviews=reviews)
    ready = state == "COMMENTED"
    out, _ = expect_cli(capsys, 0 if ready else 1, "ready")
    assert without_accepted_receipt(out) == (f"ready: {URL}\n" if ready else
                                             "not ready: no review recorded for this head. Run `loopzero review`.\n")
    if state == "forged":
        assert sum(c["argv"][:2] == ["api", "graphql"] for c in gh.calls) == 1


def open_pr_snapshot(head):
    return rest_pr_json(pr_json(headRefOid=head, isDraft=False))


def landed_snapshot(head, sha="e" * 40):
    return merge_json(state="MERGED", head=head, sha=sha)


def arm_landed_pr(gh, head, sha, *, landed_head=None, branch="lz/t1", number=7):
    arm_pr(gh, head, branch=branch, number=number, state="MERGED", isDraft=False)
    gh.append(f"api repos/{REPO}/pulls/{number}", merge_json(state="MERGED", head=landed_head or head, sha=sha))


@pytest.mark.parametrize("command,conclusion,severity", [
    ("ready", "skipped", "important"), ("merge", "failure", "critical"),
])
def test_open_finding_blocks_delivery(wt, gh, capsys, head, command, conclusion, severity):
    finding = _finding_thread("0b9b59e7", head)
    comment = finding["comments"]["nodes"][0]
    comment["body"] = comment["body"].replace("important", severity)
    arm_approved_pr(
        gh, head, wt, conclusion, isDraft=command == "ready", readiness={"threads": (finding,)},
    )
    reason = f"open {severity} finding at src/a.py:3: Off by one. Run `loopzero resolve` to list finding IDs and address the review."
    out, err = expect_cli(capsys, 1, command)
    if command == "ready":
        assert f"not ready: {reason}" in out and err == ""
    else:
        assert without_accepted_receipt(out) == ""
        assert f"not ready to merge: {reason}; required check 'checks' is failure{FAILED_CHECK_HINT}" in err
    assert wt.exists()
    assert all(c["argv"][:2] != ["pr", command] for c in gh.calls)


def lose_native_merge_reply(monkeypatch):
    original = github._gh

    def transport(*args, **kwargs):
        result = original(*args, **kwargs)
        if args[:2] == ("pr", "merge"):
            raise github.ProcTimeout("lost native merge reply", "")
        return result

    monkeypatch.setattr(github, "_gh", transport)


@pytest.mark.parametrize("lost_reply", [False, True])
@pytest.mark.parametrize("branch,changed", [
    *((branch, False) for branch in TASK_BRANCHES), ("lz/t1", True),
])
def test_merge_prints_sha_and_cleans_up(
    wt: Path, repo: Path, gh: FakeGh, capsys, changed, head, branch, monkeypatch, lost_reply
) -> None:
    git(wt, "branch", "-m", branch)
    arm_squash_merge(gh, head, wt, merge_json(state='MERGED', head='f' * 40 if changed else head_of(wt), sha='c' * 40), branch=branch)
    gh.respond("api -X", "")
    if lost_reply:
        lose_native_merge_reply(monkeypatch)
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
    git(primary, "remote", "add", "origin", str(primary))
    linked = cli.worktree.start(primary, "bare", "main")
    monkeypatch.chdir(linked)
    head = head_of(linked)
    git(primary, "update-ref", "refs/remotes/origin/main", head)
    git(primary, "symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/main")
    arm_landed_pr(gh, head, "c" * 40, branch="lz/bare")
    gh.respond("api -X", "")
    if state == "dirty":
        (linked / "README.md").write_text("uncommitted work\n")
    original, cwds = cli._git, []  # #439: no cleanup git in a primary checkout
    monkeypatch.setattr(cli, "_git", lambda cwd, *args, **kwargs: cwds.append(Path(cwd).resolve()) or (
        f"worktree {repo}\0" if state == "wrong_primary" and args == ("worktree", "list", "--porcelain", "-z")
        else original(cwd, *args, **kwargs)))

    code, out, err = run(capsys, "merge")
    assert {primary.resolve(), repo.resolve()}.isdisjoint(cwds)

    assert code == 0 and out.endswith(f"{'c' * 40}\n") and "cd " not in out, (out, err)
    if state == "wrong_primary":
        assert "warning: merged but worktree cleanup failed:" in err
        assert "does not own this linked worktree" in err
    else:
        assert ("worktree is dirty" in err) if state == "dirty" else not err
    assert linked.exists() is (state != "clean")
    assert bool(git(primary, "branch", "--list", "lz/bare")) is (state != "clean")
    assert "bare" in git(primary, "worktree", "list", "--porcelain")


@pytest.mark.parametrize("strategy,wait,already_landed,app_auth", [
    ("squash", True, False, False), ("squash", True, True, False),
    ("mergify", True, False, False), ("mergify", False, False, False),
    pytest.param("squash", True, False, True, id="app-auth-publisher-lookup"),
])
def test_merge_waits_for_pending_checks(
    wt, gh, capsys, monkeypatch, queue_requests, app_token_helper, strategy, wait, already_landed, app_auth,
):
    if app_auth:
        app_token_helper()
    if strategy == "mergify":
        head = arm_mergify_pr(wt, gh, "pending")
        stub_mergify_admission(monkeypatch)
        monkeypatch.setattr(cli, "_wait_for_mergify", lambda *_: "c" * 40)
    else:
        head = head_of(wt)
        arm_approved_pr(gh, head, wt, "pending", isDraft=False)
        gh.respond("pr merge", "")
    arm_check_progress(gh, head, "pending", "pending", "success")
    opened = open_pr_snapshot(head)
    landed = landed_snapshot(head, "c" * 40)
    snapshots = ({**opened, **landed, "head": opened["head"]}, landed) if already_landed else (opened, opened, opened, landed)
    gh.append(f"api repos/{REPO}/pulls/7", *snapshots)
    gh.respond("api -X", "")
    monkeypatch.setattr(cli, "_sleep", lambda _: None)

    if not wait:
        expect_cli(capsys, 1, "merge",
                   stderr_contains=("required check 'checks' is pending; next: loopzero merge --wait",))
        assert not queue_requests
        return
    out, _ = expect_cli(capsys, 0, "merge", "--wait", stderr="", stdout_endswith="c" * 40 + "\n")
    if already_landed:
        assert "was already merged" in out
        assert all(c["argv"][:2] != ["pr", "merge"] for c in gh.calls)
    else:
        assert "waiting: required check 'checks' is pending" in out
    assert queue_requests == ([(REPO, 7, head, "main")] if strategy == "mergify" else [])
    assert not wt.exists()
    if app_auth:
        assert sum(any("viewer { login }" in arg for arg in c["argv"]) for c in gh.calls) == 0  # publishers come from the trusted workflow, not a login lookup


@pytest.mark.parametrize("command,slow", [("merge", False), ("merge", True), ("ready", True)])
def test_source_wait_times_out_on_pending_checks(wt, gh, capsys, monkeypatch, head, command, slow):
    arm_approved_pr(gh, head, wt, "pending", isDraft=False)
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
            clock.now += 2.0
        return run_gh(argv, timeout=timeout, **kwargs)

    if slow:
        monkeypatch.setattr(github, "run", slow_check)
    out, _ = expect_cli(capsys, 3, command, "--wait=16" if slow else "--wait=0", stderr="")
    assert budgets == (pytest.approx([4.0]) if slow else [])
    assert f"timed out waiting for required checks on {head[:12]}. Run `loopzero {command} --wait` to resume on this head." in " ".join(out.splitlines())
    assert wt.exists() and all(c["argv"][:2] != ["pr", "merge"] for c in gh.calls)


@pytest.mark.parametrize("strategy", ["squash", "mergify"])
@pytest.mark.parametrize("ready", [True, False], ids=["green", "terminal-failure"])
def test_merge_wait_rejects_verdicts_after_deadline(wt, gh, capsys, monkeypatch, strategy, ready):
    if strategy == "mergify":
        commit_mergify_configuration(wt)
        monkeypatch.setattr(mergify, "configured", lambda *_: True)
        if ready:
            monkeypatch.setattr(mergify, "membership", lambda *_: None)
        monkeypatch.setattr(mergify, "request", lambda *_: pytest.fail("queued after the deadline"))
    arm_approved_pr(gh, head_of(wt), wt, "pending", isDraft=False)
    clock = poll_clock(monkeypatch, start=1000.0, patch_sleep=False)
    failed = "required check 'checks' is failure"

    def check_after_deadline(_wt, _config, pr, *_args, **_kwargs):
        clock.now += 11
        return pr, github.Readiness(ready=ready, reasons=() if ready else (failed,)), False

    monkeypatch.setattr(cli, "_wait_for_checks", check_after_deadline)
    expect_cli(capsys, 3, "merge", "--wait=10", stdout_contains=("timed out waiting for required checks",), stderr="")
    assert wt.exists()
    assert all(c["argv"][:2] != ["pr", "merge"] for c in gh.calls)


@pytest.mark.parametrize("pending", [False, True])
@pytest.mark.parametrize("safe", [False, True])
def test_merge_uses_one_queue_proof_for_admission(
    wt, gh, capsys, monkeypatch, queue_requests, source_queue_api, pending, safe,
):
    head = arm_mergify_pr(wt, gh, "pending" if pending else "success")
    repo, config_path, vendor_calls = source_queue_api
    before_final = 0

    set_unsafe = partial(gh.respond, f"api {config_path}", {"type": "symlink"})

    def after_sleep():
        nonlocal before_final
        before_final = len(gh.calls)
        vendor_calls.clear()
        arm_check_progress(gh, head, "success")
        if not safe:
            set_unsafe()

    if not pending and not safe:
        set_unsafe()
    clock = poll_clock(monkeypatch, after_sleep=after_sleep)
    stub_mergify_admission(monkeypatch, configured=None)
    monkeypatch.setattr(cli, "_wait_for_mergify", lambda *_: None)
    _, err = expect_cli(capsys, 3 if safe else 1, "merge", "--wait=90")
    assert (clock.now > 0) == pending
    assert queue_requests == ([(REPO, 7, head, "main")] if safe else [])
    assert ("Mergify queue configuration is unverified" in err) == (not safe)
    paths = [call["argv"][1] for call in gh.calls[before_final:]]
    assert paths.count(repo) == int(safe)
    assert paths.count(config_path) == (2 if safe else 1)
    assert vendor_calls == ["/merge-queue/status?branch=main", "/queues/configuration"]


@pytest.mark.parametrize("wait", [False, True])
@pytest.mark.parametrize("outcome", ["open", "changed", "closed", "invalid", "merged"])
def test_mergify_admission_rechecks_only_the_selected_pr(wt, gh, capsys, monkeypatch, wait, outcome):
    head = arm_mergify_pr(wt, gh)
    detail = f"api repos/{REPO}/pulls/7"
    merged_sha = "e" * 40
    snapshots = {
        "open": open_pr_snapshot(head),
        "changed": merge_json(head="f" * 40),
        "closed": merge_json(state="CLOSED", head=head),
        "invalid": {"state": "open", "head": {"sha": head}},
        "merged": merge_json(state="MERGED", head=head, sha=merged_sha),
    }
    monkeypatch.setattr(mergify, "configured", lambda *_: True)
    monkeypatch.setattr(mergify, "queue_attempt", lambda *_: mergify.QueueAttempt(True, outcome == "open"))

    def membership(*_):
        gh.append(detail, snapshots[outcome])
        return mergify.Membership()

    monkeypatch.setattr(mergify, "membership", membership)
    monkeypatch.setattr(mergify, "mark_confirmed", lambda *_: pytest.fail("unexpected confirmation"))
    monkeypatch.setattr(mergify, "request", lambda *_: pytest.fail("unexpected request"))
    monkeypatch.setattr(cli, "_wait_for_mergify", lambda *_: None)
    gh.respond("api -X", "")

    code, out, err = run(capsys, "merge", *(["--wait=60"] if wait else []))

    calls = [c["argv"][:2] for c in gh.calls]
    assert calls.count(LOOKUP_CALLS[0]) == 0
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
    arm_mergify_pr(wt, gh, "pending")
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
        assert err == "" and out.endswith("timed out before requesting Mergify for PR #7\nRun `loopzero merge --wait` to resume on this head.\n")
    else:
        assert budgets == [4.0]


@pytest.mark.parametrize("removed,error,hint", [
    (False, "worktree is dirty",
     "; fix the cause, then rerun `loopzero merge` in this worktree to finish cleanup"),
    (True, "cannot lock ref",
     "; worktree removed; delete local branch 'lz/t1' if it still points at the merged head"),
], ids=["worktree-remains", "branch-remains"])
def test_merge_warns_when_cleanup_fails(wt, gh, capsys, monkeypatch, head, removed, error, hint):
    arm_squash_merge(gh, head, wt, landed_snapshot(head, "d" * 40))
    gh.respond("api -X", "")

    def fail_cleanup(path, _expected_head):
        if removed:
            shutil.rmtree(path)
        raise cli.worktree.WorktreeError(error)

    monkeypatch.setattr(cli.worktree, "cleanup", fail_cleanup)
    out, err = expect_cli(capsys, 0, "merge")
    assert out.splitlines()[-1] == "d" * 40
    assert err.startswith(f"warning: merged but worktree cleanup failed: {error}")
    assert err.rstrip().endswith(hint)


def test_cleanup_in_primary_checkout_skips_unfollowable_retry(repo: Path, capsys) -> None:
    cli._cleanup(repo, SimpleNamespace(head_sha="a" * 40))
    assert capsys.readouterr().err == "note: merged; this is the primary checkout, nothing to clean up\n"
    assert repo.exists()


def test_cleanup_hints_name_branch_and_configured_repo_without_commands(tmp_path, capsys, monkeypatch):
    branch = "lz/fix$version"
    monkeypatch.setattr(cli, "_repo_root", lambda _wt: tmp_path)
    monkeypatch.setattr(cli.worktree, "branch", lambda _wt: branch)
    monkeypatch.setattr(cli.worktree, "require_started", lambda *_: None)
    monkeypatch.setattr(cli.worktree, "cleanup", Mock(side_effect=cli.worktree.WorktreeError("cannot lock ref")))
    cli._cleanup(tmp_path / "gone", SimpleNamespace(head_sha="a" * 40))
    assert capsys.readouterr().err.rstrip().endswith(
        "delete local branch 'lz/fix$version' if it still points at the merged head")

    monkeypatch.setattr(cli.github, "delete_remote_branch", Mock(side_effect=cli.github.GhError(("gh", "api"), "HTTP 500")))
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

    arm_landed_pr(gh, head, "e" * 40)
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
    arm_landed_pr(gh, head, landed_sha)
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


@pytest.mark.parametrize("cleanup_failure", ["denied", "timeout", "unwritable"])
def test_second_merge_retries_remote_branch_cleanup(wt, repo, gh, capsys, monkeypatch, head, cleanup_failure):
    arm_squash_merge(gh, head, wt, landed_snapshot(head))
    arm_landed_pr(gh, head, "e" * 40)
    gh.respond(f"api repos/{REPO}/git/ref/heads/lz/t1", {"object": {"sha": head}})
    origin = repo.parent / "origin.git"
    git(wt, "push", "origin", "HEAD:refs/heads/lz/t1")
    monkeypatch.setattr(cli, "_cleanup", lambda *_: None)
    failures = {"timeout": cli._proc.ProcTimeout("gh api timed out", ""),
                "unwritable": PermissionError("cannot create cleanup temporary directory")}
    with monkeypatch.context() as first_merge:
        if failure := failures.get(cleanup_failure):
            first_merge.setattr(cli.github, "delete_remote_branch", Mock(side_effect=failure))
        else:
            git(origin, "config", "receive.denyDeletes", "true")
        out, err = expect_cli(capsys, 0, "merge")
    assert "remote branch cleanup failed for lz/t1" in err
    if failure:
        assert without_accepted_receipt(out).splitlines()[0] == "e" * 40
        assert str(failure) in err
        assert err.rstrip().endswith(f"; delete 'lz/t1' in {REPO} if it still points at the merged head")
    git(origin, "config", "receive.denyDeletes", "false")
    expect_cli(capsys, 0, "merge", stderr="")
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
        expect_cli(capsys, 1, "status", stderr_contains=("origin/main:workflow.toml: checks.commands must be an array",))


def test_status_rejects_scratch_path_naming_worktree_root(git_repo, monkeypatch, capsys):
    monkeypatch.chdir(git_repo)
    bad_config = git_repo / "bad.toml"
    bad_config.write_text('[repo]\nname = "example/repo"\n[checks]\nscratch = ["./"]\n')

    _, err = expect_cli(capsys, 1, "--config", "bad.toml", "status")
    assert "bad.toml: checks.scratch entries must be relative, got './'" in err

    bad_config.write_text(bad_config.read_text().replace('"./"', '"./.venv"'))
    assert config.load(bad_config).scratch == ("./.venv",)


def test_status_full(wt: Path, gh: FakeGh, capsys, git_calls) -> None:
    head = head_of(wt)
    gh.respond_pr_list([])
    out, _ = expect_cli(capsys, 0, "status")
    assert "branch:   lz/t1" in out and f"head:     {head}" in out and "dirty:    no" in out
    assert "pr:       none (commit changes, then open with `loopzero pr` before a long check)" in out
    arm_approved_pr(gh, head, wt, "pending")
    (wt / "scratch").write_text("x")
    git_calls.clear()
    out, _ = expect_cli(capsys, 0, "status", stdout_contains=("dirty:    yes",))
    assert_one_workflow_read(git_calls)
    assert_contains(out, f"pr:       #7 {URL} (draft, OPEN)", f"review:   primary on {head[:12]} (approved)")
    assert "ready:    no" in out and "  - required check 'checks' is pending" in out
    assert sum(1 for c in gh.calls if c["argv"][:2] == reviews_key().split()[:2]) == 1
    (wt / "scratch").unlink()


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


def test_gh_errors_are_one_line_on_stderr(wt: Path, gh: FakeGh, capsys) -> None:
    gh.fail(pr_list_key(), "To get started with GitHub CLI, please run:  gh auth login\n", exit=4)
    out, err = expect_cli(capsys, 1, "status", stderr_startswith="loopzero status: gh api", stderr_contains=("gh auth login",))
    assert out.count("\n") >= 4 and err.count("\n") == 1


@pytest.mark.parametrize("command", ["ready", "merge"])
@pytest.mark.parametrize("budget", ["-1", "NaN", "inf", "-inf"])
def test_invalid_wait_does_not_read_pr_state(wt, gh, capsys, command, budget):
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
        git(wt, "push", "-q", "origin", f"origin/main:refs/heads/{base}")
        git(wt, "checkout", "-q", "-b", base)
    for message in ("configured base", "HEAD is detached"):
        for command in ("pr", "review", "ready", "merge"):
            expect_cli(capsys, 1, command, stdout="", stderr_contains=(message,))
        assert gh.calls == []
        if message == "configured base":
            git(wt, "checkout", "-q", "--detach")
    expect_cli(capsys, 1, "status", stdout="", stderr="loopzero status: HEAD is detached. Switch to a branch and retry.\n")


@pytest.mark.parametrize("command,closed", [
    *((command, False) for command in ("ready", "merge")),
    *((command, True) for command in ("review", "ready")),
])
def test_commands_refuse_closed_or_mismatched_pr(wt, gh, capsys, command, closed):
    if closed:
        arm_pr(gh, head_of(wt), state="CLOSED")
        expect_cli(capsys, 1, command, stderr_contains=("PR #7 is CLOSED", "Reopen a closed unmerged PR",))
    else:
        arm_pr(gh, "0" * 40, isDraft=False)
        expect_cli(capsys, 1, command, stdout="", stderr_contains=("push or pull first",))
        assert wt.exists() and all(c["argv"][:2] != ["pr", "merge"] and not is_ready_call(c) for c in gh.calls)


@pytest.mark.parametrize("case", ["owned", "external", "dirty", "unpushed"])
def test_merge_cleans_closed_pr_only_with_owned_clean_checkout_at_published_head(wt, repo, gh, capsys, case):
    head, branch = head_of(wt), "lz/t1"
    origin = repo.parent / "origin.git"
    git(wt, "push", "-q", "origin", f"HEAD:refs/heads/{branch}")
    arm_pr(gh, head, state="CLOSED")
    if case == "external":
        cli.worktree.task_file(wt).write_text("# Handwritten task\n")
    elif case == "dirty":
        (wt / "untracked").touch()
    elif case == "unpushed":
        git(wt, "commit", "--allow-empty", "-m", "local after close")
        gh.respond(f"api repos/{REPO}/commits/{head_of(wt)}/pulls?per_page=100&page=1", [])
    code, out, err = run(capsys, "merge", "--wait=0")
    assert (code, out) == (1, "")
    assert wt.exists() is (case != "owned")
    assert git(repo, "branch", "--list", branch)
    assert git(origin, "rev-parse", branch).strip() == head
    assert {"owned": "kept both branch refs", "external": "ownership is unverified",
            "dirty": "worktree is dirty", "unpushed": "push or pull first"}[case] in err
    assert all(call["argv"][:2] != ["api", "-X"] for call in gh.calls)


@pytest.mark.parametrize("changed", [False, True])
def test_merge_of_externally_merged_pr_verifies_sha_and_cleans_up(
    wt: Path, repo: Path, gh: FakeGh, capsys, changed, monkeypatch
) -> None:
    collision, wt = wt, wt.with_name(wt.name + " ")
    git(repo, "worktree", "move", str(collision), str(wt))
    git(repo, "worktree", "add", "-qb", "lz/other", str(collision), "lz/t1")
    monkeypatch.chdir(wt)
    for branch, number in (("lz/t1", 7), ("lz/other", 8)):
        arm_landed_pr(gh, head_of(wt), "e" * 40, landed_head="f" * 40 if changed else None,
                      branch=branch, number=number)
    out, err = expect_cli(capsys, int(changed), "merge")
    assert collision.exists() and wt.exists() is changed
    if changed:
        assert "head changed" in err
    else:
        assert not err and f"PR #7 was already merged as {'e' * 12}" in out and out.endswith("e" * 40 + "\n")


def native_queue(queued, head):
    return {"data": {"repository": {"pullRequest": {"isInMergeQueue": queued, "headRefOid": head}}}}


@pytest.mark.parametrize("lost_reply", [False, True])
def test_merge_queued_prints_and_keeps_worktree(wt, gh, capsys, head, monkeypatch, lost_reply):
    arm_squash_merge(gh, head, wt, merge_json(head=head))
    gh.respond("api graphql", threads_json(), native_queue(True, head))
    if lost_reply:
        lose_native_merge_reply(monkeypatch)
    expect_cli(capsys, 0, "merge", stderr="", stdout_contains=("queued for merge into main",),
               stdout_endswith="next: loopzero merge --wait follows the queue, verifies and cleans up\n")
    assert wt.exists()
    assert all(c["argv"][:2] != ["api", "-X"] for c in gh.calls)


@pytest.mark.parametrize("attested,merge_state", [
    *itertools.product((True, False), ("BEHIND", "CLEAN")), (True, "DRAFT"),
])
def test_mergify_merge_requests_once_for_behind_head(
    wt: Path, gh: FakeGh, capsys, monkeypatch, queue_requests, attested, merge_state, git_calls
) -> None:
    draft = merge_state == "DRAFT"
    head = arm_mergify_pr(wt, gh, mergeStateStatus="CLEAN" if draft else merge_state, isDraft=draft)
    stub_mergify_admission(monkeypatch, configured=attested)
    git_calls.clear()
    admits = attested and not draft
    out, err = expect_cli(capsys, int(not admits), "merge", *(["--wait=0"] if draft else []))
    assert git_calls.count(["branch", "--show-current"]) == 1
    assert queue_requests == ([(REPO, 7, head, "main")] if admits else [])
    rejection = "PR #7 is still a draft; next: loopzero ready --wait" if draft else "queue configuration is unverified"
    assert (err == "" and "requested from Mergify queue main" in out) if admits else rejection in err


@pytest.mark.parametrize(("accepted", "transient"), [(True, False), (False, False), (True, True)])
def test_overlapping_mergify_command_after_lost_reply(wt, gh, monkeypatch, accepted, transient):
    head = arm_mergify_pr(wt, gh)
    pr = github.PR(7, URL, head, "main", False, "OPEN", "MERGEABLE")
    settings = config.load_trusted(wt)
    monkeypatch.setattr(cli, "_context", lambda *_args, **_kwargs: (wt, settings))
    monkeypatch.setattr(cli, "_pr_readiness", lambda *_args, **_kwargs: (pr, github.Readiness(True, ())))
    monkeypatch.setattr(mergify, "membership", lambda *_: None)
    monkeypatch.setattr(github, "merged_sha", lambda *_args, **_kwargs: None)
    endpoint = f"repos/{REPO}/issues/7/comments"
    gh.respond(f"api {endpoint}", [])
    gh.respond(f"api {endpoint} --method POST", {})
    writing, overlap = Event(), Event()
    original_api = github._api

    def transport(path, payload, **kwargs):
        if path != endpoint:
            return original_api(path, payload, **kwargs)
        if not writing.is_set():
            writing.set()
            assert overlap.wait(10), "second merge did not reach admission"
            if accepted:
                original_api(path, payload, **kwargs)
                gh.respond(f"api {path}", [{"user": {"login": LOGIN}, "body": payload["body"]}])
            if transient:
                raise github.GhUnavailable(("GitHub API request",), "HTTP 502")
            raise github.ProcTimeout("lost Mergify command reply")
        return original_api(path, payload, **kwargs)
    monkeypatch.setattr(github, "_api", transport)
    merge = partial(cli.main, ["merge"])
    assert overlap_calls(monkeypatch, writing, overlap, merge) == (0 if accepted else 1, 0)
    assert len([c for c in gh.calls if "--input" in c and c["argv"][1] == endpoint]) == 1


@pytest.mark.parametrize("command", ["ready", "status"])
@pytest.mark.parametrize("merge_state", ["CLEAN", "BEHIND"])
@pytest.mark.parametrize("attested", [True, False])
def test_ready_allows_behind_only_after_configured_mergify_attestation(
    wt: Path, gh: FakeGh, capsys, monkeypatch, command, merge_state, attested
) -> None:
    arm_mergify_pr(wt, gh, mergeStateStatus=merge_state)
    monkeypatch.setattr(mergify, "configured", lambda *args: attested)

    gh.respond(f"api repos/{REPO}/issues/7/comments", [])
    monkeypatch.setattr(mergify, "membership", lambda *_: None)
    code, out, err = run(capsys, command)

    assert not err
    if command == "ready":
        assert code == int(not attested)
    else:
        assert f"ready:    {'yes' if attested else 'no'}" in out


@pytest.mark.parametrize("outcome", [
    "merged", "ejected", "landed-on-ejection", "head-changed-early", "head-changed-late",
    "closed-then-merged", "still-closed", "retargeted",
])
def test_merge_wait_preserves_reviewed_head_and_cleans_only_landed_work(
    wt: Path, gh: FakeGh, capsys, monkeypatch, head, outcome
) -> None:
    opened, closed = open_pr_snapshot(head), merge_json(state="CLOSED", head=head)
    landed = landed_snapshot(head)
    snapshots = {
        "merged": (opened, opened, landed),
        "ejected": (opened, opened, opened),
        "landed-on-ejection": (opened, opened, landed),
        "head-changed-early": (open_pr_snapshot("f" * 40), landed_snapshot("f" * 40)),
        "head-changed-late": (opened, landed_snapshot("f" * 40)),
        "closed-then-merged": (opened, closed, landed),
        "still-closed": (opened, closed, closed),
        "retargeted": (opened, {**opened, "base": {"ref": "release"}}),
    }
    arm_squash_merge(gh, head, wt, *snapshots[outcome])
    ejected = outcome in {"ejected", "landed-on-ejection"}
    gh.respond("api graphql", threads_json(), native_queue(True, head), native_queue(not ejected, head))
    gh.respond("api -X", "")
    clock = poll_clock(monkeypatch, jitter=1.0)
    wait = 0 if ejected else 60
    code, out, err = run(capsys, "merge", f"--wait={wait}")
    if outcome in {"closed-then-merged", "still-closed"}:
        assert clock.sleeps == [30]
    if outcome.startswith("head-changed"):
        assert code == 1 and "PR head changed" in err
        assert wt.exists(), "no cleanup after an unreviewed head"
    elif outcome == "retargeted":
        assert code == 1 and "retargeted to release" in err and "rerun" in err and wt.exists()
    elif outcome == "still-closed":
        assert code == 1 and "closed without a verified merge" in err and wt.exists()
    elif ejected:
        landed = outcome == "landed-on-ejection"
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


ELIGIBILITY_DISPATCH = f"repos/{REPO}/actions/workflows/eligibility.yml/dispatches"
ELIGIBILITY_REQUESTED = "Requested a Loop-zero Eligibility refresh for PR #7. Run `loopzero ready --wait` to follow the verdict."
REPLY_ACCEPTED = {"data": {"addPullRequestReviewThreadReply": {"comment": {"id": "C1"}}}}
THREAD_RESOLVED = {"data": {"resolveReviewThread": {"thread": {"isResolved": True}}}}


@pytest.mark.parametrize("outcome", ["refreshed", "remaining", "denied", "applied", "retry", "unknown", "missing", "missing-required"])
def test_resolve_lists_then_replies_and_resolves_one_finding(wt, finding_pr, gh: FakeGh, capsys, monkeypatch, outcome) -> None:
    if outcome == "missing-required":
        publish_workflow(wt, WORKFLOW.replace('required_ci = ["checks"]', 'required_ci = ["Loop-zero Eligibility"]'))
    sys.stdout.reconfigure(encoding="ascii" if outcome == "refreshed" else "utf-8", errors="strict")
    finding_pr["comments"]["nodes"][0]["body"] = finding_pr["comments"]["nodes"][0]["body"].replace("Off by one", "Łódź")
    listing, backoffs = threads_json({**finding_pr, "path": "src/Łódź.py"}), []
    gh.respond("api graphql", listing, listing, REPLY_ACCEPTED, THREAD_RESOLVED,
               listing if outcome == "remaining" else threads_json())
    transient = outcome in {"applied", "retry", "unknown"}
    error_status = 503 if transient else {"denied": 403, "missing": 404, "missing-required": 404}.get(outcome)
    gh.respond("api -X", {"stdout": "", "exit": 1, "stderr": f"HTTP {error_status}"} if error_status else "", "")
    monkeypatch.setattr(github, "_retry_sleep", backoffs.append)
    runs = [] if outcome == "retry" else [{"display_title": "any title"}]
    monkeypatch.setattr(github.time, "time", lambda: 1795640554)
    endpoint = ELIGIBILITY_DISPATCH.removesuffix("/dispatches")
    gh.respond(f"api {endpoint}/runs?event=workflow_dispatch&branch=main&created=%3E%3D2026-11-25T21%3A02%3A34%2B00%3A00&per_page=100", {"workflow_runs": runs, "total_count": len(runs) + (outcome == "unknown")})
    title = r"\u0141\xf3d\u017a" if outcome == "refreshed" else "Łódź"
    expect_cli(capsys, 0, "resolve", stderr="", stdout=f"0b9b59e7  important  src/{title}.py:3  {title}\n")
    assert not any("eligibility.yml/dispatches" in " ".join(c["argv"]) for c in gh.calls)
    code, out, err = run(capsys, "resolve", "0b9b59e7", "Fixed in abc1234: loop bound corrected.")
    assert out.splitlines()[0] == f"resolved 0b9b59e7: {title}"
    calls = [c["argv"] for c in gh.calls
             if c["argv"][:2] == ["api", "graphql"] and "viewer {" not in c["argv"][3]][2:]
    assert "addPullRequestReviewThreadReply" in calls[0][3] and "resolveReviewThread" in calls[1][3]
    assert "body=Fixed in abc1234: loop bound corrected." in calls[0] and "thread=THREAD_1" in calls[1]
    dispatches = [c["argv"] for c in gh.calls if ELIGIBILITY_DISPATCH in c["argv"]]
    assert dispatches == [["api", "-X", "POST", ELIGIBILITY_DISPATCH, "-f", "ref=main", "-f", "inputs[pr]=7", "--include"]] * {"remaining": 0, "retry": 2}.get(outcome, 1)
    assert backoffs == ([5] if transient else [])
    if outcome == "missing-required":
        assert code == 1 and "Check that eligibility.yml exists" in err and "before retrying" not in err
    elif outcome in {"denied", "unknown"}:
        assert code == 1 and "is resolved." in err and ("HTTP 403" if outcome == "denied" else "identify all recent refresh runs") in err
        assert f"gh api -X POST {ELIGIBILITY_DISPATCH} -f ref=main -f 'inputs[pr]=7'" in err
    else:
        assert (code, err) == (0, "")
        status = {"remaining": [], "missing": ["Hosted eligibility workflow was not found or could not be accessed. Finding resolution is complete."]}
        assert out.splitlines()[1:] == status.get(outcome, [ELIGIBILITY_REQUESTED])


@pytest.mark.parametrize("cut", ["reply", "resolution"])
def test_resolve_resumes_after_github_accepts_mutation(gh, finding_pr, capsys, monkeypatch, cut):
    node = finding_pr
    reply = "Fixed the loop bound."
    accepted = {"reply": 0, "resolution": 0}
    original_gh, original_json = github._gh, github._gh_json
    def read_or_reply(*args, **kwargs):
        if args[:2] != ("api", "graphql"):
            return original_json(*args, **kwargs)
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
            assert kwargs.get("token") == ""
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
    assert ELIGIBILITY_REQUESTED in out
    assert sum("eligibility.yml/dispatches" in " ".join(c["argv"]) for c in gh.calls) == 1
    assert run(capsys, "resolve") == (0, "", "")


@pytest.mark.parametrize("argv", [("resolve",), ("resolve", "0b9b59e7", "Fixed.")])
def test_resolve_refuses_a_pr_targeting_another_base(wt: Path, gh: FakeGh, capsys, head, argv) -> None:
    gh.respond_pr_list([pr_json(headRefOid=head, baseRefName="release")])
    expect_cli(capsys, 1, *argv, stdout="", stderr_contains=("PR #7 targets release, configured base is main",))
    assert not any("reviewThreads" in " ".join(c["argv"]) for c in gh.calls)


def test_resolve_and_ready_ignore_findings_from_non_publishers(wt: Path, gh: FakeGh, capsys, head) -> None:
    (wt / "workflow.toml").write_text(WORKFLOW_BASE + '\nreview_publishers = ["outsider"]\n')
    planted, genuine = _finding_thread("0b9b59e7", head), _finding_thread("1c2d3e4f", head)
    planted["comments"]["nodes"][0]["author"] = {"__typename": "User", "login": "outsider"}
    arm_approved_pr(gh, head, wt, readiness={"threads": (planted,)}, isDraft=False)
    expect_cli(capsys, 0, "resolve", stdout="", stderr="")
    expect_cli(capsys, 0, "ready")
    assert cli.eligibility.evaluate(REPO, 7, head, "main", (LOGIN,)).ready

    gh.respond("api graphql", threads_json(planted, genuine))
    expect_cli(capsys, 0, "resolve", stdout="1c2d3e4f  important  src/a.py:3  Off by one\n", stderr="")


@pytest.mark.parametrize("response", [
    {"data": {"resolveReviewThread": {"thread": {"isResolved": False}}}},
    {"data": {"resolveReviewThread": {"thread": {}}}},
    "invalid JSON", "failed-read",
])
def test_resolve_requires_confirmation_from_github(finding_pr, gh, capsys, response):
    listing = threads_json(finding_pr)
    failed_read = response == "failed-read"
    followup = (THREAD_RESOLVED, {"stdout": "", "stderr": "gh: Forbidden (HTTP 403)", "exit_code": 1})
    gh.respond("api graphql", listing, REPLY_ACCEPTED, *(followup if failed_read else (response, listing)))
    out, err = expect_cli(capsys, 1, "resolve", "0b9b59e7", "Fixed the loop bound.")
    if failed_read:
        assert "resolved 0b9b59e7" in out
        assert_contains(err, "Resolved finding 0b9b59e7 on PR #7", "could not refresh Loop-zero Eligibility",
                        "HTTP 403", "run `loopzero resolve`", "if no findings remain",
                        f"gh api -X POST {ELIGIBILITY_DISPATCH} -f ref=main -f 'inputs[pr]=7'")
        gh.respond("api graphql", threads_json())
        expect_cli(capsys, 0, "resolve", stdout="", stderr="")
    else:
        assert out == ""
        assert_contains(err, "reply posted to finding 0b9b59e7", "PR #7", "Retry loopzero resolve with the same reply")
        assert sum("addPullRequestReviewThreadReply" in " ".join(c["argv"]) for c in gh.calls) == 1
        expect_cli(capsys, 0, "resolve", stdout="0b9b59e7  important  src/a.py:3  Off by one\n", stderr="")
    assert not any(ELIGIBILITY_DISPATCH in c["argv"] for c in gh.calls)


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
        assert out.splitlines() == ["resolved 0b9b59e7: Off by one", ELIGIBILITY_REQUESTED]
    else:
        assert code == 1
        assert_contains(err, "reply posted to finding 0b9b59e7", "Retry loopzero resolve with the same reply")
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
    expect_cli(capsys, 1, "resolve", finding_id, reply, stderr_contains=(reason,))
    assert not any("resolveReviewThread" in a for c in gh.calls for a in c["argv"])


@pytest.mark.parametrize("args,code", [
    (("0b9b59e7",), 2), (("0b9b59e7", "  "), 2), (("--help",), 0),
])
def test_resolve_groups_arguments_before_loading_context(monkeypatch, capsys, args, code):
    monkeypatch.setattr(cli, "_context", lambda *_: pytest.fail("loaded context while parsing resolve"))
    with pytest.raises(SystemExit) as stop:
        cli.main(["resolve", *args])
    assert stop.value.code == code
    out, err = capsys.readouterr()
    assert 'resolve [<finding-id> "<reply>"]' in (err if code else out)


@pytest.mark.parametrize("failure,code", [(KeyError("headRefOid"), 1), (RuntimeError("broken"), 1), (KeyboardInterrupt, 130)])
def test_unexpected_and_interrupt_exit_codes(wt, capsys, monkeypatch, failure, code):
    def fail(*_):
        raise failure
    monkeypatch.setattr(cli.github, "pr_for_branch", fail)
    _, err = expect_cli(capsys, code, "status")
    if code == 1:  # a programming error names its location
        assert re.fullmatch(rf"loopzero status: unexpected error \({type(failure).__name__} at test_cli\.py:\d+\): {re.escape(str(failure))}\n", err)


@pytest.mark.parametrize("checkout", ["attached", "detached", "primary-task"])
def test_start_rejects_a_checkout_already_on_task_work(wt, repo, monkeypatch, capsys, checkout):
    if checkout == "primary-task":
        git(repo, "switch", "-q", "-c", "lz/stray")
        monkeypatch.chdir(repo)
    else:
        if checkout == "detached":
            git(wt, "switch", "-q", "--detach")
        monkeypatch.chdir(wt / ".loopzero")
    _, err = expect_cli(capsys, 1, "start", "nested", stdout="")
    assert not (repo / ".worktrees" / "nested").exists()
    if checkout == "primary-task":
        assert_contains(err, "switch it to another branch first", f"primary checkout {repo} is on task branch lz/stray")
        assert "run start from" not in err
    else:
        assert_contains(err, "already inside task worktree", f"run start from {repo}\n")
        assert not (wt / ".worktrees" / "nested").exists()
        assert "lz/nested" not in git(repo, "branch", "--list", "lz/nested")


@pytest.mark.parametrize("checkout", ["primary", "detached-primary", "primary-clone", "detached-harness"])
def test_start_uses_the_primary_repo_for_task_worktrees(repo, tmp_path, monkeypatch, capsys, checkout):
    if checkout == "primary-clone":
        repo = caller = shutil.copytree(repo, tmp_path / ".worktrees" / "project")
    elif checkout == "detached-harness":
        caller = tmp_path / "harness"
        git(repo, "worktree", "add", "-q", "--detach", str(caller))
    else:
        if checkout == "detached-primary":
            git(repo, "switch", "-q", "--detach")
        caller = repo / "subdir"
        caller.mkdir()
    monkeypatch.chdir(caller)
    task = repo / ".worktrees" / "nested"
    expect_cli(capsys, 0, "start", "nested", stdout=f"{task}\n", stderr="")
    assert (task / ".loopzero/task.md").exists()
    if checkout == "detached-harness":
        assert "lz/nested" in git(repo, "branch", "--list", "lz/nested")
        (task / "uncommitted.txt").write_text("work\n")
        monkeypatch.chdir(repo)
        git(repo, "worktree", "remove", str(caller))
        assert (task / "uncommitted.txt").read_text() == "work\n"


def test_reviews_are_paginated(wt: Path, gh: FakeGh, capsys, head) -> None:
    arm_approved_pr(gh, head, wt, reviews=[rev("f" * 40, "primary", login="x")] * 100)
    gh.respond(reviews_key(2), [rev(head, "primary")])
    expect_cli(capsys, 0, "ready", stdout_endswith=f"ready: {URL}\n")


@pytest.mark.parametrize("state,verdict,pr_update,expected", [
    ("APPROVED", "approve", {"mergeStateStatus": "BEHIND"}, "success"),
    ("COMMENTED", "request_changes", {"mergeStateStatus": "BEHIND"}, "success"),
    ("DISMISSED", "approve", {"mergeStateStatus": "BEHIND"}, "pending"),
    ("PENDING", "approve", {"mergeStateStatus": "BEHIND"}, "pending"),
    ("COMMENTED", "", {"mergeStateStatus": "BEHIND"}, "pending"),
    ("APPROVED", "approve", {"mergeable": "UNKNOWN"}, "pending"),
    (None, "", {}, "pending"),
    ("APPROVED", "approve", {"state": "MERGED"}, None),
])
def test_manual_eligibility_refresh_uses_live_pr_and_publisher(
    wt, gh, head, monkeypatch, state, verdict, pr_update, expected,
):
    delays = []
    monkeypatch.setattr(cli.eligibility, "sleep", delays.append)
    cfg = wt / ".loopzero" / "hosted.toml"
    cfg.write_text(WORKFLOW_BASE + f'\nreview_publishers = ["{LOGIN}"]\n')
    gh.respond_pr_view(pr_json(headRefOid=head, **pr_update))
    gh.respond("api graphql viewer", {"data": {"viewer": {"login": "ci-service"}}})
    gh.respond(reviews_key(), [rev(head, "primary"), rev(head, "delta", state=state, verdict=verdict)] if state else [])
    gh.respond("api graphql", threads_json())
    gh.respond(f"api repos/{REPO}/statuses/{head}", {})
    code = cli.eligibility.main(["--config", str(cfg), "--pr", "7", "--head", head, "--publish"])
    statuses = [json.loads(c["--input"]) for c in gh.calls if "--input" in c]
    assert [s["state"] for s in statuses] == (["pending", expected] if expected else [])
    assert delays == ([2, 4, 8] if pr_update.get("mergeable") == "UNKNOWN" else [])
    if expected:
        assert code == int(expected == "pending")
    if not state or pr_update.get("mergeable") == "UNKNOWN":
        reason = github.MERGEABILITY_PENDING if state else "Awaiting trusted review of current source head"
        assert statuses[-1]["description"] == reason
    if pr_update.get("mergeStateStatus") == "BEHIND":
        gh.respond_pr_view(pr_json(headRefOid="b" * 40))
        assert not cli.eligibility.evaluate(REPO, 7, head, "main", (LOGIN,)).ready


@pytest.mark.parametrize("publishers,pr_head,message", [
    ("", "a" * 40, "configure delivery.review_publishers"),
    ('review_publishers = ["x"]', "aaaaaaa", "full 40-character source SHA"),
    (f'review_publishers = ["{LOGIN}"]', "a" * 40, 'HTTP 404'),
])
def test_eligibility_main_reports_usage_and_github_errors(wt, gh, capsys, publishers, pr_head, message):
    cfg = wt / ".loopzero" / "hosted.toml"
    cfg.write_text(WORKFLOW_BASE + f"\n{publishers}\n")
    if message == "HTTP 404":
        gh.fail(f"api repos/{REPO}/pulls/7", 'HTTP 404: Not Found\n{"message": "Not Found"}')
    assert cli.eligibility.main(["--config", str(cfg), "--pr", "7", "--head", pr_head]) == 1
    out, err = capsys.readouterr()
    assert out == "" and message in err and "Traceback" not in err
    if message == "HTTP 404":
        assert len(err.splitlines()) == 1 and 'Not Found"}' in err


@pytest.mark.parametrize("publisher", ["stranger", "lz-bot"])
def test_ready_pins_review_publishers_to_base(wt, repo, gh, capsys, monkeypatch, publisher, head):
    policy = WORKFLOW_BASE.replace('required_ci = ["checks"]', 'required_ci = ["checks", "Loop-zero Eligibility"]')
    publish_workflow(wt, policy + '\nreview_publishers = ["stranger"]\n')
    commit_file(repo, "workflow.toml", policy + f'\nreview_publishers = ["{LOGIN}"]\n', "rotate publisher")
    git(repo, "push", str(repo.parent / "origin.git"), "main")  # preserve the retired local publisher
    (wt / "workflow.toml").write_text(policy + f'\nreview_publishers = ["{publisher}"]\n')
    arm_approved_pr(gh, head, wt, review_login=publisher, isDraft=False)
    gh.respond(f"api repos/{REPO}/commits/{head}/statuses", [{"context": "Loop-zero Eligibility", "state": "success"}])
    assert run(capsys, "ready")[0] == (0 if publisher == LOGIN else 1)
    event = repo / "event.json"
    event.write_text(json.dumps({"inputs": {"pr": "7"}}))
    monkeypatch.chdir(repo)
    monkeypatch.setenv("GITHUB_REPOSITORY", REPO)
    monkeypatch.setenv("GITHUB_EVENT_PATH", str(event))
    gh.respond(f"api repos/{REPO}/statuses/{head}", {})
    assert hosted.main() == 0
    assert json.loads(gh.calls[-1]["--input"])["state"] == ("success" if publisher == LOGIN else "pending")


def configured_mergify_pr(wt):
    head = commit_mergify_configuration(wt)
    return cli.config_mod.load_trusted(wt), github.PR(7, URL, head, "main", False, "OPEN", "MERGEABLE")


@pytest.mark.parametrize("kind", ["checks", "mergify"])
@pytest.mark.parametrize("reset", [None, -1, 20, 100])
def test_wait_restarts_the_poll_after_quota_reset(wt, monkeypatch, kind, reset):
    config, pr = configured_mergify_pr(wt)
    clock = poll_clock(monkeypatch, jitter=1.0)
    monkeypatch.setattr(cli.time, "time", lambda: clock.now)
    polls = []
    error = github.GhUnavailable(("gh",), "quota exhausted", rate_reset=reset)

    def next_poll(*_args, **_kwargs):
        polls.append(clock.now)
        return pr if kind == "checks" else ("e" * 40 if len(polls) == 2 else None)

    def gated(*_args, **_kwargs):
        if len(polls) == 1:
            raise error
        return github.Readiness(True, ())

    if kind == "checks":
        monkeypatch.setattr(github, "pr_view", next_poll)
        monkeypatch.setattr(cli, "_verify_wait_snapshot", lambda *_: config)
        monkeypatch.setattr(cli, "_readiness", gated)
        wait = partial(cli._wait_for_checks, wt, config, pr, 90, kicked=True, started=0,
                       args=SimpleNamespace(command="ready", config=None))
        expected = (pr, github.Readiness(True, ()), True)
    else:
        monkeypatch.setattr(cli, "_landed", next_poll)
        monkeypatch.setattr(mergify, "configured", gated)
        wait = partial(cli._wait_for_mergify, config, pr, 90, confirmed=True)
        expected = "e" * 40

    if reset == 20:
        assert wait() == expected
        assert polls == [0, 20]
        assert clock.sleeps == [20]
    else:
        with pytest.raises(github.GhUnavailable, match="quota exhausted"):
            wait()
        assert polls == [0]
        assert not clock.sleeps


@pytest.mark.parametrize("confirmed", [True, False])
def test_mergify_confirmed_wait_reads_pr_once_per_steady_poll(wt, gh, monkeypatch, confirmed):
    config, pr = configured_mergify_pr(wt)
    head = pr.head_sha
    detail = ["api", f"repos/{REPO}/pulls/7"]
    gh.respond(" ".join(detail), merge_json(state="OPEN", head=head))
    clock = poll_clock(monkeypatch, jitter=1.0)
    reads_at_membership = []
    configurations = []

    def membership(*_):
        reads_at_membership.append(sum(c["argv"][:2] == detail for c in gh.calls))
        if confirmed and clock.now == 30:
            gh.respond(" ".join(detail), landed_snapshot(head))
        return mergify.Membership() if confirmed or clock.now >= 210 else None
    monkeypatch.setattr(mergify, "configured", lambda *args: configurations.append(args) or True)
    gh.respond(f"api repos/{REPO}/issues/7/comments", [])
    monkeypatch.setattr(mergify, "mark_confirmed", lambda *_: gh.respond(" ".join(detail), landed_snapshot(head)))
    monkeypatch.setattr(mergify, "membership", membership)
    assert cli._wait_for_mergify(config, pr, 90 if confirmed else 360, confirmed=confirmed) == "e" * 40
    assert clock.sleeps == ([30, 55] if confirmed else [30, 60, 120, 30])
    assert set(configurations) == {(REPO, "main", "main", 90 if confirmed else 360)}
    assert len(configurations) == len(reads_at_membership) > 1
    assert all(current - previous == (1 if confirmed else 2)
               for previous, current in itertools.pairwise(reads_at_membership))


@pytest.mark.parametrize("outcome,jitter", [
    *((outcome, 1.0) for outcome in (
        "merged", "worktree_removed", "ejected", "closed_then_merged", "still_closed", "head_changed",
        "timeout", "incompatible_landed", "incompatible_absent", "incompatible_unverified", "api_error",
        "unsafe_config", "config_error", "merged_head_changed", "ejection_head_changed", "already_merged",
        "already_merged_changed", "unsafe_pending", "requested_head_changed",
    )),
    ("worktree_removed", 0.9), ("timeout", 0.9),
])
def test_mergify_wait_outcomes_preserve_unmerged_work(wt, repo, gh, capsys, monkeypatch, queue_requests, outcome, jitter):
    head = arm_mergify_pr(wt, gh, message="mergify delivery",
                         state="MERGED" if outcome.startswith("already_") else "OPEN")
    if outcome.startswith("incompatible_"):
        receipt = wt / ".loopzero/checks.json"
        receipt.write_text(json.dumps({**json.loads(receipt.read_text()), "settings_sha256": "0" * 64}))
    gh.respond("api -X", "")
    landed = outcome in {"merged", "incompatible_landed", "worktree_removed", "merged_head_changed",
                         "already_merged", "already_merged_changed"}
    initial_head = "f" * 40 if outcome == "head_changed" else head
    snapshots = [] if outcome.startswith("already_") else [open_pr_snapshot(initial_head)]
    changed = outcome in {"merged_head_changed", "already_merged_changed", "requested_head_changed"}
    final_head = "f" * 40 if changed else head
    normal = landed_snapshot(final_head) if landed else merge_json(head=final_head)
    closed = merge_json(state="CLOSED", head=head)
    snapshots.extend({
        "ejection_head_changed": [normal, merge_json(head=head), landed_snapshot("f" * 40)],
        "closed_then_merged": [closed, landed_snapshot(head)], "still_closed": [closed, closed],
    }.get(outcome, [normal]))
    gh.append(f"api repos/{REPO}/pulls/7", *snapshots)
    configuration_result = {
        "config_error": mergify.MergifyError("configuration HTTP 503"),
        "unsafe_config": False, "unsafe_pending": False,
    }.get(outcome, True)
    configured = Mock(side_effect=itertools.chain([True], itertools.repeat(configuration_result)))
    monkeypatch.setattr(mergify, "configured", configured)
    monkeypatch.setattr(mergify, "queue_attempt", Mock(return_value=mergify.QueueAttempt(
        outcome != "requested_head_changed", outcome not in {"unsafe_pending", "requested_head_changed"})))
    monkeypatch.setattr(mergify, "mark_confirmed", lambda *_: pytest.fail("unsafe confirmation"))
    member = mergify.Membership()
    first_read = {
        "incompatible_absent": None, "incompatible_unverified": mergify.MergifyError("HTTP 503"),
        "unsafe_pending": None, "requested_head_changed": None,
    }.get(outcome, member)
    later_read = {
        "incompatible_absent": None, "incompatible_unverified": mergify.MergifyError("HTTP 503"),
        "api_error": mergify.MergifyError("HTTP 503"), "ejected": None, "ejection_head_changed": None,
    }.get(outcome, member)
    membership = Mock(side_effect=itertools.chain([first_read], itertools.repeat(later_read)))
    monkeypatch.setattr(mergify, "membership", membership)
    clock = poll_clock(monkeypatch, jitter=jitter)
    if outcome == "worktree_removed":
        poll_until = cli._poll_until

        def remove_worktree_then_poll(*args, **kwargs):
            git(repo, "worktree", "remove", "--force", str(wt))
            return poll_until(*args, **kwargs)

        monkeypatch.setattr(cli, "_poll_until", remove_worktree_then_poll)
    wait = {"timeout": 250, "closed_then_merged": 60, "still_closed": 60}.get(outcome, 0)
    try:
        code, out, err = run(capsys, "merge", f"--wait={wait}")
    finally:
        if outcome == "worktree_removed":
            monkeypatch.chdir(repo)
    if outcome == "timeout":
        assert f"following Mergify queue main; head {head[:12]}; waiting up to 250s" in out
        assert clock.sleeps == ([30, 60, 120, 36, 4] if jitter == 1 else [30, 54, 108, 53, 5])
        assert sum(c["argv"][:2] == PR_LIST_READ for c in gh.calls) == 1
        assert sum(c["argv"][:2] == ["api", "graphql"] for c in gh.calls) == 1
    assert queue_requests == ([(REPO, 7, head, "main")] if outcome == "requested_head_changed" else [])
    assert all(call.args[-1] == (wait or None) for call in configured.call_args_list)
    if outcome in {"closed_then_merged", "still_closed"}:
        assert clock.sleeps == [30] and membership.call_count == 1
    if outcome.startswith("incompatible_"):
        assert membership.call_count == (2 if landed else 1)
        if not landed:
            assert "incompatible settings fingerprint" in err
    if outcome in {"merged", "incompatible_landed", "already_merged", "worktree_removed", "closed_then_merged"}:
        assert code == 0 and "e" * 40 in out and not wt.exists(), err
        if outcome == "worktree_removed":
            assert_contains(err, "warning: merged but worktree cleanup failed",
                            f"working directory {wt} does not exist")
    else:
        assert wt.exists() and code == (3 if outcome == "timeout" else 1)
        if outcome == "ejected":
            assert err.index('"Left the queue" comment') < err.index("post `@mergifyio queue`")
        message = {"ejected": "post `@mergifyio queue` as the whole PR comment (same login); a new head", "head_changed": "head changed",
                   "timeout": "timed out", "api_error": "HTTP 503", "still_closed": "closed without a verified merge",
                   "incompatible_absent": "incompatible settings fingerprint", "incompatible_unverified": "HTTP 503",
                   "unsafe_config": "unsafe or unavailable", "unsafe_pending": "unsafe or unavailable", "config_error": "HTTP 503",
                   "requested_head_changed": "head changed", "merged_head_changed": "head changed", "already_merged_changed": "head changed", "ejection_head_changed": "head changed"}[outcome]
        assert message in out + err


@pytest.mark.parametrize("confirmed,kind,mode", [
    *((False, kind, "wait") for kind in (
        "current", "stale", "missing", "stacked", "isolated", "head", "requeue", "author", "landed",
        "rule", "rule_requeue", "rule_same", "rule_unrequested", "rule_blocked", "linked_already",
    )),
    *((True, kind, "wait") for kind in ("stacked", "isolated", "unknown", "conflict", "batch", "dequeued")),
    *((False, kind, "no_wait") for kind in ("rule", "rule_requeue", "rule_blocked")),
    (False, "rule_blocked", "unverified"),
])
def test_mergify_reports_current_queue_attempt(wt, gh, capsys, monkeypatch, confirmed, kind, mode):
    blocked = kind == "rule_blocked"
    head = arm_mergify_pr(wt, gh, "failure" if blocked else "success")
    admission = kind in MERGIFY_QUEUE_FIXTURE["admission"]
    request = MERGIFY_QUEUE_FIXTURE["request"].replace("@head@", head)
    requested = {"id": 42, "user": {"login": LOGIN}, "created_at": "2026-11-25T19:00:00Z", "body": request}
    comments = [] if kind in {"rule_unrequested", "rule_blocked", "rule_requeue"} else [requested]
    if kind != "missing":
        template = MERGIFY_QUEUE_FIXTURE.get(
            "admission_report" if admission else f"{kind.partition('_')[0]}_report", MERGIFY_QUEUE_FIXTURE["report"])
        body = template.replace("@url@", URL).replace("@sha@", "f" * 7 if kind == "head" else head[:7])
        body += MERGIFY_QUEUE_FIXTURE["attribution"].get(kind, "")
        if kind in {"isolated", "stacked"}:
            job = 139880043495 if kind == "isolated" else 139939028086
            job_url = f"https://github.com/{REPO}/actions/runs/99/job/{job}"
            node = ("tests/unit/research_protocol/test_research_protocol_export.py"
                    "::TestCheckedInArtifact::test_main_check_exit_code_is_zero" if kind == "isolated"
                    else "tests/unit/infrastructure/test_final_qa_capabilities.py"
                         "::test_role_and_catalog_capability_failures_are_advisory[CREATE ROLE isolated NOLOGIN]")
            if confirmed and kind == "stacked":
                body += f"\n- ❌ [`Backend static checks and unit lane / Backend validation`]({job_url})"
            else:
                body += f"\n- ❌ **Checks failed** 19:23 · draft [#15011](/{REPO}/pull/15011)"
                candidate_sha = "d" * 40
                if confirmed or kind == "stacked":
                    gh.respond(f"api repos/{REPO}/pulls/15011", open_pr_snapshot(candidate_sha) | {"number": 15011})
                    gh.respond(f"api repos/{REPO}/commits/{candidate_sha}/check-runs", {"check_runs": [
                        {"name": "Backend validation", "conclusion": "failure" if confirmed else "timed_out", "details_url": job_url},
                        {"name": "Successful sibling", "conclusion": "success", "details_url": job_url},
                        {"name": "Foreign check", "conclusion": "failure", "details_url": "https://example.com/check"},
                    ]})
            log = f"2026-11-26T11:47:24.4155165Z FAILED {node} - detail from the test\n"
            gh.respond(f"api repos/{REPO}/actions/jobs/{job}/logs", log)
        comments.append({"id": 43, "user": {"login": "someone-else" if kind == "author" else "mergify[bot]"},
                         "updated_at": MERGIFY_QUEUE_FIXTURE["admission"].get(kind, "2026-11-25T19:01:00Z"),
                         "body": body})
    requeued = {"rule_requeue": "19:02", "requeue": "19:02", "rule_same": "19:01", "linked_already": "19:00:30"}
    if kind in requeued:  # linked_already: a command Mergify answered "already queued" before the departure
        at = requeued[kind]
        comments.append({"id": 44, "user": {"login": LOGIN}, "body": "@mergifyio queue",
                         "created_at": f"2026-11-25T{at}{':00' if at.count(':') == 1 else ''}Z"})
    endpoint = f"api repos/{REPO}/issues/7/comments"
    gh.respond(endpoint, comments)
    monkeypatch.setattr(mergify, "configured", lambda *_: True)
    membership = iter([mergify.Membership()] if confirmed else [])

    def lookup(*_):
        if mode == "unverified":
            raise mergify.MergifyError("HTTP 503")
        assert not (blocked and mode == "no_wait")
        return next(membership, None)

    monkeypatch.setattr(mergify, "membership", lookup)
    monkeypatch.setattr(mergify, "mark_confirmed", lambda *_: None)
    monkeypatch.setattr(mergify, "request", lambda *_: pytest.fail("already requested"))
    if kind == "landed":
        opened = open_pr_snapshot(head)
        gh.append(f"api repos/{REPO}/pulls/7", opened, landed_snapshot(head))
        gh.respond("api -X", "")
    if admission:
        summary, _ = expect_cli(capsys, 0, "status", stdout_contains=("waiting for admission",), stderr="")
        assert (f"{URL}#issuecomment-43" in summary) is (kind == "current")
    code, out, err = run(capsys, "merge", *(["--wait=0"] if mode != "no_wait" else []))
    if blocked and mode != "wait":
        assert code == 1 and "not ready to request Mergify" in err and "requeue" not in err
        assert ("HTTP 503" in err) == (mode == "unverified")
        assert not any(c["argv"][:2] == endpoint.split() for c in gh.calls)
    elif kind == "rule_requeue" and mode == "no_wait":
        assert code == 0 and "requested from Mergify" in out
    elif kind == "landed":
        assert (code, err) == (0, "") and not wt.exists() and out.splitlines()[-1] == "e" * 40
    elif kind in MERGIFY_QUEUE_FIXTURE["expected"]:
        if kind == "rule":
            assert sum(c["argv"][:2] == endpoint.split() for c in gh.calls) == 1
        assert code == 1 and wt.exists() and f"{URL}#issuecomment-43" in err and ("'checks'" in err) == (kind not in {"conflict", "dequeued"})
        assert all(part in err for part in MERGIFY_QUEUE_FIXTURE["expected"][kind])
        if kind in {"isolated", "stacked"}:
            if confirmed or kind == "stacked":
                assert job_url in err and "Backend validation" in err and node in err and "detail from the test" not in err
                assert "Successful sibling" not in err and "Foreign check" not in err
            else:
                assert "GitHub could not read the candidate checks" in err
        else:
            assert not any("--allow-escape-sequences" in call["argv"] for call in gh.calls)
        assert all(part in err for part in MERGIFY_QUEUE_FIXTURE["requeue_guidance"]) is (kind not in {"conflict", "dequeued"})
    else:
        assert code == 3 and wt.exists() and "waiting for admission" in out
        assert (f"{URL}#issuecomment-43" in out) is (kind == "current")


@pytest.mark.parametrize("job,count", [("139880043495", 2), ("139939028086", 10), ("140702396477", 7)])
def test_candidate_job_log_summary(gh, job, count):
    log = (Path(__file__).parent / "fixtures" / f"mergify-job-{job}.log").read_text()
    nodes = list(dict.fromkeys(line.split(" ", 2)[2].removesuffix(" - [assertion omitted]") for line in log.splitlines()))
    gh.respond(f"api repos/{REPO}/actions/jobs/{job}/logs", log)
    assert mergify._job_failure(REPO, job) == ", ".join(repr(node) for node in nodes[:count])


@pytest.mark.parametrize("param_id", ["two - parts", "outer [inner] - tail"])
def test_candidate_job_log_failure_formats(gh, param_id):
    log = (f"\x1b[31mFAILED\x1b[0m test_example.py::test_node[{param_id}] - assertion\n"
           "FAIL  suite.test.ts > suite > test\n  1) [chromium] \u203a test.spec.ts:1 \u203a title\n"
           "\x1b]8;;https://example.com\x07ERROR test_probe.py::test_probe\x1b]8;;\x07")
    gh.respond(f"api repos/{REPO}/actions/jobs/7/logs", log)
    assert mergify._job_failure(REPO, "7") == (f"'test_example.py::test_node[{param_id}]', 'suite.test.ts > suite > test', "
                                            "'[chromium] \u203a test.spec.ts:1 \u203a title', 'test_probe.py::test_probe'")


@pytest.mark.parametrize("result", ["blocking", "neutral", "skipped", None, "long", "many", "any_of"])
def test_mergify_timeout_reports_evaluated_candidate_conditions(wt, gh, capsys, monkeypatch, result):
    arm_mergify_pr(wt, gh)
    monkeypatch.setattr(mergify, "configured", lambda *_: True)
    monkeypatch.setattr(mergify, "queue_attempt", Mock(return_value=mergify.QueueAttempt(True, True)))
    blocking = result in {"long", "many", "any_of"}
    payload = MERGIFY_QUEUE_FIXTURE["candidate_responses"]["blocking" if blocking else result or "missing"]
    conditions = payload["mergeability_check"]["conditions_evaluation"]
    any_of = {"match": False, "label": "any of", "subconditions": [{"match": False, "label": f"check-{s} = gate"} for s in ("neutral", "success")]}
    if extra := {"long": 1, "many": 30, "any_of": 1}.get(result, 0):
        added = any_of if result == "any_of" else {"match": False, "label": "界" * 3000}
        monkeypatch.setitem(conditions, "subconditions", conditions["subconditions"] + [added] * extra)
    monkeypatch.setattr(mergify, "api_get", lambda *_: payload)
    out, _ = expect_cli(capsys, 3, "merge", "--wait=0", stderr="")
    assert f"https://github.com/{REPO}/pull/42" in out
    assert (MERGIFY_QUEUE_FIXTURE["candidate_condition"] in out) == (result == "blocking" or blocking)
    assert ("check-neutral = gate | check-success = gate" in out) == (result == "any_of")
    assert "failed" not in out and "check-success = checks" not in out
    assert len(out.encode()) < 2048 and ("27 more" in out) == (extra == 30)


@pytest.mark.parametrize("queue_state", ["unsafe", "error", "safe", "becomes_unsafe"])
@pytest.mark.parametrize("check_state", ["missing", "skipped"])
def test_mergify_ready_draft_requires_safe_queue_before_ci_activation(
    wt, gh, capsys, monkeypatch, queue_state, check_state
):
    head = commit_mergify_configuration(wt, "mergify delivery")
    arm_approved_pr(gh, head, wt)
    if check_state == "skipped":
        arm_check_progress(gh, head, "skipped", "success")
    else:
        arm_check_runs(gh, head, [], [{"name": "checks", "status": "completed", "conclusion": "success"}])
    calls = []
    def configured(*_):
        calls.append(True)
        if queue_state == "error":
            raise mergify.MergifyError("configuration HTTP 503")
        return queue_state == "safe" or (queue_state == "becomes_unsafe" and len(calls) == 1)
    monkeypatch.setattr(mergify, "configured", configured)
    out, err = expect_cli(capsys, 0 if queue_state == "safe" else 1, "ready", "--wait=0")
    writes = sum(is_ready_call(c) for c in gh.calls)
    assert writes == (1 if queue_state in {"safe", "becomes_unsafe"} else 0)
    if queue_state != "safe":
        assert "configuration" in out + err


@pytest.mark.parametrize("state,guidance", [
    ("OPEN", ""), (None, "from the queued PR's worktree"),
    ("CLOSED", "There is no queue entry to cancel."), ("MERGED", "There is no queue entry to cancel."),
])
def test_mergify_cancel_does_not_require_healthy_queue_or_review(wt, gh, capsys, monkeypatch, state, guidance):
    commit_mergify_configuration(wt)
    gh.respond_pr_list([] if state is None else [pr_json(headRefOid="a" * 40, state=state)])
    monkeypatch.setattr(mergify, "configured", lambda *_: pytest.fail("cancel must remain available"))
    removals = []
    monkeypatch.setattr(mergify, "dequeue", lambda *args: removals.append(args))
    _, err = expect_cli(capsys, int(bool(guidance)), "merge", "--cancel", stderr="" if state == "OPEN" else None, stderr_contains=(guidance,))
    assert ("run `loopzero pr`" not in err) and removals == ([] if guidance else [(REPO, 7)]) and wt.exists()


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
            if lineage == "delta":  # A real repair; an empty delta cannot clear blocking findings.
                history = [rev(head_of(wt), "primary", state="CHANGES_REQUESTED", verdict="request_changes")]
                (wt / "repair.py").write_text("fixed = True\n")
                git(wt, "add", "repair.py")
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
    assert cli._load_review(wt, head, kind)[0].fallback == reason


@pytest.mark.parametrize("author,expected", [("Claude", "codex"), ("Jane", "claude")])
def test_review_uses_lineage_author_after_unattributed_repair(
    wt: Path, gh: FakeGh, fake_bin: Path, capsys, author: str, expected: str
) -> None:
    git(wt, "commit", "--amend", "-q", "-m",
        f"child patch\n\nCo-Authored-By: {author} <bot@example.com>")
    git(wt, "commit", "--allow-empty", "-q", "-m", "unattributed repair")
    prepare_review(wt, gh, fake_bin)
    _fake_codex(fake_bin, json.dumps(APPROVE))

    out, _ = expect_cli(capsys, 0, "review")
    assert out.splitlines()[-1].startswith(f"primary review by {expected}")
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
        arm_approved_pr(gh, head, wt)
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


@pytest.mark.parametrize("case", ["dirty", "unpushed", "after-head-read"])
def test_cleanup_preserves_local_work(wt, case, capsys, monkeypatch):
    pr = SimpleNamespace(head_sha=cli.worktree.head(wt))
    if case == "dirty":
        branch = cli.worktree.branch(wt)
        git(wt, "config", "core.excludesFile", str(wt / ".loopzero" / "exclude"))
        (wt / ".loopzero" / "exclude").write_text("local.env\n")
        (wt / "local.env").write_text("ignored")
        (wt / "untracked").write_text("keep me")
    else:
        git(wt, "commit", "--allow-empty", "-m", "local after merge")
    if case == "after-head-read":
        monkeypatch.setattr(cli.worktree, "head", lambda _wt: pr.head_sha)
    cli._cleanup(wt, pr)
    assert wt.exists()
    if case == "dirty":
        assert (wt / "untracked").read_text() == "keep me" and (wt / "local.env").read_text() == "ignored"
        assert git(wt, "branch", "--list", branch)
    diagnostic = {"dirty": "worktree is dirty", "unpushed": "local HEAD differs", "after-head-read": "but expected"}
    assert diagnostic[case] in capsys.readouterr().err


def test_task_state_lock_times_out_with_a_live_holder(git_repo, monkeypatch):
    now = 0.0

    def advance(seconds):
        nonlocal now
        now += seconds

    monkeypatch.setattr(cli.worktree, "time", SimpleNamespace(monotonic=lambda: now, sleep=advance))
    with cli.worktree.lock_local_state(git_repo):
        with (
            pytest.raises(TimeoutError, match=r"task state is busy.*retry"),
            cli.worktree.lock_local_state(git_repo),
        ):
            pytest.fail("contender acquired a held directory lock")
        assert now == 60
        cli.worktree.write_local_state(git_repo, "review.json", "receipt", private=True)
        assert cli.worktree.read_local_state(git_repo, "review.json") == "receipt"
    with cli.worktree.lock_local_state(git_repo):
        assert now == 60


@pytest.mark.parametrize("redirected", [False, True])
def test_checks_report_survives_failed_publication(git_repo, tmp_path, monkeypatch, redirected):
    path = git_repo / cli.CHECKS_FILE
    monkeypatch.setattr(cli.worktree, "head", lambda _: next(heads))
    heads = iter(("a" * 40, "b" * 40))
    cli._save_report(git_repo, CheckReport("a" * 40, False, ()))

    def interrupt(staged, dest, *, src_dir_fd, dst_dir_fd):
        assert src_dir_fd == dst_dir_fd and dest == path.name
        assert json.loads(path.with_name(staged).read_text())["head"] == "b" * 40
        assert json.loads(path.read_text())["head"] == "a" * 40
        raise OSError("publication interrupted")

    if redirected:
        external = path.parent.rename(tmp_path / "external")
        path.parent.symlink_to(external, target_is_directory=True)
    monkeypatch.setattr(cli.os, "replace", interrupt)
    error = cli.worktree.WorktreeError if redirected else OSError
    with pytest.raises(error, match="symbolic link" if redirected else "publication interrupted"):
        cli._save_report(git_repo, CheckReport("b" * 40, False, ()))
    assert json.loads(path.read_text())["head"] == "a" * 40
    assert list(path.parent.iterdir()) == [path]
    if redirected:
        external.chmod(0o755)
        review = ReviewResult("claude", "a" * 40, "primary", "approve", (), "")
        receipt = cli._review_file(git_repo, review.head, review.kind)
        receipt.write_text("user file")
        with pytest.raises(cli.worktree.WorktreeError, match="symbolic link"):
            cli._save_review(git_repo, review)
        assert receipt.read_text() == "user file" and external.stat().st_mode & 0o777 == 0o755


@pytest.mark.parametrize("moved", [False, True])
def test_already_merged_finishes_partial_cleanup(wt, repo, gh, capsys, monkeypatch, head, moved):
    cli._review_file(wt, head, "primary").write_text("{broken")
    arm_landed_pr(gh, head, "c" * 40)
    gh.fail(f"api repos/{REPO}/git/ref/heads/lz/t1", "gh: Not Found (HTTP 404)")
    original = cli.worktree.git
    advanced = git(wt, "commit-tree", f"{head}^{{tree}}", "-p", head, "-m", "newer").strip()

    def cleanup_git(cwd, *args, **kwargs):
        if args[1:3] == ("update-ref", "--no-deref"):
            update = ("update-ref", "refs/heads/lz/t1", advanced) if moved else ("branch", "-D", "lz/t1")
            git(repo, *update)
        return original(cwd, *args, **kwargs)

    monkeypatch.setattr(cli.worktree, "git", cleanup_git)
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
        arm_new_draft(gh, head)
        arm_draft_validation(gh, wt, head)
        _, err = expect_cli(capsys, int(bool(extra)), "pr")
        if extra:
            assert not git(wt, "ls-remote", "origin", "refs/heads/lz/t1").strip()
    else:
        git(wt, "push", "-q", "-u", "origin", "lz/t1")
        arm_pr_body_patch(gh, head, body=prose)
        _, err = expect_cli(capsys, 0, "check", stdout_endswith="PASS\n")

    writes = [call for call in gh.calls if "--input" in call]
    if extra:
        assert "65537" in err and "65536" in err
        assert not writes
    else:
        assert err == ""
        assert len(writes) == 1 + create  # the draft, then its validation
        assert json.loads(writes[-1]["--input"])["body"] == prose + suffix


def merge_wait(monkeypatch, capsys, wait, fake):
    run_gh, cleaned = github.run, []
    monkeypatch.setattr(github, "run", lambda argv, *, timeout, **kw: fake(argv, timeout) or run_gh(argv, timeout=timeout, **kw))
    monkeypatch.setattr(cli, "_cleanup_remote_refs", lambda *_: None)
    monkeypatch.setattr(cli, "_cleanup", lambda *_: cleaned.append(True))
    return (*run(capsys, "merge", f"--wait={wait}"), cleaned)


@pytest.mark.parametrize("wait, latency, state, expected", [
    (1, 3.0, "MERGED", 3), (1, 0.25, "MERGED", 0), (0, 0.0, "MERGED", 0), (1, 0.0, "CLOSED", 1),
])
def test_merge_queue_landing_within_budget(
    wt, gh, capsys, monkeypatch, head, wait, latency, state, expected,
):
    publish_workflow(wt, WORKFLOW.replace('merge = "squash"', 'merge = "queue"'))
    arm_approved_pr(gh, head, wt, isDraft=False)
    clock, budgets, write_timeouts = poll_clock(monkeypatch, start=1000.0), [], []
    gh.respond("pr merge", "")
    gh.respond("api graphql", threads_json(), native_queue(False, head), native_queue(True, head))
    def landing(argv, timeout):
        if argv[1:3] == ["pr", "merge"]:
            write_timeouts.append(timeout)
        if not write_timeouts or argv[1:3] != ["api", f"repos/{REPO}/pulls/7"]:
            return None
        budgets.append(timeout)
        if len(budgets) == 1:
            return Completed(0, json.dumps(merge_json(state="OPEN", head=head)), "", 0.0)
        clock.now += latency
        return Completed(0, json.dumps(merge_json(state=state, head=head, sha="e" * 40 if state == "MERGED" else None)), "", latency)
    code, out, err, cleaned = merge_wait(monkeypatch, capsys, wait, landing)
    assert (code, bool(cleaned), write_timeouts) == (expected, expected == 0, [300])
    assert budgets == ([1.0, 1.0, 0.5] if state == "CLOSED" else [1.0 if wait else 120.0] * 2)
    assert (err == "", "closed without a verified merge" in err) == (state != "CLOSED", state == "CLOSED")
    assert ("timed out waiting for the merge queue" in out) is (expected == 3)


@pytest.mark.parametrize("verified", [True, False])
def test_direct_merge_verifies_after_queue_wait_deadline(wt, gh, capsys, monkeypatch, head, verified):
    arm_approved_pr(gh, head, wt, isDraft=False)
    clock = poll_clock(monkeypatch)
    def slow_merge(argv, _timeout):
        if argv[1:3] != ["pr", "merge"]:
            return None
        clock.now += 2
        gh.respond(f"api repos/{REPO}/pulls/7", merge_json(state="MERGED", head=head if verified else "f" * 40, sha="e" * 40))
        return Completed(0, "", "", 2)
    code, out, err, cleaned = merge_wait(monkeypatch, capsys, 1, slow_merge)
    assert ((code, err, cleaned, out.splitlines()[-1]) == (0, "", [True], "e" * 40)) if verified else (
        (code, cleaned) == (1, []) and "merge could not be verified" in err and "timed out" not in out + err)


def test_queue_wait_budget_boundaries(monkeypatch):
    clock, budgets, pr = poll_clock(monkeypatch), [], SimpleNamespace(number=7, head_sha="a" * 40, base_ref="main")
    def poll(_remaining):
        github.read_timeout(0)
        clock.now = 1
        return "e" * 40
    assert cli._queue_wait(1, poll, bounded=True) == "e" * 40  # a landing verified in time survives scope exit
    monkeypatch.setattr(github, "merge", lambda *_: setattr(clock, "now", 50))
    monkeypatch.setattr(cli, "_wait_for_merge", lambda _config, _pr, timeout: budgets.append(timeout))
    cli._merge_and_follow(SimpleNamespace(repo=REPO, merge_strategy="queue"), pr, 60, 60)
    assert budgets == [10]  # the follow budget is what the merge write left
    closed = [github.ClosedUnmerged(("gh",), "closed")]
    monkeypatch.setattr(github, "merged_sha", lambda *_args, **_kwargs: "e" * 40)
    with github.bounded_reads(0, token=None, check_on_exit=False), pytest.raises(github.ReadDeadline):
        cli._landed(SimpleNamespace(repo=REPO), pr, closed)
    assert closed == []  # a late merged read still clears lagging closed evidence


@pytest.fixture
def mergify_opener(monkeypatch):
    monkeypatch.setattr(mergify, "_credential", lambda: "test-key")

    def install(open_request):
        monkeypatch.setattr(mergify.urllib.request, "build_opener", lambda *_: SimpleNamespace(open=open_request))

    return install


def test_mergify_resume_bounds_first_membership_read(monkeypatch, capsys, mergify_opener):
    clock, budgets = poll_clock(monkeypatch), []
    def open_request(request, *, timeout):
        budgets.append(timeout)
        clock.now += 2
        raise TimeoutError("membership response exceeded the budget")
    mergify_opener(open_request)
    monkeypatch.setattr(cli, "_merge_check_hint", lambda *_: "")
    monkeypatch.setattr(cli, "_wait_for_mergify", Mock(side_effect=AssertionError("polled after deadline")))
    pr = SimpleNamespace(number=7, base_ref="main")
    assert cli._follow_blocked_mergify(SimpleNamespace(repo=REPO, base_branch="main"), pr, None, "main", 1, bounded=True) is None
    assert budgets == [1] and "timed out" in capsys.readouterr().out


def test_mergify_queue_retries_stop_at_merge_deadline(wt, gh, capsys, monkeypatch, mergify_opener):
    arm_mergify_pr(wt, gh)
    clock, budgets, sleeps, admitted = poll_clock(monkeypatch, start=1000.0), [], [], []
    monkeypatch.setattr(mergify, "queue_attempt", lambda *_: mergify.QueueAttempt(True, True))
    def open_request(request, *, timeout):
        budgets.append(timeout)
        raise mergify.urllib.error.HTTPError(request.full_url, 503, "unavailable", {}, None)
    monkeypatch.setattr(mergify, "membership", lambda *_: admitted.append(True) or mergify.Membership())
    monkeypatch.setattr(mergify, "configured", lambda repo, *_: not admitted or mergify.api_get(repo, "/merge-queue/status") is not None)
    mergify_opener(open_request)
    monkeypatch.setattr(mergify, "_retry_sleep", lambda seconds: (sleeps.append(seconds), setattr(clock, "now", clock.now + seconds)))
    out, _ = expect_cli(capsys, 3, "merge", "--wait=1", stderr="")
    assert (budgets, sleeps) == ([1.0], [1.0])
    assert "timed out waiting for Mergify to land PR #7" in out and not any(
        call["argv"][:2] == ["api", f"repos/{REPO}/issues/7/comments"] for call in gh.calls)


@pytest.mark.parametrize("refresh", ["resync", "automatic"])
def test_pr_marks_legacy_same_head_check_unverified(wt, gh, capsys, refresh):
    head = head_of(wt)
    config = cli._load_config(SimpleNamespace(command="check", config=None), wt)
    live = ("# PR\n\n## Validation\nManual validation evidence.\n\n"
            "<!-- loopzero:checks:start -->\n"
            f"Recorded for `{head[:12]}`\n- `echo ok`: exit 0\n"
            "<!-- loopzero:checks:end -->\n")
    arm_pr_body_patch(gh, head, body=live)
    stages = tuple(CheckResult(command, 0, 0.0, "") for command in config.checks)
    cli._save_report(wt, CheckReport(head, False, stages))
    git(wt, "push", "-q", "-u", "origin", "lz/t1")

    if refresh == "resync":
        expect_cli(capsys, 0, "pr")
    else:
        cli._refresh_pr_checks(wt, config)
    draft = json.loads(gh.calls[-1]["--input"])["body"]
    assert "unverified" in draft and "run `loopzero check`" in draft
    assert "Manual validation evidence." in draft

    gh.respond_pr_list([pr_json(headRefOid=head, body=draft)])
    assert run(capsys, "check")[0] == 0
    draft = json.loads(gh.calls[-1]["--input"])["body"]
    assert "unverified" not in draft
    assert_contains(draft, f"Recorded for `{head[:12]}`\n- `echo ok`: exit 0", "Manual validation evidence.")


@pytest.mark.parametrize("live,reviewed,expected", [
    ({"headRefOid": "b" * 40}, None, "PR head moved during the review"),
    ({"state": "CLOSED"}, None, "not open against main"),
    ({"baseRefName": "release"}, None, "not open against main"),
    ({}, "current", "round is already taken"),
    ({}, "a" * 40, "round changed during publication"),
], ids=["moved", "closed", "retargeted", "same-head-round", "other-head-round"])
def test_final_review_round_rechecks_live_pr_and_review(wt, gh, head, live, reviewed, expected):
    pr = github.PR(7, URL, head, "main", False, "OPEN", "MERGEABLE")
    gh.respond_pr_view(pr_json(**({"headRefOid": head} | live)))
    if reviewed:
        gh.respond("api graphql viewer", {"data": {"viewer": {"login": LOGIN}}})
        gh.respond(reviews_key(), [rev(head if reviewed == "current" else reviewed, "primary")])
    with pytest.raises(cli.CliError, match=expected):
        cli._require_open_review_round(config.load(wt / "workflow.toml"), pr, head, "primary")


def test_plain_readiness_measures_review_age_with_github_date(wt, gh, head, monkeypatch):
    review = rev(head, "primary") | {"submitted_at": "2026-09-23T12:00:00Z"}
    gh.respond("api graphql viewer", {"data": {"viewer": {"login": LOGIN}}})
    gh.respond(reviews_key(), f"HTTP/2 200 OK\nDate: Wed, 23 Sep 2026 12:00:10 GMT\n\n{json.dumps([review])}")
    monkeypatch.setattr(cli.time, "time", lambda: 1790164800 + 1200)  # host clock 20 minutes ahead
    seen = {}
    monkeypatch.setattr(cli.github, "readiness", lambda *a, **k: seen.update(k) or github.Readiness(False, ("x",)))
    pr = github.PR(7, URL, head, "main", False, "OPEN", "MERGEABLE")
    cli._readiness(wt, config.load(wt / "workflow.toml"), pr, head, diagnostics=True)
    assert seen["review_grace"] is True  # 10 s by GitHub's Date header, not 20 minutes by the host
    seen.clear()  # status reads the reviews itself and passes their Date header along
    cli._readiness(wt, config.load(wt / "workflow.toml"), pr, head, [review], diagnostics=True,
                   dates=("Wed, 23 Sep 2026 12:00:10 GMT",))
    assert seen["review_grace"] is True


@pytest.mark.parametrize("step", ["_refresh_publisher_token", "_require_open_review_round"])
def test_rejected_token_before_posting_names_the_saved_review(wt, gh, fake_bin, capsys, monkeypatch, step):
    head = prepare_review(wt, gh, fake_bin)
    rejected = github.GhAuth(("gh", "api", "reviews"), "gh: Bad credentials (HTTP 401)")
    monkeypatch.setattr(cli, step, Mock(side_effect=rejected))
    expect_cli(capsys, 1, "review", stderr_contains=("Bad credentials", "run `loopzero review --repost`"))
    assert cli._review_file(wt, head, "primary").exists()


def test_late_checkpoint_preserves_completed_pass(wt, gh, capsys, head, monkeypatch):
    def run_checks(cfg, path, *, progress, status, checkpoint, workflow):
        locked_fd = checkpoint.keywords["locked_fd"]
        results = tuple(CheckResult(c, 0, 0, "", input_key="key") for c in cfg.checks)
        passed = CheckReport(head, False, results, cli.sandbox.settings_fingerprint(cfg))
        partial = replace(passed, results=(results[0], cli.sandbox.INCOMPLETE_CHECK))
        for prior in (None, passed, replace(passed, results=results[:1])):
            if prior is not None:
                cli._save_report(wt, prior, locked_fd=locked_fd)
            checkpoint(partial)
            if prior is passed:
                assert cli._current_check_report(wt, cfg, head) == passed
            else:
                assert cli._load_report(wt) == partial
        cli._save_report(wt, passed, locked_fd=locked_fd)
        return replace(passed, results=(replace(results[0], exit_code=1),))

    monkeypatch.setattr(cli.sandbox, "run_checks", run_checks)
    code, _, err = run(capsys, "check")
    assert code == 1 and "(exit 1)" in err, err
    assert cli._load_report(wt).results[0].exit_code == 1

def test_ready_rejects_check_interrupted_after_a_failed_stage(wt, head, gh, capsys, monkeypatch):
    settings = wt / ".loopzero" / "workflow.toml"
    settings.write_text(WORKFLOW.replace("test -f README.md", "test ! -e .loopzero/fail"))
    args = ["--config", str(settings)]
    assert cli.main([*args, "check"]) == 0
    (wt / ".loopzero" / "fail").touch()
    run_one, git_check = cli.sandbox._run_one, cli.sandbox._git
    failed = False

    def observe_stage(*stage_args):
        nonlocal failed
        result = run_one(*stage_args)
        failed = result.exit_code != 0
        return result

    def interrupt_final_validation(*git_args, **kwargs):
        if failed:
            raise KeyboardInterrupt
        return git_check(*git_args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(cli.sandbox, "_run_one", observe_stage)
        patch.setattr(cli.sandbox, "_git", interrupt_final_validation)
        assert cli.main([*args, "check"]) == 130

    assert failed
    arm_approved_pr(
        gh, head, wt, isDraft=False, readiness={"local_exit": None},
    )
    assert cli.main([*args, "ready"]) == 1
    assert "check receipt records a failed stage" in capsys.readouterr().out


@pytest.mark.parametrize("command", ["ready", "merge", "status"])
@pytest.mark.parametrize("publishers", ["", "review_publishers = []\n"])
@pytest.mark.parametrize("override", [False, True])
def test_delivery_requires_explicit_review_publishers(wt, gh, capsys, tmp_path, head, command, publishers, override):
    arm_approved_pr(gh, head, wt, isDraft=False)
    path = tmp_path / "override.toml"
    path.write_text(WORKFLOW_BASE + publishers)
    if not override:  # With an override the valid trusted base must not be the source.
        publish_workflow(wt, WORKFLOW_BASE + publishers)
    calls = len(gh.calls)
    code, out, err = run(capsys, *(("--config", str(path)) if override else ()), command)
    assert code == (command != "status") and "delivery.review_publishers must list the intended" in out + err
    assert (str(path) if override else "origin/main:workflow.toml") in out + err
    assert command == "status" or len(gh.calls) == calls
    assert command != "status" or ("ready:    no" in out and "review:   not evaluated" in out)


@pytest.mark.parametrize("operation", ["ready", "request", "confirm", "resolve"])
def test_concurrent_pr_writes(wt, tmp_path, monkeypatch, operation):
    settings = replace(config.load_trusted(wt), mergify_queue="main")
    pr = github.PR(7, URL, "a" * 40, "main", operation == "ready", "OPEN", "MERGEABLE")
    writing, overlap, posted = Event(), Event(), Event()
    contender = None

    def read(value):
        if writing.is_set():
            overlap.set()
        return value

    def publish(*args, **kwargs):
        if write.call_count > 1:
            overlap.set()
        writing.set()
        assert overlap.wait(2)
        posted.set()
        return REPLY_ACCEPTED
    write = Mock(side_effect=publish)
    monkeypatch.setattr(cli, "_require_receipt", Mock())
    if operation == "ready":
        monkeypatch.setattr(github, "pr_view", lambda *_: read(replace(pr, is_draft=not posted.is_set())))
        monkeypatch.setattr(github, "mark_ready", write)
        invoke = partial(cli._mark_ready, wt, settings, pr.number, pr.head_sha, SimpleNamespace(), None)
    elif operation != "resolve":
        membership = mergify.Membership() if operation == "confirm" else None
        monkeypatch.setattr(mergify, "queue_attempt", lambda *_: read(mergify.QueueAttempt(requested=posted.is_set(), confirmed=operation == "confirm" and posted.is_set())))
        monkeypatch.setattr(mergify, "membership", Mock(return_value=membership))
        monkeypatch.setattr(mergify, "request" if operation == "request" else "mark_confirmed", write)
        monkeypatch.setattr(github, "merged_sha", Mock(return_value=None))
        monkeypatch.setattr(cli, "_mergify_wait_poll", Mock(return_value=(None, membership, False)))
        merge = partial(cli._merge_with_mergify, config=settings, pr=pr, readiness=github.Readiness(True, ()), deadline=None)
        invoke = partial(merge, wt)
        contender = (partial(cli._wait_for_mergify, settings, pr, 0, False) if operation == "confirm"
                     else partial(merge, init_git_repo(tmp_path / "other")))
    else:
        monkeypatch.setattr(github, "_parse_thread", lambda *_: ("abcdef01", None))
        monkeypatch.setattr(github, "_review_threads", lambda *_a, **_k: read([{"id": "T", "isResolved": posted.is_set()}]))
        monkeypatch.setattr(github, "_has_thread_reply", lambda node, _: node["isResolved"])
        monkeypatch.setattr(github, "_gh_json", write)
        monkeypatch.setattr(github, "_gh", Mock(return_value=json.dumps(THREAD_RESOLVED)))
        invoke = partial(github.resolve_finding, REPO, 7, "abcdef01", "Fixed", ())
    overlap_calls(monkeypatch, writing, overlap, invoke, contender, blocking_only=True)
    assert write.call_count == 1
