from __future__ import annotations

from collections.abc import Iterator
from os import environ

from ncp.adapters.base import BaseAdapter

_MODEL_WINDOWS: dict[str, int] = {
    "gpt-4o": 128000,
    "gpt-4o-mini": 128000,
    "o1": 200000,
    "o3-mini": 200000,
    "gpt-4.1": 1047576,
    "gpt-4.1-mini": 1047576,
    "gpt-4.1-nano": 1047576,
}


class OpenAIAdapter(BaseAdapter):
    def __init__(
        self,
        api_key: str = "",
        model: str = "gpt-4o",
        max_tokens: int = 4096,
        timeout: float = 120.0,
        max_retries: int = 2,
        base_url: str | None = None,
    ) -> None:
        try:
            import openai
        except ImportError as err:
            raise ImportError(
                "openai is required. Install it with: pip install 'neural-context-protocol[providers]'"
            ) from err
        self._openai = openai
        resolved_key = api_key or environ.get("OPENAI_API_KEY", "")
        kwargs: dict = {
            "api_key": self._require_api_key(resolved_key, env_var="OPENAI_API_KEY"),
            "timeout": timeout,
            "max_retries": max_retries,
        }
        if base_url is not None:
            kwargs["base_url"] = base_url
        self._client = openai.OpenAI(**kwargs)
        self._model = model
        self._max_tokens = max_tokens

    @property
    def ctx_window(self) -> int:
        return _MODEL_WINDOWS.get(self._model, 128000)

    def call(self, ncp_context: str, user_turn: str) -> str:
        resp = self._run_provider_call(
            lambda: self._client.chat.completions.create(
                model=self._model,
                max_tokens=self._max_tokens,
                messages=[
                    {"role": "system", "content": ncp_context},
                    {"role": "user", "content": user_turn},
                ],
                stream=False,
            ),
            provider="OpenAI",
            timeout_types=(self._openai.APITimeoutError, TimeoutError),
        )
        return self._coerce_text(resp.choices[0].message.content, provider="OpenAI")

    def stream(self, ncp_context: str, user_turn: str) -> Iterator[str]:
        stream = self._run_provider_call(
            lambda: self._client.chat.completions.create(
                model=self._model,
                max_tokens=self._max_tokens,
                messages=[
                    {"role": "system", "content": ncp_context},
                    {"role": "user", "content": user_turn},
                ],
                stream=True,
            ),
            provider="OpenAI",
            timeout_types=(self._openai.APITimeoutError, TimeoutError),
        )
        for chunk in stream:
            if chunk.choices and (delta := chunk.choices[0].delta.content):
                yield delta
