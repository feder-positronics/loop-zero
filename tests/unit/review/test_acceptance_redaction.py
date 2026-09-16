"""Output redaction must not strand a validator after its child completes."""

import os
import subprocess
import sys

import pytest

from loopzero.review.acceptance import redact_acceptance_output


def test_large_collection_output_returns_after_child_completion() -> None:
    # The timeout surrounds the parent, including post-child redaction. A child
    # timeout alone cannot detect the stall reported in #69.
    script = """
import subprocess
import sys
from loopzero.review.acceptance import redact_acceptance_output
child = subprocess.run(
    [sys.executable, '-c', "print('tests/unit/test_' + 'a' * 200_000 + '.py::test_case')"],
    capture_output=True, text=True, timeout=3, check=True,
)
print('child-completed', flush=True)
assert redact_acceptance_output(child.stdout) == child.stdout
# Near matches must also return without leaking credentials on later lines.
for value in (
    'a' * 200_000 + '://user:pass@example.test',
    'https://' + 'a' * 200_000,
    'https://user:' + ':' * 200_000,
    ('scheme://user:pass/' * 20_000),
    ('tests/unit/test_example.py::test_case\\n' * 20_000),
):
    output = redact_acceptance_output(value + '\\nhttps://alice:secret@example.test')
    assert output.endswith('\\nhttps://<redacted>@example.test')
    assert 'alice:secret' not in output
    assert 'user:pass@example.test' not in output
print('redaction-completed', flush=True)
"""
    result = subprocess.run(
        [sys.executable, "-c", script],
        env={**os.environ, "PYTHONPATH": os.pathsep.join(sys.path)},
        capture_output=True,
        text=True,
        timeout=8,
        check=True,
    )
    assert result.stdout.splitlines() == ["child-completed", "redaction-completed"]


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        (
            "https://alice:secret@example.test/path",
            "https://<redacted>@example.test/path",
        ),
        (
            "postgresql+asyncpg://alice:p:a:ss@db/test",
            "postgresql+asyncpg://<redacted>@db/test",
        ),
        ("ssh://alice@example.test", "ssh://<redacted>@example.test"),
        ("https://alice:@example.test", "https://<redacted>@example.test"),
        ("https://a%40b:p%2Fq@example.test", "https://<redacted>@example.test"),
        ("://alice:secret@example.test", "://<redacted>@example.test"),
        ("https://example.test/path", "https://example.test/path"),
        (
            "tests/unit/test_example.py::test_case",
            "tests/unit/test_example.py::test_case",
        ),
        ("password=secret", "password=<redacted>"),
        ("--api-key secret", "--api-key <redacted>"),
        ("Authorization: Bearer secret", "Authorization: Bearer <redacted>"),
    ],
)
def test_supported_secret_forms_and_ordinary_output(source: str, expected: str) -> None:
    assert redact_acceptance_output(source) == expected
