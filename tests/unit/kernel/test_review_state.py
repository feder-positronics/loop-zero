import hashlib
import json
from types import SimpleNamespace

import pytest

from loopzero.kernel import authority_projection
from loopzero.kernel import authority_store
from loopzero.kernel import review_state
from loopzero.review import authority as review_authority, chain
from loopzero.runners.contract import ReviewOutcome

_REPOSITORY = None


@pytest.fixture(autouse=True)
def authenticated_rows(monkeypatch, tmp_path):
    global _REPOSITORY
    _REPOSITORY = tmp_path
    monkeypatch.setattr(
        authority_projection,
        "_authenticated_coordinator_record_ids",
        lambda rows: frozenset(map(id, rows)),
    )
    monkeypatch.setattr(
        review_authority,
        "_authenticated_coordinator_record_ids",
        lambda rows: frozenset(map(id, rows)),
    )
    monkeypatch.setattr(
        review_authority,
        "_authenticated_attempt_terminal_ids",
        lambda rows, **kwargs: frozenset(map(id, rows)),
    )
    monkeypatch.setattr(
        authority_projection,
        "_authenticated_attempt_terminal_ids",
        lambda rows, **kwargs: frozenset(map(id, rows)),
    )
    with authority_store.authority_ledger_lock(tmp_path):
        chain.configure(
            SimpleNamespace(
                required_sections=("code", "security"),
                max_reviews_per_pr=1,
                max_delta_reviews=1,
            )
        )
        yield


def identity(label, tree):
    return {
        "schema_version": "patch-identity-v1",
        "candidate_sha": label * 40,
        "candidate_tree_sha": tree,
        "diff_sha256": label * 64,
        "patch_id_verbatim": label * 40,
    }


