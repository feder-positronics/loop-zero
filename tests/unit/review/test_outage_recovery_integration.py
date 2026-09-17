"""Composed source/consumer boundaries using real signed attempt history."""

import json

import pytest

from loopzero.kernel import authority_projection as projection
from loopzero.kernel import authority_store, seams
from loopzero.kernel.canonical import canonical_record_digest
from loopzero.kernel.gitscope import DispatchError, ReviewSnapshot
from loopzero.review import admission, authority, provisional_findings, routing
from loopzero.review.outage_recovery import prepare_outage_recovery

from .test_outage_recovery import signed_outage as _signed_outage

signed_outage = _signed_outage


def grant(f):
    row = f["seal"](prepare_outage_recovery(f["repository"], f["rows"], **f["kwargs"]))
    f["rows"].append(row)
    return canonical_record_digest(row)


def admit(f, authorization=None):
    task = dict(f["task"])
    task["idempotency_key"] = "outage:" + authorization if authorization else "third"
    return admission.admit_review(
        f["repository"],
        f["rows"],
        repository_binding=authority_store.authority_repository_binding(
            f["repository"]
        ),
        task=task,
        current_source_identity=f["source"],
        current_tree_sha=f["patch"]["candidate_tree_sha"],
        patch_identity=f["patch"],
        required_sections=("code",),
        equivalence_proof=None,
        format_only_proof=None,
        requested="review",
        changed_paths=None,
        security_trigger_paths=(),
        attempt_index=1,
        outage_authorization_sha256=authorization,
        replacement_route=f["route"] if authorization else None,
    )


def test_two_ordinary_failures_keep_both_limits_and_one_exception(signed_outage):
    f = signed_outage
    with authority_store.authority_ledger_lock(f["repository"]):
        denied = admit(f)
        assert isinstance(denied, admission.Blocked)
        assert denied.code == "retries-exhausted"
        digest = grant(f)
        accepted = admit(f, digest)
        assert isinstance(accepted, admission.Reserved), accepted
        assert accepted.slot.task_id == f["task"]["task_id"]
        assert accepted.slot.generation_id == f["generation"].generation_id
        before = tuple(canonical_record_digest(row) for row in f["rows"])
        f["rows"].extend(f["seal"](dict(row)) for row in accepted.records_to_append)
        assert before == tuple(
            canonical_record_digest(row) for row in f["rows"][: len(before)]
        )
        state = projection.slot_state(
            f["rows"], f["generation"].generation_id, "delivery"
        )
        assert state.attempt_count("primary") == 3
        assert not state.primary_consumed
        assert isinstance(admit(f, digest), admission.Blocked)
        with pytest.raises(DispatchError):
            prepare_outage_recovery(f["repository"], f["rows"], **f["kwargs"])


def configure_retention(monkeypatch):
    # Consumer injection points use the actual source implementations. No
    # authentication predicate or protected record is substituted.
    monkeypatch.setattr(seams, "_adapters", dict(seams._adapters))
    names = (
        "_latest_attempt_settlement_indices",
        "accepted_review_terminals",
        "authenticated_retry_outcomes",
        "authenticated_review_terminals",
        "authenticated_supersessions",
        "authenticated_verdicts",
        "delivery_controller_records",
        "latest_explicit_alias_availability",
    )
    seams.configure(**{name: getattr(authority, name) for name in names})
    seams.configure(
        authenticated_recovery_admissions=provisional_findings.authenticated_recovery_admissions,
        authenticated_publication_bindings=provisional_findings.authenticated_publication_bindings,
    )


def compact(f):
    before = f["rows"]
    retained = authority_store.retained_authority_projection(before)
    f["rows"] = authority_store._prospective_retained_view(
        retained, source_records=before
    )
    assert isinstance(f["rows"], projection.AuthorityRecordView)


