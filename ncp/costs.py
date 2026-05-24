"""Pricing and cost calculation helpers."""

from __future__ import annotations

from dataclasses import dataclass

from ncp.config import DEFAULT_CONFIG


DEFAULT_PRICING = DEFAULT_CONFIG["providers"]["pricing"]


@dataclass(slots=True)
class CostBreakdown:
    """Normalized cost output for one model call."""

    model: str
    input_cost_usd: float
    output_cost_usd: float
    cache_read_cost_usd: float

    @property
    def total_cost_usd(self) -> float:
        return self.input_cost_usd + self.output_cost_usd + self.cache_read_cost_usd


def calculate_cost(
    *,
    model: str,
    input_tokens: int,
    output_tokens: int,
    cache_read_tokens: int = 0,
    pricing: dict[str, dict[str, float]] | None = None,
) -> CostBreakdown:
    """Calculate token costs from the configured per-million-token pricing table."""

    pricing_table = DEFAULT_PRICING if pricing is None else pricing
    model_pricing = pricing_table.get(model)
    if model_pricing is None:
        raise KeyError(f"No pricing configured for model '{model}'")

    return CostBreakdown(
        model=model,
        input_cost_usd=_per_million_cost(input_tokens, float(model_pricing["input"])),
        output_cost_usd=_per_million_cost(output_tokens, float(model_pricing["output"])),
        cache_read_cost_usd=_per_million_cost(cache_read_tokens, float(model_pricing["cache_read"])),
    )


def _per_million_cost(tokens: int, unit_price: float) -> float:
    return (tokens / 1_000_000) * unit_price
