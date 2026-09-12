"""Focused tests for the Guardian untrusted-dispatch promotion boundary."""

import hashlib
import importlib.util
import json
import os
import stat
import sys
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from loopzero.guardian import deposit as module
from loopzero.kernel.authority import COORDINATOR_AUTHORITY_SCHEME, TerminalAuthority
from loopzero.kernel.authority_projection import coordinator_ledger_prefix
from loopzero.kernel.authority_store import create_coordinator_authority, load_authority_records
from loopzero.kernel.gitscope import DispatchError, immutable_task_contract, task_contract_hash
from loopzero.kernel.policy import DISPATCH_POLICY_VERSION, TELEMETRY_SCHEMA_VERSION
from loopzero.review.authority import authenticated_retry_outcomes, authenticated_verdicts


def _append_rows(repo: Path, rows: list[dict[str, object]]) -> None:
    target = repo / ".audit" / "dispatch" / "2026-09-12.jsonl"
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("a", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, sort_keys=True) + "\n")


def ensure_coordinator_authority_cutover(repo: Path) -> None:
    records = load_authority_records(repo, 30)
    if any(row.get("type") == "coordinator-authority-cutover" for row in records):
        return
    coordinator = create_coordinator_authority()
    row = coordinator.seal(
        {
            "schema_version": TELEMETRY_SCHEMA_VERSION,
            "policy_version": DISPATCH_POLICY_VERSION,
            "ts": datetime.now(UTC).isoformat(),
            "type": "coordinator-authority-cutover",
            "status": "active",
            "ledger_prefix": coordinator_ledger_prefix(records),
        },
        authority_kind="coordinator",
    )
    _append_rows(repo, [row])


def append_authoritative_record(repo, record, *, authority, authority_kind, **kwargs):
    del kwargs
    envelope = {
        "schema_version": TELEMETRY_SCHEMA_VERSION,
        "policy_version": DISPATCH_POLICY_VERSION,
        "ts": datetime.now(UTC).isoformat(),
        **record,
    }
    _append_rows(repo, [authority.seal(envelope, authority_kind=authority_kind)])


agent_dispatch = SimpleNamespace(
    COORDINATOR_AUTHORITY_SCHEME=COORDINATOR_AUTHORITY_SCHEME,
    DISPATCH_POLICY_VERSION=DISPATCH_POLICY_VERSION,
    DispatchError=DispatchError,
    TELEMETRY_SCHEMA_VERSION=TELEMETRY_SCHEMA_VERSION,
    TerminalAuthority=TerminalAuthority,
    append_authoritative_record=append_authoritative_record,
    append_imported_authority_records=_append_rows,
    authenticated_retry_outcomes=authenticated_retry_outcomes,
    authenticated_verdicts=authenticated_verdicts,
    coordinator_ledger_prefix=coordinator_ledger_prefix,
    create_coordinator_authority=create_coordinator_authority,
    ensure_coordinator_authority_cutover=ensure_coordinator_authority_cutover,
    immutable_task_contract=immutable_task_contract,
    load_authority_records=load_authority_records,
    task_contract_hash=task_contract_hash,
)


@pytest.fixture
def isolated_ptrace_scope_path(tmp_path: Path) -> Path:
    path = tmp_path / "ptrace_scope"
    path.write_text("1\n", encoding="ascii")
    return path


@pytest.fixture(autouse=True)
def isolated_coordinator_authority(
    monkeypatch: pytest.MonkeyPatch,
    isolated_ptrace_scope_path: Path,
    tmp_path: Path,
) -> None:
    monkeypatch.setitem(sys.modules, "agent_dispatch", agent_dispatch)
    authority_globals = (
        agent_dispatch.TerminalAuthority.generate.__func__.__globals__,
        agent_dispatch.create_coordinator_authority.__globals__[
            "CoordinatorAuthority"
        ].from_local_state.__func__.__globals__,
    )
    for globals_ in {id(item): item for item in authority_globals}.values():
        monkeypatch.setitem(globals_, "PTRACE_SCOPE_PATH", isolated_ptrace_scope_path)
        monkeypatch.setitem(
            globals_,
            "_coordinator_state_directory",
            lambda: tmp_path / "coordinator-authority",
        )
    module.configure(
        append_authority_records=lambda repo, rows: (
            agent_dispatch.append_imported_authority_records(repo, rows)
        )
    )


