"""Turn-id outcome resolution parity across SQLite, sync and async pgvector.

Regression for the async pgvector path reading ``r[0]`` from dict rows, which
raised ``KeyError(0)`` before a valid turn-linked outcome could be recorded.
"""

from __future__ import annotations

import os
import sqlite3
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from ncp.stores.base import merge_outcome_chunk_rows
from ncp.stores.sqlite import SQLiteStore
from ncp.types import OutcomeRecord, SubconsciousChunk


_LIVE = os.getenv("NCP_RUN_PGVECTOR_INTEGRATION") == "1"
_DSN = os.getenv("NCP_PGVECTOR_DSN", "postgresql://postgres:postgres@127.0.0.1:5432/ncp")


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


# ── helper ──────────────────────────────────────────────────────────────────


def test_merge_outcome_chunk_rows_accepts_dict_tuple_and_sqlite_rows() -> None:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    sqlite_rows = conn.execute("SELECT 'c_sqlite' AS chunk_id").fetchall()

    merged = merge_outcome_chunk_rows(
        [{"chunk_id": "c_dict"}, ("c_tuple",)],
        sqlite_rows,
    )

    assert merged == ["c_dict", "c_tuple", "c_sqlite"]


def test_merge_outcome_chunk_rows_dedups_preserving_first_group_order() -> None:
    linked = [{"chunk_id": "b"}, {"chunk_id": "a"}, {"chunk_id": "b"}]
    legacy = [{"chunk_id": "a"}, {"chunk_id": "c"}]

    assert merge_outcome_chunk_rows(linked, legacy) == ["b", "a", "c"]
    assert merge_outcome_chunk_rows([], []) == []
    assert merge_outcome_chunk_rows([{"chunk_id": None}, ("",)]) == []


# ── async pgvector (mocked dict rows) ───────────────────────────────────────


def _mock_pool_returning(*fetch_results: list[dict[str, str]]) -> MagicMock:
    cursor = MagicMock()
    cursor.fetchall = AsyncMock(side_effect=list(fetch_results))
    cursor.execute = AsyncMock()
    cursor.description = [("chunk_id",)]
    cursor.rowcount = 1
    cursor.__aenter__ = AsyncMock(return_value=cursor)
    cursor.__aexit__ = AsyncMock(return_value=False)
    conn = MagicMock()
    conn.cursor = MagicMock(return_value=cursor)
    conn.commit = AsyncMock()
    conn.rollback = AsyncMock()
    pool = MagicMock()
    pool.open = AsyncMock()
    pool.connection = MagicMock()
    pool.connection.return_value.__aenter__ = AsyncMock(return_value=conn)
    pool.connection.return_value.__aexit__ = AsyncMock(return_value=False)
    pool._cursor = cursor
    return pool


@pytest.mark.anyio
async def test_async_record_outcome_resolves_dict_rows_without_keyerror() -> None:
    pytest.importorskip("psycopg_pool")
    from ncp.stores.pgvector_async import AsyncPgvectorStore

    pool = _mock_pool_returning(
        [{"chunk_id": "c_wrote"}, {"chunk_id": "c_retrieved"}],
        [{"chunk_id": "c_retrieved"}, {"chunk_id": "c_legacy"}],
    )
    with patch("psycopg_pool.AsyncConnectionPool", return_value=pool):
        store = AsyncPgvectorStore("postgresql://localhost/test")
    store._apool = pool  # skip schema init

    assert await store.async_record_outcome(OutcomeRecord(turn_id="turn_x", success=True)) is True

    insert_args = pool._cursor.execute.await_args_list[-1].args[1]
    assert insert_args[2] == '["c_wrote", "c_retrieved", "c_legacy"]'


# ── behavioral parity: sqlite (always) + live pgvector sync/async ───────────


def _chunk(chunk_id: str, *, caused_by: str | None = None) -> SubconsciousChunk:
    return SubconsciousChunk(
        chunk_id=chunk_id,
        layer="episodic",
        content=f"distinct outcome resolution content for {chunk_id} {uuid4().hex}",
        src="tool_result",
        pipeline_id="pipe_outcome_res",
        written_by="executor",
        caused_by=caused_by,
    )


