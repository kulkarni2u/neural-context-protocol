"""Tests for 0.9.x Slice 1: AsyncPgvectorStore dedup/GC parity.

All tests must be RED before implementation. After implementation, all tests pass.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

pytest.importorskip("psycopg", reason="psycopg extra not installed")
pytest.importorskip("psycopg_pool", reason="psycopg_pool extra not installed")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_async_pool() -> MagicMock:
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
    pool._conn = conn
    pool._cur = cursor
    return pool


def _make_store(pool: MagicMock):
    from ncp.stores.pgvector_async import AsyncPgvectorStore
    with patch("psycopg_pool.AsyncConnectionPool", return_value=pool):
        return AsyncPgvectorStore("postgresql://localhost/test")


def _chunk(**kwargs):
    from ncp.types import SubconsciousChunk
    defaults = dict(
        chunk_id="gc_test_chunk",
        layer="episodic",
        content="dedup gc test content for async parity",
        src="tool_result",
        pipeline_id="pipe_gc",
    )
    defaults.update(kwargs)
    return SubconsciousChunk(**defaults)


# ---------------------------------------------------------------------------
# Slice 1a: async_write calls dedup/GC helpers
# ---------------------------------------------------------------------------

@pytest.mark.anyio
async def test_async_write_calls_soft_gc() -> None:
    """async_write must call _async_soft_gc on every write."""
    pool = _make_async_pool()
    store = _make_store(pool)
    store._async_soft_gc = AsyncMock()
    store._async_assert_src_immutable = AsyncMock()
    store._async_is_duplicate = AsyncMock(return_value=False)
    store._async_hard_gc = AsyncMock()

    await store.async_write(_chunk())

    store._async_soft_gc.assert_called_once()


@pytest.mark.anyio
async def test_async_write_calls_src_immutable_check() -> None:
    """async_write must call _async_assert_src_immutable before inserting."""
    pool = _make_async_pool()
    store = _make_store(pool)
    store._async_soft_gc = AsyncMock()
    store._async_assert_src_immutable = AsyncMock()
    store._async_is_duplicate = AsyncMock(return_value=False)
    store._async_hard_gc = AsyncMock()

    chunk = _chunk()
    await store.async_write(chunk)

    store._async_assert_src_immutable.assert_called_once()
    _, called_chunk = store._async_assert_src_immutable.call_args[0]
    assert called_chunk.chunk_id == chunk.chunk_id


@pytest.mark.anyio
async def test_async_write_calls_is_duplicate() -> None:
    """async_write must call _async_is_duplicate for content dedup."""
    pool = _make_async_pool()
    store = _make_store(pool)
    store._async_soft_gc = AsyncMock()
    store._async_assert_src_immutable = AsyncMock()
    store._async_is_duplicate = AsyncMock(return_value=False)
    store._async_hard_gc = AsyncMock()

    await store.async_write(_chunk())

    store._async_is_duplicate.assert_called_once()


@pytest.mark.anyio
async def test_async_write_returns_false_when_duplicate() -> None:
    """async_write must return False (and skip INSERT) when _async_is_duplicate returns True."""
    pool = _make_async_pool()
    store = _make_store(pool)
    store._async_soft_gc = AsyncMock()
    store._async_assert_src_immutable = AsyncMock()
    store._async_is_duplicate = AsyncMock(return_value=True)
    store._async_hard_gc = AsyncMock()

    result = await store.async_write(_chunk())

    assert result is False
    # INSERT must NOT have been executed (schema init may call execute, but not INSERT)
    calls = [str(c[0][0]) for c in pool._cur.execute.call_args_list]
    assert not any("INSERT INTO" in s for s in calls), (
        "async_write must not INSERT when dedup returns True"
    )
    # hard_gc must NOT run when write was short-circuited by dedup
    store._async_hard_gc.assert_not_called()


@pytest.mark.anyio
async def test_async_write_calls_hard_gc() -> None:
    """async_write must call _async_hard_gc after a successful INSERT."""
    pool = _make_async_pool()
    store = _make_store(pool)
    store._async_soft_gc = AsyncMock()
    store._async_assert_src_immutable = AsyncMock()
    store._async_is_duplicate = AsyncMock(return_value=False)
    store._async_hard_gc = AsyncMock()

    chunk = _chunk()
    await store.async_write(chunk)

    store._async_hard_gc.assert_called_once()
    _, kwargs = store._async_hard_gc.call_args
    assert kwargs["pipeline_id"] == chunk.pipeline_id


@pytest.mark.anyio
async def test_async_write_on_conflict_updates_all_columns() -> None:
    """async_write ON CONFLICT SET must update all columns (not just 4)."""
    pool = _make_async_pool()
    store = _make_store(pool)
    store._async_soft_gc = AsyncMock()
    store._async_assert_src_immutable = AsyncMock()
    store._async_is_duplicate = AsyncMock(return_value=False)
    store._async_hard_gc = AsyncMock()

    await store.async_write(_chunk())

    cur = pool._cur
    # execute may have been called for schema init too — find the INSERT call
    calls = [str(c[0][0]) for c in cur.execute.call_args_list]
    insert_sql = next((s for s in calls if "INSERT INTO" in s), None)
    assert insert_sql is not None, "No INSERT INTO call found on cursor"
    sql = insert_sql

    expected_update_cols = [
        "pipeline_id",
        "scope",
        "zone",
        "layer",
        "chunk_type",
        "content",
        "src",
        "written_by",
        "caused_by",
        "conscious_hash",
        "evidence_id",
        "version",
        "supersedes",
        "source_refs",
        "schema_version",
        "created_at",
        "base_trust",
        "generation",
        "result_confidence",
        "result_attempts",
        "conditions",
        "valid_while",
        "expiry",
        "owner",
        "meta",
        "embedding",
    ]
    for col in expected_update_cols:
        assert f"{col} = EXCLUDED.{col}" in sql, (
            f"ON CONFLICT SET missing: {col} = EXCLUDED.{col}"
        )


# ---------------------------------------------------------------------------
# Slice 1b: __init__ accepts GC config params
# ---------------------------------------------------------------------------

def test_async_pgvector_store_accepts_gc_params() -> None:
    """AsyncPgvectorStore must accept max_working_chunks and gc_threshold."""
    pool = _make_async_pool()
    with patch("psycopg_pool.AsyncConnectionPool", return_value=pool):
        from ncp.stores.pgvector_async import AsyncPgvectorStore
        store = AsyncPgvectorStore(
            "postgresql://localhost/test",
            max_working_chunks=300,
            gc_threshold=200,
        )
    assert store.max_working_chunks == 300
    assert store.gc_threshold == 200


def test_async_pgvector_store_gc_params_defaults() -> None:
    """AsyncPgvectorStore must default max_working_chunks=500, gc_threshold=400."""
    pool = _make_async_pool()
    with patch("psycopg_pool.AsyncConnectionPool", return_value=pool):
        from ncp.stores.pgvector_async import AsyncPgvectorStore
        store = AsyncPgvectorStore("postgresql://localhost/test")
    assert store.max_working_chunks == 500
    assert store.gc_threshold == 400
