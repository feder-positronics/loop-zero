"""Mergify candidate attestation at the trusted hosted boundary."""

from __future__ import annotations

import base64
from contextlib import nullcontext
import subprocess
from copy import deepcopy
from functools import partial
from itertools import cycle
from os import environ
from shutil import copytree
from unittest.mock import Mock, call

import pytest

from loopzero import candidate, eligibility, github
from loopzero.candidate import CandidateError, CandidateRaceError, attest_candidate
from tests.conftest import commit_file, git as isolated_git


@pytest.fixture(autouse=True)
def no_retry_delay(monkeypatch):
    clock = [0.0]
    monkeypatch.setattr("loopzero.candidate._retry_clock", lambda: clock[0])
    monkeypatch.setattr("loopzero.candidate._retry_sleep", lambda delay: clock.__setitem__(0, clock[0] + delay))
    return clock


def event(head: str = "c" * 40) -> dict:
    repo = {"id": 7, "full_name": "owner/repo"}
    bot = dict(id=37929162, login="mergify[bot]", type="Bot", html_url="https://github.com/apps/mergify")
    return {
        "action": "synchronize", "repository": repo, "sender": deepcopy(bot),
        "pull_request": {
            "number": 900, "state": "open", "draft": True,
            "user": bot,
            "head": {"sha": head, "ref": "mergify/merge-queue/batch", "repo": deepcopy(repo)},
            "base": {"sha": "b" * 40, "ref": "main", "repo": deepcopy(repo)},
        },
    }


def batch(batch_id, *, parents, number, sources, children):
    return {
        "id": batch_id, "parent_ids": parents, "queue_pull_request_number": number,
        "pull_requests": [{"number": source} for source in sources], "sub_batches": children,
    }


def status() -> dict:
    parent = batch("parent", parents=[], number=899, sources=[101], children=None)
    candidate = batch("candidate", parents=["parent"], number=900, sources=[102], children=[])
    return {"batches": [parent, batch("outer", parents=[], number=None, sources=[], children=[candidate])]}


def pull(number: int, sha: str) -> dict:
    return {
        "number": number, "state": "open", "draft": False,
        "body": f"source {number}", "head": {"sha": sha},
        "base": {"ref": "main", "repo": {"id": 7, "full_name": "owner/repo"}},
    }


def expected_sources(*sources):
    return [{"number": number, "head_sha": sha} for number, sha in sources]


def tree_commit(git, tree, *parents, subject):
    parent_args = [arg for parent in parents for arg in ("-p", parent)]
    return git("commit-tree", f"{tree}^{{tree}}", *parent_args, "-m", subject)


def pull_path(number):
    return f"/repos/owner/repo/pulls/{number}"


def source_payloads(values, head, *sources):
    for number, sha in sources:
        values[pull_path(number)] = pull(number, sha)
        values[f"/repos/owner/repo/compare/{sha}...{head}"] = dict(status="ahead", merge_base_commit={"sha": sha})


def payloads(candidate_event: dict, *sources) -> dict[str, object]:
    candidate = deepcopy(candidate_event["pull_request"]) | {"number": 900}
    values = {
        "/apps/mergify": {"id": 10562, "slug": "mergify", "owner": {"login": "Mergifyio"}},
        "/users/mergify%5Bbot%5D": deepcopy(candidate["user"]),
        pull_path(900): candidate,
    }
    source_payloads(values, candidate["head"]["sha"], *(sources or ((101, "1" * 40), (102, "2" * 40))))
    return values


def run_attestation(
    candidate_event: dict | None = None, *,
    status_reads: list[dict] | None = None, github_payloads: dict[str, object] | None = None,
    github_get=None, mergify_status=None, source_policy=lambda _number, _source: None, run=attest_candidate,
):
    candidate_event = candidate_event or event()
    reads = cycle(status_reads or [status()])
    values = github_payloads or payloads(candidate_event)
    return run(
        candidate_event,
        github_get=github_get or (lambda path: deepcopy(values[path])),
        mergify_status=mergify_status or (lambda *_: deepcopy(next(reads))),
        source_policy=source_policy,
    )


