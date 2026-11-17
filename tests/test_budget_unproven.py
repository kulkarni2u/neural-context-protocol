"""R7: adaptive budget never shrinks a slot that has no success outcome yet."""

from __future__ import annotations

import json
from pathlib import Path

from ncp.config import NCPConfig, load_config
from ncp.mcp.server import _handle_request, make_handlers
from ncp.stores.sqlite import SQLiteStore

PIPELINE = "pipe_unproven"


def _config(tmp_path: Path, *, protect: bool | None = None) -> NCPConfig:
    config = load_config(cwd=tmp_path)
    config.values["budget"]["adaptive_budget_enabled"] = True
    if protect is not None:
        config.values["budget"]["protect_unproven_slots"] = protect
    return config


def _content(raw: str) -> dict:
    response = json.loads(raw)
    assert "error" not in response, response
    return json.loads(response["result"]["content"][0]["text"])


def _get_context(handlers, **extra: object) -> dict:
    return _content(_handle_request(
        {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "ncp_get_context", "arguments": {
            "agent_id": "builder", "role": "build", "owns": [], "must_not": [],
            "task": "unproven_task", "slot": "unproven_slot", "intent": "test", "pipeline_id": PIPELINE, **extra,
        }}},
        handlers,
    ))


def _record_success(store: SQLiteStore) -> None:
    from ncp.types import OutcomeRecord

    store.record_outcome(OutcomeRecord(
        success=True, pipeline_id=PIPELINE, task="unproven_task", slot="unproven_slot", chunk_ids=[],
    ))


def _record_other_failure(store: SQLiteStore) -> None:
    from ncp.types import OutcomeRecord

    store.record_outcome(OutcomeRecord(
        success=False, pipeline_id=PIPELINE, task="other_task", slot="other_slot", chunk_ids=[],
    ))


def test_pipeline_without_outcomes_is_not_protected(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "u0.db")
    handlers = make_handlers(store, config=_config(tmp_path))
    block = _get_context(handlers)["budget_tokens"]
    assert block["budget_unproven_protected"] is False
    assert block["adjusted"] < block["requested"]


def test_protection_expires_after_unproven_max_turns(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "u4.db")
    _record_other_failure(store)
    handlers = make_handlers(store, config=_config(tmp_path))
    assert _get_context(handlers, slot_age=4)["budget_tokens"]["budget_unproven_protected"] is True
    old = _get_context(handlers, slot_age=5)["budget_tokens"]
    assert old["budget_unproven_protected"] is False


def test_has_any_outcome(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "u5.db")
    assert store.has_any_outcome(pipeline_id=PIPELINE) is False
    _record_other_failure(store)
    assert store.has_any_outcome(pipeline_id=PIPELINE) is True
    assert store.has_any_outcome(pipeline_id="elsewhere") is False
    assert store.has_any_outcome() is True


def test_unproven_slot_not_shrunk_then_shrunk_after_success(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "u.db")
    config = _config(tmp_path)
    handlers = make_handlers(store, config=config)

    _record_other_failure(store)
    first = _get_context(handlers)["budget_tokens"]
    assert first["budget_unproven_protected"] is True
    assert first["adjusted"] >= first["requested"]

    _record_success(store)
    second = _get_context(handlers)["budget_tokens"]
    assert second["budget_unproven_protected"] is False
    assert second["adjusted"] < second["requested"]


def test_protect_disabled_restores_old_behavior(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "u2.db")
    _record_other_failure(store)
    handlers = make_handlers(store, config=_config(tmp_path, protect=False))
    block = _get_context(handlers)["budget_tokens"]
    assert block["budget_unproven_protected"] is False
    assert block["adjusted"] < block["requested"]


def test_store_error_treated_as_proven(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "u3.db")

    def boom(**_kwargs: object) -> bool:
        raise RuntimeError("no outcomes")

    _record_other_failure(store)
    store.has_success_outcome = boom  # type: ignore[method-assign]
    handlers = make_handlers(store, config=_config(tmp_path))
    block = _get_context(handlers)["budget_tokens"]
    assert block["budget_unproven_protected"] is False
    assert block["adjusted"] < block["requested"]
