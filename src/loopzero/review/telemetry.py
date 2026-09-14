"""Observational review telemetry kept outside immutable review task identity."""

from __future__ import annotations

import math
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from types import MappingProxyType
from typing import TYPE_CHECKING, Literal, cast

from ..kernel.canonical import canonical_record_digest
from ..kernel.policy import (
    DISPATCH_POLICY_VERSION,
    RUNTIME_CONTRACT_VERSION,
    TELEMETRY_SCHEMA_VERSION,
)
from ..kernel.review_state import patch_identity_digest
from ..runners.contract import ReviewOutcome, RuntimeResult

if TYPE_CHECKING:
    from ..kernel.review_state import ReviewSlotSettlement
    from .admission import LaunchReason, Reserved
    from .trust_claims import Invalidation


REVIEW_LAUNCH_TYPE = "review-launch-v1"
REVIEW_LAUNCH_OUTCOME_TYPE = "review-launch-outcome-v1"
REVIEW_LAUNCH_REASONS = frozenset(
    {
        "initial",
        "bounded-delta",
        "supersession",
        "trust-verification",
        "trust-delta",
        "security-path",
        "owner-requested",
        "infrastructure-retry",
    }
)
BillingMode = Literal["subscription", "metered", "unknown"]
_SHA256_RE = re.compile(r"[0-9a-f]{64}")


class ReviewTelemetryError(ValueError):
    """A review observation is malformed or conflicts with package authority."""


def _timestamp(value: datetime | str | None) -> str:
    if value is None:
        parsed = datetime.now(UTC)
    elif isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError as exc:
            raise ReviewTelemetryError("review timestamp is invalid") from exc
    else:
        raise ReviewTelemetryError("review timestamp is invalid")
    if parsed.tzinfo is None:
        raise ReviewTelemetryError("review timestamp must include a timezone")
    return parsed.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _path_digest(paths: Sequence[str]) -> str:
    normalized = tuple(sorted(set(paths)))
    if any(not isinstance(path, str) or not path for path in normalized):
        raise ReviewTelemetryError("review path observation is invalid")
    return canonical_record_digest(
        {"scheme": "review-path-set-v1", "paths": list(normalized)}
    )


def _mapping(value: Mapping[str, object] | None) -> Mapping[str, object]:
    if value is None:
        return MappingProxyType({"status": "unknown"})
    if not isinstance(value, Mapping) or any(not isinstance(key, str) for key in value):
        raise ReviewTelemetryError("review quota state is invalid")
    return MappingProxyType(dict(value))


def _task_value(task: Mapping[str, object], name: str) -> object:
    contract = task.get("task_contract")
    if isinstance(contract, Mapping) and name in contract:
        return contract[name]
    return task.get(name)


