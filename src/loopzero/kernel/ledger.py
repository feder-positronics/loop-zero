#!/usr/bin/env python3
"""Authenticated storage primitives for the durable dispatch authority ledger."""

from __future__ import annotations
from .settings import settings

import base64
import binascii
import fcntl
import hashlib
import json
import os
import re
import stat
import zlib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from functools import lru_cache
from pathlib import Path, PurePosixPath
from typing import Self, cast

LEDGER_ACCUMULATOR_SCHEME = "dispatch-coordinator-ledger-prefix-v3"
ARCHIVE_MANIFEST_SCHEME = "dispatch-authority-archive-manifest-v1"
CHECKPOINT_SCHEME = "dispatch-authority-checkpoint-v1"
RETAINED_STATE_SCHEME = "dispatch-authority-retained-state-v1"
HOST_STATE_SCHEME = "dispatch-authority-ledger-host-state-v1"
_LEDGER_DOMAIN = settings.ledger_domain
_RECORD_DOMAIN = b"record\0"
_HEX_128_RE = re.compile(r"[0-9a-f]{32}")
_HEX_256_RE = re.compile(r"[0-9a-f]{64}")
_SEGMENT_NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,254}")
RETENTION_ANCHOR_ENCODING = "canonical-json-zlib-base64-v1"
RETENTION_ANCHOR_MAX_UNCOMPRESSED_BYTES = 64 * 1024 * 1024
RETENTION_ANCHOR_FIELDS = (
    "task_ids",
    "work_unit_contracts",
    "attempt_settlements",
    "retry_outcomes",
    "open_before_record_digests",
)


class DispatchLedgerError(RuntimeError):
    """The authority ledger cannot be authenticated safely."""


def _require_hex(value: object, *, bits: int, label: str) -> str:
    pattern = _HEX_128_RE if bits == 128 else _HEX_256_RE
    if not isinstance(value, str) or pattern.fullmatch(value) is None:
        raise DispatchLedgerError(f"{label} is invalid")
    return value


def _require_nonnegative_int(value: object, *, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise DispatchLedgerError(f"{label} is invalid")
    return value


def _canonical_json(value: object) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, UnicodeEncodeError) as exc:
        raise DispatchLedgerError("ledger payload is invalid") from exc


def encode_retention_anchor_fields(
    anchors: Mapping[str, object],
) -> dict[str, object]:
    """Encode exact historical anchors for checkpoint retention."""
    if set(anchors) != set(RETENTION_ANCHOR_FIELDS) or any(
        not isinstance(anchors[field], list) for field in RETENTION_ANCHOR_FIELDS
    ):
        raise DispatchLedgerError("authority retention anchor fields are invalid")
    raw = _canonical_json(anchors)
    if len(raw) > RETENTION_ANCHOR_MAX_UNCOMPRESSED_BYTES:
        raise DispatchLedgerError("authority retention anchor payload exceeds limit")
    return {
        "anchor_encoding": RETENTION_ANCHOR_ENCODING,
        "anchor_payload": base64.b64encode(zlib.compress(raw, level=9)).decode(),
        "anchor_payload_sha256": hashlib.sha256(raw).hexdigest(),
        "anchor_uncompressed_bytes": len(raw),
    }


@lru_cache(maxsize=1)
def _validated_retention_anchor_payload(
    payload: str, expected_digest: str, expected_size: int
) -> bytes:
    """Authenticate one retained-anchor payload under a fixed memory ceiling."""
    if (
        _HEX_256_RE.fullmatch(expected_digest) is None
        or isinstance(expected_size, bool)
        or not isinstance(expected_size, int)
        or not 0 < expected_size <= RETENTION_ANCHOR_MAX_UNCOMPRESSED_BYTES
    ):
        raise DispatchLedgerError("authority retention anchor metadata is invalid")
    try:
        compressed = base64.b64decode(payload, validate=True)
        inflater = zlib.decompressobj()
        raw = inflater.decompress(compressed, expected_size + 1)
    except (binascii.Error, zlib.error, ValueError) as exc:
        raise DispatchLedgerError("authority retention anchor payload is invalid") from exc
    if (
        len(raw) != expected_size
        or not inflater.eof
        or inflater.unconsumed_tail
        or inflater.unused_data
    ):
        raise DispatchLedgerError(
            "authority retention anchor payload size does not match"
        )
    if hashlib.sha256(raw).hexdigest() != expected_digest:
        raise DispatchLedgerError(
            "authority retention anchor payload digest does not match"
        )
    try:
        decoded = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise DispatchLedgerError("authority retention anchor payload is invalid") from exc
    if (
        not isinstance(decoded, dict)
        or set(decoded) != set(RETENTION_ANCHOR_FIELDS)
        or any(not isinstance(decoded[field], list) for field in RETENTION_ANCHOR_FIELDS)
        or _canonical_json(decoded) != raw
    ):
        raise DispatchLedgerError("authority retention anchor payload is invalid")
    return raw


def decode_retention_anchor_fields(
    payload: str, expected_digest: str, expected_size: int
) -> dict[str, list[object]]:
    """Decode exact retained anchors into a fresh caller-owned projection."""
    raw = _validated_retention_anchor_payload(payload, expected_digest, expected_size)
    return cast(dict[str, list[object]], json.loads(raw))


