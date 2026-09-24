"""Mergify candidate attestation at the trusted hosted boundary."""

from __future__ import annotations

from copy import deepcopy

import pytest

from loopzero.candidate import CandidateError, attest_candidate


def event() -> dict:
    value = {
        "action": "synchronize",
        "repository": {"id": 7, "full_name": "owner/repo"},
        "pull_request": {
            "number": 900,
            "state": "open",
            "draft": True,
            "user": {
                "id": 37929162,
                "login": "mergify[bot]",
                "type": "Bot",
                "html_url": "https://github.com/apps/mergify",
            },
            "head": {
                "sha": "c" * 40,
                "ref": "mergify/merge-queue/batch",
                "repo": {"id": 7, "full_name": "owner/repo"},
            },
            "base": {
                "sha": "b" * 40,
                "ref": "main",
                "repo": {"id": 7, "full_name": "owner/repo"},
            },
        },
    }
    value["sender"] = deepcopy(value["pull_request"]["user"])
    return value


def status() -> dict:
    return {
        "batches": [
            {
                "id": "parent",
                "parent_ids": [],
                "queue_pull_request_number": 899,
                "pull_requests": [{"number": 101}],
                "sub_batches": None,
            },
            {
                "id": "outer",
                "parent_ids": [],
                "queue_pull_request_number": None,
                "pull_requests": [],
                "sub_batches": [
                    {
                        "id": "candidate",
                        "parent_ids": ["parent"],
                        "queue_pull_request_number": 900,
                        "pull_requests": [{"number": 102}],
                        "sub_batches": [],
                    }
                ],
            },
        ]
    }


def pull(number: int, sha: str) -> dict:
    return {
        "number": number,
        "state": "open",
        "draft": False,
        "body": f"source {number}",
        "head": {"sha": sha},
        "base": {"ref": "main", "repo": {"id": 7, "full_name": "owner/repo"}},
    }


def payloads(candidate_event: dict) -> dict[str, object]:
    candidate = deepcopy(candidate_event["pull_request"])
    candidate["number"] = 900
    return {
        "/apps/mergify": {
            "id": 10562,
            "slug": "mergify",
            "owner": {"login": "Mergifyio"},
        },
        "/users/mergify%5Bbot%5D": deepcopy(candidate["user"]),
        "/repos/owner/repo/pulls/900": candidate,
        "/repos/owner/repo/pulls/101": pull(101, "1" * 40),
        "/repos/owner/repo/pulls/102": pull(102, "2" * 40),
        "/repos/owner/repo/compare/" + "1" * 40 + "..." + "c" * 40: {
            "status": "ahead",
            "merge_base_commit": {"sha": "1" * 40},
        },
        "/repos/owner/repo/compare/" + "2" * 40 + "..." + "c" * 40: {
            "status": "ahead",
            "merge_base_commit": {"sha": "2" * 40},
        },
    }


def run_attestation(
    *,
    candidate_event: dict | None = None,
    status_reads: list[dict] | None = None,
    github_payloads: dict[str, object] | None = None,
    source_policy=lambda _number, _source: None,
):
    candidate_event = candidate_event or event()
    reads = iter(status_reads or [status(), status()])
    values = github_payloads or payloads(candidate_event)
    return attest_candidate(
        candidate_event,
        github_get=lambda path: deepcopy(values[path]),
        mergify_status=lambda _owner, _repo, _base: deepcopy(next(reads)),
        source_policy=source_policy,
    )


def test_attests_nested_candidate_and_transitive_parent_sources() -> None:
    seen: list[int] = []
    assert run_attestation(source_policy=lambda number, _source: seen.append(number)) == [
        {"number": 101, "head_sha": "1" * 40},
        {"number": 102, "head_sha": "2" * 40},
    ]
    assert seen == [101, 102]


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (lambda value: value["pull_request"]["user"].update(id=1), "Mergify GitHub App"),
        (lambda value: value["sender"].update(id=1), "head event was not sent"),
        (lambda value: value.update(action="edited"), "creation or head-update event"),
        (
            lambda value: value["pull_request"]["head"].update(ref="feature/forged"),
            "unexpected head ref",
        ),
    ],
)
def test_rejects_forged_bot_or_prefix(mutation, message: str) -> None:
    candidate_event = event()
    original_payloads = payloads(candidate_event)
    mutation(candidate_event)
    with pytest.raises(CandidateError, match=message):
        run_attestation(
            candidate_event=candidate_event,
            github_payloads=original_payloads,
        )


