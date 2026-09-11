import hashlib
import importlib.util
import json
import os
import shutil
import sys
from pathlib import Path
from types import ModuleType

import pytest


def load_module() -> ModuleType:
    repo_root = Path(__file__).resolve().parents[4]
    module_path = repo_root / "scripts" / "util" / "dispatch_ledger.py"
    spec = importlib.util.spec_from_file_location("dispatch_ledger", module_path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


module = load_module()

LEDGER_ID = "1" * 32
REPOSITORY_BINDING = "2" * 64


def record(task_id: str, timestamp: str) -> dict[str, object]:
    return {
        "type": "attempt-terminal",
        "task_id": task_id,
        "status": "completed",
        "ts": timestamp,
    }


def test_v3_accumulator_resumes_to_the_exact_single_pass_digest() -> None:
    records = [
        record("one", "2026-08-26T12:00:00+00:00"),
        record("two", "2026-08-27T12:00:00+00:00"),
        record("three", "2026-08-28T12:00:00+00:00"),
    ]

    single = module.accumulate_records(
        records,
        ledger_id=LEDGER_ID,
        repository_binding=REPOSITORY_BINDING,
    )
    prefix = module.accumulate_records(
        records[:2],
        ledger_id=LEDGER_ID,
        repository_binding=REPOSITORY_BINDING,
    )
    resumed = module.accumulate_records(
        records[2:],
        ledger_id=LEDGER_ID,
        repository_binding=REPOSITORY_BINDING,
        seed=prefix,
    )

    assert resumed == single
    assert single.record_count == 3
    assert (
        single.records_sha256
        == "7f5a99352d6db4b1604bf59bf38a1c844c24239a6d54a70751af540778880b93"
    )


def test_v3_accumulator_rejects_reorder_omission_insertion_and_replay() -> None:
    first = record("one", "2026-08-26T12:00:00+00:00")
    second = record("two", "2026-08-27T12:00:00+00:00")
    baseline = module.accumulate_records(
        [first, second],
        ledger_id=LEDGER_ID,
        repository_binding=REPOSITORY_BINDING,
    )

    variants = [
        [second, first],
        [first],
        [first, record("inserted", "2026-08-26T18:00:00+00:00"), second],
    ]
    for variant in variants:
        assert (
            module.accumulate_records(
                variant,
                ledger_id=LEDGER_ID,
                repository_binding=REPOSITORY_BINDING,
            ).records_sha256
            != baseline.records_sha256
        )
    assert (
        module.accumulate_records(
            [first, second],
            ledger_id="3" * 32,
            repository_binding=REPOSITORY_BINDING,
        ).records_sha256
        != baseline.records_sha256
    )
    assert (
        module.accumulate_records(
            [first, second],
            ledger_id=LEDGER_ID,
            repository_binding="4" * 64,
        ).records_sha256
        != baseline.records_sha256
    )


def test_v3_accumulator_rejects_a_foreign_seed() -> None:
    seed = module.LedgerAccumulatorV3.initial(
        ledger_id=LEDGER_ID,
        repository_binding=REPOSITORY_BINDING,
    )

    with pytest.raises(module.DispatchLedgerError, match="seed identity"):
        module.accumulate_records(
            [],
            ledger_id="3" * 32,
            repository_binding=REPOSITORY_BINDING,
            seed=seed,
        )


def test_repository_binding_uses_the_resolved_absolute_common_directory(
    tmp_path: Path,
) -> None:
    common = tmp_path / "repo" / ".git"
    common.mkdir(parents=True)

    assert (
        module.repository_binding(common / ".." / ".git")
        == hashlib.sha256(str(common.resolve()).encode("utf-8")).hexdigest()
    )


def write_segment(path: Path, rows: list[dict[str, object]]) -> bytes:
    payload = b"".join(
        json.dumps(row, sort_keys=True).encode("utf-8") + b"\n" for row in rows
    )
    path.write_bytes(payload)
    path.chmod(0o600)
    return payload


def test_legacy_reader_accepts_historical_group_write_but_not_world_write(
    tmp_path: Path,
) -> None:
    path = tmp_path / "2026-08-27.jsonl"
    expected = [record("one", "2026-08-27T12:00:00+00:00")]
    write_segment(path, expected)
    path.chmod(0o660)

    assert module.read_legacy_jsonl_records(path) == expected
    with pytest.raises(module.DispatchLedgerError, match="unsafe"):
        module.read_secure_bytes(path)

    path.chmod(0o662)
    with pytest.raises(module.DispatchLedgerError, match="unsafe"):
        module.read_legacy_jsonl_records(path)


def test_archive_manifest_preserves_each_legacy_file_and_timestamp_range(
    tmp_path: Path,
) -> None:
    segments = tmp_path / "segments"
    segments.mkdir()
    first_payload = write_segment(
        segments / "2026-08-26.jsonl",
        [
            record("one", "2026-08-26T13:00:00+01:00"),
            record("two", "2026-08-26T11:00:00+00:00"),
        ],
    )
    second_payload = write_segment(
        segments / "2026-08-27.jsonl",
        [record("three", "2026-08-27T12:00:00Z")],
    )

    manifest = module.build_archive_manifest(
        segments,
        relative_names=("2026-08-26.jsonl", "2026-08-27.jsonl"),
        ledger_id=LEDGER_ID,
        generation=0,
    )

    assert [segment.relative_name for segment in manifest.segments] == [
        "2026-08-26.jsonl",
        "2026-08-27.jsonl",
    ]
    assert manifest.segments[0].byte_size == len(first_payload)
    assert manifest.segments[0].sha256 == hashlib.sha256(first_payload).hexdigest()
    assert manifest.segments[0].record_count == 2
    assert manifest.segments[0].min_timestamp == "2026-08-26T11:00:00+00:00"
    assert manifest.segments[0].max_timestamp == "2026-08-26T12:00:00+00:00"
    assert manifest.segments[1].byte_size == len(second_payload)
    assert module.verify_archive_manifest(segments, manifest, full=True) == manifest


@pytest.mark.parametrize(
    "relative_names",
    [
        ("../escape.jsonl",),
        ("nested/segment.jsonl",),
        ("/absolute.jsonl",),
        ("..",),
        ("line\nbreak.jsonl",),
        ("duplicate.jsonl", "duplicate.jsonl"),
    ],
)
def test_archive_manifest_rejects_unsafe_or_duplicate_names(
    tmp_path: Path, relative_names: tuple[str, ...]
) -> None:
    with pytest.raises(module.DispatchLedgerError, match="segment name"):
        module.build_archive_manifest(
            tmp_path,
            relative_names=relative_names,
            ledger_id=LEDGER_ID,
            generation=0,
        )


def test_archive_hot_verification_rejects_stat_drift_and_full_reseal_is_exact(
    tmp_path: Path,
) -> None:
    original = tmp_path / "original"
    restored = tmp_path / "restored"
    original.mkdir()
    restored.mkdir()
    write_segment(
        original / "active.jsonl",
        [record("one", "2026-08-27T12:00:00+00:00")],
    )
    manifest = module.build_archive_manifest(
        original,
        relative_names=("active.jsonl",),
        ledger_id=LEDGER_ID,
        generation=1,
    )
    shutil.copyfile(original / "active.jsonl", restored / "active.jsonl")
    (restored / "active.jsonl").chmod(0o600)

    with pytest.raises(module.DispatchLedgerError, match="stat seal"):
        module.verify_archive_manifest(restored, manifest, full=False)

    resealed = module.reseal_archive_manifest(restored, manifest)

    assert resealed.logical_digest() == manifest.logical_digest()
    assert resealed.segments[0].stat_seal != manifest.segments[0].stat_seal
    assert module.verify_archive_manifest(restored, resealed, full=False) == resealed


def test_archive_reseal_rejects_same_size_content_substitution(tmp_path: Path) -> None:
    segments = tmp_path / "segments"
    segments.mkdir()
    path = segments / "active.jsonl"
    write_segment(path, [record("one", "2026-08-27T12:00:00+00:00")])
    manifest = module.build_archive_manifest(
        segments,
        relative_names=("active.jsonl",),
        ledger_id=LEDGER_ID,
        generation=1,
    )
    payload = path.read_bytes()
    path.write_bytes(payload.replace(b'"one"', b'"two"'))
    assert path.stat().st_size == len(payload)

    with pytest.raises(module.DispatchLedgerError, match="digest"):
        module.reseal_archive_manifest(segments, manifest)


def test_archive_manifest_rejects_invalid_or_naive_timestamps(tmp_path: Path) -> None:
    segments = tmp_path / "segments"
    segments.mkdir()
    write_segment(
        segments / "active.jsonl",
        [record("one", "2026-08-27T12:00:00")],
    )

    with pytest.raises(module.DispatchLedgerError, match="timestamp"):
        module.build_archive_manifest(
            segments,
            relative_names=("active.jsonl",),
            ledger_id=LEDGER_ID,
            generation=1,
        )


def test_archive_manifest_rejects_symlink_segment(tmp_path: Path) -> None:
    segments = tmp_path / "segments"
    segments.mkdir()
    target = tmp_path / "target.jsonl"
    write_segment(target, [record("one", "2026-08-27T12:00:00+00:00")])
    (segments / "active.jsonl").symlink_to(target)

    with pytest.raises(module.DispatchLedgerError, match="segment"):
        module.build_archive_manifest(
            segments,
            relative_names=("active.jsonl",),
            ledger_id=LEDGER_ID,
            generation=1,
        )


def test_archive_manifest_rejects_symlinked_segments_directory(tmp_path: Path) -> None:
    actual = tmp_path / "actual"
    actual.mkdir()
    write_segment(
        actual / "active.jsonl",
        [record("one", "2026-08-27T12:00:00+00:00")],
    )
    linked = tmp_path / "linked"
    linked.symlink_to(actual, target_is_directory=True)

    with pytest.raises(module.DispatchLedgerError, match="archive directory"):
        module.build_archive_manifest(
            linked,
            relative_names=("active.jsonl",),
            ledger_id=LEDGER_ID,
            generation=1,
        )


def test_manifest_mapping_round_trip_is_strict(tmp_path: Path) -> None:
    segments = tmp_path / "segments"
    segments.mkdir()
    write_segment(
        segments / "active.jsonl",
        [record("one", "2026-08-27T12:00:00+00:00")],
    )
    manifest = module.build_archive_manifest(
        segments,
        relative_names=("active.jsonl",),
        ledger_id=LEDGER_ID,
        generation=1,
    )

    assert module.ArchiveManifestV1.from_mapping(manifest.to_dict()) == manifest
    invalid = manifest.to_dict()
    invalid["unexpected"] = True
    with pytest.raises(module.DispatchLedgerError, match="fields"):
        module.ArchiveManifestV1.from_mapping(invalid)

    invalid = manifest.to_dict()
    invalid["segments"][0]["min_timestamp"] = "2026-08-27T12:00:00"
    with pytest.raises(module.DispatchLedgerError, match="timestamp"):
        module.ArchiveManifestV1.from_mapping(invalid)

    invalid = manifest.to_dict()
    invalid["segments"][0]["byte_size"] += 1
    with pytest.raises(module.DispatchLedgerError, match="stat seal size"):
        module.ArchiveManifestV1.from_mapping(invalid)


def checkpoint() -> object:
    retained = (
        record("open", "2026-08-27T12:00:00+00:00"),
        {"type": "attempt-owner", "task_id": "open"},
    )
    cumulative = module.accumulate_records(
        retained,
        ledger_id=LEDGER_ID,
        repository_binding=REPOSITORY_BINDING,
    )
    return module.AuthorityLedgerCheckpointV1.create(
        ledger_id=LEDGER_ID,
        repository_binding=REPOSITORY_BINDING,
        generation=1,
        predecessor_checkpoint_digest=None,
        cumulative=cumulative,
        archived_record_count=2,
        archive_manifest_sha256="5" * 64,
        retained_records=retained,
        transaction_id="6" * 32,
        created_at="2026-08-27T12:30:00+00:00",
    )


def test_checkpoint_record_round_trip_binds_retained_state_and_v3_prefix() -> None:
    expected = checkpoint()

    record = expected.to_record()

    assert record["type"] == "coordinator-authority-cutover"
    assert record["status"] == "active"
    assert record["ledger_prefix"]["scheme"] == module.LEDGER_ACCUMULATOR_SCHEME
    assert module.AuthorityLedgerCheckpointV1.from_record(record) == expected


def test_checkpoint_rejects_retained_omission_and_accumulator_replay() -> None:
    valid = checkpoint().to_record()
    valid["retained_state"]["records"].pop()
    with pytest.raises(module.DispatchLedgerError, match="retained state"):
        module.AuthorityLedgerCheckpointV1.from_record(valid)

    valid = checkpoint().to_record()
    valid["ledger_prefix"]["ledger_id"] = "7" * 32
    with pytest.raises(module.DispatchLedgerError, match="identity"):
        module.AuthorityLedgerCheckpointV1.from_record(valid)


def test_checkpoint_generation_requires_exact_predecessor_shape() -> None:
    valid = checkpoint()

    with pytest.raises(module.DispatchLedgerError, match="predecessor"):
        module.AuthorityLedgerCheckpointV1.create(
            ledger_id=valid.ledger_id,
            repository_binding=valid.repository_binding,
            generation=2,
            predecessor_checkpoint_digest=None,
            cumulative=valid.cumulative,
            archived_record_count=valid.archived_record_count,
            archive_manifest_sha256=valid.archive_manifest_sha256,
            retained_records=valid.retained_records,
            transaction_id=valid.transaction_id,
            created_at=valid.created_at,
        )


def test_checkpoint_rejects_unknown_fields() -> None:
    valid = checkpoint().to_record()
    valid["unknown"] = True

    with pytest.raises(module.DispatchLedgerError, match="checkpoint fields"):
        module.AuthorityLedgerCheckpointV1.from_record(valid)


def test_host_state_round_trip_is_strict_and_binds_checkpoint_identity() -> None:
    state = module.AuthorityLedgerHostStateV1(
        repository_binding=REPOSITORY_BINDING,
        ledger_id=LEDGER_ID,
        generation=2,
        checkpoint_digest="3" * 64,
        predecessor_checkpoint_digest="4" * 64,
        active_relative_path="ledger/generations/00000002/active.jsonl",
        archive_manifest_relative_path=(
            "archives/11111111111111111111111111111111/"
            "generation-00000001/manifest.json"
        ),
        archive_manifest_sha256="5" * 64,
        archive_stat_seals=(),
        downgrade_barrier_sha256="6" * 64,
        active_record_count=1,
        active_records_sha256="7" * 64,
        active_byte_size=100,
    )

    assert module.AuthorityLedgerHostStateV1.from_mapping(state.to_dict()) == state
    invalid = state.to_dict()
    invalid["unexpected"] = True
    with pytest.raises(module.DispatchLedgerError, match="host state fields"):
        module.AuthorityLedgerHostStateV1.from_mapping(invalid)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("generation", 0),
        ("predecessor_checkpoint_digest", None),
        ("active_relative_path", "../active.jsonl"),
        ("archive_manifest_relative_path", "/tmp/manifest.json"),
    ],
)
def test_host_state_rejects_rollback_and_unsafe_paths(
    field: str, value: object
) -> None:
    raw = module.AuthorityLedgerHostStateV1(
        repository_binding=REPOSITORY_BINDING,
        ledger_id=LEDGER_ID,
        generation=2,
        checkpoint_digest="3" * 64,
        predecessor_checkpoint_digest="4" * 64,
        active_relative_path="ledger/generations/00000002/active.jsonl",
        archive_manifest_relative_path=(
            "archives/11111111111111111111111111111111/"
            "generation-00000001/manifest.json"
        ),
        archive_manifest_sha256="5" * 64,
        archive_stat_seals=(),
        downgrade_barrier_sha256="6" * 64,
        active_record_count=1,
        active_records_sha256="7" * 64,
        active_byte_size=100,
    ).to_dict()
    raw[field] = value

    with pytest.raises(module.DispatchLedgerError, match="host state"):
        module.AuthorityLedgerHostStateV1.from_mapping(raw)
