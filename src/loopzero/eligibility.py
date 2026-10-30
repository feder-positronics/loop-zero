"""Source-head review evidence shared by local readiness and trusted hosted callers.

Run the module only from trusted base code/config; it never checks out a PR.
Hosted callers must serialize refreshes per PR and invalidate before review edits.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
from pathlib import Path

from loopzero import config, github, runners
from loopzero.types import LoopZeroError

REVIEW_MARKER_RE = runners.REVIEW_MARKER_RE
CONTEXT = "Loop-zero Eligibility"


# OD v40 2026-10-27 adjudication, ruling SHA256 d572145304f36bb175c30088967c3caf5d7ce0dec548e4215831126db81629cf.
# OD v58 2026-10-30: closed targets and receipts retired; only open PR9179 remains.
# Immutable incident identities in trusted code, not a reusable override or local registry.
_RECOVERY_TARGETS = {9179: "f3e8d79d49214163a395d760144002048c3c564f"}
_RECOVERY_RECEIPTS = {
    5664567936: (
        9179,
        "bd5169e399a4767c33f710523b64d22d6ed56841",
        "c27fe3cbeaac5e2bfdf7fcb958733174206723ebf706a18274557709fab39421",
        "bcd2bf92a33a074ac2880abca7af1cd0ae047d1cfa9b58c3afc325e12379bc8c",
    ),
    5667990257: (
        9179,
        "f3e8d79d49214163a395d760144002048c3c564f",
        "336f13462fd1efdbfea9f471576ad311d2313907f0abef492cb6ede4dc0d0396",
        "c8c63420dcb8a81dad25f49424cfdbd092b7461675d3c1d585be1033829a2810",
    ),
}


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


def recovery_reviews(repo: str, number: int, target: str | None, reviews: list[dict]) -> tuple[int, ...]:
    """The complete named incident must match before any spent marker is adjudicated."""
    if repo != "feder-positronics/intelflo" or _RECOVERY_TARGETS.get(number) != target:
        return ()
    expected = {key: row for key, row in _RECOVERY_RECEIPTS.items() if row[0] == number}
    matched = []
    for review in reviews:
        row = expected.get(review.get("id"))
        if not row:
            continue
        body = review.get("body") or ""
        raw, envelopes = review_envelopes(body)
        if (review.get("commit_id") == row[1]
                and (review.get("user") or {}).get("login") == "loopzero-feder-positronics[bot]"
                and hashlib.sha256(body.encode()).hexdigest() == row[2]
                and hashlib.sha256(raw.encode()).hexdigest() == row[3]
                and len(envelopes) == 1 and runners.codex_fatal_startup(envelopes[0])):
            matched.append(review["id"])
    return tuple(matched) if len(matched) == len(expected) and len(set(matched)) == len(expected) else ()


def recovery_target(repo: str, number: int) -> str | None:
    return _RECOVERY_TARGETS.get(number) if repo == "feder-positronics/intelflo" else None


def recovery_inspection(evidence: dict | None, head: str) -> bool:
    return bool(isinstance(evidence, dict) and evidence.get("version") == 1
                and evidence["head"] == head and evidence["complete"]
                and all(call["fatal"] == "absent" and call["inspection"] == "verified"
                        and all((call["reported"][key] if call["reported"][key] is not None
                                 else call.get("configured", {}).get(key)) == expected
                                for key, expected in (("model", "gpt-6-astra"), ("effort", "medium")))
                        for call in evidence["calls"]))


def review_projection(review: dict, head: str, repo: str, number: int):
    """Only the anchored leading machine block; model prose/fences are never searched."""
    body = review.get("body") or ""
    block, span = runners.leading_review_block(body)
    positions = [i for i, line in enumerate(block) if line.lstrip().startswith("<!-- loopzero:codex-evidence")]
    if not positions:
        return (True, None) if any("source=pair" in m[0] for m in REVIEW_MARKER_RE.finditer(body)) else (False, None)
    try:
        if positions != [1] or len(span.encode("utf-8")) > runners.CODEX_HEADER_LIMIT:
            return True, None
        if len(block) > 2 and (len(block) != 3 or not re.fullmatch(
                r"<!-- loopzero:adjudication v=1 replaces=[0-9]+(?:,[0-9]+)* target=" + head + r" -->", block[2])):
            return True, None
        marker = REVIEW_MARKER_RE.fullmatch(block[0])
        evidence = runners.decode_codex_evidence(block[1])
        pair_marker = "source=pair" in block[0]
        if pair_marker != (evidence.get("version") == 2):
            return True, None
        if (not marker or marker[1] != head or marker[2] != evidence["kind"]
                or evidence["repo"] != repo or evidence["pr"] != number
                or evidence["head"] != head or evidence["publisher"] != (review.get("user") or {}).get("login")
                or review.get("commit_id") != head or not evidence["complete"]):
            return True, None
        return True, evidence
    except (ValueError, TypeError, KeyError):
        return True, None


def recovery_baseline(number: int, reviews: list[dict]) -> bool:
    return number != 9179 or bool(recovery_reviews(
        "feder-positronics/intelflo", number, _RECOVERY_TARGETS[number], reviews))


def recovery_suffix(repo: str, number: int, head: str, reviews: list[dict]) -> str:
    ids = recovery_reviews(repo, number, recovery_target(repo, number), reviews)
    return ("\n<!-- loopzero:adjudication v=1 replaces=" + ",".join(map(str, ids))
            + " target=" + head + " -->") if ids else ""


def eligible_review(reviews: list[dict], head: str, publishers: tuple[str, ...], *,
                    repo: str = "", number: int = 0, publication_only: bool = False) -> dict | None:
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
    present, evidence = review_projection(latest, head, repo, number)
    if present and (evidence is None or (evidence.get("version") == 2 and not publication_only and evidence["verdict"] != "approve")
                    or (evidence.get("version") != 2 and any(call["fatal"] != "absent" for call in evidence["calls"]))):
        return None
    _, envelopes = review_envelopes(body)
    if not present and any(runners.codex_fatal_startup(item) for item in envelopes):
        return None
    if target := recovery_target(repo, number):
        if not recovery_inspection(evidence, head):
            return None
        ids = recovery_reviews(repo, number, target, reviews)
        suffix = recovery_suffix(repo, number, head, reviews)
        leading = body.splitlines()
        if (not ids or len(leading) < 3 or leading[2] != suffix.lstrip("\n")
                or not recovery_baseline(number, reviews)):
            return None
        if head != target:
            comparison = github.api_get(f"repos/{repo}/compare/{target}...{head}")
            if not isinstance(comparison, dict) or comparison.get("status") != "ahead":
                return None
            merge_base = comparison.get("merge_base_commit")
            if not isinstance(merge_base, dict) or merge_base.get("sha") != target:
                return None
    return latest if verdict else None


def reviewed_head(reviews: list[dict], head: str, publishers: tuple[str, ...], *,
                  repo: str = "", number: int = 0) -> str | None:
    return head if eligible_review(reviews, head, publishers, repo=repo, number=number) else None


def evaluate(repo: str, number: int, head: str, base: str,
             publishers: tuple[str, ...]) -> github.Readiness:
    """Re-fetch reviews and threads; no local receipts, CI identity or source execution."""
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
    result = github.readiness(repo, pr, base, (), reviewed_head(reviews, head, publishers, repo=repo, number=number),
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
