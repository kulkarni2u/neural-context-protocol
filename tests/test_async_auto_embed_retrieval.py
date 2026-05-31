"""Tests for 0.12.x: AsyncPgvectorStore auto-embed parity + retrieval-count update.

All tests RED before implementation, GREEN after.
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
    cursor.executemany = AsyncMock()
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


def _make_store(pool: MagicMock, **kwargs):
    from ncp.stores.pgvector_async import AsyncPgvectorStore
    with patch("psycopg_pool.AsyncConnectionPool", return_value=pool):
        return AsyncPgvectorStore("postgresql://localhost/test", **kwargs)


def _chunk(**kwargs):
    from ncp.types import SubconsciousChunk
    defaults = dict(
        chunk_id="embed_test_chunk",
        layer="semantic",
        content="authentication bearer token JWT session secure",
        src="tool_result",
    )
    defaults.update(kwargs)
    return SubconsciousChunk(**defaults)


def _fake_adapter(return_val: list[float] | None = None):
    adapter = MagicMock()
    adapter.embed = MagicMock(return_value=return_val or [0.1] * 1536)
    return adapter


# ---------------------------------------------------------------------------
# Slice 1a: __init__ accepts embedding_adapter
# ---------------------------------------------------------------------------

def test_async_store_accepts_embedding_adapter() -> None:
    """AsyncPgvectorStore must accept embedding_adapter kwarg."""
    pool = _make_async_pool()
    adapter = _fake_adapter()
    store = _make_store(pool, embedding_adapter=adapter)
    assert store._embedding_adapter is adapter


def test_async_store_embedding_adapter_defaults_none() -> None:
    """AsyncPgvectorStore without embedding_adapter must have _embedding_adapter=None."""
    pool = _make_async_pool()
    store = _make_store(pool)
    assert store._embedding_adapter is None


# ---------------------------------------------------------------------------
# Slice 1b: async_write auto-embeds when adapter set and no embedding
# ---------------------------------------------------------------------------

@pytest.mark.anyio
async def test_async_write_calls_adapter_when_no_embedding() -> None:
    """async_write must call adapter.embed() when chunk has no embedding."""
    pool = _make_async_pool()
    adapter = _fake_adapter()
    store = _make_store(pool, embedding_adapter=adapter)

    chunk = _chunk()  # no embedding
    assert chunk.embedding is None

    # Patch dedup helpers to avoid DB interaction
    store._async_soft_gc = AsyncMock()
    store._async_assert_src_immutable = AsyncMock()
    store._async_is_duplicate = AsyncMock(return_value=False)
    store._async_hard_gc = AsyncMock()

    await store.async_write(chunk)

    adapter.embed.assert_called_once_with(chunk.content)


@pytest.mark.anyio
async def test_async_write_skips_adapter_when_embedding_present() -> None:
    """async_write must NOT call adapter.embed() when chunk already has embedding."""
    pool = _make_async_pool()
    adapter = _fake_adapter()
    store = _make_store(pool, embedding_adapter=adapter)

    chunk = _chunk(embedding=[0.5] * 1536)

    store._async_soft_gc = AsyncMock()
    store._async_assert_src_immutable = AsyncMock()
    store._async_is_duplicate = AsyncMock(return_value=False)
    store._async_hard_gc = AsyncMock()

    await store.async_write(chunk)

    adapter.embed.assert_not_called()


@pytest.mark.anyio
async def test_async_write_no_adapter_no_embed_call() -> None:
    """async_write without adapter must not attempt any embedding."""
    pool = _make_async_pool()
    store = _make_store(pool)  # no adapter

    chunk = _chunk()  # no embedding
    store._async_soft_gc = AsyncMock()
    store._async_assert_src_immutable = AsyncMock()
    store._async_is_duplicate = AsyncMock(return_value=False)
    store._async_hard_gc = AsyncMock()

    # Should not raise
    await store.async_write(chunk)


@pytest.mark.anyio
async def test_async_write_embedding_stored_after_auto_embed() -> None:
    """The embedding from adapter.embed() must be included in the INSERT SQL."""
    pool = _make_async_pool()
    adapter = _fake_adapter([0.42] * 1536)
    store = _make_store(pool, embedding_adapter=adapter)

    chunk = _chunk()  # no embedding
    store._async_soft_gc = AsyncMock()
    store._async_assert_src_immutable = AsyncMock()
    store._async_is_duplicate = AsyncMock(return_value=False)
    store._async_hard_gc = AsyncMock()

    await store.async_write(chunk)

    cur = pool._cur
    # Find the INSERT execute call
    calls = [(str(c[0][0]), c[0][1]) for c in cur.execute.call_args_list
             if len(c[0]) > 0 and "INSERT INTO" in str(c[0][0])]
    assert calls, "No INSERT call found"
    insert_sql, insert_params = calls[0]
    # Last param is embedding_val — should be non-None (adapter returned floats)
    assert insert_params[-1] is not None, (
        "INSERT embedding param must be set when adapter auto-embedded"
    )


# ---------------------------------------------------------------------------
# Slice 2a: async_query increments retrieval_count on returned chunks
# ---------------------------------------------------------------------------

@pytest.mark.anyio
async def test_async_query_increments_retrieval_count_in_memory() -> None:
    """async_query must increment retrieval_count on returned chunk objects."""
    pool = _make_async_pool()
    store = _make_store(pool)

    fake_rows = [
        (
            "rcount_chunk_01", "pipe1", "pipeline", "working", "semantic", "prose",
            "authentication bearer token JWT session secure", "tool_result",
            "agent_a", None, None, None, 1, None, "[]", 1,
            1700000000.0, 0.8, 0, None, None, "[]", None, None, None, "{}", None,
            0, None  # retrieval_count=0, last_retrieved_at=None
        )
    ]
    pool._cur.description = [
        ("chunk_id",), ("pipeline_id",), ("scope",), ("zone",), ("layer",),
        ("chunk_type",), ("content",), ("src",), ("written_by",), ("caused_by",),
        ("conscious_hash",), ("evidence_id",), ("version",), ("supersedes",),
        ("source_refs",), ("schema_version",), ("created_at",), ("base_trust",),
        ("generation",), ("result_confidence",), ("result_attempts",),
        ("conditions",), ("valid_while",), ("expiry",), ("owner",), ("meta",),
        ("embedding",), ("retrieval_count",), ("last_retrieved_at",),
    ]
    pool._cur.fetchall = AsyncMock(return_value=fake_rows)

    results = await store.async_query(
        "authentication bearer token",
        k=4,
        retrieval_mode="trust_recency",
    )

    assert results, "Expected at least one result"
    assert results[0].retrieval_count == 1, (
        f"retrieval_count must be incremented to 1 after query, got {results[0].retrieval_count}"
    )


@pytest.mark.anyio
async def test_async_query_sets_last_retrieved_at() -> None:
    """async_query must set last_retrieved_at on returned chunk objects."""
    import time
    pool = _make_async_pool()
    store = _make_store(pool)

    fake_rows = [
        (
            "rcount_chunk_02", "pipe1", "pipeline", "working", "semantic", "prose",
            "authentication bearer token JWT session secure", "tool_result",
            "agent_a", None, None, None, 1, None, "[]", 1,
            1700000000.0, 0.8, 0, None, None, "[]", None, None, None, "{}", None,
            0, None
        )
    ]
    pool._cur.description = [
        ("chunk_id",), ("pipeline_id",), ("scope",), ("zone",), ("layer",),
        ("chunk_type",), ("content",), ("src",), ("written_by",), ("caused_by",),
        ("conscious_hash",), ("evidence_id",), ("version",), ("supersedes",),
        ("source_refs",), ("schema_version",), ("created_at",), ("base_trust",),
        ("generation",), ("result_confidence",), ("result_attempts",),
        ("conditions",), ("valid_while",), ("expiry",), ("owner",), ("meta",),
        ("embedding",), ("retrieval_count",), ("last_retrieved_at",),
    ]
    pool._cur.fetchall = AsyncMock(return_value=fake_rows)

    before = time.time()
    results = await store.async_query(
        "authentication bearer token",
        k=4,
        retrieval_mode="trust_recency",
    )

    assert results, "Expected at least one result"
    assert results[0].last_retrieved_at is not None, (
        "last_retrieved_at must be set after query"
    )
    assert results[0].last_retrieved_at >= before, (
        "last_retrieved_at must be >= query start time"
    )


@pytest.mark.anyio
async def test_async_query_executes_db_update_for_retrieval_count() -> None:
    """async_query must execute UPDATE retrieval_count on the DB for returned chunks."""
    pool = _make_async_pool()
    store = _make_store(pool)

    fake_rows = [
        (
            "rcount_chunk_03", "pipe1", "pipeline", "working", "semantic", "prose",
            "authentication bearer token JWT session secure", "tool_result",
            "agent_a", None, None, None, 1, None, "[]", 1,
            1700000000.0, 0.8, 0, None, None, "[]", None, None, None, "{}", None,
            0, None
        )
    ]
    pool._cur.description = [
        ("chunk_id",), ("pipeline_id",), ("scope",), ("zone",), ("layer",),
        ("chunk_type",), ("content",), ("src",), ("written_by",), ("caused_by",),
        ("conscious_hash",), ("evidence_id",), ("version",), ("supersedes",),
        ("source_refs",), ("schema_version",), ("created_at",), ("base_trust",),
        ("generation",), ("result_confidence",), ("result_attempts",),
        ("conditions",), ("valid_while",), ("expiry",), ("owner",), ("meta",),
        ("embedding",), ("retrieval_count",), ("last_retrieved_at",),
    ]
    pool._cur.fetchall = AsyncMock(return_value=fake_rows)

    await store.async_query(
        "authentication bearer token",
        k=4,
        retrieval_mode="trust_recency",
    )

    # Find the UPDATE execute call
    update_calls = [
        str(c[0][0]) for c in pool._cur.execute.call_args_list
        if "UPDATE" in str(c[0][0]) and "retrieval_count" in str(c[0][0])
    ]
    assert update_calls, (
        "async_query must execute UPDATE retrieval_count on DB, but none found"
    )
    assert "ANY" in update_calls[0], (
        "UPDATE must use ANY() for chunk_id list"
    )


@pytest.mark.anyio
async def test_async_query_no_update_on_empty_results() -> None:
    """async_query must NOT execute UPDATE when no chunks are returned."""
    pool = _make_async_pool()
    store = _make_store(pool)
    pool._cur.fetchall = AsyncMock(return_value=[])

    results = await store.async_query(
        "no match at all",
        k=4,
        retrieval_mode="trust_recency",
    )

    assert results == []
    update_calls = [
        c for c in pool._cur.execute.call_args_list
        if "UPDATE" in str(c[0][0]) and "retrieval_count" in str(c[0][0])
    ]
    assert not update_calls, (
        f"UPDATE must not fire when results list is empty, got: {update_calls}"
    )
