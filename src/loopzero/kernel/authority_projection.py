#!/usr/bin/env python3
"""Authentication and serial-authority projections for governed dispatch records.

Move-only extraction: this sibling must not import ``agent_dispatch``.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import cast

sys.path.insert(0, str(Path(__file__).resolve().parent))

import dispatch_ledger as authority_ledger
from dispatch_authority import (
    COORDINATOR_AUTHORITY_SCHEME,
    LEGACY_COORDINATOR_AUTHORITY_SCHEME,
    TerminalAuthorityError,
    verify_legacy_coordinator_authority,
    verify_terminal_authority,
)
from dispatch_common import DispatchError, resolved_record_worktree, trusted_git_command
from dispatch_routing import (
    ATTEMPT_ABORT_TYPES,
    ATTEMPT_TERMINAL_TYPES,
    COMPATIBLE_DISPATCH_POLICY_VERSIONS,
    COORDINATOR_LEDGER_PREFIX_SCHEME,
    COORDINATOR_LEDGER_PREFIX_V3_SCHEME,
    LEGACY_COORDINATOR_LEDGER_PREFIX_SCHEME,
    LEGACY_COORDINATOR_PREFIX_POLICY_VERSIONS,
    LEGACY_COORDINATOR_PREFIX_RUNTIME_CONTRACT_VERSION,
    LEGACY_COORDINATOR_PREFIX_SCHEMA_VERSION,
    PACKAGING_TIMEOUT_FAILURE_CLASS,
    RUNTIME_CONTRACT_VERSION,
    TELEMETRY_SCHEMA_VERSION,
)
from finding_ledger import canonical_record_digest
from guardian_sandbox import environment as sandbox_environment


def _terminal_authority_attempt_key(
    record: Mapping[str, object],
) -> tuple[str, int] | None:
    task_id = record.get("task_id")
    attempt_index = record.get("attempt_index")
    if (
        not isinstance(task_id, str)
        or not task_id
        or isinstance(attempt_index, bool)
        or not isinstance(attempt_index, int)
        or attempt_index < 0
    ):
        return None
    return task_id, attempt_index


def _terminal_authority_work_unit(record: Mapping[str, object]) -> str | None:
    """Resolve the canonical unit identity used by terminal consumers."""
    for field in ("work_unit_id", "task_id"):
        value = record.get(field)
        if isinstance(value, str) and value:
            return value
    return None


def _terminal_authority_identifiers(
    record: Mapping[str, object],
) -> frozenset[str]:
    """Return every typed task/unit label that poisons legacy compatibility."""
    return frozenset(
        value
        for field in ("task_id", "work_unit_id")
        if isinstance((value := record.get(field)), str) and value
    )


def _authenticated_open_dispatch_attempts(
    records: Sequence[dict[str, object]],
    *,
    worktree: Path,
    stop_before: Mapping[str, object] | None = None,
) -> dict[tuple[str, int], dict[str, object]]:
    """Project open attempts without trusting unsigned registered settlements."""
    target = worktree.resolve()
    authority_history = _authority_record_list(records)
    governed_records = current_telemetry(authority_history)
    authenticated_coordinator_ids = _authenticated_coordinator_record_ids(
        authority_history
    )
    starts: dict[tuple[str, int], dict[str, object]] = {}
    open_attempts: dict[tuple[str, int], dict[str, object]] = {}
    for record in governed_records:
        if record is stop_before:
            break
        raw_worktree = record.get("worktree")
        if resolved_record_worktree(raw_worktree) != target:
            continue
        key = _terminal_authority_attempt_key(record)
        record_type = record.get("type")
        if record_type == "attempt-start":
            if key is not None and key not in starts:
                starts[key] = record
                open_attempts[key] = record
            continue
        if record_type not in {"attempt-terminal", "attempt-abort"} or key is None:
            continue
        start = starts.get(key)
        if start is None:
            continue
        registration = start.get("terminal_authority")
        if isinstance(registration, dict):
            try:
                verify_terminal_authority(
                    record,
                    registration=registration,
                    expected_kind="dispatcher",
                )
            except TerminalAuthorityError:
                continue
        elif isinstance(record.get("terminal_authority_proof"), dict):
            if (
                id(record) not in authenticated_coordinator_ids
                or record.get("run_id") != start.get("run_id")
                or _terminal_authority_work_unit(record)
                != _terminal_authority_work_unit(start)
            ):
                continue
        open_attempts.pop(key, None)
    return open_attempts


def _authenticated_open_before_record_projections(
    records: Sequence[dict[str, object]],
    *,
    _authenticated_start_ids: frozenset[int] | None = None,
) -> tuple[frozenset[int], frozenset[int]]:
    """Return repository-wide and worktree-local open-worker exclusions."""
    authority_history = _authority_record_list(records)
    governed_records = current_telemetry(authority_history)
    authenticated_start_ids = _authenticated_start_ids
    if authenticated_start_ids is None:
        authenticated_start_ids = _authenticated_registration_start_ids(
            authority_history
        )
    authenticated_coordinator_ids = _authenticated_coordinator_record_ids(
        authority_history
    )
    starts: dict[Path, dict[tuple[str, int], dict[str, object]]] = {}
    open_attempts: dict[Path, dict[tuple[str, int], dict[str, object]]] = {}
    authenticated_open: set[tuple[Path, tuple[str, int]]] = set()
    repository_open_before: set[int] = set()
    worktree_open_before: set[int] = set()
    for record in governed_records:
        raw_worktree = record.get("worktree")
        globally_quarantined = (
            bool(authenticated_open) and id(record) not in authenticated_start_ids
        )
        worktree = resolved_record_worktree(raw_worktree)
        if worktree is None:
            if globally_quarantined:
                repository_open_before.add(id(record))
            continue
        worktree_starts = starts.setdefault(worktree, {})
        worktree_open = open_attempts.setdefault(worktree, {})
        # Legacy history keeps its worktree-scoped concurrency contract. New
        # coordinator-authenticated starts establish a repository-wide worker
        # window, while another authenticated start remains distinguishable
        # from a worker-forged registration with a relabelled worktree.
        if worktree_open:
            worktree_open_before.add(id(record))
            repository_open_before.add(id(record))
        elif globally_quarantined:
            repository_open_before.add(id(record))
        key = _terminal_authority_attempt_key(record)
        record_type = record.get("type")
        if record_type == "attempt-start":
            if key is not None and key not in worktree_starts:
                worktree_starts[key] = record
                worktree_open[key] = record
                if id(record) in authenticated_start_ids:
                    authenticated_open.add((worktree, key))
            continue
        if record_type not in {"attempt-terminal", "attempt-abort"} or key is None:
            continue
        start = worktree_starts.get(key)
        if start is None:
            continue
        registration = start.get("terminal_authority")
        if isinstance(registration, dict):
            try:
                verify_terminal_authority(
                    record,
                    registration=registration,
                    expected_kind="dispatcher",
                )
            except TerminalAuthorityError:
                continue
        elif isinstance(record.get("terminal_authority_proof"), dict):
            if (
                id(record) not in authenticated_coordinator_ids
                or record.get("run_id") != start.get("run_id")
                or _terminal_authority_work_unit(record)
                != _terminal_authority_work_unit(start)
            ):
                continue
        worktree_open.pop(key, None)
        authenticated_open.discard((worktree, key))
    return frozenset(repository_open_before), frozenset(worktree_open_before)


def _authenticated_open_before_record_ids(
    records: Sequence[dict[str, object]],
    *,
    _authenticated_start_ids: frozenset[int] | None = None,
) -> frozenset[int]:
    """Mark authority appended during any open worker."""
    repository_open_before, _ = _authenticated_open_before_record_projections(
        records,
        _authenticated_start_ids=_authenticated_start_ids,
    )
    return repository_open_before


def _precutover_registration_start_ids(
    records: Sequence[dict[str, object]],
) -> frozenset[int]:
    """Authenticate host registrations without requiring an active cutover."""
    accepted: set[int] = set()
    legacy_compatibility_open = True
    for record in records:
        if _is_coordinator_cutover_attempt(record):
            legacy_compatibility_open = False
        if (
            record.get("type") != "attempt-start"
            or record.get("registration_authority_version") != 1
            or not isinstance(record.get("terminal_authority"), dict)
        ):
            continue
        proof = record.get("terminal_authority_proof")
        if not isinstance(proof, dict) or proof.get("authority_kind") != "coordinator":
            continue
        scheme = proof.get("scheme")
        if scheme == LEGACY_COORDINATOR_AUTHORITY_SCHEME:
            if not legacy_compatibility_open:
                continue
        elif scheme == COORDINATOR_AUTHORITY_SCHEME:
            pass
        else:
            continue
        try:
            if scheme == LEGACY_COORDINATOR_AUTHORITY_SCHEME:
                verify_legacy_coordinator_authority(record)
            else:
                verify_terminal_authority(
                    record, registration=None, expected_kind="coordinator"
                )
        except TerminalAuthorityError:
            continue
        accepted.add(id(record))
    return frozenset(accepted)


def _authenticated_registration_start_ids(
    records: Sequence[dict[str, object]],
) -> frozenset[int]:
    """Return versioned starts authenticated by the host coordinator root."""
    authority_history = _authority_record_list(records)
    coordinator_ids = _authenticated_coordinator_record_ids(authority_history)
    return frozenset(
        id(record)
        for record in current_telemetry(authority_history)
        if record.get("type") == "attempt-start"
        and record.get("registration_authority_version") == 1
        and id(record) in coordinator_ids
        and isinstance((proof := record.get("terminal_authority_proof")), dict)
        and proof.get("scheme")
        in {
            COORDINATOR_AUTHORITY_SCHEME,
            LEGACY_COORDINATOR_AUTHORITY_SCHEME,
        }
    )


def _preceding_registered_authority_starts(
    records: Sequence[dict[str, object]],
    *,
    terminal: Mapping[str, object],
    worktree: Path,
) -> list[dict[str, object]]:
    """Return registrations whose typed identity intersects one later terminal."""
    starts: list[dict[str, object]] = []
    for record in records:
        if record is terminal:
            break
        if (
            record.get("type") == "attempt-start"
            and bool(
                _terminal_authority_identifiers(record)
                & _terminal_authority_identifiers(terminal)
            )
            and record.get("run_id") == terminal.get("run_id")
            and isinstance(record.get("terminal_authority"), dict)
            and resolved_record_worktree(record.get("worktree")) == worktree
        ):
            starts.append(record)
    return starts


def _registered_authority_before_record_ids(
    records: Sequence[dict[str, object]],
) -> frozenset[int]:
    """Mark records whose typed identity intersects an earlier registration."""
    registered: set[str] = set()
    matched: set[int] = set()
    for record in current_telemetry(records):
        identifiers = _terminal_authority_identifiers(record)
        if identifiers & registered:
            matched.add(id(record))
        if record.get("type") == "attempt-start" and isinstance(
            record.get("terminal_authority"), dict
        ):
            registered.update(identifiers)
    return frozenset(matched)


def _precutover_registered_settlement_contexts(
    records: Sequence[dict[str, object]],
) -> frozenset[tuple[tuple[str, int], str, str, Path]]:
    """Verify host-registered settlements before the first cutover exists.

    Normal terminal projection deliberately withholds host-signed registrations
    until a valid cutover activates their coordinator key. Cutover admission
    cannot consume that projection without creating a circular dependency, so
    it verifies the pending host registration when present and always verifies
    the exact dispatcher settlement key while preserving open-writer exclusion.
    """
    open_before_ids = _authenticated_open_before_record_ids(
        records,
        _authenticated_start_ids=_precutover_registration_start_ids(records),
    )
    starts: dict[tuple[tuple[str, int], str, str, Path], list[dict[str, object]]] = {}
    settled: set[tuple[tuple[str, int], str, str, Path]] = set()
    for record in records:
        key = _terminal_authority_attempt_key(record)
        run_id = record.get("run_id")
        work_unit_id = _terminal_authority_work_unit(record)
        raw_worktree = record.get("worktree")
        context = (
            (key, run_id, work_unit_id, resolved_worktree)
            if key is not None
            and isinstance(run_id, str)
            and isinstance(work_unit_id, str)
            and (resolved_worktree := resolved_record_worktree(raw_worktree))
            is not None
            else None
        )
        if record.get("type") == "attempt-start":
            proof = record.get("terminal_authority_proof")
            proof_scheme = proof.get("scheme") if isinstance(proof, dict) else None
            if (
                context is None
                or not isinstance(record.get("terminal_authority"), dict)
                or id(record) in open_before_ids
            ):
                continue
            if proof is not None:
                if (
                    record.get("registration_authority_version") != 1
                    or not isinstance(proof, dict)
                    or proof.get("authority_kind") != "coordinator"
                    or proof_scheme
                    not in {
                        COORDINATOR_AUTHORITY_SCHEME,
                        LEGACY_COORDINATOR_AUTHORITY_SCHEME,
                    }
                ):
                    continue
                try:
                    if proof_scheme == LEGACY_COORDINATOR_AUTHORITY_SCHEME:
                        verify_legacy_coordinator_authority(record)
                    else:
                        verify_terminal_authority(
                            record, registration=None, expected_kind="coordinator"
                        )
                except TerminalAuthorityError:
                    continue
            starts.setdefault(context, []).append(record)
            continue
        if (
            context is None
            or record.get("type") not in ATTEMPT_TERMINAL_TYPES | ATTEMPT_ABORT_TYPES
        ):
            continue
        candidates = starts.get(context, [])
        if len(candidates) != 1:
            continue
        try:
            verify_terminal_authority(
                record,
                registration=cast(
                    dict[str, object], candidates[0]["terminal_authority"]
                ),
                expected_kind="dispatcher",
            )
        except TerminalAuthorityError:
            continue
        settled.add(context)
    return frozenset(settled)


@dataclass(frozen=True)
class AuthorityLedgerSnapshot:
    """Authenticated physical history and its checkpoint-retained projection."""

    records: AuthorityRecordView
    physical_records: tuple[dict[str, object], ...]
    checkpoint: authority_ledger.AuthorityLedgerCheckpointV1 | None
    accumulator_head: authority_ledger.LedgerAccumulatorV3 | None
    host_state: authority_ledger.AuthorityLedgerHostStateV1 | None


class AuthorityRecordView(list[dict[str, object]]):
    """List-compatible authority view that preserves verified v3 provenance."""

    def __init__(
        self,
        records: Sequence[dict[str, object]] = (),
        *,
        trusted_checkpoint_id: int | None = None,
        trusted_retained_ids: Collection[int] = (),
        checkpoint_prefix: Mapping[str, object] | None = None,
        accumulator_head: authority_ledger.LedgerAccumulatorV3 | None = None,
    ) -> None:
        super().__init__(records)
        self.trusted_checkpoint_id = trusted_checkpoint_id
        self.trusted_retained_ids = frozenset(trusted_retained_ids)
        self.checkpoint_prefix = (
            dict(checkpoint_prefix) if checkpoint_prefix is not None else None
        )
        self.accumulator_head = accumulator_head

    def filtered(self, records: Sequence[dict[str, object]]) -> AuthorityRecordView:
        retained = {id(record) for record in records}
        return AuthorityRecordView(
            records,
            trusted_checkpoint_id=(
                self.trusted_checkpoint_id
                if self.trusted_checkpoint_id in retained
                else None
            ),
            trusted_retained_ids=self.trusted_retained_ids & retained,
            checkpoint_prefix=self.checkpoint_prefix,
            accumulator_head=self.accumulator_head,
        )


def _authority_record_list(
    records: Sequence[dict[str, object]],
) -> list[dict[str, object]]:
    copied = list(records)
    if isinstance(records, AuthorityRecordView):
        return records.filtered(copied)
    return copied


def _authenticated_coordinator_record_ids(
    records: Sequence[dict[str, object]],
) -> frozenset[int]:
    """Authenticate coordinator rows with an ordered host-authority cutover."""
    accepted: set[int] = set()
    pending_host: set[int] = set()
    cutover_seen = False
    host_authority_active = False
    trusted_view = records if isinstance(records, AuthorityRecordView) else None
    for index, record in enumerate(records):
        if trusted_view is not None and id(record) in trusted_view.trusted_retained_ids:
            accepted.add(id(record))
            continue
        proof = record.get("terminal_authority_proof")
        if not isinstance(proof, dict) or proof.get("authority_kind") != "coordinator":
            continue
        proof_scheme = proof.get("scheme")
        current_host_proof = proof_scheme == COORDINATOR_AUTHORITY_SCHEME
        legacy_host_proof = proof_scheme == LEGACY_COORDINATOR_AUTHORITY_SCHEME
        host_proof = current_host_proof or legacy_host_proof
        cutover_attempt = _is_coordinator_cutover_attempt(record)
        if (not host_proof or legacy_host_proof) and cutover_seen:
            continue
        try:
            if legacy_host_proof:
                verify_legacy_coordinator_authority(record)
            else:
                verify_terminal_authority(
                    record, registration=None, expected_kind="coordinator"
                )
        except TerminalAuthorityError:
            if cutover_attempt:
                cutover_seen = True
                pending_host.clear()
            continue
        trusted_v3_checkpoint = (
            trusted_view is not None
            and trusted_view.trusted_checkpoint_id == id(record)
            and record.get("ledger_prefix") == trusted_view.checkpoint_prefix
        )
        if cutover_attempt and not (
            trusted_v3_checkpoint
            or _coordinator_ledger_prefix_matches(
                record.get("ledger_prefix"), records[:index]
            )
        ):
            cutover_seen = True
            pending_host.clear()
            continue
        if cutover_attempt:
            cutover_seen = True
            host_authority_active = True
            accepted.add(id(record))
            accepted.update(pending_host)
            pending_host.clear()
        elif host_proof and host_authority_active:
            accepted.add(id(record))
        elif host_proof and not host_authority_active:
            # A later valid, quiescent cutover may adopt host-signed records
            # written before or after an invalid cutover attempt. They do not
            # activate host authority or reopen legacy compatibility alone.
            pending_host.add(id(record))
        elif not host_proof:
            accepted.add(id(record))
    return frozenset(accepted)


def authenticated_coordinator_record_ids(
    records: Sequence[dict[str, object]],
) -> frozenset[int]:
    """Expose the ordered host-authentication projection to trusted consumers."""
    return _authenticated_coordinator_record_ids(records)


def coordinator_ledger_prefix(
    records: Sequence[dict[str, object]],
    *,
    scheme: str = COORDINATOR_LEDGER_PREFIX_SCHEME,
) -> dict[str, object]:
    """Bind a cutover to a stable, explicitly versioned ledger projection."""
    if scheme == LEGACY_COORDINATOR_LEDGER_PREFIX_SCHEME:
        selected = [
            record
            for record in records
            if _is_coordinator_cutover_attempt(record)
            or (
                record.get("schema_version") == LEGACY_COORDINATOR_PREFIX_SCHEMA_VERSION
                and record.get("policy_version")
                in LEGACY_COORDINATOR_PREFIX_POLICY_VERSIONS
                and record.get("runtime_contract_version")
                in {None, LEGACY_COORDINATOR_PREFIX_RUNTIME_CONTRACT_VERSION}
            )
        ]
    elif scheme == COORDINATOR_LEDGER_PREFIX_SCHEME:
        selected = list(records)
    elif scheme == COORDINATOR_LEDGER_PREFIX_V3_SCHEME:
        if (
            not isinstance(records, AuthorityRecordView)
            or records.accumulator_head is None
        ):
            raise DispatchError("v3 coordinator prefix requires a verified snapshot")
        return records.accumulator_head.to_dict()
    else:
        raise DispatchError(f"unsupported coordinator ledger prefix scheme: {scheme}")
    digests = [canonical_record_digest(record) for record in selected]
    return {
        "scheme": scheme,
        "record_count": len(digests),
        "records_sha256": hashlib.sha256(
            json.dumps(digests, separators=(",", ":")).encode("ascii")
        ).hexdigest(),
    }


def _coordinator_ledger_prefix_matches(
    expected: object,
    records: Sequence[dict[str, object]],
) -> bool:
    """Verify a sealed prefix without reinterpreting its version later."""
    if not isinstance(expected, dict):
        return False
    scheme = expected.get("scheme")
    if scheme not in {
        LEGACY_COORDINATOR_LEDGER_PREFIX_SCHEME,
        COORDINATOR_LEDGER_PREFIX_SCHEME,
        COORDINATOR_LEDGER_PREFIX_V3_SCHEME,
    }:
        return False
    if scheme == COORDINATOR_LEDGER_PREFIX_V3_SCHEME:
        return (
            isinstance(records, AuthorityRecordView)
            and records.accumulator_head is not None
            and expected == records.accumulator_head.to_dict()
        )
    return expected == coordinator_ledger_prefix(records, scheme=cast(str, scheme))


def _is_coordinator_cutover_attempt(record: Mapping[str, object]) -> bool:
    """Recognize the durable boundary shape before signature validation."""
    proof = record.get("terminal_authority_proof")
    return (
        record.get("type") == "coordinator-authority-cutover"
        and record.get("status") == "active"
        and isinstance(proof, Mapping)
        and proof.get("authority_kind") == "coordinator"
        and proof.get("scheme") == COORDINATOR_AUTHORITY_SCHEME
    )


def _legacy_compatibility_record_ids(
    records: Sequence[dict[str, object]],
) -> frozenset[int]:
    """Bound proofless compatibility to the prefix before cutover is attempted."""
    accepted: set[int] = (
        set(records.trusted_retained_ids)
        if isinstance(records, AuthorityRecordView)
        else set()
    )
    for record in records:
        if _is_coordinator_cutover_attempt(record):
            break
        accepted.add(id(record))
    return frozenset(accepted)


def _authenticated_attempt_terminal_ids(
    records: Sequence[dict[str, object]],
    *,
    _open_before_record_ids: frozenset[int] | None = None,
    _registered_before_record_ids: frozenset[int] | None = None,
) -> frozenset[int]:
    """Authenticate registered deposits while preserving proofless history."""
    authority_history = _authority_record_list(records)
    governed_records = current_telemetry(authority_history)
    open_before_record_ids = _open_before_record_ids
    if open_before_record_ids is None:
        open_before_record_ids = _authenticated_open_before_record_ids(
            authority_history
        )
    registered_before_ids = _registered_before_record_ids
    if registered_before_ids is None:
        registered_before_ids = _registered_authority_before_record_ids(
            authority_history
        )
    authenticated_coordinator_ids = _authenticated_coordinator_record_ids(
        authority_history
    )
    authenticated_registration_ids = _authenticated_registration_start_ids(
        authority_history
    )
    legacy_compatibility_ids = _legacy_compatibility_record_ids(authority_history)
    starts: dict[tuple[tuple[str, int], str, str, Path], list[dict[str, object]]] = {}
    accepted: set[int] = set()
    for record in governed_records:
        key = _terminal_authority_attempt_key(record)
        if record.get("type") == "attempt-start":
            raw_worktree = record.get("worktree")
            run_id = record.get("run_id")
            work_unit_id = _terminal_authority_work_unit(record)
            resolved_worktree = resolved_record_worktree(raw_worktree)
            if (
                key is not None
                and resolved_worktree is not None
                and isinstance(run_id, str)
                and isinstance(work_unit_id, str)
                and isinstance(record.get("terminal_authority"), dict)
                and id(record) not in open_before_record_ids
                and (
                    id(record) in authenticated_registration_ids
                    or (
                        id(record) in legacy_compatibility_ids
                        and not isinstance(record.get("terminal_authority_proof"), dict)
                    )
                )
            ):
                start_key = (
                    key,
                    run_id,
                    work_unit_id,
                    resolved_worktree,
                )
                starts.setdefault(start_key, []).append(record)
            continue
        if record.get("type") not in {"attempt-terminal", "attempt-abort"}:
            continue
        if id(record) not in registered_before_ids:
            if id(record) in authenticated_coordinator_ids or (
                id(record) in legacy_compatibility_ids
                and not isinstance(record.get("terminal_authority_proof"), dict)
            ):
                accepted.add(id(record))
            continue
        raw_worktree = record.get("worktree")
        run_id = record.get("run_id")
        work_unit_id = _terminal_authority_work_unit(record)
        resolved_worktree = resolved_record_worktree(raw_worktree)
        if (
            key is None
            or resolved_worktree is None
            or not isinstance(run_id, str)
            or not isinstance(work_unit_id, str)
        ):
            continue
        worktree = resolved_worktree
        candidates = starts.get((key, run_id, work_unit_id, worktree), [])
        if len(candidates) != 1:
            continue
        try:
            verify_terminal_authority(
                record,
                registration=cast(
                    dict[str, object], candidates[0]["terminal_authority"]
                ),
                expected_kind="dispatcher",
            )
        except TerminalAuthorityError:
            continue
        accepted.add(id(record))
    return frozenset(accepted)


_RECOVERY_DEPOSIT_IDENTITY_FIELDS = (
    "task_id",
    "work_unit_id",
    "root_work_unit_id",
    "attempt_index",
    "unit_attempt_number",
    "run_id",
    "read_only",
    "work_kind",
    "review_intent",
    "review_lens",
    "category",
    "source_identity",
    "output_identity",
    "baseline",
    "lineage",
    "snapshot_sha",
    "snapshot_tree_sha",
    "delta_from_snapshot_sha",
    "delta_from_tree_sha",
    "patch_identity",
    "task_contract_hash",
)


def _recovery_matches_deposit(
    recovery: Mapping[str, object], deposit: Mapping[str, object]
) -> bool:
    """Bind a historical recovery to immutable prior-deposit identity."""
    raw_recovery_worktree = recovery.get("worktree")
    raw_deposit_worktree = deposit.get("worktree")
    recovery_worktree = resolved_record_worktree(raw_recovery_worktree)
    deposit_worktree = resolved_record_worktree(raw_deposit_worktree)
    if recovery_worktree is None or deposit_worktree is None:
        return False
    if recovery_worktree != deposit_worktree:
        return False
    if not all(
        recovery.get(field) == deposit.get(field)
        for field in _RECOVERY_DEPOSIT_IDENTITY_FIELDS
    ):
        return False
    formal_review = (
        deposit.get("read_only") is True and deposit.get("work_kind") == "review"
    )
    failure_pair = (deposit.get("status"), deposit.get("failure_class"))
    recoverable = (
        formal_review and deposit.get("status") in {"blocked", "completed"}
    ) or failure_pair in {
        ("infrastructure-failure", "acceptance-environment"),
        ("toolchain-failure", "toolchain"),
        ("packaging-failure", PACKAGING_TIMEOUT_FAILURE_CLASS),
    }
    if not recoverable or recovery.get("recovered_terminal_status") != deposit.get(
        "status"
    ):
        return False
    artifact_field = (
        "recovered_result_artifact" if formal_review else "deposit_result_artifact"
    )
    digest_field = (
        "recovered_result_sha256" if formal_review else "deposit_result_sha256"
    )
    return recovery.get(artifact_field) == deposit.get(
        "result_artifact"
    ) and recovery.get(digest_field) == deposit.get("result_sha256")


_SUPERSESSION_PRESERVED_FIELDS = (
    "run_id",
    "work_unit_id",
    "output_identity",
    "source_identity",
    "snapshot_sha",
    "snapshot_tree_sha",
    "patch_identity",
    "result_artifact",
    "result_sha256",
    "task_contract_hash",
)


def _supersession_matches_deposit(
    supersession: Mapping[str, object], deposit: Mapping[str, object]
) -> bool:
    """Bind stale-source supersession to one exact accepted terminal."""
    raw_supersession_worktree = supersession.get("worktree")
    raw_deposit_worktree = deposit.get("worktree")
    supersession_worktree = resolved_record_worktree(raw_supersession_worktree)
    deposit_worktree = resolved_record_worktree(raw_deposit_worktree)
    reason = supersession.get("supersession_reason")
    supported_reason = reason == "stale-source" or (
        reason == "failed-review"
        and supersession.get("failure_class") == "failed-review"
        and deposit.get("work_kind") == "review"
        and deposit.get("read_only") is True
        and supersession.get("superseding_source_identity")
        == deposit.get("source_identity")
    )
    return (
        supersession_worktree is not None
        and supersession_worktree == deposit_worktree
        and _terminal_authority_attempt_key(supersession)
        == _terminal_authority_attempt_key(deposit)
        and all(
            deposit.get(field) == supersession.get(field)
            for field in _SUPERSESSION_PRESERVED_FIELDS
        )
        and supersession.get("superseded_task_id") == deposit.get("task_id")
        and supersession.get("status") == "superseded"
        and supported_reason
    )


_KEPT_PLAN_STABLE_FIELDS = (
    "task_id",
    "work_unit_id",
    "root_work_unit_id",
    "run_id",
    "worktree",
    "allowed_paths",
    "acceptance_commands",
    "read_only",
    "terminal_authority_required",
)


def _canonical_kept_plan(
    plans: Sequence[dict[str, object]],
) -> dict[str, object]:
    """Select the first immutable plan; reject mutable duplicate plans."""
    first = plans[0]
    contract_hash = first.get("task_contract_hash")
    if not _is_valid_sha256(contract_hash) or any(
        plan.get("task_contract_hash") != contract_hash
        or any(
            plan.get(field) != first.get(field) for field in _KEPT_PLAN_STABLE_FIELDS
        )
        for plan in plans[1:]
    ):
        raise DispatchError("conflicting governed kept plans")
    return first


def validate_serial_terminal_authority(
    records: Sequence[dict[str, object]],
    *,
    terminal: Mapping[str, object],
    worktree: Path,
    _open_before_record_ids: frozenset[int] | None = None,
) -> None:
    """Prove that a current serial terminal came from its dispatcher parent."""
    authority_history = _authority_record_list(records)
    governed_records = current_telemetry(authority_history)
    if not any(record is terminal for record in governed_records):
        raise DispatchError("serial terminal is not governed by the current policy")
    record_type = terminal.get("type")
    preceding_records: list[dict[str, object]] = []
    for record in governed_records:
        if record is terminal:
            break
        preceding_records.append(record)
    registered_before = id(terminal) in _registered_authority_before_record_ids(
        authority_history
    )
    authenticated_coordinator_ids = _authenticated_coordinator_record_ids(
        authority_history
    )
    legacy_compatibility_ids = _legacy_compatibility_record_ids(authority_history)
    if (
        not registered_before
        and not isinstance(terminal.get("terminal_authority_proof"), dict)
        and id(terminal) not in legacy_compatibility_ids
    ):
        raise DispatchError("post-cutover serial terminal has no authority proof")

    def authenticated_attempt_was_open_before(
        record: Mapping[str, object],
    ) -> bool:
        if _open_before_record_ids is not None:
            return id(record) in _open_before_record_ids
        return bool(
            _authenticated_open_dispatch_attempts(
                authority_history, worktree=worktree, stop_before=record
            )
        )

    if record_type == "inline":
        plans = [
            record
            for record in preceding_records
            if record.get("type") == "route"
            and record.get("kept") is True
            and record.get("task_id") == terminal.get("task_id")
            and bool(
                _terminal_authority_identifiers(record)
                & _terminal_authority_identifiers(terminal)
            )
            and record.get("run_id") == terminal.get("run_id")
            and resolved_record_worktree(record.get("worktree")) == worktree
        ]
        if registered_before:
            raise DispatchError(
                "authority-registered dispatch terminal cannot be relabelled as inline"
            )
        if authenticated_attempt_was_open_before(terminal):
            raise DispatchError(
                "inline terminal was recorded while an authenticated attempt was open"
            )
        if not plans:
            if not isinstance(terminal.get("terminal_authority_proof"), dict):
                return
            raise DispatchError("inline terminal has no coordinator plan")
        plan = _canonical_kept_plan(plans) if len(plans) > 1 else plans[0]
        authority_required = plan.get("terminal_authority_required") is True
        if not authority_required and not isinstance(
            terminal.get("terminal_authority_proof"), dict
        ):
            return
        if id(terminal) not in authenticated_coordinator_ids:
            raise DispatchError("inline terminal authority proof is invalid")
        return
    if not registered_before and not isinstance(
        terminal.get("terminal_authority_proof"), dict
    ):
        return
    if record_type not in {
        "attempt-terminal",
        "attempt-recovery",
        "attempt-supersession",
    }:
        raise DispatchError("serial terminal type has no authority contract")
    key = _terminal_authority_attempt_key(terminal)
    if key is None:
        raise DispatchError("serial terminal authority identity is invalid")
    if record_type == "attempt-recovery":
        deposits: list[dict[str, object]] = []
        for record in preceding_records:
            if (
                record.get("type") == "attempt-terminal"
                and _terminal_authority_attempt_key(record) == key
            ):
                deposits.append(record)
        if len(deposits) != 1:
            raise DispatchError("serial recovery has no unique authenticated deposit")
        deposit = deposits[0]
        validate_serial_terminal_authority(
            authority_history,
            terminal=deposit,
            worktree=worktree,
            _open_before_record_ids=_open_before_record_ids,
        )
        if authenticated_attempt_was_open_before(terminal):
            raise DispatchError(
                "serial recovery was recorded while an authenticated attempt was open"
            )
        if isinstance(deposit.get("terminal_authority_proof"), dict):
            raise DispatchError(
                "serial recovery cannot authorize success after an authenticated "
                "dispatcher settlement"
            )
        if not _recovery_matches_deposit(terminal, deposit):
            raise DispatchError("serial recovery does not match its deposit")
        if id(terminal) not in authenticated_coordinator_ids:
            raise DispatchError("serial recovery authority proof is invalid")
        return
    if record_type == "attempt-supersession":
        deposits = [
            record
            for record in preceding_records
            if record.get("type") == "attempt-terminal"
            and _terminal_authority_attempt_key(record) == key
        ]
        if len(deposits) != 1:
            raise DispatchError(
                "serial supersession has no unique authenticated terminal"
            )
        deposit = deposits[0]
        validate_serial_terminal_authority(
            authority_history,
            terminal=deposit,
            worktree=worktree,
            _open_before_record_ids=_open_before_record_ids,
        )
        if not _supersession_matches_deposit(terminal, deposit):
            raise DispatchError("serial supersession does not match its terminal")
        if authenticated_attempt_was_open_before(terminal):
            raise DispatchError(
                "serial supersession was recorded while an authenticated attempt was open"
            )
        if id(terminal) not in authenticated_coordinator_ids:
            raise DispatchError("serial supersession authority proof is invalid")
        return
    preceding: list[dict[str, object]] = []
    for record in preceding_records:
        if (
            record.get("type") == "attempt-start"
            and _terminal_authority_attempt_key(record) == key
        ):
            preceding.append(record)
    if len(preceding) != 1:
        raise DispatchError("serial terminal has no unique prelaunch authority")
    start = preceding[0]
    if authenticated_attempt_was_open_before(start):
        raise DispatchError(
            "serial terminal prelaunch authority was registered while an attempt was open"
        )
    if (
        start.get("run_id") != terminal.get("run_id")
        or _terminal_authority_work_unit(start)
        != _terminal_authority_work_unit(terminal)
        or resolved_record_worktree(start.get("worktree")) != worktree
    ):
        raise DispatchError("serial terminal prelaunch authority does not match")
    registration = start.get("terminal_authority")
    if not isinstance(registration, dict):
        raise DispatchError("serial terminal authority proof is missing")
    try:
        verify_terminal_authority(
            terminal,
            registration=registration,
            expected_kind="dispatcher",
        )
    except TerminalAuthorityError as exc:
        raise DispatchError(
            f"serial terminal authority proof is invalid: {exc}"
        ) from exc


def authenticated_supersessions(
    records: Sequence[dict[str, object]],
    *,
    _open_before_record_ids: frozenset[int] | None = None,
    _supersession_open_before_record_ids: frozenset[int] | None = None,
    _registered_before_record_ids: frozenset[int] | None = None,
    _authenticated_terminal_ids: frozenset[int] | None = None,
    _authenticated_coordinator_ids: frozenset[int] | None = None,
    _legacy_compatibility_ids: frozenset[int] | None = None,
) -> list[dict[str, object]]:
    """Return supersessions whose terminal authority matches their history."""
    authority_history = _authority_record_list(records)
    governed_records = current_telemetry(authority_history)
    accepted: list[dict[str, object]] = []
    open_before_record_ids = _open_before_record_ids
    supersession_open_before_record_ids = _supersession_open_before_record_ids
    if open_before_record_ids is None or supersession_open_before_record_ids is None:
        repository_open_before, worktree_open_before = (
            _authenticated_open_before_record_projections(authority_history)
        )
        if open_before_record_ids is None:
            open_before_record_ids = repository_open_before
        if supersession_open_before_record_ids is None:
            supersession_open_before_record_ids = worktree_open_before
    registered_before_ids = _registered_before_record_ids
    if registered_before_ids is None:
        registered_before_ids = _registered_authority_before_record_ids(
            authority_history
        )
    authenticated_terminal_ids = _authenticated_terminal_ids
    if authenticated_terminal_ids is None:
        authenticated_terminal_ids = _authenticated_attempt_terminal_ids(
            authority_history,
            _open_before_record_ids=open_before_record_ids,
            _registered_before_record_ids=registered_before_ids,
        )
    authenticated_coordinator_ids = _authenticated_coordinator_ids
    if authenticated_coordinator_ids is None:
        authenticated_coordinator_ids = _authenticated_coordinator_record_ids(
            authority_history
        )
    legacy_compatibility_ids = _legacy_compatibility_ids
    if legacy_compatibility_ids is None:
        legacy_compatibility_ids = _legacy_compatibility_record_ids(authority_history)
    deposits_by_key: dict[tuple[str, int], list[dict[str, object]]] = {}
    unkeyed_legacy_deposits: list[dict[str, object]] = []
    for record in governed_records:
        key = _terminal_authority_attempt_key(record)
        if (
            record.get("type") == "attempt-terminal"
            and id(record) in authenticated_terminal_ids
        ):
            if key is not None:
                deposits_by_key.setdefault(key, []).append(record)
            elif id(record) in legacy_compatibility_ids and not isinstance(
                record.get("terminal_authority_proof"), dict
            ):
                unkeyed_legacy_deposits.append(record)
            continue
        if record.get("type") != "attempt-supersession":
            continue
        raw_worktree = record.get("worktree")
        if not isinstance(raw_worktree, str):
            if (
                id(record) in legacy_compatibility_ids
                and id(record) not in registered_before_ids
                and not isinstance(record.get("terminal_authority_proof"), dict)
            ):
                accepted.append(record)
            continue
        deposits = (
            deposits_by_key.get(key, [])
            if key is not None
            else [
                deposit
                for deposit in unkeyed_legacy_deposits
                if id(record) in legacy_compatibility_ids
                and not isinstance(record.get("terminal_authority_proof"), dict)
                and _supersession_matches_deposit(record, deposit)
            ]
        )
        # A coordinator-signed supersession settles one completed terminal.
        # An open worker can race that source only in the same worktree;
        # unrelated worktrees must not permanently quarantine the settlement.
        if (
            len(deposits) != 1
            or id(record) in supersession_open_before_record_ids
            or not _supersession_matches_deposit(record, deposits[0])
        ):
            continue
        if not isinstance(record.get("terminal_authority_proof"), dict):
            if id(record) in legacy_compatibility_ids and not isinstance(
                deposits[0].get("terminal_authority_proof"), dict
            ):
                accepted.append(record)
            continue
        if id(record) not in authenticated_coordinator_ids:
            continue
        accepted.append(record)
    return accepted


def _require_authenticated_supersession(
    history: Sequence[dict[str, object]], candidate: dict[str, object]
) -> None:
    """Reject a prospective supersession before an unauthenticated write."""
    prospective = _authority_record_list(history)
    prospective.append(candidate)
    if not any(
        record is candidate for record in authenticated_supersessions(prospective)
    ):
        raise DispatchError(
            "supersession did not authenticate against current history; "
            "no record appended"
        )


def _apply_open_write_record(
    open_units: dict[str, list[str]], record: Mapping[str, object]
) -> None:
    """Fold one governed record into a worktree's open write-scope map."""
    unit_id = str(record.get("work_unit_id") or record.get("task_id") or "")
    if not unit_id:
        return
    record_type = record.get("type")
    if (
        record_type == "route"
        and record.get("kept") is True
        and record.get("read_only") is not True
    ) or (record_type == "attempt-start" and record.get("read_only") is not True):
        raw_paths = record.get("allowed_paths")
        if isinstance(raw_paths, list) and all(
            isinstance(path, str) for path in raw_paths
        ):
            open_units[unit_id] = [str(path) for path in raw_paths]
        return
    if record_type == "inline":
        # Completion satisfies the unit. A scope violation frees the paths for
        # its explicit replacement; retryable failures keep ownership so a
        # successor cannot invalidate the original unit's baseline.
        if record.get("status") in {"completed", "scope-violation"}:
            open_units.pop(unit_id, None)
        return
    if record_type in {*ATTEMPT_TERMINAL_TYPES, *ATTEMPT_ABORT_TYPES}:
        open_units.pop(unit_id, None)


