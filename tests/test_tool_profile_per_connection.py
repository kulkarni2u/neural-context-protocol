"""Per-connection tool profiles: ?profile=, X-NCP-Tool-Profile, --profile, principal allowlists."""

from __future__ import annotations

from contextlib import closing, contextmanager
import io
import json
import socket
import threading
from pathlib import Path
from typing import Iterator

import httpx
import pytest

from ncp.config import load_config
from ncp.mcp.server import (
    MCP_TOOLS,
    MINIMAL_TOOL_NAMES,
    _handle_request,
    create_http_server,
    narrow_tools,
    serve_streams,
    tools_for_config,
)
from ncp.principals import PrincipalConfigError, hash_token, load_principals

GET_CTX = {"agent_id": "a", "role": "r", "task": "t", "slot": "s", "intent": "i"}

# Character budget per profile for json.dumps(tools, separators=(",", ":")):
# ~5% above the measured size, so catalog growth fails CI and must be a
# deliberate decision (raise the number in the same change).
PROFILE_CHAR_BUDGETS = {"minimal": 5110, "core": 10870, "standard": 18400, "full": 26200}
PROFILE_TOOL_COUNTS = {"minimal": 2, "core": 5, "standard": 11, "full": 19}


def _free_port() -> int:
    with closing(socket.socket(socket.AF_INET, socket.SOCK_STREAM)) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _write_config(root: Path, toml: str) -> None:
    (root / ".ncp").mkdir(exist_ok=True)
    (root / ".ncp" / "config.toml").write_text(toml, encoding="utf-8")


def _profile_tools(tmp_path: Path, profile: str) -> list[dict]:
    _write_config(
        tmp_path,
        f'[tools]\nprofile = "{profile}"\n[tasks]\nenabled = true\n[memoization]\nenabled = true\n',
    )
    return tools_for_config(load_config(cwd=tmp_path))


class TestProfileSnapshot:
    @pytest.mark.parametrize("profile", list(PROFILE_CHAR_BUDGETS))
    def test_catalog_size_within_budget(self, tmp_path: Path, profile: str) -> None:
        tools = _profile_tools(tmp_path, profile)
        assert len(tools) == PROFILE_TOOL_COUNTS[profile]
        size = len(json.dumps(tools, separators=(",", ":")))
        assert size <= PROFILE_CHAR_BUDGETS[profile], (
            f"{profile} tool catalog is {size} chars (budget {PROFILE_CHAR_BUDGETS[profile]}); "
            "shrink the schema or raise the budget deliberately"
        )

    def test_minimal_is_get_context_and_write_memory(self, tmp_path: Path) -> None:
        names = {t["name"] for t in _profile_tools(tmp_path, "minimal")}
        assert names == {"ncp_get_context", "ncp_write_memory"} == set(MINIMAL_TOOL_NAMES)

    def test_profiles_nest(self, tmp_path: Path) -> None:
        sets = [{t["name"] for t in _profile_tools(tmp_path, p)} for p in ("minimal", "core", "standard", "full")]
        assert sets[0] < sets[1] < sets[2] < sets[3]


class TestNarrowTools:
    def test_never_widens(self) -> None:
        core = [t for t in MCP_TOOLS if t["name"] in {"ncp_get_context", "ncp_post_turn"}]
        assert narrow_tools(core, profile="full") == core
        assert narrow_tools(core, profile="standard") == core

    def test_unknown_or_empty_profile_falls_back_to_server_set(self) -> None:
        assert narrow_tools(MCP_TOOLS, profile="bogus") == list(MCP_TOOLS)
        assert narrow_tools(MCP_TOOLS, profile="") == list(MCP_TOOLS)
        assert narrow_tools(MCP_TOOLS, profile=None) == list(MCP_TOOLS)

    def test_minimal_and_allowlist_intersect(self) -> None:
        names = [t["name"] for t in narrow_tools(MCP_TOOLS, profile="minimal")]
        assert sorted(names) == sorted(MINIMAL_TOOL_NAMES)
        only = narrow_tools(MCP_TOOLS, profile="minimal", allowed_names=frozenset({"ncp_write_memory", "ncp_fetch"}))
        assert [t["name"] for t in only] == ["ncp_write_memory"]


class TestHandleRequestEnforcement:
    def test_call_outside_list_rejected(self) -> None:
        narrow = narrow_tools(MCP_TOOLS, profile="minimal")
        handlers = {"ncp_post_turn": lambda args: {"ok": True}}
        resp = json.loads(_handle_request(
            {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "ncp_post_turn", "arguments": {}}},
            handlers, tools=narrow,
        ))
        assert resp["error"]["code"] == -32601


class _Srv:
    def __init__(self, port: int) -> None:
        self.port = port

    def rpc(self, method: str, params: dict | None = None, *, path: str = "/mcp",
            headers: dict | None = None, token: str | None = None) -> dict:
        hdrs = {"Content-Type": "application/json", **(headers or {})}
        if token:
            hdrs["Authorization"] = f"Bearer {token}"
        body = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}}
        response = httpx.post(f"http://127.0.0.1:{self.port}{path}", content=json.dumps(body), headers=hdrs, timeout=10)
        assert response.status_code == 200, response.text
        return response.json()

    def names(self, **kwargs: object) -> list[str]:
        return [t["name"] for t in self.rpc("tools/list", **kwargs)["result"]["tools"]]  # type: ignore[arg-type]


