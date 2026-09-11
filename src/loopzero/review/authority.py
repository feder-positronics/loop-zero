"""Authenticated review, verdict, retry, and archived-witness projections."""

from __future__ import annotations

from collections.abc import Callable, Collection, Mapping, Sequence
from contextvars import ContextVar
from typing import cast

from ..kernel import seams
from ..kernel.authority import TerminalAuthorityError, verify_terminal_authority
from ..kernel.authority_projection import (
    _authenticated_attempt_terminal_ids,
    _authenticated_coordinator_record_ids,
    _authenticated_open_before_record_projections,
    _authority_record_list,
    _legacy_compatibility_record_ids,
    _recovery_matches_deposit,
    _registered_authority_before_record_ids,
    _terminal_authority_attempt_key,
    authenticated_supersessions,
    current_telemetry,
    retained_retry_outcomes,
)
from ..kernel.authority_store import load_authority_records
from ..kernel.gitscope import DispatchError
from .acceptance import review_acceptance_receipt_reasons
from .chain import review_chain_receipt_reasons
from .routing import supersession_reason_matches_terminal, verifier_identity_is_independent

_ARCHIVE_VALIDATOR: ContextVar[Callable[..., bool] | None] = ContextVar(
    "archive_delta_authority_validator", default=None
)
_ANCHOR_ONLY_FIELDS = frozenset(
    {"accepted_verdict", "superseded_with_result", "review_acceptance_verified",
     "review_gate_supersession", "review_gate_terminal"}
)
_START_BINDING_FIELDS = (
    "task_id", "attempt_index", "run_id", "work_unit_id", "worktree", "read_only"
)
_OUTCOME_TYPES = frozenset(
    {"attempt-terminal", "attempt-recovery", "attempt-abort", "attempt-timeout"}
)


def configure(*, uncarryable_delta_authority_is_valid: Callable[..., bool] | None = None) -> None:
    """Install the excluded delivery controller's archive validation seam."""
    _ARCHIVE_VALIDATOR.set(uncarryable_delta_authority_is_valid)


def _latest_attempt_settlement_indices(
    records: Sequence[dict[str, object]],
    record_types: Collection[str] = ("attempt-terminal", "attempt-abort", "inline"),
) -> frozenset[int]:
    latest: dict[str, tuple[int, int]] = {}
    for index, record in enumerate(records):
        if record.get("type") not in record_types:
            continue
        task_id = record.get("task_id")
        attempt = record.get("attempt_index")
        if attempt is None:
            attempt = 0 if record.get("type") == "inline" else -1
        if not isinstance(task_id, str) or not task_id or type(attempt) is not int:
            continue
        previous = latest.get(task_id)
        if previous is None or attempt >= previous[0]:
            latest[task_id] = (attempt, index)
    return frozenset(index for _attempt, index in latest.values())


def passing_archive_anchor(
    anchors: Sequence[dict[str, object]], task_id: str
) -> dict[str, object] | None:
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
    witness: object, anchor: Mapping[str, object]
) -> dict[str, object]:
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
        or any(start.get(field) != terminal.get(field) for field in _START_BINDING_FIELDS)
        or not isinstance(start.get("terminal_authority"), dict)
    ):
        raise DispatchError("archived review registration binding is invalid")
    try:
        verify_terminal_authority(start, registration=None, expected_kind="coordinator")
        verify_terminal_authority(
            terminal, registration=start["terminal_authority"], expected_kind="dispatcher"
        )
    except TerminalAuthorityError as exc:
        raise DispatchError(f"archived review signature is invalid: {exc}") from exc
    return terminal


