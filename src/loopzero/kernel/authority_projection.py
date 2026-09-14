#!/usr/bin/env python3
"""Authentication and serial-authority projections for governed dispatch records.

Move-only extraction: this sibling must not import ``agent_dispatch``.
"""

from __future__ import annotations

import copy
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


from . import ledger as authority_ledger
from .seams import archived_supersession_deposits
from .seams import (
    MissingAdapter,
    accepted_review_terminals as seam_accepted_review_terminals,
    authenticated_verdicts as seam_authenticated_verdicts,
)
from .authority import (
    COORDINATOR_AUTHORITY_SCHEME,
    authenticated_gone_owner_abort,
    LEGACY_COORDINATOR_AUTHORITY_SCHEME,
    TerminalAuthorityError,
    TerminalAuthorityOperationalError,
    verify_legacy_coordinator_authority,
    verify_terminal_authority,
)
from .gitscope import DispatchError, resolved_record_worktree, trusted_git_command
from .policy import (
    ATTEMPT_ABORT_TYPES,
    ATTEMPT_TERMINAL_TYPES,
    COMPATIBLE_DISPATCH_POLICY_VERSIONS,
    COORDINATOR_LEDGER_PREFIX_SCHEME,
    COORDINATOR_LEDGER_PREFIX_V3_SCHEME,
    DISPATCH_OUTCOME_TYPES,
    supersession_reason_matches_terminal,
    LEGACY_COORDINATOR_LEDGER_PREFIX_SCHEME,
    LEGACY_COORDINATOR_PREFIX_POLICY_VERSIONS,
    LEGACY_COORDINATOR_PREFIX_RUNTIME_CONTRACT_VERSION,
    LEGACY_COORDINATOR_PREFIX_SCHEMA_VERSION,
    PACKAGING_TIMEOUT_FAILURE_CLASS,
    RUNTIME_CONTRACT_VERSION,
    TELEMETRY_SCHEMA_VERSION,
)
from .canonical import canonical_record_digest
from .sandbox import environment as sandbox_environment

RETENTION_STATE_TYPE = "retained-authority-state"
RETENTION_STATE_VERSION = 2
RETENTION_STATE_LEGACY_VERSION = 1
RETENTION_ANCHOR_ENCODING = authority_ledger.RETENTION_ANCHOR_ENCODING
RETENTION_ANCHOR_FIELDS = authority_ledger.RETENTION_ANCHOR_FIELDS


class RetainedRetryOutcome(dict[str, object]):
    """Retry metadata whose provenance is a verified checkpoint payload."""

    checkpoint_authenticated_retention = True


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
        if isinstance(registration, dict) and not authenticated_gone_owner_abort(
            record, start, authenticated_coordinator_ids, governed_records
        ):
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
        if isinstance(registration, dict) and not authenticated_gone_owner_abort(
            record, start, authenticated_coordinator_ids, governed_records
        ):
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
    anchored_digests = retained_open_before_record_digests(authority_history)
    if anchored_digests:
        for record in governed_records:
            if canonical_record_digest(record) in anchored_digests:
                repository_open_before.add(id(record))
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
        # Trust fields are ids of contained records at every construction
        # site; enforce that structurally so a foreign id can never survive
        # into a view (and, downstream, into an authentication cache entry).
        contained = {id(record) for record in self}
        self.trusted_checkpoint_id = (
            trusted_checkpoint_id if trusted_checkpoint_id in contained else None
        )
        self.trusted_retained_ids = frozenset(trusted_retained_ids) & contained
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


# One CLI invocation recomputes the identical authentication projection many
# times over the same loaded history (measured: 18 calls / 92.5s in one
# delivery-control status on the legacy stream, #4016). The projection is
# deterministic over the exact record objects, their order, and the view's
# trust fields, so memoize on those identities. Entries hold strong references
# to the authenticated records, so a cached object id can never be recycled by
# a different object; appending to a history changes the key and therefore
# self-invalidates. In-place mutation of an already-authenticated record is
# outside the append-only authority contract.
_AUTHENTICATION_CACHE_LIMIT = 8
_authentication_cache: dict[tuple[object, ...], tuple[object, frozenset[int]]] = {}
_REVIEW_STATE_CACHE_LIMIT = 16
_generation_projection_cache: dict[
    tuple[object, ...], tuple[object, dict[str, object]]
] = {}
_carry_projection_cache: dict[
    tuple[object, ...], tuple[object, tuple[object, ...]]
] = {}
_link_projection_cache: dict[
    tuple[object, ...], tuple[object, tuple[object, ...]]
] = {}
_slot_projection_cache: dict[
    tuple[object, ...], tuple[object, object]
] = {}
_legacy_delta_projection_cache: dict[
    tuple[object, ...],
    tuple[object, tuple[tuple[object, ...], dict[str, tuple[str, ...]]]],
] = {}


def _authentication_cache_key(
    records: Sequence[dict[str, object]],
) -> tuple[object, ...]:
    if isinstance(records, AuthorityRecordView):
        # The prefix dict is rebuilt by every filtered() derivation, so key it
        # by canonical content, not identity; identity keys made checkpoint-
        # backed (v3) histories miss on every derived view. The accumulator
        # head participates for the same reason even though the current
        # prefix-match path list-slices views before reaching its v3 branch.
        prefix = records.checkpoint_prefix
        try:
            prefix_key: object = (
                None
                if prefix is None
                else json.dumps(prefix, sort_keys=True, separators=(",", ":"))
            )
        except (TypeError, ValueError):
            prefix_key = object()  # unique: never matches, always recomputes
        head = records.accumulator_head
        head_key = (
            None
            if head is None
            else (
                head.ledger_id,
                head.repository_binding,
                head.record_count,
                head.records_sha256,
            )
        )
        return (
            tuple(map(id, records)),
            records.trusted_checkpoint_id,
            records.trusted_retained_ids,
            prefix_key,
            head_key,
        )
    return (tuple(map(id, records)), None, frozenset(), None, None)


