"""CAP-C6: adaptive per-turn context token budget.

Today the assembly token budget (``[budget].context_token_budget``, or an
explicit ``max_tokens`` argument) is a single static number for every call.
This module computes an *adjusted* budget from cheap, already-observable
signals so easy turns spend fewer tokens and hard/contested turns are allowed
to spend more -- inside a hard ``[floor, ceiling]`` range that never moves.

This is intentionally not ML and not a black box: every input below is a
value the caller already has in hand *before* assembly runs (so it can be
used to size the budget passed *into* ``Assembler.assemble``), and the
formula is closed-form arithmetic you can recompute by hand from the
``reason_factors`` block returned alongside it.

Inputs (all read at the top of ``ncp_get_context``, before retrieval runs)
---------------------------------------------------------------------------
  query_text    -- the turn's task+slot search text. Reuses the same
                    ``query_length_norm`` normalization CAP-E3
                    (``ncp/tiering.py``) uses for its post-assembly
                    ``complexity_signal``: longer queries tend to encode more
                    nuance/constraints.

  drift_score   -- ``ConsciousBlock.drift_score``, already in [0.0, 1.0].
                    Reused as-is (CAP-E3 uses the same field) rather than
                    recomputed: high drift means the working context is
                    actively shifting and benefits from more room.

  pressure      -- ``BudgetContext.pressure`` ("low"/"medium"/"high"/
                    "critical"), the same field and value table CAP-E3 reads.
                    High pressure means the pipeline is already deep into its
                    context-usage budget for this run.

  slot_age      -- ``ConsciousBlock.slot_age``, the number of turns the
                    current slot has persisted (hydrated from the store, so
                    free of new state). Stands in for "recent turn cadence":
                    a slot that has been worked for many turns in a row is a
                    sustained, non-trivial thread; a brand new slot
                    (``slot_age == 0``) is a fresh, usually simpler ask.

  dollar_budget_fraction_used -- optional CAP-E2 ``BudgetSnapshot.fraction_used``
                    (``ncp/budget.py``) when a ``[budget].pipeline_budget_usd``
                    ceiling is configured; ``None`` when the $ governor is off,
                    which contributes zero economic pressure.

Formula (normative -- see docs/NCP_PROTOCOL_SPEC.md §4d)
---------------------------------------------------------------------------
Every raw input is normalized to ``[0.0, 1.0]``:

  query_length_norm = min(1.0, len(query_text) / 240)       # same reference as CAP-E3
  drift              = min(1.0, max(0.0, drift_score))
  pressure_norm      = {"low": 0.0, "medium": 0.33, "high": 0.66, "critical": 1.0}[pressure]
  cadence_norm       = min(1.0, slot_age / 10)               # 10 turns on one slot == "sustained"

The four normalized inputs blend into a task-difficulty signal (weights sum to 1.0):

  task_signal = 0.30 * query_length_norm
              + 0.30 * drift
              + 0.25 * pressure_norm
              + 0.15 * cadence_norm

``task_signal`` is mapped onto a multiplicative scale centered on 1.0x the
requested (default) budget -- 0.5x at the easiest possible turn, 1.5x at the
hardest:

  scale = 0.5 + task_signal                                  # in [0.5, 1.5]

Separately, $ budget pressure (an economic constraint, not a task-difficulty
one) pulls the scale back down -- up to 50% at a fully exhausted $ ceiling:

  dollar_pressure = 0.0 if dollar_budget_fraction_used is None
                    else min(1.0, max(0.0, dollar_budget_fraction_used))
  conserved_scale = scale * (1.0 - 0.5 * dollar_pressure)

The adjusted token budget is the requested budget scaled and clamped to the
configured hard range:

  adjusted_tokens = round(requested_tokens * conserved_scale)
  adjusted_tokens = min(ceiling_tokens, max(floor_tokens, adjusted_tokens))

The function is a pure, deterministic mapping from its arguments: the same
inputs always produce the same adjusted budget.
"""

from __future__ import annotations

from dataclasses import dataclass

# Fixed, documented normalization constants -- not tunable config, so the
# result stays deterministic and reproducible across runs/versions.
_QUERY_LENGTH_REFERENCE_CHARS = 240.0
_SLOT_AGE_REFERENCE_TURNS = 10.0

