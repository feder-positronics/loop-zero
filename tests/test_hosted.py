"""Trusted publisher behavior against GitHub metadata, without candidate execution."""
import json
import signal
import subprocess
import sys
import urllib.error
from contextlib import nullcontext
from functools import partial
from pathlib import Path
from types import SimpleNamespace

import pytest

from loopzero import github, hosted
from loopzero.candidate import CandidateError, CandidateRaceError
from tests import test_github as fixtures
from tests.conftest import git
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
    with pytest.raises(hosted.LoopZeroError) if number is None else nullcontext():
        assert hosted.event_number(event) == number


def prepare_event(tmp_path, monkeypatch, repo, event, *, base="main"):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("GITHUB_REPOSITORY", repo)
    Path("event.json").write_text(json.dumps(event))
    monkeypatch.setenv("GITHUB_EVENT_PATH", str(tmp_path / "event.json"))
    Path("workflow.toml").write_text(f'[repo]\nname="{repo}"\nbase="{base}"\n[delivery]\nreview_publishers=["trusted-publisher"]\n')


@pytest.fixture
def publisher(tmp_path, fake_bin, monkeypatch):
    prepare_event(tmp_path, monkeypatch, REPO, {"pull_request": {"number": 7}})
    gh = FakeGh(fake_bin, tmp_path)
    gh.respond_pr_view(pr_json(), branch="lz/task")
    gh.respond("api graphql", threads_json())
    gh.respond(f"api repos/{REPO}/statuses/{HEAD}", {})
    gh.respond(f"api repos/{REPO}/pulls/7/reviews?per_page=100&page=1", [])
    return gh


def approval(head=HEAD, *, state="APPROVED", verdict="**approve**"):
    return {"state": state, "commit_id": head, "user": {"login": "trusted-publisher"},
            "body": f"<!-- loopzero:review v=1 head={head} kind=primary source=model -->\n{verdict}"}


def check_source(publisher, states, *, exit_code=0, error=None, match=None):
    with pytest.raises(error, match=match) if error else nullcontext():
        assert hosted.main() == exit_code
    statuses = [json.loads(call["--input"]) for call in publisher.calls if "--input" in call]
    assert [status["state"] for status in statuses] == states
    return statuses


@pytest.mark.parametrize("case,blocker,status", [
    ("COMMENTED", "", "success"), ("APPROVED", "", "success"),
    ("absent", "", "pending"), ("untrusted", "", "pending"),
    ("stale", "", "pending"), ("malformed", "", "pending"),
    ("revoked", "", "pending"), ("absent", "important", "failure"),
    ("absent", "conflict", "failure"), ("COMMENTED", "important", "failure"),
    ("APPROVED", "unknown", "pending"), ("APPROVED", "settles", "success"),
])
def test_source_publication_is_bound_to_live_review(publisher, monkeypatch, case, blocker, status):
    review = approval() | {"state": "COMMENTED"}
    reviews = {
        "absent": [], "APPROVED": [approval()], "revoked": [approval(), review | {"state": "DISMISSED"}],
        "untrusted": [review | {"user": {"login": "attacker"}}], "stale": [approval("b" * 40)],
        "malformed": [review | {"body": "<!-- loopzero:review invalid -->\n**approve**"}],
    }.get(case, [review])
    publisher.respond(f"api repos/{REPO}/pulls/7/reviews?per_page=100&page=1", reviews)
    delays = []
    monkeypatch.setattr(hosted.eligibility, "sleep", delays.append)
    if blocker in {"conflict", "unknown", "settles"}:
        initial = pr_json(mergeable="CONFLICTING" if blocker == "conflict" else "UNKNOWN")
        snapshots = [initial, pr_json()] if blocker == "settles" else [initial]
        publisher.respond(f"api repos/{REPO}/pulls/7", *(fixtures.rest_pr_json(pr) for pr in snapshots))
    if blocker == "important":
        publisher.respond("api graphql", threads_json(fixtures.thread(fixtures.marker("important") + "\nFinding",
                                                                         author="trusted-publisher")))
    statuses = check_source(publisher, ["pending", status], exit_code=int(status == "failure"))
    assert all(s["context"] == "Loop-zero Eligibility" for s in statuses)
    assert delays == {"unknown": [2, 4, 8], "settles": [2]}.get(blocker, [])
    if status == "success":
        assert sum(c["argv"][1] != "graphql" for c in publisher.calls) <= 5 + int(blocker == "settles")
    if status == "pending":
        expected = github.MERGEABILITY_PENDING if blocker == "unknown" else "Awaiting trusted review of current source head"
        assert statuses[-1]["description"] == expected


