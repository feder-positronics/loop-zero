"""github.py against a fake `gh` that records every call and replays canned responses."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pytest

from loopzero import github
from loopzero.types import Finding, ReviewResult

REPO = "acme/widgets"
HEAD = "a" * 40

# `respond` preserves the original key-based fake. New tests use `expect`, which
# consumes complete argv interactions in order and never repeats a response.
FAKE_GH = """#!{python}
import json, os, sys
argv = sys.argv[1:]
calls, scenario = {calls!r}, {scenario!r}
key = " ".join(argv[:2])
record = {{"argv": argv}}
input_files = {{}}
for flag in ("--input", "--body-file"):
    if flag in argv:
        content = open(argv[argv.index(flag) + 1]).read()
        record[flag] = content
        input_files[flag] = content
record["input_files"] = input_files
log = json.loads(open(calls).read()) if os.path.exists(calls) else []
log.append(record)
open(calls, "w").write(json.dumps(log))
plan = json.load(open(scenario))
strict = plan.get("strict")
if strict is not None:
    index = len(log) - 1
    if index >= len(strict):
        sys.stderr.write("fake gh: exhausted after %d interactions; got %r\\n" %
                         (len(strict), argv))
        sys.exit(97)
    expected = strict[index]
    normalized = list(argv)
    for flag in ("--input", "--body-file"):
        if flag in normalized:
            normalized[normalized.index(flag) + 1] = "<temp-file>"
    if normalized != expected["argv"]:
        sys.stderr.write("fake gh: unexpected argv\\nexpected: %r\\nactual:   %r\\n" %
                         (expected["argv"], normalized))
        sys.exit(97)
    if expected.get("input_files", {{}}) != input_files:
        sys.stderr.write("fake gh: unexpected input files\\nexpected: %r\\nactual:   %r\\n" %
                         (expected.get("input_files", {{}}), input_files))
        sys.exit(97)
    resp = expected
else:
    responses = plan.get("compat", {{}}).get(key)
    if responses is None:
        sys.stderr.write("fake gh: no response for " + key + "\\n")
        sys.exit(97)
    idx = sum(1 for c in log[:-1] if " ".join(c["argv"][:2]) == key)
    resp = responses[min(idx, len(responses) - 1)]
