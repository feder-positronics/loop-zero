"""Append a JSONL entry capturing a skill-run outcome.

Called by orchestrator skills (work-issue, execute-blueprint) on exit to record
how a session went. The aggregator at `skill_stats.py` reads these logs.

Schema (one JSON object per line):
{
  "ts": "2026-05-12T22:30:00Z",          # ISO-8601 UTC
  "skill": "work-issue",                  # canonical skill name
  "duration_s": 1834,                     # wall-clock seconds (optional)
  "outcome": "merged|abandoned|blocked|in_progress",
  "review_passes": 2,                     # cycles through review-gate (optional)
  "issue": 307,                           # GitHub issue number (optional)
  "pr": 315,                              # GitHub PR number (optional)
  "footgun_bypass": false,                # primary-checkout work? (optional)
  "notes": "free-form short note"         # ≤100 chars (optional)
}

Storage: `.audit/skill-runs/YYYY-MM-DD.jsonl` (gitignored, local-only).
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

VALID_OUTCOMES = {"merged", "abandoned", "blocked", "in_progress"}


def repo_root() -> Path:
    import subprocess

    # Use --git-common-dir so skill runs from worktrees write to the main
    # repo's `.audit/skill-runs/`, not the worktree's ephemeral one.
    out = subprocess.check_output(
        ["git", "rev-parse", "--git-common-dir"], text=True
    ).strip()
    return Path(out).resolve().parent


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--skill", required=True, help="Canonical skill name")
    parser.add_argument(
        "--outcome",
        required=True,
        choices=sorted(VALID_OUTCOMES),
        help="How the run ended",
    )
    parser.add_argument("--duration-s", type=int, help="Wall-clock seconds")
    parser.add_argument("--review-passes", type=int, help="Review-gate cycles")
    parser.add_argument("--issue", type=int, help="GitHub issue number")
    parser.add_argument("--pr", type=int, help="GitHub PR number")
    parser.add_argument(
        "--footgun-bypass",
        action="store_true",
        help="Set when the run used the Footgun-bypass override",
    )
    parser.add_argument("--notes", help="Free-form short note (≤100 chars)")
    args = parser.parse_args()

    entry: dict[str, object] = {
        "ts": datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z"),
        "skill": args.skill,
        "outcome": args.outcome,
    }
    if args.duration_s is not None:
        entry["duration_s"] = args.duration_s
    if args.review_passes is not None:
        entry["review_passes"] = args.review_passes
    if args.issue is not None:
        entry["issue"] = args.issue
    if args.pr is not None:
        entry["pr"] = args.pr
    if args.footgun_bypass:
        entry["footgun_bypass"] = True
    if args.notes:
        entry["notes"] = args.notes[:100]

    root = repo_root()
    audit_dir = root / ".audit" / "skill-runs"
    audit_dir.mkdir(parents=True, exist_ok=True)
    day = datetime.now(UTC).strftime("%Y-%m-%d")
    log_path = audit_dir / f"{day}.jsonl"

    with log_path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry) + "\n")

    print(f"Logged {args.skill} → {args.outcome} ({log_path})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