def canonical_record_digest(record: Mapping[str, object]) -> str:
    """Return the stable digest consumed by the v3 ordered accumulator."""
    return hashlib.sha256(_canonical_json(dict(record))).hexdigest()


def repository_binding(common_directory: Path) -> str:
    """Bind host state to one canonical Git common directory."""
    resolved = common_directory.resolve(strict=True)
    if not resolved.is_dir():
        raise DispatchLedgerError("repository common directory is invalid")
    return hashlib.sha256(str(resolved).encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class LedgerAccumulatorV3:
    """Fixed-size ordered history state that can resume after a checkpoint."""

    ledger_id: str
    repository_binding: str
    record_count: int
    records_sha256: str

    def __post_init__(self) -> None:
        _require_hex(self.ledger_id, bits=128, label="ledger id")
        _require_hex(self.repository_binding, bits=256, label="repository binding")
        if (
            isinstance(self.record_count, bool)
            or not isinstance(self.record_count, int)
            or not 0 <= self.record_count < 2**64
        ):
            raise DispatchLedgerError("ledger record count is invalid")
        _require_hex(self.records_sha256, bits=256, label="ledger digest")

    @classmethod
    def initial(cls, *, ledger_id: str, repository_binding: str) -> Self:
        _require_hex(ledger_id, bits=128, label="ledger id")
        _require_hex(repository_binding, bits=256, label="repository binding")
        initial = hashlib.sha256(
            _LEDGER_DOMAIN
            + bytes.fromhex(ledger_id)
            + bytes.fromhex(repository_binding)
        ).hexdigest()
        return cls(
            ledger_id=ledger_id,
            repository_binding=repository_binding,
            record_count=0,
            records_sha256=initial,
        )

    def append_digest(self, record_digest: str) -> Self:
        digest = _require_hex(record_digest, bits=256, label="record digest")
        next_count = self.record_count + 1
        if next_count >= 2**64:
            raise DispatchLedgerError("ledger record count is invalid")
        next_digest = hashlib.sha256(
            _RECORD_DOMAIN
            + bytes.fromhex(self.records_sha256)
            + next_count.to_bytes(8, "big")
            + bytes.fromhex(digest)
        ).hexdigest()
        return replace(
            self,
            record_count=next_count,
            records_sha256=next_digest,
        )

    def append_record(self, record: Mapping[str, object]) -> Self:
        return self.append_digest(canonical_record_digest(record))

    def to_dict(self) -> dict[str, object]:
        return {
            "scheme": LEDGER_ACCUMULATOR_SCHEME,
            "ledger_id": self.ledger_id,
            "repository_binding": self.repository_binding,
            "record_count": self.record_count,
            "records_sha256": self.records_sha256,
        }

    @classmethod
    def from_mapping(cls, raw: Mapping[str, object]) -> Self:
        expected = {
            "scheme",
            "ledger_id",
            "repository_binding",
            "record_count",
            "records_sha256",
        }
        if set(raw) != expected or raw.get("scheme") != LEDGER_ACCUMULATOR_SCHEME:
            raise DispatchLedgerError("ledger accumulator fields are invalid")
        return cls(
            ledger_id=_require_hex(raw["ledger_id"], bits=128, label="ledger id"),
            repository_binding=_require_hex(
                raw["repository_binding"], bits=256, label="repository binding"
            ),
            record_count=_require_nonnegative_int(
                raw["record_count"], label="ledger record count"
            ),
            records_sha256=_require_hex(
                raw["records_sha256"], bits=256, label="ledger digest"
            ),
        )


def accumulate_records(
    records: Sequence[Mapping[str, object]],
    *,
    ledger_id: str,
    repository_binding: str,
    seed: LedgerAccumulatorV3 | None = None,
) -> LedgerAccumulatorV3:
    """Accumulate exact records from either the domain root or a checkpoint."""
    accumulator = seed or LedgerAccumulatorV3.initial(
        ledger_id=ledger_id,
        repository_binding=repository_binding,
    )
    if (
        accumulator.ledger_id != ledger_id
        or accumulator.repository_binding != repository_binding
    ):
        raise DispatchLedgerError("ledger seed identity does not match")
    for item in records:
        accumulator = accumulator.append_record(item)
    return accumulator


@dataclass(frozen=True)
class AuthorityLedgerCheckpointV1:
    """Coordinator-signed generation boundary and retained authority seed."""

    ledger_id: str
    repository_binding: str
    generation: int
    predecessor_checkpoint_digest: str | None
    cumulative: LedgerAccumulatorV3
    archived_record_count: int
    archive_manifest_sha256: str
    transaction_id: str
    created_at: str
    _retained_state_json: bytes = field(repr=False)

    def __post_init__(self) -> None:
        _require_hex(self.ledger_id, bits=128, label="ledger id")
        _require_hex(self.repository_binding, bits=256, label="repository binding")
        generation = _require_nonnegative_int(
            self.generation, label="checkpoint generation"
        )
        if generation < 1:
            raise DispatchLedgerError("checkpoint generation is invalid")
        if generation == 1:
            if self.predecessor_checkpoint_digest is not None:
                raise DispatchLedgerError("checkpoint predecessor is invalid")
        elif self.predecessor_checkpoint_digest is None:
            raise DispatchLedgerError("checkpoint predecessor is invalid")
        else:
            _require_hex(
                self.predecessor_checkpoint_digest,
                bits=256,
                label="checkpoint predecessor",
            )
        if (
            self.cumulative.ledger_id != self.ledger_id
            or self.cumulative.repository_binding != self.repository_binding
        ):
            raise DispatchLedgerError("checkpoint accumulator identity is invalid")
        archived_count = _require_nonnegative_int(
            self.archived_record_count, label="archived record count"
        )
        if archived_count > self.cumulative.record_count:
            raise DispatchLedgerError("archived record count is invalid")
        _require_hex(
            self.archive_manifest_sha256,
            bits=256,
            label="archive manifest digest",
        )
        _require_hex(self.transaction_id, bits=128, label="transaction id")
        _manifest_timestamp(self.created_at)
        try:
            records = json.loads(self._retained_state_json)
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise DispatchLedgerError("checkpoint retained state is invalid") from exc
        if not isinstance(records, list) or not all(
            isinstance(record, dict) for record in records
        ):
            raise DispatchLedgerError("checkpoint retained state is invalid")

    @property
    def retained_records(self) -> tuple[dict[str, object], ...]:
        records = json.loads(self._retained_state_json)
        return tuple(records)

    @property
    def retained_state_sha256(self) -> str:
        return hashlib.sha256(self._retained_state_json).hexdigest()

    @classmethod
    def create(
        cls,
        *,
        ledger_id: str,
        repository_binding: str,
        generation: int,
        predecessor_checkpoint_digest: str | None,
        cumulative: LedgerAccumulatorV3,
        archived_record_count: int,
        archive_manifest_sha256: str,
        retained_records: Sequence[Mapping[str, object]],
        transaction_id: str,
        created_at: str,
    ) -> Self:
        retained_json = _canonical_json([dict(record) for record in retained_records])
        return cls(
            ledger_id=ledger_id,
            repository_binding=repository_binding,
            generation=generation,
            predecessor_checkpoint_digest=predecessor_checkpoint_digest,
            cumulative=cumulative,
            archived_record_count=archived_record_count,
            archive_manifest_sha256=archive_manifest_sha256,
            transaction_id=transaction_id,
            created_at=created_at,
            _retained_state_json=retained_json,
        )

    def to_record(self) -> dict[str, object]:
        records = list(self.retained_records)
        return {
            "type": "coordinator-authority-cutover",
            "status": "active",
            "checkpoint_scheme": CHECKPOINT_SCHEME,
            "repository_binding": self.repository_binding,
            "ledger_id": self.ledger_id,
            "generation": self.generation,
            "predecessor_checkpoint_digest": self.predecessor_checkpoint_digest,
            "ledger_prefix": self.cumulative.to_dict(),
            "archived_record_count": self.archived_record_count,
            "archive_manifest_sha256": self.archive_manifest_sha256,
            "retained_state": {
                "scheme": RETAINED_STATE_SCHEME,
                "record_count": len(records),
                "records_sha256": self.retained_state_sha256,
                "records": records,
            },
            "transaction_id": self.transaction_id,
            "created_at": self.created_at,
        }

    @classmethod
    def from_record(cls, raw: Mapping[str, object]) -> Self:
        expected = {
            "type",
            "status",
            "checkpoint_scheme",
            "repository_binding",
            "ledger_id",
            "generation",
            "predecessor_checkpoint_digest",
            "ledger_prefix",
            "archived_record_count",
            "archive_manifest_sha256",
            "retained_state",
            "transaction_id",
            "created_at",
        }
        fields = set(raw)
        if fields == expected | {"terminal_authority_proof"}:
            proof = raw["terminal_authority_proof"]
            if not isinstance(proof, Mapping):
                raise DispatchLedgerError("checkpoint fields are invalid")
        elif fields != expected:
            raise DispatchLedgerError("checkpoint fields are invalid")
        if (
            raw.get("type") != "coordinator-authority-cutover"
            or raw.get("status") != "active"
            or raw.get("checkpoint_scheme") != CHECKPOINT_SCHEME
        ):
            raise DispatchLedgerError("checkpoint fields are invalid")
        prefix = raw["ledger_prefix"]
        retained = raw["retained_state"]
        if not isinstance(prefix, Mapping) or not isinstance(retained, Mapping):
            raise DispatchLedgerError("checkpoint fields are invalid")
        retained_expected = {"scheme", "record_count", "records_sha256", "records"}
        if (
            set(retained) != retained_expected
            or retained.get("scheme") != RETAINED_STATE_SCHEME
        ):
            raise DispatchLedgerError("checkpoint retained state is invalid")
        records = retained["records"]
        if not isinstance(records, list) or not all(
            isinstance(record, Mapping) for record in records
        ):
            raise DispatchLedgerError("checkpoint retained state is invalid")
        retained_json = _canonical_json([dict(record) for record in records])
        if (
            retained.get("record_count") != len(records)
            or retained.get("records_sha256")
            != hashlib.sha256(retained_json).hexdigest()
        ):
            raise DispatchLedgerError("checkpoint retained state is invalid")
        accumulator = LedgerAccumulatorV3.from_mapping(prefix)
        ledger_id = _require_hex(raw["ledger_id"], bits=128, label="ledger id")
        binding = _require_hex(
            raw["repository_binding"], bits=256, label="repository binding"
        )
        if (
            accumulator.ledger_id != ledger_id
            or accumulator.repository_binding != binding
        ):
            raise DispatchLedgerError("checkpoint accumulator identity is invalid")
        predecessor = raw["predecessor_checkpoint_digest"]
        if predecessor is not None and not isinstance(predecessor, str):
            raise DispatchLedgerError("checkpoint predecessor is invalid")
        created_at = raw["created_at"]
        if not isinstance(created_at, str):
            raise DispatchLedgerError("checkpoint creation time is invalid")
        return cls(
            ledger_id=ledger_id,
            repository_binding=binding,
            generation=_require_nonnegative_int(
                raw["generation"], label="checkpoint generation"
            ),
            predecessor_checkpoint_digest=predecessor,
            cumulative=accumulator,
            archived_record_count=_require_nonnegative_int(
                raw["archived_record_count"], label="archived record count"
            ),
            archive_manifest_sha256=_require_hex(
                raw["archive_manifest_sha256"],
                bits=256,
                label="archive manifest digest",
            ),
            transaction_id=_require_hex(
                raw["transaction_id"], bits=128, label="transaction id"
            ),
            created_at=created_at,
            _retained_state_json=retained_json,
        )


@dataclass(frozen=True)
class AuthorityLedgerHostStateV1:
    """Rollback-resistant private head for one repository authority ledger."""

    repository_binding: str
    ledger_id: str
    generation: int
    checkpoint_digest: str
    predecessor_checkpoint_digest: str | None
    active_relative_path: str
    archive_manifest_relative_path: str
    archive_manifest_sha256: str
    archive_stat_seals: tuple[dict[str, int], ...]
    downgrade_barrier_sha256: str
    active_record_count: int
    active_records_sha256: str
    active_byte_size: int

    def __post_init__(self) -> None:
        _require_hex(
            self.repository_binding, bits=256, label="host state repository binding"
        )
        _require_hex(self.ledger_id, bits=128, label="host state ledger id")
        if (
            isinstance(self.generation, bool)
            or not isinstance(self.generation, int)
            or self.generation < 1
        ):
            raise DispatchLedgerError("host state generation is invalid")
        _require_hex(
            self.checkpoint_digest, bits=256, label="host state checkpoint digest"
        )
        if self.generation == 1:
            if self.predecessor_checkpoint_digest is not None:
                raise DispatchLedgerError("host state predecessor is invalid")
        elif self.predecessor_checkpoint_digest is None:
            raise DispatchLedgerError("host state predecessor is invalid")
        else:
            _require_hex(
                self.predecessor_checkpoint_digest,
                bits=256,
                label="host state predecessor",
            )
        expected_active = f"ledger/generations/{self.generation:08d}/active.jsonl"
        expected_manifest = (
            f"archives/{self.ledger_id}/generation-{self.generation - 1:08d}/"
            "manifest.json"
        )
        if self.active_relative_path != expected_active:
            raise DispatchLedgerError("host state active path is invalid")
        if self.archive_manifest_relative_path != expected_manifest:
            raise DispatchLedgerError("host state archive path is invalid")
        _require_hex(
            self.archive_manifest_sha256,
            bits=256,
            label="host state archive manifest digest",
        )
        for seal in self.archive_stat_seals:
            ArchiveStatSealV1.from_mapping(seal)
        _require_hex(
            self.downgrade_barrier_sha256,
            bits=256,
            label="host state downgrade barrier digest",
        )
        if _require_nonnegative_int(
            self.active_record_count, label="host state active record count"
        ) < 1:
            raise DispatchLedgerError("host state active record count is invalid")
        _require_hex(
            self.active_records_sha256,
            bits=256,
            label="host state active digest",
        )
        if _require_nonnegative_int(
            self.active_byte_size, label="host state active byte size"
        ) < 1:
            raise DispatchLedgerError("host state active byte size is invalid")

    def to_dict(self) -> dict[str, object]:
        return {
            "scheme": HOST_STATE_SCHEME,
            "repository_binding": self.repository_binding,
            "ledger_id": self.ledger_id,
            "generation": self.generation,
            "checkpoint_digest": self.checkpoint_digest,
            "predecessor_checkpoint_digest": self.predecessor_checkpoint_digest,
            "active_relative_path": self.active_relative_path,
            "archive_manifest_relative_path": self.archive_manifest_relative_path,
            "archive_manifest_sha256": self.archive_manifest_sha256,
            "archive_stat_seals": [dict(seal) for seal in self.archive_stat_seals],
            "downgrade_barrier_sha256": self.downgrade_barrier_sha256,
            "active_record_count": self.active_record_count,
            "active_records_sha256": self.active_records_sha256,
            "active_byte_size": self.active_byte_size,
        }

    @classmethod
    def from_mapping(cls, raw: Mapping[str, object]) -> Self:
        expected = {
            "scheme",
            "repository_binding",
            "ledger_id",
            "generation",
            "checkpoint_digest",
            "predecessor_checkpoint_digest",
            "active_relative_path",
            "archive_manifest_relative_path",
            "archive_manifest_sha256",
            "archive_stat_seals",
            "downgrade_barrier_sha256",
            "active_record_count",
            "active_records_sha256",
            "active_byte_size",
        }
        if set(raw) != expected or raw.get("scheme") != HOST_STATE_SCHEME:
            raise DispatchLedgerError("host state fields are invalid")
        predecessor = raw["predecessor_checkpoint_digest"]
        if predecessor is not None and not isinstance(predecessor, str):
            raise DispatchLedgerError("host state predecessor is invalid")
        active = raw["active_relative_path"]
        manifest = raw["archive_manifest_relative_path"]
        if not isinstance(active, str) or not isinstance(manifest, str):
            raise DispatchLedgerError("host state paths are invalid")
        raw_seals = raw["archive_stat_seals"]
        if not isinstance(raw_seals, list) or not all(
            isinstance(seal, Mapping) for seal in raw_seals
        ):
            raise DispatchLedgerError("host state archive seals are invalid")
        return cls(
            repository_binding=_require_hex(
                raw["repository_binding"],
                bits=256,
                label="host state repository binding",
            ),
            ledger_id=_require_hex(
                raw["ledger_id"], bits=128, label="host state ledger id"
            ),
            generation=_require_nonnegative_int(
                raw["generation"], label="host state generation"
            ),
            checkpoint_digest=_require_hex(
                raw["checkpoint_digest"],
                bits=256,
                label="host state checkpoint digest",
            ),
            predecessor_checkpoint_digest=predecessor,
            active_relative_path=active,
            archive_manifest_relative_path=manifest,
            archive_manifest_sha256=_require_hex(
                raw["archive_manifest_sha256"],
                bits=256,
                label="host state archive manifest digest",
            ),
            archive_stat_seals=tuple(
                ArchiveStatSealV1.from_mapping(seal).to_dict() for seal in raw_seals
            ),
            downgrade_barrier_sha256=_require_hex(
                raw["downgrade_barrier_sha256"],
                bits=256,
                label="host state downgrade barrier digest",
            ),
            active_record_count=_require_nonnegative_int(
                raw["active_record_count"],
                label="host state active record count",
            ),
            active_records_sha256=_require_hex(
                raw["active_records_sha256"],
                bits=256,
                label="host state active digest",
            ),
            active_byte_size=_require_nonnegative_int(
                raw["active_byte_size"], label="host state active byte size"
            ),
        )


@dataclass(frozen=True)
class ArchiveStatSealV1:
    device: int
    inode: int
    mode: int
    owner: int
    byte_size: int
    modified_ns: int
    changed_ns: int

    def __post_init__(self) -> None:
        for name, value in self.to_dict().items():
            _require_nonnegative_int(value, label=f"archive {name}")
        if self.owner != os.getuid() or self.mode > 0o777 or self.mode & 0o022:
            raise DispatchLedgerError("archive stat seal is unsafe")

    @classmethod
    def from_path(cls, path: Path) -> Self:
        descriptor = _open_archive_segment(path)
        try:
            return cls(**_archive_stat_seal(os.fstat(descriptor)).to_dict())
        finally:
            os.close(descriptor)

    def to_dict(self) -> dict[str, int]:
        return {
            "device": self.device,
            "inode": self.inode,
            "mode": self.mode,
            "owner": self.owner,
            "byte_size": self.byte_size,
            "modified_ns": self.modified_ns,
            "changed_ns": self.changed_ns,
        }

    @classmethod
    def from_mapping(cls, raw: Mapping[str, object]) -> Self:
        expected = {
            "device",
            "inode",
            "mode",
            "owner",
            "byte_size",
            "modified_ns",
            "changed_ns",
        }
        if set(raw) != expected:
            raise DispatchLedgerError("archive stat seal fields are invalid")
        return cls(
            device=_require_nonnegative_int(raw["device"], label="archive device"),
            inode=_require_nonnegative_int(raw["inode"], label="archive inode"),
            mode=_require_nonnegative_int(raw["mode"], label="archive mode"),
            owner=_require_nonnegative_int(raw["owner"], label="archive owner"),
            byte_size=_require_nonnegative_int(
                raw["byte_size"], label="archive byte size"
            ),
            modified_ns=_require_nonnegative_int(
                raw["modified_ns"], label="archive modified time"
            ),
            changed_ns=_require_nonnegative_int(
                raw["changed_ns"], label="archive changed time"
            ),
        )


@dataclass(frozen=True)
class ArchiveSegmentV1:
    relative_name: str
    order: int
    byte_size: int
    record_count: int
    min_timestamp: str | None
    max_timestamp: str | None
    sha256: str
    stat_seal: ArchiveStatSealV1

    def __post_init__(self) -> None:
        _safe_segment_name(self.relative_name)
        _require_nonnegative_int(self.order, label="archive order")
        _require_nonnegative_int(self.byte_size, label="archive byte size")
        _require_nonnegative_int(self.record_count, label="archive record count")
        _require_hex(self.sha256, bits=256, label="archive digest")
        if self.byte_size != self.stat_seal.byte_size:
            raise DispatchLedgerError("archive stat seal size does not match")
        minimum = _manifest_timestamp(self.min_timestamp)
        maximum = _manifest_timestamp(self.max_timestamp)
        if (minimum is None) != (maximum is None) or (
            minimum is not None and maximum is not None and minimum > maximum
        ):
            raise DispatchLedgerError("archive timestamp range is invalid")

    def logical_dict(self) -> dict[str, object]:
        return {
            "relative_name": self.relative_name,
            "order": self.order,
            "byte_size": self.byte_size,
            "record_count": self.record_count,
            "min_timestamp": self.min_timestamp,
            "max_timestamp": self.max_timestamp,
            "sha256": self.sha256,
        }

    def to_dict(self) -> dict[str, object]:
        return {**self.logical_dict(), "stat_seal": self.stat_seal.to_dict()}

    @classmethod
    def from_mapping(cls, raw: Mapping[str, object]) -> Self:
        expected = {
            "relative_name",
            "order",
            "byte_size",
            "record_count",
            "min_timestamp",
            "max_timestamp",
            "sha256",
            "stat_seal",
        }
        if set(raw) != expected:
            raise DispatchLedgerError("archive segment fields are invalid")
        name = _safe_segment_name(raw["relative_name"])
        for integer_name in ("order", "byte_size", "record_count"):
            value = raw[integer_name]
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise DispatchLedgerError("archive segment fields are invalid")
        timestamps: list[str | None] = []
        for timestamp_name in ("min_timestamp", "max_timestamp"):
            value = raw[timestamp_name]
            if value is not None and not isinstance(value, str):
                raise DispatchLedgerError("archive segment fields are invalid")
            timestamps.append(value)
        stat_raw = raw["stat_seal"]
        if not isinstance(stat_raw, Mapping):
            raise DispatchLedgerError("archive segment fields are invalid")
        return cls(
            relative_name=name,
            order=_require_nonnegative_int(raw["order"], label="archive order"),
            byte_size=_require_nonnegative_int(
                raw["byte_size"], label="archive byte size"
            ),
            record_count=_require_nonnegative_int(
                raw["record_count"], label="archive record count"
            ),
            min_timestamp=timestamps[0],
            max_timestamp=timestamps[1],
            sha256=_require_hex(raw["sha256"], bits=256, label="archive digest"),
            stat_seal=ArchiveStatSealV1.from_mapping(stat_raw),
        )


@dataclass(frozen=True)
class ArchiveManifestV1:
    ledger_id: str
    generation: int
    segments: tuple[ArchiveSegmentV1, ...]

    def __post_init__(self) -> None:
        _require_hex(self.ledger_id, bits=128, label="ledger id")
        if (
            isinstance(self.generation, bool)
            or not isinstance(self.generation, int)
            or self.generation < 0
        ):
            raise DispatchLedgerError("archive generation is invalid")
        names = [segment.relative_name for segment in self.segments]
        if not names:
            raise DispatchLedgerError("archive manifest has no segments")
        if len(names) != len(set(names)) or any(
            segment.order != index for index, segment in enumerate(self.segments)
        ):
            raise DispatchLedgerError("archive segment order is invalid")

    def logical_dict(self) -> dict[str, object]:
        return {
            "scheme": ARCHIVE_MANIFEST_SCHEME,
            "ledger_id": self.ledger_id,
            "generation": self.generation,
            "segments": [segment.logical_dict() for segment in self.segments],
        }

    def logical_digest(self) -> str:
        """Digest signed by the checkpoint; stat seals remain host-local."""
        return hashlib.sha256(_canonical_json(self.logical_dict())).hexdigest()

    def to_dict(self) -> dict[str, object]:
        return {
            "scheme": ARCHIVE_MANIFEST_SCHEME,
            "ledger_id": self.ledger_id,
            "generation": self.generation,
            "segments": [segment.to_dict() for segment in self.segments],
        }

    @classmethod
    def from_mapping(cls, raw: Mapping[str, object]) -> Self:
        expected = {"scheme", "ledger_id", "generation", "segments"}
        if set(raw) != expected or raw.get("scheme") != ARCHIVE_MANIFEST_SCHEME:
            raise DispatchLedgerError("archive manifest fields are invalid")
        raw_segments = raw["segments"]
        if not isinstance(raw_segments, list) or not all(
            isinstance(segment, Mapping) for segment in raw_segments
        ):
            raise DispatchLedgerError("archive manifest fields are invalid")
        return cls(
            ledger_id=_require_hex(raw["ledger_id"], bits=128, label="ledger id"),
            generation=_require_nonnegative_int(
                raw["generation"], label="archive generation"
            ),
            segments=tuple(
                ArchiveSegmentV1.from_mapping(segment) for segment in raw_segments
            ),
        )


def _safe_segment_name(value: object) -> str:
    if (
        not isinstance(value, str)
        or _SEGMENT_NAME_RE.fullmatch(value) is None
        or value in {".", ".."}
    ):
        raise DispatchLedgerError("archive segment name is invalid")
    candidate = PurePosixPath(value)
    if candidate.is_absolute() or len(candidate.parts) != 1 or candidate.name != value:
        raise DispatchLedgerError("archive segment name is invalid")
    return value


def _manifest_timestamp(value: str | None) -> datetime | None:
    if value is None:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise DispatchLedgerError("archive timestamp range is invalid") from exc
    if parsed.tzinfo is None or parsed.astimezone(UTC).isoformat() != value:
        raise DispatchLedgerError("archive timestamp range is invalid")
    return parsed


def _archive_stat_seal(metadata: os.stat_result) -> ArchiveStatSealV1:
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_uid != os.getuid()
        or metadata.st_mode & 0o022
    ):
        raise DispatchLedgerError("archive segment is unsafe")
    return ArchiveStatSealV1(
        device=metadata.st_dev,
        inode=metadata.st_ino,
        mode=metadata.st_mode & 0o777,
        owner=metadata.st_uid,
        byte_size=metadata.st_size,
        modified_ns=metadata.st_mtime_ns,
        changed_ns=metadata.st_ctime_ns,
    )


