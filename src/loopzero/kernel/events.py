"""Append a JSONL event capturing one of phase | output | tool boundaries.

Cross-harness instrumentation for the skill workflow. Called from any harness
(Claude Code, Cursor/Codex) via Bash at well-known boundaries in SKILL.md.

Pairs with `skill_run_log.py` (one entry per skill run at exit) — this primitive
captures the *intra-run* events that explain where time and effort went.

Event kinds:

1. `phase` — boundary marker for a numbered phase/step in an orchestrator skill.
       agent_event phase --skill work-issue --phase 5 --name review-gate \\
                         --status start|complete

   `agent_event_stats.py` derives elapsed time from paired start/complete
   events in the same session. `--elapsed-s` remains supported for older
   manual stopwatch-style emission.

2. `output` — what an "additive" skill produced (code-review, security-review,
   design-handoff, resolve-findings, etc.). Drives the additive-score view.
       agent_event output --skill code-review --pass 1 \\
                          --findings-critical 0 --findings-high 2 \\
                          --findings-medium 5 --findings-low 3
       agent_event output --skill resolve-findings \\
                          --decisions-locked 1 --decisions-assumed 2 \\
                          --decisions-deferred 0

3. `tool` — duration of a long sub-process (precommit suite, openapi sync,
   lint-staged, etc.), typically emitted from inside `commit-autofix`.
       agent_event tool --category test-precommit \\
                        --duration-ms 34200 --result pass|fail

4. `invoke` — leaf-skill invocation marker, emitted at SKILL.md entry.
   Pure usage counting (run-outcome logs only cover orchestrators).
       agent_event invoke --skill refine-code
   Aggregate with `make invoke-counts DAYS=30`.

Common fields on every event:
  ts, kind, session_id, harness, git_branch, correlation_id (optional).

session_id resolution (Finding 4 — hybrid):
  - Prefer $AGENT_SESSION_ID
  - Then use platform thread ids such as $CODEX_THREAD_ID when available
  - Fall back to a deterministic hash when no stable thread/session id exists
  - The `session_source` field records which path was used.

harness resolution: $AGENT_HARNESS if set, else auto-detect via known env vars
(CLAUDE_CODE_*, CURSOR_*), else "unknown".

Storage: `.audit/agent-events/YYYY-MM-DD.jsonl` (gitignored, local-only).

Failures are warn-and-continue: instrumentation never breaks a workflow. If the
script can't write (disk full, permission denied), it prints a warning to
stderr and exits 0.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

VALID_STATUS = {"start", "complete", "failed"}
VALID_RESULT = {"pass", "fail", "skip"}


def repo_root() -> Path:
    """Resolve the path used to anchor `.audit/`.

    Uses `--git-common-dir` so worktrees write to the **main repo's** `.audit/`
    rather than their own ephemeral `.audit/` that disappears when the
    worktree is removed. Without this, every parallel-agent worktree loses
    its telemetry on cleanup.
    """
    out = subprocess.check_output(
        ["git", "rev-parse", "--git-common-dir"], text=True
    ).strip()
    # Main checkout: returns ".git"; worktree: returns "/abs/path/to/mainrepo/.git".
    # Parent of the common .git dir is the main repo root in both cases.
    return Path(out).resolve().parent


def git_branch() -> str:
    try:
        return (
            subprocess.check_output(
                ["git", "branch", "--show-current"], text=True
            ).strip()
            or "(detached)"
        )
    except subprocess.CalledProcessError:
        return "(unknown)"


def detect_harness() -> str:
    if explicit := os.environ.get("AGENT_HARNESS"):
        return explicit
    if any(k.startswith("CODEX_") for k in os.environ):
        return "codex"
    if any(k.startswith("CLAUDE_CODE") for k in os.environ):
        return "claude"
    if any(k.startswith("CURSOR_") for k in os.environ):
        return "cursor"
    return "unknown"


def resolve_session(root: Path, branch: str) -> tuple[str, str]:
    """Return (session_id, source). Prefer env, else derived hash."""
    if env_id := os.environ.get("AGENT_SESSION_ID"):
        return env_id, "env"
    if codex_thread_id := os.environ.get("CODEX_THREAD_ID"):
        return codex_thread_id, "codex_thread"
    day = datetime.now(UTC).strftime("%Y-%m-%d")
    raw = f"{branch}|{root}|{day}".encode()
    return hashlib.sha1(raw).hexdigest()[:12], "derived"


def now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def write_event(event: dict) -> None:
    try:
        root = repo_root()
        audit_dir = root / ".audit" / "agent-events"
        audit_dir.mkdir(parents=True, exist_ok=True)
        day = datetime.now(UTC).strftime("%Y-%m-%d")
        log_path = audit_dir / f"{day}.jsonl"
        with log_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(event) + "\n")
    except Exception as e:
        print(f"agent_event: warning — could not write event ({e})", file=sys.stderr)


def common_fields() -> dict:
    root = repo_root()
    branch = git_branch()
    session_id, source = resolve_session(root, branch)
    fields = {
        "ts": now_iso(),
        "session_id": session_id,
        "session_source": source,
        "harness": detect_harness(),
        "git_branch": branch,
    }
    if cid := os.environ.get("AGENT_CORRELATION_ID"):
        fields["correlation_id"] = cid
    return fields


def cmd_phase(args: argparse.Namespace) -> int:
    if args.status not in VALID_STATUS:
        print(f"agent_event: invalid --status {args.status!r}", file=sys.stderr)
        return 0
    event = {
        **common_fields(),
        "kind": "phase",
        "skill": args.skill,
        "phase": args.phase,
        "status": args.status,
    }
    if args.name:
        event["name"] = args.name
    if args.elapsed_s is not None:
        event["elapsed_s"] = args.elapsed_s
    write_event(event)
    return 0


def cmd_output(args: argparse.Namespace) -> int:
    event = {
        **common_fields(),
        "kind": "output",
        "skill": args.skill,
    }
    if args.pass_num is not None:
        event["pass"] = args.pass_num
    counts = {}
    for sev in ("critical", "high", "medium", "low"):
        v = getattr(args, f"findings_{sev}")
        if v is not None:
            counts[sev] = v
    if counts:
        event["findings"] = counts
    # generic produced-artifact counts for non-finding skills
    for key in ("docs_created", "docs_updated", "drift_fixes", "decisions_made"):
        v = getattr(args, key.replace("_", "_"), None)
        if v is not None:
            event[key] = v
    decision_counts = {
        state: getattr(args, f"decisions_{state}", None)
        for state in ("locked", "assumed", "deferred")
    }
    decision_ids = {
        state: getattr(args, f"decision_{state}_ids", None) or []
        for state in ("locked", "assumed", "deferred")
    }
    if any(value is not None for value in decision_counts.values()) or any(
        decision_ids.values()
    ):
        event["decisions"] = {
            state: int(decision_counts[state] or 0)
            for state in ("locked", "assumed", "deferred")
        }
        if any(decision_ids.values()):
            event["decisions"]["ids"] = {
                state: ids for state, ids in decision_ids.items() if ids
            }
    if reversal_ids := getattr(args, "reverses_decision_ids", None):
        event["reverses_decision_ids"] = reversal_ids
    if args.notes:
        event["notes"] = args.notes[:100]
    write_event(event)
    return 0


def cmd_tool(args: argparse.Namespace) -> int:
    if args.result and args.result not in VALID_RESULT:
        print(f"agent_event: invalid --result {args.result!r}", file=sys.stderr)
        return 0
    event = {
        **common_fields(),
        "kind": "tool",
        "category": args.category,
        "duration_ms": args.duration_ms,
    }
    if args.result:
        event["result"] = args.result
    if args.skill:
        event["skill"] = args.skill
    for key in ("cost_usd", "budget_usd", "provider", "scope_digest"):
        value = getattr(args, key, None)
        if value is not None:
            event[key] = value
    if args.notes:
        event["notes"] = args.notes[:100]
    write_event(event)
    return 0


def cmd_invoke(args: argparse.Namespace) -> int:
    event = {
        **common_fields(),
        "kind": "invoke",
        "skill": args.skill,
    }
    if args.notes:
        event["notes"] = args.notes[:100]
    write_event(event)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="kind", required=True)

    p_phase = sub.add_parser("phase", help="Phase/step boundary marker")
    p_phase.add_argument("--skill", required=True)
    p_phase.add_argument("--phase", required=True, help="Numbered phase id, e.g. '5'")
    p_phase.add_argument("--name", help="Short phase name, e.g. 'review-gate'")
    p_phase.add_argument("--status", required=True, choices=sorted(VALID_STATUS))
    p_phase.add_argument(
        "--elapsed-s",
        type=int,
        help="Seconds since phase start; optional because stats derive paired start/complete events",
    )
    p_phase.set_defaults(func=cmd_phase)

    p_out = sub.add_parser("output", help="What an additive skill produced")
    p_out.add_argument("--skill", required=True)
    p_out.add_argument("--pass", dest="pass_num", type=int, help="Review pass number")
    p_out.add_argument("--findings-critical", type=int)
    p_out.add_argument("--findings-high", type=int)
    p_out.add_argument("--findings-medium", type=int)
    p_out.add_argument("--findings-low", type=int)
    p_out.add_argument("--docs-created", type=int)
    p_out.add_argument("--docs-updated", type=int)
    p_out.add_argument("--drift-fixes", type=int)
    p_out.add_argument("--decisions-made", type=int)
    p_out.add_argument("--decisions-locked", type=int)
    p_out.add_argument("--decisions-assumed", type=int)
    p_out.add_argument("--decisions-deferred", type=int)
    p_out.add_argument(
        "--decision-locked-id", dest="decision_locked_ids", action="append"
    )
    p_out.add_argument(
        "--decision-assumed-id", dest="decision_assumed_ids", action="append"
    )
    p_out.add_argument(
        "--decision-deferred-id", dest="decision_deferred_ids", action="append"
    )
    p_out.add_argument(
        "--reverses-decision-id", dest="reverses_decision_ids", action="append"
    )
    p_out.add_argument("--notes", help="≤100 chars")
    p_out.set_defaults(func=cmd_output)

    p_tool = sub.add_parser("tool", help="Long sub-process duration")
    p_tool.add_argument(
        "--category", required=True, help="e.g. test-precommit, openapi-sync"
    )
    p_tool.add_argument("--duration-ms", required=True, type=int)
    p_tool.add_argument("--result", choices=sorted(VALID_RESULT))
    p_tool.add_argument("--skill", help="Optional skill context")
    p_tool.add_argument("--cost-usd", type=float)
    p_tool.add_argument("--budget-usd", type=float)
    p_tool.add_argument("--provider")
    p_tool.add_argument("--scope-digest")
    p_tool.add_argument("--notes", help="≤100 chars")
    p_tool.set_defaults(func=cmd_tool)

    p_invoke = sub.add_parser(
        "invoke", help="Leaf-skill invocation marker (usage counting only)"
    )
    p_invoke.add_argument("--skill", required=True)
    p_invoke.add_argument("--notes", help="≤100 chars")
    p_invoke.set_defaults(func=cmd_invoke)

    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:
        print(f"agent_event: warning — {e}", file=sys.stderr)
        sys.exit(0)
