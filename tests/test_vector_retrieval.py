"""Tests for retrieval_mode parameter — Slice 4."""

from __future__ import annotations

from pathlib import Path


from ncp.stores.retrieval import RetrievalPolicy
from ncp.stores.sqlite import SQLiteStore
from ncp.types import SubconsciousChunk


def _store(tmp_path: Path) -> SQLiteStore:
    return SQLiteStore(tmp_path / "store.db")


def _chunk(content: str, base_trust: float = 0.7, written_by: str = "agent") -> SubconsciousChunk:
    return SubconsciousChunk(
        content=content,
        layer="semantic",
        src="agent_inferred",
        written_by=written_by,
        base_trust=base_trust,
    )


# ── RetrievalPolicy.score_no_bm25 ────────────────────────────────────────────

def test_score_no_bm25_returns_value_in_unit_interval() -> None:
    policy = RetrievalPolicy()
    score = policy.score_no_bm25(age_seconds=0, base_trust=1.0)
    assert 0.0 <= score <= 1.0


def test_score_no_bm25_higher_trust_gives_higher_score() -> None:
    policy = RetrievalPolicy()
    low = policy.score_no_bm25(age_seconds=100, base_trust=0.2)
    high = policy.score_no_bm25(age_seconds=100, base_trust=0.9)
    assert high > low


def test_score_no_bm25_older_chunk_scores_lower() -> None:
    policy = RetrievalPolicy()
    fresh = policy.score_no_bm25(age_seconds=0, base_trust=0.7)
    stale = policy.score_no_bm25(age_seconds=86400, base_trust=0.7)
    assert fresh > stale


def test_score_no_bm25_generation_penalty_applies() -> None:
    policy = RetrievalPolicy()
    base_gen = policy.score_no_bm25(age_seconds=0, base_trust=0.7, generation=0)
    derived = policy.score_no_bm25(age_seconds=0, base_trust=0.7, generation=5)
    assert base_gen > derived


def test_score_no_bm25_weight_renormalization() -> None:
    # With equal trust and recency contribution, score should be near
    # the trust value (when fresh, recency ≈ 1.0, so both signals are ~1.0)
    policy = RetrievalPolicy(w_lexical=0.5, w_recency=0.3, w_trust=0.2)
    score = policy.score_no_bm25(age_seconds=0, base_trust=1.0)
    # renormalized: (0.3*1.0 + 0.2*1.0) / 0.5 = 1.0
    assert abs(score - 1.0) < 0.01


def test_score_no_bm25_zero_w_sum_returns_zero() -> None:
    # w_lexical=1.0, w_recency=0.0, w_trust=0.0 → w_sum=0 → safe return 0
    policy = RetrievalPolicy(w_lexical=1.0, w_recency=0.0, w_trust=0.0)
    score = policy.score_no_bm25(age_seconds=0, base_trust=1.0)
    assert score == 0.0


# ── trust_recency mode in SQLiteStore ────────────────────────────────────────

def test_trust_recency_returns_results_with_no_term_overlap(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.write(_chunk("completely unrelated xyz abc def", base_trust=0.8))
    # In hybrid mode this would return nothing (no term overlap with query "foo bar")
    results_tr = store.query("foo bar", k=4, min_score=0.0, retrieval_mode="trust_recency")
    assert len(results_tr) >= 1


def test_hybrid_mode_filters_no_overlap_chunks(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.write(_chunk("completely unrelated xyz abc def", base_trust=0.8))
    # hybrid should filter out chunks with no term overlap
    results_hybrid = store.query("foo bar", k=4, min_score=0.01, retrieval_mode="hybrid")
    assert len(results_hybrid) == 0


def test_default_mode_is_hybrid(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.write(_chunk("authentication token management"))
    # default should behave the same as explicit hybrid
    default = store.query("authentication token", k=4, min_score=0.0)
    explicit = store.query("authentication token", k=4, min_score=0.0, retrieval_mode="hybrid")
    assert len(default) == len(explicit)
    if default and explicit:
        assert default[0].chunk_id == explicit[0].chunk_id


def test_trust_recency_ranks_by_trust(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.write(_chunk("content alpha beta gamma", base_trust=0.3, written_by="low"))
    store.write(_chunk("content delta epsilon zeta", base_trust=0.9, written_by="high"))
    results = store.query("irrelevant query xyz", k=4, min_score=0.0, retrieval_mode="trust_recency")
    assert len(results) >= 2
    # higher trust chunk should rank first
    assert results[0].base_trust > results[-1].base_trust


def test_trust_recency_scores_in_unit_interval(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.write(_chunk("sample chunk content here", base_trust=0.6))
    results = store.query("anything", k=4, min_score=0.0, retrieval_mode="trust_recency")
    assert len(results) >= 1
    for chunk in results:
        assert 0.0 <= chunk.relevance <= 1.0


def test_invalid_retrieval_mode_raises(tmp_path: Path) -> None:
    import pytest
    store = _store(tmp_path)
    store.write(_chunk("some content"))
    with pytest.raises(ValueError, match="retrieval_mode"):
        store.query("anything", retrieval_mode="typo_mode")
