#!/usr/bin/env python3
"""Protected authority-ledger loading, locking, and retained projections.

Move-only extraction: this sibling must not import ``agent_dispatch``.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import subprocess
import sys
import threading
from collections.abc import Collection, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast

sys.path.insert(0, str(Path(__file__).resolve().parent))

import dispatch_ledger as authority_ledger
from dispatch_authority import (
    CoordinatorAuthority,
    TerminalAuthority,
    TerminalAuthorityError,
    load_coordinator_ledger_state,
    verify_terminal_authority,
)
from dispatch_authority_projection import (
    AuthorityLedgerSnapshot,
    AuthorityRecordView,
    _apply_open_write_record,
    _authenticated_coordinator_record_ids,
    _authenticated_open_before_record_ids,
    _authenticated_registration_start_ids,
    _authority_record_list,
    _is_coordinator_cutover_attempt,
    _terminal_authority_attempt_key,
    _terminal_authority_work_unit,
    current_telemetry,
)
from dispatch_common import (
    DispatchError,
    primary_repo_root,
    resolved_record_worktree,
    trusted_git_command,
)
from dispatch_review_authority import (
    _latest_attempt_settlement_indices,
    accepted_review_terminals,
    authenticated_retry_outcomes,
    authenticated_review_terminals,
    authenticated_supersessions,
    authenticated_verdicts,
    delivery_controller_records,
    latest_explicit_alias_availability,
)
from dispatch_routing import (
    ATTEMPT_HISTORY_TYPES,
    AUTHORITY_ARCHIVE_DIRECTORY,
    AUTHORITY_DOWNGRADE_BARRIER_NAME,
    AUTHORITY_LEDGER_DIRECTORY,
    COORDINATOR_LEDGER_PREFIX_SCHEME,
    DISPATCH_DIR,
    LEGACY_COORDINATOR_LEDGER_PREFIX_SCHEME,
)
from finding_ledger import canonical_record_digest
from guardian_sandbox import environment as sandbox_environment
from skill_run_log import active_run, load_entries as load_skill_run_entries

_ATTEMPT_LOCK_STATE = threading.local()
_AUTHORITY_LEDGER_LOCK_STATE = threading.local()


def worktree_branch(worktree: Path) -> str:
    completed = subprocess.run(
        trusted_git_command(worktree, "symbolic-ref", "--quiet", "--short", "HEAD"),
        capture_output=True,
        text=True,
        check=False,
        env=sandbox_environment(os.environ),
    )
    if completed.returncode != 0 or not completed.stdout.strip():
        raise DispatchError("governed dispatch requires a named delivery branch")
    return completed.stdout.strip()


def active_outer_run_id(
    worktree: Path,
    *,
    authority_repo: Path | None = None,
    branch: str | None = None,
) -> str | None:
    """Resolve the sole active delivery run for this checkout's branch."""
    repo = (
        authority_repo.resolve()
        if authority_repo is not None
        else primary_repo_root(worktree)
    )
    audit_dir = repo / ".audit" / "skill-runs"
    if not audit_dir.is_dir():
        return None
    branch = branch or worktree_branch(worktree)
    try:
        resolved = active_run(load_skill_run_entries(audit_dir), git_branch=branch)
    except ValueError as exc:
        if "multiple active logical runs" not in str(exc):
            raise DispatchError(str(exc)) from exc
        raise DispatchError(
            f"multiple active outer runs own branch {branch!r}; close the stale run"
        ) from exc
    return resolved[0] if resolved is not None else None


def _attempt_lock_key(task_id: object) -> str:
    return hashlib.sha256(str(task_id).encode("utf-8", "surrogatepass")).hexdigest()


def _held_attempt_locks() -> set[str]:
    held = getattr(_ATTEMPT_LOCK_STATE, "task_ids", None)
    if held is None:
        held = set()
        _ATTEMPT_LOCK_STATE.task_ids = held
    return cast(set[str], held)


def _held_authority_ledger_locks() -> set[str]:
    held = getattr(_AUTHORITY_LEDGER_LOCK_STATE, "repos", None)
    if held is None:
        held = set()
        _AUTHORITY_LEDGER_LOCK_STATE.repos = held
    return cast(set[str], held)


