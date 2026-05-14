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

When `--duration-s` is omitted, the script attempts to derive wall-clock time
from prior `agent_event.py` entries in the same session for the same skill.

Storage: `.audit/skill-runs/YYYY-MM-DD.jsonl` (gitignored, local-only).
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

from agent_event import common_fields, repo_root

VALID_OUTCOMES = {"merged", "abandoned", "blocked", "in_progress"}


def parse_ts(raw: str) -> datetime | None:
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None


def derive_duration_s(
    root: Path, skill: str, session_id: str, end_ts: datetime
) -> int | None:
    audit_dir = root / ".audit" / "agent-events"
    if not audit_dir.exists():
        return None

    start_ts: datetime | None = None
    for log_path in sorted(audit_dir.glob("*.jsonl")):
        for line in log_path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if event.get("skill") != skill or event.get("session_id") != session_id:
                continue
            event_ts_raw = event.get("ts")
            if not isinstance(event_ts_raw, str):
                continue
            event_ts = parse_ts(event_ts_raw)
            if event_ts is None or event_ts > end_ts:
                continue
            if start_ts is None or event_ts < start_ts:
                start_ts = event_ts

    if start_ts is None:
        return None
    return max(0, int((end_ts - start_ts).total_seconds()))


def build_entry(args: argparse.Namespace) -> dict[str, object]:
    fields = common_fields()
    entry: dict[str, object] = {
        "ts": fields["ts"],
        "skill": args.skill,
        "outcome": args.outcome,
        "session_id": fields["session_id"],
        "session_source": fields["session_source"],
        "harness": fields["harness"],
        "git_branch": fields["git_branch"],
    }

    if args.duration_s is not None:
        entry["duration_s"] = args.duration_s
    else:
        end_ts = parse_ts(str(fields["ts"]))
        if end_ts is not None:
            derived_duration = derive_duration_s(
                repo_root(), args.skill, str(fields["session_id"]), end_ts
            )
            if derived_duration is not None:
                entry["duration_s"] = derived_duration

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
    return entry


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

    entry = build_entry(args)

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