def open_write_units(
    records: Sequence[dict[str, object]], *, worktree: Path
) -> dict[str, list[str]]:
    """Return write scopes whose governed attempt or inline plan is still open."""
    target = worktree.resolve()
    open_units: dict[str, list[str]] = {}
    for record in records:
        if resolved_record_worktree(record.get("worktree")) != target:
            continue
        _apply_open_write_record(open_units, record)
    return open_units


def is_current_telemetry(record: dict[str, object]) -> bool:
    """Whether a record is governed by the current dispatcher contract.

    Only explicitly compatible policies affect routing guards and verified
    portfolio accounting. All other telemetry remains forensic evidence.
    """
    if _is_coordinator_cutover_attempt(record):
        # The fail-closed authority boundary is durable across observational
        # schema and policy rotations; its own signature selects validity.
        return True
    if record.get("schema_version") != TELEMETRY_SCHEMA_VERSION:
        return False
    if record.get("policy_version") not in COMPATIBLE_DISPATCH_POLICY_VERSIONS:
        return False
    runtime_contract_version = record.get("runtime_contract_version")
    return runtime_contract_version in {None, RUNTIME_CONTRACT_VERSION}


def current_telemetry(records: Sequence[dict[str, object]]) -> list[dict[str, object]]:
    selected = [record for record in records if is_current_telemetry(record)]
    if isinstance(records, AuthorityRecordView):
        return records.filtered(selected)
    return selected