@contextmanager
def authority_ledger_lock(repo: Path):
    """Serialize authority activation and every telemetry append per repo."""
    key = str(repo.resolve())
    held = _held_authority_ledger_locks()
    if key in held:
        yield
        return
    git_directory = repo / ".git"
    lock_path = (
        git_directory / "intelflo-authority-ledger.lock"
        if git_directory.is_dir()
        else repo / ".authority-ledger.lock"
    )
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        held.add(key)
        try:
            yield
        finally:
            held.remove(key)
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


@contextmanager
def attempt_lifecycle_lock(repo: Path, task_id: object):
    """Serialize terminal decisions for one task across dispatcher processes."""
    key = _attempt_lock_key(task_id)
    held = _held_attempt_locks()
    if key in held:
        yield
        return
    lock_path = repo / DISPATCH_DIR / ".attempt-locks" / f"{key}.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        held.add(key)
        try:
            yield
        finally:
            held.remove(key)
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def create_terminal_authority() -> TerminalAuthority:
    try:
        return TerminalAuthority.generate()
    except TerminalAuthorityError as exc:
        raise DispatchError(f"cannot create terminal authority: {exc}") from exc


def create_coordinator_authority() -> CoordinatorAuthority:
    try:
        return CoordinatorAuthority.from_local_state()
    except TerminalAuthorityError as exc:
        raise DispatchError(f"cannot create coordinator authority: {exc}") from exc


def _authority_git_common_directory(repo: Path) -> Path:
    completed = subprocess.run(
        trusted_git_command(
            repo, "rev-parse", "--path-format=absolute", "--git-common-dir"
        ),
        capture_output=True,
        text=True,
        check=False,
        env=sandbox_environment(os.environ),
    )
    if completed.returncode != 0:
        raise DispatchError("authority ledger repository identity is unavailable")
    raw = completed.stdout.strip()
    if not raw:
        raise DispatchError("authority ledger repository identity is invalid")
    common = Path(raw)
    if not common.is_absolute():
        common = repo / common
    return common.resolve(strict=True)


def _authority_repository_binding(repo: Path) -> str:
    try:
        return authority_ledger.repository_binding(
            _authority_git_common_directory(repo)
        )
    except authority_ledger.DispatchLedgerError as exc:
        raise DispatchError(f"authority ledger identity is invalid: {exc}") from exc


def _load_legacy_authority_records(repo: Path) -> list[dict[str, object]]:
    directory = repo / DISPATCH_DIR
    if not directory.is_dir():
        return []
    records: list[dict[str, object]] = []
    for path in sorted(directory.glob("*.jsonl")):
        try:
            datetime.strptime(path.stem, "%Y-%m-%d").replace(tzinfo=UTC)
        except ValueError:
            continue
        try:
            records.extend(authority_ledger.read_legacy_jsonl_records(path))
        except authority_ledger.DispatchLedgerError as exc:
            detail = str(exc)
            if detail.startswith("invalid JSON"):
                raise DispatchError(
                    detail.replace(
                        "invalid JSON in ",
                        "invalid JSON in dispatch authority ",
                        1,
                    )
                ) from exc
            if detail.startswith("invalid record"):
                raise DispatchError(
                    detail.replace(
                        "invalid record in ",
                        "invalid record in dispatch authority ",
                        1,
                    )
                ) from exc
            raise DispatchError(
                f"cannot read dispatch authority {path}: {detail}"
            ) from exc
    return records


def _load_archive_manifest(
    repo: Path,
    state: authority_ledger.AuthorityLedgerHostStateV1,
    checkpoint: authority_ledger.AuthorityLedgerCheckpointV1,
    *,
    full: bool,
) -> authority_ledger.ArchiveManifestV1:
    manifest_path = repo / DISPATCH_DIR / state.archive_manifest_relative_path
    try:
        raw = json.loads(authority_ledger.read_secure_bytes(manifest_path))
        if not isinstance(raw, dict):
            raise authority_ledger.DispatchLedgerError(
                "archive manifest fields are invalid"
            )
        manifest = authority_ledger.ArchiveManifestV1.from_mapping(raw)
        if (
            manifest.ledger_id != state.ledger_id
            or manifest.generation != state.generation - 1
            or manifest.logical_digest() != state.archive_manifest_sha256
            or manifest.logical_digest() != checkpoint.archive_manifest_sha256
            or tuple(segment.stat_seal.to_dict() for segment in manifest.segments)
            != state.archive_stat_seals
        ):
            raise authority_ledger.DispatchLedgerError(
                "archive manifest does not match protected head"
            )
        return authority_ledger.verify_archive_manifest(
            manifest_path.parent / "segments", manifest, full=full
        )
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise DispatchError("authority archive manifest is invalid") from exc
    except authority_ledger.DispatchLedgerError as exc:
        raise DispatchError(f"authority archive verification failed: {exc}") from exc


