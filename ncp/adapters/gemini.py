from __future__ import annotations

from os import environ

from ncp.adapters.base import BaseAdapter, TokenUsage


class GeminiAdapter(BaseAdapter):
    @property
    def ctx_window(self) -> int:
        return 1000000

    @property
    def model_name(self) -> str:
        return self._model_name

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
        self.last_usage = None
        prompt = f"{ncp_context}\n\n{user_turn}"
        resp = self._run_provider_call(
            lambda: self._client.models.generate_content(
                model=self._model_name,
                contents=prompt,
            ),
            provider="Gemini",
        )
        self.last_usage = self._usage_from_gemini(resp)
        return self._coerce_text(resp.text, provider="Gemini")

    @staticmethod
    def _usage_from_gemini(resp: object) -> TokenUsage | None:
        usage = getattr(resp, "usage_metadata", None)
        if usage is None:
            return None
        # prompt_token_count includes cached content; thinking tokens are
        # reported apart from candidates but billed at the output price.
        prompt = int(getattr(usage, "prompt_token_count", 0) or 0)
        cached = min(max(0, int(getattr(usage, "cached_content_token_count", 0) or 0)), prompt)
        thoughts = int(getattr(usage, "thoughts_token_count", 0) or 0)
        return TokenUsage(
            input_tokens=prompt - cached,
            output_tokens=int(getattr(usage, "candidates_token_count", 0) or 0) + thoughts,
            cache_read_tokens=cached,
            reasoning_tokens=thoughts,
        )
