from pathlib import Path
from types import SimpleNamespace

import pytest

from loopzero.delivery._publish_obligations import (
    PublicationError,
    require_obligation_acknowledgment,
)

MERGE_BASE = "c" * 40


class ObligationRunner:
    def run(self, command, **_kwargs):
        if command[:2] == ["git", "merge-base"]:
            output = MERGE_BASE + "\n"
        elif command[:4] == ["git", "diff", "--name-status", "-M"]:
            output = "M\0.cursor/rules/example.mdc\0"
        elif command[:2] == ["git", "show"] and command[2].startswith(f"{MERGE_BASE}:"):
            output = "- Always run the focused test.\n"
        elif command[:2] == ["git", "show"] and command[2].startswith("head:"):
            output = "- Run the focused test.\n"
        else:
            raise AssertionError(f"unexpected command: {command}")
        return SimpleNamespace(returncode=0, stdout=output, stderr="")


@pytest.mark.parametrize(
    ("body", "accepted"),
    [
        pytest.param(
            "Obligation-change: The weaker wording matches the bounded contract.",
            True,
            id="visible",
        ),
        pytest.param(
            "<!--\n"
            "Obligation-change: The weaker wording matches the bounded contract.\n"
            "-->",
            False,
            id="html-comment",
        ),
        pytest.param(
            "```text\n"
            "Obligation-change: The weaker wording matches the bounded contract.\n"
            "```",
            False,
            id="fenced-code",
        ),
        pytest.param("", False, id="missing"),
        pytest.param(
            "Obligation-change: <why this changed>",
            False,
            id="placeholder",
        ),
    ],
)
def test_publisher_accepts_visible_reason_and_rejects_hidden_or_invalid_reason(
    body: str, accepted: bool, capsys: pytest.CaptureFixture[str]
) -> None:
    if accepted:
        require_obligation_acknowledgment(ObligationRunner(), "base", "head", body)
        assert "ACKNOWLEDGED" in capsys.readouterr().out
    else:
        with pytest.raises(PublicationError, match="obligation"):
            require_obligation_acknowledgment(ObligationRunner(), "base", "head", body)
        assert "Obligation-change:" in capsys.readouterr().out


def test_publisher_converts_obligation_evaluator_errors_to_publication_errors() -> None:
    class InvalidMergeBaseRunner:
        def run(self, command, **_kwargs):
            assert command[:2] == ["git", "merge-base"]
            return SimpleNamespace(returncode=0, stdout="not-a-commit\n", stderr="")

    with pytest.raises(PublicationError, match="cannot evaluate obligation changes"):
        require_obligation_acknowledgment(
            InvalidMergeBaseRunner(), "base", "head", "Obligation-change: reason"
        )