@pytest.mark.parametrize("phase,expected", [("evaluate", "failure"), ("publish", "success")])
def test_sigterm_does_not_strand_or_overwrite_eligibility(publisher, phase, expected):
    script = """
import signal
import sys
from loopzero import github, hosted

def evaluate(*_, **_kwargs):
    if sys.argv[1] == "evaluate":
        signal.raise_signal(signal.SIGTERM)
    return github.Readiness(True, ())

post = github.commit_status
def publish(*args, **kwargs):
    post(*args, **kwargs)
    if sys.argv[1] == "publish" and args[3] == "success":
        signal.raise_signal(signal.SIGTERM)

hosted.eligibility.evaluate = evaluate
github.commit_status = publish
raise SystemExit(hosted.main())
"""
    def statuses():
        return [json.loads(c["--input"])["state"] for c in publisher.calls if "--input" in c]

    stopped = subprocess.run([sys.executable, "-c", script, phase], timeout=10)
    assert stopped.returncode == -signal.SIGTERM
    assert statuses() == ["pending", expected]

    subprocess.run([sys.executable, "-c", script, "retry"], timeout=10, check=True)
    assert statuses() == ["pending", expected, "pending", "success"]


@pytest.mark.parametrize("candidate", [False, True])
@pytest.mark.parametrize("change", ["exact", "ordinary", "clean merge"])
def test_hosted_review_coverage_after_source_head_changes(
    publisher, git_repo, monkeypatch, candidate, change,
):
    git(git_repo, "commit", "-qm", "base advance", "--allow-empty")
    base = git(git_repo, "rev-parse", "HEAD").strip()
    git(git_repo, "switch", "-qc", "source", "HEAD^")
    git(git_repo, "commit", "-qm", "reviewed source", "--allow-empty")
    reviewed = git(git_repo, "rev-parse", "HEAD").strip()
    if change == "ordinary":
        git(git_repo, "commit", "-qm", "unreviewed source", "--allow-empty")
    elif change == "clean merge":
        git(git_repo, "merge", "-q", "--no-edit", "main")
    head = git(git_repo, "rev-parse", "HEAD").strip()
    source = fixtures.rest_pr_json(pr_json(headRefOid=head))
    source["base"]["sha"] = base
    publisher.respond(f"api repos/{REPO}/pulls/7", source)
    verdict = "loopzero primary review by claude: **approve** (0 critical, 0 important, 0 suggestion)"
    publisher.respond(f"api repos/{REPO}/pulls/7/reviews?per_page=100&page=1",
                      [approval(reviewed, state="COMMENTED", verdict=verdict)])
    publisher.respond(f"api repos/{REPO}/compare/{reviewed}...{head}", {"status": "ahead"})
    monkeypatch.setattr(hosted.candidate_mod, "fetched_git",
                        lambda *_: nullcontext(partial(git, git_repo)))

    target = head
    if candidate:
        target = HEAD
        Path("event.json").write_text(json.dumps({
            "action": "opened", "pull_request": {"number": 9, "head": {"sha": target}},
        }))
        publisher.respond(f"api repos/{REPO}/pulls/9", fixtures.rest_pr_json(
            pr_json(number=9, headRefOid=target), branch="mergify/merge-queue/main",
        ))
        monkeypatch.setattr(hosted.candidate_mod, "attest_candidate",
                            lambda _event, *, source_policy, **_: source_policy(7, source))
    publisher.respond(f"api repos/{REPO}/statuses/{target}", {})

    ordinary = change == "ordinary"
    state = ("failure" if candidate else "pending") if ordinary else "success"
    check_source(publisher, ["pending", state], error=hosted.LoopZeroError if state == "failure" else None,
                 match="no review recorded")


