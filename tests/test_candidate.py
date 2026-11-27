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
from unittest.mock import Mock

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
    bot = {
        "id": 37929162, "login": "mergify[bot]", "type": "Bot",
        "html_url": "https://github.com/apps/mergify",
    }
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
    outer = batch("outer", parents=[], number=None, sources=[], children=[candidate])
    return {"batches": [parent, outer]}


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


def source_payloads(values, head, *sources):
    for number, sha in sources:
        values[f"/repos/owner/repo/pulls/{number}"] = pull(number, sha)
        values[f"/repos/owner/repo/compare/{sha}...{head}"] = {
            "status": "ahead", "merge_base_commit": {"sha": sha},
        }


def payloads(candidate_event: dict) -> dict[str, object]:
    candidate = deepcopy(candidate_event["pull_request"]) | {"number": 900}
    values = {
        "/apps/mergify": {"id": 10562, "slug": "mergify", "owner": {"login": "Mergifyio"}},
        "/users/mergify%5Bbot%5D": deepcopy(candidate["user"]),
        "/repos/owner/repo/pulls/900": candidate,
    }
    source_payloads(values, "c" * 40, (101, "1" * 40), (102, "2" * 40))
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


@pytest.mark.parametrize("case,diagnostic", [
    ("author", "Mergify GitHub App"), ("sender", "head event was not sent"),
    ("action", "creation or head-update event"), ("ref", "unexpected head ref"),
    ("missing status", "status is not an object"), ("missing ancestor", "not an ancestor"),
    ("same-head policy", "source policy blocked"),
    ("candidate ready", "must be a draft"),
    ("candidate retargeted", "must target the real main branch"),
    ("candidate head fork", "head repository does not match"),
    ("candidate base fork", "base repository does not match"),
    ("source retargeted", "source PR #102 does not target this repository"),
])
def test_rejects_invalid_attestation_evidence(case, diagnostic):
    incoming = event()
    values = payloads(incoming)
    status_reads = [None] if case == "missing status" else None

    def policy(number, _source):
        if case == "same-head policy" and number == 102:
            raise CandidateError("source policy blocked same-head source")

    if case == "author":
        incoming["pull_request"]["user"]["id"] = 1
    elif case == "sender":
        incoming["sender"]["id"] = 1
    elif case == "action":
        incoming["action"] = "edited"
    elif case == "ref":
        incoming["pull_request"]["head"]["ref"] = "feature/forged"
    elif case == "candidate ready":
        values["/repos/owner/repo/pulls/900"]["draft"] = False
    elif case == "candidate retargeted":
        values["/repos/owner/repo/pulls/900"]["base"]["ref"] = "release"
    elif case in {"candidate head fork", "candidate base fork"}:
        values["/repos/owner/repo/pulls/900"][case.split()[1]]["repo"]["id"] = 8
    elif case == "source retargeted":
        values["/repos/owner/repo/pulls/102"]["base"]["ref"] = "release"
    elif case == "missing ancestor":
        values[f"/repos/owner/repo/compare/{'2' * 40}...{'c' * 40}"]["status"] = "diverged"
    elif case == "same-head policy":
        head = "c" * 40
        values["/repos/owner/repo/pulls/102"]["head"]["sha"] = head
        values[f"/repos/owner/repo/compare/{head}...{head}"] = {
            "status": "identical", "merge_base_commit": {"sha": head},
        }
    with pytest.raises(CandidateError, match=diagnostic):
        run_attestation(incoming, github_payloads=values, status_reads=status_reads, source_policy=policy)


