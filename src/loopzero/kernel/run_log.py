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
  "tokens_in": 1200000,                   # caller-attributed run input tokens (optional)
  "tokens_out": 45000,                    # caller-attributed run output tokens (optional)
  "tokens_cached": 1100000,               # caller-attributed run cached input (optional)
  "token_scope": "run",                  # present only with explicit run tokens
  "session_tokens_in_cumulative": 1200000, # automatic Codex session snapshot (optional)
  "session_tokens_out_cumulative": 45000,  # automatic Codex session snapshot (optional)
  "session_tokens_cached_cumulative": 1100000, # automatic Codex session snapshot
  "issue": 307,                           # GitHub issue number (optional)
  "pr": 315,                              # GitHub PR number (optional)
  "footgun_bypass": false,                # primary-checkout work? (optional)
  "notes": "free-form short note"         # ≤100 chars (optional)
}

When `--duration-s` is omitted, the script attempts to derive wall-clock time
from prior `agent_event.py` entries in the same session for the same skill and
logical run.

Storage: `.audit/skill-runs/YYYY-MM-DD.jsonl` (gitignored, local-only).
PR deliveries hand the run ID across sessions with a hidden
`<!-- skill-run-id: ... -->` body marker.
"""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import platform
import re
import sys
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from agent_event import cmd_phase, common_fields, repo_root

TERMINAL_OUTCOMES = {"merged", "abandoned", "blocked", "resolved_no_change"}
VALID_OUTCOMES = TERMINAL_OUTCOMES | {"in_progress"}
# Campaign session ceiling (#3418 P3): past this wall-clock, a session emits
# its take-up brief and terminates. Rationale is NOT token cost — orchestrator
# tokens are ~99% prompt-cached and effectively free (2026-08-10 measurement).
# The ceiling exists for context-quality drift over very long sessions and to
# bound T3 host event-store growth (marathon threads melted state.sqlite on
# 2026-08-09; upstream #4008/#5719 unfixed). Raised 8h → 24h on 2026-08-10:
# the measured 8h drain session was cheap and productive, so the shorter bound
# was over-conservative; 24h stays well under a full state.sqlite reset cycle.
# Tune from run-log + state.sqlite-growth evidence, never per-campaign.
SESSION_CEILING_S = 24 * 3600
# Stale-run reconcile is DECOUPLED from the ceiling: a genuinely dead session
# (no run-log activity AND no provider life signs) should be reaped well
# before the handoff ceiling, so the run log reflects reality within a work
# shift rather than a day. This is the idle-liveness window, not the ceiling.
STALE_RECONCILE_IDLE_S = 8 * 3600
RUN_ID_RE = re.compile(r"^sr_[0-9a-f]{32}$")
RUN_ID_MARKER_RE = re.compile(
    r"<!--\s*skill-run-id:\s*(sr_[0-9a-f]{32})\s*-->", re.IGNORECASE
)
PHASES = ("implementation", "local-validation", "review", "ci-wait", "closeout")
PHASE_SKILLS = {"work-issue", "execute-blueprint"}


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


def require_run_owner(
    entries: list[dict[str, object]],
    *,
    run_id: str,
    skill: str,
    allow_missing: bool = False,
) -> str | None:
    """Validate one existing run's immutable skill owner without appending."""
    if not RUN_ID_RE.fullmatch(run_id):
        raise ValueError("run_id must match sr_<32 lowercase hex characters>")
    rows = [entry for entry in entries if entry.get("run_id") == run_id]
    if not rows:
        if allow_missing:
            return None
        raise ValueError(f"unknown run_id: {run_id}")
    owners = {
        str(entry["skill"])
        for entry in rows
        if isinstance(entry.get("skill"), str) and entry.get("skill")
    }
    if len(owners) != 1:
        raise ValueError(f"run_id has contradictory skill owners: {run_id}")
    owner = owners.pop()
    if owner != skill:
        raise ValueError(f"run_id is already owned by {owner}")
    return owner


