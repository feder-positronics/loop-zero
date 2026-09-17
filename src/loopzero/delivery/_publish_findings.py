"""PR-scoped finding projections; source history is never rewritten here."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import TYPE_CHECKING

sys.path.insert(0, str(Path(__file__).resolve().parent))

from ..review.predecessors import resolved_loopzero_predecessors  # noqa: F401
from ._publish_gate import format_orphan_finding_warning

if TYPE_CHECKING:
    from .publish import PublicationRequest


def _emit_orphan_finding_warning(request: PublicationRequest) -> None:
    warning = format_orphan_finding_warning(
        request.finding_ids, request.known_finding_ids
    )
    if warning:
        print(f"pr-publish: warning: {warning}", file=sys.stderr)
