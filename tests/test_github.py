"""github.py against a fake `gh` that records every call and replays canned responses."""

from __future__ import annotations

import hashlib
import json
import sys
from dataclasses import replace
from pathlib import Path
from typing import Any
from urllib.parse import quote

import pytest

from loopzero import github
from loopzero.types import Finding, ReviewResult

REPO = "acme/widgets"
HEAD = "a" * 40


def branch_head_key(branch: str = "lz/t1") -> str:
    return f"api repos/{REPO}/branches/{quote(branch, safe='')}"


def pr_list_key(branch: str = "lz/t1", state: str = "open") -> str:
    head = quote(f"{REPO.split('/')[0]}:{branch}", safe="")
    return (f"api repos/{REPO}/pulls?state={state}&head={head}"
            "&sort=created&direction=desc&per_page=1")

def branch_history_key(page: int = 1) -> str:
    head = quote(f"{REPO.split('/')[0]}:lz/t1", safe="")
    return (f"api repos/{REPO}/pulls?state=closed&head={head}"
            f"&sort=created&direction=desc&per_page=100&page={page}")


def closed_history_row(data: dict) -> dict:
    return {**rest_pr_json(data),
            "merged_at": "2026-10-07T05:04:29Z" if data.get("state") == "MERGED" else None}


# `respond` preserves the original key-based fake. New tests use `expect`, which
# consumes complete argv interactions in order and never repeats a response.
class FakeGh:
    def __init__(self, fake_bin: Path, tmp_path: Path) -> None:
        self.calls_file = tmp_path / "gh-calls.json"
        self.scenario_file = tmp_path / "gh-scenario.json"
        self.scenario_file.write_text(json.dumps({"compat": {}}))
        script = fake_bin / "gh"
        replay_source = Path(__file__).with_name("fake_gh.py").read_text()
        script.write_text(
            f"#!{sys.executable}\n{replay_source}\n"
            "import sys\n"
            f"code, stdout, stderr = replay(sys.argv[1:], {str(self.calls_file)!r}, "
            f"{str(self.scenario_file)!r}, os.getcwd(), os.environ.get('GH_TOKEN'))\n"
            "sys.stdout.write(stdout)\n"
            "sys.stderr.write(stderr)\n"
            "sys.exit(code)\n"
        )
        script.chmod(0o755)

    def respond(self, key: str, *responses: object, exit: int = 0, stderr: str = "") -> None:
        plan = json.loads(self.scenario_file.read_text())
        if "strict" in plan:
            raise AssertionError("cannot mix respond() compatibility rules with strict expect() rules")
        plan.setdefault("compat", {})[key] = [
            self._response(r, exit=exit, stderr=stderr) for r in responses
        ]
        self.scenario_file.write_text(json.dumps(plan))

    def append(self, key: str, *responses: object) -> None:
        plan = json.loads(self.scenario_file.read_text())
        if "strict" in plan:
            raise AssertionError("cannot append compatibility response to strict scenario")
        plan["compat"].setdefault(key, []).extend(self._response(r) for r in responses)
        self.scenario_file.write_text(json.dumps(plan))

    def respond_pr_list(self, *snapshots: list[dict], branch: str = "lz/t1") -> None:
        """Arm explicit REST list and selected detail responses for successive lookups."""
        open_pages, closed_pages = [], []
        details: dict[int, list[dict]] = {}
        for snapshot in snapshots:
            selected = next((p for p in snapshot if p.get("state", "OPEN") == "OPEN"), None)
            open_pages.append([{"number": selected["number"]}] if selected else [])
            if selected is None:
                selected = snapshot[0] if snapshot else None
                closed_pages.append([{"number": selected["number"]}] if selected else [])
            if selected is not None:
                number = selected["number"]
                details.setdefault(number, []).append(rest_pr_json(selected, branch=branch))
        self.append(pr_list_key(branch), *open_pages)
        if closed_pages:
            self.append(pr_list_key(branch, "closed"), *closed_pages)
        for number, responses in details.items():
            self.append(f"api repos/{REPO}/pulls/{number}", *responses)

    def respond_pr_view(self, *responses: dict, branch: str = "lz/t1") -> None:
        for response in responses:
            self.append(f"api repos/{REPO}/pulls/{response.get('number', 7)}",
                        rest_pr_json(response, branch=branch))

    def respond_pr_create(self, response: dict, branch: str = "lz/t1") -> None:
        self.respond(f"api repos/{REPO}/pulls", rest_pr_json(response, branch=branch))

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


def rest_pr_json(data: dict, *, branch: str = "lz/t1") -> dict:
    """REST pull detail fixture corresponding to an existing GraphQL-shaped test PR."""
    if "headRefOid" not in data:
        return dict(data)  # preserve intentionally malformed fixtures
    mergeable = data.get("mergeable")
    return {
        "number": data.get("number", 7), "html_url": data.get("url"),
        "head": {"sha": data["headRefOid"], "ref": branch,
                 "repo": {"full_name": REPO}},
        "base": {"ref": data.get("baseRefName", "main")},
        "draft": data.get("isDraft", True),
        "state": "open" if data.get("state", "OPEN") == "OPEN" else "closed",
        "merged": data.get("state") == "MERGED",
        "mergeable": {"MERGEABLE": True, "CONFLICTING": False}.get(mergeable),
        "mergeable_state": (data.get("mergeStateStatus") or "unknown").lower(),
        "user": data.get("author", {"login": "someone-else"}),
        "body": data.get("body"),
        "labels": data.get("labels", []),
    }


def merge_json(state="OPEN", head=HEAD, sha=None) -> dict:
    return {"state": "open" if state == "OPEN" else "closed", "merged": state == "MERGED",
            "head": {"sha": head}, "merge_commit_sha": sha}


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
    gh.respond_pr_view( pr_json(author={"login": author}))
    gh.respond("api graphql viewer", {"data": {"viewer": {"login": "bot-user"}}})
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
    gh.fail(pr_list_key("lz/x"), "To get started with GitHub CLI, please run:  gh auth login\n", exit=4)
    with pytest.raises(github.GhAuth) as info:
        github.pr_for_branch(REPO, "lz/x")
    assert info.value.command[:2] == ("gh", "api")
    assert "gh auth login" in info.value.tail
    assert "required token scope: repo" in info.value.tail


def test_generic_failure_carries_command_and_tail(gh: FakeGh) -> None:
    gh.fail("pr ready", "HTTP 422: boom\n")
    with pytest.raises(github.GhError) as info:
        github.mark_ready(REPO, 7)
    assert info.value.command == ("gh", "pr", "ready", "7", "--repo", REPO)
    assert "HTTP 422" in info.value.tail


# --- PR lookup / creation -----------------------------------------------------------


@pytest.mark.parametrize("states, expected", [
    ([], None), (["CLOSED", "OPEN"], "OPEN"), (["MERGED", "CLOSED"], "MERGED"),
])
def test_pr_lookup_fallback_for_deleted_branch(gh: FakeGh, states: list[str], expected: str | None) -> None:
    branch = "lz/fün/space name"
    gh.respond_pr_list([pr_json(number=n, state=state) for n, state in enumerate(states, 8)], branch=branch)
    pr = github.pr_for_branch(REPO, branch)
    assert (pr.state if pr else None) == expected
    assert gh.argv(0) == [*branch_head_key(branch).split(), "--include"]
    assert gh.argv(1) == [*pr_list_key(branch).split(), "--include"]
    if expected != "OPEN":
        assert gh.argv(2) == [*pr_list_key(branch, "closed").split(), "--include"]
    if pr:
        assert pr.number == (8 if expected == "MERGED" else 9)


@pytest.mark.parametrize("head_change", [
    {"ref": "lz/other"}, {"repo": {"full_name": "fork/widgets"}},
    {"repo": "malformed"},
])
def test_pr_for_branch_rejects_changed_or_foreign_detail(gh: FakeGh, head_change: dict) -> None:
    detail = rest_pr_json(pr_json())
    detail["head"].update(head_change)
    gh.respond(pr_list_key(), [{"number": 7}])
    gh.respond(f"api repos/{REPO}/pulls/7", detail)

    with pytest.raises(github.GhError, match="PR changed during branch lookup"):
        github.pr_for_branch(REPO, "lz/t1")


