from __future__ import annotations

from pathlib import Path

import pytest

from benchmarks.needle.run import needle_recall


def test_needle_recall_plants_all_needles_and_reports_bounded_scores(tmp_path: Path) -> None:
    artifact = needle_recall(
        store_path=tmp_path / "needle.db",
        turns=18,
        k_needles=4,
        budget=3,
        pipeline_id="pipe_needle_test",
    )

    assert artifact["benchmark"] == "needle_recall"
    assert len(artifact["needles"]) == 4
    assert len(artifact["recall_curve"]) == 15
    for row in artifact["recall_curve"]:
        assert 0.0 <= float(row["ncp_recall"]) <= 1.0
        assert 0.0 <= float(row["sliding_window_recall"]) <= 1.0


def test_needle_recall_reported_deficit_flag_matches_final_recall(tmp_path: Path) -> None:
    artifact = needle_recall(
        store_path=tmp_path / "needle-deficit.db",
        turns=20,
        k_needles=5,
        budget=4,
        pipeline_id="pipe_needle_deficit",
    )

    final = artifact["summary"]["recall_at_final"]
    ncp_val = float(final["ncp"])
    sliding_val = float(final["sliding_window"])
    expected_deficit = ncp_val < sliding_val
    assert artifact["summary"]["reported_deficit"] is expected_deficit


def test_needle_recall_raises_on_invalid_args(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="turns must be >= 2"):
        needle_recall(store_path=tmp_path / "a.db", turns=1, k_needles=1, budget=1)
    with pytest.raises(ValueError, match="k_needles must be >= 1"):
        needle_recall(store_path=tmp_path / "b.db", turns=5, k_needles=0, budget=1)
    with pytest.raises(ValueError, match="budget must be >= 1"):
        needle_recall(store_path=tmp_path / "c.db", turns=5, k_needles=1, budget=0)
    with pytest.raises(ValueError, match="turns must be > k_needles"):
        needle_recall(store_path=tmp_path / "d.db", turns=4, k_needles=4, budget=1)


def test_needle_recall_first_evicted_turn_keys_match_all_needles(tmp_path: Path) -> None:
    k = 4
    artifact = needle_recall(
        store_path=tmp_path / "needle-keys.db",
        turns=10,
        k_needles=k,
        budget=3,
        pipeline_id="pipe_needle_keys",
    )

    eviction_turns = artifact["summary"]["first_evicted_turn"]
    assert set(eviction_turns.keys()) == {f"needle_{i:02d}" for i in range(1, k + 1)}
    for v in eviction_turns.values():
        assert v is None or isinstance(v, int)
