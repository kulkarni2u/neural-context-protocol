"""MCP transports for stdio and HTTP/SSE JSON-RPC 2.0 endpoints."""

from __future__ import annotations

from dataclasses import dataclass
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import sys
import threading
import time
import traceback
from collections.abc import Callable
from pathlib import Path
from typing import BinaryIO

from ncp.assembler import Assembler
from ncp.config import load_config
from ncp.stores.base import BaseStore
from ncp.stores.factory import create_store
from ncp.types import BudgetContext, ConsciousBlock, SubconsciousChunk, Whisper


def _err(msg: str) -> None:
    print(msg, file=sys.stderr)


def _ok(id: int | str | None, result: object) -> str:
    return json.dumps({"jsonrpc": "2.0", "id": id, "result": result})


def _err_response(id: int | str | None, code: int, message: str) -> str:
    return json.dumps({"jsonrpc": "2.0", "id": id, "error": {"code": code, "message": message}})


def _json_bytes(payload: object) -> bytes:
    return json.dumps(payload).encode("utf-8")


ToolHandler = Callable[..., object]
DEFAULT_FETCH_SESSION_ID = "__default__"

MCP_TOOLS: list[dict[str, object]] = [
    {
        "name": "ncp_get_context",
        "description": "Assemble the NCP context block for the current agent turn. Call at the start of each turn before any provider call.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "agent_id": {"type": "string", "description": "Agent identifier (no spaces)"},
                "role": {"type": "string", "description": "Role label (no spaces)"},
                "owns": {"type": "array", "items": {"type": "string"}, "description": "Capabilities this agent owns"},
                "must_not": {"type": "array", "items": {"type": "string"}, "description": "Hard capability boundaries"},
                "task": {"type": "string", "description": "Current objective (no spaces)"},
                "slot": {"type": "string", "description": "What is being resolved (no spaces)"},
                "intent": {"type": "string", "description": "Why this action (no spaces)"},
                "pipeline_id": {"type": "string", "description": "Pipeline identifier"},
                "session_id": {"type": "string", "description": "Optional fetch-session token for this turn"},
                "stream": {"type": "boolean", "description": "If true, returns sections progressively as NDJSON (HTTP) or JSON-RPC notifications (stdio). Default false."},
            },
            "required": ["agent_id", "role", "task", "slot", "intent"],
        },
    },
    {
        "name": "ncp_write_memory",
        "description": "Write a durable subconscious chunk to the store. Use at the end of each turn to persist results.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "content": {"type": "string", "description": "Content (max 2000 chars)"},
                "layer": {"type": "string", "enum": ["episodic", "procedural", "semantic", "social", "reasoning_trace"]},
                "src": {"type": "string", "enum": ["user_verified", "tool_result", "agent_inferred", "synthesis", "subcon_retrieved"]},
                "written_by": {"type": "string", "description": "Agent writing this chunk"},
                "chunk_id": {"type": "string", "description": "Optional chunk ID (auto-generated if omitted)"},
                "pipeline_id": {"type": "string"},
            },
            "required": ["content", "layer", "src"],
        },
    },
    {
        "name": "ncp_emit_whisper",
        "description": "Emit a whisper signal to another agent in the same pipeline.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "from": {"type": "string", "description": "Sending agent ID"},
                "target": {"type": "string", "description": "Receiving agent ID or '*' for pipeline broadcast"},
                "type": {"type": "string", "enum": ["nudge", "alert", "share", "request", "dissent", "world_check", "consolidation_ready"]},
                "payload": {"type": "string", "description": "Whisper message (max 600 chars)"},
                "confidence": {"type": "number", "description": "Confidence 0.0-1.0"},
                "pipeline_id": {"type": "string"},
            },
            "required": ["from", "target", "type", "payload", "confidence"],
        },
    },
    {
        "name": "ncp_fetch",
        "description": "Retrieve additional chunks from the store mid-turn. Max 3 calls per turn.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Specific description of needed context"},
                "layer": {"type": "string", "enum": ["episodic", "procedural", "semantic", "social", "any"], "description": "Optional layer filter"},
                "k": {"type": "integer", "description": "Number of chunks (default 2, max 4)"},
                "agent_id": {"type": "string", "description": "Agent identifier to scope fetch budget"},
                "pipeline_id": {"type": "string", "description": "Pipeline identifier to scope fetch budget"},
                "session_id": {"type": "string", "description": "Optional fetch-session token returned by ncp_get_context"},
            },
            "required": ["query"],
        },
    },
]