_PRESSURE_SCORE: dict[str, float] = {
    "low": 0.0,
    "medium": 0.33,
    "high": 0.66,
    "critical": 1.0,
}

# task_signal blend weights. Sum to 1.0.
_WEIGHT_QUERY_LENGTH = 0.30
_WEIGHT_DRIFT = 0.30
_WEIGHT_PRESSURE = 0.25
_WEIGHT_CADENCE = 0.15

_SCALE_BASE = 0.5  # task_signal == 0.0 -> 0.5x requested budget
_DOLLAR_PRESSURE_MAX_CUT = 0.5  # dollar_pressure == 1.0 -> up to 50% further cut


@dataclass(slots=True)
class AdaptiveBudgetResult:
    """The CAP-C6 adjusted token budget for one ``ncp_get_context`` call."""

    requested_tokens: int
    adjusted_tokens: int
    floor_tokens: int
    ceiling_tokens: int
    reason_factors: dict[str, object]

    def to_dict(self) -> dict[str, object]:
        return {
            "requested": self.requested_tokens,
            "adjusted": self.adjusted_tokens,
            "reason_factors": self.reason_factors,
        }


def compute_adaptive_budget(
    *,
    requested_tokens: int,
    floor_tokens: int,
    ceiling_tokens: int,
    query_text: str,
    drift_score: float,
    pressure: str,
    slot_age: int,
    dollar_budget_fraction_used: float | None = None,
) -> AdaptiveBudgetResult:
    """Compute the CAP-C6 adjusted token budget. See module docstring for the formula.

    ``requested_tokens`` is what would have been used without adaptation
    (typically ``[budget].context_token_budget``). The result is always
    clamped to ``[floor_tokens, ceiling_tokens]`` regardless of inputs; a
    ``ceiling_tokens`` below ``floor_tokens`` is treated as equal to
    ``floor_tokens`` (the floor always wins) so the range is never inverted.
    """

    safe_floor = max(0, int(floor_tokens))
    safe_ceiling = max(safe_floor, int(ceiling_tokens))

    query_length_norm = min(1.0, len(query_text) / _QUERY_LENGTH_REFERENCE_CHARS)
    drift = min(1.0, max(0.0, drift_score))
    pressure_norm = _PRESSURE_SCORE.get(pressure, 0.0)
    cadence_norm = min(1.0, max(0.0, slot_age) / _SLOT_AGE_REFERENCE_TURNS)

    task_signal = (
        _WEIGHT_QUERY_LENGTH * query_length_norm
        + _WEIGHT_DRIFT * drift
        + _WEIGHT_PRESSURE * pressure_norm
        + _WEIGHT_CADENCE * cadence_norm
    )
    task_signal = min(1.0, max(0.0, task_signal))

    scale = _SCALE_BASE + task_signal

    dollar_pressure = (
        0.0
        if dollar_budget_fraction_used is None
        else min(1.0, max(0.0, dollar_budget_fraction_used))
    )
    conserved_scale = scale * (1.0 - _DOLLAR_PRESSURE_MAX_CUT * dollar_pressure)

    safe_requested = max(0, int(requested_tokens))
    raw_adjusted = round(safe_requested * conserved_scale)
    adjusted_tokens = min(safe_ceiling, max(safe_floor, raw_adjusted))

    reason_factors = {
        "query_length_chars": len(query_text),
        "query_length_norm": round(query_length_norm, 4),
        "drift_score": round(drift, 4),
        "pressure": pressure,
        "pressure_norm": round(pressure_norm, 4),
        "slot_age": slot_age,
        "cadence_norm": round(cadence_norm, 4),
        "task_signal": round(task_signal, 4),
        "scale": round(scale, 4),
        "dollar_budget_fraction_used": (
            None if dollar_budget_fraction_used is None else round(dollar_pressure, 4)
        ),
        "conserved_scale": round(conserved_scale, 4),
        "floor_tokens": safe_floor,
        "ceiling_tokens": safe_ceiling,
    }

    return AdaptiveBudgetResult(
        requested_tokens=safe_requested,
        adjusted_tokens=adjusted_tokens,
        floor_tokens=safe_floor,
        ceiling_tokens=safe_ceiling,
        reason_factors=reason_factors,
    )
