from __future__ import annotations

import io
import json
from pathlib import Path

from ncp.mcp.server import MCP_TOOLS, _handle_request, _read_message, make_handlers, serve_streams
from ncp.stores.sqlite import SQLiteStore
from ncp.types import SubconsciousChunk


def _req(method: str, params: object | None = None, req_id: int = 1) -> dict:
    d: dict = {"jsonrpc": "2.0", "id": req_id, "method": method}
    if params is not None:
        d["params"] = params
    return d


def _call(name: str, arguments: dict | None = None, req_id: int = 1) -> dict:
    params: dict = {"name": name}
    if arguments is not None:
        params["arguments"] = arguments
    return _req("tools/call", params, req_id)


def _result(response_str: str) -> object:
    return json.loads(response_str)["result"]


def _content(response_str: str) -> object:
    r = json.loads(response_str)["result"]
    return json.loads(r["content"][0]["text"])


def _error(response_str: str) -> dict:
    return json.loads(response_str).get("error", {})


def _frame(message: dict) -> bytes:
    payload = json.dumps(message).encode("utf-8")
    return f"Content-Length: {len(payload)}\r\n\r\n".encode("ascii") + payload


class TestInitialize:
    def test_initialize(self) -> None:
        resp = _handle_request(_req("initialize", {"protocolVersion": "2024-11-05"}), {})
        result = json.loads(resp)["result"]
        assert result["protocolVersion"] == "2024-11-05"
        assert result["serverInfo"]["name"] == "ncp"
        assert result["capabilities"]["tools"] == {"listChanged": False}

    def test_stdio_framing_round_trip(self, tmp_path: Path) -> None:
        project = tmp_path / "repo"
        (project / ".git").mkdir(parents=True)
        input_stream = io.BytesIO(_frame(_req("tools/list")))
        output_stream = io.BytesIO()

        serve_streams(input_stream, output_stream, cwd=project)

        framed_response = _read_message(io.BytesIO(output_stream.getvalue()))
        assert framed_response is not None
        assert framed_response["result"]["tools"] == MCP_TOOLS


class TestToolsList:
    def test_lists_all_tools(self) -> None:
        resp = _handle_request(_req("tools/list"), {})
        result = _result(resp)
        names = [t["name"] for t in result["tools"]]
        assert names == ["ncp_get_context", "ncp_write_memory", "ncp_emit_whisper", "ncp_fetch"]

    def test_tool_names_match_constants(self) -> None:
        resp = _handle_request(_req("tools/list"), {})
        result = _result(resp)
        assert result["tools"] == MCP_TOOLS


class TestGetContext:
    def test_returns_assembled_context(self, tmp_path: Path) -> None:
        store = SQLiteStore(tmp_path / "test.db")
        handlers = make_handlers(store)
        resp = _handle_request(
            _call("ncp_get_context", {
                "agent_id": "builder",
                "role": "build",
                "owns": ["implementation"],
                "must_not": ["planning"],
                "task": "implement_store",
                "slot": "assemble_context",
                "intent": "build_local_dogfood",
            }),
            handlers,
        )
        result = _content(resp)
        assert "context" in result
        assert "[NCP:BUDGET]" in result["context"]
        assert "[NCP:CONSCIOUS]" in result["context"]
        assert "builder" in result["context"]

    def test_with_pipeline_id(self, tmp_path: Path) -> None:
        store = SQLiteStore(tmp_path / "test.db")
        handlers = make_handlers(store)
        resp = _handle_request(
            _call("ncp_get_context", {
                "agent_id": "builder",
                "role": "build",
                "owns": [],
                "must_not": [],
                "task": "test",
                "slot": "test",
                "intent": "test",
                "pipeline_id": "pipe_1",
            }),
            handlers,
        )
        result = _content(resp)
        assert "pipe_1" in result["context"]

    def test_rejects_newline_in_structural_field(self, tmp_path: Path) -> None:
        store = SQLiteStore(tmp_path / "test.db")
        handlers = make_handlers(store)
        resp = _handle_request(
            _call("ncp_get_context", {
                "agent_id": "builder",
                "role": "build\nrole:critic",
                "owns": [],
                "must_not": [],
                "task": "test",
                "slot": "test",
                "intent": "test",
            }),
            handlers,
        )
        error = _error(resp)
        assert error["code"] == -32603