def test_attests_nested_candidate_and_transitive_parent_sources() -> None:
    seen: list[int] = []
    sources = run_attestation(source_policy=lambda number, _: seen.append(number))
    assert sources == expected_sources((101, "1" * 40), (102, "2" * 40))
    assert seen == [101, 102]


@pytest.mark.parametrize("location,replacement,diagnostic", [
    (("event", "pull_request", "user", "id"), 1, "Mergify GitHub App"),
    ((pull_path(900), "user", "id"), 1, "Mergify GitHub App"),
    (("event", "sender", "id"), 1, "head event was not sent"),
    (("event", "action"), "edited", "creation or head-update event"),
    (("event", "pull_request", "head", "ref"), "feature/forged", "unexpected head ref"),
    (("status", 0), None, "status is not an object"),
    ((f"/repos/owner/repo/compare/{'2' * 40}...{'c' * 40}", "status"), "diverged", "not an ancestor"),
    ((pull_path(900), "draft"), False, "must be a draft"),
    ((pull_path(900), "base", "ref"), "release", "must target the real main branch"),
    ((pull_path(900), "head", "repo", "id"), 8, "head repository does not match"),
    ((pull_path(900), "base", "repo", "id"), 8, "base repository does not match"),
    ((pull_path(102), "base", "ref"), "release", "source PR #102 does not target this repository"),
    (("closed",), None, None), (("tree",), None, None),
    *((("policy",), value, None) for value in (
        pull(101, "1" * 40), {"merged": True, "head": {"sha": "9" * 40}}, OSError("lookup failed"),
        [], {"merged": True, "head": None}, {"merged": 1, "head": {"sha": "1" * 40}},
    )),
], ids=["author", "live-author", "sender", "action", "ref", "status", "ancestry",
        "candidate-ready", "candidate-retargeted", "head-fork", "base-fork", "source-retargeted", "closed-source", "tree",
        "open", "wrong-head", "lookup-error", "bad-shape", "missing-head", "nonbool-merged"])
def test_nonrace_failure_does_not_retry(monkeypatch, location, replacement, diagnostic):
    incoming, live = event(), [status()]
    values = payloads(incoming)
    failure = location[0]
    policy_error = CandidateError("source #101: missing required review")
    if failure == "closed":
        values[pull_path(101)].update(state="closed", merged=False)
    elif failure == "tree":
        live[0]["batches"].pop(0)
        values["/repos/owner/repo/git/ref/heads/main"] = {"object": {"sha": "b" * 40}}
        monkeypatch.setattr(candidate, "_verify_tree", Mock(side_effect=CandidateError("candidate tree mismatch")))
    elif diagnostic is not None:
        target = values | {"event": incoming, "status": live}
        for key in location[:-1]:
            target = target[key]
        target[location[-1]] = replacement

    def read(path):
        if failure == "policy" and path == pull_path(101) and get.call_args_list.count(call(path)) == 2:
            if isinstance(replacement, Exception):
                raise replacement
            return deepcopy(replacement)
        return deepcopy(values[path])

    get, sleep = Mock(side_effect=read), Mock()
    policy = Mock(side_effect=policy_error if failure == "policy" else None)
    monkeypatch.setattr(candidate, "_retry_sleep", sleep)
    with pytest.raises(CandidateError, match=diagnostic) as caught:
        run_attestation(incoming, github_get=get, status_reads=live, source_policy=policy)
    assert not isinstance(caught.value, CandidateRaceError)
    sleep.assert_not_called()
    app_reads = get.call_args_list.count(call("/apps/mergify"))
    assert app_reads == 1 if diagnostic is None else app_reads <= 1
    if failure == "policy":
        assert caught.value is policy_error and get.call_args_list.count(call(pull_path(101))) == 2


def test_same_head_source_still_requires_policy():
    incoming = event()
    head = incoming["pull_request"]["head"]["sha"]
    values = payloads(incoming)
    values[pull_path(102)]["head"]["sha"] = head
    values[f"/repos/owner/repo/compare/{head}...{head}"] = dict(status="identical", merge_base_commit={"sha": head})

    def policy(number, _source):
        if number == 102:
            raise CandidateError("source policy blocked same-head source")

    with pytest.raises(CandidateError, match="source policy blocked same-head source"):
        run_attestation(incoming, github_payloads=values, source_policy=policy)


