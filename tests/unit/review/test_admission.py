import hashlib
import json
import os
import subprocess
from types import SimpleNamespace

import pytest

from loopzero.kernel import (
    authority_projection,
    authority_store,
    patch_identity as patch_identity_kernel,
    review_state,
)
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
        lambda repo, source, target: _TREE_DIFFS.get(
            (source, target), ((), target[0] * 64)
        ),
    )
    monkeypatch.setattr(
        admission,
        "security_trigger_paths_between",
        lambda repo, source, target: ("security/policy.py",),
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
        "schema_version": "patch-identity-v1",
        "base_sha": "0" * 40,
        "base_tree_sha": "1" * 40,
        "candidate_sha": label * 40,
        "candidate_tree_sha": label * 40,
        "diff_format": "git-binary-full-index-no-renames-v1",
        "diff_sha256": label * 64,
        "patch_id_verbatim": label * 40,
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
    changed_digest=None, security=("security/policy.py",),
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
        security_trigger_paths=security,
    )


def terminal(task_id, *, tree=None, released=False, unresolved=False):
    row = {"type": "attempt-terminal", "task_id": task_id}
    row["snapshot_tree_sha"] = tree
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


def append_settlement(rows, reservation, outcome, result, *, verdict="pass"):
    if reservation.to_dict() not in rows:
        rows.append(reservation.to_dict())
    generation = authority_projection.generations(rows)[reservation.generation_id]
    result = {**result, "snapshot_tree_sha": result.get("snapshot_tree_sha") or generation.tree}
    rows.append(result)
    if outcome is ReviewOutcome.CONSUMED:
        rows.append({
            "type": "verdict", "task_id": result["task_id"], "verdict": verdict
        })
    settlement = review_state.settle_review_slot(
        _REPOSITORY,
        rows,
        reservation=reservation,
        outcome=outcome,
        terminal_ref=result,
    )
    rows.append(settlement.to_dict())


def primary_history():
    first = admit([], identity("a"))
    assert isinstance(first, admission.Reserved)
    rows = list(first.records_to_append)
    append_settlement(rows, first.slot, ReviewOutcome.CONSUMED, terminal("review"))
    return rows, first.generation


def link_generation(rows, predecessor, target, *, source=None, kind="substantive"):
    source = source or predecessor.patch_identity
    rows.append(review_state.GenerationLinkV1(
        repository_binding="repo",
        predecessor_generation_id=predecessor.generation_id,
        from_identity=source,
        to_identity=target,
        transition_kind=kind,
    ).to_dict())


