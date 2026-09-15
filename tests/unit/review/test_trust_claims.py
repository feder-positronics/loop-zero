import hashlib
import json
from dataclasses import FrozenInstanceError, replace
from pathlib import Path

import pytest

from loopzero.review.trust_claims import (
    ClaimVerdict,
    TrustClaimError,
    build_trust_claim_task,
    carry_trust_claim_receipt,
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


def legacy(raw, *, source="source-1", tree=TREE, digest=SHA, whole_manifest=False):
    projected = legacy_receipt(
        raw,
        source_identity=source,
        tree_sha=tree,
        manifest_sha256=digest,
        verifier_run_id="legacy-run",
        verifier_task_id="legacy-task",
    )
    if whole_manifest:
        return projected
    return type(projected).build(
        task_hash=projected.task_hash,
        source_identity=projected.source_identity,
        tree_sha=projected.tree_sha,
        manifest_sha256=projected.manifest_sha256,
        claim_set=normalize_manifest(raw),
        claims=projected.claims,
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
            *(claim.claim_id for claim in task.retirements),
        }
    }


def compose(previous, task, results, *, changed=None):
    return compose_claim_verdict(
        previous,
        task,
        results,
        changed_paths=task.changed_paths if changed is None else changed,
    )


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


@pytest.mark.parametrize(
    "changed",
    [
        ["src/auth"],
        ["src/auth/login.py"],
        ["src/auth/old.py", "src/auth/new.py"],
        ["src/auth/old.py", "src/archive/old.py"],
        ["src/archive/new.py", "src/auth/new.py"],
    ],
    ids=[
        "directory-entry",
        "nested-file",
        "both-rename-endpoints",
        "rename-source",
        "rename-destination",
    ],
)
def test_directory_coverage_invalidates_component_descendants(changed):
    raw = manifest({"text": "auth", "paths": ["src/auth"]})
    prior = legacy(raw)
    current = normalize_manifest(raw)
    identity = current.claims[0].claim_id
    invalidation = invalidate_claims(
        prior,
        current,
        changed_paths=changed,
        base_moved=False,
        risk_paths_added=[],
    )
    assert invalidation.fresh_claim_ids == (identity,)
    assert invalidation.causes == {identity: "covered-path-changed"}


def test_directory_coverage_does_not_match_a_sibling_with_the_same_prefix():
    raw = manifest({"text": "auth", "paths": ["src/auth"]})
    prior = legacy(raw)
    current = normalize_manifest(raw)
    invalidation = invalidate_claims(
        prior,
        current,
        changed_paths=["src/auth2/login.py"],
        base_moved=False,
        risk_paths_added=[],
    )
    assert not invalidation.fresh_claim_ids
    assert set(invalidation.carried_claims) == set(current.by_id)


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