@pytest.mark.parametrize("pr_number,diagnostic", [
    (101, "head changed"), (900, "Candidate head changed"),
], ids=["source-head", "candidate-head"])
def test_rejects_head_change_during_attestation(pr_number: int, diagnostic: str) -> None:
    values = payloads(event())
    calls = 0

    def github_get(path: str):
        nonlocal calls
        result = deepcopy(values[path])
        if path == f"/repos/owner/repo/pulls/{pr_number}":
            calls += 1
            if calls == 2:
                result["head"]["sha"] = "9" * 40
        return result

    with pytest.raises(CandidateError, match=diagnostic):
        run_attestation(github_get=github_get)


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
            argv = [str(remote) if arg == "https://github.com/owner/repo.git" else arg
                    for arg in argv]
        return popen(argv, **kwargs)

    monkeypatch.setenv("GH_TOKEN", "secret-token")
    monkeypatch.setattr(subprocess, "Popen", isolated_fetch)

    def attest(
        *, head=candidate_sha, main_head=main, policy=lambda _n, _s: None,
        main_after=None, extra_source=None, lineage="vanished", base_branch="main",
    ):
        candidate_event = event(head)
        values = payloads(candidate_event)
        source_payloads(values, head, (102, source))
        queue = status()
        if lineage != "stacked":
            queue["batches"].pop(0)
        if lineage == "single":
            queue["batches"][0]["sub_batches"][0]["parent_ids"] = []
        if lineage == "stacked":
            source_payloads(values, head, (101, parent))
        if extra_source:
            source_payloads(values, head, (101, extra_source))
            queue["batches"][0]["sub_batches"][0]["pull_requests"].append({"number": 101})
        for number in (101, 102):
            values[f"/repos/owner/repo/pulls/{number}"].update(
                body="## Objective\nApply a reviewed source change.",
            )
            for endpoint in ("commits", "files"):
                values[f"/repos/owner/repo/pulls/{number}/{endpoint}?per_page=100&page=1"] = []
        if base_branch != "main":
            for number in (102, 900):
                values[f"/repos/owner/repo/pulls/{number}"]["base"]["ref"] = base_branch
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
    popen, depths = subprocess.Popen, []

    def record_fetch(argv, **kwargs):
        depths.extend(arg for arg in argv if arg.startswith(("--depth=", "--deepen=")))
        return popen(argv, **kwargs)

    monkeypatch.setattr(subprocess, "Popen", record_fetch)
    for index in range(5):
        git("commit", "--allow-empty", "-m", f"base advance {index}")
    main = git("rev-parse", "main")
    assert attest(main_head=main) == expected_sources((102, source))
    assert depths[0] == "--depth=2" and any(depth.startswith("--deepen=") for depth in depths)

    depths.clear()
    git("checkout", "--detach", source)
    git("merge", "--no-ff", main, "-m", "merge current main")
    head = git("rev-parse", "HEAD")
    with candidate.fetched_git("owner/repo", [source, head, main]) as run:
        assert eligibility._verified_base_chain(run, source, head, main, clean=True, conflict_only=False)
    assert depths[0] == "--depth=2" and any(depth.startswith("--deepen=") for depth in depths)


@pytest.mark.parametrize("mutation,diagnostic", [
    ("replaced", "tree mismatch"), ("withdrawn", "tree mismatch"), ("extra", "tree mismatch|objects unavailable"),
    ("tampered", "tree mismatch"), ("missing", "objects unavailable"),
    ("octopus", "ambiguous"), ("main conflict", "integration conflict"),
])
def test_vanished_parent_rejects_unauthorized_trees(tree_candidate, mutation, diagnostic):
    attest, git, commit, base, parent, source, candidate_sha = tree_candidate
    kwargs = {}
    if mutation == "replaced":
        kwargs["main_head"] = commit("parent", "H2\n")
    elif mutation == "withdrawn":
        kwargs["main_head"] = base
    elif mutation == "missing":
        kwargs["main_head"] = "f" * 40
    elif mutation == "main conflict":
        kwargs["main_head"] = commit("source", "conflicting main content\n")
    elif mutation == "octopus":
        kwargs["head"] = tree_commit(git, candidate_sha, base, parent, source, subject="octopus")
    else:
        git("checkout", "candidate")
        changed = commit("injected", "unreviewed\n")
        if mutation == "tampered":
            # A forged resolution retains the exact original merge parents.
            parents = git("show", "-s", "--format=%P", candidate_sha).split()
            changed = tree_commit(git, changed, *parents, subject="forged merge")
        kwargs["head"] = changed
    with pytest.raises(CandidateError, match=rf"Missing Mergify parent batches parent:.*({diagnostic})"):
        attest(**kwargs)


