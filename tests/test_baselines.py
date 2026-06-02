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
