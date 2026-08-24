"""Embedding adapters for NCP vector storage."""

from __future__ import annotations

import os
from abc import abstractmethod

from ncp.adapters.base import (
    NCPAdapterConfigurationError,
    NCPAdapterResponseError,
)


class BaseEmbeddingAdapter:
    """Minimal contract for embedding providers."""

    @abstractmethod
    def embed(self, text: str) -> list[float]:
        """Embed text and return a non-empty numeric vector."""

    def _validate_vector(self, vector: list[float]) -> list[float]:
        if not vector:
            raise NCPAdapterResponseError("Embedding must be a non-empty vector")
        try:
            return [float(value) for value in vector]
        except (TypeError, ValueError) as exc:
            raise NCPAdapterResponseError("Embedding must contain only numbers") from exc


class OpenAIEmbeddingAdapter(BaseEmbeddingAdapter):
    """Embedding adapter backed by OpenAI text-embedding-3-small (1536 dims)."""

    _REQUIRED_DIMS = 1536

    def _validate_dims(self, vector: list[float]) -> list[float]:
        vector = self._validate_vector(vector)
        if len(vector) != self._REQUIRED_DIMS:
            raise NCPAdapterResponseError(
                f"Embedding must have {self._REQUIRED_DIMS} dimensions, got {len(vector)}"
            )
        return vector

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
    """Embedding adapter backed by fastembed local models.

    Model construction is deferred to the first ``embed()`` call rather than
    done in ``__init__``. ``TextEmbedding(model_name=...)`` downloads the
    model (~130MB for the default BAAI/bge-small-en-v1.5) from Hugging Face
    on first-ever use if it isn't already cached locally, which can block
    for a long time -- or hang until a timeout -- on a slow, offline, or
    firewalled network. Since [embedding].enabled now defaults to true, that
    call used to run synchronously inside every default ``create_store()``,
    including at ``ncp serve`` startup. Deferring it means startup stays
    instant, and only the first real ``embed()`` call pays the download
    cost (or fails, gracefully -- see ``ncp/stores/*.py``'s ``_try_embed``
    helpers, which catch a failure here and fall back to lexical-only
    retrieval for the rest of that store's lifetime).
    """

    def __init__(self, model: str = "BAAI/bge-small-en-v1.5") -> None:
        try:
            from fastembed import TextEmbedding
        except ImportError as err:
            raise ImportError(
                "fastembed is required for [embedding].provider = 'local'. "
                "Install it with: pip install 'neural-context-protocol[local-embeddings]'"
            ) from err
        # Only the import happens here -- fast, no network -- so
        # `_build_embedding_adapter` can still fast-fail immediately when
        # the optional dependency genuinely isn't installed. The actual
        # `TextEmbedding(...)` construction (which may hit the network) is
        # deferred to the first `embed()` call below.
        self._TextEmbedding = TextEmbedding
        self._model_name = model
        self._model: object | None = None

    def embed(self, text: str) -> list[float]:
        try:
            if self._model is None:
                self._model = self._TextEmbedding(model_name=self._model_name)
            first = next(iter(self._model.embed([text])))
            vector = first.tolist() if hasattr(first, "tolist") else list(first)
        except Exception as exc:
            raise NCPAdapterResponseError(f"Local embedding call failed: {exc}") from exc
        return self._validate_vector(vector)