def git(repo, *args, env=None):
    completed = subprocess.run(
        ["git", *args],
        cwd=repo,
        env={**os.environ, **(env or {})},
        text=True,
        capture_output=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    return completed.stdout.strip()


def committed_candidate(repo):
    git(repo, "init", "-q")
    git(repo, "config", "user.name", "Review Test")
    git(repo, "config", "user.email", "review@example.invalid")
    (repo / "base.txt").write_text("base\n", encoding="utf-8")
    git(repo, "add", "base.txt")
    git(repo, "commit", "-q", "-m", "base")
    base = git(repo, "rev-parse", "HEAD")
    (repo / "content.txt").write_text("review me\n", encoding="utf-8")
    git(repo, "add", "content.txt")
    git(repo, "commit", "-q", "-m", "candidate")
    candidate = git(repo, "rev-parse", "HEAD")
    return base, candidate


def admitted_primary_for_commit(repo, base, candidate):
    patch = patch_identity_kernel.compute_patch_identity(
        repo, base_sha=base, candidate_sha=candidate
    )
    _TREE_DIFFS[(patch["base_tree_sha"], patch["candidate_tree_sha"])] = (
        (),
        patch["diff_sha256"],
    )
    first = admission.admit_review(
        repo,
        [],
        repository_binding="repo",
        task=task("primary", "primary"),
        current_source_identity={"head": candidate},
        current_tree_sha=patch["candidate_tree_sha"],
        patch_identity=patch,
        required_sections=("code",),
        equivalence_proof=None,
        format_only_proof=None,
        requested="review",
        changed_paths=None,
        security_trigger_paths=(),
    )
    assert isinstance(first, admission.Reserved)
    rows = list(first.records_to_append)
    append_settlement(
        rows,
        first.slot,
        ReviewOutcome.CONSUMED,
        terminal("primary", tree=patch["candidate_tree_sha"]),
    )
    return rows, first.generation


def assert_recommitted_content_carries(repo, base, candidate, rows, generation):
    patch = patch_identity_kernel.compute_patch_identity(
        repo, base_sha=base, candidate_sha=candidate
    )
    _TREE_DIFFS[(patch["base_tree_sha"], patch["candidate_tree_sha"])] = (
        (),
        patch["diff_sha256"],
    )
    result = admission.admit_review(
        repo,
        rows,
        repository_binding="repo",
        task=task("repeat", f"repeat-{candidate}"),
        current_source_identity={"head": candidate},
        current_tree_sha=patch["candidate_tree_sha"],
        patch_identity=patch,
        required_sections=("code",),
        equivalence_proof=None,
        format_only_proof=None,
        requested="review",
        changed_paths=None,
        security_trigger_paths=(),
    )
    assert isinstance(result, admission.Carry)
    assert result.generation.generation_id == generation.generation_id
    assert all(
        row["type"] != "review-slot-reservation-v1"
        for row in result.records_to_append
    )


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
    assert {verdict for _, verdict in result.verdicts} == {"pass"}
    assert result.records_to_append == ()


def test_carry_reports_an_authenticated_failing_verdict():
    first = admit([], identity("a"), review_task=task("failed", "failed"))
    rows = list(first.records_to_append)
    append_settlement(
        rows,
        first.slot,
        ReviewOutcome.CONSUMED,
        terminal("failed"),
        verdict="fail",
    )
    carried = admit(
        rows, identity("a"), review_task=task("repeat-fail", "repeat-fail")
    )
    assert isinstance(carried, admission.Carry)
    assert {verdict for _, verdict in carried.verdicts} == {"fail"}


def test_amended_commit_message_reuses_settled_generation_and_slots(tmp_path):
    repo = tmp_path
    base, original = committed_candidate(repo)
    rows, generation = admitted_primary_for_commit(repo, base, original)
    git(repo, "commit", "--amend", "-q", "-m", "amended message")
    amended = git(repo, "rev-parse", "HEAD")
    assert amended != original
    assert_recommitted_content_carries(repo, base, amended, rows, generation)


def test_new_author_date_reuses_settled_generation_and_slots(tmp_path):
    repo = tmp_path
    base, original = committed_candidate(repo)
    rows, generation = admitted_primary_for_commit(repo, base, original)
    dates = {
        "GIT_AUTHOR_DATE": "2031-01-02T03:04:05+00:00",
        "GIT_COMMITTER_DATE": "2031-01-02T03:04:06+00:00",
    }
    git(repo, "commit", "--amend", "--no-edit", "-q", env=dates)
    recommitted = git(repo, "rev-parse", "HEAD")
    assert recommitted != original
    assert_recommitted_content_carries(repo, base, recommitted, rows, generation)


def test_cherry_pick_onto_same_base_reuses_settled_generation_and_slots(tmp_path):
    repo = tmp_path
    base, original = committed_candidate(repo)
    rows, generation = admitted_primary_for_commit(repo, base, original)
    git(repo, "checkout", "-q", "-b", "replay", base)
    git(
        repo,
        "cherry-pick",
        original,
        env={"GIT_COMMITTER_DATE": "2032-02-03T04:05:06+00:00"},
    )
    cherry_picked = git(repo, "rev-parse", "HEAD")
    assert cherry_picked != original
    assert_recommitted_content_carries(
        repo, base, cherry_picked, rows, generation
    )


def test_delta_scope_is_changed_plus_security_paths():
    rows, previous = primary_history()
    changed = ("src/a.py", "src/b.py")
    _TREE_DIFFS[(previous.tree, "b" * 40)] = (changed, "e" * 64)
    link_generation(rows, previous, identity("b"))
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


def test_empty_caller_security_paths_cannot_narrow_kernel_scope():
    rows, previous = primary_history()
    changed = ("src/a.py",)
    _TREE_DIFFS[(previous.tree, "b" * 40)] = (changed, "e" * 64)
    link_generation(rows, previous, identity("b"))
    result = admit(
        rows,
        identity("b"),
        review_task=task("delta", "delta"),
        requested="delta",
        changed=changed,
        changed_digest="e" * 64,
        security=(),
    )
    assert isinstance(result, admission.Reserved)
    assert result.scoped_task["delta_scope"]["closure_paths"] == [
        "security/policy.py",
        "src/a.py",
    ]


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
        equivalence_proof=rows[-1],
        format_only_proof=None, requested="delta", changed_paths=None,
        security_trigger_paths=(),
    )
    assert isinstance(result, admission.Reserved)
    assert result.generation.generation_id == first.generation.generation_id
    assert result.slot.slot_kind == "delta"
    assert result.records_to_append[0]["sections"] == ["code"]

    rows.extend(result.records_to_append)
    repeated = admission.admit_review(
        _REPOSITORY,
        rows,
        repository_binding="repo",
        task=task("delta", "delta"),
        current_source_identity={"head": rebased["candidate_sha"]},
        current_tree_sha=rebased["candidate_tree_sha"],
        patch_identity=rebased,
        required_sections=("code", "security"),
        equivalence_proof=None,
        format_only_proof=None,
        requested="delta",
        changed_paths=None,
        security_trigger_paths=(),
    )
    assert isinstance(repeated, admission.Reserved)
    assert repeated.slot.reservation_id == result.slot.reservation_id

    full = admission.admit_review(
        _REPOSITORY,
        rows,
        repository_binding="repo",
        task=task("full", "full"),
        current_source_identity={"head": rebased["candidate_sha"]},
        current_tree_sha=rebased["candidate_tree_sha"],
        patch_identity=rebased,
        required_sections=("code", "security"),
        equivalence_proof=None,
        format_only_proof=None,
        requested="review",
        changed_paths=None,
        security_trigger_paths=(),
    )
    assert isinstance(full, admission.Blocked)
    assert full.code == "slots-exhausted"


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

    primary_rows, primary_generation = primary_history()
    link_generation(primary_rows, primary_generation, identity("b"))
    exhausted = admit(primary_rows, identity("b"), review_task=task("fresh", "fresh"))
    retries = admit(
        released_twice_history(), patch, review_task=task("third", "third")
    )
    oversized_paths = tuple(f"src/{index}.py" for index in range(301))
    _TREE_DIFFS[("a" * 40, "c" * 40)] = (oversized_paths, "f" * 64)
    link_generation(primary_rows, primary_generation, identity("c"))
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


