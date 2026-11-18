from __future__ import annotations

from pathlib import Path

from ncp.bench.refine_loop import run_refine_loop_benchmark

_CHECKS = {
    "ingest_seed",
    "repair_applied",
    "prune_refused_without_anchor",
    "good_prune_applied",
    "bad_prune_gated_and_rejected",
    "stale_apply_refused",
    "rollback_marks_rolled_back",
    "block_lists_every_attempt",
    "prune_shorter_than_repair",
    "auto_calibration_changed_trust",
    "holdout_gate_blocks_train_only_win",
    "compare_ranks_best_first",
}


def test_refine_loop_benchmark_passes_gate(tmp_path: Path) -> None:
    artifact = run_refine_loop_benchmark(workdir=tmp_path)
    summary = artifact["summary"]

    assert summary["pass"] is True, summary
    assert set(summary["checks"]) == _CHECKS
    assert all(summary["checks"].values())
    metrics = artifact["metrics"]
    assert metrics["after_prune_chars"] < metrics["after_repair_chars"]
    assert artifact["paired_comparison"]["n"] == 10


def test_refine_loop_benchmark_is_deterministic(tmp_path: Path) -> None:
    first = run_refine_loop_benchmark(workdir=tmp_path / "a")
    second = run_refine_loop_benchmark(workdir=tmp_path / "b")
    assert first == second
