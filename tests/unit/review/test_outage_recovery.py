"""Prospective recovery requires protected attempt evidence, never caller claims."""

import subprocess
from types import SimpleNamespace

import pytest

from loopzero.kernel import (
    authority as signing,
)
from loopzero.kernel import (
    authority_projection as projection,
)
from loopzero.kernel import (
    authority_store,
    patch_identity,
    review_state,
    worktree_lease,
)
from loopzero.kernel.canonical import canonical_record_digest
from loopzero.kernel.gitscope import DispatchError, task_contract_hash
from loopzero.review import admission, chain
from loopzero.review.outage_recovery import (
    outage_recovery_terminal_matches,
    prepare_outage_recovery,
    validate_outage_recovery,
    validate_outage_recovery_start,
)
from loopzero.runners.contract import ReviewOutcome


@pytest.fixture
def signed_outage(consumer, monkeypatch, tmp_path, isolated_ptrace_scope_path):
    monkeypatch.setattr(signing, "PTRACE_SCOPE_PATH", isolated_ptrace_scope_path)
    monkeypatch.setattr(
        signing, "_coordinator_state_directory", lambda: tmp_path / "signer"
    )
    coordinator = authority_store.create_coordinator_authority()

    def seal(row):
        return coordinator.seal(row, authority_kind="coordinator")

    def git(*args):
        return subprocess.check_output(
            [
                "git",
                "-C",
                str(consumer),
                "-c",
                "user.name=probe",
                "-c",
                "user.email=probe@example.invalid",
                *args,
            ],
            text=True,
        ).strip()

    base = git("rev-parse", "HEAD")
    (consumer / "owned.py").write_text("VALUE = 1\n")
    git("add", "owned.py")
    git("-c", "core.hooksPath=/dev/null", "commit", "-qm", "candidate")
    source = worktree_lease.source_identity(consumer)
    patch = patch_identity.capture_patch_identity(
        consumer, candidate_sha=source["head"], base_ref=base
    )
    rows = [
        seal(
            {
                "type": "coordinator-authority-cutover",
                "status": "active",
                "ledger_prefix": projection.coordinator_ledger_prefix([]),
            }
        )
    ]
    common = {
        "schema_version": projection.TELEMETRY_SCHEMA_VERSION,
        "policy_version": next(iter(projection.COMPATIBLE_DISPATCH_POLICY_VERSIONS)),
        "run_id": "sr_" + "1" * 32,
        "worktree": str(consumer),
    }

    def attempt(fields):
        dispatcher = signing.TerminalAuthority.generate()
        rows.append(
            seal(
                {
                    **common,
                    **fields,
                    "type": "attempt-start",
                    "registration_authority_version": 1,
                    "terminal_authority": dispatcher.registration(),
                }
            )
        )
        terminal = dispatcher.seal(
            {**common, **fields, "type": "attempt-terminal"},
            authority_kind="dispatcher",
        )
        rows.append(terminal)
        assert id(terminal) in projection._authenticated_attempt_terminal_ids(rows)
        return terminal

    def rewrite_terminal(task_id, **changes):
        terminal = next(
            r
            for r in rows
            if r.get("type") == "attempt-terminal" and r.get("task_id") == task_id
        )
        old_digest = canonical_record_digest(terminal)
        dispatcher = signing.TerminalAuthority.generate()
        start_index = next(
            i
            for i, r in enumerate(rows)
            if r.get("type") == "attempt-start" and r.get("task_id") == task_id
        )
        start = {
            k: v
            for k, v in rows[start_index].items()
            if k != "terminal_authority_proof"
        }
        start["terminal_authority"] = dispatcher.registration()
        rows[start_index] = seal(start)
        updated = dispatcher.seal(
            {
                **{
                    k: v for k, v in terminal.items() if k != "terminal_authority_proof"
                },
                **changes,
            },
            authority_kind="dispatcher",
        )
        rows[rows.index(terminal)] = updated
        for i, row in enumerate(rows):
            if (
                row.get("type") == "review-slot-settlement-v1"
                and row.get("terminal_ref") == old_digest
            ):
                values = {
                    k: v for k, v in row.items() if k != "terminal_authority_proof"
                }
                values["terminal_ref"] = canonical_record_digest(updated)
                values["settlement_id"] = review_state._settlement_id(
                    values["reservation_id"],
                    ReviewOutcome(values["outcome"]),
                    values["terminal_ref"],
                )
                rows[i] = seal(values)
        return updated

    executor = attempt(
        {
            "task_id": "implement",
            "work_unit_id": "implement",
            "attempt_index": 0,
            "read_only": False,
            "work_kind": "implementation",
            "status": "completed",
            "output_identity": source,
            "engine": "codex",
            "runtime_effective_model": "builder-model",
            "worker_identity": "codex:builder-model",
        }
    )
    contract = {
        "task_id": "review-1",
        "work_unit_id": "review-unit",
        "root_work_unit_id": "review-unit",
        "work_kind": "review",
        "review_intent": "delivery-code-review",
        "read_only": True,
        "reviewed_executor_terminal_ref": canonical_record_digest(executor),
    }
    with authority_store.authority_ledger_lock(consumer):
        chain.configure(
            SimpleNamespace(
                required_sections=("code",), max_reviews_per_pr=1, max_delta_reviews=1
            )
        )
        for index in range(2):
            task = {
                **contract,
                "task_id": f"review-{index}",
                "run_id": common["run_id"],
                "idempotency_key": f"ordinary-{index}",
            }
            admitted = admission.admit_review(
                consumer,
                rows,
                repository_binding=authority_store._authority_repository_binding(
                    consumer
                ),
                task=task,
                current_source_identity=source,
                current_tree_sha=patch["candidate_tree_sha"],
                patch_identity=patch,
                required_sections=("code",),
                equivalence_proof=None,
                format_only_proof=None,
                requested="review",
                changed_paths=None,
                security_trigger_paths=(),
            )
            assert isinstance(admitted, admission.Reserved), admitted
            rows.extend(seal(dict(row)) for row in admitted.records_to_append)
            terminal = attempt(
                {
                    **contract,
                    "task_id": task["task_id"],
                    "attempt_index": 0,
                    "task_contract": contract,
                    "task_contract_hash": task_contract_hash(contract),
                    "source_identity": source,
                    "status": "infrastructure-failure",
                    "runtime_terminal_reason": "process-exit",
                    "failure_class": "engine-output",
                    "deposit_state": "none",
                    "model_output_seen": False,
                    "engine": "claude",
                    "runtime_effective_model": "failed-model",
                    "worker_identity": "claude:failed-model",
                    "snapshot_tree_sha": admitted.generation.tree,
                    "review_generation_id": admitted.generation.generation_id,
                    "review_lineage_id": admitted.generation.lineage_id,
                    "review_family": "delivery",
                    "review_slot_kind": "primary",
                    "unit_attempt_number": index + 1,
                    "review_reservation_id": admitted.slot.reservation_id,
                }
            )
            settlement = review_state.settle_review_slot(
                consumer,
                rows,
                reservation=admitted.slot,
                outcome=ReviewOutcome.RELEASED,
                terminal_ref=terminal,
            )
            rows.append(seal(settlement.to_dict()))
    route = {"engine": "codex", "model": "independent-model", "effort": "high"}
    task = {
        **contract,
        "task_id": "review-1",
        "run_id": common["run_id"],
        "task_contract": contract,
        "idempotency_key": "recovery",
        "unit_attempt_number": 3,
    }
    kwargs = dict(
        task_id="review-1",
        run_id=common["run_id"],
        source_identity=source,
        generation_id=admitted.generation.generation_id,
        family="delivery",
        slot_kind="primary",
        replacement_route=route,
        reason="owner authorizes one independent replacement",
    )
    validate_kwargs = dict(
        task=task,
        source_identity=source,
        run_id=common["run_id"],
        generation_id=admitted.generation.generation_id,
        family="delivery",
        slot_kind="primary",
        route=route,
        attempt_index=1,
    )
    return dict(
        repository=consumer,
        rows=rows,
        task=task,
        source=source,
        generation=admitted.generation,
        seal=seal,
        route=route,
        kwargs=kwargs,
        validate_kwargs=validate_kwargs,
        patch=patch,
        executor=executor,
        attempt=attempt,
        rewrite_terminal=rewrite_terminal,
    )