def _encode_fetch_results(chunks: list[SubconsciousChunk]) -> str:
    lines = [f"ncp_fetch:results k:{len(chunks)}"]
    for chunk in chunks:
        lines.append(f"chunk:{chunk.chunk_id} layer:{chunk.layer} score:{chunk.relevance:.2f}")
        lines.append(f"  {chunk.content}")
    return "\n".join(lines)


@dataclass
class FetchSession:
    fetch_count: int = 0
    pipeline_id: str | None = None


@dataclass
class StreamResponse:
    sections: list[tuple[str, str]]
    handler_result: dict[str, object]
    request_id: int | str | None = None


def _session_id_from_args(args: dict[str, object]) -> str:
    explicit = args.get("session_id")
    if explicit:
        return str(explicit)

    agent_id = args.get("agent_id")
    pipeline_id = args.get("pipeline_id")
    if agent_id and pipeline_id:
        return f"{pipeline_id}:{agent_id}"
    if agent_id:
        return str(agent_id)
    if pipeline_id:
        return str(pipeline_id)
    return DEFAULT_FETCH_SESSION_ID


def make_handlers(store: BaseStore) -> dict[str, ToolHandler]:
    sessions: dict[str, FetchSession] = {}
    last_session_id = DEFAULT_FETCH_SESSION_ID
    coordination = getattr(store, "coordination", None)

    def _handle_get_context(args: dict[str, object]) -> object:
        nonlocal last_session_id
        session_id = _session_id_from_args(args)
        pipeline_id = args.get("pipeline_id")
        normalized_pipeline_id = None if pipeline_id is None else str(pipeline_id)
        if coordination is not None and hasattr(coordination, "reset_fetch_session"):
            coordination.reset_fetch_session(session_id, pipeline_id=normalized_pipeline_id)
        else:
            sessions[session_id] = FetchSession(fetch_count=0, pipeline_id=normalized_pipeline_id)
        last_session_id = session_id
        conscious = ConsciousBlock(
            agent_id=str(args["agent_id"]),
            role=str(args["role"]),
            owns=list(args.get("owns", []) or []),
            must_not=list(args.get("must_not", []) or []),
            task=str(args["task"]),
            slot=str(args["slot"]),
            intent=str(args["intent"]),
            pipeline_id=pipeline_id,
        )
        assembler = Assembler(store=store)
        stream = bool(args.get("stream", False))
        if stream:
            sections = list(assembler.assemble_incremental(
                conscious=conscious,
                budget=BudgetContext(),
                query_text=conscious.task + " " + conscious.slot,
            ))
            assembled = assembler.apply_post_middleware("\n\n".join(t for _, t in sections))
            return StreamResponse(
                sections=sections,
                handler_result={"context": assembled, "session_id": session_id},
            )
        result = assembler.assemble(
            conscious=conscious,
            budget=BudgetContext(),
            query_text=conscious.task + " " + conscious.slot,
        )
        return {"context": result.context, "session_id": session_id}

    def _handle_write_memory(args: dict[str, object]) -> object:
        kwargs: dict = {
            "content": str(args["content"]),
            "layer": str(args["layer"]),
            "src": str(args["src"]),
            "written_by": str(args.get("written_by", "agent")),
            "pipeline_id": args.get("pipeline_id"),
        }
        if (chunk_id := args.get("chunk_id")):
            kwargs["chunk_id"] = str(chunk_id)
        chunk = SubconsciousChunk(**kwargs)
        ok = store.write(chunk)
        return {"written": ok, "chunk_id": chunk.chunk_id}

    def _handle_emit_whisper(args: dict[str, object]) -> object:
        whisper = Whisper(
            from_agent=str(args["from"]),
            target=str(args["target"]),
            whisper_type=str(args["type"]),
            payload=str(args["payload"]),
            confidence=float(args["confidence"]),
            pipeline_id=args.get("pipeline_id"),
        )
        store.emit_whisper(whisper)
        return {"emitted": True}

    def _handle_fetch(args: dict[str, object]) -> object:
        session_id = _session_id_from_args(args)
        if session_id == DEFAULT_FETCH_SESSION_ID:
            session_id = last_session_id
        query_str = str(args["query"])
        layer = args.get("layer")
        if layer == "any":
            layer = None
        if layer is not None and layer not in ("episodic", "procedural", "semantic", "social"):
            return {"result": "ncp_fetch:invalid_layer valid:[episodic,procedural,semantic,social,any]"}
        pipeline_id = args.get("pipeline_id")
        effective_pipeline_id: str | None
        if coordination is not None and hasattr(coordination, "claim_fetch_slot"):
            _, effective_pipeline_id = coordination.claim_fetch_slot(
                session_id,
                pipeline_id=None if pipeline_id is None else str(pipeline_id),
                max_fetches=3,
            )
        else:
            session = sessions.setdefault(session_id, FetchSession())
            if session.fetch_count >= 3:
                raise ValueError("ncp_fetch limit reached: max 3 per session")
            session.fetch_count += 1
            if pipeline_id is not None:
                session.pipeline_id = str(pipeline_id)
            effective_pipeline_id = session.pipeline_id
        try:
            k = min(int(args.get("k", 2)), 4)
        except (ValueError, TypeError):
            k = 2
        chunks = store.query(text=query_str, k=k, layer=layer, pipeline_id=effective_pipeline_id)
        if not chunks:
            return {"result": "ncp_fetch:no_results query_too_specific_or_layer_empty"}
        return {"result": _encode_fetch_results(chunks)}

    return {
        "ncp_get_context": _handle_get_context,
        "ncp_write_memory": _handle_write_memory,
        "ncp_emit_whisper": _handle_emit_whisper,
        "ncp_fetch": _handle_fetch,
    }


