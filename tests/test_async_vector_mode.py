"""Tests for 0.13.x Slice 1: async_query vector mode + ivfflat_probes.

Spec for OpenCode implementer:
- Remove the ValueError that currently blocks retrieval_mode='vector' in async_query
- Add ivfflat_probes: int = 10 param to AsyncPgvectorStore.__init__; store as self._ivfflat_probes
- Implement _async_query_vector matching sync _query_vector:
    * resolve embedding (auto-embed via to_thread if adapter set, else raise ValueError)
    * validate len(embedding) == 1536
    * SET LOCAL ivfflat.probes = %s before SELECT
    * SELECT *, (embedding <=> %s::vector) AS vec_distance ... WHERE embedding IS NOT NULL ... ORDER BY
    * filter by min_score: score = 1.0 / (1.0 + distance)
    * apply diversity loop using _diversity_cap = max(1, diversity_limit)
    * fire retrieval_count UPDATE on returned chunks (same as hybrid path)
    * return results

All tests are RED before implementation. GREEN after.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

pytest.importorskip("psycopg", reason="psycopg extra not installed")
pytest.importorskip("psycopg_pool", reason="psycopg_pool extra not installed")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_async_pool(fake_rows: list = None) -> MagicMock:
    pool = MagicMock()
    cursor = MagicMock()
    cursor.fetchall = AsyncMock(return_value=fake_rows or [])
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


def _make_store(pool, **kwargs):
    from ncp.stores.pgvector_async import AsyncPgvectorStore
    with patch("psycopg_pool.AsyncConnectionPool", return_value=pool):
        return AsyncPgvectorStore("postgresql://localhost/test", **kwargs)


EMBEDDING = [0.1] * 1536


def _vec_row(chunk_id: str, distance: float = 0.1, author: str = "agent_a") -> tuple:
    return (
        chunk_id, "pipe1", "pipeline", "working", "semantic", "prose",
        f"content for {chunk_id}", "tool_result",
        author, None, None, None, 1, None, "[]", 1,
        1700000000.0, 0.8, 0, None, None, "[]", None, None, None, "{}", None,
        0, None, distance,  # retrieval_count=0, last_retrieved_at=None, vec_distance
    )


# ---------------------------------------------------------------------------
# Slice 1a: __init__ accepts ivfflat_probes
# ---------------------------------------------------------------------------

def test_async_store_accepts_ivfflat_probes() -> None:
    """AsyncPgvectorStore must accept ivfflat_probes kwarg."""
    pool = _make_async_pool()
    store = _make_store(pool, ivfflat_probes=20)
    assert store._ivfflat_probes == 20


def test_async_store_ivfflat_probes_default() -> None:
    """AsyncPgvectorStore must default ivfflat_probes=10."""
    pool = _make_async_pool()
    store = _make_store(pool)
    assert store._ivfflat_probes == 10


# ---------------------------------------------------------------------------
# Slice 1b: vector mode no longer raises
# ---------------------------------------------------------------------------

@pytest.mark.anyio
async def test_async_query_vector_mode_does_not_raise() -> None:
    """async_query with retrieval_mode='vector' must not raise ValueError."""
    pool = _make_async_pool()
    store = _make_store(pool)

    # Should not raise
    results = await store.async_query(
        "test query",
        k=4,
        retrieval_mode="vector",
        embedding=EMBEDDING,
    )
    assert isinstance(results, list)


# ---------------------------------------------------------------------------
# Slice 1c: vector SQL uses cosine operator
# ---------------------------------------------------------------------------

@pytest.mark.anyio
async def test_async_vector_query_uses_cosine_operator() -> None:
    """async_query vector mode must issue SQL with <=> cosine distance operator."""
    pool = _make_async_pool()
    store = _make_store(pool)

    await store.async_query(
        "test query",
        k=4,
        retrieval_mode="vector",
        embedding=EMBEDDING,
    )

    all_sql = [str(c[0][0]) for c in pool._cur.execute.call_args_list]
    assert any("<=>" in sql for sql in all_sql), (
        f"No <=> cosine operator found in any SQL call: {all_sql}"
    )


# ---------------------------------------------------------------------------
# Slice 1d: SET LOCAL ivfflat.probes fires before SELECT
# ---------------------------------------------------------------------------

@pytest.mark.anyio
async def test_async_vector_sets_ivfflat_probes() -> None:
    """async_query vector mode must SET LOCAL ivfflat.probes before SELECT."""
    pool = _make_async_pool()
    store = _make_store(pool, ivfflat_probes=15)

    await store.async_query(
        "test query",
        k=4,
        retrieval_mode="vector",
        embedding=EMBEDDING,
    )

    probes_calls = [
        c for c in pool._cur.execute.call_args_list
        if "ivfflat.probes" in str(c[0][0])
    ]
    assert probes_calls, "SET LOCAL ivfflat.probes must be called in vector mode"
    params = probes_calls[0][0][1]
    assert 15 in params or (15,) == params, (
        f"ivfflat.probes must be set to 15, got params={params}"
    )


# ---------------------------------------------------------------------------
# Slice 1e: raises ValueError without embedding and without adapter
# ---------------------------------------------------------------------------

@pytest.mark.anyio
async def test_async_vector_raises_without_embedding_or_adapter() -> None:
    """async_query vector mode must raise ValueError when no embedding and no adapter."""
    pool = _make_async_pool()
    store = _make_store(pool)  # no adapter

    with pytest.raises(ValueError, match="embedding"):
        await store.async_query(
            "test query",
            k=4,
            retrieval_mode="vector",
            # no embedding
        )


# ---------------------------------------------------------------------------
# Slice 1f: auto-embeds via adapter in vector mode
# ---------------------------------------------------------------------------

@pytest.mark.anyio
async def test_async_vector_auto_embeds_from_adapter() -> None:
    """async_query vector mode must call adapter.embed(text) when no embedding provided."""
    pool = _make_async_pool()
    adapter = MagicMock()
    adapter.embed = MagicMock(return_value=EMBEDDING)
    store = _make_store(pool, embedding_adapter=adapter)

    await store.async_query(
        "authentication bearer",
        k=4,
        retrieval_mode="vector",
        # no embedding — should auto-embed
    )

    adapter.embed.assert_called_once_with("authentication bearer")


# ---------------------------------------------------------------------------
# Slice 1g: raises ValueError for wrong embedding dimensions
# ---------------------------------------------------------------------------

@pytest.mark.anyio
async def test_async_vector_raises_wrong_dimensions() -> None:
    """async_query vector mode must raise ValueError for non-1536-dim embedding."""
    pool = _make_async_pool()
    store = _make_store(pool)

    with pytest.raises(ValueError, match="1536"):
        await store.async_query(
            "test",
            k=4,
            retrieval_mode="vector",
            embedding=[0.1] * 512,  # wrong dims
        )


# ---------------------------------------------------------------------------
# Slice 1h: diversity loop applies in vector mode
# ---------------------------------------------------------------------------

@pytest.mark.anyio
async def test_async_vector_diversity_loop_applied() -> None:
    """async_query vector mode must apply diversity_limit per-author."""
    # 4 rows all from agent_a — diversity_limit=1 should return at most 1
    fake_rows = [_vec_row(f"v_{i}", distance=0.1) for i in range(4)]
    pool = _make_async_pool(fake_rows)
    pool._cur.description = [
        ("chunk_id",), ("pipeline_id",), ("scope",), ("zone",), ("layer",),
        ("chunk_type",), ("content",), ("src",), ("written_by",), ("caused_by",),
        ("conscious_hash",), ("evidence_id",), ("version",), ("supersedes",),
        ("source_refs",), ("schema_version",), ("created_at",), ("base_trust",),
        ("generation",), ("result_confidence",), ("result_attempts",),
        ("conditions",), ("valid_while",), ("expiry",), ("owner",), ("meta",),
        ("embedding",), ("retrieval_count",), ("last_retrieved_at",), ("vec_distance",),
    ]
    store = _make_store(pool)

    results = await store.async_query(
        "test",
        k=10,
        retrieval_mode="vector",
        embedding=EMBEDDING,
        diversity_limit=1,
    )

    agent_a = sum(1 for c in results if c.written_by == "agent_a")
    assert agent_a <= 1, f"diversity_limit=1 violated in vector mode: agent_a={agent_a}"


# ---------------------------------------------------------------------------
# Slice 1i: retrieval count updated after vector query
# ---------------------------------------------------------------------------

@pytest.mark.anyio
async def test_async_vector_updates_retrieval_count() -> None:
    """async_query vector mode must UPDATE retrieval_count for returned chunks."""
    fake_rows = [_vec_row("vec_rc_01", distance=0.1)]
    pool = _make_async_pool(fake_rows)
    pool._cur.description = [
        ("chunk_id",), ("pipeline_id",), ("scope",), ("zone",), ("layer",),
        ("chunk_type",), ("content",), ("src",), ("written_by",), ("caused_by",),
        ("conscious_hash",), ("evidence_id",), ("version",), ("supersedes",),
        ("source_refs",), ("schema_version",), ("created_at",), ("base_trust",),
        ("generation",), ("result_confidence",), ("result_attempts",),
        ("conditions",), ("valid_while",), ("expiry",), ("owner",), ("meta",),
        ("embedding",), ("retrieval_count",), ("last_retrieved_at",), ("vec_distance",),
    ]
    store = _make_store(pool)

    results = await store.async_query(
        "test",
        k=4,
        retrieval_mode="vector",
        embedding=EMBEDDING,
    )

    update_calls = [
        c for c in pool._cur.execute.call_args_list
        if "UPDATE" in str(c[0][0]) and "retrieval_count" in str(c[0][0])
    ]
    assert update_calls, "vector mode must fire retrieval_count UPDATE"
    if results:
        assert results[0].retrieval_count == 1
