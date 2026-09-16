"""One prospective, owner-authorized exception to exhausted review retries.

All mutating consumers hold the authority ledger lock, reload history, call these
validators, sign/append/fsync returned records, and only then release the lock.
The recovery reservation spends the grant even if launch never occurs. Consumers
must separately admit the named route through their unchanged budget and circuit
checks before reservation and start. A grant is neither readiness nor execution
proof, and does not authorize fallback or a second replacement.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

from ..kernel import authority_projection as projection
from ..kernel.authority_store import _authority_repository_binding
from ..kernel.canonical import canonical_record_digest
from ..kernel.gitscope import DispatchError, primary_repo_root, task_contract_hash
from ..kernel.review_state import assert_authority_ledger_lock_held
from ..kernel.worktree_lease import source_identity as live_source_identity
from ..runners.contract import ReviewOutcome

OUTAGE_RECOVERY_TYPE = "provider-outage-recovery-authorization-v1"


class OutageRecoveryRefused(DispatchError):
    """The complete authenticated recovery contract was not established."""


def _require(condition, message):
    if not condition:
        raise OutageRecoveryRefused(message)


def _model(value):
    _require(isinstance(value, str) and bool(value.strip()), "model identity missing")
    normalized = "-".join(value.casefold().split())
    _require(
        normalized not in {"auto", "cursor-auto", "unknown"}, "opaque model identity"
    )
    return normalized


def _route(route):
    _require(isinstance(route, Mapping), "replacement route missing")
    _require(
        set(route) == {"engine", "model", "effort"},
        "replacement must name one exact route",
    )
    _require(
        all(isinstance(v, str) and v.strip() for v in route.values()),
        "replacement route incomplete",
    )
    _model(route["model"])
    return dict(route)


def _actual_model(terminal):
    model = _model(terminal.get("runtime_effective_model"))
    engine = terminal.get("engine")
    _require(isinstance(engine, str) and bool(engine), "executor engine missing")
    _require(
        terminal.get("worker_identity")
        == f"{engine}:{terminal['runtime_effective_model']}",
        "executor actual identity mismatch",
    )
    return model


def _signed(records):
    ids = projection.authenticated_coordinator_record_ids(records)
    return [row for row in records if id(row) in ids]


def _terminal(records, digest):
    matches = [
        row
        for row in records
        if row.get("type") == "attempt-terminal"
        and canonical_record_digest(row) == digest
    ]
    _require(len(matches) == 1, "exact terminal receipt missing")
    terminal = matches[0]
    _require(isinstance(terminal.get("worktree"), str), "terminal worktree missing")
    projection.validate_serial_terminal_authority(
        records, terminal=terminal, worktree=Path(terminal["worktree"])
    )
    return terminal


def _obligation(row):
    return tuple(row.get(key) for key in ("generation_id", "family", "slot_kind"))


def _binding(
    repository,
    records,
    *,
    task_id,
    run_id,
    source_identity,
    generation_id,
    family,
    slot_kind,
    replacement_route,
    consumed_key=None,
):
    assert_authority_ledger_lock_held(repository)
    route = _route(replacement_route)
    generation = projection.generations(records).get(generation_id)
    _require(generation is not None, "review generation not authenticated")
    _require(
        generation.repository_binding == _authority_repository_binding(repository),
        "generation repository mismatch",
    )
    state = projection.slot_state(records, generation_id, family)
    reservations = [r for r in state.reservations if r.slot_kind == slot_kind]
    consumed = (
        [r for r in reservations if r.idempotency_key == consumed_key]
        if consumed_key
        else []
    )
    _require(len(consumed) <= 1, "duplicate recovery reservation")
    ordinary = [r for r in reservations if r not in consumed]
    _require(
        len(ordinary) == 2
        and all(not r.idempotency_key.startswith("outage:") for r in ordinary),
        "exactly two original attempts required",
    )
    _require(
        not (
            state.primary_consumed if slot_kind == "primary" else state.delta_consumed
        ),
        "review already consumed",
    )
    _require(
        slot_kind == "primary" or (slot_kind == "delta" and state.primary_consumed),
        "delta requires primary",
    )
    _require(
        state.outstanding is None or state.outstanding in consumed,
        "review attempt outstanding",
    )
    terminals = []
    for reservation in ordinary:
        settlement = state.settlement_for(reservation.reservation_id)
        _require(
            settlement is not None
            and state._effective_outcome(settlement) is ReviewOutcome.RELEASED,
            "failed attempt not released",
        )
        terminal = _terminal(records, settlement.terminal_ref)
        _require(
            terminal.get("status") == "infrastructure-failure"
            and terminal.get("model_output_seen") is False,
            "prospective empty infrastructure failure required",
        )
        reason = terminal.get("runtime_terminal_reason")
        _require(
            reason
            in {
                "startup-failure",
                "transport-disconnect",
                "subscription-unavailable",
                "missing-terminal-event",
            }
            or (
                reason == "process-exit"
                and terminal.get("failure_class")
                in {"engine-output", "engine-output-failure"}
            ),
            "failure is not an empty provider outage",
        )
        _require(
            not any(
                terminal.get(key)
                for key in (
                    "verification_verdict",
                    "result_sha256",
                    "result_artifact",
                    "structured_output",
                    "final_output",
                    "findings",
                    "output_identity",
                    "checkpoint_identity",
                    "checkpoint_id",
                )
            )
            and terminal.get("deposit_state") in {None, "none"},
            "partial output or verdict cannot be recovered",
        )
        _require(
            not any(
                terminal.get(key)
                for key in (
                    "model_result_reason",
                    "runtime_fallback_from",
                    "fallback_used",
                    "scope_violations",
                    "head_moved",
                    "width_exceeded",
                    "trust_claim_receipt",
                )
            ),
            "contradictory empty-failure evidence",
        )
        usage = terminal.get("runtime_usage")
        _require(
            usage is None
            or (
                isinstance(usage, Mapping)
                and all(
                    usage.get(k) is None or (type(usage[k]) is int and usage[k] == 0)
                    for k in ("output_tokens", "reasoning_tokens")
                )
            ),
            "output usage contradicts empty failure",
        )
        _require(
            terminal.get("read_only") is True and terminal.get("work_kind") == "review",
            "not an independent review",
        )
        _require(
            terminal.get("run_id") == run_id
            and terminal.get("source_identity") == dict(source_identity),
            "failed attempt source/run mismatch",
        )
        contract = terminal.get("task_contract")
        _require(
            isinstance(contract, dict)
            and task_contract_hash(contract) == terminal.get("task_contract_hash"),
            "failed receipt immutable contract mismatch",
        )
        _actual_model(terminal)
        terminals.append(terminal)
    latest = terminals[-1]
    _require(
        all(
            t.get("worktree") == latest.get("worktree")
            and t.get("review_generation_id") == generation_id
            and t.get("review_lineage_id") == generation.lineage_id
            and t.get("review_family") == family
            and t.get("review_slot_kind") == slot_kind
            for t in terminals
        ),
        "failed receipt review obligation mismatch",
    )
    _require(
        latest.get("task_id") == task_id, "recovery must preserve failed task identity"
    )
    contract = latest.get("task_contract")
    _require(
        isinstance(contract, dict)
        and task_contract_hash(contract) == latest.get("task_contract_hash"),
        "immutable review contract missing",
    )
    _require(
        all(
            t.get("task_contract_hash") == latest["task_contract_hash"]
            for t in terminals
        ),
        "review contract changed",
    )
    for key in ("work_unit_id", "root_work_unit_id"):
        value = contract.get(key, contract.get("work_unit_id"))
        _require(
            isinstance(value, str) and value and latest.get(key) == value,
            "immutable work unit mismatch",
        )
    executor_ref = contract.get("reviewed_executor_terminal_ref")
    _require(isinstance(executor_ref, str), "reviewed executor receipt missing")
    executor = _terminal(records, executor_ref)
    _require(
        executor.get("run_id") == run_id
        and executor.get("status") == "completed"
        and executor.get("read_only") is False
        and executor.get("work_kind") != "review",
        "reviewed executor is not a completed same-run writer",
    )
    _require(
        executor.get("output_identity") == dict(source_identity),
        "executor did not produce reviewed source",
    )
    _require(
        _model(route["model"])
        not in {_actual_model(executor), *(_actual_model(t) for t in terminals)},
        "replacement is not independent",
    )
    worktree = latest.get("worktree")
    _require(
        isinstance(worktree, str)
        and live_source_identity(Path(worktree)) == dict(source_identity),
        "live source changed",
    )
    _require(
        primary_repo_root(Path(worktree)) == primary_repo_root(repository),
        "worktree repository mismatch",
    )
    _require(
        not projection._authenticated_open_dispatch_attempts(
            records, worktree=Path(worktree)
        ),
        "authenticated attempt is active",
    )
    _require(
        not projection.open_write_units(records, worktree=Path(worktree)),
        "writer is active",
    )
    index = latest.get("attempt_index")
    _require(type(index) is int and index >= 0, "attempt number missing")
    from .authority import authenticated_retry_outcomes

    unit_numbers = [
        row.get("unit_attempt_number")
        for row in authenticated_retry_outcomes(records)
        if row.get("run_id") == run_id
        and row.get("work_unit_id") == latest["work_unit_id"]
    ]
    _require(
        unit_numbers
        and all(type(number) is int and number > 0 for number in unit_numbers),
        "work unit attempt counter unavailable",
    )
    return {
        "repository_binding": _authority_repository_binding(repository),
        "worktree": worktree,
        "source_identity": dict(source_identity),
        "run_id": run_id,
        "task_id": task_id,
        "work_unit_id": latest["work_unit_id"],
        "root_work_unit_id": latest["root_work_unit_id"],
        "task_contract_hash": latest["task_contract_hash"],
        "evidence_manifest": latest.get("evidence_manifest"),
        "reviewed_executor_terminal_ref": executor_ref,
        "generation_id": generation_id,
        "lineage_id": generation.lineage_id,
        "family": family,
        "slot_kind": slot_kind,
        "failed_terminal_refs": [canonical_record_digest(t) for t in terminals],
        "failed_reservation_ids": [r.reservation_id for r in ordinary],
        "replacement_route": route,
        "next_attempt_index": index + 1,
        "next_unit_attempt_number": max(unit_numbers) + 1,
    }


def prepare_outage_recovery(
    repository,
    records,
    *,
    task_id,
    run_id,
    source_identity,
    generation_id,
    family,
    slot_kind,
    replacement_route,
    reason,
):
    """Return an unsigned grant; caller signs/appends/fsyncs under this lock."""
    binding = _binding(
        repository,
        records,
        task_id=task_id,
        run_id=run_id,
        source_identity=source_identity,
        generation_id=generation_id,
        family=family,
        slot_kind=slot_kind,
        replacement_route=replacement_route,
    )
    _require(
        isinstance(reason, str) and bool(reason.strip()),
        "explicit owner reason required",
    )
    _require(
        not any(
            row.get("type") == OUTAGE_RECOVERY_TYPE
            and _obligation(row) == _obligation(binding)
            for row in records
        ),
        "obligation already has a recovery authorization",
    )
    return {
        **binding,
        "type": OUTAGE_RECOVERY_TYPE,
        "schema": OUTAGE_RECOVERY_TYPE,
        "schema_version": projection.TELEMETRY_SCHEMA_VERSION,
        "policy_version": projection.DISPATCH_POLICY_VERSION,
        "runtime_contract_version": projection.RUNTIME_CONTRACT_VERSION,
        "maximum_uses": 1,
        "reason": reason,
    }


def validate_outage_recovery(
    repository,
    records,
    *,
    authorization_sha256,
    task,
    source_identity,
    run_id,
    generation_id,
    family,
    slot_kind,
    route,
    attempt_index,
    allow_consumed_reservation=False,
):
    """Validate one exact grant before reservation; no authority from flags."""
    assert_authority_ledger_lock_held(repository)
    grants = [
        r
        for r in records
        if r.get("type") == OUTAGE_RECOVERY_TYPE
        and _obligation(r) == (generation_id, family, slot_kind)
    ]
    _require(
        len(grants) == 1 and any(grants[0] is r for r in _signed(records)),
        "unique authenticated authorization required",
    )
    grant = grants[0]
    _require(
        projection.is_current_telemetry(grant), "authorization policy is not governed"
    )
    _require(
        canonical_record_digest(grant) == authorization_sha256,
        "authorization digest mismatch",
    )
    _require(
        grant.get("schema") == OUTAGE_RECOVERY_TYPE
        and type(grant.get("maximum_uses")) is int
        and grant["maximum_uses"] == 1
        and isinstance(grant.get("reason"), str)
        and bool(grant["reason"].strip()),
        "invalid single-use authorization",
    )
    contract = task.get("task_contract", task)
    _require(
        isinstance(contract, dict)
        and task_contract_hash(contract) == grant.get("task_contract_hash"),
        "recovery immutable task changed",
    )
    _require(
        task.get("task_id") == grant.get("task_id")
        and attempt_index == grant.get("next_attempt_index")
        and type(attempt_index) is int
        and type(task.get("unit_attempt_number")) is int
        and task["unit_attempt_number"] == grant.get("next_unit_attempt_number"),
        "recovery attempt identity changed",
    )
    binding = _binding(
        repository,
        records,
        task_id=task["task_id"],
        run_id=run_id,
        source_identity=source_identity,
        generation_id=generation_id,
        family=family,
        slot_kind=slot_kind,
        replacement_route=route,
        consumed_key="outage:" + authorization_sha256
        if allow_consumed_reservation
        else None,
    )
    _require(
        all(grant.get(k) == v for k, v in binding.items()),
        "authorization binding changed",
    )
    _require(
        task.get("evidence_manifest", contract.get("evidence_manifest"))
        == grant.get("evidence_manifest"),
        "review evidence changed",
    )
    return grant


def validate_outage_recovery_start(repository, records, *, reservation_id, **kwargs):
    """Consumer start boundary; ordinary route circuit/budget checks still apply."""
    grant = validate_outage_recovery(
        repository, records, allow_consumed_reservation=True, **kwargs
    )
    key = "outage:" + canonical_record_digest(grant)
    reservations = [
        r
        for r in _signed(records)
        if r.get("type") == "review-slot-reservation-v1"
        and r.get("idempotency_key") == key
    ]
    _require(
        len(reservations) == 1
        and reservations[0].get("reservation_id") == reservation_id
        and reservations[0].get("task_id") == grant["task_id"]
        and _obligation(reservations[0]) == _obligation(grant),
        "recovery reservation mismatch",
    )
    _require(
        not any(
            r.get("type") == "attempt-start"
            and r.get("review_reservation_id") == reservation_id
            for r in _signed(records)
        ),
        "recovery launch already consumed",
    )
    return grant


def outage_recovery_terminal_matches(records, terminal):
    """Guard verdict acceptance without recursively classifying slot outcomes."""
    signed = _signed(records)
    reservations = [
        r
        for r in signed
        if r.get("type") == "review-slot-reservation-v1"
        and r.get("reservation_id") == terminal.get("review_reservation_id")
        and str(r.get("idempotency_key", "")).startswith("outage:")
    ]
    if not reservations:
        bound_grants = [
            r
            for r in signed
            if r.get("type") == OUTAGE_RECOVERY_TYPE
            and r.get("task_id") == terminal.get("task_id")
            and r.get("run_id") == terminal.get("run_id")
            and canonical_record_digest(terminal)
            not in r.get("failed_terminal_refs", ())
        ]
        return not terminal.get("outage_authorization_sha256") and not bound_grants
    if len(reservations) != 1:
        return False
    digest = reservations[0]["idempotency_key"][7:]
    grants = [
        r
        for r in signed
        if r.get("type") == OUTAGE_RECOVERY_TYPE
        and canonical_record_digest(r) == digest
    ]
    starts = [
        r
        for r in signed
        if r.get("type") == "attempt-start"
        and r.get("review_reservation_id") == terminal.get("review_reservation_id")
    ]
    if len(grants) != 1 or len(starts) != 1:
        return False
    grant, start = grants[0], starts[0]
    try:
        if (
            grant.get("schema") != OUTAGE_RECOVERY_TYPE
            or type(grant.get("maximum_uses")) is not int
            or grant.get("maximum_uses") != 1
            or not projection.is_current_telemetry(grant)
        ):
            return False
        route = _route(grant.get("replacement_route"))
        if terminal.get("fallback_used") or terminal.get("runtime_fallback_from"):
            return False
        if _actual_model(terminal) != _model(route["model"]):
            return False
        for row in (terminal, start):
            if any(
                row.get(k) != grant.get(k)
                for k in (
                    "task_id",
                    "run_id",
                    "work_unit_id",
                    "root_work_unit_id",
                    "source_identity",
                    "task_contract_hash",
                    "evidence_manifest",
                )
            ):
                return False
            if (
                row.get("attempt_index") != grant.get("next_attempt_index")
                or row.get("outage_authorization_sha256") != digest
                or type(row.get("unit_attempt_number")) is not int
                or row["unit_attempt_number"] != grant.get("next_unit_attempt_number")
            ):
                return False
            if row.get("review_generation_id") != grant.get("generation_id") or row.get(
                "worktree"
            ) != grant.get("worktree"):
                return False
            contract = row.get("task_contract")
            if not isinstance(contract, dict) or task_contract_hash(
                contract
            ) != grant.get("task_contract_hash"):
                return False
            if (
                row.get("engine") != route["engine"]
                or row.get("model") != route["model"]
                or row.get("effort") != route["effort"]
            ):
                return False
        return _obligation(reservations[0]) == _obligation(grant)
    except (DispatchError, TypeError, ValueError):
        return False
