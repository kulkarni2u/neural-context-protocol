from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from ncp.adapters.anthropic import AnthropicAdapter
from ncp.adapters.base import (
    NCPAdapterConfigurationError,
    NCPAdapterError,
    NCPAdapterResponseError,
    NCPAdapterTimeoutError,
)
from ncp.adapters.cohere import CohereAdapter
from ncp.adapters.gemini import GeminiAdapter
from ncp.adapters.mistral import MistralAdapter
from ncp.adapters.ollama import OllamaAdapter
from ncp.adapters.openai import OpenAIAdapter

NCP_CTX = "You are a helpful assistant."
USER_TURN = "What is the capital of France?"


def _mock_anthropic_msg(text: str) -> MagicMock:
    content_block = MagicMock()
    content_block.type = "text"
    content_block.text = text
    msg = MagicMock()
    msg.content = [content_block]
    return msg


class TestAnthropicAdapter:
    def test_call(self) -> None:
        adapter = AnthropicAdapter(api_key="test-key", model="claude-sonnet-4-20250514")
        with patch.object(adapter._client.messages, "create") as mock_create:
            mock_create.return_value = _mock_anthropic_msg("Paris")
            result = adapter.call(NCP_CTX, USER_TURN)

        assert result == "Paris"
        mock_create.assert_called_once_with(
            model="claude-sonnet-4-20250514",
            max_tokens=4096,
            system=NCP_CTX,
            messages=[{"role": "user", "content": USER_TURN}],
        )

    def test_stream(self) -> None:
        adapter = AnthropicAdapter(api_key="test-key", model="claude-sonnet-4-20250514")

        def _events() -> object:
            for text in ["Hel", "lo", " Paris"]:
                delta = MagicMock()
                delta.type = "text_delta"
                delta.text = text
                event = MagicMock()
                event.type = "content_block_delta"
                event.delta = delta
                yield event

        class _FakeStream:
            def __iter__(self) -> object:
                return _events()

            def __enter__(self) -> _FakeStream:
                return self

            def __exit__(self, *a: object) -> None:
                pass

        with patch.object(adapter._client.messages, "stream") as mock_stream:
            mock_stream.return_value = _FakeStream()
            result = list(adapter.stream(NCP_CTX, USER_TURN))

        assert result == ["Hel", "lo", " Paris"]

    def test_ctx_window(self) -> None:
        assert AnthropicAdapter(api_key="x").ctx_window == 200000


class TestOpenAIAdapter:
    def test_call(self) -> None:
        adapter = OpenAIAdapter(api_key="test-key")
        choice = MagicMock()
        choice.message.content = "Paris"
        resp = MagicMock()
        resp.choices = [choice]

        with patch.object(adapter._client.chat.completions, "create") as mock_create:
            mock_create.return_value = resp
            result = adapter.call(NCP_CTX, USER_TURN)

        assert result == "Paris"
        mock_create.assert_called_once()
        kwargs = mock_create.call_args[1]
        assert kwargs["model"] == "gpt-4o"
        assert len(kwargs["messages"]) == 2
        assert kwargs["messages"][0]["content"] == NCP_CTX
        assert kwargs["messages"][1]["content"] == USER_TURN

    def test_stream(self) -> None:
        adapter = OpenAIAdapter(api_key="test-key")
        chunks = ["Par", "is"]

        def _chunks() -> object:
            for text in chunks:
                delta = MagicMock()
                delta.content = text
                choice = MagicMock()
                choice.delta = delta
                yield type("Chunk", (), {"choices": [choice]})()

        with patch.object(adapter._client.chat.completions, "create") as mock_create:
            mock_create.return_value = _chunks()
            result = list(adapter.stream(NCP_CTX, USER_TURN))

        assert result == chunks

    def test_ctx_window_default(self) -> None:
        assert OpenAIAdapter(api_key="x").ctx_window == 128000

    def test_ctx_window_known_model(self) -> None:
        adapter = OpenAIAdapter(api_key="x", model="gpt-4.1")
        assert adapter.ctx_window == 1047576


class TestOllamaAdapter:
    def test_call(self) -> None:
        adapter = OllamaAdapter(model="llama3.1")
        choice = MagicMock()
        choice.message.content = "Paris"
        resp = MagicMock()
        resp.choices = [choice]

        with patch.object(adapter._client.chat.completions, "create") as mock_create:
            mock_create.return_value = resp
            result = adapter.call(NCP_CTX, USER_TURN)

        assert result == "Paris"
        mock_create.assert_called_once()
        kwargs = mock_create.call_args[1]
        assert kwargs["model"] == "llama3.1"

    def test_stream_raises(self) -> None:
        adapter = OllamaAdapter()
        with pytest.raises(NotImplementedError):
            list(adapter.stream(NCP_CTX, USER_TURN))

    def test_ctx_window(self) -> None:
        assert OllamaAdapter().ctx_window == 8192


