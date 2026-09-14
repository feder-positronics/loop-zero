"""Consumer status admission and the prepared IntelFlo adapter."""

import importlib.util
import json
from copy import deepcopy
from pathlib import Path

import pytest
from test_code_health import commit, policy
from test_code_health import repository as repository_fixture

from loopzero.code_health import analyzers
from loopzero.code_health.collector import collect
from loopzero.code_health.evidence import collector_status

repository = repository_fixture


def test_status_rejects_stale_incomplete_and_mismatched_evidence(repository):
    sha = commit(repository, {"app.py": "def f(): return 1\n"})
    selected = policy()
    report = collect(repository, sha, selected)
    good = collector_status(report, exit_code=0, head=sha, policy=selected)
    assert good["valid"] and not good["threshold_failed"]
    for key, value in (
        ("sha", "f" * 40),
        ("completed", []),
        ("requested", [{}]),
        ("errors", ["failed"]),
    ):
        bad = deepcopy(report)
        bad["head"][key] = value
        assert not collector_status(bad, exit_code=0, head=sha, policy=selected)[
            "valid"
        ]
    for key, value in (
        ("policy_id", "wrong"),
        ("tools", {"ruff": "unknown"}),
        ("valid", 1),
    ):
        bad = deepcopy(report)
        bad[key] = value
        assert not collector_status(bad, exit_code=0, head=sha, policy=selected)[
            "valid"
        ]
    assert not collector_status(report, exit_code=1, head=sha, policy=selected)["valid"]
    assert not collector_status(None, exit_code=0, head=sha, policy=selected)["valid"]


def test_compare_status_requires_both_expected_sources(repository):
    base = commit(repository, {"app.py": "x = 1\n"})
    head = commit(repository, {"app.py": "x = 2\n"})
    report = collect(repository, head, policy(), base)
    assert collector_status(report, exit_code=0, head=head, base=base, policy=policy())[
        "valid"
    ]
    assert not collector_status(
        report, exit_code=0, head=head, base=head, policy=policy()
    )["valid"]
    assert not collector_status(report, exit_code=0, head=head, policy=policy())[
        "valid"
    ]


def adapter():
    path = Path(__file__).resolve().parents[2] / "examples/intelflo/code_health.py"
    spec = importlib.util.spec_from_file_location("intelflo_code_health", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_adapter_preserves_status_and_records_failed_collection(
    repository, monkeypatch
):
    sha = commit(repository, {"fastapi_backend/app/a.py": "def f(): return 1\n"})

    def unavailable(*args, **kwargs):
        raise ValueError("analyzer unavailable")

    monkeypatch.setattr(analyzers, "tool", unavailable)
    status_path = repository / "status.json"
    status_path.write_text(
        json.dumps(
            {
                "git_head": sha,
                "run_id": "current",
                "collectors": {"python": {"ruff": {"valid": True}}},
            }
        )
    )
    module = adapter()
    result = module.main(
        [
            "--root",
            str(repository),
            "--head",
            sha,
            "--target",
            "python",
            "--audit-dir",
            str(repository / "audit"),
            "--status",
            str(status_path),
        ]
    )
    assert result == 1
    status = json.loads(status_path.read_text())
    assert status["collectors"]["python"]["ruff"] == {"valid": True}
    record = status["collectors"]["python"]["code_structure"]
    assert not record["valid"] and not record["threshold_failed"]
    assert record["run_id"] == "current" and record["git_head"] == sha
    status["run_id"] = "another"
    status_path.write_text(json.dumps(status))
    with pytest.raises(ValueError, match="different source/run"):
        module.register_status(
            status_path, {}, head=sha, run_id="current", target="python"
        )
    assert json.loads(status_path.read_text()) == status


def test_colocated_tests_and_generated_client_are_classified(repository):
    sha = commit(
        repository,
        {
            "nextjs-frontend/components/view.tsx": "export const view = 1;\n",
            "nextjs-frontend/components/view.test.tsx": "export const test = 1;\n",
            "nextjs-frontend/app/openapi-client/sdk.gen.ts": "export const sdk = 1;\n",
        },
    )
    selected = adapter().policy_for("next")
    selected["signals"] = ["size"]
    report = collect(repository, sha, selected)
    assert report["valid"]
    assert report["head"]["totals"]["production"]["files"] == 1
    assert report["head"]["totals"]["tests"]["files"] == 1
    assert report["head"]["excluded"][0]["reason"] == "policy exclusion"


def test_tool_version_policy_rejects_unapproved_versions(tmp_path):
    with pytest.raises(ValueError, match="unsupported ruff version"):
        analyzers.tool("ruff", tmp_path, [], expected_version="0.0.0")