def load_authority_snapshot(
    repo: Path, *, full_archive_verify: bool = False
) -> AuthorityLedgerSnapshot:
    """Load the legacy stream or one protected checkpoint generation."""
    try:
        binding = _authority_repository_binding(repo)
    except DispatchError:
        if (repo / AUTHORITY_LEDGER_DIRECTORY).exists():
            raise
        legacy = _load_legacy_authority_records(repo)
        view = AuthorityRecordView(legacy)
        return AuthorityLedgerSnapshot(
            records=view,
            physical_records=tuple(legacy),
            checkpoint=None,
            accumulator_head=None,
            host_state=None,
        )
    try:
        raw_state = load_coordinator_ledger_state(binding)
    except TerminalAuthorityError as exc:
        raise DispatchError(f"authority protected head is invalid: {exc}") from exc
    if raw_state is None:
        generations = repo / AUTHORITY_LEDGER_DIRECTORY / "generations"
        barrier = repo / DISPATCH_DIR / AUTHORITY_DOWNGRADE_BARRIER_NAME
        if barrier.exists() or any(
            generations.iterdir() if generations.is_dir() else ()
        ):
            raise DispatchError(
                "authority protected head is missing; restore the private host "
                "state before running authority-ledger recover"
            )
        legacy = _load_legacy_authority_records(repo)
        view = AuthorityRecordView(legacy)
        return AuthorityLedgerSnapshot(
            records=view,
            physical_records=tuple(legacy),
            checkpoint=None,
            accumulator_head=None,
            host_state=None,
        )
    try:
        state = authority_ledger.AuthorityLedgerHostStateV1.from_mapping(raw_state)
        if state.repository_binding != binding:
            raise authority_ledger.DispatchLedgerError(
                "protected repository binding does not match"
            )
        active_path = repo / DISPATCH_DIR / state.active_relative_path
        active_payload = authority_ledger.read_secure_bytes(active_path)
        physical = authority_ledger.parse_jsonl_records(
            active_payload, label=str(active_path)
        )
        if not physical:
            raise authority_ledger.DispatchLedgerError("active generation is empty")
        checkpoint_record = physical[0]
        checkpoint = authority_ledger.AuthorityLedgerCheckpointV1.from_record(
            checkpoint_record
        )
        verify_terminal_authority(
            checkpoint_record, registration=None, expected_kind="coordinator"
        )
        checkpoint_digest = authority_ledger.canonical_record_digest(checkpoint_record)
        if (
            checkpoint.repository_binding != binding
            or checkpoint.ledger_id != state.ledger_id
            or checkpoint.generation != state.generation
            or checkpoint_digest != state.checkpoint_digest
            or checkpoint.predecessor_checkpoint_digest
            != state.predecessor_checkpoint_digest
            or checkpoint.cumulative.record_count != checkpoint.archived_record_count
        ):
            raise authority_ledger.DispatchLedgerError(
                "checkpoint does not match protected head"
            )
        _load_archive_manifest(repo, state, checkpoint, full=full_archive_verify)
        barrier = repo / DISPATCH_DIR / AUTHORITY_DOWNGRADE_BARRIER_NAME
        if (
            hashlib.sha256(authority_ledger.read_secure_bytes(barrier)).hexdigest()
            != state.downgrade_barrier_sha256
        ):
            raise authority_ledger.DispatchLedgerError(
                "downgrade barrier does not match protected head"
            )
        unexpected = [
            path.name
            for path in (repo / DISPATCH_DIR).glob("*.jsonl")
            if path.name != AUTHORITY_DOWNGRADE_BARRIER_NAME
        ]
        if unexpected:
            raise authority_ledger.DispatchLedgerError(
                "unexpected dated authority stream exists after compaction"
            )
        suffix = physical[1:]
        if any(
            _is_coordinator_cutover_attempt(record)
            and isinstance(record.get("ledger_prefix"), dict)
            and cast(dict[str, object], record["ledger_prefix"]).get("scheme")
            in {
                LEGACY_COORDINATOR_LEDGER_PREFIX_SCHEME,
                COORDINATOR_LEDGER_PREFIX_SCHEME,
            }
            for record in suffix
        ):
            raise authority_ledger.DispatchLedgerError(
                "legacy coordinator cutover follows checkpoint"
            )
        head = authority_ledger.accumulate_records(
            physical,
            ledger_id=state.ledger_id,
            repository_binding=binding,
            seed=checkpoint.cumulative,
        )
        if (
            head.record_count != state.active_record_count
            or head.records_sha256 != state.active_records_sha256
            or len(active_payload) != state.active_byte_size
        ):
            raise authority_ledger.DispatchLedgerError(
                "active generation does not match protected head"
            )
        retained = list(checkpoint.retained_records)
        projected = [checkpoint_record, *retained, *suffix]
        view = AuthorityRecordView(
            projected,
            trusted_checkpoint_id=id(checkpoint_record),
            trusted_retained_ids={id(record) for record in retained},
            checkpoint_prefix=checkpoint.cumulative.to_dict(),
            accumulator_head=head,
        )
        return AuthorityLedgerSnapshot(
            records=view,
            physical_records=tuple(physical),
            checkpoint=checkpoint,
            accumulator_head=head,
            host_state=state,
        )
    except TerminalAuthorityError as exc:
        raise DispatchError(
            f"authority checkpoint signature is invalid: {exc}"
        ) from exc
    except authority_ledger.DispatchLedgerError as exc:
        raise DispatchError(f"authority checkpoint is invalid: {exc}") from exc


