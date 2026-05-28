"""Cross-Encoder Reranker for NCP query operations."""

from __future__ import annotations

import json
import math
from os import environ
import warnings
from typing import Sequence

from ncp.config import NCPConfig
from ncp.types import SubconsciousChunk


class Reranker:
    """Multi-provider reranker for retrieved memory chunks."""

    def __init__(self, config: NCPConfig) -> None:
        self.enabled = config.rerank_enabled
        self.provider = config.rerank_provider.strip().lower() if config.rerank_provider else "local"
        self.model = config.rerank_model

    def rerank(self, query: str, chunks: Sequence[SubconsciousChunk]) -> list[SubconsciousChunk]:
        """Rerank candidates based on semantic cross-encoder scores."""
        if not self.enabled or not chunks:
            return list(chunks)

        chunk_list = list(chunks)
        if self.provider == "cohere":
            return self._rerank_cohere(query, chunk_list)
        return self._rerank_local(query, chunk_list)

    def _rerank_local(self, query: str, chunks: list[SubconsciousChunk]) -> list[SubconsciousChunk]:
        """Local reranker utilizing sentence-transformers if available, falling back to Jaccard."""
        try:
            from sentence_transformers import CrossEncoder
            encoder = CrossEncoder(self.model or "cross-encoder/ms-marco-MiniLM-L-6-v2")
            pairs = [(query, chunk.content) for chunk in chunks]
            scores = encoder.predict(pairs)
            for chunk, score in zip(chunks, scores):
                sigmoid = 1.0 / (1.0 + math.exp(-float(score)))
                chunk.relevance = max(0.0, min(1.0, sigmoid))
        except ImportError:
            warnings.warn(
                "sentence-transformers not installed. Local rerank falling back to Jaccard similarity. "
                "Install via: pip install sentence-transformers",
                ImportWarning,
            )
            query_set = set(query.lower().split())
            for chunk in chunks:
                doc_set = set(chunk.content.lower().split())
                intersection = query_set.intersection(doc_set)
                union = query_set.union(doc_set)
                jaccard = len(intersection) / len(union) if union else 0.0
                chunk.relevance = max(0.0, min(1.0, 0.7 * jaccard + 0.3 * chunk.base_trust))

        return sorted(chunks, key=lambda c: c.relevance, reverse=True)

    def _rerank_cohere(self, query: str, chunks: list[SubconsciousChunk]) -> list[SubconsciousChunk]:
        """Reranker querying Cohere's Rerank API endpoint."""
        api_key = environ.get("COHERE_API_KEY", "")
        if not api_key:
            warnings.warn(
                "COHERE_API_KEY environment variable not set. Cohere reranker falling back to Jaccard similarity.",
                RuntimeWarning,
            )
            return self._rerank_local(query, chunks)

        try:
            import cohere
            client = cohere.Client(api_key=api_key)
            resp = client.rerank(
                model=self.model or "rerank-english-v3.0",
                query=query,
                documents=[chunk.content for chunk in chunks],
            )
            for result in resp.results:
                index = result.index
                score = result.relevance_score
                chunks[index].relevance = max(0.0, min(1.0, float(score)))
        except Exception as exc:
            warnings.warn(
                f"Cohere Rerank call failed: {exc}. Cohere reranker falling back to Jaccard similarity.",
                RuntimeWarning,
            )
            return self._rerank_local(query, chunks)

        return sorted(chunks, key=lambda c: c.relevance, reverse=True)
