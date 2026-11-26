"""Source-head review evidence shared by local readiness and trusted hosted callers.

Run the module only from trusted base code/config; it never checks out a PR.
Hosted callers must serialize refreshes per PR and invalidate before review edits.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

from loopzero import candidate, config, github, runners
from loopzero.types import LoopZeroError

REVIEW_MARKER_RE = runners.REVIEW_MARKER_RE
CONTEXT = "Loop-zero Eligibility"


def leading_marker(body: str) -> re.Match[str] | None:
    """The review marker opening the publisher's leading machine block; prose never counts."""
    block, span = runners.leading_review_block(body if isinstance(body, str) else "")
    if not block or len(REVIEW_MARKER_RE.findall(span)) != 1:
        return None
    return REVIEW_MARKER_RE.fullmatch(block[0])


def review_envelopes(body: str) -> tuple[str, list[dict]]:
    """Decode only the publisher's raw-output block, not quoted findings/prose."""
    if not isinstance(body, str):
        return "", []
    match = re.search(r"<details><summary>Raw reviewer output</summary>\s*```\s*\n(.*?)\n```", body, re.DOTALL)
    if not match:
        return "", []
    raw = match[1].strip()
    try:
        value = json.loads(raw)
    except (ValueError, TypeError):
        return raw, []
    envelopes = value if isinstance(value, list) else [value]
    return raw, envelopes if all(isinstance(item, dict) for item in envelopes) else []


def review_projection(review: dict, head: str, repo: str, number: int):
    """Only the anchored leading machine block; model prose/fences are never searched."""
    body = review.get("body") or ""
    block, span = runners.leading_review_block(body)
    positions = [i for i, line in enumerate(block) if line.lstrip().startswith("<!-- loopzero:codex-evidence")]
    # Retired pair markers must never fall back to ordinary review evidence.
    if any("source=pair" in marker[0] for marker in REVIEW_MARKER_RE.finditer(span)):
        return True, None
    if not positions:
        return False, None
    try:
        if positions != [1] or len(span.encode("utf-8")) > runners.CODEX_HEADER_LIMIT:
            return True, None
        if len(block) > 2 and (len(block) != 3 or not re.fullmatch(
                r"<!-- loopzero:adjudication v=1 replaces=[0-9]+(?:,[0-9]+)* target=" + head + r" -->", block[2])):
            return True, None
        marker = REVIEW_MARKER_RE.fullmatch(block[0])
        evidence = runners.decode_codex_evidence(block[1])
        if (not marker or marker[1] != head or marker[2] != evidence["kind"]
                or evidence["repo"] != repo or evidence["pr"] != number
                or evidence["head"] != head or evidence["publisher"] != (review.get("user") or {}).get("login")
                or review.get("commit_id") != head or not evidence["complete"]):
            return True, None
        return True, evidence
    except (ValueError, TypeError, KeyError):
        return True, None


def eligible_review(reviews: list[dict], head: str, publishers: tuple[str, ...], *,
                    repo: str = "", number: int = 0) -> dict | None:
    """Latest trusted evidence on this exact head; revocation cannot revive older evidence."""
    trusted = {login.casefold() for login in publishers}
    latest = next((r for r in reversed(reviews)
                   if (r.get("user") or {}).get("login", "").casefold() in trusted
                   and r.get("commit_id") == head and "loopzero:review" in (r.get("body") or "")), None)
    if latest is None or latest.get("state") not in {"APPROVED", "COMMENTED", "CHANGES_REQUESTED"}:
        return None
    body = latest.get("body") or ""
    marker = leading_marker(body)
    if not marker or marker.group(1) != head or not re.fullmatch(r"[0-9a-f]{40}", head):
        return None
    verdict = latest.get("state") in {"APPROVED", "CHANGES_REQUESTED"} or any(
        f"**{value}**" in body for value in ("approve", "request_changes")
    )
    present, evidence = review_projection(latest, head, repo, number)
    if present and (evidence is None or any(call["fatal"] != "absent" for call in evidence["calls"])):
        return None
    _, envelopes = review_envelopes(body)
    if not present and any(runners.codex_fatal_startup(item) for item in envelopes):
        return None
    return latest if verdict else None