def passing_archive_ancestry(
    anchors: Sequence[dict[str, object]], task_id: str
) -> list[dict[str, object]]:
    target = passing_archive_anchor(anchors, task_id)
    if target is None:
        raise DispatchError("archived review proof requires accepted history")
    context = (
        "run_id", "delivery_family_id", "slice_id", "review_chain_id",
        "root_work_unit_id", "review_intent", "worktree",
    )
    if not all(isinstance(target.get(field), str) and target[field] for field in context):
        raise DispatchError("archived review proof lacks delivery identity")

    def branch(row):
        source = row.get("source_identity")
        return source.get("ref") if isinstance(source, Mapping) else None

    source_ref = branch(target)
    if not isinstance(source_ref, str) or not source_ref.startswith("refs/heads/"):
        raise DispatchError("archived review proof lacks a named branch")
    selected, seen, current = [], set(), target
    while True:
        snapshot = current.get("snapshot_sha")
        if snapshot in seen:
            raise DispatchError("archived review ancestry contains a cycle")
        seen.add(snapshot)
        selected.append(current)
        contract = current.get("task_contract")
        predecessor = contract.get("delta_from_snapshot") if isinstance(contract, Mapping) else None
        if not isinstance(predecessor, str):
            return selected
        matches = [
            row for row in anchors
            if row.get("snapshot_sha") == predecessor
            and all(row.get(field) == target.get(field) for field in context)
            and branch(row) == source_ref
            and passing_archive_anchor(anchors, str(row.get("task_id"))) is not None
        ]
        if len(matches) != 1:
            raise DispatchError("archived review ancestry has no unique same-delivery predecessor")
        current = matches[0]