def _open_archive_segment(path: Path, *, allow_group_write: bool = False) -> int:
    directory_descriptor = _open_anchored_directory(path.parent)
    try:
        return _open_archive_segment_at(
            directory_descriptor,
            path.name,
            allow_group_write=allow_group_write,
        )
    finally:
        os.close(directory_descriptor)


def _open_anchored_directory(path: Path) -> int:
    absolute = path.absolute()
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    if hasattr(os, "O_DIRECTORY"):
        flags |= os.O_DIRECTORY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open("/", flags)
    except OSError as exc:
        raise DispatchLedgerError("archive directory is unavailable") from exc
    try:
        for component in absolute.parts[1:]:
            try:
                before = os.stat(  # noqa: PTH116 - descriptor-anchored lookup
                    component, dir_fd=descriptor, follow_symlinks=False
                )
                child = os.open(component, flags, dir_fd=descriptor)
            except OSError as exc:
                raise DispatchLedgerError("archive directory is unsafe") from exc
            try:
                opened = os.fstat(child)
                if (
                    not stat.S_ISDIR(before.st_mode)
                    or not stat.S_ISDIR(opened.st_mode)
                    or (before.st_dev, before.st_ino) != (opened.st_dev, opened.st_ino)
                    # Rootless containers may map the filesystem root owner
                    # outside the process uid.  That immutable mount owner is
                    # as trusted as uid 0 while caller-owned descendants must
                    # still belong to this process.
                    or opened.st_uid not in {0, os.getuid(), os.stat("/").st_uid}
                    or (
                        (
                            opened.st_mode & 0o002
                            or (opened.st_uid != os.getuid() and opened.st_mode & 0o020)
                        )
                        and not opened.st_mode & stat.S_ISVTX
                    )
                ):
                    raise DispatchLedgerError("archive directory is unsafe")
            except BaseException:
                os.close(child)
                raise
            os.close(descriptor)
            descriptor = child
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _open_archive_segment_at(
    directory_descriptor: int,
    name: str,
    *,
    allow_group_write: bool = False,
) -> int:
    _safe_segment_name(name)
    try:
        initial = os.stat(  # noqa: PTH116 - descriptor-anchored lookup
            name, dir_fd=directory_descriptor, follow_symlinks=False
        )
    except OSError as exc:
        raise DispatchLedgerError("archive segment is unavailable") from exc
    if not stat.S_ISREG(initial.st_mode):
        raise DispatchLedgerError("archive segment is unsafe")
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(name, flags, dir_fd=directory_descriptor)
    except OSError as exc:
        raise DispatchLedgerError("archive segment is unavailable") from exc
    try:
        opened = os.fstat(descriptor)
        if (opened.st_dev, opened.st_ino) != (initial.st_dev, initial.st_ino):
            raise DispatchLedgerError("archive segment changed while opening")
        if (
            opened.st_uid != os.getuid()
            or opened.st_mode & 0o002
            or (not allow_group_write and opened.st_mode & 0o020)
        ):
            raise DispatchLedgerError("archive segment is unsafe")
    except DispatchLedgerError:
        os.close(descriptor)
        raise
    return descriptor


