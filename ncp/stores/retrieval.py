"""Multi-signal retrieval policy for hybrid score fusion.

Both SQLiteStore and PgvectorStore use RetrievalPolicy to keep retrieval
behavior aligned across backends. The policy combines three signals:

  - Lexical (BM25): normalized 0-1 relevance to the query
  - Recency:        exponential decay from creation time
  - Trust:          base_trust field set at write time

Weights must sum to 1.0. Generation penalty is applied multiplicatively
after fusion so old derived chunks are demoted regardless of signal mix.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Callable, TypeVar

from rank_bm25 import BM25Okapi


T = TypeVar("T")


@dataclass
class RetrievalPolicy:
    """Configurable weights for multi-signal retrieval score fusion."""

    w_lexical: float = 0.5
    w_recency: float = 0.3
    w_trust: float = 0.2
    recency_half_life_seconds: float = 14400.0  # 4-hour default
    generation_penalty_base: float = 0.9  # per-generation score multiplier

    def __post_init__(self) -> None:
        total = self.w_lexical + self.w_recency + self.w_trust
        if abs(total - 1.0) > 1e-6:
            raise ValueError(
                f"RetrievalPolicy weights must sum to 1.0, got {total:.6f}"
            )
        for name, val in (
            ("w_lexical", self.w_lexical),
            ("w_recency", self.w_recency),
            ("w_trust", self.w_trust),
        ):
            if not 0.0 <= val <= 1.0:
                raise ValueError(f"{name} must be in [0.0, 1.0], got {val}")
        if self.recency_half_life_seconds <= 0:
            raise ValueError("recency_half_life_seconds must be > 0")
        if not 0.0 < self.generation_penalty_base <= 1.0:
            raise ValueError("generation_penalty_base must be in (0.0, 1.0]")

    def score(
        self,
        *,
        bm25_normalized: float,
        age_seconds: float,
        base_trust: float,
        generation: int = 0,
        written_at_drift: float = 0.0,
    ) -> float:
        """Compute fused hybrid score for a single candidate chunk.

        Returns a value in [0, 1]. Generation penalty is applied
        multiplicatively so heavily-derived chunks are naturally demoted.
        Chunks written during high drift (written_at_drift > 0.3) are
        discounted by (1 - written_at_drift).
        """
        recency = math.exp(-0.693 * max(0.0, age_seconds) / self.recency_half_life_seconds)
        gen_penalty = self.generation_penalty_base ** max(0, generation)
        drift = max(0.0, min(1.0, written_at_drift))
        drift_penalty = 1.0 - drift if drift > 0.3 else 1.0
        fused = (
            self.w_lexical * max(0.0, bm25_normalized)
            + self.w_recency * recency
            + self.w_trust * max(0.0, min(1.0, base_trust))
        )
        return fused * gen_penalty * drift_penalty

    def score_with_vector(
        self,
        *,
        bm25_normalized: float,
        vector_normalized: float | None,
        age_seconds: float,
        base_trust: float,
        generation: int = 0,
        vector_mix: float = 0.5,
        written_at_drift: float = 0.0,
    ) -> float:
        """Compute hybrid score with an optional vector similarity signal.

        The existing lexical weight is preserved as the total "relevance"
        budget. When a vector score is present, lexical and vector signals are
        blended within that budget using ``vector_mix``.
        """
        if vector_normalized is None:
            return self.score(
                bm25_normalized=bm25_normalized,
                age_seconds=age_seconds,
                base_trust=base_trust,
                generation=generation,
                written_at_drift=written_at_drift,
            )

        mix = max(0.0, min(1.0, vector_mix))
        lexical = max(0.0, min(1.0, bm25_normalized))
        vector = max(0.0, min(1.0, vector_normalized))
        blended_relevance = ((1.0 - mix) * lexical) + (mix * vector)
        return self.score(
            bm25_normalized=blended_relevance,
            age_seconds=age_seconds,
            base_trust=base_trust,
            generation=generation,
            written_at_drift=written_at_drift,
        )

    def score_no_bm25(
        self,
        *,
        age_seconds: float,
        base_trust: float,
        generation: int = 0,
        written_at_drift: float = 0.0,
    ) -> float:
        """Score without BM25 for non-lexical backends.

        Weights are renormalized to (w_recency + w_trust) so the result
        stays in [0, 1] even though the lexical signal is absent.
        Generation penalty is still applied multiplicatively.
        Chunks written during high drift (written_at_drift > 0.3) are
        discounted by (1 - written_at_drift).
        """
        recency = math.exp(-0.693 * max(0.0, age_seconds) / self.recency_half_life_seconds)
        gen_penalty = self.generation_penalty_base ** max(0, generation)
        drift = max(0.0, min(1.0, written_at_drift))
        drift_penalty = 1.0 - drift if drift > 0.3 else 1.0
        w_sum = self.w_recency + self.w_trust
        if w_sum == 0.0:
            return 0.0
        fused = (
            self.w_recency * recency
            + self.w_trust * max(0.0, min(1.0, base_trust))
        ) / w_sum
        return fused * gen_penalty * drift_penalty


DEFAULT_RETRIEVAL_POLICY = RetrievalPolicy()


@dataclass(frozen=True)
class LexicalCandidate:
    """Shared lexical retrieval inputs for a single candidate row."""

    doc_tokens: list[str]
    lexical_signal: float | None


def normalize_query_terms(text: str) -> set[str]:
    """Normalize user query text into a lowercased term set."""
    return {term for term in text.lower().split() if term}


def lexical_signal_for_candidate(
    *,
    query_terms: set[str],
    doc_tokens: list[str],
    bm25_normalized: float,
) -> float | None:
    """Return lexical relevance budget or None when the candidate should be skipped.

    Blank queries intentionally treat every candidate as eligible and use the
    full lexical budget so trust/recency (and optional vector signals) can rank
    the current working set without accidental BM25 noise.
    """
    if not query_terms:
        return 1.0
    if not query_terms.intersection(set(doc_tokens)):
        return None
    return max(0.0, min(1.0, bm25_normalized))


def normalize_bm25_scores(raw_scores: list[float]) -> list[float]:
    """Normalize BM25 scores into [0, 1] using the max-score guard."""
    if not raw_scores:
        return []
    max_bm25 = max(raw_scores)
    if max_bm25 <= 0.0:
        return [0.0] * len(raw_scores)
    return [score / max_bm25 for score in raw_scores]


def build_lexical_candidates(text: str, documents: list[str]) -> list[LexicalCandidate]:
    """Build normalized lexical candidate signals in the input row order."""
    query_terms = normalize_query_terms(text)
    corpus = [document.lower().split() for document in documents]
    if not corpus:
        return []
    bm25 = BM25Okapi(corpus)
    raw_scores = bm25.get_scores(text.split())
    normalized_scores = normalize_bm25_scores(list(raw_scores))
    return [
        LexicalCandidate(
            doc_tokens=doc_tokens,
            lexical_signal=lexical_signal_for_candidate(
                query_terms=query_terms,
                doc_tokens=doc_tokens,
                bm25_normalized=normalized,
            ),
        )
        for normalized, doc_tokens in zip(normalized_scores, corpus, strict=True)
    ]


def score_trust_recency_candidate(
    policy: RetrievalPolicy,
    *,
    created_at: float,
    now: float,
    base_trust: float,
    generation: int,
    written_at_drift: float = 0.0,
) -> float:
    """Shared trust/recency-only candidate score."""
    age_seconds = max(0.0, now - created_at)
    return policy.score_no_bm25(
        age_seconds=age_seconds,
        base_trust=base_trust,
        generation=generation,
        written_at_drift=written_at_drift,
    )


def score_vector_distance(distance: float | None) -> float:
    """Convert pgvector distance into the shared [0, 1] relevance space."""
    if distance is None:
        distance = 1.0
    bounded = max(0.0, float(distance))
    return 1.0 / (1.0 + bounded)


def normalize_result_limit(k: int) -> int:
    """Normalize requested result count to the runtime minimum contract."""
    return max(1, k)


def apply_diversity_limit(
    ranked: list[T],
    *,
    k: int,
    diversity_limit: int,
    author_getter: Callable[[T], str],
) -> list[T]:
    """Trim ranked candidates using the shared author-diversity contract."""
    result_cap = normalize_result_limit(k)
    per_author_cap = max(1, diversity_limit)
    author_count: dict[str, int] = {}
    results: list[T] = []
    for item in ranked:
        author = author_getter(item)
        if author_count.get(author, 0) >= per_author_cap:
            continue
        author_count[author] = author_count.get(author, 0) + 1
        results.append(item)
        if len(results) >= result_cap:
            break
    return results
