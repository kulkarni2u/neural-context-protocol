"""CAP-E3: model-tiering advisory signal unit tests."""

from __future__ import annotations

import pytest

from ncp.tiering import compute_tier_signal


def _signal(**overrides: object):
    defaults: dict[str, object] = {
        "query_text": "short",
        "chunk_count": 2,
        "distinct_authors": 1,
        "drift_score": 0.0,
        "pressure": "low",
        "cold_start": False,
    }
    defaults.update(overrides)
    return compute_tier_signal(**defaults)  # type: ignore[arg-type]


def test_compute_tier_signal_is_deterministic() -> None:
    first = _signal(query_text="rebuild the auth pipeline", chunk_count=4, distinct_authors=3, drift_score=0.4, pressure="high", cold_start=False)
    second = _signal(query_text="rebuild the auth pipeline", chunk_count=4, distinct_authors=3, drift_score=0.4, pressure="high", cold_start=False)

    assert first.to_dict() == second.to_dict()


def test_low_signal_inputs_produce_light_hint() -> None:
    signal = _signal(query_text="ok", chunk_count=1, distinct_authors=1, drift_score=0.0, pressure="low", cold_start=False)

    assert signal.tier_hint == "light"
    assert signal.complexity_signal < 0.35


def test_high_signal_inputs_produce_deep_hint() -> None:
    long_query = "x" * 400
    signal = _signal(query_text=long_query, chunk_count=4, distinct_authors=4, drift_score=1.0, pressure="critical", cold_start=True)

    assert signal.tier_hint == "deep"
    assert signal.complexity_signal >= 0.65
    assert signal.complexity_signal <= 1.0


def test_mid_range_signal_produces_standard_hint() -> None:
    signal = _signal(
        query_text="moderate length query about a decision",
        chunk_count=2,
        distinct_authors=2,
        drift_score=0.5,
        pressure="high",
    )

    assert signal.tier_hint == "standard"
    assert 0.35 <= signal.complexity_signal < 0.65


def test_factors_expose_every_raw_input() -> None:
    signal = _signal(query_text="hello world", chunk_count=3, distinct_authors=2, drift_score=0.25, pressure="high", cold_start=False)

    factors = signal.factors
    assert factors["query_length_chars"] == len("hello world")
    assert factors["chunk_count"] == 3
    assert factors["distinct_authors"] == 2
    assert factors["diversity_ratio"] == pytest.approx(2 / 3, rel=1e-3)
    assert factors["drift_score"] == pytest.approx(0.25)
    assert factors["pressure"] == "high"
    assert factors["pressure_norm"] == pytest.approx(0.66)
    assert factors["cold_start"] is False


def test_diversity_ratio_zero_when_no_chunks() -> None:
    signal = _signal(chunk_count=0, distinct_authors=0)
    assert signal.factors["diversity_ratio"] == 0.0


def test_complexity_signal_bounded_to_unit_interval() -> None:
    signal = _signal(query_text="x" * 10_000, chunk_count=10, distinct_authors=10, drift_score=5.0, pressure="critical", cold_start=True)
    assert 0.0 <= signal.complexity_signal <= 1.0


def test_cold_start_flag_increases_complexity_signal() -> None:
    without_cold_start = _signal(cold_start=False)
    with_cold_start = _signal(cold_start=True)

    assert with_cold_start.complexity_signal > without_cold_start.complexity_signal
    assert with_cold_start.factors["cold_start"] is True
