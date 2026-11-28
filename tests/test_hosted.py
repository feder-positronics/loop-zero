"""Trusted publisher behavior against GitHub metadata, without candidate execution."""
import json
import re
import signal
import subprocess
import sys
import urllib.error
from contextlib import nullcontext
from functools import partial
from pathlib import Path
from types import SimpleNamespace

import pytest

from loopzero import eligibility, github, hosted
from loopzero.candidate import CandidateError, CandidateRaceError
from tests import test_github as fixtures
from tests.conftest import git, review_summary
from tests.test_candidate import event as candidate_event
from tests.test_candidate import payloads as candidate_payloads
from tests.test_candidate import status as candidate_status
from tests.test_github import HEAD, REPO, gh as gh, pr_json, threads_json


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
def publisher(tmp_path, gh, monkeypatch):
    prepare_event(tmp_path, monkeypatch, REPO, {"pull_request": {"number": 7}})
    gh.respond_pr_view(pr_json(), branch="lz/task")
    gh.respond("api graphql", threads_json())
    gh.respond(f"api repos/{REPO}/statuses/{HEAD}", {"id": 11})
    gh.respond(f"api repos/{REPO}/pulls/7/reviews --method GET", [])
    gh.respond(f"api repos/{REPO}/commits/{HEAD}/statuses", [])
    return gh


def approval(head=HEAD, *, state="APPROVED"):
    return {"state": state, "commit_id": head, "user": {"login": "trusted-publisher"},
            "body": f"<!-- loopzero:review v=1 head={head} kind=primary source=model -->\n{review_summary(head)}"}


@pytest.mark.parametrize("case,states", [
    ("manual", ["pending", "success"]), ("manual retry", ["success"]), ("manual first retry", ["failure"]), ("restore", ["pending", "failure"]),
    ("newer success", ["pending"]), ("newer pending", ["pending"]), ("newer same marker", ["pending"]), ("write lost", ["pending", "failure"]),
    ("stale marker", ["pending", "failure"]), ("many markers", ["pending", "failure"]), ("other publisher marker", ["pending", "failure"]),
    ("case-insensitive newer", ["pending"]), ("pending verdict like marker", ["pending", "pending"]), ("verdict worded like marker", ["pending", "failure"]),
])
def test_source_landing_mid_refresh_keeps_settled_verdict(publisher, case, states):
    ctx = {"context": "Loop-zero Eligibility"}
    old = ctx | {"id": 1, "state": "failure", "description": "Blocking finding"}
    marker = ctx | {"id": 11, "state": "pending", "description": "Refreshing live source review evidence"}
    if case.startswith("manual"):
        publisher.respond(f"api repos/{REPO}/pulls/7", fixtures.rest_pr_json(pr_json(state="MERGED" if case.endswith("retry") else "OPEN")))
        marker["description"] = "Refreshing source review evidence"
        old.update(state="success", description="Previously eligible")
    history = {"restore": [marker, old], "manual": [marker, old], "manual retry": [marker, old], "manual first retry": [marker],
               "newer success": [old | {"id": 12, "state": "success"}, marker, old],
               "newer pending": [marker | {"id": 12, "description": "Awaiting trusted review"}, marker, old],
               "newer same marker": [marker | {"id": 12}, marker, old],
               "write lost": [marker, old], "stale marker": [marker, marker | {"id": 10}, old],
               "other publisher marker": [marker, marker | {"id": 9, "description": "Refreshing source review evidence"}, old],
               "pending verdict like marker": [marker, marker | {"id": 9, "description": "Refreshing sources"}, old],
               "case-insensitive newer": [old | {"id": 12, "state": "success", "context": "loop-zero eligibility"}, marker, old],
               "many markers": [marker] + [marker | {"id": i + 2} for i in range(25)] + [old],
               "verdict worded like marker": [marker, old | {"description": "Refreshing live source (verdict)"}]}[case]
    publisher.respond(f"api repos/{REPO}/commits/{HEAD}/statuses", history)
    publisher.respond(f"api repos/{REPO}/pulls/7/reviews --method GET", [approval()])
    publisher.respond_pr_view(*([pr_json()] if case == "manual" else []), pr_json(state="MERGED"))
    if case == "write lost":
        publisher.respond(f"api repos/{REPO}/statuses/{HEAD}", {"id": 11},
                          {"stdout": "", "exit": 1, "stderr": "HTTP 422"})
        check_source(publisher, states, error=hosted.LoopZeroError)
    else:
        posted = check_source(publisher, states, manual=case.startswith("manual"))
        if len(posted) == 2 or case == "manual retry":
            markers = {"Refreshing live source review evidence", "Refreshing source review evidence"}
            assert posted[-1]["description"] == next(r["description"] for r in history if r["description"] not in markers)


