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


def test_rejects_source_head_change_during_attestation() -> None:
    candidate_event = event()
    values = payloads(candidate_event)
    calls = 0

    def github_get(path: str):
        nonlocal calls
        result = deepcopy(values[path])
        if path == "/repos/owner/repo/pulls/101":
            calls += 1
            if calls == 2:
                result["head"]["sha"] = "9" * 40
        return result

    reads = iter([status(), status()])
    with pytest.raises(CandidateError, match="head changed"):
        attest_candidate(
            candidate_event,
            github_get=github_get,
            mergify_status=lambda _owner, _repo, _base: deepcopy(next(reads)),
            source_policy=lambda _number, _source: None,
        )


def test_rejects_candidate_head_change_during_attestation() -> None:
    candidate_event = event()
    values = payloads(candidate_event)
    calls = 0

    def github_get(path: str):
        nonlocal calls
        result = deepcopy(values[path])
        if path == "/repos/owner/repo/pulls/900":
            calls += 1
            if calls == 2:
                result["head"]["sha"] = "9" * 40
        return result

    reads = iter([status(), status()])
    with pytest.raises(CandidateError, match="Candidate head changed"):
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
