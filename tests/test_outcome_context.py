"""R2: ncp_record_outcome context fields and server-held task/slot defaults."""

from __future__ import annotations

from pathlib import Path

import pytest

from ncp.config import load_config
from ncp.mcp import server as server_mod
from ncp.mcp.server import make_handlers
from ncp.stores.sqlite import SQLiteStore
from ncp.types import SubconsciousChunk

CTX = {
    "agent_id": "a1", "role": "pravaha", "task": "fix_bug", "slot": "payment",
    "intent": "advance", "pipeline_id": "pipe",
}


@pytest.fixture
def env(tmp_path: Path):
    config = load_config(cwd=tmp_path, env={})
    config.values["calibration"]["auto_on_outcome"] = False  # isolate from calibration
    store = SQLiteStore(tmp_path / "ncp.db")
    store.write(SubconsciousChunk(
        chunk_id="c1", layer="semantic", content="payment bug fix notes", src="tool_result",
        pipeline_id="pipe", written_by="x",
    ))
    return store, make_handlers(store, config=config)


def test_schema_declares_new_optional_properties() -> None:
    tool = next(t for t in server_mod.MCP_TOOLS if t["name"] == "ncp_record_outcome")
    props = tool["inputSchema"]["properties"]
    for name in ("pipeline_id", "task", "slot", "model", "benchmark", "eval_source",
                 "output_tokens", "input_tokens", "turns"):
        assert name in props
    assert props["eval_source"]["enum"] == ["self_report", "verifier", "human"]
    for name in ("output_tokens", "input_tokens", "turns"):
        assert props[name]["type"] == "integer"  # >= 0 is enforced by the handler
    assert "required" not in tool["inputSchema"]


def test_explicit_fields_are_stored(env) -> None:
    store, h = env
    res = h["ncp_record_outcome"]({
        "success": True, "chunk_ids": ["c1"], "pipeline_id": "pipe", "task": "t", "slot": "s",
        "model": "m", "benchmark": "b", "eval_source": "verifier",
        "output_tokens": 5, "input_tokens": 6, "turns": 2,
    })
    got = store.get_outcome(res["outcome_id"])
    assert got is not None
    assert (got.pipeline_id, got.task, got.slot, got.model, got.benchmark) == ("pipe", "t", "s", "m", "b")
    assert (got.eval_source, got.output_tokens, got.input_tokens, got.turns) == ("verifier", 5, 6, 2)


def test_defaults_from_last_get_context_survive_post_turn(env) -> None:
    store, h = env
    h["ncp_get_context"](dict(CTX))
    h["ncp_post_turn"]({**CTX, "result_summary": "done"})
    res = h["ncp_record_outcome"]({"success": True, "chunk_ids": ["c1"], "agent_id": "a1", "pipeline_id": "pipe"})
    got = store.get_outcome(res["outcome_id"])
    assert got is not None
    assert (got.task, got.slot, got.pipeline_id) == ("fix_bug", "payment", "pipe")
    assert store.has_success_outcome(task="fix_bug", slot="payment", pipeline_id="pipe")


def test_defaults_without_explicit_pipeline_and_explicit_wins(env) -> None:
    store, h = env
    h["ncp_get_context"](dict(CTX))
    res = h["ncp_record_outcome"]({"success": True, "chunk_ids": ["c1"], "agent_id": "a1", "slot": "other"})
    got = store.get_outcome(res["outcome_id"])
    assert got is not None
    assert (got.task, got.slot, got.pipeline_id) == ("fix_bug", "other", "pipe")


def test_no_defaults_for_shared_default_session_or_other_pipeline(env) -> None:
    store, h = env
    h["ncp_get_context"](dict(CTX))
    res = h["ncp_record_outcome"]({"success": True, "chunk_ids": ["c1"]})
    got = store.get_outcome(res["outcome_id"])
    assert got is not None and got.task is None and got.slot is None
    other = h["ncp_record_outcome"]({
        "success": True, "chunk_ids": ["c1"], "agent_id": "a1", "pipeline_id": "someone_else",
    })
    got2 = store.get_outcome(other["outcome_id"])
    assert got2 is not None and got2.task is None and got2.pipeline_id == "someone_else"


@pytest.mark.parametrize("field,value", [
    ("eval_source", "robot"), ("output_tokens", -1), ("input_tokens", 1.5), ("turns", "3"),
    ("turns", True), ("model", 7), ("task", ["x"]), ("benchmark", 1),
])
def test_invalid_values_rejected(env, field: str, value: object) -> None:
    _, h = env
    with pytest.raises(ValueError, match=field):
        h["ncp_record_outcome"]({"success": True, "chunk_ids": ["c1"], field: value})


def test_old_clients_unchanged(env) -> None:
    store, h = env
    res = h["ncp_record_outcome"]({"success": False, "chunk_ids": ["c1"], "note": "n"})
    assert res["recorded"] is True and res["calibrated"] is False
    got = store.get_outcome(res["outcome_id"])
    assert got is not None and got.eval_source == "self_report" and got.model is None