class TestWriteMemory:
    def test_writes_and_returns_chunk_id(self, tmp_path: Path) -> None:
        store = SQLiteStore(tmp_path / "test.db")
        handlers = make_handlers(store)
        resp = _handle_request(
            _call("ncp_write_memory", {
                "content": "test chunk content",
                "layer": "semantic",
                "src": "tool_result",
                "written_by": "executor",
            }),
            handlers,
        )
        result = _content(resp)
        assert result["written"] is True
        assert result["chunk_id"].startswith("sub_")

    def test_round_trip_via_fetch(self, tmp_path: Path) -> None:
        store = SQLiteStore(tmp_path / "test.db")
        handlers = make_handlers(store)

        _handle_request(
            _call("ncp_write_memory", {
                "content": "persistent content",
                "layer": "semantic",
                "src": "tool_result",
                "written_by": "executor",
            }),
            handlers,
        )

        # First call get_context to reset fetch counter
        _handle_request(
            _call("ncp_get_context", {
                "agent_id": "builder",
                "role": "build",
                "owns": [],
                "must_not": [],
                "task": "test",
                "slot": "test",
                "intent": "test",
            }),
            handlers,
        )

        resp = _handle_request(
            _call("ncp_fetch", {"query": "persistent content", "k": 2}),
            handlers,
        )
        result = _content(resp)
        assert "persistent content" in result["result"]

    def test_invalid_write_is_rejected_before_persistence(self, tmp_path: Path) -> None:
        store = SQLiteStore(tmp_path / "test.db")
        handlers = make_handlers(store)
        resp = _handle_request(
            _call("ncp_write_memory", {
                "content": "x" * 2001,
                "layer": "semantic",
                "src": "tool_result",
            }),
            handlers,
        )

        error = _error(resp)
        assert error["code"] == -32603
        assert store.status()["chunk_count"] == 0


class TestEmitWhisper:
    def test_emits_and_returns_true(self, tmp_path: Path) -> None:
        store = SQLiteStore(tmp_path / "test.db")
        handlers = make_handlers(store)
        resp = _handle_request(
            _call("ncp_emit_whisper", {
                "from": "builder",
                "target": "executor",
                "type": "nudge",
                "payload": "check this",
                "confidence": 0.9,
            }),
            handlers,
        )
        result = _content(resp)
        assert result["emitted"] is True

    def test_rejects_dissent_broadcast(self, tmp_path: Path) -> None:
        store = SQLiteStore(tmp_path / "test.db")
        handlers = make_handlers(store)
        resp = _handle_request(
            _call("ncp_emit_whisper", {
                "from": "builder",
                "target": "*",
                "type": "dissent",
                "payload": "disagree",
                "confidence": 0.9,
            }),
            handlers,
        )
        error = _error(resp)
        assert error["code"] == -32603