def _timestamp_range(payload: bytes) -> tuple[int, str | None, str | None]:
    observed: list[datetime] = []
    count = 0
    for line_number, raw_line in enumerate(payload.splitlines(), start=1):
        if not raw_line.strip():
            continue
        try:
            parsed = json.loads(raw_line)
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise DispatchLedgerError(
                f"archive segment contains invalid JSON at line {line_number}"
            ) from exc
        if not isinstance(parsed, dict):
            raise DispatchLedgerError(
                f"archive segment contains invalid record at line {line_number}"
            )
        count += 1
        raw_timestamp = parsed.get("ts")
        if raw_timestamp is None:
            continue
        if not isinstance(raw_timestamp, str):
            raise DispatchLedgerError("archive record timestamp is invalid")
        try:
            timestamp = datetime.fromisoformat(raw_timestamp)
        except ValueError as exc:
            raise DispatchLedgerError("archive record timestamp is invalid") from exc
        if timestamp.tzinfo is None:
            raise DispatchLedgerError("archive record timestamp is invalid")
        observed.append(timestamp.astimezone(UTC))
    if not observed:
        return count, None, None
    return count, min(observed).isoformat(), max(observed).isoformat()


def _segment_from_path(
    path: Path, *, relative_name: str, order: int
) -> ArchiveSegmentV1:
    descriptor = _open_archive_segment(path)
    try:
        before = os.fstat(descriptor)
        payload = bytearray()
        while chunk := os.read(descriptor, 1024 * 1024):
            payload.extend(chunk)
        after = os.fstat(descriptor)
    except OSError as exc:
        raise DispatchLedgerError("archive segment is unavailable") from exc
    finally:
        os.close(descriptor)
    seal = _archive_stat_seal(before)
    if _archive_stat_seal(after) != seal or len(payload) != seal.byte_size:
        raise DispatchLedgerError("archive segment changed while reading")
    payload_bytes = bytes(payload)
    record_count, minimum, maximum = _timestamp_range(payload_bytes)
    return ArchiveSegmentV1(
        relative_name=relative_name,
        order=order,
        byte_size=len(payload),
        record_count=record_count,
        min_timestamp=minimum,
        max_timestamp=maximum,
        sha256=hashlib.sha256(payload_bytes).hexdigest(),
        stat_seal=seal,
    )