@pytest.fixture(scope="session")
def candidate_seed(tmp_path_factory):
    remote = tmp_path_factory.mktemp("candidate-seed")
    git = partial(isolated_git, remote)
    git("init", "-b", "main")
    commit_file(remote, "base", "base\n", "base")
    for branch, content in (("parent", "H1\n"), ("source", "reviewed\n")):
        git("checkout", "-b", branch, "main")
        commit_file(remote, branch, content, branch)
    git("checkout", "-b", "candidate", "main")
    git("merge", "--no-ff", "parent", "-m", "parent integration")
    git("merge", "--no-ff", "source", "-m", "source integration")
    git("checkout", "main")
    git("merge", "--squash", "parent")
    git("commit", "-m", "squashed parent")
    return remote, *git("rev-parse", "source^", "parent", "source", "candidate", "main").split()


@pytest.fixture
def tree_candidate(candidate_seed, tmp_path, monkeypatch):
    seed, base, parent, source, candidate_sha, main = candidate_seed
    remote = copytree(seed, tmp_path / "remote")

    def git(*args):
        return isolated_git(remote, *args).strip()

    def commit(name, content):
        commit_file(remote, name, content, name)
        return git("rev-parse", "HEAD")

    popen = subprocess.Popen
    observed_transport = set()

    def isolated_fetch(argv, **kwargs):
        if environ.get("HTTPS_PROXY") == "http://proxy.invalid:8080" and "git" in argv:
            env = kwargs["env"]
            transport = {"HTTPS_PROXY", "GIT_CONFIG_COUNT", "GIT_CONFIG_KEY_0", "GIT_CONFIG_VALUE_0"}
            if "fetch" in argv:
                observed_transport.add("fetch")
                assert env["HTTPS_PROXY"] == environ["HTTPS_PROXY"]
                assert (env["GIT_CONFIG_COUNT"], env["GIT_CONFIG_KEY_0"]) == ("1", "http.extraHeader")
                header = env["GIT_CONFIG_VALUE_0"].removeprefix("Authorization: Basic ")
                assert base64.b64decode(header) == b"x-access-token:secret-token"
            else:
                observed_transport.add("plumbing")
                assert not transport & env.keys()
        assert not any(arg.startswith("--as") for arg in argv)  # arm64 Git exceeds RLIMIT_AS
        if "fetch" in argv:
            assert "https://github.com/owner/repo.git" in argv
            assert "secret-token" not in " ".join(argv)
            assert kwargs["env"]["GIT_CONFIG_GLOBAL"] == "/dev/null"
            argv = [str(remote) if arg == "https://github.com/owner/repo.git" else arg for arg in argv]
        return popen(argv, **kwargs)

    monkeypatch.setenv("GH_TOKEN", "secret-token")
    monkeypatch.setattr(subprocess, "Popen", isolated_fetch)

    def attest(*, head=candidate_sha, main_head=main, policy=lambda _n, _s: None,
               main_after=None, extra_source=None, lineage="vanished", base_branch="main"):
        candidate_event = event(head)
        values = payloads(candidate_event, (101, extra_source or parent), (102, source))
        queue = status()
        candidate_batch = queue["batches"][1]["sub_batches"][0]
        if lineage != "stacked":
            queue["batches"].pop(0)
        if lineage == "single":
            candidate_batch["parent_ids"].clear()
        if extra_source:
            candidate_batch["pull_requests"].append({"number": 101})
        for number in (102, 900):
            values[pull_path(number)]["base"]["ref"] = base_branch
        reads = cycle([main_head, main_after or main_head])

        def get(path):
            if not path.endswith(f"/git/ref/heads/{base_branch}"):
                return deepcopy(values[path])
            return {"object": {"sha": sha}} if (sha := next(reads)) else {}

        sources = run_attestation(candidate_event, github_get=get, status_reads=[queue],
                                  source_policy=policy, run=partial(attest_candidate, base_branch=base_branch))
        if environ.get("HTTPS_PROXY") == "http://proxy.invalid:8080":
            assert observed_transport == {"fetch", "plumbing"}
        return sources

    return attest, git, commit, base, parent, source, candidate_sha


