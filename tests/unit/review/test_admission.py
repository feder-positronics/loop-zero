import hashlib
import json
from types import SimpleNamespace

import pytest

from loopzero.kernel import authority_projection, review_state
from loopzero.review import admission, chain
from loopzero.runners.contract import ReviewOutcome


@pytest.fixture(autouse=True)
def configured(monkeypatch):
    monkeypatch.setattr(
        authority_projection,
        "_authenticated_coordinator_record_ids",
        lambda rows: frozenset(map(id, rows)),
    )
    monkeypatch.setattr(
        authority_projection,
        "_authenticated_attempt_terminal_ids",
        lambda rows, **kwargs: frozenset(map(id, rows)),
    )
    chain.configure(
        SimpleNamespace(
            required_sections=("code",),
            max_reviews_per_pr=1,
            max_delta_reviews=1,
        )
    )


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


def admit(rows, patch, *, review_task=None, requested="review", changed=()):
    review_task = review_task or task()
    return admission.admit_review(
        rows,
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
    result = admit(
        rows,
        identity("b"),
        review_task=task("delta", "delta"),
        requested="delta",
        changed=("src/b.py", "src/a.py"),
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
        [], repository_binding="repo", task=task("primary", "primary"),
        current_source_identity={"head": original["candidate_sha"]},
        current_tree_sha=original["candidate_tree_sha"], patch_identity=original,
        required_sections=("code", "security"), equivalence_proof=None,
        format_only_proof=None, requested="review", changed_paths=(),
        security_trigger_paths=("security/policy.py",),
    )
    rows = list(first.records_to_append)
    append_settlement(
        rows, first.slot, ReviewOutcome.CONSUMED, terminal("primary")
    )
    rebased = identity("b")
    result = admission.admit_review(
        rows, repository_binding="repo", task=task("delta", "delta"),
        current_source_identity={"head": rebased["candidate_sha"]},
        current_tree_sha=rebased["candidate_tree_sha"], patch_identity=rebased,
        required_sections=("code", "security"),
        equivalence_proof=equivalence(
            original, rebased, overlap=("security/policy.py",)
        ),
        format_only_proof=None, requested="delta", changed_paths=(),
        security_trigger_paths=("security/policy.py",),
    )
    assert isinstance(result, admission.Reserved)
    assert result.generation.generation_id == first.generation.generation_id
    assert result.slot.slot_kind == "delta"
    assert result.records_to_append[0]["sections"] == ["code"]


def test_every_blocked_code_is_reachable():
    patch = identity("a")
    stale = admission.admit_review(
        [], repository_binding="repo",
        task={**task(), "source_identity": {"head": "f" * 40}},
        current_source_identity={"head": patch["candidate_sha"]},
        current_tree_sha=patch["candidate_tree_sha"], patch_identity=patch,
        required_sections=("code",), equivalence_proof=None,
        format_only_proof=None, requested="review", changed_paths=(),
        security_trigger_paths=(),
    )
    invalid = admission.admit_review(
        [], repository_binding="repo", task=task(),
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
    oversized = admit(
        primary_rows,
        identity("c"),
        review_task=task("large", "large"),
        requested="delta",
        changed=tuple(f"src/{index}.py" for index in range(301)),
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