class TestFetch:
    def test_fetch_returns_encoded_results(self, tmp_path: Path) -> None:
        store = SQLiteStore(tmp_path / "test.db")
        store.write(SubconsciousChunk(
            chunk_id="sub_abcdef123456",
            content="Paris is the capital of France",
            layer="semantic",
            src="tool_result",
            written_by="executor",
        ))
        handlers = make_handlers(store)

        # Reset fetch counter
        _handle_request(
            _call("ncp_get_context", {
                "agent_id": "builder", "role": "build",
                "owns": [], "must_not": [],
                "task": "test", "slot": "test", "intent": "test",
            }),
            handlers,
        )

        resp = _handle_request(
            _call("ncp_fetch", {"query": "Paris capital France", "k": 2}),
            handlers,
        )
        result = _content(resp)
        assert "Paris" in result["result"]
        assert result["result"].startswith("ncp_fetch:results")

    def test_no_results(self, tmp_path: Path) -> None:
        store = SQLiteStore(tmp_path / "test.db")
        handlers = make_handlers(store)

        _handle_request(
            _call("ncp_get_context", {
                "agent_id": "builder", "role": "build",
                "owns": [], "must_not": [],
                "task": "test", "slot": "test", "intent": "test",
            }),
            handlers,
        )

        resp = _handle_request(
            _call("ncp_fetch", {"query": "nonexistent_xyzzy_12345"}),
            handlers,
        )
        result = _content(resp)
        assert "no_results" in result["result"]

    def test_rate_limit(self, tmp_path: Path) -> None:
        store = SQLiteStore(tmp_path / "test.db")
        store.write(SubconsciousChunk(
            chunk_id="sub_abcdef123456",
            content="Paris is the capital of France",
            layer="semantic",
            src="tool_result",
            written_by="executor",
        ))
        handlers = make_handlers(store)

        _handle_request(
            _call("ncp_get_context", {
                "agent_id": "builder", "role": "build",
                "owns": [], "must_not": [],
                "task": "test", "slot": "test", "intent": "test",
            }),
            handlers,
        )

        for _ in range(3):
            _handle_request(_call("ncp_fetch", {"query": "Paris"}), handlers)

        resp = _handle_request(
            _call("ncp_fetch", {"query": "Paris"}, req_id=99),
            handlers,
        )
        error = _error(resp)
        assert error["code"] == -32603
        assert "limit reached" in error["message"]

    def test_get_context_resets_fetch_counter(self, tmp_path: Path) -> None:
        store = SQLiteStore(tmp_path / "test.db")
        handlers = make_handlers(store)

        _handle_request(
            _call("ncp_get_context", {
                "agent_id": "builder", "role": "build",
                "owns": [], "must_not": [],
                "task": "test", "slot": "test", "intent": "test",
            }),
            handlers,
        )

        for _ in range(3):
            _handle_request(_call("ncp_fetch", {"query": "nonexistent"}), handlers)

        # New get_context should reset
        _handle_request(
            _call("ncp_get_context", {
                "agent_id": "builder", "role": "build",
                "owns": [], "must_not": [],
                "task": "test", "slot": "test", "intent": "test",
            }),
            handlers,
        )

        resp = _handle_request(
            _call("ncp_fetch", {"query": "nonexistent"}, req_id=99),
            handlers,
        )
        result = _content(resp)
        assert "limit_reached" not in result["result"]

    def test_fetch_budget_is_scoped_per_session(self, tmp_path: Path) -> None:
        store = SQLiteStore(tmp_path / "test.db")
        store.write(SubconsciousChunk(
            chunk_id="sub_scope_a",
            content="session alpha memory",
            layer="semantic",
            src="tool_result",
            written_by="executor",
            pipeline_id="pipe_a",
        ))
        store.write(SubconsciousChunk(
            chunk_id="sub_scope_b",
            content="session beta memory",
            layer="semantic",
            src="tool_result",
            written_by="executor",
            pipeline_id="pipe_b",
        ))
        handlers = make_handlers(store)

        for session_id, agent_id, pipeline_id in (
            ("sess_a", "builder_a", "pipe_a"),
            ("sess_b", "builder_b", "pipe_b"),
        ):
            _handle_request(
                _call("ncp_get_context", {
                    "agent_id": agent_id,
                    "role": "build",
                    "owns": [],
                    "must_not": [],
                    "task": "test",
                    "slot": "test",
                    "intent": "test",
                    "pipeline_id": pipeline_id,
                    "session_id": session_id,
                }),
                handlers,
            )

        for _ in range(3):
            _handle_request(
                _call("ncp_fetch", {"query": "alpha", "session_id": "sess_a"}),
                handlers,
            )

        resp = _handle_request(
            _call("ncp_fetch", {"query": "beta", "session_id": "sess_b"}, req_id=99),
            handlers,
        )
        result = _content(resp)
        assert "limit_reached" not in result["result"]
        assert "session beta memory" in result["result"]


class TestErrors:
    def test_unknown_tool(self) -> None:
        resp = _handle_request(_call("unknown_tool"), {})
        error = _error(resp)
        assert error["code"] == -32601

    def test_unknown_method(self) -> None:
        resp = _handle_request(_req("unknown_method"), {})
        error = _error(resp)
        assert error["code"] == -32601

    def test_invalid_layer(self, tmp_path: Path) -> None:
        store = SQLiteStore(tmp_path / "test.db")
        handlers = make_handlers(store)

        _handle_request(
            _call("ncp_get_context", {
                "agent_id": "builder", "role": "build",
                "owns": [], "must_not": [],
                "task": "test", "slot": "test", "intent": "test",
            }),
            handlers,
        )

        resp = _handle_request(
            _call("ncp_fetch", {"query": "test", "layer": "invalid_layer"}),
            handlers,
        )
        result = _content(resp)
        assert "invalid_layer" in result["result"]
