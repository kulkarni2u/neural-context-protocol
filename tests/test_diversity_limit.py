"""Tests for 0.10.x: configurable diversity_limit + vector-mode diversity loop.

All tests must be RED before implementation. After implementation, all pass.
"""

from __future__ import annotations

import inspect
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from ncp.types import SubconsciousChunk


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_TOPICS = [
    "authentication bearer token JWT verification",
    "rate limiting 429 retry exponential backoff",
    "caching strategy redis TTL eviction",
    "database schema migration rollback plan",
    "logging structured JSON format fields",
    "circuit breaker pattern failure threshold",
    "service mesh sidecar proxy configuration",
    "distributed tracing span correlation ID",
]


def _write_chunks(store, *, authors: list[str], pipeline_id: str = "pipe_div") -> None:
    for i, author in enumerate(authors):
        store.write(
            SubconsciousChunk(
                chunk_id=f"div_chunk_{i}_{author}",
                layer="semantic",
                content=_TOPICS[i % len(_TOPICS)],
                src="tool_result",
                written_by=author,
                pipeline_id=pipeline_id,
            )
        )


# ---------------------------------------------------------------------------
# Slice 1a: BaseStore.query() signature has diversity_limit
# ---------------------------------------------------------------------------

def test_basestore_query_has_diversity_limit_param() -> None:
    """BaseStore.query() must declare diversity_limit: int = 2."""
    from ncp.stores.base import BaseStore

    sig = inspect.signature(BaseStore.query)
    assert "diversity_limit" in sig.parameters, (
        "BaseStore.query() missing diversity_limit parameter"
    )
    param = sig.parameters["diversity_limit"]
    assert param.default == 2, f"Expected default=2, got {param.default}"


# ---------------------------------------------------------------------------
# Slice 1b: SQLiteStore respects diversity_limit
# ---------------------------------------------------------------------------

def test_sqlite_query_accepts_diversity_limit(tmp_path: Path) -> None:
    """SQLiteStore.query() must accept diversity_limit kwarg without raising."""
    from ncp.stores.sqlite import SQLiteStore

    store = SQLiteStore(tmp_path / "store.db")
    _write_chunks(store, authors=["agent_a", "agent_b", "agent_a"])
    # Should not raise
    results = store.query("authentication rate limiting", diversity_limit=2, pipeline_id="pipe_div")
    assert isinstance(results, list)


def test_sqlite_query_diversity_limit_1(tmp_path: Path) -> None:
    """SQLiteStore.query(diversity_limit=1) must return at most 1 chunk per author."""
    from ncp.stores.sqlite import SQLiteStore

    store = SQLiteStore(tmp_path / "store.db")
    # 4 chunks: agent_a x3, agent_b x1
    _write_chunks(store, authors=["agent_a", "agent_a", "agent_a", "agent_b"])
    results = store.query(
        "authentication rate limiting caching database",
        k=10,
        diversity_limit=1,
        pipeline_id="pipe_div",
    )
    author_counts: dict[str, int] = {}
    for chunk in results:
        author_counts[chunk.written_by] = author_counts.get(chunk.written_by, 0) + 1
    assert all(count <= 1 for count in author_counts.values()), (
        f"diversity_limit=1 violated: {author_counts}"
    )


def test_sqlite_query_diversity_limit_4_allows_more(tmp_path: Path) -> None:
    """SQLiteStore.query(diversity_limit=4) allows up to 4 per author."""
    from ncp.stores.sqlite import SQLiteStore

    store = SQLiteStore(tmp_path / "store.db")
    # 6 chunks all from agent_a — with limit=4 we should get up to 4
    authors = ["agent_a"] * 6 + ["agent_b"] * 2
    _write_chunks(store, authors=authors)
    results = store.query(
        "authentication rate limiting caching database logging circuit breaker",
        k=10,
        diversity_limit=4,
        pipeline_id="pipe_div",
    )
    agent_a_count = sum(1 for c in results if c.written_by == "agent_a")
    assert agent_a_count > 2, (
        f"diversity_limit=4 should allow more than 2 per author, got {agent_a_count}"
    )


def test_sqlite_query_default_diversity_limit_still_2(tmp_path: Path) -> None:
    """SQLiteStore default diversity_limit=2 must match existing behavior."""
    from ncp.stores.sqlite import SQLiteStore

    store = SQLiteStore(tmp_path / "store.db")
    _write_chunks(store, authors=["agent_a"] * 5 + ["agent_b"] * 3)
    results = store.query(
        "authentication rate limiting caching database logging circuit breaker",
        k=10,
        pipeline_id="pipe_div",
    )
    author_counts: dict[str, int] = {}
    for chunk in results:
        author_counts[chunk.written_by] = author_counts.get(chunk.written_by, 0) + 1
    assert all(count <= 2 for count in author_counts.values()), (
        f"Default diversity_limit=2 violated: {author_counts}"
    )


