"""Deterministic graph-path scoring and selection (design §6.2 steps 6-7).

``score = 0.40*query_relevance + 0.25*evidence_quality + 0.15*freshness
+ 0.10*distance_score + 0.10*diversity``, every term in [0, 1]:

* query_relevance: stemmed overlap between the request's content words and
  the path's entity keys + relation type names, over the smaller set.
* evidence_quality: weakest relation on the path, each
  ``0.5*confidence + 0.5*min(1, live_evidence/2)``. Chunk trust is *not*
  propagated onto entity truth.
* freshness: weakest relation, ``0.5 ** (age_days / 30)`` from ``observed_at``.
* distance_score: ``1 / hops``.
* diversity: share of the path's cited evidence chunks that are neither in
  the already-retrieved memory context nor cited by a higher-ranked path
  (external-attestation-only paths count 0.5). It depends on what was
  already picked, so selection is greedy.

Ties break on (path_length, relation ids). The weights are the design's
initial hypothesis, not a benchmarked set.
"""

from __future__ import annotations

import re
from collections.abc import Collection, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone

from ncp.ontology.models import Entity, Relation
from ncp.ontology.resolution import stem, tokenize

WEIGHTS: dict[str, float] = {
    "query_relevance": 0.40,
    "evidence_quality": 0.25,
    "freshness": 0.15,
    "distance_score": 0.10,
    "diversity": 0.10,
}
FRESHNESS_HALF_LIFE_DAYS = 30.0


@dataclass(frozen=True, slots=True)
class PathCandidate:
    """A traversed path whose every relation has live, readable evidence."""

    nodes: tuple[Entity, ...]
    relations: tuple[Relation, ...]
    # Per relation: live chunk evidence ids readable by the caller (sorted)
    # and the count of valid external attestations.
    chunk_evidence: tuple[tuple[str, ...], ...]
    external_evidence: tuple[int, ...]

    @property
    def hops(self) -> int:
        return len(self.relations)

    @property
    def relation_ids(self) -> tuple[str, ...]:
        return tuple(str(relation.relation_id) for relation in self.relations)

    @property
    def evidence_ids(self) -> tuple[str, ...]:
        seen: dict[str, None] = {}
        for ids in self.chunk_evidence:
            for chunk_id in ids:
                seen.setdefault(chunk_id, None)
        return tuple(seen)


@dataclass(frozen=True, slots=True)
class RankedPath:
    candidate: PathCandidate
    score: float
    components: dict[str, float]


def _clamp(value: float) -> float:
    return max(0.0, min(1.0, value))


def _stems(words: Sequence[str]) -> set[str]:
    return {stem(part) for word in words for part in re.split(r"[\W_]+", word) if part}


def _path_stems(candidate: PathCandidate) -> set[str]:
    words: list[str] = []
    for entity in candidate.nodes:
        words.extend(tokenize(entity.canonical_key))
    for relation in candidate.relations:
        words.append(relation.relation_type.lower())
    return _stems(words)


def base_components(
    candidate: PathCandidate, *, query_stems: Collection[str], now: datetime,
) -> dict[str, float]:
    path_stems = _path_stems(candidate)
    denominator = min(len(query_stems), len(path_stems))
    relevance = len(path_stems & set(query_stems)) / denominator if denominator else 0.0
    quality = min(
        0.5 * relation.confidence
        + 0.5 * min(1.0, (len(chunks) + external) / 2.0)
        for relation, chunks, external in zip(
            candidate.relations, candidate.chunk_evidence, candidate.external_evidence,
            strict=True,
        )
    )
    freshness = min(
        0.5 ** (max(0.0, (now - relation.observed_at).total_seconds()) / 86_400.0
                / FRESHNESS_HALF_LIFE_DAYS)
        for relation in candidate.relations
    )
    return {
        "query_relevance": round(_clamp(relevance), 6),
        "evidence_quality": round(_clamp(quality), 6),
        "freshness": round(_clamp(freshness), 6),
        "distance_score": round(_clamp(1.0 / max(1, candidate.hops)), 6),
    }


def _diversity(candidate: PathCandidate, seen: Collection[str]) -> float:
    cited = candidate.evidence_ids
    if not cited:
        return 0.5
    return round(sum(1 for chunk_id in cited if chunk_id not in seen) / len(cited), 6)


def _total(components: dict[str, float]) -> float:
    return round(sum(WEIGHTS[name] * value for name, value in components.items()), 6)


def rank_paths(
    candidates: Sequence[PathCandidate],
    *,
    query_terms: Sequence[str],
    context_chunk_ids: Collection[str] = (),
    now: datetime | None = None,
) -> list[RankedPath]:
    """All candidates in greedy score order; duplicates (same relation chain)
    are dropped. ``context_chunk_ids`` are the memory chunks already in the
    context, used by the diversity term to dedup evidence."""
    moment = now or datetime.now(timezone.utc)
    query_stems = _stems(query_terms)
    unique: dict[tuple[str, ...], PathCandidate] = {}
    for candidate in candidates:
        unique.setdefault(candidate.relation_ids, candidate)
    pool = [
        (candidate, base_components(candidate, query_stems=query_stems, now=moment))
        for candidate in unique.values()
    ]
    seen = set(context_chunk_ids)
    ranked: list[RankedPath] = []
    while pool:
        best_index = 0
        best_key: tuple[float, int, tuple[str, ...]] | None = None
        best_components: dict[str, float] = {}
        for index, (candidate, components) in enumerate(pool):
            full = {**components, "diversity": _diversity(candidate, seen)}
            key = (-_total(full), candidate.hops, candidate.relation_ids)
            if best_key is None or key < best_key:
                best_index, best_key, best_components = index, key, full
        candidate, _ = pool.pop(best_index)
        ranked.append(RankedPath(candidate, _total(best_components), best_components))
        seen.update(candidate.evidence_ids)
    return ranked


__all__ = ["FRESHNESS_HALF_LIFE_DAYS", "PathCandidate", "RankedPath", "WEIGHTS", "rank_paths"]
