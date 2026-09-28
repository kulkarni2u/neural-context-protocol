"""Regression: turn-based outcomes resolve to the chunks a turn wrote/retrieved."""

from __future__ import annotations

import json
import sqlite3

import anyio
import pytest

from ncp.assembler import Assembler
from ncp.config import load_config
from ncp.mcp.server import _handle_request, make_handlers
from ncp.stores.sqlite import SQLiteStore
from ncp.types import ConsciousBlock, NCPResponse, OutcomeRecord, SubconsciousChunk


def _conscious() -> ConsciousBlock:
    return ConsciousBlock(
        agent_id="builder", role="build", task="fix_bug", slot="payment",
        intent="advance", owns=["payment"], must_not=["deploy"], pipeline_id="pipe_1",
    )


def _response(turn_id: str) -> NCPResponse:
    return NCPResponse(
        content="done", turn_id=turn_id, pipeline_id="pipe_1", model="m",
        input_tokens=1, output_tokens=1, cost_usd=0.0, latency_ms=1,
    )


def _chunk(text: str, chunk_id: str | None = None) -> SubconsciousChunk:
    kwargs = {"chunk_id": chunk_id} if chunk_id else {}
    return SubconsciousChunk(
        content=text, layer="semantic", src="tool_result", written_by="builder",
        pipeline_id="pipe_1", base_trust=0.5, **kwargs,
    )


def _post(assembler: Assembler, turn_id: str, **kw) -> None:
    assembler.post_turn(
        conscious=_conscious(), response=_response(turn_id),
        result_summary="s", result_full="f", **kw,
    )


def _outcome(store: SQLiteStore, **kw) -> OutcomeRecord:
    rec = OutcomeRecord(outcome_id=f"out_{kw.get('turn_id')}", success=True, weight=0.2, **kw)
    assert store.record_outcome(rec)
    got = store.get_outcome(rec.outcome_id)
    assert got is not None
    return got


def _trust(store: SQLiteStore, chunk_id: str) -> float:
    with store._connect() as conn:
        return float(conn.execute("SELECT base_trust FROM chunks WHERE chunk_id = ?", (chunk_id,)).fetchone()[0])


def test_post_turn_written_chunk_resolves_and_calibrates(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "ncp.db")
    assembler = Assembler(store=store, config=load_config())
    chunk = _chunk("payment retry uses exponential backoff with jitter")
    _post(assembler, "turn_a", memory_chunks=[chunk])

    got = _outcome(store, turn_id="turn_a")
    assert got.chunk_ids == [chunk.chunk_id]

    assert _trust(store, chunk.chunk_id) == pytest.approx(0.5)
    store.calibrate(feedback_mode=True)
    assert _trust(store, chunk.chunk_id) == pytest.approx(0.7, abs=0.05)


def test_retrieved_chunk_ids_linked_and_resolved(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "ncp.db")
    assembler = Assembler(store=store, config=load_config())
    store.write(_chunk("older fact about ledgers", chunk_id="sub_old"))
    new = _chunk("brand new insight about idempotency keys")
    _post(assembler, "turn_b", memory_chunks=[new], retrieved_chunk_ids=["sub_old", "sub_old"])

    got = _outcome(store, turn_id="turn_b")
    assert got.chunk_ids == [new.chunk_id, "sub_old"]  # wrote first, de-duplicated


def test_suppressed_duplicate_not_linked(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "ncp.db")
    assembler = Assembler(store=store, config=load_config())
    _post(assembler, "turn_1", memory_chunks=[_chunk("identical content about webhook signature verification")])
    _post(assembler, "turn_2", memory_chunks=[_chunk("identical content about webhook signature verification")])
    assert _outcome(store, turn_id="turn_2").chunk_ids == []


def test_legacy_caused_by_still_resolves_and_unions(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "ncp.db")
    assembler = Assembler(store=store, config=load_config())
    legacy = _chunk("legacy linked chunk about refunds", chunk_id="sub_legacy")
    legacy = legacy.model_copy(update={"caused_by": "turn_c"})
    store.write(legacy)
    assert _outcome(store, turn_id="turn_c").chunk_ids == ["sub_legacy"]

    fresh = _chunk("fresh chunk about chargebacks")
    _post(assembler, "turn_c", memory_chunks=[fresh], retrieved_chunk_ids=["sub_legacy"])
    store2_outcome = OutcomeRecord(outcome_id="out_c2", turn_id="turn_c", success=True)
    store.record_outcome(store2_outcome)
    assert store.get_outcome("out_c2").chunk_ids == [fresh.chunk_id, "sub_legacy"]


def test_link_turn_chunks_validates_and_is_idempotent(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "ncp.db")
    with pytest.raises(ValueError):
        store.link_turn_chunks("t", ["c"], relation="bogus")
    assert store.link_turn_chunks("t", ["c1", "c2"], relation="wrote") == 2
    assert store.link_turn_chunks("t", ["c1"], relation="wrote") == 0
    assert store.link_turn_chunks("t", ["c1"], relation="retrieved") == 1
    assert store.link_turn_chunks("t", [], relation="wrote") == 0


def test_turn_chunks_table_added_to_existing_db(tmp_path) -> None:
    path = tmp_path / "old.db"
    SQLiteStore(path)
    with sqlite3.connect(path) as conn:
        conn.execute("DROP TABLE turn_chunks")
    store = SQLiteStore(path)
    assert store.link_turn_chunks("t", ["c"], relation="wrote") == 1


def test_post_turn_async_links_written_and_retrieved(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "ncp.db")
    assembler = Assembler(store=store, config=load_config())
    store.write(_chunk("ctx chunk", chunk_id="sub_ctx"))
    chunk = _chunk("async written chunk about reconciliation jobs")

    async def run() -> None:
        await assembler.post_turn_async(
            conscious=_conscious(), response=_response("turn_async"),
            result_summary="s", result_full="f",
            memory_chunks=[chunk], retrieved_chunk_ids=["sub_ctx"],
        )

    anyio.run(run)
    assert _outcome(store, turn_id="turn_async").chunk_ids == [chunk.chunk_id, "sub_ctx"]


def _call(name: str, arguments: dict) -> dict:
    return {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
            "params": {"name": name, "arguments": arguments}}


def _content(resp: dict) -> dict:
    return json.loads(json.loads(resp)["result"]["content"][0]["text"])


def test_mcp_post_turn_then_record_outcome_by_turn_id(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "ncp.db")
    store.write(_chunk("previously stored knowledge", chunk_id="sub_prev"))
    handlers = make_handlers(store)
    base = {"agent_id": "builder", "role": "build", "task": "fix_bug", "slot": "payment",
            "intent": "advance", "pipeline_id": "pipe_1"}

    ctx = _content(_handle_request(_call("ncp_get_context", base), handlers))
    assert isinstance(ctx["retrieved_chunk_ids"], list)

    posted = _content(_handle_request(_call("ncp_post_turn", {
        **base, "turn_id": "turn_mcp", "result_summary": "s", "result_full": "f",
        "memory_chunks": [{"content": "mcp written fact about tax rounding", "layer": "semantic",
                          "src": "tool_result", "chunk_id": "sub_mcp_new"}],
        "retrieved_chunk_ids": ["sub_prev"],
    }), handlers))
    assert posted["posted"] is True

    out = _content(_handle_request(_call("ncp_record_outcome", {
        "turn_id": "turn_mcp", "success": True, "outcome_id": "out_mcp"}), handlers))
    assert out["recorded"] is True
    assert store.get_outcome("out_mcp").chunk_ids == ["sub_mcp_new", "sub_prev"]