def test_two_signed_empty_failures_admit_exactly_one_owner_grant(signed_outage):
    f = signed_outage
    with authority_store.authority_ledger_lock(f["repository"]):
        grant = f["seal"](
            prepare_outage_recovery(f["repository"], f["rows"], **f["kwargs"])
        )
        f["rows"].append(grant)
        assert (
            validate_outage_recovery(
                f["repository"],
                f["rows"],
                authorization_sha256=canonical_record_digest(grant),
                **f["validate_kwargs"],
            )
            is grant
        )
        with pytest.raises(DispatchError, match="already has"):
            prepare_outage_recovery(f["repository"], f["rows"], **f["kwargs"])


@pytest.mark.parametrize("model", ["builder-model", "failed-model", "auto"])
def test_original_executor_and_failed_reviewer_cannot_be_replacement(
    signed_outage, model
):
    f = signed_outage
    kwargs = {**f["kwargs"], "replacement_route": {**f["route"], "model": model}}
    with (
        authority_store.authority_ledger_lock(f["repository"]),
        pytest.raises(DispatchError),
    ):
        prepare_outage_recovery(f["repository"], f["rows"], **kwargs)


@pytest.mark.parametrize(
    "changes",
    [
        {"model_output_seen": None},
        {"model_output_seen": True},
        {"model_output_seen": 0},
        {"final_output": "partial review"},
        {"result_artifact": ".audit/result.json"},
        {"verification_verdict": "inconclusive"},
        {"runtime_effective_model": None},
        {"deposit_state": "unverified-partial"},
        {"runtime_terminal_reason": "model-result"},
        {"runtime_usage": {"output_tokens": 1}},
        {"model_result_reason": "schema-invalid"},
        {"runtime_fallback_from": "prior-attempt"},
        {"fallback_used": True},
        {"scope_violations": ["owned.py"]},
        {"task_contract": {"untrusted": "changed but old digest retained"}},
    ],
)
def test_signed_legacy_partial_or_semantic_receipt_fails_closed(signed_outage, changes):
    f = signed_outage
    f["rewrite_terminal"]("review-1", **changes)
    with (
        authority_store.authority_ledger_lock(f["repository"]),
        pytest.raises(DispatchError),
    ):
        prepare_outage_recovery(f["repository"], f["rows"], **f["kwargs"])


