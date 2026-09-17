from __future__ import annotations

import copy
import json
import subprocess
from pathlib import Path

import pytest

from loopzero.kernel import authority as signing
from loopzero.kernel import authority_projection as projection
from loopzero.kernel import authority_store
from loopzero.kernel import ledger_lifecycle
from loopzero.kernel.canonical import canonical_record_digest
from loopzero.kernel.policy import (
    DISPATCH_POLICY_VERSION,
    RUNTIME_CONTRACT_VERSION,
    TELEMETRY_SCHEMA_VERSION,
)
from loopzero.review import authority as review_authority


@pytest.fixture(autouse=True)
def isolated_authority(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(signing, "_coordinator_state_directory", lambda: tmp_path / "authority")
    review_authority.configure_kernel_seams()


def _governed(row: dict[str, object]) -> dict[str, object]:
    return {
        **row,
        "schema_version": TELEMETRY_SCHEMA_VERSION,
        "policy_version": DISPATCH_POLICY_VERSION,
        "runtime_contract_version": RUNTIME_CONTRACT_VERSION,
        "ts": "2026-09-17T00:00:00+00:00",
    }


def _history(worktree: Path) -> tuple[list[dict[str, object]], object]:
    coordinator = authority_store.create_coordinator_authority()
    cutover = coordinator.seal(
        _governed(
            {
                "type": "coordinator-authority-cutover",
                "status": "active",
                "ledger_prefix": projection.coordinator_ledger_prefix([]),
            }
        ),
        authority_kind="coordinator",
    )
    common = {
        "task_id": "inline-fix",
        "work_unit_id": "fix",
        "run_id": "sr_" + "1" * 32,
        "worktree": str(worktree),
        "task_contract_hash": "c" * 64,
    }
    route = coordinator.seal(
        _governed(
            {
                **common,
                "type": "route",
                "status": "planned",
                "kept": True,
                "read_only": False,
                "allowed_paths": ["tests/test_fix.py"],
                "acceptance_commands": ["pytest tests/test_fix.py -q"],
                "source_identity": {"head": "a" * 40},
                "terminal_authority_required": True,
            }
        ),
        authority_kind="coordinator",
    )
    failures = [
        coordinator.seal(
            _governed(
                {
                    **common,
                    "type": "inline",
                    "status": "acceptance-failure",
                    "attempt_index": index,
                    "unit_attempt_number": index + 1,
                    "scope_violations": None,
                    "acceptance": [
                        {"command": "pytest tests/test_fix.py -q", "exit_code": 2}
                    ],
                    "output_identity": {"head": "a" * 40},
                }
            ),
            authority_kind="coordinator",
        )
        for index in range(2)
    ]
    settlement = coordinator.seal(
        _governed(
            {
                **common,
                "type": "kept-postmerge-settlement-v1",
                "status": "postmerge-settled",
                "route_record_sha256": canonical_record_digest(route),
                "failed_inline_record_sha256s": [
                    canonical_record_digest(row) for row in failures
                ],
                "acceptance_commands": ["pytest tests/test_fix.py -q"],
                "original_source_identity": {"head": "a" * 40},
                "settled_source_identity": {
                    "head": "b" * 40,
                    "tree_sha": "d" * 40,
                },
                "source_proof": {
                    "repository": "github.com/example/repo",
                    "pr": 4451,
                    "original_head": "a" * 40,
                    "materialized_commit": "9" * 40,
                    "materialized_tree": "8" * 40,
                    "head_sha": "b" * 40,
                    "merge_commit": "e" * 40,
                    "head_tree": "d" * 40,
                    "merge_tree": "d" * 40,
                },
                "owner_reason": "The exact tree merged after contained acceptance lost pytest.",
            }
        ),
        authority_kind="coordinator",
    )
    return [cutover, route, *failures, settlement], coordinator


def test_authenticated_postmerge_settlement_closes_only_its_kept_scope(tmp_path: Path) -> None:
    records, _coordinator = _history(tmp_path)

    assert projection.authenticated_kept_postmerge_settlements(records) == [records[-1]]
    assert projection.open_write_units(records, worktree=tmp_path) == {}
    bundle = authority_store.authority_projection_bundle_v1(records)
    assert bundle.open_write_units == ()
    assert authority_store._retention_live_record_ids(records) == frozenset(
        id(record) for record in records[1:]
    )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("route_record_sha256", "0" * 64),
        ("failed_inline_record_sha256s", []),
        ("acceptance_commands", ["true"]),
        ("task_contract_hash", "0" * 64),
        ("worktree", "/other"),
        ("owner_reason", ""),
    ],
)
def test_tampered_or_mismatched_settlement_remains_open(
    tmp_path: Path, field: str, value: object
) -> None:
    records, coordinator = _history(tmp_path)
    payload = {
        key: copy.deepcopy(item)
        for key, item in records[-1].items()
        if key != "terminal_authority_proof"
    }
    payload[field] = value
    records[-1] = coordinator.seal(payload, authority_kind="coordinator")

    assert projection.authenticated_kept_postmerge_settlements(records) == []
    assert projection.open_write_units(records, worktree=tmp_path) == {
        "fix": ["tests/test_fix.py"]
    }