def write_completed_deposit(
    deposit,
    *,
    task_id: str,
    run_id: str = "sr_" + "a" * 32,
    worktree: Path | None = None,
) -> tuple[dict[str, object], dict[str, object]]:
    worktree = (worktree or deposit.canonical_audit_root.parent).resolve()
    task = {
        "task_id": task_id,
        "work_unit_id": task_id,
        "objective": "Review the exact Guardian repair.",
        "role": "guardian-review",
        "size_points": 1,
        "work_kind": "review",
        "category": "quality-maintenance",
    }
    module.bind_launch(
        deposit,
        task=task,
        run_id=run_id,
        worktree=worktree,
        read_only=True,
    )
    agent_dispatch.ensure_coordinator_authority_cutover(
        deposit.canonical_audit_root.parent
    )
    dispatcher = agent_dispatch.TerminalAuthority.generate()
    contract = agent_dispatch.immutable_task_contract(task)
    governed = {
        "ts": "2026-08-31T12:00:00+00:00",
        "schema_version": agent_dispatch.TELEMETRY_SCHEMA_VERSION,
        "policy_version": agent_dispatch.DISPATCH_POLICY_VERSION,
        "run_id": run_id,
        "task_id": task_id,
        "work_unit_id": task_id,
        "attempt_index": 0,
        "unit_attempt_number": 1,
        "worktree": str(worktree),
        "read_only": True,
        "work_kind": "review",
        "category": "quality-maintenance",
        "role": "guardian-review",
        "alias": "sol",
        "effective_alias": "sol",
        "effort": "high",
        "worker_identity": "cursor:gpt-5.6",
        "task_contract": contract,
        "task_contract_hash": agent_dispatch.task_contract_hash(task),
    }
    sandbox_coordinator = agent_dispatch.TerminalAuthority.generate()
    start = sandbox_coordinator.seal(
        {
            **governed,
            "type": "attempt-start",
            "registration_authority_version": 1,
            "terminal_authority": dispatcher.registration(),
        },
        authority_kind="coordinator",
        include_public_key=True,
    )
    result = {"task_id": task_id, "status": "completed", "summary": "done"}
    result_path = deposit.result_path
    result_path.parent.mkdir(parents=True, exist_ok=True)
    result_path.write_text(json.dumps(result) + "\n", encoding="utf-8")
    terminal = dispatcher.seal(
        {
            **governed,
            "type": "attempt-terminal",
            "status": "completed",
            "result_artifact": f".audit/dispatch/results/{task_id}.json",
            "result_sha256": hashlib.sha256(result_path.read_bytes()).hexdigest(),
        },
        authority_kind="dispatcher",
    )
    dated = deposit.audit_root / "dispatch" / "2026-08-12.jsonl"
    with dated.open("a", encoding="utf-8") as handle:
        for row in (start, terminal):
            handle.write(json.dumps(row, sort_keys=True) + "\n")
    return start, terminal


