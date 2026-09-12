from loopzero.runners.contract import RuntimeUsage
from loopzero.runners.pricing import PRICE_TABLE_VERSION, estimated_cost_usd


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
