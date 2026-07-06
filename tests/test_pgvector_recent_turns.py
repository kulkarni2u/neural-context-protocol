"""Sprint 5d Part 2: pgvector parity for BaseStore.recent_turns (CAP-T5 drift).

SQLiteStore.recent_turns has been real since Sprint 5c; PgvectorStore and
AsyncPgvectorStore previously fell back to BaseStore's empty-list default,
which silently degrades computed drift (ncp/drift.py) to 0.0 on pgvector
deployments. These tests exercise the real implementations added here,
mirroring SQLiteStore's oldest-first / pipeline-scoped semantics exactly.

Uses the existing lightweight MagicMock connection pattern (see
tests/test_psycopg3_upgrade.py) for the sync store, and the
AsyncMock pool/connection/cursor pattern (see
tests/test_async_pgvector_store.py) for the async store -- no live Postgres
required.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

pytest.importorskip("psycopg", reason="pgvector extra not installed")

from ncp.stores.pgvector import PgvectorStore  # noqa: E402


def _turn_row(index: int, created_at: float, *, pipeline_id: str = "pipe_recent") -> dict:
    return {
        "turn_id": f"turn_{index}",
        "agent_id": "planner",
        "pipeline_id": pipeline_id,
        "task": "task",
        "slot": "slot",
        "result": f"result_{index}",
        "result_full": "full",
        "created_at": created_at,
        "expires_at": created_at + 1000,
    }


# ---------------------------------------------------------------------------
# Sync PgvectorStore.recent_turns
# ---------------------------------------------------------------------------

def _make_mock_conn(rows: list[dict]) -> MagicMock:
    conn = MagicMock()
    conn.cursor.return_value.fetchall.return_value = rows
    conn.cursor.return_value.fetchone.return_value = None
    conn.cursor.return_value.description = []
    return conn


def test_pgvector_recent_turns_returns_oldest_first() -> None:
    """PgvectorStore.recent_turns must reverse the DB's DESC order to oldest-first."""
    # The real SQL orders DESC LIMIT n; the mock returns rows already in that order.
    rows = [_turn_row(3, 400.0), _turn_row(2, 300.0), _turn_row(1, 200.0), _turn_row(0, 100.0)]
    conn = _make_mock_conn(rows)
    store = PgvectorStore("postgresql://localhost/test", connect_factory=lambda dsn: conn)

    result = store.recent_turns(pipeline_id="pipe_recent", limit=20)

    assert [r.turn_id for r in result] == ["turn_0", "turn_1", "turn_2", "turn_3"]


def test_pgvector_recent_turns_respects_limit_query_param() -> None:
    """limit must be passed through to the SQL LIMIT, not just sliced client-side."""
    rows = [_turn_row(3, 400.0), _turn_row(2, 300.0)]
    conn = _make_mock_conn(rows)
    store = PgvectorStore("postgresql://localhost/test", connect_factory=lambda dsn: conn)

    result = store.recent_turns(pipeline_id="pipe_recent", limit=2)

    assert [r.turn_id for r in result] == ["turn_2", "turn_3"]
    executed_sql, params = conn.cursor.return_value.execute.call_args_list[-1][0]
    assert "turn_records" in executed_sql
    assert "ORDER BY created_at DESC" in executed_sql
    assert params[-1] == 2


def test_pgvector_recent_turns_zero_limit_short_circuits_without_query() -> None:
    conn = _make_mock_conn([])
    store = PgvectorStore("postgresql://localhost/test", connect_factory=lambda dsn: conn)

    assert store.recent_turns(pipeline_id="pipe_recent", limit=0) == []
    # _init_db issues its own schema-bootstrap execute() (which legitimately
    # mentions turn_records in a CREATE TABLE); recent_turns(limit=0) must not
    # add a SELECT on top of that.
    calls_before = conn.cursor.return_value.execute.call_args_list
    assert not any("SELECT" in str(c) and "turn_records" in str(c) for c in calls_before)


