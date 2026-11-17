"""Cross-turn delta: ncp_get_context(delta=true) does not resend held chunks."""

from __future__ import annotations

import json
import re
from pathlib import Path

from ncp.mcp.server import MCP_TOOLS, _handle_request, make_handlers
from ncp.stores.sqlite import SQLiteStore
from ncp.types import SubconsciousChunk, TurnRecord

_SEEN_RE = re.compile(r"^chunk:(\S+) seen:1$", re.MULTILINE)


def _call(handlers: dict, arguments: dict) -> dict:
    raw = _handle_request(
        {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
         "params": {"name": "ncp_get_context", "arguments": arguments}},
        handlers,
        tools=MCP_TOOLS,
    )
    payload = json.loads(raw)
    assert "error" not in payload, payload
    return json.loads(payload["result"]["content"][0]["text"])


def _args(agent: str = "alice", **extra: object) -> dict:
    return {"agent_id": agent, "role": "build", "task": "payment_retry", "slot": "retry",
            "intent": "fix", "pipeline_id": "p", "session_id": f"s_{agent}", "compact": False, **extra}


def _store(tmp_path: Path, facts: int = 4) -> SQLiteStore:
    store = SQLiteStore(tmp_path / "ncp.db")
    for i in range(facts):
        store.write(SubconsciousChunk(
            chunk_id=f"c{i}", layer="semantic", src="tool_result", pipeline_id="p", written_by=f"w{i}",
            content=f"payment retry fact {i}: " + "backoff jitter detail " * 20,
        ))
    return store


def _chunk_chars(context: str) -> int:
    body = context.split("[NCP:SUBCONSCIOUS]", 1)[1].split("[NCP:BUDGET]", 1)[0]
    return len(body)


def test_ten_turn_run_resends_headers_only_until_delta_false_reset(tmp_path: Path) -> None:
    handlers = make_handlers(_store(tmp_path))
    first = _call(handlers, _args(delta=True))
    assert "seen:1" not in first["context"]
    full_chars = _chunk_chars(first["context"])
    assert full_chars > 400
    ids = first["retrieved_chunk_ids"]
    assert len(ids) == 4

    for _ in range(9):
        ctx = _call(handlers, _args(delta=True))
        assert sorted(_SEEN_RE.findall(ctx["context"])) == sorted(ids)
        assert "backoff jitter" not in ctx["context"]
        assert _chunk_chars(ctx["context"]) < full_chars / 5
        assert sorted(ctx["retrieved_chunk_ids"]) == sorted(ids)
        assert ctx["telemetry"]["delta"] == {"applied": True, "seen": 4}

    reset = _call(handlers, _args(delta=False))
    assert "seen:1" not in reset["context"]
    assert _chunk_chars(reset["context"]) == full_chars
    # The reset call is itself a delivery: the next delta call is header-only again.
    after = _call(handlers, _args(delta=True))
    assert len(_SEEN_RE.findall(after["context"])) == 4


def test_seen_line_is_header_only_and_frees_budget(tmp_path: Path) -> None:
    handlers = make_handlers(_store(tmp_path, facts=6))
    first = _call(handlers, _args(delta=True, max_tokens=420, k=6))
    shown_first = len(first["retrieved_chunk_ids"])
    second = _call(handlers, _args(delta=True, max_tokens=420, k=6))
    assert len(second["retrieved_chunk_ids"]) > shown_first
    assert second["context"].count("seen:1") == shown_first
    assert second["retrieved_chunk_ids"][:shown_first] == first["retrieved_chunk_ids"]


def test_changed_content_is_resent_in_full(tmp_path: Path) -> None:
    store = _store(tmp_path)
    handlers = make_handlers(store)
    _call(handlers, _args(delta=True))
    with store._connect() as connection:
        connection.execute("UPDATE chunks SET content = ? WHERE chunk_id = 'c1'", ("payment retry CHANGED text",))
    ctx = _call(handlers, _args(delta=True))
    assert "payment retry CHANGED text" in ctx["context"]
    assert "chunk:c1 seen:1" not in ctx["context"]
    assert "chunk:c0 seen:1" in ctx["context"]


def test_default_session_never_uses_delta(tmp_path: Path) -> None:
    handlers = make_handlers(_store(tmp_path))
    args = {k: v for k, v in _args(delta=True).items() if k not in ("session_id", "agent_id", "pipeline_id")}
    args["agent_id"] = ""
    first = _call(handlers, args)
    second = _call(handlers, args)
    assert first["session_id"] == "__default__"
    assert "seen:1" not in second["context"]
    assert second["telemetry"]["delta"] == {"applied": False, "reason": "default_session"}
    assert second["retrieved_chunk_ids"] == first["retrieved_chunk_ids"]


def test_new_session_id_resets_delivered_state(tmp_path: Path) -> None:
    handlers = make_handlers(_store(tmp_path))
    _call(handlers, _args(delta=True))
    assert "seen:1" in _call(handlers, _args(delta=True))["context"]
    assert "seen:1" not in _call(handlers, _args(delta=True, session_id="other"))["context"]


