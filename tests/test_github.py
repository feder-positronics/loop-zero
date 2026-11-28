"""github.py against a fake `gh` that records every call and replays canned responses."""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from dataclasses import replace
from pathlib import Path
from typing import Any
from unittest.mock import Mock
from urllib.parse import quote

import pytest

from loopzero import eligibility, github
from loopzero._proc import Completed, build_env
from loopzero.types import Finding, ReviewResult
from tests.conftest import write_fake_tool
from tests.fake_gh import replay

REPO = "acme/widgets"
HEAD = "a" * 40


def branch_head_key(branch: str = "lz/t1") -> str:
    return f"api repos/{REPO}/branches/{quote(branch, safe='')}"


def pr_list_key(branch: str = "lz/t1", state: str = "open") -> str:
    head = quote(f"{REPO.split('/')[0]}:{branch}", safe="")
    return (f"api repos/{REPO}/pulls?state={state}&head={head}"
            "&sort=created&direction=desc&per_page=1")

def branch_history_key(page: int = 1, *, branch: str = "lz/t1") -> str:
    head = quote(f"{REPO.split('/')[0]}:{branch}", safe="")
    return (f"api repos/{REPO}/pulls?state=closed&head={head}"
            f"&sort=created&direction=desc&per_page=100&page={page}")


def closed_history_row(data: dict) -> dict:
    return {**rest_pr_json(data),
            "merged_at": "2026-10-07T05:04:29Z" if data.get("state") == "MERGED" else None}


