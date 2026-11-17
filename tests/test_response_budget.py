"""ncp_get_context final response budgets and compact telemetry (handoff item 8)."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path

import pytest

from ncp.config import load_config
from ncp.mcp.response_budget import compact_telemetry, fit_response, response_tokens
from ncp.middleware.base import Middleware
from ncp.mcp.server import StreamResponse, _handle_request, make_handlers
from ncp.stores.sqlite import SQLiteStore
from ncp.types import SubconsciousChunk

PIPELINE = "pipe_resp_budget"
REQUIRED = ("context", "session_id", "pending_whisper_ids", "retrieved_chunk_ids")


# ── fit_response units ──────────────────────────────────────────────────────


def _annotated_response() -> dict:
    return {
        "context": "[NCP:CONSCIOUS] " + "ctx " * 40,
        "session_id": "s1",
        "pending_whisper_ids": ["w1"],
        "retrieved_chunk_ids": ["c1", "c2"],
        "context_policy": {"name": "p", "version": 1, "sha256": "ab" * 32},
        "telemetry": {
            "evicted_high_relevance": [{"chunk_id": f"e{i}", "relevance": 0.9} for i in range(30)],
            "evicted_whispers": [{"whisper_id": f"x{i}", "confidence": 0.8} for i in range(10)],
            "fanin_contradictions": [{"chunk_a": "c1", "chunk_b": "c2"}],
            "evicted_chunk_count": 30,
        },
        "tier_hint": "standard",
        "complexity_signal": 0.4,
        "factors": {"a": 1, "b": 2},
        "budget": {"status": "ok", "spent_usd": 0.1},
    }


def test_fitting_response_is_returned_unchanged() -> None:
    response = _annotated_response()
    size = response_tokens(response)

    assert fit_response(response, size) is response  # exact budget
    assert "truncated" not in fit_response(response, size + 10)


def test_one_token_overflow_trims_and_fits() -> None:
    response = _annotated_response()
    size = response_tokens(response)

    fitted = fit_response(response, size - 1)

    assert response_tokens(fitted) <= size - 1
    assert fitted["truncated"]["fits"] is True
    assert fitted["truncated"]["dropped"] == ["telemetry_lists"]
    assert all(fitted[key] == response[key] for key in REQUIRED)
    assert response["telemetry"]["evicted_high_relevance"]  # input not mutated


def test_annotation_heavy_response_drops_diagnostics_before_evidence() -> None:
    response = _annotated_response()
    required_only = {k: response[k] for k in (*REQUIRED, "context_policy")}
    budget = response_tokens({**required_only, "truncated": {"dropped": [], "fits": True}}) + 30

    fitted = fit_response(response, budget)

    assert fitted["truncated"]["fits"] is True
    assert "telemetry" not in fitted
    assert all(fitted[key] == response[key] for key in (*REQUIRED, "context_policy"))
    assert "context_max_tokens" not in fitted["truncated"]  # no context cut was needed


def test_reports_not_fitting_when_required_fields_exceed_budget() -> None:
    fitted = fit_response(_annotated_response(), 5)

    assert fitted["truncated"]["fits"] is False
    assert all(key in fitted for key in REQUIRED)


def test_invalid_budget_rejected() -> None:
    with pytest.raises(ValueError):
        fit_response(_annotated_response(), 0)


def test_compact_telemetry_keeps_signal_only() -> None:
    compact = compact_telemetry({
        "evicted_high_relevance": [{"chunk_id": "e"}],
        "evicted_chunk_count": 3,
        "evicted_whisper_count": 0,
        "retrieval_used_fallback": False,
        "fetch_budget_remaining": 2,
        "fetch_hint": "ncp_fetch",
        "fanin_contradictions": [{"chunk_a": "a", "chunk_b": "b"}],
        "active_features": {"x": True},
    })
    assert compact == {
        "evicted_chunk_count": 3,
        "fetch_budget_remaining": 2,
        "fetch_hint": "ncp_fetch",
        "fanin_contradictions": [{"chunk_a": "a", "chunk_b": "b"}],
    }


# ── ncp_get_context integration ─────────────────────────────────────────────


def _store(tmp_path: Path) -> SQLiteStore:
    store = SQLiteStore(tmp_path / "ncp.db")
    for i in range(6):
        store.write(SubconsciousChunk(
            chunk_id=f"rb_{i}",
            content=f"payment retry budget marker{i} " + " ".join(f"tok{i}_{j}" for j in range(40)),
            layer="semantic",
            src="tool_result",
            pipeline_id=PIPELINE,
        ))
    return store


def _get_context(handlers, **extra: object) -> dict:
    args = {
        "agent_id": "executor", "role": "build", "owns": [], "must_not": [],
        "task": "payment_retry", "slot": "budget", "intent": "fit", "pipeline_id": PIPELINE,
        "max_tokens": 2000, **extra,
    }
    request = {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
               "params": {"name": "ncp_get_context", "arguments": args}}
    response = json.loads(_handle_request(request, handlers))
    assert "error" not in response, response
    return json.loads(response["result"]["content"][0]["text"])


def _retrieval_counts(store: SQLiteStore) -> dict[str, int]:
    with sqlite3.connect(store.path) as conn:
        return dict(conn.execute("SELECT chunk_id, retrieval_count FROM chunks").fetchall())


def test_no_budget_or_compact_leaves_response_unchanged(tmp_path: Path) -> None:
    result = _get_context(make_handlers(_store(tmp_path)))
    assert "truncated" not in result and "context_sha256" not in result
    assert "evicted_high_relevance" in result["telemetry"]


def test_response_budget_refits_context_and_keeps_evidence_consistent(tmp_path: Path) -> None:
    full_store = _store(tmp_path / "full")
    full = _get_context(make_handlers(full_store))
    budget = response_tokens(full) // 2

    store = _store(tmp_path / "fit")
    fitted = _get_context(make_handlers(store), response_max_tokens=budget)

    assert fitted["truncated"]["fits"] is True
    assert response_tokens(fitted) <= budget
    assert "context_max_tokens" in fitted["truncated"]
    assert 0 < len(fitted["retrieved_chunk_ids"]) < len(full["retrieved_chunk_ids"])
    # Every reported evidence id is really in the (smaller) context, and vice versa.
    in_context = [cid for cid in full["retrieved_chunk_ids"] if f"marker{cid[-1]} " in fitted["context"]]
    assert sorted(in_context) == sorted(fitted["retrieved_chunk_ids"])
    # Re-fitting never retrieves again: retrieval feedback counts match one assembly.
    assert _retrieval_counts(store) == _retrieval_counts(full_store)


def test_compact_mode(tmp_path: Path) -> None:
    result = _get_context(make_handlers(_store(tmp_path)), compact=True)

    assert result["context_sha256"] == hashlib.sha256(result["context"].encode("utf-8")).hexdigest()
    assert "evicted_high_relevance" not in result["telemetry"]
    assert "active_features" not in result["telemetry"]
    assert result["retrieved_chunk_ids"]


@pytest.mark.parametrize("bad", [{"compact": "yes"}, {"response_max_tokens": 0}])
def test_invalid_arguments_rejected(tmp_path: Path, bad: dict) -> None:
    args = {"agent_id": "executor", "role": "build", "task": "t", "slot": "s", "intent": "i", **bad}
    request = {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
               "params": {"name": "ncp_get_context", "arguments": args}}
    assert "error" in json.loads(_handle_request(request, make_handlers(_store(tmp_path))))


@pytest.mark.parametrize("compact", [False, True])
def test_stream_budget_refits_before_emitting_sections(tmp_path: Path, compact: bool) -> None:
    full_store = _store(tmp_path / "full_stream")
    full = _get_context(make_handlers(full_store), compact=compact)
    budget = response_tokens(full) * 3 // 4 if compact else response_tokens(full) // 2
    store = _store(tmp_path / "fit_stream")
    handler = make_handlers(store)["ncp_get_context"]
    result = handler({
        "agent_id": "executor", "role": "build", "owns": [], "must_not": [],
        "task": "payment_retry", "slot": "budget", "intent": "fit", "pipeline_id": PIPELINE,
        "max_tokens": 2000, "response_max_tokens": budget, "stream": True, "compact": compact,
    })
    assert isinstance(result, StreamResponse)
    response = result.handler_result
    assert response["truncated"]["fits"] is True
    assert response_tokens(response) <= budget
    assert "context_max_tokens" in response["truncated"]
    assert "\n\n".join(text for _, text in result.sections) == response["context"]
    assert 0 < len(response["retrieved_chunk_ids"]) < len(full["retrieved_chunk_ids"])
    in_context = [cid for cid in full["retrieved_chunk_ids"]
                  if f"marker{cid[-1]} " in response["context"]]
    assert sorted(in_context) == sorted(response["retrieved_chunk_ids"])
    assert _retrieval_counts(store) == _retrieval_counts(full_store)
    if compact:
        assert response["context_sha256"] == hashlib.sha256(response["context"].encode()).hexdigest()


def test_stream_impossible_budget_reports_failure_without_losing_required_fields(tmp_path: Path) -> None:
    result = make_handlers(_store(tmp_path))["ncp_get_context"]({
        "agent_id": "executor", "role": "build", "task": "payment_retry", "slot": "budget",
        "intent": "fit", "pipeline_id": PIPELINE, "stream": True, "response_max_tokens": 1,
    })
    assert isinstance(result, StreamResponse)
    assert result.handler_result["truncated"]["fits"] is False
    assert all(key in result.handler_result for key in REQUIRED)
    assert "\n\n".join(text for _, text in result.sections) == result.handler_result["context"]


def test_configured_compact_default_preserves_context_and_evidence(tmp_path: Path) -> None:
    config = load_config(cwd=tmp_path)
    handlers = make_handlers(_store(tmp_path), config=config)
    compact = _get_context(handlers)
    full = _get_context(handlers, compact=False)
    assert compact["context"] == full["context"]
    assert all(compact[key] == full[key] for key in REQUIRED)
    assert response_tokens(compact) < response_tokens(full)
    assert "active_features" not in compact["telemetry"]
    assert "active_features" in full["telemetry"]
    assert compact["context_sha256"] == hashlib.sha256(compact["context"].encode()).hexdigest()
    config.values.setdefault("tools", {})["compact_default"] = False
    configured_full = _get_context(make_handlers(_store(tmp_path / "full_default"), config=config))
    assert "context_sha256" not in configured_full
    assert "active_features" in configured_full["telemetry"]


# ── stream sections vs post_assemble middleware ─────────────────────────────


class _Rewrite(Middleware):
    def post_assemble(self, context: str) -> str | None:
        return context.replace("marker", "MARK") + "\n[footer]"


class _NoOp(Middleware):
    def post_assemble(self, context: str) -> str | None:
        return None


def _stream_with_middleware(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, mw: object,
                            **extra: object) -> StreamResponse:
    import ncp.mcp.server as server_module
    from ncp.middleware.base import MiddlewarePipeline

    base = server_module.Assembler

    class _WithMiddleware(base):  # type: ignore[valid-type, misc]
        def __init__(self, *args: object, **kwargs: object) -> None:
            super().__init__(*args, middleware=MiddlewarePipeline([mw]), **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(server_module, "Assembler", _WithMiddleware)
    result = make_handlers(_store(tmp_path))["ncp_get_context"]({
        "agent_id": "executor", "role": "build", "owns": [], "must_not": [],
        "task": "payment_retry", "slot": "budget", "intent": "fit", "pipeline_id": PIPELINE,
        "max_tokens": 2000, "stream": True, **extra,
    })
    assert isinstance(result, StreamResponse)
    return result


@pytest.mark.parametrize("fit", [False, True])
def test_stream_with_rewriting_middleware_joins_to_final_context(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, fit: bool,
) -> None:
    extra: dict[str, object] = {}
    if fit:
        full = _get_context(make_handlers(_store(tmp_path / "full")))
        extra["response_max_tokens"] = response_tokens(full) // 2
    result = _stream_with_middleware(monkeypatch, tmp_path / "mw", _Rewrite(), **extra)
    context = result.handler_result["context"]
    assert context.endswith("[footer]") and "MARK" in context
    assert "\n\n".join(text for _, text in result.sections) == context
    assert [label for label, _ in result.sections] == ["context"]


@pytest.mark.parametrize("fit", [False, True])
def test_stream_with_noop_middleware_keeps_per_section_output(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, fit: bool,
) -> None:
    extra: dict[str, object] = {}
    if fit:
        full = _get_context(make_handlers(_store(tmp_path / "full")))
        extra["response_max_tokens"] = response_tokens(full) // 2
    result = _stream_with_middleware(monkeypatch, tmp_path / "mw", _NoOp(), **extra)
    assert "\n\n".join(text for _, text in result.sections) == result.handler_result["context"]
    labels = [label for label, _ in result.sections]
    assert labels[0] == "conscious" and labels[-1] == "budget_header"