def _is_valid_sha256(value: object) -> bool:
    return isinstance(value, str) and bool(re.fullmatch(r"[0-9a-f]{64}", value))


def worktree_status_paths(worktree: Path) -> set[str]:
    try:
        completed = subprocess.run(
            trusted_git_command(
                worktree,
                "status",
                "--porcelain=v1",
                "-z",
                "-uall",
            ),
            capture_output=True,
            check=False,
            env=sandbox_environment(os.environ),
        )
    except OSError as exc:
        raise DispatchError(f"git status failed in {worktree}") from exc
    if completed.returncode != 0:
        raise DispatchError(f"git status failed in {worktree}")
    raw_output = completed.stdout
    if not isinstance(raw_output, bytes):
        raise DispatchError(f"git status returned malformed output in {worktree}")
    paths: set[str] = set()
    entries = iter(raw_output.split(b"\0"))
    for entry in entries:
        if not entry:
            continue
        if len(entry) < 4 or entry[2:3] != b" ":
            raise DispatchError(f"git status returned malformed output in {worktree}")
        paths.add(os.fsdecode(entry[3:]))
        if entry[:1] in {b"R", b"C"} or entry[1:2] in {b"R", b"C"}:
            try:
                source = next(entries)
            except StopIteration as exc:
                raise DispatchError(
                    f"git status returned malformed rename output in {worktree}"
                ) from exc
            if not source:
                raise DispatchError(
                    f"git status returned malformed rename output in {worktree}"
                )
            paths.add(os.fsdecode(source))
    internal_prefixes = (".audit/",)
    return {
        path
        for path in paths
        if not any(path.startswith(prefix) for prefix in internal_prefixes)
    }
