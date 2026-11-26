"""Trusted publisher behavior against GitHub metadata, without candidate execution."""
import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from loopzero import eligibility, github, hosted
from loopzero.candidate import CandidateError, CandidateRaceError
from tests import test_github as fixtures
from tests.test_candidate import event as candidate_event
from tests.test_candidate import payloads as candidate_payloads
from tests.test_candidate import status as candidate_status
from tests.test_github import HEAD, REPO, FakeGh, pr_json, threads_json


@pytest.mark.parametrize("module,args", [
    ("eligibility", ["--config", "workflow.toml", "--pr", "7", "--head", HEAD]),
    ("hosted", []),
])
def test_eligibility_entry_points_report_malformed_config(tmp_path, module, args):
    (tmp_path / "workflow.toml").write_text("[repo\n")
    result = subprocess.run(
        [sys.executable, "-m", f"loopzero.{module}", *args],
        cwd=tmp_path, capture_output=True, text=True, check=False,
    )
    assert (result.returncode, result.stdout) == (1, "")
    assert result.stderr.startswith("workflow.toml: invalid TOML: ")
    assert "line 1, column 6" in result.stderr
    assert len(result.stderr.splitlines()) == 1


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
    (event := tmp_path / "event.json").write_text(json.dumps({"pull_request": {"number": 7}}))
    monkeypatch.setenv("GITHUB_EVENT_PATH", str(event))
    Path("workflow.toml").write_text(f'[repo]\nname="{REPO}"\nbase="main"\n[delivery]\nreview_publishers=["trusted-publisher"]\n')
    gh = FakeGh(fake_bin, tmp_path)
    gh.respond(f"api repos/{REPO}/pulls/7", {
        "head": {"sha": HEAD, "ref": "lz/task"}, "user": {"login": "author"},
    })
    gh.respond_pr_view(pr_json())
    gh.respond("api graphql", threads_json())
    gh.respond(f"api repos/{REPO}/statuses/{HEAD}", {})
    gh.respond(f"api repos/{REPO}/pulls/7/reviews?per_page=100&page=1", [])
    return gh


def written_statuses(publisher):
    return [json.loads(call["--input"]) for call in publisher.calls if "--input" in call]


def written_states(publisher):
    return [status["state"] for status in written_statuses(publisher)]


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
        publisher.respond("api graphql", threads_json(fixtures.thread(fixtures.marker("important") + "\nFinding",
                                                                         author="trusted-publisher")))
    assert hosted.main() == (1 if status == "failure" else 0)
    statuses = written_statuses(publisher)
    assert ([s["state"] for s in statuses] == ["pending", status]
            and all(s["context"] == "Loop-zero Eligibility" for s in statuses))
    assert status != "pending" or statuses[-1]["description"] == "Awaiting trusted review of current source head"


def test_hosted_reviews_reject_a_repeated_full_page(publisher):
    publisher.respond(f"api repos/{REPO}/pulls/7", fixtures.rest_pr_json(pr_json()))
    endpoint = f"api repos/{REPO}/pulls/7/reviews?per_page={github.PAGE}"
    page = [{"id": i, "state": "COMMENTED"} for i in range(github.PAGE)]
    for number in (1, 2):
        publisher.respond(f"{endpoint}&page={number}", page)
    with pytest.raises(github.GhError, match="GitHub repeated a full page"):
        eligibility.evaluate(REPO, 7, HEAD, "main", ("trusted-publisher",))


def test_merged_source_refresh_publishes_nothing(publisher):
    body = json.dumps({"head": {"sha": HEAD, "ref": "lz/task"}, "user": {"login": "author"}, "merged": True,
                       "title": "before\u2028after"}, ensure_ascii=False)
    publisher.respond(f"api repos/{REPO}/pulls/7", f"HTTP/2.0 200 OK\r\nX-GitHub-Request-Id: GHREQ-456\r\n\r\n{body}")
    assert hosted.main() == 0 and not [c for c in publisher.calls if "--input" in c] and publisher.argv(0)[-1] == "--include"


