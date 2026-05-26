from __future__ import annotations

from contextlib import closing
import io
import json
import socket
import threading
from pathlib import Path

import httpx

from ncp.mcp.server import (
    MCP_TOOLS,
    _handle_request,
    _read_message,
    create_http_server,
    make_handlers,
    serve_streams,
)
from ncp.stores.base import BaseStore
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


def _free_port() -> int:
    with closing(socket.socket(socket.AF_INET, socket.SOCK_STREAM)) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


class TestInitialize:
    def test_initialize(self) -> None:
        resp = _handle_request(_req("initialize", {"protocolVersion": "2024-11-05"}), {})
        result = json.loads(resp)["result"]
        assert result["protocolVersion"] == "2024-11-05"
        assert result["serverInfo"]["name"] == "ncp"
        assert result["capabilities"]["tools"] == {"listChanged": False}

    def test_initialize_accepts_latest_claude_protocol(self) -> None:
        resp = _handle_request(_req("initialize", {"protocolVersion": "2025-11-25"}), {})
        result = json.loads(resp)["result"]
        assert result["protocolVersion"] == "2025-11-25"

    def test_stdio_framing_round_trip(self, tmp_path: Path) -> None:
        project = tmp_path / "repo"
        (project / ".git").mkdir(parents=True)
        input_stream = io.BytesIO(_frame(_req("tools/list")))
        output_stream = io.BytesIO()

        serve_streams(input_stream, output_stream, cwd=project)

        framed_response = _read_message(io.BytesIO(output_stream.getvalue()))
        assert framed_response is not None
        assert framed_response["result"]["tools"] == MCP_TOOLS

    def test_http_transport_handles_initialize_and_tools_list(self, tmp_path: Path) -> None:
        port = _free_port()
        server = create_http_server(host="127.0.0.1", port=port, cwd=tmp_path)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with httpx.Client(base_url=f"http://127.0.0.1:{port}", timeout=5.0) as client:
                health = client.get("/healthz")
                assert health.status_code == 200
                assert health.json()["transport"] == "http_sse"

                sse = client.build_request("GET", "/sse")
                response = client.send(sse, stream=True)
                try:
                    assert response.status_code == 200
                    first_text = next(response.iter_text())
                    assert "event: endpoint" in first_text
                    assert "/mcp" in first_text
                finally:
                    response.close()

                initialize = client.post(
                    "/mcp",
                    json=_req("initialize", {"protocolVersion": "2025-11-25"}),
                )
                assert initialize.status_code == 200
                assert initialize.json()["result"]["protocolVersion"] == "2025-11-25"

                tools = client.post("/mcp", json=_req("tools/list"))
                assert tools.status_code == 200
                assert tools.json()["result"]["tools"] == MCP_TOOLS
        finally:
            server._shutdown_event.set()
            server.shutdown()
            thread.join(timeout=5)


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

    def test_fetch_limit_can_use_store_coordination_backend(self) -> None:
        class _Coordination:
            def __init__(self) -> None:
                self.sessions: dict[str, dict[str, object]] = {}

            def reset_fetch_session(self, session_id: str, *, pipeline_id: str | None = None, ttl_seconds: int = 3600) -> None:
                self.sessions[session_id] = {"fetch_count": 0, "pipeline_id": pipeline_id}

            def claim_fetch_slot(self, session_id: str, *, pipeline_id: str | None = None, max_fetches: int = 3, ttl_seconds: int = 3600) -> tuple[int, str | None]:
                payload = self.sessions.setdefault(session_id, {"fetch_count": 0, "pipeline_id": None})
                count = int(payload["fetch_count"])
                if count >= max_fetches:
                    raise ValueError("ncp_fetch limit reached: max 3 per session")
                payload["fetch_count"] = count + 1
                if pipeline_id is not None:
                    payload["pipeline_id"] = pipeline_id
                return int(payload["fetch_count"]), payload["pipeline_id"]  # type: ignore[return-value]

        class _Store:
            def __init__(self) -> None:
                self.coordination = _Coordination()

            def write(self, chunk):  # pragma: no cover - not used
                return True

            def query(self, text: str, *, k: int = 4, layer=None, pipeline_id=None, scope=None, zone: str = "working") -> list[SubconsciousChunk]:
                return [
                    SubconsciousChunk(
                        chunk_id="sub_coord",
                        content=f"{pipeline_id}:{text}",
                        layer="semantic",
                        src="tool_result",
                        pipeline_id=pipeline_id,
                    )
                ]

            def emit_whisper(self, whisper):  # pragma: no cover - not used
                return None

            def drain_whispers(self, *, agent_id, pipeline_id=None, max_items=3, min_confidence=0.60):
                return []

            def get_working_zone(self, *, pipeline_id=None, layer=None):
                return []

            def log_turn_record(self, record):  # pragma: no cover - not used
                return None

            def resolve_recent_ref(self, ref: str):
                return None

            def log_cost(self, *, agent_id, response):
                return None

        handlers = make_handlers(_Store())
        _handle_request(
            _call(
                "ncp_get_context",
                {
                    "agent_id": "builder",
                    "role": "build",
                    "owns": [],
                    "must_not": [],
                    "task": "test",
                    "slot": "test",
                    "intent": "test",
                    "pipeline_id": "pipe_coord",
                    "session_id": "sess_coord",
                },
            ),
            handlers,
        )

        for _ in range(3):
            _handle_request(_call("ncp_fetch", {"query": "coord", "session_id": "sess_coord"}), handlers)

        err = _handle_request(_call("ncp_fetch", {"query": "coord", "session_id": "sess_coord"}, req_id=99), handlers)

        assert _error(err)["message"] == "Tool error: ncp_fetch limit reached: max 3 per session"


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

    def test_not_implemented_backend_error_is_explicit(self) -> None:
        class _PendingStore(BaseStore):
            def write(self, chunk):
                raise NotImplementedError("pending backend path")

            def query(self, text, *, k=4, min_score=0.01, layer=None, pipeline_id=None, scope=None, zone="working"):
                raise NotImplementedError("pending backend path")

            def emit_whisper(self, whisper):
                raise NotImplementedError("pending backend path")

            def drain_whispers(self, *, agent_id, pipeline_id=None, max_items=3, min_confidence=0.60):
                raise NotImplementedError("pending backend path")

            def peek_whispers(self, *, agent_id, pipeline_id=None, max_items=3, min_confidence=0.60):
                raise NotImplementedError("pending backend path")

            def acknowledge_whispers(self, whisper_ids):
                raise NotImplementedError("pending backend path")

            def get_working_zone(self, *, pipeline_id=None, layer=None):
                raise NotImplementedError("pending backend path")

            def log_turn_record(self, record):
                raise NotImplementedError("pending backend path")

            def resolve_recent_ref(self, ref):
                raise NotImplementedError("pending backend path")

            def log_conscious(self, conscious, *, snapshot_hash):
                raise NotImplementedError("pending backend path")

            def log_cost(self, *, agent_id, response):
                raise NotImplementedError("pending backend path")

            def log_cost_raw(self, *, agent_id, model, input_tokens, output_tokens, cost_usd, pipeline_id=None, turn_id, latency_ms=0):
                raise NotImplementedError("pending backend path")

            def get_pipeline_goal_versions(self, *, pipeline_id, current_agent=None):
                raise NotImplementedError("pending backend path")

            def consolidate(self, *, pipeline_id=None, dry_run=False, similarity_threshold=0.65, trust_floor=0.10):
                raise NotImplementedError("pending backend path")

            def calibrate(self, *, pipeline_id=None, chunk_id=None, trust=None, dry_run=False, decay_factor=0.85, recency_half_life_seconds=14400):
                raise NotImplementedError("pending backend path")

            def viz_data(self, *, pipeline_id=None):
                raise NotImplementedError("pending backend path")

        handlers = make_handlers(_PendingStore())
        resp = _handle_request(
            _call(
                "ncp_emit_whisper",
                {
                    "from": "builder",
                    "target": "executor",
                    "type": "nudge",
                    "payload": "check this",
                    "confidence": 0.9,
                },
            ),
            handlers,
        )

        error = _error(resp)
        assert error["code"] == -32603
        assert "Tool unavailable for the configured store backend" in error["message"]
