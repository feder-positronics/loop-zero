import hashlib
import json
from types import SimpleNamespace

import pytest

from loopzero.kernel import authority_projection, authority_store, review_state
from loopzero.review import admission, authority as review_authority, chain
from loopzero.runners.contract import ReviewOutcome

_REPOSITORY = None
_TREE_DIFFS = {}


@pytest.fixture(autouse=True)
def configured(monkeypatch, tmp_path):
    global _REPOSITORY
    _REPOSITORY = tmp_path
    _TREE_DIFFS.clear()
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
    monkeypatch.setattr(
        admission,
        "tree_diff_paths",
        lambda repo, source, target: _TREE_DIFFS.get((source, target), ((), "d" * 64)),
    )
    with authority_store.authority_ledger_lock(tmp_path):
        chain.configure(
            SimpleNamespace(
                required_sections=("code",),
                max_reviews_per_pr=1,
                max_delta_reviews=1,
            )
        )
        yield


def identity(label):
    return {
        "candidate_sha": label * 40,
        "candidate_tree_sha": label * 40,
        "diff_sha256": label * 64,
    }


def equivalence(left, right, *, overlap=()):
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
        "base_path_overlap": list(overlap),
    }
    payload["receipt_digest"] = hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return payload


def task(name="review", key="key"):
    return {
        "task_id": name,
        "idempotency_key": key,
        "review_intent": "delivery-code-review",
    }


def admit(
    rows, patch, *, review_task=None, requested="review", changed=None,
    changed_digest=None,
):
    review_task = review_task or task()
    return admission.admit_review(
        _REPOSITORY, rows,
        repository_binding="repo",
        task=review_task,
        current_source_identity={"head": patch["candidate_sha"]},
        current_tree_sha=patch["candidate_tree_sha"],
        patch_identity=patch,
        required_sections=("code",),
        equivalence_proof=None,
        format_only_proof=None,
        requested=requested,
        changed_paths=changed,
        changed_paths_digest=changed_digest,
        security_trigger_paths=("security/policy.py",),
    )


def terminal(task_id, *, released=False, unresolved=False):
    row = {"type": "attempt-terminal", "task_id": task_id}
    if released:
        row.update(status="infrastructure-failure", terminal_reason="transport-disconnect")
    elif unresolved:
        row.update(status="unknown")
    else:
        row.update(
            status="completed",
            review_acceptance_verified=True,
            accepted_verdict="pass",
        )
    return row


def append_settlement(rows, reservation, outcome, result):
    if reservation.to_dict() not in rows:
        rows.append(reservation.to_dict())
    rows.append(result)
    if outcome is ReviewOutcome.CONSUMED:
        rows.append({
            "type": "verdict", "task_id": result["task_id"], "verdict": "pass"
        })
    settlement = review_state.settle_review_slot(
        rows, reservation=reservation, outcome=outcome, terminal_ref=result
    )
    rows.append(settlement.to_dict())


def primary_history():
    first = admit([], identity("a"))
    assert isinstance(first, admission.Reserved)
    rows = list(first.records_to_append)
    append_settlement(rows, first.slot, ReviewOutcome.CONSUMED, terminal("review"))
    return rows, first.generation


def released_twice_history():
    first = admit([], identity("a"), review_task=task("one", "one"))
    rows = list(first.records_to_append)
    append_settlement(rows, first.slot, ReviewOutcome.RELEASED, terminal("one", released=True))
    second = admit(rows, identity("a"), review_task=task("two", "two"))
    assert isinstance(second, admission.Reserved)
    rows.extend(second.records_to_append)
    append_settlement(rows, second.slot, ReviewOutcome.RELEASED, terminal("two", released=True))
    return rows