def build_archive_manifest(
    segments_directory: Path,
    *,
    relative_names: Sequence[str],
    ledger_id: str,
    generation: int,
) -> ArchiveManifestV1:
    names = tuple(_safe_segment_name(name) for name in relative_names)
    if len(names) != len(set(names)):
        raise DispatchLedgerError("archive segment name is duplicated")
    segments = tuple(
        _segment_from_path(
            segments_directory / name,
            relative_name=name,
            order=index,
        )
        for index, name in enumerate(names)
    )
    return ArchiveManifestV1(
        ledger_id=ledger_id,
        generation=generation,
        segments=segments,
    )


def verify_archive_manifest(
    segments_directory: Path,
    manifest: ArchiveManifestV1,
    *,
    full: bool,
) -> ArchiveManifestV1:
    for segment in manifest.segments:
        path = segments_directory / segment.relative_name
        if ArchiveStatSealV1.from_path(path) != segment.stat_seal:
            raise DispatchLedgerError("archive stat seal does not match")
        if full:
            actual = _segment_from_path(
                path,
                relative_name=segment.relative_name,
                order=segment.order,
            )
            if actual.logical_dict() != segment.logical_dict():
                raise DispatchLedgerError("archive segment digest does not match")
    return manifest


def reseal_archive_manifest(
    segments_directory: Path, manifest: ArchiveManifestV1
) -> ArchiveManifestV1:
    rebuilt = build_archive_manifest(
        segments_directory,
        relative_names=tuple(segment.relative_name for segment in manifest.segments),
        ledger_id=manifest.ledger_id,
        generation=manifest.generation,
    )
    if rebuilt.logical_digest() != manifest.logical_digest():
        raise DispatchLedgerError("archive segment digest does not match")
    return rebuilt


