"""Tests for 0.13.x Slice 2: async_query retrieval_mode validation parity.

Spec for Codex implementer:
- async_query currently has NO retrieval_mode validation — unknown modes silently
  fall through to BM25 hybrid scoring instead of raising ValueError
- PgvectorStore.query() validates: if retrieval_mode not in ("hybrid","trust_recency","vector"):
    raise ValueError(f"Unknown retrieval_mode {retrieval_mode!r}; ...")
- Add identical validation to AsyncPgvectorStore.async_query() BEFORE the
  retrieval_mode == "vector" check

All tests RED before implementation. GREEN after.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

pytest.importorskip("psycopg", reason="psycopg extra not installed")
pytest.importorskip("psycopg_pool", reason="psycopg_pool extra not installed")


def _make_store(**kwargs):
    pool = MagicMock()
    cursor = MagicMock()
    cursor.fetchall = AsyncMock(return_value=[])
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
    pool.connection = MagicMock()
    pool.connection.return_value.__aenter__ = AsyncMock(return_value=conn)
    pool.connection.return_value.__aexit__ = AsyncMock(return_value=False)

    from ncp.stores.pgvector_async import AsyncPgvectorStore
    with patch("psycopg_pool.AsyncConnectionPool", return_value=pool):
        return AsyncPgvectorStore("postgresql://localhost/test", **kwargs)


# ---------------------------------------------------------------------------
# Slice 2: retrieval_mode validation
# ---------------------------------------------------------------------------

@pytest.mark.anyio
async def test_async_query_rejects_unknown_retrieval_mode() -> None:
    """async_query must raise ValueError for unknown retrieval_mode."""
    store = _make_store()
    with pytest.raises(ValueError, match="retrieval_mode"):
        await store.async_query("test", retrieval_mode="unknown_mode")


@pytest.mark.anyio
async def test_async_query_rejects_typo_mode() -> None:
    """async_query must raise ValueError for typo like 'hybrd'."""
    store = _make_store()
    with pytest.raises(ValueError):
        await store.async_query("test", retrieval_mode="hybrd")


@pytest.mark.anyio
async def test_async_query_accepts_hybrid() -> None:
    """async_query must accept retrieval_mode='hybrid' without raising."""
    store = _make_store()
    results = await store.async_query("test", retrieval_mode="hybrid")
    assert isinstance(results, list)


@pytest.mark.anyio
async def test_async_query_accepts_trust_recency() -> None:
    """async_query must accept retrieval_mode='trust_recency' without raising."""
    store = _make_store()
    results = await store.async_query("test", retrieval_mode="trust_recency")
    assert isinstance(results, list)


@pytest.mark.anyio
async def test_async_query_accepts_vector() -> None:
    """async_query must accept retrieval_mode='vector' without raising (after Slice 1)."""
    store = _make_store()
    # vector with embedding must not raise ValueError for unknown mode
    # (may raise for missing embedding — that's a different ValueError)
    try:
        await store.async_query("test", retrieval_mode="vector", embedding=[0.1] * 1536)
    except ValueError as e:
        assert "retrieval_mode" not in str(e).lower() or "unknown" not in str(e).lower(), (
            f"vector mode should not raise unknown retrieval_mode error: {e}"
        )


@pytest.mark.anyio
async def test_async_query_error_message_lists_valid_modes() -> None:
    """ValueError for unknown retrieval_mode must name the valid options."""
    store = _make_store()
    with pytest.raises(ValueError) as exc_info:
        await store.async_query("test", retrieval_mode="turbo_mode")
    msg = str(exc_info.value)
    assert "hybrid" in msg or "trust_recency" in msg or "vector" in msg, (
        f"Error message should list valid modes, got: {msg}"
    )