def test_grant_cannot_follow_changed_source(signed_outage):
    f = signed_outage
    with authority_store.authority_ledger_lock(f["repository"]):
        grant = f["seal"](
            prepare_outage_recovery(f["repository"], f["rows"], **f["kwargs"])
        )
        f["rows"].append(grant)
        (f["repository"] / "owned.py").write_text("VALUE = 2\n")
        with pytest.raises(DispatchError, match="source changed"):
            validate_outage_recovery(
                f["repository"],
                f["rows"],
                authorization_sha256=canonical_record_digest(grant),
                **f["validate_kwargs"],
            )


def test_renamed_task_or_new_attempt_cannot_spend_grant(signed_outage):
    f = signed_outage
    with authority_store.authority_ledger_lock(f["repository"]):
        grant = f["seal"](
            prepare_outage_recovery(f["repository"], f["rows"], **f["kwargs"])
        )
        f["rows"].append(grant)
        for changed in (
            {"task": {**f["task"], "task_id": "renamed"}},
            {"attempt_index": 2},
        ):
            with pytest.raises(DispatchError):
                validate_outage_recovery(
                    f["repository"],
                    f["rows"],
                    authorization_sha256=canonical_record_digest(grant),
                    **{**f["validate_kwargs"], **changed},
                )


def test_deleted_terminal_marker_does_not_evade_named_model_guard(signed_outage):
    f = signed_outage
    with authority_store.authority_ledger_lock(f["repository"]):
        grant = f["seal"](
            prepare_outage_recovery(f["repository"], f["rows"], **f["kwargs"])
        )
        digest = canonical_record_digest(grant)
        f["rows"].append(grant)
        reservation = review_state.ReviewSlotReservation(
            generation_id=f["generation"].generation_id,
            family="delivery",
            slot_kind="primary",
            task_id="review-1",
            idempotency_key="outage:" + digest,
            reservation_id=review_state._reservation_id(
                f["generation"].generation_id,
                "delivery",
                "primary",
                "review-1",
                "outage:" + digest,
            ),
        )
        f["rows"].append(f["seal"](reservation.to_dict()))
        terminal = f["attempt"](
            {
                **f["task"],
                "attempt_index": 1,
                "source_identity": f["source"],
                "task_contract_hash": task_contract_hash(f["task"]["task_contract"]),
                "status": "completed",
                "review_reservation_id": reservation.reservation_id,
                "review_generation_id": f["generation"].generation_id,
                "snapshot_tree_sha": f["generation"].tree,
                "engine": "codex",
                "model": "independent-model",
                "effort": "high",
                "runtime_effective_model": "builder-model",
                "worker_identity": "codex:builder-model",
            }
        )
        assert not outage_recovery_terminal_matches(f["rows"], terminal)
        omitted_reservation = {
            k: v for k, v in terminal.items() if k != "review_reservation_id"
        }
        assert not outage_recovery_terminal_matches(f["rows"], omitted_reservation)


def test_generation_repository_binding_is_not_caller_selectable(signed_outage):
    f = signed_outage
    other = f["repository"].parent / "other"
    other.mkdir()
    subprocess.run(["git", "init", "-q", str(other)], check=True)
    with authority_store.authority_ledger_lock(other), pytest.raises(DispatchError):
        prepare_outage_recovery(other, f["rows"], **f["kwargs"])


def test_older_failed_receipt_contract_must_also_be_authentic(signed_outage):
    f = signed_outage
    f["rewrite_terminal"]("review-0", task_contract={"changed": True})
    with (
        authority_store.authority_ledger_lock(f["repository"]),
        pytest.raises(DispatchError),
    ):
        prepare_outage_recovery(f["repository"], f["rows"], **f["kwargs"])


