"""Trusted Actions entry point for source and attested Mergify candidate eligibility."""
from __future__ import annotations

import json
import os
import sys
import time
from contextlib import suppress
from dataclasses import replace
from pathlib import Path
from urllib.parse import quote

from loopzero import candidate as candidate_mod
from loopzero import config, eligibility, github, mergify
from loopzero._proc import Terminated, unwind_on_sigterm
from loopzero.types import Config, LoopZeroError


class _SourcePolicyRejection(candidate_mod.CandidateError):
    """An expected hosted policy rejection, eligible for the landing-race check."""


def event_number(event: dict) -> int:
    """Use only GitHub event association or an explicit refresh input, never PR text."""
    if "pull_request" in event:
        value = event["pull_request"].get("number")
    elif "workflow_run" in event:
        prs = event["workflow_run"].get("pull_requests", [])
        if len(prs) != 1:
            raise LoopZeroError("refresh event needs exactly one associated PR; use workflow_dispatch")
        value = prs[0].get("number")
    else:
        value = event.get("inputs", {}).get("pr", "")
    if not isinstance(value, bool) and str(value).isdigit() and (number := int(value)) > 0:
        return number
    raise LoopZeroError("refresh requires a positive PR number")


def main() -> int:
    # The workflow checks out its trusted default branch, without persisted credentials.
    try:
        cfg = config.load("workflow.toml")
    except config.ConfigError as exc:
        print(exc, file=sys.stderr)
        return 1
    try:
        with unwind_on_sigterm(), github.api_error_diagnostics():
            return _run(cfg)
    except (github.GhUnavailable, github.ProcTimeout) as exc:
        print(f"Eligibility unconfirmed before pending status publication: {exc}", file=sys.stderr)
        return 1


def _candidate_event(event: dict, head: str) -> bool:
    event_pr = event.get("pull_request")
    if not isinstance(event_pr, dict):
        raise LoopZeroError("candidate eligibility requires a pull_request_target "
                            "creation or head-update event")
    if event.get("action") not in {"opened", "synchronize"}:
        print("ignored candidate metadata event")
        return False
    event_head = event_pr.get("head")
    if not isinstance(event_head, dict) or event_head.get("sha") != head:
        raise LoopZeroError("candidate event head is no longer current")
    return True


def _source_status(result: github.Readiness, current: github.PR) -> tuple[str, str]:
    reasons = result.reasons
    if result.ready:
        if not (blocker := github.mergeability_blocker(current)):
            return "success", "Reviewed source head"
        reasons = (blocker,)
    if reasons and set(reasons) <= {github.MISSING_REVIEW_REASON, github.MERGEABILITY_PENDING}:
        if github.MISSING_REVIEW_REASON in reasons:
            return "pending", "Awaiting trusted review of current source head"
        return "pending", github.MERGEABILITY_PENDING
    return "failure", "; ".join(reasons)


def _check_candidate_source(cfg: Config, source_number: int, source: dict) -> None:
    original = github.pr_from_rest(source)
    snapshot = eligibility.source_snapshot(cfg.repo, source_number, original.head_sha, original)
    if replace(original, mergeable=snapshot.mergeable, merge_state=snapshot.merge_state) != snapshot:
        raise candidate_mod.CandidateRaceError(f"source PR #{source_number} changed during mergeability polling")
    result = eligibility.evaluate(cfg.repo, source_number, original.head_sha,
                                  cfg.base_branch, cfg.review_publishers, snapshot=snapshot)
    if result.reasons == (github.MERGEABILITY_PENDING,):
        raise github.GhUnavailable(("GitHub API request",), f"source #{source_number}: {github.MERGEABILITY_PENDING}")
    if not result.ready:
        raise _SourcePolicyRejection(f"source #{source_number}: " + "; ".join(result.reasons))