def _read_authenticated_archive_segment(
    path: Path,
    segment: authority_ledger.ArchiveSegmentV1,
) -> list[dict[str, object]]:
    try:
        payload = authority_ledger.read_secure_bytes(path)
        if (
            len(payload) != segment.byte_size
            or hashlib.sha256(payload).hexdigest() != segment.sha256
        ):
            raise authority_ledger.DispatchLedgerError(
                "archive segment digest does not match"
            )
        return authority_ledger.parse_jsonl_records(payload, label=str(path))
    except authority_ledger.DispatchLedgerError as exc:
        raise DispatchError(f"authority archive verification failed: {exc}") from exc


def _load_authenticated_archive_chain(
    repo: Path,
    checkpoint: authority_ledger.AuthorityLedgerCheckpointV1,
) -> list[tuple[Path, authority_ledger.ArchiveManifestV1]]:
    chain: list[tuple[Path, authority_ledger.ArchiveManifestV1]] = []
    current = checkpoint
    while True:
        manifest_path = (
            repo
            / AUTHORITY_ARCHIVE_DIRECTORY
            / current.ledger_id
            / f"generation-{current.generation - 1:08d}"
            / "manifest.json"
        )
        try:
            raw = json.loads(authority_ledger.read_secure_bytes(manifest_path))
            if not isinstance(raw, Mapping):
                raise authority_ledger.DispatchLedgerError(
                    "archive manifest fields are invalid"
                )
            manifest = authority_ledger.ArchiveManifestV1.from_mapping(raw)
            if (
                manifest.ledger_id != current.ledger_id
                or manifest.generation != current.generation - 1
                or manifest.logical_digest() != current.archive_manifest_sha256
            ):
                raise authority_ledger.DispatchLedgerError(
                    "archive chain does not match checkpoint"
                )
            authority_ledger.verify_archive_manifest(
                manifest_path.parent / "segments", manifest, full=False
            )
            chain.append((manifest_path, manifest))
            if current.generation == 1:
                return chain
            archived: list[dict[str, object]] = []
            for segment in manifest.segments:
                archived.extend(
                    _read_authenticated_archive_segment(
                        manifest_path.parent / "segments" / segment.relative_name,
                        segment,
                    )
                )
            if not archived:
                raise authority_ledger.DispatchLedgerError(
                    "archived generation is empty"
                )
            predecessor_record = archived[0]
            predecessor = authority_ledger.AuthorityLedgerCheckpointV1.from_record(
                predecessor_record
            )
            verify_terminal_authority(
                predecessor_record,
                registration=None,
                expected_kind="coordinator",
            )
            if (
                predecessor.ledger_id != current.ledger_id
                or predecessor.repository_binding != current.repository_binding
                or predecessor.generation != current.generation - 1
                or authority_ledger.canonical_record_digest(predecessor_record)
                != current.predecessor_checkpoint_digest
            ):
                raise authority_ledger.DispatchLedgerError(
                    "checkpoint predecessor chain does not match"
                )
            reconstructed = authority_ledger.accumulate_records(
                archived,
                ledger_id=current.ledger_id,
                repository_binding=current.repository_binding,
                seed=predecessor.cumulative,
            )
            if reconstructed != current.cumulative:
                raise authority_ledger.DispatchLedgerError(
                    "archive accumulator does not match checkpoint"
                )
            current = predecessor
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise DispatchError("authority archive manifest is invalid") from exc
        except (authority_ledger.DispatchLedgerError, TerminalAuthorityError) as exc:
            raise DispatchError(
                f"authority archive chain verification failed: {exc}"
            ) from exc


