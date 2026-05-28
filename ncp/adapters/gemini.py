from __future__ import annotations

from os import environ

from ncp.adapters.base import BaseAdapter


class GeminiAdapter(BaseAdapter):
    @property
    def ctx_window(self) -> int:
        return 1000000

    def __init__(
        self,
        api_key: str = "",
        model: str = "gemini-2.0-flash",
        timeout: float = 120.0,
    ) -> None:
        try:
            from google import genai
        except ImportError as err:
            raise ImportError(
                "google-genai is required. Install it with: pip install 'neural-context-protocol[providers]'"
            ) from err
        resolved_key = api_key or environ.get("GOOGLE_API_KEY", "")
        self._client = genai.Client(api_key=self._require_api_key(resolved_key, env_var="GOOGLE_API_KEY"))
        self._model_name = model
        self._timeout = timeout

    def call(self, ncp_context: str, user_turn: str) -> str:
        prompt = f"{ncp_context}\n\n{user_turn}"
        resp = self._run_provider_call(
            lambda: self._client.models.generate_content(
                model=self._model_name,
                contents=prompt,
            ),
            provider="Gemini",
        )
        return self._coerce_text(resp.text, provider="Gemini")
