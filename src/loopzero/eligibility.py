"""Source-head review evidence shared by local readiness and trusted hosted callers.

Run the module only from trusted base code/config; it never checks out a PR.
Hosted callers must serialize refreshes per PR and invalidate before review edits.
"""
from __future__ import annotations

import argparse
import re
from pathlib import Path

from loopzero import config, github
from loopzero.types import LoopZeroError

REVIEW_MARKER_RE = re.compile(
    r"<!--\s*loopzero:review\s+(?:v=1\s+)?head=([0-9a-fA-F]{7,40})\s+"
    r"kind=(primary|delta)(?:\s+source=(?:model|repost))?\s*-->"
)
CONTEXT = "Loop-zero Eligibility"


def eligible_review(reviews: list[dict], head: str, publishers: tuple[str, ...]) -> dict | None:
    """Latest trusted evidence on this exact head; revocation cannot revive older evidence."""
    trusted = {login.casefold() for login in publishers}
    latest = next((r for r in reversed(reviews)
                   if (r.get("user") or {}).get("login", "").casefold() in trusted
                   and r.get("commit_id") == head and "loopzero:review" in (r.get("body") or "")), None)
    if latest is None or latest.get("state") not in {"APPROVED", "COMMENTED", "CHANGES_REQUESTED"}:
        return None
    body = latest.get("body") or ""
    matches = list(REVIEW_MARKER_RE.finditer(body))
    if len(matches) != 1 or matches[0].group(1) != head or not re.fullmatch(r"[0-9a-f]{40}", head):
        return None
    verdict = latest.get("state") in {"APPROVED", "CHANGES_REQUESTED"} or any(
        f"**{value}**" in body for value in ("approve", "request_changes")
    )
    return latest if verdict else None


def reviewed_head(reviews: list[dict], head: str, publishers: tuple[str, ...]) -> str | None:
    return head if eligible_review(reviews, head, publishers) else None


def evaluate(repo: str, number: int, head: str, base: str,
             publishers: tuple[str, ...]) -> github.Readiness:
    """Re-fetch reviews and threads; no local receipts, CI identity, ancestry or source execution."""
    if not publishers or not re.fullmatch(r"[0-9a-f]{40}", head):
        raise LoopZeroError("eligibility requires trusted publishers and a full source SHA")
    pr = github.pr_view(repo, number)
    if pr.head_sha != head:
        return github.Readiness(False, ("source head changed; refresh eligibility",))
    reviews, page = [], 1
    while True:
        batch = github.api_get(f"repos/{repo}/pulls/{number}/reviews?per_page=100&page={page}")
        if not isinstance(batch, list):
            raise LoopZeroError("invalid GitHub reviews response")
        reviews.extend(batch)
        if len(batch) < 100:
            break
        page += 1
    result = github.readiness(repo, pr, base, (), reviewed_head(reviews, head, publishers),
                              allow_behind=True)
    if github.pr_view(repo, number).head_sha != head:
        return github.Readiness(False, ("source head changed during eligibility refresh",))
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True, help="trusted base workflow.toml")
    parser.add_argument("--pr", type=int, required=True)
    parser.add_argument("--head", required=True)
    parser.add_argument("--publish", action="store_true")
    args = parser.parse_args(argv)
    cfg = config.load(args.config)
    if not cfg.review_publishers or args.pr <= 0 or not re.fullmatch(r"[0-9a-f]{40}", args.head):
        raise LoopZeroError("configure delivery.review_publishers and specify PR/full source SHA")
    def publish(state: str, description: str) -> None:
        github.commit_status(cfg.repo, args.head, CONTEXT, state, description[:140])
    if args.publish:
        publish("pending", "Refreshing source review evidence")
    result = evaluate(cfg.repo, args.pr, args.head, cfg.base_branch, cfg.review_publishers)
    if args.publish:
        publish("success" if result.ready else "failure", "; ".join(result.reasons) or "Reviewed source head")
    print("eligible" if result.ready else "; ".join(result.reasons))
    return 0 if result.ready else 1


if __name__ == "__main__":
    raise SystemExit(main())
