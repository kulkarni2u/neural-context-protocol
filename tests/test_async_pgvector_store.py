"""Tests for 0.8.x Slice 2: AsyncPgvectorStore — native async via psycopg3.

Tests fail because AsyncPgvectorStore doesn't exist yet.
After implementation, all tests pass: async methods are native (no to_thread shim).
"""

from __future__ import annotations

import sys
from unittest.mock import AsyncMock, MagicMock, patch

import anyio
import pytest

pytest.importorskip("psycopg", reason="psycopg extra not installed")
pytest.importorskip("psycopg_pool", reason="psycopg_pool extra not installed")


# ---------------------------------------------------------------------------
# Helpers: mock psycopg3 async pool and connection
# ---------------------------------------------------------------------------

def _make_async_pool() -> MagicMock:
    """Mock psycopg_pool.AsyncConnectionPool with async context manager."""
    pool = MagicMock()
    cursor = MagicMock()
    cursor.fetchall = AsyncMock(return_value=[])
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
    pool._conn = conn  # expose for assertions
    return pool


# ---------------------------------------------------------------------------
# Import + basic construction
# ---------------------------------------------------------------------------

def test_async_pgvector_store_importable() -> None:
    """AsyncPgvectorStore must be importable from ncp.stores.pgvector_async."""
    from ncp.stores.pgvector_async import AsyncPgvectorStore  # noqa: F401


def test_async_pgvector_store_is_basestore_subclass() -> None:
    """AsyncPgvectorStore must subclass BaseStore."""
    from ncp.stores.base import BaseStore
    from ncp.stores.pgvector_async import AsyncPgvectorStore

    assert issubclass(AsyncPgvectorStore, BaseStore)


def test_async_pgvector_store_can_be_constructed() -> None:
    """AsyncPgvectorStore(dsn) must not open the pool synchronously."""
    mock_pool = _make_async_pool()
    mock_pool_cls = MagicMock(return_value=mock_pool)

    with patch.dict(sys.modules, {"psycopg_pool": MagicMock(AsyncConnectionPool=mock_pool_cls)}):
        from ncp.stores import pgvector_async
        import importlib
        importlib.reload(pgvector_async)
        pgvector_async.AsyncPgvectorStore("postgresql://localhost/test")

    # Pool open must NOT have been called in __init__ (no async constructor)
    mock_pool.open.assert_not_called()


# ---------------------------------------------------------------------------
# async_write: must NOT use anyio.to_thread.run_sync
# ---------------------------------------------------------------------------

@pytest.mark.anyio
async def test_async_write_does_not_use_thread_shim() -> None:
    """async_write must be native async — not delegated to anyio.to_thread.run_sync."""
    from ncp.stores.pgvector_async import AsyncPgvectorStore
    from ncp.types import SubconsciousChunk

    mock_pool = _make_async_pool()

    with patch("psycopg_pool.AsyncConnectionPool", return_value=mock_pool):
        store = AsyncPgvectorStore("postgresql://localhost/test")

    chunk = SubconsciousChunk(
        chunk_id="async_test_chunk",
        layer="episodic",
        content="async write test content",
        src="tool_result",
    )

    call_log: list[str] = []

    original = anyio.to_thread.run_sync

    async def spy_run_sync(fn, *args, **kwargs):  # type: ignore[no-untyped-def]
        call_log.append(f"to_thread.run_sync called for {getattr(fn, '__name__', fn)}")
        return await original(fn, *args, **kwargs)

    with patch("anyio.to_thread.run_sync", side_effect=spy_run_sync):
        await store.async_write(chunk)

    assert not call_log, (
        f"async_write must not delegate to anyio.to_thread.run_sync, but got: {call_log}"
    )


# ---------------------------------------------------------------------------
# async_log_turn_record: must NOT use anyio.to_thread.run_sync
# ---------------------------------------------------------------------------

