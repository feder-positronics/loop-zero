"""Consumer size policy fixture from the sanitized import."""
ALLOWLIST_PREFIXES: tuple[str, ...] = ("fastapi_backend/app/parsers/",)
# Cohesive by design: exempt regardless of size.
COHESIVE_FILES: frozenset[str] = frozenset(
    {
        # cohesive JATS XML parser
        "fastapi_backend/app/etl/fulltext/providers/pmc/jats_parser.py",
    }
)
# Baseline ratchet: exempt only while still over the hard limit; a test fails
# the entry once the file is decomposed below it, locking in the win.
BASELINE_FILES: frozenset[str] = frozenset(
    {
        # -- scripts baseline over 1600 lines at #3944 adoption (ratchet) --
        "scripts/deploy/railway-staging-lease.py",
        "scripts/docs/check_repo_workflow_policy.py",
        "scripts/ops/compute_monitor.py",
        "scripts/util/agent_dispatch.py",
        "scripts/util/agent_runtimes/codex.py",
        "scripts/util/agent_runtimes/sdk_bridge.py",
        "scripts/util/audit_manifest.py",
        "scripts/util/decision_review_dispatch.py",
        "scripts/util/delivery_control.py",
        "scripts/util/delivery_pipeline.py",
        "scripts/util/final_ci_gate.py",
        "scripts/util/finding_ledger.py",
        "scripts/util/guardian_delivery.py",
        "scripts/util/guardian_dispatch.py",
        "scripts/util/guardian_gate_b.py",
        "scripts/util/pr_merge_gate.py",
        "scripts/util/runtime_owner.py",
        "scripts/util/skill_convergence.py",
        # -- Python test baseline over 4000 lines at #3944 adoption (ratchet) --
        "fastapi_backend/tests/integration/api/test_agent_tasks_api.py",
        "fastapi_backend/tests/unit/agent_runner/test_intelflo_agent.py",
        "fastapi_backend/tests/unit/scripts/test_agent_dispatch.py",
        "fastapi_backend/tests/unit/scripts/test_agent_runtimes.py",
        "fastapi_backend/tests/unit/scripts/test_guardian_dispatch.py",
        "fastapi_backend/tests/unit/scripts/test_railway_staging_lease.py",
    }
)
ALLOWLIST_FILES: frozenset[str] = COHESIVE_FILES | BASELINE_FILES