def test_settled_idempotency_key_never_relaunches_or_bypasses_current_holder():
    patch = identity("a")
    first = admit([], patch, review_task=task("one", "old-key"))
    rows = list(first.records_to_append)
    append_settlement(
        rows, first.slot, ReviewOutcome.RELEASED,
        terminal("one", released=True),
    )
    retired = admit(rows, patch, review_task=task("one", "old-key"))
    assert isinstance(retired, admission.Blocked)
    assert retired.code == "reservation-conflict"

    second = admit(rows, patch, review_task=task("two", "current-key"))
    assert isinstance(second, admission.Reserved)
    rows.extend(second.records_to_append)
    retired_while_held = admit(rows, patch, review_task=task("one", "old-key"))
    assert isinstance(retired_while_held, admission.Blocked)
    assert retired_while_held.code == "reservation-conflict"
    competitor = admit(rows, patch, review_task=task("three", "other-key"))
    assert isinstance(competitor, admission.Blocked)
    assert competitor.code == "slot-held"
    assert competitor.evidence["reservation_id"] == second.slot.reservation_id
    append_settlement(
        rows, second.slot, ReviewOutcome.CONSUMED, terminal("two")
    )
    consumed_repeat = admit(rows, patch, review_task=task("two", "current-key"))
    assert isinstance(consumed_repeat, admission.Carry)
    assert consumed_repeat.dispatch is False


