"""Shared clustering and merge logic for consolidation passes."""

from __future__ import annotations

from dataclasses import dataclass, field
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
        return bm25_similarity(a.content, b.content, cluster_size)
    return SequenceMatcher(None, a.content, b.content).ratio()


def bm25_similarity(text_a: str, text_b: str, corpus_size: int) -> float:
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


_bm25_similarity = bm25_similarity


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


# Deterministic, no-model-call contradiction cue: a hand-picked set of
# reversal/closure phrases common in bug-triage and status-report language
# ("already fixed", "no longer reproduces", "still throws"). Similarity
# alone cannot separate "same claim, different wording" from "opposite
# claim, similar wording" -- two paraphrases of one finding can score just
# as low on lexical similarity as two genuinely different claims about the
# same topic, so a plain similarity band produces false positives on real
# paraphrase variance. Requiring one side (and not the other) of a
# same-cluster pair to carry a reversal cue is a narrower, more honest
# signal for "this looks like a status reversal, worth a second look" --
# still a heuristic, not semantic entailment, and still just surfaced for
# the reading agent to judge, never resolved by NCP itself.
_CONTRADICTION_CUES: tuple[str, ...] = (
    "already",
    "no longer",
    "not reproducible",
    "cannot reproduce",
    "can't reproduce",
    "was fixed",
    "is fixed",
    "has been fixed",
    "did not resolve",
    "didn't resolve",
    "does not resolve",
    "not resolved",
    "false positive",
    "not a bug",
    "not an issue",
    "still throws",
    "still fails",
    "still broken",
)


def _has_reversal_cue(content: str, cues: tuple[str, ...] = _CONTRADICTION_CUES) -> bool:
    lowered = content.lower()
    return any(cue in lowered for cue in cues)


@dataclass(slots=True)
class ReduceResult:
    """Result of a fan-in reduction pass over one turn's candidate chunks.

    ``kept`` preserves input order for chunks that were not absorbed into a
    merge; merged-away and contradicting chunks are reported by id so a
    caller can log or surface them without re-deriving the clustering.
    """

    kept: list[SubconsciousChunk]
    merged_away: list[tuple[str, str]] = field(default_factory=list)  # (loser_id, keeper_id)
    contradictions: list[tuple[str, str]] = field(default_factory=list)  # (chunk_id_a, chunk_id_b)
    dropped_malformed: list[str] = field(default_factory=list)


def reduce_candidates(
    chunks: list[SubconsciousChunk],
    *,
    similarity_threshold: float,
    contradict_floor: float,
    min_cluster: int,
) -> ReduceResult:
    """Deterministic fan-in reducer for many-workers-to-one-reader candidate sets.

    Drops malformed (empty/whitespace-only) candidates, clusters the rest by
    ``(layer, zone, pipeline_id)`` same as ``consolidate()``, and within any
    cluster at or above ``min_cluster`` in size: merges near-duplicate claims
    down to the highest-trust version (``find_merge_candidates`` /
    ``select_authoritative``), and flags surviving same-cluster pairs as
    contradictions when their similarity clears ``contradict_floor`` (related
    enough to plausibly be about the same thing) but stays below
    ``similarity_threshold`` (not similar enough to merge) *and* exactly one
    side carries a reversal cue the other lacks (``_has_reversal_cue`` --
    "already fixed", "no longer reproduces", "still throws", ...). The
    similarity band alone is not a reliable contradiction signal: two
    paraphrases of the same finding can score just as low as two genuinely
    different claims, so the cue requirement is what actually discriminates
    "status reversal" from "different wording." Grouping and dropping
    duplicates is a deterministic code decision; NCP never resolves a
    contradiction itself, it only surfaces the pair for the reading agent to
    reason about.

    Clusters smaller than ``min_cluster`` pass through unchanged: this is a
    fan-in reducer for genuine high-fanout bursts, not a general dedup pass
    (``consolidate()`` already covers that case as an explicit maintenance
    operation).
    """
    malformed_ids = [c.chunk_id for c in chunks if not c.content.strip()]
    well_formed = [c for c in chunks if c.content.strip()]

    clusters = cluster_by_tags(well_formed)
    clustered_ids = {c.chunk_id for cluster in clusters for c in cluster}
    kept: list[SubconsciousChunk] = [c for c in well_formed if c.chunk_id not in clustered_ids]
    merged_away: list[tuple[str, str]] = []
    contradictions: list[tuple[str, str]] = []

    for cluster in clusters:
        if len(cluster) < min_cluster:
            kept.extend(cluster)
            continue

        merges = find_merge_candidates(cluster, similarity_threshold=similarity_threshold)
        absorbed_ids = {
            c.chunk_id for _, losers in merges for c in losers
        } | {keeper.chunk_id for keeper, _ in merges}
        for keeper, losers in merges:
            merged_away.extend((loser.chunk_id, keeper.chunk_id) for loser in losers)

        standing = [keeper for keeper, _ in merges] + [c for c in cluster if c.chunk_id not in absorbed_ids]
        kept.extend(standing)

        for i in range(len(standing)):
            for j in range(i + 1, len(standing)):
                left, right = standing[i], standing[j]
                if _has_reversal_cue(left.content) == _has_reversal_cue(right.content):
                    continue
                sim = score_pair(left, right, len(cluster))
                if contradict_floor <= sim < similarity_threshold:
                    contradictions.append((left.chunk_id, right.chunk_id))

    return ReduceResult(
        kept=kept,
        merged_away=merged_away,
        contradictions=contradictions,
        dropped_malformed=malformed_ids,
    )
