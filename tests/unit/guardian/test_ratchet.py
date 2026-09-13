from pathlib import Path
from types import SimpleNamespace

from loopzero.guardian import ratchet


class Checker:
    root = Path("/")

    def claim_status(self, claim):
        return ("VERIFIED", "")


def test_evaluate_uses_configured_manifest_group(tmp_path: Path):
    manifest = tmp_path / "claims.yaml"
    manifest.write_text(
        """portable:\n  - id: P-1\n    claim: metric stays bounded\n    type: metric_max\n    command: measure\n    threshold: 2\n    headroom: 1\n    repair_scope: [src/**]\n    acceptance_command: pytest guardian\n    candidate_command: printf src\n""",
        encoding="utf-8",
    )

    def run(*args, **kwargs):
        return SimpleNamespace(returncode=0, stdout="1\n", stderr="")

    result = ratchet.evaluate_claims(
        manifest, claim_group="portable", checker=Checker(), run=run
    )
    assert [row["id"] for row in result["VERIFIED"]] == ["P-1"]
    assert result["VERIFIED"][0]["headroom"] == 1.0
    assert result["VERIFIED"][0]["policy_headroom"] == 1
    assert ratchet.select_ticket(result) is None


def test_missing_group_fails_closed(tmp_path: Path):
    manifest = tmp_path / "claims.yaml"
    manifest.write_text("different: []\n", encoding="utf-8")
    result = ratchet.evaluate_claims(
        manifest, claim_group="portable", checker=Checker()
    )
    assert result["UNSUPPORTED"][0]["id"] == "GUARDIAN-MANIFEST"


def test_evaluate_preserves_policy_headroom_separately_from_measurement(
    tmp_path: Path,
):
    manifest = tmp_path / "claims.yaml"
    manifest.write_text(
        """portable:\n  - id: P-1\n    claim: metric stays bounded\n    type: metric_max\n    command: measure\n    threshold: 5\n    headroom: 2\n    repair_scope: [src/**]\n    acceptance_command: pytest guardian\n    candidate_command: printf src\n""",
        encoding="utf-8",
    )

    def run(*args, **kwargs):
        return SimpleNamespace(returncode=0, stdout="1\n", stderr="")

    result = ratchet.evaluate_claims(
        manifest, claim_group="portable", checker=Checker(), run=run
    )
    entry = result["VERIFIED"][0]
    assert entry["value"] == 1.0
    assert entry["threshold"] == 5
    assert entry["headroom"] == 4.0
    assert entry["policy_headroom"] == 2


def test_evaluate_compatibility_wrapper_uses_configured_profile_runner(tmp_path):
    manifest = tmp_path / "claims.yaml"
    manifest.write_text(
        """quality:\n  - id: Q-1\n    claim: metric stays bounded\n    type: metric_max\n    command: measure\n    threshold: 3\n    headroom: 1\n    repair_scope: [src/**]\n    acceptance_command: pytest guardian\n    candidate_command: printf src\n""",
        encoding="utf-8",
    )
    checker = SimpleNamespace(
        root=tmp_path,
        claim_status=lambda _claim: ("VERIFIED", ""),
    )
    calls = []

    def run(*args, **kwargs):
        calls.append((args, kwargs))
        return SimpleNamespace(returncode=0, stdout="1\n", stderr="")

    ratchet.configure(SimpleNamespace(root=tmp_path), checker=checker, run=run)

    result = ratchet.evaluate(manifest)

    assert result["VERIFIED"][0]["id"] == "Q-1"
    assert result["VERIFIED"][0]["headroom"] == 2.0
    assert calls[0][1]["cwd"] == tmp_path
