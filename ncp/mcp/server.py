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
from ncp.chunker import filter_content
from ncp.config import NCPConfig, load_config
from ncp.stores.base import BaseStore
from ncp.stores.factory import create_store
from ncp.types import BudgetContext, ConsciousBlock, NCPResponse, SubconsciousChunk, Whisper
from ncp.version import __version__


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
                "k": {"type": "integer", "description": "Number of subconscious chunks to retrieve. Overrides the default budget-pressure-based value (2 for critical, 4 otherwise)."},
                "diversity_limit": {"type": "integer", "description": "Max chunks per author in retrieved results. Default 2. Set higher to allow more results from one author."},
                "max_tokens": {"type": "integer", "description": "Optional estimated token ceiling for the assembled context block."},
                "recent": {"type": "array", "items": {"type": "string"}, "description": "Optional recent refs overriding hydrated conscious state."},
                "tried": {"type": "array", "items": {"type": "string"}, "description": "Optional attempted actions overriding hydrated conscious state."},
                "failed": {"type": "array", "items": {"type": "string"}, "description": "Optional failed actions overriding hydrated conscious state."},
                "slot_age": {"type": "integer", "description": "Optional slot age overriding hydrated conscious state."},
                "slot_confidence": {"type": "number", "description": "Optional slot confidence overriding hydrated conscious state."},
                "goal_version": {"type": "integer", "description": "Optional goal version overriding hydrated conscious state."},
                "drift_score": {"type": "number", "description": "Optional drift score overriding hydrated conscious state."},
                "ctx_used": {"type": "number", "description": "Context window usage ratio 0.0-1.0."},
                "steps_completed": {"type": "integer", "description": "Completed plan steps for budget pressure."},
                "steps_total": {"type": "integer", "description": "Total plan steps for budget pressure."},
            },
            "required": ["agent_id", "role", "task", "slot", "intent"],
        },
    },
    {
        "name": "ncp_write_memory",
        "description": (
            "Write a durable subconscious chunk to the store. Content is automatically "
            "filtered at ingestion (ANSI codes, duplicate lines, boilerplate stripped). "
            "If filtering reduced the content, the response includes filtered=true, "
            "reduction_ratio, and a raw_ref chunk ID you can retrieve via ncp_fetch "
            "to recover the original."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "content": {"type": "string", "description": "Content (max 2000 chars)"},
                "layer": {"type": "string", "enum": ["episodic", "procedural", "semantic", "social", "reasoning_trace"]},
                "src": {"type": "string", "enum": ["user_verified", "tool_result", "agent_inferred", "synthesis", "subcon_retrieved"]},
                "written_by": {"type": "string", "description": "Agent writing this chunk"},
                "chunk_id": {"type": "string", "description": "Optional chunk ID (auto-generated if omitted)"},
                "pipeline_id": {"type": "string"},
                "base_trust": {"type": "number", "description": "Optional explicit trust score 0.0-1.0; otherwise derived from src."},
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
                "payload": {
                    "type": "string",
                    "description": (
                        "Whisper message (max 600 chars). share/request expect JSON "
                        "{\"ask\": str, \"files\": [str], \"slice\": str?}; dissent expects "
                        "JSON {\"issue\": str, \"alternatives\": [str]}. Plain text is accepted "
                        "by MCP and wrapped into the required shape."
                    ),
                },
                "confidence": {"type": "number", "description": "Confidence 0.0-1.0"},
                "pipeline_id": {"type": "string"},
                "ttl_seconds": {"type": "integer", "description": "Seconds before expiry. Default 1800."},
                "ref": {
                    "type": "string",
                    "description": (
                        "Optional chunk_id this whisper refers to. For a dissent whisper, set this "
                        "to the disputed chunk_id: it debits that chunk's trust and propagates the "
                        "penalty along its caused_by edge during feedback calibration."
                    ),
                },
            },
            "required": ["from", "target", "type", "payload", "confidence"],
        },
    },
    {
        "name": "ncp_post_turn",
        "description": "Record the completed turn, update conscious state, log cost, and acknowledge consumed whispers.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "agent_id": {"type": "string"},
                "role": {"type": "string"},
                "task": {"type": "string"},
                "slot": {"type": "string"},
                "intent": {"type": "string"},
                "pipeline_id": {"type": "string"},
                "turn_id": {"type": "string"},
                "result_summary": {"type": "string"},
                "result_full": {"type": "string"},
                "model": {"type": "string"},
                "input_tokens": {"type": "integer"},
                "output_tokens": {"type": "integer"},
                "cache_read_tokens": {"type": "integer"},
                "cost_usd": {"type": "number"},
                "latency_ms": {"type": "integer"},
                "ack_whisper_ids": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["agent_id", "role", "task", "slot", "intent", "result_summary", "result_full"],
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
                "diversity_limit": {"type": "integer", "description": "Max chunks per author. Default 2."},
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
        header = f"chunk:{chunk.chunk_id} layer:{chunk.layer} score:{chunk.relevance:.2f}"
        if chunk.raw_ref:
            header += f" raw_ref:{chunk.raw_ref}"
        lines.append(header)
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


def make_handlers(store: BaseStore, *, config: NCPConfig | None = None) -> dict[str, ToolHandler]:
    sessions: dict[str, FetchSession] = {}
    sessions_lock = threading.Lock()
    coordination = getattr(store, "coordination", None)
    default_whisper_ttl = config.whisper_ttl_default if config is not None else 1800

    def _fetch_budget_remaining(session_id: str) -> int:
        if coordination is not None:
            return 3
        with sessions_lock:
            session = sessions.get(session_id, FetchSession())
            return max(0, 3 - session.fetch_count)

    def _context_telemetry(result: object, *, session_id: str) -> dict[str, object]:
        evicted_high_relevance = [
            {"chunk_id": chunk_id, "relevance": relevance}
            for chunk_id, relevance in getattr(result, "evicted_high_relevance", [])
        ]
        evicted_whispers = [
            {"whisper_id": whisper_id, "confidence": confidence}
            for whisper_id, confidence in getattr(result, "evicted_whispers", [])
        ]
        pending_whisper_ids = list(getattr(result, "pending_whisper_ids", []))
        fetch_budget_remaining = _fetch_budget_remaining(session_id)
        return {
            "evicted_high_relevance": evicted_high_relevance,
            "evicted_high_relevance_count": len(evicted_high_relevance),
            "evicted_whispers": evicted_whispers,
            "evicted_whispers_count": len(evicted_whispers),
            "pending_whisper_ids": pending_whisper_ids,
            "fetch_budget_remaining": fetch_budget_remaining,
            "fetch_hint": "ncp_fetch" if evicted_high_relevance and fetch_budget_remaining > 0 else None,
        }

    def _handle_get_context(args: dict[str, object]) -> object:
        session_id = _session_id_from_args(args)
        pipeline_id = args.get("pipeline_id")
        normalized_pipeline_id = None if pipeline_id is None else str(pipeline_id)
        if coordination is not None and hasattr(coordination, "reset_fetch_session"):
            coordination.reset_fetch_session(session_id, pipeline_id=normalized_pipeline_id)
            if session_id != DEFAULT_FETCH_SESSION_ID:
                coordination.reset_fetch_session(DEFAULT_FETCH_SESSION_ID, pipeline_id=normalized_pipeline_id)
        else:
            with sessions_lock:
                sessions[session_id] = FetchSession(fetch_count=0, pipeline_id=normalized_pipeline_id)
                if session_id != DEFAULT_FETCH_SESSION_ID:
                    sessions[DEFAULT_FETCH_SESSION_ID] = FetchSession(fetch_count=0, pipeline_id=normalized_pipeline_id)
        conscious = _build_conscious_from_args(store, args)
        budget = _budget_from_args(args, conscious=conscious)
        assembler = Assembler(store=store)
        stream = bool(args.get("stream", False))
        try:
            caller_k: int | None = max(1, int(args["k"])) if "k" in args else None  # type: ignore[arg-type]
        except (ValueError, TypeError):
            caller_k = None
        try:
            caller_diversity_limit: int | None = max(1, int(args["diversity_limit"])) if "diversity_limit" in args else None  # type: ignore[arg-type]
        except (ValueError, TypeError):
            caller_diversity_limit = None
        try:
            caller_max_tokens: int | None = max(1, int(args["max_tokens"])) if "max_tokens" in args else None  # type: ignore[arg-type]
        except (ValueError, TypeError):
            caller_max_tokens = None
        if stream:
            stream_result = assembler.assemble(
                conscious=conscious,
                budget=budget,
                query_text=conscious.task + " " + conscious.slot,
                k=caller_k,
                diversity_limit=caller_diversity_limit,
                max_tokens=caller_max_tokens,
            )
            sections = assembler.sections_from_result(result=stream_result, budget=budget)
            return StreamResponse(
                sections=sections,
                handler_result={
                    "context": stream_result.context,
                    "session_id": session_id,
                    "pending_whisper_ids": stream_result.pending_whisper_ids,
                    "telemetry": _context_telemetry(stream_result, session_id=session_id),
                },
            )
        result = assembler.assemble(
            conscious=conscious,
            budget=budget,
            query_text=conscious.task + " " + conscious.slot,
            k=caller_k,
            diversity_limit=caller_diversity_limit,
            max_tokens=caller_max_tokens,
        )
        return {
            "context": result.context,
            "session_id": session_id,
            "pending_whisper_ids": result.pending_whisper_ids,
            "telemetry": _context_telemetry(result, session_id=session_id),
        }

    def _handle_write_memory(args: dict[str, object]) -> object:
        written_by = str(args.get("written_by", "agent"))
        pipeline_id = args.get("pipeline_id")
        latest = store.load_latest_conscious(
            pipeline_id=None if pipeline_id is None else str(pipeline_id),
            agent_id=written_by,
        )
        raw_content = str(args["content"])
        fr = filter_content(raw_content)
        content = fr.filtered

        kwargs: dict = {
            "content": content,
            "layer": str(args["layer"]),
            "src": str(args["src"]),
            "written_by": written_by,
            "pipeline_id": pipeline_id,
            "base_trust": _trust_from_args(args),
            "written_at_drift": 0.0 if latest is None else latest.drift_score,
        }
        if (chunk_id := args.get("chunk_id")):
            kwargs["chunk_id"] = str(chunk_id)

        raw_ref: str | None = None
        if fr.was_filtered and len(raw_content) <= 2000:
            raw_chunk = SubconsciousChunk(
                chunk_id=f"raw_{kwargs.get('chunk_id', '')}_{int(time.time() * 1000)}",
                layer=str(args["layer"]),
                content=raw_content,
                src="tool_result",
                written_by=written_by,
                pipeline_id=pipeline_id,
                base_trust=0.1,
                zone="working",
            )
            store.write(raw_chunk)
            raw_ref = raw_chunk.chunk_id
            kwargs["raw_ref"] = raw_ref

        chunk = SubconsciousChunk(**kwargs)
        ok = store.write(chunk)
        result: dict[str, object] = {"written": ok, "chunk_id": chunk.chunk_id}
        if fr.was_filtered:
            result["filtered"] = True
            result["reduction_ratio"] = round(fr.reduction_ratio, 3)
            if raw_ref is not None:
                result["raw_ref"] = raw_ref
        return result

    def _handle_emit_whisper(args: dict[str, object]) -> object:
        whisper_type = str(args["type"])
        payload = _normalize_mcp_whisper_payload(whisper_type, str(args["payload"]))
        try:
            ttl_seconds = max(1, int(args.get("ttl_seconds", default_whisper_ttl)))
        except (TypeError, ValueError):
            ttl_seconds = default_whisper_ttl
        ref = args.get("ref")
        whisper = Whisper(
            from_agent=str(args["from"]),
            target=str(args["target"]),
            whisper_type=whisper_type,
            payload=payload,
            confidence=float(args["confidence"]),
            pipeline_id=args.get("pipeline_id"),
            ttl_seconds=ttl_seconds,
            ref=None if ref is None else str(ref),
        )
        store.emit_whisper(whisper)
        result: dict[str, object] = {"emitted": True}
        if whisper_type == "dissent" and ref:
            result["dissent_recorded"] = store.record_dissent(str(ref))
        return result

    def _handle_post_turn(args: dict[str, object]) -> object:
        conscious = _build_conscious_from_args(store, args)
        assembler = Assembler(store=store)
        response = NCPResponse(
            content=str(args["result_full"]),
            turn_id=str(args.get("turn_id") or f"turn_{int(time.time() * 1000)}"),
            pipeline_id=conscious.pipeline_id,
            model=str(args.get("model", "unknown")),
            input_tokens=_int_arg(args, "input_tokens", 0),
            output_tokens=_int_arg(args, "output_tokens", 0),
            cache_read_tokens=_int_arg(args, "cache_read_tokens", 0),
            cost_usd=float(args.get("cost_usd", 0.0) or 0.0),
            latency_ms=_int_arg(args, "latency_ms", 0),
        )
        ack_ids = [str(item) for item in list(args.get("ack_whisper_ids", []) or [])]
        record = assembler.post_turn(
            conscious=conscious,
            response=response,
            result_summary=str(args["result_summary"]),
            result_full=str(args["result_full"]),
            ack_whisper_ids=ack_ids,
        )
        return {"posted": True, "turn_id": record.turn_id, "acknowledged_whisper_ids": ack_ids}

    def _handle_fetch(args: dict[str, object]) -> object:
        session_id = _session_id_from_args(args)
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
            with sessions_lock:
                session = sessions.setdefault(session_id, FetchSession())
                if session.fetch_count >= 3:
                    raise ValueError("ncp_fetch limit reached: max 3 per session")
                session.fetch_count += 1
                if pipeline_id is not None:
                    session.pipeline_id = str(pipeline_id)
                effective_pipeline_id = session.pipeline_id
        try:
            k = max(1, int(args.get("k", 2)))
        except (ValueError, TypeError):
            k = 2
        try:
            fetch_diversity_limit: int | None = max(1, int(args["diversity_limit"])) if "diversity_limit" in args else None  # type: ignore[arg-type]
        except (ValueError, TypeError):
            fetch_diversity_limit = None
        query_extra: dict = {}
        if fetch_diversity_limit is not None:
            query_extra["diversity_limit"] = fetch_diversity_limit
        chunks = store.query(text=query_str, k=k, layer=layer, pipeline_id=effective_pipeline_id, **query_extra)
        if not chunks:
            return {"result": "ncp_fetch:no_results query_too_specific_or_layer_empty"}
        return {"result": _encode_fetch_results(chunks)}

    return {
        "ncp_get_context": _handle_get_context,
        "ncp_write_memory": _handle_write_memory,
        "ncp_emit_whisper": _handle_emit_whisper,
        "ncp_post_turn": _handle_post_turn,
        "ncp_fetch": _handle_fetch,
    }


def _int_arg(args: dict[str, object], name: str, default: int) -> int:
    try:
        return max(0, int(args.get(name, default)))
    except (TypeError, ValueError):
        return default


def _float_arg(args: dict[str, object], name: str, default: float) -> float:
    try:
        return float(args.get(name, default))
    except (TypeError, ValueError):
        return default


def _list_arg(args: dict[str, object], name: str, default: list[str]) -> list[str]:
    if name not in args:
        return list(default)
    value = args.get(name)
    if not isinstance(value, list):
        return list(default)
    return [str(item) for item in value]


def _pressure_from_ctx(ctx_used: float) -> str:
    if ctx_used >= 0.90:
        return "critical"
    if ctx_used >= 0.75:
        return "high"
    if ctx_used >= 0.50:
        return "medium"
    return "low"


def _budget_from_args(args: dict[str, object], *, conscious: ConsciousBlock) -> BudgetContext:
    ctx_used = min(1.0, max(0.0, _float_arg(args, "ctx_used", conscious.ctx_used_ratio)))
    steps_completed = _int_arg(args, "steps_completed", conscious.steps_completed)
    steps_total = args.get("steps_total", conscious.steps_total)
    try:
        normalized_steps_total = None if steps_total is None else max(1, int(steps_total))
    except (TypeError, ValueError):
        normalized_steps_total = conscious.steps_total
    return BudgetContext(
        ctx_used=ctx_used,
        steps_completed=steps_completed,
        steps_total=normalized_steps_total,
        pressure=_pressure_from_ctx(ctx_used),
    )


def _build_conscious_from_args(store: BaseStore, args: dict[str, object]) -> ConsciousBlock:
    pipeline_value = args.get("pipeline_id")
    pipeline_id = None if pipeline_value is None else str(pipeline_value)
    agent_id = str(args["agent_id"])
    latest = store.load_latest_conscious(pipeline_id=pipeline_id, agent_id=agent_id)
    return ConsciousBlock(
        agent_id=agent_id,
        role=str(args["role"]),
        owns=_list_arg(args, "owns", [] if latest is None else latest.owns),
        must_not=_list_arg(args, "must_not", [] if latest is None else latest.must_not),
        task=str(args["task"]),
        slot=str(args["slot"]),
        intent=str(args["intent"]),
        pipeline_id=pipeline_id,
        recent=_list_arg(args, "recent", [] if latest is None else latest.recent),
        tried=_list_arg(args, "tried", [] if latest is None else latest.tried),
        failed=_list_arg(args, "failed", [] if latest is None else latest.failed),
        slot_age=_int_arg(args, "slot_age", 0 if latest is None else latest.slot_age),
        slot_confidence=min(1.0, max(0.0, _float_arg(args, "slot_confidence", 1.0 if latest is None else latest.slot_confidence))),
        goal_version=max(1, _int_arg(args, "goal_version", 1 if latest is None else latest.goal_version)),
        drift_score=min(1.0, max(0.0, _float_arg(args, "drift_score", 0.0 if latest is None else latest.drift_score))),
        ctx_used_ratio=min(1.0, max(0.0, _float_arg(args, "ctx_used", 0.0 if latest is None else latest.ctx_used_ratio))),
        steps_completed=_int_arg(args, "steps_completed", 0 if latest is None else latest.steps_completed),
        steps_total=(None if latest is None else latest.steps_total),
    )


def _trust_from_args(args: dict[str, object]) -> float:
    if "base_trust" in args:
        return min(1.0, max(0.0, _float_arg(args, "base_trust", 0.7)))
    return {
        "user_verified": 0.95,
        "tool_result": 0.80,
        "synthesis": 0.70,
        "agent_inferred": 0.60,
        "subcon_retrieved": 0.55,
    }.get(str(args.get("src", "")), 0.70)


def _normalize_mcp_whisper_payload(whisper_type: str, payload: str) -> str:
    trimmed = payload.strip()
    if trimmed.startswith("{") and trimmed.endswith("}"):
        return payload
    if whisper_type in {"share", "request"}:
        return json.dumps({"ask": payload})
    if whisper_type == "dissent":
        return json.dumps({"issue": payload})
    return payload


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
                "serverInfo": {"name": "ncp", "version": __version__},
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
    return make_handlers(store, config=config)


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
        if isinstance(response, StreamResponse):
            for i, (label, text) in enumerate(response.sections):
                notif = json.dumps({
                    "jsonrpc": "2.0",
                    "method": "ncp/stream_chunk",
                    "params": {
                        "request_id": response.request_id,
                        "section": label,
                        "index": i,
                        "text": text,
                    },
                })
                _write_message(output_stream, notif)
            final = _ok(
                response.request_id,
                {"content": [{"type": "text", "text": json.dumps(response.handler_result)}]},
            )
            _write_message(output_stream, final)
        elif response:
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
        auth_token: str | None,
        cors_allowed_origins: list[str],
        max_body_bytes: int,
    ) -> None:
        self.handlers = handlers
        self.sse_path = sse_path
        self.rpc_path = rpc_path
        self.keepalive_seconds = keepalive_seconds
        self.auth_token = auth_token
        self.cors_allowed_origins = cors_allowed_origins
        self.max_body_bytes = max_body_bytes
        self._shutdown_event = threading.Event()
        super().__init__(server_address, _MCPHTTPHandler)