_SUPPORTED_VERSIONS = {"2024-11-05", "2025-03-26", "2025-06-18", "2025-11-25"}
_LATEST_VERSION = "2025-11-25"


def _negotiate_version(client_version: str) -> str:
    if client_version in _SUPPORTED_VERSIONS:
        return client_version
    return _LATEST_VERSION


def _handle_request(req: dict[str, object], handlers: dict[str, ToolHandler]) -> str | StreamResponse:
    req_id = req.get("id")
    method = str(req.get("method", ""))
    params: dict[str, object] = req.get("params", {}) or {}
    if not isinstance(params, dict):
        params = {}

    # Notifications have no id and require no response
    if req_id is None and method.startswith("notifications/"):
        return ""

    if method == "initialize":
        client_version = str(params.get("protocolVersion", "2024-11-05"))
        return _ok(
            req_id,
            {
                "protocolVersion": _negotiate_version(client_version),
                "serverInfo": {"name": "ncp", "version": "0.1.0a0"},
                "capabilities": {"tools": {"listChanged": False}},
            },
        )

    if method == "ping":
        return _ok(req_id, {})

    if method == "tools/list":
        return _ok(req_id, {"tools": MCP_TOOLS})

    if method == "tools/call":
        tool_name = str(params.get("name", ""))
        arguments: dict[str, object] = params.get("arguments", {}) or {}
        if not isinstance(arguments, dict):
            arguments = {}
        handler = handlers.get(tool_name)
        if handler is None:
            return _err_response(req_id, -32601, f"Tool not found: {tool_name}")
        try:
            result = handler(arguments)
            if isinstance(result, StreamResponse):
                result.request_id = req_id
                return result
            return _ok(req_id, {"content": [{"type": "text", "text": json.dumps(result)}]})
        except NotImplementedError as exc:
            _err(f"Tool {tool_name} unavailable for current backend: {traceback.format_exc()}")
            return _err_response(
                req_id,
                -32603,
                f"Tool unavailable for the configured store backend: {exc}",
            )
        except Exception as exc:
            _err(f"Tool {tool_name} error: {traceback.format_exc()}")
            return _err_response(req_id, -32603, f"Tool error: {exc}")

    # Respond with method-not-found only for requests (have an id); silently drop unknown notifications
    if req_id is None:
        return ""
    return _err_response(req_id, -32601, f"Method not found: {method}")


