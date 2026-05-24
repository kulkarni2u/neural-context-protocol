"""Local deterministic adapter for early dogfood and testing."""

from __future__ import annotations

from collections.abc import Iterator

from ncp.adapters.base import BaseAdapter


class LocalAdapter(BaseAdapter):
    """A simple blocking/streaming adapter for the SQLite-first local path."""

    @property
    def ctx_window(self) -> int:
        return 200000

    def call(self, ncp_context: str, user_turn: str) -> str:
        return (
            "local_adapter_response\n"
            f"user_turn:{user_turn}\n"
            f"context_chars:{len(ncp_context)}"
        )

    def stream(self, ncp_context: str, user_turn: str) -> Iterator[str]:
        response = self.call(ncp_context, user_turn)
        for line in response.splitlines(keepends=True):
            yield line