def _review_cache_store(cache: dict, key: tuple[object, ...], records, value) -> None:
    """Bound projection caches while pinning record identities against reuse."""
    if len(cache) >= _REVIEW_STATE_CACHE_LIMIT:
        cache.pop(next(iter(cache)))
    cache[key] = ((tuple(records), records), value)


def _authenticated_coordinator_record_ids(
    records: Sequence[dict[str, object]],
) -> frozenset[int]:
    """Authenticate coordinator rows, memoized per exact loaded history."""
    key = _authentication_cache_key(records)
    cached = _authentication_cache.get(key)
    if cached is not None:
        return cached[1]
    result, operational_failure = _authenticate_coordinator_record_ids_uncached(records)
    if operational_failure:
        # A transient provider failure (for example an Ed25519 timeout) means
        # some proofs were skipped without being judged; never cache that
        # partial projection as if it were a durable rejection.
        return result
    if len(_authentication_cache) >= _AUTHENTICATION_CACHE_LIMIT:
        # Evict the oldest entry (insertion order); each entry pins only its
        # own values, so partial eviction cannot unpin another entry's ids.
        _authentication_cache.pop(next(iter(_authentication_cache)))
    _authentication_cache[key] = ((tuple(records), records), result)
    return result


def _authenticate_coordinator_record_ids_uncached(
    records: Sequence[dict[str, object]],
) -> tuple[frozenset[int], bool]:
    """Authenticate coordinator rows with an ordered host-authority cutover."""
    accepted: set[int] = set()
    operational_failure = False
    pending_host: set[int] = set()
    cutover_seen = False
    host_authority_active = False
    trusted_view = records if isinstance(records, AuthorityRecordView) else None
    for index, record in enumerate(records):
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
        except TerminalAuthorityError as exc:
            if isinstance(exc, TerminalAuthorityOperationalError):
                operational_failure = True
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
    return frozenset(accepted), operational_failure


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
    accepted: set[int] = set()
    if isinstance(records, AuthorityRecordView):
        # Only checkpoint-authenticated retained IDs preserve prefix membership.
        accepted.update(
            id(record)
            for record in records
            if id(record) in records.trusted_retained_ids
            and record.get("type") != RETENTION_STATE_TYPE
            and not isinstance(record.get("terminal_authority_proof"), dict)
        )
    for record in records:
        if _is_coordinator_cutover_attempt(record):
            break
        accepted.add(id(record))
    return frozenset(accepted)


def authenticated_retention_state_records(
    records: Sequence[dict[str, object]],
) -> list[dict[str, object]]:
    """Return only checkpoint-authenticated compact authority state records."""
    if not isinstance(records, AuthorityRecordView):
        if any(record.get("type") == RETENTION_STATE_TYPE for record in records):
            raise DispatchError(
                "authority retention state is not checkpoint-authenticated"
            )
        return []
    states: list[dict[str, object]] = []
    for record in current_telemetry(records):
        if record.get("type") != RETENTION_STATE_TYPE:
            continue
        if id(record) not in records.trusted_retained_ids:
            continue
        version = record.get("retention_state_version")
        if version not in {RETENTION_STATE_LEGACY_VERSION, RETENTION_STATE_VERSION}:
            raise DispatchError("authority retention state version is invalid")
        if version == RETENTION_STATE_LEGACY_VERSION:
            for field in RETENTION_ANCHOR_FIELDS:
                if not isinstance(record.get(field), list):
                    raise DispatchError(
                        f"authority retention state {field} is invalid"
                    )
        else:
            if any(field in record for field in RETENTION_ANCHOR_FIELDS):
                raise DispatchError(
                    "authority retention state mixes compact and legacy anchors"
                )
            _retention_anchor_fields_for_state(record)
        states.append(record)
    return states


def encode_retention_anchor_fields(
    anchors: Mapping[str, object],
) -> dict[str, object]:
    """Encode exact historical anchors into one checkpoint-authenticated blob."""
    try:
        return authority_ledger.encode_retention_anchor_fields(anchors)
    except authority_ledger.DispatchLedgerError as exc:
        raise DispatchError(str(exc)) from exc


def _retention_anchor_fields_for_state(
    state: Mapping[str, object],
) -> dict[str, list[object]]:
    if state.get("retention_state_version") == RETENTION_STATE_LEGACY_VERSION:
        return cast(
            dict[str, list[object]],
            copy.deepcopy({field: state[field] for field in RETENTION_ANCHOR_FIELDS}),
        )
    encoding = state.get("anchor_encoding")
    payload = state.get("anchor_payload")
    digest = state.get("anchor_payload_sha256")
    size = state.get("anchor_uncompressed_bytes")
    if (
        encoding != RETENTION_ANCHOR_ENCODING
        or not isinstance(payload, str)
        or not isinstance(digest, str)
        or type(size) is not int
    ):
        raise DispatchError("authority retention anchor metadata is invalid")
    try:
        return authority_ledger.decode_retention_anchor_fields(payload, digest, size)
    except authority_ledger.DispatchLedgerError as exc:
        raise DispatchError(str(exc)) from exc


