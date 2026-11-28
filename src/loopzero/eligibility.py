"""Source-head review evidence shared by local readiness and trusted hosted callers.

Run the module only from trusted base code/config; it never checks out a PR.
Hosted callers must serialize refreshes per PR and invalidate before review edits.
"""
from __future__ import annotations

import argparse
import re
import sys
from collections.abc import Callable
from pathlib import Path
from time import sleep

from loopzero import candidate, config, github, runners
from loopzero.types import LoopZeroError, is_sha

REVIEW_MARKER_RE = runners.REVIEW_MARKER_RE
CONTEXT = "Loop-zero Eligibility"


def leading_marker(body: str) -> re.Match[str] | None:
    """The review marker opening the publisher's leading machine block; prose never counts."""
    block, span = runners.leading_review_block(body.removeprefix(github.BODY_BLOCKERS) if isinstance(body, str) else "")
    if not block or len(REVIEW_MARKER_RE.findall(span)) != 1:
        return None
    return REVIEW_MARKER_RE.fullmatch(block[0])


def review_envelopes(body: str) -> list[dict]:
    """Decode only the publisher's raw-output block, which ends the body; quotes in findings come earlier."""
    if not (match := re.search(r"<details><summary>Raw reviewer output</summary>\s*```\s*\n((?:(?!\n```).)*)"
                                r"\n```\s*</details>\s*\Z", body, re.DOTALL)):
        return []
    try:
        envelopes = runners._read_envelopes(match[1].strip())
    except (ValueError, TypeError):
        return []
    return envelopes if all(isinstance(item, dict) for item in envelopes) else []


def review_projection(review: dict, head: str, repo: str, number: int):
    """Only the anchored leading machine block; model prose/fences are never searched."""
    body = review.get("body") or ""
    block, span = runners.leading_review_block(body.removeprefix(github.BODY_BLOCKERS))
    positions = [i for i, line in enumerate(block) if line.lstrip().startswith("<!-- loopzero:codex-evidence")]
    # Retired pair markers must never fall back to ordinary review evidence.
    if any("source=pair" in marker[0] for marker in REVIEW_MARKER_RE.finditer(span)):
        return True, None
    if not positions:
        return False, None
    try:
        if (positions != [1] or len(span.encode("utf-8")) > runners.CODEX_HEADER_LIMIT
                or (len(block) > 2 and (len(block) != 3 or not re.fullmatch(
                    r"<!-- loopzero:adjudication v=1 replaces=[0-9]+(?:,[0-9]+)* target=" + head + r" -->", block[2])))):
            return True, None
        marker = REVIEW_MARKER_RE.fullmatch(block[0])
        evidence = runners.decode_codex_evidence(block[1])
        if (not marker or marker[1] != head or marker[2] != evidence["kind"]
                or evidence["repo"] != repo or evidence["pr"] != number
                or evidence["head"] != head or not isinstance(user := review.get("user"), dict)
                or evidence["publisher"] != user.get("login")  # exact, as recorded in the evidence
                or review.get("commit_id") != head or not evidence["complete"]):
            return True, None
        return True, evidence
    except (ValueError, TypeError, KeyError):
        return True, None


def review_login(row: dict) -> str:
    """The casefolded GitHub login of a REST review or comment row; empty when malformed."""
    user = row.get("user")
    login = user.get("login") if isinstance(user, dict) else None
    return login.casefold() if isinstance(login, str) else ""


def eligible_review(reviews: list[dict], head: str, publishers: tuple[str, ...], *,
                    repo: str = "", number: int = 0) -> dict | None:
    """Latest trusted evidence on this exact head; revocation cannot revive older evidence."""
    trusted = {login.casefold() for login in publishers}
    latest = next((r for r in reversed(reviews)
                   if review_login(r) in trusted
                   and r.get("commit_id") == head and "loopzero:review" in (r.get("body") or "")), None)
    if latest is None or latest.get("state") not in {"APPROVED", "COMMENTED", "CHANGES_REQUESTED"}:
        return None
    body = latest.get("body") or ""
    marker = leading_marker(body)
    if not marker or marker.group(1) != head or not is_sha(head):
        return None
    verdict = latest["state"] != "COMMENTED" or published_verdict(body, head, marker[2])
    present, evidence = review_projection(latest, head, repo, number)
    if present and (evidence is None or any(call["fatal"] != "absent" for call in evidence["calls"])):
        return None
    envelopes = review_envelopes(body)
    if not present and any(runners.codex_fatal_startup(item) for item in envelopes):
        return None
    return latest if verdict else None


