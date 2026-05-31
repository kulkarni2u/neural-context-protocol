"""Deterministic agent behaviors for reproducible MACE runs."""

from __future__ import annotations

from dataclasses import dataclass


SAFE_FALLBACK_PATH = "oauth_pkce"


@dataclass(slots=True)
class AgentOutput:
    """Structured deterministic agent output."""

    summary: str
    attempted_paths: list[str]


def generate_agent_output(*, agent_id: str, context: str, goal: str, turn_n: int) -> AgentOutput:
    """Return a deterministic output summary from visible context only."""

    lower = context.lower()
    if agent_id == "planner":
        summary = (
            f"planner turn {turn_n:02d} keeps goal `{goal}` active, preserves oauth-only "
            "constraints, and hands execution to the next agent."
        )
        return AgentOutput(summary=summary, attempted_paths=[])

    if agent_id == "critic":
        summary = (
            f"critic turn {turn_n:02d} reviews the latest handoff, checks confidence signals, "
            "and avoids retrying previously failed integration paths."
        )
        return AgentOutput(summary=summary, attempted_paths=[])

    known_dead_ends = ["api/v1", "api/v2", "oauth_basic"]
    visible_dead_ends = [path for path in known_dead_ends if path in lower]
    if visible_dead_ends:
        summary = (
            f"executor turn {turn_n:02d} avoids {', '.join(visible_dead_ends)} and chooses "
            f"{SAFE_FALLBACK_PATH} to respect prior failures."
        )
        return AgentOutput(summary=summary, attempted_paths=[SAFE_FALLBACK_PATH])

    summary = (
        f"executor turn {turn_n:02d} lacks structured failure memory and retries api/v1 before "
        f"falling back to {SAFE_FALLBACK_PATH}."
    )
    return AgentOutput(summary=summary, attempted_paths=["api/v1", SAFE_FALLBACK_PATH])
