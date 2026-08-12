"""Tests for the fan-in reducer -- deterministic dedup/grouping/contradiction
flagging over a many-workers-to-one-reader candidate set
(``ncp.stores.consolidation.reduce_candidates``)."""

from __future__ import annotations

from ncp.stores.consolidation import ReduceResult, reduce_candidates
from ncp.types import SubconsciousChunk


def _chunk(
    chunk_id: str,
    content: str,
    *,
    layer: str = "semantic",
    zone: str = "working",
    pipeline_id: str | None = "pipe_fanin",
    base_trust: float = 0.7,
) -> SubconsciousChunk:
    return SubconsciousChunk(
        chunk_id=chunk_id,
        content=content,
        layer=layer,
        zone=zone,
        pipeline_id=pipeline_id,
        src="agent_inferred",
        written_by="worker",
        base_trust=base_trust,
        chunk_type="prose",
        scope="pipeline",
    )


_DEFAULT_KWARGS = {
    "similarity_threshold": 0.6,
    "contradict_floor": 0.35,
    "min_cluster": 3,
}


def test_reduce_empty_input() -> None:
    result = reduce_candidates([], **_DEFAULT_KWARGS)
    assert result == ReduceResult(kept=[])


def test_reduce_drops_malformed_empty_content() -> None:
    chunks = [
        _chunk("a", "database connection pool exhausted under load"),
        _chunk("b", "   "),
        _chunk("c", ""),
    ]
    result = reduce_candidates(chunks, **_DEFAULT_KWARGS)
    assert {c.chunk_id for c in result.kept} == {"a"}
    assert set(result.dropped_malformed) == {"b", "c"}


def test_cluster_below_min_cluster_passes_through_unchanged() -> None:
    # Two near-identical chunks, but min_cluster=3 -- too small a burst to reduce.
    chunks = [
        _chunk("a", "retryCount is null when payment_method is ACH", base_trust=0.5),
        _chunk("b", "retryCount is null when payment_method is ACH", base_trust=0.9),
    ]
    result = reduce_candidates(chunks, **_DEFAULT_KWARGS)
    assert {c.chunk_id for c in result.kept} == {"a", "b"}
    assert result.merged_away == []


def test_high_fanin_cluster_merges_near_duplicates_to_highest_trust() -> None:
    # 40-worker-style burst: many rewordings of the same root-cause claim.
    chunks = [
        _chunk("w1", "NPE at PaymentProcessor.java:142 retryCount null for ACH trial", base_trust=0.4),
        _chunk("w2", "NPE at PaymentProcessor.java line 142, retryCount is null for ACH trial users", base_trust=0.6),
        _chunk("w3", "NullPointerException PaymentProcessor.java:142 -- retryCount null, ACH trial", base_trust=0.95),
        _chunk("w4", "NPE PaymentProcessor.java:142 retryCount null ACH trial customer", base_trust=0.5),
    ]
    result = reduce_candidates(chunks, **_DEFAULT_KWARGS)
    kept_ids = {c.chunk_id for c in result.kept}
    assert "w3" in kept_ids  # highest base_trust survives
    assert len(kept_ids) < len(chunks)  # near-duplicates collapsed
    assert all(loser != "w3" for loser, _keeper in result.merged_away)


def test_reduce_flags_contradiction_band_without_merging() -> None:
    # Same topic (guard vs no guard), same cluster, but genuinely different
    # claims -- not a near-duplicate, so it must not be silently merged away.
    chunks = [
        _chunk("guard1", "Null guard applied at PaymentProcessor.java:142, test passes", base_trust=0.8),
        _chunk("guard2", "Null guard applied at PaymentProcessor.java:142, test passes", base_trust=0.6),
        _chunk("nofix", "PaymentProcessor.java:142 still throws NPE, guard did not resolve it", base_trust=0.7),
    ]
    result = reduce_candidates(
        chunks, similarity_threshold=0.75, contradict_floor=0.15, min_cluster=3
    )
    kept_ids = {c.chunk_id for c in result.kept}
    assert "guard1" in kept_ids and "nofix" in kept_ids
    assert any({a, b} == {"guard1", "nofix"} for a, b in result.contradictions)


def test_reduce_preserves_unclustered_chunks() -> None:
    # A chunk alone in its (layer, zone, pipeline_id) bucket is never merged
    # or dropped by clustering.
    chunks = [
        _chunk("solo", "unique observation about rate limiting", pipeline_id="pipe_other"),
        _chunk("w1", "shared claim about auth", base_trust=0.5),
        _chunk("w2", "shared claim about auth", base_trust=0.5),
        _chunk("w3", "shared claim about auth", base_trust=0.9),
    ]
    result = reduce_candidates(chunks, **_DEFAULT_KWARGS)
    kept_ids = {c.chunk_id for c in result.kept}
    assert "solo" in kept_ids


def test_reduce_result_dataclass_defaults() -> None:
    result = ReduceResult(kept=[])
    assert result.merged_away == []
    assert result.contradictions == []
    assert result.dropped_malformed == []