def archived_supersession_deposits(
    anchors: Sequence[dict[str, object]], records: Sequence[dict[str, object]]
) -> dict[tuple[str, int], list[dict[str, object]]]:
    validator = _ARCHIVE_VALIDATOR.get()
    if validator is None:
        return {}
    live_keys = {
        (row.get("task_id"), row.get("attempt_index"))
        for row in records if row.get("type") == "attempt-terminal"
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
        current, previous = record.get("superseding_source_identity"), terminal.get("source_identity")
        if (
            record.get("supersession_reason") != "stale-source"
            or not isinstance(current, Mapping) or not isinstance(previous, Mapping)
            or current.get("ref") != previous.get("ref") or current == previous
            or not validator(
                record.get("uncarryable_delta_authority"),
                predecessor_task_id=str(task_id),
                predecessor_snapshot_sha=str(terminal.get("snapshot_sha")),
                current_source=current,
            )
        ):
            continue
        attempt = terminal.get("attempt_index")
        if type(attempt) is int and (str(task_id), attempt) not in live_keys:
            deposits.setdefault((str(task_id), attempt), [terminal])
    return deposits


def review_terminal_acceptance_reasons(
    record: dict[str, object], *, allow_advisory: bool = False
) -> tuple[str, ...]:
    reasons: list[str] = []
    if record.get("type") not in {"attempt-terminal", "attempt-recovery"}:
        reasons.append("not-review-terminal")
    if record.get("status") != "completed":
        reasons.append("review-not-completed")
    if record.get("read_only") is not True:
        reasons.append("review-not-read-only")
    if record.get("work_kind") != "review":
        reasons.append("not-review-work-kind")
    if record.get("advisory") is True and not allow_advisory:
        reasons.append("advisory-review")
    reasons.extend(review_acceptance_receipt_reasons(record))
    receipt = record.get("acceptance_receipt")
    if isinstance(receipt, dict):
        phase = "recovery-time" if record.get("type") == "attempt-recovery" else "pre-model"
        if receipt.get("provenance") != phase:
            reasons.append("review-acceptance-wrong-phase")
    contract = record.get("task_contract")
    if isinstance(contract, dict) and "required_sections" in contract:
        chain = record.get("review_chain_receipt")
        if not isinstance(chain, dict):
            reasons.append("missing-review-chain-receipt")
        else:
            reasons.extend(
                f"review-chain-{reason}" for reason in review_chain_receipt_reasons(
                    chain, task={**contract, "task_id": record.get("task_id")},
                    snapshot_sha=str(record.get("snapshot_sha") or ""),
                    snapshot_tree_sha=str(record.get("snapshot_tree_sha") or ""),
                    patch_identity=(cast(dict[str, object], record["patch_identity"])
                                    if isinstance(record.get("patch_identity"), dict) else None),
                )
            )
    return tuple(dict.fromkeys(reasons))


def _review_terminal_authority_projection(
    records: Sequence[dict[str, object]], *, _superseded_task_ids: Collection[str] | None = None
) -> tuple[Collection[str], frozenset[int], frozenset[int]]:
    history = _authority_record_list(records)
    governed = current_telemetry(history)
    open_ids, supersession_open_ids = _authenticated_open_before_record_projections(history)
    registered = _registered_authority_before_record_ids(history)
    terminals = _authenticated_attempt_terminal_ids(
        history, _open_before_record_ids=open_ids, _registered_before_record_ids=registered
    )
    coordinators = _authenticated_coordinator_record_ids(history)
    legacy = _legacy_compatibility_record_ids(history)
    superseded = _superseded_task_ids
    if superseded is None:
        superseded = {
            str(row.get("superseded_task_id") or row.get("task_id"))
            for row in authenticated_supersessions(
                history, _open_before_record_ids=open_ids,
                _supersession_open_before_record_ids=supersession_open_ids,
                _registered_before_record_ids=registered,
                _authenticated_terminal_ids=terminals,
                _authenticated_coordinator_ids=coordinators,
                _legacy_compatibility_ids=legacy,
            )
            if row.get("superseded_task_id") or row.get("task_id")
        }
    deposits: dict[tuple[str, int], list[dict[str, object]]] = {}
    recoveries: set[int] = set()
    for row in governed:
        key = _terminal_authority_attempt_key(row)
        if row.get("type") == "attempt-terminal" and key is not None and id(row) in terminals:
            deposits.setdefault(key, []).append(row)
        elif row.get("type") == "attempt-recovery" and key is not None:
            matches = deposits.get(key, [])
            if len(matches) == 1 and _recovery_matches_deposit(row, matches[0]):
                if id(row) in coordinators or id(row) in legacy:
                    recoveries.add(id(row))
    return superseded, terminals, frozenset(recoveries)


def authenticated_review_terminals(
    records: Sequence[dict[str, object]], *, _superseded_task_ids: Collection[str] | None = None
) -> dict[str, dict[str, object]]:
    superseded, terminals, recoveries = _review_terminal_authority_projection(
        records, _superseded_task_ids=_superseded_task_ids
    )
    latest: dict[str, dict[str, object]] = {}
    for row in current_telemetry(records):
        task = row.get("task_id")
        valid = row.get("type") == "attempt-terminal" and id(row) in terminals
        valid = valid or row.get("type") == "attempt-recovery" and id(row) in recoveries
        if isinstance(task, str) and task not in superseded and valid:
            latest[task] = row
    return latest


def accepted_review_terminals(
    records: Sequence[dict[str, object]], *, allow_advisory: bool = False,
    _superseded_task_ids: Collection[str] | None = None,
) -> dict[str, dict[str, object]]:
    authenticated = authenticated_review_terminals(
        records, _superseded_task_ids=_superseded_task_ids
    )
    return {
        task: row for task, row in authenticated.items()
        if not review_terminal_acceptance_reasons(row, allow_advisory=allow_advisory)
    }


def accepted_review_producers(
    records: Sequence[dict[str, object]],
) -> dict[str, dict[str, object]]:
    """Bind accepted recovery evidence to its authenticated original producer."""
    accepted = accepted_review_terminals(records)
    terminal_ids = _authenticated_attempt_terminal_ids(records)
    producers: dict[str, dict[str, object]] = {}
    for task, row in accepted.items():
        if row.get("type") != "attempt-recovery":
            producers[task] = row
            continue
        deposits = [
            candidate for candidate in records
            if candidate.get("type") == "attempt-terminal"
            and id(candidate) in terminal_ids
            and _recovery_matches_deposit(row, candidate)
        ]
        if len(deposits) == 1:
            producers[task] = deposits[0]
    return producers


def authenticated_verdicts(
    records: Sequence[dict[str, object]], *,
    _accepted_terminals: Mapping[str, dict[str, object]] | None = None,
) -> dict[str, dict[str, object]]:
    accepted = _accepted_terminals or accepted_review_terminals(records)
    history = _authority_record_list(records)
    coordinator_ids = _authenticated_coordinator_record_ids(history)
    legacy_ids = _legacy_compatibility_record_ids(history)
    seen: set[str] = set()
    latest: dict[str, dict[str, object]] = {}
    for row in current_telemetry(history):
        task = row.get("task_id")
        if not isinstance(task, str):
            continue
        terminal = accepted.get(task)
        if row is terminal:
            seen.add(task)
        elif (
            row.get("type") == "verdict" and task in seen
            and row.get("verdict") in {"pass", "fail"}
            and row.get("run_id") == terminal.get("run_id")
            and verifier_identity_is_independent(
                worker_identity=terminal.get("worker_identity"),
                worker_alias=terminal.get("effective_alias") or terminal.get("alias"),
                worker_model=terminal.get("runtime_effective_model") or terminal.get("model"),
                verifier_identity=row.get("verifier_identity"),
                verifier_alias=row.get("verifier_alias"),
            )
            and (id(row) in coordinator_ids or id(row) in legacy_ids)
        ):
            latest[task] = row
    return latest


def authenticated_retry_outcomes(records: Sequence[dict[str, object]]) -> list[dict[str, object]]:
    _, terminal_ids, recovery_ids = _review_terminal_authority_projection(records)
    accepted = retained_retry_outcomes(records)
    for row in current_telemetry(records):
        if row.get("type") not in _OUTCOME_TYPES:
            continue
        if id(row) in terminal_ids or id(row) in recovery_ids:
            accepted.append(row)
    indices = _latest_attempt_settlement_indices(accepted, _OUTCOME_TYPES)
    return [row for index, row in enumerate(accepted) if index in indices]


def latest_explicit_alias_availability(
    records: Sequence[dict[str, object]], *, alias: str
) -> dict[str, object] | None:
    for row in reversed(current_telemetry(records)):
        if row.get("type") == "alias-availability" and (
            row.get("effective_alias") or row.get("alias")
        ) == alias:
            return row
    return None


def delivery_controller_records(records: Sequence[dict[str, object]]) -> list[dict[str, object]]:
    """Expose only authenticated review/controller evidence to the excluded controller."""
    history = _authority_record_list(records)
    coordinator_ids = _authenticated_coordinator_record_ids(history)
    legacy_ids = _legacy_compatibility_record_ids(history)
    accepted = accepted_review_terminals(history)
    verdict_ids = {id(row) for row in authenticated_verdicts(history, _accepted_terminals=accepted).values()}
    accepted_ids = {id(row) for row in accepted.values()}
    supersession_ids = {id(row) for row in authenticated_supersessions(history)}
    return [
        row for row in current_telemetry(history)
        if id(row) in accepted_ids or id(row) in verdict_ids or id(row) in supersession_ids
        or (row.get("type") == "delivery-control" and (id(row) in coordinator_ids or id(row) in legacy_ids))
    ]


def configure_kernel_seams() -> None:
    """Bind the kernel's declared A4 injection points to this package."""
    seams.configure(
        _latest_attempt_settlement_indices=_latest_attempt_settlement_indices,
        accepted_review_terminals=accepted_review_terminals,
        archived_supersession_deposits=archived_supersession_deposits,
        authenticated_retry_outcomes=authenticated_retry_outcomes,
        authenticated_review_terminals=authenticated_review_terminals,
        authenticated_supersessions=authenticated_supersessions,
        authenticated_verdicts=authenticated_verdicts,
        delivery_controller_records=delivery_controller_records,
        latest_explicit_alias_availability=latest_explicit_alias_availability,
        load_authority_records=load_authority_records,
        passing_archive_ancestry=passing_archive_ancestry,
        passing_archive_anchor=passing_archive_anchor,
        supersession_reason_matches_terminal=supersession_reason_matches_terminal,
        validate_archived_review_witness=validate_archived_review_witness,
    )


__all__ = [
    "accepted_review_producers", "accepted_review_terminals", "archived_supersession_deposits",
    "authenticated_retry_outcomes", "authenticated_review_terminals",
    "authenticated_supersessions", "authenticated_verdicts", "configure",
    "configure_kernel_seams", "delivery_controller_records",
    "latest_explicit_alias_availability", "passing_archive_ancestry",
    "passing_archive_anchor", "review_terminal_acceptance_reasons",
    "validate_archived_review_witness",
]