def test_unrelated_candidate_gets_fresh_lineage_and_primary_slot():
    rows, previous = primary_history()
    result = admit(rows, identity("b"), review_task=task("other", "other"))
    assert isinstance(result, admission.Reserved)
    assert result.slot.slot_kind == "primary"
    assert result.generation.predecessor_id is None
    assert result.generation.lineage_id != previous.lineage_id


def test_reviewed_tree_against_an_unproven_different_base_gets_no_primary_carry():
    rows, previous = primary_history()
    represented = {
        **identity("a"),
        "base_sha": "2" * 40,
        "base_tree_sha": "3" * 40,
    }
    result = admit(
        rows,
        represented,
        review_task=task("new-context", "new-context"),
    )

    assert isinstance(result, admission.Reserved)
    assert result.slot.slot_kind == "primary"
    assert result.generation.generation_id != previous.generation_id
    assert result.generation.predecessor_id is None
    assert result.generation.primary_origin_receipt is None


def test_caller_family_labels_cannot_open_a_second_pool():
    rows, generation = primary_history()
    spoofed = admit(
        rows,
        identity("a"),
        review_task={
            **task("spoofed", "spoofed"),
            "review_family": "trust",
            "family": "trust",
        },
    )
    assert isinstance(spoofed, admission.Carry)
    assert spoofed.generation.generation_id == generation.generation_id


def test_seen_content_with_changed_sections_cannot_receive_a_fresh_primary():
    rows, generation = primary_history()
    result = admission.admit_review(
        _REPOSITORY,
        rows,
        repository_binding="repo",
        task=task("more-sections", "more-sections"),
        current_source_identity={"head": identity("a")["candidate_sha"]},
        current_tree_sha=identity("a")["candidate_tree_sha"],
        patch_identity=identity("a"),
        required_sections=("code", "security"),
        equivalence_proof=None,
        format_only_proof=None,
        requested="review",
        changed_paths=None,
        security_trigger_paths=(),
    )
    assert isinstance(result, admission.Blocked)
    assert result.code == "missing-evidence"
    assert result.evidence["generation_id"] == generation.generation_id
    assert result.records_to_append == ()


def test_unreviewed_predecessor_breaks_inherited_primary_chain():
    rows, root = primary_history()
    middle_patch = identity("b")
    link_generation(rows, root, middle_patch)
    middle = admit(
        rows,
        middle_patch,
        review_task=task("middle-delta", "middle-delta"),
        requested="delta",
    )
    assert isinstance(middle, admission.Reserved)
    rows.extend(middle.records_to_append)

    leaf_patch = identity("c")
    link_generation(rows, middle.generation, leaf_patch)
    leaf = admit(
        rows,
        leaf_patch,
        review_task=task("leaf-primary", "leaf-primary"),
        requested="review",
    )
    assert isinstance(leaf, admission.Reserved)
    assert leaf.slot.slot_kind == "primary"
    assert leaf.generation.primary_origin_receipt is None


def test_consumed_delta_completes_an_anchored_inheritance_chain():
    rows, root = primary_history()
    middle_patch = identity("b")
    link_generation(rows, root, middle_patch)
    middle = admit(
        rows,
        middle_patch,
        review_task=task("middle-delta", "middle-delta"),
        requested="delta",
    )
    assert isinstance(middle, admission.Reserved)
    rows.extend(middle.records_to_append)
    append_settlement(
        rows,
        middle.slot,
        ReviewOutcome.CONSUMED,
        terminal("middle-delta", tree=middle.generation.tree),
    )

    leaf_patch = identity("c")
    link_generation(rows, middle.generation, leaf_patch)
    leaf = admit(
        rows,
        leaf_patch,
        review_task=task("leaf-delta", "leaf-delta"),
        requested="delta",
    )
    assert isinstance(leaf, admission.Reserved)
    assert leaf.slot.slot_kind == "delta"
    assert leaf.generation.primary_origin_receipt is not None


