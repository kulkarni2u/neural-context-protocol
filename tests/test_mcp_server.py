from __future__ import annotations

from contextlib import closing
import io
import json
import socket
import threading
from pathlib import Path

import httpx
import pytest

from ncp.version import __version__
from ncp.config import load_config
from ncp.tokens import estimate_tokens
from ncp.mcp.server import (
    MCP_TOOLS,
    StreamResponse,
    _handle_request,
    _read_message,
    create_http_server,
    make_handlers,
    serve_streams,
)
from ncp.stores.base import BaseStore
from ncp.stores.sqlite import SQLiteStore
from ncp.types import ConsciousBlock, SubconsciousChunk, Whisper


FULL_NON_MEMO_TOOL_NAMES = [
    "ncp_get_context",
    "ncp_write_memory",
    "ncp_emit_whisper",
    "ncp_post_turn",
    "ncp_remember",
    "ncp_recall",
    "ncp_improve",
    "ncp_fetch",
    "ncp_record_decision",
    "ncp_record_outcome",
]


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
        assert result["serverInfo"]["version"] == __version__
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
        assert [tool["name"] for tool in framed_response["result"]["tools"]] == FULL_NON_MEMO_TOOL_NAMES

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
                assert [tool["name"] for tool in tools.json()["result"]["tools"]] == FULL_NON_MEMO_TOOL_NAMES
        finally:
            server._shutdown_event.set()
            server.shutdown()
            thread.join(timeout=5)

    def test_http_transport_enforces_auth_when_token_configured(self, tmp_path: Path) -> None:
        port = _free_port()
        server = create_http_server(host="127.0.0.1", port=port, cwd=tmp_path, auth_token="secret")
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with httpx.Client(base_url=f"http://127.0.0.1:{port}", timeout=5.0) as client:
                denied = client.post("/mcp", json=_req("tools/list"))
                assert denied.status_code == 401

                allowed = client.post(
                    "/mcp",
                    json=_req("tools/list"),
                    headers={"Authorization": "Bearer secret"},
                )
                assert allowed.status_code == 200
                assert allowed.json()["result"]["tools"]
        finally:
            server._shutdown_event.set()
            server.shutdown()
            thread.join(timeout=5)

    def test_http_transport_rejects_oversized_body(self, tmp_path: Path) -> None:
        port = _free_port()
        server = create_http_server(host="127.0.0.1", port=port, cwd=tmp_path, max_body_bytes=8)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with httpx.Client(base_url=f"http://127.0.0.1:{port}", timeout=5.0) as client:
                response = client.post("/mcp", content=b"0123456789")
                assert response.status_code == 413
                assert response.json()["error"] == "request_too_large"
        finally:
            server._shutdown_event.set()
            server.shutdown()
            thread.join(timeout=5)

    def test_http_transport_cors_is_allowlist_only(self, tmp_path: Path) -> None:
        port = _free_port()
        server = create_http_server(
            host="127.0.0.1",
            port=port,
            cwd=tmp_path,
            cors_allowed_origins=["https://allowed.example"],
        )
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with httpx.Client(base_url=f"http://127.0.0.1:{port}", timeout=5.0) as client:
                allowed = client.options("/mcp", headers={"Origin": "https://allowed.example"})
                assert allowed.headers["access-control-allow-origin"] == "https://allowed.example"

                denied = client.options("/mcp", headers={"Origin": "https://other.example"})
                assert "access-control-allow-origin" not in denied.headers
        finally:
            server._shutdown_event.set()
            server.shutdown()
            thread.join(timeout=5)


class TestToolsList:
    def test_lists_all_tools(self) -> None:
        resp = _handle_request(_req("tools/list"), {})
        result = _result(resp)
        names = [t["name"] for t in result["tools"]]
        assert names == [
            "ncp_get_context",
            "ncp_write_memory",
            "ncp_emit_whisper",
            "ncp_post_turn",
            "ncp_remember",
            "ncp_recall",
            "ncp_improve",
            "ncp_fetch",
            "ncp_record_decision",
            "ncp_record_outcome",
            "ncp_lookup_memo",
            "ncp_record_memo",
        ]

    def test_tool_names_match_constants(self) -> None:
        resp = _handle_request(_req("tools/list"), {})
        result = _result(resp)
        assert result["tools"] == MCP_TOOLS

    def test_get_context_schema_exposes_max_tokens(self) -> None:
        get_context_tool = next(tool for tool in MCP_TOOLS if tool["name"] == "ncp_get_context")
        schema = get_context_tool["inputSchema"]
        assert "max_tokens" in schema["properties"]  # type: ignore[index]

    def test_emit_whisper_schema_exposes_ttl_and_payload_contracts(self) -> None:
        emit_tool = next(tool for tool in MCP_TOOLS if tool["name"] == "ncp_emit_whisper")
        schema = emit_tool["inputSchema"]
        properties = schema["properties"]  # type: ignore[index]
        assert "ttl_seconds" in properties
        assert "share/request" in properties["payload"]["description"]
        assert "dissent" in properties["payload"]["description"]

    def test_tool_profile_core_tools_list_advertises_only_context_lifecycle(self, tmp_path: Path) -> None:
        project = tmp_path / "repo"
        (project / ".git").mkdir(parents=True)
        (project / ".ncp").mkdir()
        (project / ".ncp" / "config.toml").write_text('[tools]\nprofile = "core"\n')
        input_stream = io.BytesIO(_frame(_req("tools/list")))
        output_stream = io.BytesIO()

        serve_streams(input_stream, output_stream, cwd=project)

        response = _read_message(io.BytesIO(output_stream.getvalue()))
        assert response is not None
        assert [tool["name"] for tool in response["result"]["tools"]] == [
            "ncp_get_context",
            "ncp_write_memory",
            "ncp_emit_whisper",
            "ncp_post_turn",
            "ncp_fetch",
        ]

    def test_tool_profile_full_tools_list_hides_disabled_memo_tools(self, tmp_path: Path) -> None:
        project = tmp_path / "repo"
        (project / ".git").mkdir(parents=True)
        (project / ".ncp").mkdir()
        (project / ".ncp" / "config.toml").write_text('[tools]\nprofile = "full"\n')
        input_stream = io.BytesIO(_frame(_req("tools/list")))
        output_stream = io.BytesIO()

        serve_streams(input_stream, output_stream, cwd=project)

        response = _read_message(io.BytesIO(output_stream.getvalue()))
        assert response is not None
        assert [tool["name"] for tool in response["result"]["tools"]] == FULL_NON_MEMO_TOOL_NAMES

    def test_tool_profile_full_memo_tools_are_advertised_when_enabled(self, tmp_path: Path) -> None:
        project = tmp_path / "repo"
        (project / ".git").mkdir(parents=True)
        (project / ".ncp").mkdir()
        (project / ".ncp" / "config.toml").write_text(
            '[tools]\nprofile = "full"\n\n[memoization]\nenabled = true\n'
        )
        input_stream = io.BytesIO(_frame(_req("tools/list")))
        output_stream = io.BytesIO()

        serve_streams(input_stream, output_stream, cwd=project)

        response = _read_message(io.BytesIO(output_stream.getvalue()))
        assert response is not None
        assert [tool["name"] for tool in response["result"]["tools"]] == [tool["name"] for tool in MCP_TOOLS]

    def test_disabled_memo_tools_are_not_callable(self, tmp_path: Path) -> None:
        project = tmp_path / "repo"
        (project / ".git").mkdir(parents=True)
        input_stream = io.BytesIO(_frame(_call("ncp_lookup_memo", {"task": "task", "context": "context"})))
        output_stream = io.BytesIO()

        serve_streams(input_stream, output_stream, cwd=project)

        response = _read_message(io.BytesIO(output_stream.getvalue()))
        assert response is not None
        assert response["error"]["code"] == -32601

    def test_tool_profile_http_and_stdio_share_the_core_catalog(self, tmp_path: Path) -> None:
        project = tmp_path / "repo"
        (project / ".git").mkdir(parents=True)
        (project / ".ncp").mkdir()
        (project / ".ncp" / "config.toml").write_text('[tools]\nprofile = "core"\n')
        input_stream = io.BytesIO(_frame(_req("tools/list")))
        output_stream = io.BytesIO()
        serve_streams(input_stream, output_stream, cwd=project)
        stdio_response = _read_message(io.BytesIO(output_stream.getvalue()))
        assert stdio_response is not None

        port = _free_port()
        server = create_http_server(host="127.0.0.1", port=port, cwd=project)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with httpx.Client(base_url=f"http://127.0.0.1:{port}", timeout=5.0) as client:
                http_response = client.post("/mcp", json=_req("tools/list"))
            assert http_response.status_code == 200
            assert http_response.json()["result"]["tools"] == stdio_response["result"]["tools"]
            assert [tool["name"] for tool in http_response.json()["result"]["tools"]] == [
                "ncp_get_context",
                "ncp_write_memory",
                "ncp_emit_whisper",
                "ncp_post_turn",
                "ncp_fetch",
            ]
        finally:
            server._shutdown_event.set()
            server.shutdown()
            thread.join(timeout=5)


