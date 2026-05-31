"""Scoring helpers for the MACE benchmark."""

from __future__ import annotations


def clamp_score(value: float) -> float:
    """Clamp a floating score into the canonical [0.0, 1.0] interval."""

    return max(0.0, min(1.0, value))


def weighted_mean(scores: dict[str, float], weights: dict[str, float]) -> float:
    """Compute a weighted mean over the scores that are present."""

    total_weight = sum(weights[key] for key in scores if key in weights)
    if total_weight <= 0:
        return 0.0
    weighted_total = sum(scores[key] * weights[key] for key in scores if key in weights)
    return weighted_total / total_weight
