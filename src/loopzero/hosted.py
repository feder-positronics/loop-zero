"""Trusted Actions entry point for source and attested Mergify candidate eligibility."""
from __future__ import annotations

import json
import os
import sys
from collections.abc import Callable
from contextlib import suppress
from pathlib import Path

from loopzero import candidate as candidate_mod
from loopzero import config, eligibility, github, mergify
from loopzero.types import Config, LoopZeroError


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
    if isinstance(value, bool) or not str(value).isdigit() or int(value) < 1:
        raise LoopZeroError("refresh requires a positive PR number")
    return int(value)


def main() -> int:
    # The workflow checks out its trusted default branch, without persisted credentials.
    try:
        cfg = config.load("workflow.toml")
    except config.ConfigError as exc:
        print(exc, file=sys.stderr)
        return 1
    with github.api_error_diagnostics():
        return _run(cfg)


def _attest(cfg: Config, event: dict) -> None:
    def policy(source_number: int, source: dict) -> None:
        result = eligibility.evaluate(cfg.repo, source_number, source["head"]["sha"],
                                      cfg.base_branch, cfg.review_publishers)
        if not result.ready:
            raise LoopZeroError(f"source #{source_number}: " + "; ".join(result.reasons))

    candidate_mod.attest_candidate(
        event,
        github_get=lambda path: github.api_get(path.lstrip("/")),
        mergify_status=lambda owner, repo, branch: mergify.api_get(
            f"{owner}/{repo}", f"/merge-queue/status?branch={branch}"),
        source_policy=policy,
    )


def _source_status(result: github.Readiness, current: github.PR) -> tuple[str, str]:
    reasons = result.reasons
    if result.ready:
        if not (blocker := github.mergeability_blocker(current)):
            return "success", "Reviewed source head"
        reasons = (blocker,)
    if reasons == (github.MISSING_REVIEW_REASON,):
        return "pending", "Awaiting trusted review of current source head"
    state = "pending" if reasons == (github.MERGEABILITY_PENDING,) else "failure"
    return state, "; ".join(reasons)


def _run(cfg: Config) -> int:
    if cfg.repo != os.environ.get("GITHUB_REPOSITORY") or not cfg.review_publishers:
        raise LoopZeroError("trusted repository configuration or review publishers missing")
    event = json.loads(Path(os.environ["GITHUB_EVENT_PATH"]).read_text())
    number = event_number(event)
    pr = github.api_get(f"repos/{cfg.repo}/pulls/{number}")
    head = pr["head"]["sha"]
    candidate = (pr["head"]["ref"].startswith("mergify/merge-queue/")
                 or pr["user"]["login"] == "mergify[bot]")
    if not candidate and pr.get("merged") is True:
        return 0  # A landed source needs no new verdict; a late refresh must not turn it red.
    if candidate:
        event_pr = event.get("pull_request")
        if not isinstance(event_pr, dict):
            raise LoopZeroError("candidate eligibility requires a pull_request_target "
                                "creation or head-update event")
        if event.get("action") not in {"opened", "synchronize"}:
            print("ignored candidate metadata event")
            return 0
        event_head = event_pr.get("head")
        if not isinstance(event_head, dict) or event_head.get("sha") != head:
            raise LoopZeroError("candidate event head is no longer current")
    def publish(state: str, message: str) -> None:
        github.commit_status(cfg.repo, head, eligibility.CONTEXT, state, message[:140])
    publish("pending", "Refreshing live source review evidence")
    try:
        if candidate:
            _attest(cfg, event)
        else:
            result = eligibility.evaluate(cfg.repo, number, head, cfg.base_branch, cfg.review_publishers)
        current = github.pr_view(cfg.repo, number)
        if current.head_sha != head:
            raise LoopZeroError("head changed before publication; refresh eligibility")
        state, message = (("success", "All attested candidate sources are reviewed")
                          if candidate else _source_status(result, current))
    except Exception as exc:
        _mark_refresh_failed(publish, exc)
        raise
    _publish_verdict(cfg.repo, head, state, message[:140])
    print(message)
    return 1 if state == "failure" else 0


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


def _mark_refresh_failed(publish: Callable[[str, str], None], exc: Exception) -> None:
    if not isinstance(exc, github.StatusLimitReached):
        with suppress(Exception):
            publish("failure", "Eligibility refresh failed; inspect workflow logs")


if __name__ == "__main__":
    raise SystemExit(main())
