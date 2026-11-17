"""Server-held ids: ncp_post_turn defaults to the last ncp_get_context ids."""

from __future__ import annotations

import json
from pathlib import Path

from ncp.mcp.server import MCP_TOOLS, _handle_request, make_handlers
from ncp.stores.sqlite import SQLiteStore
from ncp.types import SubconsciousChunk, Whisper


def _raw(handlers: dict, name: str, arguments: dict) -> dict:
    raw = _handle_request(
        {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": name, "arguments": arguments}},
        handlers,
        tools=MCP_TOOLS,
    )
    return json.loads(raw)


def _call(handlers: dict, name: str, arguments: dict) -> dict:
    payload = _raw(handlers, name, arguments)
    assert "error" not in payload, payload
    return json.loads(payload["result"]["content"][0]["text"])


def _args(agent: str, **extra: object) -> dict:
    return {"agent_id": agent, "role": "build", "task": "deploy_pipeline", "slot": "deploy",
            "intent": "i", "pipeline_id": "pipe_1", **extra}


def _setup(tmp_path: Path):
    store = SQLiteStore(tmp_path / "s.db")
    store.write(SubconsciousChunk(
        chunk_id="sub_deploy", layer="semantic", content="deploy pipeline runs migrations first",
        src="tool_result", pipeline_id="pipe_1",
    ))
    for name in ("alice", "bob"):
        store.emit_whisper(Whisper(
            whisper_id=f"wsp_{name}", from_agent="planner", target=name, whisper_type="share",
            payload='{"ask":"x"}', confidence=0.9, pipeline_id="pipe_1",
        ))
    return store, make_handlers(store)


def _record_links(store: SQLiteStore) -> list[tuple]:
    calls: list[tuple] = []
    real = store.link_turn_chunks

    def spy(turn_id, chunk_ids, *, relation):  # type: ignore[no-untyped-def]
        calls.append((turn_id, tuple(chunk_ids), relation))
        return real(turn_id, chunk_ids, relation=relation)

    store.link_turn_chunks = spy  # type: ignore[method-assign]
    return calls


def test_omitted_ids_ack_same_whispers_and_credit_same_chunks(tmp_path: Path) -> None:
    store, handlers = _setup(tmp_path)
    links = _record_links(store)
    ctx = _call(handlers, "ncp_get_context", _args("alice"))
    assert ctx["pending_whisper_ids"] == ["wsp_alice"]
    assert ctx["retrieved_chunk_ids"] == ["sub_deploy"]
    posted = _call(handlers, "ncp_post_turn", _args("alice", turn_id="turn_alice", result_summary="done"))
    assert posted["acknowledged_whisper_ids"] == ["wsp_alice"]
    assert store.peek_whispers(agent_id="alice", pipeline_id="pipe_1") == []

    (tmp_path / "x").mkdir()
    store2, handlers2 = _setup(tmp_path / "x")
    links2 = _record_links(store2)
    _call(handlers2, "ncp_get_context", _args("alice"))
    _call(handlers2, "ncp_post_turn", _args(
        "alice", turn_id="turn_alice", result_summary="done",
        ack_whisper_ids=["wsp_alice"], retrieved_chunk_ids=["sub_deploy"]))
    assert links == links2 == [("turn_alice", ("sub_deploy",), "retrieved")]
    assert store2.peek_whispers(agent_id="alice", pipeline_id="pipe_1") == []


def test_explicit_empty_arrays_mean_none(tmp_path: Path) -> None:
    store, handlers = _setup(tmp_path)
    _call(handlers, "ncp_get_context", _args("alice"))
    posted = _call(handlers, "ncp_post_turn", _args(
        "alice", result_summary="done", ack_whisper_ids=[], retrieved_chunk_ids=[]))
    assert posted["acknowledged_whisper_ids"] == []
    assert [w.whisper_id for w in store.peek_whispers(agent_id="alice", pipeline_id="pipe_1")] == ["wsp_alice"]


def test_no_cross_agent_ack(tmp_path: Path) -> None:
    store, handlers = _setup(tmp_path)
    _call(handlers, "ncp_get_context", _args("alice"))
    _call(handlers, "ncp_get_context", _args("bob"))
    posted = _call(handlers, "ncp_post_turn", _args("alice", result_summary="done"))
    assert posted["acknowledged_whisper_ids"] == ["wsp_alice"]
    assert [w.whisper_id for w in store.peek_whispers(agent_id="bob", pipeline_id="pipe_1")] == ["wsp_bob"]
    # bob posts without ever seeing alice's ids
    posted_bob = _call(handlers, "ncp_post_turn", _args("bob", result_summary="done"))
    assert posted_bob["acknowledged_whisper_ids"] == ["wsp_bob"]


def test_post_turn_without_prior_context_defaults_to_none(tmp_path: Path) -> None:
    _store, handlers = _setup(tmp_path)
    posted = _call(handlers, "ncp_post_turn", _args("carol", result_summary="done"))
    assert posted["acknowledged_whisper_ids"] == []


def test_default_key_guard_message(tmp_path: Path) -> None:
    from ncp.mcp import server as server_mod

    store, handlers = _setup(tmp_path)
    original = server_mod._session_id_from_args
    server_mod._session_id_from_args = lambda a: server_mod.DEFAULT_FETCH_SESSION_ID  # type: ignore[assignment]
    try:
        payload = _raw(handlers, "ncp_post_turn", _args("alice", result_summary="done"))
        assert "agent_id or session_id" in payload["error"]["message"]
        ok = _raw(handlers, "ncp_post_turn", _args(
            "alice", result_summary="done", ack_whisper_ids=[], retrieved_chunk_ids=[]))
        assert "error" not in ok
    finally:
        server_mod._session_id_from_args = original  # type: ignore[assignment]


def test_echo_ids_false_omits_arrays_but_server_still_holds_them(tmp_path: Path) -> None:
    store, handlers = _setup(tmp_path)
    ctx = _call(handlers, "ncp_get_context", _args("alice", echo_ids=False))
    assert "pending_whisper_ids" not in ctx and "retrieved_chunk_ids" not in ctx
    posted = _call(handlers, "ncp_post_turn", _args("alice", result_summary="done"))
    assert posted["acknowledged_whisper_ids"] == ["wsp_alice"]


def test_echo_ids_default_true_and_validated(tmp_path: Path) -> None:
    _store, handlers = _setup(tmp_path)
    ctx = _call(handlers, "ncp_get_context", _args("alice"))
    assert "pending_whisper_ids" in ctx and "retrieved_chunk_ids" in ctx
    payload = _raw(handlers, "ncp_get_context", _args("alice", echo_ids="no"))
    assert "echo_ids" in payload["error"]["message"]


def test_held_ids_are_consumed_after_use(tmp_path: Path) -> None:
    _store, handlers = _setup(tmp_path)
    _call(handlers, "ncp_get_context", _args("alice"))
    _call(handlers, "ncp_post_turn", _args("alice", result_summary="one"))
    second = _call(handlers, "ncp_post_turn", _args("alice", result_summary="two"))
    assert second["acknowledged_whisper_ids"] == []