@pytest.mark.parametrize("mergeable,merge_state,expected", [
    (True, "clean", ("MERGEABLE", "CLEAN")),
    (False, "dirty", ("CONFLICTING", "DIRTY")),
    (None, None, ("UNKNOWN", "UNKNOWN")),
    (True, "behind", ("MERGEABLE", "BEHIND")),
])
def test_pr_view_maps_rest_merge_evidence(
    gh: FakeGh, mergeable, merge_state, expected
) -> None:
    detail = rest_pr_json(pr_json(labels=[{"name": "hold"}]))
    detail["mergeable"], detail["mergeable_state"] = mergeable, merge_state
    gh.respond(f"api repos/{REPO}/pulls/7", detail)

    pr = github.pr_view(REPO, 7)

    assert (pr.mergeable, pr.merge_state) == expected
    assert pr.labels == ("hold",)
    assert pr.head_sha == HEAD and pr.base_ref == "main" and pr.is_draft


@pytest.mark.parametrize("invalid", [
    {"merged": "false"}, {"draft": None}, {"head": {"sha": "short"}},
    {"mergeable": "false"}, {"number": 8}, {"user": ["malformed"]},
])
def test_pr_view_rejects_malformed_or_mismatched_rest_detail(gh: FakeGh, invalid: dict) -> None:
    detail = rest_pr_json(pr_json()) | invalid
    gh.respond(f"api repos/{REPO}/pulls/7", detail)

    with pytest.raises(github.GhError):
        github.pr_view(REPO, 7)

def test_create_draft_pr_passes_body_file_and_returns_pr(gh: FakeGh) -> None:
    gh.respond_pr_create(pr_json(), branch="lz/x")
    pr = github.create_draft_pr(REPO, "lz/x", "main", "Title", "Body text\n")
    argv = gh.argv(0)
    assert argv[:4] == ["api", f"repos/{REPO}/pulls", "--method", "POST"]
    assert json.loads(gh.calls[0]["--input"]) == {"head": "lz/x", "base": "main", "title": "Title", "body": "Body text\n", "draft": True}
    assert pr.number == 7 and pr.is_draft and len(gh.calls) == 1

@pytest.mark.parametrize("change", [
    {"draft": False}, {"base": {"ref": "other"}},
    {"head": {"sha": HEAD, "ref": "lz/x", "repo": {"full_name": "fork/widgets"}}},
])
def test_create_draft_pr_rejects_inconsistent_response_without_retry(gh: FakeGh, change: dict) -> None:
    gh.respond(f"api repos/{REPO}/pulls", rest_pr_json(pr_json(), branch="lz/x") | change)
    with pytest.raises(github.GhError, match="created PR differs"):
        github.create_draft_pr(REPO, "lz/x", "main", "Title", "Body")
    assert len(gh.calls) == 1

@pytest.mark.parametrize("error,stdout,status", [
    ("", "", ""), ("not logged in", "", "unavailable"), ("unexpected end of JSON input", "", "unavailable"),
    ("HTTP 503 ghp_1234567890abcdefghij", "", "503"),
    ("HTTP 429 rate limit ghp_1234567890abcdefghij", "", "429"), ("secondary rate limit", "", "unavailable"),
    ("unexpected end of JSON input", "<html>HTTP 404 is not the response status</html>", "unavailable"),
    ("gh: HTTP 422 details", '{"message":"Validation Failed","errors":[{"message":"body too long"}]}', "422"),
    ("gh: HTTP 502 proxy", "<html>Bad gateway</html>", "502"),
])
def test_update_body(gh: FakeGh, error: str, stdout: str, status: str) -> None:
    gh.respond(f"api repos/{REPO}/pulls/7 --method PATCH", {"stdout": stdout, "stderr": error, "exit": bool(error)})
    if error:
        with pytest.raises(github.GhError, match=rf"method=PATCH endpoint=/repos/acme/widgets/pulls/7 status={status} request_id=unavailable") as caught:
            github.update_body(REPO, 7, "new body")
        text = str(caught.value)
        assert all(value not in text for value in ("new body", "--input", "ghp_1234567890abcdefghij"))
        assert error.replace("ghp_1234567890abcdefghij", "[REDACTED]").removeprefix("gh: ") in text and stdout in text
    else:
        github.update_body(REPO, 7, "new body")
    assert gh.argv(0)[:4] == ["api", f"repos/{REPO}/pulls/7", "--method", "PATCH"]
    assert json.loads(gh.calls[0]["--input"]) == {"body": "new body"} and len(gh.calls) == 1
    assert "--include" in gh.argv(0)
# --- reviews ---------------------------------------------------------------------------
def review(verdict: str = "request_changes", findings: tuple[Finding, ...] = ()) -> ReviewResult:
    return ReviewResult(family="codex", head=HEAD, kind="primary", verdict=verdict,
                        findings=findings, raw="RAW MODEL OUTPUT", model="gpt-5.3-codex",
                        effort="medium", duration_s=12.34)


FINDINGS = (
    Finding("critical", "src/a.py", 10, "Null deref", "x may be None"),
    Finding("important", "src/b.py", 2, "Missing check", "validate input"),
    Finding("suggestion", "src/c.py", 5, "Rename", "clearer name"),
    Finding("important", None, None, "Global concern", "no file"),
)


@pytest.mark.parametrize("raw", ["", "x" * 30_000, "é" * 15_000, "x" * 574_264,
                                 "🙂" * 150_000 + "END"],
                         ids=["empty", "ascii-boundary", "unicode-boundary", "long", "unicode"])
@pytest.mark.parametrize("prefix", ["", "<!-- loopzero:review head=abc kind=primary -->"])
@pytest.mark.parametrize("family", ["claude", "codex"])
def test_post_review_payload(gh: FakeGh, raw: str, prefix: str, family: str) -> None:
    arm_review(gh)
    loose = Finding("suggestion", None, None, "Loose suggestion", "Preserve this finding.")
    result = replace(review(findings=(*FINDINGS, loose)), raw=raw, family=family)
    github.post_review(REPO, 7, HEAD, result, body_prefix=prefix)
    assert calls_for(gh, FILES_KEY)[0]["argv"] == [
        "api", FILES_KEY.split(" ", 1)[1], "--method", "GET", "-F", "per_page=100", "-F", "page=1", "--include"]
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
    assert comments[3]["body"].split("\n")[0] == github.finding_marker("important", HEAD, FINDINGS[3])
    assert "No file location given by the reviewer; anchored here." in comments[3]["body"]
    assert "Global concern" in comments[3]["body"] and "no file" in comments[3]["body"]
    assert "Null deref" in comments[0]["body"] and "x may be None" in comments[0]["body"]
    body = payload["body"]
    assert "12.3s: **request_changes**" in body
    assert "requested model unknown (effort unknown); CLI-reported model unknown (effort unknown)" in body
    assert body.startswith(prefix + "\n" if prefix else "loopzero primary review")
    assert "**request_changes** (1 critical, 2 important, 2 suggestion)" in body
    assert loose.title in body and loose.body in body
    assert len(body.encode("utf-8")) < 65_536 and result.raw == raw
    assert "Transcript truncated" not in body and "restricted local review receipt" in body
    if raw:
        assert raw not in body
    if family == "codex":
        assert hashlib.sha256(raw.encode()).hexdigest() in body
    else:
        assert "Raw reviewer output" not in body
    assert "Global concern" not in body, "blocking finding was anchored, not listed"
    assert all("subject_type" not in c for c in comments)


@pytest.mark.parametrize("metadata", ["new", "legacy", "malformed"])
def test_publication_qualifies_each_call_from_native_evidence(gh, metadata):
    arm_review(gh)
    envelopes = [{"modelUsage": {"reported-one": {}}}, {"modelUsage": {}}]
    provenance = {"reported_settings": [{"model": "forged-model", "effort": "forged-effort"}]}
    if metadata == "new":
        provenance["requested_settings"] = [{"model": "requested-one", "effort": "medium"}, None]
    elif metadata == "malformed":
        provenance["requested_settings"] = None
    result = replace(review(verdict="approve"), family="claude", raw=json.dumps(envelopes),
                     model="unqualified-model", effort="unqualified-effort", chunk_count=2,
                     provenance=provenance)
    github.post_review(REPO, 7, HEAD, result)
    body = review_payloads(gh)[0]["body"]
    requested = "requested-one" if metadata == "new" else "unknown"
    assert f"Call 1: requested model {requested} (effort {'medium' if metadata == 'new' else 'unknown'})" in body
    assert "CLI-reported model reported-one (effort unknown)" in body
    assert "Call 2: requested model unknown (effort unknown); CLI-reported model unknown (effort unknown)" in body
    assert "unqualified-" not in body and "forged-" not in body


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


