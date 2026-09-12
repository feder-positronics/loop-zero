import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from loopzero.guardian import ratchet, state


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
    return ratchet.evaluate_claims(
        path, claim_group="quality", checker=Checker(), run=run
    )


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


# IntelFlo output-contract ports (scripts/util/guardian_tick.py
# ``_manifest_failure`` and the fixed evaluated-record field set).

SENTINEL_RECORD = {
    "id": "GUARDIAN-MANIFEST",
    "claim": "Guardian quality manifest is readable and structurally valid",
    "command": "guardian-manifest-validation",
    "threshold": None,
    "headroom": None,
    "policy_headroom": None,
    "repair_scope": [],
    "acceptance_command": None,
    "candidate_command": None,
    "value": None,
    "priority": 0,
}


def _assert_manifest_sentinel(record: dict, detail: str) -> None:
    assert set(record) == set(ratchet.RECORD_FIELDS)
    assert {key: record[key] for key in SENTINEL_RECORD} == SENTINEL_RECORD
    assert record["detail"] == detail
    assert record["command_digest"] == state.command_digest(
        "guardian-manifest-validation"
    )
    assert record["policy_digest"] == state.policy_digest(
        ratchet.MANIFEST_SENTINEL_POLICY
    )


def test_non_mapping_claim_is_the_full_manifest_sentinel(tmp_path: Path):
    def run(*args, **kwargs):
        return SimpleNamespace(returncode=0, stdout="1\n", stderr="")

    grouped = evaluate(write_manifest(tmp_path, "  - just-a-string\n" + VALID), run)
    assert len(grouped["UNSUPPORTED"]) == 1
    _assert_manifest_sentinel(
        grouped["UNSUPPORTED"][0], "quality claim 0 must be a mapping"
    )
    # The following well-formed claim is still evaluated in manifest order.
    assert [row["id"] for row in grouped["VERIFIED"]] == ["QT-one"]


@pytest.mark.parametrize(
    ("body", "detail"),
    (
        ("quality: []\n", "manifest contains no 'quality' claims"),
        ("- not-a-mapping\n", "manifest must be a mapping"),
    ),
)
def test_empty_or_malformed_manifest_is_the_full_manifest_sentinel(
    tmp_path: Path, body: str, detail: str
):
    path = tmp_path / "manifest.yaml"
    path.write_text(body, encoding="utf-8")
    grouped = evaluate(path)
    assert [row["id"] for row in grouped["UNSUPPORTED"]] == ["GUARDIAN-MANIFEST"]
    _assert_manifest_sentinel(grouped["UNSUPPORTED"][0], detail)


def test_records_carry_only_the_fixed_field_set(tmp_path: Path):
    extra = VALID + "    tracked: '#998'\n    surprise: leaks\n"

    def run(*args, **kwargs):
        return SimpleNamespace(returncode=0, stdout="1\n", stderr="")

    grouped = evaluate(write_manifest(tmp_path, extra), run)
    (record,) = grouped["VERIFIED"]
    assert set(record) == set(ratchet.RECORD_FIELDS)
    assert record["id"] == "QT-one"
    assert record["threshold"] == 5
    assert record["policy_headroom"] == 2
    assert record["repair_scope"] == ["src/**"]
    assert record["acceptance_command"] == "pytest guardian"
    assert record["candidate_command"] == "printf src"
    assert record["command_digest"] == state.command_digest("echo 1")
    # The policy digest still covers every declared manifest input.
    assert record["policy_digest"] != state.policy_digest(
        {key: value for key, value in record.items() if key in SENTINEL_RECORD}
    )


def test_empty_command_yields_no_command_digest(tmp_path: Path):
    grouped = evaluate(write_manifest(tmp_path, VALID.replace("echo 1", "''")))
    (record,) = grouped["UNSUPPORTED"]
    assert record["id"] == "QT-one"
    assert record["detail"] == "quality claim requires non-empty command"
    assert record["command"] == ""
    assert record["command_digest"] is None
    assert set(record) == set(ratchet.RECORD_FIELDS)
