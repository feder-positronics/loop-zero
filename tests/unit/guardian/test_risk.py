from pathlib import PurePosixPath

from loopzero.guardian import risk


def test_reserved_paths_and_sensitive_content_remain_blocking():
    assert risk.reserved_risk_path(PurePosixPath("src/auth/token.py"))
    assert risk.classify_candidate(
        "src/worker.py", "import subprocess\nsubprocess.run(['sh'])\n"
    ) is not None


def test_plain_data_transform_is_not_reserved():
    assert risk.classify_candidate(
        "src/formatting.py", "def upper(value: str) -> str:\n    return value.upper()\n"
    ) is None