def _observational_authority_snapshot(repo: Path) -> AuthorityLedgerSnapshot | None:
    if (repo / AUTHORITY_LEDGER_DIRECTORY).exists():
        return load_authority_snapshot(repo)
    try:
        binding = _authority_repository_binding(repo)
    except DispatchError:
        return None
    try:
        protected_state = load_coordinator_ledger_state(binding)
    except TerminalAuthorityError as exc:
        raise DispatchError(f"authority protected head is invalid: {exc}") from exc
    return load_authority_snapshot(repo) if protected_state is not None else None


def _observation_record_is_recent(
    record: Mapping[str, object], cutoff: datetime
) -> bool:
    raw_timestamp = record.get("ts")
    if raw_timestamp is None:
        return True
    if not isinstance(raw_timestamp, str):
        raise DispatchError("authority observation timestamp is invalid")
    try:
        timestamp = datetime.fromisoformat(raw_timestamp)
    except ValueError as exc:
        raise DispatchError("authority observation timestamp is invalid") from exc
    if timestamp.tzinfo is None:
        raise DispatchError("authority observation timestamp is invalid")
    return timestamp.astimezone(UTC) >= cutoff - timedelta(days=1)


def load_records(repo: Path, days: int) -> list[dict[str, object]]:
    snapshot = _observational_authority_snapshot(repo)
    if snapshot is not None and snapshot.checkpoint is not None:
        cutoff = datetime.now(UTC) - timedelta(days=days)
        observed: list[dict[str, object]] = []
        chain = _load_authenticated_archive_chain(repo, snapshot.checkpoint)
        for manifest_path, manifest in reversed(chain):
            for segment in manifest.segments:
                maximum = (
                    datetime.fromisoformat(segment.max_timestamp)
                    if segment.max_timestamp is not None
                    else None
                )
                if maximum is not None and maximum < cutoff - timedelta(days=1):
                    continue
                observed.extend(
                    _read_authenticated_archive_segment(
                        manifest_path.parent / "segments" / segment.relative_name,
                        segment,
                    )
                )
        observed.extend(
            record
            for record in snapshot.physical_records[1:]
            if _observation_record_is_recent(record, cutoff)
        )
        return observed
    directory = repo / DISPATCH_DIR
    if not directory.is_dir():
        return []
    cutoff = datetime.now(UTC) - timedelta(days=days)
    records: list[dict[str, object]] = []
    for path in sorted(directory.glob("*.jsonl")):
        try:
            file_day = datetime.strptime(path.stem, "%Y-%m-%d").replace(tzinfo=UTC)
        except ValueError:
            continue
        if file_day < cutoff - timedelta(days=1):
            continue
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(record, dict):
                records.append(record)
    return records


def load_authority_records(repo: Path, days: int) -> list[dict[str, object]]:
    """Read dispatch authority strictly before governed admission decisions."""
    if not (repo / DISPATCH_DIR).is_dir():
        snapshot = load_authority_snapshot(repo)
        if snapshot.host_state is None:
            return load_records(repo, days)
        return snapshot.records
    del days  # Authority and cutover state is durable, never an observation window.
    return load_authority_snapshot(repo).records


def _stable_record_digests(
    records: Sequence[Mapping[str, object]],
) -> tuple[str, ...]:
    return tuple(canonical_record_digest(record) for record in records)


