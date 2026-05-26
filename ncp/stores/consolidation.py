"""Shared clustering and merge logic for consolidation passes."""

from __future__ import annotations

from difflib import SequenceMatcher
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ncp.types import SubconsciousChunk

_BM25_CLUSTER_MIN = 5  # use BM25 for clusters >= this size; SequenceMatcher below


def cluster_by_tags(chunks: list[SubconsciousChunk]) -> list[list[SubconsciousChunk]]:
    """Group chunks into clusters by (layer, zone, pipeline_id).

    Only chunks within the same cluster are merge candidates.
    """
    buckets: dict[tuple[str, str, str | None], list[SubconsciousChunk]] = {}
    for chunk in chunks:
        key = (chunk.layer, chunk.zone, chunk.pipeline_id)
        buckets.setdefault(key, []).append(chunk)
    return [group for group in buckets.values() if len(group) > 1]


def score_pair(a: SubconsciousChunk, b: SubconsciousChunk, cluster_size: int) -> float:
    """Return similarity score [0, 1] between two chunks.

    Uses BM25 for large clusters (avoids SequenceMatcher's O(n²) cost at scale)
    and SequenceMatcher for small ones (BM25 is noisy on tiny corpora).
    """
    if cluster_size >= _BM25_CLUSTER_MIN:
        return _bm25_similarity(a.content, b.content, cluster_size)
    return SequenceMatcher(None, a.content, b.content).ratio()


def _bm25_similarity(text_a: str, text_b: str, corpus_size: int) -> float:
    """Approximate normalized BM25 similarity between two texts.

    Tokenises both texts, builds a tiny 2-doc corpus, scores text_b against
    text_a's terms, and normalises by the self-score of text_a.
    """
    try:
        from rank_bm25 import BM25Okapi
    except ModuleNotFoundError:  # pragma: no cover
        return SequenceMatcher(None, text_a, text_b).ratio()

    tokens_a = text_a.lower().split()
    tokens_b = text_b.lower().split()
    if not tokens_a or not tokens_b:
        return 0.0

    # Pad corpus to at least corpus_size docs so IDF doesn't collapse
    padding = [["_pad_"] for _ in range(max(0, corpus_size - 2))]
    corpus = [tokens_a, tokens_b, *padding]
    bm25 = BM25Okapi(corpus)

    scores = bm25.get_scores(tokens_a)
    self_score = float(scores[0])
    cross_score = float(scores[1])

    if self_score <= 0.0:
        return 0.0
    return min(1.0, cross_score / self_score)


def select_authoritative(candidates: list[SubconsciousChunk]) -> SubconsciousChunk:
    """Pick the chunk to keep: highest base_trust, tie-break by lowest age_seconds (newest)."""
    return max(candidates, key=lambda c: (c.base_trust, -c.age_seconds))


def find_merge_candidates(
    cluster: list[SubconsciousChunk],
    *,
    similarity_threshold: float,
) -> list[tuple[SubconsciousChunk, list[SubconsciousChunk]]]:
    """Return (authoritative_chunk, [chunks_to_merge_away]) pairs for a cluster.

    Uses a greedy union-find approach: once a chunk is absorbed into a group
    it cannot be the authoritative chunk of another group.
    """
    n = len(cluster)
    absorbed: set[str] = set()
    groups: list[list[SubconsciousChunk]] = []

    for i in range(n):
        if cluster[i].chunk_id in absorbed:
            continue
        group = [cluster[i]]
        for j in range(i + 1, n):
            if cluster[j].chunk_id in absorbed:
                continue
            sim = score_pair(cluster[i], cluster[j], n)
            if sim >= similarity_threshold:
                group.append(cluster[j])
                absorbed.add(cluster[j].chunk_id)
        if len(group) > 1:
            groups.append(group)

    result = []
    for group in groups:
        keeper = select_authoritative(group)
        losers = [c for c in group if c.chunk_id != keeper.chunk_id]
        result.append((keeper, losers))
    return result