class TestMemoryTools:
    def test_remember_recall_improve_round_trip(self, tmp_path: Path) -> None:
        store = SQLiteStore(tmp_path / "test.db")
        handlers = make_handlers(store)

        remembered = _content(_handle_request(
            _call("ncp_remember", {
                "content": "ACH trial retries need a null guard. Release checks run preflight.",
                "pipeline_id": "pipe_memory",
                "written_by": "tester",
            }),
            handlers,
        ))

        assert remembered["remembered"] is True
        assert len(remembered["atom_chunk_ids"]) == 2
        assert remembered["edge_count"] == 2

        recalled = _content(_handle_request(
            _call("ncp_recall", {
                "query": "ACH null guard",
                "pipeline_id": "pipe_memory",
            }),
            handlers,
        ))

        assert recalled["result"].startswith("ncp_recall:results")
        assert "ACH trial retries need a null guard." in recalled["result"]

        improved = _content(_handle_request(
            _call("ncp_improve", {"pipeline_id": "pipe_memory"}),
            handlers,
        ))

        assert improved["improved"] is True
        assert improved["consolidated_groups"] >= 0


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

    def test_get_context_honors_max_tokens(self, tmp_path: Path) -> None:
        store = SQLiteStore(tmp_path / "bounded.db")
        for index in range(4):
            store.write(SubconsciousChunk(
                chunk_id=f"large_{index}",
                content=" ".join(["bounded mcp context"] + [f"tokenish_{index}_{j}" for j in range(120)]),
                layer="semantic",
                src="tool_result",
                pipeline_id="pipe_mcp",
            ))
        handlers = make_handlers(store)

        resp = _handle_request(
            _call("ncp_get_context", {
                "agent_id": "builder",
                "role": "build",
                "owns": [],
                "must_not": [],
                "task": "bounded_mcp",
                "slot": "context",
                "intent": "test_bound",
                "pipeline_id": "pipe_mcp",
                "max_tokens": 200,
            }),
            handlers,
        )

        result = _content(resp)
        assert estimate_tokens(result["context"]) <= 200
        assert "[NCP:BUDGET]" in result["context"]
        assert "[NCP:CONSCIOUS]" in result["context"]

    def test_get_context_uses_config_token_budget_when_max_tokens_omitted(self, tmp_path: Path) -> None:
        store = SQLiteStore(tmp_path / "config_budget.db")
        for index in range(4):
            store.write(SubconsciousChunk(
                chunk_id=f"config_budget_{index}",
                content=" ".join(["config budget mcp context"] + [f"detail_{index}_{j}" for j in range(120)]),
                layer="semantic",
                src="tool_result",
                pipeline_id="pipe_mcp",
            ))
        config = load_config(cwd=tmp_path)
        config.values["budget"]["context_token_budget"] = 200
        handlers = make_handlers(store, config=config)

        resp = _handle_request(
            _call("ncp_get_context", {
                "agent_id": "builder",
                "role": "build",
                "owns": [],
                "must_not": [],
                "task": "config_budget",
                "slot": "context",
                "intent": "test_config_bound",
                "pipeline_id": "pipe_mcp",
            }),
            handlers,
        )

        result = _content(resp)
        assert estimate_tokens(result["context"]) <= 200
        assert "[NCP:BUDGET]" in result["context"]
        assert "[NCP:CONSCIOUS]" in result["context"]

    def test_get_context_surfaces_eviction_telemetry_and_fetch_hint(self, tmp_path: Path) -> None:
        store = SQLiteStore(tmp_path / "telemetry.db")
        for index in range(2):
            store.write(SubconsciousChunk(
                chunk_id=f"telemetry_large_{index}",
                content=" ".join(["telemetry_probe relevant fact"] + [f"detail_{index}_{j}" for j in range(160)]),
                layer="semantic",
                src="tool_result",
                pipeline_id="pipe_mcp",
                relevance=0.95,
            ))
        handlers = make_handlers(store)

        resp = _handle_request(
            _call("ncp_get_context", {
                "agent_id": "builder",
                "role": "build",
                "owns": [],
                "must_not": [],
                "task": "telemetry_probe",
                "slot": "context",
                "intent": "test_telemetry",
                "pipeline_id": "pipe_mcp",
                "max_tokens": 160,
            }),
            handlers,
        )

        result = _content(resp)
        telemetry = result["telemetry"]
        assert telemetry["evicted_high_relevance_count"] >= 1
        assert telemetry["fetch_budget_remaining"] == 3
        assert telemetry["fetch_hint"] == "ncp_fetch"
        assert telemetry["evicted_high_relevance"]

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

    def test_post_turn_hydrates_next_context_and_acknowledges_whispers(self, tmp_path: Path) -> None:
        store = SQLiteStore(tmp_path / "test.db")
        store.emit_whisper(
            Whisper(
                whisper_id="wsp_pending",
                from_agent="planner",
                target="builder",
                whisper_type="share",
                payload='{"ask":"use this"}',
                confidence=0.9,
                pipeline_id="pipe_1",
            )
        )
        handlers = make_handlers(store)

        first = _content(_handle_request(
            _call("ncp_get_context", {
                "agent_id": "builder",
                "role": "build",
                "task": "test",
                "slot": "test",
                "intent": "test",
                "pipeline_id": "pipe_1",
            }),
            handlers,
        ))
        assert first["pending_whisper_ids"] == ["wsp_pending"]

        posted = _content(_handle_request(
            _call("ncp_post_turn", {
                "agent_id": "builder",
                "role": "build",
                "task": "test",
                "slot": "test",
                "intent": "test",
                "pipeline_id": "pipe_1",
                "turn_id": "turn_builder",
                "result_summary": "summary",
                "result_full": "full result",
                "ack_whisper_ids": ["wsp_pending"],
            }),
            handlers,
        ))
        assert posted["posted"] is True
        assert store.peek_whispers(agent_id="builder", pipeline_id="pipe_1") == []

        second = _content(_handle_request(
            _call("ncp_get_context", {
                "agent_id": "builder",
                "role": "build",
                "task": "test",
                "slot": "next",
                "intent": "test",
                "pipeline_id": "pipe_1",
            }),
            handlers,
        ))
        assert "recent:[r:sub/turn_builder]" in second["context"]

    def test_post_turn_reports_dedup_suppressed_memory_chunks(self, tmp_path: Path) -> None:
        # WI-007(a): a memory chunk suppressed by the store's dedup check is
        # surfaced in the ncp_post_turn response, not silently dropped.
        store = SQLiteStore(tmp_path / "test.db")
        handlers = make_handlers(store)
        content = "the deployment pipeline validates artifacts before every release"
        store.write(SubconsciousChunk(
            chunk_id="sub_seed",
            layer="semantic",
            content=content,
            src="tool_result",
            pipeline_id="pipe_1",
        ))

        posted = _content(_handle_request(
            _call("ncp_post_turn", {
                "agent_id": "builder",
                "role": "build",
                "task": "test",
                "slot": "test",
                "intent": "test",
                "pipeline_id": "pipe_1",
                "turn_id": "turn_dedup",
                "result_summary": "summary",
                "result_full": "full result",
                "memory_chunks": [
                    {
                        "chunk_id": "sub_fresh",
                        "content": "rollbacks require a signed approval from the release captain",
                        "layer": "semantic",
                        "src": "tool_result",
                    },
                    {
                        "chunk_id": "sub_dup",
                        "content": content,
                        "layer": "semantic",
                        "src": "tool_result",
                    },
                ],
            }),
            handlers,
        ))

        assert posted["posted"] is True
        assert posted["suppressed_chunk_ids"] == ["sub_dup"]
        persisted = store.query("rollbacks signed approval release captain", pipeline_id="pipe_1")
        assert any(chunk.chunk_id == "sub_fresh" for chunk in persisted)

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

    def test_write_memory_derives_trust_and_drift_from_conscious_state(self, tmp_path: Path) -> None:
        store = SQLiteStore(tmp_path / "test.db")
        store.log_conscious(
            ConsciousBlock(
                agent_id="executor",
                role="build",
                owns=[],
                must_not=[],
                task="task",
                slot="slot",
                intent="intent",
                pipeline_id="pipe_1",
                drift_score=0.42,
            ),
            snapshot_hash="hash_executor",
        )
        handlers = make_handlers(store)

        result = _content(_handle_request(
            _call("ncp_write_memory", {
                "content": "trusted tool result",
                "layer": "semantic",
                "src": "tool_result",
                "written_by": "executor",
                "pipeline_id": "pipe_1",
            }),
            handlers,
        ))

        chunk = next(chunk for chunk in store.get_working_zone(pipeline_id="pipe_1") if chunk.chunk_id == result["chunk_id"])
        assert chunk.base_trust == 0.8
        assert chunk.written_at_drift == 0.42

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

    def test_write_memory_filters_noisy_content(self, tmp_path: Path) -> None:
        store = SQLiteStore(tmp_path / "test.db")
        handlers = make_handlers(store)
        noisy = "\x1b[32mPASSED\x1b[0m\nPASSED\nPASSED\nPASSED\nresult: ok"

        result = _content(_handle_request(
            _call("ncp_write_memory", {
                "content": noisy,
                "layer": "episodic",
                "src": "tool_result",
            }),
            handlers,
        ))

        assert result["written"] is True
        assert result["filtered"] is True
        assert result["reduction_ratio"] > 0.0
        assert "raw_ref" in result
        chunks = store.get_working_zone(pipeline_id=None)
        filtered_chunk = next(c for c in chunks if c.chunk_id == result["chunk_id"])
        assert "\x1b[" not in filtered_chunk.content
        assert "(×4)" in filtered_chunk.content

    def test_write_memory_stores_raw_ref_chunk(self, tmp_path: Path) -> None:
        store = SQLiteStore(tmp_path / "test.db")
        handlers = make_handlers(store)
        noisy = "unique_marker line\nunique_marker line\nunique_marker line\nresult"

        result = _content(_handle_request(
            _call("ncp_write_memory", {
                "content": noisy,
                "layer": "episodic",
                "src": "tool_result",
            }),
            handlers,
        ))

        raw_ref = result["raw_ref"]
        # Reset fetch session
        _handle_request(
            _call("ncp_get_context", {
                "agent_id": "tester",
                "role": "test",
                "owns": [],
                "must_not": [],
                "task": "test",
                "slot": "test",
                "intent": "test",
            }),
            handlers,
        )
        # Fetch the raw chunk by searching for its content
        fetch_resp = _content(_handle_request(
            _call("ncp_fetch", {"query": "unique_marker", "k": 4}),
            handlers,
        ))
        # The raw chunk should be retrievable and contain unfiltered content
        assert raw_ref in fetch_resp["result"]
        assert "unique_marker line" in fetch_resp["result"]

    def test_write_memory_clean_content_not_filtered(self, tmp_path: Path) -> None:
        store = SQLiteStore(tmp_path / "test.db")
        handlers = make_handlers(store)
        clean = "NPE at PaymentProcessor.java:142."

        result = _content(_handle_request(
            _call("ncp_write_memory", {
                "content": clean,
                "layer": "semantic",
                "src": "agent_inferred",
            }),
            handlers,
        ))

        assert result["written"] is True
        assert "filtered" not in result
        assert "raw_ref" not in result


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

    def test_emit_honors_ttl_seconds(self, tmp_path: Path) -> None:
        store = SQLiteStore(tmp_path / "test.db")
        handlers = make_handlers(store)
        resp = _handle_request(
            _call("ncp_emit_whisper", {
                "from": "builder",
                "target": "executor",
                "type": "nudge",
                "payload": "check this",
                "confidence": 0.9,
                "ttl_seconds": 42,
            }),
            handlers,
        )

        result = _content(resp)
        assert result["emitted"] is True
        drained = store.drain_whispers(agent_id="executor", max_items=1)
        assert drained[0].ttl_seconds == 42

    def test_emit_uses_configured_default_ttl(self, tmp_path: Path) -> None:
        store = SQLiteStore(tmp_path / "test.db")
        config = load_config(cwd=tmp_path)
        config.values["whispers"]["default_ttl_seconds"] = 77
        handlers = make_handlers(store, config=config)
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
        drained = store.drain_whispers(agent_id="executor", max_items=1)
        assert drained[0].ttl_seconds == 77

    def test_emit_wraps_plain_text_share_payload(self, tmp_path: Path) -> None:
        store = SQLiteStore(tmp_path / "test.db")
        handlers = make_handlers(store)
        resp = _handle_request(
            _call("ncp_emit_whisper", {
                "from": "builder",
                "target": "executor",
                "type": "share",
                "payload": "please review the retry slice",
                "confidence": 0.9,
            }),
            handlers,
        )

        result = _content(resp)
        assert result["emitted"] is True
        drained = store.drain_whispers(agent_id="executor", max_items=1)
        payload = json.loads(drained[0].payload)
        assert payload["ask"] == "please review the retry slice"

    def test_emit_preserves_valid_json_share_payload(self, tmp_path: Path) -> None:
        store = SQLiteStore(tmp_path / "test.db")
        handlers = make_handlers(store)
        resp = _handle_request(
            _call("ncp_emit_whisper", {
                "from": "builder",
                "target": "executor",
                "type": "share",
                "payload": '{"ask":"review retry slice","files":["ncp/mcp/server.py"]}',
                "confidence": 0.9,
            }),
            handlers,
        )

        result = _content(resp)
        assert result["emitted"] is True
        drained = store.drain_whispers(agent_id="executor", max_items=1)
        payload = json.loads(drained[0].payload)
        assert payload["ask"] == "review retry slice"
        assert payload["files"] == ["ncp/mcp/server.py"]

    def test_emit_wraps_plain_text_dissent_payload(self, tmp_path: Path) -> None:
        store = SQLiteStore(tmp_path / "test.db")
        handlers = make_handlers(store)
        resp = _handle_request(
            _call("ncp_emit_whisper", {
                "from": "reviewer",
                "target": "builder",
                "type": "dissent",
                "payload": "missing rollback path",
                "confidence": 0.9,
            }),
            handlers,
        )

        result = _content(resp)
        assert result["emitted"] is True
        drained = store.drain_whispers(agent_id="builder", max_items=1)
        payload = json.loads(drained[0].payload)
        assert payload["issue"] == "missing rollback path"

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

    def test_fetch_k_clamped_to_schema_max(self, tmp_path: Path) -> None:
        # WI-005: an over-large k must be clamped to the schema max (4) so a
        # client asking for k=500 cannot pull an unbounded read.
        store = SQLiteStore(tmp_path / "test.db")
        for i in range(8):
            store.write(SubconsciousChunk(
                content=f"shared marker chunk number {i}",
                layer="semantic", src="tool_result", written_by="executor",
            ))
        handlers = make_handlers(store)
        _handle_request(
            _call("ncp_get_context", {
                "agent_id": "builder", "role": "build", "owns": [], "must_not": [],
                "task": "test", "slot": "test", "intent": "test",
            }),
            handlers,
        )
        captured: dict = {}
        original_query = store.query

        def spy(*args, **kwargs):
            captured["k"] = kwargs.get("k")
            return original_query(*args, **kwargs)

        store.query = spy  # type: ignore[method-assign]
        resp = _handle_request(
            _call("ncp_fetch", {"query": "shared marker chunk", "k": 500}),
            handlers,
        )
        result = _content(resp)
        assert captured["k"] == 4
        # The store was never asked for more than 4, so at most 4 are served.
        assert result["result"].startswith("ncp_fetch:results")

    def test_fetch_sessions_dict_is_bounded(self, tmp_path: Path, monkeypatch) -> None:
        # WI-005: rotating session_ids must not grow the in-memory table forever.
        from ncp.mcp import server as server_mod

        monkeypatch.setattr(server_mod, "_FETCH_SESSION_MAX", 5)
        store = SQLiteStore(tmp_path / "test.db")
        handlers = make_handlers(store)
        for i in range(50):
            _handle_request(
                _call("ncp_get_context", {
                    "agent_id": f"agent_{i}", "role": "build", "owns": [], "must_not": [],
                    "task": "t", "slot": "s", "intent": "i",
                }),
                handlers,
            )
        sessions = handlers["ncp_get_context"].fetch_sessions
        assert len(sessions) <= 5

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

            def whisper_pending(self, whisper_id):
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


