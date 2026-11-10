from __future__ import annotations

from pathlib import Path

from ncp.mcp.server import MCP_TOOLS


REPO_ROOT = Path(__file__).resolve().parents[1]
HTTP_API_DOC = REPO_ROOT / "docs" / "NCP_HTTP_API.md"


def test_every_server_tool_is_documented_in_http_api() -> None:
    text = HTTP_API_DOC.read_text(encoding="utf-8")
    names = [str(tool["name"]) for tool in MCP_TOOLS]

    assert names, "server tool catalog is empty"
    missing = [name for name in names if name not in text]

    assert not missing, f"tools missing from docs/NCP_HTTP_API.md: {missing}"