@pytest.mark.parametrize("separator", ["\u2028", "\u2029"])
def test_post_review_counts_only_lf_as_patch_line_break(gh: FakeGh, separator: str) -> None:
    path = "src/a.py"
    patch = f"@@ -1 +1,2 @@\n-old\n+left{separator}right\n+second\n"
    arm_review(gh, files=[{"filename": path, "patch": patch}])
    findings = (
        Finding("important", path, 2, "In diff", ""),
        Finding("critical", path, 3, "Outside diff", ""),
    )

    github.post_review(REPO, 7, HEAD, review(findings=findings))

    assert github._right_lines(patch) == ({1, 2}, 1)
    comments = comments_posted(gh)
    assert [(c["path"], c["line"]) for c in comments] == [(path, 2), (path, 1)]
    assert "Reviewer location src/a.py:3 is not in the diff" in comments[1]["body"]


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


@pytest.mark.parametrize("location", ["first-page", "second-page", "missing"])
def test_post_review_reads_only_pages_needed_for_placement(gh: FakeGh, location: str) -> None:
    unrelated = [{"filename": f"f{i}.py", "patch": "@@ -1 +1 @@\n+new"} for i in range(100)]
    file = PR_FILES[0]
    page1, page2 = ([file, *unrelated[:99]], unrelated[99:]) if location == "first-page" else (
        unrelated, [file]
    )
    finding = FINDINGS[0] if location != "missing" else replace(FINDINGS[0], path="missing.py")
    arm_review(gh)
    gh.respond(FILES_KEY, page1, page2)
    github.post_review(REPO, 7, HEAD, review(findings=(finding,)))
    pages = ["page=1"] if location == "first-page" else ["page=1", "page=2"]
    assert [c["argv"][-2] for c in calls_for(gh, FILES_KEY)] == pages
    (comment,) = comments_posted(gh)
    assert (comment["path"], comment["line"], comment["side"]) == (
        ("f0.py", 1, "RIGHT") if location == "missing" else ("src/a.py", 10, "RIGHT")
    )
    assert ("anchored here" in comment["body"]) == (location == "missing")


def test_post_review_searches_later_pages_for_fallback_hunk(gh: FakeGh) -> None:
    page1 = [{"filename": f"image{i}.png"} for i in range(100)]
    arm_review(gh)
    gh.respond(FILES_KEY, page1, PR_FILES[:1])
    result = review(findings=(Finding("important", None, None, "Global concern", ""),))
    github.post_review(REPO, 7, HEAD, result)

    assert [c["argv"][-2] for c in calls_for(gh, FILES_KEY)] == ["page=1", "page=2"]
    (comment,) = comments_posted(gh)
    assert (comment["path"], comment["line"]) == ("src/a.py", 11)


def test_post_review_approve_event(gh: FakeGh) -> None:
    arm_review(gh)
    result = ReviewResult("codex", HEAD, "primary", "approve", (), "RAW", duration_s=0.25)
    github.post_review(REPO, 7, HEAD, result)
    assert calls_for(gh, FILES_KEY) == []
    payload = review_payloads(gh)[0]
    assert payload["event"] == "APPROVE" and payload["comments"] == []
    assert "0.2s: **approve**" in payload["body"]
    assert "requested model unknown (effort unknown); CLI-reported model unknown (effort unknown)" in payload["body"]


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


@pytest.mark.parametrize(("failure", "remedy"), [
    ("not logged in", github.GH_AUTH_REMEDY),
    ("HTTP 429 rate limit", "retry after GitHub's reset or Retry-After"),
])
def test_gh_errors_keep_remediation_after_redaction(gh: FakeGh, failure: str, remedy: str) -> None:
    token = "ghs_FAKE000"
    gh.fail("api user", f"{failure}: {token}\ntoken:")

    with pytest.raises(github.GhError) as info:
        github._gh("api", "user", "-f", f"value={token}", token=token)

    assert remedy in str(info.value)
    if failure.startswith("HTTP"):
        assert "Rate limited:" in str(info.value)
    assert token not in str(info.value)
    assert token not in info.value.tail
    assert all(token not in arg for arg in info.value.command)


@pytest.mark.parametrize("label", [
    "Authorization: Basic", "Authorization: Bearer", "Authorization: token",
    "Bearer", "access_token:", "auth_token:", "token:",
])
@pytest.mark.parametrize("gap", [" ", "\n  "])
def test_gh_errors_redact_labeled_credentials(gh: FakeGh, label: str, gap: str) -> None:
    header = f"{label}{gap}opaque-synthetic-value"
    gh.fail("api user", f"HTTP 401\n{header}")

    with pytest.raises(github.GhError, match="HTTP 401") as info:
        github._gh("api", "user", "-H", header)

    assert "opaque-synthetic-value" not in str(info.value)
    assert "opaque-synthetic-value" not in info.value.tail
    assert "opaque-synthetic-value" not in " ".join(info.value.command)


def test_invalid_gh_json_reports_location_without_response_content(gh: FakeGh) -> None:
    gh.respond("api user", {"stdout": '{"access_token":"opaque-synthetic-value"', "exit": 0})

    with pytest.raises(github.GhError, match="invalid JSON from gh at line 1, column") as info:
        github._gh_json("api", "user")

    assert "opaque-synthetic-value" not in str(info.value)
    assert "opaque-synthetic-value" not in info.value.tail


def test_gh_timeout_redacts_diagnostics(monkeypatch) -> None:
    token = "ghs_FAKE000"

    def timeout(*args, **kwargs):
        raise github.ProcTimeout(f"gh timed out: {token}", token, stderr=token)

    monkeypatch.setattr(github, "run", timeout)
    with pytest.raises(github.ProcTimeout, match="gh timed out: \\[REDACTED\\]") as info:
        github._gh("api", "user", "--method", "POST", token=token)

    assert info.value.stdout == info.value.stderr == "[REDACTED]"
    assert info.value.__suppress_context__


@pytest.mark.parametrize("verdict", ["approve", "request_changes"])
def test_post_review_posts_comment_directly_for_own_pr(gh: FakeGh, verdict: str) -> None:
    arm_review(gh, author="bot-user")
    github.post_review(REPO, 7, HEAD, review(verdict=verdict, findings=FINDINGS[:1]))
    (payload,) = review_payloads(gh)
    assert payload["event"] == "COMMENT"
    assert [c["argv"][:2] for c in gh.calls[:2]] == [["api", f"repos/{REPO}/pulls/7"],
                                               ["api", "graphql"]]
    assert gh.argv(0) == ["api", f"repos/{REPO}/pulls/7", "--include"]


def test_post_review_uses_given_pr_without_lookup(gh: FakeGh) -> None:
    arm_review(gh)
    pr = github._pr_from_rest(rest_pr_json(pr_json(author={"login": "bot-user"})))
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
    gh.respond("api graphql viewer", {"data": {"viewer": {"login": "bot-user"}}})
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


def test_markers_without_id_are_resolvable_by_thread_id(gh: FakeGh) -> None:
    legacy = {**thread("<!-- loopzero:finding severity=important -->\nheadless"), "id": "T_1"}
    gh.respond("api graphql viewer", {"data": {"viewer": {"login": "bot-user"}}})
    gh.respond("api graphql", threads_json(legacy))
    assert [(m, t) for m, t, _ in github.open_findings(REPO, 7)] == [("T_1", "T_1")]


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
    gh.respond_pr_list([{"number": 7, "url": "u"}], branch="lz/x")
    with pytest.raises(github.GhError, match="PR #7 not found or not accessible"):
        github.pr_for_branch(REPO, "lz/x")


def test_api_get_and_login_helpers(gh: FakeGh) -> None:
    gh.respond("api graphql viewer", {"data": {"viewer": {"login": "bot-user"}}})
    gh.respond("api repos/acme/widgets", {"default_branch": "main"})
    assert github.api_get("repos/acme/widgets") == {"default_branch": "main"}
    assert gh.argv(0) == ["api", "repos/acme/widgets", "--include"]
    assert github.login() == "bot-user"
    gh.respond("api graphql viewer", {"data": {"viewer": {}}})
    with pytest.raises(github.GhError, match="no login"):
        github.login()