class TestStreamingGetContext:
    def test_stream_true_returns_stream_response(self, tmp_path: Path) -> None:
        store = SQLiteStore(tmp_path / "test.db")
        handlers = make_handlers(store)
        result = _handle_request(
            _call("ncp_get_context", {
                "agent_id": "streamer",
                "role": "tester",
                "task": "stream_task",
                "slot": "stream_slot",
                "intent": "stream_intent",
                "stream": True,
            }),
            handlers,
        )
        assert isinstance(result, StreamResponse)
        assert len(result.sections) >= 2
        assert result.sections[0][0] == "conscious"
        assert "stream_task" in result.sections[0][1]
        assert result.sections[-1][0] == "budget_header"
        assert result.handler_result["context"]
        assert "[NCP:BUDGET]" in result.handler_result["context"]
        assert result.handler_result["session_id"] == "streamer"
        assert result.request_id == 1

    def test_stream_false_returns_string_unchanged(self, tmp_path: Path) -> None:
        store = SQLiteStore(tmp_path / "test.db")
        handlers = make_handlers(store)
        result = _handle_request(
            _call("ncp_get_context", {
                "agent_id": "builder",
                "role": "build",
                "task": "task",
                "slot": "slot",
                "intent": "intent",
                "stream": False,
            }),
            handlers,
        )
        assert isinstance(result, str)
        content = json.loads(result)["result"]["content"][0]["text"]
        parsed = json.loads(content)
        assert "context" in parsed

    def test_http_post_stream_true_returns_ndjson_lines(self, tmp_path: Path) -> None:
        port = _free_port()
        server = create_http_server(host="127.0.0.1", port=port, cwd=tmp_path)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with httpx.Client(base_url=f"http://127.0.0.1:{port}", timeout=5.0) as client:
                response = client.post(
                    "/mcp",
                    json=_call("ncp_get_context", {
                        "agent_id": "http_streamer",
                        "role": "tester",
                        "task": "http_stream_task",
                        "slot": "slot",
                        "intent": "intent",
                        "stream": True,
                    }),
                )
                assert response.status_code == 200
                assert "application/x-ndjson" in response.headers["content-type"]
                lines = [ln for ln in response.text.splitlines() if ln.strip()]
                assert len(lines) >= 2
                for chunk_line in lines[:-1]:
                    obj = json.loads(chunk_line)
                    assert obj["type"] == "ncp_chunk"
                    assert "section" in obj
                    assert "index" in obj
                    assert "text" in obj
                final = json.loads(lines[-1])
                assert final["jsonrpc"] == "2.0"
                assert final["id"] == 1
                payload = json.loads(final["result"]["content"][0]["text"])
                assert "[NCP:BUDGET]" in payload["context"]
                assert payload["session_id"] == "http_streamer"
        finally:
            server._shutdown_event.set()
            server.shutdown()
            thread.join(timeout=5)

    def test_http_post_sse_accept_returns_event_stream_message(self, tmp_path: Path) -> None:
        port = _free_port()
        server = create_http_server(host="127.0.0.1", port=port, cwd=tmp_path)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with httpx.Client(base_url=f"http://127.0.0.1:{port}", timeout=5.0) as client:
                response = client.post(
                    "/mcp",
                    headers={"Accept": "text/event-stream"},
                    json=_call("ncp_get_context", {
                        "agent_id": "sse_caller",
                        "role": "tester",
                        "task": "sse_task",
                        "slot": "slot",
                        "intent": "intent",
                    }),
                )
                assert response.status_code == 200
                assert "text/event-stream" in response.headers["content-type"]
                assert "event: message" in response.text
                data_line = next(
                    ln for ln in response.text.splitlines() if ln.startswith("data: ")
                )
                rpc = json.loads(data_line[len("data: "):])
                assert rpc["jsonrpc"] == "2.0"
                assert rpc["id"] == 1
                payload = json.loads(rpc["result"]["content"][0]["text"])
                assert payload["session_id"] == "sse_caller"
        finally:
            server._shutdown_event.set()
            server.shutdown()
            thread.join(timeout=5)

    def test_http_post_json_accept_still_returns_json(self, tmp_path: Path) -> None:
        port = _free_port()
        server = create_http_server(host="127.0.0.1", port=port, cwd=tmp_path)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with httpx.Client(base_url=f"http://127.0.0.1:{port}", timeout=5.0) as client:
                response = client.post(
                    "/mcp",
                    headers={"Accept": "application/json, text/event-stream"},
                    json=_req("tools/list"),
                )
                assert response.status_code == 200
                assert "application/json" in response.headers["content-type"]
                assert response.json()["result"]["tools"]
        finally:
            server._shutdown_event.set()
            server.shutdown()
            thread.join(timeout=5)

    def test_http_post_sse_accept_streams_event_stream(self, tmp_path: Path) -> None:
        port = _free_port()
        server = create_http_server(host="127.0.0.1", port=port, cwd=tmp_path)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with httpx.Client(base_url=f"http://127.0.0.1:{port}", timeout=5.0) as client:
                response = client.post(
                    "/mcp",
                    headers={"Accept": "text/event-stream"},
                    json=_call("ncp_get_context", {
                        "agent_id": "sse_streamer",
                        "role": "tester",
                        "task": "sse_stream_task",
                        "slot": "slot",
                        "intent": "intent",
                        "stream": True,
                    }),
                )
                assert response.status_code == 200
                assert "text/event-stream" in response.headers["content-type"]
                assert "event: ncp_chunk" in response.text
                assert "event: message" in response.text
                message_data = next(
                    ln[len("data: "):]
                    for ln in response.text.splitlines()
                    if ln.startswith("data: ") and '"jsonrpc"' in ln
                )
                rpc = json.loads(message_data)
                payload = json.loads(rpc["result"]["content"][0]["text"])
                assert payload["session_id"] == "sse_streamer"
        finally:
            server._shutdown_event.set()
            server.shutdown()
            thread.join(timeout=5)

    def test_stdio_stream_true_emits_notifications_then_final_response(self, tmp_path: Path) -> None:
        project = tmp_path / "repo"
        (project / ".git").mkdir(parents=True)

        req = _call("ncp_get_context", {
            "agent_id": "stdio_streamer",
            "role": "tester",
            "task": "stdio_task",
            "slot": "slot",
            "intent": "intent",
            "stream": True,
        }, req_id=42)

        input_stream = io.BytesIO(_frame(req))
        output_stream = io.BytesIO()

        serve_streams(input_stream, output_stream, cwd=project)

        output_stream.seek(0)
        messages = []
        while True:
            try:
                msg = _read_message(output_stream)
            except (ValueError, json.JSONDecodeError):
                break
            if msg is None:
                break
            messages.append(msg)

        assert len(messages) >= 2
        for notif in messages[:-1]:
            assert "id" not in notif
            assert notif["method"] == "ncp/stream_chunk"
            params = notif["params"]
            assert params["request_id"] == 42
            assert "section" in params
            assert "index" in params
            assert "text" in params

        final = messages[-1]
        assert final["id"] == 42
        payload = json.loads(final["result"]["content"][0]["text"])
        assert "[NCP:BUDGET]" in payload["context"]
        assert payload["session_id"] == "stdio_streamer"

    def test_stream_true_does_a_single_assembly_pass(self, tmp_path: Path) -> None:
        class _CountingStore(SQLiteStore):
            def __init__(self, path: Path) -> None:
                super().__init__(path)
                self.query_calls = 0
                self.peek_whispers_calls = 0

            def query(self, *args, **kwargs):
                self.query_calls += 1
                return super().query(*args, **kwargs)

            def peek_whispers(self, *args, **kwargs):
                self.peek_whispers_calls += 1
                return super().peek_whispers(*args, **kwargs)

        store = _CountingStore(tmp_path / "counting.db")
        store.write(
            SubconsciousChunk(
                chunk_id="sub_count",
                layer="semantic",
                content="counting store chunk",
                src="tool_result",
                pipeline_id="pipe_count",
            )
        )
        store.emit_whisper(
            Whisper(
                from_agent="builder",
                target="streamer",
                whisper_type="nudge",
                payload="check the counting chunk",
                confidence=0.9,
                pipeline_id="pipe_count",
            )
        )
        handlers = make_handlers(store)

        args = {
            "agent_id": "streamer",
            "role": "tester",
            "task": "stream_task",
            "slot": "stream_slot",
            "intent": "stream_intent",
            "pipeline_id": "pipe_count",
            "stream": True,
        }

        result = _handle_request(_call("ncp_get_context", args), handlers)

        assert isinstance(result, StreamResponse)
        assert store.query_calls == 1
        assert store.peek_whispers_calls == 1

    def test_stream_sections_match_non_streaming_context(self, tmp_path: Path) -> None:
        store = SQLiteStore(tmp_path / "match.db")
        store.write(
            SubconsciousChunk(
                chunk_id="sub_match",
                layer="semantic",
                content="matching store chunk",
                src="tool_result",
                pipeline_id="pipe_match",
            )
        )
        store.emit_whisper(
            Whisper(
                from_agent="builder",
                target="streamer",
                whisper_type="nudge",
                payload="check the matching chunk",
                confidence=0.9,
                pipeline_id="pipe_match",
            )
        )

        args = {
            "agent_id": "streamer",
            "role": "tester",
            "task": "stream_task",
            "slot": "stream_slot",
            "intent": "stream_intent",
            "pipeline_id": "pipe_match",
        }

        non_stream_handlers = make_handlers(SQLiteStore(tmp_path / "match.db"))
        non_stream_resp = _handle_request(_call("ncp_get_context", {**args, "stream": False}), non_stream_handlers)
        non_stream_context = _content(non_stream_resp)["context"]

        stream_handlers = make_handlers(SQLiteStore(tmp_path / "match.db"))
        stream_result = _handle_request(_call("ncp_get_context", {**args, "stream": True}), stream_handlers)

        assert isinstance(stream_result, StreamResponse)
        from ncp.assembler import Assembler

        assembled_from_sections = Assembler(store=store).apply_post_middleware(
            "\n\n".join(text for _, text in stream_result.sections)
        )
        assert assembled_from_sections == non_stream_context
        assert stream_result.handler_result["context"] == non_stream_context


