"""Baseline artifacts for the MACE benchmark."""

from __future__ import annotations

from datetime import datetime, UTC


def build_baseline_result(
    *,
    provider: str,
    turns: int,
    system_version: str,
    d1_result: dict[str, object] | None,
    run_dims: list[str],
) -> dict[str, object]:
    """Build the naive-history baseline artifact."""

    dimensions: dict[str, object] = {}
    if "d1" in run_dims and d1_result is not None:
        checkpoints = d1_result.get("checkpoints", {})
        baseline_checkpoints = {
            name: {
                "baseline_tokens": values["baseline_tokens"],
                "ncp_tokens": values["baseline_tokens"],
                "reduction_ratio": 1.0,
                "reduction_pct": 0.0,
            }
            for name, values in checkpoints.items()
        }
        dimensions["d1"] = {
            "dimension": "D1_token_efficiency",
            "score": 0.0,
            "primary_checkpoint": d1_result.get("primary_checkpoint", 40),
            "primary_reduction_ratio": 1.0,
            "checkpoints": baseline_checkpoints,
            "note": "baseline is naive history injection with no reduction",
        }
    if "d2" in run_dims:
        dimensions["d2"] = {
            "dimension": "D2_handoff_quality",
            "score": 0.0,
            "h1_uncertainty_propagation": 0.0,
            "h2_constraint_propagation": 0.0,
            "h3_whisper_delivery": 0.0,
            "note": "baseline has no structured handoff verification path",
        }
    if "d3" in run_dims:
        dimensions["d3"] = {
            "dimension": "D3_deadend_prevention",
            "score": 0.0,
            "retried_count": 3,
            "total_dead_ends": 3,
            "note": "baseline assumes no dead-end memory",
        }
    if "d4" in run_dims:
        dimensions["d4"] = {
            "dimension": "D4_goal_coherence",
            "score": 0.0,
            "turns_to_full_coherence": None,
            "note": "baseline has no automatic goal propagation mechanism",
        }
    return {
        "benchmark": "MACE",
        "version": "1.0",
        "timestamp": datetime.now(UTC).isoformat(),
        "system": "baseline_naive_history",
        "system_version": system_version,
        "provider": provider,
        "turns": turns,
        "dimensions": dimensions,
        "composite_score": 0.0,
    }
