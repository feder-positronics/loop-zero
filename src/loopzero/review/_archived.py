"""Authenticate archived review witnesses for stale-source recovery only.

These witnesses settle historical review gates. They never provide active
review coverage or grant another run, family, slice, or review budget.
"""

from __future__ import annotations

import sys
from collections.abc import Mapping, Sequence
from pathlib import Path


from .authority import _ARCHIVE_VALIDATOR
from ..kernel.authority import TerminalAuthorityError, verify_terminal_authority
from ..kernel.gitscope import DispatchError

_ANCHOR_ONLY_FIELDS = frozenset(
    {
        "accepted_verdict",
        "superseded_with_result",
        "review_acceptance_verified",
        "review_gate_supersession",
        "review_gate_terminal",
    }
)
_START_BINDING_FIELDS = (
    "task_id",
    "attempt_index",
    "run_id",
    "work_unit_id",
    "worktree",
    "read_only",
)


def passing_archive_anchor(
    anchors: Sequence[dict[str, object]],
    task_id: str,
) -> dict[str, object] | None:
    """Select one checkpoint-authenticated, accepted historical review."""
    matches = [row for row in anchors if row.get("task_id") == task_id]
    if len(matches) != 1:
        return None
    anchor = matches[0]
    if (
        getattr(anchor, "checkpoint_authenticated_retention", False) is not True
        or anchor.get("type") != "attempt-terminal"
        or anchor.get("status") != "completed"
        or anchor.get("read_only") is not True
        or anchor.get("work_kind") != "review"
        or anchor.get("review_gate_terminal") is not True
        or anchor.get("review_acceptance_verified") is not True
        or anchor.get("accepted_verdict") != "pass"
    ):
        return None
    return anchor


def validate_archived_review_witness(
    witness: object,
    anchor: Mapping[str, object],
) -> dict[str, object]:
    """Bind the real signed terminal to its retained acceptance identity."""
    if not isinstance(witness, dict) or set(witness) != {"start", "terminal"}:
        raise DispatchError("archived review witness is invalid")
    start, terminal = witness["start"], witness["terminal"]
    if not isinstance(start, dict) or not isinstance(terminal, dict):
        raise DispatchError("archived review witness is invalid")
    contract = terminal.get("task_contract")
    task = contract if isinstance(contract, Mapping) else {}
    for field, expected in anchor.items():
        if field in _ANCHOR_ONLY_FIELDS:
            continue
        actual = terminal.get(field)
        if actual is None and field != "verification_verdict":
            actual = task.get(field)
        if actual != expected:
            raise DispatchError("archived review does not match retained identity")
    if (
        start.get("type") != "attempt-start"
        or terminal.get("type") != "attempt-terminal"
        or any(
            start.get(field) != terminal.get(field) for field in _START_BINDING_FIELDS
        )
        or not isinstance(start.get("terminal_authority"), dict)
    ):
        raise DispatchError("archived review registration binding is invalid")
    try:
        verify_terminal_authority(start, registration=None, expected_kind="coordinator")
        verify_terminal_authority(
            terminal,
            registration=start["terminal_authority"],
            expected_kind="dispatcher",
        )
    except TerminalAuthorityError as exc:
        raise DispatchError(f"archived review signature is invalid: {exc}") from exc
    return terminal


def passing_archive_ancestry(
    anchors: Sequence[dict[str, object]],
    task_id: str,
) -> list[dict[str, object]]:
    """Resolve one exact accepted same-delivery chain without restoring coverage."""
    target = passing_archive_anchor(anchors, task_id)
    if target is None:
        raise DispatchError("archived review proof requires accepted history")
    context_fields = (
        "run_id",
        "delivery_family_id",
        "slice_id",
        "review_chain_id",
        "root_work_unit_id",
        "review_intent",
        "worktree",
    )
    if not all(
        isinstance(target.get(field), str) and target[field] for field in context_fields
    ):
        raise DispatchError("archived review proof lacks delivery identity")

    def branch(row):
        source = row.get("source_identity")
        return source.get("ref") if isinstance(source, Mapping) else None

    source_ref = branch(target)
    if not isinstance(source_ref, str) or not source_ref.startswith("refs/heads/"):
        raise DispatchError("archived review proof lacks a named branch")
    selected = []
    seen = set()
    current = target
    while True:
        snapshot_sha = current.get("snapshot_sha")
        if snapshot_sha in seen:
            raise DispatchError("archived review ancestry contains a cycle")
        seen.add(snapshot_sha)
        selected.append(current)
        contract = current.get("task_contract")
        predecessor = (
            contract.get("delta_from_snapshot")
            if isinstance(contract, Mapping)
            else None
        )
        if not isinstance(predecessor, str):
            return selected
        matches = [
            row
            for row in anchors
            if row.get("snapshot_sha") == predecessor
            and all(row.get(field) == target.get(field) for field in context_fields)
            and branch(row) == source_ref
            and passing_archive_anchor(anchors, str(row.get("task_id"))) is not None
        ]
        if len(matches) != 1:
            raise DispatchError(
                "archived review ancestry has no unique same-delivery predecessor"
            )
        current = matches[0]


def archived_supersession_deposits(
    anchors: Sequence[dict[str, object]],
    records: Sequence[dict[str, object]],
) -> dict[tuple[str, int], list[dict[str, object]]]:
    """Provide historical deposits solely to supersession authentication."""
    live_keys = {
        (row.get("task_id"), row.get("attempt_index"))
        for row in records
        if row.get("type") == "attempt-terminal"
    }
    deposits: dict[tuple[str, int], list[dict[str, object]]] = {}
    for record in records:
        witness = record.get("archived_review_witness")
        if record.get("type") != "attempt-supersession" or witness is None:
            continue
        task_id = record.get("task_id")
        anchor = passing_archive_anchor(anchors, str(task_id))
        if anchor is None:
            continue
        try:
            terminal = validate_archived_review_witness(witness, anchor)
        except DispatchError:
            continue
        current = record.get("superseding_source_identity")
        previous = terminal.get("source_identity")
        if (
            record.get("supersession_reason") != "stale-source"
            or any(
                record.get(field) != terminal.get(field)
                for field in (
                    "task_contract",
                    "root_work_unit_id",
                    "lineage",
                    "budget_usd",
                    "cost_usd",
                    "cost_status",
                    "tokens",
                )
            )
            or not str(record.get("discarded_snapshot_bound_reason") or "").strip()
            or not isinstance(current, Mapping)
            or not isinstance(previous, Mapping)
            or current.get("ref") != previous.get("ref")
            or current == previous
            or not _ARCHIVE_VALIDATOR.get()(
                record.get("uncarryable_delta_authority"),
                predecessor_task_id=str(task_id),
                predecessor_snapshot_sha=str(terminal.get("snapshot_sha")),
                current_source=current,
            )
        ):
            continue
        key = (str(task_id), terminal["attempt_index"])
        if key not in live_keys:
            # Replayed/updated supersessions refer to the same immutable deposit.
            deposits.setdefault(key, [terminal])
    return deposits
