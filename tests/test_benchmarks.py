from __future__ import annotations

from pathlib import Path

from ncp.benchmarks import (
    estimate_tokens,
    run_coding_pipeline_benchmark,
    run_research_pipeline_benchmark,
    token_unit,
)
from ncp.stores.sqlite import SQLiteStore
from ncp.types import SubconsciousChunk


def test_estimate_tokens_ignores_empty_whitespace() -> None:
    assert estimate_tokens("") == 0
    assert estimate_tokens("  alpha   beta\n gamma ") >= 3


_VALID_TOKEN_UNITS = {"tiktoken/cl100k_base", "chars_div4"}


def test_token_unit_is_explicit() -> None:
    assert token_unit() in _VALID_TOKEN_UNITS


def test_token_unit_defaults_to_chars_div4(monkeypatch) -> None:
    from ncp import tokens

    monkeypatch.delenv("NCP_TOKEN_UNIT", raising=False)
    tokens.reset_encoder_cache()
    try:
        assert tokens.token_unit() == "chars_div4"
        assert tokens.estimate_tokens("alpha beta gamma") == 4
    finally:
        tokens.reset_encoder_cache()


def test_token_unit_tiktoken_opt_in(monkeypatch) -> None:
    from ncp import tokens

    monkeypatch.setenv("NCP_TOKEN_UNIT", "tiktoken")
    tokens.reset_encoder_cache()
    try:
        assert tokens.token_unit() in _VALID_TOKEN_UNITS
        assert tokens.estimate_tokens("alpha beta gamma") > 0
    finally:
        tokens.reset_encoder_cache()


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


class _FakeEmbeddingAdapter:
    """Trivial adapter used to verify embed-token accounting is real, not assumed."""

    def embed(self, text: str) -> list[float]:
        return [0.1, 0.2]


def test_sqlite_store_measures_embedding_tokens_on_write_and_query(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "embed-accounting.db", embedding_adapter=_FakeEmbeddingAdapter())
    assert store.embedding_tokens_estimate() == 0

    store.write(
        SubconsciousChunk(
            chunk_id="sub_one",
            layer="semantic",
            content="alpha beta gamma delta",
            src="tool_result",
        )
    )
    after_one_write = store.embedding_tokens_estimate()
    assert after_one_write > 0
    assert after_one_write == estimate_tokens("alpha beta gamma delta")

    store.write(
        SubconsciousChunk(
            chunk_id="sub_two",
            layer="semantic",
            content="epsilon zeta eta theta iota",
            src="tool_result",
        )
    )
    after_two_writes = store.embedding_tokens_estimate()
    assert after_two_writes > after_one_write

    store.query("alpha beta", k=2, min_score=0.0, retrieval_mode="vector")
    after_query = store.embedding_tokens_estimate()
    assert after_query > after_two_writes
    assert after_query == after_two_writes + estimate_tokens("alpha beta")


def test_sqlite_store_without_embedding_adapter_reports_zero_tokens(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "no-embed.db")
    store.write(
        SubconsciousChunk(
            chunk_id="sub_only",
            layer="semantic",
            content="no embedding adapter configured here",
            src="tool_result",
        )
    )
    store.query("no embedding adapter", k=2, min_score=0.0)
    assert store.embedding_tokens_estimate() == 0


def test_coding_pipeline_benchmark_overhead_unchanged_without_embedding_adapter(
    tmp_path: Path,
) -> None:
    """The coding pipeline benchmark's own store has no embedding_adapter
    configured, so the now-measured embed_tokens must equal the previously
    hardcoded literal 0 -- i.e. this fix must not change the benchmark's
    reported numbers for the no-embedding case, only make the computation
    honest (measured zero, not assumed zero)."""
    from ncp.costs import assembly_overhead

    turns = 4
    artifact = run_coding_pipeline_benchmark(
        store_path=tmp_path / "overhead-bench.db",
        turns=turns,
        pipeline_id="pipe_test_overhead_bench",
    )

    expected_overhead = assembly_overhead(embed_tokens=0, retrieval_ops=turns, whisper_writes=0)
    economics = artifact["summary"]["economics"]
    assert economics["assembly_overhead_token_equivalent"] == round(expected_overhead.token_equivalent, 2)
    assert economics["assembly_overhead_usd"] == round(expected_overhead.total_cost_usd, 8)
