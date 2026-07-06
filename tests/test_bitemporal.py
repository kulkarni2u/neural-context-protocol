"""Sprint 5d Part 1: CAP-C5 bi-temporal memory.

Covers:
- ncp/stores/bitemporal.py pure filtering functions (backend-agnostic).
- SQLiteStore: supersedence chain, valid_to expiry, as_of point-in-time
  correctness, old-data compatibility, and the existing-DB column-add path.
- ncp_write_memory / ncp_get_context MCP wiring (valid_from/valid_to/
  supersedes, as_of).
"""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path

import pytest

from ncp.mcp.server import _handle_request, make_handlers
from ncp.stores.bitemporal import (
    collect_successor_ids,
    filter_bitemporal,
    is_currently_valid,
    is_valid_as_of,
)
from ncp.stores.sqlite import SQLiteStore
from ncp.types import SubconsciousChunk


def _req(method: str, params: object | None = None, req_id: int = 1) -> dict:
    payload: dict = {"jsonrpc": "2.0", "id": req_id, "method": method}
    if params is not None:
        payload["params"] = params
    return payload


def _call(name: str, arguments: dict | None = None, req_id: int = 1) -> dict:
    params: dict = {"name": name}
    if arguments is not None:
        params["arguments"] = arguments
    return _req("tools/call", params, req_id)


def _content(response_str: str) -> object:
    import json
    payload = json.loads(response_str)["result"]
    return json.loads(payload["content"][0]["text"])


# ---------------------------------------------------------------------------
# ncp/stores/bitemporal.py — pure functions, backend-agnostic
# ---------------------------------------------------------------------------

def test_is_currently_valid_true_for_plain_row() -> None:
    row = {"superseded_by": None, "valid_to": None}
    assert is_currently_valid(row, now=1000.0) is True


def test_is_currently_valid_false_when_superseded() -> None:
    row = {"superseded_by": "sub_new", "valid_to": None}
    assert is_currently_valid(row, now=1000.0) is False


def test_is_currently_valid_false_when_valid_to_passed() -> None:
    row = {"superseded_by": None, "valid_to": 500.0}
    assert is_currently_valid(row, now=1000.0) is False


def test_is_currently_valid_true_when_valid_to_in_future() -> None:
    row = {"superseded_by": None, "valid_to": 1500.0}
    assert is_currently_valid(row, now=1000.0) is True


def test_is_currently_valid_tolerates_missing_columns() -> None:
    """Pre-migration rows (no bi-temporal columns at all) behave as unset."""
    row = {"chunk_id": "old_row"}
    assert is_currently_valid(row, now=1000.0) is True


def test_collect_successor_ids_dedupes_and_skips_none() -> None:
    rows = [
        {"superseded_by": "sub_b"},
        {"superseded_by": "sub_b"},
        {"superseded_by": None},
        {"superseded_by": "sub_c"},
    ]
    assert collect_successor_ids(rows) == {"sub_b", "sub_c"}


def test_is_valid_as_of_excludes_not_yet_recorded() -> None:
    row = {"created_at": 2000.0, "superseded_by": None, "valid_from": None, "valid_to": None}
    assert is_valid_as_of(row, as_of=1000.0, successor_created_at={}) is False


def test_is_valid_as_of_excludes_when_successor_recorded_by_then() -> None:
    row = {"created_at": 100.0, "superseded_by": "sub_new", "valid_from": None, "valid_to": None}
    assert is_valid_as_of(row, as_of=1000.0, successor_created_at={"sub_new": 500.0}) is False


def test_is_valid_as_of_keeps_row_when_successor_recorded_after_as_of() -> None:
    row = {"created_at": 100.0, "superseded_by": "sub_new", "valid_from": None, "valid_to": None}
    assert is_valid_as_of(row, as_of=1000.0, successor_created_at={"sub_new": 1500.0}) is True