def equivalence(left, right):
    payload = {
        "schema_version": "patch-equivalence-v1",
        "left_identity_digest": review_state.patch_identity_digest(left),
        "right_identity_digest": review_state.patch_identity_digest(right),
        "left_diff_sha256": left["diff_sha256"],
        "right_diff_sha256": right["diff_sha256"],
        "replay_command": [
            "git", "apply", "--cached", "--3way", "--binary",
            "--whitespace=nowarn", "-",
        ],
        "git_version": "git version 2.50.0",
        "return_code": 0,
        "result_tree_sha": right["candidate_tree_sha"],
        "base_path_overlap": [],
    }
    payload["receipt_digest"] = hashlib.sha256(
        json.dumps(
            payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode()
    ).hexdigest()
    return payload


def recorded_proof(rows, left, right, *, kind="patch-equivalence"):
    if kind == "patch-equivalence":
        proof = equivalence(left, right)
    else:
        proof = {
            "schema_version": "format-only-v1",
            "from_identity_digest": review_state.patch_identity_digest(left),
            "from_tree": left["candidate_tree_sha"],
            "to_identity_digest": review_state.patch_identity_digest(right),
            "to_tree": right["candidate_tree_sha"],
            "verified": True,
        }
    record = review_state.GenerationProofV1(
        proof_kind=kind,
        from_identity=left,
        from_tree=left["candidate_tree_sha"],
        to_identity=right,
        to_tree=right["candidate_tree_sha"],
        proof=proof,
    )
    rows.append(record.to_dict())
    return proof


def resolve(rows, patch, *, equivalence_proof=None, format_only_proof=None):
    return review_state.resolve_generation(
        rows,
        repository_binding="repository-one",
        patch_identity=patch,
        tree_sha=patch["candidate_tree_sha"],
        required_sections=("security", "code"),
        equivalence_proof=equivalence_proof,
        format_only_proof=format_only_proof,
    )


def consumed_terminal(task_id):
    return {
        "type": "attempt-terminal",
        "task_id": task_id,
        "status": "completed",
        "review_acceptance_verified": True,
        "accepted_verdict": "pass",
    }


def released_terminal(task_id):
    return {
        "type": "attempt-terminal",
        "task_id": task_id,
        "status": "infrastructure-failure",
        "terminal_reason": "transport-disconnect",
    }


def settle(rows, reservation, terminal, outcome):
    if reservation.to_dict() not in rows:
        rows.append(reservation.to_dict())
    rows.append(terminal)
    if outcome is ReviewOutcome.CONSUMED:
        rows.append({
            "type": "verdict", "task_id": terminal["task_id"], "verdict": "pass"
        })
    settlement = review_state.settle_review_slot(
        rows,
        reservation=reservation,
        outcome=outcome,
        terminal_ref=terminal,
    )
    rows.append(settlement.to_dict())
    return settlement


def test_generation_head_classes_and_predecessor_linking():
    original = identity("a", "1" * 40)
    initial = resolve([], original)
    assert initial.kind == "new"
    assert initial.transition.kind == "initial"
    rows = [initial.generation.to_dict()]

    rebased = identity("b", "2" * 40)
    proof = recorded_proof(rows, original, rebased)
    equivalent = resolve(rows, rebased, equivalence_proof=proof)
    assert equivalent.kind == "same"
    assert equivalent.generation.generation_id == initial.generation.generation_id
    rows.append(equivalent.carry.to_dict())

    formatted = identity("c", "3" * 40)
    format_proof = recorded_proof(rows, rebased, formatted, kind="format-only")
    format_only = resolve(rows, formatted, format_only_proof=format_proof)
    assert format_only.kind == "same"
    rows.append(format_only.carry.to_dict())

    delta_patch = identity("d", "4" * 40)
    delta = resolve(rows, delta_patch)
    assert delta.kind == "new"
    assert delta.transition.kind == "substantive"
    assert delta.generation.predecessor_id == initial.generation.generation_id
    assert delta.generation.delta_from_tree == initial.generation.tree
    rows.append(delta.generation.to_dict())

    substantive_patch = identity("e", "5" * 40)
    substantive = resolve(rows, substantive_patch)
    assert substantive.kind == "new"
    assert substantive.generation.predecessor_id == delta.generation.generation_id


def test_self_digested_equivalence_receipt_needs_authenticated_ledger_provenance():
    original = identity("a", "1" * 40)
    rebased = identity("b", "2" * 40)
    initial = resolve([], original)
    rows = [initial.generation.to_dict()]
    fabricated = equivalence(original, rebased)

    refused = resolve(rows, rebased, equivalence_proof=fabricated)
    assert refused.kind == "refused"
    assert refused.reason == "invalid-proof"

    recorded_proof(rows, original, rebased)
    accepted = resolve(rows, rebased, equivalence_proof=fabricated)
    assert accepted.kind == "same"


def test_format_provenance_record_is_created_only_by_kernel_prover(monkeypatch):
    original = identity("a", "1" * 40)
    formatted = identity("b", "2" * 40)
    calls = []
    from loopzero.kernel import patch_identity as patch_identity_kernel

    monkeypatch.setattr(
        patch_identity_kernel,
        "prove_format_only",
        lambda repo, before, after: calls.append((repo, before, after)) or True,
    )
    proof = review_state.prove_generation_carry(
        _REPOSITORY,
        from_identity=original,
        to_identity=formatted,
        proof_kind="format-only",
    )
    assert proof is not None
    assert calls == [(_REPOSITORY, "1" * 40, "2" * 40)]
    assert proof.from_identity == original
    assert proof.to_identity == formatted


def test_carry_projection_requires_preceding_authenticated_exact_pair_proof():
    original = identity("a", "1" * 40)
    rebased = identity("b", "2" * 40)
    initial = resolve([], original)
    receipt = equivalence(original, rebased)
    fabricated_carry = review_state.GenerationCarryV1(
        generation_id=initial.generation.generation_id,
        from_identity=original,
        to_identity=rebased,
        proof=receipt,
        sections=("code", "security"),
    )
    rows = [initial.generation.to_dict(), fabricated_carry.to_dict()]
    assert authority_projection.generation_carries(rows) == ()

    proof = review_state.GenerationProofV1(
        proof_kind="patch-equivalence",
        from_identity=original,
        from_tree=original["candidate_tree_sha"],
        to_identity=rebased,
        to_tree=rebased["candidate_tree_sha"],
        proof=receipt,
    )
    valid_carry = review_state.GenerationCarryV1(
        generation_id=initial.generation.generation_id,
        from_identity=original,
        to_identity=rebased,
        proof=proof.to_dict(),
        sections=("code", "security"),
    )
    rows = [initial.generation.to_dict(), proof.to_dict(), valid_carry.to_dict()]
    assert authority_projection.generation_carries(rows) == (valid_carry,)


def test_supersession_transition_requires_exact_authenticated_predecessor():
    original = identity("a", "1" * 40)
    target = identity("b", "2" * 40)
    initial = resolve([], original)
    unrelated = {
        "type": "attempt-supersession",
        "review_generation_id": "cg_" + "f" * 32,
        "superseding_source_identity": {"head": target["candidate_sha"]},
    }
    assert resolve(
        [initial.generation.to_dict(), unrelated], target
    ).transition.kind == "substantive"

    bound = {
        **unrelated,
        "review_generation_id": initial.generation.generation_id,
    }
    assert resolve(
        [initial.generation.to_dict(), bound], target
    ).transition.kind == "supersession"


def test_seen_content_revert_and_branch_rename_reuse_generation():
    patch = identity("a", "1" * 40)
    initial = resolve([], patch)
    rows = [initial.generation.to_dict()]
    resolve(rows, identity("b", "2" * 40))

    reverted = resolve(rows, patch)
    renamed_branch = resolve(rows, dict(patch))
    assert reverted.kind == renamed_branch.kind == "same"
    assert reverted.generation.generation_id == initial.generation.generation_id
    assert renamed_branch.carry.proof["kind"] == "seen-content-v1"


def test_records_have_canonical_round_trips_and_digests():
    generation = resolve([], identity("a", "1" * 40)).generation
    restored = review_state.ReviewGenerationV1.from_json(generation.to_json())
    assert restored == generation
    assert restored.canonical_digest() == generation.canonical_digest()


def test_proofless_review_state_rows_are_not_grandfathered(monkeypatch):
    generation = resolve([], identity("a", "1" * 40)).generation
    row = generation.to_dict()
    monkeypatch.setattr(
        authority_projection,
        "_authenticated_coordinator_record_ids",
        lambda rows: frozenset(),
    )
    assert authority_projection.authenticated_review_state_records([row]) == []
    assert authority_projection.generations([row]) == {}


def test_provider_failure_cannot_mint_or_select_a_lineage():
    failed = {
        "type": "attempt-terminal",
        "task_id": "provider-failure",
        "run_id": "caller-selected",
        "status": "infrastructure-failure",
        "terminal_reason": "transport-disconnect",
    }
    resolved = resolve([failed], identity("a", "1" * 40))
    assert resolved.kind == "new"
    assert resolved.transition.kind == "initial"
    assert resolved.generation.predecessor_id is None


def test_competing_reservers_get_same_reference_and_second_primary_is_refused():
    generation = resolve([], identity("a", "1" * 40)).generation
    rows = [generation.to_dict()]
    first = review_state.reserve_review_slot(
        _REPOSITORY, rows, generation_id=generation.generation_id, family="delivery",
        slot_kind="primary", task_id="review-one", idempotency_key="key-one",
    )
    rows.append(first.to_dict())
    competing = review_state.reserve_review_slot(
        _REPOSITORY, rows, generation_id=generation.generation_id, family="delivery",
        slot_kind="primary", task_id="review-two", idempotency_key="key-two",
    )
    assert competing.reservation_id == first.reservation_id
    assert competing.existing and competing.conflict

    settle(rows, first, consumed_terminal("review-one"), ReviewOutcome.CONSUMED)
    with pytest.raises(review_state.ReviewSlotError, match="consumed"):
        review_state.reserve_review_slot(
            _REPOSITORY, rows, generation_id=generation.generation_id, family="delivery",
            slot_kind="primary", task_id="review-three", idempotency_key="key-three",
        )


def test_delta_requires_one_settled_primary_and_settlement_is_idempotent():
    generation = resolve([], identity("a", "1" * 40)).generation
    rows = [generation.to_dict()]
    with pytest.raises(review_state.ReviewSlotError, match="exactly one"):
        review_state.reserve_review_slot(
            _REPOSITORY, rows, generation_id=generation.generation_id, family="delivery",
            slot_kind="delta", task_id="delta", idempotency_key="delta-key",
        )
    primary = review_state.reserve_review_slot(
        _REPOSITORY, rows, generation_id=generation.generation_id, family="delivery",
        slot_kind="primary", task_id="primary", idempotency_key="primary-key",
    )
    terminal = consumed_terminal("primary")
    rows.extend((primary.to_dict(), terminal))
    rows.append({"type": "verdict", "task_id": "primary", "verdict": "pass"})
    settlement = review_state.settle_review_slot(
        rows, reservation=primary, outcome=ReviewOutcome.CONSUMED,
        terminal_ref=terminal,
    )
    rows.append(settlement.to_dict())
    repeated = review_state.settle_review_slot(
        rows, reservation=primary, outcome=ReviewOutcome.CONSUMED,
        terminal_ref=terminal,
    )
    assert repeated.settlement_id == settlement.settlement_id
    assert repeated.existing
    assert review_state.reserve_review_slot(
        _REPOSITORY, rows, generation_id=generation.generation_id, family="delivery",
        slot_kind="delta", task_id="delta", idempotency_key="delta-key",
    ).slot_kind == "delta"


def test_released_slot_is_reusable_once_and_unresolved_never_frees():
    generation = resolve([], identity("a", "1" * 40)).generation
    rows = [generation.to_dict()]
    first = review_state.reserve_review_slot(
        _REPOSITORY, rows, generation_id=generation.generation_id, family="delivery",
        slot_kind="primary", task_id="try-one", idempotency_key="try-one",
    )
    settle(rows, first, released_terminal("try-one"), ReviewOutcome.RELEASED)
    second = review_state.reserve_review_slot(
        _REPOSITORY, rows, generation_id=generation.generation_id, family="delivery",
        slot_kind="primary", task_id="try-two", idempotency_key="try-two",
    )
    settle(rows, second, released_terminal("try-two"), ReviewOutcome.RELEASED)
    with pytest.raises(review_state.ReviewSlotError) as exhausted:
        review_state.reserve_review_slot(
            _REPOSITORY, rows, generation_id=generation.generation_id, family="delivery",
            slot_kind="primary", task_id="try-three", idempotency_key="try-three",
        )
    assert exhausted.value.code == "retries-exhausted"

    other = resolve([], identity("f", "6" * 40)).generation
    unresolved_rows = [other.to_dict()]
    reservation = review_state.reserve_review_slot(
        _REPOSITORY, unresolved_rows, generation_id=other.generation_id, family="delivery",
        slot_kind="primary", task_id="unknown", idempotency_key="unknown",
    )
    terminal = {"type": "attempt-terminal", "task_id": "unknown", "status": "alien"}
    settle(unresolved_rows, reservation, terminal, ReviewOutcome.UNRESOLVED)
    held = review_state.reserve_review_slot(
        _REPOSITORY, unresolved_rows, generation_id=other.generation_id, family="delivery",
        slot_kind="primary", task_id="competitor", idempotency_key="competitor",
    )
    assert held.reservation_id == reservation.reservation_id
    assert held.conflict


def test_lock_assertion_uses_authority_ledger_lock(tmp_path):
    unlocked = tmp_path / "unlocked"
    with pytest.raises(review_state.ReviewStateError, match="must be held"):
        review_state.assert_authority_ledger_lock_held(unlocked)
    with authority_store.authority_ledger_lock(unlocked):
        review_state.assert_authority_ledger_lock_held(unlocked)


def test_reservation_api_refuses_without_authority_lock(tmp_path):
    generation = resolve([], identity("a", "1" * 40)).generation
    with pytest.raises(review_state.ReviewStateError, match="must be held"):
        review_state.reserve_review_slot(
            tmp_path / "unlocked-reservation",
            [generation.to_dict()],
            generation_id=generation.generation_id,
            family="delivery",
            slot_kind="primary",
            task_id="review",
            idempotency_key="key",
        )
