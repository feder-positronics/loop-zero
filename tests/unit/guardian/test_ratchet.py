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

    result = ratchet.evaluate(
        manifest, claim_group="portable", checker=Checker(), run=run
    )
    assert [row["id"] for row in result["VERIFIED"]] == ["P-1"]
    assert ratchet.select_ticket(result) is None


def test_missing_group_fails_closed(tmp_path: Path):
    manifest = tmp_path / "claims.yaml"
    manifest.write_text("different: []\n", encoding="utf-8")
    result = ratchet.evaluate(manifest, claim_group="portable", checker=Checker())
    assert result["UNSUPPORTED"][0]["id"] == "MANIFEST"