def test_stalled_fetch_is_transient_but_other_time_bounds_are_not():
    with pytest.raises(github.GhUnavailable, match="temporarily unavailable"):
        candidate._require_time(("fetch", f"--depth={candidate._TREE_DEPTH}"), 0)
    with pytest.raises(CandidateError, match="time bound"):
        candidate._require_time(("fetch", "--deepen=32"), 0)
    with pytest.raises(CandidateError, match="time bound"):
        candidate._require_time(("merge-tree",), 0)


def test_tree_mismatch_while_main_lands_is_a_race(tree_candidate, monkeypatch):
    monkeypatch.setattr(candidate, "_verify_tree", Mock(side_effect=CandidateError("candidate tree mismatch")))
    with pytest.raises(CandidateRaceError, match="main changed"):
        tree_candidate[0](main_after=tree_candidate[3])


@pytest.mark.parametrize("bound", ["_TREE_BYTES", "_TREE_OBJECTS", "_TREE_SECONDS", "_TREE_OUTPUT"])
def test_vanished_parent_fails_closed_at_resource_bounds(tree_candidate, monkeypatch, bound):
    monkeypatch.setattr(candidate, bound, 1 if bound != "_TREE_SECONDS" else 0)
    with pytest.raises(CandidateError, match=r"Missing Mergify parent batches parent:.*bound"):
        tree_candidate[0]()


def test_vanished_parent_rejects_main_change(tree_candidate):
    seen = []
    with pytest.raises(CandidateRaceError, match="main changed"):
        tree_candidate[0](main_after=tree_candidate[3], policy=lambda n, _: seen.append(n))
    assert set(seen) == {102} and len(seen) == 8


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

    kwargs = {}
    if case == "mismatch":
        changed = commit("injected", "unreviewed\n")
        parents = git("show", "-s", "--format=%P", head).split()
        head = tree_commit(git, changed, *parents, subject="forged merge")
        diagnostic = "tree mismatch"
    elif case == "incomplete":
        kwargs["main_head"] = ""
        diagnostic = "main object unavailable"
    elif case == "stale":
        kwargs["main_after"] = base
        diagnostic = "main changed"
    elif case == "ambiguous":
        head = tree_commit(git, head, candidate_sha, second, parent, subject="octopus")
        diagnostic = "ambiguous"
    else:
        monkeypatch.setattr(candidate, "_TREE_OBJECTS", 1)
        diagnostic = "object count bound"
    with pytest.raises(CandidateError, match=diagnostic):
        attest(head=head, extra_source=second, **kwargs)