def _work_unit_contract_projection(
    records: Sequence[dict[str, object]],
) -> tuple[tuple[str, tuple[str, ...]], ...]:
    contracts: dict[str, set[str]] = {}
    for record in records:
        record_type = record.get("type")
        if record_type not in {*ATTEMPT_HISTORY_TYPES, "route"}:
            continue
        if record_type == "route" and not record.get("kept"):
            continue
        work_unit_id = str(record.get("work_unit_id") or record.get("task_id"))
        contract_hash = record.get("task_contract_hash")
        normalized = (
            contract_hash
            if isinstance(contract_hash, str)
            and re.fullmatch(r"[0-9a-f]{64}", contract_hash)
            else "<invalid>"
        )
        contracts.setdefault(work_unit_id, set()).add(normalized)
    return tuple(
        (work_unit_id, tuple(sorted(hashes)))
        for work_unit_id, hashes in sorted(contracts.items())
    )


def authority_projection_bundle_v1(
    records: Sequence[dict[str, object]],
) -> AuthorityProjectionBundleV1:
    """Evaluate every dispatcher authority projection with stable record IDs."""
    authority_history = _authority_record_list(records)
    governed = current_telemetry(authority_history)
    aliases = sorted(
        {
            str(record.get("effective_alias") or record.get("alias"))
            for record in governed
            if isinstance(record.get("effective_alias") or record.get("alias"), str)
        }
    )

    def digests_for_ids(record_ids: Collection[int]) -> tuple[str, ...]:
        return _stable_record_digests(
            [record for record in governed if id(record) in record_ids]
        )

    coordinator_ids = _authenticated_coordinator_record_ids(authority_history)
    registration_ids = _authenticated_registration_start_ids(authority_history)
    open_before_ids = _authenticated_open_before_record_ids(authority_history)
    review_terminals = authenticated_review_terminals(authority_history)
    accepted = accepted_review_terminals(authority_history)
    advisory = accepted_review_terminals(authority_history, allow_advisory=True)
    verdicts = authenticated_verdicts(authority_history, _accepted_terminals=accepted)
    open_contexts = _retention_open_attempt_contexts(authority_history)
    open_attempt_records = [
        record
        for record in governed
        if record.get("type") == "attempt-start"
        and (
            record.get("task_id"),
            record.get("attempt_index"),
            record.get("run_id"),
            _terminal_authority_work_unit(record),
        )
        in open_contexts
    ]
    open_units_by_worktree: dict[str, dict[str, list[str]]] = {}
    for record in governed:
        worktree = resolved_record_worktree(record.get("worktree"))
        unit_id = str(record.get("work_unit_id") or record.get("task_id") or "")
        if worktree is None or not unit_id:
            continue
        units = open_units_by_worktree.setdefault(str(worktree), {})
        _apply_open_write_record(units, record)
    latest_settlement_indices = _latest_attempt_settlement_indices(governed)
    return AuthorityProjectionBundleV1(
        coordinator=_stable_record_digests(
            [
                record
                for record in governed
                if id(record) in coordinator_ids
                and record.get("type") != "coordinator-authority-cutover"
                and isinstance(record.get("terminal_authority_proof"), dict)
            ]
        ),
        registration_starts=digests_for_ids(registration_ids),
        open_before=digests_for_ids(open_before_ids),
        open_attempts=_stable_record_digests(open_attempt_records),
        supersessions=_stable_record_digests(
            authenticated_supersessions(authority_history)
        ),
        review_terminals=tuple(
            sorted(
                (task_id, canonical_record_digest(record))
                for task_id, record in review_terminals.items()
            )
        ),
        accepted_review_terminals=tuple(
            sorted(
                (task_id, canonical_record_digest(record))
                for task_id, record in accepted.items()
            )
        ),
        advisory_review_terminals=tuple(
            sorted(
                (task_id, canonical_record_digest(record))
                for task_id, record in advisory.items()
            )
        ),
        verdicts=tuple(
            sorted(
                (task_id, canonical_record_digest(record))
                for task_id, record in verdicts.items()
            )
        ),
        delivery_controller=_stable_record_digests(
            delivery_controller_records(authority_history)
        ),
        retry_outcomes=_stable_record_digests(
            authenticated_retry_outcomes(authority_history)
        ),
        work_unit_contracts=_work_unit_contract_projection(governed),
        open_write_units=tuple(
            (
                worktree,
                tuple(
                    sorted((unit_id, tuple(paths)) for unit_id, paths in units.items())
                ),
            )
            for worktree, units in sorted(open_units_by_worktree.items())
            if units
        ),
        alias_availability=tuple(
            (alias, canonical_record_digest(record))
            for alias in aliases
            if (
                record := latest_explicit_alias_availability(
                    authority_history, alias=alias
                )
            )
            is not None
        ),
        attempt_settlements=tuple(
            sorted(
                (
                    str(record["task_id"]),
                    int(
                        record.get("attempt_index")
                        if record.get("attempt_index") is not None
                        else (0 if record.get("type") == "inline" else -1)
                    ),
                    canonical_record_digest(record),
                )
                for index, record in enumerate(governed)
                if index in latest_settlement_indices
            )
        ),
    )