class TestCostGovernor:
    """CAP-E2: per-pipeline budget governance."""

    def _post_turn(self, handlers: dict, *, pipeline_id: str, cost_usd: float, turn_id: str) -> object:
        return _content(_handle_request(
            _call("ncp_post_turn", {
                "agent_id": "builder",
                "role": "build",
                "task": "test",
                "slot": "test",
                "intent": "test",
                "pipeline_id": pipeline_id,
                "turn_id": turn_id,
                "result_summary": "summary",
                "result_full": "full result",
                "model": "gpt-4o-mini",
                "cost_usd": cost_usd,
            }),
            handlers,
        ))

    def _get_context(self, handlers: dict, *, pipeline_id: str) -> object:
        return _content(_handle_request(
            _call("ncp_get_context", {
                "agent_id": "builder",
                "role": "build",
                "owns": [],
                "must_not": [],
                "task": "test",
                "slot": "test",
                "intent": "test",
                "pipeline_id": pipeline_id,
            }),
            handlers,
        ))

    def test_no_budget_configured_is_a_no_op(self, tmp_path: Path) -> None:
        store = SQLiteStore(tmp_path / "gov.db")
        handlers = make_handlers(store, config=load_config(cwd=tmp_path))

        posted = self._post_turn(handlers, pipeline_id="pipe_gov", cost_usd=100.0, turn_id="t1")
        context = self._get_context(handlers, pipeline_id="pipe_gov")

        assert "budget" not in posted
        assert "budget" not in context

    def test_off_enforcement_is_a_no_op_even_with_budget_set(self, tmp_path: Path) -> None:
        store = SQLiteStore(tmp_path / "gov_off.db")
        config = load_config(cwd=tmp_path)
        config.values["budget"]["pipeline_budget_usd"] = 1.0
        config.values["budget"]["budget_enforcement"] = "off"
        handlers = make_handlers(store, config=config)

        posted = self._post_turn(handlers, pipeline_id="pipe_gov", cost_usd=100.0, turn_id="t1")
        context = self._get_context(handlers, pipeline_id="pipe_gov")

        assert "budget" not in posted
        assert "budget" not in context

    def test_spend_accumulates_across_turns(self, tmp_path: Path) -> None:
        store = SQLiteStore(tmp_path / "gov_accum.db")
        config = load_config(cwd=tmp_path)
        config.values["budget"]["pipeline_budget_usd"] = 10.0
        config.values["budget"]["budget_warn_fraction"] = 0.8
        handlers = make_handlers(store, config=config)

        first = self._post_turn(handlers, pipeline_id="pipe_accum", cost_usd=3.0, turn_id="t1")
        assert first["budget"] == {
            "spent_usd": 3.0,
            "budget_usd": 10.0,
            "fraction_used": 0.3,
            "status": "ok",
        }

        second = self._post_turn(handlers, pipeline_id="pipe_accum", cost_usd=3.0, turn_id="t2")
        assert second["budget"]["spent_usd"] == pytest.approx(6.0)
        assert second["budget"]["fraction_used"] == pytest.approx(0.6)
        assert second["budget"]["status"] == "ok"

        context = self._get_context(handlers, pipeline_id="pipe_accum")
        assert context["budget"]["spent_usd"] == pytest.approx(6.0)

        # A separate pipeline's spend must not bleed into this one's total.
        self._post_turn(handlers, pipeline_id="pipe_other", cost_usd=50.0, turn_id="other1")
        unaffected = self._get_context(handlers, pipeline_id="pipe_accum")
        assert unaffected["budget"]["spent_usd"] == pytest.approx(6.0)

    def test_warn_threshold_crossing(self, tmp_path: Path) -> None:
        store = SQLiteStore(tmp_path / "gov_warn.db")
        config = load_config(cwd=tmp_path)
        config.values["budget"]["pipeline_budget_usd"] = 10.0
        config.values["budget"]["budget_warn_fraction"] = 0.5
        handlers = make_handlers(store, config=config)

        below = self._post_turn(handlers, pipeline_id="pipe_warn", cost_usd=4.0, turn_id="t1")
        assert below["budget"]["status"] == "ok"

        crossing = self._post_turn(handlers, pipeline_id="pipe_warn", cost_usd=2.0, turn_id="t2")
        assert crossing["budget"]["fraction_used"] == pytest.approx(0.6)
        assert crossing["budget"]["status"] == "warning"

    def test_block_mode_refuses_get_context_once_exceeded(self, tmp_path: Path) -> None:
        store = SQLiteStore(tmp_path / "gov_block.db")
        config = load_config(cwd=tmp_path)
        config.values["budget"]["pipeline_budget_usd"] = 1.0
        config.values["budget"]["budget_enforcement"] = "block"
        handlers = make_handlers(store, config=config)

        posted = self._post_turn(handlers, pipeline_id="pipe_block", cost_usd=1.5, turn_id="t1")
        assert posted["budget"]["status"] == "exceeded"

        refusal = self._get_context(handlers, pipeline_id="pipe_block")

        assert refusal["budget_exceeded"] is True
        assert refusal["context"] == ""
        assert refusal["budget"]["status"] == "exceeded"
        assert refusal["session_id"] == "pipe_block:builder"
        assert "telemetry" not in refusal
        assert "tier_hint" not in refusal

    def test_warn_mode_never_blocks_get_context(self, tmp_path: Path) -> None:
        store = SQLiteStore(tmp_path / "gov_warn_noblock.db")
        config = load_config(cwd=tmp_path)
        config.values["budget"]["pipeline_budget_usd"] = 1.0
        config.values["budget"]["budget_enforcement"] = "warn"
        handlers = make_handlers(store, config=config)

        self._post_turn(handlers, pipeline_id="pipe_wnb", cost_usd=5.0, turn_id="t1")
        context = self._get_context(handlers, pipeline_id="pipe_wnb")

        assert "budget_exceeded" not in context
        assert context["budget"]["status"] == "exceeded"
        assert "context" in context and isinstance(context["context"], str) and context["context"] != ""


