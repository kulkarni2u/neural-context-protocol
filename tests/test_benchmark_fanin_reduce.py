from __future__ import annotations

import tempfile
from pathlib import Path

from ncp.benchmarks import run_fanin_reduce_benchmark

_VALID_TOKEN_UNITS = {"tiktoken/cl100k_base", "chars_div4"}


def _run(**overrides: object) -> dict[str, object]:
    with tempfile.TemporaryDirectory(prefix="ncp-fanin-bench-") as tmpdir:
        return run_fanin_reduce_benchmark(store_path=Path(tmpdir) / "bench.db", **overrides)


def test_fanin_reduce_benchmark_artifact_structure() -> None:
    artifact = _run()

    assert artifact["benchmark"] == "fanin_reduce"
    assert artifact["token_unit"] in _VALID_TOKEN_UNITS
    assert artifact["config"]["workers"] == 40
    assert len(artifact["config"]["topics"]) == 4

    scenarios = artifact["scenarios"]
    assert {"raw_dump", "ncp_default", "ncp_reduced"} <= set(scenarios)
    for name in ("raw_dump", "ncp_default", "ncp_reduced"):
        row = scenarios[name]
        assert row["input_tokens"] > 0
        assert row["cost_usd"] > 0
        assert row["chunk_count"] > 0

    summary = artifact["summary"]
    required_summary_keys = {
        "token_reduction_vs_raw_dump",
        "cost_reduction_usd_vs_raw_dump",
        "wasted_duplicate_slots_in_default",
        "wasted_duplicate_slot_fraction_in_default",
        "contradictions_surfaced",
        "contradiction_same_topic_fraction",
        "merged_near_duplicates",
        "malformed_dropped",
        "pass",
    }
    assert required_summary_keys <= set(summary)


def test_fanin_reduce_benchmark_passes_gate() -> None:
    artifact = _run()
    summary = artifact["summary"]

    assert summary["pass"] is True
    assert summary["token_reduction_vs_raw_dump"] > 0.0
    assert summary["cost_reduction_usd_vs_raw_dump"] > 0.0
    assert summary["merged_near_duplicates"] > 0


def test_fanin_reduce_benchmark_default_retrieval_wastes_budget_on_duplicates() -> None:
    """The headline claim: without the reducer, some of NCP's own bounded
    top-k retrieval slots are near-duplicates of another slot in the same
    result -- context budget spent twice on one claim."""
    artifact = _run()
    summary = artifact["summary"]

    assert summary["wasted_duplicate_slots_in_default"] > 0
    assert 0.0 < summary["wasted_duplicate_slot_fraction_in_default"] < 1.0


def test_fanin_reduce_benchmark_reduced_and_default_return_same_chunk_count() -> None:
    """ncp_default and ncp_reduced are compared at the same k -- the
    reducer changes *which* chunks fill the budget, not how many."""
    artifact = _run()
    scenarios = artifact["scenarios"]
    assert scenarios["ncp_default"]["chunk_count"] == scenarios["ncp_reduced"]["chunk_count"]
    assert scenarios["ncp_default"]["chunk_count"] == artifact["config"]["k"]


def test_fanin_reduce_benchmark_contradictions_are_not_pure_noise() -> None:
    """Ground-truth-backed honesty check: the contradiction heuristic is
    disclosed as imperfect, but it must still land clearly better than
    chance (1/4, since the corpus has 4 topics) on this corpus."""
    artifact = _run()
    summary = artifact["summary"]
    if summary["contradictions_surfaced"] == 0:
        return
    assert summary["contradiction_same_topic_fraction"] > 0.25


def test_fanin_reduce_benchmark_is_deterministic() -> None:
    first = _run()
    second = _run()
    assert first == second


def test_fanin_reduce_benchmark_workers_must_be_positive() -> None:
    import pytest

    with tempfile.TemporaryDirectory(prefix="ncp-fanin-bench-") as tmpdir:
        with pytest.raises(ValueError):
            run_fanin_reduce_benchmark(store_path=Path(tmpdir) / "bench.db", workers=0)


def test_fanin_reduce_benchmark_scales_with_worker_count() -> None:
    small = _run(workers=8, k=4)
    large = _run(workers=40, k=16)
    assert small["scenarios"]["raw_dump"]["chunk_count"] == 8
    assert large["scenarios"]["raw_dump"]["chunk_count"] == 40