def test_app_identity_selection_and_cache(gh: FakeGh, tmp_path: Path, monkeypatch) -> None:
    helper = tmp_path / "app-token"
    counter = tmp_path / "helper-calls"
    helper.write_text(f"#!/bin/sh\necho called >> {counter}\necho synthetic-app-token\n")
    helper.chmod(0o755)
    monkeypatch.setattr(github, "APP_TOKEN_HELPER", helper)
    wall_time = boot_time = 10_000.0
    monkeypatch.setattr(github.time, "time", lambda: wall_time)
    monkeypatch.setattr(github.time, "CLOCK_BOOTTIME", 7, raising=False)
    monkeypatch.setattr(github.time, "clock_gettime", lambda _: boot_time)
    monkeypatch.setattr(github.time, "monotonic", lambda: 100.0)  # stopped during host suspend
    monkeypatch.setenv("GH_TOKEN", "operator-token")
    gh.respond("api repos/acme/widgets", {})
    gh.respond("api graphql viewer", {"data": {"viewer": {"login": "app[bot]"}}})
    gh.respond("api graphql", {})
    gh.respond(f"api repos/{REPO}/pulls/7 --method PATCH", {})
    gh.respond("api repos/acme/widgets/issues/7/comments", {})
    gh.respond("auth token", "operator-token")
    github.api_get("repos/acme/widgets")
    assert github.login() == "app[bot]"
    github.update_body(REPO, 7, "body")
    github._gh("api", "repos/acme/widgets/issues/7/comments", "--method", "POST")
    github._gh("api", "graphql", "-f", "query=mutation { resolveReviewThread(input: {}) { clientMutationId } }")
    assert github.auth_token() == "operator-token"
    assert [c["token"] for c in gh.calls] == [
        "synthetic-app-token", "synthetic-app-token", "synthetic-app-token",
        "operator-token", "operator-token", "operator-token",
    ]
    assert counter.read_text().splitlines() == ["called"]
    boot_time += 3_620
    wall_time += 2_620  # The wall clock stepped back by a thousand seconds.
    github.api_get("repos/acme/widgets")
    assert counter.read_text().splitlines() == ["called", "called"]


def test_app_token_age_counts_suspend_during_mint_and_retry(gh: FakeGh, tmp_path: Path, monkeypatch) -> None:
    helper = tmp_path / "app-token"
    counter = tmp_path / "helper-calls"
    helper.write_text(f"#!/bin/sh\necho called >> {counter}\necho synthetic-token-$(wc -l < {counter})\n")
    helper.chmod(0o755)
    monkeypatch.setattr(github, "APP_TOKEN_HELPER", helper)
    boot, real_run = [1_000_000.0], github.run
    suspend = lambda: boot.append(boot.pop() + 3601)  # noqa: E731
    monkeypatch.setattr(github, "_app_token_clock", lambda: boot[0])
    monkeypatch.setattr(github, "run", lambda argv, **k: (real_run(argv, **k), boot[0] == 1e6 and suspend())[0])
    monkeypatch.setattr(github, "_retry_sleep", lambda delay: suspend())
    monkeypatch.setenv("GH_TOKEN", "operator-token")
    gh.expect(["api", "repos/acme/widgets", "--include"], {})
    gh.expect(["api", "repos/acme/widgets", "--include"], exit=1, stderr="HTTP 502: Bad Gateway")
    gh.expect(["api", "repos/acme/widgets", "--include"], {})
    github.api_get("repos/acme/widgets")  # mint, then suspend before the token is used
    github.api_get("repos/acme/widgets")  # fails transiently, then suspends in the backoff
    assert [c["token"] for c in gh.calls] == ["operator-token", "synthetic-token-2", "synthetic-token-3"]


def test_app_helper_failure_falls_back_to_operator(gh: FakeGh, tmp_path: Path, monkeypatch) -> None:
    helper = tmp_path / "app-token"
    helper.write_text("#!/bin/sh\nexit 1\n")
    helper.chmod(0o755)
    monkeypatch.setattr(github, "APP_TOKEN_HELPER", helper)
    monkeypatch.setenv("GH_TOKEN", "operator-token")
    gh.respond("api repos/acme/widgets", {})
    github.api_get("repos/acme/widgets")
    assert gh.calls[0]["token"] == "operator-token"


def test_app_review_is_comment_with_inline_findings(gh: FakeGh, tmp_path: Path, monkeypatch) -> None:
    helper = tmp_path / "app-token"
    helper.write_text("#!/bin/sh\necho synthetic-app-token\n")
    helper.chmod(0o755)
    monkeypatch.setattr(github, "APP_TOKEN_HELPER", helper)
    gh.respond(f"api repos/{REPO}/pulls/7/files", PR_FILES)
    gh.respond(f"api repos/{REPO}/pulls/7/reviews", {"id": 3})
    github.post_review(REPO, 7, HEAD, review(verdict="approve", findings=FINDINGS[:1]),
                       pr=github.PR(7, "url", HEAD, "main", True, "OPEN", "UNKNOWN"))
    posted = gh.calls[-1]
    assert posted["token"] == "synthetic-app-token"
    payload = json.loads(posted["--input"])
    assert payload["event"] == "COMMENT" and payload["comments"]


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
    github._check_signals(REPO, HEAD)
    reads = [c["argv"] for c in gh.calls if c["argv"][0] == "api" and "-F" in c["argv"]]
    assert len(reads) == 3, "files, check-runs and status all pass -F"
    for argv in reads:
        assert argv[argv.index("--method") + 1] == "GET", argv


def test_check_runs_merges_runs_and_statuses(gh: FakeGh) -> None:
    filler = [{"name": f"job{i}", "status": "completed", "conclusion": "success"}
              for i in range(99)]
    gh.respond(f"api repos/{REPO}/commits/{HEAD}/check-runs",
               {"check_runs": [*filler,
                   {"name": "checks", "status": "completed", "conclusion": "success"}]},
               {"check_runs": [{"name": "lint", "status": "in_progress", "conclusion": None}]})
    gh.respond(f"api repos/{REPO}/commits/{HEAD}/statuses", [
        {"context": "ci/legacy", "state": "failure"},
        {"context": "checks", "state": "pending"},
    ])
    runs = github._check_signals(REPO, HEAD)[0]
    assert {k: v for k, v in runs.items() if not k.startswith("job")} == {
        "checks": "pending", "lint": "pending", "ci/legacy": "failure"}
    pages = [c["argv"][-2] for c in gh.calls if "check-runs" in c["argv"][1]]
    assert pages == ["page=1", "page=2"]


@pytest.mark.parametrize(("runs", "expected"), [
    ([
        {"id": 10, "name": "checks", "status": "completed", "conclusion": "skipped"},
        {"id": 11, "name": "checks", "status": "completed", "conclusion": "success"},
    ], {"checks": "success"}),
    ([
        {"id": 20, "name": "checks", "status": "in_progress", "conclusion": None},
        {"id": 19, "name": "checks", "status": "completed", "conclusion": "success"},
    ], {"checks": "pending"}),
    ([
        {"id": 30, "name": "checks", "status": "completed", "conclusion": "success"},
        {"id": 31, "name": "lint", "status": "completed", "conclusion": "failure"},
    ], {"checks": "success", "lint": "failure"}),
    ([
        {"id": 40, "name": "checks", "started_at": "2026-09-18T01:00:00Z",
         "status": "completed", "conclusion": "failure"},
        {"id": 40, "name": "checks", "started_at": "2026-09-18T02:00:00Z",
         "status": "completed", "conclusion": "success"},
    ], {"checks": "success"}),
], ids=["stale-skipped", "newer-pending", "distinct-names", "timestamp-tie"])
def test_check_runs_ordering(gh: FakeGh, runs: list[dict], expected: dict) -> None:
    gh.respond(f"api repos/{REPO}/commits/{HEAD}/check-runs", {"check_runs": runs})
    gh.respond(f"api repos/{REPO}/commits/{HEAD}/statuses", [])
    assert github._check_signals(REPO, HEAD)[0] == expected


