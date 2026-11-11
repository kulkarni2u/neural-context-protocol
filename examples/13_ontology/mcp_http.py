"""Minimal MCP-over-HTTP JSON-RPC client (standard library only).

Shared by ``seed_graph.py`` and ``mcp_client.py``. Any MCP client that speaks
JSON-RPC ``tools/call`` to ``ncp serve`` works the same way; nothing here is
specific to the ontology.
"""

from __future__ import annotations

import itertools
import json
import os
import urllib.request
from urllib.parse import urlparse

DEFAULT_URL = os.environ.get("NCP_MCP_URL", "http://127.0.0.1:4242/mcp")
_LOOPBACK = {"127.0.0.1", "localhost", "::1"}


class MCPError(RuntimeError):
    """A JSON-RPC error, or a tool result with ``"ok": false``."""


class MCPClient:
    def __init__(self, url: str = DEFAULT_URL, token: str | None = None) -> None:
        self.url = url
        self.token = token if token is not None else os.environ.get("NCP_AUTH_TOKEN")
        self._ids = itertools.count(1)
        handlers = []
        if urlparse(url).hostname in _LOOPBACK:
            handlers.append(urllib.request.ProxyHandler({}))  # never proxy loopback
        self._opener = urllib.request.build_opener(*handlers)

    def request(self, method: str, params: dict | None = None) -> object:
        body = {"jsonrpc": "2.0", "id": next(self._ids), "method": method}
        if params is not None:
            body["params"] = params
        headers = {"Content-Type": "application/json", "Accept": "application/json"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        req = urllib.request.Request(
            self.url, data=json.dumps(body).encode(), headers=headers, method="POST",
        )
        with self._opener.open(req, timeout=30) as response:
            payload = json.loads(response.read().decode())
        if "error" in payload:
            raise MCPError(f"{method}: {payload['error']}")
        return payload["result"]

    def call(self, name: str, arguments: dict | None = None, *, check: bool = True) -> dict:
        """``tools/call``; returns the decoded JSON tool result."""
        result = self.request("tools/call", {"name": name, "arguments": arguments or {}})
        text = result["content"][0]["text"]  # type: ignore[index]
        decoded = json.loads(text)
        if check and isinstance(decoded, dict) and decoded.get("ok") is False:
            raise MCPError(f"{name}: {decoded.get('error')}")
        return decoded