@pytest.mark.parametrize("phase", ["failures", "grant", "spent"])
def test_real_checkpoint_compaction_preserves_recovery_authority(
    signed_outage, monkeypatch, phase
):
    from loopzero.kernel import ledger_lifecycle

    f = signed_outage
    configure_retention(monkeypatch)
    repository = f["repository"]
    # A consumer's audit directory is outside the reviewed source identity.
    with (repository / ".git/info/exclude").open("a") as stream:
        stream.write("\n.audit/\n")
    with authority_store.authority_ledger_lock(repository):
        digest = grant(f) if phase != "failures" else None
        if phase == "spent":
            reserved = admit(f, digest)
            assert isinstance(reserved, admission.Reserved)
            f["rows"].extend(f["seal"](dict(row)) for row in reserved.records_to_append)
    receipts = {
        canonical_record_digest(row)
        for row in f["rows"]
        if row.get("type") in {"attempt-start", "attempt-terminal"}
    }
    stream = repository / ".audit/dispatch/2026-09-16.jsonl"
    stream.parent.mkdir(parents=True, mode=0o700)
    stream.write_text("".join(json.dumps(row) + "\n" for row in f["rows"]))
    stream.chmod(0o600)
    for _ in range(2):
        ledger_lifecycle.compact_authority_ledger(repository)
        f["rows"] = authority_store.load_authority_records(repository, 3650)
        assert isinstance(f["rows"], projection.AuthorityRecordView)
        assert receipts <= {canonical_record_digest(row) for row in f["rows"]}
    with authority_store.authority_ledger_lock(repository):
        if phase == "failures":
            assert prepare_outage_recovery(repository, f["rows"], **f["kwargs"])
        elif phase == "grant":
            assert isinstance(admit(f, digest), admission.Reserved)
        else:
            assert isinstance(admit(f, digest), admission.Blocked)
            with pytest.raises(DispatchError):
                prepare_outage_recovery(repository, f["rows"], **f["kwargs"])


def test_compaction_before_and_after_grant_preserves_receipts_and_spend(
    signed_outage, monkeypatch
):
    f = signed_outage
    configure_retention(monkeypatch)
    with authority_store.authority_ledger_lock(f["repository"]):
        references = {
            canonical_record_digest(row)
            for row in f["rows"]
            if row.get("type") in {"attempt-start", "attempt-terminal"}
        }
        compact(f)
        digest = grant(f)
        compact(f)
        accepted = admit(f, digest)
        assert isinstance(accepted, admission.Reserved), accepted
        # Storage appends preserve the authenticated view's provenance.
        f["rows"].extend(f["seal"](dict(row)) for row in accepted.records_to_append)
        compact(f)
        assert references <= {canonical_record_digest(row) for row in f["rows"]}
        assert isinstance(admit(f, digest), admission.Blocked)
        with pytest.raises(DispatchError):
            prepare_outage_recovery(f["repository"], f["rows"], **f["kwargs"])


def test_ordinary_routing_limit_is_not_renewed_by_authorization(signed_outage):
    f = signed_outage
    with authority_store.authority_ledger_lock(f["repository"]):
        grant(f)
        with pytest.raises(DispatchError, match="retry budget exhausted"):
            routing.validate_retry_policy(
                f["rows"],
                task_id="attempt-to-rename",
                work_unit_id="new-unit",
                alias="other-alias",
                effort="high",
                lineage=f["generation"].lineage_id,
                generation=f["generation"].generation_id,
                family="delivery",
                slot_kind="primary",
            )


def test_authenticated_active_writer_blocks_authorization(signed_outage):
    f = signed_outage
    prior = next(row for row in f["rows"] if row.get("type") == "attempt-start")
    active = {
        key: value for key, value in prior.items() if key != "terminal_authority_proof"
    }
    active.update(
        task_id="active-writer",
        work_unit_id="active-writer",
        read_only=False,
        write_files=["owned.py"],
    )
    f["rows"].append(f["seal"](active))
    with (
        authority_store.authority_ledger_lock(f["repository"]),
        pytest.raises(DispatchError),
    ):
        prepare_outage_recovery(f["repository"], f["rows"], **f["kwargs"])


def recovery_terminal(f, digest, *, actual_model, **overrides):
    accepted = admit(f, digest)
    assert isinstance(accepted, admission.Reserved), accepted
    f["rows"].extend(f["seal"](dict(row)) for row in accepted.records_to_append)
    terminal = f["attempt"](
        {
            **f["task"],
            "attempt_index": 1,
            "source_identity": f["source"],
            "task_contract_hash": next(
                row["task_contract_hash"]
                for row in f["rows"]
                if row.get("type") == "provider-outage-recovery-authorization-v1"
            ),
            "status": "completed",
            "review_reservation_id": accepted.slot.reservation_id,
            "review_generation_id": f["generation"].generation_id,
            "snapshot_tree_sha": f["generation"].tree,
            "engine": f["route"]["engine"],
            "model": f["route"]["model"],
            "effort": f["route"]["effort"],
            "runtime_effective_model": actual_model,
            "worker_identity": f"{f['route']['engine']}:{actual_model}",
            "outage_authorization_sha256": digest,
            **overrides,
        }
    )
    f["rows"].append(
        f["seal"](
            {
                "type": "verdict",
                "schema_version": terminal["schema_version"],
                "policy_version": terminal["policy_version"],
                "task_id": terminal["task_id"],
                "run_id": terminal["run_id"],
                "terminal_ref": canonical_record_digest(terminal),
                "verdict": "pass",
            }
        )
    )
    return terminal