@pytest.mark.anyio
async def test_async_log_turn_record_does_not_use_thread_shim() -> None:
    """async_log_turn_record must be native async."""
    from ncp.stores.pgvector_async import AsyncPgvectorStore
    from ncp.types import TurnRecord

    mock_pool = _make_async_pool()

    with patch("psycopg_pool.AsyncConnectionPool", return_value=mock_pool):
        store = AsyncPgvectorStore("postgresql://localhost/test")

    record = TurnRecord(
        turn_id="t_async_01",
        agent_id="agent",
        pipeline_id="pipe_async",
        task="test_task",
        slot="test_slot",
        result="async test",
        result_full="async test full",
    )

    call_log: list[str] = []
    original = anyio.to_thread.run_sync

    async def spy(fn, *args, **kwargs):  # type: ignore[no-untyped-def]
        call_log.append(repr(fn))
        return await original(fn, *args, **kwargs)

    with patch("anyio.to_thread.run_sync", side_effect=spy):
        await store.async_log_turn_record(record)

    assert not call_log, f"async_log_turn_record must be native async, got: {call_log}"


# ---------------------------------------------------------------------------
# async_log_conscious: must NOT use anyio.to_thread.run_sync
# ---------------------------------------------------------------------------

@pytest.mark.anyio
async def test_async_log_conscious_does_not_use_thread_shim() -> None:
    """async_log_conscious must be native async."""
    from ncp.stores.pgvector_async import AsyncPgvectorStore
    from ncp.types import ConsciousBlock

    mock_pool = _make_async_pool()

    with patch("psycopg_pool.AsyncConnectionPool", return_value=mock_pool):
        store = AsyncPgvectorStore("postgresql://localhost/test")

    conscious = ConsciousBlock(
        agent_id="agent",
        role="build",
        owns=[],
        must_not=[],
        task="test_task",
        slot="test_slot",
        intent="test_intent",
    )

    call_log: list[str] = []
    original = anyio.to_thread.run_sync

    async def spy(fn, *args, **kwargs):  # type: ignore[no-untyped-def]
        call_log.append(repr(fn))
        return await original(fn, *args, **kwargs)

    with patch("anyio.to_thread.run_sync", side_effect=spy):
        await store.async_log_conscious(conscious, snapshot_hash="deadbeef")

    assert not call_log, f"async_log_conscious must be native async, got: {call_log}"


# ---------------------------------------------------------------------------
# async_log_cost: must NOT use anyio.to_thread.run_sync
# ---------------------------------------------------------------------------

@pytest.mark.anyio
async def test_async_log_cost_does_not_use_thread_shim() -> None:
    """async_log_cost must be native async."""
    from ncp.stores.pgvector_async import AsyncPgvectorStore
    from ncp.types import NCPResponse

    mock_pool = _make_async_pool()

    with patch("psycopg_pool.AsyncConnectionPool", return_value=mock_pool):
        store = AsyncPgvectorStore("postgresql://localhost/test")

    response = NCPResponse(
        content="result",
        input_tokens=100,
        output_tokens=50,
        cost_usd=0.001,
        model="claude-sonnet",
        pipeline_id="pipe_async",
        turn_id="t_cost_01",
        latency_ms=200,
    )

    call_log: list[str] = []
    original = anyio.to_thread.run_sync

    async def spy(fn, *args, **kwargs):  # type: ignore[no-untyped-def]
        call_log.append(repr(fn))
        return await original(fn, *args, **kwargs)

    with patch("anyio.to_thread.run_sync", side_effect=spy):
        await store.async_log_cost(agent_id="agent", response=response)

    assert not call_log, f"async_log_cost must be native async, got: {call_log}"


# ---------------------------------------------------------------------------
# Sync abstract methods must raise NotImplementedError
# ---------------------------------------------------------------------------

def test_sync_write_raises_not_implemented() -> None:
    """AsyncPgvectorStore sync write() must raise NotImplementedError."""
    from ncp.stores.pgvector_async import AsyncPgvectorStore
    from ncp.types import SubconsciousChunk

    mock_pool = _make_async_pool()

    with patch("psycopg_pool.AsyncConnectionPool", return_value=mock_pool):
        store = AsyncPgvectorStore("postgresql://localhost/test")

    chunk = SubconsciousChunk(
        chunk_id="sync_test",
        layer="episodic",
        content="test",
        src="tool_result",
    )
    with pytest.raises(NotImplementedError):
        store.write(chunk)


def test_sync_query_raises_not_implemented() -> None:
    """AsyncPgvectorStore sync query() must raise NotImplementedError."""
    from ncp.stores.pgvector_async import AsyncPgvectorStore

    mock_pool = _make_async_pool()

    with patch("psycopg_pool.AsyncConnectionPool", return_value=mock_pool):
        store = AsyncPgvectorStore("postgresql://localhost/test")

    with pytest.raises(NotImplementedError):
        store.query("test query")
