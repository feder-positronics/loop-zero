"""Native agent-runtime transport contracts and CLI adapters."""

# Stable public schema surface.  The implementation remains in the private
# schema module so provider-specific contract code can share it without cycles.
from ._review_schema import TRUST_CLAIM_ID_PATTERN, trust_claim_verdicts_schema

from .contract import (
    RUNTIME_PROGRESS_PROTOCOL_VERSION,
    ReviewOutcome,
    RuntimeAdapter,
    RuntimeBillingMode,
    RuntimeCapabilityProfile,
    RuntimeCostSource,
    RuntimeCostStatus,
    RuntimeEvent,
    RuntimePhase,
    RuntimeProgress,
    RuntimeProgressCallback,
    RuntimeProgressSignal,
    RuntimeReadiness,
    RuntimeRequest,
    RuntimeResult,
    RuntimeStatus,
    RuntimeTransportAttempt,
    SubscriptionEligibility,
)
from .registry import NATIVE_RUNTIME_REGISTRY, RuntimeRegistration, RuntimeRegistry
from .settings import RuntimeBudget, RuntimeSettings

__all__ = [
    "RUNTIME_PROGRESS_PROTOCOL_VERSION",
    "ReviewOutcome",
    "RuntimeAdapter",
    "RuntimeBillingMode",
    "RuntimeEvent",
    "RuntimePhase",
    "RuntimeProgress",
    "RuntimeProgressCallback",
    "RuntimeProgressSignal",
    "RuntimeReadiness",
    "RuntimeRequest",
    "RuntimeResult",
    "RuntimeCapabilityProfile",
    "RuntimeCostSource",
    "RuntimeCostStatus",
    "RuntimeTransportAttempt",
    "RuntimeStatus",
    "SubscriptionEligibility",
    "TRUST_CLAIM_ID_PATTERN",
    "trust_claim_verdicts_schema",
    "NATIVE_RUNTIME_REGISTRY",
    "RuntimeRegistration",
    "RuntimeRegistry",
    "RuntimeBudget",
    "RuntimeSettings",
]