@pytest.mark.parametrize("state,readback", [
    ("success", "match"), ("failure", "match"), ("success", "absent"),
    ("success", "publisher"), ("success", "description"), ("success", "state"),
    ("success", "unreadable"),
])
def test_final_status_lost_reply(publisher, monkeypatch, state, readback):
    ready = state == "success"
    monkeypatch.setattr(hosted.eligibility, "evaluate",
                        lambda *_: github.Readiness(ready, () if ready else ("Blocking finding",)))
    post = github.commit_status
    endpoint = f"api repos/{REPO}/commits/{HEAD}/statuses"
    monkeypatch.setattr(github, "_retry_sleep", lambda _: None)

    def lose_reply(repo, head, context, value, description):
        post(repo, head, context, value, description)
        if description.startswith(("Refreshing", "Eligibility refresh failed")):
            return
        accepted = {"context": context, "state": value, "description": description,
                    "creator": {"login": "github-actions[bot]"}}
        assert value == state
        observed = {
            "absent": [],
            "publisher": [accepted | {"creator": {"login": "other"}}, accepted],
            "description": [accepted | {"description": "other"}, accepted],
            "state": [accepted | {"state": "pending"}, accepted],
            "unreadable": {"exit": 1, "stderr": "unavailable"},
        }.get(readback, [accepted])
        publisher.respond(endpoint, observed)
        raise github.ProcTimeout("lost status reply", "")

    monkeypatch.setattr(github, "commit_status", lose_reply)
    if readback == "match":
        assert hosted.main() == (0 if ready else 1)
    else:
        with pytest.raises(hosted.LoopZeroError, match="confirm eligibility status publication"):
            hosted.main()
    assert written_states(publisher) == ["pending", state]


@pytest.mark.parametrize("error", ["api", "recover", "head", "publication", "config", "before"])
def test_source_errors_cannot_become_successful_waits(publisher, monkeypatch, capsys, error):
    sleeps = []
    monkeypatch.setattr(github, "_retry_sleep", sleeps.append)
    endpoint = f"api repos/{REPO}/pulls/7" + ("" if error == "before" else "/reviews?per_page=100&page=1")
    if error in {"api", "recover", "before"}:
        fault = {"stdout": "HTTP/2.0 503 Service Unavailable\r\nX-GitHub-Request-Id: GHREQ-8774\r\n\r\n{\"message\":\"Service unavailable github_pat_1234567890ABCDEFGHIJKLMNOPQRSTUVWXYZ\"}", "exit": 1}
        publisher.respond(endpoint, fault, [{"state": "APPROVED", "commit_id": HEAD, "user": {"login": "trusted-publisher"}, "body": f"<!-- loopzero:review head={HEAD} kind=primary -->\nreview: **approve**"}] if error == "recover" else fault)
    elif error == "head":
        publisher.respond_pr_view(pr_json(), pr_json(headRefOid="b" * 40))
    elif error == "publication":
        publisher.respond(f"api repos/{REPO}/statuses/{HEAD}", {}, {"stdout": "", "exit": 1, "stderr": "invalid description (HTTP 422)"})
        publisher.respond(f"api repos/{REPO}/commits/{HEAD}/statuses", [{"context": "other"}])
    else:
        monkeypatch.setenv("GITHUB_REPOSITORY", "other/repo")
    if error in {"api", "recover", "before"}:
        assert hosted.main() == (1 if error == "before" else 0) and sleeps == [5]
        assert ("status=503 request_id=GHREQ-8774 message=Service unavailable [REDACTED]" in capsys.readouterr().err) == (error != "recover")
    else:
        with pytest.raises(hosted.LoopZeroError, match="invalid description" if error == "publication" else None):
            hosted.main()
    states = {"before": [], "config": [], "head": ["pending", "failure"], "api": ["pending"], "recover": ["pending", "success"]}
    assert written_states(publisher) == states.get(error, ["pending", "pending"])


