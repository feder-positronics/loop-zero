"""Bounded alternate-runtime review through the runner registry."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..config import Profile
from ..runners.contract import (
    RuntimeCapabilityProfile, RuntimeCommercialMode, RuntimeRequest, RuntimeResult,
    RuntimeStatus, SubscriptionEligibility,
)
from ..runners.registry import RUNTIME_REGISTRY, RuntimeRegistry
from ..runners.settings import RuntimeSettings
from . import routing
from .evidence import _validate_result_findings


class CrossHarnessError(RuntimeError):
    """The advisory review could not be prepared or completed safely."""


@dataclass(frozen=True)
class AdvisoryResult:
    findings: tuple[dict[str, object], ...]
    finding_count: int
    max_severity: str | None
    runtime: RuntimeResult


_SEVERITY = {"suggestion": 0, "important": 1, "critical": 2}


def _result_payload(result: RuntimeResult) -> Mapping[str, object]:
    if result.structured_output is not None:
        return result.structured_output
    if result.final_output is None:
        raise CrossHarnessError("review runtime returned no structured result")
    try:
        payload = json.loads(result.final_output)
    except json.JSONDecodeError as exc:
        raise CrossHarnessError("review runtime result is not JSON") from exc
    if not isinstance(payload, Mapping):
        raise CrossHarnessError("review runtime result must be an object")
    return payload


def run_review(
    profile: Profile,
    *,
    worktree: Path,
    alias: str,
    effort: str,
    prompt: str,
    attempt_id: str,
    registry: RuntimeRegistry = RUNTIME_REGISTRY,
    adapter_options: Mapping[str, Any] | None = None,
    output_schema: dict[str, object] | None = None,
) -> AdvisoryResult:
    """Route and run one read-only advisory review without constructing CLI argv."""
    routing.configure(profile)
    decision = routing.route(alias, effort)
    try:
        registration = registry.registration(decision.engine)
        adapter = registry.create(
            decision.engine,
            settings=RuntimeSettings.from_profile(profile),
            **dict(adapter_options or {}),
        )
    except (TypeError, ValueError) as exc:
        raise CrossHarnessError(f"configured review runtime is unavailable: {exc}") from exc
    request = RuntimeRequest(
        vendor=decision.engine,  # type: ignore[arg-type]
        transport=registration.preferred_transport,
        requested_model=decision.model,
        effort=effort,
        prompt=prompt,
        cwd=worktree.resolve(),
        timeout_s=float(profile.default_timeout_s),
        read_only=True,
        attempt_id=attempt_id,
        tooling_root=profile.root.resolve(),
        eligibility=SubscriptionEligibility.APPROVED,
        commercial_mode=RuntimeCommercialMode.SUBSCRIPTION_ONLY,
        budget_usd=profile.routing_budgets.get(effort),
        output_schema=output_schema,
        capability_profile=RuntimeCapabilityProfile(read_roots=(worktree.resolve(),)),
        visible_tools=(),
        allowed_tools=(),
    )
    readiness = adapter.probe(request)
    if not readiness.ready:
        reason = readiness.failure.value if readiness.failure is not None else "unavailable"
        raise CrossHarnessError(f"review runtime is not ready: {reason}")
    result = adapter.run(request)
    if result.status is not RuntimeStatus.COMPLETED:
        raise CrossHarnessError(f"review runtime did not complete: {result.terminal_reason.value}")
    findings = _validate_result_findings(_result_payload(result).get("findings"))
    maximum = max((str(row["severity"]) for row in findings), key=_SEVERITY.get, default=None)
    return AdvisoryResult(tuple(findings), len(findings), maximum, result)


__all__ = ["AdvisoryResult", "CrossHarnessError", "run_review"]
