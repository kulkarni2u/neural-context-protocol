"""Tests for 0.7.x Slice 1: caller-controlled k — no hidden max-4 cap."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from ncp.stores.sqlite import SQLiteStore
from ncp.types import SubconsciousChunk


_CHUNK_CONTENTS = [
    "bearer token validated for oauth2 session handshake",
    "jwt refresh secret rotated in credential store",
    "memory retrieval pipeline emits subconscious chunks",
    "episodic layer records turn history for agent context",
    "auth middleware blocks unauthenticated requests at boundary",
    "semantic cache stores synthesized reasoning traces",
    "procedural rules govern trust calibration during assembly",
    "pgvector index accelerates cosine similarity lookups",
    "redis coordination bus routes whisper payloads",
    "hybrid scoring blends bm25 and recency for ranked results",
]


def _chunk(idx: int, *, author: str, content_idx: int) -> SubconsciousChunk:
    # Content must be unique and dissimilar to pass the store's fuzzy dedup gate.
    return SubconsciousChunk(
        chunk_id=f"sub_{author}_{idx}",
        layer="episodic",
        content=_CHUNK_CONTENTS[content_idx],
        src="tool_result",
        written_by=author,
    )


def _store_with_ten_chunks(tmp_path: Path) -> SQLiteStore:
    """Write 10 chunks across 5 authors (2 each) to a fresh SQLite store."""
    store = SQLiteStore(tmp_path / "store.db")
    content_idx = 0
    for author_idx in range(5):
        for chunk_idx in range(2):
            assert store.write(_chunk(chunk_idx, author=f"agent_{author_idx}", content_idx=content_idx)) is True
            content_idx += 1
    return store


# ---------------------------------------------------------------------------
# SQLite hybrid mode
# ---------------------------------------------------------------------------

def test_sqlite_hybrid_k8_returns_more_than_four(tmp_path: Path) -> None:
    """k=8 should return up to 8 results, not be capped at 4."""
    store = _store_with_ten_chunks(tmp_path)
    # Query with terms that appear across many of the 10 chunks.
    results = store.query("token bearer auth memory layer trust", k=8, min_score=0.0, retrieval_mode="hybrid")
    assert len(results) > 4, (
        f"expected > 4 results with k=8 across 10 chunks/5 authors, got {len(results)}"
    )


def test_sqlite_hybrid_k_equals_result_count_upper_bound(tmp_path: Path) -> None:
    """result count must never exceed k, regardless of how many chunks exist."""
    store = _store_with_ten_chunks(tmp_path)
    for k in (1, 3, 6, 10):
        results = store.query("token bearer auth memory layer trust", k=k, min_score=0.0, retrieval_mode="hybrid")
        assert len(results) <= k, f"got {len(results)} results for k={k}"


# ---------------------------------------------------------------------------
# SQLite trust_recency mode
# ---------------------------------------------------------------------------

def test_sqlite_trust_recency_k8_returns_more_than_four(tmp_path: Path) -> None:
    """trust_recency path respects k > 4."""
    store = _store_with_ten_chunks(tmp_path)
    results = store.query("anything", k=8, min_score=0.0, retrieval_mode="trust_recency")
    assert len(results) > 4, (
        f"expected > 4 results with k=8 across 10 chunks/5 authors, got {len(results)}"
    )


def test_sqlite_trust_recency_k_upper_bound(tmp_path: Path) -> None:
    """Result count must not exceed k for trust_recency."""
    store = _store_with_ten_chunks(tmp_path)
    for k in (1, 3, 6, 10):
        results = store.query("anything", k=k, min_score=0.0, retrieval_mode="trust_recency")
        assert len(results) <= k, f"got {len(results)} results for k={k}"


# ---------------------------------------------------------------------------
# PgvectorStore _query_vector — SQL LIMIT must equal max(1, k)
# ---------------------------------------------------------------------------

pytest.importorskip("psycopg2", reason="pgvector extra not installed")

from ncp.stores.pgvector import PgvectorStore  # noqa: E402


def _mock_conn_returning(rows: list[dict]) -> MagicMock:
    """Build a mock psycopg2 connection whose cursor returns `rows` on fetchall."""
    cursor = MagicMock()
    cursor.fetchall.return_value = rows
    cursor.description = [(col, None, None, None, None, None, None) for col in rows[0]] if rows else []

    conn = MagicMock()
    conn.cursor.return_value = cursor
    return conn


def _pgvector_store_with_factory(factory: object) -> PgvectorStore:
    return PgvectorStore("postgresql://localhost/test", connect_factory=factory)  # type: ignore[arg-type]


def test_pgvector_vector_sql_limit_respects_k() -> None:
    """_query_vector must pass max(1, k) as LIMIT, not min(k, 4)."""
    execute_calls: list[tuple] = []

    def factory(dsn: str) -> MagicMock:
        conn = MagicMock()
        cursor = MagicMock()
        cursor.fetchall.return_value = []
        cursor.description = []

        def capture_execute(sql: str, params: tuple = ()) -> None:
            execute_calls.append((sql, params))

        cursor.execute.side_effect = capture_execute
        conn.cursor.return_value = cursor
        return conn

    store = _pgvector_store_with_factory(factory)
    embedding = [0.1] * 1536
    store.query("token auth", k=8, retrieval_mode="vector", embedding=embedding, min_score=0.0)

    # Find the SELECT ... LIMIT %s call and assert LIMIT param == 8
    limit_params = [
        params[-1]
        for sql, params in execute_calls
        if "LIMIT" in sql.upper() and params
    ]
    assert limit_params, "no LIMIT-bearing execute call found"
    assert any(p == 8 for p in limit_params), (
        f"expected LIMIT=8 somewhere in execute calls, got limit_params={limit_params}"
    )


def test_pgvector_vector_rerank_slice_respects_k() -> None:
    """After reranking, results must be sliced to k, not min(k, 4)."""
    from ncp.stores.rerank import Reranker

    # Build 10 fake row dicts that _row_to_chunk can process (all fields from schema).
    fake_rows = [
        {
            "chunk_id": f"sub_{i}",
            "layer": "episodic",
            "content": f"token auth bearer chunk {i}",
            "src": "tool_result",
            "written_by": f"agent_{i}",
            "caused_by": None,
            "conscious_hash": None,
            "evidence_id": None,
            "generation": 1,
            "base_trust": 0.8,
            "result_confidence": None,
            "result_attempts": None,
            "conditions": "[]",
            "valid_while": None,
            "expiry": None,
            "owner": None,
            "chunk_type": "prose",
            "pipeline_id": None,
            "scope": "pipeline",
            "zone": "working",
            "schema_version": 1,
            "supersedes": None,
            "source_refs": "[]",
            "created_at": 0.0,
            "last_retrieved_at": None,
            "retrieval_count": 0,
            "embedding": None,
            "vec_distance": 0.1,
        }
        for i in range(10)
    ]

    call_count = [0]

    def factory(dsn: str) -> MagicMock:
        conn = MagicMock()
        cursor = MagicMock()
        cursor.description = [
            (col, None, None, None, None, None, None) for col in fake_rows[0]
        ]

        def fetchall() -> list:
            if call_count[0] == 0:
                call_count[0] += 1
                return [list(r.values()) for r in fake_rows]
            return []

        cursor.fetchall.side_effect = fetchall
        conn.cursor.return_value = cursor
        return conn

    mock_reranker = MagicMock(spec=Reranker)
    mock_reranker.enabled = True

    store = _pgvector_store_with_factory(factory)
    store.reranker = mock_reranker

    # reranker.rerank returns same list; after fix, slice should be [:k] = [:8]
    def rerank(text: str, chunks: list) -> list:
        return chunks  # identity rerank

    mock_reranker.rerank.side_effect = rerank

    embedding = [0.1] * 1536
    results = store.query("token auth bearer", k=8, retrieval_mode="vector", embedding=embedding, min_score=0.0)

    assert len(results) > 4, (
        f"expected > 4 results with k=8 after identity reranker, got {len(results)}"
    )