@pytest.mark.parametrize("page", [{"total_count": 1}, {"check_runs": 1}, {"check_runs": []}])
def test_check_runs_validates_check_page_shape(gh: FakeGh, page: dict) -> None:
    endpoint = f"repos/{REPO}/commits/{HEAD}/check-runs"
    gh.respond(f"api {endpoint}", page)
    gh.respond(f"api repos/{REPO}/commits/{HEAD}/statuses", [])

    if page.get("check_runs") == []:
        assert github._check_signals(REPO, HEAD)[0] == {}
    else:
        with pytest.raises(github.GhError, match=endpoint):
            github._check_signals(REPO, HEAD)


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
    runs = github._check_signals(REPO, HEAD)[0]
    assert runs["required-page-two"] == "success"
    assert runs["duplicate"] == "failure", "the first (latest) status wins"


# --- readiness ----------------------------------------------------------------------------


def arm_readiness(gh: FakeGh, *, threads=(), runs=None) -> None:
    gh.respond("api graphql", threads_json(*threads))
    runs = {"checks": "success"} if runs is None else runs
    gh.respond(f"api repos/{REPO}/commits/{HEAD}/check-runs", {"check_runs": [
        {"name": n, "status": "completed", "conclusion": c} for n, c in runs.items()]})
    gh.respond(f"api repos/{REPO}/commits/{HEAD}/statuses", [])


def arm_workflow_check(gh: FakeGh, check: dict, statuses: list[dict] | None = None) -> None:
    gh.respond("api graphql", threads_json())
    gh.respond(f"api repos/{REPO}/commits/{HEAD}/check-runs", {"check_runs": [check]})
    gh.respond(f"api repos/{REPO}/commits/{HEAD}/statuses", statuses or [])


def readiness(*, reviewed=HEAD, workflow_wait=False):
    pr = github._pr_from_rest(rest_pr_json(pr_json()))
    return github.readiness(REPO, pr, "main", ("checks",), reviewed, workflow_wait=workflow_wait)


def failed_check(conclusion="failure", **fields):
    return {"name": "checks", "status": "completed", "conclusion": conclusion,
            "check_suite": {"id": 10}, **fields}


def producing_run(**fields):
    return {"id": 1, "check_suite_id": 10, "workflow_id": 7, "head_sha": HEAD,
            "status": "completed", "created_at": "2026-09-23T21:53:00Z", **fields}


def replacement_run():
    return dict(producing_run(), id=2, check_suite_id=20, status="in_progress",
                created_at="2026-09-23T21:54:21Z")


def arm_workflow_runs(gh, *runs):
    gh.respond(f"api repos/{REPO}/actions/runs?head_sha={HEAD}", {"workflow_runs": list(runs)})


def test_readiness_ready(gh: FakeGh) -> None:
    arm_readiness(gh)
    assert readiness() == github.Readiness(True, ())
    assert not any("actions/runs" in " ".join(c["argv"]) for c in gh.calls)


def test_readiness_head_moved(gh: FakeGh) -> None:
    arm_readiness(gh)
    r = readiness(reviewed="b" * 40)
    assert not r.ready and any("differs from reviewed" in x for x in r.reasons)


def test_readiness_no_review(gh: FakeGh) -> None:
    arm_readiness(gh)
    r = readiness(reviewed=None)
    assert r.reasons == ("no review recorded for the current head",)


def test_readiness_blocking_finding(gh: FakeGh) -> None:
    arm_readiness(gh, threads=(thread(f"{marker('important')}\n**important: Oops**"),))
    r = readiness()
    assert r.reasons == ("open important finding at src/a.py:3: Oops",)


def test_readiness_blocks_on_anchored_unlocated_finding(gh: FakeGh) -> None:
    body = f"{marker('critical')}\nNo file location given by the reviewer; anchored here.\n\n" \
           "**critical: Global**\n\nno file"
    arm_readiness(gh, threads=(thread(body, path="src/first.py", line=12),))
    r = readiness()
    assert not r.ready
    assert r.reasons == ("open critical finding at src/first.py:12: Global",)


def test_readiness_resolved_or_suggestion_threads_do_not_block(gh: FakeGh) -> None:
    arm_readiness(gh, threads=(thread(f"{marker('critical')}\nX", resolved=True),
                               thread(f"{marker('suggestion')}\nY")))
    assert readiness().ready


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
    result = readiness()
    assert result.reasons == (
        "open important finding at src/a.py:3: Still applies",
        "open critical finding at src/a.py:3: Current",
    )


@pytest.mark.parametrize("failure", ["failure", "error", "cancelled", "timed_out"])
@pytest.mark.parametrize("wait,grace", [(False, True), (True, False), (True, True)])
def test_readiness_missing_and_failed_checks(gh: FakeGh, failure, wait, grace) -> None:
    arm_readiness(gh, runs={"checks": failure})
    r = github.readiness(
        REPO, github._pr_from_rest(rest_pr_json(pr_json())), "main", ("checks", "e2e"), HEAD, workflow_wait=wait, review_grace=grace
    )
    assert r.reasons == (
        f"required check 'checks' is {failure}",
        f"required check 'e2e' missing on {HEAD[:12]}",
    )
    assert not r.ready
    assert r.waitable_failures == ((f"required check 'checks' is {failure}",) if wait and grace else ())


@pytest.mark.parametrize("failure,changes", [
    pytest.param("failure", {}, id="failed"), pytest.param("cancelled", {}, id="cancelled"),
    pytest.param("failure", {"workflow_id": 8}, id="different-workflow"),
    pytest.param("failure", {"head_sha": "b" * 40}, id="different-head"),
    pytest.param("failure", {"status": "completed"}, id="completed"),
    pytest.param("failure", {"created_at": "2026-09-23T21:52:00Z"}, id="older"),
])
def test_readiness_waits_only_for_newer_active_producing_workflow(gh, failure, changes):
    arm_workflow_check(gh, failed_check(
        failure, id=100, started_at="2026-09-23T21:53:00Z", completed_at="2026-09-23T21:54:41Z",
    ))
    arm_workflow_runs(gh, producing_run(), {**replacement_run(), **changes})
    result = readiness(workflow_wait=True)
    assert result.reasons == (f"required check 'checks' is {failure}",)
    assert result.waitable_failures == (() if changes else result.reasons)
    assert not result.ready


def test_readiness_newer_run_in_same_second_waits(gh: FakeGh) -> None:
    arm_workflow_check(gh, failed_check(id=100))
    arm_workflow_runs(gh, producing_run(created_at="2026-09-23T21:54:21Z"), replacement_run())
    result = readiness(workflow_wait=True)
    assert result.waitable_failures == result.reasons


def test_readiness_keeps_failing_commit_status_blocker(gh: FakeGh) -> None:
    arm_workflow_check(gh, failed_check(id=100), [{"context": "checks", "state": "failure"}])
    arm_workflow_runs(gh, producing_run(), replacement_run())
    result = readiness(workflow_wait=True)
    assert result.reasons == ("required check 'checks' is failure",)
    assert result.waitable_failures == ()


def test_readiness_looks_up_only_required_failed_checks(gh: FakeGh) -> None:
    gh.respond("api graphql", threads_json())
    gh.respond(f"api repos/{REPO}/commits/{HEAD}/check-runs", {"check_runs": [
        {"name": "checks", "status": "completed", "conclusion": "success"},
        {"name": "optional", "status": "completed", "conclusion": "failure",
         "check_suite": {"id": 10}},
    ]})
    gh.respond(f"api repos/{REPO}/commits/{HEAD}/statuses", [])
    result = readiness(workflow_wait=True)
    assert result.ready
    assert not any("actions/runs" in " ".join(c["argv"]) for c in gh.calls)


@pytest.mark.parametrize("conclusion", ["skipped", "neutral"])
def test_readiness_wait_does_not_lookup_nonfailure_conclusion(
    gh: FakeGh, conclusion: str
) -> None:
    arm_workflow_check(gh, failed_check(conclusion))
    result = readiness(workflow_wait=True)
    assert result.reasons == (f"required check 'checks' is {conclusion}",)
    assert result.waitable_failures == ()
    assert not any("actions/runs" in " ".join(c["argv"]) for c in gh.calls)


