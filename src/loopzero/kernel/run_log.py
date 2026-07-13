"""Append an idempotent lifecycle event for one logical skill run.

Called by orchestrator skills (work-issue, execute-blueprint) on exit to record
how a session went. The aggregator at `skill_stats.py` reads these logs.

Schema (one append-only JSON object per lifecycle transition):
{
  "ts": "2026-05-12T22:30:00Z",          # ISO-8601 UTC
  "run_id": "sr_<32 lowercase hex>",      # stable across sessions
  "skill": "work-issue",                  # canonical skill name
  "duration_s": 1834,                     # wall-clock seconds (optional)
  "outcome": "in_progress|merged|abandoned|blocked|resolved_no_change",
  "review_passes": 2,                     # cycles through review-gate (optional)
  "issue": 307,                           # GitHub issue number (optional)
  "pr": 315,                              # GitHub PR number (optional)
  "footgun_bypass": false,                # primary-checkout work? (optional)
  "notes": "free-form short note"         # ≤100 chars (optional)
}

When `--duration-s` is omitted, the script attempts to derive wall-clock time
from prior `agent_event.py` entries in the same session for the same skill.

Storage: `.audit/skill-runs/YYYY-MM-DD.jsonl` (gitignored, local-only).
PR deliveries hand the run ID across sessions with a hidden
`<!-- skill-run-id: ... -->` body marker.
"""

from __future__ import annotations

import argparse
import fcntl
import json
import re
import sys
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from agent_event import common_fields, repo_root

TERMINAL_OUTCOMES = {"merged", "abandoned", "blocked", "resolved_no_change"}
VALID_OUTCOMES = TERMINAL_OUTCOMES | {"in_progress"}
RUN_ID_RE = re.compile(r"^sr_[0-9a-f]{32}$")
RUN_ID_MARKER_RE = re.compile(
    r"<!--\s*skill-run-id:\s*(sr_[0-9a-f]{32})\s*-->", re.IGNORECASE
)


def load_entries(audit_dir: Path) -> list[dict[str, object]]:
    """Load the append-only stream, ignoring malformed legacy lines."""
    entries: list[dict[str, object]] = []
    if not audit_dir.exists():
        return entries
    for log_path in sorted(audit_dir.glob("*.jsonl")):
        for line in log_path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(entry, dict):
                entries.append(entry)
    return entries


def extract_run_id_marker(body: str) -> str | None:
    """Read the portable logical-run handoff marker from a PR body."""
    matches = {match.lower() for match in RUN_ID_MARKER_RE.findall(body)}
    return matches.pop() if len(matches) == 1 else None


def resolve_start_run_id(
    entries: list[dict[str, object]],
    *,
    skill: str,
    issue: int | None,
    git_branch: str,
) -> str:
    """Reuse one exact active start or mint a new opaque logical identity."""
    latest_by_run: dict[str, dict[str, object]] = {}
    for entry in entries:
        run_id = entry.get("run_id")
        if isinstance(run_id, str) and RUN_ID_RE.fullmatch(run_id):
            latest_by_run[run_id] = entry
    matches = [
        run_id
        for run_id, entry in latest_by_run.items()
        if entry.get("skill") == skill
        and entry.get("issue") == issue
        and entry.get("git_branch") == git_branch
        and entry.get("outcome") == "in_progress"
    ]
    if len(matches) > 1:
        raise ValueError("multiple active logical runs match this start identity")
    if matches:
        return matches[0]
    return f"sr_{uuid4().hex}"


def validate_transition(
    entries: list[dict[str, object]], new_entry: dict[str, object]
) -> bool:
    """Return whether to append; reject contradictory lifecycle evidence."""
    run_id = new_entry.get("run_id")
    if not isinstance(run_id, str) or not RUN_ID_RE.fullmatch(run_id):
        raise ValueError("run_id must match sr_<32 lowercase hex characters>")
    prior = [entry for entry in entries if entry.get("run_id") == run_id]
    if not prior:
        return True
    if any(entry.get("skill") != new_entry.get("skill") for entry in prior):
        raise ValueError("run_id is already owned by another skill")
    previous = prior[-1].get("outcome")
    requested = new_entry.get("outcome")
    if previous == requested:
        return False
    if previous in TERMINAL_OUTCOMES:
        raise ValueError(
            f"contradictory terminal transition for {run_id}: "
            f"{previous} -> {requested}"
        )
    if previous != "in_progress" or requested not in TERMINAL_OUTCOMES:
        raise ValueError(f"invalid lifecycle transition for {run_id}")
    return True


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
        "run_id": args.run_id,
        "session_id": fields["session_id"],
        "session_source": fields["session_source"],
        "harness": fields["harness"],
        "git_branch": fields["git_branch"],
    }
    if args.git_branch:
        entry["git_branch"] = args.git_branch

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
    parser.add_argument("--start", action="store_true", help="Start or reuse a run")
    parser.add_argument("--run-id", help="Stable logical run ID")
    parser.add_argument(
        "--outcome", choices=sorted(VALID_OUTCOMES), help="Lifecycle state"
    )
    parser.add_argument("--duration-s", type=int, help="Wall-clock seconds")
    parser.add_argument("--review-passes", type=int, help="Review-gate cycles")
    parser.add_argument("--issue", type=int, help="GitHub issue number")
    parser.add_argument("--pr", type=int, help="GitHub PR number")
    parser.add_argument("--git-branch", help="Explicit delivery branch identity")
    parser.add_argument(
        "--footgun-bypass",
        action="store_true",
        help="Set when the run used the Footgun-bypass override",
    )
    parser.add_argument("--notes", help="Free-form short note (≤100 chars)")
    args = parser.parse_args()
    root = repo_root()
    audit_dir = root / ".audit" / "skill-runs"
    audit_dir.mkdir(parents=True, exist_ok=True)
    lock_path = audit_dir / ".write.lock"
    with lock_path.open("a+", encoding="utf-8") as lock_file:
        fcntl.flock(lock_file, fcntl.LOCK_EX)
        entries = load_entries(audit_dir)

        if args.start:
            if args.outcome not in {None, "in_progress"}:
                parser.error("--start only supports the in_progress outcome")
            args.outcome = "in_progress"
            fields = common_fields()
            branch = args.git_branch or str(fields["git_branch"])
            args.git_branch = branch
            args.run_id = args.run_id or resolve_start_run_id(
                entries,
                skill=args.skill,
                issue=args.issue,
                git_branch=branch,
            )
        else:
            if args.outcome is None:
                parser.error("--outcome is required unless --start is used")
            if args.run_id is None:
                parser.error("--run-id is required for lifecycle transitions")

        entry = build_entry(args)
        day = datetime.now(UTC).strftime("%Y-%m-%d")
        log_path = audit_dir / f"{day}.jsonl"
        should_append = validate_transition(entries, entry)
        if should_append:
            with log_path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(entry) + "\n")

    if args.start:
        print(args.run_id)
    else:
        action = "Logged" if should_append else "Already logged"
        print(f"{action} {args.skill} → {args.outcome} ({log_path})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
