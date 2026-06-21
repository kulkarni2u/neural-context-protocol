from __future__ import annotations

from benchmarks.compression import CORPUS, run_compression_benchmark

_VALID_TOKEN_UNITS = {"tiktoken/cl100k_base", "chars_div4"}


def test_compression_benchmark_artifact_structure() -> None:
    artifact = run_compression_benchmark()

    assert artifact["benchmark"] == "content_compression"
    assert artifact["token_unit"] in _VALID_TOKEN_UNITS
    assert artifact["config"]["payload_count"] == len(CORPUS)
    assert len(artifact["payloads"]) == len(CORPUS)

    required_row_keys = {
        "payload_id",
        "category",
        "description",
        "was_filtered",
        "raw_chars",
        "filtered_chars",
        "char_reduction_ratio",
        "raw_tokens",
        "filtered_tokens",
        "token_reduction_ratio",
    }
    for row in artifact["payloads"]:
        assert required_row_keys <= set(row)
        assert row["filtered_chars"] <= row["raw_chars"]
        assert row["filtered_tokens"] <= row["raw_tokens"]
        assert row["char_reduction_ratio"] >= 0.0
        assert row["token_reduction_ratio"] >= 0.0


def test_compression_benchmark_passes_gate() -> None:
    artifact = run_compression_benchmark()
    summary = artifact["summary"]

    assert summary["pass"] is True
    assert summary["aggregate_token_reduction_ratio"] > 0.0
    assert summary["aggregate_char_reduction_ratio"] > 0.0
    assert summary["aggregate_token_reduction_ratio"] >= summary["pass_threshold"]
    assert summary["total_filtered_tokens"] < summary["total_raw_tokens"]
    assert summary["total_filtered_chars"] < summary["total_raw_chars"]


def test_compression_benchmark_is_deterministic() -> None:
    first = run_compression_benchmark()
    second = run_compression_benchmark()
    assert first == second


def test_compression_benchmark_per_category_present() -> None:
    artifact = run_compression_benchmark()
    by_category = artifact["summary"]["by_category"]
    categories = {payload.category for payload in CORPUS}
    assert set(by_category) == categories