def _prospective_retained_view(
    retained: Sequence[dict[str, object]],
) -> AuthorityRecordView:
    """Model the retained seed after its checkpoint authenticates every row."""
    copied = list(retained)
    return AuthorityRecordView(
        copied,
        trusted_retained_ids={id(record) for record in copied},
    )


def _progress_identity(record: Mapping[str, object]) -> tuple[object, ...]:
    return (
        record.get("task_id"),
        record.get("attempt_index"),
        record.get("run_id"),
        record.get("work_unit_id"),
    )


def retained_authority_projection(
    records: Sequence[dict[str, object]],
    *,
    active_run_ids: Collection[str] = (),
) -> list[dict[str, object]]:
    """Return the deterministic dependency seed for the current authority policy."""
    governed = current_telemetry(records)
    unknown = sorted(
        {
            str(record.get("type"))
            for record in governed
            if record.get("type") not in _COMPACTABLE_AUTHORITY_RECORD_TYPES
        }
    )
    if unknown:
        raise DispatchError(
            "authority compaction does not know current-policy record families: "
            + ", ".join(unknown)
        )
    open_contexts = _retention_open_attempt_contexts(governed)
    active_runs = frozenset(active_run_ids)
    latest_progress: dict[tuple[object, ...], int] = {}
    latest_standing: dict[tuple[object, ...], int] = {}
    latest_contract_anchor: dict[tuple[str, str], int] = {}
    for index, record in enumerate(governed):
        if record.get("type") == "attempt-progress":
            latest_progress[_progress_identity(record)] = index
        if not isinstance(record.get("run_id"), str):
            latest_standing[
                (
                    record.get("type"),
                    record.get("task_id"),
                    record.get("work_unit_id"),
                    record.get("alias"),
                )
            ] = index
        record_type = record.get("type")
        if record_type in {*ATTEMPT_HISTORY_TYPES, "route"} and (
            record_type != "route" or record.get("kept")
        ):
            work_unit_id = str(record.get("work_unit_id") or record.get("task_id"))
            contract_hash = record.get("task_contract_hash")
            normalized = (
                contract_hash
                if isinstance(contract_hash, str)
                and re.fullmatch(r"[0-9a-f]{64}", contract_hash)
                else "<invalid>"
            )
            latest_contract_anchor[(work_unit_id, normalized)] = index
    contract_anchor_indices = frozenset(latest_contract_anchor.values())
    settlement_indices = _latest_attempt_settlement_indices(governed)
    retained: list[dict[str, object]] = []
    for index, record in enumerate(governed):
        record_type = record.get("type")
        if record_type == "coordinator-authority-cutover":
            continue
        context = (
            record.get("task_id"),
            record.get("attempt_index"),
            record.get("run_id"),
            _terminal_authority_work_unit(record),
        )
        belongs_to_live_chain = (
            record.get("run_id") in active_runs or context in open_contexts
        )
        preserves_work_unit_contract = index in contract_anchor_indices
        preserves_attempt_settlement = index in settlement_indices
        standing_key = (
            record_type,
            record.get("task_id"),
            record.get("work_unit_id"),
            record.get("alias"),
        )
        if (
            not belongs_to_live_chain
            and not preserves_work_unit_contract
            and not preserves_attempt_settlement
            and (
                isinstance(record.get("run_id"), str)
                or latest_standing.get(standing_key) != index
            )
        ):
            continue
        if (
            record_type == "attempt-progress"
            and latest_progress.get(_progress_identity(record)) != index
        ):
            continue
        retained.append(record)
    if retained_authority_projection_once(retained) != retained:
        raise DispatchError("authority retained projection is not idempotent")
    return retained


