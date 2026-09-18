"""github.py against a fake `gh` that records every call and replays canned responses."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from loopzero import github
from loopzero.types import Finding, ReviewResult

REPO = "acme/widgets"
HEAD = "a" * 40

# The fake dispatches on a key: "<cmd> <sub>" for `gh pr X`, "api <endpoint>" for `gh api`.
# Each key maps to a list of responses consumed in order (the last one repeats).
FAKE_GH = """#!{python}
import json, sys
argv = sys.argv[1:]
calls, scenario = {calls!r}, {scenario!r}
key = " ".join(argv[:2])
record = {{"argv": argv}}
for flag in ("--input", "--body-file"):
    if flag in argv:
        record[flag] = open(argv[argv.index(flag) + 1]).read()
log = json.loads(open(calls).read()) if __import__("os").path.exists(calls) else []
log.append(record)
open(calls, "w").write(json.dumps(log))
plan = json.load(open(scenario))
responses = plan.get(key)
if responses is None:
    sys.stderr.write("fake gh: no response for " + key + "\\n")
    sys.exit(97)
idx = sum(1 for c in log[:-1] if " ".join(c["argv"][:2]) == key)
resp = responses[min(idx, len(responses) - 1)]
sys.stdout.write(resp.get("stdout", ""))
sys.stderr.write(resp.get("stderr", ""))
sys.exit(resp.get("exit", 0))
"""


class FakeGh:
    def __init__(self, fake_bin: Path, tmp_path: Path) -> None:
        self.calls_file = tmp_path / "gh-calls.json"
        self.scenario_file = tmp_path / "gh-scenario.json"
        self.scenario_file.write_text("{}")
        script = fake_bin / "gh"
        script.write_text(FAKE_GH.format(
            python=sys.executable, calls=str(self.calls_file), scenario=str(self.scenario_file)
        ))
        script.chmod(0o755)

    def respond(self, key: str, *responses: object, exit: int = 0, stderr: str = "") -> None:
        plan = json.loads(self.scenario_file.read_text())
        plan[key] = [
            r if isinstance(r, dict) and "stdout" in r
            else {"stdout": r if isinstance(r, str) else json.dumps(r), "exit": exit,
                  "stderr": stderr}
            for r in responses
        ]
        self.scenario_file.write_text(json.dumps(plan))

    def fail(self, key: str, stderr: str, exit: int = 1) -> None:
        self.respond(key, {"stdout": "", "stderr": stderr, "exit": exit})

    @property
    def calls(self) -> list[dict]:
        if not self.calls_file.exists():
            return []
        return json.loads(self.calls_file.read_text())

    def argv(self, index: int) -> list[str]:
        return self.calls[index]["argv"]


@pytest.fixture
def gh(fake_bin: Path, tmp_path: Path) -> FakeGh:
    return FakeGh(fake_bin, tmp_path)


def pr_json(**over: object) -> dict:
    base = {
        "number": 7, "url": f"https://github.com/{REPO}/pull/7", "headRefOid": HEAD,
        "baseRefName": "main", "isDraft": True, "state": "OPEN", "mergeable": "MERGEABLE",
    }
    return {**base, **over}


def threads_json(*nodes: dict, has_next: bool = False, cursor: str | None = None) -> dict:
    return {"data": {"repository": {"pullRequest": {"reviewThreads": {
        "pageInfo": {"hasNextPage": has_next, "endCursor": cursor}, "nodes": list(nodes),
    }}}}}


def thread(body: str, resolved: bool = False, path: str = "src/a.py", line: int = 3) -> dict:
    return {"isResolved": resolved, "isOutdated": False, "path": path, "line": line,
            "comments": {"nodes": [{"body": body}]}}


def marker(sev: str, head: str = HEAD) -> str:
    return f"<!-- loopzero:finding severity={sev} head={head} -->"


# --- errors ------------------------------------------------------------------------


def test_missing_gh_raises_typed_error(fake_bin: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PATH", str(fake_bin))  # empty dir: no gh, no git
    with pytest.raises(github.GhMissing):
        github.pr_for_branch(REPO, "lz/x")


def test_auth_failure_is_gh_auth(gh: FakeGh) -> None:
    gh.fail("pr list", "To get started with GitHub CLI, please run:  gh auth login\n", exit=4)
    with pytest.raises(github.GhAuth) as info:
        github.pr_for_branch(REPO, "lz/x")
    assert info.value.command[:3] == ("gh", "pr", "list")
    assert "gh auth login" in info.value.tail


def test_generic_failure_carries_command_and_tail(gh: FakeGh) -> None:
    gh.fail("pr ready", "HTTP 422: boom\n")
    with pytest.raises(github.GhError) as info:
        github.mark_ready(REPO, 7)
    assert info.value.command == ("gh", "pr", "ready", "7", "--repo", REPO)
    assert "HTTP 422" in info.value.tail


# --- PR lookup / creation -----------------------------------------------------------


def test_pr_for_branch_none_when_empty(gh: FakeGh) -> None:
    gh.respond("pr list", [])
    assert github.pr_for_branch(REPO, "lz/x") is None
    assert gh.argv(0) == [
        "pr", "list", "--repo", REPO, "--head", "lz/x", "--state", "all",
        "--limit", "10", "--json", github.PR_FIELDS,
    ]


def test_pr_for_branch_prefers_open(gh: FakeGh) -> None:
    gh.respond("pr list", [pr_json(number=3, state="CLOSED"), pr_json(number=9)])
    pr = github.pr_for_branch(REPO, "lz/x")
    assert pr == github.PR(9, f"https://github.com/{REPO}/pull/7", HEAD, "main", True,
                           "OPEN", "MERGEABLE")


def test_create_draft_pr_passes_body_file_and_returns_pr(gh: FakeGh) -> None:
    gh.respond("pr create", f"https://github.com/{REPO}/pull/7\n")
    gh.respond("pr list", [pr_json()])
    pr = github.create_draft_pr(REPO, "lz/x", "main", "Title", "Body text\n")
    create = gh.calls[0]
    argv = create["argv"]
    assert argv[:2] == ["pr", "create"] and "--draft" in argv
    assert argv[argv.index("--head") + 1] == "lz/x"
    assert argv[argv.index("--base") + 1] == "main"
    assert argv[argv.index("--title") + 1] == "Title"
    assert create["--body-file"] == "Body text\n"
    assert pr.number == 7 and pr.is_draft


def test_update_body(gh: FakeGh) -> None:
    gh.respond("pr edit", "")
    github.update_body(REPO, 7, "new body")
    assert gh.argv(0) == ["pr", "edit", "7", "--repo", REPO, "--body-file", gh.argv(0)[-1]]
    assert gh.calls[0]["--body-file"] == "new body"


# --- reviews ---------------------------------------------------------------------------


def review(verdict: str = "request_changes", findings: tuple[Finding, ...] = ()) -> ReviewResult:
    return ReviewResult(family="codex", head=HEAD, kind="primary", verdict=verdict,
                        findings=findings, raw="RAW MODEL OUTPUT")


FINDINGS = (
    Finding("critical", "src/a.py", 10, "Null deref", "x may be None"),
    Finding("important", "src/b.py", 2, "Missing check", "validate input"),
    Finding("suggestion", "src/c.py", 5, "Rename", "clearer name"),
    Finding("important", None, None, "Global concern", "no file"),
)


def test_post_review_payload(gh: FakeGh) -> None:
    gh.respond(f"api repos/{REPO}/pulls/7/reviews", {"id": 1})
    github.post_review(REPO, 7, HEAD, review(findings=FINDINGS))
    call = gh.calls[0]
    assert call["argv"][:5] == ["api", f"repos/{REPO}/pulls/7/reviews", "--method", "POST",
                                "--input"]
    payload = json.loads(call["--input"])
    assert payload["commit_id"] == HEAD and payload["event"] == "REQUEST_CHANGES"
    comments = payload["comments"]
    assert [(c["path"], c["line"], c["side"]) for c in comments] == [
        ("src/a.py", 10, "RIGHT"), ("src/b.py", 2, "RIGHT"), ("src/c.py", 5, "RIGHT")]
    assert comments[0]["body"].split("\n")[0] == marker("critical")
    assert comments[1]["body"].split("\n")[0] == marker("important")
    assert "loopzero:finding" not in comments[2]["body"]
    assert "Null deref" in comments[0]["body"] and "x may be None" in comments[0]["body"]
    body = payload["body"]
    assert "<details>" in body and "RAW MODEL OUTPUT" in body and "</details>" in body
    assert "Global concern" in body


def test_post_review_approve_event(gh: FakeGh) -> None:
    gh.respond(f"api repos/{REPO}/pulls/7/reviews", {"id": 1})
    github.post_review(REPO, 7, HEAD, review(verdict="approve"))
    payload = json.loads(gh.calls[0]["--input"])
    assert payload["event"] == "APPROVE" and payload["comments"] == []


@pytest.mark.parametrize("verdict,msg", [
    ("approve", "Can not approve your own pull request"),
    ("request_changes", "Can not request changes on your own pull request"),
])
def test_post_review_falls_back_to_comment_for_own_pr(gh: FakeGh, verdict: str, msg: str) -> None:
    key = f"api repos/{REPO}/pulls/7/reviews"
    gh.respond(key, {"stdout": "", "stderr": f'gh: Unprocessable Entity (HTTP 422)\n{msg}\n',
                     "exit": 1}, {"stdout": '{"id": 2}', "exit": 0})
    github.post_review(REPO, 7, HEAD, review(verdict=verdict, findings=FINDINGS[:1]))
    events = [json.loads(c["--input"])["event"] for c in gh.calls]
    assert events == [verdict.upper(), "COMMENT"]
    first, second = (json.loads(c["--input"]) for c in gh.calls)
    assert first["comments"] == second["comments"] and first["body"] == second["body"]


def test_post_review_other_errors_propagate(gh: FakeGh) -> None:
    gh.fail(f"api repos/{REPO}/pulls/7/reviews", "HTTP 500 server error")
    with pytest.raises(github.GhError):
        github.post_review(REPO, 7, HEAD, review())
    assert len(gh.calls) == 1


# --- blocking findings ------------------------------------------------------------


def test_open_blocking_findings_filters_and_parses(gh: FakeGh) -> None:
    gh.respond("api graphql", threads_json(
        thread(f"{marker('critical')}\n**critical: Null deref**\n\nx may be None"),
        thread(f"{marker('important')}\nplain title", resolved=True),
        thread(f"{marker('suggestion')}\n**suggestion: Rename**"),
        thread("no marker here\n" + marker("critical")),
        thread(f"{marker('important')}\n**important: Missing check**\n\nvalidate",
               path="src/b.py", line=2),
    ))
    found = github.open_blocking_findings(REPO, 7)
    assert found == [
        Finding("critical", "src/a.py", 3, "Null deref", "**critical: Null deref**\n\nx may be None"),
        Finding("important", "src/b.py", 2, "Missing check",
                "**important: Missing check**\n\nvalidate"),
    ]
    argv = gh.argv(0)
    assert argv[:2] == ["api", "graphql"]
    assert "-F" in argv and "owner=acme" in argv and "name=widgets" in argv and "number=7" in argv
    assert any(a.startswith("query=") and "reviewThreads" in a for a in argv)


def test_open_blocking_findings_paginates(gh: FakeGh) -> None:
    gh.respond("api graphql",
               threads_json(thread(f"{marker('critical')}\nA"), has_next=True, cursor="C1"),
               threads_json(thread(f"{marker('important')}\nB")))
    found = github.open_blocking_findings(REPO, 7)
    assert [f.title for f in found] == ["A", "B"]
    assert "after=C1" not in gh.argv(0) and "after=C1" in gh.argv(1)


# --- checks --------------------------------------------------------------------------------


def test_check_runs_merges_runs_and_statuses(gh: FakeGh) -> None:
    gh.respond(f"api repos/{REPO}/commits/{HEAD}/check-runs", {"check_runs": [
        {"name": "checks", "status": "completed", "conclusion": "success"},
        {"name": "lint", "status": "in_progress", "conclusion": None},
    ]})
    gh.respond(f"api repos/{REPO}/commits/{HEAD}/status", {"statuses": [
        {"context": "ci/legacy", "state": "failure"},
        {"context": "checks", "state": "pending"},
    ]})
    assert github.check_runs(REPO, HEAD) == {
        "checks": "success", "lint": "in_progress", "ci/legacy": "failure"}


# --- readiness ----------------------------------------------------------------------------


def arm_readiness(gh: FakeGh, *, threads=(), runs=None) -> None:
    gh.respond("api graphql", threads_json(*threads))
    runs = {"checks": "success"} if runs is None else runs
    gh.respond(f"api repos/{REPO}/commits/{HEAD}/check-runs", {"check_runs": [
        {"name": n, "status": "completed", "conclusion": c} for n, c in runs.items()]})
    gh.respond(f"api repos/{REPO}/commits/{HEAD}/status", {"statuses": []})


def test_readiness_ready(gh: FakeGh) -> None:
    arm_readiness(gh)
    pr = github._pr_from_json(pr_json())
    assert github.readiness(REPO, pr, ("checks",), HEAD) == github.Readiness(True, ())


def test_readiness_head_moved(gh: FakeGh) -> None:
    arm_readiness(gh)
    r = github.readiness(REPO, github._pr_from_json(pr_json()), ("checks",), "b" * 40)
    assert not r.ready and any("differs from reviewed" in x for x in r.reasons)


def test_readiness_no_review(gh: FakeGh) -> None:
    arm_readiness(gh)
    r = github.readiness(REPO, github._pr_from_json(pr_json()), ("checks",), None)
    assert r.reasons == ("no review recorded for the current head",)


def test_readiness_blocking_finding(gh: FakeGh) -> None:
    arm_readiness(gh, threads=(thread(f"{marker('important')}\n**important: Oops**"),))
    r = github.readiness(REPO, github._pr_from_json(pr_json()), ("checks",), HEAD)
    assert r.reasons == ("open important finding at src/a.py:3: Oops",)


def test_readiness_resolved_or_suggestion_threads_do_not_block(gh: FakeGh) -> None:
    arm_readiness(gh, threads=(thread(f"{marker('critical')}\nX", resolved=True),
                               thread(f"{marker('suggestion')}\nY")))
    assert github.readiness(REPO, github._pr_from_json(pr_json()), ("checks",), HEAD).ready


def test_readiness_missing_and_failed_checks(gh: FakeGh) -> None:
    arm_readiness(gh, runs={"checks": "failure"})
    r = github.readiness(REPO, github._pr_from_json(pr_json()), ("checks", "e2e"), HEAD)
    assert r.reasons == (
        "required check 'checks' is failure",
        f"required check 'e2e' missing on {HEAD[:12]}",
    )


def test_readiness_pr_not_open(gh: FakeGh) -> None:
    arm_readiness(gh)
    r = github.readiness(REPO, github._pr_from_json(pr_json(state="MERGED")), ("checks",), HEAD)
    assert r.reasons == ("PR #7 is MERGED, not open",)


def test_readiness_without_required_ci_skips_check_lookup(gh: FakeGh) -> None:
    gh.respond("api graphql", threads_json())
    assert github.readiness(REPO, github._pr_from_json(pr_json()), (), HEAD).ready
    assert [c["argv"][:2] for c in gh.calls] == [["api", "graphql"]]


# --- ready / merge ---------------------------------------------------------------------


def test_mark_ready(gh: FakeGh) -> None:
    gh.respond("pr ready", "")
    github.mark_ready(REPO, 7)
    assert gh.argv(0) == ["pr", "ready", "7", "--repo", REPO]


def test_merge_success_returns_merge_sha(gh: FakeGh) -> None:
    gh.respond("pr merge", "")
    gh.respond("pr view", {"merged": True, "mergeCommit": {"oid": "c" * 40}})
    assert github.merge(REPO, 7, "squash", HEAD) == "c" * 40
    assert gh.argv(0) == ["pr", "merge", "7", "--repo", REPO, "--squash",
                          "--match-head-commit", HEAD, "--delete-branch"]
    assert gh.argv(1) == ["pr", "view", "7", "--repo", REPO, "--json", "mergeCommit,merged"]


def test_merge_command_failure(gh: FakeGh) -> None:
    gh.fail("pr merge", "head commit mismatch")
    with pytest.raises(github.MergeFailed) as info:
        github.merge(REPO, 7, "rebase", HEAD)
    assert "--rebase" in info.value.command and "mismatch" in info.value.tail


def test_merge_unverified(gh: FakeGh) -> None:
    gh.respond("pr merge", "")
    gh.respond("pr view", {"merged": False, "mergeCommit": None})
    with pytest.raises(github.MergeFailed):
        github.merge(REPO, 7, "merge", HEAD)


def test_merge_rejects_unknown_strategy(gh: FakeGh) -> None:
    with pytest.raises(github.MergeFailed):
        github.merge(REPO, 7, "fast-forward", HEAD)
    assert gh.calls == []
