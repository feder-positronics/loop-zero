import hashlib
import json
from dataclasses import FrozenInstanceError

import pytest

from loopzero.review.trust_claims import (
    ClaimVerdict,
    TrustClaimError,
    build_trust_claim_task,
    claim_id,
    compose_claim_verdict,
    invalidate_claims,
    legacy_receipt,
    normalize_manifest,
    receipt_covers,
)


SHA = "a" * 64
TREE = "1" * 40


def manifest(*claims, risk_paths=("src/auth.py",)):
    return {
        "risk_paths": list(risk_paths),
        "actors_assets": list(claims),
    }


def legacy(raw, *, source="source-1", tree=TREE, digest=SHA):
    return legacy_receipt(
        raw,
        source_identity=source,
        tree_sha=tree,
        manifest_sha256=digest,
        verifier_run_id="legacy-run",
        verifier_task_id="legacy-task",
    )


def task_for(previous, current, *, changed=(), base_moved=False, added=()):
    invalidation = invalidate_claims(
        previous,
        current,
        changed_paths=changed,
        base_moved=base_moved,
        risk_paths_added=added,
    )
    return build_trust_claim_task(
        current,
        invalidation,
        generation_ref="generation",
        source_identity="source-2",
        tree_sha="2" * 40,
        manifest_sha256="b" * 64,
        delta_from_tree_sha=TREE,
        changed_paths=changed,
    )


def fresh(task, verdicts=None):
    verdicts = verdicts or {}
    return {
        identity: {
            "verdict": verdicts.get(identity, "pass"),
            "verifier_run_id": "fresh-run",
            "verifier_task_id": "fresh-task",
            "evidence_digest": "c" * 64,
            "verified_tree_sha": task.tree_sha,
            "task_hash": task.task_hash,
        }
        for identity in {
            *(claim.claim_id for claim in task.invalidated_claims),
            *task.retirements,
        }
    }