def test_is_valid_as_of_keeps_row_when_successor_unknown() -> None:
    """Can't confirm supersession happened by as_of -- correctness over aggressiveness."""
    row = {"created_at": 100.0, "superseded_by": "sub_new", "valid_from": None, "valid_to": None}
    assert is_valid_as_of(row, as_of=1000.0, successor_created_at={}) is True


def test_is_valid_as_of_respects_valid_from_and_valid_to_window() -> None:
    row = {"created_at": 0.0, "superseded_by": None, "valid_from": 500.0, "valid_to": 800.0}
    assert is_valid_as_of(row, as_of=400.0, successor_created_at={}) is False  # too early
    assert is_valid_as_of(row, as_of=600.0, successor_created_at={}) is True  # within window
    assert is_valid_as_of(row, as_of=900.0, successor_created_at={}) is False  # too late


def test_filter_bitemporal_default_view_vs_as_of_view() -> None:
    rows = [
        {"chunk_id": "a", "created_at": 100.0, "superseded_by": "b", "valid_from": None, "valid_to": 200.0},
        {"chunk_id": "b", "created_at": 200.0, "superseded_by": None, "valid_from": 200.0, "valid_to": None},
    ]
    default_view = filter_bitemporal(rows, as_of=None, now=1000.0)
    assert [r["chunk_id"] for r in default_view] == ["b"]

    as_of_view = filter_bitemporal(rows, as_of=150.0, now=1000.0, successor_created_at={"b": 200.0})
    assert [r["chunk_id"] for r in as_of_view] == ["a"]


# ---------------------------------------------------------------------------
# SQLiteStore — supersedence chain, expiry, as_of, old-data compatibility
# ---------------------------------------------------------------------------

def test_sqlite_supersede_default_view_returns_only_new(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "s.db")
    store.write(SubconsciousChunk(
        chunk_id="fact_a", layer="semantic", src="tool_result",
        content="the login flow accepts a session cookie", pipeline_id="p1",
    ))
    store.write(SubconsciousChunk(
        chunk_id="fact_b", layer="semantic", src="tool_result",
        content="access control now requires a signed bearer token instead", pipeline_id="p1",
    ))
    assert store.supersede("fact_a", "fact_b") is True

    zone_ids = {c.chunk_id for c in store.get_working_zone(pipeline_id="p1")}
    assert "fact_b" in zone_ids
    assert "fact_a" not in zone_ids

    queried_ids = {c.chunk_id for c in store.query("fact", pipeline_id="p1", retrieval_mode="trust_recency")}
    assert "fact_b" in queried_ids
    assert "fact_a" not in queried_ids


def test_sqlite_supersede_as_of_before_new_chunk_returns_old(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "s.db")
    store.write(SubconsciousChunk(
        chunk_id="fact_a", layer="semantic", src="tool_result",
        content="the login flow accepts a session cookie", pipeline_id="p1",
    ))
    t_before_b = time.time()
    time.sleep(0.01)
    store.write(SubconsciousChunk(
        chunk_id="fact_b", layer="semantic", src="tool_result",
        content="access control now requires a signed bearer token instead", pipeline_id="p1",
    ))
    store.supersede("fact_a", "fact_b")

    as_of_before = {c.chunk_id for c in store.get_working_zone(pipeline_id="p1", as_of=t_before_b)}
    assert as_of_before == {"fact_a"}

    as_of_now = {c.chunk_id for c in store.get_working_zone(pipeline_id="p1", as_of=time.time())}
    assert as_of_now == {"fact_b"}


def test_sqlite_supersede_rowcount_false_for_unknown_old_id(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "s.db")
    assert store.supersede("does_not_exist", "sub_new") is False