@pytest.mark.parametrize("base_branch", ["main", "trunk"])
def test_vanished_squash_parent_passes_real_tree_attestation(tree_candidate, base_branch, monkeypatch):
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.invalid:8080")
    attest, _, _, _, _, source, _ = tree_candidate
    seen = []
    assert attest(base_branch=base_branch, policy=lambda n, _: seen.append(n)) == expected_sources((102, source))
    assert seen == [102]


def test_isolated_proofs_deepen_for_an_older_merge_base(tree_candidate, monkeypatch):
    attest, git, _, _, _, source, _ = tree_candidate
    monkeypatch.setattr(candidate, "_TREE_DEPTH", 2)
    popen = Mock(wraps=subprocess.Popen)
    monkeypatch.setattr(subprocess, "Popen", popen)
    for index in range(5):
        git("commit", "--allow-empty", "-m", f"base advance {index}")
    main = git("rev-parse", "main")
    def verify_depth():
        depths = [arg for c in popen.call_args_list for arg in c.args[0] if arg.startswith(("--depth=", "--deepen="))]
        assert depths[0] == "--depth=2" and any(arg.startswith("--deepen=") for arg in depths)
        popen.reset_mock()

    assert attest(main_head=main) == expected_sources((102, source))
    verify_depth()
    git("checkout", "--detach", source)
    git("merge", "--no-ff", main, "-m", "merge current main")
    head = git("rev-parse", "HEAD")
    with candidate.fetched_git("owner/repo", [source, head, main]) as run:
        assert eligibility._verified_base_chain(run, source, head, main, clean=True, conflict_only=False)
    verify_depth()


def forged_tree(git, commit, original):
    changed = commit("injected", "unreviewed\n")
    parents = git("show", "-s", "--format=%P", original).split()
    return tree_commit(git, changed, *parents, subject="forged merge")


@pytest.mark.parametrize("mutation,diagnostic", [
    ("replaced", "tree mismatch"), ("withdrawn", "tree mismatch"), ("extra", "tree mismatch|objects unavailable"),
    ("tampered", "tree mismatch"), ("missing", "objects unavailable"),
    ("octopus", "ambiguous"), ("main conflict", "integration conflict"),
    ("main-moved", "main changed"), ("mismatch-and-main-moved", "main changed"),
    *((bound, "bound") for bound in ("_TREE_BYTES", "_TREE_OBJECTS", "_TREE_SECONDS", "_TREE_OUTPUT")),
])
def test_vanished_parent_rejects_invalid_or_unprovable_trees(tree_candidate, monkeypatch, mutation, diagnostic):
    attest, git, commit, base, parent, source, candidate_sha = tree_candidate
    kwargs, seen = {}, []
    if diagnostic == "main changed":
        kwargs.update(main_after=base, policy=lambda n, _: seen.append(n))
        if mutation == "mismatch-and-main-moved":
            monkeypatch.setattr(candidate, "_verify_tree", Mock(side_effect=CandidateError("candidate tree mismatch")))
    elif diagnostic == "bound":
        monkeypatch.setattr(candidate, mutation, int(mutation != "_TREE_SECONDS"))
    elif mutation in {"replaced", "main conflict"}:
        name, content = ("parent", "H2\n") if mutation == "replaced" else ("source", "conflicting main content\n")
        kwargs["main_head"] = commit(name, content)
    elif mutation in {"withdrawn", "missing"}:
        kwargs["main_head"] = base if mutation == "withdrawn" else "f" * 40
    elif mutation == "octopus":
        kwargs["head"] = tree_commit(git, candidate_sha, base, parent, source, subject="octopus")
    else:
        git("checkout", "candidate")
        kwargs["head"] = (forged_tree(git, commit, candidate_sha) if mutation == "tampered"
                          else commit("injected", "unreviewed\n"))
    error = CandidateRaceError if diagnostic == "main changed" else CandidateError
    message = diagnostic if error is CandidateRaceError else rf"Missing Mergify parent batches parent:.*({diagnostic})"
    with pytest.raises(error, match=message):
        attest(**kwargs)
    if mutation == "main-moved":
        assert seen == [102] * 8


@pytest.mark.parametrize("argv,error,diagnostic", [
    (("fetch", f"--depth={candidate._TREE_DEPTH}"), github.GhUnavailable, "temporarily unavailable"),
    (("fetch", "--deepen=32"), CandidateError, "time bound"),
    (("merge-tree",), CandidateError, "time bound"),
])
def test_candidate_time_bound_only_retries_initial_fetch(argv, error, diagnostic):
    with pytest.raises(error, match=diagnostic):
        candidate._require_time(argv, 0)


