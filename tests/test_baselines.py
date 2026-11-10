"""Unit tests for benchmark baseline strategies."""

from __future__ import annotations

from ncp.bench.baselines import RawReplayBaseline, RollingSummaryBaseline, SlidingWindowBaseline


def test_raw_replay_returns_full_transcript() -> None:
    b = RawReplayBaseline()
    assert b.context_for(transcript=["a", "b", "c"], turn="x") == "a\nb\nc"


def test_raw_replay_empty_transcript() -> None:
    b = RawReplayBaseline()
    assert b.context_for(transcript=[], turn="x") == ""


def test_sliding_window_keeps_last_n() -> None:
    b = SlidingWindowBaseline(last_entries=2)
    result = b.context_for(transcript=["a", "b", "c", "d"], turn="x")
    assert result == "c\nd"


def test_sliding_window_empty_transcript() -> None:
    b = SlidingWindowBaseline(last_entries=4)
    assert b.context_for(transcript=[], turn="x") == ""


def test_sliding_window_zero_entries_returns_empty() -> None:
    b = SlidingWindowBaseline(last_entries=0)
    assert b.context_for(transcript=["a", "b"], turn="x") == ""


def test_sliding_window_larger_than_transcript() -> None:
    b = SlidingWindowBaseline(last_entries=10)
    result = b.context_for(transcript=["a", "b"], turn="x")
    assert result == "a\nb"


def test_rolling_summary_empty_transcript() -> None:
    b = RollingSummaryBaseline()
    assert b.context_for(transcript=[], turn="x") == ""


def test_rolling_summary_short_transcript_returns_verbatim() -> None:
    b = RollingSummaryBaseline(every_k=4, keep_recent=4)
    result = b.context_for(transcript=["a", "b"], turn="x")
    assert result == "a\nb"


def test_rolling_summary_compresses_older_entries() -> None:
    b = RollingSummaryBaseline(every_k=2, keep_recent=2)
    entries = ["alpha", "beta", "gamma", "delta", "epsilon", "zeta"]
    result = b.context_for(transcript=entries, turn="x")
    assert "SUMMARY" in result
    assert "epsilon" in result
    assert "zeta" in result


def test_rolling_summary_keep_recent_zero_returns_full_summary() -> None:
    b = RollingSummaryBaseline(every_k=2, keep_recent=0)
    entries = ["alpha", "beta", "gamma", "delta"]
    result = b.context_for(transcript=entries, turn="x")
    assert "SUMMARY" in result


def test_rolling_summary_truncates_long_entries() -> None:
    b = RollingSummaryBaseline(every_k=1, keep_recent=1)
    long_entry = "x" * 200
    result = b.context_for(transcript=[long_entry, "recent"], turn="x")
    assert "…" in result
    assert "recent" in result
    summary_lines = [line for line in result.split("\n") if line.startswith("SUMMARY")]
    assert len(summary_lines) == 1
    assert len(summary_lines[0]) < 200


# --- PlainRagBaseline -------------------------------------------------------

from ncp.bench.baselines import PlainRagBaseline  # noqa: E402
from ncp.tokens import estimate_tokens  # noqa: E402

_RAG_TRANSCRIPT = [
    "deploy checklist for the staging cluster",
    "the zenbrix relay v4 is the approved transport path",
    "lunch order was sandwiches and soup",
    "retry backoff tuned to two seconds",
    "dashboard colors changed to teal",
]


def test_plain_rag_returns_lexically_matching_turn_first() -> None:
    b = PlainRagBaseline(budget_tokens=100)
    result = b.context_for(transcript=_RAG_TRANSCRIPT, turn="which zenbrix relay transport is approved")
    assert result.split("\n")[0] == _RAG_TRANSCRIPT[1]


def test_plain_rag_respects_token_budget() -> None:
    budget = estimate_tokens(_RAG_TRANSCRIPT[1]) + 1
    b = PlainRagBaseline(budget_tokens=budget)
    result = b.context_for(transcript=_RAG_TRANSCRIPT, turn="zenbrix relay approved")
    assert estimate_tokens(result) <= budget + len(_RAG_TRANSCRIPT)  # joins add newlines only
    assert sum(estimate_tokens(e) for e in b.select(transcript=_RAG_TRANSCRIPT, query="zenbrix")) <= budget
    assert _RAG_TRANSCRIPT[1] in result


def test_plain_rag_is_deterministic_and_tie_breaks_by_order() -> None:
    b = PlainRagBaseline(budget_tokens=100)
    ties = ["alpha one", "alpha two", "alpha three"]
    first = b.select(transcript=ties, query="alpha")
    assert first == b.select(transcript=ties, query="alpha")
    assert first == ties


def test_plain_rag_empty_inputs_and_zero_budget() -> None:
    assert PlainRagBaseline().context_for(transcript=[], turn="x") == ""
    assert PlainRagBaseline(budget_tokens=0).context_for(transcript=["a"], turn="a") == ""
    assert PlainRagBaseline(budget_tokens=50).select(transcript=["a b"], query="") == ["a b"]


def test_plain_rag_skips_oversized_entry_and_keeps_filling() -> None:
    long_hit = "zenbrix " + "filler " * 200
    short_hit = "zenbrix short note"
    b = PlainRagBaseline(budget_tokens=20)
    assert b.select(transcript=[long_hit, short_hit], query="zenbrix") == [short_hit]
