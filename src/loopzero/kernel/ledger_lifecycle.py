#!/usr/bin/env python3
"""Compaction, recovery, quarantine, and verification for the authority ledger.

Move-only extraction: this sibling must not import ``agent_dispatch``.
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import re
import stat
import sys
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import fields as dataclass_fields
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import dispatch_ledger as authority_ledger
from dispatch_authority import (
    PROOF_FIELD,
    TerminalAuthorityError,
    commit_coordinator_ledger_state,
    load_coordinator_ledger_state,
    verify_terminal_authority,
)
from dispatch_authority_projection import (
    AuthorityLedgerSnapshot,
    _authenticated_coordinator_record_ids,
)
from dispatch_authority_store import (
    AuthorityProjectionBundleV1,
    _authority_repository_binding,
    _load_archive_manifest,
    _prospective_retained_view,
    active_outer_run_id,
    authority_ledger_lock,
    authority_projection_bundle_v1,
    create_coordinator_authority,
    load_authority_snapshot,
    retained_authority_projection,
)
from dispatch_common import DispatchError, primary_repo_root
from dispatch_routing import (
    AUTHORITY_ARCHIVE_DIRECTORY,
    AUTHORITY_DOWNGRADE_BARRIER_NAME,
    AUTHORITY_LEDGER_DIRECTORY,
    AUTHORITY_LEDGER_MAX_BYTES,
    AUTHORITY_LEDGER_MAX_RECORDS,
    DISPATCH_DIR,
    DISPATCH_POLICY_VERSION,
    LEGACY_AUTHORITY_RECOVERY_DIRECTORY,
    RUNTIME_CONTRACT_VERSION,
    TELEMETRY_SCHEMA_VERSION,
)


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(
        path,
        os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_DIRECTORY", 0),
    )
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _write_synced(path: Path, payload: bytes, *, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0)
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(path, flags, mode)
    try:
        view = memoryview(payload)
        while view:
            written = os.write(descriptor, view)
            if written <= 0:
                raise DispatchError("authority ledger write made no progress")
            view = view[written:]
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    _fsync_directory(path.parent)


def _replace_synced(source: Path, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    source.replace(target)
    _fsync_directory(target.parent)


def _jsonl_payload(records: Sequence[Mapping[str, object]]) -> bytes:
    return b"".join(
        authority_ledger.canonical_json_bytes(dict(record), newline=True)
        for record in records
    )


def _downgrade_barrier_payload(
    checkpoint_record: dict[str, object], transaction_id: str
) -> bytes:
    reserved = {
        "type": "attempt-start",
        "task_id": f"authority-ledger-downgrade-{transaction_id}",
        "work_unit_id": "authority-ledger-downgrade-barrier",
        "attempt_index": 0,
        "run_id": "sr_" + transaction_id,
        "status": "reserved",
        "ownership_required": True,
        "worktree": "/unverifiable/authority-ledger-downgrade-barrier",
        "ts": checkpoint_record["created_at"],
        "schema_version": TELEMETRY_SCHEMA_VERSION,
        "policy_version": DISPATCH_POLICY_VERSION,
        "runtime_contract_version": RUNTIME_CONTRACT_VERSION,
    }
    return _jsonl_payload((checkpoint_record, reserved))


def _copy_exact_authority_segment(
    source: Path,
    target: Path,
    *,
    legacy: bool,
) -> None:
    try:
        payload = (
            authority_ledger.read_legacy_bytes(source)
            if legacy
            else authority_ledger.read_secure_bytes(source)
        )
    except authority_ledger.DispatchLedgerError as exc:
        raise DispatchError(f"cannot freeze authority segment: {exc}") from exc
    _write_synced(target, payload)


def _legacy_authority_stream_paths(repo: Path) -> list[Path]:
    paths: list[Path] = []
    for path in sorted((repo / DISPATCH_DIR).glob("*.jsonl")):
        if path.name == AUTHORITY_DOWNGRADE_BARRIER_NAME:
            continue
        try:
            datetime.strptime(path.stem, "%Y-%m-%d").replace(tzinfo=UTC)
        except ValueError as exc:
            raise DispatchError(
                f"legacy authority directory contains unexpected JSONL stream: "
                f"{path.name}"
            ) from exc
        paths.append(path)
    return paths


def _maybe_crash(point: str, crash_after: str | None) -> None:
    if point == crash_after:
        raise DispatchError(f"injected authority ledger crash after {point}")


def _compact_authority_ledger_unlocked(
    repo: Path,
    *,
    dry_run: bool,
    crash_after: str | None = None,
) -> dict[str, object]:
    transactions = repo / AUTHORITY_LEDGER_DIRECTORY / "transactions"
    barrier = repo / DISPATCH_DIR / AUTHORITY_DOWNGRADE_BARRIER_NAME
    if barrier.exists() and not (repo / AUTHORITY_LEDGER_DIRECTORY).exists():
        raise DispatchError(
            "legacy authority stream collides with the reserved downgrade barrier "
            "filename"
        )
    if any(
        path.is_dir() and _authority_transaction_is_pending(path)
        for path in (transactions.iterdir() if transactions.is_dir() else ())
    ):
        raise DispatchError(
            "authority ledger has a prepared transaction; run "
            "`agent_dispatch.py authority-ledger recover`"
        )
    snapshot = load_authority_snapshot(repo, full_archive_verify=True)
    records = snapshot.records
    active_run = active_outer_run_id(repo, authority_repo=repo)
    retained = retained_authority_projection(
        records,
        active_run_ids=(() if active_run is None else (active_run,)),
    )
    before_projection = authority_projection_bundle_v1(records)
    after_projection = authority_projection_bundle_v1(
        _prospective_retained_view(retained)
    )
    if before_projection != after_projection:
        changed = [
            field.name
            for field in dataclass_fields(AuthorityProjectionBundleV1)
            if getattr(before_projection, field.name)
            != getattr(after_projection, field.name)
        ]
        raise DispatchError(
            "authority compaction would change authority-bearing projections: "
            + ", ".join(changed)
        )
    retained_payload = authority_ledger.canonical_json_bytes(retained)
    if (
        len(retained) + 1 >= AUTHORITY_LEDGER_MAX_RECORDS
        or len(retained_payload) >= AUTHORITY_LEDGER_MAX_BYTES
    ):
        raise DispatchError(
            "authority retained seed reaches the fixed ledger limit; "
            "update the owning projection before appending"
        )
    if snapshot.host_state is None:
        authenticated = _authenticated_coordinator_record_ids(records)
        if not any(
            id(record) in authenticated
            and record.get("type") == "coordinator-authority-cutover"
            for record in records
        ):
            raise DispatchError(
                "authority compaction requires an authenticated coordinator cutover"
            )
        ledger_id = uuid.uuid4().hex
        generation = 1
        predecessor = None
        cumulative = authority_ledger.accumulate_records(
            snapshot.physical_records,
            ledger_id=ledger_id,
            repository_binding=_authority_repository_binding(repo),
        )
        source_paths = _legacy_authority_stream_paths(repo)
    else:
        assert snapshot.accumulator_head is not None
        ledger_id = snapshot.host_state.ledger_id
        generation = snapshot.host_state.generation + 1
        predecessor = snapshot.host_state.checkpoint_digest
        cumulative = snapshot.accumulator_head
        source_paths = [repo / DISPATCH_DIR / snapshot.host_state.active_relative_path]
    summary: dict[str, object] = {
        "ledger_id": ledger_id,
        "generation": generation,
        "archived_records": cumulative.record_count,
        "retained_records": len(retained),
        "dry_run": dry_run,
    }
    if dry_run:
        return summary

    transaction_id = uuid.uuid4().hex
    transaction_root = (
        repo / AUTHORITY_LEDGER_DIRECTORY / "transactions" / transaction_id
    )
    archive_stage = transaction_root / "archive"
    segments_stage = archive_stage / "segments"
    generation_stage = transaction_root / "generation"
    segments_stage.mkdir(parents=True)
    generation_stage.mkdir()
    relative_names: list[str] = []
    for source in source_paths:
        name = source.name
        if name in relative_names:
            raise DispatchError("authority archive source name is duplicated")
        relative_names.append(name)
        _copy_exact_authority_segment(
            source,
            segments_stage / name,
            legacy=snapshot.host_state is None,
        )
    try:
        manifest = authority_ledger.build_archive_manifest(
            segments_stage,
            relative_names=relative_names,
            ledger_id=ledger_id,
            generation=generation - 1,
        )
    except authority_ledger.DispatchLedgerError as exc:
        raise DispatchError(f"cannot build authority archive: {exc}") from exc
    manifest_payload = authority_ledger.canonical_json_bytes(
        manifest.to_dict(), newline=True
    )
    _write_synced(archive_stage / "manifest.json", manifest_payload)
    checkpoint = authority_ledger.AuthorityLedgerCheckpointV1.create(
        ledger_id=ledger_id,
        repository_binding=_authority_repository_binding(repo),
        generation=generation,
        predecessor_checkpoint_digest=predecessor,
        cumulative=cumulative,
        archived_record_count=cumulative.record_count,
        archive_manifest_sha256=manifest.logical_digest(),
        retained_records=retained,
        transaction_id=transaction_id,
        created_at=datetime.now(UTC).isoformat(),
    )
    try:
        checkpoint_record = create_coordinator_authority().seal(
            checkpoint.to_record(),
            authority_kind="coordinator",
            include_public_key=True,
        )
    except TerminalAuthorityError as exc:
        raise DispatchError(f"cannot sign authority checkpoint: {exc}") from exc
    checkpoint_digest = authority_ledger.canonical_record_digest(checkpoint_record)
    active_payload = _jsonl_payload((checkpoint_record,))
    published_head = authority_ledger.accumulate_records(
        (checkpoint_record,),
        ledger_id=ledger_id,
        repository_binding=checkpoint.repository_binding,
        seed=cumulative,
    )
    _write_synced(generation_stage / "active.jsonl", active_payload)
    barrier_payload = _downgrade_barrier_payload(checkpoint_record, transaction_id)
    _write_synced(transaction_root / "barrier.jsonl", barrier_payload)
    next_state = authority_ledger.AuthorityLedgerHostStateV1(
        repository_binding=checkpoint.repository_binding,
        ledger_id=ledger_id,
        generation=generation,
        checkpoint_digest=checkpoint_digest,
        predecessor_checkpoint_digest=predecessor,
        active_relative_path=(f"ledger/generations/{generation:08d}/active.jsonl"),
        archive_manifest_relative_path=(
            f"archives/{ledger_id}/generation-{generation - 1:08d}/manifest.json"
        ),
        archive_manifest_sha256=manifest.logical_digest(),
        archive_stat_seals=tuple(
            segment.stat_seal.to_dict() for segment in manifest.segments
        ),
        downgrade_barrier_sha256=hashlib.sha256(barrier_payload).hexdigest(),
        active_record_count=published_head.record_count,
        active_records_sha256=published_head.records_sha256,
        active_byte_size=len(active_payload),
    )
    prepare = {
        "scheme": "dispatch-authority-ledger-prepare-v1",
        "transaction_id": transaction_id,
        "previous_generation": generation - 1 if generation > 1 else None,
        "previous_state": (
            snapshot.host_state.to_dict() if snapshot.host_state is not None else None
        ),
        "next_state": next_state.to_dict(),
        "source_relative_paths": [str(path.relative_to(repo)) for path in source_paths],
        "archive_payload_sha256": hashlib.sha256(manifest_payload).hexdigest(),
        "active_payload_sha256": hashlib.sha256(active_payload).hexdigest(),
        "barrier_payload_sha256": hashlib.sha256(barrier_payload).hexdigest(),
    }
    try:
        signed_prepare = create_coordinator_authority().seal(
            prepare,
            authority_kind="coordinator",
            include_public_key=True,
        )
    except TerminalAuthorityError as exc:
        raise DispatchError(f"cannot sign authority ledger prepare: {exc}") from exc
    _write_synced(
        transaction_root / "prepare.json",
        authority_ledger.canonical_json_bytes(signed_prepare, newline=True),
    )
    _fsync_directory(transaction_root)
    _maybe_crash("prepare", crash_after)

    barrier_target = repo / DISPATCH_DIR / AUTHORITY_DOWNGRADE_BARRIER_NAME
    _replace_synced(transaction_root / "barrier.jsonl", barrier_target)
    _maybe_crash("barrier", crash_after)
    archive_target = (
        repo
        / AUTHORITY_ARCHIVE_DIRECTORY
        / ledger_id
        / f"generation-{generation - 1:08d}"
    )
    if archive_target.exists():
        raise DispatchError("authority archive generation already exists")
    archive_target.parent.mkdir(parents=True, exist_ok=True)
    _replace_synced(archive_stage, archive_target)
    _maybe_crash("archive", crash_after)
    generation_target = (
        repo / AUTHORITY_LEDGER_DIRECTORY / "generations" / f"{generation:08d}"
    )
    if generation_target.exists():
        raise DispatchError("authority active generation already exists")
    generation_target.parent.mkdir(parents=True, exist_ok=True)
    _replace_synced(generation_stage, generation_target)
    _maybe_crash("generation", crash_after)
    try:
        commit_coordinator_ledger_state(
            next_state.repository_binding,
            next_state.to_dict(),
            expected_generation=(generation - 1 if generation > 1 else None),
        )
    except TerminalAuthorityError as exc:
        raise DispatchError(f"cannot advance authority protected head: {exc}") from exc
    _maybe_crash("protected-state", crash_after)
    _finish_authority_compaction_cleanup(repo, prepare)
    _maybe_crash("cleanup", crash_after)
    summary["checkpoint_digest"] = checkpoint_digest
    return summary


def _finish_authority_compaction_cleanup(
    repo: Path, prepare: Mapping[str, object]
) -> None:
    raw_sources = prepare.get("source_relative_paths")
    if not isinstance(raw_sources, list) or not all(
        isinstance(item, str) for item in raw_sources
    ):
        raise DispatchError("authority ledger prepare sources are invalid")
    raw_next = prepare.get("next_state")
    if not isinstance(raw_next, Mapping):
        raise DispatchError("authority ledger prepare state is invalid")
    try:
        state = authority_ledger.AuthorityLedgerHostStateV1.from_mapping(raw_next)
        manifest_path = repo / DISPATCH_DIR / state.archive_manifest_relative_path
        raw_manifest = json.loads(authority_ledger.read_secure_bytes(manifest_path))
        if not isinstance(raw_manifest, Mapping):
            raise authority_ledger.DispatchLedgerError(
                "archive manifest fields are invalid"
            )
        manifest = authority_ledger.ArchiveManifestV1.from_mapping(raw_manifest)
    except (
        authority_ledger.DispatchLedgerError,
        json.JSONDecodeError,
        UnicodeDecodeError,
    ) as exc:
        raise DispatchError(f"authority cleanup manifest is invalid: {exc}") from exc
    expected_sources = (
        [str(DISPATCH_DIR / segment.relative_name) for segment in manifest.segments]
        if state.generation == 1
        else [
            str(
                DISPATCH_DIR
                / "ledger"
                / "generations"
                / f"{state.generation - 1:08d}"
                / "active.jsonl"
            )
        ]
    )
    if raw_sources != expected_sources:
        raise DispatchError("authority ledger prepare sources diverge from archive")
    source_directories: set[Path] = set()
    for relative in expected_sources:
        source = repo / relative
        source.unlink(missing_ok=True)
        source_directories.add(source.parent)
    for source_directory in sorted(source_directories):
        _fsync_directory(source_directory)
    transaction_id = prepare.get("transaction_id")
    if not isinstance(transaction_id, str):
        raise DispatchError("authority ledger prepare transaction is invalid")
    transaction_root = (
        repo / AUTHORITY_LEDGER_DIRECTORY / "transactions" / transaction_id
    )
    committed = transaction_root / "committed.json"
    if not committed.exists():
        marker = {
            "scheme": "dispatch-authority-ledger-committed-v1",
            "transaction_id": transaction_id,
            "checkpoint_digest": state.checkpoint_digest,
        }
        try:
            signed_marker = create_coordinator_authority().seal(
                marker,
                authority_kind="coordinator",
                include_public_key=True,
            )
        except TerminalAuthorityError as exc:
            raise DispatchError(
                f"cannot sign authority ledger commit marker: {exc}"
            ) from exc
        _write_synced(
            committed,
            authority_ledger.canonical_json_bytes(signed_marker, newline=True),
        )


def compact_authority_ledger(
    repo: Path,
    *,
    dry_run: bool = False,
    _crash_after: str | None = None,
) -> dict[str, object]:
    with authority_ledger_lock(repo):
        return _compact_authority_ledger_unlocked(
            repo, dry_run=dry_run, crash_after=_crash_after
        )


def _parse_prepare(payload: bytes) -> dict[str, object]:
    try:
        raw = json.loads(payload)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise DispatchError("authority ledger prepare is invalid") from exc
    expected = {
        "scheme",
        "transaction_id",
        "previous_generation",
        "previous_state",
        "next_state",
        "source_relative_paths",
        "archive_payload_sha256",
        "active_payload_sha256",
        "barrier_payload_sha256",
        PROOF_FIELD,
    }
    if (
        not isinstance(raw, dict)
        or set(raw) != expected
        or raw.get("scheme") != "dispatch-authority-ledger-prepare-v1"
    ):
        raise DispatchError("authority ledger prepare is invalid")
    try:
        verify_terminal_authority(
            raw,
            registration=None,
            expected_kind="coordinator",
        )
    except TerminalAuthorityError as exc:
        raise DispatchError(
            f"authority ledger prepare is unauthenticated: {exc}"
        ) from exc
    return raw


def _read_prepare(path: Path) -> dict[str, object]:
    try:
        payload = authority_ledger.read_secure_bytes(path)
    except authority_ledger.DispatchLedgerError as exc:
        raise DispatchError(f"authority ledger prepare is unsafe: {exc}") from exc
    return _parse_prepare(payload)


def _parse_committed(payload: bytes) -> dict[str, object]:
    try:
        raw = json.loads(payload)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise DispatchError("authority ledger commit marker is invalid") from exc
    expected = {
        "scheme",
        "transaction_id",
        "checkpoint_digest",
        PROOF_FIELD,
    }
    if (
        not isinstance(raw, dict)
        or set(raw) != expected
        or raw.get("scheme") != "dispatch-authority-ledger-committed-v1"
    ):
        raise DispatchError("authority ledger commit marker is invalid")
    try:
        verify_terminal_authority(
            raw,
            registration=None,
            expected_kind="coordinator",
        )
    except TerminalAuthorityError as exc:
        raise DispatchError(
            f"authority ledger commit marker is unauthenticated: {exc}"
        ) from exc
    return raw


def _read_committed(path: Path) -> dict[str, object]:
    try:
        payload = authority_ledger.read_secure_bytes(path)
    except authority_ledger.DispatchLedgerError as exc:
        raise DispatchError(f"authority ledger commit marker is unsafe: {exc}") from exc
    return _parse_committed(payload)


def _open_authority_transaction_root(transaction_root: Path) -> int | None:
    directory_flags = (
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    try:
        parent_descriptor = os.open(transaction_root.parent, directory_flags)
    except OSError as exc:
        raise DispatchError("authority transaction parent is unsafe") from exc
    try:
        try:
            initial = os.stat(
                transaction_root.name,
                dir_fd=parent_descriptor,
                follow_symlinks=False,
            )
        except FileNotFoundError:
            return None
        except OSError as exc:
            raise DispatchError("authority transaction directory is unsafe") from exc
        if not stat.S_ISDIR(initial.st_mode):
            return None
        try:
            descriptor = os.open(
                transaction_root.name,
                directory_flags,
                dir_fd=parent_descriptor,
            )
        except OSError as exc:
            try:
                current = os.stat(
                    transaction_root.name,
                    dir_fd=parent_descriptor,
                    follow_symlinks=False,
                )
            except FileNotFoundError:
                return None
            except OSError as validation_exc:
                raise DispatchError(
                    "authority transaction directory is unsafe"
                ) from validation_exc
            if not stat.S_ISDIR(current.st_mode):
                return None
            raise DispatchError("authority transaction directory is unsafe") from exc
        opened = os.fstat(descriptor)
        if (
            (opened.st_dev, opened.st_ino) != (initial.st_dev, initial.st_ino)
            or not stat.S_ISDIR(opened.st_mode)
            or opened.st_uid != os.getuid()
            or opened.st_mode & 0o002
        ):
            os.close(descriptor)
            raise DispatchError("authority transaction directory is unsafe")
        return descriptor
    finally:
        os.close(parent_descriptor)


def _read_authority_transaction_file_at(
    directory_descriptor: int, name: str
) -> bytes | None:
    try:
        initial = os.stat(
            name,
            dir_fd=directory_descriptor,
            follow_symlinks=False,
        )
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise DispatchError("authority transaction file is unsafe") from exc
    if (
        not stat.S_ISREG(initial.st_mode)
        or initial.st_uid != os.getuid()
        or initial.st_mode & 0o022
    ):
        raise DispatchError("authority transaction file is unsafe")
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(name, flags, dir_fd=directory_descriptor)
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise DispatchError("authority transaction file is unsafe") from exc
    try:
        fcntl.flock(descriptor, fcntl.LOCK_SH)
        before = os.fstat(descriptor)
        if (
            (before.st_dev, before.st_ino) != (initial.st_dev, initial.st_ino)
            or not stat.S_ISREG(before.st_mode)
            or before.st_uid != os.getuid()
            or before.st_mode & 0o022
            or before.st_size > 1024 * 1024
        ):
            raise DispatchError("authority transaction file is unsafe")
        payload = bytearray()
        while len(payload) <= 1024 * 1024:
            chunk = os.read(
                descriptor,
                min(1024 * 1024, 1024 * 1024 + 1 - len(payload)),
            )
            if not chunk:
                break
            payload.extend(chunk)
        after = os.fstat(descriptor)
    except OSError as exc:
        raise DispatchError("authority transaction file is unsafe") from exc
    finally:
        os.close(descriptor)
    stable_fields = (
        "st_dev",
        "st_ino",
        "st_mode",
        "st_uid",
        "st_size",
        "st_mtime_ns",
        "st_ctime_ns",
    )
    if (
        len(payload) > 1024 * 1024
        or len(payload) != before.st_size
        or any(
            getattr(before, field) != getattr(after, field) for field in stable_fields
        )
    ):
        raise DispatchError("authority transaction file changed while reading")
    return bytes(payload)


def _authority_transaction_is_pending(transaction_root: Path) -> bool:
    if re.fullmatch(r"[0-9a-f]{32}", transaction_root.name) is None:
        return False
    try:
        directory_descriptor = _open_authority_transaction_root(transaction_root)
    except DispatchError:
        return True
    if directory_descriptor is None:
        return False
    try:
        try:
            prepare_payload = _read_authority_transaction_file_at(
                directory_descriptor, "prepare.json"
            )
            if prepare_payload is None:
                return False
            committed_payload = _read_authority_transaction_file_at(
                directory_descriptor, "committed.json"
            )
            if committed_payload is None:
                return True
            prepare = _parse_prepare(prepare_payload)
            committed = _parse_committed(committed_payload)
        except DispatchError:
            return True
    finally:
        os.close(directory_descriptor)
    raw_next = prepare.get("next_state")
    return (
        not isinstance(raw_next, Mapping)
        or committed.get("transaction_id") != prepare.get("transaction_id")
        or committed.get("transaction_id") != transaction_root.name
        or committed.get("checkpoint_digest") != raw_next.get("checkpoint_digest")
    )


def quarantine_invalid_authority_transaction(
    repo: Path, transaction_id: str
) -> dict[str, object]:
    """Move one unauthenticated prepare aside without destroying its evidence."""
    if re.fullmatch(r"[0-9a-f]{32}", transaction_id) is None:
        raise DispatchError("authority transaction id must be 32 lowercase hex digits")
    with authority_ledger_lock(repo):
        transactions = repo / AUTHORITY_LEDGER_DIRECTORY / "transactions"
        transaction_root = transactions / transaction_id
        if transaction_root.is_symlink() or not transaction_root.is_dir():
            raise DispatchError("authority transaction is unavailable")
        transaction_metadata = transaction_root.stat()
        if (
            transaction_metadata.st_uid != os.getuid()
            or transaction_metadata.st_mode & 0o002
        ):
            raise DispatchError("authority transaction directory is unsafe")
        prepare_path = transaction_root / "prepare.json"
        if not prepare_path.is_file():
            raise DispatchError("authority transaction has no prepare journal")
        try:
            _read_prepare(prepare_path)
        except DispatchError:
            pass
        else:
            raise DispatchError(
                "authority transaction prepare is authenticated; run recover"
            )
        quarantine = transactions / "quarantine"
        quarantine.mkdir(mode=0o700, exist_ok=True)
        if quarantine.is_symlink() or not quarantine.is_dir():
            raise DispatchError("authority transaction quarantine is unsafe")
        quarantine_metadata = quarantine.stat()
        if (
            quarantine_metadata.st_uid != os.getuid()
            or quarantine_metadata.st_mode & 0o077
        ):
            raise DispatchError("authority transaction quarantine is unsafe")
        target = quarantine / transaction_id
        if target.exists() or target.is_symlink():
            raise DispatchError("authority transaction is already quarantined")
        _replace_synced(transaction_root, target)
        _fsync_directory(transactions)
        return {"quarantined": True, "transaction_id": transaction_id}


def quarantine_legacy_authority_line(
    repo: Path,
    *,
    file_name: str,
    line_number: int,
) -> dict[str, object]:
    """Preserve and remove one malformed pre-checkpoint authority line."""
    try:
        datetime.strptime(file_name, "%Y-%m-%d.jsonl").replace(tzinfo=UTC)
    except ValueError as exc:
        raise DispatchError(
            "legacy authority recovery requires a dated JSONL file name"
        ) from exc
    if file_name == AUTHORITY_DOWNGRADE_BARRIER_NAME:
        raise DispatchError("the authority downgrade barrier is not a legacy stream")
    if line_number < 1:
        raise DispatchError("legacy authority recovery line must be positive")

    with authority_ledger_lock(repo):
        ledger_directory = repo / AUTHORITY_LEDGER_DIRECTORY
        if ledger_directory.exists():
            raise DispatchError(
                "legacy authority recovery is unavailable after checkpoint compaction"
            )
        try:
            binding = _authority_repository_binding(repo)
        except DispatchError:
            binding = None
        if binding is not None:
            try:
                protected_state = load_coordinator_ledger_state(binding)
            except TerminalAuthorityError as exc:
                raise DispatchError(
                    f"authority protected head is invalid: {exc}"
                ) from exc
            if protected_state is not None:
                raise DispatchError(
                    "legacy authority recovery is unavailable with a protected head"
                )

        source = repo / DISPATCH_DIR / file_name
        try:
            original = authority_ledger.read_legacy_bytes(source)
        except authority_ledger.DispatchLedgerError as exc:
            raise DispatchError(f"cannot read legacy authority stream: {exc}") from exc
        lines = original.splitlines(keepends=True)
        if line_number > len(lines):
            raise DispatchError("legacy authority recovery line is unavailable")
        invalid_line = lines[line_number - 1]
        if not invalid_line.strip():
            raise DispatchError("blank authority lines do not require recovery")
        try:
            parsed = json.loads(invalid_line)
        except (json.JSONDecodeError, UnicodeDecodeError):
            parsed = None
        if isinstance(parsed, dict):
            raise DispatchError("refusing to quarantine a valid authority record")

        repaired = b"".join(
            line for index, line in enumerate(lines, start=1) if index != line_number
        )
        try:
            authority_ledger.parse_jsonl_records(
                repaired,
                label=f"repaired {file_name}",
            )
        except authority_ledger.DispatchLedgerError as exc:
            raise DispatchError(
                "legacy authority recovery requires the selected line to be the "
                f"only malformed record: {exc}"
            ) from exc

        original_digest = hashlib.sha256(original).hexdigest()
        invalid_digest = hashlib.sha256(invalid_line).hexdigest()
        repaired_digest = hashlib.sha256(repaired).hexdigest()
        recovery_parent = repo / LEGACY_AUTHORITY_RECOVERY_DIRECTORY
        recovery_parent.mkdir(parents=True, mode=0o700, exist_ok=True)
        recovery_metadata = recovery_parent.stat()
        if (
            recovery_parent.is_symlink()
            or recovery_metadata.st_uid != os.getuid()
            or recovery_metadata.st_mode & 0o077
        ):
            raise DispatchError("legacy authority recovery directory is unsafe")
        recovery = recovery_parent / (
            f"{source.stem}-line-{line_number}-{original_digest[:16]}"
        )
        try:
            recovery.mkdir(mode=0o700)
        except FileExistsError:
            try:
                staged_metadata = recovery.lstat()
            except OSError as exc:
                raise DispatchError(
                    "legacy authority recovery stage is unsafe"
                ) from exc
            if (
                recovery.is_symlink()
                or not stat.S_ISDIR(staged_metadata.st_mode)
                or staged_metadata.st_uid != os.getuid()
                or staged_metadata.st_mode & 0o077
            ):
                raise DispatchError("legacy authority recovery stage is unsafe")
            incomplete = recovery.with_name(
                f"{recovery.name}-incomplete-{uuid.uuid4().hex[:16]}"
            )
            if incomplete.exists() or incomplete.is_symlink():
                raise DispatchError(
                    "legacy authority recovery archive identity collided"
                )
            _replace_synced(recovery, incomplete)
            recovery.mkdir(mode=0o700)
        _fsync_directory(recovery_parent)
        _write_synced(recovery / "original.jsonl", original)
        _write_synced(recovery / "invalid-line.bin", invalid_line)
        _write_synced(recovery / "repaired.jsonl", repaired)
        manifest = {
            "schema_version": "legacy-authority-line-recovery-v1",
            "source_file": file_name,
            "line_number": line_number,
            "original_sha256": original_digest,
            "invalid_line_sha256": invalid_digest,
            "repaired_sha256": repaired_digest,
            "prepared_at": datetime.now(UTC).isoformat(),
        }
        _write_synced(
            recovery / "manifest.json",
            authority_ledger.canonical_json_bytes(manifest, newline=True),
        )

        temporary = source.with_name(f".{source.name}.repair-{original_digest[:16]}")
        replacement = temporary
        try:
            _write_synced(temporary, repaired)
        except FileExistsError:
            try:
                temporary_metadata = temporary.lstat()
                if (
                    temporary.is_symlink()
                    or not stat.S_ISREG(temporary_metadata.st_mode)
                    or temporary_metadata.st_uid != os.getuid()
                    or temporary_metadata.st_mode & 0o077
                ):
                    raise DispatchError("legacy authority repair temporary is unsafe")
                interrupted = recovery / "interrupted-repair.bin"
                if interrupted.exists() or interrupted.is_symlink():
                    raise DispatchError(
                        "legacy authority repair evidence identity collided"
                    )
                _replace_synced(temporary, interrupted)
                _fsync_directory(temporary.parent)
                interrupted_payload = authority_ledger.read_secure_bytes(
                    interrupted,
                    max_bytes=len(repaired) + 1,
                )
            except (OSError, authority_ledger.DispatchLedgerError) as exc:
                raise DispatchError(
                    "legacy authority repair temporary is unsafe"
                ) from exc
            if interrupted_payload == repaired:
                replacement = interrupted
            else:
                _write_synced(temporary, repaired)
        _replace_synced(replacement, source)
        if replacement.parent != source.parent:
            _fsync_directory(replacement.parent)
        _write_synced(
            recovery / "committed.json",
            authority_ledger.canonical_json_bytes(
                {
                    "schema_version": "legacy-authority-line-recovery-commit-v1",
                    "repaired_sha256": repaired_digest,
                    "committed_at": datetime.now(UTC).isoformat(),
                },
                newline=True,
            ),
        )
        return {
            "quarantined": True,
            "source_file": file_name,
            "line_number": line_number,
            "recovery_path": str(recovery.relative_to(repo)),
        }


def _prepared_digest(prepare: Mapping[str, object], field: str) -> str:
    value = prepare.get(field)
    if not isinstance(value, str) or re.fullmatch(r"[0-9a-f]{64}", value) is None:
        raise DispatchError("authority ledger prepare digest is invalid")
    return value


def _publish_prepared_file(
    source: Path,
    target: Path,
    *,
    expected_sha256: str,
    replace_sha256: str | None = None,
) -> None:
    if target.exists():
        try:
            actual = hashlib.sha256(
                authority_ledger.read_secure_bytes(target)
            ).hexdigest()
        except authority_ledger.DispatchLedgerError as exc:
            raise DispatchError(f"prepared authority file is unsafe: {exc}") from exc
        if actual == expected_sha256:
            return
        if replace_sha256 is None or actual != replace_sha256:
            raise DispatchError("published authority file diverges from prepare")
    if not source.exists():
        raise DispatchError("prepared authority file is missing")
    try:
        actual = hashlib.sha256(authority_ledger.read_secure_bytes(source)).hexdigest()
    except authority_ledger.DispatchLedgerError as exc:
        raise DispatchError(f"prepared authority file is unsafe: {exc}") from exc
    if actual != expected_sha256:
        raise DispatchError("prepared authority file diverges from prepare")
    _replace_synced(source, target)


def _publish_prepared_directory(source: Path, target: Path) -> None:
    if target.exists():
        return
    if not source.is_dir():
        raise DispatchError("prepared authority directory is missing")
    target.parent.mkdir(parents=True, exist_ok=True)
    _replace_synced(source, target)


def _recover_uncommitted_active_tail(repo: Path) -> bool:
    binding = _authority_repository_binding(repo)
    try:
        raw_state = load_coordinator_ledger_state(binding)
        if raw_state is None:
            return False
        state = authority_ledger.AuthorityLedgerHostStateV1.from_mapping(raw_state)
        active = repo / DISPATCH_DIR / state.active_relative_path
        payload = authority_ledger.read_secure_bytes(active)
        if len(payload) <= state.active_byte_size:
            return False
        protected_payload = payload[: state.active_byte_size]
        if not protected_payload.endswith(b"\n"):
            raise authority_ledger.DispatchLedgerError(
                "protected active boundary is not a complete record"
            )
        physical = authority_ledger.parse_jsonl_records(
            protected_payload, label=str(active)
        )
        if not physical:
            raise authority_ledger.DispatchLedgerError(
                "protected active generation is empty"
            )
        checkpoint = authority_ledger.AuthorityLedgerCheckpointV1.from_record(
            physical[0]
        )
        verify_terminal_authority(
            physical[0], registration=None, expected_kind="coordinator"
        )
        head = authority_ledger.accumulate_records(
            physical,
            ledger_id=state.ledger_id,
            repository_binding=state.repository_binding,
            seed=checkpoint.cumulative,
        )
        if (
            head.record_count != state.active_record_count
            or head.records_sha256 != state.active_records_sha256
        ):
            raise authority_ledger.DispatchLedgerError(
                "protected active prefix does not match host head"
            )
    except (authority_ledger.DispatchLedgerError, TerminalAuthorityError) as exc:
        raise DispatchError(f"authority active-tail recovery failed: {exc}") from exc
    temporary = active.with_name(f".{active.name}.{uuid.uuid4().hex}.recover")
    _write_synced(temporary, protected_payload)
    _replace_synced(temporary, active)
    return True


def _quarantine_unprepared_first_compaction(repo: Path) -> str | None:
    """Preserve an unsigned first-compaction stage so legacy writes can resume."""
    ledger = repo / AUTHORITY_LEDGER_DIRECTORY
    if not ledger.exists():
        return None
    if ledger.is_symlink() or not ledger.is_dir():
        return None
    try:
        binding = _authority_repository_binding(repo)
        protected_state = load_coordinator_ledger_state(binding)
    except TerminalAuthorityError as exc:
        raise DispatchError(f"authority protected head is invalid: {exc}") from exc
    if protected_state is not None:
        return None
    entries = list(ledger.iterdir())
    transactions = ledger / "transactions"
    if any(path != transactions for path in entries):
        return None
    if transactions.exists():
        if transactions.is_symlink() or not transactions.is_dir():
            return None
        for transaction in transactions.iterdir():
            if transaction.name == "quarantine":
                if transaction.is_symlink() or not transaction.is_dir():
                    return None
                quarantine_metadata = transaction.stat()
                if (
                    quarantine_metadata.st_uid != os.getuid()
                    or quarantine_metadata.st_mode & 0o077
                ):
                    return None
                continue
            if (
                transaction.is_symlink()
                or not transaction.is_dir()
                or re.fullmatch(r"[0-9a-f]{32}", transaction.name) is None
                or (transaction / "prepare.json").exists()
                or (transaction / "prepare.json").is_symlink()
            ):
                return None
    quarantine = ledger.with_name(f".{ledger.name}.unprepared-{uuid.uuid4().hex}")
    _replace_synced(ledger, quarantine)
    return str(quarantine.relative_to(repo))


def _recover_authority_ledger_unlocked(repo: Path) -> dict[str, object]:
    quarantined = _quarantine_unprepared_first_compaction(repo)
    if quarantined is not None:
        snapshot = load_authority_snapshot(repo, full_archive_verify=True)
        return {
            "recovered": True,
            "generation": None,
            "quarantined_unprepared_staging": quarantined,
        }
    transactions = repo / AUTHORITY_LEDGER_DIRECTORY / "transactions"
    pending = [
        path
        for path in sorted(transactions.iterdir() if transactions.is_dir() else ())
        if path.is_dir() and _authority_transaction_is_pending(path)
    ]
    if not pending:
        repaired = _recover_uncommitted_active_tail(repo)
        snapshot = load_authority_snapshot(repo, full_archive_verify=True)
        return {
            "recovered": repaired,
            "generation": (
                snapshot.host_state.generation if snapshot.host_state else None
            ),
        }
    if len(pending) != 1:
        raise DispatchError("authority ledger recovery found multiple prepared forks")
    transaction_root = pending[0]
    if (transaction_root / "committed.json").exists():
        _read_committed(transaction_root / "committed.json")
        raise DispatchError("authority ledger commit marker diverges from prepare")
    prepare = _read_prepare(transaction_root / "prepare.json")
    transaction_id = prepare.get("transaction_id")
    if transaction_id != transaction_root.name:
        raise DispatchError("authority ledger prepare transaction does not match")
    raw_next = prepare.get("next_state")
    if not isinstance(raw_next, Mapping):
        raise DispatchError("authority ledger prepare state is invalid")
    try:
        next_state = authority_ledger.AuthorityLedgerHostStateV1.from_mapping(raw_next)
    except authority_ledger.DispatchLedgerError as exc:
        raise DispatchError(
            f"authority ledger prepare state is invalid: {exc}"
        ) from exc
    binding = _authority_repository_binding(repo)
    if next_state.repository_binding != binding:
        raise DispatchError("authority ledger prepare repository does not match")
    previous = prepare.get("previous_generation")
    expected_previous = next_state.generation - 1 if next_state.generation > 1 else None
    if previous != expected_previous:
        raise DispatchError("authority ledger prepare generation is invalid")
    raw_previous = prepare.get("previous_state")
    if expected_previous is None:
        if raw_previous is not None:
            raise DispatchError("authority ledger prepare predecessor is invalid")
    else:
        if not isinstance(raw_previous, Mapping):
            raise DispatchError("authority ledger prepare predecessor is invalid")
        try:
            previous_state = authority_ledger.AuthorityLedgerHostStateV1.from_mapping(
                raw_previous
            )
        except authority_ledger.DispatchLedgerError as exc:
            raise DispatchError(
                f"authority ledger prepare predecessor is invalid: {exc}"
            ) from exc
        if (
            previous_state.repository_binding != binding
            or previous_state.ledger_id != next_state.ledger_id
            or previous_state.generation != expected_previous
            or previous_state.checkpoint_digest
            != next_state.predecessor_checkpoint_digest
        ):
            raise DispatchError("authority ledger prepare predecessor is invalid")
    try:
        raw_current = load_coordinator_ledger_state(binding)
    except TerminalAuthorityError as exc:
        raise DispatchError(f"authority protected head is invalid: {exc}") from exc
    current_generation = raw_current.get("generation") if raw_current else None
    if current_generation not in {expected_previous, next_state.generation}:
        raise DispatchError("authority ledger recovery cannot roll back or skip")
    if current_generation == expected_previous and raw_current != raw_previous:
        raise DispatchError("authority ledger recovery found a divergent predecessor")
    if (
        current_generation == next_state.generation
        and raw_current != next_state.to_dict()
    ):
        raise DispatchError(
            "authority ledger recovery found a divergent protected head"
        )

    barrier_target = repo / DISPATCH_DIR / AUTHORITY_DOWNGRADE_BARRIER_NAME
    replace_barrier_sha256 = None
    if current_generation == expected_previous and raw_current is not None:
        candidate = raw_current.get("downgrade_barrier_sha256")
        if not isinstance(candidate, str):
            raise DispatchError("authority protected head barrier digest is invalid")
        replace_barrier_sha256 = candidate
    _publish_prepared_file(
        transaction_root / "barrier.jsonl",
        barrier_target,
        expected_sha256=_prepared_digest(prepare, "barrier_payload_sha256"),
        replace_sha256=replace_barrier_sha256,
    )
    archive_target = (
        repo
        / AUTHORITY_ARCHIVE_DIRECTORY
        / next_state.ledger_id
        / f"generation-{next_state.generation - 1:08d}"
    )
    _publish_prepared_directory(transaction_root / "archive", archive_target)
    generation_target = (
        repo
        / AUTHORITY_LEDGER_DIRECTORY
        / "generations"
        / f"{next_state.generation:08d}"
    )
    _publish_prepared_directory(transaction_root / "generation", generation_target)
    active_path = repo / DISPATCH_DIR / next_state.active_relative_path
    try:
        active_payload = authority_ledger.read_secure_bytes(active_path)
        manifest_payload = authority_ledger.read_secure_bytes(
            repo / DISPATCH_DIR / next_state.archive_manifest_relative_path
        )
    except authority_ledger.DispatchLedgerError as exc:
        raise DispatchError(
            f"prepared authority publication is invalid: {exc}"
        ) from exc
    if hashlib.sha256(active_payload).hexdigest() != _prepared_digest(
        prepare, "active_payload_sha256"
    ) or hashlib.sha256(manifest_payload).hexdigest() != _prepared_digest(
        prepare, "archive_payload_sha256"
    ):
        raise DispatchError("prepared authority publication diverges from journal")
    physical = authority_ledger.parse_jsonl_records(
        active_payload, label=str(active_path)
    )
    if not physical:
        raise DispatchError("prepared authority generation is empty")
    try:
        checkpoint = authority_ledger.AuthorityLedgerCheckpointV1.from_record(
            physical[0]
        )
        verify_terminal_authority(
            physical[0], registration=None, expected_kind="coordinator"
        )
    except (authority_ledger.DispatchLedgerError, TerminalAuthorityError) as exc:
        raise DispatchError(f"prepared authority checkpoint is invalid: {exc}") from exc
    if (
        checkpoint.generation != next_state.generation
        or authority_ledger.canonical_record_digest(physical[0])
        != next_state.checkpoint_digest
    ):
        raise DispatchError(
            "prepared authority checkpoint diverges from protected head"
        )
    _load_archive_manifest(repo, next_state, checkpoint, full=True)
    if current_generation != next_state.generation:
        try:
            commit_coordinator_ledger_state(
                binding,
                next_state.to_dict(),
                expected_generation=expected_previous,
            )
        except TerminalAuthorityError as exc:
            raise DispatchError(
                f"cannot recover authority protected head: {exc}"
            ) from exc
    _finish_authority_compaction_cleanup(repo, prepare)
    load_authority_snapshot(repo, full_archive_verify=True)
    return {"recovered": True, "generation": next_state.generation}


def recover_authority_ledger(repo: Path) -> dict[str, object]:
    with authority_ledger_lock(repo):
        return _recover_authority_ledger_unlocked(repo)


def reseal_authority_ledger_archive(repo: Path) -> dict[str, object]:
    """Reseal byte-identical archive stats without changing the signed head."""
    with authority_ledger_lock(repo):
        binding = _authority_repository_binding(repo)
        try:
            raw_state = load_coordinator_ledger_state(binding)
            if raw_state is None:
                raise DispatchError("authority ledger is not compacted")
            state = authority_ledger.AuthorityLedgerHostStateV1.from_mapping(raw_state)
            active = authority_ledger.read_jsonl_records(
                repo / DISPATCH_DIR / state.active_relative_path
            )
            if not active:
                raise authority_ledger.DispatchLedgerError("active generation is empty")
            checkpoint = authority_ledger.AuthorityLedgerCheckpointV1.from_record(
                active[0]
            )
            verify_terminal_authority(
                active[0], registration=None, expected_kind="coordinator"
            )
            if (
                authority_ledger.canonical_record_digest(active[0])
                != state.checkpoint_digest
            ):
                raise authority_ledger.DispatchLedgerError(
                    "checkpoint does not match protected head"
                )
            manifest_path = repo / DISPATCH_DIR / state.archive_manifest_relative_path
            raw_manifest = json.loads(authority_ledger.read_secure_bytes(manifest_path))
            if not isinstance(raw_manifest, Mapping):
                raise authority_ledger.DispatchLedgerError(
                    "archive manifest fields are invalid"
                )
            manifest = authority_ledger.ArchiveManifestV1.from_mapping(raw_manifest)
            resealed = authority_ledger.reseal_archive_manifest(
                manifest_path.parent / "segments", manifest
            )
            if (
                resealed.logical_digest() != state.archive_manifest_sha256
                or resealed.logical_digest() != checkpoint.archive_manifest_sha256
            ):
                raise authority_ledger.DispatchLedgerError(
                    "archive logical head changed during reseal"
                )
            temporary = manifest_path.with_name(
                f".{manifest_path.name}.{uuid.uuid4().hex}.reseal"
            )
            _write_synced(
                temporary,
                authority_ledger.canonical_json_bytes(resealed.to_dict(), newline=True),
            )
            _replace_synced(temporary, manifest_path)
            replacement = authority_ledger.AuthorityLedgerHostStateV1(
                repository_binding=state.repository_binding,
                ledger_id=state.ledger_id,
                generation=state.generation,
                checkpoint_digest=state.checkpoint_digest,
                predecessor_checkpoint_digest=state.predecessor_checkpoint_digest,
                active_relative_path=state.active_relative_path,
                archive_manifest_relative_path=state.archive_manifest_relative_path,
                archive_manifest_sha256=state.archive_manifest_sha256,
                archive_stat_seals=tuple(
                    segment.stat_seal.to_dict() for segment in resealed.segments
                ),
                downgrade_barrier_sha256=state.downgrade_barrier_sha256,
                active_record_count=state.active_record_count,
                active_records_sha256=state.active_records_sha256,
                active_byte_size=state.active_byte_size,
            )
            commit_coordinator_ledger_state(
                binding,
                replacement.to_dict(),
                expected_generation=state.generation,
                allow_same_generation_reseal=True,
            )
        except (authority_ledger.DispatchLedgerError, TerminalAuthorityError) as exc:
            raise DispatchError(f"authority archive reseal failed: {exc}") from exc
        load_authority_snapshot(repo, full_archive_verify=True)
        return {"generation": state.generation, "resealed": True}


def authority_ledger_status(repo: Path) -> dict[str, object]:
    snapshot = load_authority_snapshot(repo)
    if snapshot.host_state is None:
        paths = [
            path
            for path in (repo / DISPATCH_DIR).glob("*.jsonl")
            if path.name != AUTHORITY_DOWNGRADE_BARRIER_NAME
        ]
        return {
            "mode": "legacy",
            "generation": None,
            "active_records": len(snapshot.physical_records),
            "active_bytes": sum(path.stat().st_size for path in paths),
            "archives": 0,
            "recovery": "none",
        }
    transactions = repo / AUTHORITY_LEDGER_DIRECTORY / "transactions"
    pending = sum(
        1
        for path in (transactions.iterdir() if transactions.is_dir() else ())
        if path.is_dir() and _authority_transaction_is_pending(path)
    )
    active = repo / DISPATCH_DIR / snapshot.host_state.active_relative_path
    archive_root = repo / AUTHORITY_ARCHIVE_DIRECTORY / snapshot.host_state.ledger_id
    return {
        "mode": "checkpoint",
        "ledger_id": snapshot.host_state.ledger_id,
        "generation": snapshot.host_state.generation,
        "active_records": len(snapshot.physical_records),
        "active_bytes": active.stat().st_size,
        "archives": sum(
            1 for path in archive_root.glob("generation-*") if path.is_dir()
        ),
        "recovery": "pending" if pending else "none",
    }


def verify_authority_ledger_full(repo: Path) -> AuthorityLedgerSnapshot:
    """Hash every archive and reconstruct the cumulative checkpoint chain."""
    snapshot = load_authority_snapshot(repo, full_archive_verify=True)
    if snapshot.checkpoint is None or snapshot.host_state is None:
        return snapshot
    checkpoint = snapshot.checkpoint
    while True:
        manifest_path = (
            repo
            / AUTHORITY_ARCHIVE_DIRECTORY
            / checkpoint.ledger_id
            / f"generation-{checkpoint.generation - 1:08d}"
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
                manifest.logical_digest() != checkpoint.archive_manifest_sha256
                or manifest.generation != checkpoint.generation - 1
            ):
                raise authority_ledger.DispatchLedgerError(
                    "archive chain does not match checkpoint"
                )
            authority_ledger.verify_archive_manifest(
                manifest_path.parent / "segments", manifest, full=True
            )
            archived: list[dict[str, object]] = []
            for segment in manifest.segments:
                archived.extend(
                    authority_ledger.read_jsonl_records(
                        manifest_path.parent / "segments" / segment.relative_name
                    )
                )
            if checkpoint.generation == 1:
                reconstructed = authority_ledger.accumulate_records(
                    archived,
                    ledger_id=checkpoint.ledger_id,
                    repository_binding=checkpoint.repository_binding,
                )
                if reconstructed != checkpoint.cumulative:
                    raise authority_ledger.DispatchLedgerError(
                        "legacy archive accumulator does not match checkpoint"
                    )
                break
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
            predecessor_digest = authority_ledger.canonical_record_digest(
                predecessor_record
            )
            if predecessor_digest != checkpoint.predecessor_checkpoint_digest:
                raise authority_ledger.DispatchLedgerError(
                    "checkpoint predecessor chain does not match"
                )
            reconstructed = authority_ledger.accumulate_records(
                archived,
                ledger_id=checkpoint.ledger_id,
                repository_binding=checkpoint.repository_binding,
                seed=predecessor.cumulative,
            )
            if reconstructed != checkpoint.cumulative:
                raise authority_ledger.DispatchLedgerError(
                    "archive accumulator does not match checkpoint"
                )
            checkpoint = predecessor
        except (authority_ledger.DispatchLedgerError, TerminalAuthorityError) as exc:
            raise DispatchError(f"authority full verification failed: {exc}") from exc
    return snapshot


def cmd_authority_ledger(args: argparse.Namespace) -> int:
    repo = primary_repo_root(args.repo.resolve())
    if args.authority_action == "status":
        result = authority_ledger_status(repo)
    elif args.authority_action == "compact":
        result = compact_authority_ledger(repo, dry_run=args.dry_run)
    elif args.authority_action == "verify":
        if args.reseal and not args.full:
            raise DispatchError("authority archive reseal requires --full")
        if args.reseal:
            result = reseal_authority_ledger_archive(repo)
        else:
            snapshot = (
                verify_authority_ledger_full(repo)
                if args.full
                else load_authority_snapshot(repo)
            )
            result = {
                "generation": (
                    snapshot.host_state.generation
                    if snapshot.host_state is not None
                    else None
                ),
                "verified": True,
                "full": args.full,
            }
    elif args.authority_action == "recover":
        result = recover_authority_ledger(repo)
    elif args.authority_action == "quarantine-invalid":
        result = quarantine_invalid_authority_transaction(repo, args.transaction_id)
    elif args.authority_action == "quarantine-legacy-line":
        result = quarantine_legacy_authority_line(
            repo,
            file_name=args.file,
            line_number=args.line_number,
        )
    else:
        raise DispatchError("authority ledger action is invalid")
    print(json.dumps(result, sort_keys=True))
    return 0