def _read_message(input_stream: BinaryIO) -> dict[str, object] | None:
    headers: dict[str, str] = {}
    while True:
        line = input_stream.readline()
        if not line:
            return None if not headers else None
        if line in (b"\r\n", b"\n"):
            break
        header_text = line.decode("ascii").strip()
        if not header_text:
            break
        name, _, value = header_text.partition(":")
        if not _:
            raise ValueError(f"Invalid MCP header: {header_text}")
        headers[name.strip().lower()] = value.strip()

    content_length = headers.get("content-length")
    if content_length is None:
        raise ValueError("Missing Content-Length header")
    try:
        cl_int = int(content_length)
    except ValueError:
        raise ValueError(f"Invalid Content-Length: {content_length!r}")
    if cl_int < 0 or cl_int > 10_485_760:
        raise ValueError(f"Content-Length out of allowed range: {cl_int}")
    body = input_stream.read(cl_int)
    if len(body) != cl_int:
        raise ValueError("Incomplete MCP message body")
    payload = json.loads(body.decode("utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("MCP request body must be a JSON object")
    return payload


def _write_message(output_stream: BinaryIO, payload: str) -> None:
    body = payload.encode("utf-8")
    output_stream.write(f"Content-Length: {len(body)}\r\n\r\n".encode("ascii"))
    output_stream.write(body)
    output_stream.flush()


def _create_handlers(
    *,
    store_path: str | Path | None = None,
    cwd: Path | None = None,
) -> dict[str, ToolHandler]:
    if store_path:
        config = load_config(env={"NCP_STORE_PATH": str(store_path)})
    else:
        config = load_config(cwd=cwd or Path.cwd())
    store = create_store(config)
    return make_handlers(store)


def serve_streams(
    input_stream: BinaryIO,
    output_stream: BinaryIO,
    *,
    store_path: str | Path | None = None,
    cwd: Path | None = None,
) -> None:
    """Run the MCP server against arbitrary binary streams."""
    try:
        handlers = _create_handlers(store_path=store_path, cwd=cwd)
    except Exception as exc:
        _err(f"NCP server failed to start: {exc}\n{traceback.format_exc()}")
        sys.exit(1)

    while True:
        try:
            req = _read_message(input_stream)
        except json.JSONDecodeError as exc:
            # Body was fully consumed but JSON was invalid — stream still in sync
            _err(f"Invalid MCP JSON: {exc}")
            continue
        except ValueError as exc:
            # Header/framing error — stream position is unknown, must stop
            _err(f"Invalid MCP framing: {exc}")
            break
        if req is None:
            break

        response = _handle_request(req, handlers)
        if response:
            _write_message(output_stream, response)


def serve(store_path: str | Path | None = None, *, cwd: Path | None = None) -> None:
    """Run the MCP stdio server loop."""
    serve_streams(sys.stdin.buffer, sys.stdout.buffer, store_path=store_path, cwd=cwd)


class _MCPHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(
        self,
        server_address: tuple[str, int],
        *,
        handlers: dict[str, ToolHandler],
        sse_path: str,
        rpc_path: str,
        keepalive_seconds: float,
    ) -> None:
        self.handlers = handlers
        self.sse_path = sse_path
        self.rpc_path = rpc_path
        self.keepalive_seconds = keepalive_seconds
        self._shutdown_event = threading.Event()
        super().__init__(server_address, _MCPHTTPHandler)


class _MCPHTTPHandler(BaseHTTPRequestHandler):
    server: _MCPHTTPServer
    protocol_version = "HTTP/1.1"

    def log_message(self, format: str, *args: object) -> None:
        return

    def _send_json(self, status: HTTPStatus, payload: object) -> None:
        body = _json_bytes(payload)
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)
        self.wfile.flush()

    def _send_empty(self, status: HTTPStatus) -> None:
        self.send_response(status)
        self.send_header("Content-Length", "0")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()

    def _stream_ndjson(self, sr: StreamResponse) -> None:
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "application/x-ndjson")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        try:
            for i, (label, text) in enumerate(sr.sections):
                line = json.dumps({"type": "ncp_chunk", "section": label, "index": i, "text": text}) + "\n"
                self.wfile.write(line.encode("utf-8"))
                self.wfile.flush()
            final = _ok(sr.request_id, {"content": [{"type": "text", "text": json.dumps(sr.handler_result)}]})
            self.wfile.write((final + "\n").encode("utf-8"))
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            return

    def do_OPTIONS(self) -> None:
        self.send_response(HTTPStatus.NO_CONTENT)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Access-Control-Allow-Methods", "GET,POST,OPTIONS")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self) -> None:
        if self.path == "/healthz":
            self._send_json(
                HTTPStatus.OK,
                {"ok": True, "transport": "http_sse", "rpc_path": self.server.rpc_path, "sse_path": self.server.sse_path},
            )
            return
        if self.path != self.server.sse_path:
            self._send_json(HTTPStatus.NOT_FOUND, {"error": "not_found"})
            return

        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        try:
            endpoint_event = f"event: endpoint\ndata: {self.server.rpc_path}\n\n".encode("utf-8")
            self.wfile.write(endpoint_event)
            self.wfile.flush()
            while not self.server._shutdown_event.is_set():
                self.wfile.write(b": keepalive\n\n")
                self.wfile.flush()
                time.sleep(self.server.keepalive_seconds)
        except (BrokenPipeError, ConnectionResetError):
            return

    def do_POST(self) -> None:
        if self.path not in (self.server.rpc_path, "/message"):
            self._send_json(HTTPStatus.NOT_FOUND, {"error": "not_found"})
            return

        try:
            content_length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": "invalid_content_length"})
            return
        try:
            payload = json.loads(self.rfile.read(content_length).decode("utf-8"))
        except json.JSONDecodeError as exc:
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": "invalid_json", "detail": str(exc)})
            return
        if not isinstance(payload, dict):
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": "invalid_payload"})
            return

        response = _handle_request(payload, self.server.handlers)
        if isinstance(response, StreamResponse):
            self._stream_ndjson(response)
        elif response:
            self._send_json(HTTPStatus.OK, json.loads(response))
        else:
            self._send_empty(HTTPStatus.ACCEPTED)


