"""Embedding adapters for NCP vector storage."""

from __future__ import annotations

import os
from abc import abstractmethod

from ncp.adapters.base import (
    NCPAdapterConfigurationError,
    NCPAdapterResponseError,
)


class BaseEmbeddingAdapter:
    """Minimal contract for embedding providers. Must return exactly 1536 floats."""

    _REQUIRED_DIMS = 1536

    @abstractmethod
    def embed(self, text: str) -> list[float]:
        """Embed text and return a 1536-dimensional vector."""

    def _validate_dims(self, vector: list[float]) -> list[float]:
        if len(vector) != self._REQUIRED_DIMS:
            raise NCPAdapterResponseError(
                f"Embedding must have {self._REQUIRED_DIMS} dimensions, got {len(vector)}"
            )
        return vector


class OpenAIEmbeddingAdapter(BaseEmbeddingAdapter):
    """Embedding adapter backed by OpenAI text-embedding-3-small (1536 dims)."""

    def __init__(
        self,
        api_key: str = "",
        model: str = "text-embedding-3-small",
        timeout: float = 30.0,
    ) -> None:
        try:
            from openai import OpenAI
        except ImportError as err:
            raise ImportError(
                "openai is required. Install it with: pip install 'neural-context-protocol[providers]'"
            ) from err
        resolved_key = api_key or os.environ.get("OPENAI_API_KEY", "")
        if not resolved_key.strip():
            raise NCPAdapterConfigurationError(
                "OpenAIEmbeddingAdapter requires OPENAI_API_KEY; "
                "configure it or pass api_key explicitly"
            )
        self._client = OpenAI(api_key=resolved_key, timeout=timeout)
        self._model = model

    def embed(self, text: str) -> list[float]:
        try:
            resp = self._client.embeddings.create(input=[text], model=self._model)
            vector = list(resp.data[0].embedding)
        except Exception as exc:
            raise NCPAdapterResponseError(f"OpenAI embeddings call failed: {exc}") from exc
        return self._validate_dims(vector)


class LocalEmbeddingAdapter(BaseEmbeddingAdapter):
    """Embedding adapter backed by sentence-transformers (model must output 1536 dims)."""

    def __init__(self, model: str = "sentence-transformers/all-MiniLM-L6-v2") -> None:
        try:
            from sentence_transformers import SentenceTransformer
        except ImportError as err:
            raise ImportError(
                "sentence-transformers is required. "
                "Install it with: pip install sentence-transformers"
            ) from err
        self._model = SentenceTransformer(model)

    def embed(self, text: str) -> list[float]:
        try:
            vector = self._model.encode(text, convert_to_numpy=True).tolist()
        except Exception as exc:
            raise NCPAdapterResponseError(f"Local embedding call failed: {exc}") from exc
        return self._validate_dims(vector)
