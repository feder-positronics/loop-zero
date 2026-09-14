"""Versioned reporting prices for normalized native-runtime usage.

These rates are deliberately package data rather than live vendor lookups.  They
make nightly evidence reproducible and are used only for reporting and budget
ceiling decisions; they are not a claim about an invoice or subscription value.
All values are USD per million tokens.
"""

from __future__ import annotations

from dataclasses import dataclass
import math
from types import MappingProxyType

from .contract import RuntimeCostSource, RuntimeUsage

PRICE_TABLE_VERSION = "2026-09-12"
MAX_VENDOR_COST_USD = 1_000_000


@dataclass(frozen=True, slots=True)
class ModelPrice:
    input_per_million: float
    output_per_million: float
    cache_read_per_million: float = 0.0
    cache_write_per_million: float = 0.0
    cache_read_in_input: bool = False


MODEL_PRICES = MappingProxyType(
    {
        "claude-fable-5-1": ModelPrice(5.0, 25.0, 0.50, 6.25),
        "claude-fable-5-medium": ModelPrice(3.0, 15.0, 0.30, 3.75),
        "claude-opus-5": ModelPrice(15.0, 75.0, 1.50, 18.75),
        "gpt-5.6-sol": ModelPrice(2.50, 15.0, 0.25, cache_read_in_input=True),
        "gpt-5.6-terra": ModelPrice(1.25, 10.0, 0.125, cache_read_in_input=True),
        "gpt-5.6-luna": ModelPrice(0.25, 2.0, 0.025, cache_read_in_input=True),
        "cursor-grok-4.6-high": ModelPrice(3.0, 15.0, 0.30),
    }
)


def estimated_cost_usd(model: str, usage: RuntimeUsage | None) -> float | None:
    """Price complete token evidence, preserving unknown instead of using zero."""
    price = MODEL_PRICES.get(model)
    if (
        price is None
        or usage is None
        or usage.input_tokens is None
        or usage.output_tokens is None
    ):
        return None
    # A completed turn with input but no output is commonly a partial or
    # provider-truncated usage record.  It is unknown, never zero-cost proof.
    if usage.output_tokens == 0:
        return None
    cache_read = usage.cache_read_tokens or 0
    cache_write = usage.cache_write_tokens or 0
    billable_input = usage.input_tokens
    if price.cache_read_in_input:
        if cache_read > billable_input:
            return None
        billable_input -= cache_read
    total = (
        billable_input * price.input_per_million
        + usage.output_tokens * price.output_per_million
        + cache_read * price.cache_read_per_million
        + cache_write * price.cache_write_per_million
    ) / 1_000_000
    return round(total, 9)


def normalized_vendor_cost_usd(value: object) -> float | None:
    """Accept only finite, non-boolean vendor costs inside the sanity bound."""
    if (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and 0 <= value <= MAX_VENDOR_COST_USD
        and math.isfinite(value)
    ):
        return float(value)
    return None


def resolved_cost_usd(
    model: str,
    usage: RuntimeUsage | None,
    vendor_cost_usd: object,
) -> tuple[float | None, RuntimeCostSource]:
    """Prefer bounded vendor cost, then complete pinned-price evidence."""
    vendor_cost = normalized_vendor_cost_usd(vendor_cost_usd)
    if vendor_cost is not None:
        return vendor_cost, RuntimeCostSource.VENDOR
    estimated = estimated_cost_usd(model, usage)
    if estimated is not None:
        return estimated, RuntimeCostSource.ESTIMATED
    return None, RuntimeCostSource.UNKNOWN


def total_tokens(usage: RuntimeUsage | None, *, model: str | None = None) -> int | None:
    """Return total evidence using the pinned model's cache-counter semantics."""
    if usage is None or usage.input_tokens is None or usage.output_tokens is None:
        return None
    price = MODEL_PRICES.get(model) if model is not None else None
    cache_read = 0 if price is not None and price.cache_read_in_input else (
        usage.cache_read_tokens or 0
    )
    return (
        usage.input_tokens
        + usage.output_tokens
        + cache_read
        + (usage.cache_write_tokens or 0)
    )


__all__ = [
    "MAX_VENDOR_COST_USD", "MODEL_PRICES", "PRICE_TABLE_VERSION", "ModelPrice",
    "estimated_cost_usd", "normalized_vendor_cost_usd", "resolved_cost_usd",
    "total_tokens",
]
