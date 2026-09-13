from pathlib import Path
from types import SimpleNamespace

import pytest

from loopzero.config import Alias, Tier
from loopzero.kernel.gitscope import DispatchError
from loopzero.review import routing


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
