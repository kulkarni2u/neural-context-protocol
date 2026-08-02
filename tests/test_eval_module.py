from __future__ import annotations

from ncp.eval import (
    EvalTurn,
    matched_budget_conditions,
    raw_replay_condition,
    sliding_window_condition,
    term_appears_unnegated,
)


def test_term_appears_unnegated_basic() -> None:
    term = "some_dead_end_slug"
    assert term_appears_unnegated(f"let's retry {term} now", term) is True
    assert term_appears_unnegated(f"we will not use {term} again", term) is False
    assert term_appears_unnegated("nothing relevant here", term) is False


def test_term_appears_unnegated_only_flags_unnegated_occurrence() -> None:
    term = "legacy_bridge"
    text = f"{term} was rejected; do not use {term} going forward"
    assert term_appears_unnegated(text, term) is False


def test_sliding_window_condition_drops_old_turns_under_budget() -> None:
    turns = [EvalTurn(content=f"turn {i} " * 20) for i in range(10)]
    result = sliding_window_condition(turns, budget=50)
    assert result.condition == "sliding_window"
    assert result.unbounded is False
    assert result.context_tokens <= 50 or result.context == ""
    # Only the most recent turns should survive.
    assert "turn 9" in result.context
    assert "turn 0" not in result.context


def test_raw_replay_condition_includes_everything_and_is_unbounded() -> None:
    turns = [EvalTurn(content=f"turn {i}") for i in range(5)]
    result = raw_replay_condition(turns)
    assert result.unbounded is True
    for i in range(5):
        assert f"turn {i}" in result.context


def test_matched_budget_conditions_recovers_planted_fact_ncp_only() -> None:
    turns = [
        EvalTurn(
            content="decision: approved_widget_9x is the approved path",
            src="user_verified",
            base_trust=0.97,
            relevance=0.95,
            layer="semantic",
            conditions=["approved_path"],
        ),
    ]
    filler = [
        EvalTurn(
            content=f"turn {i:02d}: routine status update, nothing new here today",
            src="agent_inferred",
            base_trust=0.4,
            relevance=0.05,
        )
        for i in range(1, 14)
    ]
    conditions = matched_budget_conditions(
        turns + filler,
        budget=120,
        query_text="approved path decision",
        pipeline_id="pipe_eval_module_test",
    )

    assert set(conditions) == {"ncp", "sliding_window", "raw_replay"}
    assert "approved_widget_9x" in conditions["ncp"].context
    assert conditions["ncp"].unbounded is False
    assert conditions["sliding_window"].unbounded is False
    assert conditions["raw_replay"].unbounded is True
    # Recency-only window is pushed past the planted fact by filler turns.
    assert "approved_widget_9x" not in conditions["sliding_window"].context
