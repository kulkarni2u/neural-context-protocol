"""Cross-Encoder Reranker for NCP query operations."""

from __future__ import annotations

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
        # Lazily built and cached on first use so repeated rerank() calls
        # reuse the loaded cross-encoder/client instead of reloading it
        # (a cross-encoder load can take hundreds of ms to seconds).
        self._local_encoder: object | None = None
        self._local_encoder_unavailable = False
        self._cohere_client: object | None = None

    def rerank(self, query: str, chunks: Sequence[SubconsciousChunk]) -> list[SubconsciousChunk]:
        """Rerank candidates based on semantic cross-encoder scores."""
        if not self.enabled or not chunks:
            return list(chunks)

        chunk_list = list(chunks)
        if self.provider == "cohere":
            return self._rerank_cohere(query, chunk_list)
        return self._rerank_local(query, chunk_list)

    def _get_local_encoder(self) -> object | None:
        if self._local_encoder is not None:
            return self._local_encoder
        if self._local_encoder_unavailable:
            return None
        try:
            from sentence_transformers import CrossEncoder
            self._local_encoder = CrossEncoder(self.model or "cross-encoder/ms-marco-MiniLM-L-6-v2")
        except ImportError:
            self._local_encoder_unavailable = True
            warnings.warn(
                "sentence-transformers not installed. Local rerank falling back to Jaccard similarity. "
                "Install via: pip install sentence-transformers",
                ImportWarning,
            )
            return None
        return self._local_encoder

    def _rerank_local(self, query: str, chunks: list[SubconsciousChunk]) -> list[SubconsciousChunk]:
        """Local reranker utilizing sentence-transformers if available, falling back to Jaccard."""
        encoder = self._get_local_encoder()
        if encoder is not None:
            pairs = [(query, chunk.content) for chunk in chunks]
            scores = encoder.predict(pairs)  # type: ignore[attr-defined]
            for chunk, score in zip(chunks, scores):
                sigmoid = 1.0 / (1.0 + math.exp(-float(score)))
                chunk.relevance = max(0.0, min(1.0, sigmoid))
        else:
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
            if self._cohere_client is None:
                import cohere
                self._cohere_client = cohere.Client(api_key=api_key)
            client = self._cohere_client
            resp = client.rerank(  # type: ignore[attr-defined]
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