def test_primary_reservation_scopes_generation_fields():
    result = admit([], identity("a"))
    assert isinstance(result, admission.Reserved)
    assert result.slot.slot_kind == "primary"
    assert result.scoped_task["review_generation_id"] == result.generation.generation_id
    assert [row["type"] for row in result.records_to_append] == [
        "review-generation-v1",
        "review-slot-reservation-v1",
    ]


def test_carry_dispatches_nothing():
    rows, generation = primary_history()
    result = admit(rows, identity("a"), review_task=task("repeat", "repeat"))
    assert isinstance(result, admission.Carry)
    assert result.dispatch is False
    assert result.generation.generation_id == generation.generation_id
    assert result.receipts
    assert [row["type"] for row in result.records_to_append] == [
        "generation-carry-v1"
    ]


def test_delta_scope_is_changed_plus_security_paths():
    rows, previous = primary_history()
    changed = ("src/a.py", "src/b.py")
    _TREE_DIFFS[(previous.tree, "b" * 40)] = (changed, "e" * 64)
    result = admit(
        rows,
        identity("b"),
        review_task=task("delta", "delta"),
        requested="delta",
        changed=("src/b.py", "src/a.py"),
        changed_digest="e" * 64,
    )
    assert isinstance(result, admission.Reserved)
    assert result.slot.slot_kind == "delta"
    assert result.scoped_task["delta_scope"] == {
        "from_tree": previous.tree,
        "to_tree": "b" * 40,
        "changed_paths": ["src/a.py", "src/b.py"],
        "closure_paths": ["security/policy.py", "src/a.py", "src/b.py"],
    }


def test_security_overlap_keeps_generation_but_requires_delta():
    original = identity("a")
    first = admission.admit_review(
        _REPOSITORY, [], repository_binding="repo", task=task("primary", "primary"),
        current_source_identity={"head": original["candidate_sha"]},
        current_tree_sha=original["candidate_tree_sha"], patch_identity=original,
        required_sections=("code", "security"), equivalence_proof=None,
        format_only_proof=None, requested="review", changed_paths=None,
        security_trigger_paths=("security/policy.py",),
    )
    rows = list(first.records_to_append)
    append_settlement(
        rows, first.slot, ReviewOutcome.CONSUMED, terminal("primary")
    )
    rebased = identity("b")
    receipt = equivalence(
        original, rebased, overlap=("security/policy.py",)
    )
    rows.append(review_state.GenerationProofV1(
        proof_kind="patch-equivalence",
        from_identity=original,
        from_tree=original["candidate_tree_sha"],
        to_identity=rebased,
        to_tree=rebased["candidate_tree_sha"],
        proof=receipt,
    ).to_dict())
    result = admission.admit_review(
        _REPOSITORY, rows, repository_binding="repo", task=task("delta", "delta"),
        current_source_identity={"head": rebased["candidate_sha"]},
        current_tree_sha=rebased["candidate_tree_sha"], patch_identity=rebased,
        required_sections=("code", "security"),
        equivalence_proof=receipt,
        format_only_proof=None, requested="delta", changed_paths=None,
        security_trigger_paths=("security/policy.py",),
    )
    assert isinstance(result, admission.Reserved)
    assert result.generation.generation_id == first.generation.generation_id
    assert result.slot.slot_kind == "delta"
    assert result.records_to_append[0]["sections"] == ["code"]


