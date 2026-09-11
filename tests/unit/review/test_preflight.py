import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from loopzero.review import preflight


class FakeRunner:
    def __init__(self, responses):
        self.responses = responses
        self.repo = Path("/consumer")

    def run(self, args, *, check=True):
        code, output = self.responses.get(tuple(args), (0, ""))
        result = subprocess.CompletedProcess(args, code, output, "")
        if check and code:
            raise AssertionError(args)
        return result


def test_collect_changed_paths_includes_every_candidate_state():
    runner = FakeRunner(
        {
            ("git", "rev-parse", "origin/main"): (0, "base\n"),
            ("git", "diff", "--name-only", "-z", "base...HEAD"): (0, "committed.py\0"),
            ("git", "diff", "--name-only", "-z"): (0, "unstaged.py\0"),
            ("git", "diff", "--cached", "--name-only", "-z"): (0, "staged.py\0"),
            ("git", "ls-files", "--others", "--exclude-standard", "-z"): (0, "new.py\0"),
        }
    )
    assert preflight.collect_changed_paths(runner, "origin/main") == (
        "committed.py", "new.py", "staged.py", "unstaged.py"
    )


def test_base_freshness_uses_injected_classifier_and_overlap_guard():
    runner = FakeRunner(
        {
            ("git", "merge-base", "--is-ancestor", "origin/main", "HEAD"): (1, ""),
            ("git", "merge-base", "origin/main", "HEAD"): (0, "base\n"),
            ("git", "diff", "--name-only", "-z", "base..origin/main"): (0, "same.py\0"),
        }
    )
    with pytest.raises(preflight.ReviewPreflightError, match="strict-base"):
        preflight.ensure_local_base_freshness(
            runner, "origin/main", ("a.py",), requires_strict_base=lambda paths: True
        )
    with pytest.raises(preflight.ReviewPreflightError, match="overlaps"):
        preflight.ensure_local_base_freshness(
            runner, "origin/main", ("same.py",), requires_strict_base=lambda paths: False
        )


def test_preflight_injects_evidence_and_risk_policy():
    runner = FakeRunner(
        {
            ("git", "rev-parse", "origin/main"): (0, "base\n"),
            ("git", "diff", "--name-only", "-z", "base...HEAD"): (0, "a.py\0"),
            ("git", "merge-base", "origin/main", "HEAD"): (0, "merge-base\n"),
        }
    )
    calls = []
    result = preflight.run_preflight(
        runner,
        body="evidence",
        evidence_evaluator=lambda paths, body: SimpleNamespace(ok=True, reason="present"),
        risk_classifier=lambda repo, base, head: calls.append((repo, base, head)) or {
            "required_sections": ["code"], "security_trigger_paths": []
        },
        requires_strict_base=lambda paths: False,
    )
    assert calls == [(Path("/consumer"), "merge-base", "HEAD")]
    assert result["visual_evidence_ok"] is True
    assert result["required_sections"] == ["code"]


def test_preflight_rejects_dirty_candidate_before_policy_callbacks():
    runner = FakeRunner(
        {
            ("git", "status", "--porcelain=v1", "-z", "--untracked-files=all"): (
                0, " M a.py\0"
            )
        }
    )
    with pytest.raises(preflight.ReviewPreflightError, match="clean normalized"):
        preflight.run_preflight(
            runner, body="", evidence_evaluator=lambda *_: None,
            risk_classifier=lambda *_: {}, requires_strict_base=lambda _: False,
        )
