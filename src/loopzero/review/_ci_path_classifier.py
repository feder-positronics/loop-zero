"""Classify changed repository paths for local and hosted CI gates."""

from __future__ import annotations

import argparse
import fnmatch
import sys
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path

BACKEND_PATHS = (
    "fastapi_backend/**",
    "!fastapi_backend/tests/unit/scripts/**",
    "scripts/util/test_durations_stats.py",
    "scripts/util/slow_marker_exemptions.txt",
    "scripts/util/changed_backend_db_tests.py",
    "scripts/util/playwright-fresh-environment.sh",
    "scripts/util/verify-e2e-stack.sh",
    "scripts/ci/**",
    "scripts/deploy/**",
    ".railway/**",
    "package.json",
    "pnpm-lock.yaml",
    "pnpm-workspace.yaml",
    "scripts/docs/check_repo_workflow_policy.py",
    "docs/policies/repo-workflow.yaml",
    "shared-data/openapi.json",
    "nextjs-frontend/app/openapi-client/**",
    ".github/workflows/**",
    ".github/actions/critical-playwright/**",
)
AGENT_TOOLING_PATHS = (
    "Makefile",
    "scripts/util/**",
    "scripts/hooks/**",
    "scripts/docs/**",
    "scripts/deploy/**",
    "fastapi_backend/tests/unit/scripts/**",
    ".github/workflows/**",
    ".github/actions/**",
)
BACKEND_RISK_PATHS = (
    "scripts/deploy/**",
    ".railway/**",
    "package.json",
    "pnpm-lock.yaml",
    "pnpm-workspace.yaml",
    "scripts/ci/check-maintenance-railway.sh",
    "scripts/ci/check-release-parity.py",
    "scripts/ci/railway-deployment-policy.json",
    "fastapi_backend/alembic_migrations/**",
    "fastapi_backend/app/auth/**",
    "fastapi_backend/app/bootstrap/routes.py",
    "fastapi_backend/app/core/**",
    "fastapi_backend/app/db/**",
    "fastapi_backend/app/dependencies.py",
    "fastapi_backend/app/dependencies/**",
    "fastapi_backend/app/middleware/**",
    "fastapi_backend/app/models/**",
    "fastapi_backend/app/routers/**",
    "fastapi_backend/app/schemas/**",
    "fastapi_backend/app/tasks/**",
    "fastapi_backend/app/workers/**",
    "fastapi_backend/tests/conftest.py",
    "fastapi_backend/tests/**/conftest.py",
    "fastapi_backend/tests/fixtures/**",
    "fastapi_backend/pyproject.toml",
    "fastapi_backend/uv.lock",
    "shared-data/openapi.json",
    "nextjs-frontend/app/openapi-client/**",
    ".github/workflows/ci.yml",
    ".github/workflows/integration-suite.yml",
    ".github/workflows/scheduled-ci.yml",
    ".github/actions/critical-playwright/**",
)
FRONTEND_PATHS = (
    "Makefile",
    "nextjs-frontend/**",
    "fastapi_backend/app/scripts/ops/ensure_playwright_contract.py",
    "fastapi_backend/app/scripts/ops/playwright_admin_source_fixtures.py",
    "scripts/util/seed_e2e_contract.sh",
    "scripts/util/playwright-fresh-environment.sh",
    "scripts/util/verify-e2e-stack.sh",
    ".github/workflows/ci.yml",
    ".github/workflows/scheduled-ci.yml",
    ".github/actions/critical-playwright/**",
)
FRONTEND_E2E_PATHS = (
    "Makefile",
    "nextjs-frontend/__tests__/critical/**",
    "nextjs-frontend/__tests__/e2e/**",
    "nextjs-frontend/playwright.config.ts",
    "fastapi_backend/app/scripts/ops/ensure_playwright_contract.py",
    "fastapi_backend/app/scripts/ops/playwright_admin_source_fixtures.py",
    "scripts/util/seed_e2e_contract.sh",
    "scripts/util/playwright-fresh-environment.sh",
    "scripts/util/verify-e2e-stack.sh",
    ".github/workflows/ci.yml",
    ".github/workflows/scheduled-ci.yml",
    ".github/actions/critical-playwright/**",
)
DOCS_PATHS = (
    "docs/**",
    ".cursor/**",
    ".agents/**",
    "AGENTS.md",
    "fastapi_backend/AGENTS.md",
    "nextjs-frontend/AGENTS.md",
    "suppressions.yaml",
    "Makefile",
    "scripts/util/agent_event.py",
    "scripts/util/commit-auto-fix.sh",
    "scripts/util/changed_backend_db_tests.py",
    "scripts/util/ci_path_classifier.py",
    "scripts/util/skill_convergence.py",
    "scripts/util/test_skill_convergence.py",
    "scripts/util/skill_semantic_paths.py",
    "scripts/util/test_skill_semantic_paths.py",
    "scripts/util/pr_publish.py",
    "scripts/util/review_gate_preflight.py",
    "scripts/util/security_review_scope.py",
    "scripts/util/skill_convergence_manifest.json",
    "scripts/util/skill_convergence_receipts.json",
    "scripts/hooks/ci-mirror-check.sh",
    "scripts/docs/blueprint-drift-check.py",
    "scripts/docs/check_repo_workflow_policy.py",
    "fastapi_backend/tests/unit/scripts/test_check_repo_workflow_policy.py",
    "fastapi_backend/tests/unit/scripts/test_changed_backend_db_tests.py",
    ".github/actions/docs-governance/**",
)
BLUEPRINT_PATHS = (
    "docs/design/blueprints/**",
    "scripts/docs/blueprint-drift-check.py",
)
GOVERNANCE_PATHS = (
    "Makefile",
    "scripts/util/ci_path_classifier.py",
    "scripts/util/pr_publish.py",
    "scripts/util/review_gate_preflight.py",
    "scripts/util/security_review_scope.py",
    "scripts/util/skill_convergence.py",
    "scripts/util/test_skill_convergence.py",
    "scripts/util/skill_semantic_paths.py",
    "scripts/util/test_skill_semantic_paths.py",
    "scripts/util/skill_convergence_manifest.json",
    "scripts/util/skill_convergence_receipts.json",
    "scripts/hooks/ci-mirror-check.sh",
    "scripts/util/changed_backend_db_tests.py",
    "scripts/docs/blueprint-drift-check.py",
    "scripts/docs/check_repo_workflow_policy.py",
    "fastapi_backend/tests/unit/scripts/test_check_repo_workflow_policy.py",
    "fastapi_backend/tests/unit/scripts/test_changed_backend_db_tests.py",
    ".github/actions/docs-governance/**",
)

