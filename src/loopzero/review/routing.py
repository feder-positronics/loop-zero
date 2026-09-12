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
    verifier_models: Mapping[str, str]

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
            verifier_models=getattr(profile, "verifier_models", {}),
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
    value = configured[alias]
    known_efforts = tuple(dict.fromkeys(
        effort_name
        for configured_alias in configured.values()
        for effort_name in getattr(
            configured_alias,
            "allowed_efforts",
            ("low", "medium", "high", "xhigh", "max"),
        )
    ))
    if effort not in known_efforts:
        raise DispatchError(
            f"unknown effort {effort!r}; known: {', '.join(known_efforts)}"
        )
    allowed_efforts = tuple(
        getattr(value, "allowed_efforts", ("low", "medium", "high", "xhigh", "max"))
    )
    if effort not in allowed_efforts:
        raise DispatchError(
            f"effort {effort!r} is a banned grade for {alias} "
            f"(cost-ineffective on the benchmark curve); allowed: "
            f"{', '.join(allowed_efforts)}"
        )
    engines = tuple(getattr(value, "engines", ())) or (value.runner,)
    primary = engines[0]
    backup = engines[1] if len(engines) > 1 else None
    return RoutingDecision(
        alias=alias,
        effort=effort,
        engine=primary,
        model=value.model,
        backup_engine=backup,
        rationale=f"{alias} is {getattr(value, 'family', value.runner)}-family: "
        f"primary engine {primary}" + (
            f", backup {backup} (model {getattr(value, 'backup_model', None)})"
            if backup else ""
        ),
    )


def effective_attempt_alias(alias: str, engine: str) -> str:
    configured = settings().aliases
    if alias not in configured:
        raise DispatchError(
            f"unknown model alias {alias!r}; known: {', '.join(sorted(configured))}"
        )
    value = configured[alias]
    engines = tuple(getattr(value, "engines", ())) or (value.runner,)
    if engine not in engines:
        raise DispatchError(f"runner {engine!r} is not configured for alias {alias!r}")
    if engine == "cursor" and value.runner != "cursor":
        return "auto"
    return alias


def resolve_alias_effort(alias: str) -> str:
    try:
        return getattr(settings().aliases[alias], "default_effort", "medium")
    except KeyError:
        raise DispatchError(f"unknown model alias {alias!r}") from None


def model_verifier_aliases() -> tuple[str, ...]:
    configured = settings()
    return (*configured.aliases, *configured.verifier_models)


def has_opaque_auto_effective_identity(record: Mapping[str, object]) -> bool:
    runtime_model = record.get("runtime_effective_model")
    normalized_runtime = (
        "-".join(runtime_model.casefold().split())
        if isinstance(runtime_model, str) and runtime_model.strip()
        else None
    )
    return bool(
        record.get("effective_alias") == "auto"
        and record.get("engine") == "cursor"
        and record.get("model") == "auto"
        and record.get("worker_identity") == "cursor:auto"
        and normalized_runtime in {"auto", "cursor-auto"}
    )


def is_configured_alias(value: object) -> bool:
    """Return whether a value names consumer routing data."""
    return isinstance(value, str) and value in settings().aliases


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
        and bool(supersession.get("replacement_effort"))
    )


def verifier_identity_is_independent(
    *, worker_identity: object, worker_alias: object, worker_model: object,
    verifier_identity: object, verifier_alias: object,
) -> bool:
    if not isinstance(verifier_identity, str) or not verifier_identity.strip():
        return False
    if verifier_identity == worker_identity or verifier_alias == worker_alias:
        return False
    if verifier_alias in {"auto", "cursor-auto"}:
        return False
    actual_worker_model = worker_model if isinstance(worker_model, str) else None
    if not actual_worker_model and isinstance(worker_identity, str):
        actual_worker_model = worker_identity.partition(":")[2]
    normalized_worker = "-".join((actual_worker_model or "").casefold().split())
    if verifier_alias == "human":
        return True
    if normalized_worker in {"auto", "cursor-auto"}:
        return False
    configured = settings()
    verifier_model = configured.verifier_models.get(str(verifier_alias))
    verifier_route = configured.aliases.get(str(verifier_alias))
    verifier_models = {
        "-".join(str(value).casefold().split())
        for value in (
            verifier_model,
            verifier_route.model if verifier_route else None,
            verifier_route.backup_model if verifier_route else None,
        )
        if value
    }
    return bool(verifier_models) and normalized_worker not in verifier_models


