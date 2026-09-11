"""Deterministic claim-ratchet engine extracted from the Guardian tick."""

from __future__ import annotations

import math
import subprocess
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any, Protocol

from . import state


class ClaimChecker(Protocol):
    root: Path

    def claim_status(self, claim: Mapping[str, Any]) -> tuple[str, str]: ...


STATUSES = ("VERIFIED", "KNOWN-GAP", "VIOLATED", "FIXED?!", "UNSUPPORTED")


def _policy_error(claim: Mapping[str, Any]) -> str | None:
    if claim.get("type") not in {"metric_max", "predicate"}:
        return "claim type must be metric_max or predicate"
    for field in ("id", "claim", "command"):
        if not isinstance(claim.get(field), str) or not claim[field].strip():
            return f"claim requires non-empty {field}"
    repair_scope = claim.get("repair_scope")
    if not isinstance(repair_scope, list) or any(
        not isinstance(path, str) or not path for path in repair_scope
    ):
        return "claim requires non-empty string repair_scope entries"
    if claim.get("expected", "verified") not in {"verified", "known-gap"}:
        return "claim expected must be verified or known-gap"
    if claim.get("type") == "metric_max":
        for field in ("threshold", "headroom"):
            value = claim.get(field)
            if type(value) not in (int, float) or not math.isfinite(float(value)):
                return f"metric claim requires finite {field}"
    return None


def _metric_status(
    claim: Mapping[str, Any], *, root: Path,
    run: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> tuple[str, str, float | None]:
    try:
        result = run(
            str(claim["command"]), shell=True, cwd=root, capture_output=True,
            text=True, timeout=120,
        )
        if result.returncode:
            raise ValueError(f"metric command failed ({result.returncode}): {result.stderr.strip()[:200]}")
        lines = [line for line in result.stdout.splitlines() if line.strip()]
        if not lines:
            raise ValueError("metric command produced no output")
        value = float(lines[-1])
        threshold = float(claim["threshold"])
        if not math.isfinite(value):
            raise ValueError("metric value must be finite")
    except (KeyError, ValueError, OSError, subprocess.TimeoutExpired) as exc:
        return "UNSUPPORTED", str(exc), None
    holds = value <= threshold
    expected = claim.get("expected", "verified")
    if holds and expected == "verified":
        return "VERIFIED", "", value
    if not holds and expected == "known-gap":
        return "KNOWN-GAP", f"metric {value:.3f} exceeds threshold {threshold:.3f}", value
    if holds:
        return "FIXED?!", "gap appears closed", value
    return "VIOLATED", f"metric {value:.3f} exceeds threshold {threshold:.3f}", value


def evaluate(
    manifest_path: Path,
    *,
    claim_group: str,
    checker: ClaimChecker,
    run: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> dict[str, list[dict[str, Any]]]:
    """Evaluate one configured manifest group in manifest order.

    PyYAML is imported only when this optional mechanism is called.
    """
    try:
        import yaml
    except ImportError as exc:  # pragma: no cover - depends on optional install
        raise RuntimeError("Guardian ratchets require loopzero[guardian]") from exc

    grouped = {status: [] for status in STATUSES}
    try:
        loaded = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
        if not isinstance(loaded, dict):
            raise ValueError("manifest must be a mapping")
        claims = loaded.get(claim_group)
        if not isinstance(claims, list) or not claims:
            raise ValueError(f"manifest contains no {claim_group!r} claims")
        ids = [claim.get("id") for claim in claims if isinstance(claim, dict)]
        if len(ids) != len(set(ids)) or any(not isinstance(value, str) for value in ids):
            raise ValueError("claim ids must be unique strings")
    except (OSError, ValueError, yaml.YAMLError) as exc:
        grouped["UNSUPPORTED"].append({"id": "MANIFEST", "detail": str(exc), "priority": 0})
        return grouped

    for priority, claim in enumerate(claims):
        if not isinstance(claim, dict):
            grouped["UNSUPPORTED"].append({"id": "?", "detail": "claim must be a mapping", "priority": priority})
            continue
        problem = _policy_error(claim)
        if problem:
            status, detail, value = "UNSUPPORTED", problem, None
        elif claim["type"] == "metric_max":
            status, detail, value = _metric_status(claim, root=checker.root, run=run)
        else:
            status, detail = checker.claim_status(claim)
            value = None
        entry = dict(claim)
        entry.update(
            priority=priority,
            detail=detail,
            value=value,
            command_digest=state.command_digest(str(claim["command"])),
            policy_digest=state.policy_digest(claim),
        )
        grouped[status].append(entry)
    return grouped


def select_ticket(grouped: Mapping[str, list[dict[str, Any]]]) -> dict[str, Any] | None:
    """Return the first violated claim, preserving manifest priority."""
    violated = grouped.get("VIOLATED", [])
    return min(violated, key=lambda item: int(item["priority"])) if violated else None


__all__ = ["ClaimChecker", "STATUSES", "evaluate", "select_ticket"]
