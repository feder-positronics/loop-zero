"""Trusted publisher behavior against GitHub metadata, without candidate execution."""
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from loopzero import github, hosted
from loopzero.candidate import CandidateError
from tests import test_github as fixtures
from tests.test_candidate import event as candidate_event
from tests.test_candidate import payloads as candidate_payloads
from tests.test_candidate import status as candidate_status
from tests.test_github import HEAD, REPO, FakeGh, pr_json, threads_json


@pytest.mark.parametrize("event,number", [
    ({"pull_request": {"number": 7}}, 7),
    ({"workflow_run": {"pull_requests": [{"number": 7}]}}, 7),
    ({"inputs": {"pr": "7"}}, 7),
    ({"inputs": {"pr": "$(echo unsafe)"}}, None),
    ({"inputs": {"pr": True}}, None),
    ({"workflow_run": {"pull_requests": []}}, None),
    ({"workflow_run": {"pull_requests": [{"number": 7}, {"number": 8}]}}, None),
])
def test_event_selection(event, number):
    if number is None:
        with pytest.raises(hosted.LoopZeroError):
            hosted.event_number(event)
    else:
        assert hosted.event_number(event) == number


@pytest.fixture
def publisher(tmp_path, fake_bin, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("GITHUB_REPOSITORY", REPO)
    event = tmp_path / "event.json"
    event.write_text(json.dumps({"pull_request": {"number": 7}}))
    monkeypatch.setenv("GITHUB_EVENT_PATH", str(event))
    Path("workflow.toml").write_text(f'[repo]\nname="{REPO}"\nbase="main"\n[delivery]\nreview_publishers=["trusted-publisher"]\n')
    gh = FakeGh(fake_bin, tmp_path)
    gh.respond(f"api repos/{REPO}/pulls/7", {
        "head": {"sha": HEAD, "ref": "lz/task"}, "user": {"login": "author"},
    })
    gh.respond_pr_view(pr_json())
    gh.respond("api graphql", threads_json())
    gh.respond(f"api repos/{REPO}/statuses/{HEAD}", {})
    return gh


@pytest.mark.parametrize("case,blocker,status", [
    ("COMMENTED", "", "success"), ("APPROVED", "", "success"),
    ("DISMISSED", "", "pending"), ("PENDING", "", "pending"),
    ("absent", "", "pending"), ("untrusted", "", "pending"),
    ("stale", "", "pending"), ("malformed", "", "pending"),
    ("revoked", "", "pending"), ("absent", "important", "failure"),
    ("absent", "conflict", "failure"), ("COMMENTED", "important", "failure"),
])
def test_source_publication_is_bound_to_live_review(publisher, case, blocker, status):
    review = {"state": "COMMENTED", "commit_id": HEAD, "user": {"login": "trusted-publisher"},
              "body": f"<!-- loopzero:review v=1 head={HEAD} kind=primary source=model -->\n**approve**"}
    changed = {"state": case} if case in {"APPROVED", "DISMISSED", "PENDING"} else {
        "untrusted": {"user": {"login": "attacker"}}, "stale": {"commit_id": "b" * 40},
        "malformed": {"body": "<!-- loopzero:review invalid -->\n**approve**"}, "revoked": {"state": "DISMISSED"},
    }.get(case, {})
    reviews = [] if case == "absent" else [review | changed]
    if case == "revoked":
        reviews.insert(0, review | {"state": "APPROVED"})
    publisher.respond(f"api repos/{REPO}/pulls/7/reviews?per_page=100&page=1", reviews)
    if blocker == "conflict":
        publisher.respond(f"api repos/{REPO}/pulls/7", fixtures.rest_pr_json(pr_json(mergeable="CONFLICTING")))
    if blocker == "important":
        publisher.respond("api graphql", threads_json(fixtures.thread(fixtures.marker("important") + "\nFinding")))
    assert hosted.main() == (1 if status == "failure" else 0)
    statuses = [json.loads(c["--input"]) for c in publisher.calls if "--input" in c]
    assert [s["state"] for s in statuses] == ["pending", status]
    assert all(s["context"] == "Loop-zero Eligibility" for s in statuses)
    if status == "pending":
        assert statuses[-1]["description"] == "Awaiting trusted review of current source head"


@pytest.mark.parametrize("error", ["api", "head", "publication", "config"])
def test_source_errors_cannot_become_successful_waits(publisher, monkeypatch, error):
    monkeypatch.setattr(github, "_retry_sleep", lambda _: None)
    publisher.respond(f"api repos/{REPO}/pulls/7/reviews?per_page=100&page=1", [])
    if error == "api":
        publisher.respond(f"api repos/{REPO}/pulls/7/reviews?per_page=100&page=1",
                          "", exit=1, stderr="HTTP 503")
    elif error == "head":
        publisher.respond_pr_view(pr_json(), pr_json(headRefOid="b" * 40))
    elif error == "publication":
        publisher.respond(f"api repos/{REPO}/statuses/{HEAD}", {},
                          {"stdout": "", "exit": 1, "stderr": "HTTP 422"})
    else:
        monkeypatch.setenv("GITHUB_REPOSITORY", "other/repo")
    with pytest.raises((github.GhError, hosted.LoopZeroError)):
        hosted.main()
    statuses = [json.loads(c["--input"])["state"] for c in publisher.calls if "--input" in c]
    assert statuses == ([] if error == "config" else ["pending", "pending"]
                        if error == "publication" else ["pending"])


def candidate_publisher(tmp_path, monkeypatch, event, values=None):
    values = values or candidate_payloads(event)
    (event_path := tmp_path / "event.json").write_text(json.dumps(event))
    (tmp_path / "workflow.toml").write_text('[repo]\nname="owner/repo"\nbase="main"\n[delivery]\nreview_publishers=["trusted-publisher"]\n')
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("GITHUB_REPOSITORY", "owner/repo")
    monkeypatch.setenv("GITHUB_EVENT_PATH", str(event_path))
    monkeypatch.setattr(github, "api_get", lambda path: event["repository"]
                        if path == "repos/owner/repo" else values["/" + path])
    published = []
    monkeypatch.setattr(github, "commit_status", lambda *args: published.append(args))
    return published


@pytest.mark.parametrize("ready", [True, False])
def test_candidate_publishes_aggregate_source_eligibility(tmp_path, monkeypatch, ready):
    published = candidate_publisher(tmp_path, monkeypatch, candidate_event())
    evaluated = []
    def evaluate(repo, number, head, base, publishers):
        evaluated.append((repo, number, head, base, publishers))
        return github.Readiness(ready, () if ready else (github.MISSING_REVIEW_REASON,))
    monkeypatch.setattr(github, "pr_view", lambda *_args: SimpleNamespace(head_sha="c" * 40))
    monkeypatch.setattr(hosted.eligibility, "evaluate", evaluate)
    monkeypatch.setattr("loopzero.mergify.api_get", lambda repo, suffix: candidate_status()
                        if (repo, suffix) == ("owner/repo", "/merge-queue/status?branch=main") else None)
    if not ready:
        with pytest.raises(hosted.LoopZeroError, match="no review recorded"):
            hosted.main()
        assert [call[3] for call in published] == ["pending"]
        return
    assert hosted.main() == 0
    assert evaluated == [("owner/repo", 101, "1" * 40, "main", ("trusted-publisher",)),
                         ("owner/repo", 102, "2" * 40, "main", ("trusted-publisher",))]
    assert published == [("owner/repo", "c" * 40, "Loop-zero Eligibility", "pending",
                          "Refreshing live source review evidence"),
                         ("owner/repo", "c" * 40, "Loop-zero Eligibility", "success",
                          "All attested candidate sources are reviewed")]


@pytest.mark.parametrize("case,error,message,states", [
    ("manual", hosted.LoopZeroError, "pull_request_target creation", []),
    ("metadata", None, "", []),
    ("stale", hosted.LoopZeroError, "head is no longer current", []),
    ("untrusted", CandidateError, "head event was not sent", ["pending"]),
])
def test_candidate_event_controls(tmp_path, monkeypatch, case, error, message, states):
    candidate = candidate_event()
    candidate["action"] = "edited" if case == "metadata" else candidate["action"]
    candidate["sender"]["id"] = 1 if case == "untrusted" else candidate["sender"]["id"]
    live = candidate_payloads(candidate)
    if case == "stale":
        live["/repos/owner/repo/pulls/900"]["head"]["sha"] = "d" * 40
    published = candidate_publisher(tmp_path, monkeypatch, candidate, live)
    if case == "manual":
        (tmp_path / "event.json").write_text(json.dumps({"inputs": {"pr": "900"}}))
    if error:
        with pytest.raises(error, match=message):
            hosted.main()
    else:
        assert hosted.main() == 0
    assert [call[3] for call in published] == states


def test_privileged_workflow_runs_only_the_trusted_default_branch_package():
    workflow = (Path(__file__).parents[1] / ".github/workflows/eligibility.yml").read_text()
    assert "pull_request_target:" in workflow
    assert "ref: ${{ github.event.repository.default_branch }}" in workflow
    assert "persist-credentials: false" in workflow
    assert "environment:\n      name: mergify-metadata\n      deployment: false" in workflow
    assert "permissions:\n      contents: read\n      pull-requests: read\n      statuses: write" in workflow
    assert "PYTHONPATH: src" in workflow
    assert "python -m loopzero.hosted" in workflow
    assert "github.event.pull_request.head" not in workflow

    ruleset = json.loads(
        (Path(__file__).parents[1] / ".github/rulesets/main.json").read_text()
    )
    status_rule = next(rule for rule in ruleset["rules"]
                       if rule["type"] == "required_status_checks")
    assert [check["context"] for check in
            status_rule["parameters"]["required_status_checks"]] == ["checks", "Loop-zero Eligibility"]