def _clean_base_merges(repo: str, number: int, reviewed: str, head: str, base_branch: str) -> bool:
    """Prove each intervening first-parent commit is an unmodified merge from the base."""
    try:
        pr = github.api_get(f"repos/{repo}/pulls/{number}")
        base = pr["base"]["sha"]
        if (pr["head"]["sha"] != head or pr["base"]["ref"] != base_branch
                or not re.fullmatch(r"[0-9a-f]{40}", base)):
            return False
        with candidate.fetched_git(repo, [reviewed, head, base]) as git:
            history = git("rev-list", "--first-parent", "--parents", f"{reviewed}..{head}")
            cursor = head
            for row in history.splitlines():
                commits = row.split()
                if len(commits) != 3 or commits[0] != cursor:
                    return False
                merge, first, second = commits
                git("merge-base", "--is-ancestor", second, base)
                merge_bases = git("merge-base", "--all", first, second).splitlines()
                # A shallow root outside the known merge bases could hide another base.
                if git("rev-list", "--max-parents=0", first, second, "--not", *merge_bases):
                    return False
                tree = git("merge-tree", "--write-tree", first, second)
                if tree != git("rev-parse", f"{merge}^{{tree}}"):
                    return False
                cursor = first
            return cursor == reviewed
    except github.GhUnavailable:
        raise
    except (candidate.CandidateError, github.GhError, KeyError, OSError, TypeError, ValueError):
        return False


def readiness_review(reviews: list[dict], head: str, publishers: tuple[str, ...], *,
                     repo: str, number: int, base_branch: str) -> dict | None:
    """An eligible review at the current head or an ancestor that covers it."""
    trusted = {login.casefold() for login in publishers}
    marked = [r for r in reviews if (r.get("user") or {}).get("login", "").casefold() in trusted
              and "loopzero:review" in (r.get("body") or "")]
    if not marked or any(r.get("commit_id") == head for r in marked):
        return eligible_review(marked, head, publishers, repo=repo, number=number)
    latest = marked[-1]
    marker = leading_marker(latest["body"])
    if not marker or marker[2] not in {"primary", "delta"}:
        return None
    reviewed = marker[1]
    if eligible_review(marked, reviewed, publishers, repo=repo, number=number) is not latest:
        return None
    if marker[2] == "primary":
        _, machine_block = runners.leading_review_block(latest["body"])
        # App reviews use COMMENTED for either verdict. Read the publisher's
        # summary before model prose, which can quote either verdict.
        summary, _, rest = latest["body"][len(machine_block):].lstrip().partition("\n")
        if summary.startswith(("Reposted from runner session: ", "Reposted from runner sessions: ")):
            summary = rest.partition("\n")[0]
        if not summary.startswith("loopzero primary review by "):
            return None
        verdict = summary.partition(": **")[2]
        state = latest.get("state")
        if verdict.startswith("request_changes** (") and state in {"CHANGES_REQUESTED", "COMMENTED"}:
            if github.open_blocking_findings(repo, number, reviewed, publishers):
                return None
        elif not (verdict.startswith("approve** (") and state in {"APPROVED", "COMMENTED"}):
            return None
    if not _clean_base_merges(repo, number, reviewed, head, base_branch):
        return None
    return latest


def evaluate(repo: str, number: int, head: str, base: str,
             publishers: tuple[str, ...]) -> github.Readiness:
    """Re-fetch reviews and threads; no local receipts, CI identity or source execution."""
    if not publishers or not re.fullmatch(r"[0-9a-f]{40}", head):
        raise LoopZeroError("eligibility requires trusted publishers and a full source SHA")
    pr = github.pr_view(repo, number)
    if pr.head_sha != head:
        return github.Readiness(False, ("source head changed; refresh eligibility",))
    if hold := github.merge_hold(pr.number, pr.labels):
        return github.Readiness(False, (hold,))
    reviews, page = [], 1
    while True:
        batch = github.api_get(f"repos/{repo}/pulls/{number}/reviews?per_page=100&page={page}")
        if not isinstance(batch, list):
            raise LoopZeroError("invalid GitHub reviews response")
        reviews.extend(batch)
        if len(batch) < 100:
            break
        page += 1
    review = readiness_review(reviews, head, publishers, repo=repo, number=number, base_branch=base)
    result = github.readiness(repo, pr, base, (), head if review else None, allow_behind=True,
                              ancestor_review=review is not None and review["commit_id"] != head,
                              publishers=publishers)
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
    try:
        cfg = config.load(args.config)
    except config.ConfigError as exc:
        print(exc, file=sys.stderr)
        return 1
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