def test_unsigned_settlement_and_tree_mismatch_remain_open(tmp_path: Path) -> None:
    records, coordinator = _history(tmp_path)
    unsigned = {
        key: copy.deepcopy(value)
        for key, value in records[-1].items()
        if key != "terminal_authority_proof"
    }
    records[-1] = unsigned
    assert projection.open_write_units(records, worktree=tmp_path)

    unsigned["source_proof"] = {**unsigned["source_proof"], "merge_tree": "f" * 40}
    records[-1] = coordinator.seal(unsigned, authority_kind="coordinator")
    assert projection.open_write_units(records, worktree=tmp_path)

    unsigned["source_proof"] = {
        **unsigned["source_proof"],
        "merge_tree": "d" * 40,
        "original_head": "f" * 40,
    }
    records[-1] = coordinator.seal(unsigned, authority_kind="coordinator")
    assert projection.open_write_units(records, worktree=tmp_path)


def test_settlement_rejects_another_open_writer(tmp_path: Path) -> None:
    records, coordinator = _history(tmp_path)
    other = coordinator.seal(
        _governed(
            {
                "type": "route",
                "status": "planned",
                "kept": True,
                "read_only": False,
                "task_id": "inline-other",
                "work_unit_id": "other",
                "run_id": "sr_" + "1" * 32,
                "worktree": str(tmp_path),
                "task_contract_hash": "f" * 64,
                "allowed_paths": ["tests/other.py"],
                "acceptance_commands": ["true"],
            }
        ),
        authority_kind="coordinator",
    )
    records.insert(-1, other)

    assert projection.authenticated_kept_postmerge_settlements(records) == []
    assert set(projection.open_write_units(records, worktree=tmp_path)) == {"fix", "other"}


def test_settlement_rejects_an_active_dispatch_attempt(tmp_path: Path) -> None:
    records, _coordinator = _history(tmp_path)
    records.insert(
        -1,
        _governed(
            {
                "type": "attempt-start",
                "status": "running",
                "task_id": "concurrent-task",
                "work_unit_id": "concurrent-unit",
                "run_id": "sr_" + "1" * 32,
                "worktree": str(tmp_path),
                "attempt_index": 0,
                "read_only": False,
            }
        ),
    )

    assert projection.authenticated_kept_postmerge_settlements(records) == []
    assert projection.open_write_units(records, worktree=tmp_path)


def test_compaction_round_trip_preserves_lineage_and_closed_scope(tmp_path: Path) -> None:
    subprocess.run(["git", "init", "-b", "main"], cwd=tmp_path, check=True)
    subprocess.run(
        ["git", "config", "user.email", "test@example.com"], cwd=tmp_path, check=True
    )
    subprocess.run(["git", "config", "user.name", "Test"], cwd=tmp_path, check=True)
    subprocess.run(
        ["git", "remote", "add", "origin", "https://github.com/example/repo.git"],
        cwd=tmp_path,
        check=True,
    )
    (tmp_path / "README.md").write_text("test\n")
    subprocess.run(["git", "add", "README.md"], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-m", "test"], cwd=tmp_path, check=True)
    records, _coordinator = _history(tmp_path)
    failures = {
        canonical_record_digest(record)
        for record in records
        if record.get("type") == "inline"
    }
    stream = tmp_path / ".audit/dispatch/2026-09-17.jsonl"
    stream.parent.mkdir(parents=True)
    stream.write_text("".join(json.dumps(row) + "\n" for row in records))
    (tmp_path / ".git/info/exclude").write_text(".audit/\n")

    ledger_lifecycle.compact_authority_ledger(tmp_path)
    reloaded = authority_store.load_authority_records(tmp_path, 3650)

    assert isinstance(reloaded, projection.AuthorityRecordView)
    assert len(projection.authenticated_kept_postmerge_settlements(reloaded)) == 1
    assert failures <= {
        canonical_record_digest(record)
        for record in reloaded
        if record.get("type") == "inline"
    }
    assert projection.open_write_units(reloaded, worktree=tmp_path) == {}