def check_source(publisher, states, *, exit_code=0, error=None, match=None, manual=False):
    with pytest.raises(error, match=match) if error else nullcontext():
        assert (eligibility.main(["--config", "workflow.toml", "--pr", "7", "--head", HEAD, "--publish"]) if manual else hosted.main()) == exit_code
    statuses = [json.loads(call["--input"]) for call in publisher.calls if "--input" in call]
    assert [status["state"] for status in statuses] == states
    return statuses


@pytest.mark.parametrize("case,blocker,status", [
    ("COMMENTED", "", "success"), ("APPROVED", "", "success"), ("absent", "", "pending"),
    ("untrusted", "", "pending"), ("stale", "", "pending"), ("malformed", "", "pending"),
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
    publisher.respond(f"api repos/{REPO}/pulls/7/reviews --method GET", reviews)
    delays = []
    monkeypatch.setattr(hosted.eligibility, "sleep", delays.append)
    if blocker in {"conflict", "unknown", "settles"}:
        initial = pr_json(mergeable="CONFLICTING" if blocker == "conflict" else "UNKNOWN")
        snapshots = [initial, pr_json()] if blocker == "settles" else [initial]
        publisher.respond(f"api repos/{REPO}/pulls/7", *(fixtures.rest_pr_json(pr) for pr in snapshots))
    if blocker == "important":
        publisher.respond("api graphql", threads_json(fixtures.thread(fixtures.marker("important") + "\nFinding",
                                                                         author="trusted-publisher")))
    statuses = check_source(publisher, ["pending", status])
    assert all(s["context"] == "Loop-zero Eligibility" for s in statuses)
    assert delays == {"unknown": [2, 4, 8], "settles": [2]}.get(blocker, [])
    if status == "success":
        assert sum(c["argv"][1] != "graphql" for c in publisher.calls) <= 5 + int(blocker == "settles")
    if blocker == "conflict":
        assert github.MISSING_REVIEW_REASON in statuses[-1]["description"]
    elif status == "pending":
        assert statuses[-1]["description"] == (github.MERGEABILITY_PENDING if blocker == "unknown"
                                               else "Awaiting trusted review of current source head")


@pytest.mark.parametrize("change,reason", [
    ({"headRefOid": "b" * 40}, "source changed during eligibility refresh"),
    ({"mergeable": "UNKNOWN"}, github.MERGEABILITY_PENDING),
])
def test_live_eligibility_rejects_changes_during_refresh(publisher, change, reason):
    publisher.respond(f"api repos/{REPO}/pulls/7", fixtures.rest_pr_json(pr_json()),
                      fixtures.rest_pr_json(pr_json(**change)))
    publisher.respond(f"api repos/{REPO}/pulls/7/reviews --method GET", [approval()])
    readiness = hosted.eligibility.evaluate(REPO, 7, HEAD, "main", ("trusted-publisher",))
    assert not readiness.ready
    assert readiness.reasons == (reason,)


@pytest.mark.parametrize("phase,expected", [("evaluate", "failure"), ("publish", "success")])
def test_sigterm_does_not_strand_or_overwrite_eligibility(publisher, phase, expected):
    script = """
import signal
import sys
from loopzero import eligibility, github, hosted

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
    for mode, exit_code, states in [
        (phase, -signal.SIGTERM, ["pending", expected]),
        ("retry", 0, ["pending", expected, "pending", "success"]),
    ]:
        assert subprocess.run([sys.executable, "-c", script, mode], timeout=10, check=False).returncode == exit_code
        assert [json.loads(c["--input"])["state"] for c in publisher.calls if "--input" in c] == states


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
    publisher.respond(f"api repos/{REPO}/pulls/7/reviews --method GET",
                      [approval(reviewed, state="COMMENTED")])
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
    statuses = check_source(publisher, ["pending", state])
    if state == "failure":
        assert "source #7: no review recorded" in statuses[-1]["description"]


@pytest.mark.parametrize("unfinished_refresh", [False, True])
def test_landed_source_refresh_preserves_settled_verdict(publisher, unfinished_refresh):
    body = json.dumps({
        "head": {"sha": HEAD, "ref": "lz/task"}, "user": {"login": "author"},
        "merged": True, "title": "before\u2028after",
    }, ensure_ascii=False)
    publisher.respond(f"api repos/{REPO}/pulls/7", f"HTTP/2.0 200 OK\r\nX-GitHub-Request-Id: GHREQ-456\r\n\r\n{body}")
    if unfinished_refresh:
        context = {"context": "Loop-zero Eligibility"}
        prior = context | {"id": 1, "state": "failure", "description": "Blocking finding"}
        refresh = context | {"id": 11, "state": "pending", "description": "Refreshing live source review evidence"}
        publisher.respond(f"api repos/{REPO}/commits/{HEAD}/statuses", [refresh, prior])
        publisher.respond(f"api repos/{REPO}/statuses/{HEAD}", {"id": 12})
        posted = check_source(publisher, ["failure"])
        assert posted[0]["description"] == prior["description"]
    else:
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
    check_source(publisher, ["pending", state],
                 error=hosted.LoopZeroError if readback not in {"match", "publisher"} else None,
                 match="confirm eligibility status publication")


@pytest.mark.parametrize("error,states", [
    ("before", []), ("config", []), ("no-publishers", []), ("head", ["pending", "failure"]),
    ("metadata", ["pending", "failure"]), ("merged", ["pending"]),
    ("api", ["pending"]), ("recover", ["pending", "success"]),
    ("publication", ["pending", "pending"]), ("pages", ["pending", "failure"]),
])
def test_source_errors_cannot_become_successful_waits(publisher, monkeypatch, capsys, error, states):
    sleeps = []
    monkeypatch.setattr(github, "_retry_sleep", sleeps.append)
    pr = f"api repos/{REPO}/pulls/7"
    reviews = f"{pr}/reviews"
    if error in {"api", "recover", "before"}:
        fault = {"stdout": "HTTP/2.0 503 Service Unavailable\r\nX-GitHub-Request-Id: GHREQ-8774\r\n\r\n"
                           '{"message":"Service unavailable github_pat_1234567890ABCDEFGHIJKLMNOPQRSTUVWXYZ"}', "exit": 1}
        endpoint = pr if error == "before" else f"{reviews} --method GET"
        publisher.respond(endpoint, fault, [approval()] if error == "recover" else fault)
        check_source(publisher, states, exit_code=int(error != "recover"))
        assert sleeps == [5]
        assert ("status=503 request_id=GHREQ-8774 message=Service unavailable [REDACTED]" in capsys.readouterr().err) == (error != "recover")
        return

    exception, message = hosted.LoopZeroError, None
    if error in {"head", "metadata", "merged"}:
        changes = {"head": {"headRefOid": "b" * 40}, "metadata": {"labels": [{"name": "hold"}]},
                   "merged": {"state": "MERGED"}}
        publisher.respond_pr_view(pr_json(**changes[error]))
        publisher.respond(f"{reviews} --method GET", [approval()])
    elif error == "publication":
        message = "invalid description"
        publisher.respond(f"api repos/{REPO}/statuses/{HEAD}", {}, {"stdout": "", "exit": 1, "stderr": f"{message} (HTTP 422)"})
        publisher.respond(f"api repos/{REPO}/commits/{HEAD}/statuses", [{"context": "other"}])
    elif error == "pages":
        page = [{"id": i, "state": "COMMENTED"} for i in range(github.PAGE)]
        for number in (1, 2):
            publisher.respond(f"{reviews} " + ("--method GET" if number == 1 else f"page={number}"), page)
        exception, message = github.GhError, "GitHub repeated a full page"
    elif error == "no-publishers":
        Path("workflow.toml").write_text(f'[repo]\nname="{REPO}"\n')
    else:
        monkeypatch.setenv("GITHUB_REPOSITORY", "other/repo")
    check_source(publisher, states, error=None if error == "merged" else exception, match=message)
    if error == "merged":
        assert not sleeps and "status=503" not in capsys.readouterr().err


@pytest.mark.parametrize("case,error,message", [
    ("valid", None, ""), ("trunk", None, ""), ("policy", None, "source #101: no review recorded"),
    ("settled_mergeability", None, ""), ("pending_mergeability", None, ""),
    ("poll_drift", CandidateRaceError, "changed during mergeability polling"),
    ("hold", None, "source #101: PR #101 has a merge-authority hold"),
    ("race", CandidateRaceError, "metadata changed"),
    ("malformed", CandidateError, "invalid payload"), ("head", hosted.LoopZeroError, "head changed"),
    ("publisher", CandidateError, "not open"), ("interrupt", KeyboardInterrupt, ""),
    ("api", None, ""), ("mergify", None, ""), ("mergify_auth", hosted.mergify.MergifyError, "HTTP 401"),
    ("manual", hosted.LoopZeroError, "pull_request_target creation"), ("metadata", None, ""),
    *((case, hosted.LoopZeroError, "head is no longer current") for case in ("stale", "head-shape")),
    ("untrusted", CandidateError, "head event was not sent")])
def test_candidate_publication_and_event_controls(tmp_path, fake_bin, monkeypatch, capsys, request, case, error, message):
    event = candidate_event()
    event["action"], event["sender"]["id"] = (
        "edited" if case == "metadata" else event["action"], 1 if case == "untrusted" else event["sender"]["id"])
    branch = "trunk" if case == "trunk" else "main"
    values = candidate_payloads(event)
    polling = case in {"settled_mergeability", "pending_mergeability", "poll_drift"}
    if polling or case in {"valid", "trunk", "hold"}:
        request.getfixturevalue("gh").respond("api graphql", threads_json())
    for number, sha in [(101, "1" * 40), (102, "2" * 40), (900, "c" * 40)]:
        path = f"/repos/owner/repo/pulls/{number}"
        values[path] = fixtures.rest_pr_json(pr_json(
            number=number, headRefOid=sha, isDraft=False,
            labels=[{"name": "hold"}] if case in {"hold", "poll_drift"} and number == 101 else [],
        )) | values[path]
        values[path]["base"]["ref"] = branch
        values[f"{path}/reviews"] = [approval(sha)]
    if polling:
        values["/repos/owner/repo/pulls/101"]["mergeable"] = None
    if case == "stale":
        values["/repos/owner/repo/pulls/900"]["head"]["sha"] = "d" * 40
    if case == "malformed":
        values["/repos/owner/repo/pulls/101"] = []
    if case == "head-shape":
        del event["pull_request"]["head"]
    prepare_event(tmp_path, monkeypatch, "owner/repo", {"inputs": {"pr": "900"}} if case == "manual" else event, base=branch)
    rest_calls, sleeps = [], []

    def api_get(path):
        rest_calls.append(path)
        if case == "race" and path == "repos/owner/repo/pulls/101":
            values["/" + path]["labels"].append({"name": "changed"})
        return event["repository"] if path == "repos/owner/repo" else values["/" + path]
    monkeypatch.setattr(github, "api_get", api_get)
    monkeypatch.setattr(github, "paged", api_get)
    now = [0.0]
    monkeypatch.setattr("loopzero.candidate._retry_clock", lambda: now[0])
    monkeypatch.setattr("loopzero.candidate._retry_sleep", lambda s: (sleeps.append(s), now.__setitem__(0, now[0] + s)))
    mergeability_sleeps = []
    monkeypatch.setattr(hosted.eligibility, "sleep",
                        lambda s: (mergeability_sleeps.append(s), now.__setitem__(0, now[0] + s)))
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
        if polling or case in {"valid", "trunk", "hold"}:
            return real_evaluate(repo, number, head, base, publishers, snapshot=snapshot)
        if fault := {"interrupt": KeyboardInterrupt(), "api": github.GhUnavailable(("gh",), "HTTP 503")}.get(case):
            raise fault
        reasons = (github.MISSING_REVIEW_REASON,) if case == "policy" else ()
        return github.Readiness(not reasons, reasons)
    if polling:
        pr_view = github.pr_view

        def poll(repo, number):
            if number == 101:
                source = values[f"/repos/{repo}/pulls/{number}"]
                if case == "poll_drift":
                    source = source | {"labels": []}
                return github.pr_from_rest(source | {"mergeable": None if case == "pending_mergeability" else True})
            return pr_view(repo, number)

        monkeypatch.setattr(github, "pr_view", poll)
    elif case not in {"valid", "trunk", "hold"}:
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
        assert hosted.main() == int(case in {"api", "mergify", "pending_mergeability"})
    states = {case: ["pending", "success"] for case in ("valid", "trunk", "settled_mergeability")}
    states.update({case: ["pending"] for case in ("interrupt", "api", "mergify", "pending_mergeability")})
    states.update({case: [] for case in ("manual", "metadata", "stale", "head-shape")})
    assert [call[3] for call in published] == states.get(case, ["pending", "failure"])
    if case in {"settled_mergeability", "pending_mergeability"}:
        assert mergeability_sleeps[:3] == ([2] if case == "settled_mergeability" else [2, 4, 8])
        assert (set(sleeps) == {5}) == (case == "pending_mergeability")  # a persistent outage retries for the 90 s window
    assert all(call[:3] == ("owner/repo", "c" * 40, "Loop-zero Eligibility") for call in published)
    if case == "race":
        assert rest_calls.count("apps/mergify") == 8 and sleeps == [4] * 7 and len(evaluated) == 16
    if case == "valid":
        assert len(rest_calls) + len(published) <= 16
        assert evaluated == [("owner/repo", n, str(n - 100) * 40, "main", ("trusted-publisher",))
                             for n in (101, 102)]
        assert [call[-1] for call in published] == [
            "Refreshing live source review evidence", "All attested candidate sources are reviewed"]
    elif case in {"hold", "policy"}:
        assert message in published[-1][-1]
        assert message in capsys.readouterr().out
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
    assert re.search(r"LOOPZERO_PUBLISHER: .*github.repository == 'feder-positronics/loop-zero' && '\.' \|\| "
                     r"'git\+https://github.com/feder-positronics/loop-zero@[0-9a-f]{40}'", workflow)
    for job in (source, candidate):
        assert "ref: ${{ github.event.repository.default_branch }}" in job
        assert "ref: ${{ github.event.pull_request.head" not in job
        assert "persist-credentials: false" in job
        assert "permissions:\n      contents: read\n      pull-requests: read\n      statuses: write" in job
        assert "PYTHONPATH" not in job and "uv run --no-project" in job and '--with "$LOOPZERO_PUBLISHER"' in job
        assert "python -P -m loopzero.hosted" in job
    ruleset = json.loads((Path(__file__).parents[1] / ".github/rulesets/main.json").read_text())
    status_rule = next(rule for rule in ruleset["rules"] if rule["type"] == "required_status_checks")
    assert [check["context"] for check in status_rule["parameters"]["required_status_checks"]] == [
        "checks", "Loop-zero Eligibility",
    ]


@pytest.mark.parametrize("prior", [998, 999, 1000])
def test_source_status_limit(publisher, capsys, monkeypatch, prior):
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

    publisher.respond(f"api repos/{REPO}/pulls/7/reviews --method GET", [approval()])
    if prior == 998:
        check_source(publisher, ["pending", "success"])
        assert not any("/commits/" in " ".join(call["argv"]) for call in publisher.calls)
        return

    message = rf"{HEAD}.*{context}.*1,000.*fresh head.*review"
    expected = ["pending", "success"] if prior == 999 else ["pending"]
    for states in (expected, [*expected, "pending"]):
        check_source(publisher, states, error=github.GhError, match=message)
    assert capsys.readouterr().out == ""


def test_source_status_flattens_reasons_and_keeps_later_ones_visible():
    result = github.Readiness(False, ("src/a\n::notice title=x::forged" + "z" * 200, "has merge conflicts"))
    state, message = hosted.eligibility.source_status(result)
    assert state == "failure" and "\n" not in message and message.endswith("; has merge conflicts")
