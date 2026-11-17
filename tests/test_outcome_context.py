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


def test_schema_declares_ctx_object_only() -> None:
    tool = next(t for t in server_mod.MCP_TOOLS if t["name"] == "ncp_record_outcome")
    props = tool["inputSchema"]["properties"]
    assert props["ctx"]["type"] == "object"
    for name in ("pipeline_id", "task", "slot", "model", "benchmark", "eval_source",
                 "output_tokens", "input_tokens", "turns"):
        assert name not in props
    for name in ("agent_id", "success", "chunk_ids", "turn_id", "decision_id", "outcome_id", "weight", "note"):
        assert name in props
    assert "required" not in tool["inputSchema"]


def test_ctx_fields_are_stored(env) -> None:
    store, h = env
    res = h["ncp_record_outcome"]({
        "success": True, "chunk_ids": ["c1"],
        "ctx": {"task": "t", "slot": "s", "model": "m", "benchmark": "b", "eval_source": "verifier",
                "output_tokens": 5, "input_tokens": 6, "turns": 2},
    })
    got = store.get_outcome(res["outcome_id"])
    assert got is not None
    # No held session: pipeline derives from the chunks' shared pipeline.
    assert (got.pipeline_id, got.task, got.slot, got.model, got.benchmark) == ("pipe", "t", "s", "m", "b")
    assert (got.eval_source, got.output_tokens, got.input_tokens, got.turns) == ("verifier", 5, 6, 2)


def test_defaults_from_last_get_context_survive_post_turn(env) -> None:
    store, h = env
    h["ncp_get_context"](dict(CTX))
    h["ncp_post_turn"]({**CTX, "result_summary": "done"})
    res = h["ncp_record_outcome"]({"success": True, "chunk_ids": ["c1"], "agent_id": "a1"})
    got = store.get_outcome(res["outcome_id"])
    assert got is not None
    assert (got.task, got.slot, got.pipeline_id) == ("fix_bug", "payment", "pipe")
    assert store.has_success_outcome(task="fix_bug", slot="payment", pipeline_id="pipe")


def test_ctx_overrides_held_task_slot(env) -> None:
    store, h = env
    h["ncp_get_context"](dict(CTX))
    res = h["ncp_record_outcome"](
        {"success": True, "chunk_ids": ["c1"], "agent_id": "a1", "ctx": {"slot": "other"}}
    )
    got = store.get_outcome(res["outcome_id"])
    assert got is not None
    assert (got.task, got.slot, got.pipeline_id) == ("fix_bug", "other", "pipe")


def test_pipeline_id_is_not_a_client_argument(env) -> None:
    store, h = env
    h["ncp_get_context"](dict(CTX))
    res = h["ncp_record_outcome"](
        {"success": True, "chunk_ids": ["c1"], "agent_id": "a1", "pipeline_id": "someone_else"}
    )
    got = store.get_outcome(res["outcome_id"])
    assert got is not None and got.pipeline_id == "pipe"


def test_pipeline_derivation_from_chunks(env) -> None:
    store, h = env
    store.write(SubconsciousChunk(
        chunk_id="c2", layer="semantic", content="other pipeline fact", src="tool_result",
        pipeline_id="elsewhere", written_by="x",
    ))
    got = store.get_outcome(h["ncp_record_outcome"]({"success": True, "chunk_ids": ["c1"]})["outcome_id"])
    assert got is not None and got.pipeline_id == "pipe" and got.task is None
    mixed = store.get_outcome(h["ncp_record_outcome"]({"success": True, "chunk_ids": ["c1", "c2"]})["outcome_id"])
    assert mixed is not None and mixed.pipeline_id is None


@pytest.mark.parametrize("field,value", [
    ("eval_source", "robot"), ("output_tokens", -1), ("input_tokens", 1.5), ("turns", "3"),
    ("turns", True), ("model", 7), ("task", ["x"]), ("benchmark", 1),
])
def test_invalid_values_rejected(env, field: str, value: object) -> None:
    _, h = env
    with pytest.raises(ValueError, match=field):
        h["ncp_record_outcome"]({"success": True, "chunk_ids": ["c1"], "ctx": {field: value}})


def test_ctx_unknown_keys_and_non_object_rejected(env) -> None:
    _, h = env
    with pytest.raises(ValueError, match="unknown keys"):
        h["ncp_record_outcome"]({"success": True, "chunk_ids": ["c1"], "ctx": {"pipeline_id": "x"}})
    with pytest.raises(ValueError, match="must be an object"):
        h["ncp_record_outcome"]({"success": True, "chunk_ids": ["c1"], "ctx": "model=x"})