class TestTierHintSignal:
    """CAP-E3: advisory model-tiering signal on ncp_get_context."""

    def test_tier_hint_present_by_default_with_factors(self, tmp_path: Path) -> None:
        store = SQLiteStore(tmp_path / "tier.db")
        handlers = make_handlers(store)

        result = _content(_handle_request(
            _call("ncp_get_context", {
                "agent_id": "builder",
                "role": "build",
                "owns": [],
                "must_not": [],
                "task": "test",
                "slot": "test",
                "intent": "test",
                "pipeline_id": "pipe_tier",
            }),
            handlers,
        ))

        assert result["tier_hint"] in {"light", "standard", "deep"}
        assert 0.0 <= result["complexity_signal"] <= 1.0
        factors = result["factors"]
        for key in (
            "query_length_chars", "query_length_norm", "chunk_count",
            "distinct_authors", "diversity_ratio", "drift_score",
            "pressure", "pressure_norm", "cold_start",
        ):
            assert key in factors

    def test_tier_hints_disabled_omits_fields(self, tmp_path: Path) -> None:
        store = SQLiteStore(tmp_path / "tier_off.db")
        config = load_config(cwd=tmp_path)
        config.values["tiering"]["tier_hints_enabled"] = False
        handlers = make_handlers(store, config=config)

        result = _content(_handle_request(
            _call("ncp_get_context", {
                "agent_id": "builder",
                "role": "build",
                "owns": [],
                "must_not": [],
                "task": "test",
                "slot": "test",
                "intent": "test",
                "pipeline_id": "pipe_tier_off",
            }),
            handlers,
        ))

        assert "tier_hint" not in result
        assert "complexity_signal" not in result
        assert "factors" not in result

    def test_tier_hint_is_deterministic_for_identical_state(self, tmp_path: Path) -> None:
        store = SQLiteStore(tmp_path / "tier_det.db")
        handlers = make_handlers(store)
        args = {
            "agent_id": "builder",
            "role": "build",
            "owns": [],
            "must_not": [],
            "task": "test",
            "slot": "test",
            "intent": "test",
            "pipeline_id": "pipe_tier_det",
        }

        first = _content(_handle_request(_call("ncp_get_context", args), handlers))
        second = _content(_handle_request(_call("ncp_get_context", args), handlers))

        assert first["tier_hint"] == second["tier_hint"]
        assert first["complexity_signal"] == pytest.approx(second["complexity_signal"])
        assert first["factors"] == second["factors"]

    def test_cold_start_turn_raises_complexity_signal(self, tmp_path: Path) -> None:
        # Empty store -> the assembler's cold-start bootstrap fires, which
        # the tier signal should surface via factors["cold_start"].
        store = SQLiteStore(tmp_path / "tier_cold.db")
        handlers = make_handlers(store)

        result = _content(_handle_request(
            _call("ncp_get_context", {
                "agent_id": "builder",
                "role": "build",
                "owns": [],
                "must_not": [],
                "task": "test",
                "slot": "test",
                "intent": "test",
                "pipeline_id": "pipe_tier_cold",
            }),
            handlers,
        ))

        assert result["factors"]["cold_start"] is True
        assert result["factors"]["chunk_count"] == 1


