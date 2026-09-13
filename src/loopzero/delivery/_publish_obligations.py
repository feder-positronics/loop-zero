"""Publication-time mirror of the trusted obligation-change policy."""

from __future__ import annotations

from ._obligations import ObligationCheckError, check_obligation_changes
from ._publish_paths import PublicationError


def require_obligation_acknowledgment(runner, base: str, head: str, body: str) -> None:
    try:
        missing_acknowledgment = check_obligation_changes(
            base, head, acknowledgment_text=body, runner=runner
        )
    except ObligationCheckError as exc:
        raise PublicationError(f"cannot evaluate obligation changes: {exc}") from exc
    if missing_acknowledgment:
        raise PublicationError(
            "agent-config obligation changes require a substantive acknowledgment"
        )
