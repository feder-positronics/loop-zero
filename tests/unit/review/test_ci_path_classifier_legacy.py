import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import pytest


from loopzero.review import _ci_path_classifier as module


@pytest.fixture(autouse=True)
def exact_intelflo_path_tables():
    previous = module.PATH_CLASSES, module.PARENT_CLASSES
    module.configure(module._DEFAULT_PATH_CLASSES, module._DEFAULT_PARENT_CLASSES)
    try:
        yield
    finally:
        module.PATH_CLASSES, module.PARENT_CLASSES = previous


@pytest.mark.parametrize(
    ("paths", "expected_true"),
    [
        (
            ["scripts/hooks/ci-mirror-check.sh"],
            {"agent-tooling", "docs", "governance"},
        ),
        (
            ["scripts/util/ci_path_classifier.py"],
            {"agent-tooling", "docs", "governance"},
        ),
        (
            ["scripts/util/changed_backend_db_tests.py"],
            {"agent-tooling", "backend", "docs", "governance"},
        ),
        (
            ["scripts/util/pr_publish.py"],
            {"agent-tooling", "docs", "governance"},
        ),
        (
            ["scripts/util/review_gate_preflight.py"],
            {"agent-tooling", "docs", "governance"},
        ),
        (
            ["scripts/util/security_review_scope.py"],
            {"agent-tooling", "docs", "governance"},
        ),
        (
            ["scripts/util/ci_mirror_receipts.py"],
            {"agent-tooling"},
        ),
        (
            ["scripts/util/pr_closeout.sh"],
            {"agent-tooling"},
        ),
        (
            ["scripts/util/pr_merge_gate.py"],
            {"agent-tooling"},
        ),
        (
            ["fastapi_backend/tests/unit/scripts/test_pr_closeout.py"],
            {"agent-tooling"},
        ),
        (
            ["fastapi_backend/tests/unit/scripts/test_changed_backend_db_tests.py"],
            {"agent-tooling", "docs", "governance"},
        ),
        (
            ["nextjs-frontend/app/page.tsx"],
            {"frontend"},
        ),
        (
            ["fastapi_backend/app/services/listing.py"],
            {"backend"},
        ),
        (
            [".railway/railway.ts"],
            {"backend", "backend-risk"},
        ),
        (
            ["scripts/deploy/railway-workers-setup.sh"],
            {"agent-tooling", "backend", "backend-risk"},
        ),
        (
            ["scripts/deploy/railway-staging-lease.py"],
            {"agent-tooling", "backend", "backend-risk"},
        ),
        (
            ["package.json"],
            {"backend", "backend-risk"},
        ),
        (
            ["pnpm-lock.yaml"],
            {"backend", "backend-risk"},
        ),
        (
            ["pnpm-workspace.yaml"],
            {"backend", "backend-risk"},
        ),
        (
            ["scripts/ci/railway-deployment-policy.json"],
            {"backend", "backend-risk"},
        ),
        (
            ["fastapi_backend/app/routers/content.py"],
            {"backend", "backend-risk"},
        ),
        (
            ["shared-data/openapi.json"],
            {"backend", "backend-risk"},
        ),
        (
            ["nextjs-frontend/app/openapi-client/schema.ts"],
            {"backend", "backend-risk", "frontend"},
        ),
        (
            ["docs/design/blueprints/planned/example.md"],
            {"docs", "blueprints"},
        ),
        (
            ["scripts/docs/blueprint-drift-check.py"],
            {"agent-tooling", "docs", "blueprints", "governance"},
        ),
        (
            [".github/actions/docs-governance/action.yml"],
            {"agent-tooling", "docs", "governance"},
        ),
        (
            [".github/workflows/ci.yml"],
            {
                "agent-tooling",
                "backend",
                "backend-risk",
                "frontend",
                "frontend-e2e",
            },
        ),
        (
            [".github/workflows/scheduled-ci.yml"],
            {
                "agent-tooling",
                "backend",
                "backend-risk",
                "frontend",
                "frontend-e2e",
            },
        ),
        (
            [".github/actions/critical-playwright/action.yml"],
            {
                "agent-tooling",
                "backend",
                "backend-risk",
                "frontend",
                "frontend-e2e",
            },
        ),
        (
            ["fastapi_backend/app/scripts/ops/ensure_playwright_contract.py"],
            {"backend", "frontend", "frontend-e2e"},
        ),
        (
            ["fastapi_backend/app/scripts/ops/playwright_admin_source_fixtures.py"],
            {"backend", "frontend", "frontend-e2e"},
        ),
        (
            ["scripts/util/playwright-fresh-environment.sh"],
            {"agent-tooling", "backend", "frontend", "frontend-e2e"},
        ),
        (
            ["scripts/util/verify-e2e-stack.sh"],
            {"agent-tooling", "backend", "frontend", "frontend-e2e"},
        ),
        (
            ["Makefile"],
            {"agent-tooling", "frontend", "frontend-e2e", "docs", "governance"},
        ),
    ],
)
def test_classify_paths(paths: list[str], expected_true: set[str]) -> None:
    result = module.classify_paths(paths)

    assert {name for name, matched in result.items() if matched} == expected_true


def test_nested_conftest_is_backend_risk() -> None:
    result = module.classify_paths(
        ["fastapi_backend/tests/integration/content/conftest.py"]
    )

    assert result["backend"] is True
    assert result["backend-risk"] is True


def test_mixed_agent_tooling_and_product_backend_keeps_both_lanes() -> None:
    result = module.classify_paths(
        [
            "scripts/util/pr_closeout.sh",
            "fastapi_backend/app/services/listing.py",
        ]
    )

    assert result["agent-tooling"] is True
    assert result["backend"] is True


@pytest.mark.parametrize(
    ("child", "parent"),
    [
        ("backend-risk", "backend"),
        ("frontend-e2e", "frontend"),
        ("blueprints", "docs"),
        ("governance", "docs"),
    ],
)
def test_child_path_classes_always_select_their_parent(child: str, parent: str) -> None:
    for pattern in module.PATH_CLASSES[child]:
        representative = pattern.replace("**", "representative.py")
        classification = module.classify_paths([representative])

        assert classification[parent], f"{child} pattern lacks {parent}: {pattern}"


def test_write_github_outputs_uses_lowercase_booleans(tmp_path: Path) -> None:
    output = tmp_path / "github-output"

    module.write_github_outputs(
        module.classify_paths(["scripts/hooks/ci-mirror-check.sh"]), output
    )

    assert output.read_text(encoding="utf-8").splitlines() == [
        "backend=false",
        "backend-risk=false",
        "agent-tooling=true",
        "frontend=false",
        "frontend-e2e=false",
        "docs=true",
        "blueprints=false",
        "governance=true",
    ]