sys.stdout.write(resp.get("stdout", ""))
sys.stderr.write(resp.get("stderr", ""))
sys.exit(resp.get("exit_code", resp.get("exit", 0)))
"""


class FakeGh:
    def __init__(self, fake_bin: Path, tmp_path: Path) -> None:
        self.calls_file = tmp_path / "gh-calls.json"
        self.scenario_file = tmp_path / "gh-scenario.json"
        self.scenario_file.write_text(json.dumps({"compat": {}}))
        script = fake_bin / "gh"
        script.write_text(FAKE_GH.format(
            python=sys.executable, calls=str(self.calls_file), scenario=str(self.scenario_file)
        ))
        script.chmod(0o755)

    def respond(self, key: str, *responses: object, exit: int = 0, stderr: str = "") -> None:
        plan = json.loads(self.scenario_file.read_text())
        if "strict" in plan:
            raise AssertionError("cannot mix respond() compatibility rules with strict expect() rules")
        plan.setdefault("compat", {})[key] = [
            r if isinstance(r, dict) and "stdout" in r
            else {"stdout": r if isinstance(r, str) else json.dumps(r), "exit": exit,
                  "stderr": stderr}
            for r in responses
        ]
        self.scenario_file.write_text(json.dumps(plan))

    @staticmethod
    def _response(response: object, *, exit: int = 0, stderr: str = "") -> dict[str, Any]:
        if isinstance(response, dict) and "stdout" in response:
            return dict(response)
        return {
            "stdout": response if isinstance(response, str) else json.dumps(response),
            "stderr": stderr,
            "exit_code": exit,
        }

    def expect(
        self,
        argv: list[str],
        response: object = "",
        *,
        exit: int = 0,
        stderr: str = "",
        input_files: dict[str, str] | None = None,
    ) -> None:
        """Queue one exact interaction; temp path arguments are ``<temp-file>``."""
        plan = json.loads(self.scenario_file.read_text())
        if plan.get("compat"):
            raise AssertionError("cannot mix strict expect() rules with respond() compatibility rules")
        item = self._response(response, exit=exit, stderr=stderr)
        item["argv"] = argv
        item["input_files"] = input_files or {}
        plan.setdefault("strict", []).append(item)
        plan.pop("compat", None)
        self.scenario_file.write_text(json.dumps(plan))

    def load_scenario(
        self, scenario: str, replacements: dict[str, str] | None = None
    ) -> None:
        """Load one redacted recorder fixture as the next strict interaction."""
        fixture = Path(__file__).parent / "fixtures" / "gh" / f"{scenario}.json"
        text = fixture.read_text()
        for old, new in (replacements or {}).items():
            text = text.replace(old, new)
        data = json.loads(text)
        argv = ["<temp-file>" if arg.startswith("<TEMP_FILE") else arg
                for arg in data["argv"]]
        self.expect(
            argv,
            {"stdout": data["stdout"], "stderr": data["stderr"],
             "exit_code": data["exit_code"]},
            input_files=data.get("input_files", {}),
        )

    def assert_complete(self) -> None:
        plan = json.loads(self.scenario_file.read_text())
        expected = len(plan.get("strict", []))
        if "strict" in plan and len(self.calls) != expected:
            raise AssertionError(f"fake gh: {expected - len(self.calls)} expected interactions remain")

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
        "author": {"login": "someone-else"},
    }
    return {**base, **over}


def threads_json(*nodes: dict, has_next: bool = False, cursor: str | None = None) -> dict:
    return {"data": {"repository": {"pullRequest": {"reviewThreads": {
        "pageInfo": {"hasNextPage": has_next, "endCursor": cursor}, "nodes": list(nodes),
    }}}}}


def thread(body: str, resolved: bool = False, path: str = "src/a.py", line: int = 3,
           author: str = "bot-user", edited: str | None = None,
           outdated: bool = False) -> dict:
    return {"isResolved": resolved, "isOutdated": outdated, "path": path, "line": line,
            "comments": {"nodes": [{"body": body, "createdAt": "2026-09-18T00:00:00Z",
                                    "lastEditedAt": edited, "author": {"login": author}}]}}


def marker(sev: str, head: str = HEAD) -> str:
    return f"<!-- loopzero:finding severity={sev} head={head} -->"


FILES_KEY = f"api repos/{REPO}/pulls/7/files"
REVIEWS_KEY = f"api repos/{REPO}/pulls/7/reviews"
# src/a.py: right lines 10..12 ; src/b.py: right lines 1..3 ; src/c.py: right lines 5..6
PR_FILES = [
    {"filename": "src/a.py", "patch": "@@ -10,2 +10,3 @@\n ctx\n+new\n ctx2"},
    {"filename": "src/b.py", "patch": "@@ -1,3 +1,3 @@\n-old\n+new\n ctx\n ctx"},
    {"filename": "src/c.py", "patch": "@@ -5,2 +5,2 @@\n+x\n+y\n-z\n-w"},
    {"filename": "img.png"},
]


def arm_review(gh: FakeGh, files: list | None = None, author: str = "someone-else") -> None:
    gh.respond("pr view", pr_json(author={"login": author}))
    gh.respond("api user", {"login": "bot-user"})
    gh.respond(REVIEWS_KEY, {"id": 1})
    gh.respond(FILES_KEY, PR_FILES if files is None else files)


def calls_for(gh: FakeGh, key: str) -> list[dict]:
    return [c for c in gh.calls if " ".join(c["argv"][:2]) == key]


def review_payloads(gh: FakeGh) -> list[dict]:
    return [json.loads(c["--input"]) for c in calls_for(gh, REVIEWS_KEY)]


def comments_posted(gh: FakeGh, index: int = -1) -> list[dict]:
    return review_payloads(gh)[index]["comments"]


# --- errors ------------------------------------------------------------------------


def test_missing_gh_raises_typed_error(fake_bin: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PATH", str(fake_bin))  # empty dir: no gh, no git
    with pytest.raises(github.GhMissing) as info:
        github.pr_for_branch(REPO, "lz/x")
    assert "gh auth login --scopes repo" in str(info.value)
    assert "required token scope: repo" in str(info.value)


def test_auth_failure_is_gh_auth(gh: FakeGh) -> None:
    gh.fail("pr list", "To get started with GitHub CLI, please run:  gh auth login\n", exit=4)
    with pytest.raises(github.GhAuth) as info:
        github.pr_for_branch(REPO, "lz/x")
    assert info.value.command[:3] == ("gh", "pr", "list")
    assert "gh auth login" in info.value.tail
    assert "required token scope: repo" in info.value.tail


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
                           "OPEN", "MERGEABLE", "someone-else")


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
    gh.respond(f"api repos/{REPO}/pulls/7", {})
    github.update_body(REPO, 7, "new body")
    assert gh.argv(0)[:4] == ["api", f"repos/{REPO}/pulls/7", "--method", "PATCH"]
    assert json.loads(gh.calls[0]["--input"]) == {"body": "new body"}


# --- reviews ---------------------------------------------------------------------------


def review(verdict: str = "request_changes", findings: tuple[Finding, ...] = ()) -> ReviewResult:
    return ReviewResult(family="codex", head=HEAD, kind="primary", verdict=verdict,
                        findings=findings, raw="RAW MODEL OUTPUT", model="gpt-5.3-codex",
                        duration_s=12.34)


FINDINGS = (
    Finding("critical", "src/a.py", 10, "Null deref", "x may be None"),
    Finding("important", "src/b.py", 2, "Missing check", "validate input"),
    Finding("suggestion", "src/c.py", 5, "Rename", "clearer name"),
    Finding("important", None, None, "Global concern", "no file"),
)


def test_post_review_payload(gh: FakeGh) -> None:
    arm_review(gh)
    github.post_review(REPO, 7, HEAD, review(findings=FINDINGS))
    assert calls_for(gh, FILES_KEY)[0]["argv"] == [
        "api", FILES_KEY.split(" ", 1)[1], "--method", "GET", "-F", "per_page=100", "-F", "page=1"]
    (call,) = calls_for(gh, REVIEWS_KEY)
    assert call["argv"][:5] == ["api", f"repos/{REPO}/pulls/7/reviews", "--method", "POST",
                                "--input"]
    payload = json.loads(call["--input"])
    assert payload["commit_id"] == HEAD and payload["event"] == "REQUEST_CHANGES"
    comments = payload["comments"]
    assert [(c["path"], c["line"], c["side"]) for c in comments] == [
        ("src/a.py", 10, "RIGHT"), ("src/b.py", 2, "RIGHT"), ("src/c.py", 5, "RIGHT"),
        ("src/a.py", 11, "RIGHT")]
    assert comments[0]["body"].split("\n")[0] == github.finding_marker("critical", HEAD, FINDINGS[0])
    assert comments[1]["body"].split("\n")[0] == github.finding_marker("important", HEAD, FINDINGS[1])
    assert "loopzero:finding" not in comments[2]["body"]
    assert "Null deref" in comments[0]["body"] and "x may be None" in comments[0]["body"]
    body = payload["body"]
    assert "model gpt-5.3-codex, 12.3s" in body
    assert "<details>" in body and "RAW MODEL OUTPUT" in body and "</details>" in body
    assert "Global concern" not in body, "blocking finding was anchored, not listed"
    assert all("subject_type" not in c for c in comments)


def test_post_review_anchors_unlocated_blocking_findings(gh: FakeGh) -> None:
    arm_review(gh)
    findings = (Finding("critical", None, None, "Global", "no file"),
                Finding("suggestion", None, None, "Nit", "loose"))
    github.post_review(REPO, 7, HEAD, review(findings=findings))
    payload = review_payloads(gh)[0]
    (comment,) = payload["comments"]
    assert comment == {**comment, "path": "src/a.py", "line": 11, "side": "RIGHT"}
    lines = comment["body"].split("\n")
    assert lines[0] == github.finding_marker("critical", HEAD, findings[0])
    assert lines[1] == "No file location given by the reviewer; anchored here."
    assert "Global" in comment["body"] and "no file" in comment["body"]
    assert "Nit" in payload["body"] and "Global" not in payload["body"]


def test_post_review_reroutes_findings_outside_diff(gh: FakeGh) -> None:
    arm_review(gh)
    findings = (
        Finding("important", "src/a.py", 99, "Off diff", "line not in hunk"),
        Finding("important", "src/zzz.py", 1, "Unknown file", "path not in PR"),
        Finding("critical", "src/c.py", 7, "Deleted side", "line 7 was only on the left"),
        Finding("suggestion", "src/a.py", 99, "Nit off diff", "stays loose"),
    )
    github.post_review(REPO, 7, HEAD, review(findings=findings))
    payload = review_payloads(gh)[0]
    comments = payload["comments"]
    assert [(c["path"], c["line"]) for c in comments] == [("src/a.py", 11)] * 3
    assert comments[0]["body"].split("\n")[1] == (
        "Reviewer location src/a.py:99 is not in the diff; anchored here.")
    assert "Nit off diff" in payload["body"]


def test_post_review_anchor_skips_files_without_hunks(gh: FakeGh) -> None:
    arm_review(gh, files=[{"filename": "img.png"},
                          {"filename": "src/d.py", "patch": "@@ -3,0 +4,2 @@\n+p\n+q"}])
    github.post_review(REPO, 7, HEAD, review(findings=(Finding("important", None, None, "G", ""),)))
    (comment,) = comments_posted(gh)
    assert (comment["path"], comment["line"]) == ("src/d.py", 4)
    assert "subject_type" not in comment


def test_post_review_fails_loud_when_nothing_to_anchor(gh: FakeGh) -> None:
    arm_review(gh, files=[{"filename": "img.png"}])
    with pytest.raises(github.GhError, match="no diff hunks"):
        github.post_review(REPO, 7, HEAD, review(findings=(Finding("critical", None, None, "G", ""),)))
    assert calls_for(gh, REVIEWS_KEY) == []


def test_post_review_paginates_files(gh: FakeGh) -> None:
    page1 = [{"filename": f"f{i}.py", "patch": f"@@ -1 +1 @@\n+l{i}"} for i in range(100)]
    arm_review(gh)
    gh.respond(FILES_KEY, page1, PR_FILES)
    github.post_review(REPO, 7, HEAD, review(findings=FINDINGS[:1]))
    assert [c["argv"][-1] for c in calls_for(gh, FILES_KEY)] == ["page=1", "page=2"]
    assert comments_posted(gh)[0]["path"] == "src/a.py"


def test_post_review_body_prefix_is_first_line(gh: FakeGh) -> None:
    arm_review(gh)
    github.post_review(REPO, 7, HEAD, review(), body_prefix="<!-- loopzero:review head=abc -->")
    body = review_payloads(gh)[0]["body"]
    assert body.split("\n")[0] == "<!-- loopzero:review head=abc -->"
    assert body.split("\n")[1].startswith("loopzero primary review")
    arm_review(gh)
    github.post_review(REPO, 7, HEAD, review())
    assert review_payloads(gh)[1]["body"].startswith("loopzero primary review")


def test_post_review_approve_event(gh: FakeGh) -> None:
    arm_review(gh)
    result = ReviewResult("codex", HEAD, "primary", "approve", (), "RAW", duration_s=0.25)
    github.post_review(REPO, 7, HEAD, result)
    payload = review_payloads(gh)[0]
    assert payload["event"] == "APPROVE" and payload["comments"] == []
    assert "model unknown, 0.2s: **approve**" in payload["body"]


@pytest.mark.parametrize("verdict,msg", [
    ("approve", "Can not approve your own pull request"),
    ("request_changes", "Can not request changes on your own pull request"),
])
def test_post_review_falls_back_to_comment_for_own_pr(gh: FakeGh, verdict: str, msg: str) -> None:
    """Hand fake: recording the 422 would require a forbidden write to the reference PR."""
    arm_review(gh)
    body = {"message": "Unprocessable Entity", "errors": [{"message": msg}],
            "documentation_url": "https://docs.github.com/rest"}
    gh.respond(REVIEWS_KEY,
               {"stdout": json.dumps(body), "stderr": "gh: Unprocessable Entity (HTTP 422)\n",
                "exit": 1},
               {"stdout": '{"id": 2}', "exit": 0})
    github.post_review(REPO, 7, HEAD, review(verdict=verdict, findings=FINDINGS[:1]))
    first, second = review_payloads(gh)
    assert (first["event"], second["event"]) == (verdict.upper(), "COMMENT")
    assert first["comments"] == second["comments"] and first["body"] == second["body"]


def test_gh_error_tail_joins_stderr_and_stdout(gh: FakeGh) -> None:
    gh.respond("pr ready", {"stdout": '{"message": "detail on stdout"}', "stderr": "gh: HTTP 422\n",
                            "exit": 1})
    with pytest.raises(github.GhError) as info:
        github.mark_ready(REPO, 7)
    assert info.value.tail == 'gh: HTTP 422\n\n{"message": "detail on stdout"}'


@pytest.mark.parametrize("verdict", ["approve", "request_changes"])
def test_post_review_posts_comment_directly_for_own_pr(gh: FakeGh, verdict: str) -> None:
    arm_review(gh, author="bot-user")
    github.post_review(REPO, 7, HEAD, review(verdict=verdict, findings=FINDINGS[:1]))
    (payload,) = review_payloads(gh)
    assert payload["event"] == "COMMENT"
    assert [c["argv"][:2] for c in gh.calls[:2]] == [["pr", "view"], ["api", "user"]]
    assert gh.argv(0) == ["pr", "view", "7", "--repo", REPO, "--json", github.PR_FIELDS]


def test_post_review_uses_given_pr_without_lookup(gh: FakeGh) -> None:
    arm_review(gh)
    pr = github._pr_from_json(pr_json(author={"login": "bot-user"}))
    github.post_review(REPO, 7, HEAD, review(verdict="approve"), pr=pr)
    assert calls_for(gh, "pr view") == []
    assert review_payloads(gh)[0]["event"] == "COMMENT"


def test_post_review_other_errors_propagate(gh: FakeGh) -> None:
    arm_review(gh)
    gh.fail(REVIEWS_KEY, "HTTP 500 server error")
    with pytest.raises(github.GhError):
        github.post_review(REPO, 7, HEAD, review())
    assert len(calls_for(gh, REVIEWS_KEY)) == 1


# --- blocking findings ------------------------------------------------------------


def test_open_blocking_findings_filters_and_parses(gh: FakeGh) -> None:
    versioned = github.finding_marker(
        "critical", HEAD, Finding("critical", "src/a.py", 3, "Null deref", "x may be None")
    )
    gh.respond("api user", {"login": "bot-user"})
    gh.respond("api graphql", threads_json(
        thread(f"{versioned}\n**critical: Null deref**\n\nx may be None"),
        thread(f"{marker('important')}\nplain title", resolved=True),
        thread(f"{marker('suggestion')}\n**suggestion: Rename**"),
        thread("human intro\n" + marker("critical") + "\n**critical: Human marker**"),
        thread("<!-- loopzero:finding severity=important -->\nheadless", path="h.py", line=9),
        thread(f"{marker('important')}\n**important: Missing check**\n\nvalidate",
               path="src/b.py", line=2),
    ))
    found = github.open_blocking_findings(REPO, 7, HEAD)
    assert found == [
        Finding("critical", "src/a.py", 3, "Null deref", "**critical: Null deref**\n\nx may be None"),
        Finding("critical", "src/a.py", 3, "Human marker", "**critical: Human marker**"),
        Finding("important", "h.py", 9, "headless", "headless"),
        Finding("important", "src/b.py", 2, "Missing check",
                "**important: Missing check**\n\nvalidate"),
    ]
    argv = gh.argv(0)
    assert argv[:2] == ["api", "graphql"]
    assert "-F" in argv and "owner=acme" in argv and "name=widgets" in argv and "number=7" in argv
    assert any(a.startswith("query=") and "lastEditedAt" not in a and "author" not in a for a in argv)


@pytest.mark.parametrize("data", [
    {"data": {"repository": {"pullRequest": None}}},
    {"data": {"repository": None}},
    {"data": {"repository": {"pullRequest": {"reviewThreads": {"nodes": []}}}}},
    {"errors": [{"message": "nope"}]},
])
def test_open_blocking_findings_missing_pr_is_gh_error(gh: FakeGh, data: dict) -> None:
    gh.respond("api graphql", data)
    with pytest.raises(github.GhError, match="PR #7 not found or not accessible"):
        github.open_blocking_findings(REPO, 7, HEAD)


def test_pr_for_branch_missing_key_is_gh_error(gh: FakeGh) -> None:
    gh.respond("pr list", [{"number": 7, "url": "u"}])
    with pytest.raises(github.GhError, match="PR #7 not found or not accessible"):
        github.pr_for_branch(REPO, "lz/x")


def test_api_get_and_login_helpers(gh: FakeGh) -> None:
    gh.respond("api user", {"login": "bot-user"})
    gh.respond("api repos/acme/widgets", {"default_branch": "main"})
    assert github.api_get("repos/acme/widgets") == {"default_branch": "main"}
    assert gh.argv(0) == ["api", "repos/acme/widgets"]
    assert github.login() == "bot-user"
    gh.respond("api user", {})
    with pytest.raises(github.GhError, match="no login"):
        github.login()


def test_marker_parser_accepts_old_and_v1_forms() -> None:
    old = github.MARKER_RE.search("<!-- loopzero:finding severity=critical -->")
    assert old and old.groupdict() == {
        "version": None, "severity": "critical", "head": None, "id": None,
    }
    finding = Finding("important", "src/b.py", 2, "Missing check", "validate")
    new = github.MARKER_RE.search(github.finding_marker("important", HEAD, finding))
    assert new and new.groupdict() == {
        "version": "1", "severity": "important", "head": HEAD,
        "id": github.finding_id(HEAD, "src/b.py", 2, "Missing check"),
    }


def test_finding_id_is_deterministic_and_uses_location_and_title() -> None:
    expected = "aa3b81ec"
    assert github.finding_id(HEAD, "src/a.py", 10, "Null deref") == expected
    assert github.finding_id(HEAD, "src/a.py", 10, "Null deref") == expected
    assert github.finding_id(HEAD, "src/a.py", 11, "Null deref") != expected


def test_open_blocking_findings_counts_only_explicit_blocking_markers(gh: FakeGh) -> None:
    gh.respond("api graphql", threads_json(
        thread(f"{marker('suggestion')}\n**suggestion: nit**", edited="2026-09-18T01:00:00Z"),
        thread(f"{marker('critical')}\nX", edited="2026-09-18T01:00:00Z", path="e.py", line=4),
    ))
    found = github.open_blocking_findings(REPO, 7, HEAD)
    assert [(f.severity, f.path, f.line, f.title) for f in found] == [
        ("critical", "e.py", 4, "X"),
    ]
    assert all(c["argv"][:2] == ["api", "graphql"] for c in gh.calls), "no login lookup needed"


def test_open_blocking_findings_does_not_guess_from_body_or_author(gh: FakeGh) -> None:
    gh.respond("api graphql", threads_json(
        thread("stripped loopzero marker\n**critical: X**", author="bot-user"),
        thread("mentions loopzero but a human wrote it", author="human"),
        thread("bot-user wrote this but it is unrelated", author="bot-user"),
    ))
    found = github.open_blocking_findings(REPO, 7, HEAD)
    assert found == []
    assert all(c["argv"] != ["api", "user"] for c in gh.calls)


def test_open_blocking_findings_paginates(gh: FakeGh) -> None:
    gh.respond("api graphql",
               threads_json(thread(f"{marker('critical')}\nA"), has_next=True, cursor="C1"),
               threads_json(thread(f"{marker('important')}\nB")))
    found = github.open_blocking_findings(REPO, 7, HEAD)
    assert [f.title for f in found] == ["A", "B"]
    assert "after=C1" not in gh.argv(0) and "after=C1" in gh.argv(1)


# --- checks --------------------------------------------------------------------------------


def test_gh_api_reads_with_fields_use_method_get(gh: FakeGh) -> None:
    arm_review(gh)
    github.post_review(REPO, 7, HEAD, review(findings=FINDINGS[:1]))
    gh.respond(f"api repos/{REPO}/commits/{HEAD}/check-runs", {"check_runs": []})
    gh.respond(f"api repos/{REPO}/commits/{HEAD}/statuses", [])
    github.check_runs(REPO, HEAD)
    reads = [c["argv"] for c in gh.calls if c["argv"][0] == "api" and "-F" in c["argv"]]
    assert len(reads) == 3, "files, check-runs and status all pass -F"
    for argv in reads:
        assert argv[argv.index("--method") + 1] == "GET", argv


def test_check_runs_merges_runs_and_statuses(gh: FakeGh) -> None:
    filler = [{"name": f"job{i}", "status": "completed", "conclusion": "success"}
              for i in range(99)]
    gh.respond(f"api repos/{REPO}/commits/{HEAD}/check-runs",
               {"check_runs": filler + [
                   {"name": "checks", "status": "completed", "conclusion": "success"}]},
               {"check_runs": [{"name": "lint", "status": "in_progress", "conclusion": None}]})
    gh.respond(f"api repos/{REPO}/commits/{HEAD}/statuses", [
        {"context": "ci/legacy", "state": "failure"},
        {"context": "checks", "state": "pending"},
    ])
    runs = github.check_runs(REPO, HEAD)
    assert {k: v for k, v in runs.items() if not k.startswith("job")} == {
        "checks": "pending", "lint": "pending", "ci/legacy": "failure"}
    pages = [c["argv"][-1] for c in gh.calls if "check-runs" in c["argv"][1]]
    assert pages == ["page=1", "page=2"]


def test_check_runs_stale_skipped_newer_success_is_success(gh: FakeGh) -> None:
    runs = [
        {"id": 10, "name": "checks", "status": "completed", "conclusion": "skipped"},
        {"id": 11, "name": "checks", "status": "completed", "conclusion": "success"},
    ]
    gh.respond(f"api repos/{REPO}/commits/{HEAD}/check-runs", {"check_runs": runs})
    gh.respond(f"api repos/{REPO}/commits/{HEAD}/statuses", [])
    assert github.check_runs(REPO, HEAD)["checks"] == "success"


def test_check_runs_newer_pending_older_success_is_pending(gh: FakeGh) -> None:
    runs = [
        {"id": 20, "name": "checks", "status": "in_progress", "conclusion": None},
        {"id": 19, "name": "checks", "status": "completed", "conclusion": "success"},
    ]
    gh.respond(f"api repos/{REPO}/commits/{HEAD}/check-runs", {"check_runs": runs})
    gh.respond(f"api repos/{REPO}/commits/{HEAD}/statuses", [])
    assert github.check_runs(REPO, HEAD)["checks"] == "pending"


def test_check_runs_two_names_one_failing_is_failure(gh: FakeGh) -> None:
    runs = [
        {"id": 30, "name": "checks", "status": "completed", "conclusion": "success"},
        {"id": 31, "name": "lint", "status": "completed", "conclusion": "failure"},
    ]
    gh.respond(f"api repos/{REPO}/commits/{HEAD}/check-runs", {"check_runs": runs})
    gh.respond(f"api repos/{REPO}/commits/{HEAD}/statuses", [])
    assert github.check_runs(REPO, HEAD) == {"checks": "success", "lint": "failure"}


def test_check_runs_ties_ids_by_started_at(gh: FakeGh) -> None:
    runs = [
        {"id": 40, "name": "checks", "started_at": "2026-09-18T01:00:00Z",
         "status": "completed", "conclusion": "failure"},
        {"id": 40, "name": "checks", "started_at": "2026-09-18T02:00:00Z",
         "status": "completed", "conclusion": "success"},
    ]
    gh.respond(f"api repos/{REPO}/commits/{HEAD}/check-runs", {"check_runs": runs})
    gh.respond(f"api repos/{REPO}/commits/{HEAD}/statuses", [])
    assert github.check_runs(REPO, HEAD)["checks"] == "success"


def test_check_runs_paginates_statuses_and_keeps_latest_context(gh: FakeGh) -> None:
    gh.respond(f"api repos/{REPO}/commits/{HEAD}/check-runs", {"check_runs": []})
    page = [{"context": f"old-{i}", "state": "success"} for i in range(99)]
    page += [{"context": "duplicate", "state": "failure"}]
    gh.respond(
        f"api repos/{REPO}/commits/{HEAD}/statuses",
        page,
        [{"context": "required-page-two", "state": "success"},
         {"context": "duplicate", "state": "success"}],
    )
    runs = github.check_runs(REPO, HEAD)
    assert runs["required-page-two"] == "success"
    assert runs["duplicate"] == "failure", "the first (latest) status wins"


# --- readiness ----------------------------------------------------------------------------


def arm_readiness(gh: FakeGh, *, threads=(), runs=None) -> None:
    gh.respond("api graphql", threads_json(*threads))
    runs = {"checks": "success"} if runs is None else runs
    gh.respond(f"api repos/{REPO}/commits/{HEAD}/check-runs", {"check_runs": [
        {"name": n, "status": "completed", "conclusion": c} for n, c in runs.items()]})
    gh.respond(f"api repos/{REPO}/commits/{HEAD}/statuses", [])


def test_readiness_ready(gh: FakeGh) -> None:
    arm_readiness(gh)
    pr = github._pr_from_json(pr_json())
    assert github.readiness(REPO, pr, "main", ("checks",), HEAD) == github.Readiness(True, ())


def test_readiness_head_moved(gh: FakeGh) -> None:
    arm_readiness(gh)
    r = github.readiness(REPO, github._pr_from_json(pr_json()), "main", ("checks",), "b" * 40)
    assert not r.ready and any("differs from reviewed" in x for x in r.reasons)


def test_readiness_no_review(gh: FakeGh) -> None:
    arm_readiness(gh)
    r = github.readiness(REPO, github._pr_from_json(pr_json()), "main", ("checks",), None)
    assert r.reasons == ("no review recorded for the current head",)


def test_readiness_blocking_finding(gh: FakeGh) -> None:
    arm_readiness(gh, threads=(thread(f"{marker('important')}\n**important: Oops**"),))
    r = github.readiness(REPO, github._pr_from_json(pr_json()), "main", ("checks",), HEAD)
    assert r.reasons == ("open important finding at src/a.py:3: Oops",)


def test_readiness_blocks_on_anchored_unlocated_finding(gh: FakeGh) -> None:
    body = f"{marker('critical')}\nNo file location given by the reviewer; anchored here.\n\n" \
           "**critical: Global**\n\nno file"
    arm_readiness(gh, threads=(thread(body, path="src/first.py", line=12),))
    r = github.readiness(REPO, github._pr_from_json(pr_json()), "main", ("checks",), HEAD)
    assert not r.ready
    assert r.reasons == ("open critical finding at src/first.py:12: Global",)


def test_readiness_resolved_or_suggestion_threads_do_not_block(gh: FakeGh) -> None:
    arm_readiness(gh, threads=(thread(f"{marker('critical')}\nX", resolved=True),
                               thread(f"{marker('suggestion')}\nY")))
    assert github.readiness(REPO, github._pr_from_json(pr_json()), "main", ("checks",), HEAD).ready


def test_readiness_uses_head_or_non_outdated_rule_and_resolved_never_blocks(
    gh: FakeGh,
) -> None:
    stale = "b" * 40
    arm_readiness(gh, threads=(
        thread(f"{marker('important', stale)}\n**important: Still applies**"),
        thread(f"{marker('critical', stale)}\n**critical: Obsolete**", outdated=True),
        thread(f"{marker('critical')}\n**critical: Current**", outdated=True),
        thread(f"{marker('critical')}\n**critical: Resolved**", resolved=True),
    ))
    result = github.readiness(REPO, github._pr_from_json(pr_json()), "main", ("checks",), HEAD)
    assert result.reasons == (
        "open important finding at src/a.py:3: Still applies",
        "open critical finding at src/a.py:3: Current",
    )


def test_readiness_missing_and_failed_checks(gh: FakeGh) -> None:
    arm_readiness(gh, runs={"checks": "failure"})
    r = github.readiness(
        REPO, github._pr_from_json(pr_json()), "main", ("checks", "e2e"), HEAD
    )
    assert r.reasons == (
        "required check 'checks' is failure",
        f"required check 'e2e' missing on {HEAD[:12]}",
    )


def test_readiness_conflicting(gh: FakeGh) -> None:
    arm_readiness(gh)
    pr = github._pr_from_json(pr_json(mergeable="CONFLICTING"))
    r = github.readiness(REPO, pr, "main", ("checks",), HEAD)
    assert r.reasons == ("PR #7 has merge conflicts with main",)


def test_readiness_pr_not_open(gh: FakeGh) -> None:
    arm_readiness(gh)
    r = github.readiness(
        REPO, github._pr_from_json(pr_json(state="MERGED")), "main", ("checks",), HEAD
    )
    assert r.reasons == ("PR #7 is MERGED, not open",)


def test_readiness_without_required_ci_skips_check_lookup(gh: FakeGh) -> None:
    gh.respond("api graphql", threads_json())
    assert github.readiness(REPO, github._pr_from_json(pr_json()), "main", (), HEAD).ready
    assert [c["argv"][:2] for c in gh.calls] == [["api", "graphql"]]


# --- ready / merge ---------------------------------------------------------------------


def test_mark_ready(gh: FakeGh) -> None:
    gh.respond("pr ready", "")
    github.mark_ready(REPO, 7)
    assert gh.argv(0) == ["pr", "ready", "7", "--repo", REPO]


def test_merge_success_replays_recorded_verification(
    gh: FakeGh,
) -> None:
    gh.expect(
        ["pr", "merge", "7", "--repo", REPO, "--squash", "--match-head-commit", HEAD]
    )
    gh.load_scenario("pr_view_merged", {
        "<REPO>": REPO, "<PR>": "7", "<SHA_1>": "c" * 40,
    })
    assert github.merge(REPO, 7, "squash", HEAD) == "c" * 40
    assert gh.argv(0) == ["pr", "merge", "7", "--repo", REPO, "--squash",
                          "--match-head-commit", HEAD]
    assert "--delete-branch" not in gh.argv(0)
    assert gh.argv(1) == ["pr", "view", "7", "--repo", REPO, "--json",
                          "mergeCommit,state,headRefName"]
    gh.assert_complete()


def test_merge_command_failure(gh: FakeGh) -> None:
    gh.fail("pr merge", "head commit mismatch")
    with pytest.raises(github.MergeFailed) as info:
        github.merge(REPO, 7, "rebase", HEAD)
    assert "--rebase" in info.value.command and "mismatch" in info.value.tail


@pytest.mark.parametrize("status", [404, 422])
def test_delete_remote_branch_accepts_already_deleted_ref(gh: FakeGh, status: int) -> None:
    gh.fail("api -X", f"HTTP {status}: Reference does not exist")
    github.delete_remote_branch(REPO, "lz/x")


def test_merge_unverified(gh: FakeGh) -> None:
    gh.respond("pr merge", "")
    gh.respond("pr view", {"state": "OPEN", "mergeCommit": None, "headRefName": "lz/x"})
    with pytest.raises(github.MergeFailed):
        github.merge(REPO, 7, "merge", HEAD)
    assert not any(c["argv"][:2] == ["api", "-X"] for c in gh.calls), "no ref delete"


def test_merge_rejects_unknown_strategy(gh: FakeGh) -> None:
    with pytest.raises(github.MergeFailed):
        github.merge(REPO, 7, "fast-forward", HEAD)
    assert gh.calls == []
