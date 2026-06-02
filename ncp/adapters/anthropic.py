from __future__ import annotations

from collections.abc import Iterator
from os import environ

from ncp.adapters.base import BaseAdapter, NCPAdapterError, NCPAdapterTimeoutError


class AnthropicAdapter(BaseAdapter):
    @property
    def ctx_window(self) -> int:
        return 200000

    def __init__(
        self,
        api_key: str = "",
        model: str = "claude-sonnet-4-20250514",
        max_tokens: int = 4096,
        timeout: float = 120.0,
    ) -> None:
        try:
            import anthropic
        except ImportError as err:
            raise ImportError(
                "anthropic is required. Install it with: pip install 'neural-context-protocol[providers]'"
            ) from err
        self._anthropic = anthropic
        resolved_key = api_key or environ.get("ANTHROPIC_API_KEY", "")
        self._client = anthropic.Anthropic(
            api_key=self._require_api_key(resolved_key, env_var="ANTHROPIC_API_KEY"),
            timeout=timeout,
        )
        self._model = model
        self._max_tokens = max_tokens

    def call(self, ncp_context: str, user_turn: str) -> str:
        msg = self._run_provider_call(
            lambda: self._client.messages.create(
                model=self._model,
                max_tokens=self._max_tokens,
                system=ncp_context,
                messages=[{"role": "user", "content": user_turn}],
            ),
            provider="Anthropic",
            timeout_types=(self._anthropic.APITimeoutError, TimeoutError),
        )
        texts = [b.text for b in msg.content if b.type == "text"]
        return self._coerce_text("".join(texts), provider="Anthropic")

    def stream(self, ncp_context: str, user_turn: str) -> Iterator[str]:
        stream_ctx = self._run_provider_call(
            lambda: self._client.messages.stream(
                model=self._model,
                max_tokens=self._max_tokens,
                system=ncp_context,
                messages=[{"role": "user", "content": user_turn}],
            ),
            provider="Anthropic",
            timeout_types=(self._anthropic.APITimeoutError, TimeoutError),
        )
        try:
            with stream_ctx as stream:
                for event in stream:
                    if event.type == "content_block_delta" and event.delta.type == "text_delta":
                        yield event.delta.text
        except self._anthropic.APITimeoutError as exc:
            raise NCPAdapterTimeoutError(f"Anthropic stream timed out: {exc}") from exc
        except Exception as exc:
            raise NCPAdapterError(f"Anthropic stream failed: {exc}") from exc