class TestGeminiAdapter:
    def test_call(self) -> None:
        adapter = GeminiAdapter(api_key="test-key", model="gemini-2.0-flash")
        resp = MagicMock()
        resp.text = "Paris"

        with patch.object(adapter._model, "generate_content") as mock_gen:
            mock_gen.return_value = resp
            result = adapter.call(NCP_CTX, USER_TURN)

        assert result == "Paris"
        mock_gen.assert_called_once()
        args, kwargs = mock_gen.call_args
        assert NCP_CTX in args[0]
        assert USER_TURN in args[0]
        assert "timeout" in kwargs.get("request_options", {})

    def test_stream_raises(self) -> None:
        adapter = GeminiAdapter(api_key="test-key")
        with pytest.raises(NotImplementedError):
            list(adapter.stream(NCP_CTX, USER_TURN))

    def test_ctx_window(self) -> None:
        assert GeminiAdapter(api_key="x").ctx_window == 1000000


class TestMistralAdapter:
    def test_call(self) -> None:
        adapter = MistralAdapter(api_key="test-key", model="mistral-large-latest")
        choice = MagicMock()
        choice.message.content = "Paris"
        resp = MagicMock()
        resp.choices = [choice]

        with patch.object(adapter._client.chat, "complete") as mock_complete:
            mock_complete.return_value = resp
            result = adapter.call(NCP_CTX, USER_TURN)

        assert result == "Paris"
        mock_complete.assert_called_once()
        kwargs = mock_complete.call_args[1]
        assert kwargs["model"] == "mistral-large-latest"

    def test_stream_raises(self) -> None:
        adapter = MistralAdapter(api_key="x")
        with pytest.raises(NotImplementedError):
            list(adapter.stream(NCP_CTX, USER_TURN))

    def test_ctx_window(self) -> None:
        assert MistralAdapter(api_key="x").ctx_window == 128000


class TestCohereAdapter:
    def test_call(self) -> None:
        adapter = CohereAdapter(api_key="test-key", model="command-a-03-2025")
        resp = MagicMock()
        resp.text = "Paris"

        with patch.object(adapter._client, "chat") as mock_chat:
            mock_chat.return_value = resp
            result = adapter.call(NCP_CTX, USER_TURN)

        assert result == "Paris"
        mock_chat.assert_called_once()
        kwargs = mock_chat.call_args[1]
        assert kwargs["model"] == "command-a-03-2025"

    def test_stream_raises(self) -> None:
        adapter = CohereAdapter(api_key="x")
        with pytest.raises(NotImplementedError):
            list(adapter.stream(NCP_CTX, USER_TURN))

    def test_ctx_window(self) -> None:
        assert CohereAdapter(api_key="x").ctx_window == 128000


class TestGoldenContextParity:
    """Parity check 1: same context + user turn gives a clean response on all 6."""

    GOLDEN_CTX = "You are a geography expert. Answer concisely in one word."
    GOLDEN_TURN = "Capital of France?"

    def test_anthropic(self) -> None:
        adapter = AnthropicAdapter(api_key="test-key")
        with patch.object(adapter._client.messages, "create") as mock_create:
            mock_create.return_value = _mock_anthropic_msg("Paris")
            result = adapter.call(self.GOLDEN_CTX, self.GOLDEN_TURN)
        assert isinstance(result, str)
        assert len(result) > 0

    def test_openai(self) -> None:
        adapter = OpenAIAdapter(api_key="test-key")
        choice = MagicMock()
        choice.message.content = "Paris"
        resp = MagicMock()
        resp.choices = [choice]

        with patch.object(adapter._client.chat.completions, "create") as mock_create:
            mock_create.return_value = resp
            result = adapter.call(self.GOLDEN_CTX, self.GOLDEN_TURN)
        assert isinstance(result, str)
        assert len(result) > 0

    def test_ollama(self) -> None:
        adapter = OllamaAdapter()
        choice = MagicMock()
        choice.message.content = "Paris"
        resp = MagicMock()
        resp.choices = [choice]

        with patch.object(adapter._client.chat.completions, "create") as mock_create:
            mock_create.return_value = resp
            result = adapter.call(self.GOLDEN_CTX, self.GOLDEN_TURN)
        assert isinstance(result, str)
        assert len(result) > 0

    def test_gemini(self) -> None:
        adapter = GeminiAdapter(api_key="test-key")
        resp = MagicMock()
        resp.text = "Paris"

        with patch.object(adapter._model, "generate_content") as mock_gen:
            mock_gen.return_value = resp
            result = adapter.call(self.GOLDEN_CTX, self.GOLDEN_TURN)
        assert isinstance(result, str)
        assert len(result) > 0

    def test_mistral(self) -> None:
        adapter = MistralAdapter(api_key="test-key")
        choice = MagicMock()
        choice.message.content = "Paris"
        resp = MagicMock()
        resp.choices = [choice]

        with patch.object(adapter._client.chat, "complete") as mock_complete:
            mock_complete.return_value = resp
            result = adapter.call(self.GOLDEN_CTX, self.GOLDEN_TURN)
        assert isinstance(result, str)
        assert len(result) > 0

    def test_cohere(self) -> None:
        adapter = CohereAdapter(api_key="test-key")
        resp = MagicMock()
        resp.text = "Paris"

        with patch.object(adapter._client, "chat") as mock_chat:
            mock_chat.return_value = resp
            result = adapter.call(self.GOLDEN_CTX, self.GOLDEN_TURN)
        assert isinstance(result, str)
        assert len(result) > 0


