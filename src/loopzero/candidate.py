"""Attest a live Mergify candidate around a caller-owned source policy.

The caller owns API transport and source eligibility. This module only binds an
authenticated temporary candidate to stable, current source PR heads without
checking out or executing candidate code.
"""

from __future__ import annotations

import base64
import contextlib
import os
import re
import signal
import subprocess
import tempfile
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

MERGIFY_APP_ID = 10562
MERGIFY_APP_OWNER = "Mergifyio"
MERGIFY_APP_URL = "https://github.com/apps/mergify"
MERGIFY_BOT_ID = 37929162
MERGIFY_BOT_LOGIN = "mergify[bot]"
QUEUE_BRANCH_PREFIX = "mergify/merge-queue/"

_REPOSITORY = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
_SHA = re.compile(r"^[0-9a-f]{40}$")
_TREE_SECONDS = 120
_TREE_BYTES = 256 * 1024 * 1024
_TREE_OUTPUT = 8 * 1024 * 1024
_TREE_OBJECTS = 100_000
_TREE_DEPTH = 200  # IntelFlo main at depth 200: ~16k objects, 29 MiB (full: 147k)
_PASS_ENV = ("HTTPS_PROXY", "https_proxy", "NO_PROXY", "no_proxy", "SSL_CERT_FILE", "GIT_SSL_CAINFO")

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


def _flatten_batches(status: object) -> dict[str, JsonObject]:
    _require(isinstance(status, dict), "Mergify status is not an object")
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


def _candidate_lineage(status: JsonObject, candidate_number: int) -> tuple[str, list[int], list[str]]:
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
    missing: list[str] = []
    while pending:
        batch_id = pending.pop()
        if batch_id in visited:
            continue
        batch = indexed.get(batch_id)
        visited.add(batch_id)
        if batch is None:
            missing.append(batch_id)
            continue
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
    return candidate_id, sorted(source_numbers), sorted(missing)


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


def _verify_tree(repo: str, main: str, candidate: str, sources: list[str]) -> None:
    """Reconstruct only first-parent, two-parent integrations; never execute objects."""
    from loopzero import github

    _require(len(sources) <= 32 and len(set(sources)) == len(sources),
             "ambiguous or oversized source set")
    token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
    if not token:
        try:
            token = github.auth_token()
        except github.GhError:
            raise CandidateError("GitHub fetch authentication unavailable") from None
    with tempfile.TemporaryDirectory(prefix="loopzero-candidate-") as directory:
        root = Path(directory)
        env = {name: os.environ[name] for name in _PASS_ENV if name in os.environ} | {
            "PATH": os.environ.get("PATH", os.defpath), "HOME": directory,
            "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_ATTR_NOSYSTEM": "1",
            "GIT_TERMINAL_PROMPT": "0", "GIT_NO_REPLACE_OBJECTS": "1",
            "GIT_AUTHOR_NAME": "loopzero", "GIT_AUTHOR_EMAIL": "loopzero@localhost",
            "GIT_COMMITTER_NAME": "loopzero", "GIT_COMMITTER_EMAIL": "loopzero@localhost",
            "GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "http.extraHeader",
            "GIT_CONFIG_VALUE_0": "Authorization: Basic " + base64.b64encode(
                f"x-access-token:{token}".encode()).decode(),
        }
        deadline = time.monotonic() + _TREE_SECONDS

        def git(*args: str) -> str:
            with (root / "output").open("w+b") as output:
                process = subprocess.Popen(
                    ["prlimit", f"--fsize={_TREE_BYTES}", "--",
                     "git", "-c", "core.hooksPath=/dev/null", "-c", "gc.auto=0",
                     "-c", "maintenance.auto=false", *args], cwd=root, env=env,
                    stdin=subprocess.DEVNULL, stdout=output, stderr=subprocess.DEVNULL,
                    start_new_session=True,
                )
                def store_size() -> int:  # Git renames temporaries: skip files that vanish
                    total = 0
                    for folder, _, names in os.walk(root):
                        for name in names:
                            with contextlib.suppress(FileNotFoundError):
                                total += os.lstat(os.path.join(folder, name)).st_size
                    return total

                try:
                    while process.poll() is None:
                        _require(store_size() <= _TREE_BYTES, "Git object/size bound exceeded")
                        _require(time.monotonic() < deadline, "Git time bound exceeded")
                        time.sleep(0.02)
                    _require(process.returncode == 0,
                             f"git {args[0]} exited {process.returncode}: objects unavailable, "
                             "resource bound hit, or integration conflict")
                    _require(store_size() <= _TREE_BYTES, "Git object/size bound exceeded")
                    output.seek(0)
                    result = output.read(_TREE_OUTPUT + 1)
                    _require(len(result) <= _TREE_OUTPUT, "Git object/output bound exceeded")
                    return result.decode().strip()
                finally:
                    if process.poll() is None:
                        os.killpg(process.pid, signal.SIGKILL)
                    process.wait()

        git("init", "--bare", "--template=", ".")
        heads = [main, candidate, *sources]
        git("fetch", "--no-tags", "--no-recurse-submodules", "--no-write-fetch-head",
            f"--depth={_TREE_DEPTH}",
            f"https://github.com/{repo}.git", *heads)
        objects = git("rev-list", "--objects", "--no-object-names", *heads).splitlines()
        _require(len(objects) <= _TREE_OBJECTS, "Git object count bound exceeded")
        graph = {row[0]: row[1:] for line in git(
            "rev-list", "--parents", candidate).splitlines() if (row := line.split())}
        pending, reverse_order = set(sources), []
        cursor = candidate
        while cursor in graph and pending:
            parents = graph[cursor]
            if cursor in pending:
                pending.remove(cursor)
                reverse_order.append(cursor)
            if len(parents) > 1:
                _require(len(parents) == 2, "ambiguous candidate integration topology")
                if parents[1] in pending:
                    pending.remove(parents[1])
                    reverse_order.append(parents[1])
            cursor = parents[0] if parents else ""
        _require(not pending, "ambiguous source order in candidate topology")
        integrated = main
        for source in reversed(reverse_order):
            tree = git("merge-tree", "--write-tree", integrated, source)
            _require(_SHA.fullmatch(tree) is not None, "invalid integration tree")
            integrated = git("commit-tree", tree, "-p", integrated, "-p", source,
                             "-m", "Candidate tree attestation")
        _require(git("rev-parse", f"{integrated}^{{tree}}") ==
                 git("rev-parse", f"{candidate}^{{tree}}"), "candidate tree mismatch")


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
    _require(
        event.get("action") in {"opened", "synchronize"},
        "Candidate attestation requires its creation or head-update event",
    )
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
    _require(
        _identity(event.get("sender")) == expected_identity,
        "Candidate head event was not sent by the verified Mergify GitHub App",
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
    main_path = f"/repos/{full_name}/git/ref/heads/main"
    main_before = github_get(main_path) if first_lineage[2] else None
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

    if first_lineage[2]:
        try:
            main_ref = _object(main_before, "main ref unavailable")
            main_sha = _object(main_ref.get("object"), "main object unavailable").get("sha")
            _require(isinstance(main_sha, str) and _SHA.fullmatch(main_sha) is not None,
                     "invalid main head")
            _verify_tree(full_name, main_sha, candidate_sha,
                         [snapshot[0] for snapshot in source_snapshots.values()])
            _require(github_get(main_path) == main_before, "main changed during attestation")
        except (CandidateError, OSError, ValueError) as exc:
            raise CandidateError(
                f"Missing Mergify parent batches {', '.join(first_lineage[2])}: {exc}"
            ) from None

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
