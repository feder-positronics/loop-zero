import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from loopzero.guardian import ratchet


class Checker:
    root = Path("/")

    def claim_status(self, claim):
        raise AssertionError("quality claims must use the metric evaluator")


def write_manifest(tmp_path: Path, entry: str) -> Path:
    path = tmp_path / "manifest.yaml"
    path.write_text("quality:\n" + entry, encoding="utf-8")
    return path


VALID = """
  - id: QT-one
    claim: value stays low
    type: metric_max
    command: echo 1
    threshold: 5
    headroom: 2
    repair_scope: [src/**]
    acceptance_command: pytest guardian
    candidate_command: printf src
    expected: verified
"""


def evaluate(path: Path, run=None):
    return ratchet.evaluate(path, claim_group="quality", checker=Checker(), run=run)


def test_missing_repair_authority_is_broken_before_measurement(tmp_path: Path):
    incomplete = VALID.replace("    repair_scope: [src/**]\n", "")
    grouped = evaluate(write_manifest(tmp_path, incomplete))
    assert "requires non-empty" in grouped["UNSUPPORTED"][0]["detail"]


@pytest.mark.parametrize("field", ["acceptance_command", "candidate_command"])
def test_missing_command_authority_is_broken_before_measurement(tmp_path: Path, field: str):
    incomplete = VALID.replace(f"    {field}: ", f"    removed_{field}: ")
    grouped = evaluate(write_manifest(tmp_path, incomplete))
    assert f"requires non-empty {field}" in grouped["UNSUPPORTED"][0]["detail"]


def test_empty_repair_scope_is_broken_before_measurement(tmp_path: Path):
    grouped = evaluate(write_manifest(tmp_path, VALID.replace("[src/**]", "[]")))
    assert grouped["UNSUPPORTED"][0]["detail"] == "quality claim requires non-empty repair_scope"


def test_subprocess_timeout_is_structured_unsupported(tmp_path: Path):
    def timeout(*args, **kwargs):
        raise subprocess.TimeoutExpired(args[0], kwargs["timeout"])

    grouped = evaluate(write_manifest(tmp_path, VALID), timeout)
    assert grouped["UNSUPPORTED"][0]["id"] == "QT-one"
    assert "timed out after 120 seconds" in grouped["UNSUPPORTED"][0]["detail"]


def test_first_violated_claim_is_the_ticket(tmp_path: Path):
    entries = VALID.replace("QT-one", "QT-first").replace("echo 1", "echo 9")
    entries += VALID.replace("QT-one", "QT-second").replace("echo 1", "echo 9")

    def run(*args, **kwargs):
        return SimpleNamespace(returncode=0, stdout="9\n", stderr="")

    grouped = evaluate(write_manifest(tmp_path, entries), run)
    assert ratchet.select_ticket(grouped)["id"] == "QT-first"