class TestErrorSemantics:
    """Parity check 4: adapter handles errors gracefully."""

    def test_openai_timeout_raises(self) -> None:
        adapter = OpenAIAdapter(api_key="test-key")
        with patch.object(adapter._client.chat.completions, "create") as mock_create:
            import openai

            mock_create.side_effect = openai.APITimeoutError("timed out")
            with pytest.raises(NCPAdapterTimeoutError, match="OpenAI timed out"):
                adapter.call(NCP_CTX, USER_TURN)

    def test_anthropic_timeout_raises(self) -> None:
        adapter = AnthropicAdapter(api_key="test-key")
        with patch.object(adapter._client.messages, "create") as mock_create:
            import anthropic

            mock_create.side_effect = anthropic.APITimeoutError("timed out")
            with pytest.raises(NCPAdapterTimeoutError, match="Anthropic timed out"):
                adapter.call(NCP_CTX, USER_TURN)

    def test_ollama_timeout_raises(self) -> None:
        adapter = OllamaAdapter()
        with patch.object(adapter._client.chat.completions, "create") as mock_create:
            import openai

            mock_create.side_effect = openai.APITimeoutError("timed out")
            with pytest.raises(NCPAdapterTimeoutError, match="Ollama timed out"):
                adapter.call(NCP_CTX, USER_TURN)

    def test_gemini_timeout_raises(self) -> None:
        adapter = GeminiAdapter(api_key="test-key")
        with patch.object(adapter._model, "generate_content") as mock_gen:
            mock_gen.side_effect = TimeoutError("timed out")
            with pytest.raises(NCPAdapterTimeoutError, match="Gemini timed out"):
                adapter.call(NCP_CTX, USER_TURN)

    def test_mistral_timeout_raises(self) -> None:
        adapter = MistralAdapter(api_key="test-key")
        with patch.object(adapter._client.chat, "complete") as mock_complete:
            mock_complete.side_effect = TimeoutError("timed out")
            with pytest.raises(NCPAdapterTimeoutError, match="Mistral timed out"):
                adapter.call(NCP_CTX, USER_TURN)

    def test_cohere_timeout_raises(self) -> None:
        adapter = CohereAdapter(api_key="test-key")
        with patch.object(adapter._client, "chat") as mock_chat:
            mock_chat.side_effect = TimeoutError("timed out")
            with pytest.raises(NCPAdapterTimeoutError, match="Cohere timed out"):
                adapter.call(NCP_CTX, USER_TURN)

    def test_openai_empty_response_raises_adapter_response_error(self) -> None:
        adapter = OpenAIAdapter(api_key="test-key")
        choice = MagicMock()
        choice.message.content = None
        resp = MagicMock()
        resp.choices = [choice]

        with patch.object(adapter._client.chat.completions, "create") as mock_create:
            mock_create.return_value = resp
            with pytest.raises(NCPAdapterResponseError, match="empty text response"):
                adapter.call(NCP_CTX, USER_TURN)

    def test_openai_missing_api_key_raises_configuration_error(self) -> None:
        with pytest.raises(NCPAdapterConfigurationError, match="OPENAI_API_KEY"):
            OpenAIAdapter(api_key="")

    def test_cohere_wraps_non_timeout_provider_error(self) -> None:
        adapter = CohereAdapter(api_key="test-key")
        with patch.object(adapter._client, "chat") as mock_chat:
            mock_chat.side_effect = RuntimeError("provider boom")
            with pytest.raises(NCPAdapterError, match="Cohere call failed"):
                adapter.call(NCP_CTX, USER_TURN)