def _read_owned_bytes(
    path: Path,
    *,
    max_bytes: int,
    allow_group_write: bool,
) -> bytes:
    if max_bytes < 1:
        raise DispatchLedgerError("secure read limit is invalid")
    descriptor = _open_archive_segment(
        path,
        allow_group_write=allow_group_write,
    )
    try:
        fcntl.flock(descriptor, fcntl.LOCK_SH)
        before = os.fstat(descriptor)
        if before.st_size > max_bytes:
            raise DispatchLedgerError("authority ledger file is too large")
        payload = bytearray()
        while len(payload) <= max_bytes:
            chunk = os.read(descriptor, min(1024 * 1024, max_bytes + 1 - len(payload)))
            if not chunk:
                break
            payload.extend(chunk)
        after = os.fstat(descriptor)
        fcntl.flock(descriptor, fcntl.LOCK_UN)
    except OSError as exc:
        raise DispatchLedgerError("authority ledger file is unavailable") from exc
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
        len(payload) > max_bytes
        or any(getattr(before, field) != getattr(after, field) for field in stable_fields)
        or len(payload) != before.st_size
    ):
        raise DispatchLedgerError("authority ledger file changed while reading")
    return bytes(payload)


def read_secure_bytes(path: Path, *, max_bytes: int = 256 * 1024 * 1024) -> bytes:
    """Read one private authority file through a validated no-follow descriptor."""
    return _read_owned_bytes(
        path,
        max_bytes=max_bytes,
        allow_group_write=False,
    )