def test_readiness_paginates_actions_runs_before_deferring_failure(gh: FakeGh) -> None:
    arm_workflow_check(gh, failed_check())
    filler = [{"id": i + 100, "check_suite_id": i + 100, "workflow_id": 99,
               "head_sha": HEAD, "status": "completed"} for i in range(100)]
    gh.respond(f"api repos/{REPO}/actions/runs?head_sha={HEAD}",
               {"workflow_runs": filler},
               {"workflow_runs": [
                   producing_run(),
                   replacement_run(),
               ]})
    result = readiness(workflow_wait=True)
    assert result.waitable_failures == result.reasons
    pages = [c["argv"][-2] for c in gh.calls if "actions/runs" in c["argv"][1]]
    assert pages == ["page=1", "page=2"]


@pytest.mark.parametrize(("replacement_fields", "waitable"), [
    ({}, True),
    ({"workflow_id": 8}, False),
    ({"head_sha": "b" * 40}, False),
])
def test_readiness_recovers_workflow_producer_at_search_limit(
    gh: FakeGh, replacement_fields: dict, waitable: bool,
) -> None:
    arm_workflow_check(gh, failed_check())
    runs = [
        replacement_run() | replacement_fields,
        *(producing_run(id=i, check_suite_id=i, workflow_id=99) for i in range(100, 1099)),
    ]
    gh.respond(f"api repos/{REPO}/actions/runs?head_sha={HEAD}",
               *({"workflow_runs": runs[i:i + 100]} for i in range(0, len(runs) + 1, 100)))
    gh.respond(f"api repos/{REPO}/actions/runs?check_suite_id=10",
               {"workflow_runs": [producing_run()]})

    result = readiness(workflow_wait=True)

    assert not result.ready
    assert result.waitable_failures == (result.reasons if waitable else ())


def test_readiness_actions_api_error_does_not_defer_failure(gh: FakeGh) -> None:
    arm_workflow_check(gh, failed_check())
    gh.respond(f"api repos/{REPO}/actions/runs?head_sha={HEAD}",
               {"stdout": "", "stderr": "HTTP 403: Actions read denied", "exit_code": 1})

    with pytest.raises(github.GhError, match="Actions read denied"):
        readiness(workflow_wait=True)


@pytest.mark.parametrize("started,waitable", [
    ("2026-09-23T21:55:00Z", True), ("2026-09-23T21:54:00Z", False),
])
def test_readiness_rerun_attempt_requires_start_after_old_failure(
    gh: FakeGh, started: str, waitable: bool
) -> None:
    arm_workflow_check(gh, failed_check(id=100, completed_at="2026-09-23T21:54:41Z"))
    arm_workflow_runs(gh, producing_run(status="in_progress", run_attempt=2, run_started_at=started))
    result = readiness(workflow_wait=True)
    assert result.waitable_failures == (result.reasons if waitable else ())


@pytest.mark.parametrize(("metadata", "reason"), [
    ({"mergeStateStatus": "BEHIND"}, "PR #7 is behind main; rebase and rerun checks"),
    ({"mergeable": "CONFLICTING"}, "PR #7 has merge conflicts with main"),
    ({"state": "MERGED"}, "PR #7 is MERGED, not open"),
], ids=["behind-base", "merge-conflict", "merged-pr"])
def test_readiness_metadata_blocker(gh: FakeGh, metadata: dict, reason: str) -> None:
    arm_readiness(gh)
    pr = github._pr_from_rest(rest_pr_json(pr_json(**metadata)))
    r = github.readiness(REPO, pr, "main", ("checks",), HEAD)
    assert r.reasons == (reason,)


def test_readiness_without_required_ci_skips_check_lookup(gh: FakeGh) -> None:
    gh.respond("api graphql", threads_json())
    assert github.readiness(REPO, github._pr_from_rest(rest_pr_json(pr_json())), "main", (), HEAD).ready
    assert [c["argv"][:2] for c in gh.calls] == [["api", "graphql"]]


# --- ready / merge ---------------------------------------------------------------------


def test_mark_ready(gh: FakeGh) -> None:
    gh.respond("pr ready", "")
    github.mark_ready(REPO, 7)
    assert gh.argv(0) == ["pr", "ready", "7", "--repo", REPO]


def test_merge_success_verifies_rest_landing(
    gh: FakeGh,
) -> None:
    gh.expect(
        ["pr", "merge", "7", "--repo", REPO, "--squash", "--match-head-commit", HEAD]
    )
    gh.expect(["api", f"repos/{REPO}/pulls/7", "--include"], merge_json("MERGED", sha="c" * 40))
    assert github.merge(REPO, 7, "squash", HEAD) == "c" * 40
    assert gh.argv(0) == ["pr", "merge", "7", "--repo", REPO, "--squash",
                          "--match-head-commit", HEAD]
    assert "--delete-branch" not in gh.argv(0)
    assert gh.argv(1) == ["api", f"repos/{REPO}/pulls/7", "--include"]
    gh.assert_complete()


def test_merge_command_failure(gh: FakeGh) -> None:
    gh.fail("pr merge", "head commit mismatch")
    gh.respond(f"api repos/{REPO}/pulls/7", merge_json())
    gh.respond("api graphql", _queue_json(False))
    with pytest.raises(github.MergeFailed) as info:
        github.merge(REPO, 7, "rebase", HEAD)
    assert "--rebase" in info.value.command and "mismatch" in info.value.tail


@pytest.mark.parametrize("remote_head", ["f" * 40, None])
def test_delete_remote_branch_checks_encoded_ref(gh: FakeGh, remote_head: str | None) -> None:
    get = f"api repos/{REPO}/git/ref/heads/lz/fix%23516%3Fx%25"
    if remote_head is None:
        gh.fail(get, "gh: Not Found (HTTP 404)")
    else:
        gh.respond(get, {"object": {"sha": remote_head}})
    assert github.delete_remote_branch(REPO, "lz/fix#516?x%", HEAD) is (remote_head is None)
    assert gh.argv(0) == [*get.split(), "--include"]
    assert len(gh.calls) == 1


def _queue_json(queued: bool) -> dict:
    return {"data": {"repository": {"pullRequest": {"isInMergeQueue": queued, "headRefOid": HEAD}}}}


@pytest.mark.parametrize("missing", [False, True])
def test_merge_unverified(gh: FakeGh, missing: bool) -> None:
    unmerged = merge_json(state='OPEN', head=HEAD, sha=None)
    gh.respond("pr merge", "")
    gh.respond(f"api repos/{REPO}/pulls/7", unmerged, unmerged)
    response = {"data": {"repository": {"pullRequest": None}}} if missing else _queue_json(False)
    gh.respond("api graphql", response)
    message = "GitHub queue lookup for PR #7: queue status could not be verified" if missing else "neither merged nor queued"
    with pytest.raises(github.MergeFailed, match=message):
        github.merge(REPO, 7, "merge", HEAD)
    assert not any(c["argv"][:2] == ["api", "-X"] for c in gh.calls), "no ref delete"


@pytest.mark.parametrize("strategy,auto", [("squash", False), ("queue", False), ("queue", True)])
def test_merge_queued_returns_none(gh: FakeGh, strategy, auto) -> None:
    gh.respond("pr merge", "")
    gh.respond(f"api repos/{REPO}/pulls/7", merge_json())
    accepted = _queue_json(not auto)
    if auto:
        accepted["data"]["repository"]["pullRequest"]["autoMergeRequest"] = {"enabledAt": "t"}
    gh.respond("api graphql", *([_queue_json(False)] if strategy == "queue" else []), accepted)
    assert github.merge(REPO, 7, strategy, HEAD) is None
    if strategy == "queue":
        assert github.merge(REPO, 7, strategy, HEAD) is None
    writes = [c["argv"] for c in gh.calls if c["argv"][:2] == ["pr", "merge"]]
    assert len(writes) == 1
    assert ("--squash" in writes[0]) == (strategy == "squash")
    assert "--queue" not in writes[0]


def test_merge_landed_between_reads_is_reported_merged(gh: FakeGh) -> None:
    gh.respond("pr merge", "")
    gh.respond(f"api repos/{REPO}/pulls/7", merge_json(state='OPEN', head=HEAD, sha=None),
               merge_json(state='MERGED', head=HEAD, sha='d' * 40))
    gh.respond("api graphql", _queue_json(False))
    assert github.merge(REPO, 7, "squash", HEAD) == "d" * 40