@pytest.mark.parametrize("case", [
    "single", "stacked", "combined", "mismatch", "incomplete", "stale", "ambiguous", "budget",
])
def test_candidate_policy(tree_candidate, monkeypatch, case):
    attest, git, commit, base, parent, source, candidate_sha = tree_candidate
    if case == "single":
        head = tree_commit(git, source, base, source, subject="single source integration")
        assert attest(lineage="single", head=head, main_head=base) == expected_sources((102, source))
        return
    if case == "stacked":
        assert attest(lineage="stacked", main_head=base) == expected_sources((101, parent), (102, source))
        return

    # The earlier batch has landed by squash. Both live source heads still need
    # to be recovered, and their integration must reproduce the candidate tree.
    git("checkout", "-b", "second", source)
    second = commit("source", "reviewed second version\n")
    git("checkout", "candidate")
    git("merge", "--no-ff", second, "-m", "second integration")
    head = git("rev-parse", "HEAD")
    if case == "combined":
        assert attest(head=head, extra_source=second) == expected_sources((101, second), (102, source))
        return

    kwargs, diagnostic = {
        "mismatch": ({}, "tree mismatch"),
        "incomplete": ({"main_head": ""}, "main object unavailable"),
        "stale": ({"main_after": base}, "main changed"),
        "ambiguous": ({}, "ambiguous"),
        "budget": ({}, "object count bound"),
    }[case]
    if case == "mismatch":
        head = forged_tree(git, commit, head)
    elif case == "ambiguous":
        head = tree_commit(git, head, candidate_sha, second, parent, subject="octopus")
    elif case == "budget":
        monkeypatch.setattr(candidate, "_TREE_OBJECTS", 1)
    with pytest.raises(CandidateError, match=diagnostic):
        attest(head=head, extra_source=second, **kwargs)

@pytest.mark.parametrize("timing", ["metadata", "policy", "initial", TypeError, AttributeError, KeyError])
def test_predecessor_landing_restarts_complete_attestation(tree_candidate, monkeypatch, timing):
    _, git, _, _, parent, source, head = tree_candidate
    candidate_event = event(head)
    values = payloads(candidate_event, (101, parent), (102, source))
    main, vanished = git("rev-parse", "main"), status()
    vanished["batches"].pop(0)
    reads = iter([status()] * (2 if timing == "metadata" else 1) + [vanished, vanished])
    lineage, verify = Mock(side_effect=reads), Mock(wraps=candidate._verify_tree)
    seen, sleeps = [], []
    values["/repos/owner/repo/git/ref/heads/main"] = {"object": {"sha": main}}

    def get(path):
        result = deepcopy(values[path])
        if path == pull_path(101):
            assert lineage.call_count > 0
            if timing == "initial" or read_count(101) == 2:
                result.update(state="closed", merged=True)
        return result

    fetch = Mock(side_effect=get)

    def read_count(number):
        return fetch.call_args_list.count(call(pull_path(number)))

    def policy(number, snapshot):
        seen.append(number)
        if isinstance(timing, type) and number == 101:
            raise timing("policy implementation error")
        if timing == "policy" and number == 101:
            assert snapshot["state"] == "open" and read_count(101) == 1 and lineage.call_count == 1
            raise CandidateError("source #101: PR #101 is MERGED, not open")

    monkeypatch.setattr(candidate, "_retry_sleep", sleeps.append)
    monkeypatch.setattr(candidate, "_verify_tree", verify)
    if isinstance(timing, type):
        with pytest.raises(timing, match="policy implementation error"):
            run_attestation(candidate_event, github_get=fetch, mergify_status=lineage, source_policy=policy)
        assert seen == [101] and read_count(101) == 1 and not sleeps
        return
    assert run_attestation(candidate_event, github_get=fetch, mergify_status=lineage,
                           source_policy=policy) == expected_sources((102, source))
    assert seen == {"metadata": [101, 102, 102], "policy": [101, 102], "initial": [102]}[timing]
    assert read_count(900) == (4 if timing == "metadata" else 3)
    verify.assert_called_once_with("owner/repo", main, head, [source])
    assert sleeps == [4]


