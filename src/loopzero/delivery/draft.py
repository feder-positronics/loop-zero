"""Source-verified draft creation, independent of review/readiness admission.

Call under the consumer's publication lease using its approved repository policy.
This module creates no review authority or readiness evidence. Formal registration
must independently verify the returned PR against its current reviewed source.
"""
from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from ..kernel.run_identity import RUN_ID_MARKER_RE
from .publish import (
    CommandRunner,
    JsonRunner,
    PublicationError,
    SecureGitRunner,
    require_active_publication_run,
    require_local_publication_prerequisites,
    require_remote_publication_prerequisites,
)


@dataclass(frozen=True)
class DraftPublicationRequest:
    title: str
    body: str
    head: str
    base: str
    expected_head: str
    expected_base: str
    expected_repository: str
    run_id: str


@dataclass(frozen=True)
class DraftPublicationResult:
    number: int
    url: str
    repository: str
    head: str
    base: str
    base_sha: str
    adopted: bool


def _validate_request(request: DraftPublicationRequest) -> None:
    if any(re.fullmatch(r"[0-9a-f]{40}", sha) is None
           for sha in (request.expected_head, request.expected_base)):
        raise PublicationError("draft source requires exact head and base SHAs")
    if re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", request.expected_repository) is None:
        raise PublicationError("draft repository identity is invalid")
    if re.fullmatch(r"sr_[0-9a-f]{32}", request.run_id) is None:
        raise PublicationError("draft run identity is invalid")
    if not request.title.strip() or not request.body.strip():
        raise PublicationError("draft objective and acceptance body are required")
    markers = RUN_ID_MARKER_RE.findall(request.body)
    if markers and markers != [request.run_id]:
        raise PublicationError("draft body identifies another or ambiguous run")
    if not request.head.strip() or not request.base.strip() or request.head == request.base:
        raise PublicationError("draft requires distinct head and base branches")


def _verify_pr(payload: object, request: DraftPublicationRequest) -> Mapping[str, object]:
    if not isinstance(payload, dict):
        raise PublicationError("draft PR response is unavailable")
    number = payload.get("number")
    if type(number) is not int or number <= 0:
        raise PublicationError("draft PR number is invalid")
    if payload.get("html_url") != f"https://github.com/{request.expected_repository}/pull/{number}":
        raise PublicationError("draft PR URL differs from repository identity")
    body = payload.get("body")
    if not isinstance(body, str) or RUN_ID_MARKER_RE.findall(body) != [request.run_id]:
        raise PublicationError("draft PR body does not identify its active run")
    if payload.get("state") != "open" or payload.get("draft") is not True:
        raise PublicationError("existing PR is not an open draft")
    for name, branch, sha in (("head", request.head, request.expected_head),
                              ("base", request.base, request.expected_base)):
        source = payload.get(name)
        repository = source.get("repo") if isinstance(source, dict) else None
        if (not isinstance(source, dict) or source.get("ref") != branch
                or source.get("sha") != sha or not isinstance(repository, dict)
                or repository.get("full_name") != request.expected_repository):
            raise PublicationError("draft PR source differs from expected repository/head/base")
    return payload


def _verify_remote(runner: JsonRunner, request: DraftPublicationRequest) -> None:
    repository = runner.run_json(["gh", "api", "--method", "GET", "repos/{owner}/{repo}"])
    if not isinstance(repository, dict) or repository.get("full_name") != request.expected_repository:
        raise PublicationError("remote repository differs from approved draft repository")
    require_remote_publication_prerequisites(runner, head=request.head,
                                             expected_head=request.expected_head)
    require_remote_publication_prerequisites(runner, head=request.base,
                                             expected_head=request.expected_base)


def ensure_draft_pr(
    runner: JsonRunner,
    request: DraftPublicationRequest,
    *,
    worktree: Path,
    authority_repo: Path,
    command_runner: CommandRunner | None = None,
) -> DraftPublicationResult:
    """Create/adopt one source-bound draft; a failed request never implies success.

    Every invocation scans all PR states before creating. An uncertain creation
    outcome therefore reconciles on re-entry instead of blindly repeating POST.
    A closed or ready PR, ambiguous response, or source drift requires a separate
    explicit action; no foreign PR is quarantined or otherwise mutated here.
    """
    _validate_request(request)
    body = request.body
    if not RUN_ID_MARKER_RE.search(body):
        body = body.rstrip() + f"\n\n<!-- skill-run-id: {request.run_id} -->\n"
    command_runner = command_runner or SecureGitRunner(worktree)

    def verify_local() -> None:
        checkout = command_runner.run(["git", "rev-parse", "--show-toplevel"]).stdout.strip()
        common = command_runner.run([
            "git", "rev-parse", "--path-format=absolute", "--git-common-dir",
        ]).stdout.strip()
        if (not checkout or not common or Path(checkout).resolve() != worktree.resolve()
                or Path(common).resolve().parent != authority_repo.resolve()):
            raise PublicationError("draft checkout does not belong to its authority repository")
        require_active_publication_run(run_id=request.run_id, worktree=worktree,
                                       authority_repo=authority_repo, branch=request.head)
        require_local_publication_prerequisites(worktree=worktree, head=request.head,
                                                expected_head=request.expected_head,
                                                runner=command_runner)

    verify_local()
    _verify_remote(runner, request)
    owner = request.expected_repository.split("/", 1)[0]
    matches = runner.run_json([
        "gh", "api", "--method", "GET", "-f", "state=all",
        "-f", f"head={owner}:{request.head}",
        "-f", "per_page=100", "repos/{owner}/{repo}/pulls",
    ])
    if not isinstance(matches, list) or len(matches) > 1:
        raise PublicationError("draft reconciliation found unavailable or ambiguous PRs")
    adopted = bool(matches)
    if adopted:
        created = _verify_pr(matches[0], request)
    else:
        created = _verify_pr(runner.run_json(
            ["gh", "api", "--method", "POST", "--input", "-", "repos/{owner}/{repo}/pulls"],
            payload={"title": request.title, "body": body, "head": request.head,
                     "base": request.base, "draft": True},
        ), request)
    number = int(created["number"])
    fresh = _verify_pr(runner.run_json([
        "gh", "api", "--method", "GET", f"repos/{{owner}}/{{repo}}/pulls/{number}",
    ]), request)
    if fresh["number"] != number:
        raise PublicationError("live draft PR identity changed")
    verify_local()
    _verify_remote(runner, request)
    return DraftPublicationResult(number=number, url=str(fresh["html_url"]),
                                  repository=request.expected_repository,
                                  head=request.expected_head, base=request.base,
                                  base_sha=request.expected_base, adopted=adopted)
