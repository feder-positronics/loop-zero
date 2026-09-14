"""Reproducible review measurements over current and legacy ledger rows."""

from __future__ import annotations

import math
import statistics
from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from typing import cast

from ..kernel.canonical import canonical_record_digest
from .telemetry import REVIEW_LAUNCH_OUTCOME_TYPE, REVIEW_LAUNCH_TYPE

UNRECORDED = "unrecorded"


@dataclass(frozen=True, slots=True)
class ReviewTotals:
    starts: int
    terminals: int
    verdicts: int
    failures: int
    distinct_source_identities: int
    source_identity_unrecorded: int
    distinct_patch_identities: int
    patch_identity_unrecorded: int
    distinct_manifest_identities: int
    manifest_identity_unrecorded: int
    unchanged_content_repetitions: int
    fresh_claims: int
    carried_claims: int
    claim_counts_unrecorded: int
    minutes: float | None
    elapsed_unknown: int
    tokens: int | None
    tokens_unknown: int
    api_equivalent_usd: float | None
    known_api_equivalent_usd: float
    cost_unknown: int


@dataclass(frozen=True, slots=True)
class Percentile:
    median: float | None
    p95: float | None
    unknown: int = 0


@dataclass(frozen=True, slots=True)
class ReviewStats:
    filters: Mapping[str, object]
    totals: ReviewTotals
    per_pr: Mapping[str, ReviewTotals]
    per_intent: Mapping[str, ReviewTotals]
    per_reason: Mapping[str, ReviewTotals]
    per_engine: Mapping[str, ReviewTotals]
    failures_by_class: Mapping[str, int]
    percentiles: Mapping[str, Percentile]

    def to_dict(self) -> dict[str, object]:
        def table(values: Mapping[str, ReviewTotals]) -> dict[str, object]:
            return {key: asdict(value) for key, value in values.items()}

        return {
            "filters": dict(self.filters),
            "totals": asdict(self.totals),
            "per_pr": table(self.per_pr),
            "per_intent": table(self.per_intent),
            "per_reason": table(self.per_reason),
            "per_engine": table(self.per_engine),
            "failures_by_class": dict(self.failures_by_class),
            "percentiles": {
                key: asdict(value) for key, value in self.percentiles.items()
            },
        }


@dataclass(slots=True)
class _Attempt:
    key: tuple[object, ...]
    pr: str = UNRECORDED
    intent: str = UNRECORDED
    reason: str = UNRECORDED
    engine: str = UNRECORDED
    started: bool = False
    terminal: bool = False
    verdict: bool = False
    failure_class: str | None = None
    source_identity: str | None = None
    patch_identity: str | None = None
    manifest_identity: str | None = None
    fresh_claims: int | None = None
    carried_claims: int | None = None
    elapsed_seconds: float | None = None
    token_count: int | None = None
    cost: float | None = None
    timestamp: datetime | None = None
    unchanged_repeat: bool = False


