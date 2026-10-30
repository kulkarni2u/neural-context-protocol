"""Base adapter contract."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Callable, TypeVar


@dataclass(slots=True)
class TokenUsage:
    """Real per-call token usage reported by a provider response.

    Adapters populate ``BaseAdapter.last_usage`` with one of these after a
    provider call so the runtime can bill actual (not estimated) tokens.

    The four billed buckets are disjoint, whatever the provider's own
    convention: ``input_tokens`` is *uncached* input only (billed at the
    full input price), ``cache_read_tokens`` and ``cache_write_tokens`` are
    the cached-input buckets, and ``output_tokens`` is everything billed at
    the output price. ``reasoning_tokens`` is the part of ``output_tokens``
    spent on hidden reasoning -- informational, never billed twice. ``raw``
    keeps the provider's own usage payload for audit.
    """

    input_tokens: int
    output_tokens: int
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    reasoning_tokens: int = 0
    raw: dict[str, object] | None = None


def _raw_usage(usage: object) -> dict[str, object] | None:
    """Best-effort plain-dict copy of a provider usage object, for audit."""
    dump = getattr(usage, "model_dump", None)
    if callable(dump):
        try:
            data = dump()
        except Exception:
            return None
        return data if isinstance(data, dict) else None
    if isinstance(usage, dict):
        return dict(usage)
    return None


class NCPAdapterError(RuntimeError):
    """Base class for provider adapter failures."""


class NCPAdapterConfigurationError(NCPAdapterError):
    """Raised when an adapter is misconfigured before making a call."""


class NCPAdapterTimeoutError(NCPAdapterError):
    """Raised when a provider call times out."""


class NCPAdapterResponseError(NCPAdapterError):
    """Raised when a provider returns an unusable response."""


_T = TypeVar("_T")


class BaseAdapter(ABC):
    """Minimal provider adapter contract for the first NCP API slice."""

    #: Real token usage from the most recent provider call, when the provider
    #: reports it. ``None`` means the adapter has no authoritative usage (e.g.
    #: the local/mock path) and the runtime should fall back to an estimate.
    last_usage: TokenUsage | None = None

    @property
    def ctx_window(self) -> int:
        return 200000

    @property
    def model_name(self) -> str:
        """Provider model id used for pricing; falls back to the class name."""

        return getattr(self, "_model", None) or type(self).__name__.lower()

    @staticmethod
    def _usage_from_openai_shape(response: object) -> TokenUsage | None:
        """Extract usage from an OpenAI-compatible response (OpenAI/Ollama/Mistral).

        OpenAI's ``prompt_tokens`` *includes* ``cached_tokens``; split them so
        cached input is billed once, at the cache-read price. Reasoning
        tokens are already inside ``completion_tokens``.
        """

        usage = getattr(response, "usage", None)
        if usage is None:
            return None
        prompt = int(getattr(usage, "prompt_tokens", 0) or 0)
        details = getattr(usage, "prompt_tokens_details", None)
        cached = int(getattr(details, "cached_tokens", 0) or 0) if details is not None else 0
        cached = min(max(0, cached), prompt)
        completion_details = getattr(usage, "completion_tokens_details", None)
        reasoning = (
            int(getattr(completion_details, "reasoning_tokens", 0) or 0) if completion_details is not None else 0
        )
        return TokenUsage(
            input_tokens=prompt - cached,
            output_tokens=int(getattr(usage, "completion_tokens", 0) or 0),
            cache_read_tokens=cached,
            reasoning_tokens=reasoning,
            raw=_raw_usage(usage),
        )

    @abstractmethod
    def call(self, ncp_context: str, user_turn: str) -> str:
        """Return a blocking response for one assembled context."""

    def stream(self, ncp_context: str, user_turn: str) -> Iterator[str]:
        """Yield a streamed response for one assembled context.

        Tier 2 providers override only if they support streaming.
        """
        raise NotImplementedError(
            f"{type(self).__name__} does not support streaming in NCP V1; use blocking call()"
        )

    def _require_api_key(self, api_key: str, *, env_var: str) -> str:
        if api_key.strip():
            return api_key
        raise NCPAdapterConfigurationError(
            f"{type(self).__name__} requires {env_var}; configure it or pass api_key explicitly"
        )

    def _coerce_text(self, value: str | None, *, provider: str) -> str:
        text = (value or "").strip()
        if text:
            return text
        raise NCPAdapterResponseError(f"{provider} returned an empty text response")

    def _run_provider_call(
        self,
        call: Callable[[], _T],
        *,
        provider: str,
        timeout_types: tuple[type[BaseException], ...] = (TimeoutError,),
    ) -> _T:
        try:
            return call()
        except NCPAdapterError:
            raise
        except timeout_types as exc:
            raise NCPAdapterTimeoutError(f"{provider} timed out: {exc}") from exc
        except Exception as exc:
            raise NCPAdapterError(f"{provider} call failed: {exc}") from exc
