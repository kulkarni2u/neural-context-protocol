"""Tests for S4.1: CAP-T4 reputation-weighted retrieval in AsyncPgvectorStore.

The sync stores (SQLiteStore, PgvectorStore) blend author reputation into
base_trust at query time when ``[retrieval].reputation_weight > 0.0``.
AsyncPgvectorStore.async_query must apply the same blending so the same
config yields the same rankings sync vs async.
"""

from __future__ import annotations

import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

pytest.importorskip("psycopg", reason="psycopg extra not installed")
pytest.importorskip("psycopg_pool", reason="psycopg_pool extra not installed")

from ncp.config import NCPConfig


def _make_store(**kwargs):
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
    pool.connection = MagicMock()
    pool.connection.return_value.__aenter__ = AsyncMock(return_value=conn)
    pool.connection.return_value.__aexit__ = AsyncMock(return_value=False)

    from ncp.stores.pgvector_async import AsyncPgvectorStore

    with patch("psycopg_pool.AsyncConnectionPool", return_value=pool):
        store = AsyncPgvectorStore("postgresql://localhost/test", **kwargs)
    store._test_cursor = cursor
    store._test_conn = conn
    store._test_pool = pool
    return store


def _make_chunk_row(
    chunk_id: str,
    written_by: str,
    *,
    content: str = "shared identical content",
    base_trust: float = 0.7,
    created_at: float | None = None,
) -> dict:
    return {
        "chunk_id": chunk_id,
        "layer": "semantic",
        "content": content,
        "src": "agent_inferred",
        "written_by": written_by,
        "generation": 0,
        "base_trust": base_trust,
        "pipeline_id": "pipe1",
        "zone": "working",
        "scope": "pipeline",
        "chunk_type": "prose",
        "schema_version": 1,
        "supersedes": None,
        "created_at": created_at if created_at is not None else time.time() - 60,
        "retrieval_count": 0,
        "caused_by": None,
        "conscious_hash": None,
        "evidence_id": None,
        "result_confidence": None,
        "result_attempts": None,
        "valid_while": None,
        "expiry": None,
        "owner": None,
        "written_at_drift": 0.0,
        "embedding": None,
        "dissent_count": 0,
        "verified": False,
    }


def _config_with_weight(weight: float) -> NCPConfig:
    return NCPConfig(
        values={"retrieval": {"reputation_weight": weight}},
        project_root=Path("."),
    )


@pytest.mark.anyio
async def test_async_query_blends_reputation_when_weight_positive() -> None:
    """With reputation_weight=0.5, identical chunks by different-reputation
    authors must receive different blended trust (and thus different scores)."""
    created_at = time.time() - 60
    rows = [
        _make_chunk_row("chunk_alice", "alice", created_at=created_at),
        _make_chunk_row("chunk_bob", "bob", created_at=created_at),
    ]
    store = _make_store()
    store.config = _config_with_weight(0.5)
    store._test_cursor.fetchall = AsyncMock(return_value=rows)
    store._aload_reputation = AsyncMock(  # type: ignore[method-assign]
        return_value={
            "alice": (95.0, 5.0),  # confidence 0.95
            "bob": (1.0, 99.0),    # confidence 0.01
        }
    )

    results = await store.async_query("shared identical content", k=4)

    store._aload_reputation.assert_awaited_once()
    assert len(results) == 2
    by_author = {c.written_by: c for c in results}
    # High-reputation author must outrank the low-reputation one.
    assert results[0].written_by == "alice"
    # Both chunks are identical except for author, so the relevance gap is
    # exactly w_trust * (blended_alice - blended_bob).
    blended_alice = 0.5 * 0.7 + 0.5 * (95.0 / 100.0)
    blended_bob = 0.5 * 0.7 + 0.5 * (1.0 / 100.0)
    expected_gap = 0.2 * (blended_alice - blended_bob)
    actual_gap = by_author["alice"].relevance - by_author["bob"].relevance
    assert actual_gap == pytest.approx(expected_gap, rel=1e-4)


@pytest.mark.anyio
async def test_async_query_unknown_author_falls_back_to_base_trust() -> None:
    """Authors without a reputation row keep their raw base_trust."""
    created_at = time.time() - 60
    rows = [
        _make_chunk_row("chunk_known", "alice", created_at=created_at),
        _make_chunk_row("chunk_unknown", "nobody", created_at=created_at),
    ]
    store = _make_store()
    store.config = _config_with_weight(0.5)
    store._test_cursor.fetchall = AsyncMock(return_value=rows)
    store._aload_reputation = AsyncMock(return_value={"alice": (95.0, 5.0)})  # type: ignore[method-assign]

    results = await store.async_query("shared identical content", k=4)

    assert len(results) == 2
    by_author = {c.written_by: c for c in results}
    # Unknown author's trust contribution is the raw base_trust (0.7); the
    # known high-reputation author blends up to 0.825, so the score gap is
    # w_trust * (0.825 - 0.7).
    blended_alice = 0.5 * 0.7 + 0.5 * 0.95
    expected_gap = 0.2 * (blended_alice - 0.7)
    actual_gap = by_author["alice"].relevance - by_author["nobody"].relevance
    assert actual_gap == pytest.approx(expected_gap, rel=1e-4)


@pytest.mark.anyio
async def test_async_query_weight_zero_never_queries_reputation() -> None:
    """With the default reputation_weight=0.0 the reputation table must not
    be queried and identical chunks score identically."""
    created_at = time.time() - 60
    rows = [
        _make_chunk_row("chunk_alice", "alice", created_at=created_at),
        _make_chunk_row("chunk_bob", "bob", created_at=created_at),
    ]
    store = _make_store()
    store.config = _config_with_weight(0.0)
    store._test_cursor.fetchall = AsyncMock(return_value=rows)
    store._aload_reputation = AsyncMock(  # type: ignore[method-assign]
        return_value={"alice": (95.0, 5.0), "bob": (1.0, 99.0)}
    )

    results = await store.async_query("shared identical content", k=4)

    store._aload_reputation.assert_not_awaited()
    assert len(results) == 2
    assert results[0].relevance == pytest.approx(results[1].relevance, rel=1e-6)


@pytest.mark.anyio
async def test_async_query_trust_recency_mode_uses_blended_trust() -> None:
    """Blending applies to trust_recency mode too, mirroring the sync stores."""
    created_at = time.time() - 60
    rows = [
        _make_chunk_row("chunk_alice", "alice", created_at=created_at),
        _make_chunk_row("chunk_bob", "bob", created_at=created_at),
    ]
    store = _make_store()
    store.config = _config_with_weight(0.5)
    store._test_cursor.fetchall = AsyncMock(return_value=rows)
    store._aload_reputation = AsyncMock(  # type: ignore[method-assign]
        return_value={"alice": (95.0, 5.0), "bob": (1.0, 99.0)}
    )

    results = await store.async_query(
        "shared identical content", k=4, retrieval_mode="trust_recency"
    )

    store._aload_reputation.assert_awaited_once()
    assert len(results) == 2
    assert results[0].written_by == "alice"
    assert results[0].relevance > results[1].relevance