class TestAdaptiveBudget:
    """CAP-C6: adaptive per-turn context token budget."""

    def _args(self, **overrides: object) -> dict:
        base = {
            "agent_id": "builder",
            "role": "build",
            "owns": [],
            "must_not": [],
            "task": "test",
            "slot": "test",
            "intent": "test",
            "pipeline_id": "pipe_adaptive",
        }
        base.update(overrides)
        return base

    def test_disabled_by_default_omits_budget_tokens_block(self, tmp_path: Path) -> None:
        store = SQLiteStore(tmp_path / "adaptive_off.db")
        handlers = make_handlers(store)

        result = _content(_handle_request(_call("ncp_get_context", self._args()), handlers))

        assert "budget_tokens" not in result

    def test_disabled_flag_is_byte_identical_to_no_config(self, tmp_path: Path) -> None:
        store_a = SQLiteStore(tmp_path / "adaptive_legacy_a.db")
        store_b = SQLiteStore(tmp_path / "adaptive_legacy_b.db")
        handlers_no_config = make_handlers(store_a)

        config = load_config(cwd=tmp_path)
        config.values["budget"]["adaptive_budget_enabled"] = False
        handlers_explicit_off = make_handlers(store_b, config=config)

        args = self._args(pipeline_id="pipe_legacy")
        result_no_config = _content(_handle_request(_call("ncp_get_context", args), handlers_no_config))
        result_explicit_off = _content(_handle_request(_call("ncp_get_context", args), handlers_explicit_off))

        assert result_no_config["context"] == result_explicit_off["context"]
        assert "budget_tokens" not in result_no_config
        assert "budget_tokens" not in result_explicit_off

    def test_enabled_without_caller_max_tokens_exposes_budget_tokens_block(self, tmp_path: Path) -> None:
        store = SQLiteStore(tmp_path / "adaptive_on.db")
        config = load_config(cwd=tmp_path)
        config.values["budget"]["adaptive_budget_enabled"] = True
        handlers = make_handlers(store, config=config)

        result = _content(_handle_request(_call("ncp_get_context", self._args()), handlers))

        assert "budget_tokens" in result
        block = result["budget_tokens"]
        assert set(block) == {"requested", "adjusted", "reason_factors"}
        assert block["requested"] == config.context_token_budget
        assert config.adaptive_budget_floor_tokens <= block["adjusted"] <= config.adaptive_budget_ceiling_tokens
        assert estimate_tokens(result["context"]) <= block["adjusted"]

    def test_explicit_caller_max_tokens_is_never_overridden(self, tmp_path: Path) -> None:
        store = SQLiteStore(tmp_path / "adaptive_explicit.db")
        for index in range(4):
            store.write(SubconsciousChunk(
                chunk_id=f"adaptive_large_{index}",
                content=" ".join(["adaptive explicit context"] + [f"tokenish_{index}_{j}" for j in range(120)]),
                layer="semantic",
                src="tool_result",
                pipeline_id="pipe_adaptive_explicit",
            ))
        config = load_config(cwd=tmp_path)
        config.values["budget"]["adaptive_budget_enabled"] = True
        config.values["budget"]["adaptive_budget_floor_tokens"] = 50
        config.values["budget"]["adaptive_budget_ceiling_tokens"] = 100
        handlers = make_handlers(store, config=config)

        result = _content(_handle_request(
            _call("ncp_get_context", self._args(
                pipeline_id="pipe_adaptive_explicit",
                max_tokens=200,
                drift_score=1.0,
                ctx_used=0.95,
            )),
            handlers,
        ))

        assert result["budget_tokens"] == {
            "requested": 200,
            "adjusted": 200,
            "reason_factors": {"explicit_override": True},
        }
        assert estimate_tokens(result["context"]) <= 200

    def test_adjusted_always_within_configured_floor_and_ceiling(self, tmp_path: Path) -> None:
        store = SQLiteStore(tmp_path / "adaptive_bounds.db")
        config = load_config(cwd=tmp_path)
        config.values["budget"]["adaptive_budget_enabled"] = True
        config.values["budget"]["adaptive_budget_floor_tokens"] = 120
        config.values["budget"]["adaptive_budget_ceiling_tokens"] = 180
        handlers = make_handlers(store, config=config)

        easy = _content(_handle_request(
            _call("ncp_get_context", self._args(pipeline_id="pipe_bounds_easy", task="t", slot="s")),
            handlers,
        ))
        hard = _content(_handle_request(
            _call("ncp_get_context", self._args(
                pipeline_id="pipe_bounds_hard",
                task="x" * 200,
                slot="y" * 100,
                drift_score=1.0,
                ctx_used=0.95,
            )),
            handlers,
        ))

        assert 120 <= easy["budget_tokens"]["adjusted"] <= 180
        assert 120 <= hard["budget_tokens"]["adjusted"] <= 180
        assert hard["budget_tokens"]["adjusted"] >= easy["budget_tokens"]["adjusted"]

    def test_budget_tokens_is_deterministic_for_identical_state(self, tmp_path: Path) -> None:
        store = SQLiteStore(tmp_path / "adaptive_det.db")
        config = load_config(cwd=tmp_path)
        config.values["budget"]["adaptive_budget_enabled"] = True
        handlers = make_handlers(store, config=config)
        args = self._args(pipeline_id="pipe_adaptive_det", drift_score=0.4, ctx_used=0.6)

        first = _content(_handle_request(_call("ncp_get_context", args), handlers))
        second = _content(_handle_request(_call("ncp_get_context", args), handlers))

        assert first["budget_tokens"] == second["budget_tokens"]


