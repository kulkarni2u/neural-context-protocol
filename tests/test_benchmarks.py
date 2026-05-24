from __future__ import annotations

from pathlib import Path

from ncp.benchmarks import (
    estimate_tokens,
    run_coding_pipeline_benchmark,
    run_research_pipeline_benchmark,
)


def test_estimate_tokens_ignores_empty_whitespace() -> None:
    assert estimate_tokens("") == 0
    assert estimate_tokens("  alpha   beta\n gamma ") == 3


def test_coding_pipeline_benchmark_beats_naive_replay(tmp_path: Path) -> None:
    artifact = run_coding_pipeline_benchmark(
        store_path=tmp_path / "bench.db",
        turns=12,
        pipeline_id="pipe_test_bench",
    )

    assert artifact["benchmark"] == "coding_pipeline"
    assert artifact["turns"] == 12
    assert len(artifact["turn_rows"]) == 12
    assert artifact["summary"]["beats_naive"] is True
    assert artifact["summary"]["material_reduction"] is True
    assert artifact["summary"]["pass"] is True
    assert artifact["summary"]["peak_ncp_tokens"] < artifact["summary"]["peak_naive_tokens"]


def test_public_package_exports_coding_pipeline_benchmark() -> None:
    import ncp

    assert callable(ncp.run_coding_pipeline_benchmark)


def test_research_pipeline_benchmark_beats_naive_replay(tmp_path: Path) -> None:
    artifact = run_research_pipeline_benchmark(
        store_path=tmp_path / "research-bench.db",
        turns=10,
        pipeline_id="pipe_test_research_bench",
    )

    assert artifact["benchmark"] == "research_pipeline"
    assert artifact["turns"] == 10
    assert len(artifact["agents"]) == 6
    assert len(artifact["turn_rows"]) == 10
    assert artifact["summary"]["beats_naive"] is True
    assert artifact["summary"]["material_reduction"] is True
    assert artifact["summary"]["pass"] is True
    assert artifact["summary"]["peak_ncp_tokens"] < artifact["summary"]["peak_naive_tokens"]


def test_public_package_exports_research_pipeline_benchmark() -> None:
    import ncp

    assert callable(ncp.run_research_pipeline_benchmark)