def test_task_deserialization_rejects_hash_tampering_and_dropped_retirement():
    old = manifest(
        {"text": "keep", "paths": ["src/auth.py"]},
        {"text": "remove", "paths": ["src/old.py"]},
    )
    prior = legacy(old)
    current = normalize_manifest(
        manifest({"text": "keep", "paths": ["src/auth.py"]})
    )
    task = task_for(prior, current)
    assert task is not None and task.retirements
    assert task.retirements[0].text == "remove"
    assert task.retirements[0].section == "actors_assets"
    assert task.retirements[0].paths == ("src/old.py",)

    tampered_hash = task.to_dict()
    tampered_hash["task_hash"] = "0" * 64
    with pytest.raises(TrustClaimError, match="task hash"):
        type(task).from_mapping(tampered_hash, claim_set=current, previous=prior)

    dropped = task.to_dict()
    dropped["retirements"] = []
    payload = {key: value for key, value in dropped.items() if key != "task_hash"}
    dropped["task_hash"] = hashlib.sha256(
        json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()
    with pytest.raises(TrustClaimError, match="retirements"):
        type(task).from_mapping(dropped, claim_set=current, previous=prior)


@pytest.mark.parametrize("cause", ["path", "pathless", "new", "inconclusive", "base", "risk"])
def test_task_deserialization_rejects_a_rehashed_dropped_invalidation(cause):
    raw = manifest(
        {"text": "changed", "paths": [] if cause in {"pathless", "base", "risk"} else ["src/auth.py"]},
        {"text": "carried", "paths": ["src/db.py"]},
    )
    prior = legacy(raw)
    if cause == "new":
        prior = legacy(manifest(raw["actors_assets"][1]))
    elif cause == "inconclusive":
        identity = next(claim.claim_id for claim in normalize_manifest(raw).claims if claim.text == "changed")
        prior = type(prior).build(
            task_hash=prior.task_hash,
            source_identity=prior.source_identity,
            tree_sha=prior.tree_sha,
            manifest_sha256=prior.manifest_sha256,
            claim_set=normalize_manifest(raw),
            claims={**prior.claims, identity: replace(prior.claims[identity], verdict="inconclusive")},
        )
    if cause == "risk":
        raw["risk_paths"].append("src/new.py")
    current = normalize_manifest(raw)
    task = task_for(
        prior, current,
        changed=["src/auth.py"] if cause in {"path", "pathless"} else [],
        base_moved=cause == "base",
        added=["src/new.py"] if cause == "risk" else [],
    )
    assert task is not None and len(task.invalidated_claims) == 1
    dropped = task.to_dict()
    dropped["invalidated_claims"] = []
    payload = {key: value for key, value in dropped.items() if key != "task_hash"}
    dropped["task_hash"] = hashlib.sha256(
        json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()
    with pytest.raises(TrustClaimError, match="invalidated set"):
        type(task).from_mapping(dropped, claim_set=current, previous=prior)


def test_task_deserialization_rejects_rehashed_dropped_carried_digest():
    raw = manifest(
        {"text": "changed", "paths": ["src/auth.py"]},
        {"text": "carried", "paths": ["src/db.py"]},
    )
    prior = legacy(raw)
    current = normalize_manifest(raw)
    task = task_for(prior, current, changed=["src/auth.py"])
    assert task is not None and task.carried_receipt_digests
    dropped = task.to_dict()
    dropped["carried_receipt_digests"] = []
    payload = {key: value for key, value in dropped.items() if key != "task_hash"}
    dropped["task_hash"] = hashlib.sha256(
        json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()
    with pytest.raises(TrustClaimError, match="carried receipt binding"):
        type(task).from_mapping(dropped, claim_set=current, previous=prior)


def test_task_restoration_requires_the_bound_previous_receipt():
    raw = manifest({"text": "claim", "paths": ["src/auth.py"]})
    current = normalize_manifest(raw)
    task = task_for(None, current)
    assert task is not None
    with pytest.raises(TrustClaimError, match="required for restoration"):
        type(task).from_mapping(task.to_dict(), claim_set=current, previous=None)


def test_composition_rechecks_the_caller_supplied_changed_path_receipt():
    raw = manifest({"text": "claim", "paths": ["src/auth.py"]})
    prior = legacy(raw)
    current = normalize_manifest(raw)
    task = task_for(prior, current, changed=["src/auth.py"])
    assert task is not None
    assert task.changed_paths_digest != "0" * 64
    with pytest.raises(TrustClaimError, match="task diff"):
        compose(prior, task, fresh(task), changed=["src/other.py"])


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
        compose(legacy(raw), task, results)


def test_composition_requires_every_carried_forward_unfinished_claim():
    prior_raw = manifest(
        {"text": "claim-a", "paths": ["src/a.py"]},
        {"text": "claim-b", "paths": ["src/b.py"]},
        risk_paths=(),
    )
    prior = legacy(prior_raw)
    current = normalize_manifest(
        manifest(
            *prior_raw["actors_assets"],
            {"text": "claim-c", "paths": ["src/c.py"]},
            risk_paths=(),
        )
    )
    ordinary = invalidate_claims(
        prior,
        current,
        changed_paths=(),
        base_moved=False,
        risk_paths_added=(),
    )
    by_text = {claim.text: claim.claim_id for claim in current.claims}
    unfinished = by_text["claim-a"]
    forced = replace(
        ordinary,
        fresh_claim_ids=(*ordinary.fresh_claim_ids, unfinished),
        carried_claims={
            identity: binding
            for identity, binding in ordinary.carried_claims.items()
            if identity != unfinished
        },
        causes={**ordinary.causes, unfinished: "prior-inconclusive"},
    )
    task = build_trust_claim_task(
        current,
        forced,
        generation_ref="generation",
        source_identity="source-2",
        tree_sha="2" * 40,
        manifest_sha256="b" * 64,
        delta_from_tree_sha=prior.tree_sha,
        changed_paths=(),
    )
    assert task is not None
    restored = type(task).from_mapping(
        task.to_dict(), claim_set=current, previous=prior
    )
    only_new = fresh(restored)
    only_new.pop(unfinished)
    with pytest.raises(TrustClaimError, match="missing claims"):
        compose(prior, restored, only_new)

    receipt, aggregate = compose(prior, restored, fresh(restored))
    assert aggregate == "pass"
    assert receipt.claims[unfinished].carried_from is None


def test_aggregate_pass_fail_inconclusive_and_uncovered_risk_path():
    raw = manifest({"text": "claim", "paths": ["src/auth.py"]})
    prior = legacy(raw)
    current = normalize_manifest(raw)
    task = task_for(prior, current, changed=["src/auth.py"])
    assert task is not None
    identity = task.invalidated_claims[0].claim_id

    passed, aggregate = compose(prior, task, fresh(task))
    assert aggregate == "pass"
    assert passed.claims[identity].carried_from is None

    _, aggregate = compose(prior, task, fresh(task, {identity: "fail"}))
    assert aggregate == "fail"
    _, aggregate = compose(prior, task, fresh(task, {identity: "inconclusive"}))
    assert aggregate == "inconclusive"

    uncovered_raw = manifest(
        {"text": "scoped elsewhere", "paths": ["src/other.py"]}
    )
    uncovered = normalize_manifest(uncovered_raw)
    uncovered_task = task_for(None, uncovered)
    assert uncovered_task is not None
    receipt, aggregate = compose(None, uncovered_task, fresh(uncovered_task))
    assert aggregate == "inconclusive"
    assert receipt.uncovered_risk_paths == ("src/auth.py",)
    envelope = {
        "claim_verdicts": [
            {
                "claim_id": uncovered_task.invalidated_claims[0].claim_id,
                "verdict": "pass",
                "rationale": "claim passes but coverage is incomplete",
            }
        ],
        "verification_verdict": "inconclusive",
        "verifier_run_id": "run",
        "verifier_task_id": "task",
        "evidence_digest": "c" * 64,
        "verified_tree_sha": uncovered_task.tree_sha,
        "task_hash": uncovered_task.task_hash,
    }
    composed, aggregate = compose(None, uncovered_task, envelope)
    assert aggregate == "inconclusive"
    assert composed.uncovered_risk_paths == ("src/auth.py",)

    envelope["verification_verdict"] = "pass"
    with pytest.raises(TrustClaimError, match="aggregate"):
        compose(None, uncovered_task, envelope)


def test_risk_coverage_requires_declared_exact_or_component_prefix_paths():
    for declared in ("src/auth.py", "src"):
        raw = manifest(
            {"text": "covered", "paths": [declared]},
            "repository-wide dependency",
        )
        current = normalize_manifest(raw)
        task = task_for(None, current)
        assert task is not None
        receipt, aggregate = compose(None, task, fresh(task))
        assert aggregate == "pass"
        assert receipt.uncovered_risk_paths == ()

    pathless = normalize_manifest(manifest("repository-wide dependency"))
    task = task_for(None, pathless)
    assert task is not None
    receipt, aggregate = compose(None, task, fresh(task))
    assert aggregate == "inconclusive"
    assert receipt.uncovered_risk_paths == ("src/auth.py",)


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
    retirement_id = task.retirements[0].claim_id
    results.pop(retirement_id)
    with pytest.raises(TrustClaimError, match="missing"):
        compose(prior, task, results)
    failed = fresh(task, {retirement_id: "fail"})
    failed_receipt, aggregate = compose(prior, task, failed)
    assert aggregate == "fail"
    assert failed_receipt.retirements[retirement_id].verdict == "fail"
    assert not receipt_covers(
        failed_receipt,
        source_identity=failed_receipt.source_identity,
        tree_sha=failed_receipt.tree_sha,
        manifest_sha256=failed_receipt.manifest_sha256,
        risk_paths=current.risk_paths,
    )
    inconclusive = fresh(task, {retirement_id: "inconclusive"})
    inconclusive_receipt, aggregate = compose(prior, task, inconclusive)
    assert aggregate == "inconclusive"
    assert inconclusive_receipt.retirements[retirement_id].verdict == "inconclusive"
    assert not receipt_covers(
        inconclusive_receipt,
        source_identity=inconclusive_receipt.source_identity,
        tree_sha=inconclusive_receipt.tree_sha,
        manifest_sha256=inconclusive_receipt.manifest_sha256,
        risk_paths=current.risk_paths,
    )
    restored = type(failed_receipt).from_mapping(
        failed_receipt.to_dict(), claim_set=current
    )
    assert restored.retirements == failed_receipt.retirements
    tampered = failed_receipt.to_dict()
    tampered["retirements"][retirement_id]["verdict"] = "pass"
    with pytest.raises(TrustClaimError, match="receipt digest"):
        type(failed_receipt).from_mapping(tampered, claim_set=current)
    with pytest.raises(TrustClaimError, match="unresolved retirements"):
        invalidate_claims(
            failed_receipt,
            current,
            changed_paths=[],
            base_moved=False,
            risk_paths_added=[],
        )


def test_carried_claim_preserves_verifier_binding_and_names_prior_receipt():
    raw = manifest(
        {"text": "changed", "paths": ["src/auth.py"]},
        {"text": "carried", "paths": ["src/db.py"]},
    )
    prior = legacy(raw)
    current = normalize_manifest(raw)
    task = task_for(prior, current, changed=["src/auth.py"])
    assert task is not None
    receipt, aggregate = compose(prior, task, fresh(task))
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
    with pytest.raises(TrustClaimError, match="previous receipt"):
        compose(unrelated, task, fresh(task))


def test_task_binds_previous_receipt_even_without_carried_claims():
    raw = manifest({"text": "claim", "paths": ["src/auth.py"]})
    prior = legacy(raw)
    current = normalize_manifest(raw)
    task = task_for(prior, current, changed=["src/auth.py"])
    assert task is not None and not task.carried_receipt_digests
    unrelated = legacy(raw, source="unrelated", digest="d" * 64)
    with pytest.raises(TrustClaimError, match="previous receipt"):
        compose(unrelated, task, fresh(task))
    with pytest.raises(TrustClaimError, match="previous receipt"):
        type(task).from_mapping(task.to_dict(), claim_set=current, previous=unrelated)


def test_carry_only_composition_rederives_invalidation():
    raw = manifest({"text": "claim", "paths": ["src/auth.py"]})
    prior = legacy(raw)
    current = normalize_manifest(raw)
    invalidation = invalidate_claims(
        prior, current, changed_paths=[], base_moved=False, risk_paths_added=[]
    )
    forged = replace(invalidation, changed_paths=("src/auth.py",))
    with pytest.raises(TrustClaimError, match="carry invalidation"):
        carry_trust_claim_receipt(
            prior, current, forged, source_identity="source-2", tree_sha="2" * 40,
            manifest_sha256=SHA, delta_from_tree_sha=TREE, changed_paths=["src/auth.py"],
        )


def test_composition_and_deserialization_reject_a_wrong_delta_base_tree():
    raw = manifest(
        {"text": "changed", "paths": ["src/auth.py"]},
        {"text": "carried", "paths": ["src/db.py"]},
    )
    prior = legacy(raw)
    current = normalize_manifest(raw)
    task = task_for(prior, current, changed=["src/auth.py"])
    assert task is not None
    forged = replace(task, delta_from_tree_sha="9" * 40)
    with pytest.raises(TrustClaimError, match="delta tree"):
        compose(prior, forged, fresh(forged))
    with pytest.raises(TrustClaimError, match="delta tree"):
        type(task).from_mapping(
            forged.to_dict(), claim_set=current, previous=prior
        )


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
    receipt = legacy(raw, whole_manifest=True)
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
    assert set(invalidation.fresh_claim_ids) == set(current.by_id)
    assert not invalidation.carried_claims
    assert set(invalidation.causes.values()) == {"repository-wide-dependency"}
    restored = type(receipt).from_mapping(
        receipt.to_dict(), claim_set=normalize_manifest(raw)
    )
    assert restored.to_dict() == receipt.to_dict()


def test_pathless_legacy_receipt_covers_only_its_exact_whole_manifest_source():
    raw = manifest("repository wide")
    receipt = legacy(raw, whole_manifest=True)
    assert receipt.uncovered_risk_paths == ("src/auth.py",)
    assert receipt_covers(
        receipt,
        source_identity="source-1",
        tree_sha=TREE,
        manifest_sha256=SHA,
        risk_paths=["src/auth.py"],
    )
    assert not receipt_covers(
        receipt,
        source_identity="source-2",
        tree_sha=TREE,
        manifest_sha256=SHA,
        risk_paths=["src/auth.py"],
    )


def test_receipt_digest_tampering_is_detected():
    raw = manifest({"text": "claim", "paths": ["src/auth.py"]})
    receipt = legacy(raw)
    tampered = receipt.to_dict()
    tampered["tree_sha"] = "9" * 40
    with pytest.raises(TrustClaimError, match="receipt digest"):
        type(receipt).from_mapping(
            tampered, claim_set=normalize_manifest(raw)
        )


def test_fresh_verdict_outside_the_closed_set_is_rejected():
    raw = manifest({"text": "claim", "paths": ["src/auth.py"]})
    prior = legacy(raw)
    current = normalize_manifest(raw)
    task = task_for(prior, current, changed=["src/auth.py"])
    assert task is not None
    result = fresh(task)
    result[task.invalidated_claims[0].claim_id]["verdict"] = "clean"
    with pytest.raises(TrustClaimError, match="verdict is invalid"):
        compose(prior, task, result)


@pytest.mark.parametrize("change_kind", ["tree", "manifest-whitespace"])
def test_zero_invalidation_yields_a_carry_receipt_for_the_new_head(change_kind):
    old = manifest({"text": " scoped ", "paths": ["src/auth.py"]})
    prior = legacy(old)
    current_raw = (
        manifest({"text": "scoped", "paths": ["src/auth.py"]})
        if change_kind == "manifest-whitespace"
        else old
    )
    current = normalize_manifest(current_raw)
    changed = ["README.md"] if change_kind == "tree" else []
    invalidation = invalidate_claims(
        prior,
        current,
        changed_paths=changed,
        base_moved=False,
        risk_paths_added=[],
    )
    assert (
        build_trust_claim_task(
            current,
            invalidation,
            generation_ref="generation",
            source_identity="source-2",
            tree_sha="2" * 40,
            manifest_sha256="b" * 64,
            delta_from_tree_sha=prior.tree_sha,
            changed_paths=changed,
        )
        is None
    )
    carried, aggregate = carry_trust_claim_receipt(
        prior,
        current,
        invalidation,
        source_identity="source-2",
        tree_sha="2" * 40,
        manifest_sha256="b" * 64,
        delta_from_tree_sha=prior.tree_sha,
        changed_paths=changed,
    )
    assert aggregate == "pass"
    assert carried.tree_sha == "2" * 40
    assert carried.manifest_sha256 == "b" * 64
    assert all(
        binding.carried_from == prior.receipt_digest
        for binding in carried.claims.values()
    )
    assert receipt_covers(
        carried,
        source_identity="source-2",
        tree_sha="2" * 40,
        manifest_sha256="b" * 64,
        risk_paths=["src/auth.py"],
    )


def test_carry_only_preserves_a_failed_claim_as_fail():
    raw = manifest({"text": "claim", "paths": ["src/auth.py"]})
    current = normalize_manifest(raw)
    task = task_for(None, current)
    assert task is not None
    identity = current.claims[0].claim_id
    prior, aggregate = compose(None, task, fresh(task, {identity: "fail"}))
    assert aggregate == "fail"
    invalidation = invalidate_claims(
        prior, current, changed_paths=["README.md"], base_moved=False, risk_paths_added=[]
    )
    carried, aggregate = carry_trust_claim_receipt(
        prior, current, invalidation, source_identity="source-3", tree_sha="3" * 40,
        manifest_sha256=SHA, delta_from_tree_sha=prior.tree_sha, changed_paths=["README.md"],
    )
    assert aggregate == "fail"
    assert carried.claims[identity].verdict == "fail"
    assert carried.claims[identity].carried_from == prior.receipt_digest
    assert not receipt_covers(
        carried, source_identity="source-3", tree_sha="3" * 40,
        manifest_sha256=SHA, risk_paths=current.risk_paths,
    )


def test_empty_claim_set_can_carry_to_a_new_head_without_a_task():
    raw = manifest(risk_paths=[])
    prior = legacy(raw)
    current = normalize_manifest(raw)
    invalidation = invalidate_claims(
        prior, current, changed_paths=["README.md"], base_moved=False, risk_paths_added=[]
    )
    receipt, aggregate = carry_trust_claim_receipt(
        prior, current, invalidation, source_identity="source-2", tree_sha="2" * 40,
        manifest_sha256=SHA, delta_from_tree_sha=TREE, changed_paths=["README.md"],
    )
    assert aggregate == "pass"
    assert receipt_covers(
        receipt, source_identity="source-2", tree_sha="2" * 40,
        manifest_sha256=SHA, risk_paths=[],
    )


def test_delivery_contract_states_exact_claim_coverage_rule():
    contract = (
        Path(__file__).parents[3] / "core" / "CONTRACT.md"
    ).read_text(encoding="utf-8")
    assert (
        "The aggregate\n"
        "passes only when every claim passes and every required risk path is covered,\n"
        "exactly or by path-component prefix, by a passing claim that declares that\n"
        "coverage. Claims without declared paths provide no risk-path coverage."
    ) in contract
    assert (
        "A legacy whole-manifest pass remains reusable at its exact source identity,\n"
        "tree, and manifest digest because that pass judged the inventory as a whole,\n"
        "even when its projected claims do not declare enough paths to cover the risk\n"
        "inventory mechanically. At a changed source, the first claim-level run after\n"
        "adoption is a full run; selective coverage reuse begins only with that first\n"
        "claim-level receipt."
    ) in contract


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
    assert invalidation.causes == {identity: "prior-inconclusive"}
