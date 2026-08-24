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
        return self._validate_vector([0.1] * _DIM)


class _BadDimAdapter(BaseEmbeddingAdapter):
    def embed(self, text: str) -> list[float]:
        return self._validate_vector([])


def test_base_adapter_passes_correct_dims() -> None:
    assert len(_GoodAdapter().embed("hi")) == _DIM


def test_base_adapter_rejects_wrong_dims() -> None:
    with pytest.raises(NCPAdapterResponseError, match="non-empty"):
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
    mock_model.embed.return_value = iter([MagicMock(tolist=lambda: [0.2] * 384)])
    adapter._model = mock_model
    result = adapter.embed("hello world")
    assert len(result) == 384


def test_local_adapter_rejects_empty_vector() -> None:
    adapter = LocalEmbeddingAdapter.__new__(LocalEmbeddingAdapter)
    mock_model = MagicMock()
    mock_model.embed.return_value = iter([MagicMock(tolist=lambda: [])])
    adapter._model = mock_model
    with pytest.raises(NCPAdapterResponseError, match="non-empty"):
        adapter.embed("hello")


def test_local_adapter_missing_extra_error_mentions_install() -> None:
    with patch.dict("sys.modules", {"fastembed": None}):
        with pytest.raises(ImportError, match="local-embeddings"):
            LocalEmbeddingAdapter()


def _fake_fastembed_module(text_embedding_cls: type) -> MagicMock:
    module = MagicMock()
    module.TextEmbedding = text_embedding_cls
    return module


def test_local_adapter_init_does_not_construct_model() -> None:
    """__init__ must only import fastembed (fast, no network) and must NOT
    call TextEmbedding(model_name=...), since that constructor downloads the
    model on first-ever use and can block for a long time on a slow/offline
    network. Construction is deferred to the first embed() call."""
    construct_calls: list[str] = []

    class _RecordingTextEmbedding:
        def __init__(self, model_name: str) -> None:
            construct_calls.append(model_name)

    with patch.dict(
        "sys.modules", {"fastembed": _fake_fastembed_module(_RecordingTextEmbedding)}
    ):
        adapter = LocalEmbeddingAdapter(model="some/model")

    assert construct_calls == []
    assert adapter._model is None
    assert adapter._model_name == "some/model"


def test_local_adapter_constructs_model_lazily_on_first_embed_only() -> None:
    construct_calls: list[str] = []

    class _RecordingTextEmbedding:
        def __init__(self, model_name: str) -> None:
            construct_calls.append(model_name)

        def embed(self, texts: list[str]) -> list[list[float]]:
            return [[0.3] * 384]

    with patch.dict(
        "sys.modules", {"fastembed": _fake_fastembed_module(_RecordingTextEmbedding)}
    ):
        adapter = LocalEmbeddingAdapter(model="some/model")
        assert construct_calls == []

        first = adapter.embed("hello")
        assert len(first) == 384
        assert construct_calls == ["some/model"]

        adapter.embed("world again")
        assert construct_calls == ["some/model"]  # not reconstructed on 2nd call


def test_local_adapter_raises_ncp_error_when_lazy_construction_fails() -> None:
    """A network/model-download failure on first use must raise
    NCPAdapterResponseError exactly like any other embed-time failure --
    not crash the caller with a raw exception."""

    class _FailingTextEmbedding:
        def __init__(self, model_name: str) -> None:
            raise RuntimeError("simulated network timeout downloading model")

    with patch.dict(
        "sys.modules", {"fastembed": _fake_fastembed_module(_FailingTextEmbedding)}
    ):
        adapter = LocalEmbeddingAdapter(model="some/model")
        with pytest.raises(NCPAdapterResponseError, match="Local embedding call failed"):
            adapter.embed("hello")
