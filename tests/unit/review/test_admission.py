import hashlib
import json
import os
import subprocess
from types import SimpleNamespace

import pytest

# Parent regression evidence was run from a clean `git archive 6215393` checkout
# after copying this file, with:
# `PYTHONPATH=src <repair>/.venv/bin/pytest -q
# tests/unit/review/test_admission.py::test_admission_rejects_caller_supplied_launch_reason
# tests/unit/review/test_admission.py::test_base_motion_invalidates_trust_carry_for_both_requested_kinds
# tests/unit/review/test_admission.py::test_successor_initializes_each_family_from_its_own_predecessor
# tests/unit/review/test_admission.py::test_blocked_trust_obligation_cannot_be_covered_by_an_old_delta
# tests/unit/review/test_admission.py::test_consumed_delivery_task_cannot_be_relabelled_nonverdict_without_contract
# tests/unit/review/test_admission.py::test_native_delivery_settlement_rejects_nonverdict_and_cross_family_terminals
# tests/unit/review/test_admission.py::test_caller_rehashed_stale_trust_digests_cannot_carry_old_receipt
# tests/unit/review/test_admission.py::test_package_declared_nonverdict_intents_launch_without_slot_authority`.
# All fourteen parametrized cases failed on 6215393.

from loopzero.kernel import (
    authority_projection,
    authority_store,
    patch_identity as patch_identity_kernel,
    review_state,
)
from loopzero.review import admission, authority as review_authority, chain
from loopzero.review.trust_claims import TrustClaim, normalize_manifest
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


def generation_proof(source, target, *, kind="patch-equivalence", overlap=()):
    proof = (
        equivalence(source, target, overlap=overlap)
        if kind == "patch-equivalence"
        else {
            "schema_version": "format-only-v1",
            "from_identity_digest": review_state.patch_identity_digest(source),
            "from_tree": source["candidate_tree_sha"],
            "to_identity_digest": review_state.patch_identity_digest(target),
            "to_tree": target["candidate_tree_sha"],
            "verified": True,
        }
    )
    return review_state.GenerationProofV1(
        proof_kind=kind,
        from_identity=source,
        from_tree=source["candidate_tree_sha"],
        to_identity=target,
        to_tree=target["candidate_tree_sha"],
        proof=proof,
    ).to_dict()


def task(name="review", key="key"):
    return {
        "task_id": name,
        "idempotency_key": key,
        "review_intent": "delivery-code-review",
    }