PATH_CLASSES: Mapping[str, tuple[str, ...]] = {
    "backend": BACKEND_PATHS,
    "backend-risk": BACKEND_RISK_PATHS,
    "agent-tooling": AGENT_TOOLING_PATHS,
    "frontend": FRONTEND_PATHS,
    "frontend-e2e": FRONTEND_E2E_PATHS,
    "docs": DOCS_PATHS,
    "blueprints": BLUEPRINT_PATHS,
    "governance": GOVERNANCE_PATHS,
}
PARENT_CLASSES: Mapping[str, str] = {
    "backend-risk": "backend",
    "frontend-e2e": "frontend",
    "blueprints": "docs",
    "governance": "docs",
}
_DEFAULT_PATH_CLASSES = dict(PATH_CLASSES)
_DEFAULT_PARENT_CLASSES = dict(PARENT_CLASSES)


def configure(
    path_classes: Mapping[str, Sequence[str]],
    parent_classes: Mapping[str, str] | None = None,
) -> None:
    """Replace product path names with validated consumer policy."""
    global PATH_CLASSES, PARENT_CLASSES
    PATH_CLASSES = (
        {name: tuple(patterns) for name, patterns in path_classes.items()}
        if path_classes
        else _DEFAULT_PATH_CLASSES
    )
    PARENT_CLASSES = (
        dict(parent_classes) if parent_classes else _DEFAULT_PARENT_CLASSES
    )


def _matches_pattern(path: str, pattern: str) -> bool:
    if pattern.endswith("/**"):
        return path.startswith(pattern.removesuffix("**"))
    if pattern.endswith("/"):
        return path.startswith(pattern)
    if any(character in pattern for character in "*?["):
        return fnmatch.fnmatchcase(path, pattern)
    return path == pattern


def _matches_patterns(path: str, patterns: Sequence[str]) -> bool:
    """Apply paths-filter's some-with-excludes predicate to one path."""
    included = any(
        _matches_pattern(path, pattern)
        for pattern in patterns
        if not pattern.startswith("!")
    )
    excluded = any(
        _matches_pattern(path, pattern[1:])
        for pattern in patterns
        if pattern.startswith("!")
    )
    return included and not excluded


def classify_paths(paths: Iterable[str]) -> dict[str, bool]:
    """Return every stable CI path class for the supplied changed paths."""
    normalized = tuple(path.strip() for path in paths if path.strip())
    classification = {
        name: any(_matches_patterns(path, patterns) for path in normalized)
        for name, patterns in PATH_CLASSES.items()
    }
    for child, parent in PARENT_CLASSES.items():
        if child in classification and parent in classification:
            classification[parent] = classification[parent] or classification[child]
    return classification


def write_github_outputs(classification: Mapping[str, bool], output: Path) -> None:
    """Append a classification using GitHub Actions output-file syntax."""
    with output.open("a", encoding="utf-8") as stream:
        for name in PATH_CLASSES:
            value = "true" if classification.get(name, False) else "false"
            stream.write(f"{name}={value}\n")


# CLI parsing and GitHub Actions emission orchestration stay in the consumer.