def test_merged_sha_only_for_merged_state(gh: FakeGh) -> None:
    gh.respond(f"api repos/{REPO}/pulls/7", merge_json(state='MERGED', sha='c' * 40),
               merge_json(state='OPEN', sha="d" * 40))
    assert github.merged_sha(REPO, 7) == "c" * 40
    assert github.merged_sha(REPO, 7) is None


def test_merge_rejects_unknown_strategy(gh: FakeGh) -> None:
    with pytest.raises(github.MergeFailed):
        github.merge(REPO, 7, "fast-forward", HEAD)
    assert gh.calls == []


@pytest.mark.parametrize("args, attempts, error", [
    (("api", "repos/acme/widgets/pulls/1"), 2, ""),
    (("api", "repos/acme/widgets/commits/x/check-runs", "--method", "GET", "-F", "per_page=100"), 2, "dial tcp 140.82.121.5:443: i/o timeout"),
    (("api", "graphql", "-f", "query=query { viewer { login } }"), 2, "net/http: TLS handshake timeout"),
    (("api", "repos/acme/widgets/pulls/7"), 2, "error connecting to api.github.com"),
    (("api", "repos/acme/widgets/pulls/7/reviews"), 2, "unexpected end of JSON input"),
    (("api", "graphql", "-f", "query=query { viewer { login } }"), 2, "gh: Something went wrong while executing your query"),
    (("api", "repos/acme/widgets/issues/1/comments", "-f", "body=x"), 1, "HTTP 503"),
    (("api", "graphql", "-f", "query=mutation { resolve }"), 1, "HTTP 503"),
])
def test_gh_retries_only_transient_reads(monkeypatch, args, attempts, error):
    seen: list[list[str]] = []
    def run(argv, **_):
        seen.append(argv)
        failed = len(seen) == 1
        if failed and "pulls/1" in argv[2]:
            raise github.ProcTimeout("gh api timed out")
        return type("Done", (), {"exit_code": int(failed), "stdout": "ok", "stderr": error * failed})
    monkeypatch.setattr(github, "run", run)
    monkeypatch.setattr(github, "_retry_sleep", lambda _: None)
    if attempts == 2:
        assert github._gh(*args) == "ok"
    else:
        with pytest.raises(github.GhError):
            github._gh(*args)
    assert len(seen) == attempts


@pytest.mark.parametrize("error", ["API rate limit exceeded", "API rate limit already exceeded",
                                  "secondary rate limit", "HTTP 429 Too Many Requests",
                                  "GraphQL: API rate limit exceeded"])
def test_rest_rate_limit_is_not_retried_or_probed(gh, monkeypatch, tmp_path, error):
    gh.fail(pr_list_key("lz/x"), error)
    monkeypatch.setattr(github, "_retry_sleep", lambda _: pytest.fail("retried rate limit"))
    worktree = tmp_path / "removed-worktree"
    worktree.mkdir()
    with monkeypatch.context() as m:
        m.chdir(worktree)
        worktree.rmdir()
        with pytest.raises(github.GhError, match="Rate limited"):
            github.pr_for_branch(REPO, "lz/x")
    assert len(gh.calls) == 2  # Remote branch read, then the rate-limited fallback.


@pytest.mark.parametrize(("remaining", "retry_after", "hint"), [
    ("0", "", "hourly quota exhausted: retry after 2026-11-25T21:03:14+00:00"),
    ("4676", "90", "short-window limit: retry after 90 s"),
    ("4676", "", "short-window limit: retry after ~60 s"),
    ("", "90", "short-window limit: retry after 90 s"),
    ("", "Wed, 25 Nov 2026 21:03:14 GMT", "short-window limit: retry after Wed, 25 Nov 2026 21:03:14 GMT"),
    ("", "", "Rate limited: retry after GitHub's reset or Retry-After; No automatic retry."),
])
def test_rest_rate_limit_uses_failed_response_headers(gh, remaining, retry_after, hint):
    headers = "HTTP/2.0 403 Forbidden\r\nX-Unrelated: never print this\r\n"
    if remaining:
        headers += (
            f"X-RateLimit-Resource: core\r\nX-RateLimit-Remaining: {remaining}\r\n"
            "X-RateLimit-Reset: 1795640594\r\n"
        )
    if retry_after:
        headers += f"Retry-After: {retry_after}\r\n"
    gh.respond(pr_list_key("lz/x"), headers + '\r\n{"message":"API rate limit exceeded"}',
               exit=1, stderr="gh: API rate limit exceeded for installation ID 123 (HTTP 403)")

    with pytest.raises(github.GhError) as info:
        github.pr_for_branch(REPO, "lz/x")

    error = str(info.value)
    assert hint in error
    if remaining:
        assert "X-RateLimit-Resource: core" in error
        assert f"X-RateLimit-Remaining: {remaining}" in error
        assert "X-RateLimit-Reset: 2026-11-25T21:03:14+00:00" in error
    if retry_after:
        assert f"Retry-After: {retry_after}" in error
    assert "never print this" not in error
    assert [call["argv"] for call in gh.calls] == [
        [*branch_head_key("lz/x").split(), "--include"],
        [*pr_list_key("lz/x").split(), "--include"],
    ]


def test_graphql_limit_reports_verified_reset_from_one_diagnostic(gh: FakeGh) -> None:
    gh.respond("api graphql",
               {"stdout": "", "stderr": "GraphQL: API rate limit exceeded", "exit": 1},
               {"data": {"rateLimit": {"remaining": 0, "resetAt": "2099-01-01T00:00:00Z"}}})

    with pytest.raises(github.GhError, match="GraphQL quota resets at 2099-01-01") as info:
        github._gh("api", "graphql", "-f", "query={viewer{login}}")

    assert info.value.command[-1] == "query={viewer{login}}"
    assert len(gh.calls) == 2
    assert "rateLimit { remaining resetAt }" in gh.calls[1]["argv"][-1]


@pytest.mark.parametrize("diagnostic", [
    "null", "[]", {"data": {"rateLimit": {"remaining": 0, "resetAt": 42}}},
    {"data": []}, {"data": {"rateLimit": None}},
])
def test_bad_graphql_reset_evidence_preserves_original_error(gh: FakeGh, diagnostic: object) -> None:
    gh.respond("api graphql",
               {"stdout": "", "stderr": "GraphQL: API rate limit exceeded", "exit": 1},
               diagnostic)

    with pytest.raises(github.GhError, match="GraphQL: API rate limit exceeded") as info:
        github._gh("api", "graphql", "-f", "query={viewer{login}}")

    assert "quota resets at" not in str(info.value)
    assert info.value.command[-1] == "query={viewer{login}}"
    assert len(gh.calls) == 2


def test_graphql_limit_prefers_failing_response_reset_header(gh: FakeGh) -> None:
    gh.fail("api graphql", "GraphQL: API rate limit exceeded\n"
            "x-ratelimit-resource: graphql\nx-ratelimit-remaining: 0\n"
            "x-ratelimit-reset: 4070908800")  # 2099-01-01 UTC

    with pytest.raises(github.GhError, match="response header"):
        github._gh("api", "graphql", "-f", "query={viewer{login}}")

    assert len(gh.calls) == 1


@pytest.mark.parametrize("view", [None, {}, merge_json("CLOSED"),
    merge_json("MERGED"), merge_json("MERGED", sha="invalid"),
    merge_json("MERGED", sha="c" * 40) | {"merged": "true"},
    merge_json(head="f" * 40), merge_json(head=None), merge_json() | {"head": []}])
def test_invalid_merge_evidence_fails_closed(gh, view):
    gh.respond(f"api repos/{REPO}/pulls/7", view)
    with pytest.raises(github.GhError, match=r"merge evidence|without a verified merge|head changed"):
        github.merged_sha(REPO, 7, expected_head=HEAD)


@pytest.mark.parametrize("merged", [None, pr_json(number=101, state="MERGED")])
def test_merged_branch_history_reads_terminal_page(gh: FakeGh, merged: dict | None) -> None:
    gh.respond(branch_head_key(), {"commit": {"sha": HEAD}})
    gh.respond(f"api repos/{REPO}/commits/{HEAD}/pulls?per_page=100&page=1", [])
    gh.respond(branch_history_key(), [
        closed_history_row(pr_json(number=n, state="CLOSED")) for n in range(1, 101)
    ])
    gh.respond(branch_history_key(2), [closed_history_row(merged)] if merged else [])
    if merged:
        gh.respond_pr_view(merged)
    pr = github.merged_pr_for_branch(REPO, "lz/t1")
    assert ((pr.number, pr.state) if pr else None) == ((101, "MERGED") if merged else None)
    assert gh.argv(3) == [*branch_history_key(2).split(), "--include"]


