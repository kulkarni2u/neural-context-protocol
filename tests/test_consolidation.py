"""Tests for subconscious consolidation — clustering, merge, dry-run, report."""

from __future__ import annotations

from pathlib import Path


from ncp.stores.consolidation import (
    cluster_by_tags,
    find_merge_candidates,
    score_pair,
    select_authoritative,
)
from ncp.stores.sqlite import SQLiteStore
from ncp.types import ConsolidationReport, SubconsciousChunk


def _chunk(
    chunk_id: str,
    content: str,
    *,
    layer: str = "semantic",
    zone: str = "working",
    pipeline_id: str | None = "pipe_test",
    base_trust: float = 0.7,
) -> SubconsciousChunk:
    return SubconsciousChunk(
        chunk_id=chunk_id,
        content=content,
        layer=layer,
        zone=zone,
        pipeline_id=pipeline_id,
        src="agent_inferred",
        written_by="test_agent",
        base_trust=base_trust,
        chunk_type="prose",
        scope="pipeline",
    )


# ---------------------------------------------------------------------------
# cluster_by_tags
# ---------------------------------------------------------------------------


def test_cluster_groups_by_layer_zone_pipeline() -> None:
    chunks = [
        _chunk("a", "hello world", layer="semantic", zone="working", pipeline_id="p1"),
        _chunk("b", "hello there", layer="semantic", zone="working", pipeline_id="p1"),
        _chunk("c", "different layer", layer="episodic", zone="working", pipeline_id="p1"),
    ]
    clusters = cluster_by_tags(chunks)
    assert len(clusters) == 1
    assert {c.chunk_id for c in clusters[0]} == {"a", "b"}


def test_cluster_separates_different_pipelines() -> None:
    chunks = [
        _chunk("a", "same text", layer="semantic", zone="working", pipeline_id="pipe_a"),
        _chunk("b", "same text", layer="semantic", zone="working", pipeline_id="pipe_b"),
    ]
    clusters = cluster_by_tags(chunks)
    assert clusters == []


def test_cluster_single_chunk_not_returned() -> None:
    chunks = [_chunk("a", "solo")]
    clusters = cluster_by_tags(chunks)
    assert clusters == []


def test_cluster_empty_store() -> None:
    assert cluster_by_tags([]) == []


# ---------------------------------------------------------------------------
# score_pair
# ---------------------------------------------------------------------------


def test_score_pair_identical_content_high_score() -> None:
    a = _chunk("a", "authentication token refresh logic")
    b = _chunk("b", "authentication token refresh logic")
    score = score_pair(a, b, cluster_size=2)
    assert score >= 0.95


def test_score_pair_unrelated_content_low_score() -> None:
    a = _chunk("a", "database connection pool configuration")
    b = _chunk("b", "user interface button color theme")
    score = score_pair(a, b, cluster_size=2)
    assert score < 0.3


def test_score_pair_uses_seqmatcher_for_small_cluster() -> None:
    a = _chunk("a", "retry logic with exponential backoff")
    b = _chunk("b", "retry logic with exponential backoff and jitter")
    score = score_pair(a, b, cluster_size=3)
    assert 0.7 < score < 1.0


def test_score_pair_uses_bm25_for_large_cluster() -> None:
    a = _chunk("a", "cache invalidation strategy for distributed systems")
    b = _chunk("b", "cache invalidation strategy for distributed systems")
    score = score_pair(a, b, cluster_size=10)
    assert score >= 0.9


# ---------------------------------------------------------------------------
# select_authoritative
# ---------------------------------------------------------------------------


def test_select_authoritative_picks_highest_trust() -> None:
    chunks = [
        _chunk("low", "same content here for testing", base_trust=0.4),
        _chunk("high", "same content here for testing", base_trust=0.9),
        _chunk("mid", "same content here for testing", base_trust=0.6),
    ]
    keeper = select_authoritative(chunks)
    assert keeper.chunk_id == "high"


# ---------------------------------------------------------------------------
# find_merge_candidates
# ---------------------------------------------------------------------------


def test_find_merge_candidates_returns_keeper_and_losers() -> None:
    chunks = [
        _chunk("a", "authentication token refresh logic for api", base_trust=0.5),
        _chunk("b", "authentication token refresh logic for api", base_trust=0.9),
        _chunk("c", "completely unrelated database migration script", base_trust=0.7),
    ]
    result = find_merge_candidates(chunks, similarity_threshold=0.65)
    assert len(result) == 1
    keeper, losers = result[0]
    assert keeper.chunk_id == "b"
    assert [c.chunk_id for c in losers] == ["a"]


def test_find_merge_candidates_empty_cluster() -> None:
    assert find_merge_candidates([], similarity_threshold=0.65) == []


def test_find_merge_candidates_single_chunk_no_merge() -> None:
    result = find_merge_candidates([_chunk("a", "only chunk")], similarity_threshold=0.65)
    assert result == []


def test_find_merge_candidates_no_similar_pairs() -> None:
    chunks = [
        _chunk("a", "database schema migration scripts"),
        _chunk("b", "javascript frontend button styling"),
    ]
    result = find_merge_candidates(chunks, similarity_threshold=0.65)
    assert result == []


# ---------------------------------------------------------------------------
# SQLiteStore.consolidate() integration
# ---------------------------------------------------------------------------


def test_consolidate_merges_duplicates(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "store.db")
    # Two similar chunks in same tag-scope (layer+zone+pipeline) but different enough content
    # to bypass write-time dedup (0.92 threshold) yet similar enough for consolidation (0.65).
    c1 = _chunk("m1", "auth token refresh logic handles expiry", base_trust=0.5, layer="semantic")
    c2 = _chunk("m2", "auth token refresh logic handles expiry and rotation", base_trust=0.9, layer="semantic")
    store.write(c1)
    store.write(c2)

    report = store.consolidate(pipeline_id="pipe_test")
    assert isinstance(report, ConsolidationReport)
    assert report.dry_run is False
    assert report.clusters_scanned >= 1
    assert report.merged >= 1
    assert report.tombstoned >= 1
    assert report.duration_seconds >= 0.0


def test_consolidate_dry_run_does_not_modify_store(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "store.db")
    c1 = _chunk("dr1", "retry with backoff on transient failures", base_trust=0.5)
    c2 = _chunk("dr2", "retry with backoff on transient failures and timeouts", base_trust=0.9)
    store.write(c1)
    store.write(c2)

    before_status = store.status()
    report = store.consolidate(pipeline_id="pipe_test", dry_run=True)

    after_status = store.status()
    assert report.dry_run is True
    # Dry run must not change chunk count
    assert before_status["chunk_count"] == after_status["chunk_count"]


def test_consolidate_empty_store_is_noop(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "store2.db")
    report = store.consolidate()
    assert report.merged == 0
    assert report.tombstoned == 0
    assert report.clusters_scanned == 0


def test_consolidate_report_fields_present(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "store3.db")
    report = store.consolidate(pipeline_id="pipe_test", dry_run=True)
    assert hasattr(report, "merged")
    assert hasattr(report, "tombstoned")
    assert hasattr(report, "skipped")
    assert hasattr(report, "clusters_scanned")
    assert hasattr(report, "duration_seconds")
    assert hasattr(report, "dry_run")
    assert hasattr(report, "merge_log")


def test_consolidation_report_defaults() -> None:
    report = ConsolidationReport()
    assert report.merged == 0
    assert report.tombstoned == 0
    assert report.skipped == 0
    assert report.clusters_scanned == 0
    assert report.dry_run is False
    assert report.pipeline_id is None
    assert report.merge_log == []