def create_http_server(
    *,
    host: str = "127.0.0.1",
    port: int = 4242,
    store_path: str | Path | None = None,
    cwd: Path | None = None,
    sse_path: str = "/sse",
    rpc_path: str = "/mcp",
    keepalive_seconds: float = 15.0,
) -> _MCPHTTPServer:
    handlers = _create_handlers(store_path=store_path, cwd=cwd)
    return _MCPHTTPServer(
        (host, port),
        handlers=handlers,
        sse_path=sse_path,
        rpc_path=rpc_path,
        keepalive_seconds=keepalive_seconds,
    )


def serve_http(
    *,
    host: str = "127.0.0.1",
    port: int = 4242,
    store_path: str | Path | None = None,
    cwd: Path | None = None,
    sse_path: str = "/sse",
    rpc_path: str = "/mcp",
    keepalive_seconds: float = 15.0,
) -> None:
    """Run the MCP server over HTTP POST plus an SSE discovery stream."""
    try:
        server = create_http_server(
            host=host,
            port=port,
            store_path=store_path,
            cwd=cwd,
            sse_path=sse_path,
            rpc_path=rpc_path,
            keepalive_seconds=keepalive_seconds,
        )
    except Exception as exc:
        _err(f"NCP HTTP server failed to start: {exc}\n{traceback.format_exc()}")
        sys.exit(1)

    try:
        server.serve_forever()
    finally:
        server._shutdown_event.set()
        server.server_close()