class _MCPHTTPHandler(BaseHTTPRequestHandler):
    server: _MCPHTTPServer
    protocol_version = "HTTP/1.1"

    def log_message(self, format: str, *args: object) -> None:
        return

    def _cors_origin(self) -> str | None:
        origin = self.headers.get("Origin")
        if not origin:
            return None
        allowed = self.server.cors_allowed_origins
        if "*" in allowed:
            return "*"
        if origin in allowed:
            return origin
        return None

    def _send_cors_headers(self) -> None:
        origin = self._cors_origin()
        if origin is not None:
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Vary", "Origin")

    def _authorized(self) -> bool:
        token = self.server.auth_token
        if not token:
            return True
        auth = self.headers.get("Authorization", "")
        return auth == f"Bearer {token}"

    def _send_json(self, status: HTTPStatus, payload: object) -> None:
        body = _json_bytes(payload)
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self._send_cors_headers()
        self.end_headers()
        self.wfile.write(body)
        self.wfile.flush()

    def _send_empty(self, status: HTTPStatus) -> None:
        self.send_response(status)
        self.send_header("Content-Length", "0")
        self.send_header("Cache-Control", "no-store")
        self._send_cors_headers()
        self.end_headers()

    def _stream_ndjson(self, sr: StreamResponse) -> None:
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "application/x-ndjson")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        self._send_cors_headers()
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

    def _prefers_event_stream(self) -> bool:
        accept = self.headers.get("Accept", "")
        return "text/event-stream" in accept and "application/json" not in accept

    def _begin_event_stream(self) -> None:
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        self._send_cors_headers()
        self.end_headers()

    def _send_sse_message(self, rpc_json: str) -> None:
        self._begin_event_stream()
        try:
            self.wfile.write(f"event: message\ndata: {rpc_json}\n\n".encode("utf-8"))
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            return

    def _stream_sse(self, sr: StreamResponse) -> None:
        self._begin_event_stream()
        try:
            for i, (label, text) in enumerate(sr.sections):
                chunk = json.dumps({"type": "ncp_chunk", "section": label, "index": i, "text": text})
                self.wfile.write(f"event: ncp_chunk\ndata: {chunk}\n\n".encode("utf-8"))
                self.wfile.flush()
            final = _ok(sr.request_id, {"content": [{"type": "text", "text": json.dumps(sr.handler_result)}]})
            self.wfile.write(f"event: message\ndata: {final}\n\n".encode("utf-8"))
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            return

    def do_OPTIONS(self) -> None:
        self.send_response(HTTPStatus.NO_CONTENT)
        self._send_cors_headers()
        allowed_headers = "Content-Type, Authorization" if self.server.auth_token else "Content-Type"
        self.send_header("Access-Control-Allow-Headers", allowed_headers)
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
        if not self._authorized():
            self._send_json(HTTPStatus.UNAUTHORIZED, {"error": "unauthorized"})
            return

        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self._send_cors_headers()
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

    def _drain_request_body(self) -> None:
        """Consume the request body before an early error response.

        Responding and closing while unread body bytes sit on the socket can
        trigger a TCP reset that discards the response before the client reads
        it. Bodies too large to drain force the connection closed instead.
        """
        try:
            content_length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            self.close_connection = True
            return
        if content_length <= 0:
            return
        if content_length > self.server.max_body_bytes:
            self.close_connection = True
            return
        self.rfile.read(content_length)

    def do_POST(self) -> None:
        if self.path not in (self.server.rpc_path, "/message"):
            self._drain_request_body()
            self._send_json(HTTPStatus.NOT_FOUND, {"error": "not_found"})
            return
        if not self._authorized():
            self._drain_request_body()
            self._send_json(HTTPStatus.UNAUTHORIZED, {"error": "unauthorized"})
            return

        try:
            content_length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            self.close_connection = True
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": "invalid_content_length"})
            return
        if content_length > self.server.max_body_bytes:
            self.close_connection = True
            self._send_json(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, {"error": "request_too_large"})
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
        prefers_sse = self._prefers_event_stream()
        if isinstance(response, StreamResponse):
            if prefers_sse:
                self._stream_sse(response)
            else:
                self._stream_ndjson(response)
        elif response:
            if prefers_sse:
                self._send_sse_message(response)
            else:
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
    auth_token: str | None = None,
    cors_allowed_origins: list[str] | None = None,
    max_body_bytes: int = 10_485_760,
) -> _MCPHTTPServer:
    handlers = _create_handlers(store_path=store_path, cwd=cwd)
    return _MCPHTTPServer(
        (host, port),
        handlers=handlers,
        sse_path=sse_path,
        rpc_path=rpc_path,
        keepalive_seconds=keepalive_seconds,
        auth_token=auth_token,
        cors_allowed_origins=list(cors_allowed_origins or []),
        max_body_bytes=max(1, max_body_bytes),
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
    auth_token: str | None = None,
    cors_allowed_origins: list[str] | None = None,
    max_body_bytes: int = 10_485_760,
) -> None:
    """Run the MCP server over HTTP POST plus an SSE discovery stream."""
    if host not in {"127.0.0.1", "localhost", "::1"} and not auth_token:
        _err(
            "WARNING: NCP HTTP server is bound to a non-loopback host without auth_token. "
            "Set an auth token before exposing this endpoint."
        )
    try:
        server = create_http_server(
            host=host,
            port=port,
            store_path=store_path,
            cwd=cwd,
            sse_path=sse_path,
            rpc_path=rpc_path,
            keepalive_seconds=keepalive_seconds,
            auth_token=auth_token,
            cors_allowed_origins=cors_allowed_origins,
            max_body_bytes=max_body_bytes,
        )
    except Exception as exc:
        _err(f"NCP HTTP server failed to start: {exc}\n{traceback.format_exc()}")
        sys.exit(1)

    try:
        server.serve_forever()
    finally:
        server._shutdown_event.set()
        server.server_close()