# ---------------------------------------------------------------------------
# Slice 1c: PgvectorStore.query() accepts diversity_limit
# ---------------------------------------------------------------------------

def test_pgvector_query_has_diversity_limit_param() -> None:
    """PgvectorStore.query() must accept diversity_limit kwarg."""
    from ncp.stores.pgvector import PgvectorStore

    sig = inspect.signature(PgvectorStore.query)
    assert "diversity_limit" in sig.parameters, (
        "PgvectorStore.query() missing diversity_limit parameter"
    )
    assert sig.parameters["diversity_limit"].default == 2


def test_pgvector_query_diversity_limit_passed_through() -> None:
    """PgvectorStore.query() hybrid mode must use the provided diversity_limit."""
    from ncp.stores.pgvector import PgvectorStore

    mock_conn = MagicMock()
    mock_cursor = MagicMock()
    mock_cursor.__enter__ = MagicMock(return_value=mock_cursor)
    mock_cursor.__exit__ = MagicMock(return_value=False)

    # 4 rows all from same author
    fake_rows = [
        {
            "chunk_id": f"pg_div_{i}", "layer": "semantic",
            "content": _TOPICS[i], "src": "tool_result",
            "written_by": "agent_a", "pipeline_id": "pipe1",
            "scope": "pipeline", "zone": "working",
            "base_trust": 0.8, "generation": 0, "created_at": 1700000000.0 + i,
            "caused_by": None, "conscious_hash": None, "evidence_id": None,
            "result_confidence": None, "result_attempts": None,
            "conditions": "[]", "valid_while": None, "expiry": None,
            "owner": None, "chunk_type": "prose", "schema_version": 1,
            "supersedes": None, "source_refs": "[]", "embedding": None,
            "version": 1, "meta": "{}", "retrieval_count": 0, "last_retrieved_at": None,
        }
        for i in range(4)
    ]

    mock_cursor.fetchall = MagicMock(return_value=fake_rows)
    mock_cursor.fetchone = MagicMock(return_value=None)
    mock_conn.cursor = MagicMock(return_value=mock_cursor)
    mock_conn.__enter__ = MagicMock(return_value=mock_conn)
    mock_conn.__exit__ = MagicMock(return_value=False)

    def fake_connect(_dsn: str) -> object:
        return mock_conn

    store = PgvectorStore("postgresql://localhost/test", connect_factory=fake_connect)
    results = store.query(
        "authentication rate limiting caching database",
        k=10,
        diversity_limit=1,
        pipeline_id="pipe1",
    )
    agent_a_count = sum(1 for c in results if c.written_by == "agent_a")
    assert agent_a_count <= 1, (
        f"diversity_limit=1 on pgvector hybrid should allow at most 1 per author, got {agent_a_count}"
    )


# ---------------------------------------------------------------------------
# Slice 1d: AsyncPgvectorStore.async_query() accepts diversity_limit
# ---------------------------------------------------------------------------

def test_async_query_has_diversity_limit_param() -> None:
    """AsyncPgvectorStore.async_query() must accept diversity_limit kwarg."""
    pytest.importorskip("psycopg_pool")
    from ncp.stores.pgvector_async import AsyncPgvectorStore

    sig = inspect.signature(AsyncPgvectorStore.async_query)
    assert "diversity_limit" in sig.parameters, (
        "AsyncPgvectorStore.async_query() missing diversity_limit parameter"
    )
    assert sig.parameters["diversity_limit"].default == 2


# ---------------------------------------------------------------------------
# Slice 2: vector-mode diversity loop in PgvectorStore
# ---------------------------------------------------------------------------