def load_phase_events(root: Path) -> list[dict[str, object]]:
    """Load phase evidence without rewriting malformed or legacy JSONL rows."""
    events: list[dict[str, object]] = []
    audit_dir = root / ".audit" / "agent-events"
    if not audit_dir.exists():
        return events
    for log_path in sorted(audit_dir.glob("*.jsonl")):
        for line in log_path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(event, dict):
                events.append(event)
    return events


def _phase_index(phase: object) -> int:
    if not isinstance(phase, str) or not phase.isdecimal():
        raise ValueError("phase event has an unknown phase")
    index = int(phase) - 1
    if index < 0 or index >= len(PHASES):
        raise ValueError("phase event has an unknown phase")
    return index


def _phase_history(
    events: list[dict[str, object]], *, skill: str, run_id: str
) -> tuple[int | None, int]:
    """Return active and last completed phase indices for one logical run.

    Run-scoped rows are a small state machine. Any contradictory row fails
    closed instead of inventing a duration or skipping work.
    """
    relevant = [
        event
        for event in events
        if event.get("kind") == "phase"
        and event.get("run_id") == run_id
        and event.get("skill") == skill
    ]
    active: int | None = None
    completed = -1
    for event in relevant:
        index = _phase_index(event.get("phase"))
        expected_name = PHASES[index]
        if event.get("name") not in {None, expected_name}:
            raise ValueError("phase event name does not match the fixed taxonomy")
        status = event.get("status")
        if status == "start":
            if active is not None or index != completed + 1:
                raise ValueError("contradictory phase history")
            active = index
        elif status == "complete":
            if active != index:
                raise ValueError("contradictory phase history")
            completed = index
            active = None
        else:
            raise ValueError("phase event has an unsupported status")
    return active, completed


def emit_phase_event(*, skill: str, run_id: str, phase: str, status: str) -> None:
    """Write via the existing event primitive so session diagnostics remain."""
    result = cmd_phase(
        argparse.Namespace(
            skill=skill,
            run_id=run_id,
            phase=str(PHASES.index(phase) + 1),
            name=phase,
            status=status,
            elapsed_s=None,
        )
    )
    if result != 0:
        raise RuntimeError("phase event was not written durably")


def transition_phase(*, root: Path, skill: str, run_id: str, target: str) -> bool:
    """Advance one run through exactly one allowed phase boundary.

    The current phase is completed and only its immediate successor starts.
    Repeating a current phase is an idempotent no-op; gaps and reversals fail
    closed. A recovered half-transition starts the already-authorized next
    phase without duplicating the prior completion.
    """
    if not RUN_ID_RE.fullmatch(run_id):
        raise ValueError("run_id must match sr_<32 lowercase hex characters>")
    if target not in PHASES:
        raise ValueError(f"unknown phase: {target}")

    target_index = PHASES.index(target)
    active, completed = _phase_history(
        load_phase_events(root), skill=skill, run_id=run_id
    )
    actions: list[tuple[int, str]] = []
    if active is not None:
        if target_index == active:
            return False
        if target_index != active + 1:
            raise ValueError("phase transition must target the immediately next phase")
        actions = [(active, "complete"), (target_index, "start")]
    elif completed == -1:
        if target_index != 0:
            raise ValueError("a new run must start with implementation")
        actions = [(target_index, "start")]
    elif target_index == completed + 1:
        # The completion landed before an interrupted process could append the
        # next start. Resume that exact forward transition without a duplicate.
        actions = [(target_index, "start")]
    elif target_index == completed:
        return False
    elif target_index < completed:
        raise ValueError("phase transition is backward")
    else:
        raise ValueError("phase transition skips one or more phases")

    for index, status in actions:
        emit_phase_event(
            skill=skill,
            run_id=run_id,
            phase=PHASES[index],
            status=status,
        )
    return True


