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
        # Sliding-window baseline estimate:
        #   H1=0.0  (no structured retrieval → chunk not in assembly)
        #   H2=0.3  (task text mentions "oauth" keyword but lacks structured constraint chunk)
        #   H3=0.5  (whisper block present in window but structured payload not parsed)
        _h1 = 0.0
        _h2 = 0.3
        _h3 = 0.5
        _d2_score = round((_h1 + _h2 + _h3) / 3, 4)
        dimensions["d2"] = {
            "dimension": "D2_handoff_quality",
            "score": _d2_score,
            "h1_uncertainty_propagation": _h1,
            "h2_constraint_propagation": _h2,
            "h3_whisper_delivery": _h3,
            "note": (
                "sliding-window estimate: H1=0 (no chunk retrieval), "
                "H2=0.3 (oauth keyword present in task text), "
                "H3=0.5 (whisper block visible but payload unstructured)"
            ),
        }
    if "d3" in run_dims:
        # Sliding-window baseline: agent sees oauth_basic is bad (in recent window)
        # but forgets api/v1 and api/v2 → retries 2 of 3 dead ends → score=(3-2)/3=0.33
        dimensions["d3"] = {
            "dimension": "D3_deadend_prevention",
            "score": round(1.0 / 3.0, 4),
            "retried_count": 2,
            "total_dead_ends": 3,
            "note": (
                "sliding-window estimate: api/v1 and api/v2 retried (evicted from window), "
                "oauth_basic avoided (still in recent context); score=(3-2)/3=0.33"
            ),
        }
    if "d4" in run_dims:
        # Sliding-window baseline: goal propagates slowly without NCP;
        # 5 turns to full coherence → score = max(0, 1 - 4/5) = 0.2
        dimensions["d4"] = {
            "dimension": "D4_goal_coherence",
            "score": 0.2,
            "turns_to_full_coherence": 5,
            "note": (
                "sliding-window estimate: goal coherence propagates slowly without NCP; "
                "5 turns to full coherence → score = max(0, 1 - 4/5) = 0.2"
            ),
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
        "composite_score": round(
            sum(v["score"] for v in dimensions.values()) / len(dimensions) if dimensions else 0.0,
            4,
        ),
    }