def _parse_time(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(UTC)


def _since(value: datetime | str | None) -> datetime | None:
    if value is None:
        return None
    parsed = value if isinstance(value, datetime) else _parse_time(value)
    if parsed is None or parsed.tzinfo is None:
        raise ValueError("since must be an ISO timestamp with a timezone")
    return parsed.astimezone(UTC)


def _string(value: object) -> str:
    return value if isinstance(value, str) and value else UNRECORDED


def _pr(value: object) -> str:
    if isinstance(value, int) and not isinstance(value, bool) and value > 0:
        return f"#{value}"
    if isinstance(value, str) and value:
        stripped = value.removeprefix("#")
        return f"#{stripped}" if stripped.isdigit() else value
    return UNRECORDED


def _first(record: Mapping[str, object], *names: str) -> object:
    contract = record.get("task_contract")
    for name in names:
        if name in record and record[name] is not None:
            return record[name]
        if isinstance(contract, Mapping) and contract.get(name) is not None:
            return contract[name]
    return None


def _identity(value: object) -> str | None:
    if isinstance(value, str) and value:
        return value
    if isinstance(value, Mapping):
        return canonical_record_digest(dict(value))
    return None


def _attempt_key(record: Mapping[str, object]) -> tuple[object, ...] | None:
    attempt_id = record.get("attempt_id")
    if isinstance(attempt_id, str) and attempt_id:
        return ("attempt-id", attempt_id)
    task_id = record.get("task_id")
    attempt_index = record.get("attempt_index")
    if isinstance(task_id, str) and task_id:
        return ("legacy", record.get("run_id"), task_id, attempt_index)
    return None


def _is_review(record: Mapping[str, object]) -> bool:
    values = (
        _first(record, "review_intent", "intent"),
        _first(record, "work_unit_id"),
        _first(record, "category"),
    )
    return any(
        isinstance(value, str)
        and any(
            token in value.lower()
            for token in ("review", "trust-manifest", "adjudication", "discovery")
        )
        for value in values
    )


def _set_dimensions(attempt: _Attempt, record: Mapping[str, object]) -> None:
    raw_pr = _first(record, "pr_number", "pull_request_number", "pr")
    raw_intent = _first(record, "intent", "review_intent")
    raw_engine = _first(record, "engine", "effective_alias", "alias", "provider")
    if attempt.pr == UNRECORDED:
        attempt.pr = _pr(raw_pr)
    if attempt.intent == UNRECORDED:
        attempt.intent = _string(raw_intent)
    if attempt.engine == UNRECORDED:
        attempt.engine = _string(raw_engine)


def _numeric(value: object) -> float | None:
    if (
        isinstance(value, bool)
        or not isinstance(value, int | float)
        or not math.isfinite(value)
        or value < 0
    ):
        return None
    return float(value)


def _tokens(record: Mapping[str, object]) -> int | None:
    raw = record.get("tokens")
    if isinstance(raw, Mapping):
        values = [
            value
            for name, value in raw.items()
            if name not in {"cache_read", "cache_write"}
            and isinstance(value, int)
            and not isinstance(value, bool)
            and value >= 0
        ]
        return sum(values) if values else None
    direct = [
        record.get(name)
        for name in ("input_tokens", "output_tokens", "reasoning_tokens")
    ]
    known = [
        cast(int, value)
        for value in direct
        if isinstance(value, int) and not isinstance(value, bool) and value >= 0
    ]
    return sum(known) if known else None


def _legacy_failure(record: Mapping[str, object]) -> str | None:
    explicit = _first(record, "failure_class")
    if isinstance(explicit, str) and explicit:
        return explicit
    status = record.get("status")
    if status in {"completed", "pass", "passed", "success", "clean"}:
        return None
    reason = _first(record, "terminal_reason")
    if isinstance(reason, str) and reason not in {"completed"}:
        return reason
    if isinstance(status, str) and status:
        return status
    return None


def _record_timestamp(record: Mapping[str, object]) -> datetime | None:
    for name in ("admitted_at", "terminal_at", "ts", "timestamp", "created_at"):
        parsed = _parse_time(record.get(name))
        if parsed is not None:
            return parsed
    return None


def _collect(records: Sequence[Mapping[str, object]]) -> list[_Attempt]:
    attempts: dict[tuple[object, ...], _Attempt] = {}
    launches: dict[tuple[str, str], _Attempt] = {}
    new_attempt_ids = {
        cast(str, record["attempt_id"])
        for record in records
        if record.get("type") == REVIEW_LAUNCH_TYPE
        and isinstance(record.get("attempt_id"), str)
    }
    review_legacy_keys = {
        key
        for record in records
        if record.get("type") == "attempt-start" and _is_review(record)
        if (key := _attempt_key(record)) is not None
    }

    for index, record in enumerate(records):
        record_type = record.get("type")
        if record_type == REVIEW_LAUNCH_TYPE:
            reservation = record.get("reservation_id")
            attempt_id = record.get("attempt_id")
            if not isinstance(reservation, str) or not isinstance(attempt_id, str):
                continue
            key = ("review-launch", reservation, attempt_id)
            attempt = attempts.setdefault(key, _Attempt(key))
            launches[(reservation, attempt_id)] = attempt
            attempt.started = True
            _set_dimensions(attempt, record)
            attempt.reason = _string(record.get("reason"))
            attempt.source_identity = _identity(record.get("source_identity_digest"))
            attempt.patch_identity = _identity(record.get("patch_identity_digest"))
            attempt.manifest_identity = _identity(record.get("manifest_digest"))
            raw_fresh = record.get("fresh_claim_count")
            raw_carried = record.get("carried_claim_count")
            attempt.fresh_claims = raw_fresh if isinstance(raw_fresh, int) else None
            attempt.carried_claims = (
                raw_carried if isinstance(raw_carried, int) else None
            )
            attempt.timestamp = _record_timestamp(record)
            continue
        if record_type == REVIEW_LAUNCH_OUTCOME_TYPE:
            reservation = record.get("reservation_id")
            attempt_id = record.get("attempt_id")
            if not isinstance(reservation, str) or not isinstance(attempt_id, str):
                continue
            key = ("review-launch", reservation, attempt_id)
            attempt = launches.get((reservation, attempt_id)) or attempts.setdefault(
                key, _Attempt(key)
            )
            attempt.terminal = True
            outcome = record.get("review_outcome")
            attempt.verdict = outcome == "consumed"
            if outcome != "consumed":
                attempt.failure_class = _string(record.get("failure_class") or outcome)
            attempt.elapsed_seconds = _numeric(record.get("elapsed_seconds"))
            attempt.token_count = _tokens(record)
            attempt.cost = _numeric(record.get("api_equivalent_usd"))
            attempt.timestamp = _record_timestamp(record) or attempt.timestamp
            continue

        key = _attempt_key(record)
        if key is None:
            continue
        if key[0] == "attempt-id" and key[1] in new_attempt_ids:
            continue
        if key not in review_legacy_keys and not _is_review(record):
            continue
        attempt = attempts.setdefault(key, _Attempt(key))
        _set_dimensions(attempt, record)
        attempt.timestamp = _record_timestamp(record) or attempt.timestamp
        if record_type == "attempt-start":
            attempt.started = True
            attempt.source_identity = _identity(
                _first(record, "source_identity", "source_identity_digest")
            )
            attempt.patch_identity = _identity(
                _first(record, "patch_identity", "patch_identity_digest", "patch_id")
            )
            attempt.manifest_identity = _identity(
                _first(record, "manifest_sha256", "manifest_digest")
            )
        elif record_type in {"attempt-terminal", "attempt-recovery", "attempt-abort"}:
            attempt.terminal = True
            attempt.failure_class = _legacy_failure(record)
            duration = _numeric(_first(record, "elapsed_seconds", "duration_s"))
            if duration is None:
                milliseconds = _numeric(record.get("duration_ms"))
                duration = milliseconds / 1000 if milliseconds is not None else None
            attempt.elapsed_seconds = duration
            attempt.token_count = _tokens(record)
            attempt.cost = _numeric(
                _first(record, "api_equivalent_usd", "cost_usd", "total_cost_usd")
            )
            accepted = _first(record, "accepted_verdict")
            attempt.verdict = accepted in {"pass", "fail", "clean", "findings"}

    verdict_tasks = {
        (record.get("run_id"), record.get("task_id"))
        for record in records
        if record.get("type") == "verdict"
        and record.get("verdict") in {"pass", "fail", "clean", "findings"}
    }
    for attempt in attempts.values():
        if (
            attempt.key[0] == "legacy"
            and (attempt.key[1], attempt.key[2]) in verdict_tasks
        ):
            attempt.verdict = True

    seen: dict[tuple[str, str], set[str]] = defaultdict(set)
    for attempt in attempts.values():
        if not attempt.started:
            continue
        content = (
            attempt.source_identity
            if attempt.intent == "trust-manifest-verification"
            else attempt.patch_identity or attempt.source_identity
        )
        if content is None:
            continue
        scope = (attempt.pr, attempt.intent)
        attempt.unchanged_repeat = content in seen[scope]
        seen[scope].add(content)
    return list(attempts.values())


def _totals(attempts: Iterable[_Attempt]) -> ReviewTotals:
    values = list(attempts)
    terminals = [attempt for attempt in values if attempt.terminal]
    elapsed_unknown = sum(attempt.elapsed_seconds is None for attempt in terminals)
    tokens_unknown = sum(attempt.token_count is None for attempt in terminals)
    cost_unknown = sum(attempt.cost is None for attempt in terminals)
    elapsed = sum(attempt.elapsed_seconds or 0.0 for attempt in terminals)
    tokens = sum(attempt.token_count or 0 for attempt in terminals)
    known_cost = sum(attempt.cost or 0.0 for attempt in terminals)
    return ReviewTotals(
        starts=sum(attempt.started for attempt in values),
        terminals=len(terminals),
        verdicts=sum(attempt.verdict for attempt in values),
        failures=sum(attempt.failure_class is not None for attempt in terminals),
        distinct_source_identities=len(
            {attempt.source_identity for attempt in values if attempt.source_identity}
        ),
        source_identity_unrecorded=sum(
            attempt.started and attempt.source_identity is None for attempt in values
        ),
        distinct_patch_identities=len(
            {attempt.patch_identity for attempt in values if attempt.patch_identity}
        ),
        patch_identity_unrecorded=sum(
            attempt.started and attempt.patch_identity is None for attempt in values
        ),
        distinct_manifest_identities=len(
            {
                attempt.manifest_identity
                for attempt in values
                if attempt.manifest_identity
            }
        ),
        manifest_identity_unrecorded=sum(
            attempt.started and attempt.manifest_identity is None for attempt in values
        ),
        unchanged_content_repetitions=sum(
            attempt.unchanged_repeat for attempt in values
        ),
        fresh_claims=sum(attempt.fresh_claims or 0 for attempt in values),
        carried_claims=sum(attempt.carried_claims or 0 for attempt in values),
        claim_counts_unrecorded=sum(
            attempt.started
            and (attempt.fresh_claims is None or attempt.carried_claims is None)
            for attempt in values
        ),
        minutes=(round(elapsed / 60, 6) if terminals and not elapsed_unknown else None),
        elapsed_unknown=elapsed_unknown,
        tokens=(tokens if terminals and not tokens_unknown else None),
        tokens_unknown=tokens_unknown,
        api_equivalent_usd=(
            round(known_cost, 6) if terminals and not cost_unknown else None
        ),
        known_api_equivalent_usd=round(known_cost, 6),
        cost_unknown=cost_unknown,
    )


def _table(attempts: Sequence[_Attempt], attribute: str) -> dict[str, ReviewTotals]:
    grouped: dict[str, list[_Attempt]] = defaultdict(list)
    for attempt in attempts:
        grouped[cast(str, getattr(attempt, attribute))].append(attempt)
    return {key: _totals(grouped[key]) for key in sorted(grouped)}


def _percentile(values: Sequence[float | None]) -> Percentile:
    known = sorted(value for value in values if value is not None)
    unknown = len(values) - len(known)
    if not known:
        return Percentile(None, None, unknown)
    p95_index = max(0, math.ceil(0.95 * len(known)) - 1)
    return Percentile(float(statistics.median(known)), float(known[p95_index]), unknown)


def _merged_prs(
    records: Sequence[Mapping[str, object]], attempts: Sequence[_Attempt], count: int
) -> set[str]:
    merged: dict[str, datetime] = {}
    for record in records:
        if (
            record.get("type")
            not in {
                "pull-request-merged",
                "pr-merged",
                "merge",
                "delivery-merged",
            }
            and record.get("merged") is not True
        ):
            continue
        number = _pr(_first(record, "pr_number", "pull_request_number", "pr"))
        timestamp = _parse_time(_first(record, "merged_at", "ts", "timestamp"))
        if number != UNRECORDED and timestamp is not None:
            merged[number] = max(timestamp, merged.get(number, timestamp))
    if not merged:
        for attempt in attempts:
            if attempt.pr != UNRECORDED and attempt.timestamp is not None:
                merged[attempt.pr] = max(
                    attempt.timestamp, merged.get(attempt.pr, attempt.timestamp)
                )
    return {
        pr
        for pr, _ in sorted(
            merged.items(), key=lambda item: (item[1], item[0]), reverse=True
        )[:count]
    }


def review_stats(
    records: Sequence[Mapping[str, object]],
    *,
    last_merged: int | None = None,
    since: datetime | str | None = None,
) -> ReviewStats:
    """Aggregate review launches and legacy attempts without inventing evidence."""
    if last_merged is not None and (
        isinstance(last_merged, bool)
        or not isinstance(last_merged, int)
        or last_merged < 1
    ):
        raise ValueError("last_merged must be a positive integer")
    cutoff = _since(since)
    attempts = _collect(records)
    if cutoff is not None:
        attempts = [
            attempt
            for attempt in attempts
            if attempt.timestamp is not None and attempt.timestamp >= cutoff
        ]
    selected_prs: set[str] | None = None
    if last_merged is not None:
        selected_prs = _merged_prs(records, attempts, last_merged)
        attempts = [attempt for attempt in attempts if attempt.pr in selected_prs]

    per_pr = _table(attempts, "pr")
    if selected_prs is not None:
        per_pr = {key: per_pr.get(key, _totals(())) for key in sorted(selected_prs)}
    failures: dict[str, int] = defaultdict(int)
    for attempt in attempts:
        if attempt.terminal and attempt.failure_class is not None:
            failures[attempt.failure_class] += 1
    metrics = (
        "starts",
        "terminals",
        "verdicts",
        "failures",
        "unchanged_content_repetitions",
        "fresh_claims",
        "carried_claims",
        "minutes",
        "api_equivalent_usd",
    )
    filters: dict[str, object] = {
        "last_merged": last_merged,
        "since": cutoff.isoformat().replace("+00:00", "Z") if cutoff else None,
        "selected_prs": sorted(selected_prs) if selected_prs is not None else None,
    }
    return ReviewStats(
        filters=filters,
        totals=_totals(attempts),
        per_pr=per_pr,
        per_intent=_table(attempts, "intent"),
        per_reason=_table(attempts, "reason"),
        per_engine=_table(attempts, "engine"),
        failures_by_class=dict(sorted(failures.items())),
        percentiles={
            metric: _percentile(
                [
                    cast(float | None, getattr(total, metric))
                    for total in per_pr.values()
                ]
            )
            for metric in metrics
        },
    )


__all__ = [
    "UNRECORDED",
    "Percentile",
    "ReviewStats",
    "ReviewTotals",
    "review_stats",
]
