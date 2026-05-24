import pytest

from ncp.costs import calculate_cost


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


def test_calculate_cost_requires_known_model() -> None:
    with pytest.raises(KeyError, match="No pricing configured"):
        calculate_cost(model="missing-model", input_tokens=1, output_tokens=1)