def test_pgvector_vector_sql_limit_always_k_times_4() -> None:
    """_query_vector SQL LIMIT must be k*4 always (not only with reranker)."""
    from ncp.stores.pgvector import PgvectorStore

    mock_conn = MagicMock()
    mock_cursor = MagicMock()
    mock_cursor.__enter__ = MagicMock(return_value=mock_cursor)
    mock_cursor.__exit__ = MagicMock(return_value=False)
    mock_cursor.fetchall = MagicMock(return_value=[])
    mock_conn.cursor = MagicMock(return_value=mock_cursor)
    mock_conn.__enter__ = MagicMock(return_value=mock_conn)
    mock_conn.__exit__ = MagicMock(return_value=False)

    executed_params: list[tuple] = []

    def capture_execute(sql, params=None):
        if params:
            executed_params.append(params)

    mock_cursor.execute = MagicMock(side_effect=capture_execute)

    store = PgvectorStore("postgresql://localhost/test", connect_factory=lambda _: mock_conn)
    store.query(
        "test query",
        k=4,
        retrieval_mode="vector",
        embedding=[0.1] * 1536,
    )

    # Find the LIMIT value in execute calls — last param of the SELECT call
    limits = [p[-1] for p in executed_params if len(p) >= 3]
    assert any(lim == 16 for lim in limits), (
        f"Expected SQL LIMIT=16 (k*4=4*4) in vector mode, got limits={limits}"
    )


def test_pgvector_vector_mode_applies_diversity_loop() -> None:
    """vector mode must apply author diversity loop before returning results."""
    from ncp.stores.pgvector import PgvectorStore

    mock_conn = MagicMock()
    mock_cursor = MagicMock()
    mock_cursor.__enter__ = MagicMock(return_value=mock_cursor)
    mock_cursor.__exit__ = MagicMock(return_value=False)

    # 4 rows all from same author — with diversity_limit=2, should return at most 2
    fake_rows = [
        {
            "chunk_id": f"vec_div_{i}", "layer": "semantic",
            "content": _TOPICS[i], "src": "tool_result",
            "written_by": "agent_a", "pipeline_id": "pipe1",
            "scope": "pipeline", "zone": "working",
            "base_trust": 0.8, "generation": 0, "created_at": 1700000000.0,
            "caused_by": None, "conscious_hash": None, "evidence_id": None,
            "result_confidence": None, "result_attempts": None,
            "conditions": "[]", "valid_while": None, "expiry": None,
            "owner": None, "chunk_type": "prose", "schema_version": 1,
            "supersedes": None, "source_refs": "[]",
            "embedding": None, "version": 1, "meta": "{}",
            "retrieval_count": 0, "last_retrieved_at": None,
            "vec_distance": 0.1,  # close distance → high score
        }
        for i in range(4)
    ]

    mock_cursor.fetchall = MagicMock(return_value=fake_rows)
    mock_cursor.fetchone = MagicMock(return_value=None)
    mock_cursor.execute = MagicMock()
    mock_conn.cursor = MagicMock(return_value=mock_cursor)
    mock_conn.__enter__ = MagicMock(return_value=mock_conn)
    mock_conn.__exit__ = MagicMock(return_value=False)

    store = PgvectorStore("postgresql://localhost/test", connect_factory=lambda _: mock_conn)
    results = store.query(
        "test query",
        k=10,
        retrieval_mode="vector",
        embedding=[0.1] * 1536,
        diversity_limit=2,
    )
    agent_a_count = sum(1 for c in results if c.written_by == "agent_a")
    assert agent_a_count <= 2, (
        f"vector mode diversity_limit=2 should cap agent_a at 2, got {agent_a_count}"
    )


def test_pgvector_vector_mode_diversity_limit_1() -> None:
    """vector mode diversity_limit=1 must return at most 1 chunk per author."""
    from ncp.stores.pgvector import PgvectorStore

    mock_conn = MagicMock()
    mock_cursor = MagicMock()
    mock_cursor.__enter__ = MagicMock(return_value=mock_cursor)
    mock_cursor.__exit__ = MagicMock(return_value=False)

    fake_rows = [
        {
            "chunk_id": f"vec_d1_{i}", "layer": "semantic",
            "content": _TOPICS[i % len(_TOPICS)], "src": "tool_result",
            "written_by": "agent_a" if i < 3 else "agent_b",
            "pipeline_id": "pipe1", "scope": "pipeline", "zone": "working",
            "base_trust": 0.8, "generation": 0, "created_at": 1700000000.0,
            "caused_by": None, "conscious_hash": None, "evidence_id": None,
            "result_confidence": None, "result_attempts": None,
            "conditions": "[]", "valid_while": None, "expiry": None,
            "owner": None, "chunk_type": "prose", "schema_version": 1,
            "supersedes": None, "source_refs": "[]",
            "embedding": None, "version": 1, "meta": "{}",
            "retrieval_count": 0, "last_retrieved_at": None,
            "vec_distance": 0.1,
        }
        for i in range(5)
    ]

    mock_cursor.fetchall = MagicMock(return_value=fake_rows)
    mock_cursor.fetchone = MagicMock(return_value=None)
    mock_cursor.execute = MagicMock()
    mock_conn.cursor = MagicMock(return_value=mock_cursor)
    mock_conn.__enter__ = MagicMock(return_value=mock_conn)
    mock_conn.__exit__ = MagicMock(return_value=False)

    store = PgvectorStore("postgresql://localhost/test", connect_factory=lambda _: mock_conn)
    results = store.query(
        "test query",
        k=10,
        retrieval_mode="vector",
        embedding=[0.1] * 1536,
        diversity_limit=1,
    )
    author_counts: dict[str, int] = {}
    for c in results:
        author_counts[c.written_by] = author_counts.get(c.written_by, 0) + 1
    assert all(count <= 1 for count in author_counts.values()), (
        f"vector diversity_limit=1 violated: {author_counts}"
    )


