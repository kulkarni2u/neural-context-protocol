from __future__ import annotations

from pathlib import Path

from benchmarks.harness_overhead.run import run_harness_overhead_benchmark


def test_harness_overhead_benchmark_passes_gate(tmp_path: Path) -> None:
    artifact = run_harness_overhead_benchmark(workdir=tmp_path)
    metrics = artifact["metrics"]

    assert artifact["summary"]["pass"] is True, artifact["summary"]
    assert artifact["unit"] == "chars_div4"
    for name in (
        "a_tool_catalog_chars",
        "b_protocol_call_chars_per_turn",
        "c_handoff_return_chars",
        "d_resent_chunk_chars",
        "g_dispatch_chars",
    ):
        assert metrics[name]["after"] <= metrics[name]["before"], name
    prefix = metrics["e_prefix_stable_fraction"]
    assert prefix["after"] >= prefix["before"]
    assert metrics["f_budget_accuracy"]["never_exceeds"] is True
    assert all(row["conservative_tokens"] <= row["max_tokens"] for row in metrics["f_budget_accuracy"]["rows"])
    chars = metrics["a_tool_catalog_chars"]["chars"]
    assert chars["minimal"] < chars["core"] < chars["standard"] < chars["full"]
    assert metrics["g_dispatch_chars"]["worker_receives_skill_text"] is True


def test_harness_overhead_release_invariants(tmp_path: Path) -> None:
    artifact = run_harness_overhead_benchmark(workdir=tmp_path)
    invariants = artifact["release_invariants"]
    checks = artifact["summary"]["checks"]
    for name in (
        "prefix_stable_after_outcomes",
        "no_outcome_pipeline_budget_unchanged",
        "standard_catalog_chars_le_limit",
        "agents_md_chars_le_limit",
    ):
        assert invariants["checks"][name] is True, (name, invariants)
        assert checks[f"release_{name}"] is True
    assert invariants["standard_catalog_chars"] <= 16_000
    assert invariants["agents_md_chars"] <= 9_000
    assert invariants["no_outcome_budget"]["budget_unproven_protected"] is False
    assert invariants["prefix_after_outcomes"]["headers_changed"] == 0


def test_harness_overhead_benchmark_is_deterministic(tmp_path: Path) -> None:
    first = run_harness_overhead_benchmark(workdir=tmp_path / "a", delta_turns=4, workers=3)
    second = run_harness_overhead_benchmark(workdir=tmp_path / "b", delta_turns=4, workers=3)
    assert first == second
