from pathlib import Path
from types import SimpleNamespace

import pytest


def load_module():
    from loopzero.review import chain

    chain.configure(
        SimpleNamespace(
            required_sections=("code", "security"),
            max_reviews_per_pr=1,
            max_delta_reviews=1,
        )
    )
    return chain


module = load_module()


def _finding(claim: str = "unsafe boundary") -> dict[str, object]:
    return {
        "severity": "important",
        "claim": claim,
        "path": "app/config.py",
        "line_start": 3,
        "line_end": 3,
    }


def _task(*, security: bool) -> dict[str, object]:
    paths = ["app/config.py"] if security else []
    return {
        "task_id": "review-one",
        "work_kind": "review",
        "review_intent": "delivery-code-review",
        "required_sections": ["code", *(("security",) if security else ())],
        "security_trigger_paths": paths,
    }


def _result(*, security: bool) -> dict[str, object]:
    code_findings: list[dict[str, object]] = []
    sections: dict[str, object] = {
        "code": {
            "completion": "completed",
            "verdict": "clean",
            "findings": code_findings,
        }
    }
    findings: list[dict[str, object]] = []
    if security:
        security_findings = [_finding()]
        sections["security"] = {
            "completion": "completed",
            "verdict": "findings",
            "threat_model_summary": "Untrusted config crosses the process boundary.",
            "findings": security_findings,
        }
        findings.extend(security_findings)
    return {"review_sections": sections, "findings": findings}


def test_required_sections_are_closed_over_trigger_classifier() -> None:
    assert module.validate_review_chain_task(_task(security=False)) == ("code",)
    assert module.validate_review_chain_task(_task(security=True)) == (
        "code",
        "security",
    )

    with pytest.raises(module.ReviewChainError, match="required_sections"):
        module.validate_review_chain_task(
            {**_task(security=True), "required_sections": ["code"]}
        )


def test_review_budget_cannot_be_widened_and_can_be_narrowed() -> None:
    module.configure(
        SimpleNamespace(
            required_sections=("code",), max_reviews_per_pr=9, max_delta_reviews=9
        )
    )
    with pytest.raises(module.ReviewChainError, match="primary"):
        module.enforce_review_budget(
            completed_reviews=1, completed_delta_reviews=0, requested="review",
            generation="cg_" + "1" * 32,
        )
    with pytest.raises(module.ReviewChainError, match="delta"):
        module.enforce_review_budget(
            completed_reviews=0, completed_delta_reviews=1, requested="delta",
            generation="cg_" + "1" * 32,
        )
    with pytest.raises(module.ReviewChainError, match="exactly one primary"):
        module.enforce_review_budget(
            completed_reviews=0, completed_delta_reviews=0, requested="delta",
            generation="cg_" + "1" * 32,
        )

    module.configure(
        SimpleNamespace(
            required_sections=("code",), max_reviews_per_pr=0, max_delta_reviews=0
        )
    )
    with pytest.raises(module.ReviewChainError, match="primary"):
        module.enforce_review_budget(
            completed_reviews=0, completed_delta_reviews=0, requested="review",
            generation="cg_" + "1" * 32,
        )
    module.configure(
        SimpleNamespace(
            required_sections=("code", "security"),
            max_reviews_per_pr=1,
            max_delta_reviews=1,
        )
    )


def test_required_sections_reuse_the_shared_scope_policy(monkeypatch) -> None:
    module.configure(
        SimpleNamespace(
            required_sections=("code", "shared-policy"),
            max_reviews_per_pr=1,
            max_delta_reviews=1,
        )
    )

    task = {
        **_task(security=True),
        "required_sections": ["code", "shared-policy", "security"],
    }

    assert module.validate_review_chain_task(task) == (
        "code",
        "shared-policy",
        "security",
    )
    module.configure(
        SimpleNamespace(
            required_sections=("code", "security"),
            max_reviews_per_pr=1,
            max_delta_reviews=1,
        )
    )


def test_section_result_requires_security_completion_and_threat_model() -> None:
    task = _task(security=True)
    result = _result(security=True)
    assert module.validate_review_chain_result(result, task=task) == result["findings"]

    missing = _result(security=True)
    del missing["review_sections"]["security"]
    with pytest.raises(module.ReviewChainError, match="exactly match"):
        module.validate_review_chain_result(missing, task=task)

    no_threat_model = _result(security=True)
    del no_threat_model["review_sections"]["security"]["threat_model_summary"]
    with pytest.raises(module.ReviewChainError, match="threat-model"):
        module.validate_review_chain_result(no_threat_model, task=task)


