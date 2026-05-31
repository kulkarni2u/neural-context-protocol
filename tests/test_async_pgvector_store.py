"""Tests for 0.8.x Slice 2: AsyncPgvectorStore — native async via psycopg3.

Tests fail because AsyncPgvectorStore doesn't exist yet.
After implementation, all tests pass: async methods are native (no to_thread shim).
"""

from __future__ import annotations

from contextlib import asynccontextmanager
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


def _fake_aconnect():
    @asynccontextmanager
    async def _manager():
        yield object()

    return _manager


@pytest.mark.anyio
async def test_async_status_detail_is_native_and_matches_sync_shape() -> None:
    """async_status_detail should avoid thread shim and mirror sync status payload shape."""
    from ncp.stores.pgvector_async import AsyncPgvectorStore

    mock_pool = _make_async_pool()
    mock_coord = MagicMock()
    mock_coord.async_whisper_stats = AsyncMock(
        return_value={"count": 2, "last_activity_at": 250.0, "by_type": {"share": 1, "dissent": 1}}
    )

    with patch("psycopg_pool.AsyncConnectionPool", return_value=mock_pool):
        store = AsyncPgvectorStore("postgresql://localhost/test", coordination=mock_coord)

    store._aconnect = _fake_aconnect()  # type: ignore[method-assign]
    store._acount_rows = AsyncMock(side_effect=[3, 1, 1, 1, 2])  # type: ignore[method-assign]
    store._acount_distinct_pipelines = AsyncMock(return_value=2)  # type: ignore[method-assign]
    store._asum_cost = AsyncMock(return_value=0.031)  # type: ignore[method-assign]
    store._amax_column = AsyncMock(side_effect=[100.0, 150.0, 200.0])  # type: ignore[method-assign]
    store._alayer_counts = AsyncMock(return_value={"semantic": 2, "episodic": 1})  # type: ignore[method-assign]
    store._arecent_pipelines = AsyncMock(return_value=[{"pipeline_id": "pipe_async", "chunk_count": 3, "last_chunk_at": 100.0}])  # type: ignore[method-assign]

    call_log: list[str] = []
    original = anyio.to_thread.run_sync

    async def spy(fn, *args, **kwargs):  # type: ignore[no-untyped-def]
        call_log.append(repr(fn))
        return await original(fn, *args, **kwargs)

    with patch("anyio.to_thread.run_sync", side_effect=spy):
        detail = await store.async_status_detail(pipeline_id="pipe_async")

    assert not call_log, f"async_status_detail must be native async, got: {call_log}"
    assert detail["overview"]["chunk_count"] == 3
    assert detail["overview"]["whisper_count"] == 2
    assert detail["overview"]["last_activity_at"] == 250.0
    assert detail["layer_counts"] == {"semantic": 2, "episodic": 1}
    assert detail["recent_pipelines"][0]["pipeline_id"] == "pipe_async"
    mock_coord.async_whisper_stats.assert_awaited_once_with(pipeline_id="pipe_async")


@pytest.mark.anyio
async def test_async_cost_summary_is_native_and_matches_sync_shape() -> None:
    """async_cost_summary should avoid thread shim and mirror sync cost payload shape."""
    from ncp.stores.pgvector_async import AsyncPgvectorStore

    mock_pool = _make_async_pool()
    with patch("psycopg_pool.AsyncConnectionPool", return_value=mock_pool):
        store = AsyncPgvectorStore("postgresql://localhost/test")

    store._aconnect = _fake_aconnect()  # type: ignore[method-assign]
    store._acost_summary_row = AsyncMock(return_value={  # type: ignore[method-assign]
        "cost_usd_total": 0.02,
        "input_tokens_total": 100,
        "output_tokens_total": 20,
        "cache_read_tokens_total": 0,
        "entry_count": 1,
        "avg_latency_ms": 123.0,
    })
    store._acost_group_rows = AsyncMock(side_effect=[  # type: ignore[method-assign]
        [{"agent_id": "planner", "turns": 1, "cost_usd_total": 0.02}],
        [{"model": "claude-sonnet", "turns": 1, "cost_usd_total": 0.02}],
    ])
    store._arecent_cost_rows = AsyncMock(return_value=[  # type: ignore[method-assign]
        {
            "turn_id": "turn_cost_01",
            "pipeline_id": "pipe_async",
            "agent_id": "planner",
            "model": "claude-sonnet",
            "input_tokens": 100,
            "output_tokens": 20,
            "cache_read_tokens": 0,
            "cost_usd": 0.02,
            "latency_ms": 123,
            "logged_at": 250.0,
        }
    ])

    call_log: list[str] = []
    original = anyio.to_thread.run_sync

    async def spy(fn, *args, **kwargs):  # type: ignore[no-untyped-def]
        call_log.append(repr(fn))
        return await original(fn, *args, **kwargs)

    with patch("anyio.to_thread.run_sync", side_effect=spy):
        costs = await store.async_cost_summary(pipeline_id="pipe_async", limit=5)

    assert not call_log, f"async_cost_summary must be native async, got: {call_log}"
    assert costs["summary"]["entry_count"] == 1
    assert costs["by_agent"][0]["agent_id"] == "planner"
    assert costs["by_model"][0]["model"] == "claude-sonnet"
    assert costs["recent_entries"][0]["turn_id"] == "turn_cost_01"


@pytest.mark.anyio
async def test_async_viz_data_is_native_and_uses_async_whisper_stats() -> None:
    """async_viz_data should avoid thread shim and surface whisper queue details."""
    from ncp.stores.pgvector_async import AsyncPgvectorStore

    mock_pool = _make_async_pool()
    mock_coord = MagicMock()
    mock_coord.async_whisper_stats = AsyncMock(
        return_value={"count": 3, "last_activity_at": 300.0, "by_type": {"share": 2, "dissent": 1}}
    )

    with patch("psycopg_pool.AsyncConnectionPool", return_value=mock_pool):
        store = AsyncPgvectorStore("postgresql://localhost/test", coordination=mock_coord)

    store._aconnect = _fake_aconnect()  # type: ignore[method-assign]
    store._achunk_distribution = AsyncMock(return_value=[  # type: ignore[method-assign]
        {"layer": "semantic", "zone": "working", "count": 2}
    ])
    store._aage_brackets = AsyncMock(return_value=[  # type: ignore[method-assign]
        {"bracket": "<1h", "count": 2, "avg_trust": 0.7, "top_layer": "semantic"}
    ])
    store._atop_chunks = AsyncMock(return_value=[  # type: ignore[method-assign]
        {"chunk_id": "sub_123", "layer": "semantic", "zone": "working", "pipeline_id": "pipe_async", "base_trust": 0.9, "age_seconds": 12.0}
    ])
    store._apipeline_summary = AsyncMock(return_value=[  # type: ignore[method-assign]
        {"pipeline_id": "pipe_async", "chunk_count": 2, "last_activity": 123.0}
    ])

    call_log: list[str] = []
    original = anyio.to_thread.run_sync

    async def spy(fn, *args, **kwargs):  # type: ignore[no-untyped-def]
        call_log.append(repr(fn))
        return await original(fn, *args, **kwargs)

    with patch("anyio.to_thread.run_sync", side_effect=spy):
        data = await store.async_viz_data(pipeline_id="pipe_async")

    assert not call_log, f"async_viz_data must be native async, got: {call_log}"
    assert data["chunk_distribution"][0]["count"] == 2
    assert data["whisper_queue"] == {"total": 3, "by_type": {"share": 2, "dissent": 1}}
    mock_coord.async_whisper_stats.assert_awaited_once_with(pipeline_id="pipe_async")
