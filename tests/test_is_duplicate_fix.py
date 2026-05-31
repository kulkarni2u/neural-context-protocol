"""Tests for 0.11.x Slice 2: _is_duplicate self-match fix across all three stores.

An idempotent upsert (write same chunk_id + same content again) must NOT be
rejected as a duplicate — only *different* chunk_ids with similar content should be.
All tests RED before fix, GREEN after.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from ncp.types import SubconsciousChunk


CONTENT = "authentication bearer token JWT session management secure"


def _chunk(chunk_id: str, content: str = CONTENT, **kwargs) -> SubconsciousChunk:
    defaults = dict(layer="semantic", src="tool_result", pipeline_id="pipe_fix")
    defaults.update(kwargs)
    return SubconsciousChunk(chunk_id=chunk_id, content=content, **defaults)


# ---------------------------------------------------------------------------
# Slice 2a: SQLiteStore — idempotent upsert not rejected
# ---------------------------------------------------------------------------

def test_sqlite_upsert_same_chunk_id_not_rejected(tmp_path: Path) -> None:
    """Re-writing the same chunk_id with same content must not be rejected."""
    from ncp.stores.sqlite import SQLiteStore

    store = SQLiteStore(tmp_path / "store.db")
    c = _chunk("dup_fix_01")

    r1 = store.write(c)
    assert r1 is True, "First write must succeed"

    r2 = store.write(c)
    assert r2 is True, (
        "Second write of same chunk_id must not be rejected as duplicate (idempotent upsert)"
    )


def test_sqlite_different_chunk_id_same_content_still_rejected(tmp_path: Path) -> None:
    """A different chunk_id with the same content must still be rejected as duplicate."""
    from ncp.stores.sqlite import SQLiteStore

    store = SQLiteStore(tmp_path / "store.db")
    r1 = store.write(_chunk("dup_fix_a"))
    assert r1 is True

    r2 = store.write(_chunk("dup_fix_b"))  # different chunk_id, same content
    assert r2 is False, (
        "Different chunk_id with same content must still be rejected as duplicate"
    )


# ---------------------------------------------------------------------------
# Slice 2b: PgvectorStore — idempotent upsert not rejected
# ---------------------------------------------------------------------------

def _pgvector_store_with_rows(rows_by_call: list[list[dict]]) -> MagicMock:
    """Return a PgvectorStore mock where fetchall returns rows_by_call in sequence."""
    from ncp.stores.pgvector import PgvectorStore

    call_count = [0]

    def _fetch_all(cursor):
        idx = min(call_count[0], len(rows_by_call) - 1)
        call_count[0] += 1
        return rows_by_call[idx]

    mock_conn = MagicMock()
    mock_cursor = MagicMock()
    mock_cursor.__enter__ = MagicMock(return_value=mock_cursor)
    mock_cursor.__exit__ = MagicMock(return_value=False)
    mock_cursor.fetchall = MagicMock(side_effect=lambda: _fetch_all(mock_cursor))
    mock_cursor.fetchone = MagicMock(return_value=None)
    mock_cursor.execute = MagicMock()
    mock_conn.cursor = MagicMock(return_value=mock_cursor)
    mock_conn.__enter__ = MagicMock(return_value=mock_conn)
    mock_conn.__exit__ = MagicMock(return_value=False)

    store = PgvectorStore("postgresql://localhost/test", connect_factory=lambda _: mock_conn)
    return store


def test_pgvector_upsert_same_chunk_id_not_rejected() -> None:
    """PgvectorStore: re-writing same chunk_id must not be rejected as self-duplicate."""
    from ncp.stores.pgvector import PgvectorStore

    mock_conn = MagicMock()
    mock_cursor = MagicMock()
    mock_cursor.__enter__ = MagicMock(return_value=mock_cursor)
    mock_cursor.__exit__ = MagicMock(return_value=False)
    mock_cursor.execute = MagicMock()

    # _is_duplicate query returns one existing row — same content, same chunk_id
    # After fix: AND chunk_id != %s means this row is excluded → not duplicate → True
    existing_row = {"content": CONTENT}
    mock_cursor.fetchall = MagicMock(return_value=[existing_row])
    mock_cursor.fetchone = MagicMock(return_value=None)
    mock_conn.cursor = MagicMock(return_value=mock_cursor)
    mock_conn.__enter__ = MagicMock(return_value=mock_conn)
    mock_conn.__exit__ = MagicMock(return_value=False)

    chunk = _chunk("upsert_fix_01")
    store = PgvectorStore("postgresql://localhost/test", connect_factory=lambda _: mock_conn)

    # Capture the SQL passed to _is_duplicate execute call to verify chunk_id is in params
    execute_calls: list[tuple] = []

    def capture(sql, params=None):
        execute_calls.append((sql, params or ()))

    mock_cursor.execute.side_effect = capture

    store.write(chunk)

    # Find the _is_duplicate SELECT call
    dup_calls = [(sql, params) for sql, params in execute_calls
                 if "SELECT content" in str(sql)]
    assert dup_calls, "No _is_duplicate SELECT found in execute calls"
    dup_sql, dup_params = dup_calls[0]
    assert chunk.chunk_id in dup_params, (
        f"chunk_id must be in _is_duplicate WHERE params for self-exclusion. "
        f"params={dup_params}"
    )


def test_pgvector_different_chunk_id_same_content_rejected() -> None:
    """PgvectorStore: different chunk_id with same content must still be rejected."""
    from ncp.stores.pgvector import PgvectorStore

    mock_conn = MagicMock()
    mock_cursor = MagicMock()
    mock_cursor.__enter__ = MagicMock(return_value=mock_cursor)
    mock_cursor.__exit__ = MagicMock(return_value=False)
    mock_cursor.execute = MagicMock()

    # _is_duplicate query returns row with different chunk_id but same content
    # After fix: AND chunk_id != 'new_chunk_id' — the existing row (other_chunk_id)
    # is NOT excluded → still detected as duplicate → write returns False
    existing_row = {"content": CONTENT}
    mock_cursor.fetchall = MagicMock(return_value=[existing_row])
    mock_cursor.fetchone = MagicMock(return_value=None)
    mock_conn.cursor = MagicMock(return_value=mock_cursor)
    mock_conn.__enter__ = MagicMock(return_value=mock_conn)
    mock_conn.__exit__ = MagicMock(return_value=False)

    # Write NEW chunk (different chunk_id) — should be rejected as duplicate
    new_chunk = _chunk("brand_new_id")
    store = PgvectorStore("postgresql://localhost/test", connect_factory=lambda _: mock_conn)
    result = store.write(new_chunk)
    assert result is False, (
        "Different chunk_id with similar content must still be rejected as duplicate"
    )


# ---------------------------------------------------------------------------
# Slice 2c: AsyncPgvectorStore — idempotent upsert not rejected
# ---------------------------------------------------------------------------

@pytest.mark.anyio
async def test_async_upsert_same_chunk_id_not_rejected() -> None:
    """AsyncPgvectorStore: re-writing same chunk_id must not be rejected as self-duplicate."""
    pytest.importorskip("psycopg_pool")
    from ncp.stores.pgvector_async import AsyncPgvectorStore

    pool = MagicMock()
    cursor = MagicMock()
    cursor.execute = AsyncMock()
    cursor.description = [("content",)]

    # _async_is_duplicate returns no rows when chunk_id is excluded (fixed behavior)
    # We simulate: the existing row has same content + same chunk_id → excluded by fix
    cursor.fetchall = AsyncMock(return_value=[])  # empty after chunk_id exclusion
    cursor.fetchone = AsyncMock(return_value=None)
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

    with patch("psycopg_pool.AsyncConnectionPool", return_value=pool):
        store = AsyncPgvectorStore("postgresql://localhost/test")

    chunk = _chunk("async_upsert_01")

    # Capture execute calls to verify chunk_id appears in _async_is_duplicate params
    execute_calls: list[tuple] = []
    async def capture(sql, params=None):
        execute_calls.append((str(sql), params or ()))

    cursor.execute = AsyncMock(side_effect=capture)

    await store.async_write(chunk)

    # Find the _async_is_duplicate SELECT call
    dup_calls = [(sql, params) for sql, params in execute_calls
                 if "SELECT content" in sql]
    assert dup_calls, "No _async_is_duplicate SELECT found in execute calls"
    dup_sql, dup_params = dup_calls[0]
    assert chunk.chunk_id in dup_params, (
        f"chunk_id must be in _async_is_duplicate WHERE params. params={dup_params}"
    )