def test_unclassified_or_inconsistent_findings_are_rejected() -> None:
    task = _task(security=False)
    result = _result(security=False)
    result["findings"] = [_finding("not owned by a section")]
    with pytest.raises(module.ReviewChainError, match="unclassified"):
        module.validate_review_chain_result(result, task=task)

    inconsistent = _result(security=True)
    inconsistent["review_sections"]["security"]["verdict"] = "clean"
    with pytest.raises(module.ReviewChainError, match="verdict"):
        module.validate_review_chain_result(inconsistent, task=_task(security=True))


def test_primary_receipt_binds_one_snapshot_and_all_sections() -> None:
    task = _task(security=True)
    result = _result(security=True)
    receipt = module.build_review_chain_receipt(
        task=task,
        snapshot_sha="a" * 40,
        snapshot_tree_sha="b" * 40,
        patch_identity={"schema_version": "PatchIdentityV1"},
        result=result,
        finding_ids=["f_security"],
    )

    assert receipt["schema_version"] == "ReviewChainReceiptV1"
    assert receipt["required_sections"] == ["code", "security"]
    assert receipt["sections"]["code"]["finding_ids"] == []
    assert receipt["sections"]["security"]["finding_ids"] == ["f_security"]
    assert receipt["trigger_classifier"]["security_trigger_paths"] == ["app/config.py"]
    assert (
        module.review_chain_receipt_reasons(
            receipt,
            task=task,
            snapshot_sha="a" * 40,
            snapshot_tree_sha="b" * 40,
            patch_identity={"schema_version": "PatchIdentityV1"},
        )
        == ()
    )

    assert "snapshot-tree-mismatch" in module.review_chain_receipt_reasons(
        receipt,
        task=task,
        snapshot_sha="a" * 40,
        snapshot_tree_sha="c" * 40,
        patch_identity={"schema_version": "PatchIdentityV1"},
    )


def test_primary_receipt_rejects_duplicate_finding_ids() -> None:
    task = _task(security=True)
    result = _result(security=True)
    result["findings"] = [result["findings"][0], dict(result["findings"][0])]
    result["review_sections"]["code"]["findings"] = [result["findings"][0]]
    result["review_sections"]["code"]["verdict"] = "findings"
    result["review_sections"]["security"]["findings"] = [result["findings"][1]]

    with pytest.raises(module.ReviewChainError, match="finding IDs"):
        module.build_review_chain_receipt(
            task=task,
            snapshot_sha="a" * 40,
            snapshot_tree_sha="b" * 40,
            patch_identity={"schema_version": "PatchIdentityV1"},
            result=result,
            finding_ids=["f_duplicate", "f_duplicate"],
        )

    receipt = module.build_review_chain_receipt(
        task=task,
        snapshot_sha="a" * 40,
        snapshot_tree_sha="b" * 40,
        patch_identity={"schema_version": "PatchIdentityV1"},
        result=result,
        finding_ids=["f_code", "f_security"],
    )
    receipt["sections"]["security"]["finding_ids"] = ["f_code"]

    assert "duplicate-or-invalid-finding-id" in module.review_chain_receipt_reasons(
        receipt,
        task=task,
        snapshot_sha="a" * 40,
        snapshot_tree_sha="b" * 40,
        patch_identity={"schema_version": "PatchIdentityV1"},
    )


def test_advisory_child_cannot_satisfy_or_mutate_primary_sections() -> None:
    primary = module.build_review_chain_receipt(
        task=_task(security=True),
        snapshot_sha="a" * 40,
        snapshot_tree_sha="b" * 40,
        patch_identity={"schema_version": "PatchIdentityV1"},
        result=_result(security=True),
        finding_ids=["f_security"],
    )
    child = module.build_review_chain_advisory_receipt(
        primary_receipt=primary,
        advisory_task_id="cross-harness-one",
        request_sha256="c" * 64,
        result_sha256="d" * 64,
        finding_ids=["f_advisory"],
    )

    assert child["schema_version"] == "ReviewChainAdvisoryReceiptV1"
    assert child["chain_id"] == primary["chain_id"]
    assert child["snapshot_tree_sha"] == primary["snapshot_tree_sha"]
    assert child["finding_ids"] == ["f_advisory"]
    assert "required_sections" not in child
    assert "sections" not in child


def test_gate_key_ignores_legacy_lens_relabeling() -> None:
    code = {
        "review_intent": "delivery-code-review",
        "review_lens": "code",
        "category": "final-code",
    }
    security = {
        "review_intent": "delivery-code-review",
        "review_lens": "security",
        "category": "relabeled-security",
    }
    assert module.review_gate_key(code) == "delivery-chain"
    assert module.review_gate_key(security) == "delivery-chain"
    assert module.review_record_lens(code) == "code"
    assert module.review_record_lens(security) == "security"


