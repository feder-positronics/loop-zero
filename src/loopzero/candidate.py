"""Attest a live Mergify candidate around a caller-owned source policy.

The caller owns API transport and source eligibility. This module only binds an
authenticated temporary candidate to stable, current source PR heads without
checking out or executing candidate code.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from typing import Any

MERGIFY_APP_ID = 10562
MERGIFY_APP_OWNER = "Mergifyio"
MERGIFY_APP_URL = "https://github.com/apps/mergify"
MERGIFY_BOT_ID = 37929162
MERGIFY_BOT_LOGIN = "mergify[bot]"
QUEUE_BRANCH_PREFIX = "mergify/merge-queue/"

_REPOSITORY = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
_SHA = re.compile(r"^[0-9a-f]{40}$")

JsonObject = dict[str, Any]
JsonValue = JsonObject | list[Any]
GitHubGet = Callable[[str], JsonValue]
MergifyStatus = Callable[[str, str, str], JsonObject]
SourcePolicy = Callable[[int, JsonObject], None]


class CandidateError(RuntimeError):
    """Candidate identity, membership, history, or source policy failed closed."""


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise CandidateError(message)


def _identity(actor: object) -> tuple[object, object, object, object]:
    if not isinstance(actor, dict):
        return (None, None, None, None)
    return (
        actor.get("id"),
        actor.get("login"),
        actor.get("type"),
        actor.get("html_url"),
    )


def _object(value: JsonValue, message: str) -> JsonObject:
    _require(isinstance(value, dict), message)
    return value


def _flatten_batches(status: JsonObject) -> dict[str, JsonObject]:
    batches = status.get("batches")
    _require(isinstance(batches, list), "Mergify status has no batches list")
    indexed: dict[str, JsonObject] = {}

    def visit(items: list[object]) -> None:
        for item in items:
            _require(isinstance(item, dict), "Mergify status contains an invalid batch")
            batch_id = item.get("id")
            _require(isinstance(batch_id, str) and batch_id, "Mergify batch has no id")
            _require(
                batch_id not in indexed,
                f"Mergify batch id is duplicated: {batch_id}",
            )
            indexed[batch_id] = item
            children = item.get("sub_batches")
            if children is not None:
                _require(
                    isinstance(children, list),
                    f"Mergify batch {batch_id} has invalid sub_batches",
                )
                visit(children)

    visit(batches)
    return indexed


def _candidate_lineage(status: JsonObject, candidate_number: int) -> tuple[str, list[int]]:
    indexed = _flatten_batches(status)
    matches = [
        batch
        for batch in indexed.values()
        if batch.get("queue_pull_request_number") == candidate_number
    ]
    _require(
        len(matches) == 1,
        f"PR #{candidate_number} is not exactly one live Mergify batch",
    )
    candidate_id = matches[0]["id"]
    pending = [candidate_id]
    visited: set[str] = set()
    source_numbers: list[int] = []
    while pending:
        batch_id = pending.pop()
        if batch_id in visited:
            continue
        batch = indexed.get(batch_id)
        _require(batch is not None, f"Mergify parent batch is missing: {batch_id}")
        visited.add(batch_id)
        pull_requests = batch.get("pull_requests")
        _require(
            isinstance(pull_requests, list),
            f"Mergify batch {batch_id} has no pull_requests list",
        )
        for pull_request in pull_requests:
            _require(
                isinstance(pull_request, dict)
                and isinstance(pull_request.get("number"), int)
                and pull_request["number"] > 0,
                f"Mergify batch {batch_id} has an invalid source pull request",
            )
            source_numbers.append(pull_request["number"])
        parent_ids = batch.get("parent_ids", [])
        _require(
            isinstance(parent_ids, list)
            and all(isinstance(parent_id, str) and parent_id for parent_id in parent_ids),
            f"Mergify batch {batch_id} has invalid parent_ids",
        )
        pending.extend(parent_ids)
    _require(source_numbers, f"Mergify batch for PR #{candidate_number} has no sources")
    _require(
        len(source_numbers) == len(set(source_numbers)),
        f"Mergify batch for PR #{candidate_number} repeats a source pull request",
    )
    return candidate_id, sorted(source_numbers)


def _candidate_snapshot(
    candidate: JsonObject,
    *,
    event_pr: JsonObject,
    repository: JsonObject,
    expected_identity: tuple[object, object, object, object],
) -> None:
    _require(
        candidate.get("number") == event_pr.get("number"),
        "GitHub returned the wrong candidate PR",
    )
    _require(candidate.get("state") == "open", "Mergify candidate PR is not open")
    _require(candidate.get("draft") is True, "Mergify candidate PR must be a draft")
    _require(
        _identity(candidate.get("user")) == expected_identity,
        "Candidate author is not the verified Mergify GitHub App",
    )
    head = candidate.get("head")
    event_head = event_pr.get("head")
    base = candidate.get("base")
    _require(
        isinstance(head, dict) and isinstance(event_head, dict) and isinstance(base, dict),
        "Candidate refs are missing",
    )
    _require(
        head.get("sha") == event_head.get("sha"),
        "Candidate head changed after the workflow event",
    )
    _require(
        head.get("ref") == event_head.get("ref")
        and isinstance(head.get("ref"), str)
        and head["ref"].startswith(QUEUE_BRANCH_PREFIX),
        "Mergify candidate has an unexpected head ref",
    )
    _require(
        base.get("ref") == "main",
        "Mergify candidate must target the real main branch",
    )
    expected_repo = (repository.get("id"), repository.get("full_name"))
    for label, repo in (("head", head.get("repo")), ("base", base.get("repo"))):
        _require(
            isinstance(repo, dict) and (repo.get("id"), repo.get("full_name")) == expected_repo,
            f"Mergify candidate {label} repository does not match this repository",
        )


def _source_fingerprint(source: JsonObject) -> tuple[object, ...]:
    head = source.get("head")
    base = source.get("base")
    base_repo = base.get("repo") if isinstance(base, dict) else None
    return (
        source.get("state"),
        source.get("draft"),
        source.get("body"),
        head.get("sha") if isinstance(head, dict) else None,
        base.get("ref") if isinstance(base, dict) else None,
        base_repo.get("id") if isinstance(base_repo, dict) else None,
        base_repo.get("full_name") if isinstance(base_repo, dict) else None,
    )


def _attest_source(
    source: JsonObject,
    *,
    number: int,
    repository: JsonObject,
    candidate_sha: str,
    github_get: GitHubGet,
) -> str:
    _require(
        source.get("number") == number,
        f"GitHub returned the wrong source PR for #{number}",
    )
    _require(source.get("state") == "open", f"source PR #{number} is not open")
    head = source.get("head")
    base = source.get("base")
    _require(
        isinstance(head, dict) and isinstance(base, dict),
        f"source PR #{number} refs are missing",
    )
    base_repo = base.get("repo")
    _require(
        base.get("ref") == "main"
        and isinstance(base_repo, dict)
        and (base_repo.get("id"), base_repo.get("full_name"))
        == (repository.get("id"), repository.get("full_name")),
        f"source PR #{number} does not target this repository's main branch",
    )
    source_sha = head.get("sha")
    _require(
        isinstance(source_sha, str) and _SHA.fullmatch(source_sha) is not None,
        f"source PR #{number} has an invalid head",
    )
    comparison = _object(
        github_get(f"/repos/{repository['full_name']}/compare/{source_sha}...{candidate_sha}"),
        "GitHub compare returned an invalid payload",
    )
    merge_base = comparison.get("merge_base_commit")
    _require(
        comparison.get("status") in {"ahead", "identical"}
        and isinstance(merge_base, dict)
        and merge_base.get("sha") == source_sha,
        f"source PR #{number} head {source_sha} is not an ancestor of the candidate",
    )
    return source_sha


def attest_candidate(
    event: JsonObject,
    *,
    github_get: GitHubGet,
    mergify_status: MergifyStatus,
    source_policy: SourcePolicy,
) -> list[dict[str, object]]:
    """Return stable source evidence after every source passes `source_policy`."""
    repository = event.get("repository")
    event_pr = event.get("pull_request")
    _require(isinstance(repository, dict), "GitHub event repository is missing")
    _require(isinstance(event_pr, dict), "GitHub event pull request is missing")
    full_name = repository.get("full_name")
    _require(
        isinstance(full_name, str) and _REPOSITORY.fullmatch(full_name) is not None,
        "GitHub event repository name is invalid",
    )
    event_head = event_pr.get("head")
    _require(isinstance(event_head, dict), "GitHub event candidate head is missing")
    event_ref = event_head.get("ref")
    _require(
        isinstance(event_ref, str) and event_ref.startswith(QUEUE_BRANCH_PREFIX),
        "Mergify candidate has an unexpected head ref",
    )
    owner, repo = full_name.split("/", 1)

    app = _object(github_get("/apps/mergify"), "GitHub App lookup is invalid")
    app_owner = app.get("owner")
    _require(
        app.get("id") == MERGIFY_APP_ID
        and app.get("slug") == "mergify"
        and isinstance(app_owner, dict)
        and app_owner.get("login") == MERGIFY_APP_OWNER,
        "Could not resolve the official Mergify GitHub App",
    )
    expected_actor = _object(
        github_get("/users/mergify%5Bbot%5D"),
        "GitHub bot lookup is invalid",
    )
    expected_identity = _identity(expected_actor)
    _require(
        expected_identity == (MERGIFY_BOT_ID, MERGIFY_BOT_LOGIN, "Bot", MERGIFY_APP_URL),
        "Could not resolve the Mergify GitHub App identity",
    )
    _require(
        _identity(event_pr.get("user")) == expected_identity,
        "Candidate author is not the verified Mergify GitHub App",
    )

    candidate_number = event_pr.get("number")
    candidate_sha = event_head.get("sha")
    _require(
        isinstance(candidate_number, int) and candidate_number > 0,
        "Candidate PR number is invalid",
    )
    _require(
        isinstance(candidate_sha, str) and _SHA.fullmatch(candidate_sha) is not None,
        "Candidate head SHA is invalid",
    )
    candidate_path = f"/repos/{full_name}/pulls/{candidate_number}"
    candidate_before = _object(
        github_get(candidate_path),
        "GitHub candidate lookup returned an invalid payload",
    )
    _candidate_snapshot(
        candidate_before,
        event_pr=event_pr,
        repository=repository,
        expected_identity=expected_identity,
    )

    first_lineage = _candidate_lineage(mergify_status(owner, repo, "main"), candidate_number)
    source_snapshots: dict[int, tuple[str, tuple[object, ...]]] = {}
    for number in first_lineage[1]:
        source = _object(
            github_get(f"/repos/{full_name}/pulls/{number}"),
            f"GitHub source PR #{number} lookup returned an invalid payload",
        )
        source_sha = _attest_source(
            source,
            number=number,
            repository=repository,
            candidate_sha=candidate_sha,
            github_get=github_get,
        )
        source_snapshots[number] = (source_sha, _source_fingerprint(source))
        source_policy(number, source)

    second_lineage = _candidate_lineage(mergify_status(owner, repo, "main"), candidate_number)
    _require(
        second_lineage == first_lineage,
        "Mergify candidate membership changed during attestation",
    )
    candidate_after = _object(
        github_get(candidate_path),
        "GitHub candidate lookup returned an invalid payload",
    )
    _candidate_snapshot(
        candidate_after,
        event_pr=event_pr,
        repository=repository,
        expected_identity=expected_identity,
    )
    for number, (expected_sha, expected_fingerprint) in source_snapshots.items():
        current = _object(
            github_get(f"/repos/{full_name}/pulls/{number}"),
            f"GitHub source PR #{number} lookup returned an invalid payload",
        )
        current_head = current.get("head")
        _require(
            isinstance(current_head, dict) and current_head.get("sha") == expected_sha,
            f"source PR #{number} head changed during attestation",
        )
        _require(
            _source_fingerprint(current) == expected_fingerprint,
            f"source PR #{number} metadata changed during attestation",
        )
    return [
        {"number": number, "head_sha": source_snapshots[number][0]} for number in first_lineage[1]
    ]