def test_merged_source_refresh_publishes_nothing(publisher):
    body = json.dumps({"head": {"sha": HEAD, "ref": "lz/task"}, "user": {"login": "author"}, "merged": True,
                       "title": "before\u2028after"}, ensure_ascii=False)
    publisher.respond(f"api repos/{REPO}/pulls/7", f"HTTP/2.0 200 OK\r\nX-GitHub-Request-Id: GHREQ-456\r\n\r\n{body}")
    check_source(publisher, [])
    assert publisher.argv(0)[-1] == "--include"


@pytest.mark.parametrize("state,readback", [
    ("success", "match"), ("failure", "match"), ("success", "absent"),
    ("success", "publisher"), ("success", "description"), ("success", "state"),
    ("success", "unreadable"),
])
def test_final_status_lost_reply(publisher, monkeypatch, state, readback):
    ready = state == "success"
    monkeypatch.setattr(hosted.eligibility, "evaluate",
                        lambda *_, **__: github.Readiness(ready, () if ready else ("Blocking finding",)))
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
    check_source(publisher, ["pending", state], exit_code=int(not ready),
                 error=hosted.LoopZeroError if readback != "match" else None,
                 match="confirm eligibility status publication")


@pytest.mark.parametrize("error,states", [
    ("before", []), ("config", []), ("head", ["pending", "failure"]),
    ("metadata", ["pending", "failure"]), ("merged", ["pending"]),
    ("api", ["pending"]), ("recover", ["pending", "success"]),
    ("publication", ["pending", "pending"]), ("pages", ["pending", "failure"]),
])
def test_source_errors_cannot_become_successful_waits(publisher, monkeypatch, capsys, error, states):
    sleeps = []
    monkeypatch.setattr(github, "_retry_sleep", sleeps.append)
    pr = f"api repos/{REPO}/pulls/7"
    reviews = f"{pr}/reviews?per_page={github.PAGE}"
    if error in {"api", "recover", "before"}:
        fault = {"stdout": "HTTP/2.0 503 Service Unavailable\r\nX-GitHub-Request-Id: GHREQ-8774\r\n\r\n"
                           '{"message":"Service unavailable github_pat_1234567890ABCDEFGHIJKLMNOPQRSTUVWXYZ"}', "exit": 1}
        endpoint = pr if error == "before" else f"{reviews}&page=1"
        publisher.respond(endpoint, fault, [approval()] if error == "recover" else fault)
        check_source(publisher, states, exit_code=int(error == "before"))
        assert sleeps == [5]
        assert ("status=503 request_id=GHREQ-8774 message=Service unavailable [REDACTED]" in capsys.readouterr().err) == (error != "recover")
        return

    exception, message = hosted.LoopZeroError, None
    if error in {"head", "metadata", "merged"}:
        changes = {"head": {"headRefOid": "b" * 40}, "metadata": {"labels": [{"name": "hold"}]},
                   "merged": {"state": "MERGED"}}
        publisher.respond_pr_view(pr_json(**changes[error]))
        publisher.respond(f"{reviews}&page=1", [approval()])
    elif error == "publication":
        message = "invalid description"
        publisher.respond(f"api repos/{REPO}/statuses/{HEAD}", {}, {"stdout": "", "exit": 1, "stderr": f"{message} (HTTP 422)"})
        publisher.respond(f"api repos/{REPO}/commits/{HEAD}/statuses", [{"context": "other"}])
    elif error == "pages":
        page = [{"id": i, "state": "COMMENTED"} for i in range(github.PAGE)]
        for number in (1, 2):
            publisher.respond(f"{reviews}&page={number}", page)
        exception, message = github.GhError, "GitHub repeated a full page"
    else:
        monkeypatch.setenv("GITHUB_REPOSITORY", "other/repo")
    check_source(publisher, states, error=None if error == "merged" else exception, match=message)
    if error == "merged":
        assert not sleeps and "status=503" not in capsys.readouterr().err


