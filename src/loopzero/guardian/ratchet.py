"""Deterministic claim-ratchet engine extracted from the Guardian tick."""

from __future__ import annotations

import math
import subprocess
from collections.abc import Callable, Mapping
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from . import state


class ClaimChecker(Protocol):
    root: Path

    def claim_status(self, claim: Mapping[str, Any]) -> tuple[str, str]: ...


STATUSES = ("VERIFIED", "KNOWN-GAP", "VIOLATED", "FIXED?!", "UNSUPPORTED")


@dataclass(frozen=True)
class RatchetComposition:
    """Consumer-bound dependencies for the IntelFlo-compatible wrapper."""

    root: Path
    checker: ClaimChecker
    run: Callable[..., object]
    claim_group: str = "quality"


_COMPOSITION: ContextVar[RatchetComposition | None] = ContextVar(
    "guardian_ratchet_composition", default=None
)


def configure(
    profile: Any,
    *,
    checker: ClaimChecker,
    run: Callable[..., object],
    claim_group: str = "quality",
) -> None:
    """Bind the consumer profile and its sandboxed metric runner."""
    root = Path(profile.root).resolve()
    if Path(checker.root).resolve() != root:
        raise ValueError("Guardian checker root must match the configured profile")
    if not callable(run):
        raise TypeError("Guardian metric runner must be callable")
    if not isinstance(claim_group, str) or not claim_group.strip():
        raise ValueError("Guardian claim group must be non-empty")
    _COMPOSITION.set(RatchetComposition(root, checker, run, claim_group.strip()))


def _policy_error(claim: Mapping[str, Any]) -> str | None:
    if claim.get("type") != "metric_max":
        return "quality claim type must be metric_max"
    for field in (
        "id",
        "claim",
        "command",
        "acceptance_command",
        "candidate_command",
    ):
        if not isinstance(claim.get(field), str) or not claim[field].strip():
            return f"quality claim requires non-empty {field}"
    repair_scope = claim.get("repair_scope")
    if (
        not isinstance(repair_scope, list)
        or not repair_scope
        or not all(isinstance(path, str) and path for path in repair_scope)
    ):
        return "quality claim requires non-empty repair_scope"
    if claim.get("expected", "verified") not in {"verified", "known-gap"}:
        return "quality claim expected must be verified or known-gap"
    for field in ("threshold", "headroom"):
        value = claim.get(field)
        if type(value) not in (int, float) or not math.isfinite(float(value)):
            return f"quality claim requires finite {field}"
    return None


def _metric_status(
    claim: Mapping[str, Any], *, root: Path,
    run: Callable[..., object] | None,
) -> tuple[str, str, float | None]:
    if run is None:
        return "UNSUPPORTED", "metric claims require an injected sandboxed runner", None
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
    except (KeyError, ValueError, OSError, TimeoutError, subprocess.TimeoutExpired) as exc:
        return "UNSUPPORTED", str(exc), None
    holds = value <= threshold
    expected = claim.get("expected", "verified")
    if holds and expected == "verified":
        return "VERIFIED", "", value
    if not holds and expected == "known-gap":
        return "KNOWN-GAP", f"metric {value:.3f} exceeds threshold {threshold:.3f}", value
    if holds:
        return (
            "FIXED?!",
            "gap appears closed — flip `expected: verified` and close the tracking note",
            value,
        )
    return "VIOLATED", f"metric {value:.3f} exceeds threshold {threshold:.3f}", value


def evaluate_claims(
    manifest_path: Path,
    *,
    claim_group: str,
    checker: ClaimChecker,
    run: Callable[..., object] | None = None,
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
        grouped["UNSUPPORTED"].append(
            {"id": "GUARDIAN-MANIFEST", "detail": str(exc), "priority": 0}
        )
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
        threshold = claim.get("threshold")
        try:
            measured_headroom = float(threshold) - value if value is not None else None
        except (TypeError, ValueError):
            measured_headroom = None
        entry = dict(claim)
        entry.update(
            priority=priority,
            detail=detail,
            value=value,
            headroom=measured_headroom,
            policy_headroom=claim.get("headroom"),
            command_digest=state.command_digest(str(claim["command"])),
            policy_digest=state.policy_digest(claim),
        )
        grouped[status].append(entry)
    return grouped


def evaluate(manifest_path: Path) -> dict[str, list[dict[str, Any]]]:
    """Evaluate IntelFlo's quality group through configured trusted seams."""
    composition = _COMPOSITION.get()
    if composition is None:
        raise RuntimeError("Guardian ratchet is not configured")
    return evaluate_claims(
        manifest_path,
        claim_group=composition.claim_group,
        checker=composition.checker,
        run=composition.run,
    )


def select_ticket(grouped: Mapping[str, list[dict[str, Any]]]) -> dict[str, Any] | None:
    """Return the first violated claim, preserving manifest priority."""
    violated = grouped.get("VIOLATED", [])
    return min(violated, key=lambda item: int(item["priority"])) if violated else None


__all__ = [
    "ClaimChecker",
    "RatchetComposition",
    "STATUSES",
    "configure",
    "evaluate",
    "evaluate_claims",
    "select_ticket",
]