def test_claim_identity_is_stable_under_manifest_path_order_and_crlf():
    left = normalize_manifest(
        manifest({"text": "  cafe\u0301\r\nline  ", "paths": ["z", "a", "a"]})
    )
    right = normalize_manifest(
        manifest({"text": "caf\u00e9\nline", "paths": ["a", "z"]})
    )
    assert left.claims == right.claims
    assert left.claim_set_digest == right.claim_set_digest
    expected = hashlib.sha256(
        json.dumps(
            ["trust-claim-v1", "actors_assets", "caf\u00e9\nline", ["a", "z"]],
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()
    assert left.claims[0].claim_id == f"tc_{expected}"
    assert claim_id("actors_assets", " caf\u00e9\nline ", ["z", "a"]) == (
        left.claims[0].claim_id
    )


@pytest.mark.parametrize(
    "raw,match",
    [
        (manifest("same", "same"), "duplicate"),
        (manifest({"text": "claim", "paths": ["../secret"]}), "canonical"),
        (manifest({"text": "claim", "paths": ["src\\secret"]}), "canonical"),
        ({"risk_paths": [], "surprise": []}, "unknown"),
        ({"risk_paths": [], "actors_assets": "claim"}, "must be a list"),
        (manifest({"text": 7, "paths": []}), "text must be a string"),
        (manifest("  "), "must not be empty"),
    ],
)
def test_manifest_rejects_duplicates_bad_paths_unknown_fields_and_bad_text(raw, match):
    with pytest.raises(TrustClaimError, match=match):
        normalize_manifest(raw)


def test_invalidation_causes_are_independent_and_scoped():
    raw = manifest(
        {"text": "auth", "paths": ["src/auth.py"]},
        {"text": "db", "paths": ["src/db.py"]},
        "repository assumption",
    )
    prior = legacy(raw)
    current = normalize_manifest(raw)
    by_text = {claim.text: claim.claim_id for claim in current.claims}

    changed = invalidate_claims(
        prior,
        current,
        changed_paths=["src/auth.py"],
        base_moved=False,
        risk_paths_added=[],
    )
    assert changed.causes == {
        by_text["auth"]: "covered-path-changed",
        by_text["repository assumption"]: "repository-wide-dependency",
    }
    assert set(changed.carried_claims) == {by_text["db"]}

    moved = invalidate_claims(
        prior, current, changed_paths=[], base_moved=True, risk_paths_added=[]
    )
    assert moved.causes == {by_text["repository assumption"]: "base-moved"}

    risk = invalidate_claims(
        prior,
        current,
        changed_paths=[],
        base_moved=False,
        risk_paths_added=["src/new.py"],
    )
    assert risk.causes == {by_text["repository assumption"]: "new-risk-path"}


def test_text_or_coverage_change_and_retirement_are_both_reported():
    old = manifest({"text": "claim", "paths": ["src/auth.py"]}, "retire me")
    prior = legacy(old)
    current = normalize_manifest(
        manifest({"text": "claim changed", "paths": ["src/auth.py"]})
    )
    result = invalidate_claims(
        prior, current, changed_paths=[], base_moved=False, risk_paths_added=[]
    )
    assert result.causes[current.claims[0].claim_id] == "text-or-coverage-changed"
    assert set(result.retired_claim_ids) == set(prior.claims) - set(current.by_id)
    assert all(result.causes[identity] == "retired" for identity in result.retired)


def test_combined_repository_wide_causes_have_deterministic_precedence():
    raw = manifest("wide")
    prior = legacy(raw)
    current = normalize_manifest(raw)
    identity = current.claims[0].claim_id
    result = invalidate_claims(
        prior,
        current,
        changed_paths=["src/other.py"],
        base_moved=True,
        risk_paths_added=["src/new.py"],
    )
    assert result.causes == {identity: "base-moved"}


def test_zero_invalidation_returns_no_task():
    raw = manifest({"text": "scoped", "paths": ["src/auth.py"]})
    current = normalize_manifest(raw)
    assert task_for(legacy(raw), current) is None


def test_task_is_frozen_and_hash_is_deterministic_across_input_order():
    raw = manifest(
        {"text": "a", "paths": ["src/a.py"]},
        {"text": "b", "paths": ["src/b.py"]},
    )
    prior = legacy(raw)
    current = normalize_manifest(raw)
    first = task_for(prior, current, changed=["src/b.py", "src/a.py"])
    second = task_for(prior, current, changed=["src/a.py", "src/b.py"])
    assert first is not None and second is not None
    assert first.task_hash == second.task_hash
    assert first.to_dict()["task_hash"] == first.task_hash
    restored = type(first).from_mapping(
        first.to_dict(), claim_set=current, previous=prior
    )
    assert restored.to_dict() == first.to_dict()
    with pytest.raises(FrozenInstanceError):
        first.tree_sha = "different"  # type: ignore[misc]


@pytest.mark.parametrize("mutation", ["extra", "missing", "tree", "task"])
def test_composition_rejects_extra_missing_and_forged_fresh_results(mutation):
    raw = manifest({"text": "claim", "paths": ["src/auth.py"]})
    current = normalize_manifest(raw)
    task = task_for(legacy(raw), current, changed=["src/auth.py"])
    assert task is not None
    results = fresh(task)
    identity = task.invalidated_claims[0].claim_id
    if mutation == "extra":
        results["tc_" + "f" * 64] = dict(results[identity])
    elif mutation == "missing":
        results.pop(identity)
    elif mutation == "tree":
        results[identity]["verified_tree_sha"] = TREE
    else:
        results[identity]["task_hash"] = "0" * 64
    with pytest.raises(TrustClaimError):
        compose_claim_verdict(legacy(raw), task, results)


def test_aggregate_pass_fail_inconclusive_and_uncovered_risk_path():
    raw = manifest({"text": "claim", "paths": ["src/auth.py"]})
    prior = legacy(raw)
    current = normalize_manifest(raw)
    task = task_for(prior, current, changed=["src/auth.py"])
    assert task is not None
    identity = task.invalidated_claims[0].claim_id

    passed, aggregate = compose_claim_verdict(prior, task, fresh(task))
    assert aggregate == "pass"
    assert passed.claims[identity].carried_from is None

    _, aggregate = compose_claim_verdict(
        prior, task, fresh(task, {identity: "fail"})
    )
    assert aggregate == "fail"
    _, aggregate = compose_claim_verdict(
        prior, task, fresh(task, {identity: "inconclusive"})
    )
    assert aggregate == "inconclusive"

    uncovered_raw = manifest(
        {"text": "scoped elsewhere", "paths": ["src/other.py"]}
    )
    uncovered = normalize_manifest(uncovered_raw)
    uncovered_task = task_for(None, uncovered)
    assert uncovered_task is not None
    _, aggregate = compose_claim_verdict(None, uncovered_task, fresh(uncovered_task))
    assert aggregate == "inconclusive"
    envelope = {
        "claim_verdicts": [
            {
                "claim_id": uncovered_task.invalidated_claims[0].claim_id,
                "verdict": "pass",
                "rationale": "claim passes but coverage is incomplete",
            }
        ],
        "verification_verdict": "pass",
        "verifier_run_id": "run",
        "verifier_task_id": "task",
        "evidence_digest": "c" * 64,
        "verified_tree_sha": uncovered_task.tree_sha,
        "task_hash": uncovered_task.task_hash,
    }
    with pytest.raises(TrustClaimError, match="aggregate"):
        compose_claim_verdict(None, uncovered_task, envelope)


def test_retirement_requires_a_result_and_can_block_the_aggregate():
    raw = manifest(
        {"text": "keep", "paths": ["src/auth.py"]},
        {"text": "remove", "paths": ["src/old.py"]},
    )
    prior = legacy(raw)
    current = normalize_manifest(
        manifest({"text": "keep", "paths": ["src/auth.py"]})
    )
    task = task_for(prior, current)
    assert task is not None and task.retirements
    results = fresh(task)
    results.pop(task.retirements[0])
    with pytest.raises(TrustClaimError, match="missing"):
        compose_claim_verdict(prior, task, results)
    failed = fresh(task, {task.retirements[0]: "fail"})
    _, aggregate = compose_claim_verdict(prior, task, failed)
    assert aggregate == "fail"


def test_carried_claim_preserves_verifier_binding_and_names_prior_receipt():
    raw = manifest(
        {"text": "changed", "paths": ["src/auth.py"]},
        {"text": "carried", "paths": ["src/db.py"]},
    )
    prior = legacy(raw)
    current = normalize_manifest(raw)
    task = task_for(prior, current, changed=["src/auth.py"])
    assert task is not None
    receipt, aggregate = compose_claim_verdict(prior, task, fresh(task))
    carried_id = next(
        claim.claim_id for claim in current.claims if claim.text == "carried"
    )
    binding = receipt.claims[carried_id]
    assert aggregate == "pass"
    assert binding.verifier_run_id == "legacy-run"
    assert binding.verified_tree_sha == TREE
    assert binding.carried_from == prior.receipt_digest


def test_composition_rejects_a_different_previous_receipt_for_carried_claims():
    raw = manifest(
        {"text": "changed", "paths": ["src/auth.py"]},
        {"text": "carried", "paths": ["src/db.py"]},
    )
    prior = legacy(raw)
    current = normalize_manifest(raw)
    task = task_for(prior, current, changed=["src/auth.py"])
    assert task is not None
    unrelated = legacy(raw, source="unrelated", digest="d" * 64)
    with pytest.raises(TrustClaimError, match="carried binding"):
        compose_claim_verdict(unrelated, task, fresh(task))


@pytest.mark.parametrize(
    "override",
    [
        {"source_identity": "other"},
        {"tree_sha": "3" * 40},
        {"manifest_sha256": "d" * 64},
        {"risk_paths": ["src/other.py"]},
    ],
)
def test_receipt_covers_only_an_exact_complete_match(override):
    raw = manifest({"text": "claim", "paths": ["src/auth.py"]})
    receipt = legacy(raw)
    arguments = {
        "source_identity": "source-1",
        "tree_sha": TREE,
        "manifest_sha256": SHA,
        "risk_paths": ["src/auth.py"],
        **override,
    }
    assert not receipt_covers(receipt, **arguments)


def test_receipt_covers_exact_legacy_pass_and_legacy_seeding_is_conservative():
    raw = manifest(
        "repository wide",
        {"text": "scoped", "paths": ["src/auth.py"]},
    )
    receipt = legacy(raw)
    assert receipt_covers(
        receipt,
        source_identity="source-1",
        tree_sha=TREE,
        manifest_sha256=SHA,
        risk_paths=["src/auth.py"],
    )
    current = normalize_manifest(raw)
    invalidation = invalidate_claims(
        receipt,
        current,
        changed_paths=["src/unrelated.py"],
        base_moved=False,
        risk_paths_added=[],
    )
    wide = next(claim.claim_id for claim in current.claims if not claim.paths)
    scoped = next(claim.claim_id for claim in current.claims if claim.paths)
    assert invalidation.fresh_claim_ids == (wide,)
    assert set(invalidation.carried_claims) == {scoped}
    restored = type(receipt).from_mapping(
        receipt.to_dict(), claim_set=normalize_manifest(raw)
    )
    assert restored.to_dict() == receipt.to_dict()


def test_inconclusive_never_carries():
    raw = manifest({"text": "claim", "paths": ["src/auth.py"]})
    prior = legacy(raw)
    identity = next(iter(prior.claims))
    inconclusive = type(prior).build(
        task_hash=prior.task_hash,
        source_identity=prior.source_identity,
        tree_sha=prior.tree_sha,
        manifest_sha256=prior.manifest_sha256,
        claim_set=normalize_manifest(raw),
        claims={
            identity: ClaimVerdict(
                verdict="inconclusive",
                verifier_run_id="run",
                verifier_task_id="task",
                evidence_digest=SHA,
                verified_tree_sha=TREE,
            )
        },
    )
    invalidation = invalidate_claims(
        inconclusive,
        normalize_manifest(raw),
        changed_paths=[],
        base_moved=False,
        risk_paths_added=[],
    )
    assert invalidation.fresh_claim_ids == (identity,)
