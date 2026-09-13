"""PR-scoped finding projections; source history is never rewritten here."""

from __future__ import annotations

import sys
from pathlib import Path
from collections.abc import Mapping
from typing import TYPE_CHECKING

sys.path.insert(0, str(Path(__file__).resolve().parent))

from ..kernel.run_log import load_entries
from ..kernel.run_identity import run_delivery_contract
from ._publish_gate import format_orphan_finding_warning

if TYPE_CHECKING:
    from .publish import PublicationRequest


def resolved_loopzero_predecessors(
    repo: Path,
    *,
    terminals: Mapping[str, dict[str, object]],
    current_review_task_id: str | None,
    verdicts: Mapping[str, dict[str, object]],
) -> set[str]:
    """A passing same-chain delta replaces manual closure of its predecessor."""
    resolved_predecessors: set[str] = set()
    current = terminals.get(current_review_task_id or "")
    if current is not None and isinstance(current.get("run_id"), str):
        rows = load_entries(repo / ".audit" / "skill-runs")
        run_id = str(current["run_id"])
        if (
            any(row.get("run_id") == run_id for row in rows)
            and run_delivery_contract(rows, run_id) == "loop-zero-v1"
        ):
            contract = current.get("task_contract")
            verdict = verdicts.get(current_review_task_id or "", {})
            if isinstance(contract, Mapping) and verdict.get("verdict") == "pass":
                predecessor = contract.get("delta_from_snapshot")
                # The accepted delta reviews all original findings. Its current
                # material findings block below; the old result remains raw
                # history, without a second finding-closure workflow.
                for prior_id, prior in terminals.items():
                    prior_contract = prior.get("task_contract")
                    if (
                        isinstance(predecessor, str)
                        and prior.get("snapshot_sha") == predecessor
                        and prior.get("run_id") == run_id
                        and isinstance(prior_contract, Mapping)
                        and prior_contract.get("review_chain_id")
                        == contract.get("review_chain_id")
                        and contract.get("review_chain_id")
                    ):
                        resolved_predecessors.add(prior_id)
    return resolved_predecessors


def _emit_orphan_finding_warning(request: PublicationRequest) -> None:
    warning = format_orphan_finding_warning(
        request.finding_ids, request.known_finding_ids
    )
    if warning:
        print(f"pr-publish: warning: {warning}", file=sys.stderr)