def test_review_finding_lens_uses_chain_section_ids_not_category() -> None:
    terminal = {
        "review_intent": "delivery-code-review",
        "category": "security-review",
        "review_chain_receipt": {
            "schema_version": "ReviewChainReceiptV1",
            "required_sections": ["code", "security"],
            "sections": {
                "code": {"finding_ids": ["f_code"]},
                "security": {"finding_ids": ["f_security"]},
            },
        },
    }

    assert module.review_finding_lens(terminal, "f_code") == "code"
    assert module.review_finding_lens(terminal, "f_security") == "security"


@pytest.mark.parametrize("finding_id", ["f_missing", "f_duplicate"])
def test_review_finding_lens_fails_closed_on_invalid_chain_membership(
    finding_id: str,
) -> None:
    terminal = {
        "review_intent": "delivery-code-review",
        "review_lens": "security",
        "category": "security-review",
        "review_chain_receipt": {
            "schema_version": "ReviewChainReceiptV1",
            "required_sections": ["code", "security"],
            "sections": {
                "code": {"finding_ids": ["f_duplicate"]},
                "security": {"finding_ids": ["f_duplicate"]},
            },
        },
    }

    assert module.review_finding_lens(terminal, finding_id) == ""


def test_review_finding_lens_fails_closed_on_same_section_duplicate() -> None:
    terminal = {
        "review_intent": "delivery-code-review",
        "review_chain_receipt": {
            "schema_version": "ReviewChainReceiptV1",
            "required_sections": ["code", "security"],
            "sections": {
                "code": {"finding_ids": ["f_duplicate", "f_duplicate"]},
                "security": {"finding_ids": []},
            },
        },
    }

    assert module.review_finding_lens(terminal, "f_duplicate") == ""


@pytest.mark.parametrize("security", [False, True])
def test_receipt_keeps_suggestions_in_results_without_ledger_ids(
    security: bool,
) -> None:
    task = _task(security=security)
    result = _result(security=security)
    suggestion = {**_finding("nit"), "severity": "suggestion"}
    result["review_sections"]["code"]["findings"] = [suggestion]
    result["review_sections"]["code"]["verdict"] = "findings"
    result["findings"].insert(0, suggestion)
    ids = ["f_security"] if security else []
    receipt = module.build_review_chain_receipt(
        task=task,
        snapshot_sha="a" * 40,
        snapshot_tree_sha="b" * 40,
        patch_identity=None,
        result=result,
        finding_ids=ids,
    )
    assert receipt["sections"]["code"]["finding_ids"] == []
    assert result["findings"][0] == suggestion
    if security:
        assert receipt["sections"]["security"]["finding_ids"] == ids


def test_receipt_defaults_missing_review_intent_to_the_deposit_policy() -> None:
    task = _task(security=False)
    del task["review_intent"]
    result = _result(security=False)
    defect = _finding("defect")
    result["review_sections"]["code"]["findings"] = [defect]
    result["review_sections"]["code"]["verdict"] = "findings"
    result["findings"] = [defect]
    receipt = module.build_review_chain_receipt(
        task=task,
        snapshot_sha="a" * 40,
        snapshot_tree_sha="b" * 40,
        patch_identity=None,
        result=result,
        finding_ids=["f_defect"],
    )
    assert receipt["sections"]["code"]["finding_ids"] == ["f_defect"]
    with pytest.raises(module.ReviewChainError, match="finding IDs"):
        module.build_review_chain_receipt(
            task=task,
            snapshot_sha="a" * 40,
            snapshot_tree_sha="b" * 40,
            patch_identity=None,
            result=result,
            finding_ids=[],
        )


def test_legacy_primary_projects_a_consumed_content_generation(monkeypatch) -> None:
    from loopzero.kernel import authority_projection

    terminal = {
        "type": "attempt-terminal",
        "task_id": "legacy-review",
        "snapshot_tree_sha": "b" * 40,
        "patch_identity": {
            "candidate_sha": "a" * 40,
            "candidate_tree_sha": "b" * 40,
        },
        "review_lens": "code",
    }
    monkeypatch.setattr(
        authority_projection,
        "seam_accepted_review_terminals",
        lambda rows: {"legacy-review": terminal},
    )
    monkeypatch.setattr(
        authority_projection,
        "seam_authenticated_verdicts",
        lambda rows, **kwargs: {"legacy-review": {"verdict": "pass"}},
    )
    projected = authority_projection.generations([terminal])
    assert len(projected) == 1
    generation = next(iter(projected.values()))
    assert authority_projection.slot_state(
        [terminal], generation.generation_id, "delivery"
    ).primary_consumed


