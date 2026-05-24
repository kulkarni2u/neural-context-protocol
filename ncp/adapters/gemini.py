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
            import google.generativeai as genai
        except ImportError as err:
            raise ImportError(
                "google-generativeai is required. Install it with: pip install 'ncp-sdk[providers]'"
            ) from err
        resolved_key = api_key or environ.get("GOOGLE_API_KEY", "")
        genai.configure(api_key=self._require_api_key(resolved_key, env_var="GOOGLE_API_KEY"))
        self._model = genai.GenerativeModel(model)
        self._timeout = timeout

    def call(self, ncp_context: str, user_turn: str) -> str:
        prompt = f"{ncp_context}\n\n{user_turn}"
        resp = self._run_provider_call(
            lambda: self._model.generate_content(
                prompt,
                request_options={"timeout": self._timeout},
            ),
            provider="Gemini",
        )
        return self._coerce_text(resp.text, provider="Gemini")