def test_sqlite_supersede_survives_rewrite_of_old_chunk(tmp_path: Path) -> None:
    """Re-writing the same (old) chunk_id must not erase its supersession.

    valid_to/superseded_by are system-managed via supersede() and are
    deliberately excluded from write()'s ON CONFLICT UPDATE SET, mirroring
    how retrieval_count/dissent_count are protected from upsert resets.
    """
    store = SQLiteStore(tmp_path / "s.db")
    original = SubconsciousChunk(
        chunk_id="fact_a", layer="semantic", src="tool_result",
        content="the login flow accepts a session cookie", pipeline_id="p1",
    )
    store.write(original)
    store.write(SubconsciousChunk(
        chunk_id="fact_b", layer="semantic", src="tool_result",
        content="access control now requires a signed bearer token instead", pipeline_id="p1",
    ))
    store.supersede("fact_a", "fact_b")

    # Re-write fact_a (e.g. a retry) -- must not resurrect it in the default view.
    store.write(original)
    zone_ids = {c.chunk_id for c in store.get_working_zone(pipeline_id="p1")}
    assert "fact_a" not in zone_ids
    assert "fact_b" in zone_ids


def test_sqlite_valid_to_expiry_filtering(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "s.db")
    now = time.time()
    store.write(SubconsciousChunk(
        chunk_id="expired_fact", layer="semantic", src="tool_result",
        content="a fact that already stopped being true", pipeline_id="p1",
        valid_to=now - 10,
    ))
    store.write(SubconsciousChunk(
        chunk_id="live_fact", layer="semantic", src="tool_result",
        content="a fact still true", pipeline_id="p1",
        valid_to=now + 10_000,
    ))

    zone_ids = {c.chunk_id for c in store.get_working_zone(pipeline_id="p1")}
    assert "expired_fact" not in zone_ids
    assert "live_fact" in zone_ids


def test_sqlite_as_of_point_in_time_validity_window(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "s.db")
    # Transaction time (created_at) is always real wall-clock time at write,
    # independent of valid time -- so a "recorded by as_of" check only makes
    # sense for as_of values at/after the real write. To exercise the
    # valid_from/valid_to window in isolation, give this fact a *future*
    # validity window relative to "now" (e.g. "this policy takes effect next
    # week"): every as_of below is safely after created_at, so only the
    # valid_from/valid_to bounds differ between them.
    now = time.time()
    store.write(SubconsciousChunk(
        chunk_id="windowed_fact", layer="semantic", src="tool_result",
        content="a policy that only takes effect for a future window", pipeline_id="p1",
        valid_from=now + 1000, valid_to=now + 2000,
    ))

    before = {c.chunk_id for c in store.get_working_zone(pipeline_id="p1", as_of=now + 1)}
    during = {c.chunk_id for c in store.get_working_zone(pipeline_id="p1", as_of=now + 1500)}
    after = {c.chunk_id for c in store.get_working_zone(pipeline_id="p1", as_of=now + 2500)}

    assert "windowed_fact" not in before
    assert "windowed_fact" in during
    assert "windowed_fact" not in after


def test_sqlite_old_data_compatibility(tmp_path: Path) -> None:
    """Chunks written without any bi-temporal params behave exactly as before."""
    store = SQLiteStore(tmp_path / "s.db")
    chunk = SubconsciousChunk(
        chunk_id="plain_fact", layer="semantic", src="tool_result",
        content="ordinary chunk with no bi-temporal fields", pipeline_id="p1",
    )
    store.write(chunk)

    default_zone = {c.chunk_id for c in store.get_working_zone(pipeline_id="p1")}
    assert "plain_fact" in default_zone

    as_of_zone = {c.chunk_id for c in store.get_working_zone(pipeline_id="p1", as_of=time.time() + 10)}
    assert "plain_fact" in as_of_zone

    fetched = store.get_chunks_by_ids(["plain_fact"])
    assert fetched[0].valid_from is None
    assert fetched[0].valid_to is None
    assert fetched[0].superseded_by is None