def _grant_and_reservation(f):
    grant = f["seal"](
        prepare_outage_recovery(f["repository"], f["rows"], **f["kwargs"])
    )
    digest = canonical_record_digest(grant)
    f["rows"].append(grant)
    reservation = review_state.ReviewSlotReservation(
        generation_id=f["generation"].generation_id,
        family="delivery",
        slot_kind="primary",
        task_id="review-1",
        idempotency_key="outage:" + digest,
        reservation_id=review_state._reservation_id(
            f["generation"].generation_id,
            "delivery",
            "primary",
            "review-1",
            "outage:" + digest,
        ),
    )
    f["rows"].append(f["seal"](reservation.to_dict()))
    return grant, reservation


def test_start_permission_is_one_use_and_spent_reservation_is_not_refunded(
    signed_outage,
):
    f = signed_outage
    with authority_store.authority_ledger_lock(f["repository"]):
        grant, reservation = _grant_and_reservation(f)
        digest = canonical_record_digest(grant)
        kwargs = dict(
            authorization_sha256=digest,
            reservation_id=reservation.reservation_id,
            **f["validate_kwargs"],
        )
        assert (
            validate_outage_recovery_start(f["repository"], f["rows"], **kwargs)
            is grant
        )
        with pytest.raises(DispatchError):
            validate_outage_recovery(
                f["repository"],
                f["rows"],
                authorization_sha256=digest,
                **f["validate_kwargs"],
            )
        terminal = f["attempt"](
            {
                **f["task"],
                "attempt_index": 1,
                "status": "infrastructure-failure",
                "failure_class": "engine-output",
                "runtime_terminal_reason": "process-exit",
                "source_identity": f["source"],
                "review_reservation_id": reservation.reservation_id,
                "review_generation_id": f["generation"].generation_id,
                "snapshot_tree_sha": f["generation"].tree,
            }
        )
        # A registered start is enough to spend launch permission, even before
        # a terminal is available. It is not refunded by another empty failure.
        f["rows"].remove(terminal)
        with pytest.raises(DispatchError):
            validate_outage_recovery_start(f["repository"], f["rows"], **kwargs)
        f["rows"].append(terminal)
        with pytest.raises(DispatchError):
            validate_outage_recovery_start(f["repository"], f["rows"], **kwargs)
        with pytest.raises(DispatchError):
            prepare_outage_recovery(f["repository"], f["rows"], **f["kwargs"])


@pytest.mark.parametrize(
    "change",
    [
        {"run_id": "sr_" + "2" * 32},
        {"route": {"engine": "codex", "model": "another-model", "effort": "high"}},
        {"attempt_index": True},
    ],
)
def test_authorization_cannot_be_rebound(signed_outage, change):
    f = signed_outage
    with authority_store.authority_ledger_lock(f["repository"]):
        grant = f["seal"](
            prepare_outage_recovery(f["repository"], f["rows"], **f["kwargs"])
        )
        f["rows"].append(grant)
        with pytest.raises(DispatchError):
            validate_outage_recovery(
                f["repository"],
                f["rows"],
                authorization_sha256=canonical_record_digest(grant),
                **{**f["validate_kwargs"], **change},
            )
        for changed_task in (
            {
                **f["task"],
                "task_contract": {**f["task"]["task_contract"], "scope": "other"},
            },
            {**f["task"], "evidence_manifest": [{"sha256": "b" * 64}]},
        ):
            with pytest.raises(DispatchError):
                validate_outage_recovery(
                    f["repository"],
                    f["rows"],
                    authorization_sha256=canonical_record_digest(grant),
                    **{**f["validate_kwargs"], "task": changed_task},
                )


def test_unsigned_grant_with_correct_digest_cannot_authorize(signed_outage):
    f = signed_outage
    with authority_store.authority_ledger_lock(f["repository"]):
        grant = prepare_outage_recovery(f["repository"], f["rows"], **f["kwargs"])
        f["rows"].append(grant)
        with pytest.raises(DispatchError, match="authenticated"):
            validate_outage_recovery(
                f["repository"],
                f["rows"],
                authorization_sha256=canonical_record_digest(grant),
                **f["validate_kwargs"],
            )


def test_unsigned_empty_failure_cannot_authorize_recovery(tmp_path):
    with authority_store.authority_ledger_lock(tmp_path):
        with pytest.raises(DispatchError):
            prepare_outage_recovery(
                tmp_path,
                [{"type": "attempt-terminal", "model_output_seen": False}],
                task_id="failed",
                run_id="sr_" + "1" * 32,
                source_identity={"head": "a" * 40},
                generation_id="missing",
                family="delivery",
                slot_kind="primary",
                replacement_route={
                    "engine": "other",
                    "model": "named",
                    "effort": "high",
                },
                reason="owner explicitly authorizes one replacement",
            )
