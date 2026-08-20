"""Append JSONL events for skill execution and decision calibration.

Cross-harness instrumentation for the skill workflow. Called from any harness
(Claude Code, Cursor/Codex) via Bash at well-known boundaries in SKILL.md.

Pairs with `skill_run_log.py` (append-only logical-run lifecycle events) — this primitive
captures the *intra-run* events that explain where time and effort went.

Event kinds:

1. `phase` — boundary marker written by `skill_run_log.py --transition` for a
   numbered phase in an outer delivery run. Direct emission remains available
   as the storage primitive.

   `agent_event_stats.py` derives elapsed time from paired start/complete
   events under a stable run ID, with session pairing for historical rows.
   `--elapsed-s` remains supported for older manual stopwatch-style emission.

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

5. `proposal` — a stable proposal identifier at the point a roadmap or design
   skill emits it. A later event with the same (skill, proposal_id) may attach
   the artifact path once the owner accepts it. `make proposal-outcomes` joins
   these events to the committed artifact lifecycle and merged PR history.

6. `decision` / `verdict` / `outcome` — one addressable autonomous decision,
   its owner-calibration result, and its later product result. These detailed
   events coexist with legacy output tallies and feed `make decision-stats`.

7. `friction` — one workflow observation using the bounded friction-signal
   vocabulary. Stable `issue_key` values make exact recurrence measurable.

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
import fcntl
import hashlib
import json
import os
import re
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

VALID_STATUS = {"start", "complete", "failed"}
VALID_RESULT = {"pass", "fail", "skip"}
DECISION_STATES = {"locked", "assumed", "deferred"}
DECISION_WEIGHTS = {"routine", "normal", "high", "reserved"}
AUTHORITY_MODES = {"ask", "recommend", "decide-notify", "autonomous"}
CONFIDENCE_LEVELS = {"low", "medium", "high"}
CHALLENGER_VERDICTS = {"not-run", "agree", "mixed", "disagree"}
OWNER_VERDICTS = {"pending", "confirmed", "no-veto", "notified", "edited", "vetoed"}
OUTCOME_VERDICTS = {"unknown", "supported", "mixed", "failed"}
FRICTION_SIGNALS = {
    "owner-correction",
    "repeated-finding",
    "escape-hatch",
    "blocked-workflow",
    "unexpected-workaround",
    "useful-simplification",
}
KEYED_FRICTION_SIGNALS = {"repeated-finding", "unexpected-workaround"}
FRICTION_STATUSES = {"observed", "resolved", "dismissed"}
FRICTION_EVIDENCE_KINDS = {
    "command-output",
    "escape-hatch",
    "owner-verdict",
    "review-finding",
    "run-outcome",
    "session-correction",
    "self-report",
}
CONTEXT_BOUNDARY_DISPOSITIONS = {"rollover", "kept_inline"}
OUTER_ORCHESTRATORS = {"work-issue", "execute-blueprint"}
RUN_ID_RE = re.compile(r"^sr_[0-9a-f]{32}$")


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
    """Name the ENGINE running this command (claude / codex / cursor / zed).

    `AGENT_HARNESS` is the authoritative override and the supported way for any
    client to identify itself; env sniffing is best-effort.

    This is deliberately the engine, not the orchestrator. t3code can wrap ANY
    engine, so collapsing both into one field loses information either way:
    reporting `codex` hides that t3code drove it, and reporting `t3code` hides
    which engine actually ran. `detect_wrapper()` carries the other dimension.
    """
    if explicit := os.environ.get("AGENT_HARNESS"):
        return explicit
    if any(k.startswith("CODEX_") for k in os.environ):
        return "codex"
    if any(k.startswith("CLAUDE_CODE") for k in os.environ):
        return "claude"
    if any(k.startswith("CURSOR_") for k in os.environ):
        return "cursor"
    return "unknown"


def detect_wrapper() -> str | None:
    """Name the SURFACE the engine was driven from, when it is not the CLI.

    t3code and Zed are editors/orchestrators, not engines: each can drive any
    engine. They are therefore a separate dimension, reported alongside the
    engine rather than instead of it — 10 t3code runs previously appeared as
    plain `codex`, making the surface invisible in fleet views.

    A bare engine with no wrapper means it ran directly from the CLI.
    """
    if explicit := os.environ.get("AGENT_WRAPPER"):
        return explicit
    if any(k.startswith("T3_") for k in os.environ):
        return "t3code"
    if any(k.startswith("ZED_") for k in os.environ):
        return "zed"
    try:
        if str(Path.cwd()).startswith(str(Path.home() / ".t3")):
            return "t3code"
    except OSError:  # pragma: no cover - cwd removed underneath us
        return None
    if git_branch().startswith("t3code/"):
        return "t3code"
    return None


def runtime_kind() -> str:
    """Classify the caller through the canonical harness/wrapper contract."""
    return (
        "agent"
        if detect_harness() != "unknown" or detect_wrapper() is not None
        else "human"
    )


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


def write_event(event: dict) -> bool:
    try:
        root = repo_root()
        audit_dir = root / ".audit" / "agent-events"
        audit_dir.mkdir(parents=True, exist_ok=True)
        day = datetime.now(UTC).strftime("%Y-%m-%d")
        log_path = audit_dir / f"{day}.jsonl"
        lock_path = audit_dir / ".write.lock"
        with lock_path.open("a+", encoding="utf-8") as lock_file:
            fcntl.flock(lock_file, fcntl.LOCK_EX)
            with log_path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(event) + "\n")
        return True
    except Exception as e:
        print(f"agent_event: warning — could not write event ({e})", file=sys.stderr)
        return False


def write_unique_event(event: dict, *, key_fields: tuple[str, ...]) -> bool:
    """Append once for an exact logical key; failures remain non-blocking."""
    try:
        root = repo_root()
        audit_dir = root / ".audit" / "agent-events"
        audit_dir.mkdir(parents=True, exist_ok=True)
        lock_path = audit_dir / ".write.lock"
        with lock_path.open("a+", encoding="utf-8") as lock_file:
            fcntl.flock(lock_file, fcntl.LOCK_EX)
            for log_path in sorted(audit_dir.glob("*.jsonl")):
                for line in log_path.read_text(encoding="utf-8").splitlines():
                    if not line.strip():
                        continue
                    try:
                        existing = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if not isinstance(existing, dict):
                        continue
                    if all(
                        existing.get(field) == event.get(field) for field in key_fields
                    ):
                        return False
            day = str(event.get("ts", now_iso()))[:10]
            log_path = audit_dir / f"{day}.jsonl"
            with log_path.open("a", encoding="utf-8") as output:
                output.write(json.dumps(event) + "\n")
        return True
    except Exception as exc:
        print(
            f"agent_event: warning — could not write unique event ({exc})",
            file=sys.stderr,
        )
        return False


def write_context_boundary_event(
    *,
    run_id: str,
    skill: str,
    disposition: str,
    issue: int | None = None,
    pr: int | None = None,
) -> bool:
    """Record one idempotent context-boundary observation for a logical run."""
    if not RUN_ID_RE.fullmatch(run_id):
        raise ValueError("invalid logical run ID")
    if skill not in OUTER_ORCHESTRATORS:
        raise ValueError("context boundary requires an outer orchestrator")
    if disposition not in CONTEXT_BOUNDARY_DISPOSITIONS:
        raise ValueError("invalid context boundary disposition")
    try:
        event = {
            **common_fields(),
            "kind": "context_boundary",
            "run_id": run_id,
            "skill": skill,
            "disposition": disposition,
        }
    except Exception as exc:
        print(
            f"agent_event: warning — could not build context boundary ({exc})",
            file=sys.stderr,
        )
        return False
    if issue is not None:
        event["issue"] = issue
    if pr is not None:
        event["pr"] = pr
    return write_unique_event(
        event,
        key_fields=("kind", "run_id", "disposition"),
    )


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
    if wrapper := detect_wrapper():
        fields["wrapper"] = wrapper
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
    if args.run_id:
        event["run_id"] = args.run_id
    return 0 if write_event(event) is not False else 1


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
        v = getattr(args, key, None)
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
    _enforce_review_pass_escape_hatch(args, event)
    return 0


# Review Gate escape hatch (review-gate.mdc): 5 non-converging passes stop
# the loop. Deterministic guard (#3106 B2): detection and the durable
# friction record fire mechanically instead of relying on prose recall.
REVIEW_GATE_SKILLS = frozenset({"code-review", "security-review", "review-design-doc"})
ESCAPE_HATCH_PASS = 5


def _enforce_review_pass_escape_hatch(args: argparse.Namespace, event: dict) -> None:
    pass_num = getattr(args, "pass_num", None)
    if (
        args.skill not in REVIEW_GATE_SKILLS
        or pass_num is None
        or pass_num < ESCAPE_HATCH_PASS
    ):
        return
    write_unique_event(
        {
            **common_fields(),
            "kind": "friction",
            "skill": args.skill,
            "signal": "escape-hatch",
            "status": "observed",
            "issue_key": (
                f"{args.skill}:5-pass-escape-hatch:{event.get('session_id')}"
            ),
            "summary": (
                f"review pass {pass_num} recorded — the 5-pass "
                "non-convergence escape hatch fired (review-gate)"
            ),
        },
        key_fields=("kind", "issue_key"),
    )
    print(
        f"⛔ escape hatch: pass {pass_num} on {args.skill} — review-gate "
        "caps non-converging passes at 5. Stop fixing in place: summarize "
        "the remaining findings and escalate to the owner "
        "(deterministic guard, #3106 B2).",
        file=sys.stderr,
    )


def cmd_tool(args: argparse.Namespace) -> int:
    if args.result and args.result not in VALID_RESULT:
        print(f"agent_event: invalid --result {args.result!r}", file=sys.stderr)
        return 0
    finding_count = getattr(args, "finding_count", None)
    max_severity = getattr(args, "max_severity", None)
    if finding_count is not None or max_severity is not None:
        valid_pair = (
            not isinstance(finding_count, bool)
            and isinstance(finding_count, int)
            and finding_count >= 0
            and max_severity in {"critical", "important", "suggestion", "none"}
            and ((finding_count == 0) == (max_severity == "none"))
        )
        if not valid_pair:
            print(
                "agent_event: invalid cross-harness finding telemetry",
                file=sys.stderr,
            )
            finding_count = None
            max_severity = None
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
    for key in (
        "cost_usd",
        "budget_usd",
        "provider",
        "scope_digest",
        "prompt_chars",
        "timeout_seconds",
    ):
        value = getattr(args, key, None)
        if value is not None:
            event[key] = value
    if finding_count is not None:
        event["finding_count"] = finding_count
        event["max_severity"] = max_severity
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


def cmd_proposal(args: argparse.Namespace) -> int:
    """Record a proposal without making telemetry a workflow dependency."""
    event = {
        **common_fields(),
        "kind": "proposal",
        "skill": args.skill,
        "proposal_id": args.proposal_id,
    }
    if args.artifact_type:
        event["artifact_type"] = args.artifact_type
    if args.artifact_path:
        event["artifact_path"] = args.artifact_path
    if args.created_at:
        event["created_at"] = args.created_at
    if args.notes:
        event["notes"] = args.notes[:100]
    write_event(event)
    return 0


def _copy_present(event: dict, args: argparse.Namespace, keys: tuple[str, ...]) -> None:
    for key in keys:
        value = getattr(args, key, None)
        if value is not None:
            event[key] = value


def cmd_decision(args: argparse.Namespace) -> int:
    """Record one decision with enough context to calibrate authority later."""
    event = {
        **common_fields(),
        "kind": "decision",
        "decision_id": args.decision_id,
        "state": args.state,
        "owner_verdict": "pending",
        "outcome_verdict": "unknown",
    }
    _copy_present(
        event,
        args,
        (
            "skill",
            "category",
            "weight",
            "authority",
            "confidence",
            "reversibility",
            "calibration_deadline",
            "challenger_verdict",
            "rejected_alternative",
            "grounding",
            "expected_outcome",
        ),
    )
    if args.notes:
        event["notes"] = args.notes[:100]
    write_event(event)
    return 0


def cmd_verdict(args: argparse.Namespace) -> int:
    event = {
        **common_fields(),
        "kind": "decision-verdict",
        "decision_id": args.decision_id,
        "owner_verdict": args.owner_verdict,
    }
    if args.notes:
        event["notes"] = args.notes[:100]
    write_event(event)
    return 0


def cmd_outcome(args: argparse.Namespace) -> int:
    event = {
        **common_fields(),
        "kind": "decision-outcome",
        "decision_id": args.decision_id,
        "outcome_verdict": args.outcome_verdict,
    }
    if args.notes:
        event["notes"] = args.notes[:100]
    write_event(event)
    return 0


def cmd_friction(args: argparse.Namespace) -> int:
    """Record one bounded workflow-friction observation."""
    event = {
        **common_fields(),
        "kind": "friction",
        "skill": args.skill,
        "signal": args.signal,
        "summary": args.summary,
        "status": args.status,
    }
    _copy_present(
        event,
        args,
        ("evidence_kind", "evidence_id", "surface", "issue_key"),
    )
    write_event(event)
    return 0


def cmd_context_boundary(args: argparse.Namespace) -> int:
    write_context_boundary_event(
        run_id=args.run_id,
        skill=args.skill,
        disposition=args.disposition,
        issue=args.issue,
        pr=args.pr,
    )
    return 0


def cmd_runtime_kind(_args: argparse.Namespace) -> int:
    """Print the stable agent/human classification for shell gate composition."""
    print(runtime_kind())
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="kind", required=True)

    p_phase = sub.add_parser("phase", help="Phase/step boundary marker")
    p_phase.add_argument("--skill", required=True)
    p_phase.add_argument("--phase", required=True, help="Numbered phase id, e.g. '5'")
    p_phase.add_argument("--name", help="Short phase name, e.g. 'review-gate'")
    p_phase.add_argument("--status", required=True, choices=sorted(VALID_STATUS))
    p_phase.add_argument(
        "--run-id",
        help="Stable logical run ID; preserves session fields for diagnostics",
    )
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
    p_tool.add_argument("--prompt-chars", type=int)
    p_tool.add_argument("--timeout-seconds", type=int)
    # Residual-miss telemetry: a cross-harness pass runs AFTER local review has
    # converged, so any finding it reports is a local-gate miss over a known
    # denominator. Counting catches without counting misses made gate
    # effectiveness unmeasurable.
    p_tool.add_argument(
        "--finding-count",
        type=int,
        help="Findings reported by an advisory pass; requires consistent --max-severity",
    )
    p_tool.add_argument(
        "--max-severity",
        choices=("critical", "important", "suggestion", "none"),
        help="Highest severity; requires --finding-count (none iff count is 0)",
    )
    p_tool.add_argument("--notes", help="≤100 chars")
    p_tool.set_defaults(func=cmd_tool)

    p_invoke = sub.add_parser(
        "invoke", help="Leaf-skill invocation marker (usage counting only)"
    )
    p_invoke.add_argument("--skill", required=True)
    p_invoke.add_argument("--notes", help="≤100 chars")
    p_invoke.set_defaults(func=cmd_invoke)

    p_proposal = sub.add_parser(
        "proposal", help="Stable proposal identifier for outcome telemetry"
    )
    p_proposal.add_argument(
        "--skill",
        required=True,
        choices=("frontier-roadmap", "fortify-roadmap", "write-design-doc"),
    )
    p_proposal.add_argument("--proposal-id", required=True)
    p_proposal.add_argument(
        "--artifact-type",
        help="e.g. blueprint, exploration, adr, initiative, issue, scenario",
    )
    p_proposal.add_argument(
        "--artifact-path",
        help="Repo-relative path; add in a companion event once an artifact exists",
    )
    p_proposal.add_argument(
        "--created-at", help="Original proposal date (YYYY-MM-DD), for backfills"
    )
    p_proposal.add_argument("--notes", help="≤100 chars")
    p_proposal.set_defaults(func=cmd_proposal)

    p_decision = sub.add_parser(
        "decision", help="One addressable decision with calibration metadata"
    )
    p_decision.add_argument("--decision-id", required=True)
    p_decision.add_argument("--state", required=True, choices=sorted(DECISION_STATES))
    p_decision.add_argument("--skill")
    p_decision.add_argument("--category", required=True)
    p_decision.add_argument("--weight", required=True, choices=sorted(DECISION_WEIGHTS))
    p_decision.add_argument(
        "--authority", required=True, choices=sorted(AUTHORITY_MODES)
    )
    p_decision.add_argument(
        "--confidence", required=True, choices=sorted(CONFIDENCE_LEVELS)
    )
    p_decision.add_argument(
        "--reversibility", required=True, help="e.g. single-pr, migration, irreversible"
    )
    p_decision.add_argument("--calibration-deadline", help="YYYY-MM-DD")
    p_decision.add_argument(
        "--challenger-verdict",
        default="not-run",
        choices=sorted(CHALLENGER_VERDICTS),
    )
    p_decision.add_argument("--rejected-alternative")
    p_decision.add_argument("--grounding")
    p_decision.add_argument("--expected-outcome")
    p_decision.add_argument("--notes", help="≤100 chars")
    p_decision.set_defaults(func=cmd_decision)

    p_verdict = sub.add_parser("verdict", help="Owner verdict on a recorded decision")
    p_verdict.add_argument("--decision-id", required=True)
    p_verdict.add_argument(
        "--owner-verdict", required=True, choices=sorted(OWNER_VERDICTS)
    )
    p_verdict.add_argument("--notes", help="≤100 chars")
    p_verdict.set_defaults(func=cmd_verdict)

    p_outcome = sub.add_parser("outcome", help="Observed product outcome of a decision")
    p_outcome.add_argument("--decision-id", required=True)
    p_outcome.add_argument(
        "--outcome-verdict", required=True, choices=sorted(OUTCOME_VERDICTS)
    )
    p_outcome.add_argument("--notes", help="≤100 chars")
    p_outcome.set_defaults(func=cmd_outcome)

    p_friction = sub.add_parser(
        "friction", help="One observed workflow-friction signal"
    )
    p_friction.add_argument("--skill", required=True)
    p_friction.add_argument("--signal", required=True, choices=sorted(FRICTION_SIGNALS))
    p_friction.add_argument("--summary", required=True)
    p_friction.add_argument("--evidence-kind", choices=sorted(FRICTION_EVIDENCE_KINDS))
    p_friction.add_argument("--evidence-id")
    p_friction.add_argument("--surface")
    p_friction.add_argument("--issue-key")
    p_friction.add_argument(
        "--status", choices=sorted(FRICTION_STATUSES), default="observed"
    )
    p_friction.set_defaults(func=cmd_friction)

    p_boundary = sub.add_parser(
        "context-boundary", help="Logical-task context rollover marker"
    )
    p_boundary.add_argument("--run-id", required=True)
    p_boundary.add_argument(
        "--skill", required=True, choices=sorted(OUTER_ORCHESTRATORS)
    )
    p_boundary.add_argument(
        "--disposition",
        required=True,
        choices=sorted(CONTEXT_BOUNDARY_DISPOSITIONS),
    )
    p_boundary.add_argument("--issue", type=int)
    p_boundary.add_argument("--pr", type=int)
    p_boundary.set_defaults(func=cmd_context_boundary)

    p_runtime = sub.add_parser(
        "runtime-kind", help="Print agent or human from the canonical runtime identity"
    )
    p_runtime.set_defaults(func=cmd_runtime_kind)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.kind == "friction":
        if args.signal in KEYED_FRICTION_SIGNALS and not args.issue_key:
            parser.error(f"--issue-key is required for {args.signal}")
        if args.status != "observed" and not args.issue_key:
            parser.error("--issue-key is required for resolved/dismissed friction")
    return args.func(args)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:
        print(f"agent_event: warning — {e}", file=sys.stderr)
        sys.exit(0)