def complete_closeout_phase(
    *, root: Path, skill: str, run_id: str, outcome: str, verified_merged: bool
) -> bool:
    """Complete phase five when present; phase telemetry never blocks settlement."""
    if outcome != "merged" or not verified_merged or skill not in PHASE_SKILLS:
        return False
    try:
        active, completed = _phase_history(
            load_phase_events(root), skill=skill, run_id=run_id
        )
        if active == len(PHASES) - 1:
            emit_phase_event(
                skill=skill, run_id=run_id, phase="closeout", status="complete"
            )
            return True
        if completed == len(PHASES) - 1:
            return False
        # A verified merge remains authoritative when an older, interrupted,
        # or cross-harness run has no active closeout phase.
        return False
    except (OSError, RuntimeError, ValueError):
        # Phase events are coarse observational telemetry. Contradictory input
        # or a failed measurement write stays absent; never synthesize it or
        # block the authoritative terminal row.
        return False


def parse_ts(raw: str) -> datetime | None:
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None


def derive_duration_s(
    root: Path, skill: str, session_id: str, run_id: str, end_ts: datetime
) -> int | None:
    """Derive duration from exact run evidence; never use a session fallback."""
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
            if (
                event.get("skill") != skill
                or event.get("session_id") != session_id
                or event.get("run_id") != run_id
            ):
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


def derive_codex_session_tokens(session_id: str) -> dict[str, int] | None:
    """Read provider-reported cumulative usage from a codex session rollout.

    Codex appends `token_count` events with `total_token_usage` to its rollout
    JSONL; the last one is the session's cumulative bill. Only exact
    provider-reported numbers are returned — no estimates (schema contract).
    `CODEX_HOME` overrides the default `~/.codex` (test seam + shadow homes).
    """
    if not session_id:
        return None
    codex_home = Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex")))
    matches = sorted(codex_home.glob(f"sessions/*/*/*/rollout-*{session_id}.jsonl"))
    if not matches:
        return None
    last: dict[str, object] | None = None
    try:
        with matches[-1].open(encoding="utf-8") as fh:
            for line in fh:
                if '"token_count"' not in line:
                    continue
                try:
                    payload = json.loads(line).get("payload", {})
                except json.JSONDecodeError:
                    continue
                if payload.get("type") != "token_count":
                    continue
                usage = (payload.get("info") or {}).get("total_token_usage")
                if isinstance(usage, dict):
                    last = usage
    except OSError:
        return None
    if not isinstance(last, dict):
        return None
    tokens: dict[str, int] = {}
    for field, source in (
        ("session_tokens_in_cumulative", "input_tokens"),
        ("session_tokens_out_cumulative", "output_tokens"),
        ("session_tokens_cached_cumulative", "cached_input_tokens"),
    ):
        value = last.get(source)
        if isinstance(value, int):
            tokens[field] = value
    return tokens or None


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
        # Which machine ran this. Agents can run on more than one host, and
        # without it "which client is working on what" cannot be answered when
        # they do. Consumers show it only when a window contains more than one
        # host, so the single-machine case stays noise-free.
        "host": platform.node() or "unknown",
        "git_branch": fields["git_branch"],
    }
    # t3code can wrap any engine, so the orchestrator is a separate dimension
    # from the engine — recording only one loses information either way.
    if wrapper := fields.get("wrapper"):
        entry["wrapper"] = wrapper
    if args.git_branch:
        entry["git_branch"] = args.git_branch

    if args.duration_s is not None:
        entry["duration_s"] = args.duration_s
    else:
        end_ts = parse_ts(str(fields["ts"]))
        if end_ts is not None:
            derived_duration = derive_duration_s(
                repo_root(),
                args.skill,
                str(fields["session_id"]),
                args.run_id,
                end_ts,
            )
            if derived_duration is not None:
                entry["duration_s"] = derived_duration

    if args.review_passes is not None:
        entry["review_passes"] = args.review_passes
    # Explicit CLI values are attributable only when the caller supplies
    # run-local usage. Automatic Codex values are session-wide snapshots and
    # retain cumulative names so consumers cannot infer a run delta.
    # getattr keeps programmatic Namespace callers valid.
    explicit_tokens = False
    for field in ("tokens_in", "tokens_out", "tokens_cached"):
        value = getattr(args, field, None)
        if value is not None:
            entry[field] = value
            explicit_tokens = True
    if not explicit_tokens and entry.get("harness") == "codex":
        derived_tokens = derive_codex_session_tokens(str(entry.get("session_id") or ""))
        if derived_tokens:
            entry.update(derived_tokens)
    if explicit_tokens:
        entry["token_scope"] = "run"
    if args.issue is not None:
        entry["issue"] = args.issue
    if args.pr is not None:
        entry["pr"] = args.pr
    if args.footgun_bypass:
        entry["footgun_bypass"] = True
    if args.notes:
        entry["notes"] = args.notes[:100]
    if args.skill in {"work-issue", "execute-blueprint"}:
        entry["reentry_contract_version"] = 1
    return entry


