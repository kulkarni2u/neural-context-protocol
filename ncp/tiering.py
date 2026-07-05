"""CAP-E3: model-tiering advisory signal.

NCP does not route models itself. It computes a small, deterministic
"complexity" heuristic from inputs that are already observable at context
assembly time, and exposes it (with the raw inputs) so an external
orchestrator can decide whether a turn is safe to route to a cheaper model.

Formula (normative — see docs/NCP_PROTOCOL_SPEC.md §4c)
---------------------------------------------------------------------------
Every raw input is normalized to ``[0.0, 1.0]``:

  query_length_norm = min(1.0, len(query_text) / 240)
      240 chars is a fixed reference for a "long" turn query; longer queries
      tend to encode more nuance/constraints.

  diversity_ratio = distinct_authors / chunk_count   (0.0 if chunk_count == 0)
      Fraction of retrieved chunks that come from distinct authors. A high
      ratio means the assembled context synthesizes many independent
      viewpoints, which is harder to reason over than one coherent source.

  pressure_norm = {"low": 0.0, "medium": 0.33, "high": 0.66, "critical": 1.0}
      Read directly from the assembler's ``BudgetContext.pressure``.

  cold_start_flag = 1.0 if this turn hit the assembler's cold-start bootstrap
      (no retrieved chunks; a synthetic pipeline_summary chunk was
      substituted) else 0.0. Cold start means there is no working memory to
      lean on, so the turn is inherently harder.

  drift = ConsciousBlock.drift_score, already in [0.0, 1.0].

The blend (weights sum to 1.0):

  complexity_signal = 0.25 * query_length_norm
                     + 0.25 * drift
                     + 0.25 * pressure_norm
                     + 0.15 * diversity_ratio
                     + 0.10 * cold_start_flag

tier_hint thresholds:

  complexity_signal <  0.35              -> "light"
  0.35 <= complexity_signal < 0.65        -> "standard"
  complexity_signal >= 0.65               -> "deep"

The function is a pure, deterministic mapping from its arguments: same
inputs always produce the same signal and hint.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

TierHint = Literal["light", "standard", "deep"]

# Fixed, documented normalization constants -- not tunable config, so the
# signal stays deterministic and reproducible across runs/versions.
_QUERY_LENGTH_REFERENCE_CHARS = 240.0

_PRESSURE_SCORE: dict[str, float] = {
    "low": 0.0,
    "medium": 0.33,
    "high": 0.66,
    "critical": 1.0,
}

# Blend weights. Sum to 1.0.
_WEIGHT_QUERY_LENGTH = 0.25
_WEIGHT_DRIFT = 0.25
_WEIGHT_PRESSURE = 0.25
_WEIGHT_DIVERSITY = 0.15
_WEIGHT_COLD_START = 0.10

_LIGHT_MAX = 0.35
_DEEP_MIN = 0.65


@dataclass(slots=True)
class TierSignal:
    """The CAP-E3 advisory signal for one ``ncp_get_context`` call."""

    tier_hint: TierHint
    complexity_signal: float
    factors: dict[str, object]

    def to_dict(self) -> dict[str, object]:
        return {
            "tier_hint": self.tier_hint,
            "complexity_signal": round(self.complexity_signal, 4),
            "factors": self.factors,
        }


def compute_tier_signal(
    *,
    query_text: str,
    chunk_count: int,
    distinct_authors: int,
    drift_score: float,
    pressure: str,
    cold_start: bool,
) -> TierSignal:
    """Compute the CAP-E3 ``tier_hint`` / ``complexity_signal`` / ``factors``.

    See the module docstring for the exact formula. All inputs are values
    already produced by one ``Assembler.assemble`` call -- this function does
    no I/O and makes no model calls.
    """

    query_length_norm = min(1.0, len(query_text) / _QUERY_LENGTH_REFERENCE_CHARS)
    diversity_ratio = 0.0 if chunk_count <= 0 else min(1.0, distinct_authors / chunk_count)
    pressure_norm = _PRESSURE_SCORE.get(pressure, 0.0)
    cold_start_flag = 1.0 if cold_start else 0.0
    drift = min(1.0, max(0.0, drift_score))

    complexity_signal = (
        _WEIGHT_QUERY_LENGTH * query_length_norm
        + _WEIGHT_DRIFT * drift
        + _WEIGHT_PRESSURE * pressure_norm
        + _WEIGHT_DIVERSITY * diversity_ratio
        + _WEIGHT_COLD_START * cold_start_flag
    )
    complexity_signal = min(1.0, max(0.0, complexity_signal))

    if complexity_signal < _LIGHT_MAX:
        tier_hint: TierHint = "light"
    elif complexity_signal >= _DEEP_MIN:
        tier_hint = "deep"
    else:
        tier_hint = "standard"

    factors = {
        "query_length_chars": len(query_text),
        "query_length_norm": round(query_length_norm, 4),
        "chunk_count": chunk_count,
        "distinct_authors": distinct_authors,
        "diversity_ratio": round(diversity_ratio, 4),
        "drift_score": round(drift, 4),
        "pressure": pressure,
        "pressure_norm": round(pressure_norm, 4),
        "cold_start": cold_start,
    }

    return TierSignal(tier_hint=tier_hint, complexity_signal=complexity_signal, factors=factors)