@pytest.mark.parametrize("timing", ["metadata", "policy", "initial", TypeError, AttributeError, KeyError])
def test_predecessor_landing_restarts_complete_attestation(tree_candidate, monkeypatch, timing):
    _, git, _, _, parent, source, head = tree_candidate
    candidate_event = event(head)
    values = payloads(candidate_event)
    source_payloads(values, head, (101, parent), (102, source))
    main, vanished = git("rev-parse", "main"), status()
    vanished["batches"].pop(0)
    reads = iter([status()] * (2 if timing == "metadata" else 1) + [vanished, vanished])
    source_reads = candidate_reads = status_reads = 0
    seen, verified, sleeps, verify = [], [], [], candidate._verify_tree

    def get(path):
        nonlocal source_reads, candidate_reads
        if path.endswith("/git/ref/heads/main"):
            return {"object": {"sha": main}}
        result = deepcopy(values[path])
        candidate_reads += path.endswith("/pulls/900")
        if path.endswith("/pulls/101"):
            source_reads += 1
            assert status_reads > 0  # lineage precedes the landing REST/policy race
            if timing == "initial" or source_reads == 2:
                result.update(state="closed", merged=True)
        return result

    def lineage(*_):
        nonlocal status_reads
        status_reads += 1
        return deepcopy(next(reads))

    def policy(number, snapshot):
        seen.append(number)
        if isinstance(timing, type) and number == 101:
            raise timing("policy implementation error")
        if timing == "policy" and number == 101:
            assert snapshot["state"] == "open" and source_reads == 1 and status_reads == 1
            raise CandidateError("source #101: PR #101 is MERGED, not open")

    def verify_tree(repo, main_sha, candidate_sha, sources):
        verified.append((main_sha, candidate_sha, sources))
        verify(repo, main_sha, candidate_sha, sources)

    monkeypatch.setattr(candidate, "_retry_sleep", sleeps.append)
    monkeypatch.setattr(candidate, "_verify_tree", verify_tree)
    if isinstance(timing, type):
        with pytest.raises(timing, match="policy implementation error"):
            run_attestation(candidate_event, github_get=get, mergify_status=lineage, source_policy=policy)
        assert seen == [101] and source_reads == 1 and not sleeps
        return
    assert run_attestation(candidate_event, github_get=get, mergify_status=lineage,
                           source_policy=policy) == expected_sources((102, source))
    assert seen == {"metadata": [101, 102, 102], "policy": [101, 102], "initial": [102]}[timing]
    assert candidate_reads == (4 if timing == "metadata" else 3)
    assert verified == [(main, head, [source])] and sleeps == [4]


@pytest.mark.parametrize("field,value", [("body", "new policy evidence"), ("draft", True)])
def test_metadata_retry_evaluates_fresh_policy(field, value):
    values, snapshots, reads = payloads(event()), [], 0

    def get(path):
        nonlocal reads
        if path.endswith("/pulls/101"):
            reads += 1
            if reads == 2:
                values[path][field] = value
        return deepcopy(values[path])

    def policy(number, source):
        if number == 101:
            snapshots.append((source["body"], source["draft"]))
            if source[field] == value:
                raise CandidateError("fresh policy rejects drift")

    with pytest.raises(CandidateError, match="fresh policy rejects drift"):
        run_attestation(github_get=get, source_policy=policy)
    assert snapshots == [
        ("source 101", False),
        (value if field == "body" else "source 101", field == "draft"),
    ]
    assert reads == 4


@pytest.mark.parametrize("failure,reread", [(f, None) for f in ["closed", "tree", "identity"]] +
                         [("policy", v) for v in [pull(101, "1" * 40),
                          {"merged": True, "head": {"sha": "9" * 40}}, OSError("lookup failed"),
                          [], {"merged": True, "head": None},
                          {"merged": 1, "head": {"sha": "1" * 40}}]])