def test_promotes_only_task_bound_dispatch_evidence(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    state = tmp_path / "state"
    dispatch = repo / ".audit" / "dispatch"
    dispatch.mkdir(parents=True)
    (dispatch / "2026-08-11.jsonl").write_text(
        json.dumps({"type": "attempt-terminal", "task_id": "older"}) + "\n",
        encoding="utf-8",
    )
    task_id = "guardian-impl-aaaaaaaaaaaaaaaaaaaa"
    deposit = module.prepare(repo=repo, state_dir=state, task_id=task_id)
    write_completed_deposit(deposit, task_id=task_id)

    promoted = module.promote(deposit)

    assert promoted == repo / ".audit" / "dispatch" / "results" / f"{task_id}.json"
    rows = [
        json.loads(line)
        for path in dispatch.glob("*.jsonl")
        for line in path.read_text(encoding="utf-8").splitlines()
    ]
    assert sum(row.get("task_id") == task_id for row in rows) == 2
    promoted_start = next(
        row
        for row in rows
        if row.get("task_id") == task_id and row.get("type") == "attempt-start"
    )
    assert (
        promoted_start["terminal_authority_proof"]["scheme"]
        == agent_dispatch.COORDINATOR_AUTHORITY_SCHEME
    )
    records = agent_dispatch.load_authority_records(repo, 30)
    assert [
        row["task_id"] for row in agent_dispatch.authenticated_retry_outcomes(records)
    ] == [task_id]

    terminal = agent_dispatch.authenticated_retry_outcomes(records)[0]
    verdict = {
        "type": "verdict",
        "run_id": terminal["run_id"],
        "task_id": task_id,
        "verdict": "pass",
        "target_worker_identity": terminal["worker_identity"],
        "verifier_identity": "reviewer:root",
        "verifier_alias": "human",
    }
    agent_dispatch.append_authoritative_record(
        repo,
        verdict,
        authority=agent_dispatch.create_coordinator_authority(),
        authority_kind="coordinator",
        cutover_established=True,
    )
    records = agent_dispatch.load_authority_records(repo, 30)
    assert agent_dispatch.authenticated_verdicts(records)[task_id]["verdict"] == "pass"

    module.promote(deposit)
    rows = [
        json.loads(line)
        for path in dispatch.glob("*.jsonl")
        for line in path.read_text(encoding="utf-8").splitlines()
    ]
    assert sum(row.get("task_id") == task_id for row in rows) == 3


def test_rejects_foreign_or_rewritten_dispatch_authority(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    state = tmp_path / "state"
    dispatch = repo / ".audit" / "dispatch"
    dispatch.mkdir(parents=True)
    history = dispatch / "2026-08-12.jsonl"
    history.write_text(json.dumps({"type": "attempt", "task_id": "older"}) + "\n")
    task_id = "guardian-review-aaaaaaaaaaaaaaaaaaaa"
    deposit = module.prepare(repo=repo, state_dir=state, task_id=task_id)
    write_completed_deposit(deposit, task_id=task_id)
    sandbox_history = deposit.audit_root / "dispatch" / "2026-08-12.jsonl"
    sandbox_history.write_text(
        json.dumps({"type": "attempt", "task_id": "forged"}) + "\n",
        encoding="utf-8",
    )

    with pytest.raises(module.AuditDepositError, match="rewrote governed history"):
        module.promote(deposit)


def test_rejects_task_bound_but_unsupported_authority_family(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    state = tmp_path / "state"
    dispatch = repo / ".audit" / "dispatch"
    dispatch.mkdir(parents=True)
    task_id = "guardian-review-unsupported-family"
    deposit = module.prepare(repo=repo, state_dir=state, task_id=task_id)
    write_completed_deposit(deposit, task_id=task_id)
    sandbox_history = deposit.audit_root / "dispatch" / "2026-08-12.jsonl"
    with sandbox_history.open("a", encoding="utf-8") as handle:
        handle.write(
            json.dumps(
                {
                    "type": "route",
                    "task_id": task_id,
                    "kept": True,
                    "work_unit_id": "poisoned-future-contract",
                }
            )
            + "\n"
        )

    with pytest.raises(module.AuditDepositError, match="unsupported authority family"):
        module.promote(deposit)


def test_rejects_start_that_does_not_match_trusted_launch_binding(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    state = tmp_path / "state"
    task_id = "guardian-review-mismatched-launch"
    deposit = module.prepare(repo=repo, state_dir=state, task_id=task_id)
    write_completed_deposit(deposit, task_id=task_id)
    dated = deposit.audit_root / "dispatch" / "2026-08-12.jsonl"
    rows = [json.loads(line) for line in dated.read_text().splitlines()]
    rows[0]["worktree"] = str(tmp_path / "forged-worktree")
    dated.write_text(
        "".join(json.dumps(row, sort_keys=True) + "\n" for row in rows),
        encoding="utf-8",
    )

    with pytest.raises(module.AuditDepositError, match="start binding is invalid"):
        module.promote(deposit)


def test_rejects_terminal_with_invalid_dispatcher_signature(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    state = tmp_path / "state"
    task_id = "guardian-review-forged-terminal"
    deposit = module.prepare(repo=repo, state_dir=state, task_id=task_id)
    write_completed_deposit(deposit, task_id=task_id)
    dated = deposit.audit_root / "dispatch" / "2026-08-12.jsonl"
    rows = [json.loads(line) for line in dated.read_text().splitlines()]
    rows[1]["worker_identity"] = "cursor:forged"
    dated.write_text(
        "".join(json.dumps(row, sort_keys=True) + "\n" for row in rows),
        encoding="utf-8",
    )

    with pytest.raises(module.AuditDepositError, match="terminal authority is invalid"):
        module.promote(deposit)


def test_rejects_symlinked_dispatch_authority_without_reading_target(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    state = tmp_path / "state"
    task_id = "guardian-review-symlinked-authority"
    deposit = module.prepare(repo=repo, state_dir=state, task_id=task_id)
    target = tmp_path / "outside.jsonl"
    target.write_text(
        json.dumps(
            {
                "type": "attempt-terminal",
                "task_id": task_id,
                "status": "completed",
            }
        )
        + "\n",
        encoding="utf-8",
    )
    sandbox_authority = deposit.audit_root / "dispatch" / "host.jsonl"
    sandbox_authority.symlink_to(target)

    with pytest.raises(module.AuditDepositError, match="authority is unsafe"):
        module._deposited_records(deposit)


def test_rejects_dispatch_authority_above_aggregate_append_budget(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = tmp_path / "repo"
    state = tmp_path / "state"
    task_id = "guardian-review-oversized-authority"
    deposit = module.prepare(repo=repo, state_dir=state, task_id=task_id)
    row = (json.dumps({"type": "attempt-progress", "task_id": task_id}) + "\n").encode()
    monkeypatch.setattr(module, "MAX_DEPOSIT_APPEND_BYTES", len(row) * 2 - 1)
    sandbox_dispatch = deposit.audit_root / "dispatch"
    (sandbox_dispatch / "new-a.jsonl").write_bytes(row)
    (sandbox_dispatch / "new-b.jsonl").write_bytes(row)

    with pytest.raises(module.AuditDepositError, match="authority exceeds size budget"):
        module._deposited_records(deposit)


def test_rejects_deposit_tree_above_path_count_budget(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = tmp_path / "repo"
    state = tmp_path / "state"
    task_id = "guardian-review-too-many-paths"
    deposit = module.prepare(repo=repo, state_dir=state, task_id=task_id)
    monkeypatch.setattr(module, "MAX_DEPOSIT_PATHS", 2)
    sandbox_dispatch = deposit.audit_root / "dispatch"
    for index in range(3):
        (sandbox_dispatch / f"empty-{index}.txt").touch()

    with pytest.raises(module.AuditDepositError, match="path count budget"):
        module._deposited_records(deposit)


def test_rejects_deposit_tree_above_depth_budget(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = tmp_path / "repo"
    state = tmp_path / "state"
    task_id = "guardian-review-too-deep"
    deposit = module.prepare(repo=repo, state_dir=state, task_id=task_id)
    monkeypatch.setattr(module, "MAX_DEPOSIT_DEPTH", 1)
    nested = deposit.audit_root / "dispatch" / "one" / "two"
    nested.mkdir(parents=True)
    (nested / "empty.jsonl").touch()

    with pytest.raises(module.AuditDepositError, match="depth budget"):
        module._deposited_records(deposit)


def test_trims_oldest_observational_authority_above_record_count_budget(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = tmp_path / "repo"
    state = tmp_path / "state"
    task_id = "guardian-review-too-many-records"
    deposit = module.prepare(repo=repo, state_dir=state, task_id=task_id)
    monkeypatch.setattr(module, "MAX_DEPOSIT_RECORDS", 2)
    sandbox_history = deposit.audit_root / "dispatch" / "new.jsonl"
    sandbox_history.write_text(
        "".join(
            json.dumps({"type": "attempt-progress", "task_id": task_id}) + "\n"
            for _ in range(3)
        ),
        encoding="utf-8",
    )

    records = module._deposited_records(deposit)

    assert len(records) == 2
    assert all(record["type"] == "attempt-progress" for record in records)


def test_record_count_trimming_preserves_start_and_terminal_authority(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = tmp_path / "repo"
    state = tmp_path / "state"
    task_id = "guardian-review-preserved-terminal"
    deposit = module.prepare(repo=repo, state_dir=state, task_id=task_id)
    write_completed_deposit(deposit, task_id=task_id)
    sandbox_history = deposit.audit_root / "dispatch" / "2026-08-12.jsonl"
    start, terminal = (
        json.loads(line) for line in sandbox_history.read_text().splitlines()
    )
    progress = [
        {"type": "attempt-progress", "task_id": task_id, "sequence": sequence}
        for sequence in range(3)
    ]
    sandbox_history.write_text(
        "".join(
            json.dumps(record, sort_keys=True) + "\n"
            for record in [start, *progress, terminal]
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(module, "MAX_DEPOSIT_RECORDS", 2)

    records = module._deposited_records(deposit)

    assert [record["type"] for record in records] == [
        "attempt-start",
        "attempt-terminal",
    ]
    assert module.promote_completed(deposit) == deposit.canonical_result_path


def test_stages_canonical_result_before_batch_import_and_recovers_on_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = tmp_path / "repo"
    state = tmp_path / "state"
    task_id = "guardian-review-import-failure"
    deposit = module.prepare(repo=repo, state_dir=state, task_id=task_id)
    write_completed_deposit(deposit, task_id=task_id)
    imported_dispatch = sys.modules["agent_dispatch"]
    expected_result = deposit.result_path.read_bytes()
    original_import = imported_dispatch.append_imported_authority_records

    def reject_import(*_args, **_kwargs) -> None:
        assert deposit.canonical_result_path.read_bytes() == expected_result
        raise imported_dispatch.DispatchError("simulated import failure")

    monkeypatch.setattr(
        imported_dispatch, "append_imported_authority_records", reject_import
    )

    with pytest.raises(module.AuditDepositError, match="could not be promoted"):
        module.promote(deposit)
    assert deposit.canonical_result_path.read_bytes() == expected_result

    monkeypatch.setattr(
        imported_dispatch, "append_imported_authority_records", original_import
    )
    assert module.promote_completed(deposit) == deposit.canonical_result_path
    assert deposit.canonical_result_path.read_bytes() == expected_result


def test_rejects_canonical_result_conflict_before_authority_promotion(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = tmp_path / "repo"
    state = tmp_path / "state"
    task_id = "guardian-review-result-conflict"
    deposit = module.prepare(repo=repo, state_dir=state, task_id=task_id)
    write_completed_deposit(deposit, task_id=task_id)
    deposit.canonical_result_path.parent.mkdir(parents=True)
    deposit.canonical_result_path.write_text("conflict\n", encoding="utf-8")
    imported_dispatch = sys.modules["agent_dispatch"]

    def fail_if_promoted(*_args, **_kwargs) -> None:
        pytest.fail("authority promotion ran before canonical result validation")

    monkeypatch.setattr(
        imported_dispatch, "append_imported_authority_records", fail_if_promoted
    )

    with pytest.raises(
        module.AuditDepositError, match="canonical dispatch result conflicts"
    ):
        module.promote(deposit)


def test_retry_reestablishes_result_directory_durability_before_authority(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = tmp_path / "repo"
    state = tmp_path / "state"
    task_id = "guardian-review-result-fsync-retry"
    deposit = module.prepare(repo=repo, state_dir=state, task_id=task_id)
    write_completed_deposit(deposit, task_id=task_id)
    original_fsync = os.fsync
    directory_fsyncs = 0

    def fail_first_directory_fsync(descriptor: int) -> None:
        nonlocal directory_fsyncs
        if stat.S_ISDIR(os.fstat(descriptor).st_mode):
            directory_fsyncs += 1
            if directory_fsyncs == 1:
                raise OSError("simulated directory fsync failure")
        original_fsync(descriptor)

    monkeypatch.setattr(module.os, "fsync", fail_first_directory_fsync)

    with pytest.raises(module.AuditDepositError, match="could not be published"):
        module.promote(deposit)
    assert (
        deposit.canonical_result_path.read_bytes() == deposit.result_path.read_bytes()
    )
    records = agent_dispatch.load_authority_records(repo, 30)
    assert not agent_dispatch.authenticated_retry_outcomes(records)

    assert module.promote(deposit) == deposit.canonical_result_path
    assert directory_fsyncs == 2
    records = agent_dispatch.load_authority_records(repo, 30)
    assert [
        row["task_id"] for row in agent_dispatch.authenticated_retry_outcomes(records)
    ] == [task_id]


def test_rejects_symlinked_non_jsonl_baseline_without_reading_target(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    state = tmp_path / "state"
    dispatch_results = repo / ".audit" / "dispatch" / "results"
    dispatch_results.mkdir(parents=True)
    (dispatch_results / "baseline.json").write_text("{}\n", encoding="utf-8")
    task_id = "guardian-review-symlinked-result"
    deposit = module.prepare(repo=repo, state_dir=state, task_id=task_id)
    target = tmp_path / "outside.json"
    target.write_text("{}\n", encoding="utf-8")
    sandbox_result = deposit.audit_root / "dispatch" / "results" / "baseline.json"
    sandbox_result.unlink()
    sandbox_result.symlink_to(target)

    with pytest.raises(module.AuditDepositError, match="authority is unsafe"):
        module._deposited_records(deposit)


def test_rejects_oversized_non_jsonl_baseline_before_reading_it(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    state = tmp_path / "state"
    dispatch_results = repo / ".audit" / "dispatch" / "results"
    dispatch_results.mkdir(parents=True)
    (dispatch_results / "baseline.json").write_text("{}\n", encoding="utf-8")
    task_id = "guardian-review-oversized-result"
    deposit = module.prepare(repo=repo, state_dir=state, task_id=task_id)
    sandbox_result = deposit.audit_root / "dispatch" / "results" / "baseline.json"
    sandbox_result.write_bytes(b"x" * 1024)

    with pytest.raises(module.AuditDepositError, match="authority exceeds size budget"):
        module._deposited_records(deposit)


def test_promotes_the_exact_result_bytes_that_passed_provenance(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = tmp_path / "repo"
    state = tmp_path / "state"
    task_id = "guardian-review-stable-result"
    deposit = module.prepare(repo=repo, state_dir=state, task_id=task_id)
    write_completed_deposit(deposit, task_id=task_id)
    accepted = deposit.result_path.read_bytes()
    replacement = json.dumps(
        {"task_id": task_id, "status": "completed", "summary": "replaced"}
    ).encode()
    original_sha256 = module._sha256

    def replace_after_provenance(path: Path) -> str:
        digest = original_sha256(path)
        if path == deposit.result_path:
            path.write_bytes(replacement)
        return digest

    monkeypatch.setattr(module, "_sha256", replace_after_provenance)

    promoted = module.promote(deposit)

    assert promoted.read_bytes() == accepted
