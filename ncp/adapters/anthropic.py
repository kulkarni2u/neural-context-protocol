from __future__ import annotations

from collections.abc import Iterator
from os import environ

from ncp.adapters.base import BaseAdapter, NCPAdapterError, NCPAdapterTimeoutError, TokenUsage

# WI-10 (ncp/encoder.py) orders assembled context as stable blocks
# ([NCP:CONSCIOUS], [NCP:SUBCONSCIOUS], [NCP:WHISPERS]) followed by the one
# block that changes every turn, [NCP:BUDGET]. That ordering exists so the
# prefix up to [NCP:BUDGET] is byte-identical turn over turn, which is
# exactly what Anthropic's prompt caching wants: a `cache_control`
# breakpoint on the stable prefix lets the provider skip re-processing (and
# re-billing at full price) everything before the volatile budget block.
_BUDGET_MARKER = "[NCP:BUDGET]"


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
        enable_prompt_caching: bool = True,
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
        self._enable_prompt_caching = enable_prompt_caching

    def call(self, ncp_context: str, user_turn: str) -> str:
        msg = self._run_provider_call(
            lambda: self._client.messages.create(
                model=self._model,
                max_tokens=self._max_tokens,
                system=self._build_system_param(ncp_context),
                messages=[{"role": "user", "content": user_turn}],
            ),
            provider="Anthropic",
            timeout_types=(self._anthropic.APITimeoutError, TimeoutError),
        )
        self.last_usage = self._usage_from_anthropic(msg)
        texts = [b.text for b in msg.content if b.type == "text"]
        return self._coerce_text("".join(texts), provider="Anthropic")

    def _build_system_param(self, ncp_context: str) -> str | list[dict[str, object]]:
        """Return the ``system`` request param for one turn.

        Plain-string ``system`` when caching is disabled (matches the prior
        behavior exactly); otherwise the structured system-blocks format
        with a cache breakpoint on the stable prefix (see
        ``_split_stable_prefix``).
        """

        if not self._enable_prompt_caching:
            return ncp_context
        return self._split_stable_prefix(ncp_context)

    @staticmethod
    def _split_stable_prefix(ncp_context: str) -> list[dict[str, object]]:
        """Split into a cached stable-prefix block and an uncached volatile block.

        The split point is the literal ``[NCP:BUDGET]`` marker, which
        ``ncp/encoder.py``'s ``assemble_from_parts`` always appends last (the
        budget block is never omitted, even when other optional blocks are).
        Only the stable prefix gets ``cache_control`` -- the budget block
        changes every turn, and caching it would defeat the point (the next
        turn's byte-different budget block would just miss instead of
        reusing the cached prefix before it).

        If the marker isn't found (defensive -- e.g. a caller passes a raw
        string that never went through the encoder), the whole string is
        sent as a single cached block; still worth caching even without a
        clean split.

        Anthropic only actually caches a block at or above a per-model
        minimum (1024 tokens for Sonnet/Opus-class, 2048 for Haiku-class).
        There's no need to gate on token count here: setting `cache_control`
        below that threshold is harmless, the API just silently skips
        caching that block.
        """

        idx = ncp_context.find(_BUDGET_MARKER)
        if idx == -1:
            return [{"type": "text", "text": ncp_context, "cache_control": {"type": "ephemeral"}}]

        stable, volatile = ncp_context[:idx], ncp_context[idx:]
        blocks: list[dict[str, object]] = []
        if stable:
            blocks.append({"type": "text", "text": stable, "cache_control": {"type": "ephemeral"}})
        blocks.append({"type": "text", "text": volatile})
        return blocks

    @staticmethod
    def _usage_from_anthropic(msg: object) -> TokenUsage | None:
        usage = getattr(msg, "usage", None)
        if usage is None:
            return None
        return TokenUsage(
            input_tokens=int(getattr(usage, "input_tokens", 0) or 0),
            output_tokens=int(getattr(usage, "output_tokens", 0) or 0),
            cache_read_tokens=int(getattr(usage, "cache_read_input_tokens", 0) or 0),
            cache_write_tokens=int(getattr(usage, "cache_creation_input_tokens", 0) or 0),
        )

    def stream(self, ncp_context: str, user_turn: str) -> Iterator[str]:
        timeout_types = (self._anthropic.APITimeoutError, TimeoutError)
        stream_ctx = self._run_provider_call(
            lambda: self._client.messages.stream(
                model=self._model,
                max_tokens=self._max_tokens,
                system=self._build_system_param(ncp_context),
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
