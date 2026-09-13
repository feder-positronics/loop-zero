import hashlib
import importlib.util
import json
import os
import shutil
import stat
import subprocess
import sys
import textwrap
from pathlib import Path
from types import ModuleType

import pytest


def load_module() -> ModuleType:
    return importlib.import_module('loopzero.kernel.ledger')


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


@pytest.fixture(autouse=True)
def require_visible_root_ownership(request):
    archive_tests = {
        "test_legacy_reader_accepts_historical_group_write_but_not_world_write",
        "test_archive_manifest_preserves_each_legacy_file_and_timestamp_range",
        "test_archive_hot_verification_rejects_stat_drift_and_full_reseal_is_exact",
        "test_archive_reseal_rejects_same_size_content_substitution",
        "test_archive_manifest_rejects_invalid_or_naive_timestamps",
        "test_archive_manifest_rejects_symlink_segment",
        "test_manifest_mapping_round_trip_is_strict",
    }
    if request.node.originalname in archive_tests and Path("/tmp").stat().st_uid not in {0, os.getuid()}:
        pytest.skip("archive ownership checks require visible root UID; sandbox maps /tmp owner to nobody")


# Anchored archive directory trust ---------------------------------------------


def _open_and_close(path: Path) -> None:
    descriptor = module._open_anchored_directory(path)
    os.close(descriptor)


def _fake_owner(monkeypatch, target: Path, owner: int) -> None:
    """Report ``owner`` for ``target`` from ``os.fstat`` without chown rights."""
    real_fstat = os.fstat
    identity = (target.stat().st_dev, target.stat().st_ino)

    def fstat(descriptor):
        result = real_fstat(descriptor)
        if (result.st_dev, result.st_ino) != identity:
            return result
        values = list(result)
        values[stat.ST_UID] = owner
        return os.stat_result(values)

    monkeypatch.setattr(module.os, "fstat", fstat)


def test_anchored_directory_documents_the_accepted_owner_set() -> None:
    """Directory owners accepted on the walk from ``/`` to the archive.

    ``0`` covers system-owned ancestors (``/home``, ``/srv``), the process uid
    covers caller-owned state, and the owner of ``/`` covers rootless user
    namespaces (``bwrap --unshare-user``), where an unmapped system owner is
    rendered as the overflow uid on ``/`` and on every system-owned ancestor
    alike.  Nothing else is trusted, and the segment file itself must still
    belong to the process uid.
    """
    source = Path(module.__file__).read_text(encoding="utf-8")
    assert 'opened.st_uid not in {0, os.getuid(), os.stat("/").st_uid}' in source
    assert "opened.st_uid != os.getuid()" in source  # archive segment owner


@pytest.mark.parametrize("trusted_owner", ("root", "process", "mount-root"))
def test_anchored_directory_accepts_each_trusted_owner(
    tmp_path: Path, monkeypatch, trusted_owner: str
) -> None:
    segments = tmp_path / "segments"
    segments.mkdir()
    segments.chmod(0o755)  # host umask must not add group write
    owner = {
        "root": 0,
        "process": os.getuid(),
        "mount-root": os.stat("/").st_uid,
    }[trusted_owner]
    _fake_owner(monkeypatch, segments, owner)
    _open_and_close(segments)


def test_anchored_directory_rejects_foreign_owned_descendant(
    tmp_path: Path, monkeypatch
) -> None:
    segments = tmp_path / "segments"
    segments.mkdir()
    segments.chmod(0o755)
    foreign = max(os.getuid(), os.stat("/").st_uid) + 4242
    assert foreign not in {0, os.getuid(), os.stat("/").st_uid}
    _fake_owner(monkeypatch, segments, foreign)
    with pytest.raises(module.DispatchLedgerError, match="archive directory is unsafe"):
        _open_and_close(segments)


def test_anchored_directory_rejects_world_writable_descendant(tmp_path: Path) -> None:
    segments = tmp_path / "segments"
    segments.mkdir()
    nested = segments / "nested"
    nested.mkdir()
    segments.chmod(0o777)
    try:
        with pytest.raises(module.DispatchLedgerError, match="archive directory is unsafe"):
            _open_and_close(nested)
        # Sticky world-writable directories (``/tmp`` shape) remain acceptable.
        segments.chmod(0o1777)
        _open_and_close(nested)
    finally:
        segments.chmod(0o700)


def test_anchored_directory_rejects_group_writable_foreign_descendant(
    tmp_path: Path, monkeypatch
) -> None:
    segments = tmp_path / "segments"
    segments.mkdir()
    segments.chmod(0o770)
    try:
        # Group write is tolerated only on a caller-owned directory.
        _open_and_close(segments)
        _fake_owner(monkeypatch, segments, 0)
        with pytest.raises(module.DispatchLedgerError, match="archive directory is unsafe"):
            _open_and_close(segments)
    finally:
        segments.chmod(0o700)


def _bwrap_userns_available() -> bool:
    if shutil.which("bwrap") is None:
        return False
    probe = subprocess.run(
        ["bwrap", "--unshare-user", "--ro-bind", "/", "/", "--dev", "/dev",
         "--proc", "/proc", "--", "/bin/true"],
        capture_output=True,
    )
    return probe.returncode == 0


def test_anchored_directory_opens_inside_rootless_user_namespace(tmp_path: Path) -> None:
    """The mount-root owner is what makes archives reachable under ``--unshare-user``.

    Inside the namespace ``/`` and every system-owned ancestor of the archive
    report the overflow uid rather than ``0``; the widened owner set accepts
    them while the caller-owned tail still has to belong to the process uid.
    """
    if not _bwrap_userns_available():
        pytest.skip("bubblewrap user namespaces are unavailable here")
    segments = tmp_path / "segments"
    segments.mkdir()
    script = textwrap.dedent(
        """
        import json, os, sys
        from pathlib import Path
        from loopzero.kernel import ledger
        target = Path(sys.argv[1])
        ancestors = [Path(*target.parts[: index + 1]) for index in range(1, len(target.parts))]
        report = {
            "uid": os.getuid(),
            "root_owner": os.stat("/").st_uid,
            "ancestor_owners": [path.stat().st_uid for path in ancestors],
        }
        try:
            os.close(ledger._open_anchored_directory(target))
            report["opened"] = True
        except ledger.DispatchLedgerError as exc:
            report["opened"] = False
            report["error"] = str(exc)
        print(json.dumps(report))
        """
    )
    source_root = Path(module.__file__).resolve().parents[2]
    proc = subprocess.run(
        [
            "bwrap", "--unshare-user", "--ro-bind", "/", "/",
            "--bind", str(tmp_path), str(tmp_path),
            "--dev", "/dev", "--proc", "/proc", "--clearenv",
            "--setenv", "PATH", "/usr/bin:/bin",
            "--setenv", "PYTHONPATH", str(source_root),
            "--setenv", "PYTHONDONTWRITEBYTECODE", "1",
            "--setenv", "LOOPZERO_ENV_PREFIX", os.environ.get("LOOPZERO_ENV_PREFIX", "LOOPZERO"),
            "--chdir", str(tmp_path),
            "--", sys.executable, "-c", script, str(segments),
        ],
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, proc.stderr
    report = json.loads(proc.stdout)
    assert report["uid"] == os.getuid()
    if report["root_owner"] == os.getuid() or report["root_owner"] not in report["ancestor_owners"]:
        pytest.skip("no ancestor is rendered with the mount-root owner in this namespace")
    assert report["root_owner"] != 0
    assert report["ancestor_owners"][-1] == os.getuid()
    assert report["opened"] is True, report