def test_old_clients_unchanged(env) -> None:
    store, h = env
    res = h["ncp_record_outcome"]({"success": False, "chunk_ids": ["c1"], "note": "n"})
    assert res["recorded"] is True and res["calibrated"] is False
    got = store.get_outcome(res["outcome_id"])
    assert got is not None and got.eval_source == "self_report" and got.model is None


# ── review fixes: admin keys and ambiguous suffix borrow ─────────────────────


def _admin(monkeypatch: pytest.MonkeyPatch, name: str = "router"):  # type: ignore[no-untyped-def]
    from ncp.principals import Principal

    principal = Principal(name=name, agent_id=None, pipelines=None, capabilities=frozenset({"admin"}))
    monkeypatch.setattr(server_mod, "current_principal", lambda: principal)
    return principal


def test_admin_dash_key_falls_back_to_held_context(env, monkeypatch: pytest.MonkeyPatch) -> None:
    store, h = env
    admin = _admin(monkeypatch)
    # get_context keyed with the pipeline; record_outcome omits it -> key ends in "-".
    h["ncp_get_context"]({**CTX, "session_id": f"{admin.fetch_session_key}:pipe:a1"})
    res = h["ncp_record_outcome"](
        {"success": True, "chunk_ids": ["c1"], "session_id": f"{admin.fetch_session_key}:-:-"}
    )
    got = store.get_outcome(res["outcome_id"])
    assert got is not None and (got.task, got.slot, got.pipeline_id) == ("fix_bug", "payment", "pipe")


def test_admin_fallback_refuses_ambiguous_pipelines(env, monkeypatch: pytest.MonkeyPatch) -> None:
    store, h = env
    admin = _admin(monkeypatch)
    h["ncp_get_context"]({**CTX, "session_id": f"{admin.fetch_session_key}:pipe:a1"})
    h["ncp_get_context"]({**CTX, "pipeline_id": "other", "task": "t2",
                          "session_id": f"{admin.fetch_session_key}:other:a1"})
    res = h["ncp_record_outcome"](
        {"success": True, "chunk_ids": ["c1"], "session_id": f"{admin.fetch_session_key}:-:-"}
    )
    got = store.get_outcome(res["outcome_id"])
    # Ambiguous held contexts: no task/slot borrowed; pipeline falls back to the chunks'.
    assert got is not None and (got.task, got.slot, got.pipeline_id) == (None, None, "pipe")


def test_agent_suffix_borrow_requires_unique_pipeline(env) -> None:
    store, h = env
    h["ncp_get_context"](dict(CTX))
    h["ncp_get_context"]({**CTX, "pipeline_id": "other", "task": "t2"})
    # Two pipelines for agent a1: no borrow (pipeline_id is not a client argument).
    res = h["ncp_record_outcome"]({"success": True, "chunk_ids": ["c1"], "agent_id": "a1", "pipeline_id": "pipe"})
    got = store.get_outcome(res["outcome_id"])
    assert got is not None and got.task is None and got.pipeline_id == "pipe"


def test_non_admin_principal_without_agent_id_still_records(tmp_path: Path) -> None:
    from ncp.principals import Principal, ToolAuthorizer

    config = load_config(cwd=tmp_path, env={})
    config.values["calibration"]["auto_on_outcome"] = False
    store = SQLiteStore(tmp_path / "ncp.db")
    store.write(SubconsciousChunk(
        chunk_id="c1", layer="semantic", content="payment bug fix notes", src="tool_result",
        pipeline_id="pipe", written_by="x",
    ))
    h = make_handlers(store, config=config)
    principal = Principal(
        name="bob", agent_id="bob", pipelines=None, capabilities=frozenset({"record_outcome"})
    )
    auth = ToolAuthorizer(store)
    ctx = auth.check_call(principal, "ncp_get_context", dict(CTX, agent_id="bob"))
    h["ncp_get_context"](ctx)
    args = auth.check_call(principal, "ncp_record_outcome", {"success": True, "chunk_ids": ["c1"]})
    res = h["ncp_record_outcome"](args)
    got = store.get_outcome(res["outcome_id"])
    assert got is not None and got.task == "fix_bug" and got.pipeline_id == "pipe"
