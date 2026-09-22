"""Trusted publisher behavior against GitHub metadata, without candidate execution."""
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from loopzero import github, hosted
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
    Path("workflow.toml").write_text(
        f'[repo]\nname="{REPO}"\nbase="main"\n'
        '[delivery]\nreview_publishers=["trusted-publisher"]\n'
    )
    gh = FakeGh(fake_bin, tmp_path)
    gh.respond(f"api repos/{REPO}/pulls/7", {
        "head": {"sha": HEAD, "ref": "lz/task"}, "user": {"login": "author"},
    })
    gh.respond("pr view", pr_json())
    gh.respond("api graphql", threads_json())
    gh.respond(f"api repos/{REPO}/statuses/{HEAD}", {})
    return gh


@pytest.mark.parametrize("state,expected", [("COMMENTED", 0), ("DISMISSED", 1), ("PENDING", 1)])
def test_source_publication_is_bound_to_live_review(publisher, state, expected):
    publisher.respond(f"api repos/{REPO}/pulls/7/reviews?per_page=100&page=1", [{
        "state": state, "commit_id": HEAD, "user": {"login": "trusted-publisher"},
        "body": f"<!-- loopzero:review v=1 head={HEAD} kind=primary source=model -->\n**approve**",
    }])
    assert hosted.main() == expected
    statuses = [json.loads(c["--input"]) for c in publisher.calls if "--input" in c]
    assert [s["state"] for s in statuses] == ["pending", "success" if expected == 0 else "failure"]
    assert all(s["context"] == "Loop-zero Eligibility" for s in statuses)


def test_review_api_failure_cannot_leave_the_refresh_green(publisher):
    publisher.respond(f"api repos/{REPO}/pulls/7/reviews?per_page=100&page=1",
                      "", exit=1, stderr="HTTP 503")
    with pytest.raises(github.GhError):
        hosted.main()
    statuses = [json.loads(c["--input"]) for c in publisher.calls if "--input" in c]
    assert [s["state"] for s in statuses] == ["pending"]


def test_candidate_publishes_aggregate_source_eligibility(tmp_path, monkeypatch):
    candidate = candidate_event()
    values = candidate_payloads(candidate)
    event_path = tmp_path / "event.json"
    event_path.write_text(json.dumps({"pull_request": {"number": 900}}))
    (tmp_path / "workflow.toml").write_text(
        '[repo]\nname="owner/repo"\nbase="main"\n'
        '[delivery]\nreview_publishers=["trusted-publisher"]\n'
    )
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("GITHUB_REPOSITORY", "owner/repo")
    monkeypatch.setenv("GITHUB_EVENT_PATH", str(event_path))

    def github_get(path):
        if path == "repos/owner/repo":
            return candidate["repository"]
        return values["/" + path]

    evaluated = []

    def evaluate(repo, number, head, base, publishers):
        evaluated.append((repo, number, head, base, publishers))
        return github.Readiness(True, ())

    published = []
    monkeypatch.setattr(github, "api_get", github_get)
    monkeypatch.setattr(github, "pr_view", lambda *_args: SimpleNamespace(head_sha="c" * 40))
    monkeypatch.setattr(github, "commit_status", lambda *args: published.append(args))
    monkeypatch.setattr(hosted.eligibility, "evaluate", evaluate)
    monkeypatch.setattr(
        "loopzero.mergify.api_get",
        lambda repo, suffix: candidate_status()
        if repo == "owner/repo" and suffix == "/merge-queue/status?branch=main"
        else None,
    )

    assert hosted.main() == 0
    assert evaluated == [
        ("owner/repo", 101, "1" * 40, "main", ("trusted-publisher",)),
        ("owner/repo", 102, "2" * 40, "main", ("trusted-publisher",)),
    ]
    assert published == [
        ("owner/repo", "c" * 40, "Loop-zero Eligibility", "pending",
         "Refreshing live source review evidence"),
        ("owner/repo", "c" * 40, "Loop-zero Eligibility", "success",
         "All attested candidate sources are reviewed"),
    ]


def test_privileged_workflow_runs_only_the_trusted_default_branch_package():
    workflow = (Path(__file__).parents[1] / ".github/workflows/eligibility.yml").read_text()
    assert "pull_request_target:" in workflow
    assert "ref: ${{ github.event.repository.default_branch }}" in workflow
    assert "persist-credentials: false" in workflow
    assert "permissions:\n      contents: read\n      pull-requests: read\n      statuses: write" in workflow
    assert "PYTHONPATH: src" in workflow
    assert "python -m loopzero.hosted" in workflow
    assert "github.event.pull_request.head" not in workflow