@pytest.mark.parametrize("case,error,message", [
    ("valid", None, ""), ("trunk", None, ""), ("policy", hosted.LoopZeroError, "no review recorded"),
    ("hold", hosted.LoopZeroError, "source #101: PR #101 has a merge-authority hold"),
    ("race", CandidateRaceError, "metadata changed"),
    ("malformed", CandidateError, "invalid payload"),
    ("head", hosted.LoopZeroError, "head changed"), ("publisher", CandidateError, "not open"),
    ("interrupt", KeyboardInterrupt, ""), ("api", None, ""), ("mergify", None, ""),
    ("mergify_auth", hosted.mergify.MergifyError, "HTTP 401"),
    ("manual", hosted.LoopZeroError, "pull_request_target creation"), ("metadata", None, ""),
    ("stale", hosted.LoopZeroError, "head is no longer current"),
    ("untrusted", CandidateError, "head event was not sent"),
])
def test_candidate_publication_and_event_controls(tmp_path, fake_bin, monkeypatch, case, error, message):
    event = candidate_event()
    event["action"], event["sender"]["id"] = (
        "edited" if case == "metadata" else event["action"], 1 if case == "untrusted" else event["sender"]["id"])
    branch = "trunk" if case == "trunk" else "main"
    values = candidate_payloads(event)
    if case in {"valid", "trunk", "hold"}:
        gh = FakeGh(fake_bin, tmp_path)
        gh.respond("api graphql", threads_json())
    for number, sha in [(101, "1" * 40), (102, "2" * 40), (900, "c" * 40)]:
        path = f"/repos/owner/repo/pulls/{number}"
        values[path] = fixtures.rest_pr_json(pr_json(
            number=number, headRefOid=sha, isDraft=False,
            labels=[{"name": "hold"}] if case == "hold" and number == 101 else [],
        )) | values[path]
        values[path]["base"]["ref"] = branch
        values[f"{path}/reviews?per_page=100&page=1"] = [approval(sha)]
    if case == "stale":
        values["/repos/owner/repo/pulls/900"]["head"]["sha"] = "d" * 40
    if case == "malformed":
        values["/repos/owner/repo/pulls/101"] = []
    prepare_event(tmp_path, monkeypatch, "owner/repo", {"inputs": {"pr": "900"}} if case == "manual" else event, base=branch)
    rest_calls, sleeps = [], []

    def api_get(path):
        rest_calls.append(path)
        if case == "race" and path == "repos/owner/repo/pulls/101":
            values["/" + path]["labels"].append({"name": "changed"})
        return event["repository"] if path == "repos/owner/repo" else values["/" + path]
    monkeypatch.setattr(github, "api_get", api_get)
    monkeypatch.setattr("loopzero.candidate._retry_sleep", sleeps.append)
    published = []
    def publish(*args):
        published.append(args)
        if args[3] == "pending" and case == "publisher":
            values["/repos/owner/repo/pulls/101"]["state"] = "closed"
        if args[3] == "failure" and case == "publisher":
            raise RuntimeError("publication details must not replace attestation error")
    monkeypatch.setattr(github, "commit_status", publish)
    evaluated, real_evaluate = [], hosted.eligibility.evaluate

    def evaluate(repo, number, head, base, publishers, *, snapshot):
        evaluated.append((repo, number, head, base, publishers))
        if case in {"valid", "trunk", "hold"}:
            return real_evaluate(repo, number, head, base, publishers, snapshot=snapshot)
        if fault := {"interrupt": KeyboardInterrupt(), "api": github.GhUnavailable(("gh",), "HTTP 503")}.get(case):
            raise fault
        reasons = (github.MISSING_REVIEW_REASON,) if case == "policy" else ()
        return github.Readiness(not reasons, reasons)
    if case not in {"valid", "trunk", "hold"}:
        monkeypatch.setattr(github, "pr_view", lambda *_: SimpleNamespace(head_sha=("d" if case == "head" else "c") * 40))
    monkeypatch.setattr(hosted.eligibility, "evaluate", evaluate)
    http_codes = {"mergify": 503, "mergify_auth": 401}
    if case in http_codes:
        def fail_request(request):
            raise urllib.error.HTTPError(request.full_url, http_codes[case], "", {}, None)

        monkeypatch.setenv("MERGIFY_API_KEY", "synthetic-key")
        monkeypatch.setattr(hosted.mergify, "_open", fail_request)
        monkeypatch.setattr(hosted.mergify, "_retry_sleep", lambda _: None)
    else:
        monkeypatch.setattr(hosted.mergify, "api_get", lambda repo, suffix: candidate_status()
                            if (repo, suffix) == ("owner/repo", f"/merge-queue/status?branch={branch}") else None)
    with pytest.raises(error, match=message or None) if error else nullcontext():
        assert hosted.main() == 0
    states = {"valid": ["pending", "success"], "trunk": ["pending", "success"], "interrupt": ["pending"], "api": ["pending"],
              "mergify": ["pending"], "manual": [], "metadata": [], "stale": []}
    assert [call[3] for call in published] == states.get(case, ["pending", "failure"])
    assert all(call[:3] == ("owner/repo", "c" * 40, "Loop-zero Eligibility") for call in published)
    if case == "race":
        assert rest_calls.count("apps/mergify") == 3 and sleeps == [2, 2] and len(evaluated) == 6
    if case == "valid":
        assert len(rest_calls) + len(published) <= 16
        assert evaluated == [("owner/repo", n, str(n - 100) * 40, "main", ("trusted-publisher",))
                             for n in (101, 102)]
        assert [call[-1] for call in published] == [
            "Refreshing live source review evidence", "All attested candidate sources are reviewed"]
    elif case not in states:
        assert published[-1][-1] == "Eligibility refresh failed; inspect workflow logs"