def retention_anchor_fields(
    records: Sequence[dict[str, object]],
) -> dict[str, list[object]]:
    """Project exact anchors from legacy or compact authenticated state."""
    anchors: dict[str, list[object]] = {
        field: [] for field in RETENTION_ANCHOR_FIELDS
    }
    for state in authenticated_retention_state_records(records):
        decoded = _retention_anchor_fields_for_state(state)
        for field in RETENTION_ANCHOR_FIELDS:
            anchors[field].extend(decoded[field])
    return anchors


def retained_task_ids(records: Sequence[dict[str, object]]) -> frozenset[str]:
    """Project historical task identities sealed into retention state."""
    task_ids: set[str] = set()
    raw = retention_anchor_fields(records)["task_ids"]
    if not all(isinstance(item, str) and item for item in raw):
        raise DispatchError("authority retention state task_ids are invalid")
    task_ids.update(cast(list[str], raw))
    return frozenset(task_ids)


def retained_work_unit_contracts(
    records: Sequence[dict[str, object]],
) -> tuple[tuple[str, str], ...]:
    """Project immutable unit contracts sealed into retention state."""
    contracts: set[tuple[str, str]] = set()
    raw = retention_anchor_fields(records)["work_unit_contracts"]
    for item in raw:
        if not isinstance(item, dict):
            raise DispatchError(
                "authority retention state work_unit_contracts are invalid"
            )
        work_unit_id = item.get("work_unit_id")
        contract_hash = item.get("task_contract_hash")
        if (
            not isinstance(work_unit_id, str)
            or not work_unit_id
            or not isinstance(contract_hash, str)
        ):
            raise DispatchError(
                "authority retention state work_unit_contracts are invalid"
            )
        contracts.add((work_unit_id, contract_hash))
    return tuple(sorted(contracts))


def retained_attempt_settlements(
    records: Sequence[dict[str, object]],
) -> list[dict[str, object]]:
    """Project compact settlement watermarks sealed into retention state."""
    settlements: list[dict[str, object]] = []
    raw = retention_anchor_fields(records)["attempt_settlements"]
    for item in raw:
        if not isinstance(item, dict):
            raise DispatchError(
                "authority retention state attempt_settlements are invalid"
            )
        task_id = item.get("task_id")
        attempt_index = item.get("attempt_index")
        source_digest = item.get("source_record_digest")
        if (
            not isinstance(task_id, str)
            or not task_id
            or type(attempt_index) is not int
            or attempt_index < -1
            or not isinstance(source_digest, str)
            or re.fullmatch(r"[0-9a-f]{64}", source_digest) is None
        ):
            raise DispatchError(
                "authority retention state attempt_settlements are invalid"
            )
        settlements.append(dict(item))
    return settlements


def retained_retry_outcomes(
    records: Sequence[dict[str, object]],
) -> list[dict[str, object]]:
    """Project compact authenticated retry outcomes from retention state."""
    outcomes: list[dict[str, object]] = []
    raw = retention_anchor_fields(records)["retry_outcomes"]
    for item in raw:
        if (
            not isinstance(item, dict)
            or item.get("type") not in DISPATCH_OUTCOME_TYPES
            or not isinstance(item.get("task_id"), str)
            or (
                item.get("unit_attempt_number") is not None
                and type(item.get("unit_attempt_number")) is not int
            )
        ):
            raise DispatchError(
                "authority retention state retry_outcomes are invalid"
            )
        outcomes.append(RetainedRetryOutcome(item))
    return outcomes


def retained_open_before_record_digests(
    records: Sequence[dict[str, object]],
) -> frozenset[str]:
    """Project quarantined retained rows sealed into compact state."""
    digests: set[str] = set()
    raw = retention_anchor_fields(records)["open_before_record_digests"]
    if not all(
        isinstance(item, str) and re.fullmatch(r"[0-9a-f]{64}", item)
        for item in raw
    ):
        raise DispatchError(
            "authority retention state open_before_record_digests are invalid"
        )
    digests.update(cast(list[str], raw))
    return frozenset(digests)


def _authenticated_attempt_terminal_ids(
    records: Sequence[dict[str, object]],
    *,
    _open_before_record_ids: frozenset[int] | None = None,
    _registered_before_record_ids: frozenset[int] | None = None,
) -> frozenset[int]:
    """Authenticate registered deposits while preserving proofless history."""
    accepted, _registrations = _authenticated_attempt_terminal_projection(
        records,
        _open_before_record_ids=_open_before_record_ids,
        _registered_before_record_ids=_registered_before_record_ids,
    )
    return accepted


def _authenticated_attempt_terminal_registrations(
    records: Sequence[dict[str, object]],
) -> dict[int, dict[str, object]]:
    """Map each registered, authenticated settlement to its registration.

    The registration is the coordinator-signed (or legacy proofless) start of
    the very attempt the settlement completes: the unique start sharing the
    settlement's ``(task_id, attempt_index, run_id, work_unit_id, worktree)``
    whose registered dispatcher key verified the settlement.  Consumers that
    act on claims a dispatcher makes about its attempt (for example a delivery
    PR number) must bind those claims to this registration rather than trust
    the dispatcher's self-description.
    """
    _accepted, registrations = _authenticated_attempt_terminal_projection(records)
    return registrations