def test_sqlite_get_chunks_by_ids_honors_bitemporal_filter(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "s.db")
    store.write(SubconsciousChunk(
        chunk_id="fact_a", layer="semantic", src="tool_result",
        content="v1", pipeline_id="p1",
    ))
    store.write(SubconsciousChunk(
        chunk_id="fact_b", layer="semantic", src="tool_result",
        content="v2", pipeline_id="p1",
    ))
    store.supersede("fact_a", "fact_b")

    default_fetch = {c.chunk_id for c in store.get_chunks_by_ids(["fact_a", "fact_b"])}
    assert default_fetch == {"fact_b"}


# ---------------------------------------------------------------------------
# SQLiteStore — existing-DB column-add path (pre-CAP-C5 schema, reopened)
# ---------------------------------------------------------------------------

def test_sqlite_store_adds_bitemporal_columns_to_existing_db(tmp_path: Path) -> None:
    """A DB created before CAP-C5 (no valid_from/valid_to/superseded_by
    columns) must gain them on reopen, with existing rows intact and NULL
    in the new columns."""
    db_path = tmp_path / "old_schema.db"
    connection = sqlite3.connect(db_path)
    try:
        # Minimal pre-CAP-C5 chunks table (mirrors SQLiteStore.SCHEMA sans the
        # three new columns) plus the other tables _init_db expects to exist.
        connection.executescript(
            """
            CREATE TABLE chunks (
                chunk_id TEXT PRIMARY KEY,
                pipeline_id TEXT,
                scope TEXT DEFAULT 'pipeline',
                zone TEXT DEFAULT 'working',
                layer TEXT NOT NULL,
                chunk_type TEXT DEFAULT 'prose',
                content TEXT NOT NULL,
                src TEXT NOT NULL,
                written_by TEXT DEFAULT 'system',
                caused_by TEXT,
                conscious_hash TEXT,
                evidence_id TEXT,
                version INTEGER DEFAULT 1,
                supersedes TEXT,
                source_refs TEXT DEFAULT '[]',
                schema_version INTEGER DEFAULT 1,
                created_at REAL NOT NULL,
                base_trust REAL DEFAULT 0.7,
                generation INTEGER DEFAULT 0,
                result_confidence REAL,
                result_attempts INTEGER,
                conditions TEXT DEFAULT '[]',
                valid_while TEXT,
                expiry REAL,
                owner TEXT,
                meta TEXT DEFAULT '{}'
            );
            CREATE TABLE tombstones (
                chunk_id TEXT PRIMARY KEY,
                forward_ref TEXT,
                tombstoned_at REAL NOT NULL,
                expires_at REAL NOT NULL
            );
            """
        )
        connection.execute(
            "INSERT INTO chunks (chunk_id, layer, content, src, created_at, pipeline_id)"
            " VALUES ('pre_existing', 'semantic', 'a row from before CAP-C5', 'tool_result', ?, 'p1')",
            (time.time(),),
        )
        connection.commit()
    finally:
        connection.close()

    # Reopening via SQLiteStore must run the ALTER TABLE upgrade path without error.
    store = SQLiteStore(db_path)

    raw = sqlite3.connect(db_path)
    try:
        columns = {row[1] for row in raw.execute("PRAGMA table_info(chunks)").fetchall()}
    finally:
        raw.close()
    assert {"valid_from", "valid_to", "superseded_by"} <= columns

    fetched = store.get_chunks_by_ids(["pre_existing"])
    assert len(fetched) == 1
    assert fetched[0].valid_from is None
    assert fetched[0].valid_to is None
    assert fetched[0].superseded_by is None

    zone_ids = {c.chunk_id for c in store.get_working_zone(pipeline_id="p1")}
    assert "pre_existing" in zone_ids


# ---------------------------------------------------------------------------
# MCP wiring: ncp_write_memory (valid_from/valid_to/supersedes) and
# ncp_get_context (as_of)
# ---------------------------------------------------------------------------