def _optional_string(value: object, *, label: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value:
        raise ReviewTelemetryError(f"{label} is invalid")
    return value


def _pr_number(value: object) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ReviewTelemetryError("review pull request number is invalid")
    return value


@dataclass(frozen=True, slots=True)
class ReviewLaunchV1:
    """One package-derived launch observation joined to a durable reservation."""

    reservation_id: str
    attempt_id: str
    _reason: LaunchReason = field(repr=False)
    _secondary_triggers: tuple[LaunchReason, ...] = field(repr=False)
    invalidation_causes: Mapping[str, str]
    generation_id: str
    family: Literal["delivery", "trust"]
    slot_kind: Literal["primary", "delta"]
    changed_path_count: int
    changed_paths_digest: str
    covered_path_count: int
    covered_paths_digest: str
    fresh_claim_count: int
    carried_claim_count: int
    quota_state: Mapping[str, object]
    admitted_at: str
    intent: str | None = None
    engine: str | None = None
    pr_number: int | None = None
    source_identity_digest: str | None = None
    patch_identity_digest: str | None = None
    manifest_digest: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.reservation_id, str) or not self.reservation_id:
            raise ReviewTelemetryError("review launch reservation id is invalid")
        if not isinstance(self.attempt_id, str) or not self.attempt_id:
            raise ReviewTelemetryError("review launch attempt id is invalid")
        if self._reason not in REVIEW_LAUNCH_REASONS:
            raise ReviewTelemetryError("review launch reason is invalid")
        secondary = tuple(dict.fromkeys(self._secondary_triggers))
        if self._reason in secondary or any(
            value not in REVIEW_LAUNCH_REASONS for value in secondary
        ):
            raise ReviewTelemetryError("review launch secondary triggers are invalid")
        if self.family not in {"delivery", "trust"}:
            raise ReviewTelemetryError("review launch family is invalid")
        if self.slot_kind not in {"primary", "delta"}:
            raise ReviewTelemetryError("review launch slot kind is invalid")
        for label, value in (
            ("changed path count", self.changed_path_count),
            ("covered path count", self.covered_path_count),
            ("fresh claim count", self.fresh_claim_count),
            ("carried claim count", self.carried_claim_count),
        ):
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ReviewTelemetryError(f"review launch {label} is invalid")
        for label, value in (
            ("changed paths digest", self.changed_paths_digest),
            ("covered paths digest", self.covered_paths_digest),
        ):
            if not isinstance(value, str) or _SHA256_RE.fullmatch(value) is None:
                raise ReviewTelemetryError(f"review launch {label} is invalid")
        for label, value in (
            ("source identity digest", self.source_identity_digest),
            ("patch identity digest", self.patch_identity_digest),
            ("manifest digest", self.manifest_digest),
        ):
            if value is not None and (
                not isinstance(value, str) or _SHA256_RE.fullmatch(value) is None
            ):
                raise ReviewTelemetryError(f"review launch {label} is invalid")
        causes = dict(self.invalidation_causes)
        if any(
            not isinstance(key, str)
            or not key
            or not isinstance(value, str)
            or not value
            for key, value in causes.items()
        ):
            raise ReviewTelemetryError("review launch invalidation causes are invalid")
        object.__setattr__(self, "_secondary_triggers", secondary)
        object.__setattr__(self, "invalidation_causes", MappingProxyType(causes))
        object.__setattr__(self, "quota_state", _mapping(self.quota_state))
        object.__setattr__(self, "admitted_at", _timestamp(self.admitted_at))
        object.__setattr__(self, "pr_number", _pr_number(self.pr_number))

    @property
    def reason(self) -> LaunchReason:
        return self._reason

    @property
    def secondary_triggers(self) -> tuple[LaunchReason, ...]:
        return self._secondary_triggers

    @classmethod
    def from_admission(
        cls,
        admitted: Reserved,
        *,
        attempt_id: str,
        admitted_at: datetime | str | None = None,
        invalidation: Invalidation | None = None,
        quota_state: Mapping[str, object] | None = None,
        intent: str | None = None,
        engine: str | None = None,
        pr_number: int | None = None,
    ) -> ReviewLaunchV1:
        """Build telemetry from a successful admission; no reason label is accepted."""
        from .admission import Reserved

        if not isinstance(admitted, Reserved):
            raise ReviewTelemetryError("review launch requires a reserved admission")
        task = admitted.scoped_task
        forbidden = {
            name for name in ("launch_reason", "review_launch_reason") if name in task
        }
        if forbidden:
            raise ReviewTelemetryError(
                "review launch reasons are package-derived, not caller labels"
            )
        changed = admitted.generation.changed_paths
        delta_scope = task.get("delta_scope")
        raw_covered = (
            delta_scope.get("closure_paths")
            if isinstance(delta_scope, Mapping)
            else (*changed, *admitted.generation.dependency_paths)
        )
        covered = tuple(raw_covered) if isinstance(raw_covered, list | tuple) else ()
        source = task.get("source_identity")
        patch = task.get("patch_identity")
        source_digest = (
            canonical_record_digest(dict(source))
            if isinstance(source, Mapping)
            else None
        )
        patch_digest = (
            patch_identity_digest(cast(Mapping[str, object], patch))
            if isinstance(patch, Mapping)
            else None
        )
        inferred_pr = next(
            (
                task.get(name)
                for name in ("pr_number", "pull_request_number", "pr")
                if task.get(name) is not None
            ),
            None,
        )
        inferred_engine = next(
            (
                task.get(name)
                for name in ("engine", "effective_alias", "alias")
                if task.get(name)
            ),
            None,
        )
        causes = dict(invalidation.causes) if invalidation is not None else {}
        return cls(
            reservation_id=admitted.slot.reservation_id,
            attempt_id=attempt_id,
            _reason=admitted.launch_reason,
            _secondary_triggers=admitted.secondary_triggers,
            invalidation_causes=causes,
            generation_id=admitted.generation.generation_id,
            family=admitted.slot.family,
            slot_kind=admitted.slot.slot_kind,
            changed_path_count=len(changed),
            changed_paths_digest=_path_digest(changed),
            covered_path_count=len(set(covered)),
            covered_paths_digest=_path_digest(covered),
            fresh_claim_count=(
                len(invalidation.fresh_claim_ids) if invalidation else 0
            ),
            carried_claim_count=(
                len(invalidation.carried_claims) if invalidation else 0
            ),
            quota_state=_mapping(quota_state),
            admitted_at=_timestamp(admitted_at),
            intent=_optional_string(
                intent if intent is not None else _task_value(task, "review_intent"),
                label="review intent",
            ),
            engine=_optional_string(
                engine if engine is not None else inferred_engine,
                label="review engine",
            ),
            pr_number=_pr_number(pr_number if pr_number is not None else inferred_pr),
            source_identity_digest=source_digest,
            patch_identity_digest=patch_digest,
            manifest_digest=_optional_string(
                _task_value(task, "manifest_sha256"), label="review manifest digest"
            ),
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "type": REVIEW_LAUNCH_TYPE,
            "schema_version": TELEMETRY_SCHEMA_VERSION,
            "policy_version": DISPATCH_POLICY_VERSION,
            "runtime_contract_version": RUNTIME_CONTRACT_VERSION,
            "reservation_id": self.reservation_id,
            "attempt_id": self.attempt_id,
            "reason": self.reason,
            "secondary_triggers": list(self.secondary_triggers),
            "invalidation_causes": dict(self.invalidation_causes),
            "generation_id": self.generation_id,
            "family": self.family,
            "slot_kind": self.slot_kind,
            "changed_path_count": self.changed_path_count,
            "changed_paths_digest": self.changed_paths_digest,
            "covered_path_count": self.covered_path_count,
            "covered_paths_digest": self.covered_paths_digest,
            "fresh_claim_count": self.fresh_claim_count,
            "carried_claim_count": self.carried_claim_count,
            "quota_state": dict(self.quota_state),
            "admitted_at": self.admitted_at,
            "intent": self.intent,
            "engine": self.engine,
            "pr_number": self.pr_number,
            "source_identity_digest": self.source_identity_digest,
            "patch_identity_digest": self.patch_identity_digest,
            "manifest_digest": self.manifest_digest,
        }

    to_record = to_dict

    @classmethod
    def from_mapping(cls, record: Mapping[str, object]) -> ReviewLaunchV1:
        metadata = {"type", "schema_version", "policy_version", "runtime_contract_version"}
        fields = {
            "reservation_id",
            "attempt_id",
            "reason",
            "secondary_triggers",
            "invalidation_causes",
            "generation_id",
            "family",
            "slot_kind",
            "changed_path_count",
            "changed_paths_digest",
            "covered_path_count",
            "covered_paths_digest",
            "fresh_claim_count",
            "carried_claim_count",
            "quota_state",
            "admitted_at",
            "intent",
            "engine",
            "pr_number",
            "source_identity_digest",
            "patch_identity_digest",
            "manifest_digest",
        }
        if (
            set(record) != metadata | fields
            or record.get("type") != REVIEW_LAUNCH_TYPE
            or not isinstance(record.get("secondary_triggers"), list)
            or not isinstance(record.get("invalidation_causes"), Mapping)
            or not isinstance(record.get("quota_state"), Mapping)
        ):
            raise ReviewTelemetryError("review launch record fields are invalid")
        return cls(
            reservation_id=cast(str, record["reservation_id"]),
            attempt_id=cast(str, record["attempt_id"]),
            _reason=cast("LaunchReason", record["reason"]),
            _secondary_triggers=tuple(
                cast(list["LaunchReason"], record["secondary_triggers"])
            ),
            invalidation_causes=cast(Mapping[str, str], record["invalidation_causes"]),
            generation_id=cast(str, record["generation_id"]),
            family=cast(Literal["delivery", "trust"], record["family"]),
            slot_kind=cast(Literal["primary", "delta"], record["slot_kind"]),
            changed_path_count=cast(int, record["changed_path_count"]),
            changed_paths_digest=cast(str, record["changed_paths_digest"]),
            covered_path_count=cast(int, record["covered_path_count"]),
            covered_paths_digest=cast(str, record["covered_paths_digest"]),
            fresh_claim_count=cast(int, record["fresh_claim_count"]),
            carried_claim_count=cast(int, record["carried_claim_count"]),
            quota_state=cast(Mapping[str, object], record["quota_state"]),
            admitted_at=cast(str, record["admitted_at"]),
            intent=cast(str | None, record["intent"]),
            engine=cast(str | None, record["engine"]),
            pr_number=cast(int | None, record["pr_number"]),
            source_identity_digest=cast(str | None, record["source_identity_digest"]),
            patch_identity_digest=cast(str | None, record["patch_identity_digest"]),
            manifest_digest=cast(str | None, record["manifest_digest"]),
        )

    from_dict = from_mapping