def test_nonrace_failure_does_not_retry(monkeypatch, failure, reread):
    values, live = payloads(event()), status()
    sleeps, attempts, source_reads = [], [], []
    expected = CandidateError("source #101: missing required review")
    if failure == "closed":
        values["/repos/owner/repo/pulls/101"].update(state="closed", merged=False)
    if failure == "identity":
        values["/repos/owner/repo/pulls/900"]["user"]["id"] = 1
    if failure == "tree":
        live["batches"].pop(0)
        values["/repos/owner/repo/git/ref/heads/main"] = {"object": {"sha": "b" * 40}}

        monkeypatch.setattr(candidate, "_verify_tree", Mock(side_effect=CandidateError("candidate tree mismatch")))

    def get(path):
        if path == "/apps/mergify":
            attempts.append(path)
        result = deepcopy(values[path])
        if path.endswith("/pulls/101"):
            source_reads.append(path)
            if len(source_reads) == 2 and failure == "policy":
                if isinstance(reread, Exception):
                    raise reread
                return deepcopy(reread)
        return result

    policy = Mock(side_effect=expected if failure == "policy" else None)
    monkeypatch.setattr(candidate, "_retry_sleep", sleeps.append)
    with pytest.raises(CandidateError) as caught:
        run_attestation(github_get=get, status_reads=[live], source_policy=policy)
    assert not isinstance(caught.value, CandidateRaceError) and len(attempts) == 1 and sleeps == []
    if failure == "policy":
        assert caught.value is expected and len(source_reads) == 2


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
    else:
        with pytest.raises(CandidateRaceError, match="membership changed") as caught:
            run_attestation(github_get=lambda _: None, status_reads=[None])
        assert len(attempts) == attempt_count and caught.value is attempts[-1]
        assert sum(sleeps) < 30


def test_membership_drift_is_a_retryable_race():
    changed = status()
    changed["batches"][1]["sub_batches"][0]["pull_requests"] = [{"number": 103}]
    with pytest.raises(CandidateRaceError, match="membership changed"):
        run_attestation(status_reads=[status(), changed])


@pytest.mark.parametrize("second,stop", [(s, t) for s in (False, True) for t in ("settled", "ambiguous", "closed", "slow", "oversleep", "changed")])
def test_rebuild_wait(monkeypatch, no_retry_delay, second, stop):
    clock, sleeps, reads, values, seen = no_retry_delay, [], [], payloads(event()), []
    ambiguous = {"batches": []}
    def sleep(delay):
        sleeps.append(delay)
        clock[0] += 100 if stop == "oversleep" else delay
        if stop == "closed":
            values["/repos/owner/repo/pulls/900"]["state"] = "closed"
    def read_status(*_):
        reads.append(clock[0])
        if stop == "slow":
            clock[0] += 100
        settled = status()
        if stop == "changed" and len(reads) >= 6:
            settled["batches"][1]["sub_batches"][0]["id"] = "replacement"
        return settled if (
            (second and len(reads) == 1)
            or (stop == "settled" and len(reads) > 1 + second)
            or (stop == "changed" and len(reads) >= 6)
        ) else ambiguous
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
    class Child:
        pid = 123
        def __init__(self, argv, cwd, **kwargs):
            self.root, self.running = cwd, "test-command" in argv
            self.returncode = 128 if outcome in {"fetch", "denied"} and "fetch" in argv else 0
            if self.returncode:
                kwargs["stderr"].write(f"fatal: {message}".encode())

        def poll(self):
            return None if self.running else self.returncode

        def wait(self, timeout=None):
            if self.running:
                waits.append(timeout)
                if timeout is not None and outcome == "bound":
                    (self.root / "oversized").write_bytes(b"x" * 65)
                    raise subprocess.TimeoutExpired("test-command", timeout)
                self.running = False
            return self.returncode

    monkeypatch.setenv("GH_TOKEN", "test-token")
    monkeypatch.setattr(candidate, "_TREE_BYTES", 64)
    monkeypatch.setattr(subprocess, "Popen", Child)
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


def test_failed_fetch_is_not_reported_as_missing_review(monkeypatch):
    from loopzero import eligibility
    monkeypatch.setattr(eligibility.github, "api_get", lambda _: {"base": {"sha": "b" * 40, "ref": "main"}, "head": {"sha": "c" * 40}})
    def fail(*_a):
        raise candidate.FetchFailed("git fetch exited 128: objects unavailable")
    monkeypatch.setattr(eligibility.candidate, "fetched_git", fail)
    with pytest.raises(candidate.FetchFailed):
        eligibility.base_merge_material("o/r", 7, "a" * 40, "c" * 40, "main", clean=True)
