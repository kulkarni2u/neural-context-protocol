from __future__ import annotations

from pathlib import Path

from ncp.benchmarks import (
    estimate_tokens,
    run_coding_pipeline_benchmark,
    run_research_pipeline_benchmark,
    token_unit,
)


def test_estimate_tokens_ignores_empty_whitespace() -> None:
    assert estimate_tokens("") == 0
    assert estimate_tokens("  alpha   beta\n gamma ") >= 3


_VALID_TOKEN_UNITS = {"tiktoken/cl100k_base", "chars_div4"}


def test_token_unit_is_explicit() -> None:
    assert token_unit() in _VALID_TOKEN_UNITS


def test_estimate_tokens_chars_div4_fallback_when_no_tiktoken() -> None:
    if token_unit() == "chars_div4":
        # chars_div4: len("alpha beta gamma") == 16, 16 // 4 == 4
        assert estimate_tokens("alpha beta gamma") == 4
    else:
        assert estimate_tokens("alpha beta gamma") > 0


def test_coding_pipeline_benchmark_beats_naive_replay(tmp_path: Path) -> None:
    artifact = run_coding_pipeline_benchmark(
        store_path=tmp_path / "bench.db",
        turns=12,
        pipeline_id="pipe_test_bench",
    )

    assert artifact["benchmark"] == "coding_pipeline"
    assert artifact["token_unit"] in _VALID_TOKEN_UNITS
    assert artifact["turns"] == 12
    assert len(artifact["turn_rows"]) == 12
    assert set(artifact["summary"]["baselines"]) == {"raw_replay", "sliding_window", "rolling_summary"}
    # NCP beats raw replay at all scales; material_reduction (>=3x),
    # beats_sliding_window, and pass require the full 40-turn run where raw
    # replay has grown enough and NCP's bounded context advantage is clear.
    assert artifact["summary"]["beats_naive"] is True
    assert artifact["summary"]["peak_ncp_tokens"] < artifact["summary"]["peak_naive_tokens"]
    assert (
        int(artifact["summary"]["baselines"]["raw_replay"]["peak_tokens"])
        >= int(artifact["summary"]["ncp"]["peak_tokens"])
    )
    assert (
        int(artifact["summary"]["baselines"]["raw_replay"]["final_tokens"])
        >= int(artifact["summary"]["ncp"]["final_tokens"])
    )


def test_full_coding_pipeline_benchmark_gate_passes(tmp_path: Path) -> None:
    artifact = run_coding_pipeline_benchmark(
        store_path=tmp_path / "bench-full.db",
        turns=40,
        pipeline_id="pipe_test_full_bench",
    )

    assert artifact["context_token_budget"] == 340
    assert artifact["summary"]["beats_sliding_window"] is True
    assert artifact["summary"]["pass"] is True


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
    assert artifact["token_unit"] in _VALID_TOKEN_UNITS
    assert artifact["turns"] == 10
    assert len(artifact["agents"]) == 6
    assert len(artifact["turn_rows"]) == 10
    assert set(artifact["summary"]["baselines"]) == {"raw_replay", "sliding_window", "rolling_summary"}
    # material_reduction (>=3x) and beats_sliding_window require the full 36-turn run.
    assert artifact["summary"]["beats_naive"] is True
    assert artifact["summary"]["peak_ncp_tokens"] < artifact["summary"]["peak_naive_tokens"]
    assert (
        int(artifact["summary"]["baselines"]["raw_replay"]["peak_tokens"])
        >= int(artifact["summary"]["ncp"]["peak_tokens"])
    )
    assert (
        int(artifact["summary"]["baselines"]["raw_replay"]["final_tokens"])
        >= int(artifact["summary"]["ncp"]["final_tokens"])
    )


def test_public_package_exports_research_pipeline_benchmark() -> None:
    import ncp

    assert callable(ncp.run_research_pipeline_benchmark)