# (name, linked [(chunk_id, relation)], legacy caused_by chunk ids, expected)
_CASES = [
    ("linked_only", [("c_w", "wrote"), ("c_r", "retrieved")], [], ["c_w", "c_r"]),
    ("legacy_only", [], ["c_l"], ["c_l"]),
    ("combined", [("c_r", "retrieved"), ("c_w", "wrote")], ["c_l"], ["c_w", "c_r", "c_l"]),
    ("empty", [], [], []),
    ("duplicate", [("c_d", "wrote"), ("c_d", "retrieved")], ["c_d"], ["c_d"]),
]


def _sync_scenario(store: object, case: tuple) -> list[str]:
    _, linked, legacy, _ = case
    turn_id = f"turn_{uuid4().hex[:8]}"
    written = {cid for cid, _ in linked}
    for cid in sorted(written - set(legacy)):
        store.write(_chunk(cid))  # type: ignore[attr-defined]
    for cid in legacy:
        store.write(_chunk(cid, caused_by=turn_id))  # type: ignore[attr-defined]
    for cid, relation in linked:
        store.link_turn_chunks(turn_id, [cid], relation=relation)  # type: ignore[attr-defined]
    outcome = OutcomeRecord(turn_id=turn_id, success=True)
    assert store.record_outcome(outcome) is True  # type: ignore[attr-defined]
    # Exactly once: a replay of the same outcome id must not insert again.
    assert store.record_outcome(outcome) is False  # type: ignore[attr-defined]
    stored = store.get_outcome(outcome.outcome_id)  # type: ignore[attr-defined]
    assert stored is not None
    return stored.chunk_ids


@pytest.mark.parametrize("case", _CASES, ids=[c[0] for c in _CASES])
def test_sqlite_turn_resolution(tmp_path, case) -> None:
    store = SQLiteStore(tmp_path / "ncp.db")
    assert _sync_scenario(store, case) == case[3]


def _require_live() -> None:
    if not _LIVE:
        pytest.skip("set NCP_RUN_PGVECTOR_INTEGRATION=1 to run live pgvector tests")
    pytest.importorskip("psycopg")
    pytest.importorskip("psycopg_pool")


@pytest.mark.parametrize("case", _CASES, ids=[c[0] for c in _CASES])
def test_pgvector_sync_turn_resolution(case) -> None:
    _require_live()
    from ncp.stores.pgvector import PgvectorStore

    store = PgvectorStore(_DSN, schema=f"ncp_or_{uuid4().hex[:8]}", table_prefix="it_")
    assert _sync_scenario(store, case) == case[3]


@pytest.mark.anyio
@pytest.mark.parametrize("case", _CASES, ids=[c[0] for c in _CASES])
async def test_pgvector_async_turn_resolution(case) -> None:
    _require_live()
    from ncp.stores.pgvector_async import AsyncPgvectorStore

    _, linked, legacy, expected = case
    store = AsyncPgvectorStore(_DSN, schema=f"ncp_aor_{uuid4().hex[:8]}", table_prefix="it_")
    try:
        turn_id = f"turn_{uuid4().hex[:8]}"
        written = {cid for cid, _ in linked}
        for cid in sorted(written - set(legacy)):
            await store.async_write(_chunk(cid))
        for cid in legacy:
            await store.async_write(_chunk(cid, caused_by=turn_id))
        for cid, relation in linked:
            await store.async_link_turn_chunks(turn_id, [cid], relation=relation)

        outcome = OutcomeRecord(turn_id=turn_id, success=True)
        assert await store.async_record_outcome(outcome) is True
        # Exactly once: a replay of the same outcome id must not insert again.
        assert await store.async_record_outcome(outcome) is False
        stored = await store.async_get_outcome(outcome.outcome_id)
        assert stored is not None
        assert stored.chunk_ids == expected
    finally:
        await store.close()
