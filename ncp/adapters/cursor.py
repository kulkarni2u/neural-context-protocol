"""Cursor Cloud Agent REST API adapter.

Creates a cloud agent run per call and polls until completion.
Requires CURSOR_API_KEY and a target GitHub repository URL.

For local non-interactive runs use CursorCLIDogfoodAdapter in ncp.dogfood instead.
For IDE / interactive use, register NCP as an MCP server in .cursor/mcp.json and
Cursor's agent will have ncp_fetch / ncp_write_memory / ncp_get_context available
without any extra adapter wiring.
"""

from __future__ import annotations

import time
from os import environ

from ncp.adapters.base import (
    BaseAdapter,
    NCPAdapterConfigurationError,
    NCPAdapterError,
    NCPAdapterResponseError,
    NCPAdapterTimeoutError,
)


class CursorAPIAdapter(BaseAdapter):
    """Adapter backed by the Cursor Cloud Agent REST API (POST /v0/agents)."""

    _BASE_URL = "https://api.cursor.com"

    @property
    def ctx_window(self) -> int:
        return 200000

    def __init__(
        self,
        *,
        api_key: str = "",
        repository: str = "",
        model: str | None = None,
        base_url: str = _BASE_URL,
        poll_interval: float = 10.0,
        timeout: float = 300.0,
    ) -> None:
        try:
            import httpx
        except ImportError as err:
            raise ImportError(
                "httpx is required. Install it with: pip install httpx"
            ) from err

        resolved_key = api_key or environ.get("CURSOR_API_KEY", "")
        self._api_key = self._require_api_key(resolved_key, env_var="CURSOR_API_KEY")

        resolved_repo = repository or environ.get("CURSOR_REPOSITORY", "")
        if not resolved_repo:
            raise NCPAdapterConfigurationError(
                "CursorAPIAdapter requires a GitHub repository URL via "
                "repository= or CURSOR_REPOSITORY env var"
            )
        self._repository = resolved_repo
        self._model = model
        self._base_url = base_url.rstrip("/")
        self._poll_interval = poll_interval
        self._timeout = timeout
        self._client = httpx.Client(
            auth=(self._api_key, ""),
            headers={"Content-Type": "application/json"},
            timeout=30.0,
        )

    def call(self, ncp_context: str, user_turn: str) -> str:
        prompt_text = f"{ncp_context}\n\n{user_turn}".strip()
        body: dict[str, object] = {
            "prompt": {"text": prompt_text},
            "source": {"repository": self._repository},
        }
        if self._model:
            body["model"] = self._model

        try:
            resp = self._client.post(f"{self._base_url}/v0/agents", json=body)
        except Exception as exc:
            raise NCPAdapterError(f"Cursor API request failed: {exc}") from exc

        if resp.status_code not in (200, 201, 202):
            raise NCPAdapterResponseError(
                f"Cursor API error {resp.status_code}: {resp.text}"
            )

        agent_id: str = resp.json().get("id", "")
        if not agent_id:
            raise NCPAdapterResponseError("Cursor API returned no agent id")

        deadline = time.monotonic() + self._timeout
        while True:
            if time.monotonic() > deadline:
                raise NCPAdapterTimeoutError(
                    f"Cursor agent {agent_id} did not complete within {self._timeout}s"
                )
            time.sleep(self._poll_interval)
            try:
                data = self._client.get(
                    f"{self._base_url}/v0/agents/{agent_id}"
                ).json()
            except Exception as exc:
                raise NCPAdapterError(f"Cursor status poll failed: {exc}") from exc

            status = data.get("status", "")
            if status == "failed":
                raise NCPAdapterResponseError(
                    f"Cursor agent failed: {data.get('summary', '')}"
                )
            if status == "completed":
                text = (
                    data.get("summary")
                    or data.get("output")
                    or data.get("result")
                    or ""
                )
                return self._coerce_text(text, provider="Cursor")
