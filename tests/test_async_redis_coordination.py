"""Tests for 0.9.x Slice 2: AsyncRedisCoordination + native async whispers.

All tests must be RED before implementation. After implementation, all tests pass.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import anyio
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
    return pool


def _make_store(pool: MagicMock, **kwargs):
    from ncp.stores.pgvector_async import AsyncPgvectorStore
    with patch("psycopg_pool.AsyncConnectionPool", return_value=pool):
        return AsyncPgvectorStore("postgresql://localhost/test", **kwargs)


def _whisper():
    from ncp.types import Whisper
    return Whisper(
        whisper_id="w_async_01",
        from_agent="claude",
        target="opencode",
        whisper_type="nudge",
        payload="test async whisper payload",
        confidence=0.9,
        pipeline_id="pipe_async",
    )


# ---------------------------------------------------------------------------
# Slice 2a: AsyncRedisCoordination importable
# ---------------------------------------------------------------------------

def test_async_redis_coordination_importable() -> None:
    """AsyncRedisCoordination must be importable from ncp.stores.redis_coordination."""
    from ncp.stores.redis_coordination import AsyncRedisCoordination  # noqa: F401


def test_async_redis_coordination_can_be_constructed() -> None:
    """AsyncRedisCoordination(url) must not connect on construction."""
    from ncp.stores.redis_coordination import AsyncRedisCoordination

    mock_factory = MagicMock(return_value=MagicMock())
    coord = AsyncRedisCoordination("redis://localhost", client_factory=mock_factory)
    # factory must NOT have been called yet (lazy init)
    mock_factory.assert_not_called()
    assert coord.url == "redis://localhost"


# ---------------------------------------------------------------------------
# Slice 2b: AsyncPgvectorStore accepts redis_url
# ---------------------------------------------------------------------------

def test_async_pgvector_store_accepts_redis_url() -> None:
    """AsyncPgvectorStore must accept redis_url kwarg."""
    pool = _make_async_pool()
    store = _make_store(pool, redis_url="redis://localhost:6379")
    assert store._acoordination is not None


def test_async_pgvector_store_with_redis_url_gets_async_coordination() -> None:
    """AsyncPgvectorStore(redis_url=...) must store an AsyncRedisCoordination instance."""
    from ncp.stores.redis_coordination import AsyncRedisCoordination

    pool = _make_async_pool()
    store = _make_store(pool, redis_url="redis://localhost:6379")
    assert isinstance(store._acoordination, AsyncRedisCoordination)


def test_async_pgvector_store_no_redis_url_has_none_coordination() -> None:
    """AsyncPgvectorStore without redis_url must have _acoordination = None."""
    pool = _make_async_pool()
    store = _make_store(pool)
    assert store._acoordination is None


def test_async_pgvector_store_accepts_coordination_object() -> None:
    """AsyncPgvectorStore must accept a pre-built coordination= kwarg."""
    from ncp.stores.redis_coordination import AsyncRedisCoordination

    pool = _make_async_pool()
    mock_coord = MagicMock(spec=AsyncRedisCoordination)
    store = _make_store(pool, coordination=mock_coord)
    assert store._acoordination is mock_coord


# ---------------------------------------------------------------------------
# Slice 2c: async_emit_whisper does NOT use anyio.to_thread.run_sync
# ---------------------------------------------------------------------------

@pytest.mark.anyio
async def test_async_emit_whisper_does_not_use_thread_shim() -> None:
    """async_emit_whisper must NOT delegate to anyio.to_thread.run_sync."""
    pool = _make_async_pool()

    mock_coord = MagicMock()
    mock_coord.emit_whisper = AsyncMock()
    store = _make_store(pool, coordination=mock_coord)

    call_log: list[str] = []
    original = anyio.to_thread.run_sync

    async def spy(fn, *args, **kwargs):  # type: ignore[no-untyped-def]
        call_log.append(repr(fn))
        return await original(fn, *args, **kwargs)

    with patch("anyio.to_thread.run_sync", side_effect=spy):
        await store.async_emit_whisper(_whisper())

    assert not call_log, (
        f"async_emit_whisper must not use thread shim, but got: {call_log}"
    )
    mock_coord.emit_whisper.assert_called_once()


@pytest.mark.anyio
async def test_async_drain_whispers_does_not_use_thread_shim() -> None:
    """async_drain_whispers must NOT delegate to anyio.to_thread.run_sync."""
    pool = _make_async_pool()

    mock_coord = MagicMock()
    mock_coord.drain_whispers = AsyncMock(return_value=[])
    store = _make_store(pool, coordination=mock_coord)

    call_log: list[str] = []
    original = anyio.to_thread.run_sync

    async def spy(fn, *args, **kwargs):  # type: ignore[no-untyped-def]
        call_log.append(repr(fn))
        return await original(fn, *args, **kwargs)

    with patch("anyio.to_thread.run_sync", side_effect=spy):
        result = await store.async_drain_whispers(
            agent_id="claude",
            pipeline_id="pipe_async",
            max_items=3,
            min_confidence=0.60,
        )

    assert not call_log, (
        f"async_drain_whispers must not use thread shim, but got: {call_log}"
    )
    mock_coord.drain_whispers.assert_called_once_with(
        agent_id="claude",
        pipeline_id="pipe_async",
        max_items=3,
        min_confidence=0.60,
    )
    assert result == []


# ---------------------------------------------------------------------------
# Slice 2d: emit_whisper / drain_whispers raise when no coordination
# ---------------------------------------------------------------------------

@pytest.mark.anyio
async def test_async_emit_whisper_raises_without_redis() -> None:
    """async_emit_whisper must raise NCPStoreUnavailableError when no coordination configured."""
    from ncp.stores.base import NCPStoreUnavailableError

    pool = _make_async_pool()
    store = _make_store(pool)  # no redis_url

    with pytest.raises(NCPStoreUnavailableError, match="[Rr]edis"):
        await store.async_emit_whisper(_whisper())


@pytest.mark.anyio
async def test_async_drain_whispers_raises_without_redis() -> None:
    """async_drain_whispers must raise NCPStoreUnavailableError when no coordination configured."""
    from ncp.stores.base import NCPStoreUnavailableError

    pool = _make_async_pool()
    store = _make_store(pool)  # no redis_url

    with pytest.raises(NCPStoreUnavailableError, match="[Rr]edis"):
        await store.async_drain_whispers(agent_id="claude")
