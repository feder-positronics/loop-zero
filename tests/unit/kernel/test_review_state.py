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
        "base_sha": "0" * 40,
        "base_tree_sha": "1" * 40,
        "candidate_sha": label * 40,
        "candidate_tree_sha": tree,
        "diff_format": "git-binary-full-index-no-renames-v1",
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


def recorded_link(rows, predecessor, left, right, *, kind="substantive"):
    link = review_state.GenerationLinkV1(
        repository_binding="repository-one",
        predecessor_generation_id=predecessor.generation_id,
        from_identity=left,
        to_identity=right,
        transition_kind=kind,
    )
    rows.append(link.to_dict())
    return link


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


def consumed_terminal(task_id, tree=None):
    return {
        "type": "attempt-terminal",
        "task_id": task_id,
        "status": "completed",
        "review_acceptance_verified": True,
        "accepted_verdict": "pass",
        "snapshot_tree_sha": tree,
    }


def released_terminal(task_id, tree=None):
    return {
        "type": "attempt-terminal",
        "task_id": task_id,
        "status": "infrastructure-failure",
        "terminal_reason": "transport-disconnect",
        "snapshot_tree_sha": tree,
    }


def settle(rows, reservation, terminal, outcome):
    if reservation.to_dict() not in rows:
        rows.append(reservation.to_dict())
    generation = authority_projection.generations(rows)[reservation.generation_id]
    terminal = {**terminal, "snapshot_tree_sha": terminal.get("snapshot_tree_sha") or generation.tree}
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
    recorded_link(rows, initial.generation, formatted, delta_patch)
    delta = resolve(rows, delta_patch)
    assert delta.kind == "new"
    assert delta.transition.kind == "substantive"
    assert delta.generation.predecessor_id == initial.generation.generation_id
    assert delta.generation.delta_from_tree == formatted["candidate_tree_sha"]
    rows.append(delta.generation.to_dict())

    substantive_patch = identity("e", "5" * 40)
    rows.append(delta.generation.to_dict())
    recorded_link(rows, delta.generation, delta_patch, substantive_patch)
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
    unrelated = review_state.GenerationLinkV1(
        repository_binding="repository-one",
        predecessor_generation_id="cg_" + "f" * 32,
        from_identity=original,
        to_identity=target,
        transition_kind="supersession",
    ).to_dict()
    assert resolve([initial.generation.to_dict(), unrelated], target).transition.kind == "initial"

    bound = review_state.GenerationLinkV1(
        repository_binding="repository-one",
        predecessor_generation_id=initial.generation.generation_id,
        from_identity=original,
        to_identity=target,
        transition_kind="supersession",
    ).to_dict()
    supersession = {
        "type": "attempt-supersession",
        "review_generation_id": initial.generation.generation_id,
        "superseding_source_identity": {"head": target["candidate_sha"]},
    }
    assert resolve(
        [initial.generation.to_dict(), supersession, bound], target
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


def test_seen_content_ignores_commit_metadata_but_rejects_identity_extensions():
    patch = identity("a", "1" * 40)
    initial = resolve([], patch)
    recommitted = {
        **patch,
        "candidate_sha": "b" * 40,
    }
    assert review_state.generation_id_for(
        "repository-one", patch, patch["candidate_tree_sha"], ("code", "security")
    ) == review_state.generation_id_for(
        "repository-one", recommitted, recommitted["candidate_tree_sha"], ("code",)
    )
    same = resolve([initial.generation.to_dict()], recommitted)
    assert same.kind == "same"
    assert same.generation.generation_id == initial.generation.generation_id
    assert same.carry.proof == {
        "kind": "seen-content-v1",
        "content_digest": review_state.patch_content_digest(patch),
        "tree": patch["candidate_tree_sha"],
    }

    extended = resolve(
        [initial.generation.to_dict()], {**recommitted, "caller_label": "trusted"}
    )
    assert extended.kind == "refused"
    assert extended.reason.startswith("missing-evidence:patch identity fields")


def test_seen_content_section_change_cannot_mint_another_generation():
    patch = identity("a", "1" * 40)
    initial = resolve([], patch)
    same = review_state.resolve_generation(
        [initial.generation.to_dict()],
        repository_binding="repository-one",
        patch_identity=patch,
        tree_sha=patch["candidate_tree_sha"],
        required_sections=("code",),
        equivalence_proof=None,
        format_only_proof=None,
    )
    assert same.kind == "same"
    assert same.generation.generation_id == initial.generation.generation_id


def test_seen_carry_endpoint_preserves_only_authenticated_reachable_sections():
    original = identity("a", "1" * 40)
    rebased = identity("b", "2" * 40)
    initial = resolve([], original)
    rows = [initial.generation.to_dict()]
    recorded_proof(rows, original, rebased)
    rows.append(
        review_state.GenerationCarryV1(
            generation_id=initial.generation.generation_id,
            from_identity=original,
            to_identity=rebased,
            proof=rows[-1],
            sections=("code",),
        ).to_dict()
    )

    repeated = resolve(rows, rebased)

    assert repeated.kind == "same"
    assert repeated.generation.generation_id == initial.generation.generation_id
    assert repeated.carry.sections == ("code",)


def test_same_tree_and_diff_on_an_unproven_different_base_is_substantive():
    original = identity("a", "1" * 40)
    different_base = {
        **original,
        "base_sha": "2" * 40,
        "base_tree_sha": "3" * 40,
    }
    initial = resolve([], original)

    changed_context = resolve([initial.generation.to_dict()], different_base)

    assert changed_context.kind == "new"
    assert changed_context.transition.kind == "substantive"
    assert changed_context.generation.predecessor_id is None
    assert changed_context.generation.primary_origin_receipt is None
    assert changed_context.generation.lineage_id != initial.generation.lineage_id


def test_same_tree_and_diff_on_a_proven_equivalent_base_reuses_generation():
    original = identity("a", "1" * 40)
    equivalent_base = {
        **original,
        "base_sha": "2" * 40,
        "base_tree_sha": "3" * 40,
    }
    initial = resolve([], original)
    rows = [initial.generation.to_dict()]
    proof = recorded_proof(rows, original, equivalent_base)

    resolved = resolve(rows, equivalent_base, equivalence_proof=proof)

    assert resolved.kind == "same"
    assert resolved.generation.generation_id == initial.generation.generation_id


def test_unrelated_content_starts_fresh_lineage_without_authenticated_link():
    first = resolve([], identity("a", "1" * 40))
    second = resolve([first.generation.to_dict()], identity("b", "2" * 40))
    assert second.kind == "new"
    assert second.transition.kind == "initial"
    assert second.generation.predecessor_id is None
    assert second.generation.lineage_id != first.generation.lineage_id


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


def test_successor_projection_requires_a_preceding_authenticated_content_link():
    first = resolve([], identity("a", "1" * 40))
    target = identity("b", "2" * 40)
    link = review_state.GenerationLinkV1(
        repository_binding="repository-one",
        predecessor_generation_id=first.generation.generation_id,
        from_identity=first.generation.patch_identity,
        to_identity=target,
        transition_kind="substantive",
    )
    successor = resolve([first.generation.to_dict(), link.to_dict()], target)
    rows_without_link = [first.generation.to_dict(), successor.generation.to_dict()]
    assert successor.generation.generation_id not in authority_projection.generations(
        rows_without_link
    )
    rows_with_link = [
        first.generation.to_dict(), link.to_dict(), successor.generation.to_dict()
    ]
    assert successor.generation.generation_id in authority_projection.generations(
        rows_with_link
    )


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


def test_repository_binding_must_match_verified_ledger_head():
    rows = authority_projection.AuthorityRecordView(
        [],
        accumulator_head=SimpleNamespace(
            ledger_id="1" * 32,
            repository_binding="other-repository",
            record_count=0,
            records_sha256="2" * 64,
        ),
    )
    refused = resolve(rows, identity("a", "1" * 40))
    assert refused.kind == "refused"
    assert refused.reason == "missing-evidence:repository binding mismatch"


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
    terminal = consumed_terminal("primary", generation.tree)
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


def test_idempotency_key_is_reusable_only_for_an_unsettled_reservation():
    generation = resolve([], identity("a", "1" * 40)).generation
    rows = [generation.to_dict()]
    first = review_state.reserve_review_slot(
        _REPOSITORY, rows, generation_id=generation.generation_id, family="delivery",
        slot_kind="primary", task_id="first", idempotency_key="first-key",
    )
    rows.append(first.to_dict())
    repeated = review_state.reserve_review_slot(
        _REPOSITORY, rows, generation_id=generation.generation_id, family="delivery",
        slot_kind="primary", task_id="first", idempotency_key="first-key",
    )
    assert repeated.existing and not repeated.conflict

    settle(rows, first, released_terminal("first"), ReviewOutcome.RELEASED)
    with pytest.raises(review_state.ReviewSlotError) as settled_key:
        review_state.reserve_review_slot(
            _REPOSITORY, rows, generation_id=generation.generation_id,
            family="delivery", slot_kind="primary", task_id="first",
            idempotency_key="first-key",
        )
    assert settled_key.value.code == "reservation-conflict"

    second = review_state.reserve_review_slot(
        _REPOSITORY, rows, generation_id=generation.generation_id, family="delivery",
        slot_kind="primary", task_id="second", idempotency_key="second-key",
    )
    rows.append(second.to_dict())
    with pytest.raises(review_state.ReviewSlotError) as old_key:
        review_state.reserve_review_slot(
            _REPOSITORY, rows, generation_id=generation.generation_id,
            family="delivery", slot_kind="primary", task_id="first",
            idempotency_key="first-key",
        )
    assert old_key.value.code == "reservation-conflict"
    held = review_state.reserve_review_slot(
        _REPOSITORY, rows, generation_id=generation.generation_id, family="delivery",
        slot_kind="primary", task_id="third", idempotency_key="third-key",
    )
    assert held.conflict and held.reservation_id == second.reservation_id


def test_idempotency_key_is_bound_across_generations_and_families():
    first = resolve([], identity("a", "1" * 40)).generation
    second = resolve([], identity("b", "2" * 40)).generation
    rows = [first.to_dict(), second.to_dict()]
    reservation = review_state.reserve_review_slot(
        _REPOSITORY,
        rows,
        generation_id=first.generation_id,
        family="delivery",
        slot_kind="primary",
        task_id="first",
        idempotency_key="global-key",
    )
    rows.append(reservation.to_dict())
    with pytest.raises(review_state.ReviewSlotError) as reused:
        review_state.reserve_review_slot(
            _REPOSITORY,
            rows,
            generation_id=second.generation_id,
            family="trust",
            slot_kind="primary",
            task_id="second",
            idempotency_key="global-key",
        )
    assert reused.value.code == "reservation-conflict"


def test_unresolved_settlement_resolves_from_a_late_verdict_without_a_second_row():
    generation = resolve([], identity("a", "1" * 40)).generation
    rows = [generation.to_dict()]
    reservation = review_state.reserve_review_slot(
        _REPOSITORY, rows, generation_id=generation.generation_id, family="delivery",
        slot_kind="primary", task_id="late", idempotency_key="late-key",
    )
    terminal = consumed_terminal("late", generation.tree)
    rows.extend((reservation.to_dict(), terminal))
    settlement = review_state.settle_review_slot(
        rows, reservation=reservation, outcome=ReviewOutcome.UNRESOLVED,
        terminal_ref=terminal,
    )
    rows.append(settlement.to_dict())
    assert authority_projection.slot_state(
        rows, generation.generation_id, "delivery"
    ).outstanding == reservation
    with pytest.raises(review_state.ReviewSlotError) as unresolved_key:
        review_state.reserve_review_slot(
            _REPOSITORY, rows, generation_id=generation.generation_id,
            family="delivery", slot_kind="primary", task_id="late",
            idempotency_key="late-key",
        )
    assert unresolved_key.value.code == "reservation-conflict"

    duplicate = review_state.settle_review_slot(
        rows, reservation=reservation, outcome=ReviewOutcome.UNRESOLVED,
        terminal_ref=terminal,
    )
    assert duplicate.existing
    rows.append({"type": "verdict", "task_id": "late", "verdict": "pass"})
    resolved = authority_projection.slot_state(
        rows, generation.generation_id, "delivery"
    )
    assert resolved.primary_consumed
    assert resolved.outstanding is None
    assert len(resolved.settlements) == 1

    no_second_row = review_state.settle_review_slot(
        rows, reservation=reservation, outcome=ReviewOutcome.CONSUMED,
        terminal_ref=terminal,
    )
    assert no_second_row.existing


def test_settlement_rejects_terminal_from_outside_generation_tree():
    generation = resolve([], identity("a", "1" * 40)).generation
    rows = [generation.to_dict()]
    reservation = review_state.reserve_review_slot(
        _REPOSITORY, rows, generation_id=generation.generation_id, family="delivery",
        slot_kind="primary", task_id="wrong-tree", idempotency_key="wrong-tree",
    )
    terminal = consumed_terminal("wrong-tree", "f" * 40)
    rows.extend((reservation.to_dict(), terminal))
    rows.append({"type": "verdict", "task_id": "wrong-tree", "verdict": "pass"})
    with pytest.raises(review_state.ReviewSlotError, match="outside"):
        review_state.settle_review_slot(
            rows, reservation=reservation, outcome=ReviewOutcome.CONSUMED,
            terminal_ref=terminal,
        )


def test_checkpoint_retention_keeps_terminal_needed_by_consumed_settlement(
    monkeypatch,
):
    generation = resolve([], identity("a", "1" * 40)).generation
    rows = [generation.to_dict()]
    reservation = review_state.reserve_review_slot(
        _REPOSITORY, rows, generation_id=generation.generation_id, family="delivery",
        slot_kind="primary", task_id="retained", idempotency_key="retained",
    )
    settle(
        rows, reservation, consumed_terminal("retained", generation.tree),
        ReviewOutcome.CONSUMED,
    )
    terminal = next(row for row in rows if row.get("type") == "attempt-terminal")
    verdict = next(row for row in rows if row.get("type") == "verdict")
    monkeypatch.setattr(authority_store, "delivery_controller_records", lambda rows: [])
    monkeypatch.setattr(authority_store, "_retention_open_attempt_contexts", lambda rows: set())
    live = authority_store._retention_live_record_ids(rows)
    assert id(terminal) in live
    assert id(verdict) in live

    compacted = authority_projection.AuthorityRecordView(
        [row for row in rows if id(row) in live],
        trusted_retained_ids=live,
    )
    state = authority_projection.slot_state(
        compacted, generation.generation_id, "delivery"
    )
    assert state.primary_consumed
    assert state.outstanding is None


def test_projection_never_drops_a_settlement_with_missing_terminal_evidence():
    generation = resolve([], identity("a", "1" * 40)).generation
    rows = [generation.to_dict()]
    reservation = review_state.reserve_review_slot(
        _REPOSITORY, rows, generation_id=generation.generation_id, family="delivery",
        slot_kind="primary", task_id="missing-terminal", idempotency_key="missing",
    )
    terminal = consumed_terminal("missing-terminal", generation.tree)
    complete = [*rows, reservation.to_dict(), terminal]
    complete.append({
        "type": "verdict", "task_id": "missing-terminal", "verdict": "pass"
    })
    settlement = review_state.settle_review_slot(
        complete, reservation=reservation, outcome=ReviewOutcome.CONSUMED,
        terminal_ref=terminal,
    )
    projected = authority_projection.slot_state(
        [*rows, reservation.to_dict(), settlement.to_dict()],
        generation.generation_id,
        "delivery",
    )
    assert projected.settlements == (settlement,)
    assert not projected.primary_consumed
    assert projected.outstanding == reservation


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
