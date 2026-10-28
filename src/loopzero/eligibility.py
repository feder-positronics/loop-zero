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
# OD v41 ruling 113 Notes 1–2 add only the six proven retained startup failures.
# Immutable incident identities in trusted code, not a reusable override or local registry.
_RECOVERY_TARGETS = {9173: "f07d09b764c0a8c06929bf0f02b877a58a4c1ca4",
                     9174: "c3e1e2f2fcfbdac413aaa3b9388e8837bfe1be9b",
                     9175: "6e80b6ce590cc308792b914d8f02f5416ef77765",
                     9179: "f3e8d79d49214163a395d760144002048c3c564f",
                     9284: "1b836da3a29e31cb08a9fab19f0f567711e7ab4a",
                     9275: "aab1b62bb242f62da4ceebcb593b0725942074e0"}
_RECOVERY_RECEIPTS = {
    5664087591: (
        9173,
        "074a3e6996d2815c318787f1ea88558b9f42ac0f",
        "5199f71e0494c0316ad6ba38b1fa34c5e623257358cec7e099f3ccf589feaaf6",
        "378ef6549f1217aa08ab4b14e4ed541c487f7ddc19984e48b21c5a96d4ddbbb6",
    ),
    5665739371: (
        9173,
        "f07d09b764c0a8c06929bf0f02b877a58a4c1ca4",
        "846fd82cf586e85654012de1d7eb271a93f1a4c440b06e13f2f9f952a5f6eb67",
        "1dc00276651a2d2c2c0114a124d50c427ce7d17b540e14f341355f9e1eb02f5a",
    ),
    5664104781: (
        9174,
        "c3e1e2f2fcfbdac413aaa3b9388e8837bfe1be9b",
        "acc92bb127db9f2402ba1085480f47146039f1260c67dd433715e46d51eeb271",
        "68f0acaf342b443bb87f9beb7c94b3d40f37b29ac9ab286048daf4795da1f496",
    ),
    5664194446: (
        9175,
        "6e3f73a3a245352143cd155acbca104ee5ff6407",
        "35ceeb61e9ed58476feb2702eb9d29cd624592e518dec0b77ecf53fdac0e5b4b",
        "6193089860cb66898f1bfb6c74533144bf4632b2c8b0c7b11ed100f7fb8cce2d",
    ),
    5665844550: (
        9175,
        "6e80b6ce590cc308792b914d8f02f5416ef77765",
        "50c5e9e695519d1fb1103f46456cfe156b891b7b09b57467b1e4f7be48346eb5",
        "f83317d1d4085ad4f967d9b69222d4a08895ab4dcbe9ba0d1829d38289682212",
    ),
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
    5667628969: (
        9284,
        "b68e719fcf964951dacb8cbea9649b93316c366c",
        "66023a1fc660f0907f47ad8e851ae4234e9eaf2ac97cb41912a00a47d9e91381",
        "3f2b403f04e799a50526a10b76bc5b02a17214662a6e901f3b10f231c109355d",
    ),
    5667664997: (
        9284,
        "1b836da3a29e31cb08a9fab19f0f567711e7ab4a",
        "c5fbf52fbe7aef3e7999082eb51232cad3f86af069c5fcc586a2c8871bfb0cf7",
        "4cb0982d405fc2a95f8a7eef3a3d2924b2af11ea4c1bcab6cfa995aaa272e8e1",
    ),
    5666873014: (
        9275,
        "da55dd044369edccb4e8cf44b033396cc25cbd7e",
        "64422d33ab7989a0d20454f3c7031c5fe7168cb7138215243d53c776a5630f9f",
        "991fe6fcb60254eb4413f4403ef26b5ef4b4973386ee1c5bc4e452ce26ef0cdd",
    ),
    5667433401: (
        9275,
        "3f8c01e550641cf00ee9dc6079eae5535342aa5e",
        "359526e22837d958741b9516598e37b50a8ae1d1ef3989ddb7972be02a0efc6e",
        "030164aa4de085c653161c01d271ad43e030ea70ea2d855242935e7721e7d35d",
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
    return bool(evidence and evidence["head"] == head and evidence["complete"]
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


def recovery_baseline(number: int, reviews: list[dict]) -> bool:
    return number != 9179 or bool(recovery_reviews(
        "feder-positronics/intelflo", number, _RECOVERY_TARGETS[number], reviews))


def recovery_suffix(repo: str, number: int, head: str, reviews: list[dict]) -> str:
    ids = recovery_reviews(repo, number, recovery_target(repo, number), reviews)
    return ("\n<!-- loopzero:adjudication v=1 replaces=" + ",".join(map(str, ids))
            + " target=" + head + " -->") if ids else ""


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
    matches = list(REVIEW_MARKER_RE.finditer(body))
    if len(matches) != 1 or matches[0].group(1) != head or not re.fullmatch(r"[0-9a-f]{40}", head):
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
