"""Trusted Actions entry point for source and attested Mergify candidate eligibility."""
from __future__ import annotations

import json
import os
from contextlib import suppress
from pathlib import Path

from loopzero import candidate as candidate_mod
from loopzero import config, eligibility, github, mergify
from loopzero.types import LoopZeroError


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
    cfg = config.load("workflow.toml")
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
            ready, message = True, "All attested candidate sources are reviewed"
        else:
            result = eligibility.evaluate(cfg.repo, number, head, cfg.base_branch, cfg.review_publishers)
            ready, message = result.ready, "; ".join(result.reasons) or "Reviewed source head"
        if github.pr_view(cfg.repo, number).head_sha != head:
            raise LoopZeroError("head changed before publication; refresh eligibility")
        state = "success" if ready else "failure"
        if not candidate and not ready and result.reasons == (github.MISSING_REVIEW_REASON,):
            message = "Awaiting trusted review of current source head"
            state = "pending"
        publish(state, message)
        print(message)
        return 0 if ready or state == "pending" else 1
    except Exception:
        with suppress(Exception):
            publish("failure", "Eligibility refresh failed; inspect workflow logs")
        raise


if __name__ == "__main__":
    raise SystemExit(main())
