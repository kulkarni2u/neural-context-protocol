"""CAP-E2: per-pipeline budget governance over real (CAP-E1) cost accounting.

This module never fabricates a number. ``spent_usd`` is always read back from
``store.cost_summary()``, which aggregates the ``cost_log`` table populated by
``Assembler.post_turn`` / ``BaseStore.log_cost``. Those rows are themselves
priced only from real provider usage (``cost_source == "measured"``) or left
at ``0.0`` for the local/mock estimate path (``cost_source == "estimated"``,
see ``ncp/api.py::_build_response``). The governor therefore only ever
enforces against money that was actually billed.

This module is a pure classifier: given a recorded spend and a configured
ceiling, it says whether the pipeline is "ok", "warning", or "exceeded", and
callers (``ncp/mcp/server.py``) decide what to do with that (surface a
telemetry block, or refuse assembly in ``block`` mode).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

BudgetStatus = Literal["ok", "warning", "exceeded"]
BudgetEnforcement = Literal["off", "warn", "block"]


@dataclass(slots=True)
class BudgetSnapshot:
    """One classified spend-vs-ceiling reading for a pipeline."""

    spent_usd: float
    budget_usd: float
    fraction_used: float
    status: BudgetStatus

    def to_dict(self) -> dict[str, object]:
        return {
            "spent_usd": round(self.spent_usd, 6),
            "budget_usd": self.budget_usd,
            "fraction_used": round(self.fraction_used, 4),
            "status": self.status,
        }


def evaluate_budget(
    *,
    spent_usd: float,
    budget_usd: float,
    warn_fraction: float,
) -> BudgetSnapshot:
    """Classify recorded spend against a configured pipeline budget ceiling.

    ``fraction_used = spent_usd / budget_usd`` (a non-positive ``budget_usd``
    is treated as an already-exhausted ceiling: any recorded spend is
    "exceeded", zero spend is "ok").

    status:
      - "exceeded" when ``spent_usd >= budget_usd``
      - "warning"  when ``fraction_used >= warn_fraction``
      - "ok"       otherwise
    """

    safe_budget = max(0.0, budget_usd)
    if safe_budget <= 0.0:
        fraction_used = 1.0 if spent_usd > 0.0 else 0.0
        exceeded = spent_usd > 0.0
    else:
        fraction_used = spent_usd / safe_budget
        exceeded = spent_usd >= safe_budget

    if exceeded:
        status: BudgetStatus = "exceeded"
    elif fraction_used >= warn_fraction:
        status = "warning"
    else:
        status = "ok"

    return BudgetSnapshot(
        spent_usd=spent_usd,
        budget_usd=budget_usd,
        fraction_used=fraction_used,
        status=status,
    )