def _retention_open_attempt_contexts(
    records: Sequence[dict[str, object]],
) -> set[tuple[object, ...]]:
    """Project all authenticated open attempts in one linear ledger scan."""
    authenticated_coordinator_ids = _authenticated_coordinator_record_ids(records)
    starts: dict[tuple[Path, tuple[str, int]], dict[str, object]] = {}
    opened: dict[tuple[Path, tuple[str, int]], dict[str, object]] = {}
    for record in records:
        worktree = resolved_record_worktree(record.get("worktree"))
        key = _terminal_authority_attempt_key(record)
        if worktree is None or key is None:
            continue
        context = (worktree, key)
        if record.get("type") == "attempt-start":
            if context not in starts:
                starts[context] = record
                opened[context] = record
            continue
        if record.get("type") not in {"attempt-terminal", "attempt-abort"}:
            continue
        start = starts.get(context)
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
        opened.pop(context, None)
    return {
        (
            start.get("task_id"),
            start.get("attempt_index"),
            start.get("run_id"),
            _terminal_authority_work_unit(start),
        )
        for start in opened.values()
    }


def retained_authority_projection_once(
    records: Sequence[dict[str, object]],
) -> list[dict[str, object]]:
    """Internal single-pass form used to assert planner convergence."""
    latest_progress: dict[tuple[object, ...], int] = {}
    for index, record in enumerate(records):
        if record.get("type") == "attempt-progress":
            latest_progress[_progress_identity(record)] = index
    return [
        record
        for index, record in enumerate(records)
        if record.get("type") != "coordinator-authority-cutover"
        and (
            record.get("type") != "attempt-progress"
            or latest_progress.get(_progress_identity(record)) == index
        )
    ]


_COMPACTABLE_AUTHORITY_RECORD_TYPES = frozenset(
    {
        "alias-availability",
        "attempt-abort",
        "attempt-checkpoint",
        "attempt-owner",
        "attempt-patch-identity-carry",
        "attempt-progress",
        "attempt-recovery",
        "attempt-start",
        "attempt-supersession",
        "attempt-terminal",
        "coordinator-authority-cutover",
        "delivery-control",
        "deposit-verification",
        "evidence-cleanup",
        "inline",
        "review-chain-advisory",
        "review-recovery-verification",
        "route",
        "verdict",
    }
)


@dataclass(frozen=True)
class AuthorityProjectionBundleV1:
    """Canonical, identity-independent output of authority-bearing consumers."""

    coordinator: tuple[str, ...]
    registration_starts: tuple[str, ...]
    open_before: tuple[str, ...]
    open_attempts: tuple[str, ...]
    supersessions: tuple[str, ...]
    review_terminals: tuple[tuple[str, str], ...]
    accepted_review_terminals: tuple[tuple[str, str], ...]
    advisory_review_terminals: tuple[tuple[str, str], ...]
    verdicts: tuple[tuple[str, str], ...]
    delivery_controller: tuple[str, ...]
    retry_outcomes: tuple[str, ...]
    work_unit_contracts: tuple[tuple[str, tuple[str, ...]], ...]
    open_write_units: tuple[tuple[str, tuple[tuple[str, tuple[str, ...]], ...]], ...]
    alias_availability: tuple[tuple[str, str], ...]
    attempt_settlements: tuple[tuple[str, int, str], ...]


AUTHORITY_PROJECTION_REGISTRY_V1 = (
    ("coordinator", "authenticated_coordinator_record_ids"),
    ("registration_starts", "authenticated_registration_starts"),
    ("open_before", "authenticated_open_before"),
    ("open_attempts", "authenticated_open_dispatch_attempts"),
    ("supersessions", "authenticated_supersessions"),
    ("review_terminals", "authenticated_review_terminals"),
    ("accepted_review_terminals", "accepted_review_terminals"),
    ("advisory_review_terminals", "accepted_advisory_review_terminals"),
    ("verdicts", "authenticated_verdicts"),
    ("delivery_controller", "delivery_controller_records"),
    ("retry_outcomes", "authenticated_retry_outcomes"),
    ("work_unit_contracts", "validate_work_unit_contract"),
    ("open_write_units", "open_write_units"),
    ("alias_availability", "latest_explicit_alias_availability"),
    ("attempt_settlements", "winning_attempt_settlements"),
)
AUTHORITY_PROJECTION_CONSUMERS_V1 = tuple(
    consumer for _field, consumer in AUTHORITY_PROJECTION_REGISTRY_V1
)
