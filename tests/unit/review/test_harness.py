import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from loopzero.runners.fake import ScenarioSpec
from loopzero.review import harness


def _profile(tmp_path):
    return SimpleNamespace(
        aliases={
            "alternate": SimpleNamespace(runner="fake", model="review-model", write=False)
        },
        tiers={"review": SimpleNamespace(alias="alternate", effort="low")},
        routing_budgets={"low": 1.0},
        routing_policy_version="policy-v1",
        telemetry_schema_version="telemetry-v1",
        compatible_policy_versions=(),
        default_timeout_s=30,
        engine_cooldown_s=10,
        audit_root=Path("private"),
        env_prefix="CONSUMER",
        toolchain={}, state_root="unused", state_root_explicit=False,
        root=tmp_path,
    )


def test_harness_uses_registry_and_normalized_runtime_contract(tmp_path):
    result = harness.run_review(
        _profile(tmp_path), worktree=tmp_path, alias="alternate", effort="low",
        prompt="review this", attempt_id="attempt-1",
        adapter_options={
            "scenario": ScenarioSpec(
                output=json.dumps(
                    {"findings": [{"severity": "important", "claim": "fix it"}]}
                )
            )
        },
    )
    assert result.finding_count == 1
    assert result.max_severity == "important"
    assert result.runtime.vendor == "fake"


def test_harness_rejects_malformed_or_failed_runtime_result(tmp_path):
    with pytest.raises(harness.CrossHarnessError, match="not JSON"):
        harness.run_review(
            _profile(tmp_path), worktree=tmp_path, alias="alternate", effort="low",
            prompt="review", attempt_id="attempt-1",
            adapter_options={"scenario": ScenarioSpec(output="not json")},
        )
    with pytest.raises(harness.CrossHarnessError, match="did not complete"):
        harness.run_review(
            _profile(tmp_path), worktree=tmp_path, alias="alternate", effort="low",
            prompt="review", attempt_id="attempt-2",
            adapter_options={"scenario": "timeout/expiry"},
        )


def test_harness_contains_no_native_command_shapes():
    source = Path(harness.__file__).read_text(encoding="utf-8")
    assert "subprocess" not in source
    assert "_command(" not in source