def test_pgvector_vector_mode_default_diversity_is_2() -> None:
    """vector mode default diversity_limit=2 must cap each author at 2."""
    from ncp.stores.pgvector import PgvectorStore

    mock_conn = MagicMock()
    mock_cursor = MagicMock()
    mock_cursor.__enter__ = MagicMock(return_value=mock_cursor)
    mock_cursor.__exit__ = MagicMock(return_value=False)

    fake_rows = [
        {
            "chunk_id": f"vec_def_{i}", "layer": "semantic",
            "content": _TOPICS[i % len(_TOPICS)], "src": "tool_result",
            "written_by": "agent_a",  # all from one author
            "pipeline_id": "pipe1", "scope": "pipeline", "zone": "working",
            "base_trust": 0.8, "generation": 0, "created_at": 1700000000.0,
            "caused_by": None, "conscious_hash": None, "evidence_id": None,
            "result_confidence": None, "result_attempts": None,
            "conditions": "[]", "valid_while": None, "expiry": None,
            "owner": None, "chunk_type": "prose", "schema_version": 1,
            "supersedes": None, "source_refs": "[]",
            "embedding": None, "version": 1, "meta": "{}",
            "retrieval_count": 0, "last_retrieved_at": None,
            "vec_distance": 0.1,
        }
        for i in range(6)
    ]

    mock_cursor.fetchall = MagicMock(return_value=fake_rows)
    mock_cursor.fetchone = MagicMock(return_value=None)
    mock_cursor.execute = MagicMock()
    mock_conn.cursor = MagicMock(return_value=mock_cursor)
    mock_conn.__enter__ = MagicMock(return_value=mock_conn)
    mock_conn.__exit__ = MagicMock(return_value=False)

    store = PgvectorStore("postgresql://localhost/test", connect_factory=lambda _: mock_conn)
    results = store.query(
        "test query",
        k=10,
        retrieval_mode="vector",
        embedding=[0.1] * 1536,
        # no diversity_limit — should use default 2
    )
    agent_a_count = sum(1 for c in results if c.written_by == "agent_a")
    assert agent_a_count <= 2, (
        f"vector default diversity_limit=2 violated: agent_a has {agent_a_count} results"
    )


# ---------------------------------------------------------------------------
# Slice 1e: trust_recency mode respects diversity_limit (both stores)
# ---------------------------------------------------------------------------

def test_sqlite_trust_recency_diversity_limit_1(tmp_path: Path) -> None:
    """SQLiteStore trust_recency mode must respect diversity_limit=1."""
    from ncp.stores.sqlite import SQLiteStore

    store = SQLiteStore(tmp_path / "store.db")
    _write_chunks(store, authors=["agent_a"] * 4 + ["agent_b"] * 2)
    results = store.query(
        "anything",
        k=10,
        retrieval_mode="trust_recency",
        diversity_limit=1,
        pipeline_id="pipe_div",
    )
    author_counts: dict[str, int] = {}
    for chunk in results:
        author_counts[chunk.written_by] = author_counts.get(chunk.written_by, 0) + 1
    assert all(count <= 1 for count in author_counts.values()), (
        f"trust_recency diversity_limit=1 violated: {author_counts}"
    )