def test_rejects_candidate_absent_from_live_status() -> None:
    first = status()
    first["batches"][1]["sub_batches"][0]["queue_pull_request_number"] = 901
    with pytest.raises(CandidateError, match="live Mergify batch"):
        run_attestation(status_reads=[first])


def test_rejects_missing_live_status_as_candidate_error() -> None:
    with pytest.raises(CandidateError, match="status is not an object"):
        run_attestation(status_reads=[None])


def test_rejects_source_head_missing_from_candidate_history() -> None:
    values = payloads(event())
    values["/repos/owner/repo/compare/" + "2" * 40 + "..." + "c" * 40] = {
        "status": "diverged",
        "merge_base_commit": {"sha": "0" * 40},
    }
    with pytest.raises(CandidateError, match="not an ancestor"):
        run_attestation(github_payloads=values)


def test_rejects_membership_change_during_attestation() -> None:
    changed = status()
    changed["batches"][1]["sub_batches"][0]["pull_requests"] = [{"number": 103}]
    with pytest.raises(CandidateError, match="membership changed"):
        run_attestation(status_reads=[status(), changed])


@pytest.mark.parametrize(
    ("pr_number", "diagnostic"),
    [(101, "head changed"), (900, "Candidate head changed")],
    ids=["source-head", "candidate-head"],
)
def test_rejects_head_change_during_attestation(pr_number: int, diagnostic: str) -> None:
    candidate_event = event()
    values = payloads(candidate_event)
    calls = 0

    def github_get(path: str):
        nonlocal calls
        result = deepcopy(values[path])
        if path == f"/repos/owner/repo/pulls/{pr_number}":
            calls += 1
            if calls == 2:
                result["head"]["sha"] = "9" * 40
        return result

    reads = iter([status(), status()])
    with pytest.raises(CandidateError, match=diagnostic):
        attest_candidate(
            candidate_event,
            github_get=github_get,
            mergify_status=lambda _owner, _repo, _base: deepcopy(next(reads)),
            source_policy=lambda _number, _source: None,
        )


def test_same_head_still_obeys_source_policy() -> None:
    values = payloads(event())
    values["/repos/owner/repo/pulls/102"]["head"]["sha"] = "c" * 40
    values["/repos/owner/repo/compare/" + "c" * 40 + "..." + "c" * 40] = {
        "status": "identical",
        "merge_base_commit": {"sha": "c" * 40},
    }

    def block(number: int, _source: dict) -> None:
        if number == 102:
            raise CandidateError("source policy blocked same-head source")

    with pytest.raises(CandidateError, match="source policy blocked"):
        run_attestation(github_payloads=values, source_policy=block)


def test_source_policy_rejection_blocks_candidate_immediately() -> None:
    seen: list[int] = []

    def reject(number: int, _source: dict) -> None:
        seen.append(number)
        raise CandidateError("ineligible source")

    with pytest.raises(CandidateError, match="ineligible source"):
        run_attestation(source_policy=reject)
    assert seen == [101]


@pytest.fixture
def tree_candidate(tmp_path, monkeypatch):
    import subprocess

    remote = tmp_path / "remote"
    remote.mkdir()

    def git(*args):
        return subprocess.run(["git", *args], cwd=remote, check=True, capture_output=True,
                              text=True).stdout.strip()

    git("init", "-b", "main")
    git("config", "user.name", "Test")
    git("config", "user.email", "test@example.org")

    def commit(name, content):
        (remote / name).write_text(content)
        git("add", ".")
        git("commit", "-m", name)
        return git("rev-parse", "HEAD")

    base = commit("base", "base\n")
    git("checkout", "-b", "parent")
    parent = commit("parent", "H1\n")
    git("checkout", "-b", "source", base)
    source = commit("source", "reviewed\n")
    git("checkout", "-b", "candidate", base)
    git("merge", "--no-ff", parent, "-m", "parent integration")
    git("merge", "--no-ff", source, "-m", "source integration")
    candidate_sha = git("rev-parse", "HEAD")
    git("checkout", "main")
    git("merge", "--squash", parent)
    git("commit", "-m", "squashed parent")
    main = git("rev-parse", "HEAD")
    popen = subprocess.Popen

    def isolated_fetch(argv, **kwargs):
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

    def attest(*, head=candidate_sha, main_head=main, policy=lambda _n, _s: None,
               main_after=None, extra_source=None):
        candidate_event = event()
        candidate_event["pull_request"]["head"]["sha"] = head
        values = payloads(candidate_event)
        values["/repos/owner/repo/pulls/102"] = pull(102, source)
        values[f"/repos/owner/repo/compare/{source}...{head}"] = {
            "status": "ahead", "merge_base_commit": {"sha": source},
        }
        vanished = status()
        vanished["batches"].pop(0)
        if extra_source:
            values["/repos/owner/repo/pulls/101"] = pull(101, extra_source)
            values[f"/repos/owner/repo/compare/{extra_source}...{head}"] = {
                "status": "ahead", "merge_base_commit": {"sha": extra_source},
            }
            vanished["batches"][0]["sub_batches"][0]["pull_requests"].append({"number": 101})
        reads = iter([main_head, main_after or main_head])

        def get(path):
            if path.endswith("/git/ref/heads/main"):
                return {"object": {"sha": next(reads)}}
            return deepcopy(values[path])

        return attest_candidate(candidate_event, github_get=get,
                                mergify_status=lambda *_: deepcopy(vanished), source_policy=policy)

    return attest, git, commit, base, parent, source, candidate_sha


