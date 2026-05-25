from __future__ import annotations

from os import environ
import warnings

from ncp.adapters.base import BaseAdapter


class CohereAdapter(BaseAdapter):
    @property
    def ctx_window(self) -> int:
        return 128000

    def __init__(
        self,
        api_key: str = "",
        model: str = "command-a-03-2025",
        max_tokens: int = 4096,
        timeout: float = 120.0,
    ) -> None:
        try:
            with warnings.catch_warnings():
                warnings.filterwarnings(
                    "ignore",
                    message=".*iscoroutinefunction.*",
                    category=DeprecationWarning,
                )
                import cohere
        except ImportError as err:
            raise ImportError(
                "cohere is required. Install it with: pip install 'neural-context-protocol[providers]'"
            ) from err
        resolved_key = api_key or environ.get("COHERE_API_KEY", "")
        with warnings.catch_warnings():
            warnings.filterwarnings(
                "ignore",
                message=".*iscoroutinefunction.*",
                category=DeprecationWarning,
            )
            self._client = cohere.Client(
                api_key=self._require_api_key(resolved_key, env_var="COHERE_API_KEY"),
                timeout=timeout,
            )
        self._model = model
        self._max_tokens = max_tokens

    def call(self, ncp_context: str, user_turn: str) -> str:
        from cohere.types import ChatMessage

        resp = self._run_provider_call(
            lambda: self._client.chat(
                model=self._model,
                max_tokens=self._max_tokens,
                preamble=ncp_context,
                messages=[
                    ChatMessage(role="user", message=user_turn),
                ],
            ),
            provider="Cohere",
        )
        return self._coerce_text(resp.text, provider="Cohere")