REVIEW_INTENTS = (
    "discovery", "resolution-adjudication", "trust-manifest-verification", "delivery-code-review"
)
MODEL_VERIFIED_REVIEW_INTENTS = frozenset(REVIEW_INTENTS)
REVIEW_LENSES = ("code", "security")
NON_MODEL_VERIFIER_ALIASES = ("human",)
SKILL_RUN_ID_RE = re.compile(r"^sr_[0-9a-f]{32}$")
RUNTIME_CONTRACT_VERSION = 4
WORK_UNIT_HISTORY_DAYS = 3650
DISPATCH_OUTCOME_TYPES = frozenset(
    {"attempt-terminal", "attempt-recovery", "attempt-abort", "attempt-timeout"}
)
ATTEMPT_HISTORY_TYPES = frozenset(
    {
        "attempt-start",
        "attempt-checkpoint",
        "deposit-verification",
        "review-recovery-verification",
        "attempt-terminal",
        "attempt-recovery",
        "attempt-supersession",
        "inline",
        "attempt-abort",
    }
)
ESCALATION_TARGETS: dict[str, frozenset[str]] = {
    "luna": frozenset({"terra"}),
    "auto": frozenset({"terra"}),
    "terra": frozenset({"sol"}),
    "fable": frozenset({"sol"}),
    "fable-cursor": frozenset({"sol"}),
    "grok-cursor": frozenset({"sol"}),
    "opus": frozenset({"sol"}),
}


def validate_retry_policy(
    records: list[dict[str, object]],
    *,
    task_id: str,
    work_unit_id: str,
    alias: str,
    effort: str,
    retry_args: object | None = None,
) -> int:
    """Enforce the routing-owned one-step escalation graph.

    Cutover B owns the surrounding attempt-state admission.  This retained
    mechanism deliberately projects only authenticated outcomes and route
    succession, which is the portion formerly delegated to this routing table.
    """
    del retry_args
    from ..kernel.authority_projection import current_telemetry, retained_task_ids
    from .authority import authenticated_retry_outcomes

    governed = current_telemetry(records)
    if task_id in retained_task_ids(records) or any(
        record.get("type") in ATTEMPT_HISTORY_TYPES
        and record.get("task_id") == task_id
        for record in governed
    ):
        raise DispatchError(f"task_id {task_id!r} already exists")
    outcomes = authenticated_retry_outcomes(records)
    attempts: list[dict[str, object]] = []
    seen: set[str] = set()
    for record in reversed(outcomes):
        if str(record.get("work_unit_id") or record.get("task_id")) != work_unit_id:
            continue
        prior_task_id = str(record.get("task_id"))
        if prior_task_id in seen:
            continue
        seen.add(prior_task_id)
        attempts.append(record)
    attempts.reverse()
    if attempts:
        prior = attempts[-1]
        prior_alias = str(prior.get("effective_alias") or prior.get("alias"))
        allowed = ESCALATION_TARGETS.get(prior_alias, frozenset())
        if alias not in allowed:
            target_text = ", ".join(sorted(allowed)) or "none"
            raise DispatchError(
                f"work unit {work_unit_id!r} must escalate from {prior_alias} "
                f"to one of: {target_text}; {alias} {effort} is not allowed"
            )
    numbers = [
        value
        for record in outcomes
        if str(record.get("work_unit_id") or record.get("task_id")) == work_unit_id
        and type(value := record.get("unit_attempt_number")) is int
    ]
    return max(numbers, default=0) + 1


def __getattr__(name: str):
    dynamic = {
        "ROUTING", "TIER_DEFAULTS", "MODEL_VERIFIER_ALIASES", "DEFAULT_TIMEOUT_S",
        "DEFAULT_MEDIUM_BUDGET_USD", "DEFAULT_HIGH_BUDGET_USD",
        "TELEMETRY_SCHEMA_VERSION", "DISPATCH_POLICY_VERSION",
        "COMPATIBLE_DISPATCH_POLICY_VERSIONS", "ENGINE_COOLDOWN_S",
        "SKILL_RUN_ID_ENV", "RESULT_ARTIFACT_DIR",
    }
    if name not in dynamic:
        raise AttributeError(name)
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
    return values[name]


__all__ = [
    "CommandResult", "EnginePreflight", "RoutingDecision", "RoutingSettings",
    "configure", "effective_attempt_alias", "model_verifier_aliases", "route",
    "resolve_tier_default", "supersession_reason_matches_terminal",
    "validate_retry_policy", "verifier_identity_is_independent",
]
