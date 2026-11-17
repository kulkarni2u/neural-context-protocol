"""ncp_post_turn: optional result_full, result_ref and the result_full cap."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ncp.config import load_config
from ncp.mcp.server import MCP_TOOLS, _handle_request, make_handlers
from ncp.stores.sqlite import SQLiteStore
from ncp.types import SubconsciousChunk

BASE = {
    "agent_id": "builder",
    "role": "build",
    "task": "t",
    "slot": "s",
    "intent": "i",
    "pipeline_id": "pipe_1",
}


def _call(handlers: dict, name: str, arguments: dict) -> dict:
    raw = _handle_request(
        {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": name, "arguments": arguments}},
        handlers,
        tools=MCP_TOOLS,
    )
    payload = json.loads(raw)
    assert "error" not in payload, payload
    return json.loads(payload["result"]["content"][0]["text"])


def _config(tmp_path: Path, cap: int | None = None):
    if cap is not None:
        (tmp_path / ".ncp").mkdir(exist_ok=True)
        (tmp_path / ".ncp" / "config.toml").write_text(f"[turns]\nresult_full_max_chars = {cap}\n")
    return load_config(cwd=tmp_path)


def test_schema_result_full_is_optional() -> None:
    tool = next(t for t in MCP_TOOLS if t["name"] == "ncp_post_turn")
    assert "result_full" not in tool["inputSchema"]["required"]
    assert "result_ref" in tool["inputSchema"]["properties"]


def test_summary_only_succeeds_and_stores_summary(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "s.db")
    handlers = make_handlers(store, config=_config(tmp_path))
    posted = _call(handlers, "ncp_post_turn", {**BASE, "turn_id": "turn_a", "result_summary": "just a summary"})
    assert posted["posted"] is True
    record = store.resolve_recent_ref("r:sub/turn_a")
    assert record.result == "just a summary"
    assert record.result_full == "just a summary"
    # Recent-ref retrieval still yields useful content from a summary-only turn.
    ctx = _call(handlers, "ncp_get_context", {**BASE, "recent": ["r:sub/turn_a"]})
    assert "just a summary" in ctx["context"]


def test_explicit_result_full_below_cap_unchanged(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "s.db")
    handlers = make_handlers(store, config=_config(tmp_path))
    posted = _call(handlers, "ncp_post_turn", {
        **BASE, "turn_id": "turn_b", "result_summary": "sum", "result_full": "x" * 5000,
    })
    assert "result_truncated" not in posted and "raw_ref" not in posted
    assert store.resolve_recent_ref("r:sub/turn_b").result_full == "x" * 5000


def test_result_full_over_cap_is_truncated_and_original_kept(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "s.db")
    handlers = make_handlers(store, config=_config(tmp_path, cap=100))
    original = "".join(chr(97 + i % 26) for i in range(4500))
    posted = _call(handlers, "ncp_post_turn", {
        **BASE, "turn_id": "turn_c", "result_summary": "sum", "result_full": original,
    })
    assert posted["result_truncated"] is True
    assert store.resolve_recent_ref("r:sub/turn_c").result_full == original[:100]
    ids = posted["raw_ref_chunk_ids"]
    assert posted["raw_ref"] == ids[0]
    kept = "".join(c.content for c in sorted(
        store.get_chunks_by_ids(ids), key=lambda c: ids.index(c.chunk_id)))
    assert kept == original


def test_default_cap_is_8000(tmp_path: Path) -> None:
    assert _config(tmp_path).turn_result_full_max_chars == 8000


def test_result_ref_uses_existing_chunk(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "s.db")
    store.write(SubconsciousChunk(
        chunk_id="sub_full", layer="episodic", content="the complete answer text",
        src="tool_result", pipeline_id="pipe_1",
    ))
    handlers = make_handlers(store, config=_config(tmp_path))
    _call(handlers, "ncp_post_turn", {
        **BASE, "turn_id": "turn_d", "result_summary": "sum", "result_ref": "sub_full",
    })
    assert store.resolve_recent_ref("r:sub/turn_d").result_full == "the complete answer text"


def test_result_ref_unknown_or_foreign_pipeline_rejected(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "s.db")
    store.write(SubconsciousChunk(
        chunk_id="sub_other", layer="episodic", content="secret", src="tool_result", pipeline_id="pipe_2",
    ))
    handlers = make_handlers(store, config=_config(tmp_path))
    for ref in ("missing_chunk", "sub_other"):
        raw = _handle_request(
            {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {
                "name": "ncp_post_turn",
                "arguments": {**BASE, "result_summary": "sum", "result_ref": ref}}},
            handlers, tools=MCP_TOOLS,
        )
        assert "result_ref" in json.loads(raw)["error"]["message"]


def test_invalid_cap_config_rejected(tmp_path: Path) -> None:
    (tmp_path / ".ncp").mkdir()
    (tmp_path / ".ncp" / "config.toml").write_text("[turns]\nresult_full_max_chars = 0\n")
    with pytest.raises(ValueError):
        load_config(cwd=tmp_path).turn_result_full_max_chars


def test_result_ref_rejects_reserved_skill_chunk(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "s.db")
    store.write(SubconsciousChunk(
        chunk_id="skl_deadbeef0001", layer="procedural", content="skill body", src="tool_result",
        pipeline_id="pipe_1", written_by="ncp:skill_cache:x",
    ))
    handlers = make_handlers(store, config=_config(tmp_path))
    raw = _handle_request(
        {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "ncp_post_turn", "arguments": {
            **BASE, "result_summary": "s", "result_ref": "skl_deadbeef0001"}}},
        handlers, tools=MCP_TOOLS,
    )
    assert "reserved skill-cache namespace" in json.loads(raw)["error"]["message"]


def test_redact_true_scrubs_summary_full_and_memory_chunks(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "s.db")
    handlers = make_handlers(store, config=_config(tmp_path))
    secret = "api_key=abc123SECRET"
    posted = _call(handlers, "ncp_post_turn", {
        **BASE, "turn_id": "turn_r", "result_summary": f"sum {secret}", "result_full": f"full {secret}",
        "memory_chunks": [{"content": f"mem {secret} Bearer tok_123456", "layer": "semantic", "src": "tool_result"}],
        "redact": True,
    })
    assert posted["posted"] is True
    record = store.resolve_recent_ref("r:sub/turn_r")
    assert "abc123SECRET" not in record.result and "abc123SECRET" not in record.result_full
    assert "[REDACTED]" in record.result
    contents = [c.content for c in store.query("mem", pipeline_id="pipe_1", k=10)]
    assert contents and all("abc123SECRET" not in c and "tok_123456" not in c for c in contents)


def test_redact_default_false_keeps_text_and_must_be_boolean(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "s.db")
    handlers = make_handlers(store, config=_config(tmp_path))
    _call(handlers, "ncp_post_turn", {**BASE, "turn_id": "turn_k", "result_summary": "api_key=keepme"})
    assert "keepme" in store.resolve_recent_ref("r:sub/turn_k").result
    raw = _handle_request(
        {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "ncp_post_turn", "arguments": {
            **BASE, "result_summary": "s", "redact": "yes"}}},
        handlers, tools=MCP_TOOLS,
    )
    assert "redact" in json.loads(raw)["error"]["message"]