@pytest.mark.parametrize("change", [
    {"number": True}, {"state": "open"}, {"merged_at": False},
    {"merged_at": "bad-date"}, {"merged_at": "2026-10-07T05:04:29"},
    {"head": None}, {"head": {"ref": "lz/other", "repo": {"full_name": REPO}}},
    {"head": {"ref": "lz/t1", "repo": {"full_name": "other/widgets"}}},
])
def test_merged_branch_history_rejects_invalid_rows(gh: FakeGh, change: dict) -> None:
    row = closed_history_row(pr_json(state="CLOSED"))
    gh.respond(branch_history_key(), [{**row, **change}])

    with pytest.raises(github.GhError, match="invalid or repeated closed PR history"):
        github.merged_pr_for_branch(REPO, "lz/t1")


@pytest.mark.parametrize("detail", [
    rest_pr_json(pr_json(state="CLOSED")),
    rest_pr_json(pr_json(state="MERGED"), branch="lz/other"),
    {**rest_pr_json(pr_json(state="MERGED")),
     "head": {"sha": HEAD, "ref": "lz/t1", "repo": {"full_name": "other/widgets"}}},
])
def test_merged_branch_history_rejects_changed_detail(gh: FakeGh, detail: dict) -> None:
    gh.respond(branch_history_key(), [closed_history_row(pr_json(state="MERGED"))])
    gh.respond(f"api repos/{REPO}/pulls/7", detail)

    with pytest.raises(github.GhError, match="PR changed during completion lookup"):
        github.merged_pr_for_branch(REPO, "lz/t1")


def test_merged_branch_history_fails_at_page_limit(monkeypatch) -> None:
    monkeypatch.setattr(github, "_branch_head", lambda repo, branch: None)
    monkeypatch.setattr(github, "PAGE", 1)
    pages = ({branch_history_key(page).removeprefix("api ").replace("per_page=100", "per_page=1"):
              [closed_history_row(pr_json(number=page, state="CLOSED"))]} for page in range(1, 101))
    monkeypatch.setattr(github, "api_get", lambda endpoint: next(pages)[endpoint])
    with pytest.raises(github.GhError, match="exceeds 100-page publication limit"):
        github.merged_pr_for_branch(REPO, "lz/t1")
    assert next(pages, None) is None


@pytest.mark.parametrize("state", ["OPEN", "MERGED", "CLOSED"])
def test_branch_pr_lookup_uses_commit_and_ignores_foreign_heads(gh: FakeGh, state: str) -> None:
    row = closed_history_row(pr_json(number=6 if state == "CLOSED" else 7, state=state))
    row["created_at"] = "2026-10-06T00:00:00Z"
    fork = row | {"number": 9, "head": row["head"] | {"repo": {"full_name": "fork/widgets"}}}
    other_branch = row | {"number": 8, "head": row["head"] | {"ref": "lz/other"}}
    commits = f"api repos/{REPO}/commits/{HEAD}/pulls?per_page=100&page=1"
    gh.respond(branch_head_key(), {"commit": {"sha": HEAD}})
    gh.respond(commits, [fork, other_branch, row])
    if state == "CLOSED":
        gh.respond_pr_list([pr_json()])
    else:
        gh.respond(f"api repos/{REPO}/pulls/7", row)
    lookup = github.merged_pr_for_branch if state == "MERGED" else github.pr_for_branch
    pr = lookup(REPO, "lz/t1")
    assert pr is not None and pr.number == 7 and pr.state == ("OPEN" if state == "CLOSED" else state)
    endpoint = pr_list_key() if state == "CLOSED" else f"api repos/{REPO}/pulls/7"
    assert gh.argv(2) == [*endpoint.split(), "--include"]


@pytest.mark.parametrize("capped", [False, True])
def test_readiness_distinguishes_capped_check_suites_from_missing_check(gh: FakeGh, capped: bool) -> None:
    arm_readiness(gh)
    runs = [
        {"name": "required-ci", "conclusion": "success", "check_suite": {"id": 1}},
        *({"name": "unrelated", "conclusion": "success", "check_suite": {"id": suite}}
          for suite in range(2, 1002)),
    ]
    visible = runs[-1000:] if capped else runs[1:-1]
    gh.respond(f"api repos/{REPO}/commits/{HEAD}/check-runs",
               *({"check_runs": visible[i:i + 100]} for i in range(0, len(visible) + 1, 100)))

    result = github.readiness(REPO, github._pr_from_rest(rest_pr_json(pr_json())),
                              "main", ("required-ci",), HEAD)

    assert not result.ready
    assert result.reasons == (
        f"cannot determine required check 'required-ci' on {HEAD[:12]}: GitHub's 1,000-suite limit"
        if capped else f"required check 'required-ci' missing on {HEAD[:12]}",
    )


@pytest.mark.parametrize(("unrelated", "include_failure", "reason"), [
    (1000, False, f"cannot determine required check 'required-ci' on {HEAD[:12]}: GitHub's 1,000-suite limit"),
    (100, True, "required check 'required-ci' is failure"),
    (100, False, None),
])
def test_readiness_requires_check_run_evidence_for_status_success_at_suite_cap(
    gh: FakeGh, unrelated: int, include_failure: bool, reason: str | None,
) -> None:
    arm_readiness(gh)
    gh.respond(f"api repos/{REPO}/commits/{HEAD}/statuses",
               [{"context": "required-ci", "state": "success"}])
    runs = [
        {"name": "unrelated", "conclusion": "success", "check_suite": {"id": suite}}
        for suite in range(2, unrelated + 2)
    ]
    if include_failure:
        runs.append({"name": "required-ci", "conclusion": "failure", "check_suite": {"id": 1}})
    gh.respond(f"api repos/{REPO}/commits/{HEAD}/check-runs",
               *({"check_runs": runs[i:i + 100]} for i in range(0, len(runs) + 1, 100)))

    result = github.readiness(REPO, github._pr_from_rest(rest_pr_json(pr_json())),
                              "main", ("required-ci",), HEAD)

    assert result.ready == (reason is None)
    assert result.reasons == ((reason,) if reason else ())


@pytest.mark.parametrize("changed_files,path,severity,live_head,anchor", [
    (3001, "file-3001", "important", HEAD, None),
    (3000, "file-3000", "important", HEAD, "file-3000"),
    (3000, "outside-diff", "important", HEAD, "file-0001"),
    (3001, "file-3001", "suggestion", HEAD, None),
    (3000, "outside-diff", "important", "b" * 40, None),
])
def test_review_file_limit(gh: FakeGh, changed_files: int, path: str, severity: str,
                           live_head: str, anchor: str | None) -> None:
    files = [{"filename": f"file-{i:04d}", "patch": "@@ -1 +1 @@\n-before\n+after"}
             for i in range(1, changed_files + 1)][:3000]
    gh.respond(FILES_KEY, *(files[i:i + 100] for i in range(0, 3001, 100)))
    detail_key = f"api repos/{REPO}/pulls/7"
    gh.respond(detail_key, rest_pr_json(pr_json(headRefOid=live_head)) | {"changed_files": changed_files})
    result = review(findings=(Finding(severity, path, 1, "Incorrect", "Fix this"),))

    if severity == "suggestion":
        payload = github.review_payload(REPO, 7, HEAD, result, "")
        assert payload["comments"] == [] and "Incorrect" in payload["body"]
    elif anchor is None:
        message = rf"PR #7.*{path}.*3,000" if live_head == HEAD else "PR #7 head moved"
        with pytest.raises(github.GhError, match=message):
            github.review_payload(REPO, 7, HEAD, result, "")
    else:
        payload = github.review_payload(REPO, 7, HEAD, result, "")
        (comment,) = payload["comments"]
        assert (comment["path"], comment["line"]) == (anchor, 1)
    assert len(calls_for(gh, detail_key)) == int(severity == "important" and path != "file-3000")