def test_mcp_write_memory_accepts_valid_from_valid_to(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "mcp.db")
    handlers = make_handlers(store)
    resp = _handle_request(
        _call("ncp_write_memory", {
            "content": "a bounded-validity fact",
            "layer": "semantic",
            "src": "tool_result",
            "pipeline_id": "p1",
            "chunk_id": "bounded_fact",
            "valid_from": "2026-01-01T00:00:00Z",
            "valid_to": "2099-06-01T00:00:00Z",
        }),
        handlers,
    )
    result = _content(resp)
    assert result["written"] is True

    fetched = store.get_chunks_by_ids(["bounded_fact"])[0]
    assert fetched.valid_from == pytest.approx(1767225600.0)
    assert fetched.valid_to == pytest.approx(4083955200.0)


def test_mcp_write_memory_supersedes_marks_old_chunk(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "mcp.db")
    handlers = make_handlers(store)
    _handle_request(
        _call("ncp_write_memory", {
            "content": "original fact",
            "layer": "semantic",
            "src": "tool_result",
            "pipeline_id": "p1",
            "chunk_id": "fact_a",
        }),
        handlers,
    )
    resp = _handle_request(
        _call("ncp_write_memory", {
            "content": "updated fact",
            "layer": "semantic",
            "src": "tool_result",
            "pipeline_id": "p1",
            "chunk_id": "fact_b",
            "supersedes": "fact_a",
        }),
        handlers,
    )
    result = _content(resp)
    assert result["written"] is True
    assert result["superseded"] is True

    zone_ids = {c.chunk_id for c in store.get_working_zone(pipeline_id="p1")}
    assert zone_ids == {"fact_b"}


def test_mcp_write_memory_supersedes_unknown_chunk_reports_false(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "mcp.db")
    handlers = make_handlers(store)
    resp = _handle_request(
        _call("ncp_write_memory", {
            "content": "a fact",
            "layer": "semantic",
            "src": "tool_result",
            "pipeline_id": "p1",
            "supersedes": "does_not_exist",
        }),
        handlers,
    )
    result = _content(resp)
    assert result["superseded"] is False


def test_mcp_get_context_as_of_returns_pre_supersession_view(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "mcp.db")
    handlers = make_handlers(store)

    _handle_request(
        _call("ncp_write_memory", {
            "content": "auth uses basic tokens for the login flow",
            "layer": "semantic",
            "src": "tool_result",
            "pipeline_id": "pipe_asof",
            "chunk_id": "fact_a",
        }),
        handlers,
    )
    t_before_b = time.time()
    time.sleep(0.01)
    _handle_request(
        _call("ncp_write_memory", {
            "content": "auth uses bearer tokens for the login flow",
            "layer": "semantic",
            "src": "tool_result",
            "pipeline_id": "pipe_asof",
            "chunk_id": "fact_b",
            "supersedes": "fact_a",
        }),
        handlers,
    )

    as_of_resp = _handle_request(
        _call("ncp_get_context", {
            "agent_id": "builder",
            "role": "build",
            "task": "login_flow",
            "slot": "auth",
            "intent": "check_history",
            "pipeline_id": "pipe_asof",
            "as_of": t_before_b,
        }),
        handlers,
    )
    as_of_result = _content(as_of_resp)
    assert "fact_a" in as_of_result["context"] or "basic tokens" in as_of_result["context"]
    assert "bearer tokens" not in as_of_result["context"]
    assert as_of_result["as_of"] == pytest.approx(t_before_b)

    current_resp = _handle_request(
        _call("ncp_get_context", {
            "agent_id": "builder",
            "role": "build",
            "task": "login_flow",
            "slot": "auth",
            "intent": "check_current",
            "pipeline_id": "pipe_asof",
        }),
        handlers,
    )
    current_result = _content(current_resp)
    assert "bearer tokens" in current_result["context"]
    assert "basic tokens" not in current_result["context"]
    assert "as_of" not in current_result
