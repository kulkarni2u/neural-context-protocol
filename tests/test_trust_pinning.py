"""Session-pinned trust display: headers stay stable while live trust moves."""

from __future__ import annotations

import re
from pathlib import Path

from ncp.config import load_config
from ncp.mcp import server as server_mod
from ncp.mcp.server import make_handlers
from ncp.stores.sqlite import SQLiteStore
from ncp.types import SubconsciousChunk

ARGS = {"agent_id": "a", "role": "build", "task": "fix_payment_bug", "slot": "payment",
        "intent": "advance", "pipeline_id": "p"}


def _setup(tmp_path: Path, *, pin: bool = True):
    config = load_config(cwd=tmp_path, env={})
    config.values["encoder"]["pin_trust_per_session"] = pin
    store = SQLiteStore(tmp_path / "ncp.db")
    for i in range(3):
        store.write(SubconsciousChunk(
            chunk_id=f"c{i}", layer="semantic", content=f"payment retry bug fact {i} idempotency keys",
            src="tool_result", pipeline_id="p", written_by=f"w{i}", base_trust=0.5,
        ))
    return store, make_handlers(store, config=config)


def _trusts(context: str) -> dict[str, str]:
    return dict(re.findall(r"chunk:(\S+) .*?trust:([0-9.]+)", context))


def _set_trust(store: SQLiteStore, value: float) -> None:
    with store._connect() as c:
        c.execute("UPDATE chunks SET base_trust = ?", (value,))


def test_header_trust_pinned_within_session_and_live_in_new_session(tmp_path: Path) -> None:
    store, h = _setup(tmp_path)
    first = h["ncp_get_context"](dict(ARGS))
    shown = _trusts(first["context"])
    assert shown and set(shown.values()) == {"0.5"}

    _set_trust(store, 0.9)
    second = h["ncp_get_context"](dict(ARGS))
    assert _trusts(second["context"]) == shown  # same session: pinned
    assert store.get_chunks_by_ids(["c0"])[0].base_trust == 0.9  # stored chunk untouched

    other_session = h["ncp_get_context"]({**ARGS, "agent_id": "b"})
    assert set(_trusts(other_session["context"]).values()) == {"0.9"}


def test_pin_disabled_shows_live_trust(tmp_path: Path) -> None:
    store, h = _setup(tmp_path, pin=False)
    h["ncp_get_context"](dict(ARGS))
    _set_trust(store, 0.9)
    assert set(_trusts(h["ncp_get_context"](dict(ARGS))["context"]).values()) == {"0.9"}


def test_pin_map_is_bounded_lru(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(server_mod, "_TRUST_PINS_MAX", 2)
    store, h = _setup(tmp_path)
    h["ncp_get_context"](dict(ARGS))
    session = h["ncp_get_context"].fetch_sessions["p:a"]
    assert 0 < len(session.trust_pins) <= 2