def run_groups(entries: list[dict[str, object]]) -> dict[str, list[dict[str, object]]]:
    """Group valid-run-id entries by run, preserving append order."""
    groups: dict[str, list[dict[str, object]]] = {}
    for entry in entries:
        run_id = entry.get("run_id")
        if isinstance(run_id, str) and RUN_ID_RE.fullmatch(run_id):
            groups.setdefault(run_id, []).append(entry)
    return groups


def session_alive(session_id: str, within_s: int) -> bool:
    """Liveness via provider evidence: a codex rollout touched recently."""
    if not session_id:
        return False
    codex_home = Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex")))
    for path in codex_home.glob(f"sessions/*/*/*/rollout-*{session_id}.jsonl"):
        if datetime.now(UTC).timestamp() - path.stat().st_mtime < within_s:
            return True
    return False


def cmd_check_ceiling(args: argparse.Namespace, entries: list[dict]) -> int:
    """Exit 3 when the run's wall-clock exceeds the session ceiling."""
    groups = run_groups(entries)
    run_id = args.run_id
    if run_id is None:
        candidates = [
            (rows[-1].get("ts"), rid)
            for rid, rows in groups.items()
            if rows[0].get("skill") == args.skill
            and rows[-1].get("outcome") == "in_progress"
        ]
        if not candidates:
            print(f"no active {args.skill} run to check")
            return 2
        run_id = max(candidates)[1]
    rows = groups.get(run_id or "")
    if not rows:
        print(f"unknown run {run_id}")
        return 2
    started = parse_ts(str(rows[0].get("ts")))
    if started is None:
        print(f"run {run_id} has no parseable start timestamp")
        return 2
    elapsed = int((datetime.now(UTC) - started).total_seconds())
    ceiling = args.ceiling_s
    if elapsed >= ceiling:
        print(
            f"CEILING: run {run_id} at {elapsed}s >= {ceiling}s — emit the "
            "take-up brief (reentry contract) and terminate this session"
        )
        return 3
    print(f"run {run_id} at {elapsed}s / ceiling {ceiling}s")
    return 0