def test_vanished_squash_parent_passes_real_tree_attestation(tree_candidate):
    attest, _, _, _, _, source, _ = tree_candidate
    seen = []
    assert attest(policy=lambda n, _: seen.append(n)) == [{"number": 102, "head_sha": source}]
    assert seen == [102]


@pytest.mark.parametrize("mutation", ["replaced", "withdrawn", "extra", "tampered", "missing"])
def test_vanished_parent_rejects_unauthorized_trees(tree_candidate, mutation):
    attest, git, commit, base, _, _, candidate_sha = tree_candidate
    kwargs = {}
    if mutation == "replaced":
        kwargs["main_head"] = commit("parent", "H2\n")
    elif mutation == "withdrawn":
        kwargs["main_head"] = base
    elif mutation == "missing":
        kwargs["main_head"] = "f" * 40
    else:
        git("checkout", "candidate")
        changed = commit("injected", "unreviewed\n")
        if mutation == "tampered":
            # A forged resolution retains the exact original merge parents.
            parents = git("show", "-s", "--format=%P", candidate_sha).split()
            changed = git("commit-tree", git("rev-parse", f"{changed}^{{tree}}"),
                          "-p", parents[0], "-p", parents[1], "-m", "forged merge")
        kwargs["head"] = changed
    with pytest.raises(CandidateError, match="Missing Mergify parent batches parent:.*"
                       "(tree mismatch|objects unavailable)"):
        attest(**kwargs)


@pytest.mark.parametrize("bound", ["_TREE_BYTES", "_TREE_OBJECTS", "_TREE_SECONDS", "_TREE_OUTPUT"])
def test_vanished_parent_fails_closed_at_resource_bounds(tree_candidate, monkeypatch, bound):
    from loopzero import candidate

    monkeypatch.setattr(candidate, bound, 1 if bound != "_TREE_SECONDS" else 0)
    with pytest.raises(CandidateError, match="Missing Mergify parent batches parent:.*bound"):
        tree_candidate[0]()


def test_vanished_parent_rejects_main_change(tree_candidate):
    with pytest.raises(CandidateError, match="parent: main changed"):
        tree_candidate[0](main_after=tree_candidate[3])


def test_vanished_parent_rejects_ambiguous_topology(tree_candidate):
    attest, git, _, base, parent, source, candidate_sha = tree_candidate
    head = git("commit-tree", git("rev-parse", f"{candidate_sha}^{{tree}}"),
               "-p", base, "-p", parent, "-p", source, "-m", "octopus")
    with pytest.raises(CandidateError, match="parent: ambiguous"):
        attest(head=head)


def test_vanished_parent_orders_live_sources_by_topology(tree_candidate):
    attest, git, commit, _, _, source, _ = tree_candidate
    git("checkout", "-b", "second", source)
    second = commit("source", "reviewed second version\n")
    git("checkout", "candidate")
    git("merge", "--no-ff", second, "-m", "second integration")
    assert attest(head=git("rev-parse", "HEAD"), extra_source=second) == [
        {"number": 101, "head_sha": second}, {"number": 102, "head_sha": source},
    ]


def test_vanished_parent_rejects_conflicting_current_main(tree_candidate):
    attest, _, commit, *_ = tree_candidate
    main = commit("source", "conflicting main content\n")
    with pytest.raises(CandidateError, match="parent:.*integration conflict"):
        attest(main_head=main)