def _run(cfg: Config) -> int:
    if cfg.repo != os.environ.get("GITHUB_REPOSITORY") or not cfg.review_publishers:
        raise LoopZeroError("trusted repository configuration or review publishers missing")
    event = json.loads(Path(os.environ["GITHUB_EVENT_PATH"]).read_text())
    number = event_number(event)
    pr = github.api_get(f"repos/{cfg.repo}/pulls/{number}")
    head = pr["head"]["sha"]
    candidate = (pr["head"]["ref"].startswith("mergify/merge-queue/")
                 or pr["user"]["login"] == "mergify[bot]")
    active = _candidate_event(event, head) if candidate else pr.get("merged") is not True
    if not active:
        return 0  # A landed source needs no new verdict; a late refresh must not turn it red.
    github.commit_status(cfg.repo, head, eligibility.CONTEXT, "pending",
                         "Refreshing live source review evidence")
    snapshot = None
    try:
        if candidate:
            def policy(source_number: int, source: dict) -> None:
                _check_candidate_source(cfg, source_number, source)
            candidate_mod.attest_candidate(
                event, base_branch=cfg.base_branch,
                github_get=lambda path: github.api_get(path.lstrip("/")),
                mergify_status=lambda owner, repo, branch: mergify.api_get(
                    f"{owner}/{repo}", f"/merge-queue/status?branch={quote(branch, safe='')}"),
                source_policy=policy,
            )
        else:
            snapshot = eligibility.source_snapshot(cfg.repo, number, head, github.pr_from_rest(pr))
            result = eligibility.evaluate(cfg.repo, number, head, cfg.base_branch, cfg.review_publishers,
                                          snapshot=snapshot)
        current = _publication_snapshot(cfg.repo, number, head, snapshot)
        if current is None:
            return 0
        state, message = (("success", "All attested candidate sources are reviewed") if candidate
                          else _source_status(result, current))
    except (github.GhUnavailable, github.ProcTimeout, mergify.MergifyUnavailable) as exc:
        print(f"Eligibility pending: evidence unavailable ({exc}); rerun this workflow", file=sys.stderr)
        return 1
    except _SourcePolicyRejection as exc:
        state, message = "failure", str(exc)
    except (Exception, Terminated) as exc:
        _mark_refresh_failed(cfg.repo, head, exc)
        raise
    _publish_verdict(cfg.repo, head, state, message[:140])
    print(message)
    return 0  # A published "not eligible" verdict is the result, not a workflow failure.


def _publication_snapshot(
    repo: str, number: int, head: str, snapshot: github.PR | None,
) -> github.PR | None:
    current = github.pr_view(repo, number)
    if current.head_sha != head:
        raise LoopZeroError("head changed before publication; refresh eligibility")
    if snapshot is not None:
        if current.state == "MERGED":
            return None
        if eligibility.source_fingerprint(current) != eligibility.source_fingerprint(snapshot):
            raise LoopZeroError("source metadata changed before publication; refresh eligibility")
    return current


def _publish_verdict(repo: str, head: str, state: str, description: str) -> None:
    context = eligibility.CONTEXT
    try:
        github.commit_status(repo, head, context, state, description)
    except github.StatusLimitReached:
        raise
    except (github.GhError, github.ProcTimeout, OSError) as exc:
        with suppress(github.GhError, github.ProcTimeout, OSError):
            if github.status_matches(repo, head, context, state, description):
                return
        raise LoopZeroError(f"Could not confirm eligibility status publication: {exc}") from exc


def _mark_refresh_failed(repo: str, head: str, exc: Exception | Terminated) -> None:
    if isinstance(exc, github.StatusLimitReached):
        return
    with suppress(Exception):
        if isinstance(exc, Terminated):
            timeout = 2.0
            with github.bounded_reads(time.monotonic() + timeout):
                github.commit_status(repo, head, eligibility.CONTEXT, "failure",
                                     "Eligibility refresh interrupted", timeout=timeout)
        else:
            github.commit_status(repo, head, eligibility.CONTEXT, "failure",
                                 "Eligibility refresh failed; inspect workflow logs")


if __name__ == "__main__":
    raise SystemExit(main())
