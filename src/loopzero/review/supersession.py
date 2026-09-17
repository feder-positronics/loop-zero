"""Read-only compatibility for authenticated legacy supersession lineage."""

from collections.abc import Sequence

from ..kernel.authority_projection import (
    _authenticated_attempt_terminal_registrations,
    authenticated_coordinator_record_ids,
)
from ..kernel.gitscope import task_contract_hash
from .authority import authenticated_review_terminals
from .ordinary_findings import ADMISSION_TYPE, authenticated_capture_admissions

_LINEAGE_FIELDS = (
    "task_id",
    "work_unit_id",
    "root_work_unit_id",
    "unit_attempt_number",
    "task_contract_hash",
)
_START_FIELDS = (
    *_LINEAGE_FIELDS,
    "run_id",
    "attempt_index",
    "worktree",
    "source_identity",
    "snapshot_sha",
    "snapshot_tree_sha",
    "patch_identity",
    "task_contract",
    "work_kind",
    "read_only",
    "review_intent",
)


def effective_supersession_lineage(
    records: Sequence[dict[str, object]],
    *,
    task_id: str,
) -> dict[str, object] | None:
    """Validate lineage without granting supersession or modifying signed rows.

    Consumers use this only in their missing-lineage guard and retain the exact
    original terminal fields when constructing a supersession. Existing signed
    nonempty lineage is opaque and unchanged. Null legacy lineage is derived
    only from a unique authenticated start/terminal and any referenced capture;
    caller-supplied lineage and unsigned historical compatibility never qualify.
    All existing supersession admission and FL-2 checks remain required.
    """
    terminal = authenticated_review_terminals(records).get(task_id)
    if terminal is None:
        return None
    registrations = _authenticated_attempt_terminal_registrations(records)
    start = registrations.get(id(terminal))
    if start is None or id(start) not in authenticated_coordinator_record_ids(records):
        return None
    if any(
        sum(
            row.get("type") == kind and row.get("task_id") == task_id for row in records
        )
        != 1
        for kind in ("attempt-start", "attempt-terminal")
    ):
        return None
    lineage = terminal.get("lineage")
    if isinstance(lineage, dict) and lineage:
        return dict(lineage)
    if lineage is not None or start.get("lineage") is not None:
        return None
    if (
        terminal.get("status") != "completed"
        or terminal.get("work_kind") != "review"
        or terminal.get("read_only") is not True
        or terminal.get("advisory") is True
        or terminal.get("review_intent") != "delivery-code-review"
    ):
        return None
    if any(
        terminal.get(key) is None or terminal.get(key) != start.get(key)
        for key in _START_FIELDS
    ):
        return None
    if any(
        not isinstance(terminal.get(key), str) or not terminal[key]
        for key in (
            "task_id",
            "work_unit_id",
            "root_work_unit_id",
            "run_id",
            "worktree",
            "result_artifact",
            "result_sha256",
        )
    ):
        return None
    if (
        type(terminal.get("unit_attempt_number")) is not int
        or terminal["unit_attempt_number"] < 1
        or type(terminal.get("attempt_index")) is not int
        or terminal["attempt_index"] < 0
    ):
        return None
    contract = terminal.get("task_contract")
    if not isinstance(contract, dict) or not contract:
        return None
    if task_contract_hash(contract) != terminal.get("task_contract_hash"):
        return None
    if any(
        key in contract and contract[key] != terminal.get(key)
        for key in (*_LINEAGE_FIELDS, "run_id", "attempt_index")
    ):
        return None
    if contract.get("root_work_unit_id") != terminal["root_work_unit_id"]:
        return None
    # A review snapshot may be a synthetic commit of the authenticated source
    # tree. Bind the patch to the source commit and the snapshot to its tree.
    source, patch = terminal.get("source_identity"), terminal.get("patch_identity")
    if (
        not isinstance(source, dict)
        or not source.get("ref")
        or not _hex_digest(source.get("head"), 40)
        or not isinstance(patch, dict)
        or patch.get("candidate_sha") != source.get("head")
        or patch.get("candidate_tree_sha") != terminal.get("snapshot_tree_sha")
    ):
        return None
    if any(
        not _hex_digest(terminal.get(key), length)
        for key, length in (
            ("snapshot_sha", 40),
            ("snapshot_tree_sha", 40),
            ("result_sha256", 64),
        )
    ):
        return None
    captures = [
        row
        for row in records
        if row.get("type") == ADMISSION_TYPE and row.get("task_id") == task_id
    ]
    if (
        captures
        or terminal.get("finding_capture_receipt")
        or terminal.get("provisional_owner_id")
    ):
        if len(captures) != 1:
            return None
        capture = captures[0]
        if authenticated_capture_admissions(records).get(task_id) is not capture:
            return None
        if any(
            capture.get(key) != terminal.get(key)
            for key in (
                *_LINEAGE_FIELDS,
                "run_id",
                "attempt_index",
                "source_identity",
                "snapshot_sha",
                "snapshot_tree_sha",
                "patch_identity",
                "result_artifact",
                "result_sha256",
                "lineage",
            )
        ):
            return None
    return {key: terminal[key] for key in _LINEAGE_FIELDS}


def _hex_digest(value: object, length: int) -> bool:
    return (
        isinstance(value, str)
        and len(value) == length
        and all(character in "0123456789abcdef" for character in value)
    )