def read_legacy_bytes(path: Path) -> bytes:
    """Read pre-v3 bytes while preserving their historical owner/mode contract."""
    return _read_owned_bytes(
        path,
        max_bytes=256 * 1024 * 1024,
        allow_group_write=True,
    )


def read_legacy_jsonl_records(path: Path) -> list[dict[str, object]]:
    """Parse a pre-v3 stream after a descriptor-safe compatibility read."""
    return parse_jsonl_records(read_legacy_bytes(path), label=str(path))


def parse_jsonl_records(payload: bytes, *, label: str) -> list[dict[str, object]]:
    """Strictly parse a complete authority JSONL payload."""
    records: list[dict[str, object]] = []
    for line_number, raw_line in enumerate(payload.splitlines(), start=1):
        if not raw_line.strip():
            continue
        try:
            parsed = json.loads(raw_line)
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise DispatchLedgerError(f"invalid JSON in {label}:{line_number}") from exc
        if not isinstance(parsed, dict):
            raise DispatchLedgerError(f"invalid record in {label}:{line_number}")
        records.append(parsed)
    return records


def read_jsonl_records(path: Path) -> list[dict[str, object]]:
    return parse_jsonl_records(read_secure_bytes(path), label=str(path))


def canonical_json_bytes(value: object, *, newline: bool = False) -> bytes:
    payload = _canonical_json(value)
    return payload + (b"\n" if newline else b"")
