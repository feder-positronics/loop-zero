import importlib.util
import json
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest


from loopzero.review import preflight as module


class FakeRunner:
    def __init__(self, responses: dict[tuple[str, ...], tuple[int, str]]) -> None:
        self.responses = responses

    def run(self, args: list[str], *, check: bool = True):
        returncode, stdout = self.responses.get(tuple(args), (0, ""))
        result = subprocess.CompletedProcess(args, returncode, stdout, "")
        if check and returncode:
            raise AssertionError(f"unexpected failing command: {args}")
        return result


def test_collect_changed_paths_includes_every_candidate_state() -> None:
    runner = FakeRunner(
        {
            ("git", "rev-parse", "origin/main"): (0, "base\n"),
            ("git", "diff", "--name-only", "-z", "base...HEAD"): (0, "committed.py\0"),
            ("git", "diff", "--name-only", "-z"): (0, "unstaged.py\0"),
            ("git", "diff", "--cached", "--name-only", "-z"): (0, "staged.py\0"),
            ("git", "ls-files", "--others", "--exclude-standard", "-z"): (
                0,
                "untracked.py\0",
            ),
        }
    )

    assert module.collect_changed_paths(runner, "origin/main") == (
        "committed.py",
        "staged.py",
        "unstaged.py",
        "untracked.py",
    )


def test_collect_changed_paths_rejects_control_characters() -> None:
    runner = FakeRunner(
        {
            ("git", "rev-parse", "origin/main"): (0, "base\n"),
            ("git", "diff", "--name-only", "-z", "base...HEAD"): (
                0,
                "docs/unsafe\nname.md\0",
            ),
        }
    )

    with pytest.raises(module.ReviewPreflightError, match="control character"):
        module.collect_changed_paths(runner, "origin/main")


def test_base_freshness_rejects_overlapping_stale_candidate() -> None:
    runner = FakeRunner(
        {
            ("git", "merge-base", "--is-ancestor", "origin/main", "HEAD"): (1, ""),
            ("git", "merge-base", "origin/main", "HEAD"): (0, "base\n"),
            ("git", "diff", "--name-only", "-z", "base..origin/main"): (
                0,
                "docs/guide.md\0",
            ),
        }
    )

    with pytest.raises(module.ReviewPreflightError, match="overlaps base changes"):
        module.ensure_local_base_freshness(runner, "origin/main", ("docs/guide.md",))


def test_base_freshness_allows_non_risk_stale_candidate_without_overlap() -> None:
    runner = FakeRunner(
        {
            ("git", "merge-base", "--is-ancestor", "origin/main", "HEAD"): (1, ""),
            ("git", "merge-base", "origin/main", "HEAD"): (0, "base\n"),
            ("git", "diff", "--name-only", "-z", "base..origin/main"): (
                0,
                "docs/other.md\0",
            ),
        }
    )

    assert (
        module.ensure_local_base_freshness(
            runner, "origin/main", ("docs/candidate.md",)
        )
        == "behind-without-overlap"
    )


def test_preflight_returns_scope_from_fetched_merge_base(monkeypatch) -> None:
    runner = FakeRunner(
        {
            ("git", "fetch", "--quiet", "origin", "main"): (0, ""),
            ("git", "status", "--porcelain=v1", "-z", "--untracked-files=all"): (
                0,
                "",
            ),
            ("git", "rev-parse", "origin/main"): (0, "base-head\n"),
            ("git", "diff", "--name-only", "-z", "base-head...HEAD"): (
                0,
                "app/config.py\0",
            ),
            ("git", "merge-base", "--is-ancestor", "origin/main", "HEAD"): (
                0,
                "",
            ),
            ("git", "merge-base", "origin/main", "HEAD"): (0, "merge-base\n"),
        }
    )
    risk_json = (
        '{"required_sections":["code","security"],'
        '"security_trigger_paths":["app/config.py"]}'
    )
    monkeypatch.setattr(
        module,
        "compute_review_risk",
        lambda worktree, base, head, *, runner: {
            "review_risk_json": risk_json,
            "review_risk_sha256": "a" * 64,
        },
    )
    monkeypatch.setattr(
        module, "parse_review_risk", lambda payload: json.loads(payload)
    )

    result = module.run_preflight(runner, body="No visual changes.")

    assert result["security_trigger_paths"] == ["app/config.py"]
    assert result["required_sections"] == ["code", "security"]
    assert result["review_risk_sha256"] == "a" * 64
    assert result["visual_evidence_ok"] is True


def test_preflight_warns_on_missing_visual_evidence_without_blocking(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Advisory since PIL-VISUAL-DEMOTE-1: a UI diff without evidence emits a
    stderr warning and a machine-readable not-ok flag instead of raising."""
    runner = FakeRunner(
        {
            ("git", "fetch", "--quiet", "origin", "main"): (0, ""),
            ("git", "status", "--porcelain=v1", "-z", "--untracked-files=all"): (
                0,
                "",
            ),
            ("git", "rev-parse", "origin/main"): (0, "base-head\n"),
            ("git", "diff", "--name-only", "-z", "base-head...HEAD"): (
                0,
                "nextjs-frontend/components/repair-card.tsx\0",
            ),
            ("git", "merge-base", "--is-ancestor", "origin/main", "HEAD"): (
                0,
                "",
            ),
            ("git", "merge-base", "origin/main", "HEAD"): (0, "merge-base\n"),
        }
    )
    risk_json = '{"required_sections":["code"],"security_trigger_paths":[]}'
    monkeypatch.setattr(
        module,
        "compute_review_risk",
        lambda worktree, base, head, *, runner: {
            "review_risk_json": risk_json,
            "review_risk_sha256": "b" * 64,
        },
    )
    monkeypatch.setattr(
        module, "parse_review_risk", lambda payload: json.loads(payload)
    )

    result = module.run_preflight(runner, body="No evidence in this body.")

    assert result["visual_evidence_ok"] is False
    assert result["visual_evidence"]
    assert "visual-evidence advisory" in capsys.readouterr().err


def test_preflight_rejects_dirty_candidate_before_scope_derivation() -> None:
    runner = FakeRunner(
        {
            ("git", "fetch", "--quiet", "origin", "main"): (0, ""),
            ("git", "status", "--porcelain=v1", "-z", "--untracked-files=all"): (
                0,
                " M scripts/util/agent_dispatch.py\0",
            ),
        }
    )

    with pytest.raises(module.ReviewPreflightError, match="clean normalized"):
        module.run_preflight(runner, body="No visual changes.")