@pytest.mark.parametrize("case,error,message", [
    ("valid", None, ""), ("policy", hosted.LoopZeroError, "no review recorded"),
    ("hold", hosted.LoopZeroError, "source #101: PR #101 has a merge-authority hold"),
    ("merged", CandidateError, "not open"),
    ("race", CandidateRaceError, "metadata changed"),
    ("malformed", CandidateError, "invalid payload"),
    ("head", hosted.LoopZeroError, "head changed"), ("publisher", CandidateError, "not open"),
    ("interrupt", KeyboardInterrupt, ""), ("api", None, ""),
    ("manual", hosted.LoopZeroError, "pull_request_target creation"), ("metadata", None, ""),
    ("stale", hosted.LoopZeroError, "head is no longer current"),
    ("untrusted", CandidateError, "head event was not sent"),
])
def test_candidate_publication_and_event_controls(tmp_path, fake_bin, monkeypatch, case, error, message):
    event = candidate_event()
    event["action"], event["sender"]["id"] = (
        "edited" if case == "metadata" else event["action"], 1 if case == "untrusted" else event["sender"]["id"])
    values = candidate_payloads(event)
    if case in {"valid", "hold"}:
        gh = FakeGh(fake_bin, tmp_path)
        gh.respond("api graphql", threads_json())
        for number, sha in [(101, "1" * 40), (102, "2" * 40), (900, "c" * 40)]:
            path = f"/repos/owner/repo/pulls/{number}"
            values[path] = fixtures.rest_pr_json(pr_json(
                number=number, headRefOid=sha, isDraft=False,
                labels=[{"name": "hold"}] if case == "hold" and number == 101 else [],
            )) | values[path]
            values[f"{path}/reviews?per_page=100&page=1"] = [{
                "state": "APPROVED", "commit_id": sha, "user": {"login": "trusted-publisher"},
                "body": f"<!-- loopzero:review head={sha} kind=primary -->\nreview: **approve**",
            }]
    if case == "stale":
        values["/repos/owner/repo/pulls/900"]["head"]["sha"] = "d" * 40
    if case == "malformed":
        values["/repos/owner/repo/pulls/101"] = []
    (tmp_path / "event.json").write_text(json.dumps(event))
    (tmp_path / "workflow.toml").write_text('[repo]\nname="owner/repo"\nbase="main"\n[delivery]\nreview_publishers=["trusted-publisher"]\n')
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("GITHUB_REPOSITORY", "owner/repo")
    monkeypatch.setenv("GITHUB_EVENT_PATH", str(tmp_path / "event.json"))
    attempts, sleeps = [], []

    def api_get(path):
        if path == "apps/mergify":
            attempts.append(path)
        if case == "race" and path == "repos/owner/repo/pulls/101":
            values["/" + path]["body"] += " changed"
        return event["repository"] if path == "repos/owner/repo" else values["/" + path]
    monkeypatch.setattr(github, "api_get", api_get)
    monkeypatch.setattr("loopzero.candidate._retry_sleep", sleeps.append)
    published = []
    if case == "manual":
        (tmp_path / "event.json").write_text(json.dumps({"inputs": {"pr": "900"}}))

    def publish(*args):
        published.append(args)
        if args[3] == "pending" and case in {"merged", "publisher"}:
            values["/repos/owner/repo/pulls/101"]["state"] = "closed"
        if args[3] == "failure" and case == "publisher":
            raise RuntimeError("publication details must not replace attestation error")
    monkeypatch.setattr(github, "commit_status", publish)
    evaluated, real_evaluate = [], hosted.eligibility.evaluate

    def evaluate(repo, number, head, base, publishers):
        evaluated.append((repo, number, head, base, publishers))
        if case in {"valid", "hold"}:
            return real_evaluate(repo, number, head, base, publishers)
        if fault := {"interrupt": KeyboardInterrupt(), "api": github.GhUnavailable(("gh",), "HTTP 503")}.get(case):
            raise fault
        reasons = (github.MISSING_REVIEW_REASON,) if case == "policy" else ()
        return github.Readiness(not reasons, reasons)
    if case not in {"valid", "hold"}:
        monkeypatch.setattr(github, "pr_view", lambda *_: SimpleNamespace(head_sha=("d" if case == "head" else "c") * 40))
    monkeypatch.setattr(hosted.eligibility, "evaluate", evaluate)
    monkeypatch.setattr("loopzero.mergify.api_get", lambda repo, suffix: candidate_status()
                        if (repo, suffix) == ("owner/repo", "/merge-queue/status?branch=main") else None)
    if error:
        with pytest.raises(error, match=message or None):
            hosted.main()
    else:
        assert hosted.main() == 0
    terminal = [] if case in {"manual", "metadata", "stale", "interrupt", "api"} else ["failure"]
    expected = [] if case in {"manual", "metadata", "stale"} else ["pending", *terminal]
    assert case != "race" or (len(attempts) == 3 and sleeps == [2, 2] and len(evaluated) == 6)
    if case == "valid":
        assert evaluated == [("owner/repo", 101, "1" * 40, "main", ("trusted-publisher",)),
                             ("owner/repo", 102, "2" * 40, "main", ("trusted-publisher",))]
        assert published == [("owner/repo", "c" * 40, "Loop-zero Eligibility", "pending",
                              "Refreshing live source review evidence"),
                             ("owner/repo", "c" * 40, "Loop-zero Eligibility", "success",
                              "All attested candidate sources are reviewed")]
    else:
        assert [call[3] for call in published] == expected
        assert all(call[1] == "c" * 40 for call in published)
        assert not terminal or published[-1][-1] == "Eligibility refresh failed; inspect workflow logs"


