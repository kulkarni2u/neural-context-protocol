import pytest

from ncp.costs import assembly_overhead, calculate_cost


def test_calculate_cost_uses_default_pricing() -> None:
    breakdown = calculate_cost(
        model="gpt-4o-mini",
        input_tokens=1_000_000,
        output_tokens=500_000,
        cache_read_tokens=100_000,
    )

    assert breakdown.input_cost_usd == pytest.approx(0.15)
    assert breakdown.output_cost_usd == pytest.approx(0.30)
    assert breakdown.cache_read_cost_usd == pytest.approx(0.0075)
    assert breakdown.total_cost_usd == pytest.approx(0.4575)


def test_calculate_cost_supports_pricing_override() -> None:
    breakdown = calculate_cost(
        model="custom-model",
        input_tokens=200_000,
        output_tokens=100_000,
        pricing={"custom-model": {"input": 2.0, "output": 4.0, "cache_read": 1.0}},
    )

    assert breakdown.total_cost_usd == pytest.approx(0.8)


def test_calculate_cost_prices_cache_write_tokens() -> None:
    breakdown = calculate_cost(
        model="claude-sonnet-4-20250514",
        input_tokens=1_000_000,
        output_tokens=0,
        cache_write_tokens=1_000_000,
    )

    # 1.25x the model's base input price ($3.00) for the default 5-minute
    # ephemeral cache TTL -- see the pricing-table comment in ncp/config.py.
    assert breakdown.cache_write_cost_usd == pytest.approx(3.75)
    assert breakdown.total_cost_usd == pytest.approx(3.00 + 3.75)


def test_calculate_cost_defaults_missing_cache_write_pricing_to_zero() -> None:
    # A pricing entry with no ``cache_write`` key (e.g. a provider with no
    # cache-write premium, like the real gpt-4o entries) must not KeyError,
    # and must price cache_write_tokens as 0 rather than fail.
    breakdown = calculate_cost(
        model="gpt-4o-mini",
        input_tokens=0,
        output_tokens=0,
        cache_write_tokens=500_000,
    )

    assert breakdown.cache_write_cost_usd == pytest.approx(0.0)
    assert breakdown.total_cost_usd == pytest.approx(0.0)


def test_calculate_cost_requires_known_model() -> None:
    with pytest.raises(KeyError, match="No pricing configured"):
        calculate_cost(model="missing-model", input_tokens=1, output_tokens=1)


def test_assembly_overhead_reports_cost_and_token_equivalent() -> None:
    overhead = assembly_overhead(
        embed_tokens=1000,
        retrieval_ops=10,
        whisper_writes=4,
        reference_model="gpt-4o-mini",
    )

    assert overhead.total_cost_usd > 0.0
    assert overhead.token_equivalent > 0.0


def test_assembly_overhead_embed_cost_uses_per_million_pricing() -> None:
    overhead = assembly_overhead(
        embed_tokens=1_000_000,
        retrieval_ops=0,
        whisper_writes=0,
        reference_model="gpt-4o-mini",
        embedding_unit_price=0.02,
    )

    assert overhead.embed_token_cost_usd == pytest.approx(0.02)
    assert overhead.retrieval_cost_usd == pytest.approx(0.0)
    assert overhead.total_cost_usd == pytest.approx(0.02)


def test_assembly_overhead_zero_inputs_produces_zero_cost() -> None:
    overhead = assembly_overhead(reference_model="gpt-4o-mini")

    assert overhead.total_cost_usd == 0.0
    assert overhead.token_equivalent == 0.0


def test_assembly_overhead_requires_known_reference_model() -> None:
    with pytest.raises(KeyError, match="No pricing configured"):
        assembly_overhead(reference_model="missing-model")


def test_assembly_overhead_token_equivalent_handles_zero_reference_price() -> None:
    overhead = assembly_overhead(
        embed_tokens=1000,
        retrieval_ops=10,
        whisper_writes=4,
        reference_model="free-model",
        pricing={"free-model": {"input": 0.0, "output": 0.0, "cache_read": 0.0}},
    )

    assert overhead.total_cost_usd > 0.0
    assert overhead.token_equivalent == 0.0
