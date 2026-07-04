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
    """

    input_tokens: int
    output_tokens: int
    cache_read_tokens: int = 0


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
        """Extract usage from an OpenAI-compatible response (OpenAI/Ollama/Mistral)."""

        usage = getattr(response, "usage", None)
        if usage is None:
            return None
        details = getattr(usage, "prompt_tokens_details", None)
        cached = int(getattr(details, "cached_tokens", 0) or 0) if details is not None else 0
        return TokenUsage(
            input_tokens=int(getattr(usage, "prompt_tokens", 0) or 0),
            output_tokens=int(getattr(usage, "completion_tokens", 0) or 0),
            cache_read_tokens=cached,
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
