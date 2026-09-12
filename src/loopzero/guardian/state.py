"""Strict, append-only Guardian claim observations.

Guardian observations share the configured local audit event stream.
Unlike generic agent telemetry, an observation is an admission control record:
if the existing stream is malformed or the new row cannot be durably written
and read back, callers must treat the tick as broken.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import math
import os
import re
import subprocess
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Iterable, Mapping

from ..kernel import settings as kernel_settings

SHA_RE = re.compile(r"^[0-9a-f]{40}$")
DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")
SLOT_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T(?:06|12|18):00Z$")
QUALIFICATION_RE = re.compile(r"^qualification:\d{4}-\d{2}-\d{2}:[A-Za-z0-9._-]+$")
FAILURE_CLASS_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,63}$")
TRANSITIONS = frozenset(
    {
        "quiet",
        "broken",
        "ticket",
        "tracked",
        "paused",
        "failed",
        "escalated",
        "resolved",
    }
)
# A ticket deliberately remains resumable: delivery records its eventual outcome.
TERMINAL_TRANSITIONS = frozenset(
    {"quiet", "broken", "tracked", "failed", "escalated", "resolved"}
)


class GuardianStateError(RuntimeError):
    """The evidence stream cannot safely authorize further Guardian work."""


@dataclass(frozen=True)
class GuardianState:
    """Deterministic reduction of the Guardian portion of the event stream."""

    events: tuple[dict[str, Any], ...]
    transition_keys: frozenset[str]
    last_consumed_slot: str | None
    pending_slots: tuple[str, ...]


def stable_digest(value: object) -> str:
    """Return a stable SHA-256 digest for a command or policy value."""
    encoded = json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    )
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def command_digest(command: str) -> str:
    if not isinstance(command, str) or not command.strip():
        raise GuardianStateError("observation command must be a non-empty string")
    return stable_digest(command)


def policy_digest(claim: Mapping[str, object]) -> str:
    """Digest reviewed claim policy, excluding only presentation-only prose."""
    if not isinstance(claim, Mapping):
        raise GuardianStateError("claim policy must be a mapping")
    # Keep policy additions in the identity by digesting all declared inputs.
    return stable_digest(dict(claim))


def scheduled_slot(now: datetime) -> str:
    """Latest fixed 06/12/18 UTC slot not later than ``now``."""
    if now.tzinfo is None:
        raise GuardianStateError("scheduled time must be timezone-aware")
    now = now.astimezone(UTC).replace(second=0, microsecond=0)
    for hour in (18, 12, 6):
        candidate = now.replace(hour=hour, minute=0)
        if candidate <= now:
            return candidate.strftime("%Y-%m-%dT%H:%MZ")
    return (now.replace(hour=18, minute=0) - timedelta(days=1)).strftime(
        "%Y-%m-%dT%H:%MZ"
    )


def manual_evaluation_id(qualification_key: str) -> str:
    """Validate the explicit key required for a recording manual qualification."""
    if not QUALIFICATION_RE.fullmatch(qualification_key):
        raise GuardianStateError(
            "manual recording requires qualification:YYYY-MM-DD:<unique-id>"
        )
    return qualification_key


def evaluation_id(
    identity: str,
    claim_id: str,
    measured_source_sha: str,
    command: str,
    policy: Mapping[str, object],
) -> str:
    """Bind a claim evaluation to its timing/manual identity and reviewed inputs."""
    if not (SLOT_RE.fullmatch(identity) or QUALIFICATION_RE.fullmatch(identity)):
        raise GuardianStateError("invalid Guardian evaluation identity")
    _validate_sha(measured_source_sha, "measured_source_sha")
    if not isinstance(claim_id, str) or not claim_id:
        raise GuardianStateError("claim_id is required")
    return stable_digest(
        {
            "identity": identity,
            "claim_id": claim_id,
            "measured_source_sha": measured_source_sha,
            "command_digest": command_digest(command),
            "policy_digest": policy_digest(policy),
        }
    )


def transition_key(evaluation: str, transition: str) -> str:
    if not DIGEST_RE.fullmatch(evaluation):
        raise GuardianStateError("evaluation_id must be a SHA-256 digest")
    if transition not in TRANSITIONS:
        raise GuardianStateError(f"unsupported Guardian transition {transition!r}")
    return stable_digest({"evaluation_id": evaluation, "transition": transition})


def build_observation(
    *,
    identity: str,
    claim: Mapping[str, object],
    measured_source_sha: str,
    transition: str,
    value: float | None = None,
    verified_source_sha: str | None = None,
    ts: str | None = None,
    **metadata: object,
) -> dict[str, Any]:
    """Build one validated source-bound transition event.

    ``identity`` is a timer slot or an explicit qualification key.  The caller
    must never manufacture an identity for a diagnostic run.
    """
    claim_id = claim.get("id")
    command = claim.get("command")
    if not isinstance(claim_id, str) or not isinstance(command, str):
        raise GuardianStateError("claim observation requires id and command")
    evaluation = evaluation_id(identity, claim_id, measured_source_sha, command, claim)
    event: dict[str, Any] = {
        "kind": "claim_observation",
        "schema_version": 1,
        "ts": ts
        or datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z"),
        "identity": identity,
        "slot": identity if SLOT_RE.fullmatch(identity) else None,
        "evaluation_id": evaluation,
        "transition": transition,
        "idempotency_key": transition_key(evaluation, transition),
        "claim_id": claim_id,
        "measured_source_sha": measured_source_sha,
        "verified_source_sha": verified_source_sha,
        "claim_command": command,
        "command_digest": command_digest(command),
        "claim_policy": dict(claim),
        "policy_digest": policy_digest(claim),
        "value": value,
        "threshold": claim.get("threshold"),
        "headroom": metadata.pop("headroom", claim.get("headroom")),
        "policy_headroom": claim.get("headroom"),
        "repair_scope": claim.get("repair_scope", []),
        "acceptance_command": claim.get("acceptance_command"),
        "candidate_command": claim.get("candidate_command"),
        "quality_verdicts": metadata.pop("quality_verdicts", []),
        "charter": metadata.pop("charter", None),
    }
    overlap = set(metadata).intersection(event)
    if overlap:
        raise GuardianStateError(
            "observation metadata cannot replace core fields: "
            + ", ".join(sorted(overlap))
        )
    event.update(metadata)
    validate_observation(event)
    return event


def _validate_sha(value: object, field: str) -> None:
    if not isinstance(value, str) or not SHA_RE.fullmatch(value):
        raise GuardianStateError(f"{field} must be a lowercase 40-character SHA")


def validate_observation(event: Mapping[str, object]) -> None:
    """Reject incomplete or internally inconsistent Guardian rows."""
    required = {
        "kind",
        "schema_version",
        "ts",
        "identity",
        "evaluation_id",
        "transition",
        "idempotency_key",
        "claim_id",
        "measured_source_sha",
        "verified_source_sha",
        "claim_command",
        "command_digest",
        "claim_policy",
        "policy_digest",
        "value",
        "threshold",
        "headroom",
        "policy_headroom",
        "repair_scope",
        "acceptance_command",
        "candidate_command",
        "quality_verdicts",
        "charter",
    }
    missing = required.difference(event)
    if missing:
        raise GuardianStateError(
            f"observation missing required fields: {', '.join(sorted(missing))}"
        )
    if event.get("kind") != "claim_observation" or event.get("schema_version") != 1:
        raise GuardianStateError("unsupported Guardian observation schema")
    timestamp = event.get("ts")
    if not isinstance(timestamp, str):
        raise GuardianStateError("observation timestamp must be an ISO-8601 string")
    try:
        parsed_timestamp = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
    except ValueError as exc:
        raise GuardianStateError(
            "observation timestamp must be valid ISO-8601"
        ) from exc
    if parsed_timestamp.tzinfo is None:
        raise GuardianStateError("observation timestamp must include a timezone")
    if parsed_timestamp.utcoffset() != timedelta(0):
        raise GuardianStateError("observation timestamp must be UTC")
    identity = event.get("identity")
    if not isinstance(identity, str) or not (
        SLOT_RE.fullmatch(identity) or QUALIFICATION_RE.fullmatch(identity)
    ):
        raise GuardianStateError("invalid observation identity")
    slot = event.get("slot")
    if slot != (identity if SLOT_RE.fullmatch(identity) else None):
        raise GuardianStateError("observation slot does not match its identity")
    if not isinstance(event.get("claim_id"), str) or not event["claim_id"]:
        raise GuardianStateError("observation claim_id is required")
    if (
        not isinstance(event.get("claim_command"), str)
        or not event["claim_command"]
        or command_digest(event["claim_command"]) != event.get("command_digest")
    ):
        raise GuardianStateError("observation command does not match its digest")
    _validate_sha(event.get("measured_source_sha"), "measured_source_sha")
    verified = event.get("verified_source_sha")
    if verified is not None:
        _validate_sha(verified, "verified_source_sha")
    for field in (
        "evaluation_id",
        "idempotency_key",
        "command_digest",
        "policy_digest",
    ):
        if not isinstance(event.get(field), str) or not DIGEST_RE.fullmatch(
            event[field]
        ):
            raise GuardianStateError(f"{field} must be a SHA-256 digest")
    claim_policy = event.get("claim_policy")
    if (
        not isinstance(claim_policy, Mapping)
        or policy_digest(claim_policy) != event["policy_digest"]
    ):
        raise GuardianStateError(
            "observation claim policy does not match policy digest"
        )
    policy_bindings = {
        "id": event["claim_id"],
        "command": event["claim_command"],
        "threshold": event.get("threshold"),
        "headroom": event.get("policy_headroom"),
        "repair_scope": event.get("repair_scope"),
        "acceptance_command": event.get("acceptance_command"),
        "candidate_command": event.get("candidate_command"),
    }
    if any(claim_policy.get(key) != value for key, value in policy_bindings.items()):
        raise GuardianStateError(
            "observation executable authority does not match claim policy digest"
        )
    expected_evaluation = stable_digest(
        {
            "identity": identity,
            "claim_id": event["claim_id"],
            "measured_source_sha": event["measured_source_sha"],
            "command_digest": event["command_digest"],
            "policy_digest": event["policy_digest"],
        }
    )
    if event["evaluation_id"] != expected_evaluation:
        raise GuardianStateError(
            "evaluation_id does not bind the measured source and policy"
        )
    transition = event.get("transition")
    if transition not in TRANSITIONS:
        raise GuardianStateError(f"invalid Guardian transition {transition!r}")
    if event["idempotency_key"] != transition_key(event["evaluation_id"], transition):
        raise GuardianStateError(
            "observation idempotency key does not bind its transition"
        )
    if transition == "resolved" and event.get("verified_source_sha") is None:
        raise GuardianStateError("resolved observation requires verified_source_sha")
    charter = event.get("charter")
    if charter is not None and (not isinstance(charter, str) or not charter.strip()):
        raise GuardianStateError("observation charter must be non-empty or null")
    if transition in {"ticket", "paused"} and not isinstance(charter, str):
        raise GuardianStateError(f"{transition} observation requires its charter")
    if transition == "escalated" and (
        not isinstance(event.get("issue_number"), int) or event["issue_number"] <= 0
    ):
        raise GuardianStateError(
            "escalated observation requires a positive issue_number"
        )
    for field in ("value", "threshold", "headroom", "policy_headroom"):
        numeric = event.get(field)
        if numeric is not None and (
            not isinstance(numeric, (int, float)) or not math.isfinite(float(numeric))
        ):
            raise GuardianStateError(f"{field} must be a finite number or null")
    repair_scope = event.get("repair_scope")
    if not isinstance(repair_scope, list) or not all(
        isinstance(item, str) and item for item in repair_scope
    ):
        raise GuardianStateError("repair_scope must be a list of non-empty paths")
    for field in ("acceptance_command", "candidate_command"):
        if event.get(field) is not None and not isinstance(event[field], str):
            raise GuardianStateError(f"{field} must be a string or null")
    quality_verdicts = event.get("quality_verdicts")
    if not isinstance(quality_verdicts, list) or len(quality_verdicts) > 20:
        raise GuardianStateError("quality_verdicts must be a bounded list")
    for verdict in quality_verdicts:
        if not isinstance(verdict, dict) or set(verdict) != {
            "claim_id",
            "status",
            "value",
            "threshold",
        }:
            raise GuardianStateError("quality_verdicts contains a malformed row")
        if not isinstance(verdict["claim_id"], str) or verdict["status"] not in {
            "VERIFIED",
            "KNOWN-GAP",
            "VIOLATED",
            "FIXED?!",
            "UNSUPPORTED",
        }:
            raise GuardianStateError("quality_verdicts contains an invalid identity")
        for field in ("value", "threshold"):
            numeric = verdict[field]
            if numeric is not None and (
                not isinstance(numeric, (int, float))
                or not math.isfinite(float(numeric))
            ):
                raise GuardianStateError(
                    f"quality_verdicts {field} must be finite or null"
                )
    token_fields = {
        "tokens_in",
        "tokens_out",
        "token_coverage",
        "token_tasks_total",
        "token_tasks_covered",
    }
    present_token_fields = token_fields.intersection(event)
    if present_token_fields:
        if present_token_fields != token_fields or transition != "resolved":
            raise GuardianStateError(
                "provider token evidence must be complete and resolved-only"
            )
        coverage = event["token_coverage"]
        total = event["token_tasks_total"]
        covered = event["token_tasks_covered"]
        tokens_in = event["tokens_in"]
        tokens_out = event["tokens_out"]
        if (
            coverage not in {"full", "partial", "none"}
            or isinstance(total, bool)
            or not isinstance(total, int)
            or total <= 0
            or isinstance(covered, bool)
            or not isinstance(covered, int)
            or not 0 <= covered <= total
        ):
            raise GuardianStateError("provider token coverage is malformed")
        has_tokens = (
            not isinstance(tokens_in, bool)
            and isinstance(tokens_in, int)
            and tokens_in >= 0
            and not isinstance(tokens_out, bool)
            and isinstance(tokens_out, int)
            and tokens_out >= 0
        )
        if (
            (coverage == "full" and (covered != total or not has_tokens))
            or (coverage == "partial" and (not 0 < covered < total or not has_tokens))
            or (
                coverage == "none"
                and (covered != 0 or tokens_in is not None or tokens_out is not None)
            )
        ):
            raise GuardianStateError("provider token evidence conflicts with coverage")
    lineage_fields = {
        "run_id",
        "implementation_task_id",
        "review_task_id",
        "security_review_task_id",
        "delivery_commit_sha",
        "pr_number",
    }
    present_lineage_fields = lineage_fields.intersection(event)
    if present_lineage_fields:
        if present_lineage_fields != lineage_fields or transition != "resolved":
            raise GuardianStateError(
                "delivery lineage must be complete and resolved-only"
            )
        security_task = event["security_review_task_id"]
        if (
            not isinstance(event["run_id"], str)
            or not re.fullmatch(r"sr_[0-9a-f]{32}", event["run_id"])
            or not isinstance(event["implementation_task_id"], str)
            or not event["implementation_task_id"]
            or not isinstance(event["review_task_id"], str)
            or not event["review_task_id"]
            or not (
                security_task is None
                or isinstance(security_task, str)
                and bool(security_task)
            )
            or not isinstance(event["delivery_commit_sha"], str)
            or not SHA_RE.fullmatch(event["delivery_commit_sha"])
            or isinstance(event["pr_number"], bool)
            or not isinstance(event["pr_number"], int)
            or event["pr_number"] <= 0
        ):
            raise GuardianStateError("delivery lineage is malformed")


def _default_audit_dir() -> Path:
    try:
        common = subprocess.check_output(
            ["git", "rev-parse", "--git-common-dir"], text=True
        ).strip()
        return Path(common).resolve().parent / kernel_settings.settings.audit_root / "agent-events"
    except (OSError, subprocess.CalledProcessError):
        return Path.cwd() / kernel_settings.settings.audit_root / "agent-events"


def read_observations(audit_dir: Path | None = None) -> tuple[dict[str, Any], ...]:
    """Read and validate every Guardian row; corruption is never skipped."""
    directory = audit_dir or _default_audit_dir()
    events: list[dict[str, Any]] = []
    try:
        for path in sorted(directory.glob("*.jsonl")) if directory.exists() else ():
            for number, line in enumerate(
                path.read_text(encoding="utf-8").splitlines(), 1
            ):
                if not line.strip():
                    continue
                try:
                    row = json.loads(line)
                except json.JSONDecodeError as exc:
                    raise GuardianStateError(
                        f"invalid JSON in {path.name}:{number}"
                    ) from exc
                if not isinstance(row, dict):
                    raise GuardianStateError(
                        f"event row in {path.name}:{number} is not an object"
                    )
                if row.get("kind") == "claim_observation":
                    validate_observation(row)
                    events.append(row)
                elif row.get("kind") == "guardian_dispatch_outcome":
                    validate_dispatch_outcome(row)
    except OSError as exc:
        raise GuardianStateError(f"cannot read Guardian observations: {exc}") from exc
    return tuple(events)


def validate_dispatch_outcome(event: Mapping[str, object]) -> None:
    """Validate the privacy-bounded dispatcher-boundary event schema."""
    required = {
        "kind",
        "schema_version",
        "ts",
        "day",
        "transition",
        "stage",
        "failure_class",
        "idempotency_key",
    }
    allowed = required | {"qualification_invocation_id"}
    if frozenset(event) not in {frozenset(required), frozenset(allowed)}:
        raise GuardianStateError("dispatcher outcome fields are invalid")
    if (
        event.get("kind") != "guardian_dispatch_outcome"
        or event.get("schema_version") != 1
        or event.get("transition") != "broken"
        or event.get("stage") != "dispatcher-boundary"
    ):
        raise GuardianStateError("unsupported dispatcher outcome schema")
    timestamp = event.get("ts")
    if not isinstance(timestamp, str):
        raise GuardianStateError("dispatcher outcome timestamp is invalid")
    try:
        parsed_timestamp = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
    except ValueError as exc:
        raise GuardianStateError("dispatcher outcome timestamp is invalid") from exc
    if parsed_timestamp.tzinfo is None or parsed_timestamp.utcoffset() != timedelta(0):
        raise GuardianStateError("dispatcher outcome timestamp must be UTC")
    failure_class = event.get("failure_class")
    if not isinstance(failure_class, str) or not FAILURE_CLASS_RE.fullmatch(
        failure_class
    ):
        raise GuardianStateError("dispatcher outcome failure class is invalid")
    qualification_invocation_id = event.get("qualification_invocation_id")
    if qualification_invocation_id is not None and (
        not isinstance(qualification_invocation_id, str)
        or not re.fullmatch(r"[0-9a-f]{32}", qualification_invocation_id)
    ):
        raise GuardianStateError("qualification invocation id is invalid")
    identity = {
        "kind": "guardian_dispatch_outcome",
        "day": timestamp[:10],
        "transition": "broken",
        "stage": "dispatcher-boundary",
        "failure_class": failure_class,
    }
    if qualification_invocation_id is not None:
        identity["qualification_invocation_id"] = qualification_invocation_id
    if event.get("day") != timestamp[:10]:
        raise GuardianStateError("dispatcher outcome day is invalid")
    if event.get("idempotency_key") != stable_digest(identity):
        raise GuardianStateError("dispatcher outcome identity is invalid")


def read_dispatch_outcomes(
    audit_dir: Path | None = None,
) -> tuple[dict[str, Any], ...]:
    """Read every privacy-bounded dispatcher failure; reject corrupt rows."""
    directory = audit_dir or _default_audit_dir()
    outcomes: list[dict[str, Any]] = []
    try:
        for path in sorted(directory.glob("*.jsonl")) if directory.exists() else ():
            for number, line in enumerate(
                path.read_text(encoding="utf-8").splitlines(), 1
            ):
                if not line.strip():
                    continue
                try:
                    row = json.loads(line)
                except json.JSONDecodeError as exc:
                    raise GuardianStateError(
                        f"invalid JSON in {path.name}:{number}"
                    ) from exc
                if not isinstance(row, dict):
                    raise GuardianStateError(
                        f"event row in {path.name}:{number} is not an object"
                    )
                if row.get("kind") == "guardian_dispatch_outcome":
                    validate_dispatch_outcome(row)
                    outcomes.append(row)
    except OSError as exc:
        raise GuardianStateError(f"cannot read dispatcher outcomes: {exc}") from exc
    return tuple(outcomes)


def reduce_observations(events: Iterable[Mapping[str, object]]) -> GuardianState:
    """Reduce immutable transitions and find slots that must be resumed."""
    rows: list[dict[str, Any]] = []
    keys: set[str] = set()
    slots: dict[str, list[dict[str, Any]]] = {}
    for incoming in events:
        row = dict(incoming)
        validate_observation(row)
        key = row["idempotency_key"]
        if key in keys:
            previous = next(
                existing for existing in rows if existing["idempotency_key"] == key
            )
            if not _same_transition(previous, row):
                raise GuardianStateError("conflicting rows share an idempotency key")
            continue
        keys.add(key)
        rows.append(row)
        if isinstance(row["slot"], str):
            slots.setdefault(row["slot"], []).append(row)
    by_evaluation: dict[str, set[str]] = {}
    for row in rows:
        by_evaluation.setdefault(str(row["evaluation_id"]), set()).add(
            str(row["transition"])
        )
    ticket_lifecycle = {"ticket", "paused", "failed", "escalated", "resolved"}
    for transitions in by_evaluation.values():
        if len(transitions) == 1:
            valid = True
        elif "ticket" in transitions:
            valid = transitions.issubset(ticket_lifecycle) and not (
                "resolved" in transitions
                and bool(transitions.intersection({"failed", "escalated"}))
            )
        else:
            valid = False
        if not valid:
            raise GuardianStateError("Guardian transition lifecycle is contradictory")
    pending = tuple(
        slot
        for slot, transitions in sorted(slots.items())
        if not any(row["transition"] in TERMINAL_TRANSITIONS for row in transitions)
    )
    completed = [
        slot
        for slot, transitions in slots.items()
        if any(row["transition"] in TERMINAL_TRANSITIONS for row in transitions)
    ]
    return GuardianState(
        events=tuple(rows),
        transition_keys=frozenset(keys),
        last_consumed_slot=max(completed) if completed else None,
        pending_slots=pending,
    )


def _same_transition(left: Mapping[str, object], right: Mapping[str, object]) -> bool:
    """Treat a retry timestamp as non-substantive; all other fields stay strict."""
    return {key: value for key, value in left.items() if key != "ts"} == {
        key: value for key, value in right.items() if key != "ts"
    }


def select_scheduled_identity(now: datetime, state: GuardianState) -> str | None:
    """Resume an unfinished slot; otherwise select the latest eligible UTC slot."""
    current = scheduled_slot(now)
    candidates = [slot for slot in state.pending_slots if slot <= current]
    if candidates:
        return candidates[0]
    if state.last_consumed_slot is not None and current <= state.last_consumed_slot:
        return None
    return current


def _fsync_directory(directory: Path) -> None:
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    directory_fd = os.open(directory, flags)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)


def _ensure_durable_directory(directory: Path) -> None:
    missing: list[Path] = []
    cursor = directory
    while not cursor.exists():
        missing.append(cursor)
        if cursor.parent == cursor:
            break
        cursor = cursor.parent
    directory.mkdir(parents=True, exist_ok=True)
    for created in reversed(missing):
        _fsync_directory(created.parent)


def _append_jsonl(path: Path, encoded: str) -> None:
    with path.open("a", encoding="utf-8") as output:
        output.write(encoded)
        output.flush()
        os.fsync(output.fileno())
    _fsync_directory(path.parent)


def append_observation(
    event: Mapping[str, object], audit_dir: Path | None = None
) -> bool:
    """Strictly append one event, fsync it, and confirm the exact reread row.

    Returns ``False`` only when an identical transition already exists.  Every
    storage or validation failure raises ``GuardianStateError`` for fail-closed
    caller handling.
    """
    row = dict(event)
    validate_observation(row)
    directory = audit_dir or _default_audit_dir()
    try:
        _ensure_durable_directory(directory)
        lock_path = directory / ".write.lock"  # shared agent_event primitive
        with lock_path.open("a+", encoding="utf-8") as lock_file:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
            existing = read_observations(directory)
            state = reduce_observations(existing)
            if row["idempotency_key"] in state.transition_keys:
                matching = [
                    existing_row
                    for existing_row in state.events
                    if existing_row["idempotency_key"] == row["idempotency_key"]
                ]
                if len(matching) == 1 and _same_transition(matching[0], row):
                    return False
                raise GuardianStateError(
                    "conflicting observation already uses this idempotency key"
                )
            reduce_observations((*state.events, row))
            day = str(row["ts"])[:10]
            if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", day):
                raise GuardianStateError(
                    "observation timestamp must start with an ISO date"
                )
            path = directory / f"{day}.jsonl"
            encoded = json.dumps(row, sort_keys=True, separators=(",", ":")) + "\n"
            _append_jsonl(path, encoded)
            # Re-read under the same lock: a buffered/partial write is broken.
            reread = read_observations(directory)
            if not any(candidate == row for candidate in reread):
                raise GuardianStateError(
                    "Guardian observation was not present after fsync/reread"
                )
            return True
    except GuardianStateError:
        raise
    except OSError as exc:
        raise GuardianStateError(
            f"cannot durably append Guardian observation: {exc}"
        ) from exc


def append_dispatch_broken(
    *,
    failure_class: str,
    audit_dir: Path | None = None,
    ts: str | None = None,
    qualification_invocation_id: str | None = None,
) -> bool:
    """Durably record a privacy-bounded dispatcher boundary failure.

    The event deliberately carries only the exception class. Exception messages
    may contain credentials, paths, or user data and never enter the audit stream.
    """
    if not isinstance(failure_class, str) or not FAILURE_CLASS_RE.fullmatch(
        failure_class
    ):
        raise GuardianStateError("dispatcher failure class is invalid")
    if qualification_invocation_id is not None and not re.fullmatch(
        r"[0-9a-f]{32}", qualification_invocation_id
    ):
        raise GuardianStateError("qualification invocation id is invalid")
    timestamp = ts or datetime.now(UTC).isoformat(timespec="seconds").replace(
        "+00:00", "Z"
    )
    try:
        parsed_timestamp = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
    except (AttributeError, ValueError) as exc:
        raise GuardianStateError("dispatcher timestamp must be valid ISO-8601") from exc
    if parsed_timestamp.tzinfo is None or parsed_timestamp.utcoffset() != timedelta(0):
        raise GuardianStateError("dispatcher timestamp must be UTC")
    day = timestamp[:10]
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", day):
        raise GuardianStateError("dispatcher timestamp must start with an ISO date")
    identity = {
        "kind": "guardian_dispatch_outcome",
        "day": day,
        "transition": "broken",
        "stage": "dispatcher-boundary",
        "failure_class": failure_class,
    }
    if qualification_invocation_id is not None:
        identity["qualification_invocation_id"] = qualification_invocation_id
    row: dict[str, object] = {
        **identity,
        "schema_version": 1,
        "ts": timestamp,
        "idempotency_key": stable_digest(identity),
    }
    validate_dispatch_outcome(row)
    directory = audit_dir or _default_audit_dir()
    try:
        _ensure_durable_directory(directory)
        lock_path = directory / ".write.lock"
        with lock_path.open("a+", encoding="utf-8") as lock_file:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
            outcomes: list[dict[str, object]] = []
            for path in sorted(directory.glob("*.jsonl")):
                for number, line in enumerate(
                    path.read_text(encoding="utf-8").splitlines(), 1
                ):
                    if not line.strip():
                        continue
                    try:
                        existing = json.loads(line)
                    except json.JSONDecodeError as exc:
                        raise GuardianStateError(
                            f"invalid JSON in {path.name}:{number}"
                        ) from exc
                    if not isinstance(existing, dict):
                        raise GuardianStateError(
                            f"event row in {path.name}:{number} is not an object"
                        )
                    if existing.get("kind") == "guardian_dispatch_outcome":
                        validate_dispatch_outcome(existing)
                        outcomes.append(existing)
            matching = [
                existing
                for existing in outcomes
                if existing.get("idempotency_key") == row["idempotency_key"]
            ]
            if matching:
                if len(matching) == 1 and _same_transition(matching[0], row):
                    return False
                raise GuardianStateError(
                    "conflicting dispatcher outcome uses this idempotency key"
                )
            path = directory / f"{day}.jsonl"
            encoded = json.dumps(row, sort_keys=True, separators=(",", ":")) + "\n"
            _append_jsonl(path, encoded)
            reread = read_dispatch_outcomes(directory)
            if not any(candidate == row for candidate in reread):
                raise GuardianStateError(
                    "Guardian dispatcher outcome was not present after fsync/reread"
                )
            return True
    except GuardianStateError:
        raise
    except OSError as exc:
        raise GuardianStateError(
            "cannot durably append Guardian dispatcher outcome"
        ) from exc


def classify_escalation(
    claim: Mapping[str, object],
    github_issues: Iterable[Mapping[str, object]] | None,
    prior_escalation: Mapping[str, object] | None = None,
) -> str:
    """Return ``tracked``, ``eligible``, or ``broken`` for escalation ownership.

    A policy-owned issue must resolve to exactly one GitHub issue.  A closed
    issue intentionally re-enables its claim.  Both GitHub and the canonical
    pending-owner queue are required inputs whenever ownership is declared.
    """
    owner = (
        prior_escalation.get("issue_number")
        if prior_escalation is not None
        else claim.get("escalation_issue", claim.get("tracked"))
    )
    if not owner:
        return "eligible"
    if github_issues is None:
        return "broken"
    token = str(owner).lstrip("#")
    matches = [
        dict(issue)
        for issue in github_issues
        if str(issue.get("number", issue.get("id", ""))) == token
    ]
    if len(matches) != 1:
        return "broken"
    issue = matches[0]
    state = str(issue.get("state", "")).lower()
    if state == "closed":
        return "eligible"
    if state != "open":
        return "broken"
    return "tracked"


def latest_escalation(
    events: Iterable[Mapping[str, object]], claim_id: str, expected_policy_digest: str
) -> dict[str, Any] | None:
    """Return the latest durable escalation owner for the current claim policy."""
    matches = [
        dict(event)
        for event in events
        if event.get("kind") == "claim_observation"
        and event.get("transition") == "escalated"
        and event.get("claim_id") == claim_id
        and event.get("policy_digest") == expected_policy_digest
    ]
    if not matches:
        return None
    latest = matches[-1]
    issue_number = latest.get("issue_number")
    if not isinstance(issue_number, int) or issue_number <= 0:
        raise GuardianStateError("escalated observation has no valid issue_number")
    return latest