class FakeGh:
    def __init__(self, fake_bin: Path, tmp_path: Path) -> None:
        self.calls_file = tmp_path / "gh-calls.json"
        self.scenario_file = tmp_path / "gh-scenario.json"
        self.calls_file.write_text("[]")
        self.scenario_file.write_text("{}")
        replay_source = Path(__file__).with_name("fake_gh.py").read_text()
        write_fake_tool(fake_bin, "gh", f"""#!{sys.executable}
{replay_source}
import sys
code, stdout, stderr = replay(sys.argv[1:], {str(self.calls_file)!r},
                             {str(self.scenario_file)!r}, os.getcwd(), os.environ)
sys.stdout.write(stdout)
sys.stderr.write(stderr)
sys.exit(code)
""")

    def replay_in_process(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Replay fake gh requests without starting an interpreter for each call."""
        run_process = github.run

        def run(argv, *, cwd, env_allowlist, extra_env=None, **kwargs):
            if argv[0] != "gh":
                return run_process(argv, cwd=cwd, env_allowlist=env_allowlist,
                                   extra_env=extra_env, **kwargs)
            code, stdout, stderr = replay(
                argv[1:], str(self.calls_file), str(self.scenario_file), str(cwd),
                build_env(env_allowlist, extra_env))
            return Completed(code, stdout, stderr, 0)

        monkeypatch.setattr(github, "run", run)

    def respond(self, key: str, *responses: object, exit: int = 0, stderr: str = "") -> None:
        self._set_responses(key, [self._response(r, exit=exit, stderr=stderr) for r in responses])

    def append(self, key: str, *responses: object) -> None:
        self._set_responses(key, [self._response(r) for r in responses], append=True)

    def _set_responses(self, key, responses, *, append=False):
        plan = json.loads(self.scenario_file.read_text())
        plan[key] = [*plan.get(key, ()), *responses] if append else responses
        self.scenario_file.write_text(json.dumps(plan))

    def respond_pr_list(self, *snapshots: list[dict], branch: str = "lz/t1") -> None:
        """Arm explicit REST list and selected detail responses for successive lookups."""
        open_pages, closed_pages, selected = [], [], []
        for snapshot in snapshots:
            opened = next((pr for pr in snapshot if pr.get("state", "OPEN") == "OPEN"), None)
            chosen = opened or (snapshot[0] if snapshot else None)
            page = [{"number": chosen["number"]}] if chosen else []
            open_pages.append(page if opened else [])
            if not opened:
                closed_pages.append(page)
            if chosen:
                selected.append(chosen)
        self.append(pr_list_key(branch), *open_pages)
        if closed_pages:
            self.append(pr_list_key(branch, "closed"), *closed_pages)
        self.respond_pr_view(*selected, branch=branch)

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
        stdout = response if isinstance(response, str) else json.dumps(response)
        return dict(stdout=stdout, stderr=stderr, exit_code=exit)

    def fail(self, key: str, stderr: str, exit: int = 1) -> None:
        self.respond(key, {"stdout": "", "stderr": stderr, "exit": exit})

    @property
    def calls(self) -> list[dict]:
        return json.loads(self.calls_file.read_text())

    def argv(self, index: int) -> list[str]:
        return self.calls[index]["argv"]


@pytest.fixture
def gh(fake_bin: Path, tmp_path: Path, monkeypatch) -> FakeGh:
    fake = FakeGh(fake_bin, tmp_path)
    fake.replay_in_process(monkeypatch)
    return fake


def pr_json(**over: object) -> dict:
    return {
        "number": 7, "url": f"https://github.com/{REPO}/pull/7", "headRefOid": HEAD,
        "baseRefName": "main", "isDraft": True, "state": "OPEN", "mergeable": "MERGEABLE",
        "author": {"login": "someone-else"}, **over,
    }


def rest_pr_json(data: dict, *, branch: str = "lz/t1") -> dict:
    """REST pull detail fixture corresponding to an existing GraphQL-shaped test PR."""
    if "headRefOid" not in data:
        return dict(data)  # preserve intentionally malformed fixtures
    mergeable = data.get("mergeable")
    return {
        "number": data.get("number", 7), "html_url": data.get("url"), "node_id": "PR_7",
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
            "head": {"sha": head}, "base": {"ref": "main"}, "merge_commit_sha": sha}


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
    return f"<!-- loopzero:finding v=1 severity={sev} head={head} id=0b9b59e7 -->"


PUBLISHERS = ("bot-user",)


FILES_KEY = f"api repos/{REPO}/pulls/7/files"
REVIEWS_KEY = f"api repos/{REPO}/pulls/7/reviews"
# src/a.py: right lines 10..12 ; src/b.py: right lines 1..3 ; src/c.py: right lines 5..6
PR_FILES = [
    {"filename": "src/a.py", "patch": "@@ -10,2 +10,3 @@\n ctx\n+new\n ctx2"},
    {"filename": "src/b.py", "patch": "@@ -1,3 +1,3 @@\n-old\n+new\n ctx\n ctx"},
    {"filename": "src/c.py", "patch": "@@ -5,2 +5,2 @@\n+x\n+y\n-z\n-w"},
    {"filename": "img.png"},
]


def arm_review(gh: FakeGh, files: list | None = None, author: str = "someone-else", *, branch="lz/t1") -> None:
    gh.respond_pr_view(pr_json(author={"login": author}), branch=branch)
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


@pytest.mark.parametrize("missing", [True, False], ids=["missing-cli", "unauthenticated"])
def test_pr_lookup_reports_github_cli_remediation(request, fake_bin, monkeypatch, missing):
    if missing:
        monkeypatch.setenv("PATH", str(fake_bin / "missing"))
    else:
        request.getfixturevalue("gh").fail(pr_list_key("lz/x"), "To get started with GitHub CLI, please run:  gh auth login\n", exit=4)
    with pytest.raises(github.GhMissing if missing else github.GhAuth) as info:
        github.pr_for_branch(REPO, "lz/x")
    diagnostic = str(info.value) if missing else info.value.tail
    assert "required token scope: repo" in diagnostic
    assert ("gh auth login --scopes repo" if missing else "gh auth login") in diagnostic
    if not missing:
        assert info.value.command[:2] == ("gh", "api")


@pytest.mark.parametrize("node_id,endpoint,stderr,stdout,error", [
    ("PR_7", "api graphql ready", "gh: HTTP 502: Bad Gateway\n", "", github.GhUnavailable),
    ("", "pr ready", "gh: HTTP 502: Bad Gateway\n", "", github.GhUnavailable),
    *(("", "pr ready", "gh: HTTP 422\n", content, github.GhError)
      for content in ("", '{"message": "detail on stdout"}')),
])
def test_mark_ready_preserves_failure_without_replay(gh, monkeypatch, node_id, endpoint, stderr, stdout, error):
    monkeypatch.setattr(github, "_retry_sleep", lambda _delay: None)
    gh.respond(endpoint, {"stdout": stdout, "stderr": stderr, "exit": 1})
    with pytest.raises(error) as info:
        github.mark_ready(REPO, 7, node_id)
    assert [call["argv"][:2] for call in gh.calls] == [endpoint.split()[:2]]
    if error is github.GhError:
        assert info.value.command == ("gh", "pr", "ready", "7", "--repo", REPO)
        assert info.value.tail == "gh: HTTP 422" + ("\n\n" + stdout if stdout else "\n")


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


def test_commit_pr_lookup_rejects_a_repeated_full_page(gh: FakeGh) -> None:
    page = [rest_pr_json(pr_json(number=n), branch="lz/other") for n in range(1, github.PAGE + 1)]
    gh.respond(branch_head_key(), {"commit": {"sha": HEAD}})
    gh.respond(f"api repos/{REPO}/commits/{HEAD}/pulls --method GET", page, page, [])
    with pytest.raises(github.GhError, match="GitHub repeated a full page"):
        github.pr_for_branch(REPO, "lz/t1")
    assert sum(c["argv"][1] == f"repos/{REPO}/commits/{HEAD}/pulls" for c in gh.calls) == 2


@pytest.mark.parametrize("message, fallback", [
    (f"No commit found for SHA: {HEAD}", True),
    ("Validation failed", False),
])
def test_pr_lookup_with_local_head_handles_only_unknown_sha_422(gh: FakeGh, message: str, fallback: bool) -> None:
    assoc = f"api repos/{REPO}/commits/{HEAD}/pulls --method GET"
    gh.fail(assoc, f"gh: {message} (HTTP 422)")
    gh.respond_pr_list([pr_json()])
    if fallback:
        pr = github.pr_for_branch(REPO, "lz/t1", head_sha=HEAD)
        assert pr is not None and pr.number == 7
        assert gh.argv(1) == [*pr_list_key().split(), "--include"]
    else:
        with pytest.raises(github.GhError, match="Validation failed"):
            github.pr_for_branch(REPO, "lz/t1", head_sha=HEAD)
        assert len(gh.calls) == 1


@pytest.mark.parametrize("head_change", [
    {"ref": "lz/other"}, {"repo": {"full_name": "fork/widgets"}},
    {"repo": "malformed"}, None,
], ids=["changed-ref", "fork", "invalid-repo", "missing-head"])
def test_pr_for_branch_rejects_changed_or_malformed_detail(gh: FakeGh, head_change):
    if head_change is None:
        branch, diagnostic = "lz/x", "malformed PR detail for #7"
        gh.respond_pr_list([{"number": 7, "url": "u"}], branch=branch)
    else:
        branch, diagnostic = "lz/t1", "PR changed during branch lookup"
        detail = rest_pr_json(pr_json())
        detail["head"].update(head_change)
        gh.respond(pr_list_key(), [{"number": 7}])
        gh.respond(f"api repos/{REPO}/pulls/7", detail)
    with pytest.raises(github.GhError, match=diagnostic):
        github.pr_for_branch(REPO, branch)


@pytest.mark.parametrize("mergeable,merge_state,expected", [
    (True, "clean", ("MERGEABLE", "CLEAN")),
    (False, "dirty", ("CONFLICTING", "DIRTY")),
    (None, None, ("UNKNOWN", "UNKNOWN")),
    (True, "behind", ("MERGEABLE", "BEHIND")),
])
def test_pr_view_maps_rest_merge_evidence(gh: FakeGh, mergeable, merge_state, expected) -> None:
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
    gh.respond(f"api repos/{REPO}/pulls/7", rest_pr_json(pr_json()) | invalid)

    with pytest.raises(github.GhError):
        github.pr_view(REPO, 7)

@pytest.mark.parametrize("change", [
    {}, {"draft": False}, {"base": {"ref": "other"}},
    {"head": {"sha": HEAD, "ref": "lz/x", "repo": {"full_name": "fork/widgets"}}},
], ids=["created", "ready", "wrong-base", "foreign-head"])
def test_create_draft_pr_verifies_response(gh: FakeGh, change: dict) -> None:
    gh.respond(f"api repos/{REPO}/pulls", rest_pr_json(pr_json(), branch="lz/x") | change)
    if change:
        with pytest.raises(github.GhError, match="created PR differs"):
            github.create_draft_pr(REPO, "lz/x", "main", "Title", "Body")
    else:
        pr = github.create_draft_pr(REPO, "lz/x", "main", "Title", "Body text\n")
        assert gh.argv(0)[:4] == ["api", f"repos/{REPO}/pulls", "--method", "POST"]
        assert json.loads(gh.calls[0]["--input"]) == {
            "head": "lz/x", "base": "main", "title": "Title", "body": "Body text\n", "draft": True}
        assert pr.number == 7 and pr.is_draft
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


@pytest.mark.parametrize("raw", ["", "🙂" * 150_000 + "END"], ids=["empty", "unicode"])
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
    for comment, finding in zip(comments, FINDINGS, strict=True):
        if finding.severity == "suggestion":
            assert "loopzero:finding" not in comment["body"]
        else:
            assert comment["body"].splitlines()[0] == github.finding_marker(finding.severity, HEAD, finding)
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
    assert not raw or raw not in body
    assert "Raw reviewer output" not in body
    if family == "codex":
        assert hashlib.sha256(raw.encode()).hexdigest() in body and "<summary>Review evidence notice<" in body
    assert "Global concern" not in body, "blocking finding was anchored, not listed"
    assert all("subject_type" not in c for c in comments)


def test_legacy_transcript_truncates_on_utf8_boundary():
    tail = "x" * 29_999
    result = ReviewResult("claude", HEAD, "primary", "approve", (), "🧪" + tail)
    output = github._review_transcript(result, legacy_display=True)
    assert "Transcript truncated" in output and "<summary>Raw reviewer output<" in output
    assert output.endswith(f"\n{tail}\n```\n</details>")


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
                     duration_s=0.25, provenance=provenance)
    github.post_review(REPO, 7, HEAD, result)
    payload = review_payloads(gh)[0]
    assert payload["event"] == "APPROVE" and payload["comments"] == [] and not calls_for(gh, FILES_KEY)
    body = payload["body"]
    assert "0.2s: **approve**" in body
    requested = "requested-one" if metadata == "new" else "unknown"
    assert f"Call 1: requested model {requested} (effort {'medium' if metadata == 'new' else 'unknown'})" in body
    assert "CLI-reported model reported-one (effort unknown)" in body
    assert "Call 2: requested model unknown (effort unknown); CLI-reported model unknown (effort unknown)" in body
    assert "unqualified-" not in body and "forged-" not in body


def test_post_review_counts_only_lf_as_patch_line_break(gh: FakeGh) -> None:
    path = "src/a.py"
    patch = "@@ -1 +1,2 @@\n-old\n+left\u2028right\n+second\n"
    arm_review(gh, files=[{"filename": path, "patch": patch}])
    locations = [("important", 2, "In diff"), ("critical", 3, "Outside diff"), ("suggestion", 3, "Off-diff suggestion")]
    findings = tuple(Finding(severity, path, line, title, "") for severity, line, title in locations)
    github.post_review(REPO, 7, HEAD, review(findings=findings))
    assert github._right_lines(patch) == ({1, 2}, 1)
    payload, = review_payloads(gh)
    comments = payload["comments"]
    assert [(c["path"], c["line"]) for c in comments] == [(path, 2), (path, 1)]
    assert "Reviewer location src/a.py:3 is not in the diff" in comments[1]["body"]
    assert "Off-diff suggestion" in payload["body"]


@pytest.mark.parametrize(("files", "severity", "anchor"), [
    ([{"filename": "img.png"}, {"filename": "src/d.py", "patch": "@@ -3,0 +4,2 @@\n+p\n+q"}],
     "important", ("src/d.py", 4)),
    ([{"filename": "img.png"}], "critical", None),
    ([{"filename": "removed.py", "patch": "@@ -1 +0,0 @@\n-old"}], "critical", None),
], ids=["skip-image", "no-hunks", "deletion-only"])
def test_post_review_preserves_blocking_findings_without_an_anchor(gh: FakeGh, monkeypatch, files, severity, anchor):
    arm_review(gh, files=files)
    result = review(findings=(Finding(severity, None, None, "G", ""),))
    marker = f"<!-- loopzero:review v=1 head={HEAD} kind=primary source=model -->"
    github.post_review(REPO, 7, HEAD, result, marker)
    if anchor is None:
        payload, = review_payloads(gh)
        assert payload["comments"] == [] and payload["body"].startswith(github.BODY_BLOCKERS)
        assert "**critical**: G" in payload["body"]
        published = dict(body=payload["body"], user={"login": "publisher"}, state="COMMENTED", commit_id=HEAD)
        monkeypatch.setattr(github, "open_blocking_findings", lambda *_: [])
        clean_merge, changed = "b" * 40, "c" * 40
        monkeypatch.setattr(eligibility, "base_merge_material", lambda *a, **k: b"" if a[3] == clean_merge else None)
        assert eligibility.readiness_review([published], changed, ("publisher",), repo=REPO, number=7,
                                            base_branch="main") is None
        for head in (HEAD, clean_merge):
            evidence = eligibility.readiness_review([published], head, ("publisher",), repo=REPO, number=7, base_branch="main")
            pr = github.PR(7, f"https://github.com/{REPO}/pull/7", head, "main", False, "OPEN", "MERGEABLE")
            blocker = eligibility.body_blocker(evidence)
            assert blocker and github.readiness(REPO, pr, "main", (), head, publishers=("publisher",),
                                                review_blocker=blocker).reasons == (blocker,)
    else:
        (comment,) = comments_posted(gh)
        assert (comment["path"], comment["line"]) == anchor
        assert "subject_type" not in comment


@pytest.mark.parametrize("location", ["first-page", "second-page", "missing", "fallback-page"])
def test_post_review_reads_only_pages_needed_for_placement(gh, location):
    file = PR_FILES[0]
    unrelated = [{"filename": f"f{i}.py", "patch": "@@ -1 +1 @@\n+new"} for i in range(100)]
    page1, page2 = unrelated, [file]
    finding = FINDINGS[0]
    if location == "first-page":
        page1, page2 = [file, *unrelated[:99]], unrelated[99:]
    if location == "missing":
        finding = replace(finding, path="missing.py")
    elif location == "fallback-page":
        page1 = [{"filename": f"image{i}.png"} for i in range(100)]
        finding = Finding("important", None, None, "Global concern", "")
    arm_review(gh)
    gh.respond(FILES_KEY, page1, page2)
    github.post_review(REPO, 7, HEAD, review(findings=(finding,)))

    pages = ["page=1"] if location == "first-page" else ["page=1", "page=2"]
    assert [c["argv"][-2] for c in calls_for(gh, FILES_KEY)] == pages
    (comment,) = comments_posted(gh)
    anchor = {"missing": ("f0.py", 1), "fallback-page": ("src/a.py", 11)}
    assert (comment["path"], comment["line"]) == anchor.get(location, ("src/a.py", 10))
    if location != "fallback-page":
        assert comment["side"] == "RIGHT"
        assert ("anchored here" in comment["body"]) == (location == "missing")


@pytest.mark.parametrize("failure,remedy,label,gap", [
    ("not logged in", github.GH_AUTH_REMEDY, "", ""),
    ("HTTP 429 rate limit", "retry after GitHub's reset or Retry-After", "", ""),
    *(("HTTP 401", "HTTP 401", label, gap) for label in (
        "Authorization: Basic", "Authorization: Bearer", "Authorization: token",
        "Bearer", "access_token:", "auth_token:", "token:",
    ) for gap in (" ", "\n  ")),
])
def test_gh_errors_redact_credentials_and_keep_remediation(gh, failure, remedy, label, gap):
    if label:
        secret = "opaque-synthetic-value"
        header = f"{label}{gap}{secret}"
        gh.fail("api user", f"{failure}\n{header}")
        args, token = ("-H", header), ""
    else:
        secret = token = "ghs_FAKE000"
        gh.fail("api user", f"{failure}: {token}\ntoken:")
        args = ("-f", f"value={token}")

    with pytest.raises(github.GhError) as info:
        github._gh("api", "user", *args, token=token)

    assert remedy in str(info.value)
    if failure.startswith("HTTP 429"):
        assert "Rate limited:" in str(info.value)
    assert all(secret not in text for text in (str(info.value), info.value.tail, " ".join(info.value.command)))


def test_invalid_gh_json_reports_location_without_response_content(gh: FakeGh) -> None:
    gh.respond("api user", {"stdout": '{"access_token":"opaque-synthetic-value"', "exit": 0})

    with pytest.raises(github.GhError, match="invalid JSON from gh at line 1, column") as info:
        github._gh_json("api", "user")

    assert "opaque-synthetic-value" not in str(info.value)
    assert "opaque-synthetic-value" not in info.value.tail


def test_gh_timeout_redacts_diagnostics(monkeypatch) -> None:
    token = "ghs_FAKE000"
    timeout = github.ProcTimeout(f"gh timed out: {token}", token, stderr=token)
    monkeypatch.setattr(github, "run", Mock(side_effect=timeout))
    with pytest.raises(github.ProcTimeout, match="gh timed out: \\[REDACTED\\]") as info:
        github._gh("api", "user", "--method", "POST", token=token)

    assert info.value.stdout == info.value.stderr == "[REDACTED]"
    assert info.value.__suppress_context__


def arm_own_review_rejection(gh, action, *responses):
    rejection = {"message": "Unprocessable Entity", "errors": [{"message": f"Can not {action} your own pull request"}],
                 "documentation_url": "https://docs.github.com/rest"}
    gh.respond(REVIEWS_KEY, {"stdout": json.dumps(rejection), "stderr": "gh: Unprocessable Entity (HTTP 422)\n",
                           "exit": 1}, *responses)


@pytest.mark.parametrize("verdict,scenario", [
    ("approve", "lookup"), ("request_changes", "lookup"), ("approve", "supplied"),
    ("approve", "fallback"), ("request_changes", "fallback"), ("approve", "app"),
])
def test_post_review_submits_comment(gh: FakeGh, app_token_helper, verdict: str, scenario: str) -> None:
    arm_review(gh, author="bot-user" if scenario == "lookup" else "someone-else")
    pr = github.pr_from_rest(rest_pr_json(pr_json(author={"login": "bot-user"}))) if scenario == "supplied" else None
    if scenario == "app":
        app_token_helper()
        pr = github.PR(7, "url", HEAD, "main", True, "OPEN", "UNKNOWN")
    if scenario == "fallback":
        action = "approve" if verdict == "approve" else "request changes on"
        arm_own_review_rejection(gh, action, {"stdout": '{"id": 2}', "exit": 0})
    github.post_review(REPO, 7, HEAD, review(verdict=verdict, findings=FINDINGS[:1]), pr=pr)
    payloads = review_payloads(gh)
    assert payloads[-1]["event"] == "COMMENT"
    if scenario == "fallback":
        first, second = payloads
        assert first["event"] == verdict.upper()
        assert first["comments"] == second["comments"] and first["body"] == second["body"]
    else:
        assert len(payloads) == 1
        details = f"api repos/{REPO}/pulls/7"
        looked_up = scenario == "lookup"
        assert [c["argv"] for c in calls_for(gh, details)] == ([[*details.split(), "--include"]] if looked_up else [])
        if scenario == "app":
            assert calls_for(gh, REVIEWS_KEY)[0]["token"] == "synthetic-app-token" and payloads[0]["comments"]
        else:
            assert gh.argv(int(looked_up))[:2] == ["api", "graphql"]


@pytest.mark.parametrize("live,rejection,attempts,diagnostic", [
    ({"state": "CLOSED"}, None, 0, "PR is"),
    ({"baseRefName": "release"}, None, 0, "PR is"),
    ({"baseRefName": "release"}, "own PR", 1, "PR is"),
    ({"headRefOid": "b" * 40}, None, 0, "PR is"),
    ({}, "server error", 1, "HTTP 500"),
], ids=["closed", "retargeted", "retargeted-before-fallback", "moved", "posting-fails"])
def test_post_review_fails_closed(gh, live, rejection, attempts, diagnostic):
    arm_review(gh)
    respond = gh.append if rejection == "own PR" else gh.respond
    respond(f"api repos/{REPO}/pulls/7", rest_pr_json(pr_json(**live)))
    if rejection == "own PR":
        arm_own_review_rejection(gh, "approve")
    elif rejection:
        gh.fail(REVIEWS_KEY, "HTTP 500 server error")
    options = {}
    if rejection != "server error":
        pr = github.pr_from_rest(rest_pr_json(pr_json(isDraft=False)))
        options = dict(pr=pr, token="", publisher="bot-user", base="main")
    with pytest.raises(github.GhError, match=diagnostic):
        github.post_review(REPO, 7, HEAD, review(verdict="approve" if options else "request_changes"), **options)
    assert len(calls_for(gh, REVIEWS_KEY)) == attempts


# --- blocking findings ------------------------------------------------------------


def test_open_blocking_findings_filters_and_parses(gh: FakeGh) -> None:
    versioned = github.finding_marker(
        "critical", HEAD, Finding("critical", "src/a.py", 3, "Null deref", "x may be None")
    )
    app = thread(f"{marker('important')}\n**important: From the App**", path="src/c.py")
    app["comments"]["nodes"][0]["author"] = {"__typename": "Bot", "login": "loopzero-app"}
    gh.respond("api graphql", threads_json(
        thread(f"{versioned}\n**critical: Null deref**\n\nx may be None", edited="2026-09-18T01:00:00Z"),
        thread(f"{marker('important')}\nplain title", resolved=True),
        thread(f"{marker('suggestion')}\n**suggestion: Rename**", edited="2026-09-18T01:00:00Z"),
        thread(f"{marker('important')}\n**important: Missing check**\n\nvalidate",
               path="src/b.py", line=2),
        app,
    ))
    found = github.open_blocking_findings(REPO, 7, HEAD, ("BOT-user", "loopzero-app[bot]"))
    assert found == [
        Finding("critical", "src/a.py", 3, "Null deref", "**critical: Null deref**\n\nx may be None"),
        Finding("important", "src/b.py", 2, "Missing check",
                "**important: Missing check**\n\nvalidate"),
        Finding("important", "src/c.py", 3, "From the App", "**important: From the App**"),
    ]
    (argv,) = (call["argv"] for call in gh.calls)
    assert argv[:2] == ["api", "graphql"]
    assert "owner=acme" in argv and "name=widgets" in argv and "number=7" in argv
    assert any(a.startswith("query=") and "author { __typename login }" in a for a in argv)
    assert github.open_blocking_findings(REPO, 7, HEAD, ()) == []


@pytest.mark.parametrize("body, author", [
    # A valid marker written by someone who is not a review publisher.
    (f"{marker('critical')}\n**critical: Planted**", "outsider"),
    # The App user name without the Bot type is a different (user) account.
    (f"{marker('critical')}\n**critical: Lookalike**", "loopzero-app"),
    # A publisher comment that only quotes a marker below its first line.
    (f"**suggestion: Quote**\n\n{marker('critical')}", "bot-user"),
    (f" {marker('critical')}\n**critical: Indented**", "bot-user"),
    # Markers without a head and id are not tied to a reviewed head.
    ("<!-- loopzero:finding severity=important -->\nheadless", "bot-user"),
    (f"<!-- loopzero:finding severity=important head={HEAD} -->\nunversioned", "bot-user"),
    # Only the exact emitted line counts: not split, trailed by prose or truncated.
    (marker("critical").replace(" head=", "\nhead=") + "\n**critical: Split**", "bot-user"),
    (marker("critical") + " trailing prose\n**critical: Trailed**", "bot-user"),
    (marker("critical", head=HEAD[:7]) + "\n**critical: Short head**", "bot-user"),
    ("stripped loopzero marker\n**critical: X**", "bot-user"),
    ("mentions loopzero but a human wrote it", "human"),
    ("bot-user wrote this but it is unrelated", "bot-user"),
])
def test_open_findings_ignore_untrusted_or_quoted_markers(gh: FakeGh, body: str, author: str) -> None:
    gh.respond("api graphql", threads_json({**thread(body, author=author), "id": "T_1"}))
    assert github.open_blocking_findings(REPO, 7, HEAD, ("bot-user", "loopzero-app[bot]")) == []
    assert github.open_findings(REPO, 7, ("bot-user", "loopzero-app[bot]")) == []


@pytest.mark.parametrize("data", [
    {"data": {"repository": {"pullRequest": None}}},
    {"data": {"repository": None}},
    {"data": {"repository": {"pullRequest": {"reviewThreads": {"nodes": []}}}}},
    {"errors": [{"message": "nope"}]},
])
def test_open_blocking_findings_missing_pr_is_gh_error(gh: FakeGh, data: dict) -> None:
    gh.respond("api graphql", data)
    with pytest.raises(github.GhError, match="PR #7 not found or not accessible"):
        github.open_blocking_findings(REPO, 7, HEAD, PUBLISHERS)


def test_api_get_and_login_helpers(fake_bin, tmp_path, monkeypatch) -> None:
    gh = FakeGh(fake_bin, tmp_path)
    monkeypatch.setattr(subprocess, "_text_encoding", lambda: "ascii")
    gh.respond("api graphql viewer", '{"data":{"viewer":{"login":"Łódź"}}}', {"data": {"viewer": {}}})
    gh.respond("api repos/acme/widgets", '{"default_branch":"Łódź"}')
    assert (github.api_get("repos/acme/widgets"), gh.argv(0)) == ({"default_branch": "Łódź"}, ["api", "repos/acme/widgets", "--include"])
    assert github.login() == "Łódź"
    github._logins.clear()
    with pytest.raises(github.GhError, match="no login"):
        github.login()


def test_app_identity_selection_and_cache(gh: FakeGh, tmp_path: Path, monkeypatch, app_token_helper) -> None:
    counter = tmp_path / "helper-calls"
    app_token_helper(
        'test -z "${GH_TOKEN+x}${GITHUB_TOKEN+x}" || exit 1\n'
        f"echo called >> {counter}\necho synthetic-app-token\n"
    )
    monkeypatch.setenv("GITHUB_TOKEN", "secondary-operator-token")
    wall_time = boot_time = 10_000.0
    monkeypatch.setattr(github.time, "time", lambda: wall_time)
    monkeypatch.setattr(github.time, "CLOCK_BOOTTIME", 7, raising=False)
    monkeypatch.setattr(github.time, "clock_gettime", lambda _: boot_time)
    monkeypatch.setattr(github.time, "monotonic", lambda: 100.0)  # stopped during host suspend
    monkeypatch.setenv("GH_CONFIG_DIR", "/fixture/gh")
    monkeypatch.setenv("XDG_CONFIG_HOME", "/fixture/xdg")
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
    assert [c["token"] for c in gh.calls] == ["synthetic-app-token"] * 3 + ["operator-token"] * 3
    assert all(call["config"] == ["/fixture/gh", "/fixture/xdg"] for call in gh.calls)
    assert counter.read_text().splitlines() == ["called"]
    boot_time += 3_620
    wall_time += 2_620  # The wall clock stepped back by a thousand seconds.
    github.api_get("repos/acme/widgets")
    assert counter.read_text().splitlines() == ["called", "called"]


def test_app_token_age_counts_suspend_during_mint_and_retry(gh: FakeGh, tmp_path: Path, monkeypatch, app_token_helper) -> None:
    counter = tmp_path / "helper-calls"
    app_token_helper(f"echo called >> {counter}\necho synthetic-token-$(wc -l < {counter})\n")
    boot, real_run = [1_000_000.0], github.run
    suspend = lambda: boot.append(boot.pop() + 3601)  # noqa: E731
    monkeypatch.setattr(github, "_app_token_clock", lambda: boot[0])
    monkeypatch.setattr(github, "run", lambda argv, **k: (real_run(argv, **k), boot[0] == 1e6 and suspend())[0])
    monkeypatch.setattr(github, "_retry_sleep", lambda delay: suspend())
    monkeypatch.setenv("GH_TOKEN", "operator-token")
    gh.respond("api repos/acme/widgets", {}, {"stdout": "", "exit_code": 1,
                                              "stderr": "HTTP 502: Bad Gateway"}, {})
    github.api_get("repos/acme/widgets")  # mint, then suspend before the token is used
    github.api_get("repos/acme/widgets")  # fails transiently, then suspends in the backoff
    assert [c["token"] for c in gh.calls] == ["operator-token", "synthetic-token-2", "synthetic-token-3"]


def test_app_helper_failure_falls_back_to_operator(gh: FakeGh, monkeypatch, app_token_helper) -> None:
    app_token_helper("exit 1\n")
    monkeypatch.setenv("GH_TOKEN", "operator-token")
    gh.respond("api repos/acme/widgets", {})
    github.api_get("repos/acme/widgets")
    assert gh.calls[0]["token"] == "operator-token"


def test_marker_parser_and_finding_ids() -> None:
    assert github.MARKER_RE.fullmatch("<!-- loopzero:finding severity=critical -->") is None
    finding = Finding("important", "src/b.py", 2, "Missing check", "validate")
    new = github.MARKER_RE.fullmatch(github.finding_marker("important", HEAD, finding))
    assert new and new.groupdict() == {
        "severity": "important", "head": HEAD,
        "id": github.finding_id(HEAD, "src/b.py", 2, "Missing check"),
    }
    expected = github.finding_id(HEAD, "src/a.py", 10, "Null deref")
    assert all(github.finding_id(HEAD, "src/a.py", 10, "Null deref") == expected for _ in range(2))
    assert github.finding_id(HEAD, "src/a.py", 11, "Null deref") != expected
    assert github.finding_id(HEAD, "test/config1", 23, "Validate required option") != github.finding_id(
        HEAD, "test/config", 123, "Validate required option")


def test_open_blocking_findings_paginates(gh: FakeGh) -> None:
    gh.respond("api graphql",
               threads_json(thread(f"{marker('critical')}\nA"), has_next=True, cursor="C1"),
               threads_json(thread(f"{marker('important')}\nB")))
    found = github.open_blocking_findings(REPO, 7, HEAD, PUBLISHERS)
    assert [f.title for f in found] == ["A", "B"]
    assert "after=C1" not in gh.argv(0) and "after=C1" in gh.argv(1)
    assert gh.argv(1)[gh.argv(1).index("after=C1") - 1] == "-f"


@pytest.mark.parametrize("cursors", [(None,), ("C1", "C2", "C1"), ("C1",)], ids=["missing", "repeated", "reply"])
def test_review_thread_reads_reject_invalid_cursor(gh: FakeGh, cursors):
    reply = cursors == ("C1",)
    if reply:
        page = {"hasNextPage": True, "endCursor": "C1"}
        gh.respond("api graphql", {"data": {"node": {"comments": {"pageInfo": page, "nodes": []}}}})
    else:
        gh.respond("api graphql", *(threads_json(has_next=True, cursor=c) for c in cursors))
    with pytest.raises(github.GhError, match="missing or repeated review thread cursor"):
        if reply:
            github._has_thread_reply({"id": "T1", "comments": {"pageInfo": page}}, "reply")
        else:
            github.open_blocking_findings(REPO, 7, HEAD, PUBLISHERS)
    assert len(gh.calls) == len(cursors)
    if reply:
        argv = gh.argv(0)
        assert argv[argv.index("after=C1") - 1] == argv[argv.index("thread=T1") - 1] == "-f"


# --- checks --------------------------------------------------------------------------------


def check_run(conclusion, *, name="checks", status="completed", **fields):
    return {"name": name, "status": status, "conclusion": conclusion, **fields}


def arm_check_pages(gh, *pages, statuses=()):
    gh.respond(f"api repos/{REPO}/commits/{HEAD}/check-runs", *pages)
    gh.respond(f"api repos/{REPO}/commits/{HEAD}/statuses", *(statuses or ([],)))


def test_check_runs_merges_paginated_runs_and_statuses(gh: FakeGh) -> None:
    names = [f"job{i}" for i in range(99)]
    runs = [check_run("success", name=name) for name in names]
    statuses = [{"context": name, "state": "failure"} for name in [*(f"legacy/{n}" for n in names), "Checks"]]
    second = [{"context": "required-page-two", "state": "success"}, {"context": "checks", "state": "success"}]
    arm_check_pages(gh, {"check_runs": [*runs, check_run("success")]},
                    {"check_runs": [check_run(None, name="lint", status="in_progress")]},
                    statuses=(statuses, second))
    assert github._check_signals(REPO, HEAD)[0] == {
        **dict.fromkeys(names, "success"), **dict.fromkeys((row["context"] for row in statuses[:-1]), "failure"),
        "checks": "failure", "Checks": "failure", "lint": "pending", "required-page-two": "success",
    }
    requests = [(call["argv"][1].rsplit("/", 1)[-1], call["argv"][-2]) for call in gh.calls]
    assert requests == [("check-runs", "page=1"), ("check-runs", "page=2"),
                        ("statuses", "page=1"), ("statuses", "page=2")]
    for argv in (call["argv"] for call in gh.calls):
        assert "-F" in argv and argv[argv.index("--method") + 1] == "GET"


@pytest.mark.parametrize("rows,expected", [
    ([(10, "checks", None, "skipped"), (11, "checks", None, "success")], {"checks": "success"}),
    ([(20, "checks", "01:00:00Z", None), (19, "checks", "02:00:00Z", "success")], {"checks": "pending"}),
    ([(30, "checks", None, "success"), (31, "lint", None, "failure")], {"checks": "success", "lint": "failure"}),
], ids=[
    "stale-skipped", "id-before-time", "distinct-names",
])
def test_check_runs_ordering(gh: FakeGh, rows: list[tuple], expected: dict) -> None:
    runs = [check_run(conclusion, name=name, status="completed" if conclusion else "in_progress",
                      started_at=stamp and f"2026-09-23T{stamp}", **({"id": number} if number is not None else {}))
            for number, name, stamp, conclusion in rows]
    arm_check_pages(gh, {"check_runs": runs})
    assert github._check_signals(REPO, HEAD)[0] == expected


@pytest.mark.parametrize("page", [{"total_count": 1}, {"check_runs": 1}])
def test_check_runs_validates_check_page_shape(gh: FakeGh, page: dict) -> None:
    endpoint = f"repos/{REPO}/commits/{HEAD}/check-runs"
    arm_check_pages(gh, page)

    with pytest.raises(github.GhError, match=endpoint):
        github._check_signals(REPO, HEAD)


@pytest.mark.parametrize("pages,error", [
    ([[{"id": 1}, "not a review"]], "invalid paginated response"),
    ([{"check_runs": []}], "invalid paginated response"),  # an envelope where a list is due
    ([[{"id": i} for i in range(github.PAGE + 1)]], "invalid paginated response"),
    ([[{"id": page * github.PAGE + i} for i in range(github.PAGE)] for page in range(100)],
     "pagination exceeds 100 pages"),
])
def test_list_pagination_rejects_wrong_shapes_and_endless_pages(monkeypatch, pages, error) -> None:
    monkeypatch.setattr(github, "_gh_json", Mock(side_effect=pages))
    with pytest.raises(github.GhError, match=error):
        github.paged(f"repos/{REPO}/pulls/7/reviews")


@pytest.mark.parametrize("user", [{"login": 123}, "bot", {}])
def test_malformed_review_login_is_untrusted(user) -> None:
    assert eligibility.review_login({"user": user}) == ""


# --- readiness ----------------------------------------------------------------------------


def arm_readiness(gh: FakeGh, *, threads=(), runs=None) -> None:
    runs = {"checks": "success"} if runs is None else runs
    checks = (check_run(conclusion, name=name) for name, conclusion in runs.items())
    arm_workflow_check(gh, *checks, threads=threads)


def arm_workflow_check(gh: FakeGh, *checks: dict, statuses=(), threads=()) -> None:
    gh.respond("api graphql", threads_json(*threads))
    arm_check_pages(gh, {"check_runs": list(checks)}, statuses=(list(statuses),))


def readiness(*, reviewed=HEAD, workflow_wait=False, required_ci=("checks",), review_grace=False, **metadata):
    pr = github.pr_from_rest(rest_pr_json(pr_json(**metadata)))
    return github.readiness(REPO, pr, "main", required_ci, reviewed,
                            workflow_wait=workflow_wait, review_grace=review_grace, publishers=PUBLISHERS)


def failed_check(conclusion="failure", **fields):
    return check_run(conclusion, **({"check_suite": {"id": 10}} | fields))


def producing_run(**fields):
    return {"id": 1, "check_suite_id": 10, "workflow_id": 7, "head_sha": HEAD,
            "status": "completed", "created_at": "2026-09-23T21:53:00Z", **fields}


def replacement_run():
    return dict(producing_run(), id=2, check_suite_id=20, status="in_progress",
                created_at="2026-09-23T21:54:21Z")


def arm_workflow_runs(gh, *runs):
    pages = [{"workflow_runs": list(runs[i:i + 100])} for i in range(0, len(runs) + 1, 100)]
    gh.respond(f"api repos/{REPO}/actions/runs?head_sha={HEAD}", *pages)


NONBLOCKING_THREADS = (
    thread(f"{marker('critical')}\nResolved", resolved=True),
    thread(f"{marker('suggestion')}\nSuggestion"),
)


@pytest.mark.parametrize("reviewed,threads,reasons", [
    (HEAD, (), ()),
    ("b" * 40, (), (f"head {HEAD[:12]} differs from reviewed {'b' * 12}. Run `loopzero review` for the current head.",)),
    (None, (), ("no review recorded for this head. Run `loopzero review`.",)),
    pytest.param(HEAD, (thread(f"{marker('important')}\n**important: Oops**"),),
                 ("open important finding at src/a.py:3: Oops. Run `loopzero resolve` to list finding IDs and address the review.",), id="located"),
    pytest.param(HEAD, (thread(f"{marker('critical')}\nNo file location given by the reviewer; anchored here.\n\n"
                              "**critical: Global**\n\nno file", path="src/first.py", line=12),),
                 ("open critical finding at src/first.py:12: Global. Run `loopzero resolve` to list finding IDs and address the review.",), id="anchored"),
    pytest.param(HEAD, (
        thread(f"{marker('important', 'b' * 40)}\n**important: Still applies**"),
        thread(f"{marker('critical', 'b' * 40)}\n**critical: Obsolete**", outdated=True),
        thread(f"{marker('critical')}\n**critical: Current**", outdated=True),
    ), ("open important finding at src/a.py:3: Still applies. Run `loopzero resolve` to list finding IDs and address the review.",
        "open critical finding at src/a.py:3: Current. Run `loopzero resolve` to list finding IDs and address the review."), id="outdated"),
])
def test_readiness_requires_current_review_and_resolved_findings(gh, reviewed, threads, reasons):
    arm_readiness(gh, threads=(*threads, *NONBLOCKING_THREADS))
    assert readiness(reviewed=reviewed) == github.Readiness(not reasons, reasons)
    assert not calls_for(gh, f"api repos/{REPO}/actions/runs?head_sha={HEAD}")


@pytest.mark.parametrize("failure", ["failure", "error", "cancelled", "timed_out"])
@pytest.mark.parametrize("wait,grace", [(False, True), (True, False), (True, True)])
def test_readiness_missing_and_failed_checks(gh: FakeGh, failure, wait, grace) -> None:
    arm_readiness(gh, runs={"checks": failure})
    r = readiness(required_ci=("checks", "e2e"), workflow_wait=wait, review_grace=grace)
    assert r.reasons == (
        f"required check 'checks' is {failure}",
        f"required check 'e2e' missing on {HEAD[:12]}",
    )
    assert not r.ready
    assert r.waitable_failures == ((f"required check 'checks' is {failure}",) if wait and grace else ())


@pytest.mark.parametrize("check,runs,statuses,waitable,producer", [
    *(pytest.param(
        failed_check(failure, id=100, started_at="2026-09-23T21:53:00Z", completed_at="2026-09-23T21:54:41Z"),
        (producing_run(), replacement_run() | changes), [], waitable, None, id=case,
    ) for case, failure, changes, waitable in (
        ("failed", "failure", {}, True), ("cancelled", "cancelled", {}, True),
        ("different-workflow", "failure", {"workflow_id": 8}, False),
        ("different-head", "failure", {"head_sha": "b" * 40}, False),
        ("completed", "failure", {"status": "completed"}, False),
        ("older", "failure", {"created_at": "2026-09-23T21:52:00Z"}, False),
        ("offset-tie", "failure", {"created_at": "2026-09-23T22:53:00+01:00", "id": 0}, False),
        ("newer-fraction", "failure", {"created_at": "2026-09-23T21:53:00.250Z"}, True),
        ("invalid-creation", "failure", {"created_at": "invalid"}, False),
    )),
    pytest.param(failed_check(id=100), (producing_run(created_at=replacement_run()["created_at"]), replacement_run()),
                 [], True, None, id="same-second-newer-run"),
    pytest.param(failed_check(id=100), (producing_run(), replacement_run()),
                 [{"context": "checks", "state": "failure"}], False, None, id="failing-commit-status"),
    *(pytest.param(
        failed_check(id=100, completed_at="2026-09-23T21:54:41Z"),
        (producing_run(status="in_progress", run_attempt=2, run_started_at=started and f"2026-09-23T{started}"),),
        [], waitable, None, id=f"rerun-{started}",
    ) for started, waitable in (
        ("21:55:00Z", True), ("21:54:00Z", False), ("21:54:41.250Z", True),
        ("22:54:41+01:00", False), ("23:54:40.999+02:00", False), ("invalid", False), (None, False),
    )),
    pytest.param(failed_check(), (producing_run(), replacement_run()), [], True, "page", id="paginated-producer"),
    *(pytest.param(failed_check(), (replacement_run() | edits,), [], waitable, "suite", id=case)
      for case, edits, waitable in (
          ("suite-producer", {}, True), ("suite-wrong-workflow", {"workflow_id": 8}, False),
          ("suite-wrong-head", {"head_sha": "b" * 40}, False),
      )),
])
def test_readiness_defers_only_replaceable_failure(gh, check, runs, statuses, waitable, producer):
    arm_workflow_check(gh, check, statuses=statuses)
    suite = f"api repos/{REPO}/actions/runs?check_suite_id=10"
    if producer:
        filler = [producing_run(id=i, check_suite_id=i, workflow_id=99)
                  for i in range(100, 200 if producer == "page" else 1099)]
        if producer == "page":
            runs = (*filler, *runs)
        else:
            runs = (*runs, *filler)
            gh.respond(suite, {"workflow_runs": [producing_run()]})
    arm_workflow_runs(gh, *runs)
    result = readiness(workflow_wait=True)
    assert not result.ready and result.reasons == (f"required check 'checks' is {check['conclusion']}",)
    assert result.waitable_failures == (result.reasons if waitable else ())
    if producer:
        assert len(calls_for(gh, suite)) == int(producer == "suite")
    if producer == "page":
        assert [c["argv"][-2] for c in gh.calls if "actions/runs" in c["argv"][1]] == ["page=1", "page=2"]


@pytest.mark.parametrize("conclusion", [None, "skipped", "neutral", "failure"])
def test_readiness_reads_actions_only_for_failed_required_check(gh, conclusion):
    checks = (check_run("success"), failed_check(name="optional")) if conclusion is None else (failed_check(conclusion),)
    arm_workflow_check(gh, *checks)
    if conclusion == "failure":
        gh.fail(f"api repos/{REPO}/actions/runs?head_sha={HEAD}", "HTTP 403: Actions read denied")
        with pytest.raises(github.GhError, match="Actions read denied"):
            readiness(workflow_wait=True)
        return
    result = readiness(workflow_wait=True)
    if conclusion is None:
        assert result.ready
    else:
        assert result.reasons == (f"required check 'checks' is {conclusion}",)
        assert result.waitable_failures == ()
    assert not any("actions/runs" in " ".join(c["argv"]) for c in gh.calls)


@pytest.mark.parametrize(("metadata", "reason"), [
    ({"mergeStateStatus": "BEHIND"}, "PR #7 is behind main. Run `git fetch origin main`, `git merge origin/main`, `loopzero check` and `loopzero pr` before retrying readiness."),
    ({"mergeable": "CONFLICTING"}, "PR #7 has merge conflicts with main. Run `git fetch origin main` and `git merge origin/main`. Resolve conflicts, run `loopzero check` and `loopzero pr`, then follow the review guidance from `loopzero ready`."),
    ({"state": "MERGED"}, "PR #7 is MERGED, not open"),
], ids=["behind-base", "merge-conflict", "merged-pr"])
def test_readiness_metadata_blocker(gh: FakeGh, metadata: dict, reason: str) -> None:
    arm_readiness(gh)
    assert readiness(**metadata).reasons == (reason,)


# --- ready / merge ---------------------------------------------------------------------


@pytest.mark.parametrize("remote_head", ["f" * 40, None])
def test_delete_remote_branch_checks_encoded_ref(gh: FakeGh, remote_head: str | None) -> None:
    get = f"api repos/{REPO}/git/ref/heads/lz/fix%23516%3Fx%25"
    if remote_head is None:
        gh.fail(get, "gh: Not Found (HTTP 404)")
    else:
        gh.respond(get, {"object": {"sha": remote_head}})
    assert github.delete_remote_branch(REPO, "lz/fix#516?x%", HEAD) is (remote_head is None)
    assert [call["argv"] for call in gh.calls] == [[*get.split(), "--include"]]


def test_delete_remote_branch_scopes_operator_tokens(gh: FakeGh, fake_tool, monkeypatch) -> None:
    monkeypatch.setenv("GH_TOKEN", "operator-token")
    monkeypatch.setenv("GITHUB_TOKEN", "secondary-operator-token")
    gh.respond(f"api repos/{REPO}/git/ref/heads/probe", {"object": {"sha": HEAD}})
    fake_tool("git", f'''case "$1" in
        init) test -z "${{GH_TOKEN+x}}${{GITHUB_TOKEN+x}}" ;;
        -c) test "$GH_TOKEN:$GITHUB_TOKEN" = "operator-token:secondary-operator-token" &&
            test "$2" = 'credential.helper=!gh auth git-credential' &&
            test "$3" = push &&
            test "$4" = '--force-with-lease=refs/heads/probe:{HEAD}' ;;
        *) exit 1 ;;
    esac''')
    assert github.delete_remote_branch(REPO, "probe", HEAD)


def _queue_json(queued: bool) -> dict:
    return {"data": {"repository": {"pullRequest": {"isInMergeQueue": queued, "headRefOid": HEAD}}}}


def arm_merge(gh: FakeGh, queue: dict, *details: dict) -> None:
    gh.respond("pr merge", "")
    gh.respond(f"api repos/{REPO}/pulls/7", merge_json(), *details)
    gh.respond("api graphql", queue)


@pytest.mark.parametrize("case,strategy,diagnostic", [
    ("command error", "rebase", "mismatch"),
    ("hold", "squash", "merge-authority hold"), ("hold", "queue", "merge-authority hold"),
    ("missing", "merge", "GitHub queue lookup for PR #7: queue status could not be verified"),
    ("not queued", "merge", "neither merged nor queued"),
    ("other head", "queue", "head changed or unavailable"),
    ("invalid strategy", "fast-forward", "unknown merge strategy"),
    ("queued", "squash", None), ("queued", "queue", None), ("auto", "queue", None),
    ("landed", "squash", None), ("landed-during-outage", "squash", None),
])
def test_merge_requires_proof_of_admission_or_landing(gh, monkeypatch, case, strategy, diagnostic):
    queue = _queue_json(case in {"queued", "other head"})
    pr = queue["data"]["repository"]["pullRequest"]
    if case == "missing":
        queue["data"]["repository"]["pullRequest"] = None
    if case == "other head":
        pr["headRefOid"] = "b" * 40
    if case == "auto":
        pr["autoMergeRequest"] = {"enabledAt": "t"}
    landed = merge_json(state="MERGED", sha="d" * 40) if case.startswith("landed") else merge_json()
    if case == "hold":
        gh.respond(f"api repos/{REPO}/pulls/7", merge_json() | {"labels": [{"name": "hold"}]})
        gh.respond("api graphql", queue)
    elif case == "other head":
        gh.respond("api graphql", queue)
    elif case != "invalid strategy":
        arm_merge(gh, queue, merge_json(), landed)
    if case == "command error":
        gh.fail("pr merge", "head commit mismatch")
    if case == "landed-during-outage":
        monkeypatch.setattr(github, "_retry_sleep", lambda _: None)
        gh.fail("api graphql", "HTTP 502")

    if diagnostic:
        with pytest.raises(github.GhError, match=diagnostic) as info:
            github.merge(REPO, 7, strategy, HEAD, "main")
        calls = [c["argv"][:2] for c in gh.calls]
        assert ["api", "-X"] not in calls
        if case == "hold":
            assert ["pr", "merge"] not in calls
        if case == "command error":
            assert "--rebase" in info.value.command and "mismatch" in info.value.tail
        if case in {"invalid strategy", "other head"}:
            assert calls == ([] if case == "invalid strategy" else [["api", "graphql"]])
        return

    if strategy == "queue":
        gh.respond("api graphql", _queue_json(False), queue)
    assert github.merge(REPO, 7, strategy, HEAD, "main") == landed["merge_commit_sha"]
    if strategy == "queue":
        assert github.merge(REPO, 7, strategy, HEAD, "main") is None
    (write,) = [c["argv"] for c in gh.calls if c["argv"][:2] == ["pr", "merge"]]
    if strategy == "squash":
        assert write == ["pr", "merge", "7", "--repo", REPO, "--squash", "--match-head-commit", HEAD]
    else:
        assert "--squash" not in write
    assert "--queue" not in write


def test_eligibility_dispatch_retry_refuses_changed_head(monkeypatch):
    pr = github.PR(7, "https://example.invalid/7", HEAD, "main", False, "OPEN", "MERGEABLE")
    monkeypatch.setattr(github, "_gh", Mock(side_effect=github.ProcTimeout("lost reply", "")))
    monkeypatch.setattr(github, "_retry_sleep", lambda _: None)
    monkeypatch.setattr(github, "pr_for_branch", lambda *_args, **_kwargs: replace(pr, head_sha="b" * 40))
    monkeypatch.setattr(github, "_eligibility_dispatched", lambda *_args: True)

    with pytest.raises(github.GhError, match="the PR or head changed after resolution"):
        github.refresh_eligibility(REPO, pr, "lz/task", "main", lambda: False)


@pytest.mark.parametrize("args, attempts, error", [
    (("api", "repos/acme/widgets/pulls/1"), 2, ""),
    (("api", "repos/acme/widgets/commits/x/check-runs", "--method", "GET", "-F", "per_page=100"), 2, "dial tcp 140.82.121.5:443: i/o timeout"),
    (("api", "graphql", "-f", "query=query { viewer { login } }"), 2, "net/http: TLS handshake timeout"),
    (("api", "repos/acme/widgets/pulls/7"), 2, "connection reset by peer"),
    (("api", "repos/acme/widgets/pulls/7/reviews"), 2, "unexpected end of JSON input"),
    (("api", "graphql", "-f", "query=query { viewer { login } }"), 2, "gh: Something went wrong while executing your query"),
    (("api", "repos/acme/widgets/issues/1/comments", "-f", "body=x"), 1, "HTTP 503"),
    (("api", "graphql", "-f", "query=mutation { resolve }"), 1, "HTTP 503"),
])
def test_gh_retries_only_transient_reads(monkeypatch, args, attempts, error):
    seen, sleeps = [], []
    def run(argv, **_):
        seen.append(argv)
        failed = len(seen) == 1 or "connection reset" in error
        if failed and "pulls/1" in argv[2]:
            raise github.ProcTimeout("gh api timed out")
        return type("Done", (), {"exit_code": int(failed), "stdout": "ok", "stderr": error * failed})
    monkeypatch.setattr(github, "run", run)
    monkeypatch.setattr(github, "_retry_sleep", sleeps.append)
    if attempts == 2 and "connection reset" not in error:
        assert github._gh(*args) == "ok"
    else:
        with pytest.raises(github.GhError):
            github._gh(*args)
    assert len(seen) == attempts and sleeps == ([5] if attempts == 2 else [])


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
    ("0", "90", "hourly quota exhausted: retry after 2026-11-25T21:03:14+00:00"),
    ("4676", "90", "short-window limit: retry after 90 s"),
    ("4676", "", "short-window limit: retry after ~60 s"),
    ("", "90", "short-window limit: retry after 90 s"),
    ("", "Wed, 25 Nov 2026 21:03:14 GMT", "short-window limit: retry after Wed, 25 Nov 2026 21:03:14 GMT"),
    ("", "", "Rate limited: retry after GitHub's reset or Retry-After."),
])
def test_rest_rate_limit_uses_failed_response_headers(gh, monkeypatch, remaining, retry_after, hint):
    sleeps = []
    monkeypatch.setattr(github, "_retry_sleep", sleeps.append)
    monkeypatch.setattr(github.time, "time", lambda: 1795640554)
    headers = "HTTP/2.0 403 Forbidden\r\nX-Unrelated: never print this\r\n"
    if remaining:
        headers += f"X-RateLimit-Resource: core\r\nX-RateLimit-Remaining: {remaining}\r\nX-RateLimit-Reset: 1795640594\r\n"
    if retry_after:
        headers += f"Retry-After: {retry_after}\r\n"
    gh.respond(pr_list_key("lz/x"), headers + '\r\n{"message":"API rate limit exceeded"}',
               exit=1, stderr="gh: API rate limit exceeded for installation ID 123 (HTTP 403)")

    budget = 50 if remaining == "4676" else 100
    with github.bounded_reads(github.time.monotonic() + budget, retry=True), pytest.raises(github.GhError) as info:
        github.pr_for_branch(REPO, "lz/x")
    error = str(info.value)
    assert hint in error
    assert info.value.rate_reset == (1795640594 if remaining == "0" else None)
    if remaining:
        assert "X-RateLimit-Resource: core" in error
        assert f"X-RateLimit-Remaining: {remaining}" in error
        assert "X-RateLimit-Reset: 2026-11-25T21:03:14+00:00" in error
    if retry_after:
        assert f"Retry-After: {retry_after}" in error
    assert "never print this" not in error
    assert sleeps == ([90 if retry_after.isdigit() else 40] if retry_after and not remaining else [])
    assert [call["argv"] for call in gh.calls] == (
        [[*branch_head_key("lz/x").split(), "--include"]] + [[*pr_list_key("lz/x").split(), "--include"]] * (1 + len(sleeps)))


def test_published_findings_redact_secret_classes(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "gw_FAKE_reviewer_key\n")
    secret = ("eyJheader.eyJzdWIi.signature rt_FAKE0123456789abcdefghijkl AKIAFAKE0123456789AB "
              "-----BEGIN RSA PRIVATE KEY-----\nMIIEFAKEKEYBODY\n-----END RSA PRIVATE KEY----- "
              "postgresql://app:hunter2-FAKE@db/app gw_FAKE_reviewer_key")
    finding = Finding("important", None, None, secret, secret)
    assert github._comment_body(finding, HEAD, prefix=secret + "\n").count("[REDACTED]") == 18
    assert github._review_body(review(findings=(finding,)), [finding]).count("[REDACTED]") >= 6


@pytest.mark.parametrize("diagnostic", [
    {"data": {"rateLimit": {"remaining": 0, "resetAt": "2099-01-01T00:00:00Z"}}},
    "null", "[]", {"data": {"rateLimit": {"remaining": 0, "resetAt": 42}}},
    {"data": []}, {"data": {"rateLimit": None}}, "response-header", "http-200",
])
def test_graphql_limit_reports_only_verified_reset(gh, diagnostic):
    failure = "GraphQL: API rate limit exceeded"
    header = diagnostic in ("response-header", "http-200")
    verified = isinstance(diagnostic, dict) and "2099-01-01" in str(diagnostic)
    if header:
        headers = "X-RateLimit-Resource: graphql\r\nX-RateLimit-Remaining: 0\r\nX-RateLimit-Reset: 4070908800"
        if diagnostic == "http-200":
            gh.respond("api graphql", f"HTTP/2.0 200 OK\r\n{headers}\r\n\r\n"
                       '{"errors":[{"type":"RATE_LIMITED","message":"API rate limit exceeded"}]}', exit=1)
        else:
            gh.fail("api graphql", failure + "\n" + headers.lower().replace("\r\n", "\n"))
        match = "hourly quota exhausted" if diagnostic == "http-200" else "response header"
    else:
        gh.respond("api graphql", {"stdout": "", "stderr": failure, "exit": 1}, diagnostic)
        match = "GraphQL quota resets at 2099-01-01" if verified else failure
    with pytest.raises(github.GhError, match=match) as info:
        github._gh("api", "graphql", "-f", "query={viewer{login}}")

    assert len(gh.calls) == (1 if header else 2)
    if diagnostic == "http-200":
        assert isinstance(info.value, github.GhUnavailable) and info.value.rate_reset == 4070908800
    elif not header:
        assert info.value.command[-1] == "query={viewer{login}}"
        if verified:
            assert "rateLimit { remaining resetAt }" in gh.calls[1]["argv"][-1]
        else:
            assert "quota resets at" not in str(info.value)


@pytest.mark.parametrize("views,expected", [
    ((merge_json("MERGED", sha="c" * 40), merge_json(sha="d" * 40)), ("c" * 40, None)),
    *(((view,), "reject") for view in (
        None, {}, merge_json("CLOSED"), merge_json("MERGED"), merge_json("MERGED", sha="invalid"),
        merge_json("MERGED", sha="c" * 40) | {"merged": "true"}, merge_json(head="f" * 40),
        merge_json(head=None), merge_json() | {"head": []},
    )),
])
def test_merged_sha_requires_verified_merge_evidence(gh, views, expected):
    gh.respond(f"api repos/{REPO}/pulls/7", *views)
    if expected == "reject":
        with pytest.raises(github.GhError, match=r"merge evidence|without a verified merge|head changed"):
            github.merged_sha(REPO, 7, expected_head=HEAD)
    else:
        assert tuple(github.merged_sha(REPO, 7) for _ in views) == expected


@pytest.mark.parametrize("merged", [None, pr_json(number=101, state="MERGED")])
def test_merged_branch_history_reads_terminal_page(gh: FakeGh, merged: dict | None) -> None:
    gh.respond(branch_head_key(), {"commit": {"sha": HEAD}})
    gh.respond(f"api repos/{REPO}/commits/{HEAD}/pulls --method GET", [])
    closed_page = [closed_history_row(pr_json(number=n, state="CLOSED")) for n in range(1, 101)]
    gh.respond(branch_history_key(), closed_page)
    gh.respond(branch_history_key(2), [closed_history_row(merged)] if merged else [])
    if merged:
        gh.respond_pr_view(merged)
    pr = github.merged_pr_for_branch(REPO, "lz/t1")
    assert ((pr.number, pr.state) if pr else None) == ((101, "MERGED") if merged else None)
    assert gh.argv(3) == [*branch_history_key(2).split(), "--include"]


@pytest.mark.parametrize("change,detail", [
    *((change, None) for change in (
        {"number": True}, {"state": "open"}, {"merged_at": False},
        {"merged_at": "bad-date"}, {"merged_at": "2026-10-07T05:04:29"},
        {"head": None}, {"head": {"ref": "lz/other", "repo": {"full_name": REPO}}},
        {"head": {"ref": "lz/t1", "repo": {"full_name": "other/widgets"}}},
    )),
    *(({}, detail) for detail in (
        rest_pr_json(pr_json(state="CLOSED")),
        rest_pr_json(pr_json(state="MERGED"), branch="lz/other"),
        rest_pr_json(pr_json(state="MERGED")) |
        {"head": {"sha": HEAD, "ref": "lz/t1", "repo": {"full_name": "other/widgets"}}},
    )),
])
def test_merged_branch_history_requires_consistent_evidence(gh: FakeGh, change: dict, detail: dict | None) -> None:
    row = closed_history_row(pr_json(state="CLOSED" if detail is None else "MERGED"))
    gh.respond(branch_history_key(), [row | change])
    reason = "invalid or repeated closed PR history"
    if detail is not None:
        gh.respond(f"api repos/{REPO}/pulls/7", detail)
        reason = "PR changed during completion lookup"
    with pytest.raises(github.GhError, match=reason):
        github.merged_pr_for_branch(REPO, "lz/t1")


def test_merged_branch_history_fails_at_page_limit(monkeypatch) -> None:
    monkeypatch.setattr(github, "_branch_sha", lambda *_args, **_kwargs: None)
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
    commits = f"api repos/{REPO}/commits/{HEAD}/pulls --method GET"
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


@pytest.mark.parametrize(("unrelated", "legacy_success", "check_failure", "reason"), [
    (1000, False, False, "suite limit"),
    (999, False, False, "missing"),
    (1000, True, False, "suite limit"),
    (100, True, True, "failure"),
    (100, True, False, None),
])
def test_readiness_at_check_suite_limit(gh: FakeGh, monkeypatch, unrelated, legacy_success, check_failure, reason):
    gh.respond("api graphql", threads_json())
    statuses = [{"context": "required-ci", "state": "success"}] if legacy_success else []
    runs = [check_run("success", name="unrelated", check_suite={"id": suite})
            for suite in range(2, unrelated + 2)]
    if check_failure:
        runs.append(failed_check(name="required-ci", check_suite={"id": 1}))
    arm_check_pages(gh, *({"check_runs": runs[i:i + 100]} for i in range(0, len(runs) + 1, 100)),
                    statuses=(statuses,))
    result = readiness(required_ci=("required-ci",))

    reasons = {
        "suite limit": f"cannot determine required check 'required-ci' on {HEAD[:12]}: GitHub's 1,000-suite limit",
        "missing": f"required check 'required-ci' missing on {HEAD[:12]}",
        "failure": "required check 'required-ci' is failure",
    }
    assert result.ready == (reason is None)
    assert result.reasons == ((reasons[reason],) if reason else ())


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


def etag_response(body, tag="v1"):
    return f'HTTP/2.0 200 OK\r\nEtag: "{tag}"\r\n\r\n{body}'


def conditional_headers(gh):
    return [arg for call in gh.calls for arg in call["argv"] if arg.startswith("If-None-Match")]


def test_repeated_rest_reads_revalidate_by_etag_and_login_is_read_once(gh, app_token_helper):
    app_token_helper()
    endpoint = f"repos/{REPO}/pulls/7"
    date = "Fri, 27 Nov 2026 15:00:00 GMT"
    gh.respond(f"api {endpoint}", etag_response('{"n": 7}'), {
        "exit_code": 1, "stdout": f"HTTP/2.0 304 Not Modified\r\nDate: {date}\r\n\r\n",
        "stderr": "gh: HTTP 304",
    })
    gh.respond("api graphql viewer", {"data": {"viewer": {"login": "bot"}}})
    comments = f"repos/{REPO}/issues/1/comments"
    gh.respond(f"api {comments}", {})
    dates = []
    assert github.api_get(endpoint) == {"n": 7}
    assert github._gh("api", endpoint, conditional=True, dates=dates) == '{"n": 7}'
    assert dates == [date]
    assert github.login() == github.login() == "bot"
    assert sum("graphql" in call["argv"] for call in gh.calls) == 1
    github._gh("api", comments, "-f", "body=x")
    assert conditional_headers(gh) == ['If-None-Match: "v1"']
    assert 'If-None-Match: "v1"' in gh.argv(1)


def test_oversized_response_does_not_evict_other_reads(gh, monkeypatch, app_token_helper):
    app_token_helper()
    monkeypatch.setattr(github, "_ETAG_CACHE_BYTES", 100)
    for name, count in (("small", 5), ("big", 60)):  # 10 vs 120 UTF-8 bytes
        endpoint = f"repos/a/b/{name}"
        gh.respond(f"api {endpoint}", etag_response("ł" * count))
        github._gh("api", endpoint, conditional=True)
    assert [key[0][1] for key in github._etag_cache] == ["repos/a/b/small"]


@pytest.mark.parametrize("token", ["app-a", None, "env-a"], ids=["app", "stored-account", "environment-token"])
def test_login_and_etag_cache_depend_on_token(gh, monkeypatch, app_token_helper, token):
    gh.respond("api graphql viewer", {"data": {"viewer": {"login": "a"}}})
    endpoint = "repos/a/b/pulls/1"
    gh.respond(f"api {endpoint}", etag_response("{}"))
    if token == "app-a":
        app_token_helper("echo app-a\n")
    elif token:
        monkeypatch.setenv("GH_TOKEN", token)
    assert github.login() == github.login() == "a"
    github.api_get(endpoint)
    github.api_get(endpoint)
    assert (len(gh.calls), conditional_headers(gh)) == ((3, ['If-None-Match: "v1"']) if token else (4, []))
    assert github._logins == ({token: "a"} if token else {})
    assert all(call["token"] == token for call in gh.calls)
    if token == "env-a":
        monkeypatch.setenv("GH_TOKEN", "env-b")
        assert github.login() == "a" and len(gh.calls) == 4


@pytest.mark.parametrize("repo", ["acme/123", "acme/true", "acme/@etc-passwd"])
def test_graphql_string_variables_are_raw_fields(repo: str) -> None:
    """`gh -F` coerces numbers and booleans and reads `@file`; names and ids must stay strings."""
    argv = github._pull_query(repo, 7, "query")
    owner, name = repo.split("/")
    assert argv[argv.index(f"owner={owner}") - 1] == "-f" and argv[argv.index(f"name={name}") - 1] == "-f"
    assert argv[argv.index("number=7") - 1] == "-F"


def test_resolve_finding_sends_thread_id_as_raw_field(gh: FakeGh) -> None:
    node = {**thread(f"{marker('important')}\nA"), "id": "T1"}
    node["comments"]["pageInfo"] = {"hasNextPage": False, "endCursor": None}
    gh.respond("api graphql", threads_json(node),
               {"data": {"addPullRequestReviewThreadReply": {"comment": {"id": "C1"}}}},
               {"data": {"resolveReviewThread": {"thread": {"isResolved": True}}}})
    github.resolve_finding(REPO, 7, "0b9b59e7", "Fixed.", PUBLISHERS)
    for call in (1, 2):
        argv = gh.argv(call)
        assert argv[argv.index("thread=T1") - 1] == "-f"