def _authenticated_attempt_terminal_projection(
    records: Sequence[dict[str, object]],
    *,
    _open_before_record_ids: frozenset[int] | None = None,
    _registered_before_record_ids: frozenset[int] | None = None,
) -> tuple[frozenset[int], dict[int, dict[str, object]]]:
    """Return accepted settlement ids and the registered settlement bindings."""
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
    registrations: dict[int, dict[str, object]] = {}
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
            if not authenticated_gone_owner_abort(
                record, candidates[0], authenticated_coordinator_ids, governed_records
            ):
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
        registrations[id(record)] = candidates[0]
    return frozenset(accepted), registrations


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
    preserved_fields = _SUPERSESSION_PRESERVED_FIELDS
    if supersession.get("supersession_reason") == "identity-unverifiable":
        preserved_fields += ("root_work_unit_id", "task_contract", "lineage")
    return (
        supersession_worktree is not None
        and supersession_worktree == deposit_worktree
        and _terminal_authority_attempt_key(supersession)
        == _terminal_authority_attempt_key(deposit)
        and all(
            deposit.get(field) == supersession.get(field)
            for field in preserved_fields
        )
        and supersession.get("superseded_task_id") == deposit.get("task_id")
        and supersession.get("status") == "superseded"
        and supersession_reason_matches_terminal(supersession, deposit)
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
    deposits_by_key = archived_supersession_deposits(
        retained_retry_outcomes(authority_history), governed_records
    )
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


def authenticated_review_state_records(
    records: Sequence[dict[str, object]],
) -> list[dict[str, object]]:
    """Return coordinator-authenticated D29 review-state rows in ledger order.

    Unlike observational telemetry, these rows carry their own record version
    and are selected by authority rather than the consumer's telemetry schema.
    Proofless rows are never grandfathered into this post-cutover authority.
    """
    from .review_state import (
        GENERATION_CARRY_TYPE,
        GENERATION_LINK_TYPE,
        GENERATION_PROOF_TYPE,
        REVIEW_GENERATION_TYPE,
        REVIEW_SLOT_RESERVATION_TYPE,
        REVIEW_SLOT_SETTLEMENT_TYPE,
    )

    record_types = {
        REVIEW_GENERATION_TYPE,
        GENERATION_PROOF_TYPE,
        GENERATION_LINK_TYPE,
        GENERATION_CARRY_TYPE,
        REVIEW_SLOT_RESERVATION_TYPE,
        REVIEW_SLOT_SETTLEMENT_TYPE,
    }
    authenticated = _authenticated_coordinator_record_ids(records)
    return [
        record
        for record in records
        if record.get("type") in record_types and id(record) in authenticated
    ]


def _legacy_review_generations(
    records: Sequence[dict[str, object]],
) -> dict[str, object]:
    """Synthesize conservative content generations for accepted old terminals."""
    from .review_state import (
        GenerationCarryV1,
        ReviewGenerationV1,
        generation_id_for,
        patch_identity_digest,
    )

    authenticated = _authenticated_coordinator_record_ids(records)
    d29_cutover_index = next(
        (
            index
            for index, record in enumerate(records)
            if record.get("type") == "review-generation-v1"
            and id(record) in authenticated
        ),
        len(records),
    )
    if not any(
        record.get("type") in {"attempt-terminal", "attempt-recovery"}
        for record in records[:d29_cutover_index]
    ):
        return {}
    try:
        accepted = seam_accepted_review_terminals(records)
        verdicts = seam_authenticated_verdicts(records, _accepted_terminals=accepted)
    except MissingAdapter as exc:
        raise MissingAdapter(
            "legacy review projection requires accepted-terminal and verdict adapters"
        ) from exc
    record_indices = {id(record): index for index, record in enumerate(records)}
    legacy: dict[str, ReviewGenerationV1] = {}
    primary_terminals: dict[str, Mapping[str, object]] = {}
    for task_id, terminal in accepted.items():
        contract = terminal.get("task_contract")
        is_delta = terminal.get("delta_from_snapshot_sha") is not None or (
            isinstance(contract, Mapping)
            and contract.get("delta_from_snapshot_sha") is not None
        )
        if (
            terminal.get("advisory") is True
            or is_delta
            or record_indices.get(id(terminal), len(records)) >= d29_cutover_index
        ):
            continue
        verdict = verdicts.get(task_id)
        if not isinstance(verdict, Mapping) or verdict.get("verdict") not in {
            "pass",
            "fail",
        }:
            continue
        identity = terminal.get("patch_identity")
        tree = terminal.get("snapshot_tree_sha")
        if (
            not isinstance(identity, Mapping)
            or not isinstance(tree, str)
            or identity.get("candidate_tree_sha") != tree
        ):
            continue
        if set(identity) == {"candidate_sha", "candidate_tree_sha"}:
            candidate_sha = identity.get("candidate_sha")
            if not isinstance(candidate_sha, str) or re.fullmatch(
                r"[0-9a-f]{40,64}", candidate_sha
            ) is None:
                continue
            legacy_diff = hashlib.sha256(
                json.dumps(
                    ["legacy-patch-identity-v1", dict(identity)],
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode()
            ).hexdigest()
            identity = {
                "schema_version": "patch-identity-v1",
                "base_sha": candidate_sha,
                "base_tree_sha": tree,
                "candidate_sha": candidate_sha,
                "candidate_tree_sha": tree,
                "diff_format": "git-binary-full-index-no-renames-v1",
                "diff_sha256": legacy_diff,
                "patch_id_verbatim": candidate_sha,
            }
        chain_receipt = terminal.get("review_chain_receipt")
        raw_sections = (
            chain_receipt.get("required_sections")
            if isinstance(chain_receipt, Mapping)
            else contract.get("required_sections")
            if isinstance(contract, Mapping)
            else None
        )
        if not isinstance(raw_sections, list) or not all(
            isinstance(section, str) and section for section in raw_sections
        ):
            lens = terminal.get("review_lens")
            raw_sections = [lens] if isinstance(lens, str) and lens else ["code"]
        sections = tuple(sorted(set(raw_sections)))
        source = terminal.get("source_identity")
        accumulator = getattr(records, "accumulator_head", None)
        binding = getattr(accumulator, "repository_binding", None)
        if not isinstance(binding, str) or not binding:
            binding = terminal.get("repository_binding")
        if not isinstance(binding, str) or not binding:
            source_binding = (
                source.get("repository_binding")
                if isinstance(source, Mapping)
                else None
            )
            binding = (
                source_binding
                if isinstance(source_binding, str) and source_binding
                else "legacy-" + patch_identity_digest(identity)
            )
        generation_id = generation_id_for(binding, identity, tree, sections)
        policy_digest = hashlib.sha256(
            json.dumps(
                ["review-generation-policy-v1", list(sections)],
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
        ).hexdigest()
        legacy[generation_id] = ReviewGenerationV1(
            repository_binding=binding,
            lineage_id="rl_" + hashlib.sha256(
                json.dumps(
                    [
                        "legacy-review-lineage-v1",
                        binding,
                        patch_identity_digest(identity),
                        tree,
                    ],
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode()
            ).hexdigest()[:32],
            generation_id=generation_id,
            predecessor_id=None,
            patch_identity=dict(identity),
            tree=tree,
            required_sections=sections,
            policy_digest=policy_digest,
            delta_from_tree=None,
            delta_sha256=None,
            changed_paths=(),
            dependency_paths=(),
            primary_origin_receipt=canonical_record_digest(terminal),
            inherited_coverage=sections,
            invalidated_sections=(),
        )
        primary_terminals[generation_id] = terminal

    legacy_carries: list[GenerationCarryV1] = []
    legacy_delta_refs: dict[str, list[str]] = {}
    for task_id, terminal in accepted.items():
        contract = terminal.get("task_contract")
        delta_from_snapshot = terminal.get("delta_from_snapshot_sha")
        if delta_from_snapshot is None and isinstance(contract, Mapping):
            delta_from_snapshot = contract.get("delta_from_snapshot_sha")
        delta_from_tree = terminal.get("delta_from_tree_sha")
        if delta_from_tree is None and isinstance(contract, Mapping):
            delta_from_tree = contract.get("delta_from_tree_sha")
        terminal_index = record_indices.get(id(terminal), len(records))
        if (
            terminal.get("advisory") is True
            or not isinstance(delta_from_snapshot, str)
            or re.fullmatch(r"[0-9a-f]{40,64}", delta_from_snapshot) is None
            or (
                delta_from_tree is not None
                and not isinstance(delta_from_tree, str)
            )
            or terminal_index >= d29_cutover_index
        ):
            continue
        verdict = verdicts.get(task_id)
        if not isinstance(verdict, Mapping) or verdict.get("verdict") not in {
            "pass",
            "fail",
        }:
            continue
        identity = terminal.get("patch_identity")
        tree = terminal.get("snapshot_tree_sha")
        if (
            not isinstance(identity, Mapping)
            or not isinstance(tree, str)
            or identity.get("candidate_tree_sha") != tree
        ):
            continue
        if set(identity) == {"candidate_sha", "candidate_tree_sha"}:
            candidate_sha = identity.get("candidate_sha")
            if not isinstance(candidate_sha, str) or re.fullmatch(
                r"[0-9a-f]{40,64}", candidate_sha
            ) is None:
                continue
            legacy_diff = hashlib.sha256(
                json.dumps(
                    ["legacy-patch-identity-v1", dict(identity)],
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode()
            ).hexdigest()
            identity = {
                "schema_version": "patch-identity-v1",
                "base_sha": candidate_sha,
                "base_tree_sha": tree,
                "candidate_sha": candidate_sha,
                "candidate_tree_sha": tree,
                "diff_format": "git-binary-full-index-no-renames-v1",
                "diff_sha256": legacy_diff,
                "patch_id_verbatim": candidate_sha,
            }
        candidates: list[tuple[str, ReviewGenerationV1]] = []
        for generation_id, primary in primary_terminals.items():
            if record_indices.get(id(primary), len(records)) >= terminal_index:
                continue
            generation = legacy[generation_id]
            primary_snapshot = primary.get("snapshot_sha")
            primary_candidate = generation.patch_identity.get("candidate_sha")
            if delta_from_snapshot not in {primary_snapshot, primary_candidate}:
                continue
            if delta_from_tree is not None and delta_from_tree != generation.tree:
                continue
            candidates.append((generation_id, generation))
        if len(candidates) != 1:
            # Historical compatibility never guesses which primary owns a
            # delta. Ambiguous or incomplete links remain unindexed.
            continue
        generation_id, generation = candidates[0]
        chain_receipt = terminal.get("review_chain_receipt")
        raw_sections = (
            chain_receipt.get("required_sections")
            if isinstance(chain_receipt, Mapping)
            else contract.get("required_sections")
            if isinstance(contract, Mapping)
            else None
        )
        if not isinstance(raw_sections, list) or not all(
            isinstance(section, str) and section for section in raw_sections
        ):
            lens = terminal.get("review_lens")
            raw_sections = [lens] if isinstance(lens, str) and lens else ["code"]
        sections = tuple(
            sorted(set(generation.required_sections).intersection(raw_sections))
        )
        if not sections:
            continue
        delta_ref = canonical_record_digest(terminal)
        legacy_carries.append(
            GenerationCarryV1(
                generation_id=generation_id,
                from_identity=generation.patch_identity,
                to_identity=identity,
                proof={
                    "kind": "legacy-delta-review-v1",
                    "primary_terminal_ref": generation.primary_origin_receipt,
                    "delta_terminal_ref": delta_ref,
                },
                sections=sections,
            )
        )
        legacy_delta_refs.setdefault(generation_id, []).append(delta_ref)

    cache_key = _authentication_cache_key(records)
    _review_cache_store(
        _legacy_delta_projection_cache,
        cache_key,
        records,
        (
            tuple(legacy_carries),
            {
                generation_id: tuple(dict.fromkeys(refs))
                for generation_id, refs in legacy_delta_refs.items()
            },
        ),
    )
    return cast(dict[str, object], legacy)


def _legacy_delta_projection(
    records: Sequence[dict[str, object]],
) -> tuple[tuple[object, ...], dict[str, tuple[str, ...]]]:
    """Return authenticated historical delta endpoints and their receipts."""
    cache_key = _authentication_cache_key(records)
    cached = _legacy_delta_projection_cache.get(cache_key)
    if cached is None:
        _legacy_review_generations(records)
        cached = _legacy_delta_projection_cache.get(cache_key)
    return cached[1] if cached is not None else ((), {})


def generations(records: Sequence[dict[str, object]]) -> dict[str, object]:
    """Project authenticated native generations plus legacy primary seeds."""
    from .review_state import REVIEW_GENERATION_TYPE, ReviewGenerationV1

    cache_key = _authentication_cache_key(records)
    cached = _generation_projection_cache.get(cache_key)
    if cached is not None:
        return dict(cached[1])
    projected = _legacy_review_generations(records)
    legacy_ids = set(projected)
    ambiguous_ids: set[str] = set()
    authenticated_rows = authenticated_review_state_records(records)
    record_indices = {id(record): index for index, record in enumerate(records)}
    links = [
        (record_indices[id(record)], link)
        for record in authenticated_rows
        if record.get("type") == "review-generation-link-v1"
        for link in _parsed_generation_link(record)
    ]
    for record in authenticated_rows:
        if record.get("type") != REVIEW_GENERATION_TYPE:
            continue
        try:
            generation = ReviewGenerationV1.from_dict(record)
        except (TypeError, ValueError):
            continue
        if generation.generation_id in ambiguous_ids:
            continue
        previous = projected.get(generation.generation_id)
        if previous is not None and previous != generation:
            if generation.generation_id in legacy_ids:
                # A native D29 start replaces its synthetic compatibility view.
                projected.pop(generation.generation_id, None)
                legacy_ids.remove(generation.generation_id)
            else:
                # An authenticated native identifier collision is ambiguous and
                # cannot be resolved by record order.
                projected.pop(generation.generation_id, None)
                ambiguous_ids.add(generation.generation_id)
                continue
        if generation.predecessor_id is not None:
            predecessor = projected.get(generation.predecessor_id)
            if predecessor is None or getattr(predecessor, "lineage_id", None) != (
                generation.lineage_id
            ):
                continue
            generation_index = record_indices[id(record)]
            if not any(
                link_index < generation_index
                and link.repository_binding == generation.repository_binding
                and link.predecessor_generation_id == generation.predecessor_id
                and link.to_identity == generation.patch_identity
                for link_index, link in links
            ):
                continue
        projected[generation.generation_id] = generation
    _review_cache_store(
        _generation_projection_cache, cache_key, records, dict(projected)
    )
    return projected


def _parsed_generation_link(record: Mapping[str, object]) -> tuple[object, ...]:
    from .review_state import GenerationLinkV1

    try:
        return (GenerationLinkV1.from_dict(record),)
    except (TypeError, ValueError):
        return ()


def generation_links(records: Sequence[dict[str, object]]) -> tuple[object, ...]:
    """Project coordinator-authenticated exact content-predecessor links."""
    cache_key = _authentication_cache_key(records)
    cached = _link_projection_cache.get(cache_key)
    if cached is not None:
        return cached[1]
    projected = tuple(
        link
        for record in authenticated_review_state_records(records)
        if record.get("type") == "review-generation-link-v1"
        for link in _parsed_generation_link(record)
    )
    _review_cache_store(_link_projection_cache, cache_key, records, projected)
    return projected


def generation_carries(records: Sequence[dict[str, object]]) -> tuple[object, ...]:
    """Project proof-valid authenticated carries whose source is already known."""
    from .review_state import (
        GENERATION_CARRY_TYPE,
        GENERATION_PROOF_TYPE,
        GenerationCarryV1,
        GenerationProofV1,
        patch_content_digest,
        patch_identity_digest,
    )

    cache_key = _authentication_cache_key(records)
    cached = _carry_projection_cache.get(cache_key)
    if cached is not None:
        return cached[1]
    projected = generations(records)
    known: dict[str, set[str]] = {
        generation_id: {patch_identity_digest(generation.patch_identity)}
        for generation_id, generation in projected.items()
    }
    legacy_carries, _legacy_delta_refs = _legacy_delta_projection(records)
    carries: list[GenerationCarryV1] = [
        carry
        for carry in legacy_carries
        if carry.generation_id in projected
        and patch_identity_digest(carry.from_identity)
        in known.get(carry.generation_id, set())
    ]
    for carry in carries:
        known.setdefault(carry.generation_id, set()).add(
            patch_identity_digest(carry.to_identity)
        )
    authenticated_rows = authenticated_review_state_records(records)
    authenticated_ids = {id(record) for record in authenticated_rows}
    proof_rows: dict[int, GenerationProofV1] = {}
    for index, record in enumerate(records):
        if id(record) not in authenticated_ids or record.get("type") != GENERATION_PROOF_TYPE:
            continue
        try:
            proof_rows[index] = GenerationProofV1.from_dict(record)
        except (TypeError, ValueError):
            continue
    record_indices = {id(record): index for index, record in enumerate(records)}
    for record in authenticated_rows:
        if record.get("type") != GENERATION_CARRY_TYPE:
            continue
        try:
            carry = GenerationCarryV1.from_dict(record)
        except (TypeError, ValueError):
            continue
        generation = projected.get(carry.generation_id)
        source_digest = patch_identity_digest(carry.from_identity)
        target_digest = patch_identity_digest(carry.to_identity)
        if (
            generation is None
            or source_digest not in known.get(carry.generation_id, set())
            or not set(carry.sections) <= set(generation.required_sections)
        ):
            continue
        proof = carry.proof
        carry_index = record_indices[id(record)]
        proof_valid = any(
            proof_index < carry_index
            and candidate.to_dict() == proof
            and candidate.from_identity == carry.from_identity
            and candidate.to_identity == carry.to_identity
            for proof_index, candidate in proof_rows.items()
        ) or (
            proof
            == {
                "kind": "seen-content-v1",
                "content_digest": patch_content_digest(carry.to_identity),
                "tree": carry.to_identity.get("candidate_tree_sha"),
            }
            and patch_content_digest(carry.from_identity)
            == patch_content_digest(carry.to_identity)
        )
        if not proof_valid:
            continue
        carries.append(carry)
        known.setdefault(carry.generation_id, set()).add(target_digest)
    result = tuple(carries)
    _review_cache_store(_carry_projection_cache, cache_key, records, result)
    return result


def generation_proofs(records: Sequence[dict[str, object]]) -> tuple[object, ...]:
    """Project only coordinator-authenticated exact-pair kernel proof records."""
    from .review_state import GENERATION_PROOF_TYPE, GenerationProofV1

    proofs: list[GenerationProofV1] = []
    for record in authenticated_review_state_records(records):
        if record.get("type") != GENERATION_PROOF_TYPE:
            continue
        try:
            proofs.append(GenerationProofV1.from_dict(record))
        except (TypeError, ValueError):
            continue
    return tuple(proofs)


@dataclass(frozen=True, slots=True)
class ReviewSlotState:
    generation_id: str
    family: str
    reservations: tuple[object, ...]
    settlements: tuple[object, ...]
    inherited_primary_ref: str | None = None
    inherited_delta_refs: tuple[str, ...] = ()
    effective_outcomes: tuple[tuple[str, object], ...] = ()

    def settlement_for(self, reservation_id: str):
        return next(
            (
                settlement
                for settlement in reversed(self.settlements)
                if settlement.reservation_id == reservation_id
            ),
            None,
        )

    def _effective_outcome(self, settlement):
        return dict(self.effective_outcomes).get(
            settlement.reservation_id, settlement.outcome
        )

    @property
    def outstanding(self):
        from ..runners.contract import ReviewOutcome

        for reservation in reversed(self.reservations):
            settlement = self.settlement_for(reservation.reservation_id)
            if settlement is None or self._effective_outcome(settlement) is (
                ReviewOutcome.UNRESOLVED
            ):
                return reservation
        return None

    def attempt_count(self, slot_kind: str) -> int:
        return sum(
            1 for reservation in self.reservations
            if reservation.slot_kind == slot_kind
        )

    def _consumed(self, slot_kind: str) -> bool:
        from ..runners.contract import ReviewOutcome

        return any(
            settlement.slot_kind == slot_kind
            and self._effective_outcome(settlement) is ReviewOutcome.CONSUMED
            for settlement in self.settlements
        )

    @property
    def primary_consumed(self) -> bool:
        return self.inherited_primary_ref is not None or self._consumed("primary")

    @property
    def own_primary_consumed(self) -> bool:
        """Whether this generation, rather than an ancestor, consumed primary."""
        return self._consumed("primary")

    @property
    def delta_consumed(self) -> bool:
        return bool(self.inherited_delta_refs) or self._consumed("delta")

    @property
    def primary_terminal_ref(self) -> str | None:
        from ..runners.contract import ReviewOutcome

        for settlement in reversed(self.settlements):
            if (
                settlement.slot_kind == "primary"
                and self._effective_outcome(settlement) is ReviewOutcome.CONSUMED
            ):
                return cast(str, settlement.terminal_ref)
        return self.inherited_primary_ref

    @property
    def receipts(self) -> tuple[str, ...]:
        from ..runners.contract import ReviewOutcome

        values = [
            settlement.terminal_ref
            for settlement in self.settlements
            if self._effective_outcome(settlement) is ReviewOutcome.CONSUMED
        ]
        if self.inherited_primary_ref is not None:
            values.insert(0, self.inherited_primary_ref)
        values.extend(self.inherited_delta_refs)
        return tuple(dict.fromkeys(values))


def slot_state(
    records: Sequence[dict[str, object]], generation_id: str, family: str
) -> ReviewSlotState:
    """Fold authenticated reservations and terminal-bound settlements."""
    from .review_state import (
        REVIEW_SLOT_RESERVATION_TYPE,
        REVIEW_SLOT_SETTLEMENT_TYPE,
        ReviewSlotReservation,
        ReviewSlotSettlement,
    )
    from ..review.authority import classify_review_outcome

    cache_key = (*_authentication_cache_key(records), generation_id, family)
    cached = _slot_projection_cache.get(cache_key)
    if cached is not None:
        return cast(ReviewSlotState, cached[1])
    generation = generations(records).get(generation_id)
    authenticated = authenticated_review_state_records(records)
    reservations = []
    seen_reservation_ids: set[str] = set()
    for record in authenticated:
        if record.get("type") != REVIEW_SLOT_RESERVATION_TYPE:
            continue
        try:
            reservation = ReviewSlotReservation.from_dict(record)
        except (TypeError, ValueError):
            continue
        if (
            reservation.generation_id != generation_id
            or reservation.family != family
            or reservation.reservation_id in seen_reservation_ids
        ):
            continue
        reservations.append(reservation)
        seen_reservation_ids.add(reservation.reservation_id)

    coordinator = set(_authenticated_coordinator_record_ids(records))
    terminals = set(_authenticated_attempt_terminal_ids(records))
    terminal_by_digest = {
        canonical_record_digest(record): record
        for record in records
        if id(record) in coordinator or id(record) in terminals
    }
    from ..runners.contract import ReviewOutcome

    settlements = []
    settled_ids: set[str] = set()
    settled_reservations: set[str] = set()
    effective_outcomes: list[tuple[str, ReviewOutcome]] = []
    reservations_by_id = {
        reservation.reservation_id: reservation for reservation in reservations
    }
    for record in authenticated:
        if record.get("type") != REVIEW_SLOT_SETTLEMENT_TYPE:
            continue
        try:
            settlement = ReviewSlotSettlement.from_dict(record)
        except (TypeError, ValueError):
            continue
        reservation = reservations_by_id.get(settlement.reservation_id)
        terminal = terminal_by_digest.get(settlement.terminal_ref)
        classified = (
            classify_review_outcome(terminal, records)
            if terminal is not None
            else ReviewOutcome.UNRESOLVED
        )
        effective = (
            ReviewOutcome.UNRESOLVED
            if terminal is None
            else ReviewOutcome.CONSUMED
            if settlement.outcome is ReviewOutcome.UNRESOLVED
            and classified is ReviewOutcome.CONSUMED
            else settlement.outcome
        )
        generation_trees = (
            {generation.tree}
            if generation is not None
            else set()
        )
        generation_trees.update(
            cast(str, carry.to_identity["candidate_tree_sha"])
            for carry in generation_carries(records)
            if carry.generation_id == generation_id
            and isinstance(carry.to_identity.get("candidate_tree_sha"), str)
        )
        if (
            reservation is None
            or settlement.settlement_id in settled_ids
            or settlement.reservation_id in settled_reservations
            or (
                terminal is not None
                and terminal.get("task_id") != reservation.task_id
            )
            or (
                terminal is not None
                and terminal.get("snapshot_tree_sha") not in generation_trees
            )
            or settlement.generation_id != reservation.generation_id
            or settlement.family != reservation.family
            or settlement.slot_kind != reservation.slot_kind
            or settlement.task_id != reservation.task_id
            or (
                terminal is not None
                and settlement.outcome is not ReviewOutcome.UNRESOLVED
                and classified is not settlement.outcome
            )
        ):
            continue
        settlements.append(settlement)
        settled_ids.add(settlement.settlement_id)
        settled_reservations.add(settlement.reservation_id)
        effective_outcomes.append((settlement.reservation_id, effective))
    inherited = None
    inherited_deltas: tuple[str, ...] = ()
    if generation is not None:
        native_generation_ids = {
            record.get("generation_id")
            for record in authenticated
            if record.get("type") == "review-generation-v1"
        }
        if generation.generation_id not in native_generation_ids:
            # A synthesized legacy generation represents an already accepted
            # primary and has no native slot rows.
            if family == "delivery":
                inherited = generation.primary_origin_receipt
                _legacy_carries, delta_refs = _legacy_delta_projection(records)
                inherited_deltas = delta_refs.get(generation_id, ())
        elif generation.predecessor_id is not None:
            predecessor = generations(records).get(generation.predecessor_id)
            predecessor_state = slot_state(records, generation.predecessor_id, family)
            predecessor_complete = bool(
                predecessor_state.own_primary_consumed
                or (
                    predecessor_state.inherited_primary_ref is not None
                    and predecessor is not None
                    and (
                        not predecessor.invalidated_sections
                        or predecessor_state.delta_consumed
                    )
                )
            )
            candidate = (
                predecessor_state.primary_terminal_ref
                if predecessor_complete
                else None
            )
            if family != "delivery" or generation.primary_origin_receipt == candidate:
                inherited = candidate
    result = ReviewSlotState(
        generation_id=generation_id,
        family=family,
        reservations=tuple(reservations),
        settlements=tuple(settlements),
        inherited_primary_ref=inherited,
        inherited_delta_refs=inherited_deltas,
        effective_outcomes=tuple(effective_outcomes),
    )
    _review_cache_store(_slot_projection_cache, cache_key, records, result)
    return result


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
