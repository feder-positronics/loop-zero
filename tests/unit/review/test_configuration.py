from pathlib import Path
from types import SimpleNamespace

from loopzero.kernel import seams
from loopzero.review import acceptance, chain, configure, evidence, findings, risk, routing
from loopzero.kernel import settings as kernel_settings


def test_composition_root_configures_kernel_seams(tmp_path):
    old_kernel = kernel_settings.settings
    contexts = (
        (acceptance._PROFILE, acceptance._PROFILE.get()),
        (chain._PROFILE, chain._PROFILE.get()),
        (evidence._PROFILE, evidence._PROFILE.get()),
        (risk._SETTINGS, risk._SETTINGS.get()),
        (routing._SETTINGS, routing._SETTINGS.get()),
    )
    old_findings = (
        findings.FINDINGS_DIR, findings.OPERATIONS_DIR, findings._SEVERITY_RANK,
        findings._SECURITY_PATH_PATTERNS, findings._CONFIGURED_ROOT,
        findings._REQUIRE_PR_SCOPE,
    )
    profile = SimpleNamespace(
        root=tmp_path, env_prefix="CONSUMER", audit_root=Path("private"),
        state_root="~/.local/state/consumer", toolchain={},
        aliases={}, tiers={}, routing_budgets={}, routing_policy_version="policy",
        telemetry_schema_version="telemetry", compatible_policy_versions=(),
        default_timeout_s=30, engine_cooldown_s=30, required_sections=("code",),
        max_reviews_per_pr=1, max_delta_reviews=1,
        finding_severities=("critical", "important", "suggestion"),
        path_classes={}, security_patterns=(),
        github=SimpleNamespace(ref_namespace="refs/consumer"),
    )
    try:
        configure(profile)
        assert seams._latest_attempt_settlement_indices(
            [{"type": "inline", "task_id": "task"}]
        ) == frozenset({0})
    finally:
        kernel_settings.configure(old_kernel)
        for context, value in contexts:
            context.set(value)
        (
            findings.FINDINGS_DIR, findings.OPERATIONS_DIR, findings._SEVERITY_RANK,
            findings._SECURITY_PATH_PATTERNS, findings._CONFIGURED_ROOT,
            findings._REQUIRE_PR_SCOPE,
        ) = old_findings
