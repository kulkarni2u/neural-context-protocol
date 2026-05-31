"""Tests for RetrievalPolicy and hybrid retrieval scoring in both stores."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pytest

from ncp.stores.retrieval import (
    DEFAULT_RETRIEVAL_POLICY,
    RetrievalPolicy,
    apply_diversity_limit,
    build_lexical_candidates,
    lexical_signal_for_candidate,
    normalize_bm25_scores,
    normalize_query_terms,
    normalize_result_limit,
    score_trust_recency_candidate,
    score_vector_distance,
)
from ncp.stores.sqlite import SQLiteStore
from ncp.types import SubconsciousChunk


# ---------------------------------------------------------------------------
# RetrievalPolicy unit tests
# ---------------------------------------------------------------------------


def test_default_policy_weights_sum_to_one() -> None:
    p = DEFAULT_RETRIEVAL_POLICY
    assert abs(p.w_lexical + p.w_recency + p.w_trust - 1.0) < 1e-9


def test_policy_rejects_weights_that_dont_sum_to_one() -> None:
    with pytest.raises(ValueError, match="sum to 1.0"):
        RetrievalPolicy(w_lexical=0.5, w_recency=0.4, w_trust=0.2)


def test_policy_rejects_negative_weights() -> None:
    with pytest.raises(ValueError, match="w_trust"):
        RetrievalPolicy(w_lexical=0.8, w_recency=0.4, w_trust=-0.2)


def test_policy_rejects_zero_half_life() -> None:
    with pytest.raises(ValueError, match="recency_half_life_seconds"):
        RetrievalPolicy(w_lexical=0.5, w_recency=0.3, w_trust=0.2, recency_half_life_seconds=0)


def test_fresh_high_trust_chunk_scores_near_one() -> None:
    p = RetrievalPolicy()
    score = p.score(bm25_normalized=1.0, age_seconds=0.0, base_trust=1.0, generation=0)
    assert score == pytest.approx(1.0, abs=1e-6)


def test_old_chunk_scores_lower_than_fresh_chunk() -> None:
    p = RetrievalPolicy()
    fresh = p.score(bm25_normalized=0.8, age_seconds=0.0, base_trust=0.7)
    old = p.score(bm25_normalized=0.8, age_seconds=86400.0, base_trust=0.7)  # 24h
    assert fresh > old


def test_high_generation_chunk_is_penalized() -> None:
    p = RetrievalPolicy()
    gen0 = p.score(bm25_normalized=0.8, age_seconds=100.0, base_trust=0.7, generation=0)
    gen3 = p.score(bm25_normalized=0.8, age_seconds=100.0, base_trust=0.7, generation=3)
    assert gen0 > gen3
    assert gen3 == pytest.approx(gen0 * (0.9 ** 3), rel=1e-6)


def test_trust_weight_breaks_tie_between_same_bm25_and_recency() -> None:
    p = RetrievalPolicy()
    kwargs = dict(bm25_normalized=0.6, age_seconds=60.0, generation=0)
    high = p.score(base_trust=0.9, **kwargs)
    low = p.score(base_trust=0.3, **kwargs)
    assert high > low


def test_score_bounded_in_unit_interval() -> None:
    p = RetrievalPolicy()
    for bm25 in (0.0, 0.5, 1.0):
        for age in (0.0, 3600.0, 86400.0):
            for trust in (0.0, 0.5, 1.0):
                s = p.score(bm25_normalized=bm25, age_seconds=age, base_trust=trust)
                assert 0.0 <= s <= 1.0, f"score={s} out of [0,1] for bm25={bm25} age={age} trust={trust}"


def test_lexical_only_policy_ignores_recency_and_trust() -> None:
    p = RetrievalPolicy(w_lexical=1.0, w_recency=0.0, w_trust=0.0)
    s1 = p.score(bm25_normalized=0.7, age_seconds=0.0, base_trust=1.0)
    s2 = p.score(bm25_normalized=0.7, age_seconds=86400.0, base_trust=0.0)
    assert s1 == pytest.approx(s2, rel=1e-9)


def test_vector_aware_score_matches_score_when_vector_missing() -> None:
    p = RetrievalPolicy()
    base = p.score(bm25_normalized=0.65, age_seconds=120.0, base_trust=0.8, generation=1)
    hybrid = p.score_with_vector(
        bm25_normalized=0.65,
        vector_normalized=None,
        age_seconds=120.0,
        base_trust=0.8,
        generation=1,
    )
    assert hybrid == pytest.approx(base, rel=1e-9)


def test_vector_aware_score_remains_bounded_in_unit_interval() -> None:
    p = RetrievalPolicy()
    for bm25 in (0.0, 0.4, 1.0):
        for vector in (0.0, 0.5, 1.0):
            score = p.score_with_vector(
                bm25_normalized=bm25,
                vector_normalized=vector,
                age_seconds=600.0,
                base_trust=0.7,
                generation=0,
            )
            assert 0.0 <= score <= 1.0


def test_vector_signal_can_break_tie_between_same_lexical_chunks() -> None:
    p = RetrievalPolicy()
    stronger_vector = p.score_with_vector(
        bm25_normalized=0.6,
        vector_normalized=0.95,
        age_seconds=120.0,
        base_trust=0.7,
        generation=0,
    )
    weaker_vector = p.score_with_vector(
        bm25_normalized=0.6,
        vector_normalized=0.10,
        age_seconds=120.0,
        base_trust=0.7,
        generation=0,
    )
    assert stronger_vector > weaker_vector


def test_normalize_query_terms_trims_and_lowercases() -> None:
    assert normalize_query_terms("  Token   Auth  bearer  ") == {"token", "auth", "bearer"}


def test_lexical_signal_filters_zero_overlap() -> None:
    assert lexical_signal_for_candidate(
        query_terms={"token", "auth"},
        doc_tokens=["schema", "migration"],
        bm25_normalized=0.9,
    ) is None


def test_lexical_signal_uses_full_budget_for_blank_query() -> None:
    assert lexical_signal_for_candidate(
        query_terms=set(),
        doc_tokens=["anything"],
        bm25_normalized=0.2,
    ) == pytest.approx(1.0)


def test_normalize_result_limit_enforces_minimum_one() -> None:
    assert normalize_result_limit(0) == 1
    assert normalize_result_limit(-3) == 1
    assert normalize_result_limit(5) == 5


@dataclass
class _Candidate:
    chunk_id: str
    written_by: str


def test_apply_diversity_limit_caps_results_per_author() -> None:
    ranked = [
        _Candidate("sub_1", "alice"),
        _Candidate("sub_2", "alice"),
        _Candidate("sub_3", "bob"),
        _Candidate("sub_4", "alice"),
    ]
    results = apply_diversity_limit(
        ranked,
        k=4,
        diversity_limit=1,
        author_getter=lambda item: item.written_by,
    )
    assert [item.chunk_id for item in results] == ["sub_1", "sub_3"]


def test_build_lexical_candidates_preserves_input_order() -> None:
    docs = [
        "token auth bearer failure",
        "schema migration notes",
        "token refresh bearer expiry",
    ]
    candidates = build_lexical_candidates("token bearer", docs)
    assert [candidate.doc_tokens for candidate in candidates] == [
        ["token", "auth", "bearer", "failure"],
        ["schema", "migration", "notes"],
        ["token", "refresh", "bearer", "expiry"],
    ]


def test_build_lexical_candidates_filters_zero_overlap_via_none_signal() -> None:
    docs = [
        "token auth bearer failure",
        "schema migration notes",
    ]
    candidates = build_lexical_candidates("token bearer", docs)
    assert candidates[0].lexical_signal is not None
    assert candidates[1].lexical_signal is None


def test_build_lexical_candidates_blank_query_uses_full_budget_for_all_rows() -> None:
    candidates = build_lexical_candidates("   ", ["alpha beta", "gamma delta"])
    assert [candidate.lexical_signal for candidate in candidates] == [pytest.approx(1.0), pytest.approx(1.0)]


def test_build_lexical_candidates_normalizes_top_match_to_one() -> None:
    candidates = build_lexical_candidates(
        "token bearer failure",
        [
            "token bearer failure extra",
            "token bearer",
            "schema migration notes",
        ],
    )
    assert candidates[0].lexical_signal == pytest.approx(1.0)
    assert candidates[1].lexical_signal is not None
    assert candidates[1].lexical_signal < candidates[0].lexical_signal


def test_normalize_bm25_scores_handles_empty_input() -> None:
    assert normalize_bm25_scores([]) == []


def test_normalize_bm25_scores_handles_all_zero_scores() -> None:
    assert normalize_bm25_scores([0.0, 0.0]) == [0.0, 0.0]


def test_normalize_bm25_scores_normalizes_against_max_score() -> None:
    normalized = normalize_bm25_scores([0.5, 1.0, 0.25])
    assert normalized == pytest.approx([0.5, 1.0, 0.25])


def test_score_trust_recency_candidate_matches_policy_score_no_bm25() -> None:
    policy = RetrievalPolicy()
    score = score_trust_recency_candidate(
        policy,
        created_at=100.0,
        now=250.0,
        base_trust=0.8,
        generation=2,
    )
    expected = policy.score_no_bm25(
        age_seconds=150.0,
        base_trust=0.8,
        generation=2,
    )
    assert score == pytest.approx(expected)


def test_score_vector_distance_prefers_smaller_distance() -> None:
    assert score_vector_distance(0.1) > score_vector_distance(0.9)


def test_score_vector_distance_is_bounded_and_handles_missing() -> None:
    assert score_vector_distance(None) == pytest.approx(0.5)
    assert score_vector_distance(0.0) == pytest.approx(1.0)
    assert 0.0 <= score_vector_distance(3.0) <= 1.0


# ---------------------------------------------------------------------------
# SQLiteStore hybrid retrieval integration tests
# ---------------------------------------------------------------------------


def _write_chunk(
    store: SQLiteStore,
    chunk_id: str,
    content: str,
    *,
    layer: str = "semantic",
    base_trust: float = 0.7,
    generation: int = 0,
    written_by: str = "system",
    pipeline_id: str = "pipe_test",
) -> None:
    store.write(
        SubconsciousChunk(
            chunk_id=chunk_id,
            layer=layer,  # type: ignore[arg-type]
            content=content,
            src="tool_result",
            pipeline_id=pipeline_id,
            base_trust=base_trust,
            generation=generation,
            written_by=written_by,
        )
    )


def test_hybrid_retrieval_trust_signal_promotes_high_trust_chunk(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "store.db")
    # Different layers prevent duplicate deduplication while keeping BM25 scores equal.
    _write_chunk(store, "sub_low", "bearer token auth failure handling", layer="semantic", base_trust=0.2, written_by="agentA")
    _write_chunk(store, "sub_high", "bearer token auth failure handling", layer="procedural", base_trust=0.95, written_by="agentB")

    results = store.query("bearer token auth failure", pipeline_id="pipe_test", k=4)
    assert results[0].chunk_id == "sub_high"
    assert results[1].chunk_id == "sub_low"


def test_hybrid_retrieval_recency_signal_promotes_newer_chunk(tmp_path: Path) -> None:
    import sqlite3

    store = SQLiteStore(tmp_path / "store.db")
    # Different layers so duplicate guard doesn't block the second write.
    _write_chunk(store, "sub_new", "database migration schema rollback steps", layer="semantic", written_by="agentA")
    _write_chunk(store, "sub_old", "database migration schema rollback steps", layer="procedural", written_by="agentB")

    # Backdate sub_old by 24 hours so the recency signal clearly favours sub_new.
    conn = sqlite3.connect(tmp_path / "store.db")
    conn.execute("UPDATE chunks SET created_at = created_at - 86400 WHERE chunk_id = 'sub_old'")
    conn.commit()
    conn.close()

    results = store.query("database migration schema rollback", pipeline_id="pipe_test", k=4)
    assert len(results) >= 2
    assert results[0].chunk_id == "sub_new"


def test_hybrid_retrieval_zero_overlap_guard_preserved(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "store.db")
    _write_chunk(store, "sub_topic", "python async await coroutine event loop", written_by="agentA")
    _write_chunk(store, "sub_other", "database migration schema notes", written_by="agentB")

    off_topic = store.query("unrelated astronomy orbit telescope", pipeline_id="pipe_test", k=4)
    assert off_topic == []


def test_hybrid_retrieval_blank_query_returns_by_trust_desc(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "store.db")
    # Use different layers so duplicate guard doesn't deduplicate identical content.
    _write_chunk(store, "sub_low", "anything goes here", layer="semantic", base_trust=0.2, written_by="a1")
    _write_chunk(store, "sub_high", "anything goes here", layer="procedural", base_trust=0.95, written_by="a2")
    _write_chunk(store, "sub_mid", "anything goes here", layer="episodic", base_trust=0.6, written_by="a3")

    results = store.query("   ", pipeline_id="pipe_test", k=4)
    assert results[0].chunk_id == "sub_high"
    chunk_ids = {c.chunk_id for c in results}
    assert "sub_low" in chunk_ids
    assert "sub_mid" in chunk_ids


def test_hybrid_retrieval_custom_policy_changes_ranking(tmp_path: Path) -> None:
    trust_first = RetrievalPolicy(w_lexical=0.0, w_recency=0.0, w_trust=1.0)
    store = SQLiteStore(tmp_path / "store.db", retrieval_policy=trust_first)

    _write_chunk(store, "sub_low", "token auth bearer failure", layer="semantic", base_trust=0.1, written_by="a1")
    _write_chunk(store, "sub_high", "token auth bearer failure", layer="procedural", base_trust=0.99, written_by="a2")

    results = store.query("token auth bearer failure", pipeline_id="pipe_test", k=4)
    assert results[0].chunk_id == "sub_high"
    assert results[0].relevance > results[1].relevance


def test_hybrid_relevance_scores_bounded_in_unit_interval(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "store.db")
    for i in range(5):
        _write_chunk(store, f"sub_{i}", f"retrieval ranking trust score test chunk {i}", written_by=f"agent_{i}")

    results = store.query("retrieval ranking trust score", pipeline_id="pipe_test", k=4)
    for chunk in results:
        assert 0.0 <= chunk.relevance <= 1.0, f"relevance={chunk.relevance} out of [0,1]"


def test_hybrid_retrieval_generation_penalty_demotes_derived_chunks(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "store.db")
    # Different layers prevent duplicate deduplication.
    store.write(
        SubconsciousChunk(
            chunk_id="sub_gen0",
            layer="semantic",
            content="context window usage token budget planning",
            src="tool_result",
            pipeline_id="pipe_test",
            base_trust=0.8,
            generation=0,
            written_by="agentA",
        )
    )
    store.write(
        SubconsciousChunk(
            chunk_id="sub_gen5",
            layer="procedural",
            content="context window usage token budget planning",
            src="synthesis",
            pipeline_id="pipe_test",
            base_trust=0.8,
            generation=5,
            written_by="agentB",
        )
    )
    results = store.query("context window token budget", pipeline_id="pipe_test", k=4)
    assert len(results) >= 2
    ids = [c.chunk_id for c in results]
    assert ids[0] == "sub_gen0"
