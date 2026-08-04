from __future__ import annotations

from collections.abc import Iterator
from os import environ

from ncp.adapters.base import BaseAdapter, NCPAdapterError, NCPAdapterTimeoutError, TokenUsage


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
        self.last_usage = self._usage_from_anthropic(msg)
        texts = [b.text for b in msg.content if b.type == "text"]
        return self._coerce_text("".join(texts), provider="Anthropic")

    @staticmethod
    def _usage_from_anthropic(msg: object) -> TokenUsage | None:
        usage = getattr(msg, "usage", None)
        if usage is None:
            return None
        return TokenUsage(
            input_tokens=int(getattr(usage, "input_tokens", 0) or 0),
            output_tokens=int(getattr(usage, "output_tokens", 0) or 0),
            cache_read_tokens=int(getattr(usage, "cache_read_input_tokens", 0) or 0),
        )

    def stream(self, ncp_context: str, user_turn: str) -> Iterator[str]:
        timeout_types = (self._anthropic.APITimeoutError, TimeoutError)
        stream_ctx = self._run_provider_call(
            lambda: self._client.messages.stream(
                model=self._model,
                max_tokens=self._max_tokens,
                system=ncp_context,
                messages=[{"role": "user", "content": user_turn}],
            ),
            provider="Anthropic",
            timeout_types=timeout_types,
        )
        # ``.stream()`` above only builds a deferred MessageStreamManager; the
        # real HTTP request happens on ``__enter__`` below, so that entry (and
        # the iteration that follows) must be wrapped too, or real provider
        # failures (auth, rate limit, connection, timeout) escape as raw
        # anthropic.* exceptions instead of this adapter's NCPAdapterError
        # contract — see call()'s equivalent wrapping via _run_provider_call.
        try:
            with stream_ctx as stream:
                for event in stream:
                    if event.type == "content_block_delta" and event.delta.type == "text_delta":
                        yield event.delta.text
                final = getattr(stream, "get_final_message", None)
                if callable(final):
                    try:
                        self.last_usage = self._usage_from_anthropic(final())
                    except Exception:
                        self.last_usage = None
        except NCPAdapterError:
            raise
        except timeout_types as exc:
            raise NCPAdapterTimeoutError(f"Anthropic timed out: {exc}") from exc
        except Exception as exc:
            raise NCPAdapterError(f"Anthropic call failed: {exc}") from exc