@pytest.mark.parametrize("pr_number,field,value,diagnostic", [
    (101, "head", {"sha": "9" * 40}, "head changed"),
    (900, "head", {"sha": "9" * 40}, "Candidate head changed"),
    (101, "body", "new policy evidence", "fresh policy rejects drift"),
    (101, "draft", True, "fresh policy rejects drift"),
], ids=["source-head", "candidate-head", "body", "draft"])
def test_attestation_detects_head_and_policy_changes(pr_number, field, value, diagnostic):
    values, snapshots, endpoint = payloads(event()), [], pull_path(pr_number)

    def read(path):
        row = values[path]
        if path == endpoint and get.call_args_list.count(call(endpoint)) == 2:
            if field == "head":
                return deepcopy(row) | {field: value}
            row[field] = value
        return deepcopy(row)

    def policy(number, source):
        if number == 101:
            snapshots.append((source["body"], source["draft"]))
            if source[field] == value:
                raise CandidateError("fresh policy rejects drift")

    get = Mock(side_effect=read)
    with pytest.raises(CandidateError, match=diagnostic):
        run_attestation(github_get=get, **({"source_policy": policy} if field != "head" else {}))
    if field != "head":
        expected = (value, False) if field == "body" else ("source 101", True)
        assert snapshots == [("source 101", False), expected]
        assert get.call_args_list.count(call(endpoint)) == 4


@pytest.mark.parametrize("elapsed,oversleep,attempt_count,landing", [
    (0, 0, 8, None), (29, 0, 1, None), (0, 30, 1, None), (0, 0, None, 10), (0, 0, None, 24),
])
def test_candidate_race_retry_budget(monkeypatch, no_retry_delay, elapsed, oversleep, attempt_count, landing):
    attempts, sleeps, clock = [], [], no_retry_delay
    sources = expected_sources((102, "2" * 40))

    def attest(*_, **__):
        if landing is not None and clock[0] >= landing:
            return sources
        error = CandidateRaceError("Mergify candidate membership changed during attestation")
        attempts.append(error)
        clock[0] += elapsed
        raise error

    def sleep(seconds):
        sleeps.append(seconds)
        clock[0] += seconds + oversleep

    monkeypatch.setattr(candidate, "_attest_candidate_once", attest)
    monkeypatch.setattr(candidate, "_retry_sleep", sleep)
    if landing is not None:
        assert run_attestation() == sources
        return
    with pytest.raises(CandidateRaceError, match="membership changed") as caught:
        run_attestation()
    assert (len(attempts), caught.value) == (attempt_count, attempts[-1])
    assert sum(sleeps) < 30


def test_membership_drift_is_a_retryable_race():
    changed = status()
    changed["batches"][1]["sub_batches"][0]["pull_requests"] = [{"number": 103}]
    with pytest.raises(CandidateRaceError, match="membership changed"):
        run_attestation(status_reads=[status(), changed])


@pytest.mark.parametrize("second,stop", [(s, t) for s in (False, True) for t in ("settled", "ambiguous", "closed", "slow", "oversleep", "changed")])
def test_rebuild_wait(monkeypatch, no_retry_delay, second, stop):
    clock, sleeps, reads, values, seen = no_retry_delay, [], [], payloads(event()), []
    ambiguous = {"batches": [{"id": str(i), "queue_pull_request_number": 900} for i in range(2 * second)]}
    def sleep(delay):
        sleeps.append(delay)
        clock[0] += 100 if stop == "oversleep" else delay
        if stop == "closed":
            values[pull_path(900)]["state"] = "closed"
    def read_status(*_):
        reads.append(clock[0])
        if stop == "slow":
            clock[0] += 100
        if stop == "changed" and len(reads) >= 6:
            settled = status()
            settled["batches"][1]["sub_batches"][0]["id"] = "replacement"
            return settled
        if (second and len(reads) == 1) or (stop == "settled" and len(reads) > 1 + second):
            return status()
        return ambiguous
    monkeypatch.setattr(candidate, "_retry_sleep", sleep)
    run = partial(run_attestation, github_payloads=values, mergify_status=read_status,
                  source_policy=lambda n, _: seen.append(n))
    if stop in {"settled", "changed"}:
        assert run() == expected_sources((101, "1" * 40), (102, "2" * 40))
        assert seen == [101, 102] * (2 if stop == "changed" and second else 1)
    else:
        with pytest.raises(CandidateError, match="superseded" if stop == "closed" else "not exactly one live"):
            run()
        assert len(reads) <= 7 and sum(sleeps) <= 90
        assert clock[0] == {"closed": 2, "ambiguous": 90, "slow": 100 * (1 + second), "oversleep": 100}[stop]


