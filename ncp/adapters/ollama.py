from __future__ import annotations

from os import environ

from ncp.adapters.base import BaseAdapter

_DEFAULT_OLLAMA_BASE_URL = "http://localhost:11434/v1"


class OllamaAdapter(BaseAdapter):
    @property
    def ctx_window(self) -> int:
        return 8192

    def __init__(
        self,
        base_url: str = "",
        model: str = "llama3.1",
        max_tokens: int = 4096,
        timeout: float = 120.0,
        max_retries: int = 2,
    ) -> None:
        try:
            import openai
        except ImportError as err:
            raise ImportError(
                "openai is required. Install it with: pip install 'neural-context-protocol[providers]'"
            ) from err
        self._openai = openai
        resolved_base_url = base_url or environ.get("OLLAMA_BASE_URL", _DEFAULT_OLLAMA_BASE_URL)
        self._client = openai.OpenAI(
            base_url=resolved_base_url,
            api_key="ollama",
            timeout=timeout,
            max_retries=max_retries,
        )
        self._model = model
        self._max_tokens = max_tokens

    def call(self, ncp_context: str, user_turn: str) -> str:
        resp = self._run_provider_call(
            lambda: self._client.chat.completions.create(
                model=self._model,
                max_tokens=self._max_tokens,
                messages=[
                    {"role": "system", "content": ncp_context},
                    {"role": "user", "content": user_turn},
                ],
            ),
            provider="Ollama",
            timeout_types=(self._openai.APITimeoutError, TimeoutError),
        )
        return self._coerce_text(resp.choices[0].message.content, provider="Ollama")