class TestComputedDrift:
    """CAP-T5: computed drift overrides the self-reported drift_score."""

    def _args(self, **overrides: object) -> dict:
        base = {
            "agent_id": "builder",
            "role": "build",
            "owns": [],
            "must_not": [],
            "task": "test",
            "slot": "test",
            "intent": "test",
            "pipeline_id": "pipe_drift",
        }
        base.update(overrides)
        return base

    def _post_turn(self, handlers, *, pipeline_id: str, turn_id: str, task: str, slot: str, result_summary: str) -> None:
        posted = _content(_handle_request(
            _call("ncp_post_turn", {
                "agent_id": "builder",
                "role": "build",
                "task": task,
                "slot": slot,
                "intent": "test",
                "pipeline_id": pipeline_id,
                "turn_id": turn_id,
                "result_summary": result_summary,
                "result_full": result_summary,
            }),
            handlers,
        ))
        assert posted["posted"] is True

    def test_disabled_by_default_omits_drift_block(self, tmp_path: Path) -> None:
        store = SQLiteStore(tmp_path / "drift_off.db")
        handlers = make_handlers(store)

        result = _content(_handle_request(_call("ncp_get_context", self._args()), handlers))

        assert "drift" not in result

    def test_disabled_flag_is_byte_identical_to_no_config(self, tmp_path: Path) -> None:
        store_a = SQLiteStore(tmp_path / "drift_legacy_a.db")
        store_b = SQLiteStore(tmp_path / "drift_legacy_b.db")
        handlers_no_config = make_handlers(store_a)

        config = load_config(cwd=tmp_path)
        config.values["drift"]["drift_computed_enabled"] = False
        handlers_explicit_off = make_handlers(store_b, config=config)

        args = self._args(pipeline_id="pipe_drift_legacy", drift_score=0.9)
        result_no_config = _content(_handle_request(_call("ncp_get_context", args), handlers_no_config))
        result_explicit_off = _content(_handle_request(_call("ncp_get_context", args), handlers_explicit_off))

        assert result_no_config["context"] == result_explicit_off["context"]
        assert "drift" not in result_no_config
        assert "drift" not in result_explicit_off

    def test_zero_prior_turns_computes_zero_drift_without_crashing(self, tmp_path: Path) -> None:
        store = SQLiteStore(tmp_path / "drift_zero.db")
        config = load_config(cwd=tmp_path)
        config.values["drift"]["drift_computed_enabled"] = True
        handlers = make_handlers(store, config=config)

        result = _content(_handle_request(
            _call("ncp_get_context", self._args(pipeline_id="pipe_drift_new")),
            handlers,
        ))

        assert result["drift"]["score"] == 0.0
        assert result["drift"]["method"] == "lexical"
        assert result["drift"]["window_turns"] == 5
        assert result["drift"]["self_reported"] is None

    def test_self_reported_is_null_when_never_supplied(self, tmp_path: Path) -> None:
        store = SQLiteStore(tmp_path / "drift_null.db")
        config = load_config(cwd=tmp_path)
        config.values["drift"]["drift_computed_enabled"] = True
        handlers = make_handlers(store, config=config)

        result = _content(_handle_request(
            _call("ncp_get_context", self._args(pipeline_id="pipe_drift_no_claim")),
            handlers,
        ))

        assert result["drift"]["self_reported"] is None

    def test_self_reported_is_exposed_alongside_computed_score(self, tmp_path: Path) -> None:
        store = SQLiteStore(tmp_path / "drift_self_reported.db")
        config = load_config(cwd=tmp_path)
        config.values["drift"]["drift_computed_enabled"] = True
        handlers = make_handlers(store, config=config)

        result = _content(_handle_request(
            _call("ncp_get_context", self._args(pipeline_id="pipe_drift_claim", drift_score=0.95)),
            handlers,
        ))

        # The client claimed near-total drift; the computed score for a
        # fresh pipeline with no history is 0.0 -- the divergence is visible.
        assert result["drift"]["self_reported"] == 0.95
        assert result["drift"]["score"] == 0.0

    def test_computed_drift_overrides_tiering_and_adaptive_budget_inputs(self, tmp_path: Path) -> None:
        store = SQLiteStore(tmp_path / "drift_override.db")
        config = load_config(cwd=tmp_path)
        config.values["drift"]["drift_computed_enabled"] = True
        config.values["budget"]["adaptive_budget_enabled"] = True
        handlers = make_handlers(store, config=config)

        self._post_turn(
            handlers,
            pipeline_id="pipe_drift_wired",
            turn_id="turn_1",
            task="billing",
            slot="invoice",
            result_summary="billing invoice reconciliation",
        )

        # Client asserts zero drift, but the task has totally changed topic
        # from the turn history -- computed drift should override it upward
        # and that higher value should be what tiering/adaptive-budget see.
        result = _content(_handle_request(
            _call("ncp_get_context", self._args(
                pipeline_id="pipe_drift_wired",
                task="astronomy",
                slot="telescope",
                drift_score=0.0,
            )),
            handlers,
        ))

        assert result["drift"]["self_reported"] == 0.0
        assert result["drift"]["score"] == pytest.approx(1.0)
        assert result["drift"]["method"] == "lexical"
        # CAP-E3 tiering read the overridden (high) drift, not the claimed 0.0.
        assert result["factors"]["drift_score"] == pytest.approx(1.0)

    def test_window_turns_config_is_respected_and_reported(self, tmp_path: Path) -> None:
        store = SQLiteStore(tmp_path / "drift_window.db")
        config = load_config(cwd=tmp_path)
        config.values["drift"]["drift_computed_enabled"] = True
        config.values["drift"]["drift_window_turns"] = 2
        handlers = make_handlers(store, config=config)

        for index in range(4):
            self._post_turn(
                handlers,
                pipeline_id="pipe_drift_window",
                turn_id=f"turn_{index}",
                task=f"topic_{index}",
                slot=f"topic_{index}",
                result_summary=f"topic_{index}",
            )

        result = _content(_handle_request(
            _call("ncp_get_context", self._args(pipeline_id="pipe_drift_window", task="topic_0", slot="topic_0")),
            handlers,
        ))

        # window_turns=2 means only turn_2/turn_3 (topic_2/topic_3) are in
        # scope; topic_0 no longer overlaps with the window, so drift is high.
        assert result["drift"]["window_turns"] == 2
        assert result["drift"]["score"] == pytest.approx(1.0)

    def test_computed_drift_is_deterministic(self, tmp_path: Path) -> None:
        store = SQLiteStore(tmp_path / "drift_det.db")
        config = load_config(cwd=tmp_path)
        config.values["drift"]["drift_computed_enabled"] = True
        handlers = make_handlers(store, config=config)
        args = self._args(pipeline_id="pipe_drift_det")

        first = _content(_handle_request(_call("ncp_get_context", args), handlers))
        second = _content(_handle_request(_call("ncp_get_context", args), handlers))

        assert first["drift"] == second["drift"]

    def test_drift_use_embeddings_falls_back_to_lexical_without_fastembed(self, tmp_path: Path) -> None:
        # fastembed is not installed in this environment; enabling the blend
        # must never raise -- it silently degrades to the lexical method.
        store = SQLiteStore(tmp_path / "drift_embed_fallback.db")
        config = load_config(cwd=tmp_path)
        config.values["drift"]["drift_computed_enabled"] = True
        config.values["drift"]["drift_use_embeddings"] = True
        handlers = make_handlers(store, config=config)

        result = _content(_handle_request(_call("ncp_get_context", self._args()), handlers))

        assert result["drift"]["method"] == "lexical"
