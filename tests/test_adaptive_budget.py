"""CAP-C6: adaptive per-turn context token budget unit tests."""

from __future__ import annotations

import pytest

from ncp.adaptive_budget import compute_adaptive_budget


def _budget(**overrides: object):
    defaults: dict[str, object] = {
        "requested_tokens": 840,
        "floor_tokens": 300,
        "ceiling_tokens": 2000,
        "query_text": "short",
        "drift_score": 0.0,
        "pressure": "low",
        "slot_age": 0,
        "dollar_budget_fraction_used": None,
    }
    defaults.update(overrides)
    return compute_adaptive_budget(**defaults)  # type: ignore[arg-type]


def test_is_deterministic() -> None:
    first = _budget(query_text="rebuild the auth pipeline", drift_score=0.4, pressure="high", slot_age=5)
    second = _budget(query_text="rebuild the auth pipeline", drift_score=0.4, pressure="high", slot_age=5)
    assert first.to_dict() == second.to_dict()


def test_easiest_turn_scales_toward_half_of_requested() -> None:
    result = _budget(query_text="", drift_score=0.0, pressure="low", slot_age=0)
    assert result.adjusted_tokens == round(840 * 0.5)
    assert result.reason_factors["task_signal"] == pytest.approx(0.0, abs=1e-6)


def test_hardest_turn_scales_toward_one_and_a_half_times_requested() -> None:
    long_query = "x" * 400
    result = _budget(query_text=long_query, drift_score=1.0, pressure="critical", slot_age=50)
    assert result.reason_factors["task_signal"] == pytest.approx(1.0)
    assert result.adjusted_tokens == round(840 * 1.5)


def test_dollar_budget_pressure_pulls_adjusted_down() -> None:
    long_query = "x" * 400
    no_dollar_pressure = _budget(query_text=long_query, drift_score=1.0, pressure="critical", slot_age=50, dollar_budget_fraction_used=None)
    full_dollar_pressure = _budget(query_text=long_query, drift_score=1.0, pressure="critical", slot_age=50, dollar_budget_fraction_used=1.0)

    assert full_dollar_pressure.adjusted_tokens < no_dollar_pressure.adjusted_tokens
    assert full_dollar_pressure.adjusted_tokens == round(no_dollar_pressure.adjusted_tokens * 0.5)


def test_dollar_budget_fraction_used_is_clamped_to_unit_interval() -> None:
    over_budget = _budget(dollar_budget_fraction_used=5.0)
    negative = _budget(dollar_budget_fraction_used=-5.0)
    zero = _budget(dollar_budget_fraction_used=0.0)

    assert over_budget.reason_factors["dollar_budget_fraction_used"] == pytest.approx(1.0)
    assert negative.reason_factors["dollar_budget_fraction_used"] == pytest.approx(0.0)
    assert negative.adjusted_tokens == zero.adjusted_tokens


def test_result_always_within_floor_and_ceiling() -> None:
    below_floor = _budget(requested_tokens=1, query_text="", drift_score=0.0, pressure="low", slot_age=0)
    assert below_floor.adjusted_tokens == 300

    above_ceiling = _budget(requested_tokens=100_000, query_text="x" * 10_000, drift_score=1.0, pressure="critical", slot_age=1000)
    assert above_ceiling.adjusted_tokens == 2000

    for query_len in (0, 50, 240, 1000, 5000):
        for drift in (0.0, 0.25, 0.5, 0.75, 1.0):
            for pressure in ("low", "medium", "high", "critical"):
                for slot_age in (0, 3, 10, 40):
                    for dollar in (None, 0.0, 0.5, 1.0, 2.0):
                        result = _budget(
                            query_text="x" * query_len,
                            drift_score=drift,
                            pressure=pressure,
                            slot_age=slot_age,
                            dollar_budget_fraction_used=dollar,
                        )
                        assert 300 <= result.adjusted_tokens <= 2000


def test_ceiling_below_floor_is_treated_as_equal_to_floor() -> None:
    result = _budget(floor_tokens=500, ceiling_tokens=100)
    assert result.floor_tokens == 500
    assert result.ceiling_tokens == 500
    assert result.adjusted_tokens == 500


def test_unknown_pressure_defaults_to_zero_norm() -> None:
    result = _budget(pressure="nonsense")
    assert result.reason_factors["pressure_norm"] == 0.0


def test_to_dict_shape() -> None:
    result = _budget(query_text="hello world", drift_score=0.25, pressure="high", slot_age=2)
    payload = result.to_dict()
    assert set(payload) == {"requested", "adjusted", "reason_factors"}
    assert payload["requested"] == 840
    factors = payload["reason_factors"]
    assert factors["query_length_chars"] == len("hello world")
    assert factors["pressure"] == "high"
    assert factors["slot_age"] == 2
