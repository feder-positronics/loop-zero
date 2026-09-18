"""cli.py end to end: real git with a bare origin, fake `gh`, `claude`, `codex` and `bwrap`."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from loopzero import cli
from loopzero.types import CheckReport, CheckResult, ReviewResult
from tests.conftest import git
from tests.test_github import FakeGh, pr_json, threads_json
from tests.test_runners import APPROVE, CHANGES, _claude_envelope, _fake_claude
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
    git(path, "add", "feature.py")
    git(path, "commit", "-q", "-m", "add feature")
    monkeypatch.chdir(path)
    return path


def head_of(path: Path) -> str:
    return git(path, "rev-parse", "HEAD").strip()


def marker(head: str, kind: str) -> str:
    return cli.review_marker(head, kind)


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
    assert lines[0].startswith("exit 0") and lines[0].endswith("  echo ok")
    assert lines[1].endswith("  test -f README.md")
    report = json.loads((wt / ".loopzero" / "checks.json").read_text())
    assert report["head"] == head_of(wt)
    assert report["dirty"] is False
    assert [(r["command"], r["exit_code"]) for r in report["results"]] == [
        ("echo ok", 0), ("test -f README.md", 0)]
    assert report["results"][0]["tail"] == "ok"


def test_check_fail_exits_one(wt: Path, capsys) -> None:
    (wt / "workflow.toml").write_text(WORKFLOW.replace('"echo ok"', '"echo no; exit 3"'))
    code, out, err = run(capsys, "check")
    assert code == 1 and out.splitlines()[-1] == "FAIL"
    assert out.splitlines()[0].startswith("exit 3")
    assert "README.md" not in out
    assert err == "--- echo no; exit 3 (exit 3) ---\nno\n", "failing tail goes to stderr"
    report = json.loads((wt / ".loopzero" / "checks.json").read_text())
    assert [result["command"] for result in report["results"]] == ["echo no; exit 3"]


def test_check_failure_tail_is_capped_at_40_lines(wt: Path, capsys) -> None:
    cmd = "seq 1 50; exit 1"
    (wt / "workflow.toml").write_text(WORKFLOW.replace('"echo ok", "test -f README.md"', f'"{cmd}"'))
    code, _, err = run(capsys, "check")
    lines = err.splitlines()
    assert code == 1 and lines[0] == f"--- {cmd} (exit 1) ---"
    assert lines[1:] == [str(i) for i in range(11, 51)]


def test_check_sandbox_unavailable_exits_two(wt: Path, fake_tool, capsys) -> None:
    fake_tool("bwrap", "echo 'bwrap: No permissions' >&2\nexit 1\n")
    code, out, err = run(capsys, "check")
    assert code == 2 and out == "" and "No permissions" in err
    assert not (wt / ".loopzero" / "checks.json").exists()


def test_config_flag_overrides_root(wt: Path, tmp_path: Path, capsys) -> None:
    alt = tmp_path / "alt.toml"
    alt.write_text(WORKFLOW.replace('"echo ok", "test -f README.md"', '"echo alt"'))
    code, out, _ = run(capsys, "--config", str(alt), "check")
    assert code == 0 and "echo alt" in out and "README" not in out


# --- pr ------------------------------------------------------------------------------------


def test_pr_pushes_and_creates_draft(wt: Path, repo: Path, gh: FakeGh, capsys) -> None:
    run(capsys, "check")
    head = head_of(wt)
    gh.respond("pr list", [], [pr_json(headRefOid=head)])
    gh.respond("pr create", URL + "\n")
    code, out, err = run(capsys, "pr")
    assert (code, out, err) == (0, URL + "\n", "")
    origin = repo.parent / "origin.git"
    assert git(origin, "rev-parse", "lz/t1").strip() == head
    create = next(c for c in gh.calls if c["argv"][:2] == ["pr", "create"])
    assert "--draft" in create["argv"]
    heading = (wt / ".loopzero" / "task.md").read_text().splitlines()[0].lstrip("# ")
    assert create["argv"][create["argv"].index("--title") + 1] == heading
    body = create["--body-file"]
    assert "## Objective" in body and git(repo, "rev-parse", "origin/main").strip() in body
    assert body.count("## Checks") == 1 and f"Recorded for `{head[:12]}`" in body
    assert "(filled by `loopzero pr`" not in body, "placeholder replaced in place"
    assert body.index("## Checks") < body.index("## Review") < body.index("## Notes")
    assert "- `echo ok`: exit 0" in body and "- `test -f README.md`: exit 0" in body


def test_pr_title_prefers_human_heading(wt: Path, gh: FakeGh, capsys) -> None:
    task = wt / ".loopzero" / "task.md"
    task.write_text("## Objective\nx\n\n# Add the feature flag\n\nBase: "
                    + git(wt, "rev-parse", "HEAD~1"))
    gh.respond("pr list", [], [pr_json(headRefOid=head_of(wt))])
    gh.respond("pr create", URL + "\n")
    assert run(capsys, "pr")[0] == 0
    argv = gh.calls[1]["argv"]
    assert argv[argv.index("--title") + 1] == "Add the feature flag"
    task.write_text("## Objective\nonly sections\n")
    gh.respond("pr list", [pr_json(headRefOid=head_of(wt))])
    gh.respond(f"api repos/{REPO}/pulls/7", {})
    assert run(capsys, "pr")[0] == 0
    assert cli._pr_title(wt, "lz/t1") == "lz/t1"


def test_pr_updates_existing_open_pr(wt: Path, gh: FakeGh, capsys) -> None:
    gh.respond("pr list", [pr_json(headRefOid=head_of(wt))])
    gh.respond(f"api repos/{REPO}/pulls/7", {})
    code, out, _ = run(capsys, "pr")
    assert code == 0 and out == URL + "\n"
    assert [c["argv"][:2] for c in gh.calls] == [
        ["pr", "list"], ["api", f"repos/{REPO}/pulls/7"]
    ]
    assert "(no `loopzero check` run recorded)" in gh.calls[1]["--input"]


def test_pr_refuses_dirty(wt: Path, gh: FakeGh, capsys) -> None:
    (wt / "scratch.txt").write_text("x")
    code, out, err = run(capsys, "pr")
    assert code == 1 and out == "" and "uncommitted or untracked" in err
    assert gh.calls == []


# --- review ---------------------------------------------------------------------------


def arm_pr(gh: FakeGh, head: str, **over) -> None:
    gh.respond("pr list", [pr_json(headRefOid=head, **over)])


def test_review_primary_posts_marker(wt: Path, gh: FakeGh, fake_bin: Path, capsys) -> None:
    head = head_of(wt)
    arm_pr(gh, head)
    gh.respond(reviews_key(), [])
    gh.respond(post_key(), {"id": 1})
    _fake_claude(fake_bin, _claude_envelope(CHANGES))
    code, out, err = run(capsys, "review")
    assert (code, err) == (0, "")
    assert out == (
        f"primary review by claude on {head[:12]}: request_changes "
        "(0 critical, 1 important, 1 suggestion)\n"
    )
    post = next(c for c in gh.calls if c["argv"][1] == post_key().split(" ")[1])
    payload = json.loads(post["--input"])
    assert payload["commit_id"] == head and payload["event"] == "REQUEST_CHANGES"
    assert marker(head, "primary") in payload["body"]
    prompt = (fake_bin / "claude.stdin").read_text()
    assert "+print('hi')" in prompt and "primary review" in prompt and "## Objective" in prompt


def test_review_saves_result_and_repost_skips_model(wt: Path, gh: FakeGh, fake_bin: Path,
                                                    capsys) -> None:
    head = head_of(wt)
    arm_pr(gh, head)
    gh.respond(reviews_key(), [])
    gh.fail(post_key(), "HTTP 500 server error")
    _fake_claude(fake_bin, _claude_envelope(CHANGES))
    saved = wt / ".loopzero" / f"review-{head[:12]}-primary.json"
    code, out, err = run(capsys, "review")
    assert code == 1 and out == "" and saved.exists()
    assert f"review saved at {saved}" in err and "loopzero review --repost" in err
    data = json.loads(saved.read_text())
    assert data["head"] == head and data["kind"] == "primary" and data["family"] == "claude"
    assert data["verdict"] == "request_changes" and len(data["findings"]) == 2
    (fake_bin / "claude.stdin").unlink()
    gh.respond(post_key(), {"id": 5})
    code, out, err = run(capsys, "review", "--repost")
    assert (code, err) == (0, "") and out.startswith("primary review by claude"), err
    assert not (fake_bin / "claude.stdin").exists(), "no model invoked"
    payload = json.loads(gh.calls[-1]["--input"])
    assert marker(head, "primary") in payload["body"] and "Nit" in payload["body"]
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
    payload = json.loads(gh.calls[-1]["--input"])
    assert marker(head, "delta") in payload["body"]


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


def test_review_refuses_same_head_twice(wt: Path, gh: FakeGh, capsys) -> None:
    head = head_of(wt)
    arm_pr(gh, head)
    gh.respond(reviews_key(), [rev(head, "primary")])
    code, _, err = run(capsys, "review")
    assert code == 1 and "already has a primary review" in err


def test_review_refuses_without_pr(wt: Path, gh: FakeGh, capsys) -> None:
    gh.respond("pr list", [])
    code, out, err = run(capsys, "review")
    assert code == 1 and out == "" and "no pull request for lz/t1" in err


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
    assert code == 1 and out == ""
    assert "codex: CLI is not authenticated" in err


def test_review_fails_when_every_reviewer_fails(
    wt: Path, gh: FakeGh, fake_bin: Path, fake_tool, capsys
) -> None:
    head = head_of(wt)
    arm_pr(gh, head)
    gh.respond(reviews_key(), [])
    _fake_claude(fake_bin, "Not logged in. Please run /login", exit_code=1)
    fake_tool("codex", "echo boom >&2\nexit 2\n")  # RunnerBadOutput
    code, out, err = run(capsys, "review")
    assert code == 1 and out == ""
    assert "every configured reviewer failed: claude: CLI is not authenticated; codex: exited 2" in err
    assert all(c["argv"][1] != post_key().split(" ")[1] for c in gh.calls)


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


def test_ready_allows_resolved_request_changes_threads(wt: Path, gh: FakeGh, capsys) -> None:
    head = head_of(wt)
    arm_pr(gh, head)
    gh.respond(reviews_key(), [rev(head, "primary", state="COMMENTED",
                                   verdict="request_changes")])
    arm_readiness(gh, head, wt)
    gh.respond("pr ready", "")
    code, out, _ = run(capsys, "ready")
    assert code == 0 and out == f"ready: {URL}\n"


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
    monkeypatch.setattr(cli.worktree, "cleanup", lambda *_: (_ for _ in ()).throw(
        cli.worktree.WorktreeError("worktree is dirty")))
    code, out, err = run(capsys, "merge")
    assert code == 0 and out.splitlines()[0] == "d" * 40
    assert err.startswith("warning: merged but worktree cleanup failed")


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


def test_merge_of_externally_merged_pr_only_cleans_up(wt: Path, repo: Path, gh: FakeGh,
                                                       capsys) -> None:
    arm_pr(gh, head_of(wt), state="MERGED", isDraft=False)
    code, out, err = run(capsys, "merge")
    assert (code, err) == (0, "")
    assert out.splitlines()[-1] == f"cd {repo}"
    assert "PR #7 was already merged externally" in out and not wt.exists()
    assert all(c["argv"][:2] != ["pr", "merge"] for c in gh.calls)


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