def test_pgvector_trust_recency_diversity_limit_1() -> None:
    """PgvectorStore trust_recency mode must respect diversity_limit=1."""
    from ncp.stores.pgvector import PgvectorStore

    mock_conn = MagicMock()
    mock_cursor = MagicMock()
    mock_cursor.__enter__ = MagicMock(return_value=mock_cursor)
    mock_cursor.__exit__ = MagicMock(return_value=False)

    fake_rows = [
        {
            "chunk_id": f"tr_div_{i}", "layer": "semantic",
            "content": _TOPICS[i % len(_TOPICS)], "src": "tool_result",
            "written_by": "agent_a",
            "pipeline_id": "pipe1", "scope": "pipeline", "zone": "working",
            "base_trust": 0.8, "generation": 0, "created_at": 1700000000.0 + i,
            "caused_by": None, "conscious_hash": None, "evidence_id": None,
            "result_confidence": None, "result_attempts": None,
            "conditions": "[]", "valid_while": None, "expiry": None,
            "owner": None, "chunk_type": "prose", "schema_version": 1,
            "supersedes": None, "source_refs": "[]", "embedding": None,
            "version": 1, "meta": "{}", "retrieval_count": 0, "last_retrieved_at": None,
        }
        for i in range(4)
    ]
    mock_cursor.fetchall = MagicMock(return_value=fake_rows)
    mock_cursor.fetchone = MagicMock(return_value=None)
    mock_conn.cursor = MagicMock(return_value=mock_cursor)
    mock_conn.__enter__ = MagicMock(return_value=mock_conn)
    mock_conn.__exit__ = MagicMock(return_value=False)

    store = PgvectorStore("postgresql://localhost/test", connect_factory=lambda _: mock_conn)
    results = store.query(
        "authentication",
        k=10,
        retrieval_mode="trust_recency",
        diversity_limit=1,
        pipeline_id="pipe1",
    )
    agent_a_count = sum(1 for c in results if c.written_by == "agent_a")
    assert agent_a_count <= 1, (
        f"trust_recency diversity_limit=1 violated: agent_a has {agent_a_count} results"
    )


# ---------------------------------------------------------------------------
# Slice 1f: AsyncPgvectorStore.async_query behavioral diversity test
# ---------------------------------------------------------------------------

@pytest.mark.anyio
async def test_async_query_diversity_limit_applied() -> None:
    """AsyncPgvectorStore.async_query must apply the diversity loop."""
    pytest.importorskip("psycopg_pool")
    from unittest.mock import AsyncMock
    from ncp.stores.pgvector_async import AsyncPgvectorStore

    mock_pool = MagicMock()
    cursor = MagicMock()
    cursor.execute = AsyncMock()
    cursor.description = [("chunk_id",), ("layer",), ("content",), ("src",),
                           ("written_by",), ("pipeline_id",), ("scope",), ("zone",),
                           ("base_trust",), ("generation",), ("created_at",),
                           ("caused_by",), ("conscious_hash",), ("evidence_id",),
                           ("result_confidence",), ("result_attempts",),
                           ("conditions",), ("valid_while",), ("expiry",),
                           ("owner",), ("chunk_type",), ("schema_version",),
                           ("supersedes",), ("source_refs",), ("embedding",),
                           ("version",), ("meta",), ("retrieval_count",),
                           ("last_retrieved_at",)]

    # 4 rows all from agent_a — with diversity_limit=1 should get at most 1
    fake_rows = [
        (f"async_div_{i}", "semantic", _TOPICS[i % len(_TOPICS)], "tool_result",
         "agent_a", "pipe1", "pipeline", "working", 0.8, 0, 1700000000.0 + i,
         None, None, None, None, None, "[]", None, None, None, "prose", 1,
         None, "[]", None, 1, "{}", 0, None)
        for i in range(4)
    ]
    cursor.fetchall = AsyncMock(return_value=fake_rows)
    cursor.fetchone = AsyncMock(return_value=None)
    cursor.__aenter__ = AsyncMock(return_value=cursor)
    cursor.__aexit__ = AsyncMock(return_value=False)

    conn = MagicMock()
    conn.cursor = MagicMock(return_value=cursor)
    conn.commit = AsyncMock()
    conn.rollback = AsyncMock()
    conn.__aenter__ = AsyncMock(return_value=conn)
    conn.__aexit__ = AsyncMock(return_value=False)

    mock_pool.open = AsyncMock()
    mock_pool.connection = MagicMock()
    mock_pool.connection.return_value.__aenter__ = AsyncMock(return_value=conn)
    mock_pool.connection.return_value.__aexit__ = AsyncMock(return_value=False)

    with __import__("unittest.mock", fromlist=["patch"]).patch(
        "psycopg_pool.AsyncConnectionPool", return_value=mock_pool
    ):
        store = AsyncPgvectorStore("postgresql://localhost/test")

    results = await store.async_query(
        "authentication rate limiting caching database",
        k=10,
        diversity_limit=1,
        pipeline_id="pipe1",
    )
    agent_a_count = sum(1 for c in results if c.written_by == "agent_a")
    assert agent_a_count <= 1, (
        f"async_query diversity_limit=1 violated: agent_a has {agent_a_count} results"
    )
