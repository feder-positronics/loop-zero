"""Persisted ledger vocabulary. Routing decisions belong to the A4 consumer."""
from . import ledger as authority_ledger
from .settings import settings
from .seams import supersession_reason_matches_terminal
DISPATCH_DIR = settings.audit_root / "dispatch"

TELEMETRY_SCHEMA_VERSION = settings.telemetry_schema_version

DISPATCH_POLICY_VERSION = settings.policy_version

COMPATIBLE_DISPATCH_POLICY_VERSIONS = frozenset(
    {"2026-07-24-v9", "2026-08-06-v10", DISPATCH_POLICY_VERSION}
)

LEGACY_COORDINATOR_LEDGER_PREFIX_SCHEME = "dispatch-coordinator-ledger-prefix-v1"

COORDINATOR_LEDGER_PREFIX_SCHEME = "dispatch-coordinator-ledger-prefix-v2"

COORDINATOR_LEDGER_PREFIX_V3_SCHEME = authority_ledger.LEDGER_ACCUMULATOR_SCHEME

AUTHORITY_LEDGER_DIRECTORY = DISPATCH_DIR / "ledger"

AUTHORITY_ARCHIVE_DIRECTORY = DISPATCH_DIR / "archives"

LEGACY_AUTHORITY_RECOVERY_DIRECTORY = DISPATCH_DIR / "legacy-recovery"

AUTHORITY_LEDGER_MAX_BYTES = settings.ledger_max_bytes

AUTHORITY_LEDGER_MAX_RECORDS = settings.ledger_max_records

AUTHORITY_DOWNGRADE_BARRIER_NAME = settings.downgrade_barrier_name

LEGACY_COORDINATOR_PREFIX_SCHEMA_VERSION = "dispatch-telemetry-v9"

LEGACY_COORDINATOR_PREFIX_POLICY_VERSIONS = frozenset(
    {"2026-07-24-v9", "2026-08-06-v10", "2026-08-17-v11"}
)

LEGACY_COORDINATOR_PREFIX_RUNTIME_CONTRACT_VERSION = 4

RUNTIME_CONTRACT_VERSION = 4

PACKAGING_TIMEOUT_FAILURE_CLASS = "result-packaging"

ATTEMPT_TERMINAL_TYPES = frozenset(
    {
        "attempt-terminal",
        "attempt-recovery",
        "attempt-supersession",
        "inline",
    }
)

ATTEMPT_ABORT_TYPES = frozenset({"attempt-abort"})

ATTEMPT_HISTORY_TYPES = frozenset(
    {
        "attempt-start",
        "attempt-checkpoint",
        "deposit-verification",
        "review-recovery-verification",
        *ATTEMPT_TERMINAL_TYPES,
        *ATTEMPT_ABORT_TYPES,
    }
)

DISPATCH_TERMINAL_TYPES = frozenset({"attempt-terminal"})

DISPATCH_OUTCOME_TYPES = frozenset(
    {*DISPATCH_TERMINAL_TYPES, "attempt-recovery", *ATTEMPT_ABORT_TYPES}
)
