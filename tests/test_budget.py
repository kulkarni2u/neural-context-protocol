"""CAP-E2: per-pipeline cost governor unit tests."""

from __future__ import annotations

import pytest

from ncp.budget import evaluate_budget


def test_evaluate_budget_ok_below_warn_fraction() -> None:
    snapshot = evaluate_budget(spent_usd=1.0, budget_usd=10.0, warn_fraction=0.8)

    assert snapshot.status == "ok"
    assert snapshot.fraction_used == pytest.approx(0.1)
    assert snapshot.to_dict() == {
        "spent_usd": 1.0,
        "budget_usd": 10.0,
        "fraction_used": 0.1,
        "status": "ok",
    }


def test_evaluate_budget_warns_at_configured_fraction() -> None:
    below = evaluate_budget(spent_usd=7.99, budget_usd=10.0, warn_fraction=0.8)
    assert below.status == "ok"

    at_threshold = evaluate_budget(spent_usd=8.0, budget_usd=10.0, warn_fraction=0.8)
    assert at_threshold.status == "warning"
    assert at_threshold.fraction_used == pytest.approx(0.8)

    above = evaluate_budget(spent_usd=9.5, budget_usd=10.0, warn_fraction=0.8)
    assert above.status == "warning"


def test_evaluate_budget_exceeded_at_or_above_ceiling() -> None:
    at_ceiling = evaluate_budget(spent_usd=10.0, budget_usd=10.0, warn_fraction=0.8)
    assert at_ceiling.status == "exceeded"
    assert at_ceiling.fraction_used == pytest.approx(1.0)

    over_ceiling = evaluate_budget(spent_usd=15.0, budget_usd=10.0, warn_fraction=0.8)
    assert over_ceiling.status == "exceeded"
    assert over_ceiling.fraction_used == pytest.approx(1.5)


def test_evaluate_budget_zero_spend_is_ok() -> None:
    snapshot = evaluate_budget(spent_usd=0.0, budget_usd=10.0, warn_fraction=0.8)
    assert snapshot.status == "ok"
    assert snapshot.fraction_used == 0.0


def test_evaluate_budget_non_positive_ceiling_treated_as_exhausted() -> None:
    zero_ceiling_no_spend = evaluate_budget(spent_usd=0.0, budget_usd=0.0, warn_fraction=0.8)
    assert zero_ceiling_no_spend.status == "ok"
    assert zero_ceiling_no_spend.fraction_used == 0.0

    zero_ceiling_any_spend = evaluate_budget(spent_usd=0.01, budget_usd=0.0, warn_fraction=0.8)
    assert zero_ceiling_any_spend.status == "exceeded"
    assert zero_ceiling_any_spend.fraction_used == 1.0

    negative_ceiling = evaluate_budget(spent_usd=0.01, budget_usd=-5.0, warn_fraction=0.8)
    assert negative_ceiling.status == "exceeded"


def test_evaluate_budget_is_deterministic() -> None:
    first = evaluate_budget(spent_usd=4.2, budget_usd=10.0, warn_fraction=0.8)
    second = evaluate_budget(spent_usd=4.2, budget_usd=10.0, warn_fraction=0.8)
    assert first.to_dict() == second.to_dict()