def test_legacy_delta_terminal_is_not_projected_as_a_primary(monkeypatch) -> None:
    from loopzero.kernel import authority_projection

    terminal = {
        "type": "attempt-terminal",
        "task_id": "legacy-delta",
        "snapshot_tree_sha": "b" * 40,
        "delta_from_snapshot_sha": "a" * 40,
        "patch_identity": {
            "candidate_sha": "c" * 40,
            "candidate_tree_sha": "b" * 40,
        },
        "review_lens": "code",
    }
    monkeypatch.setattr(
        authority_projection,
        "seam_accepted_review_terminals",
        lambda rows: {"legacy-delta": terminal},
    )
    monkeypatch.setattr(
        authority_projection,
        "seam_authenticated_verdicts",
        lambda rows, **kwargs: {"legacy-delta": {"verdict": "pass"}},
    )
    assert authority_projection.generations([terminal]) == {}


def test_generation_projection_is_cached_for_an_unchanged_ledger(monkeypatch) -> None:
    from loopzero.kernel import authority_projection

    terminal = {
        "type": "attempt-terminal",
        "task_id": "legacy-cache-probe",
        "snapshot_tree_sha": "d" * 40,
        "patch_identity": {
            "candidate_sha": "c" * 40,
            "candidate_tree_sha": "d" * 40,
        },
        "review_lens": "code",
    }
    calls = 0

    def accepted(rows):
        nonlocal calls
        calls += 1
        return {"legacy-cache-probe": terminal}

    monkeypatch.setattr(authority_projection, "seam_accepted_review_terminals", accepted)
    monkeypatch.setattr(
        authority_projection,
        "seam_authenticated_verdicts",
        lambda rows, **kwargs: {"legacy-cache-probe": {"verdict": "pass"}},
    )
    records = [terminal]
    first = authority_projection.generations(records)
    assert authority_projection.generations(records) == first
    generation = next(iter(first.values()))
    authority_projection.slot_state(records, generation.generation_id, "delivery")
    authority_projection.slot_state(records, generation.generation_id, "delivery")
    assert calls == 1


def test_legacy_projection_refuses_when_adapter_is_missing(monkeypatch) -> None:
    from loopzero.kernel import authority_projection, seams

    terminal = {
        "type": "attempt-terminal",
        "task_id": "legacy-review",
        "snapshot_tree_sha": "b" * 40,
        "patch_identity": {
            "candidate_sha": "a" * 40,
            "candidate_tree_sha": "b" * 40,
        },
    }
    monkeypatch.setattr(
        authority_projection,
        "seam_accepted_review_terminals",
        lambda rows: (_ for _ in ()).throw(seams.MissingAdapter("missing")),
    )
    with pytest.raises(seams.MissingAdapter, match="legacy review projection"):
        authority_projection.generations([terminal])


def test_generation_carry_is_a_tree_coverage_disjunct(monkeypatch, tmp_path) -> None:
    from loopzero.kernel import authority_projection
    from loopzero.review import _tree_coverage as coverage

    before = {"candidate_sha": "a" * 40, "candidate_tree_sha": "b" * 40}
    after = {"candidate_sha": "c" * 40, "candidate_tree_sha": "d" * 40}
    generation = SimpleNamespace(
        generation_id="cg_" + "1" * 32,
        required_sections=("code",),
        patch_identity=before,
        tree="b" * 40,
        primary_origin_receipt=None,
    )
    carry = SimpleNamespace(
        generation_id=generation.generation_id,
        from_identity=before,
        to_identity=after,
        sections=("code",),
    )
    monkeypatch.setattr(
        authority_projection, "generations", lambda rows: {generation.generation_id: generation}
    )
    monkeypatch.setattr(
        authority_projection, "generation_carries", lambda rows: (carry,)
    )
    monkeypatch.setattr(coverage, "accepted_review_patch_identity", lambda terminal: before)
    monkeypatch.setattr(coverage, "carried_review_patch_identity", lambda *args, **kwargs: after)
    monkeypatch.setattr(coverage, "section_patch_equivalence", lambda *args, **kwargs: None)
    monkeypatch.setattr(coverage, "load_delta_edges", lambda *args, **kwargs: {})
    monkeypatch.setattr(coverage, "mechanical_review_carry", lambda *args: False)
    monkeypatch.setattr(coverage, "chain_covers_rebased_tree", lambda *args, **kwargs: False)
    terminal = {
        "task_id": "review",
        "review_intent": "delivery-code-review",
        "review_lens": "code",
        "snapshot_tree_sha": "b" * 40,
        "patch_identity": before,
        "source_identity": {
            "version": 2,
            "ref": "refs/heads/feature",
            "head": "a" * 40,
        },
        "task_contract": {
            "review_intent": "delivery-code-review",
            "review_lens": "code",
        },
    }
    assert coverage.review_task_covers_tree(
        tmp_path,
        records=[],
        accepted_terminals={"review": terminal},
        latest_verdicts={"review": "pass"},
        finding_records=[],
        task_id="review",
        lens="code",
        current_source={
            "version": 2,
            "ref": "refs/heads/feature",
            "head": "c" * 40,
        },
        current_tree="d" * 40,
    )