def trust_task(
    patch,
    *,
    name="trust",
    key="trust",
    manifest="6" * 64,
    claim_set="7" * 64,
    invalidated=("a" * 64,),
    retirements=(),
):
    current_labels = tuple(invalidated) or (
        (("a" * 64,) if retirements else ("a" * 64,))
    )
    current_claims = tuple(
        sorted(
            (
                TrustClaim("actors_assets", f"claim-{label}", ())
                for label in current_labels
                if label not in retirements
            ),
            key=lambda claim: claim.claim_id,
        )
    )
    retired_claims = tuple(
        sorted(
            (
                TrustClaim("actors_assets", f"claim-{label}", ())
                for label in retirements
            ),
            key=lambda claim: claim.claim_id,
        )
    )
    trust_manifest = {
        "actors_assets": [
            {"text": claim.text, "paths": list(claim.paths)}
            for claim in current_claims
        ],
        "risk_paths": [],
    }
    derived_claim_set = normalize_manifest(trust_manifest)
    payload = {
        "generation_ref": review_state.generation_id_for(
            "repo", patch, patch["candidate_tree_sha"], ("trust",)
        ),
        "source_identity": review_state.canonical_record_digest(
            {"head": patch["candidate_sha"]}
        ),
        "tree_sha": patch["candidate_tree_sha"],
        "manifest_sha256": manifest,
        "claim_set_digest": derived_claim_set.claim_set_digest,
        "invalidated_claims": [claim.to_dict() for claim in current_claims]
        if invalidated
        else [],
        "retirements": [claim.to_dict() for claim in retired_claims],
        "carried_receipt_digests": [],
        "delta_from_tree_sha": None,
        "changed_paths": [],
        "changed_paths_digest": hashlib.sha256(
            json.dumps(
                ["trust-claim-changed-paths-v1", []],
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
        ).hexdigest(),
        "base_moved": False,
        "risk_paths_added": [],
        "previous_receipt_digest": None,
    }
    payload["task_hash"] = review_state.canonical_record_digest(payload)
    return {
        **task(name, key),
        "review_intent": "trust-manifest-verification",
        "trust_claim_task": payload,
        "trust_claim_manifest": trust_manifest,
    }


def test_admission_rejects_caller_supplied_launch_reason():
    result = admit(
        [],
        identity("a"),
        review_task={**task(), "launch_reason": "initial"},
    )
    assert isinstance(result, admission.Blocked)
    assert result.code == "invalid-launch-label"
    assert result.evidence["message"] == "Blocked"
    assert result.evidence["cause_class"] == "InvalidLaunchLabelError"
    assert "initial" not in repr(result.evidence)

    missing_identity = admit(
        [],
        identity("a"),
        review_task={"launch_reason": "initial"},
    )
    assert isinstance(missing_identity, admission.Blocked)
    assert missing_identity.code == "missing-evidence"
    assert missing_identity.evidence["cause_class"] == "MissingEvidenceError"


def test_all_launch_reasons_are_observable_through_admission(monkeypatch):
    monkeypatch.setattr(
        admission, "security_trigger_paths_between", lambda repo, source, target: ()
    )
    initial = admit([], identity("a"))
    assert isinstance(initial, admission.Reserved)
    assert initial.launch_reason == "initial"

    monkeypatch.setattr(
        admission,
        "security_trigger_paths_between",
        lambda repo, source, target: ("security/policy.py",),
    )
    security = admit([], identity("b"), review_task=task("security", "security"))
    assert isinstance(security, admission.Reserved)
    assert security.launch_reason == "security-path"

    trust = admit(
        [],
        identity("c"),
        review_task=trust_task(identity("c")),
    )
    assert isinstance(trust, admission.Reserved)
    assert trust.launch_reason == "trust-verification"

    rows, previous = primary_history()
    target = identity("d")
    link_generation(rows, previous, target)
    bounded = admit(
        rows,
        target,
        review_task=task("bounded", "bounded"),
        requested="delta",
    )
    assert isinstance(bounded, admission.Reserved)
    assert bounded.launch_reason == "bounded-delta"

    trust_rows = list(trust.records_to_append)
    append_settlement(
        trust_rows,
        trust.slot,
        ReviewOutcome.CONSUMED,
        terminal("trust", tree=trust.generation.tree),
    )
    trust_target = identity("e")
    link_generation(trust_rows, trust.generation, trust_target)
    trust_delta = admit(
        trust_rows,
        trust_target,
        review_task=trust_task(
            trust_target, name="trust-delta", key="trust-delta"
        ),
        requested="delta",
    )
    assert isinstance(trust_delta, admission.Reserved)
    assert trust_delta.launch_reason == "trust-delta"

    for transition, reason in (
        ("owner-requested", "owner-requested"),
        ("supersession", "supersession"),
    ):
        transition_rows, predecessor = primary_history()
        transition_target = identity("f" if transition == "owner-requested" else "9")
        if transition == "owner-requested":
            transition_rows.append(
                {
                    "type": "review-generation-owner-request",
                    "generation_id": predecessor.generation_id,
                }
            )
        else:
            transition_rows.append(
                {
                    "type": "attempt-supersession",
                    "review_generation_id": predecessor.generation_id,
                    "superseding_source_identity": {
                        "head": transition_target["candidate_sha"]
                    },
                }
            )
        link_generation(
            transition_rows, predecessor, transition_target, kind=transition
        )
        observed = admit(
            transition_rows,
            transition_target,
            review_task=task(reason, reason),
            requested="delta",
        )
        assert isinstance(observed, admission.Reserved)
        assert observed.launch_reason == reason

    first = admit([], identity("8"), review_task=task("failed", "failed"))
    assert isinstance(first, admission.Reserved)
    retry_rows = list(first.records_to_append)
    append_settlement(
        retry_rows,
        first.slot,
        ReviewOutcome.RELEASED,
        terminal("failed", tree=first.generation.tree, released=True),
    )
    retry = admit(
        retry_rows,
        identity("8"),
        review_task=task("retry", "retry"),
    )
    assert isinstance(retry, admission.Reserved)
    assert retry.launch_reason == "infrastructure-retry"


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
    result = {
        **result,
        "snapshot_tree_sha": result.get("snapshot_tree_sha") or generation.tree,
        "review_family": reservation.family,
        "review_intent": (
            "trust-manifest-verification"
            if reservation.family == "trust"
            else "delivery-code-review"
        ),
    }
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


def admit_all_sections(
    rows, patch, name, *, proof=None, kind="patch-equivalence", requested="delta"
):
    return admission.admit_review(
        _REPOSITORY,
        rows,
        repository_binding="repo",
        task=task(name, name),
        current_source_identity={"head": patch["candidate_sha"]},
        current_tree_sha=patch["candidate_tree_sha"],
        patch_identity=patch,
        required_sections=("code", "security"),
        equivalence_proof=proof if kind == "patch-equivalence" else None,
        format_only_proof=proof if kind == "format-only" else None,
        requested=requested,
        changed_paths=None,
        security_trigger_paths=(),
    )


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
        "review-family-coverage-v1",
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


def withheld_proof_hop_ledger():
    original = identity("a")
    primary = admit_all_sections(
        [], original, "primary", requested="review"
    )
    assert isinstance(primary, admission.Reserved)
    rows = list(primary.records_to_append)
    append_settlement(
        rows,
        primary.slot,
        ReviewOutcome.CONSUMED,
        {**terminal("primary"), "patch_identity": original},
    )
    primary_terminal = next(
        row for row in rows if row.get("type") == "attempt-terminal"
    )

    rebased = identity("b")
    first_proof = generation_proof(
        original,
        rebased,
        overlap=("security/policy.py",),
    )
    rows.append(first_proof)
    first_hop = admit_all_sections(
        rows, rebased, "delta-b", proof=first_proof
    )
    assert isinstance(first_hop, admission.Reserved)
    rows.extend(first_hop.records_to_append)

    equivalent = identity("c")
    equivalence_proof = generation_proof(rebased, equivalent)
    rows.append(equivalence_proof)
    equivalence_hop = admit_all_sections(
        rows, equivalent, "delta-c", proof=equivalence_proof
    )
    rows.extend(equivalence_hop.records_to_append)

    formatted = identity("d")
    format_proof = generation_proof(rebased, formatted, kind="format-only")
    rows.append(format_proof)
    format_hop = admit_all_sections(
        rows,
        formatted,
        "delta-d",
        proof=format_proof,
        kind="format-only",
    )
    rows.extend(format_hop.records_to_append)
    return (
        rows,
        primary,
        primary_terminal,
        (original, rebased, equivalent, formatted),
        equivalence_hop,
        format_hop,
    )


def test_proof_hops_never_regrant_a_section_withheld_by_an_earlier_carry():
    (
        rows,
        primary,
        _primary_terminal,
        (_original, rebased, equivalent, formatted),
        equivalence_hop,
        format_hop,
    ) = withheld_proof_hop_ledger()

    assert isinstance(equivalence_hop, admission.Blocked)
    assert equivalence_hop.code == "slot-held"
    assert isinstance(format_hop, admission.Blocked)
    assert format_hop.code == "slot-held"
    carries = {
        (
            carry.from_identity["candidate_tree_sha"],
            carry.to_identity["candidate_tree_sha"],
        ): carry.sections
        for carry in authority_projection.generation_carries(rows)
    }
    assert carries == {
        ("a" * 40, rebased["candidate_tree_sha"]): ("code",),
        (rebased["candidate_tree_sha"], equivalent["candidate_tree_sha"]): (
            "code",
        ),
        (rebased["candidate_tree_sha"], formatted["candidate_tree_sha"]): (
            "code",
        ),
    }
    state = authority_projection.slot_state(
        rows, primary.generation.generation_id, "delivery"
    )
    assert state.outstanding is not None
    assert state.outstanding.task_id == "delta-b"
    assert not state.delta_consumed


def test_admission_resolution_and_publication_agree_per_section_on_proof_hops():
    from loopzero.review import _tree_coverage as coverage

    rows, _primary, primary_terminal, heads, _equivalence_hop, _format_hop = (
        withheld_proof_hop_ledger()
    )
    for head in heads:
        resolution = review_state.resolve_generation(
            rows,
            repository_binding="repo",
            patch_identity=head,
            tree_sha=head["candidate_tree_sha"],
            required_sections=("code", "security"),
            equivalence_proof=None,
            format_only_proof=None,
        )
        assert resolution.kind == "same"
        assert resolution.carry is not None
        admitted_sections = set(resolution.carry.sections)
        for section in ("code", "security"):
            assert (section in admitted_sections) is coverage._generation_chain_covers_tree(
                rows,
                primary_terminal,
                lens=section,
                current_tree=head["candidate_tree_sha"],
            )


@pytest.mark.parametrize("requested", ["review", "delta"])
def test_base_motion_invalidates_trust_carry_for_both_requested_kinds(requested):
    original = identity("a")
    delivery = admit_all_sections([], original, "delivery", requested="review")
    rows = list(delivery.records_to_append)
    append_settlement(rows, delivery.slot, ReviewOutcome.CONSUMED, terminal("delivery"))
    trust = admission.admit_review(
        _REPOSITORY, rows, repository_binding="repo", task=trust_task(original),
        current_source_identity={"head": original["candidate_sha"]},
        current_tree_sha=original["candidate_tree_sha"], patch_identity=original,
        required_sections=("trust",), equivalence_proof=None,
        format_only_proof=None, requested="review", changed_paths=None,
        security_trigger_paths=(),
    )
    assert isinstance(trust, admission.Reserved)
    rows.extend(trust.records_to_append)
    append_settlement(rows, trust.slot, ReviewOutcome.CONSUMED, terminal("trust"))

    target = identity("b")
    proof = generation_proof(
        original, target, overlap=("security/policy.py",)
    )
    rows.append(proof)
    delivery_delta = admit_all_sections(
        rows, target, "delivery-delta", proof=proof
    )
    assert isinstance(delivery_delta, admission.Reserved)
    rows.extend(delivery_delta.records_to_append)
    target_trust_task = trust_task(
        target, name="trust-carry", key="trust-carry"
    )
    trust_delta = admission.admit_review(
        _REPOSITORY, rows, repository_binding="repo",
        task=target_trust_task,
        current_source_identity={"head": target["candidate_sha"]},
        current_tree_sha=target["candidate_tree_sha"], patch_identity=target,
        required_sections=("trust",), equivalence_proof=proof,
        format_only_proof=None, requested=requested, changed_paths=None,
        security_trigger_paths=(),
    )
    assert isinstance(trust_delta, admission.Reserved)
    assert trust_delta.slot.family == "trust"
    assert trust_delta.slot.slot_kind == "delta"
    assert trust_delta.scoped_task["delta_scope"]["invalidated_claim_ids"] == [
        target_trust_task["trust_claim_task"]["invalidated_claims"][0]["claim_id"]
    ]
    delivery_carry = next(
        row for row in delivery_delta.records_to_append
        if row["type"] == "generation-carry-v1"
    )
    assert delivery_carry["family"] == "delivery"
    assert delivery_carry["sections"] == ["code"]

    # Parent proof (6215393): running this node after copying it onto the
    # reviewed head returns Carry for both parameters, so both cases fail.


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
    invalid_label = admit(
        [], patch, review_task={**task("label", "label"), "launch_reason": "initial"}
    )
    results = {
        result.code
        for result in (
            stale, invalid, missing, held, conflict, exhausted, retries, oversized,
            invalid_label,
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
        "invalid-launch-label",
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
    assert first.slot.reservation_id not in repr(competing.evidence)


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
    assert second.slot.reservation_id not in repr(competitor.evidence)
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


@pytest.mark.parametrize("first_family", ["delivery", "trust"])
def test_delivery_and_trust_own_independent_coverage_on_identical_content(first_family):
    patch = identity("a")
    first_task = task("delivery", "delivery")
    second_task = trust_task(patch)
    first_sections = ("code",)
    second_sections = ("trust",)
    if first_family == "trust":
        first_task, second_task = second_task, first_task
        first_sections, second_sections = second_sections, first_sections

    first = admission.admit_review(
        _REPOSITORY, [], repository_binding="repo", task=first_task,
        current_source_identity={"head": patch["candidate_sha"]},
        current_tree_sha=patch["candidate_tree_sha"], patch_identity=patch,
        required_sections=first_sections, equivalence_proof=None,
        format_only_proof=None, requested="review", changed_paths=None,
        security_trigger_paths=(),
    )
    assert isinstance(first, admission.Reserved)
    rows = list(first.records_to_append)
    append_settlement(
        rows, first.slot, ReviewOutcome.CONSUMED,
        terminal(first.slot.task_id, tree=patch["candidate_tree_sha"]),
    )
    second = admission.admit_review(
        _REPOSITORY, rows, repository_binding="repo", task=second_task,
        current_source_identity={"head": patch["candidate_sha"]},
        current_tree_sha=patch["candidate_tree_sha"], patch_identity=patch,
        required_sections=second_sections, equivalence_proof=None,
        format_only_proof=None, requested="review", changed_paths=None,
        security_trigger_paths=(),
    )

    assert isinstance(second, admission.Reserved)
    assert second.slot.slot_kind == "primary"
    assert second.slot.family != first.slot.family
    assert second.generation.generation_id == first.generation.generation_id
    assert second.scoped_task["required_sections"] == list(second_sections)


@pytest.mark.parametrize("first_family", ["delivery", "trust"])
def test_successor_initializes_each_family_from_its_own_predecessor(first_family):
    original = identity("a")
    delivery = admit([], original, review_task=task("delivery", "delivery"))
    rows = list(delivery.records_to_append)
    append_settlement(
        rows, delivery.slot, ReviewOutcome.CONSUMED, terminal("delivery")
    )
    trust = admission.admit_review(
        _REPOSITORY,
        rows,
        repository_binding="repo",
        task=trust_task(original),
        current_source_identity={"head": original["candidate_sha"]},
        current_tree_sha=original["candidate_tree_sha"],
        patch_identity=original,
        required_sections=("trust",),
        equivalence_proof=None,
        format_only_proof=None,
        requested="review",
        changed_paths=None,
        security_trigger_paths=(),
    )
    rows.extend(trust.records_to_append)
    append_settlement(rows, trust.slot, ReviewOutcome.CONSUMED, terminal("trust"))

    target = identity("b")
    link_generation(rows, delivery.generation, target)

    def family_delta(family, suffix):
        if family == "delivery":
            return admit(
                rows,
                target,
                review_task=task(f"delivery-{suffix}", f"delivery-{suffix}"),
                requested="delta",
            )
        return admission.admit_review(
            _REPOSITORY,
            rows,
            repository_binding="repo",
            task=trust_task(
                target, name=f"trust-{suffix}", key=f"trust-{suffix}"
            ),
            current_source_identity={"head": target["candidate_sha"]},
            current_tree_sha=target["candidate_tree_sha"],
            patch_identity=target,
            required_sections=("trust",),
            equivalence_proof=None,
            format_only_proof=None,
            requested="delta",
            changed_paths=None,
            security_trigger_paths=(),
        )

    first = family_delta(first_family, "first")
    assert isinstance(first, admission.Reserved)
    assert first.slot.slot_kind == "delta"
    rows.extend(first.records_to_append)
    second_family = "trust" if first_family == "delivery" else "delivery"
    second = family_delta(second_family, "second")
    assert isinstance(second, admission.Reserved)
    assert second.slot.slot_kind == "delta"
    assert second.generation.generation_id == first.generation.generation_id

    # Parent proof (6215393): copying this node onto the reviewed head and
    # running it blocks the second family with missing-evidence in both orders.


def test_trust_carry_key_binds_manifest_and_claim_set_on_the_same_tree():
    patch = identity("a")
    first = admission.admit_review(
        _REPOSITORY, [], repository_binding="repo", task=trust_task(patch),
        current_source_identity={"head": patch["candidate_sha"]},
        current_tree_sha=patch["candidate_tree_sha"], patch_identity=patch,
        required_sections=("trust",), equivalence_proof=None,
        format_only_proof=None, requested="review", changed_paths=None,
        security_trigger_paths=(),
    )
    assert isinstance(first, admission.Reserved)
    rows = list(first.records_to_append)
    append_settlement(
        rows, first.slot, ReviewOutcome.CONSUMED,
        terminal("trust", tree=patch["candidate_tree_sha"]),
    )

    carried = admission.admit_review(
        _REPOSITORY, rows, repository_binding="repo",
        task=trust_task(patch, name="same", key="same", invalidated=()),
        current_source_identity={"head": patch["candidate_sha"]},
        current_tree_sha=patch["candidate_tree_sha"], patch_identity=patch,
        required_sections=("trust",), equivalence_proof=None,
        format_only_proof=None, requested="review", changed_paths=None,
        security_trigger_paths=(),
    )
    assert isinstance(carried, admission.Carry)

    changed_task = trust_task(
        patch, name="changed", key="changed", manifest="8" * 64,
        claim_set="9" * 64, invalidated=("b" * 64,),
    )
    changed = admission.admit_review(
        _REPOSITORY, rows, repository_binding="repo",
        task=changed_task,
        current_source_identity={"head": patch["candidate_sha"]},
        current_tree_sha=patch["candidate_tree_sha"], patch_identity=patch,
        required_sections=("trust",), equivalence_proof=None,
        format_only_proof=None, requested="review", changed_paths=None,
        security_trigger_paths=(),
    )
    assert isinstance(changed, admission.Reserved)
    assert changed.slot.slot_kind == "delta"
    assert changed.scoped_task["delta_scope"]["invalidated_claim_ids"] == [
        changed_task["trust_claim_task"]["invalidated_claims"][0]["claim_id"]
    ]


def test_caller_rehashed_stale_trust_digests_cannot_carry_old_receipt():
    patch = identity("a")
    initial_task = trust_task(patch)
    first = admission.admit_review(
        _REPOSITORY, [], repository_binding="repo", task=initial_task,
        current_source_identity={"head": patch["candidate_sha"]},
        current_tree_sha=patch["candidate_tree_sha"], patch_identity=patch,
        required_sections=("trust",), equivalence_proof=None,
        format_only_proof=None, requested="review", changed_paths=None,
        security_trigger_paths=(),
    )
    rows = list(first.records_to_append)
    append_settlement(rows, first.slot, ReviewOutcome.CONSUMED, terminal("trust"))

    forged = trust_task(
        patch, name="forged", key="forged", invalidated=("b" * 64,)
    )
    forged_claim_task = dict(forged["trust_claim_task"])
    forged_claim_task["manifest_sha256"] = initial_task["trust_claim_task"][
        "manifest_sha256"
    ]
    forged_claim_task["claim_set_digest"] = initial_task["trust_claim_task"][
        "claim_set_digest"
    ]
    forged_claim_task["task_hash"] = review_state.canonical_record_digest(
        {
            key: value
            for key, value in forged_claim_task.items()
            if key != "task_hash"
        }
    )
    forged["trust_claim_task"] = forged_claim_task
    result = admission.admit_review(
        _REPOSITORY, rows, repository_binding="repo", task=forged,
        current_source_identity={"head": patch["candidate_sha"]},
        current_tree_sha=patch["candidate_tree_sha"], patch_identity=patch,
        required_sections=("trust",), equivalence_proof=None,
        format_only_proof=None, requested="review", changed_paths=None,
        security_trigger_paths=(),
    )
    assert isinstance(result, admission.Blocked)
    assert result.code == "missing-evidence"
    assert result.records_to_append == ()

    # Parent proof (6215393): copying this node onto the reviewed head returns
    # Carry because only the caller-recomputable task hash and stale digests are
    # checked there.


def test_manifest_only_retirement_is_a_trust_delta():
    patch = identity("a")
    first_task = trust_task(
        patch, invalidated=("a" * 64, "b" * 64), claim_set="7" * 64
    )
    first = admission.admit_review(
        _REPOSITORY, [], repository_binding="repo", task=first_task,
        current_source_identity={"head": patch["candidate_sha"]},
        current_tree_sha=patch["candidate_tree_sha"], patch_identity=patch,
        required_sections=("trust",), equivalence_proof=None,
        format_only_proof=None, requested="review", changed_paths=None,
        security_trigger_paths=(),
    )
    rows = list(first.records_to_append)
    append_settlement(rows, first.slot, ReviewOutcome.CONSUMED, terminal("trust"))
    retired_task = trust_task(
        patch, name="retire", key="retire", manifest="8" * 64,
        claim_set="9" * 64, invalidated=(), retirements=("b" * 64,),
    )
    retired = admission.admit_review(
        _REPOSITORY, rows, repository_binding="repo",
        task=retired_task,
        current_source_identity={"head": patch["candidate_sha"]},
        current_tree_sha=patch["candidate_tree_sha"], patch_identity=patch,
        required_sections=("trust",), equivalence_proof=None,
        format_only_proof=None, requested="review", changed_paths=None,
        security_trigger_paths=(),
    )
    assert isinstance(retired, admission.Reserved)
    assert retired.slot.slot_kind == "delta"
    assert retired.scoped_task["delta_scope"]["retirement_claim_ids"] == [
        retired_task["trust_claim_task"]["retirements"][0]["claim_id"]
    ]


def test_blocked_trust_obligation_cannot_be_covered_by_an_old_delta():
    patch = identity("a")
    primary = admission.admit_review(
        _REPOSITORY, [], repository_binding="repo", task=trust_task(patch),
        current_source_identity={"head": patch["candidate_sha"]},
        current_tree_sha=patch["candidate_tree_sha"], patch_identity=patch,
        required_sections=("trust",), equivalence_proof=None,
        format_only_proof=None, requested="review", changed_paths=None,
        security_trigger_paths=(),
    )
    rows = list(primary.records_to_append)
    append_settlement(rows, primary.slot, ReviewOutcome.CONSUMED, terminal("trust"))
    delta = admission.admit_review(
        _REPOSITORY, rows, repository_binding="repo",
        task=trust_task(
            patch, name="trust-b", key="trust-b", manifest="8" * 64,
            claim_set="9" * 64, invalidated=("b" * 64,),
        ),
        current_source_identity={"head": patch["candidate_sha"]},
        current_tree_sha=patch["candidate_tree_sha"], patch_identity=patch,
        required_sections=("trust",), equivalence_proof=None,
        format_only_proof=None, requested="delta", changed_paths=None,
        security_trigger_paths=(),
    )
    rows.extend(delta.records_to_append)
    append_settlement(rows, delta.slot, ReviewOutcome.CONSUMED, terminal("trust-b"))

    obligation_c = trust_task(
        patch, name="trust-c", key="trust-c", manifest="a" * 64,
        claim_set="b" * 64, invalidated=("c" * 64,),
    )
    blocked = admission.admit_review(
        _REPOSITORY, rows, repository_binding="repo", task=obligation_c,
        current_source_identity={"head": patch["candidate_sha"]},
        current_tree_sha=patch["candidate_tree_sha"], patch_identity=patch,
        required_sections=("trust",), equivalence_proof=None,
        format_only_proof=None, requested="delta", changed_paths=None,
        security_trigger_paths=(),
    )
    assert isinstance(blocked, admission.Blocked)
    assert blocked.code == "slots-exhausted"
    assert blocked.records_to_append == ()
    repeated = admission.admit_review(
        _REPOSITORY, [*rows, *blocked.records_to_append],
        repository_binding="repo", task=obligation_c,
        current_source_identity={"head": patch["candidate_sha"]},
        current_tree_sha=patch["candidate_tree_sha"], patch_identity=patch,
        required_sections=("trust",), equivalence_proof=None,
        format_only_proof=None, requested="delta", changed_paths=None,
        security_trigger_paths=(),
    )
    assert isinstance(repeated, admission.Blocked)
    assert repeated.code == "slots-exhausted"

    # Parent proof (6215393): persisting the first Blocked.records_to_append and
    # retrying returns Carry, so this test fails on the reviewed head.


@pytest.mark.parametrize("intent", ["discovery", "resolution-adjudication"])
def test_package_declared_nonverdict_intents_launch_without_slot_authority(intent):
    patch = identity("a")
    result = admit(
        [], patch,
        review_task={
            **task(intent, intent),
            "review_intent": intent,
            "task_contract": {"review_intent": intent, "run_id": "run"},
        },
    )
    assert isinstance(result, admission.NonVerdictReviewLaunch)
    assert result.family is None
    assert [row["type"] for row in result.records_to_append] == [
        "review-nonverdict-launch-v1"
    ]
    assert result.launch_record["task_id"] == intent
    assert result.launch_record["attempt_index"] == 0
    assert result.launch_record["run_id"] == "run"


def test_unknown_or_contradictory_intent_cannot_bypass_delivery_admission():
    patch = identity("a")
    unknown = admit(
        [], patch,
        review_task={**task("unknown", "unknown"), "review_intent": "free-label"},
    )
    assert isinstance(unknown, admission.Blocked)
    assert unknown.code == "unknown-intent"

    bypass = admit(
        [], patch,
        review_task={
            **task("bypass", "bypass"),
            "review_intent": "discovery",
            "task_contract": {"review_intent": "delivery-code-review"},
        },
    )
    assert isinstance(bypass, admission.Blocked)
    assert bypass.code == "unknown-intent"


def test_consumed_delivery_task_cannot_be_relabelled_nonverdict_without_contract():
    patch = identity("a")
    delivery = admit([], patch)
    rows = list(delivery.records_to_append)
    append_settlement(
        rows, delivery.slot, ReviewOutcome.CONSUMED, terminal("review")
    )
    relabelled = admit(
        rows,
        patch,
        review_task={
            "task_id": "review",
            "idempotency_key": "key",
            "review_intent": "discovery",
        },
    )
    assert isinstance(relabelled, admission.Blocked)
    assert relabelled.code == "unknown-intent"
    assert relabelled.records_to_append == ()

    # Parent proof (6215393): copying this node onto the reviewed head returns
    # NonVerdictReviewLaunch and therefore fails this assertion.


def test_nonverdict_terminal_can_never_supply_delivery_coverage():
    from loopzero.review import _tree_coverage

    patch = identity("a")
    delivery = admit([], patch)
    rows = list(delivery.records_to_append)
    forged_terminal = {
        **terminal("discovery", tree=patch["candidate_tree_sha"]),
        "patch_identity": patch,
        "task_contract": {"review_intent": "discovery"},
    }
    assert not _tree_coverage._generation_chain_covers_tree(
        rows,
        forged_terminal,
        lens="code",
        current_tree=patch["candidate_tree_sha"],
    )


@pytest.mark.parametrize(
    ("intent", "family"),
    [
        ("discovery", None),
        ("resolution-adjudication", None),
        ("trust-manifest-verification", "trust"),
        (None, "delivery"),
    ],
)
def test_native_delivery_settlement_rejects_nonverdict_and_cross_family_terminals(
    intent, family
):
    patch = identity("a")
    delivery = admit([], patch)
    rows = list(delivery.records_to_append)
    candidate = {
        **terminal("review", tree=patch["candidate_tree_sha"]),
    }
    candidate.pop("review_intent", None)
    candidate.pop("review_family", None)
    if intent is not None:
        candidate["review_intent"] = intent
    if family is not None:
        candidate["review_family"] = family
    rows.extend(
        [
            candidate,
            {"type": "verdict", "task_id": "review", "verdict": "pass"},
        ]
    )
    with pytest.raises(review_state.ReviewSlotError):
        review_state.settle_review_slot(
            _REPOSITORY,
            rows,
            reservation=delivery.slot,
            outcome=ReviewOutcome.CONSUMED,
            terminal_ref=candidate,
        )

    # Parent proof (6215393): copying this node onto the reviewed head and
    # running all three parameters produces consumed settlements.


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
    assert generation.generation_id not in repr(result.evidence)
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