def billing_mode_for_credential_kind(kind: str | None) -> BillingMode:
    """Classify only a sealed credential kind; absent/unknown evidence stays unknown."""
    if kind in {
        "oauth",
        "oauth-file",
        "subscription",
        "claude-oauth",
        "codex-auth-file",
        "codex-oauth",
        "cursor-auth-file",
        "cursor-login",
    }:
        return "subscription"
    if kind in {
        "api-key",
        "console",
        "metered",
        "token-env",
        "token-file",
        "token-file(default)",
    }:
        return "metered"
    return "unknown"


@dataclass(frozen=True, slots=True)
class ReviewLaunchOutcomeV1:
    """One normalized terminal observation joined to launch and settlement."""

    reservation_id: str
    attempt_id: str
    review_outcome: ReviewOutcome
    failure_class: str | None
    elapsed_seconds: float | None
    tokens: Mapping[str, int | None]
    api_equivalent_usd: float | None
    cost_source: Literal["vendor", "estimated", "unknown"]
    billing_mode: BillingMode
    quota_state: Mapping[str, object]
    settlement_ref: str
    terminal_at: str

    def __post_init__(self) -> None:
        try:
            outcome = ReviewOutcome(self.review_outcome)
        except (TypeError, ValueError) as exc:
            raise ReviewTelemetryError("review telemetry outcome is invalid") from exc
        if not isinstance(self.reservation_id, str) or not self.reservation_id:
            raise ReviewTelemetryError("review outcome reservation id is invalid")
        if not isinstance(self.attempt_id, str) or not self.attempt_id:
            raise ReviewTelemetryError("review outcome attempt id is invalid")
        if self.elapsed_seconds is not None and (
            isinstance(self.elapsed_seconds, bool)
            or not isinstance(self.elapsed_seconds, int | float)
            or not math.isfinite(self.elapsed_seconds)
            or self.elapsed_seconds < 0
        ):
            raise ReviewTelemetryError("review elapsed seconds is invalid")
        normalized_tokens: dict[str, int | None] = {}
        for name, value in self.tokens.items():
            if not isinstance(name, str) or (
                value is not None
                and (isinstance(value, bool) or not isinstance(value, int) or value < 0)
            ):
                raise ReviewTelemetryError("review token observation is invalid")
            normalized_tokens[name] = value
        if self.api_equivalent_usd is not None and (
            isinstance(self.api_equivalent_usd, bool)
            or not isinstance(self.api_equivalent_usd, int | float)
            or not math.isfinite(self.api_equivalent_usd)
            or self.api_equivalent_usd < 0
        ):
            raise ReviewTelemetryError("review API-equivalent cost is invalid")
        if self.cost_source not in {"vendor", "estimated", "unknown"}:
            raise ReviewTelemetryError("review cost source is invalid")
        if self.api_equivalent_usd is None and self.cost_source != "unknown":
            raise ReviewTelemetryError("missing review cost must have unknown source")
        if self.billing_mode not in {"subscription", "metered", "unknown"}:
            raise ReviewTelemetryError("review billing mode is invalid")
        if self.failure_class is not None and (
            not isinstance(self.failure_class, str) or not self.failure_class
        ):
            raise ReviewTelemetryError("review failure class is invalid")
        if (
            not isinstance(self.settlement_ref, str)
            or _SHA256_RE.fullmatch(self.settlement_ref) is None
        ):
            raise ReviewTelemetryError("review settlement reference is invalid")
        object.__setattr__(self, "review_outcome", outcome)
        object.__setattr__(self, "tokens", MappingProxyType(normalized_tokens))
        object.__setattr__(self, "quota_state", _mapping(self.quota_state))
        object.__setattr__(self, "terminal_at", _timestamp(self.terminal_at))

    @classmethod
    def from_settlement(
        cls,
        launch: ReviewLaunchV1,
        settlement: ReviewSlotSettlement,
        result: RuntimeResult,
        *,
        sealed_credential_kind: str | None = None,
        quota_state: Mapping[str, object] | None = None,
        terminal_at: datetime | str | None = None,
    ) -> ReviewLaunchOutcomeV1:
        if settlement.reservation_id != launch.reservation_id:
            raise ReviewTelemetryError("review outcome does not match its launch")
        if result.attempt_id != launch.attempt_id:
            raise ReviewTelemetryError("runtime result does not match its launch")
        usage = result.usage
        tokens = {
            "input": usage.input_tokens if usage is not None else None,
            "output": usage.output_tokens if usage is not None else None,
            "cache_read": usage.cache_read_tokens if usage is not None else None,
            "cache_write": usage.cache_write_tokens if usage is not None else None,
            "reasoning": usage.reasoning_tokens if usage is not None else None,
        }
        return cls(
            reservation_id=launch.reservation_id,
            attempt_id=launch.attempt_id,
            review_outcome=settlement.outcome,
            failure_class=(
                None
                if settlement.outcome is ReviewOutcome.CONSUMED
                else result.terminal_reason.value
            ),
            elapsed_seconds=result.duration_s,
            tokens=tokens,
            api_equivalent_usd=result.cost_usd,
            cost_source=result.cost_source.value,
            billing_mode=billing_mode_for_credential_kind(sealed_credential_kind),
            quota_state=_mapping(quota_state),
            settlement_ref=canonical_record_digest(settlement.to_dict()),
            terminal_at=_timestamp(terminal_at),
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "type": REVIEW_LAUNCH_OUTCOME_TYPE,
            "schema_version": TELEMETRY_SCHEMA_VERSION,
            "policy_version": DISPATCH_POLICY_VERSION,
            "runtime_contract_version": RUNTIME_CONTRACT_VERSION,
            "reservation_id": self.reservation_id,
            "attempt_id": self.attempt_id,
            "review_outcome": self.review_outcome.value,
            "failure_class": self.failure_class,
            "elapsed_seconds": self.elapsed_seconds,
            "tokens": dict(self.tokens),
            "api_equivalent_usd": self.api_equivalent_usd,
            "cost_source": self.cost_source,
            "billing_mode": self.billing_mode,
            "quota_state": dict(self.quota_state),
            "settlement_ref": self.settlement_ref,
            "terminal_at": self.terminal_at,
        }

    to_record = to_dict

    @classmethod
    def from_mapping(cls, record: Mapping[str, object]) -> ReviewLaunchOutcomeV1:
        metadata = {"type", "schema_version", "policy_version", "runtime_contract_version"}
        fields = {
            "reservation_id",
            "attempt_id",
            "review_outcome",
            "failure_class",
            "elapsed_seconds",
            "tokens",
            "api_equivalent_usd",
            "cost_source",
            "billing_mode",
            "quota_state",
            "settlement_ref",
            "terminal_at",
        }
        if (
            set(record) != metadata | fields
            or record.get("type") != REVIEW_LAUNCH_OUTCOME_TYPE
            or not isinstance(record.get("tokens"), Mapping)
            or not isinstance(record.get("quota_state"), Mapping)
        ):
            raise ReviewTelemetryError("review launch outcome record fields are invalid")
        return cls(
            reservation_id=cast(str, record["reservation_id"]),
            attempt_id=cast(str, record["attempt_id"]),
            review_outcome=ReviewOutcome(cast(str, record["review_outcome"])),
            failure_class=cast(str | None, record["failure_class"]),
            elapsed_seconds=cast(float | None, record["elapsed_seconds"]),
            tokens=cast(Mapping[str, int | None], record["tokens"]),
            api_equivalent_usd=cast(float | None, record["api_equivalent_usd"]),
            cost_source=cast(Literal["vendor", "estimated", "unknown"], record["cost_source"]),
            billing_mode=cast(BillingMode, record["billing_mode"]),
            quota_state=cast(Mapping[str, object], record["quota_state"]),
            settlement_ref=cast(str, record["settlement_ref"]),
            terminal_at=cast(str, record["terminal_at"]),
        )

    from_dict = from_mapping


__all__ = [
    "REVIEW_LAUNCH_OUTCOME_TYPE",
    "REVIEW_LAUNCH_REASONS",
    "REVIEW_LAUNCH_TYPE",
    "BillingMode",
    "ReviewLaunchOutcomeV1",
    "ReviewLaunchV1",
    "ReviewTelemetryError",
    "billing_mode_for_credential_kind",
]
