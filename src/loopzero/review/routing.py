"""Configuration-owned routing decisions for review mechanisms."""

from __future__ import annotations

import re
from collections.abc import Mapping
from contextvars import ContextVar
from dataclasses import dataclass, field
from pathlib import Path

from ..config import EFFORTS, Profile
from ..kernel.gitscope import DispatchError
from ..runners.contract import ModelResultReason, RuntimeCommercialMode, RuntimeResult


@dataclass(frozen=True)
class RoutingSettings:
    aliases: Mapping[str, object]
    tiers: Mapping[str, object]
    budgets: Mapping[str, float]
    policy_version: str
    telemetry_schema_version: str
    compatible_policy_versions: tuple[str, ...]
    default_timeout_s: int
    engine_cooldown_s: int
    audit_root: Path
    env_prefix: str

    @classmethod
    def from_profile(cls, profile: Profile) -> "RoutingSettings":
        return cls(
            aliases=profile.aliases,
            tiers=profile.tiers,
            budgets=profile.routing_budgets,
            policy_version=profile.routing_policy_version,
            telemetry_schema_version=profile.telemetry_schema_version,
            compatible_policy_versions=profile.compatible_policy_versions,
            default_timeout_s=profile.default_timeout_s,
            engine_cooldown_s=profile.engine_cooldown_s,
            audit_root=profile.audit_root,
            env_prefix=profile.env_prefix,
        )


_SETTINGS: ContextVar[RoutingSettings | None] = ContextVar("routing_settings", default=None)


def configure(profile: Profile) -> None:
    _SETTINGS.set(RoutingSettings.from_profile(profile))


def settings() -> RoutingSettings:
    value = _SETTINGS.get()
    if value is None:
        raise DispatchError("review routing requires a configured Profile")
    return value


@dataclass(frozen=True)
class RoutingDecision:
    alias: str
    effort: str
    engine: str
    model: str
    backup_engine: str | None
    rationale: str


@dataclass(frozen=True)
class CommandResult:
    returncode: int
    stdout: str = field(repr=False)
    stderr: str = field(repr=False)
    duration_s: float = 0.0
    runtime: RuntimeResult | None = None
    transport_failure_class: str | None = None

    @property
    def timed_out(self) -> bool:
        return self.returncode == 124

    @property
    def output_limited(self) -> bool:
        return False

    @property
    def progress_diagnostic(self) -> bool:
        return False


@dataclass(frozen=True)
class EnginePreflight:
    engine: str
    binary: str
    ok: bool
    failure_class: str | None
    returncode: int | None
    duration_s: float
    commercial_mode: RuntimeCommercialMode = RuntimeCommercialMode.SUBSCRIPTION_ONLY
    transport: str | None = None
    credential_issue: str | None = None


class AttemptAlreadySettledError(DispatchError):
    pass


class GovernedResultValidationError(DispatchError):
    def __init__(self, message: str, reason: ModelResultReason) -> None:
        super().__init__(message)
        self.reason = reason


def resolve_tier_default(tier: str) -> tuple[str, str]:
    configured = settings().tiers
    if tier not in configured:
        raise DispatchError(f"unknown tier {tier!r}; known: {', '.join(sorted(configured))}")
    value = configured[tier]
    return value.alias, value.effort


def route(alias: str, effort: str) -> RoutingDecision:
    configured = settings().aliases
    if alias not in configured:
        raise DispatchError(f"unknown model alias {alias!r}; known: {', '.join(sorted(configured))}")
    if effort not in EFFORTS:
        raise DispatchError(f"unknown effort {effort!r}; known: {', '.join(EFFORTS)}")
    value = configured[alias]
    return RoutingDecision(
        alias=alias,
        effort=effort,
        engine=value.runner,
        model=value.model,
        backup_engine=None,
        rationale=f"configured alias {alias} uses runner {value.runner}",
    )


def effective_attempt_alias(alias: str, engine: str) -> str:
    decision = route(alias, resolve_alias_effort(alias))
    if decision.engine != engine:
        raise DispatchError(f"runner {engine!r} is not configured for alias {alias!r}")
    return alias


def resolve_alias_effort(alias: str) -> str:
    matches = [tier.effort for tier in settings().tiers.values() if tier.alias == alias]
    return matches[0] if matches else "medium"


