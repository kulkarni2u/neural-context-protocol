from __future__ import annotations

from os import environ

from ncp.adapters.base import BaseAdapter


class MistralAdapter(BaseAdapter):
    @property
    def ctx_window(self) -> int:
        return 128000

    def __init__(
        self,
        api_key: str = "",
        model: str = "mistral-large-latest",
        max_tokens: int = 4096,
        timeout: float = 120.0,
    ) -> None:
        try:
            from mistralai.client import Mistral
        except ImportError as err:
            raise ImportError(
                "mistralai is required. Install it with: pip install 'neural-context-protocol[providers]'"
            ) from err
        resolved_key = api_key or environ.get("MISTRAL_API_KEY", "")
        self._client = Mistral(
            api_key=self._require_api_key(resolved_key, env_var="MISTRAL_API_KEY"),
            timeout_ms=int(timeout * 1000),
        )
        self._model = model
        self._max_tokens = max_tokens

    def call(self, ncp_context: str, user_turn: str) -> str:
        resp = self._run_provider_call(
            lambda: self._client.chat.complete(
                model=self._model,
                max_tokens=self._max_tokens,
                messages=[
                    {"role": "system", "content": ncp_context},
                    {"role": "user", "content": user_turn},
                ],
            ),
            provider="Mistral",
        )
        return self._coerce_text(resp.choices[0].message.content, provider="Mistral")