def test_pgvector_recent_turns_none_pipeline_id_queries_is_null() -> None:
    conn = _make_mock_conn([_turn_row(0, 100.0, pipeline_id=None)])  # type: ignore[arg-type]
    store = PgvectorStore("postgresql://localhost/test", connect_factory=lambda dsn: conn)

    store.recent_turns(pipeline_id=None, limit=5)

    executed_sql, params = conn.cursor.return_value.execute.call_args_list[-1][0]
    assert "pipeline_id IS NULL" in executed_sql
    assert params == (5,)


# ---------------------------------------------------------------------------
# Async AsyncPgvectorStore.async_recent_turns
# ---------------------------------------------------------------------------

def _make_async_pool(rows: list[dict]) -> MagicMock:
    pool = MagicMock()
    cursor = MagicMock()
    cursor.fetchall = AsyncMock(return_value=rows)
    cursor.fetchone = AsyncMock(return_value=None)
    cursor.execute = AsyncMock()
    cursor.description = []
    cursor.__aenter__ = AsyncMock(return_value=cursor)
    cursor.__aexit__ = AsyncMock(return_value=False)

    conn = MagicMock()
    conn.cursor = MagicMock(return_value=cursor)
    conn.commit = AsyncMock()
    conn.rollback = AsyncMock()
    conn.__aenter__ = AsyncMock(return_value=conn)
    conn.__aexit__ = AsyncMock(return_value=False)

    pool.open = AsyncMock()
    pool.close = AsyncMock()
    pool.connection = MagicMock()
    pool.connection.return_value.__aenter__ = AsyncMock(return_value=conn)
    pool.connection.return_value.__aexit__ = AsyncMock(return_value=False)
    pool._conn = conn
    pool._cur = cursor
    return pool


@pytest.mark.anyio
async def test_async_pgvector_recent_turns_returns_oldest_first() -> None:
    pytest.importorskip("psycopg_pool", reason="psycopg_pool extra not installed")
    from ncp.stores.pgvector_async import AsyncPgvectorStore

    rows = [_turn_row(3, 400.0), _turn_row(2, 300.0), _turn_row(1, 200.0), _turn_row(0, 100.0)]
    mock_pool = _make_async_pool(rows)

    with patch("psycopg_pool.AsyncConnectionPool", return_value=mock_pool):
        store = AsyncPgvectorStore("postgresql://localhost/test")

    result = await store.async_recent_turns(pipeline_id="pipe_recent", limit=20)

    assert [r.turn_id for r in result] == ["turn_0", "turn_1", "turn_2", "turn_3"]


@pytest.mark.anyio
async def test_async_pgvector_recent_turns_zero_limit_short_circuits() -> None:
    pytest.importorskip("psycopg_pool", reason="psycopg_pool extra not installed")
    from ncp.stores.pgvector_async import AsyncPgvectorStore

    mock_pool = _make_async_pool([])

    with patch("psycopg_pool.AsyncConnectionPool", return_value=mock_pool):
        store = AsyncPgvectorStore("postgresql://localhost/test")

    assert await store.async_recent_turns(pipeline_id="pipe_recent", limit=0) == []
    mock_pool._cur.execute.assert_not_called()


@pytest.mark.anyio
async def test_async_pgvector_recent_turns_does_not_use_thread_shim() -> None:
    pytest.importorskip("psycopg_pool", reason="psycopg_pool extra not installed")
    import anyio

    from ncp.stores.pgvector_async import AsyncPgvectorStore

    mock_pool = _make_async_pool([_turn_row(0, 100.0)])

    with patch("psycopg_pool.AsyncConnectionPool", return_value=mock_pool):
        store = AsyncPgvectorStore("postgresql://localhost/test")

    call_log: list[str] = []
    original = anyio.to_thread.run_sync

    async def spy_run_sync(fn, *args, **kwargs):  # type: ignore[no-untyped-def]
        call_log.append(f"to_thread.run_sync called for {getattr(fn, '__name__', fn)}")
        return await original(fn, *args, **kwargs)

    with patch("anyio.to_thread.run_sync", side_effect=spy_run_sync):
        await store.async_recent_turns(pipeline_id="pipe_recent", limit=5)

    assert not call_log, (
        f"async_recent_turns must not delegate to anyio.to_thread.run_sync, got: {call_log}"
    )
