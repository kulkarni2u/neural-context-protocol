"""Tests for CAP-C3 async memoization telemetry-accuracy fixes.

Covers:
  - Fix 2: async_record_memo's upsert must preserve hit_count/created_at/
    outcome/verified on re-record (SET clause must not touch them).
  - Fix 3: async_memo_stats must not mask a query failure as an all-zero
    result; it must log a warning and re-raise.

Follows the AsyncPgvectorStore mocking pattern used by
tests/test_async_consolidate.py — no live Postgres required.
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
    cursor.fetchone = AsyncMock(return_value=None)
    cursor.execute = AsyncMock()
    cursor.description = []
    cursor.rowcount = 1
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


# ---------------------------------------------------------------------------
# Fix 2: re-record upsert must preserve hit_count/created_at/outcome/verified
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_async_record_memo_upsert_preserves_counters_sql() -> None:
    """The ON CONFLICT SET clause must not overwrite created_at/hit_count/outcome/verified."""
    store = _make_store()

    await store.async_record_memo("sig1", "task", ["c1"], "summary")

    sql_calls = [str(c.args[0]) for c in store._test_cursor.execute.call_args_list]
    upsert_calls = [c for c in sql_calls if "memo_entries" in c and "ON CONFLICT" in c]
    assert upsert_calls, f"Expected an upsert INSERT ... ON CONFLICT call; got: {sql_calls}"
    upsert_sql = upsert_calls[0]

    set_clause = upsert_sql.split("DO UPDATE SET", 1)[1]
    assert "created_at" not in set_clause, (
        "re-record must not reset created_at; found it in SET clause"
    )
    assert "hit_count" not in set_clause, (
        "re-record must not reset hit_count; found it in SET clause"
    )
    assert "outcome" not in set_clause, "re-record must not reset outcome"
    assert "verified" not in set_clause, "re-record must not reset verified"
    # Content fields are still legitimately updated.
    assert "task" in set_clause
    assert "result_summary" in set_clause
    assert "output_tokens_est" in set_clause


# ---------------------------------------------------------------------------
# Fix 3: async_memo_stats must log a warning and re-raise on query failure,
# not silently fabricate an all-zero result.
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_async_memo_stats_reraises_on_query_failure() -> None:
    store = _make_store()
    store._test_cursor.execute = AsyncMock(side_effect=RuntimeError("relation does not exist"))

    with pytest.raises(RuntimeError, match="relation does not exist"):
        await store.async_memo_stats()


@pytest.mark.anyio
async def test_async_memo_stats_logs_warning_on_query_failure() -> None:
    import structlog

    store = _make_store()
    store._test_cursor.execute = AsyncMock(side_effect=RuntimeError("boom"))

    with structlog.testing.capture_logs() as captured:
        with pytest.raises(RuntimeError):
            await store.async_memo_stats()

    warnings = [e for e in captured if e.get("log_level") == "warning"]
    assert warnings, f"Expected a warning to be logged on failure; captured: {captured}"


@pytest.mark.anyio
async def test_async_memo_stats_returns_real_zeros_when_no_data() -> None:
    """Legitimate 'no memo activity yet' must still return zeros without raising."""
    store = _make_store()
    store._test_cursor.fetchone = AsyncMock(
        side_effect=[
            {"entry_count": 0, "hits": 0, "tokens_saved": 0},
            {"stat_value": 0},
        ]
    )

    stats = await store.async_memo_stats()

    assert stats == {
        "hits": 0,
        "misses": 0,
        "entry_count": 0,
        "estimated_tokens_saved": 0,
    }