def test_privileged_workflow_runs_only_the_trusted_default_branch_package():
    workflow = (Path(__file__).parents[1] / ".github/workflows/eligibility.yml").read_text()
    assert "pull_request_target:" in workflow
    events = next(line for line in workflow.splitlines() if line.strip().startswith("types:"))
    assert "labeled" in events and "unlabeled" in events
    source, candidate = workflow.split("\n  source-eligibility:\n", 1)[1].split(
        "\n  candidate-eligibility:\n", 1
    )
    assert "    environment:" not in source
    assert "${{ secrets.MERGIFY_API_KEY }}" not in source
    assert "github.event.label.name == 'hold'" in source
    assert "environment:\n      name: mergify-metadata\n      deployment: false" in candidate
    assert "MERGIFY_API_KEY: ${{ secrets.MERGIFY_API_KEY }}" in candidate
    for job in (source, candidate):
        assert "ref: ${{ github.event.repository.default_branch }}" in job
        assert "ref: ${{ github.event.pull_request.head" not in job
        assert "persist-credentials: false" in job
        assert "permissions:\n      contents: read\n      pull-requests: read\n      statuses: write" in job
        assert "PYTHONPATH: src" in job
        assert "python -m loopzero.hosted" in job
    ruleset = json.loads(
        (Path(__file__).parents[1] / ".github/rulesets/main.json").read_text()
    )
    status_rule = next(rule for rule in ruleset["rules"]
                       if rule["type"] == "required_status_checks")
    assert [check["context"] for check in
            status_rule["parameters"]["required_status_checks"]] == ["checks", "Loop-zero Eligibility"]


@pytest.mark.parametrize("prior", [998, 999, 1000])
def test_source_status_limit(publisher, monkeypatch, capsys, prior):
    context = hosted.eligibility.CONTEXT
    endpoint = f"api repos/{REPO}/statuses/{HEAD}"
    refused = {
        "stdout": 'HTTP/2.0 422 Unprocessable Entity\r\nX-GitHub-Request-Id: TEST-422\r\n\r\n'
                  '{"message":"Validation Failed"}',
        "stderr": "gh: Validation Failed (HTTP 422)",
        "exit": 1,
    }
    successful = max(0, 1000 - prior)
    publisher.respond(endpoint, *([{}] * successful), refused, refused)
    if prior >= 999:
        statuses = [{"id": i, "context": context, "state": "success"} for i in range(prior)]
        if prior == 999:
            statuses.insert(0, {"id": prior, "context": context, "state": "pending"})
        pages = [statuses[i:i + 100] for i in range(0, 1001, 100)]
        publisher.respond(f"api repos/{REPO}/commits/{HEAD}/statuses", *pages, *pages)

    monkeypatch.setattr(hosted.eligibility, "evaluate", lambda *args: SimpleNamespace(
        ready=True, reasons=(),
    ))

    if prior == 998:
        assert hosted.main() == 0
        assert written_states(publisher) == ["pending", "success"]
        assert not any("/commits/" in " ".join(call["argv"]) for call in publisher.calls)
        return

    message = rf"{HEAD}.*{context}.*1,000.*fresh head.*review"
    expected = ["pending", "success"] if prior == 999 else ["pending"]
    with pytest.raises(github.GhError, match=message):
        hosted.main()
    assert written_states(publisher) == expected

    with pytest.raises(github.GhError, match=message):
        hosted.main()
    assert written_states(publisher) == [*expected, "pending"]
    assert capsys.readouterr().out == ""