def _verified_merge(git: Callable[..., str], merge: str, first: str, second: str,
                    base: str, *, clean: bool) -> bool:
    git("merge-base", "--is-ancestor", second, base)
    merge_bases = git("merge-base", "--all", first, second).splitlines()
    # A shallow root outside the known merge bases could hide another base.
    if git("rev-list", "--max-parents=0", first, second, "--not", *merge_bases):
        return False
    return not clean or git("merge-tree", "--write-tree", first, second) == git("rev-parse", f"{merge}^{{tree}}")


def _verified_base_chain(git: Callable[..., str], reviewed: str, head: str, base: str, *,
                         clean: bool, conflict_only: bool) -> bool:
    cursor, conflict = head, False
    row_lengths = {3} if clean or conflict_only else {2, 3}
    for row in git("rev-list", "--first-parent", "--parents", f"{reviewed}..{head}").splitlines():
        commits = row.split()
        if len(commits) not in row_lengths or commits[0] != cursor:
            return False
        cursor = commits[1]
        if len(commits) == 2:
            continue
        if not _verified_merge(git, *commits, base, clean=clean):
            return False
        if conflict_only and not conflict:
            conflict = git("merge-tree", "--write-tree", *commits[1:], merge_conflict_status=True) == "1"
    return cursor == reviewed and (conflict or not conflict_only)


def base_merge_material(
    repo: str, number: int, reviewed: str, head: str, base_branch: str, *, clean: bool, conflict_only: bool = False,
) -> bytes | None:
    """Return a verified delta patch, or empty bytes for proven clean inheritance."""
    try:
        pr = github.api_get(f"repos/{repo}/pulls/{number}")
        base = pr["base"]["sha"]
        if pr["head"]["sha"] != head or pr["base"]["ref"] != base_branch or not is_sha(base):
            return None
        with candidate.fetched_git(repo, [reviewed, head, base]) as git:
            git("merge-base", "--is-ancestor", reviewed, head)
            if not _verified_base_chain(git, reviewed, head, base, clean=clean, conflict_only=conflict_only):
                return None
            if clean:
                return b""
            patch = git("log", "--first-parent", "--reverse", "--remerge-diff", "-p", "--format=",
                        "--no-color", "--no-ext-diff", "--no-textconv", "--text",
                        "--ignore-submodules=none", "--submodule=short",
                        f"{reviewed}..{head}", preserve_newlines=True)
            return patch.encode("utf-8", errors="surrogateescape")
    except (github.GhError, github.ProcTimeout, candidate.FetchFailed):
        raise
    except (LoopZeroError, KeyError, OSError, TypeError, ValueError):
        return None


def published_verdict(body: str, head: str, kind: str) -> str:
    """Read the publisher's complete summary before any model prose."""
    body = body.removeprefix(github.BODY_BLOCKERS)
    _, machine_block = runners.leading_review_block(body)
    lines = filter(None, body[len(machine_block):].splitlines())
    summary = next(lines, "")
    if summary.startswith(("Reposted from runner session: ", "Reposted from runner sessions: ")):
        summary = next(lines, "")
    pattern = (rf"loopzero {kind} review by (?:claude|codex) on `{head}`(?:, (?:[0-9]+\.[0-9]s|unknown))?: "
               r"\*\*(approve|request_changes)\*\* \([0-9]+ critical, [0-9]+ important, [0-9]+ suggestion\)")
    return match[1] if (match := re.fullmatch(pattern, summary)) else ""


def approved_delta(reviews: list[dict], head: str, publishers: tuple[str, ...], *, repo: str, number: int) -> bool:
    review = eligible_review(reviews, head, publishers, repo=repo, number=number)
    return bool(review and review["state"] in {"APPROVED", "COMMENTED"}
                and published_verdict(review["body"], head, "delta") == "approve")


def body_blocker(review: dict | None) -> str | None:
    if review and review["body"].startswith(github.BODY_BLOCKERS):
        return "blocking findings in the review body need fixes"
    return None


def readiness_review(reviews: list[dict], head: str, publishers: tuple[str, ...], *,
                     repo: str, number: int, base_branch: str) -> dict | None:
    """The review that covers or explicitly blocks this head."""
    trusted = {login.casefold() for login in publishers}
    marked = [r for r in reviews if review_login(r) in trusted
              and "loopzero:review" in (r.get("body") or "")]
    if not marked or any(r.get("commit_id") == head for r in marked):
        return eligible_review(marked, head, publishers, repo=repo, number=number)
    latest = marked[-1]
    marker = leading_marker(latest["body"])
    if not marker:
        return None
    reviewed = marker[1]
    if eligible_review(marked, reviewed, publishers, repo=repo, number=number) is not latest:
        return None
    if marker[2] == "primary" and not body_blocker(latest):
        verdict = published_verdict(latest["body"], reviewed, "primary")
        state = latest.get("state")
        if verdict == "request_changes" and state in {"CHANGES_REQUESTED", "COMMENTED"}:
            if github.open_blocking_findings(repo, number, reviewed, publishers):
                return None
        elif not (verdict == "approve" and state in {"APPROVED", "COMMENTED"}):
            return None
    if base_merge_material(repo, number, reviewed, head, base_branch, clean=True) is None:
        return None
    return latest