def model_verifier_aliases() -> tuple[str, ...]:
    return tuple(settings().aliases)


def has_opaque_auto_effective_identity(record: Mapping[str, object]) -> bool:
    alias = record.get("effective_alias")
    try:
        configured = settings().aliases[str(alias)]
    except (KeyError, DispatchError):
        return False
    runtime_model = record.get("runtime_effective_model")
    return bool(
        configured.model == "auto"
        and record.get("engine") == configured.runner
        and isinstance(runtime_model, str)
        and "auto" in runtime_model.casefold()
    )


def supersession_reason_matches_terminal(
    supersession: Mapping[str, object], deposit: Mapping[str, object]
) -> bool:
    reason = supersession.get("supersession_reason")
    return reason == "stale-source" or (
        reason == "failed-review"
        and supersession.get("failure_class") == "failed-review"
        and deposit.get("work_kind") == "review"
        and deposit.get("read_only") is True
        and supersession.get("superseding_source_identity") == deposit.get("source_identity")
    ) or (
        reason == "identity-unverifiable"
        and supersession.get("failure_class") == "identity-unverifiable"
        and deposit.get("work_kind") == "review"
        and deposit.get("read_only") is True
        and deposit.get("status") == "completed"
        and has_opaque_auto_effective_identity(deposit)
        and supersession.get("superseding_source_identity") == deposit.get("source_identity")
        and isinstance(supersession.get("replacement_alias"), str)
        and supersession.get("replacement_alias") not in {"", "auto"}
        and isinstance(supersession.get("replacement_effort"), str)
    )


def verifier_identity_is_independent(
    *, worker_identity: object, worker_alias: object, worker_model: object,
    verifier_identity: object, verifier_alias: object,
) -> bool:
    if not isinstance(verifier_identity, str) or not verifier_identity.strip():
        return False
    if verifier_identity == worker_identity or verifier_alias == worker_alias:
        return False
    if verifier_alias == "human":
        return True
    if not isinstance(verifier_alias, str) or verifier_alias not in settings().aliases:
        return False
    verifier_model = settings().aliases[verifier_alias].model
    actual_worker_model = worker_model if isinstance(worker_model, str) else ""
    if not actual_worker_model and isinstance(worker_identity, str):
        actual_worker_model = worker_identity.partition(":")[2]
    return bool(actual_worker_model and verifier_model.casefold() != actual_worker_model.casefold())


REVIEW_INTENTS = (
    "discovery", "resolution-adjudication", "trust-manifest-verification", "delivery-code-review"
)
MODEL_VERIFIED_REVIEW_INTENTS = frozenset(REVIEW_INTENTS)
REVIEW_LENSES = ("code", "security")
NON_MODEL_VERIFIER_ALIASES = ("human",)
SKILL_RUN_ID_RE = re.compile(r"^sr_[0-9a-f]{32}$")
RUNTIME_CONTRACT_VERSION = 4


def __getattr__(name: str):
    configured = settings()
    values = {
        "ROUTING": configured.aliases,
        "TIER_DEFAULTS": configured.tiers,
        "MODEL_VERIFIER_ALIASES": model_verifier_aliases(),
        "DEFAULT_TIMEOUT_S": configured.default_timeout_s,
        "DEFAULT_MEDIUM_BUDGET_USD": configured.budgets.get("medium", 0.0),
        "DEFAULT_HIGH_BUDGET_USD": configured.budgets.get("high", 0.0),
        "TELEMETRY_SCHEMA_VERSION": configured.telemetry_schema_version,
        "DISPATCH_POLICY_VERSION": configured.policy_version,
        "COMPATIBLE_DISPATCH_POLICY_VERSIONS": frozenset(configured.compatible_policy_versions),
        "ENGINE_COOLDOWN_S": configured.engine_cooldown_s,
        "SKILL_RUN_ID_ENV": f"{configured.env_prefix}_SKILL_RUN_ID",
        "RESULT_ARTIFACT_DIR": configured.audit_root / "dispatch" / "results",
    }
    if name in values:
        return values[name]
    raise AttributeError(name)


__all__ = [
    "CommandResult", "EnginePreflight", "RoutingDecision", "RoutingSettings",
    "configure", "effective_attempt_alias", "model_verifier_aliases", "route",
    "resolve_tier_default", "supersession_reason_matches_terminal",
    "verifier_identity_is_independent",
]