def test_ttl_expiry_resets_delivered_state(tmp_path: Path, monkeypatch) -> None:
    import ncp.mcp.server as server

    handlers = make_handlers(_store(tmp_path))
    _call(handlers, _args(delta=True))
    real = server.time.monotonic
    monkeypatch.setattr(server.time, "monotonic", lambda: real() + server._FETCH_SESSION_TTL_SECONDS + 5)
    assert "seen:1" not in _call(handlers, _args(delta=True))["context"]


def test_delivered_state_is_bounded(tmp_path: Path, monkeypatch) -> None:
    import ncp.mcp.server as server

    monkeypatch.setattr(server, "_DELIVERED_MAX", 2)
    handlers = make_handlers(_store(tmp_path))
    _call(handlers, _args(delta=True))
    ctx = _call(handlers, _args(delta=True))
    assert len(_SEEN_RE.findall(ctx["context"])) == 2


def test_own_turn_recent_refs_skipped_but_others_kept(tmp_path: Path) -> None:
    store = _store(tmp_path, facts=1)
    for turn_id, agent in (("t_own", "alice"), ("t_other", "bob")):
        store.log_turn_record(TurnRecord(
            turn_id=turn_id, agent_id=agent, pipeline_id="p", task="payment_retry", slot="retry",
            result=f"payment retry result from {agent}", result_full=f"payment retry result from {agent}",
        ))
    handlers = make_handlers(store)
    recent = ["r:sub/t_own", "r:sub/t_other"]
    plain = _call(handlers, _args(recent=recent))["context"]
    assert "result from alice" in plain and "result from bob" in plain
    delta = _call(handlers, _args(recent=recent, delta=True, session_id="fresh"))["context"]
    assert "result from alice" not in delta
    assert "result from bob" in delta


def test_stable_order_with_new_chunk_appended(tmp_path: Path) -> None:
    store = _store(tmp_path, facts=3)
    handlers = make_handlers(store)
    first = _call(handlers, _args(delta=True))
    order_first = re.findall(r"^chunk:(\S+)", first["context"], re.MULTILINE)
    store.write(SubconsciousChunk(
        chunk_id="c_new", layer="semantic", src="tool_result", pipeline_id="p", written_by="wn",
        content="payment retry fact new: highest ranked " + "backoff " * 10, trust_score=1.0,
    ))
    second = _call(handlers, _args(delta=True))
    order_second = re.findall(r"^chunk:(\S+)", second["context"], re.MULTILINE)
    assert order_second[: len(order_first)] == order_first
    assert order_second[len(order_first):] == ["c_new"]
    assert "chunk:c_new seen:1" not in second["context"]
    # Without delta the order is the ranking's: the new top-ranked chunk is first.
    plain = _call(handlers, _args(session_id="plain"))
    assert re.findall(r"^chunk:(\S+)", plain["context"], re.MULTILINE)[0] == "c_new"


def test_retrieved_ids_complete_and_held_for_post_turn(tmp_path: Path) -> None:
    store = _store(tmp_path)
    handlers = make_handlers(store)
    first = _call(handlers, _args(delta=True))
    second = _call(handlers, _args(delta=True, echo_ids=False))
    assert "retrieved_chunk_ids" not in second
    # The server-held ids (what ncp_post_turn credits) still include seen chunks.
    held = _call(handlers, _args(delta=True))
    assert sorted(held["retrieved_chunk_ids"]) == sorted(first["retrieved_chunk_ids"])
    links: list[tuple] = []
    real = store.link_turn_chunks
    store.link_turn_chunks = lambda turn_id, ids, *, relation: (links.append((tuple(ids), relation)), real(turn_id, ids, relation=relation))[1]  # type: ignore[method-assign]
    raw = _handle_request(
        {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "ncp_post_turn", "arguments": {
            "agent_id": "alice", "role": "build", "task": "payment_retry", "slot": "retry", "intent": "fix",
            "pipeline_id": "p", "session_id": "s_alice", "turn_id": "turn_x", "result_summary": "done"}}},
        handlers, tools=MCP_TOOLS,
    )
    assert "error" not in json.loads(raw)
    assert [(sorted(ids), rel) for ids, rel in links] == [(sorted(first["retrieved_chunk_ids"]), "retrieved")]


def test_non_delta_calls_unchanged_by_prior_delivery(tmp_path: Path) -> None:
    handlers = make_handlers(_store(tmp_path))
    a = _call(handlers, _args(session_id="x"))
    b = _call(handlers, _args(session_id="x"))
    assert a["context"] == b["context"]
    assert "seen:1" not in b["context"]
    assert "delta" not in b["telemetry"]


def test_over_budget_telemetry_only_when_true(tmp_path: Path) -> None:
    handlers = make_handlers(_store(tmp_path))
    ok = _call(handlers, _args(compact=True))
    assert "over_budget" not in ok["telemetry"]
    tiny = _call(handlers, _args(compact=True, max_tokens=5))
    assert tiny["telemetry"].get("over_budget") is True
    full = _call(handlers, _args(compact=False, max_tokens=5))
    assert full["telemetry"].get("over_budget") is True


def test_delta_must_be_boolean(tmp_path: Path) -> None:
    handlers = make_handlers(_store(tmp_path))
    raw = _handle_request(
        {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
         "params": {"name": "ncp_get_context", "arguments": _args(delta="yes")}},
        handlers, tools=MCP_TOOLS,
    )
    assert "delta" in json.loads(raw)["error"]["message"]
