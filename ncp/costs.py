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


@dataclass(slots=True)
class AssemblyOverheadBreakdown:
    """Heuristic overhead estimate for one benchmarked NCP assembly path."""

    embed_token_cost_usd: float
    retrieval_cost_usd: float
    whisper_cost_usd: float
    reference_input_cost_per_token_usd: float

    @property
    def total_cost_usd(self) -> float:
        return self.embed_token_cost_usd + self.retrieval_cost_usd + self.whisper_cost_usd

    @property
    def token_equivalent(self) -> float:
        if self.reference_input_cost_per_token_usd <= 0:
            return 0.0
        return self.total_cost_usd / self.reference_input_cost_per_token_usd


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


def assembly_overhead(
    *,
    embed_tokens: int = 0,
    retrieval_ops: int = 0,
    whisper_writes: int = 0,
    pricing: dict[str, dict[str, float]] | None = None,
    reference_model: str = "gpt-4o-mini",
    embedding_unit_price: float = 0.02,
    retrieval_op_usd: float = 0.000002,
    whisper_write_usd: float = 0.0000005,
) -> AssemblyOverheadBreakdown:
    """Estimate assembly overhead with explicit heuristic fixed-cost terms."""

    pricing_table = DEFAULT_PRICING if pricing is None else pricing
    model_pricing = pricing_table.get(reference_model)
    if model_pricing is None:
        raise KeyError(f"No pricing configured for model '{reference_model}'")

    reference_input_cost_per_token_usd = float(model_pricing["input"]) / 1_000_000
    return AssemblyOverheadBreakdown(
        embed_token_cost_usd=_per_million_cost(embed_tokens, embedding_unit_price),
        retrieval_cost_usd=retrieval_ops * retrieval_op_usd,
        whisper_cost_usd=whisper_writes * whisper_write_usd,
        reference_input_cost_per_token_usd=reference_input_cost_per_token_usd,
    )
