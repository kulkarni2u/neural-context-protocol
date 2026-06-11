"""Tests for BaseEmbeddingAdapter, OpenAIEmbeddingAdapter, LocalEmbeddingAdapter."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from ncp.adapters.base import NCPAdapterConfigurationError, NCPAdapterResponseError
from ncp.adapters.embedding import (
    BaseEmbeddingAdapter,
    LocalEmbeddingAdapter,
    OpenAIEmbeddingAdapter,
)

_DIM = 1536


class _GoodAdapter(BaseEmbeddingAdapter):
    def embed(self, text: str) -> list[float]:
        return self._validate_dims([0.1] * _DIM)


class _BadDimAdapter(BaseEmbeddingAdapter):
    def embed(self, text: str) -> list[float]:
        return self._validate_dims([0.1] * 100)


def test_base_adapter_passes_correct_dims() -> None:
    assert len(_GoodAdapter().embed("hi")) == _DIM


def test_base_adapter_rejects_wrong_dims() -> None:
    with pytest.raises(NCPAdapterResponseError, match="1536"):
        _BadDimAdapter().embed("hi")


def test_openai_adapter_raises_on_missing_key() -> None:
    pytest.importorskip("openai", reason="openai provider extra not installed")
    with pytest.raises(NCPAdapterConfigurationError, match="OPENAI_API_KEY"):
        OpenAIEmbeddingAdapter(api_key="")


def test_openai_adapter_embed() -> None:
    pytest.importorskip("openai", reason="openai provider extra not installed")
    adapter = OpenAIEmbeddingAdapter(api_key="sk-test")
    mock_resp = MagicMock()
    mock_resp.data = [MagicMock(embedding=[0.5] * _DIM)]
    with patch.object(adapter._client.embeddings, "create", return_value=mock_resp):
        result = adapter.embed("hello world")
    assert len(result) == _DIM
    assert result[0] == pytest.approx(0.5)


def test_openai_adapter_rejects_wrong_dims() -> None:
    pytest.importorskip("openai", reason="openai provider extra not installed")
    adapter = OpenAIEmbeddingAdapter(api_key="sk-test")
    mock_resp = MagicMock()
    mock_resp.data = [MagicMock(embedding=[0.5] * 512)]
    with patch.object(adapter._client.embeddings, "create", return_value=mock_resp):
        with pytest.raises(NCPAdapterResponseError, match="1536"):
            adapter.embed("hello")


def test_local_adapter_embed() -> None:
    adapter = LocalEmbeddingAdapter.__new__(LocalEmbeddingAdapter)
    mock_model = MagicMock()
    mock_model.encode.return_value = MagicMock(tolist=lambda: [0.2] * _DIM)
    adapter._model = mock_model
    result = adapter.embed("hello world")
    assert len(result) == _DIM


def test_local_adapter_rejects_wrong_dims() -> None:
    adapter = LocalEmbeddingAdapter.__new__(LocalEmbeddingAdapter)
    mock_model = MagicMock()
    mock_model.encode.return_value = MagicMock(tolist=lambda: [0.2] * 384)
    adapter._model = mock_model
    with pytest.raises(NCPAdapterResponseError, match="1536"):
        adapter.embed("hello")
