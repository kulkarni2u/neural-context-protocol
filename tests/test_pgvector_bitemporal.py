"""Sprint 5d Part 1: CAP-C5 bi-temporal memory -- pgvector (sync) and
AsyncPgvectorStore parity.

Deep functional coverage (supersedence chains, as_of point-in-time
correctness) lives in tests/test_bitemporal.py against SQLiteStore and the
backend-agnostic ncp/stores/bitemporal.py pure functions -- those don't need
a live Postgres and are exhaustive. Here we verify pgvector-specific wiring
only: the write() INSERT/supersede() UPDATE SQL shape, and that
_apply_bitemporal_filter / _async_apply_bitemporal_filter correctly delegate
to ncp.stores.bitemporal (including triggering the one-shot successor
created_at lookup only when as_of is given and a row is superseded).

Uses the existing lightweight MagicMock connection pattern (see
tests/test_psycopg3_upgrade.py) for the sync store, and the AsyncMock
pool/connection/cursor pattern (see tests/test_async_pgvector_store.py) for
the async store -- no live Postgres required.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

pytest.importorskip("psycopg", reason="pgvector extra not installed")

from ncp.stores.pgvector import PgvectorStore  # noqa: E402
from ncp.types import ChunkEdge, SubconsciousChunk  # noqa: E402


def _make_mock_conn(*, fetchall_return: list | None = None, rowcount: int | None = None) -> MagicMock:
    conn = MagicMock()
    cur = conn.cursor.return_value
    cur.fetchall.return_value = fetchall_return if fetchall_return is not None else []
    cur.fetchone.return_value = None
    cur.description = []
    if rowcount is not None:
        cur.rowcount = rowcount
    return conn


# ---------------------------------------------------------------------------
# Sync PgvectorStore
# ---------------------------------------------------------------------------

def test_pgvector_write_insert_includes_bitemporal_columns() -> None:
    conn = _make_mock_conn()
    store = PgvectorStore("postgresql://localhost/test", connect_factory=lambda dsn: conn)
    store._soft_gc = MagicMock()  # type: ignore[method-assign]
    store._assert_src_immutable = MagicMock()  # type: ignore[method-assign]
    store._is_duplicate = MagicMock(return_value=False)  # type: ignore[method-assign]
    store._hard_gc = MagicMock()  # type: ignore[method-assign]

    chunk = SubconsciousChunk(
        chunk_id="sub_bitemporal", layer="episodic", content="a fact with a validity window",
        src="tool_result", valid_from=100.0, valid_to=200.0,
    )
    assert store.write(chunk) is True

    insert_calls = [
        c for c in conn.cursor.return_value.execute.call_args_list
        if len(c.args) > 0 and "INSERT INTO" in str(c.args[0]) and "chunks (" in str(c.args[0])
    ]
    assert insert_calls, "No chunks INSERT call found"
    insert_sql, insert_params = insert_calls[0].args
    assert "valid_from" in insert_sql
    assert "valid_to" in insert_sql
    assert "superseded_by" in insert_sql
    assert "src IS NOT DISTINCT FROM EXCLUDED.src" in insert_sql
    assert "written_by IS NOT DISTINCT FROM EXCLUDED.written_by" in insert_sql
    assert "pipeline_id IS NOT DISTINCT FROM EXCLUDED.pipeline_id" in insert_sql
    # Last 3 params are valid_from, valid_to, superseded_by (appended after embedding).
    assert insert_params[-3:] == (100.0, 200.0, None)


def test_pgvector_write_rejects_atomic_ownership_conflict() -> None:
    conn = _make_mock_conn(rowcount=0)
    store = PgvectorStore("postgresql://localhost/test", connect_factory=lambda dsn: conn)
    store._soft_gc = MagicMock()  # type: ignore[method-assign]
    store._assert_src_immutable = MagicMock()  # type: ignore[method-assign]
    store._is_duplicate = MagicMock(return_value=False)  # type: ignore[method-assign]

    with pytest.raises(ValueError, match="ownership changed concurrently"):
        store.write(
            SubconsciousChunk(
                chunk_id="atomic_conflict",
                pipeline_id="pipeline_attacker",
                layer="semantic",
                content="must not overwrite",
                src="tool_result",
                written_by="attacker",
            )
        )


def test_pgvector_write_rejects_cross_pipeline_scalar_edge() -> None:
    conn = _make_mock_conn(
        fetchall_return=[{"chunk_id": "victim", "pipeline_id": "pipeline_A"}]
    )
    store = PgvectorStore("postgresql://localhost/test", connect_factory=lambda dsn: conn)
    store._soft_gc = MagicMock()  # type: ignore[method-assign]
    store._assert_src_immutable = MagicMock()  # type: ignore[method-assign]
    store._is_duplicate = MagicMock(return_value=False)  # type: ignore[method-assign]

    with pytest.raises(ValueError, match="edge target pipeline mismatch"):
        store.write(
            SubconsciousChunk(
                chunk_id="attacker",
                pipeline_id="pipeline_B",
                layer="semantic",
                content="must not link across pipelines",
                src="tool_result",
                caused_by="victim",
            )
        )

    assert not any(
        "INSERT INTO" in str(call.args[0]) and "chunks (" in str(call.args[0])
        for call in conn.cursor.return_value.execute.call_args_list
    )


def test_pgvector_supersede_updates_superseded_by_and_valid_to() -> None:
    conn = _make_mock_conn(rowcount=1)
    conn.cursor.return_value.fetchone.side_effect = [
        {"pipeline_id": "pipe_same"},
        {"pipeline_id": "pipe_same"},
    ]
    store = PgvectorStore("postgresql://localhost/test", connect_factory=lambda dsn: conn)

    assert store.supersede("old_chunk", "new_chunk", valid_to=555.0) is True

    update_calls = [
        c for c in conn.cursor.return_value.execute.call_args_list
        if len(c.args) > 0 and "UPDATE" in str(c.args[0]) and "superseded_by" in str(c.args[0])
    ]
    assert len(update_calls) == 1
    sql, params = update_calls[0].args
    assert "SET superseded_by = %s, valid_to = %s WHERE chunk_id = %s" in sql
    assert params == ("new_chunk", 555.0, "old_chunk")


def test_pgvector_supersede_resolves_valid_to_to_now_when_omitted() -> None:
    conn = _make_mock_conn(rowcount=1)
    conn.cursor.return_value.fetchone.side_effect = [
        {"pipeline_id": "pipe_same"},
        {"pipeline_id": "pipe_same"},
    ]
    store = PgvectorStore("postgresql://localhost/test", connect_factory=lambda dsn: conn)

    before = __import__("time").time()
    store.supersede("old_chunk", "new_chunk")
    after = __import__("time").time()

    _, params = conn.cursor.return_value.execute.call_args_list[-1].args
    assert before <= params[1] <= after


def test_pgvector_supersede_returns_false_when_rowcount_zero() -> None:
    conn = _make_mock_conn(rowcount=0)
    store = PgvectorStore("postgresql://localhost/test", connect_factory=lambda dsn: conn)
    assert store.supersede("missing", "new_chunk") is False


def test_pgvector_apply_bitemporal_filter_default_view_excludes_superseded() -> None:
    conn = _make_mock_conn()
    store = PgvectorStore("postgresql://localhost/test", connect_factory=lambda dsn: conn)

    rows = [
        {"chunk_id": "a", "superseded_by": "b", "valid_to": None},
        {"chunk_id": "b", "superseded_by": None, "valid_to": None},
    ]
    filtered = store._apply_bitemporal_filter(conn, rows, as_of=None)
    assert [r["chunk_id"] for r in filtered] == ["b"]
    # Default view needs no successor lookup -- no extra SELECT beyond schema bootstrap.
    assert not any(
        "chunk_id, created_at" in str(c.args[0]) for c in conn.cursor.return_value.execute.call_args_list
    )


def test_pgvector_apply_bitemporal_filter_as_of_triggers_successor_lookup() -> None:
    successor_rows = [{"chunk_id": "b", "created_at": 500.0}]
    conn = _make_mock_conn(fetchall_return=successor_rows)
    store = PgvectorStore("postgresql://localhost/test", connect_factory=lambda dsn: conn)

    rows = [
        {"chunk_id": "a", "created_at": 100.0, "superseded_by": "b", "valid_from": None, "valid_to": None},
    ]
    # as_of=200 is before the successor's created_at (500) -> "a" is not yet
    # superseded as of that transaction time, so it stays visible.
    filtered = store._apply_bitemporal_filter(conn, rows, as_of=200.0)
    assert [r["chunk_id"] for r in filtered] == ["a"]

    lookup_calls = [
        c for c in conn.cursor.return_value.execute.call_args_list
        if len(c.args) > 0 and "chunk_id, created_at" in str(c.args[0])
    ]
    assert len(lookup_calls) == 1
    _, params = lookup_calls[0].args
    assert params[0] == ["b"]


def test_pgvector_apply_bitemporal_filter_as_of_excludes_when_superseded_by_then() -> None:
    successor_rows = [{"chunk_id": "b", "created_at": 500.0}]
    conn = _make_mock_conn(fetchall_return=successor_rows)
    store = PgvectorStore("postgresql://localhost/test", connect_factory=lambda dsn: conn)

    rows = [
        {"chunk_id": "a", "created_at": 100.0, "superseded_by": "b", "valid_from": None, "valid_to": None},
    ]
    # as_of=1000 is after the successor's created_at (500) -> "a" was already
    # superseded by then.
    filtered = store._apply_bitemporal_filter(conn, rows, as_of=1000.0)
    assert filtered == []


def test_pgvector_load_query_rows_applies_bitemporal_filter() -> None:
    """Integration: _load_query_rows fetches then filters via the same helper."""
    rows = [
        {"chunk_id": "a", "created_at": 100.0, "superseded_by": "b", "valid_to": None},
        {"chunk_id": "b", "created_at": 200.0, "superseded_by": None, "valid_to": None},
    ]
    conn = _make_mock_conn(fetchall_return=rows)
    store = PgvectorStore("postgresql://localhost/test", connect_factory=lambda dsn: conn)

    result = store._load_query_rows(conn, layer=None, pipeline_id="p1", scope=None, zone="working")
    assert [r["chunk_id"] for r in result] == ["b"]


# ---------------------------------------------------------------------------
# Async AsyncPgvectorStore
# ---------------------------------------------------------------------------

def _make_async_pool(fetchall_return: list | None = None) -> MagicMock:
    pool = MagicMock()
    cursor = MagicMock()
    cursor.fetchall = AsyncMock(return_value=fetchall_return if fetchall_return is not None else [])
    cursor.fetchone = AsyncMock(return_value=None)
    cursor.execute = AsyncMock()
    cursor.rowcount = 1
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


@pytest.mark.anyio
async def test_async_pgvector_write_insert_includes_bitemporal_columns() -> None:
    pytest.importorskip("psycopg_pool", reason="psycopg_pool extra not installed")
    from ncp.stores.pgvector_async import AsyncPgvectorStore

    mock_pool = _make_async_pool()
    with patch("psycopg_pool.AsyncConnectionPool", return_value=mock_pool):
        store = AsyncPgvectorStore("postgresql://localhost/test")

    store._async_soft_gc = AsyncMock()  # type: ignore[method-assign]
    store._async_assert_src_immutable = AsyncMock()  # type: ignore[method-assign]
    store._async_is_duplicate = AsyncMock(return_value=False)  # type: ignore[method-assign]
    store._async_hard_gc = AsyncMock()  # type: ignore[method-assign]

    chunk = SubconsciousChunk(
        chunk_id="async_bitemporal", layer="episodic", content="a fact with a validity window",
        src="tool_result", valid_from=100.0, valid_to=200.0,
    )
    await store.async_write(chunk)

    calls = [
        c for c in mock_pool._cur.execute.call_args_list
        if len(c.args) > 0 and "INSERT INTO" in str(c.args[0])
    ]
    assert calls, "No INSERT call found"
    insert_sql, insert_params = calls[0].args
    assert "valid_from" in insert_sql
    assert "src IS NOT DISTINCT FROM EXCLUDED.src" in insert_sql
    assert "written_by IS NOT DISTINCT FROM EXCLUDED.written_by" in insert_sql
    assert "pipeline_id IS NOT DISTINCT FROM EXCLUDED.pipeline_id" in insert_sql
    assert insert_params[-3:] == (100.0, 200.0, None)


@pytest.mark.anyio
async def test_async_pgvector_write_rejects_atomic_ownership_conflict() -> None:
    pytest.importorskip("psycopg_pool", reason="psycopg_pool extra not installed")
    from ncp.stores.pgvector_async import AsyncPgvectorStore

    mock_pool = _make_async_pool()
    mock_pool._cur.rowcount = 0
    with patch("psycopg_pool.AsyncConnectionPool", return_value=mock_pool):
        store = AsyncPgvectorStore("postgresql://localhost/test")

    store._async_soft_gc = AsyncMock()  # type: ignore[method-assign]
    store._async_assert_src_immutable = AsyncMock()  # type: ignore[method-assign]
    store._async_is_duplicate = AsyncMock(return_value=False)  # type: ignore[method-assign]

    with pytest.raises(ValueError, match="ownership changed concurrently"):
        await store.async_write(
            SubconsciousChunk(
                chunk_id="atomic_conflict",
                pipeline_id="pipeline_attacker",
                layer="semantic",
                content="must not overwrite",
                src="tool_result",
                written_by="attacker",
            )
        )


@pytest.mark.anyio
async def test_async_pgvector_write_rejects_cross_pipeline_scalar_edge() -> None:
    pytest.importorskip("psycopg_pool", reason="psycopg_pool extra not installed")
    from ncp.stores.pgvector_async import AsyncPgvectorStore

    mock_pool = _make_async_pool(
        fetchall_return=[{"chunk_id": "victim", "pipeline_id": "pipeline_A"}]
    )
    with patch("psycopg_pool.AsyncConnectionPool", return_value=mock_pool):
        store = AsyncPgvectorStore("postgresql://localhost/test")

    store._async_soft_gc = AsyncMock()  # type: ignore[method-assign]
    store._async_assert_src_immutable = AsyncMock()  # type: ignore[method-assign]
    store._async_is_duplicate = AsyncMock(return_value=False)  # type: ignore[method-assign]

    with pytest.raises(ValueError, match="edge target pipeline mismatch"):
        await store.async_write(
            SubconsciousChunk(
                chunk_id="attacker",
                pipeline_id="pipeline_B",
                layer="semantic",
                content="must not link across pipelines",
                src="tool_result",
                caused_by="victim",
            )
        )

    assert not any(
        "INSERT INTO" in str(call.args[0]) and "chunks (" in str(call.args[0])
        for call in mock_pool._cur.execute.call_args_list
    )


@pytest.mark.anyio
async def test_async_pgvector_pipeline_id_is_immutable_for_existing_chunk_id() -> None:
    pytest.importorskip("psycopg_pool", reason="psycopg_pool extra not installed")
    from ncp.stores.pgvector_async import AsyncPgvectorStore

    mock_pool = _make_async_pool()
    mock_pool._cur.fetchone.return_value = {
        "src": "tool_result",
        "written_by": "shared_agent_name",
        "pipeline_id": "pipeline_victim",
    }
    with patch("psycopg_pool.AsyncConnectionPool", return_value=mock_pool):
        store = AsyncPgvectorStore("postgresql://localhost/test")

    chunk = SubconsciousChunk(
        chunk_id="sub_pipeline_lock",
        pipeline_id="pipeline_attacker",
        layer="semantic",
        content="moved content",
        src="tool_result",
        written_by="shared_agent_name",
    )
    with pytest.raises(ValueError, match="pipeline_id is immutable"):
        await store._async_assert_src_immutable(mock_pool._conn, chunk)


@pytest.mark.anyio
async def test_async_pgvector_supersede_updates_correct_columns() -> None:
    pytest.importorskip("psycopg_pool", reason="psycopg_pool extra not installed")
    from ncp.stores.pgvector_async import AsyncPgvectorStore

    mock_pool = _make_async_pool()
    with patch("psycopg_pool.AsyncConnectionPool", return_value=mock_pool):
        store = AsyncPgvectorStore("postgresql://localhost/test")

    mock_pool._cur.fetchone.side_effect = [
        {"pipeline_id": "pipe_same"},
        {"pipeline_id": "pipe_same"},
    ]

    result = await store.async_supersede("old_chunk", "new_chunk", valid_to=555.0)
    assert result is True

    sql, params = mock_pool._cur.execute.call_args_list[-1].args
    assert "SET superseded_by = %s, valid_to = %s WHERE chunk_id = %s" in sql
    assert params == ("new_chunk", 555.0, "old_chunk")


@pytest.mark.anyio
async def test_async_pgvector_supersede_rejects_cross_pipeline_target() -> None:
    pytest.importorskip("psycopg_pool", reason="psycopg_pool extra not installed")
    from ncp.stores.pgvector_async import AsyncPgvectorStore

    mock_pool = _make_async_pool()
    mock_pool._cur.fetchone.side_effect = [
        {"pipeline_id": "pipeline_A"},
        {"pipeline_id": "pipeline_B"},
    ]
    with patch("psycopg_pool.AsyncConnectionPool", return_value=mock_pool):
        store = AsyncPgvectorStore("postgresql://localhost/test")

    assert await store.async_supersede("victim", "attacker") is False
    assert not any(
        "UPDATE" in str(call.args[0]) and "superseded_by" in str(call.args[0])
        for call in mock_pool._cur.execute.call_args_list
    )


@pytest.mark.anyio
async def test_async_pgvector_add_chunk_edges_rejects_cross_pipeline_dst() -> None:
    pytest.importorskip("psycopg_pool", reason="psycopg_pool extra not installed")
    from ncp.stores.pgvector_async import AsyncPgvectorStore

    mock_pool = _make_async_pool(
        fetchall_return=[
            {"chunk_id": "attacker", "pipeline_id": "pipeline_B"},
            {"chunk_id": "victim", "pipeline_id": "pipeline_A"},
        ]
    )
    with patch("psycopg_pool.AsyncConnectionPool", return_value=mock_pool):
        store = AsyncPgvectorStore("postgresql://localhost/test")

    written = await store.async_add_chunk_edges(
        [ChunkEdge(src_chunk_id="attacker", dst_chunk_id="victim", edge_type="contradicts")]
    )

    assert written == 0
    assert not any(
        "INSERT INTO" in str(call.args[0]) and "chunk_edges" in str(call.args[0])
        for call in mock_pool._cur.execute.call_args_list
    )


@pytest.mark.anyio
async def test_async_pgvector_supersede_does_not_use_thread_shim() -> None:
    pytest.importorskip("psycopg_pool", reason="psycopg_pool extra not installed")
    import anyio

    from ncp.stores.pgvector_async import AsyncPgvectorStore

    mock_pool = _make_async_pool()
    with patch("psycopg_pool.AsyncConnectionPool", return_value=mock_pool):
        store = AsyncPgvectorStore("postgresql://localhost/test")

    mock_pool._cur.fetchone.side_effect = [
        {"pipeline_id": "pipe_same"},
        {"pipeline_id": "pipe_same"},
    ]

    call_log: list[str] = []
    original = anyio.to_thread.run_sync

    async def spy(fn, *args, **kwargs):  # type: ignore[no-untyped-def]
        call_log.append(repr(fn))
        return await original(fn, *args, **kwargs)

    with patch("anyio.to_thread.run_sync", side_effect=spy):
        await store.async_supersede("old_chunk", "new_chunk")

    assert not call_log, f"async_supersede must be native async, got: {call_log}"


@pytest.mark.anyio
async def test_async_pgvector_apply_bitemporal_filter_default_view() -> None:
    pytest.importorskip("psycopg_pool", reason="psycopg_pool extra not installed")
    from ncp.stores.pgvector_async import AsyncPgvectorStore

    mock_pool = _make_async_pool()
    with patch("psycopg_pool.AsyncConnectionPool", return_value=mock_pool):
        store = AsyncPgvectorStore("postgresql://localhost/test")

    rows = [
        {"chunk_id": "a", "superseded_by": "b", "valid_to": None},
        {"chunk_id": "b", "superseded_by": None, "valid_to": None},
    ]
    filtered = await store._async_apply_bitemporal_filter(mock_pool._conn, rows, as_of=None)
    assert [r["chunk_id"] for r in filtered] == ["b"]


@pytest.mark.anyio
async def test_async_pgvector_apply_bitemporal_filter_as_of_triggers_successor_lookup() -> None:
    pytest.importorskip("psycopg_pool", reason="psycopg_pool extra not installed")
    from ncp.stores.pgvector_async import AsyncPgvectorStore

    mock_pool = _make_async_pool(fetchall_return=[{"chunk_id": "b", "created_at": 500.0}])
    with patch("psycopg_pool.AsyncConnectionPool", return_value=mock_pool):
        store = AsyncPgvectorStore("postgresql://localhost/test")

    rows = [
        {"chunk_id": "a", "created_at": 100.0, "superseded_by": "b", "valid_from": None, "valid_to": None},
    ]
    filtered = await store._async_apply_bitemporal_filter(mock_pool._conn, rows, as_of=1000.0)
    assert filtered == []  # superseded by "b", which was recorded (500) before as_of (1000)

    lookup_calls = [
        c for c in mock_pool._cur.execute.call_args_list
        if len(c.args) > 0 and "chunk_id, created_at" in str(c.args[0])
    ]
    assert len(lookup_calls) == 1