def test_privileged_workflow_runs_only_the_trusted_default_branch_package():
    workflow = (Path(__file__).parents[1] / ".github/workflows/eligibility.yml").read_text()
    assert "pull_request_target:" in workflow
    events = next(line for line in workflow.splitlines() if line.strip().startswith("types:"))
    assert "labeled" in events and "unlabeled" in events
    source, candidate = workflow.split("\n  source-eligibility:\n", 1)[1].split(
        "\n  candidate-eligibility:\n", 1
    )
    secret = "${{ secrets.MERGIFY_API_KEY }}"
    assert "    environment:" not in source and secret not in source
    assert "github.event.label.name == 'hold'" in source
    assert "environment:\n      name: mergify-metadata\n      deployment: false" in candidate
    assert f"MERGIFY_API_KEY: {secret}" in candidate
    for job in (source, candidate):
        assert "ref: ${{ github.event.repository.default_branch }}" in job
        assert "ref: ${{ github.event.pull_request.head" not in job
        assert "persist-credentials: false" in job
        assert "permissions:\n      contents: read\n      pull-requests: read\n      statuses: write" in job
        assert "PYTHONPATH: src" in job
        assert "python -m loopzero.hosted" in job
    ruleset = json.loads((Path(__file__).parents[1] / ".github/rulesets/main.json").read_text())
    status_rule = next(rule for rule in ruleset["rules"] if rule["type"] == "required_status_checks")
    assert [check["context"] for check in status_rule["parameters"]["required_status_checks"]] == [
        "checks", "Loop-zero Eligibility",
    ]


@pytest.mark.parametrize("prior", [998, 999, 1000])
def test_source_status_limit(publisher, capsys, prior):
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

    publisher.respond(f"api repos/{REPO}/pulls/7/reviews?per_page=100&page=1", [approval()])
    if prior == 998:
        check_source(publisher, ["pending", "success"])
        assert not any("/commits/" in " ".join(call["argv"]) for call in publisher.calls)
        return

    message = rf"{HEAD}.*{context}.*1,000.*fresh head.*review"
    expected = ["pending", "success"] if prior == 999 else ["pending"]
    for states in (expected, [*expected, "pending"]):
        check_source(publisher, states, error=github.GhError, match=message)
    assert capsys.readouterr().out == ""