def test_every_blocked_code_is_reachable():
    patch = identity("a")
    stale = admission.admit_review(
        _REPOSITORY, [], repository_binding="repo",
        task={**task(), "source_identity": {"head": "f" * 40}},
        current_source_identity={"head": patch["candidate_sha"]},
        current_tree_sha=patch["candidate_tree_sha"], patch_identity=patch,
        required_sections=("code",), equivalence_proof=None,
        format_only_proof=None, requested="review", changed_paths=(),
        security_trigger_paths=(),
    )
    invalid = admission.admit_review(
        _REPOSITORY, [], repository_binding="repo", task=task(),
        current_source_identity={"head": patch["candidate_sha"]},
        current_tree_sha=patch["candidate_tree_sha"], patch_identity=patch,
        required_sections=("code",), equivalence_proof={"forged": True},
        format_only_proof=None, requested="review", changed_paths=(),
        security_trigger_paths=(),
    )
    missing = admit([], patch, review_task={"task_id": "missing"})

    held_first = admit([], patch, review_task=task("holder", "holder"))
    held_rows = list(held_first.records_to_append)
    held = admit(held_rows, patch, review_task=task("competing", "competing"))
    conflict = admit(held_rows, patch, review_task=task("other", "holder"))

    primary_rows, _ = primary_history()
    exhausted = admit(
        primary_rows, identity("b"), review_task=task("fresh", "fresh")
    )
    retries = admit(
        released_twice_history(), patch, review_task=task("third", "third")
    )
    oversized_paths = tuple(f"src/{index}.py" for index in range(301))
    _TREE_DIFFS[("a" * 40, "c" * 40)] = (oversized_paths, "f" * 64)
    oversized = admit(
        primary_rows,
        identity("c"),
        review_task=task("large", "large"),
        requested="delta",
        changed=None,
    )
    results = {
        result.code
        for result in (
            stale, invalid, missing, held, conflict, exhausted, retries, oversized
        )
    }
    assert results == {
        "stale-source",
        "invalid-proof",
        "missing-evidence",
        "slot-held",
        "slots-exhausted",
        "retries-exhausted",
        "oversized-delta",
        "reservation-conflict",
    }


def test_idempotent_admission_returns_own_reservation_and_blocks_competitor():
    patch = identity("a")
    first = admit([], patch, review_task=task("one", "same-key"))
    rows = list(first.records_to_append)

    repeated = admit(rows, patch, review_task=task("one", "same-key"))
    assert isinstance(repeated, admission.Reserved)
    assert repeated.slot.existing
    assert repeated.slot.reservation_id == first.slot.reservation_id
    assert all(
        row["type"] != "review-slot-reservation-v1"
        for row in repeated.records_to_append
    )

    competing = admit(rows, patch, review_task=task("two", "other-key"))
    assert isinstance(competing, admission.Blocked)
    assert competing.code == "slot-held"
    assert competing.evidence["reservation_id"] == first.slot.reservation_id


def test_substantive_generation_needs_delta_before_exact_content_can_carry():
    rows, _ = primary_history()
    changed = identity("b")
    opened = admit(rows, changed, review_task=task("delta", "delta"), requested="delta")
    assert isinstance(opened, admission.Reserved)
    history = [*rows, *opened.records_to_append]

    repeated = admit(
        history,
        changed,
        review_task=task("delta", "delta"),
        requested="delta",
    )
    assert isinstance(repeated, admission.Reserved)
    assert repeated.slot.existing


def test_caller_paths_and_digest_cannot_narrow_kernel_diff():
    rows, previous = primary_history()
    _TREE_DIFFS[(previous.tree, "b" * 40)] = (
        ("src/a.py", "src/omitted.py"),
        "9" * 64,
    )
    result = admit(
        rows,
        identity("b"),
        review_task=task("delta", "delta"),
        requested="delta",
        changed=("src/a.py",),
        changed_digest="9" * 64,
    )
    assert isinstance(result, admission.Blocked)
    assert result.code == "invalid-proof"


def test_admission_refuses_without_authority_lock(tmp_path):
    unlocked = tmp_path / "unlocked"
    with pytest.raises(review_state.ReviewStateError, match="must be held"):
        admission.admit_review(
            unlocked,
            [],
            repository_binding="repo",
            task=task(),
            current_source_identity={"head": "a" * 40},
            current_tree_sha="a" * 40,
            patch_identity=identity("a"),
            required_sections=("code",),
            equivalence_proof=None,
            format_only_proof=None,
            requested="review",
            changed_paths=None,
            security_trigger_paths=(),
        )