@contextmanager
def _serve(root: Path, toml: str, **kwargs: object) -> Iterator[_Srv]:
    _write_config(root, toml)
    port = _free_port()
    server = create_http_server(host="127.0.0.1", port=port, cwd=root, **kwargs)  # type: ignore[arg-type]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield _Srv(port)
    finally:
        server._shutdown_event.set()
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


class TestHttpProfiles:
    def test_two_clients_see_different_lists(self, tmp_path: Path) -> None:
        with _serve(tmp_path, '[tools]\nprofile = "standard"\n') as srv:
            default = srv.names()
            minimal_q = srv.names(path="/mcp?profile=minimal")
            minimal_h = srv.names(headers={"X-NCP-Tool-Profile": "minimal"})
            core = srv.names(path="/mcp?profile=core")
        assert len(default) == 11
        assert sorted(minimal_q) == sorted(minimal_h) == ["ncp_get_context", "ncp_write_memory"]
        assert len(core) == 5

    def test_cannot_widen_and_unknown_falls_back(self, tmp_path: Path) -> None:
        with _serve(tmp_path, '[tools]\nprofile = "core"\n') as srv:
            assert len(srv.names(path="/mcp?profile=full")) == 5
            assert len(srv.names(path="/mcp?profile=nonsense")) == 5
            assert len(srv.names(headers={"X-NCP-Tool-Profile": "full"})) == 5

    def test_call_outside_connection_set_rejected(self, tmp_path: Path) -> None:
        with _serve(tmp_path, '[tools]\nprofile = "standard"\n') as srv:
            args = {"name": "ncp_post_turn", "arguments": {}}
            narrow = srv.rpc("tools/call", args, path="/mcp?profile=minimal")
            assert narrow["error"]["code"] == -32601
            # The same tool is reachable on a connection that did not narrow.
            wide = srv.rpc("tools/call", {"name": "ncp_get_context", "arguments": GET_CTX})
            assert "error" not in wide
            allowed = srv.rpc("tools/call", {"name": "ncp_get_context", "arguments": GET_CTX}, path="/mcp?profile=minimal")
            assert "error" not in allowed

    def test_sse_endpoint_event_carries_profile(self, tmp_path: Path) -> None:
        with _serve(tmp_path, '[tools]\nprofile = "standard"\n', keepalive_seconds=0.05) as srv:
            with httpx.stream("GET", f"http://127.0.0.1:{srv.port}/sse?profile=minimal", timeout=5) as response:
                assert response.status_code == 200
                for line in response.iter_lines():
                    if line.startswith("data:"):
                        assert line == "data: /mcp?profile=minimal"
                        break

    def test_server_profile_flag_narrows_whole_server(self, tmp_path: Path) -> None:
        with _serve(tmp_path, '[tools]\nprofile = "standard"\n', profile="core") as srv:
            assert len(srv.names()) == 5
            assert len(srv.names(path="/mcp?profile=standard")) == 5


class TestPrincipalAllowlist:
    TOML = f"""
[tools]
profile = "standard"

[principals.alice]
token_sha256 = "{hash_token("tok-a")}"
agent_id = "alice"
tools = ["ncp_get_context", "ncp_post_turn"]

[principals.bob]
token_sha256 = "{hash_token("tok-b")}"
agent_id = "bob"
"""

    def test_allowlist_intersects_profile_and_is_enforced(self, tmp_path: Path) -> None:
        with _serve(tmp_path, self.TOML) as srv:
            assert sorted(srv.names(token="tok-a")) == ["ncp_get_context", "ncp_post_turn"]
            assert srv.names(token="tok-a", path="/mcp?profile=minimal") == ["ncp_get_context"]
            assert len(srv.names(token="tok-b")) == 11
            denied = srv.rpc("tools/call", {"name": "ncp_write_memory", "arguments": {}}, token="tok-a")
            assert denied["error"]["code"] == -32601

    def test_principal_session_ids_round_trip_for_post_turn(self, tmp_path: Path) -> None:
        with _serve(tmp_path, self.TOML) as srv:
            ctx = srv.rpc("tools/call", {"name": "ncp_get_context", "arguments": GET_CTX | {"agent_id": "alice"}}, token="tok-a")
            assert "error" not in ctx
            post = srv.rpc("tools/call", {"name": "ncp_post_turn", "arguments": {
                **GET_CTX, "agent_id": "alice", "result_summary": "done"}}, token="tok-a")
            assert "error" not in post, post

    def test_tools_key_validation(self) -> None:
        base = {"agent_id": "x", "token_sha256": hash_token("t")}
        registry = load_principals({"p": {**base, "tools": ["ncp_fetch"]}})
        assert registry is not None and registry.principals[0].tools == frozenset({"ncp_fetch"})
        assert load_principals({"p": base}).principals[0].tools is None  # type: ignore[union-attr]
        with pytest.raises(PrincipalConfigError):
            load_principals({"p": {**base, "tools": []}})
        with pytest.raises(PrincipalConfigError):
            load_principals({"p": {**base, "tools": "ncp_fetch"}})


class TestStdioProfile:
    def test_serve_streams_profile_narrows(self, tmp_path: Path) -> None:
        (tmp_path / ".git").mkdir()
        body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/list"}).encode()
        stream = io.BytesIO(f"Content-Length: {len(body)}\r\n\r\n".encode() + body)
        out = io.BytesIO()
        serve_streams(stream, out, cwd=tmp_path, profile="minimal")
        payload = out.getvalue().split(b"\r\n\r\n", 1)[1]
        assert sorted(t["name"] for t in json.loads(payload)["result"]["tools"]) == sorted(MINIMAL_TOOL_NAMES)