@pytest.mark.parametrize("outcome,message", [("normal", ""), ("bound", ""), ("fetch", "returned error: 503"), ("fetch", "Could not resolve host: github.com"), ("fetch", "fatal: early EOF"), ("fetch", "GnuTLS recv error (-110)"), ("fetch", "unable to access 'https://github.com/o/r.git': Empty reply from server"), ("denied", "unable to access 'https://github.com/acme/proxy-ssl.git': The requested URL returned error: 403"), ("fetch", "Could not resolve proxy: proxy.example"), ("fetch", "RPC failed; curl 92 HTTP/2 stream 3 was not closed cleanly"), ("fetch", "RPC failed; HTTP 429 curl 22"), ("denied", "RPC failed; HTTP 403 curl 22"), ("denied", "repository 'https://github.com/acme/ssl_connect.git/' not found")])
def test_fetched_git_waits_for_exit_or_resumes_size_checks(monkeypatch, outcome, message):
    waits, killed = [], []
    def spawn(argv, cwd, **kwargs):
        exit_code = 128 if outcome in {"fetch", "denied"} and "fetch" in argv else 0
        if exit_code:
            kwargs["stderr"].write(f"fatal: {message}".encode())
        child = Mock(pid=123, returncode=exit_code)
        child.poll.return_value = None if "test-command" in argv else exit_code

        def wait(timeout=None):
            if child.poll.return_value is None:
                waits.append(timeout)
                if timeout is not None and outcome == "bound":
                    (cwd / "oversized").write_bytes(b"x" * 65)
                    raise subprocess.TimeoutExpired("test-command", timeout)
                child.poll.return_value = exit_code
            return exit_code

        child.wait.side_effect = wait
        return child

    monkeypatch.setenv("GH_TOKEN", "test-token")
    monkeypatch.setattr(candidate, "_TREE_BYTES", 64)
    monkeypatch.setattr(subprocess, "Popen", spawn)
    monkeypatch.setattr(candidate.os, "killpg", lambda pid, sig: killed.append((pid, sig)))
    monkeypatch.setattr(candidate.time, "sleep", lambda _: pytest.fail("used a fixed delay"))

    error = {"fetch": pytest.raises(github.GhUnavailable, match="temporarily unavailable"),
             "denied": pytest.raises(CandidateError, match="objects unavailable")}.get(outcome, nullcontext())
    with error, candidate.fetched_git("owner/repo", []) as git:
        bound = pytest.raises(CandidateError, match="Git object/size bound exceeded")
        with bound if outcome == "bound" else nullcontext():
            assert git("test-command") == ""

    assert waits == {"bound": [0.02, None], "normal": [0.02], "fetch": [], "denied": []}[outcome]
    assert killed == ([(123, candidate.signal.SIGKILL)] if outcome == "bound" else [])


@pytest.mark.parametrize("failure", [candidate.FetchFailed("git fetch exited 128"), github.ProcTimeout("read timed out"),
                                     github.GhError(("gh",), "HTTP 403"), github.GhError(("gh",), "HTTP 404")])
def test_failed_inheritance_read_is_not_reported_as_missing_review(monkeypatch, failure):
    pr = {"base": {"sha": "b" * 40, "ref": "main"}, "head": {"sha": "c" * 40}}
    fetch_failed = isinstance(failure, candidate.FetchFailed)
    monkeypatch.setattr(github, "api_get", Mock(return_value=pr, side_effect=None if fetch_failed else failure))
    monkeypatch.setattr(candidate, "fetched_git", Mock(side_effect=failure))
    with pytest.raises(type(failure), match=str(failure)):
        eligibility.base_merge_material("o/r", 7, "a" * 40, "c" * 40, "main", clean=True)
