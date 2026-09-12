"""Path classification and Git tree checks for PR publication."""

import re
from pathlib import Path
from typing import Protocol

from ..integrations.github import GateError


class PublicationError(RuntimeError, ValueError):
    """Raised when a PR cannot be published without avoidable metadata fanout."""


class CommandOutcome(Protocol):
    returncode: int
    stdout: str
    stderr: str


class CommandRunner(Protocol):
    def run(self, args: list[str], *, check: bool = True) -> CommandOutcome: ...


def require_local_publication_prerequisites(
    *,
    worktree: Path,
    head: str,
    expected_head: str,
    runner: CommandRunner,
) -> None:
    """Fail before authority reads unless this checkout is publishable as named."""
    if re.fullmatch(r"[0-9a-f]{40}", expected_head) is None:
        raise PublicationError("expected head must be exactly 40 lowercase hex")
    try:
        local_head = runner.run(["git", "rev-parse", "HEAD"]).stdout.strip()
        symbolic = runner.run(
            ["git", "symbolic-ref", "--quiet", "--short", "HEAD"], check=False
        )
        if symbolic.returncode != 0:
            raise PublicationError(
                f"HEAD is detached at {local_head}; run the publisher from the "
                f"delivery worktree on branch {head}"
            )
        branch = symbolic.stdout.strip()
        if local_head != expected_head:
            raise PublicationError("local HEAD does not match expected head")
        if branch != head:
            raise PublicationError("checked-out branch does not match publication head")
        dirty = runner.run(["git", "status", "--porcelain"]).stdout.strip()
    except GateError as exc:
        raise PublicationError(
            f"cannot determine local publication prerequisites: {exc}"
        ) from exc
    if dirty:
        raise PublicationError("publication requires a clean worktree")


def changed_paths_between(
    runner: CommandRunner,
    base_ref: str,
    head_ref: str,
) -> tuple[str, ...]:
    """Return both sides of renames so a docs destination cannot hide its source."""

    return tuple(
        path
        for path in runner.run(
            [
                "git",
                "diff",
                "--name-only",
                "-z",
                "--no-renames",
                f"{base_ref}..{head_ref}",
            ]
        ).stdout.split("\0")
        if path
    )
