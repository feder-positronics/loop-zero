"""Immutable logical-run ownership and original delivery contract projections.

Storage and append transactions remain owned by skill_run_log. These pure
projections are shared by entry, review, publication and closeout readers.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime

RUN_ID_MARKER_RE = re.compile(
    r"<!--\s*skill-run-id:\s*(sr_[0-9a-f]{32})\s*-->", re.IGNORECASE
)


RUN_ID_RE = re.compile(r"^sr_[0-9a-f]{32}$")


def active_run(
    entries: list[dict[str, object]], *, git_branch: str
) -> tuple[str, str | None] | None:
    """Return the ID and optional start timestamp of the active branch run.

    The run identity is usable by ownership consumers even for legacy rows
    without timestamps. Consumers that need a temporal boundary must reject
    a missing timestamp rather than silently choosing "now".
    """
    latest_by_run: dict[str, dict[str, object]] = {}
    start_by_run: dict[str, str] = {}
    for entry in entries:
        run_id = entry.get("run_id")
        if not isinstance(run_id, str) or not RUN_ID_RE.fullmatch(run_id):
            continue
        latest_by_run[run_id] = entry
        timestamp = entry.get("ts")
        if run_id not in start_by_run and isinstance(timestamp, str):
            start_by_run[run_id] = timestamp
    matches = [
        run_id
        for run_id, entry in latest_by_run.items()
        if entry.get("git_branch") == git_branch
        and entry.get("outcome") == "in_progress"
    ]
    if not matches:
        return None
    if len(matches) > 1:
        raise ValueError(f"multiple active logical runs for branch {git_branch!r}")
    timestamp = start_by_run.get(matches[0])
    parsed_timestamp = parse_ts(timestamp) if timestamp is not None else None
    return matches[0], timestamp if parsed_timestamp is not None else None


def extract_run_id_marker(body: str) -> str | None:
    """Read the portable logical-run handoff marker from a PR body."""
    matches = {match.lower() for match in RUN_ID_MARKER_RE.findall(body)}
    return matches.pop() if len(matches) == 1 else None


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


def parse_ts(raw: str) -> datetime | None:
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None


def run_delivery_contract(entries: list[dict[str, object]], run_id: str) -> str:
    """Read the original contract; missing historical fields mean legacy authority."""
    rows = [row for row in entries if row.get("run_id") == run_id]
    if not rows:
        raise ValueError("delivery contract requires an existing run")
    contract = rows[0].get("delivery_contract", "intelflo-v1")
    if contract not in {"intelflo-v1", "loop-zero-v1"} or any(
        row.get("delivery_contract", contract) != contract for row in rows
    ):
        raise ValueError("delivery contract is invalid or changed within the run")
    return str(contract)


def bind_delivery_contract(
    entry: dict[str, object], entries: list[dict[str, object]], *, start: bool = False
) -> None:
    """Bind new tasks once; never migrate a resumed or frozen old task."""
    run_id = str(entry["run_id"])
    entry["delivery_contract"] = (
        run_delivery_contract(entries, run_id)
        if any(row.get("run_id") == run_id for row in entries)
        else "loop-zero-v1" if start else "intelflo-v1"
    )
