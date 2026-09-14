import pytest

from loopzero.runners.contract import RuntimeCostSource, RuntimeUsage
from loopzero.runners.pricing import (
    PRICE_TABLE_VERSION,
    estimated_cost_usd,
    resolved_cost_usd,
    total_tokens,
)


def test_price_table_is_versioned_and_prices_complete_usage():
    assert PRICE_TABLE_VERSION == "2026-09-12"
    usage = RuntimeUsage(
        input_tokens=1_000_000,
        output_tokens=1_000_000,
        cache_read_tokens=1_000_000,
        cache_write_tokens=1_000_000,
    )
    assert estimated_cost_usd("claude-fable-5-1", usage) == 36.75


def test_price_is_unknown_without_tokens_or_a_pinned_model():
    assert estimated_cost_usd("claude-fable-5-1", None) is None
    assert estimated_cost_usd("claude-fable-5-1", RuntimeUsage()) is None
    assert estimated_cost_usd(
        "unlisted-model", RuntimeUsage(input_tokens=1, output_tokens=1)
    ) is None
    assert estimated_cost_usd(
        "gpt-5.6-luna", RuntimeUsage(input_tokens=0, output_tokens=0)
    ) is None
    assert estimated_cost_usd(
        "gpt-5.6-luna", RuntimeUsage(input_tokens=100, output_tokens=0)
    ) is None


def test_codex_cached_input_is_not_billed_twice():
    usage = RuntimeUsage(
        input_tokens=17,
        output_tokens=9,
        cache_read_tokens=5,
    )
    # Codex reports cached input inside input_tokens: 12 uncached input tokens,
    # five cached input tokens, and nine output tokens.
    expected = round((12 * 0.25 + 5 * 0.025 + 9 * 2.0) / 1_000_000, 9)
    assert estimated_cost_usd("gpt-5.6-luna", usage) == expected
    assert total_tokens(usage, model="gpt-5.6-luna") == 26


@pytest.mark.parametrize(
    "vendor_cost",
    [None, True, False, float("nan"), float("inf"), -0.01, 1_000_000.01],
)
def test_invalid_or_absent_vendor_cost_falls_back_to_estimate(vendor_cost):
    usage = RuntimeUsage(input_tokens=1, output_tokens=1)

    cost, source = resolved_cost_usd("gpt-5.6-luna", usage, vendor_cost)

    assert cost == estimated_cost_usd("gpt-5.6-luna", usage)
    assert source is RuntimeCostSource.ESTIMATED


def test_unknown_cost_is_none_and_never_synthesized_as_zero():
    cost, source = resolved_cost_usd(
        "unlisted-model",
        RuntimeUsage(input_tokens=100, output_tokens=0),
        None,
    )

    assert cost is None
    assert source is RuntimeCostSource.UNKNOWN