def cmd_reconcile_stale(args: argparse.Namespace, entries: list[dict]) -> list[dict]:
    """Close in_progress runs with owner-absence evidence; never a live one.

    Guarded, never blind (#3418 P3, cross-review 2026-08-09): a run is closed
    only when its last run-log activity is older than the idle window AND its
    session shows no provider-side life signs (codex rollout mtime). The idle
    window is DECOUPLED from the (24h) handoff ceiling — a dead session is
    reaped within a work shift, not a day. The caller appends the returned
    rows under the same lock that loaded `entries`, so the latest-state check
    cannot race a concurrent writer.
    """
    idle_window = args.idle_s
    now = datetime.now(UTC)
    closures: list[dict] = []
    for run_id, rows in run_groups(entries).items():
        latest = rows[-1]
        if latest.get("outcome") != "in_progress":
            continue
        last_ts = parse_ts(str(latest.get("ts")))
        if last_ts is None:
            continue
        idle_s = int((now - last_ts).total_seconds())
        if idle_s < idle_window:
            continue
        session_id = str(latest.get("session_id") or "")
        if session_alive(session_id, idle_window):
            print(f"skip {run_id}: session {session_id[:12]}… shows recent activity")
            continue
        closures.append(
            {
                "ts": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "skill": latest.get("skill"),
                "outcome": "abandoned",
                "run_id": run_id,
                "session_id": latest.get("session_id"),
                "session_source": latest.get("session_source"),
                "harness": latest.get("harness"),
                "host": platform.node() or "unknown",
                "git_branch": latest.get("git_branch"),
                "notes": f"stale-reconcile: idle {idle_s // 3600}h past ceiling",
            }
        )
        print(f"close {run_id}: {latest.get('skill')} idle {idle_s // 3600}h")
    return closures


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--skill", help="Canonical skill name")
    parser.add_argument("--start", action="store_true", help="Start or reuse a run")
    parser.add_argument("--run-id", help="Stable logical run ID")
    parser.add_argument(
        "--transition",
        choices=PHASES,
        help="Advance the logical run to this immediate next delivery phase",
    )
    parser.add_argument(
        "--outcome", choices=sorted(VALID_OUTCOMES), help="Lifecycle state"
    )
    parser.add_argument("--duration-s", type=int, help="Wall-clock seconds")
    parser.add_argument("--review-passes", type=int, help="Review-gate cycles")
    parser.add_argument(
        "--tokens-in",
        type=int,
        help="Input tokens attributable to this logical run (never an estimate)",
    )
    parser.add_argument(
        "--tokens-out",
        type=int,
        help="Output tokens attributable to this logical run (never an estimate)",
    )
    parser.add_argument(
        "--tokens-cached",
        type=int,
        help="Cached-input tokens attributable to this logical run",
    )
    parser.add_argument("--issue", type=int, help="GitHub issue number")
    parser.add_argument("--pr", type=int, help="GitHub PR number")
    parser.add_argument("--git-branch", help="Explicit delivery branch identity")
    parser.add_argument(
        "--footgun-bypass",
        action="store_true",
        help="Set when the run used the Footgun-bypass override",
    )
    parser.add_argument("--notes", help="Free-form short note (≤100 chars)")
    parser.add_argument(
        "--check-ceiling",
        action="store_true",
        help="Exit 3 when the (given or active) run exceeds the session ceiling",
    )
    parser.add_argument(
        "--check-owner",
        action="store_true",
        help="Validate that --skill owns the existing --run-id without appending",
    )
    parser.add_argument(
        "--allow-missing-owner",
        action="store_true",
        help="With --check-owner, admit an explicit pre-contract recovery with no row",
    )
    parser.add_argument(
        "--reconcile-stale",
        action="store_true",
        help="Close in_progress runs idle past the ceiling with owner-absence "
        "evidence (guarded; live sessions survive)",
    )
    parser.add_argument(
        "--idle-s",
        type=int,
        default=STALE_RECONCILE_IDLE_S,
        help="reconcile-stale only: idle seconds before a dead run is reaped "
        f"(default {STALE_RECONCILE_IDLE_S}; decoupled from the handoff ceiling)",
    )
    parser.add_argument(
        "--ceiling-s",
        type=int,
        default=SESSION_CEILING_S,
        help=f"Session ceiling in seconds (default {SESSION_CEILING_S})",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="reconcile-stale only: report closures without appending",
    )
    parser.add_argument(
        "--verified-merged",
        action="store_true",
        help="Permit closeout phase completion after the caller verified remote merge state",
    )
    args = parser.parse_args()
    if not args.reconcile_stale and not args.skill:
        parser.error("--skill is required except with --reconcile-stale")
    if not args.start and args.outcome == "merged" and not args.verified_merged:
        parser.error("--outcome merged requires --verified-merged")
    root = repo_root()
    audit_dir = root / ".audit" / "skill-runs"
    audit_dir.mkdir(parents=True, exist_ok=True)
    lock_path = audit_dir / ".write.lock"
    with lock_path.open("a+", encoding="utf-8") as lock_file:
        fcntl.flock(lock_file, fcntl.LOCK_SH if args.check_owner else fcntl.LOCK_EX)
        entries = load_entries(audit_dir)

        if args.check_owner:
            if args.start or args.transition or args.outcome is not None:
                parser.error(
                    "--check-owner cannot be combined with --start, --transition, or --outcome"
                )
            if args.run_id is None:
                parser.error("--run-id is required with --check-owner")
            try:
                owner = require_run_owner(
                    entries,
                    run_id=args.run_id,
                    skill=args.skill,
                    allow_missing=args.allow_missing_owner,
                )
            except ValueError as exc:
                parser.error(str(exc))
            if owner is None:
                print(f"Unowned historical run {args.run_id}")
            else:
                print(f"Owned {owner} run {args.run_id}")
            return 0
        if args.allow_missing_owner:
            parser.error("--allow-missing-owner requires --check-owner")
        if args.check_ceiling:
            return cmd_check_ceiling(args, entries)
        if args.reconcile_stale:
            closures = cmd_reconcile_stale(args, entries)
            if not closures:
                print("no stale runs to reconcile")
                return 0
            if args.dry_run:
                print(f"dry-run: {len(closures)} closure(s) not appended")
                return 0
            day = datetime.now(UTC).strftime("%Y-%m-%d")
            log_path = audit_dir / f"{day}.jsonl"
            with log_path.open("a", encoding="utf-8") as f:
                for closure in closures:
                    f.write(json.dumps(closure) + "\n")
            print(f"reconciled {len(closures)} stale run(s) ({log_path})")
            return 0

        if args.transition:
            if args.start or args.outcome is not None:
                parser.error(
                    "--transition cannot be combined with --start or --outcome"
                )
            if args.run_id is None:
                parser.error("--run-id is required for phase transitions")
            run_rows = [
                entry for entry in entries if entry.get("run_id") == args.run_id
            ]
            if not run_rows:
                parser.error("phase transition requires an existing logical run")
            if any(entry.get("skill") != args.skill for entry in run_rows):
                parser.error("run_id is already owned by another skill")
            if run_rows[-1].get("outcome") != "in_progress":
                parser.error("phase transition requires an active logical run")
            changed = transition_phase(
                root=root,
                skill=args.skill,
                run_id=args.run_id,
                target=args.transition,
            )
            action = "Transitioned" if changed else "Already in"
            print(f"{action} {args.skill} → {args.transition}")
            return 0

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
        day = str(entry["ts"])[:10]
        log_path = audit_dir / f"{day}.jsonl"
        should_append = validate_transition(entries, entry)
        if (
            should_append
            and not args.start
            and args.outcome == "merged"
            and args.verified_merged
        ):
            # The remote merge has already been verified by the closeout
            # adapter. Finish timing first so an interrupted lifecycle-log
            # append can be retried without stranding an incomplete phase.
            try:
                complete_closeout_phase(
                    root=root,
                    skill=args.skill,
                    run_id=args.run_id,
                    outcome=args.outcome,
                    verified_merged=True,
                )
            except (OSError, RuntimeError, ValueError):
                # Timing is observational. The verified terminal row remains
                # authoritative even if future phase readers or writers fail.
                pass
        if should_append:
            with log_path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(entry) + "\n")

        if args.start and args.skill in PHASE_SKILLS:
            active_phase, completed_phase = _phase_history(
                load_phase_events(root), skill=args.skill, run_id=args.run_id
            )
            if active_phase is None and completed_phase == -1:
                # A retry after the lifecycle row landed but the first phase
                # event did not must repair that half-written start. Re-entry
                # into a run that already progressed remains a no-op.
                transition_phase(
                    root=root,
                    skill=args.skill,
                    run_id=args.run_id,
                    target="implementation",
                )
    if args.start:
        print(args.run_id)
    else:
        action = "Logged" if should_append else "Already logged"
        print(f"{action} {args.skill} → {args.outcome} ({log_path})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
