"""Source assertions whose owners stay outside the A4 mechanism package."""
import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.skip(
    reason="(a) baseline provenance and Guardian tamper manifests stay with excluded controllers"
)
module = None


class TestExcludedDispatcherBaselineProvenance:
    def test_modified_existing_tests_uses_clean_baseline_tree(
        self, git_repo: Path
    ) -> None:
        existing = "fastapi_backend/tests/unit/test_existing.py"
        added = "fastapi_backend/tests/unit/test_added.py"
        existing_path = git_repo / existing
        existing_path.parent.mkdir(parents=True)
        existing_path.write_text("def test_existing(): pass\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(git_repo), "add", existing], check=True)
        subprocess.run(
            ["git", "-C", str(git_repo), "commit", "-qm", "add existing test"],
            check=True,
        )
        baseline = module.capture_worktree_baseline(git_repo)

        existing_path.write_text("def test_existing(): assert True\n", encoding="utf-8")
        (git_repo / added).write_text("def test_added(): pass\n", encoding="utf-8")

        assert baseline["paths"] == {}
        assert module.modified_existing_test_paths(
            [existing, added], baseline, worktree=git_repo
        ) == [existing]

    def test_modified_existing_tests_uses_serial_predecessor_tree(
        self, git_repo: Path
    ) -> None:
        existing = "fastapi_backend/tests/unit/test_predecessor.py"
        existing_path = git_repo / existing
        existing_path.parent.mkdir(parents=True)
        existing_path.write_text("def test_predecessor(): pass\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(git_repo), "add", existing], check=True)
        subprocess.run(
            ["git", "-C", str(git_repo), "commit", "-qm", "add predecessor test"],
            check=True,
        )
        predecessor_baseline = module.governed_source_identity(git_repo)

        existing_path.write_text(
            "def test_predecessor(): assert True\n", encoding="utf-8"
        )

        assert module.modified_existing_test_paths(
            [existing], predecessor_baseline, worktree=git_repo
        ) == [existing]

    def test_modified_existing_tests_lists_only_baseline_test_files(self) -> None:
        baseline = {
            "paths": {
                "fastapi_backend/tests/integration/test_team_regression.py": "d1",
                "fastapi_backend/app/services/teams.py": "d2",
                "nextjs-frontend/components/team.test.tsx": "d3",
            }
        }
        baseline["paths"]["fastapi_backend/conftest.py"] = "d4"
        changed = [
            "fastapi_backend/tests/integration/test_team_regression.py",
            "fastapi_backend/tests/integration/test_new_feature.py",
            "fastapi_backend/app/services/teams.py",
            "fastapi_backend/conftest.py",
            "nextjs-frontend/components/team.test.tsx",
        ]

        assert module.modified_existing_test_paths(changed, baseline) == [
            "fastapi_backend/conftest.py",
            "fastapi_backend/tests/integration/test_team_regression.py",
            "nextjs-frontend/components/team.test.tsx",
        ]

    def test_modified_existing_tests_tolerates_invalid_baseline(
        self, git_repo: Path
    ) -> None:
        assert module.modified_existing_test_paths(
            ["tests/test_x.py"],
            {"tree_sha": "not-a-git-tree", "paths": {"tests/test_x.py": "d"}},
            worktree=git_repo,
        ) == ["tests/test_x.py"]

    def test_modified_existing_tests_reports_unproven_tree_and_head(
        self, git_repo: Path
    ) -> None:
        paths, diagnostic = module.modified_existing_test_provenance(
            ["tests/test_x.py"],
            {"tree_sha": "not-a-git-tree", "head": "also-not-a-git-tree", "paths": {}},
            worktree=git_repo,
        )

        assert paths == []
        assert diagnostic == (
            "modified-existing-test provenance is unproven: baseline tree and "
            "legacy HEAD are unavailable"
        )

    def test_modified_existing_tests_uses_legacy_head_baseline(
        self, git_repo: Path
    ) -> None:
        existing = "fastapi_backend/tests/unit/test_legacy.py"
        existing_path = git_repo / existing
        existing_path.parent.mkdir(parents=True)
        existing_path.write_text("def test_legacy(): pass\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(git_repo), "add", existing], check=True)
        subprocess.run(
            ["git", "-C", str(git_repo), "commit", "-qm", "add legacy test"],
            check=True,
        )
        legacy_baseline = {"head": module.worktree_head(git_repo), "paths": {}}

        existing_path.write_text("def test_legacy(): assert True\n", encoding="utf-8")

        assert module.modified_existing_test_paths(
            [existing], legacy_baseline, worktree=git_repo
        ) == [existing]


def test_extracted_modules_stay_in_guardian_tamper_manifests() -> None:
    """The #3944 split must never move authority code outside the guardian
    tamper-evidence boundary (critical finding f_dbdcca14099c8daed3ba)."""
    repo_root = Path(__file__).resolve().parents[4]
    for rel in (
        "scripts/util/guardian_dispatch.py",
        "scripts/util/guardian_gate_b.py",
    ):
        src = (repo_root / rel).read_text(encoding="utf-8")
        for pinned in (
            "scripts/util/dispatch_acceptance.py",
            "scripts/util/dispatch_acceptance_grammar.py",
            "scripts/util/dispatch_common.py",
            "scripts/util/dispatch_review_evidence.py",
            "scripts/util/dispatch_routing.py",
        ):
            assert f'"{pinned}",' in src, f"{rel} must pin {pinned}"
