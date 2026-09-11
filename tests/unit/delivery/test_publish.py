import json
import subprocess
from pathlib import Path

import pytest

from loopzero.delivery import publish


CONTRACT = publish.BodyContract(("Context and goal", "Validation"))
BODY = """## Context and goal

Portable publication behavior.

## Validation

Unit checks passed.

Closes #42
"""


def artifact():
    return {
        "base_sha": "a" * 40,
        "head_sha": "b" * 40,
        "head_tree_sha": "c" * 40,
    }


def test_body_contract_is_supplied_by_configuration():
    assert publish.validate_contract(BODY, contract=CONTRACT) is None
    assert "## Validation" in publish.validate_contract(
        "## Context and goal\n\ntext\n\nCloses #1\n", contract=CONTRACT
    )


def test_issue_less_ready_body_needs_configured_standalone_authority():
    body = BODY.replace("Closes #42\n", "")
    assert "No work reference" in publish.validate_contract(body, contract=CONTRACT)
    assert publish.validate_contract(body, contract=CONTRACT, standalone=True) is None
    assert publish.validate_contract(
        body + "Standalone-Reason: maintenance\n", contract=CONTRACT
    ) is None


def test_evidence_binding_is_single_and_exact():
    bound = publish.bind_loopzero_evidence(
        BODY, artifact=artifact(), review_task_id="review-1", run_id="run-1"
    )
    assert bound.count(publish.EVIDENCE_START) == 1
    publish.validate_loopzero_evidence(
        bound, artifact=artifact(), review_task_id="review-1", run_id="run-1"
    )
    with pytest.raises(publish.PublicationError, match="does not match"):
        publish.validate_loopzero_evidence(
            bound, artifact=artifact(), review_task_id="review-2", run_id="run-1"
        )


def test_publication_receipt_is_idempotent_and_conflict_safe(tmp_path: Path):
    record = {
        "status": "published", "pr": 42, "base": "a", "head": "b",
        "expected_head": "b", "review_task_id": None, "review_exemption": "T0",
        "review_risk_json": "{}", "review_risk_sha256": "c", "review_risk_tier": "T0",
        "admission_tier_floor": "T0", "run_id": "run", "adopted": False,
    }
    first = publish.write_published_evidence_once(tmp_path, record, generation=0)
    second = publish.write_published_evidence_once(
        tmp_path, record | {"adopted": True}, generation=0
    )
    assert first == second
    rows = [json.loads(line) for line in first.read_text().splitlines()]
    assert rows == [record]


class Runner:
    def __init__(self, responses):
        self.responses = responses

    def run(self, args, *, check=True):
        code, stdout = self.responses[tuple(args)]
        return subprocess.CompletedProcess(args, code, stdout, "")


def test_local_prerequisites_and_rename_paths_fail_closed():
    runner = Runner(
        {
            ("git", "rev-parse", "HEAD"): (0, "a" * 40 + "\n"),
            ("git", "symbolic-ref", "--quiet", "--short", "HEAD"): (0, "feature\n"),
            ("git", "status", "--porcelain"): (0, ""),
            ("git", "diff", "--name-only", "-z", "--no-renames", "base..HEAD"): (
                0, "old.md\0new.md\0"
            ),
        }
    )
    publish.require_local_publication_prerequisites(
        head="feature", expected_head="a" * 40, runner=runner
    )
    assert publish.changed_paths_between(runner, "base", "HEAD") == (
        "old.md", "new.md"
    )


def test_obligation_policy_is_injected():
    calls = []
    publish.require_obligation_acknowledgment(
        base="base", head="head", body="ack",
        evaluator=lambda *args, **kwargs: calls.append((args, kwargs)) or False,
    )
    assert calls[0][1] == {"acknowledgment_text": "ack"}
    with pytest.raises(publish.PublicationError, match="acknowledgment"):
        publish.require_obligation_acknowledgment(
            base="base", head="head", body="", evaluator=lambda *args, **kwargs: True
        )


def test_thread_reader_paginates_through_github_boundary():
    class Github:
        def __init__(self):
            self.calls = 0

        def repository(self):
            return type("Repo", (), {"owner": "o", "name": "r"})()

        def api(self, endpoint, *, fields):
            self.calls += 1
            return {"data": {"repository": {"pullRequest": {"reviewThreads": {
                "nodes": [],
                "pageInfo": {"hasNextPage": self.calls == 1, "endCursor": "next"},
            }}}}}

    github = Github()
    publish.require_resolved_review_threads(github, pr=7)
    assert github.calls == 2