def source_fingerprint(pr: github.PR) -> tuple[object, ...]:
    return (pr.number, pr.head_sha, pr.base_ref, pr.state, pr.mergeable == "CONFLICTING",
            github.merge_hold(pr.number, pr.labels))


def source_snapshot(repo: str, number: int, head: str, pr: github.PR | None = None) -> github.PR:
    pr = pr if pr is not None else github.pr_view(repo, number)
    for delay in (2, 4, 8):
        if pr.head_sha != head or github.mergeability_blocker(pr) != github.MERGEABILITY_PENDING:
            break
        sleep(delay)
        pr = github.pr_view(repo, number)
    return pr


def evaluate(repo: str, number: int, head: str, base: str,
             publishers: tuple[str, ...], *, snapshot: github.PR | None = None) -> github.Readiness:
    """Evaluate live review evidence. Callers supplying a snapshot own the final source check."""
    if not publishers or not is_sha(head):
        raise LoopZeroError("eligibility requires trusted publishers and a full source SHA")
    pr = snapshot if snapshot is not None else source_snapshot(repo, number, head)
    if pr.number != number or pr.head_sha != head:
        return github.Readiness(False, ("source head changed; refresh eligibility",))
    if hold := github.merge_hold(pr.number, pr.labels):
        return github.Readiness(False, (hold,))
    reviews = github.paged(f"repos/{repo}/pulls/{number}/reviews")
    review = readiness_review(reviews, head, publishers, repo=repo, number=number, base_branch=base)
    result = github.readiness(repo, pr, base, (), head if review else None, allow_behind=True,
                              ancestor_review=review is not None and review["commit_id"] != head,
                              review_blocker=body_blocker(review), publishers=publishers)
    current = github.pr_view(repo, number) if snapshot is None else pr
    if snapshot is None and source_fingerprint(current) != source_fingerprint(pr):
        return github.Readiness(False, ("source changed during eligibility refresh",))
    if snapshot is None and result.ready and (blocker := github.mergeability_blocker(current)):
        return github.Readiness(False, (blocker,))
    return result


def reasons_text(reasons: tuple[str, ...]) -> str:
    """One line per status: flatten whitespace and cap each reason so later ones stay visible."""
    return "; ".join(" ".join(reason.split())[:60] for reason in reasons)


def source_status(result: github.Readiness) -> tuple[str, str]:
    if result.ready:
        return "success", "Reviewed source head"
    reasons = result.reasons
    if reasons and set(reasons) <= {github.MISSING_REVIEW_REASON, github.MERGEABILITY_PENDING}:
        if github.MISSING_REVIEW_REASON in reasons:
            return "pending", "Awaiting trusted review of current source head"
        return "pending", github.MERGEABILITY_PENDING
    return "failure", reasons_text(reasons)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True, help="trusted base workflow.toml")
    parser.add_argument("--pr", type=int, required=True)
    parser.add_argument("--head", required=True)
    parser.add_argument("--publish", action="store_true")
    args = parser.parse_args(argv)
    try:
        cfg = config.load(args.config)
        if not cfg.review_publishers:
            raise LoopZeroError("configure delivery.review_publishers in the trusted workflow.toml")
        if args.pr <= 0 or not is_sha(args.head):
            raise LoopZeroError("specify the PR number and the full 40-character source SHA")
        def publish(state: str, description: str) -> None:
            github.commit_status(cfg.repo, args.head, CONTEXT, state, description[:140])
        if args.publish and github.pr_view(cfg.repo, args.pr).state == "MERGED":
            args.publish = False  # A landed head needs no verdict.
        if args.publish:
            publish("pending", "Refreshing source review evidence")
        result = evaluate(cfg.repo, args.pr, args.head, cfg.base_branch, cfg.review_publishers)
        if args.publish:
            publish(*source_status(result))
    except (LoopZeroError, OSError) as exc:
        print(" | ".join(line.strip() for line in str(exc).splitlines() if line.strip()), file=sys.stderr)
        return 1
    print("eligible" if result.ready else "; ".join(result.reasons))
    return 0 if result.ready else 1


if __name__ == "__main__":
    raise SystemExit(main())