def test_chain_without_any_consumed_primary_admits_each_leaf_as_primary():
    root_patch = identity("a")
    root = admit(
        [], root_patch, review_task=task("root-primary", "root-primary")
    )
    assert isinstance(root, admission.Reserved)
    rows = list(root.records_to_append)
    middle_patch = identity("b")
    link_generation(rows, root.generation, middle_patch)
    middle = admit(
        rows,
        middle_patch,
        review_task=task("middle-primary", "middle-primary"),
    )
    assert isinstance(middle, admission.Reserved)
    assert middle.slot.slot_kind == "primary"
    rows.extend(middle.records_to_append)
    leaf_patch = identity("c")
    link_generation(rows, middle.generation, leaf_patch)
    leaf = admit(
        rows,
        leaf_patch,
        review_task=task("leaf-primary", "leaf-primary"),
    )
    assert isinstance(leaf, admission.Reserved)
    assert leaf.slot.slot_kind == "primary"
    assert leaf.generation.primary_origin_receipt is None


def test_released_root_primary_does_not_anchor_inheritance():
    root_patch = identity("a")
    root = admit(
        [], root_patch, review_task=task("root-primary", "root-primary")
    )
    assert isinstance(root, admission.Reserved)
    rows = list(root.records_to_append)
    append_settlement(
        rows,
        root.slot,
        ReviewOutcome.RELEASED,
        terminal("root-primary", released=True),
    )
    middle_patch = identity("b")
    link_generation(rows, root.generation, middle_patch)
    middle = admit(
        rows,
        middle_patch,
        review_task=task("middle-primary", "middle-primary"),
    )
    assert isinstance(middle, admission.Reserved)
    assert middle.slot.slot_kind == "primary"
    rows.extend(middle.records_to_append)
    leaf_patch = identity("c")
    link_generation(rows, middle.generation, leaf_patch)
    leaf = admit(
        rows,
        leaf_patch,
        review_task=task("leaf-primary", "leaf-primary"),
    )
    assert isinstance(leaf, admission.Reserved)
    assert leaf.slot.slot_kind == "primary"
    assert leaf.generation.primary_origin_receipt is None


def test_substantive_generation_needs_delta_before_exact_content_can_carry():
    rows, previous = primary_history()
    changed = identity("b")
    link_generation(rows, previous, changed)
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
    link_generation(rows, previous, identity("b"))
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


def test_caller_cannot_change_identity_diff_to_evade_seen_content_index():
    rows, generation = primary_history()
    forged = {**identity("a"), "diff_sha256": "f" * 64}
    result = admit(
        rows,
        forged,
        review_task=task("forged-content", "forged-content"),
    )
    assert isinstance(result, admission.Blocked)
    assert result.code == "invalid-proof"
    assert result.records_to_append == ()
    assert authority_projection.slot_state(
        rows, generation.generation_id, "delivery"
    ).primary_consumed


def test_oversized_delta_can_only_fall_back_to_an_explicit_fresh_primary():
    rows, previous = primary_history()
    target = identity("b")
    link_generation(rows, previous, target)
    oversized_paths = tuple(f"src/{index}.py" for index in range(301))
    _TREE_DIFFS[(previous.tree, target["candidate_tree_sha"])] = (
        oversized_paths,
        "8" * 64,
    )
    blocked = admit(
        rows, target, review_task=task("delta", "delta"), requested="delta"
    )
    assert isinstance(blocked, admission.Blocked)
    assert blocked.code == "oversized-delta"

    full = admit(
        rows, target, review_task=task("full", "full"), requested="review"
    )
    assert isinstance(full, admission.Reserved)
    assert full.slot.slot_kind == "primary"
    assert full.generation.predecessor_id is None
    assert full.generation.primary_origin_receipt is None
    assert full.generation.lineage_id != previous.lineage_id

    small_target = identity("c")
    small_rows, small_previous = primary_history()
    link_generation(small_rows, small_previous, small_target)
    _TREE_DIFFS[(small_previous.tree, small_target["candidate_tree_sha"])] = (
        ("src/one.py",),
        "7" * 64,
    )
    small_full = admit(
        small_rows,
        small_target,
        review_task=task("small-full", "small-full"),
        requested="review",
    )
    assert isinstance(small_full, admission.Blocked)
    assert small_full.code == "slots-exhausted"


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