def test_wrong_actual_replacement_cannot_supply_a_verdict(signed_outage):
    f = signed_outage
    with authority_store.authority_ledger_lock(f["repository"]):
        digest = grant(f)
        terminal = recovery_terminal(f, digest, actual_model="builder-model")
        assert (
            authority.authenticated_review_verdict(
                terminal, f["rows"], expected_family="delivery"
            )
            is None
        )


def test_replacement_verdict_cannot_relabel_original_failed_receipt(signed_outage):
    f = signed_outage
    original = next(
        row for row in reversed(f["rows"]) if row.get("type") == "attempt-terminal"
    )
    with authority_store.authority_ledger_lock(f["repository"]):
        digest = grant(f)
        terminal = recovery_terminal(f, digest, actual_model=f["route"]["model"])
        assert (
            authority.authenticated_review_verdict(
                terminal, f["rows"], expected_family="delivery"
            )
            == "pass"
        )
        assert (
            authority.authenticated_review_verdict(
                original, f["rows"], expected_family="delivery"
            )
            is None
        )


@pytest.mark.parametrize(
    "overrides",
    [
        {"runtime_fallback_from": "claude-sdk"},
        {"fallback_used": True},
        {"unit_attempt_number": 0},
        {
            "attempt_index": 2,
            "outage_authorization_sha256": None,
            "review_reservation_id": None,
        },
    ],
)
def test_recovery_cannot_hide_fallback_or_an_extra_attempt(signed_outage, overrides):
    f = signed_outage
    with authority_store.authority_ledger_lock(f["repository"]):
        digest = grant(f)
        terminal = recovery_terminal(
            f, digest, actual_model=f["route"]["model"], **overrides
        )
        assert (
            authority.authenticated_review_verdict(
                terminal, f["rows"], expected_family="delivery"
            )
            is None
        )


@pytest.mark.parametrize("refusal", ["circuit-open", "budget-exhausted"])
def test_consumer_admission_cannot_override_normal_execution_refusal(
    signed_outage, refusal
):
    from loopzero.review.outage_admission import admit_outage_review

    f = signed_outage
    with authority_store.authority_ledger_lock(f["repository"]):
        digest = grant(f)
    before = [canonical_record_digest(row) for row in f["rows"]]

    def reject(records, task, route):
        assert records is f["rows"]
        assert route == f["route"]
        raise DispatchError(refusal)

    result = admit_outage_review(
        f["repository"],
        worktree=f["repository"],
        task=f["task"],
        expected_source=f["source"],
        authorization_sha256=digest,
        route=f["route"],
        attempt_index=1,
        snapshot=ReviewSnapshot(
            f["source"]["head"], f["generation"].tree, f["repository"], f["patch"]
        ),
        load_records=lambda: f["rows"],
        append_records=lambda rows: pytest.fail("refused route must not reserve"),
        admit_execution=reject,
        required_sections=("code",),
    )
    assert isinstance(result, admission.Blocked)
    assert before == [canonical_record_digest(row) for row in f["rows"]]


@pytest.mark.parametrize("interrupt_after_reservation", [False, True])
def test_consumer_admission_spends_once_and_preserves_budget_accounting(
    signed_outage, interrupt_after_reservation
):
    from loopzero.review.outage_admission import admit_outage_review

    f = signed_outage
    with authority_store.authority_ledger_lock(f["repository"]):
        digest = grant(f)
    calls = []

    def admit_execution(records, task, route):
        # Existing failure costs remain visible to the ordinary budget gate.
        failures = [
            row
            for row in records
            if row.get("type") == "attempt-terminal"
            and row.get("status") == "infrastructure-failure"
        ]
        assert len(failures) == 2
        calls.append((task["task_id"], route))

    def append(rows):
        f["rows"].extend(f["seal"](dict(row)) for row in rows)
        if interrupt_after_reservation:
            raise InterruptedError("synthetic interruption after durable reservation")

    kwargs = dict(
        worktree=f["repository"],
        task=f["task"],
        expected_source=f["source"],
        authorization_sha256=digest,
        route=f["route"],
        attempt_index=1,
        snapshot=ReviewSnapshot(
            f["source"]["head"], f["generation"].tree, f["repository"], f["patch"]
        ),
        load_records=lambda: f["rows"],
        append_records=append,
        admit_execution=admit_execution,
        required_sections=("code",),
    )
    result = admit_outage_review(f["repository"], **kwargs)
    assert isinstance(
        result, admission.Blocked if interrupt_after_reservation else admission.Reserved
    ), result
    assert len(calls) == 1
    assert isinstance(admit_outage_review(f["repository"], **kwargs), admission.Blocked)
    assert len(calls) == 1
