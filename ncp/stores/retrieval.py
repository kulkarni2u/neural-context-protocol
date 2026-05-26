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


@dataclass
class RetrievalPolicy:
    """Configurable weights for multi-signal retrieval score fusion."""

    w_lexical: float = 0.5
    w_recency: float = 0.3
    w_trust: float = 0.2
    recency_half_life_seconds: float = 14400.0  # 4-hour default

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

    def score(
        self,
        *,
        bm25_normalized: float,
        age_seconds: float,
        base_trust: float,
        generation: int = 0,
    ) -> float:
        """Compute fused hybrid score for a single candidate chunk.

        Returns a value in [0, 1]. Generation penalty is applied
        multiplicatively so heavily-derived chunks are naturally demoted.
        """
        recency = math.exp(-0.693 * max(0.0, age_seconds) / self.recency_half_life_seconds)
        gen_penalty = 0.9 ** max(0, generation)
        fused = (
            self.w_lexical * max(0.0, bm25_normalized)
            + self.w_recency * recency
            + self.w_trust * max(0.0, min(1.0, base_trust))
        )
        return fused * gen_penalty


DEFAULT_RETRIEVAL_POLICY = RetrievalPolicy()
