"""MCP transports for stdio and HTTP/SSE JSON-RPC 2.0 endpoints."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import sys
import threading
import time
import traceback
from collections.abc import Callable
from pathlib import Path
from typing import BinaryIO, get_args

from ncp.adaptive_budget import AdaptiveBudgetResult, compute_adaptive_budget
from ncp.assembler import Assembler
from ncp.budget import BudgetSnapshot, evaluate_budget
from ncp.chunker import filter_content
from ncp.config import NCPConfig, load_config
from ncp.drift import EmbeddingAdapter, compute_drift
from ncp.stores.base import BaseStore
from ncp.stores.factory import create_store
from ncp.tiering import compute_tier_signal
from ncp.types import (
    BudgetContext,
    ChunkEdge,
    ChunkEdgeType,
    ConsciousBlock,
    NCPResponse,
    OutcomeRecord,
    SubconsciousChunk,
    Whisper,
)

from ncp.version import __version__

# Graph engineering: closed edge-type set, derived from ncp.types.ChunkEdgeType
# so the MCP schema/validation can't drift from the pydantic model's Literal.
_CHUNK_EDGE_TYPES = get_args(ChunkEdgeType)


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
                "max_tokens": {"type": "integer", "description": "Optional estimated token ceiling for the assembled context block. Always honored as-is; when omitted and [budget].adaptive_budget_enabled is true, the ceiling is instead computed per-turn (see the response's budget_tokens block)."},
                "recent": {"type": "array", "items": {"type": "string"}, "description": "Optional recent refs overriding hydrated conscious state."},
                "tried": {"type": "array", "items": {"type": "string"}, "description": "Optional attempted actions overriding hydrated conscious state."},
                "failed": {"type": "array", "items": {"type": "string"}, "description": "Optional failed actions overriding hydrated conscious state."},
                "slot_age": {"type": "integer", "description": "Optional slot age overriding hydrated conscious state."},
                "slot_confidence": {"type": "number", "description": "Optional slot confidence overriding hydrated conscious state."},
                "goal_version": {"type": "integer", "description": "Optional goal version overriding hydrated conscious state."},
                "drift_score": {"type": "number", "description": "Optional drift score overriding hydrated conscious state. Ignored -- and reported back as self_reported -- when [drift].drift_computed_enabled is true; see the response's drift block."},
                "ctx_used": {"type": "number", "description": "Context window usage ratio 0.0-1.0."},
                "steps_completed": {"type": "integer", "description": "Completed plan steps for budget pressure."},
                "steps_total": {"type": "integer", "description": "Total plan steps for budget pressure."},
                "as_of": {
                    "type": "string",
                    "description": (
                        "CAP-C5: optional ISO-8601 timestamp (or epoch seconds). When given, "
                        "assembles the bi-temporal view as of that transaction time -- only "
                        "chunks recorded by then, not superseded by a chunk recorded by then, "
                        "and valid (valid_from/valid_to) at that instant. Omit for the default "
                        "currently-valid view."
                    ),
                },
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
            "to recover the original. When [graph].infer_edges is enabled, the response "
            "also includes edges_inferred: the count of 'refines' edges automatically "
            "linked to similar recent chunks in the same pipeline (0 is valid)."
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
                "signature": {
                    "type": "string",
                    "description": (
                        "Optional base64 Ed25519 signature over the canonical authorship "
                        "payload (written_by | sha256(content) | pipeline_id). When present it "
                        "is verified and recorded. Required only if [identity].require_signatures "
                        "is enabled, in which case an unverifiable write is rejected."
                    ),
                },
                "valid_from": {
                    "type": "string",
                    "description": (
                        "CAP-C5: optional ISO-8601 timestamp (or epoch seconds) for when this "
                        "fact became true in the world (valid time). Defaults to unset (no bound)."
                    ),
                },
                "valid_to": {
                    "type": "string",
                    "description": (
                        "CAP-C5: optional ISO-8601 timestamp (or epoch seconds) for when this "
                        "chunk's own fact stops being true in the world. Defaults to unset (no "
                        "bound). Independent of 'supersedes', which sets the *previous* chunk's "
                        "valid_to, not this one's."
                    ),
                },
                "supersedes": {
                    "type": "string",
                    "description": (
                        "CAP-C5: chunk_id of an existing chunk this write honestly replaces. "
                        "The old chunk is NOT deleted: its superseded_by is set to this new "
                        "chunk's id and its own valid_to is set to this chunk's valid_from (or "
                        "now if valid_from is omitted). Default (non-as_of) reads then return "
                        "only this new chunk; an as_of query before the supersession still "
                        "returns the old one."
                    ),
                },
                "edges": {
                    "type": "array",
                    "description": (
                        "Graph engineering: optional typed edges from this new chunk to other "
                        "existing chunks. Each item is {dst, type, weight?}; 'type' must be one "
                        "of caused_by, supersedes, supports, contradicts, refines, derived_from. "
                        "Written via the typed chunk_edges graph with src = this new chunk's id."
                    ),
                    "items": {
                        "type": "object",
                        "properties": {
                            "dst": {"type": "string", "description": "Target chunk_id"},
                            "type": {"type": "string", "enum": list(_CHUNK_EDGE_TYPES)},
                            "weight": {"type": "number", "description": "Optional edge weight, default 1.0"},
                        },
                        "required": ["dst", "type"],
                    },
                },
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
                "signature": {
                    "type": "string",
                    "description": (
                        "Optional base64 Ed25519 signature over the canonical authorship payload "
                        "(from | sha256(payload) | pipeline_id). Verified and recorded when present; "
                        "required only if [identity].require_signatures is enabled."
                    ),
                },
            },
            "required": ["from", "target", "type", "payload", "confidence"],
        },
    },
    {
        "name": "ncp_post_turn",
        "description": (
            "Record the completed turn, update conscious state, log cost, and acknowledge "
            "consumed whispers. Optionally persists memory_chunks; any writes suppressed by "
            "deduplication are reported in the response's suppressed_chunk_ids list."
        ),
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
                "memory_chunks": {
                    "type": "array",
                    "description": (
                        "Optional subconscious chunks to persist with this turn. Chunks whose "
                        "write is suppressed (e.g. near-duplicate content) are listed in the "
                        "response's suppressed_chunk_ids."
                    ),
                    "items": {
                        "type": "object",
                        "properties": {
                            "content": {"type": "string", "description": "Content (max 2000 chars)"},
                            "layer": {"type": "string", "enum": ["episodic", "procedural", "semantic", "social", "reasoning_trace"]},
                            "src": {"type": "string", "enum": ["user_verified", "tool_result", "agent_inferred", "synthesis", "subcon_retrieved"]},
                            "chunk_id": {"type": "string", "description": "Optional chunk ID (auto-generated if omitted)"},
                            "written_by": {"type": "string", "description": "Agent writing this chunk (defaults to agent_id)"},
                        },
                        "required": ["content", "layer", "src"],
                    },
                },
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
    {
        "name": "ncp_record_decision",
        "description": (
            "Record a structured decision trace: what was decided, what alternatives "
            "were considered, and what evidence supported it. Stored as a reasoning_trace "
            "chunk with caused_by edges for graph traversal and precedent queries."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "decision": {"type": "string", "description": "What was decided (concise statement)"},
                "alternatives": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Other options that were considered",
                },
                "rationale": {"type": "string", "description": "Why this alternative was chosen over others"},
                "evidence_refs": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Chunk IDs or turn refs that informed this decision",
                },
                "outcome": {
                    "type": "string",
                    "enum": ["pending", "succeeded", "failed", "superseded"],
                    "description": "Current outcome status. Default 'pending'.",
                },
                "confidence": {"type": "number", "description": "Decision confidence 0.0-1.0"},
                "agent_id": {"type": "string", "description": "Agent making this decision"},
                "pipeline_id": {"type": "string", "description": "Pipeline scope"},
                "caused_by": {"type": "string", "description": "Chunk ID that prompted this decision (causal parent)"},
                "tags": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Searchable tags for precedent queries (e.g. 'architecture', 'null-guard', 'retry-logic')",
                },
            },
            "required": ["decision", "rationale", "agent_id"],
        },
    },
    {
        "name": "ncp_record_outcome",
        "description": (
            "Record a task outcome for outcome-calibrated reputation (CAP-T3). "
            "Provide either chunk_ids (explicit) or turn_id (resolved to chunk_ids). "
            "Success=True increases author reputation; success=False decreases it."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "success": {
                    "type": "boolean",
                    "description": "Whether the outcome was successful",
                },
                "chunk_ids": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Chunk IDs that contributed to this outcome. Exclusive with turn_id.",
                },
                "turn_id": {
                    "type": "string",
                    "description": "Turn ID to resolve to chunk_ids. Exclusive with chunk_ids.",
                },
                "outcome_id": {
                    "type": "string",
                    "description": "Optional custom outcome_id. Auto-generated if omitted.",
                },
                "weight": {
                    "type": "number",
                    "description": "Evidence weight multiplier (default 1.0). Higher = stronger signal.",
                },
                "note": {
                    "type": "string",
                    "description": "Optional human-readable note about this outcome.",
                },
            },
        },
    },
    {
        "name": "ncp_lookup_memo",
        "description": (
            "Look up a previously recorded work memo by task+context signature (CAP-C3). "
            "Returns the memo if found, not stale, and meeting minimum outcome/verified criteria. "
            "Provide either task+context or an explicit signature."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "task": {
                    "type": "string",
                    "description": "Task description for signature computation (ignored if signature is provided)",
                },
                "context": {
                    "type": "string",
                    "description": "Optional context string for signature computation",
                },
                "signature": {
                    "type": "string",
                    "description": "Explicit memo signature (overrides task+context hash)",
                },
            },
        },
    },
    {
        "name": "ncp_record_memo",
        "description": (
            "Record a work memo for CAP-C3 semantic memoization. "
            "Stores a signature-keyed entry that can be looked up later to skip redundant work."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "task": {
                    "type": "string",
                    "description": "Task description for signature computation (ignored if signature is provided)",
                },
                "chunk_ids": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Chunk IDs produced by this work",
                },
                "result_summary": {
                    "type": "string",
                    "description": "Optional summary of the work result",
                },
                "signature": {
                    "type": "string",
                    "description": "Explicit memo signature (overrides task+context hash)",
                },
                "output_tokens_est": {
                    "type": "integer",
                    "description": (
                        "Optional real output token count of the memoized work; "
                        "estimated from result_summary when omitted"
                    ),
                },
                "kind": {
                    "type": "string",
                    "enum": ["function", "class", "module"],
                    "description": (
                        "Optional declared shape of result_summary when it holds code. "
                        "If set, result_summary must parse as Python and (for 'function'/"
                        "'class') contain a matching top-level def/class, or the memo is "
                        "rejected. This is a structural presence check only — it does not "
                        "verify correctness or safety of the code."
                    ),
                },
            },
            "required": ["task", "chunk_ids"],
        },
    },
]


def _encode_fetch_results(chunks: list[SubconsciousChunk]) -> str:
    lines = [f"ncp_fetch:results k:{len(chunks)}"]
    for chunk in chunks:
        header = f"chunk:{chunk.chunk_id} layer:{chunk.layer} score:{chunk.relevance:.2f}"
        if chunk.verified:
            header += " verified:1"
        if chunk.raw_ref:
            header += f" raw_ref:{chunk.raw_ref}"
        lines.append(header)
        lines.append(f"  {chunk.content}")
    return "\n".join(lines)


def _verify_authorship(
    store: BaseStore,
    *,
    identity_id: str,
    content: str,
    pipeline_id: str | None,
    signature: str | None,
) -> bool:
    """Verify an optional authorship signature via the store.

    Returns ``False`` when no signature is supplied, the store cannot verify, or
    verification fails (unknown/revoked identity or bad signature). Never raises.
    """

    if signature is None:
        return False
    verifier = getattr(store, "verify_authorship", None)
    if not callable(verifier):
        return False
    from ncp.identity import canonical_authorship_payload

    payload = canonical_authorship_payload(identity_id, content, pipeline_id)
    try:
        return bool(verifier(identity_id, payload, signature))
    except Exception:
        return False


@dataclass
class FetchSession:
    fetch_count: int = 0
    pipeline_id: str | None = None
    last_access: float = field(default_factory=time.monotonic)


# Bound the in-memory fetch-session table so a client rotating session_ids
# cannot grow it without limit. Entries older than the TTL are dropped; if the
# table still exceeds the cap, the least-recently-accessed entries are evicted.
_FETCH_SESSION_MAX = 1024
_FETCH_SESSION_TTL_SECONDS = 3600.0


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


def _build_drift_embedding_adapter(config: NCPConfig) -> EmbeddingAdapter | None:
    """CAP-T5: best-effort local embedding adapter for the drift blend.

    Built once at handler setup (not per-request) since loading the
    fastembed model is comparatively expensive. ``None`` -- e.g. fastembed
    is not installed -- means computed drift silently stays lexical-only.
    """
    try:
        from ncp.adapters.embedding import LocalEmbeddingAdapter

        return LocalEmbeddingAdapter(model=config.embedding_model)
    except Exception:
        return None


def make_handlers(store: BaseStore, *, config: NCPConfig | None = None) -> dict[str, ToolHandler]:
    sessions: dict[str, FetchSession] = {}
    sessions_lock = threading.Lock()
    coordination = getattr(store, "coordination", None)
    default_whisper_ttl = config.whisper_ttl_default if config is not None else 1800
    require_signatures = config.require_signatures if config is not None else False
    # CAP-E2: per-pipeline budget governance settings.
    pipeline_budget_usd = config.pipeline_budget_usd if config is not None else None
    budget_warn_fraction = config.budget_warn_fraction if config is not None else 0.8
    budget_enforcement = config.budget_enforcement if config is not None else "warn"
    # CAP-E3: model-tiering advisory signal, on by default.
    tier_hints_enabled = config.tier_hints_enabled if config is not None else True
    # CAP-C6: adaptive per-turn context token budget, opt-in (off by default).
    adaptive_budget_enabled = config.adaptive_budget_enabled if config is not None else False
    adaptive_budget_floor_tokens = config.adaptive_budget_floor_tokens if config is not None else 300
    adaptive_budget_ceiling_tokens = config.adaptive_budget_ceiling_tokens if config is not None else 2000
    default_context_token_budget = config.context_token_budget if config is not None else 840
    # CAP-T5: computed drift, opt-in (off by default -- legacy self-reported
    # drift_score is unchanged when disabled).
    drift_computed_enabled = config.drift_computed_enabled if config is not None else False
    drift_window_turns = config.drift_window_turns if config is not None else 5
    drift_use_embeddings = config.drift_use_embeddings if config is not None else False
    # CAP-C7/WI-P2: write-time edge inference, opt-in (off by default -- the
    # `edges_inferred` response field is only surfaced when enabled).
    infer_edges_enabled = config.infer_edges if config is not None else False
    drift_embedding_adapter: EmbeddingAdapter | None = None
    if drift_computed_enabled and drift_use_embeddings:
        drift_embedding_adapter = _build_drift_embedding_adapter(config)

    def _budget_snapshot(*, pipeline_id: str | None) -> BudgetSnapshot | None:
        """CAP-E2: read real recorded spend and classify it against the ceiling.

        Returns ``None`` when no budget is configured, the governor is off,
        or the store cannot report cost (never fabricates a number).
        """
        if pipeline_budget_usd is None or budget_enforcement == "off":
            return None
        cost_summary_fn = getattr(store, "cost_summary", None)
        if not callable(cost_summary_fn):
            return None
        try:
            summary = cost_summary_fn(pipeline_id=pipeline_id, limit=1)
            spent_usd = float(summary.get("summary", {}).get("cost_usd_total", 0.0))
        except Exception:
            return None
        return evaluate_budget(
            spent_usd=spent_usd,
            budget_usd=pipeline_budget_usd,
            warn_fraction=budget_warn_fraction,
        )

    def _prune_sessions() -> None:
        # Caller must hold sessions_lock. Drop expired entries, then LRU-evict
        # down to the cap so rotating session_ids cannot grow the table forever.
        now = time.monotonic()
        expired = [
            sid
            for sid, sess in sessions.items()
            if now - sess.last_access > _FETCH_SESSION_TTL_SECONDS
        ]
        for sid in expired:
            del sessions[sid]
        if len(sessions) > _FETCH_SESSION_MAX:
            for sid in sorted(sessions, key=lambda s: sessions[s].last_access)[
                : len(sessions) - _FETCH_SESSION_MAX
            ]:
                del sessions[sid]

    def _fetch_budget_remaining(session_id: str) -> int:
        if coordination is not None:
            if hasattr(coordination, "fetch_budget_remaining"):
                return int(coordination.fetch_budget_remaining(session_id))
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

    def _tier_signal(result: object, *, budget: BudgetContext, search_text: str):
        """CAP-E3: derive the tier-hint signal from one assembly result.

        ``cold_start`` mirrors ``Assembler._cold_start_bootstrap``: it fires
        when retrieval came back empty and the assembler substituted its
        single synthetic ``cold_*`` pipeline_summary chunk.
        """
        chunks = list(getattr(result, "chunks", []))
        chunk_count = len(chunks)
        distinct_authors = len({chunk.written_by for chunk in chunks})
        cold_start = chunk_count == 1 and chunks[0].chunk_id.startswith("cold_")
        conscious = getattr(result, "conscious", None)
        drift_score = float(getattr(conscious, "drift_score", 0.0))
        return compute_tier_signal(
            query_text=search_text,
            chunk_count=chunk_count,
            distinct_authors=distinct_authors,
            drift_score=drift_score,
            pressure=budget.pressure,
            cold_start=cold_start,
        )

    def _adaptive_budget_tokens(
        *,
        caller_max_tokens: int | None,
        conscious: ConsciousBlock,
        budget: BudgetContext,
        search_text: str,
        budget_snapshot: BudgetSnapshot | None,
    ) -> tuple[int | None, dict[str, object] | None]:
        """CAP-C6: resolve the effective ``max_tokens`` for this call.

        Returns ``(effective_max_tokens, budget_tokens_block)``. The block is
        ``None`` whenever the feature is disabled; ``effective_max_tokens`` is
        always exactly ``caller_max_tokens`` (including ``None``, i.e. "let
        the assembler use its configured default") when disabled, so a
        disabled flag reproduces legacy behavior byte-for-byte.

        An explicit caller ``max_tokens`` always wins: when present, no
        adaptive computation runs and the block (when the feature is on)
        simply echoes it back with ``reason_factors: {"explicit_override": True}``.
        """

        if not adaptive_budget_enabled:
            return caller_max_tokens, None

        if caller_max_tokens is not None:
            return caller_max_tokens, {
                "requested": caller_max_tokens,
                "adjusted": caller_max_tokens,
                "reason_factors": {"explicit_override": True},
            }

        result: AdaptiveBudgetResult = compute_adaptive_budget(
            requested_tokens=default_context_token_budget,
            floor_tokens=adaptive_budget_floor_tokens,
            ceiling_tokens=adaptive_budget_ceiling_tokens,
            query_text=search_text,
            drift_score=conscious.drift_score,
            pressure=budget.pressure,
            slot_age=conscious.slot_age,
            dollar_budget_fraction_used=(
                None if budget_snapshot is None else budget_snapshot.fraction_used
            ),
        )
        return result.adjusted_tokens, result.to_dict()

    def _apply_computed_drift(
        conscious: ConsciousBlock,
        args: dict[str, object],
    ) -> tuple[ConsciousBlock, dict[str, object] | None]:
        """CAP-T5: override ``drift_score`` with a computed reading.

        Returns ``(conscious, drift_block)``. When disabled, ``conscious`` is
        returned unchanged and ``drift_block`` is ``None`` -- exact legacy
        behavior, no response change. When enabled, ``conscious.drift_score``
        is overridden with the computed value *before* the caller runs
        tiering/adaptive-budget/assembly, and ``drift_block`` exposes both the
        computed score and the original self-reported value (``None`` when
        nothing was ever self-reported for this agent/pipeline -- an
        untouched hardcoded default is not a claim) so the two can be
        compared.
        """
        if not drift_computed_enabled:
            return conscious, None

        had_prior_conscious = (
            store.load_latest_conscious(pipeline_id=conscious.pipeline_id, agent_id=conscious.agent_id)
            is not None
        )
        self_reported = (
            conscious.drift_score if ("drift_score" in args or had_prior_conscious) else None
        )
        turns_fn = getattr(store, "recent_turns", None)
        recent_records = (
            turns_fn(pipeline_id=conscious.pipeline_id, limit=drift_window_turns) if callable(turns_fn) else []
        )
        recent_turn_texts = [f"{record.task} {record.slot} {record.result}" for record in recent_records]
        reading = compute_drift(
            recent_turn_texts,
            conscious.task + " " + conscious.slot,
            window_turns=drift_window_turns,
            use_embeddings=drift_use_embeddings,
            embedding_adapter=drift_embedding_adapter,
        )
        updated_conscious = conscious.model_copy(update={"drift_score": reading.score})
        drift_block = reading.to_dict()
        drift_block["self_reported"] = self_reported
        return updated_conscious, drift_block

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
                _prune_sessions()
        conscious = _build_conscious_from_args(store, args)
        # CAP-T5: compute drift from observable turn history and override the
        # self-reported drift_score *before* budget/tiering/assembly consume
        # it. No-op (drift_block stays None) when the feature is disabled.
        conscious, drift_block = _apply_computed_drift(conscious, args)
        budget = _budget_from_args(args, conscious=conscious)

        # CAP-E2: pre-flight budget check. In "block" mode an already-exceeded
        # pipeline gets a structured refusal instead of paying for assembly.
        budget_snapshot = _budget_snapshot(pipeline_id=normalized_pipeline_id)
        if budget_snapshot is not None and budget_enforcement == "block" and budget_snapshot.status == "exceeded":
            return {
                "budget_exceeded": True,
                "context": "",
                "session_id": session_id,
                "budget": budget_snapshot.to_dict(),
            }

        assembler = Assembler(store=store, config=config)
        stream = bool(args.get("stream", False))
        search_text = conscious.task + " " + conscious.slot
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
        # CAP-C5: optional bi-temporal point-in-time view.
        as_of_raw = args.get("as_of")
        as_of: float | None = None if as_of_raw is None else _parse_iso_timestamp(as_of_raw, field="as_of")
        # CAP-C6: adaptive budget only ever substitutes for an *omitted*
        # max_tokens; an explicit caller value is never overridden.
        effective_max_tokens, budget_tokens_block = _adaptive_budget_tokens(
            caller_max_tokens=caller_max_tokens,
            conscious=conscious,
            budget=budget,
            search_text=search_text,
            budget_snapshot=budget_snapshot,
        )
        if stream:
            stream_result = assembler.assemble(
                conscious=conscious,
                budget=budget,
                query_text=search_text,
                k=caller_k,
                diversity_limit=caller_diversity_limit,
                max_tokens=effective_max_tokens,
                as_of=as_of,
            )
            sections = assembler.sections_from_result(result=stream_result, budget=budget)
            handler_result: dict[str, object] = {
                "context": stream_result.context,
                "session_id": session_id,
                "pending_whisper_ids": stream_result.pending_whisper_ids,
                "telemetry": _context_telemetry(stream_result, session_id=session_id),
            }
            if budget_snapshot is not None:
                handler_result["budget"] = budget_snapshot.to_dict()
            if tier_hints_enabled:
                handler_result.update(_tier_signal(stream_result, budget=budget, search_text=search_text).to_dict())
            if budget_tokens_block is not None:
                handler_result["budget_tokens"] = budget_tokens_block
            if drift_block is not None:
                handler_result["drift"] = drift_block
            if as_of is not None:
                handler_result["as_of"] = as_of
            return StreamResponse(
                sections=sections,
                handler_result=handler_result,
            )
        result = assembler.assemble(
            conscious=conscious,
            budget=budget,
            query_text=search_text,
            k=caller_k,
            diversity_limit=caller_diversity_limit,
            max_tokens=effective_max_tokens,
            as_of=as_of,
        )
        response: dict[str, object] = {
            "context": result.context,
            "session_id": session_id,
            "pending_whisper_ids": result.pending_whisper_ids,
            "telemetry": _context_telemetry(result, session_id=session_id),
        }
        if budget_snapshot is not None:
            response["budget"] = budget_snapshot.to_dict()
        if tier_hints_enabled:
            response.update(_tier_signal(result, budget=budget, search_text=search_text).to_dict())
        if budget_tokens_block is not None:
            response["budget_tokens"] = budget_tokens_block
        if drift_block is not None:
            response["drift"] = drift_block
        if as_of is not None:
            response["as_of"] = as_of
        return response

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

        # Graph engineering: validate requested edges before any writes happen,
        # so an unknown edge type rejects the whole call rather than persisting
        # a chunk with edges silently dropped.
        raw_edges = args.get("edges") or []
        if not isinstance(raw_edges, list):
            raise ValueError("ncp_write_memory: 'edges' must be an array")
        for item in raw_edges:
            if not isinstance(item, dict) or "dst" not in item or "type" not in item:
                raise ValueError("ncp_write_memory: each edges item must be an object with 'dst' and 'type'")
            if item["type"] not in _CHUNK_EDGE_TYPES:
                raise ValueError(
                    f"ncp_write_memory: unknown edge type {item['type']!r}; "
                    f"expected one of {sorted(_CHUNK_EDGE_TYPES)}"
                )

        # CAP-T1/WI-013: opt-in authorship verification. The client signs over the
        # canonical (written_by | sha256(raw_content) | pipeline_id) payload, so we
        # verify against the raw content it sent (not the post-filter content).
        signature = args.get("signature")
        verified = _verify_authorship(
            store,
            identity_id=written_by,
            content=raw_content,
            pipeline_id=None if pipeline_id is None else str(pipeline_id),
            signature=None if signature is None else str(signature),
        )
        if require_signatures and not verified:
            raise ValueError(
                "ncp_write_memory rejected: require_signatures is enabled and authorship "
                "could not be verified (missing/invalid signature or revoked identity)."
            )

        # CAP-C5: optional bi-temporal validity window and honest supersedence.
        valid_from_raw = args.get("valid_from")
        valid_from = (
            None if valid_from_raw is None else _parse_iso_timestamp(valid_from_raw, field="valid_from")
        )
        valid_to_raw = args.get("valid_to")
        valid_to = None if valid_to_raw is None else _parse_iso_timestamp(valid_to_raw, field="valid_to")
        supersedes = args.get("supersedes")

        kwargs: dict = {
            "content": content,
            "layer": str(args["layer"]),
            "src": str(args["src"]),
            "written_by": written_by,
            "pipeline_id": pipeline_id,
            "base_trust": _trust_from_args(args),
            "written_at_drift": 0.0 if latest is None else latest.drift_score,
            "verified": verified,
            "valid_from": valid_from,
            "valid_to": valid_to,
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
        if infer_edges_enabled:
            # CAP-C7/WI-P2: write() ran deterministic similarity inference;
            # 0 is a valid, meaningful count (no similar recent chunk found).
            result["edges_inferred"] = getattr(store, "last_write_inferred_edge_count", 0)
        if raw_edges:
            edges = [
                ChunkEdge(
                    src_chunk_id=chunk.chunk_id,
                    dst_chunk_id=str(item["dst"]),
                    edge_type=item["type"],
                    weight=float(item.get("weight", 1.0)),
                    created_by=written_by,
                )
                for item in raw_edges
            ]
            result["edges_written"] = store.add_chunk_edges(edges)
        if signature is not None:
            result["verified"] = verified
        if fr.was_filtered:
            result["filtered"] = True
            result["reduction_ratio"] = round(fr.reduction_ratio, 3)
            if raw_ref is not None:
                result["raw_ref"] = raw_ref
        if supersedes is not None:
            # Honest supersedence: the old chunk is never deleted, only marked.
            # Its valid_to becomes this chunk's valid_from (or "now" if unset).
            resolved_old_valid_to = time.time() if valid_from is None else valid_from
            result["superseded"] = store.supersede(
                str(supersedes), chunk.chunk_id, valid_to=resolved_old_valid_to
            )
        return result

    def _handle_emit_whisper(args: dict[str, object]) -> object:
        whisper_type = str(args["type"])
        payload = _normalize_mcp_whisper_payload(whisper_type, str(args["payload"]))
        try:
            ttl_seconds = max(1, int(args.get("ttl_seconds", default_whisper_ttl)))
        except (TypeError, ValueError):
            ttl_seconds = default_whisper_ttl
        ref = args.get("ref")
        from_agent = str(args["from"])
        pipeline_id = args.get("pipeline_id")
        # CAP-T1/WI-013: sign whispers over (from | sha256(payload) | pipeline_id).
        signature = args.get("signature")
        verified = _verify_authorship(
            store,
            identity_id=from_agent,
            content=payload,
            pipeline_id=None if pipeline_id is None else str(pipeline_id),
            signature=None if signature is None else str(signature),
        )
        if require_signatures and not verified:
            raise ValueError(
                "ncp_emit_whisper rejected: require_signatures is enabled and authorship "
                "could not be verified (missing/invalid signature or revoked identity)."
            )
        whisper = Whisper(
            from_agent=from_agent,
            target=str(args["target"]),
            whisper_type=whisper_type,
            payload=payload,
            confidence=float(args["confidence"]),
            pipeline_id=pipeline_id,
            ttl_seconds=ttl_seconds,
            ref=None if ref is None else str(ref),
            verified=verified,
        )
        store.emit_whisper(whisper)
        result: dict[str, object] = {"emitted": True}
        if signature is not None:
            result["verified"] = verified
        if whisper_type == "dissent" and ref:
            result["dissent_recorded"] = store.record_dissent(str(ref))
        return result

    def _handle_post_turn(args: dict[str, object]) -> object:
        conscious = _build_conscious_from_args(store, args)
        assembler = Assembler(store=store, config=config)
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
        memory_chunks: list[SubconsciousChunk] = []
        for item in list(args.get("memory_chunks", []) or []):
            if not isinstance(item, dict):
                raise ValueError("ncp_post_turn: memory_chunks items must be objects")
            chunk_kwargs: dict = {
                "content": str(item["content"]),
                "layer": str(item["layer"]),
                "src": str(item["src"]),
                "written_by": str(item.get("written_by") or conscious.agent_id),
                "pipeline_id": conscious.pipeline_id,
            }
            if (chunk_id := item.get("chunk_id")):
                chunk_kwargs["chunk_id"] = str(chunk_id)
            memory_chunks.append(SubconsciousChunk(**chunk_kwargs))
        record = assembler.post_turn(
            conscious=conscious,
            response=response,
            result_summary=str(args["result_summary"]),
            result_full=str(args["result_full"]),
            memory_chunks=memory_chunks or None,
            ack_whisper_ids=ack_ids,
        )
        result: dict[str, object] = {"posted": True, "turn_id": record.turn_id, "acknowledged_whisper_ids": ack_ids}
        # WI-007(a): surface dedup-suppressed memory writes to the host.
        if record.suppressed_chunk_ids:
            result["suppressed_chunk_ids"] = record.suppressed_chunk_ids
        # CAP-E2: reflect spend *after* this turn's cost was just logged above.
        budget_snapshot = _budget_snapshot(pipeline_id=conscious.pipeline_id)
        if budget_snapshot is not None:
            result["budget"] = budget_snapshot.to_dict()
        return result

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
                session.last_access = time.monotonic()
                if pipeline_id is not None:
                    session.pipeline_id = str(pipeline_id)
                effective_pipeline_id = session.pipeline_id
                _prune_sessions()
        try:
            # Clamp to the schema max (4); a client asking for k=500 gets 4.
            k = min(4, max(1, int(args.get("k", 2))))
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

    def _handle_record_decision(args: dict[str, object]) -> object:
        decision = str(args["decision"])
        rationale = str(args["rationale"])
        agent_id = str(args["agent_id"])
        alternatives = [str(a) for a in (args.get("alternatives") or [])]
        evidence_refs = [str(r) for r in (args.get("evidence_refs") or [])]
        outcome = str(args.get("outcome", "pending"))
        confidence = float(args.get("confidence", 0.8) or 0.8)
        pipeline_id = args.get("pipeline_id")
        caused_by = args.get("caused_by")
        tags = [str(t) for t in (args.get("tags") or [])]

        alt_section = ""
        if alternatives:
            alt_section = "\nalternatives: " + " | ".join(alternatives)
        evidence_section = ""
        if evidence_refs:
            evidence_section = "\nevidence: " + " ".join(evidence_refs)
        tag_section = ""
        if tags:
            tag_section = "\ntags: " + " ".join(tags)

        content = (
            f"decision: {decision}\n"
            f"rationale: {rationale}"
            f"{alt_section}"
            f"\noutcome: {outcome}"
            f"{evidence_section}"
            f"{tag_section}"
        )
        if len(content) > 2000:
            content = content[:1997] + "..."

        chunk = SubconsciousChunk(
            layer="reasoning_trace",
            content=content,
            src="agent_inferred",
            written_by=agent_id,
            pipeline_id=pipeline_id,
            caused_by=None if caused_by is None else str(caused_by),
            base_trust=min(1.0, max(0.0, confidence)),
            source_refs=evidence_refs,
        )
        ok = store.write(chunk)
        return {
            "recorded": ok,
            "chunk_id": chunk.chunk_id,
            "outcome": outcome,
            "tag_count": len(tags),
            "evidence_count": len(evidence_refs),
        }

    def _handle_record_outcome(args: dict[str, object]) -> object:
        success_raw = args.get("success")
        if success_raw is None:
            raise ValueError("ncp_record_outcome requires 'success' (boolean)")
        if not isinstance(success_raw, bool):
            raise ValueError("ncp_record_outcome 'success' must be a boolean")
        success: bool = success_raw

        chunk_ids: list[str] = [str(c) for c in (args.get("chunk_ids") or [])]
        turn_id: str | None = args.get("turn_id")
        if not isinstance(turn_id, str) and turn_id is not None:
            raise ValueError("ncp_record_outcome 'turn_id' must be a string or null")
        outcome_id: str | None = args.get("outcome_id")
        if outcome_id is not None and not isinstance(outcome_id, str):
            raise ValueError("ncp_record_outcome 'outcome_id' must be a string or null")
        weight = float(args.get("weight", 1.0) or 1.0)
        note: str | None = args.get("note")
        if note is not None and not isinstance(note, str):
            raise ValueError("ncp_record_outcome 'note' must be a string or null")

        outcome = OutcomeRecord(
            outcome_id=outcome_id or f"out_{int(time.time() * 1000)}",
            turn_id=turn_id,
            chunk_ids=chunk_ids,
            success=success,
            weight=weight,
            note=note,
        )
        recorded = store.record_outcome(outcome)
        return {"recorded": recorded, "outcome_id": outcome.outcome_id}

    def _memo_counters() -> dict[str, int] | None:
        """Cheap hits/misses totals for hosts (S4.1 memoization telemetry)."""
        stats_fn = getattr(store, "memo_stats", None)
        if not callable(stats_fn):
            return None
        try:
            stats = stats_fn()
        except Exception:
            return None
        return {"hits": int(stats.get("hits", 0)), "misses": int(stats.get("misses", 0))}

    def _handle_lookup_memo(args: dict[str, object]) -> object:
        from ncp.stores.memo import compute_memo_signature
        sig = args.get("signature")
        if sig is None:
            task = str(args.get("task", ""))
            context = str(args.get("context", ""))
            sig = compute_memo_signature(task, context)
        sig = str(sig)
        # Use the telemetry-neutral peek here, not `store.lookup_memo` — the
        # latter unconditionally counts a hit as soon as a fresh row exists,
        # before the outcome/verified gating below runs. We only want to
        # count a hit once we know the memo will actually be returned as
        # usable; anything else (not found, stale, or gated) is a miss.
        memo = store.peek_memo(sig)
        result: dict[str, object] = {"found": False, "memo": None}
        cfg = config
        usable = False
        if memo is not None and (cfg is None or cfg.memoization_enabled):
            # Apply outcome and verified gating at the handler level
            gated = False
            if cfg is not None:
                if float(memo.get("outcome", 0.0)) < cfg.memoization_min_outcome:
                    gated = True
                elif not cfg.memoization_allow_unverified and not bool(memo.get("verified", 0)):
                    gated = True
            if not gated:
                usable = True
                result = {"found": True, "memo": dict(memo)}
        if usable:
            store.record_memo_hit(sig)
        else:
            store.record_memo_miss()
        counters = _memo_counters()
        if counters is not None:
            result["stats"] = counters
        return result

    def _handle_record_memo(args: dict[str, object]) -> object:
        from ncp.stores.memo import compute_memo_signature, validate_code_memo_ast
        task = str(args.get("task", ""))
        sig = args.get("signature")
        if sig is None:
            sig = compute_memo_signature(task)
        sig = str(sig)
        chunk_ids = [str(c) for c in list(args.get("chunk_ids", []) or [])]
        result_summary = args.get("result_summary")
        if result_summary is not None:
            result_summary = str(result_summary)
        kind = args.get("kind")
        if kind is not None:
            kind = str(kind)
            if not validate_code_memo_ast(result_summary or "", kind):
                return {
                    "recorded": False,
                    "signature": sig,
                    "error": (
                        f"result_summary does not parse as Python matching declared kind {kind!r}"
                    ),
                }
        output_tokens_est: int | None = None
        raw_tokens = args.get("output_tokens_est")
        if raw_tokens is not None:
            try:
                output_tokens_est = max(0, int(raw_tokens))  # type: ignore[arg-type]
            except (TypeError, ValueError):
                output_tokens_est = None
        recorded = store.record_memo(
            sig, task, chunk_ids, result_summary, output_tokens_est
        )
        return {"recorded": recorded, "signature": sig}

    # Expose the in-memory fetch-session table for observability/testing of the
    # LRU+TTL pruning without widening the returned handler mapping.
    _handle_get_context.fetch_sessions = sessions  # type: ignore[attr-defined]

    return {
        "ncp_get_context": _handle_get_context,
        "ncp_write_memory": _handle_write_memory,
        "ncp_emit_whisper": _handle_emit_whisper,
        "ncp_post_turn": _handle_post_turn,
        "ncp_fetch": _handle_fetch,
        "ncp_record_decision": _handle_record_decision,
        "ncp_record_outcome": _handle_record_outcome,
        "ncp_lookup_memo": _handle_lookup_memo,
        "ncp_record_memo": _handle_record_memo,
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


def _parse_iso_timestamp(value: object, *, field: str) -> float:
    """CAP-C5: parse an ISO-8601 timestamp arg into epoch seconds.

    Accepts a bare epoch number too (int/float or numeric string) so callers
    that already track epoch time don't need to round-trip through ISO.
    """
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip()
    try:
        return float(text)
    except ValueError:
        pass
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp()
    except ValueError as exc:
        raise ValueError(f"{field} must be an ISO-8601 timestamp or epoch seconds: {value!r}") from exc


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
