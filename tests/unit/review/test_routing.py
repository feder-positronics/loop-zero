from pathlib import Path
from types import SimpleNamespace

import pytest

from loopzero.config import Alias, Tier
from loopzero.kernel.gitscope import DispatchError
from loopzero.review import routing
from loopzero.review import authority


def configure():
    routing.configure(
        SimpleNamespace(
            aliases={
                "writer": Alias("fake", "consumer-model-1", write=True),
                "reviewer": Alias("fake", "consumer-model-2", write=False),
            },
            tiers={"C": Tier("writer", "low"), "S": Tier("reviewer", "high", True)},
            routing_budgets={"medium": 5.0, "high": 10.0},
            routing_policy_version="consumer-policy-v1",
            telemetry_schema_version="consumer-telemetry-v1",
            compatible_policy_versions=("consumer-policy-v1",),
            default_timeout_s=900,
            engine_cooldown_s=600,
            audit_root=Path("records"),
            env_prefix="CONSUMER",
        )
    )


def test_aliases_models_tiers_and_budgets_are_consumer_data():
    configure()
    assert routing.resolve_tier_default("S") == ("reviewer", "high")
    decision = routing.route("writer", "low")
    assert (decision.engine, decision.model, decision.backup_engine) == (
        "fake", "consumer-model-1", None
    )
    assert routing.DEFAULT_MEDIUM_BUDGET_USD == 5.0
    assert routing.DISPATCH_POLICY_VERSION == "consumer-policy-v1"
    assert routing.TELEMETRY_SCHEMA_VERSION == "consumer-telemetry-v1"


def test_unknown_alias_tier_and_effort_fail_closed():
    configure()
    with pytest.raises(DispatchError, match="unknown tier"):
        routing.resolve_tier_default("missing")
    with pytest.raises(DispatchError, match="unknown model alias"):
        routing.route("missing", "low")
    with pytest.raises(DispatchError, match="unknown effort"):
        routing.route("writer", "impossible")


def test_verifier_must_be_a_different_configured_model():
    configure()
    assert routing.verifier_identity_is_independent(
        worker_identity="fake:consumer-model-1", worker_alias="writer",
        worker_model="consumer-model-1", verifier_identity="session-2",
        verifier_alias="reviewer",
    )
    assert not routing.verifier_identity_is_independent(
        worker_identity="fake:consumer-model-1", worker_alias="writer",
        worker_model="consumer-model-1", verifier_identity="session-2",
        verifier_alias="writer",
    )


def retry_outcome(task_id, *, reason="transport-disconnect"):
    return {
        "type": "attempt-terminal",
        "task_id": task_id,
        "work_unit_id": task_id,
        "unit_attempt_number": 1,
        "effective_alias": "luna",
        "status": "infrastructure-failure",
        "terminal_reason": reason,
        "review_lineage_id": "rl_" + "1" * 32,
        "review_generation_id": "cg_" + "2" * 32,
        "review_family": "delivery",
        "review_slot_kind": "primary",
    }


def test_retry_bound_is_per_content_obligation_across_task_ids(monkeypatch):
    first = retry_outcome("provider-one")
    monkeypatch.setattr(authority, "authenticated_retry_outcomes", lambda rows: rows)
    assert routing.validate_retry_policy(
        [first],
        task_id="provider-two",
        work_unit_id="different-work-unit",
        alias="terra",
        effort="high",
        lineage="rl_" + "1" * 32,
        generation="cg_" + "2" * 32,
        family="delivery",
        slot_kind="primary",
    ) == 2

    second = {**retry_outcome("provider-two"), "unit_attempt_number": 2}
    with pytest.raises(DispatchError, match="retry budget exhausted"):
        routing.validate_retry_policy(
            [first, second],
            task_id="provider-three",
            work_unit_id="yet-another-unit",
            alias="sol",
            effort="high",
            lineage="rl_" + "1" * 32,
            generation="cg_" + "2" * 32,
            family="delivery",
            slot_kind="primary",
        )


def test_unresolved_obligation_cannot_retry(monkeypatch):
    unresolved = retry_outcome("unknown", reason="unknown-provider-result")
    monkeypatch.setattr(authority, "authenticated_retry_outcomes", lambda rows: rows)
    with pytest.raises(DispatchError, match="not released"):
        routing.validate_retry_policy(
            [unresolved],
            task_id="retry",
            work_unit_id="retry",
            alias="terra",
            effort="high",
            lineage="rl_" + "1" * 32,
            generation="cg_" + "2" * 32,
            family="delivery",
            slot_kind="primary",
        )
