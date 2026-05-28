"""Tests for optional embedding storage and ANN retrieval (0.5.x Slice 3)."""

from __future__ import annotations

import pytest

from ncp.stores.sqlite import SQLiteStore
from ncp.types import SubconsciousChunk


_EMBEDDING_DIM = 1536


def _vec(seed: float = 0.1) -> list[float]:
    return [seed] * _EMBEDDING_DIM


# ---------------------------------------------------------------------------
# SubconsciousChunk embedding field
# ---------------------------------------------------------------------------


def test_chunk_accepts_none_embedding() -> None:
    chunk = SubconsciousChunk(layer="semantic", content="hello", src="synthesis")
    assert chunk.embedding is None


def test_chunk_accepts_valid_embedding() -> None:
    chunk = SubconsciousChunk(
        layer="semantic", content="hello", src="synthesis", embedding=_vec()
    )
    assert len(chunk.embedding) == _EMBEDDING_DIM  # type: ignore[arg-type]


def test_chunk_rejects_wrong_dimension_embedding() -> None:
    with pytest.raises(Exception, match="1536"):
        SubconsciousChunk(layer="semantic", content="hello", src="synthesis", embedding=[0.1] * 10)


def test_chunk_rejects_zero_dimension_embedding() -> None:
    with pytest.raises(Exception, match="1536"):
        SubconsciousChunk(layer="semantic", content="hello", src="synthesis", embedding=[])


# ---------------------------------------------------------------------------
# SQLite: vector mode raises ValueError
# ---------------------------------------------------------------------------


def test_sqlite_vector_mode_raises(tmp_path: pytest.TempdirFactory) -> None:
    store = SQLiteStore(tmp_path / "store.db")
    with pytest.raises(ValueError, match="pgvector"):
        store.query("test", retrieval_mode="vector")


def test_sqlite_vector_mode_raises_with_embedding(tmp_path: pytest.TempdirFactory) -> None:
    store = SQLiteStore(tmp_path / "store.db")
    with pytest.raises(ValueError, match="pgvector"):
        store.query("test", retrieval_mode="vector", embedding=_vec())


def test_sqlite_unknown_mode_still_raises(tmp_path: pytest.TempdirFactory) -> None:
    store = SQLiteStore(tmp_path / "store.db")
    with pytest.raises(ValueError, match="Unknown retrieval_mode"):
        store.query("test", retrieval_mode="bogus")


# ---------------------------------------------------------------------------
# Migration 003 file presence and format
# ---------------------------------------------------------------------------


def test_migration_003_exists() -> None:
    from pathlib import Path
    migration = Path(__file__).parent.parent / "ncp" / "migrations" / "003_add_embedding_column.sql"
    assert migration.exists(), "Migration 003 file is missing"
    content = migration.read_text()
    assert "-- UP" in content
    assert "-- DOWN" in content
    assert "embedding" in content
    assert "vector(1536)" in content


# ---------------------------------------------------------------------------
# PgvectorStore._query_vector validation (unit tests without real DB)
# ---------------------------------------------------------------------------


def test_pgvector_vector_mode_requires_embedding() -> None:
    """ValueError is raised when retrieval_mode='vector' but no embedding given."""
    from unittest.mock import MagicMock

    mock_conn = MagicMock()
    mock_conn.cursor.return_value.__enter__ = lambda s: s
    mock_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)
    mock_conn.cursor.return_value.fetchall.return_value = []

    def factory(_dsn: str) -> MagicMock:
        return mock_conn

    from ncp.stores.pgvector import PgvectorStore

    store = PgvectorStore("postgresql://localhost/ncp_test", connect_factory=factory)
    with pytest.raises(ValueError, match="embedding"):
        store.query("test", retrieval_mode="vector")


def test_pgvector_vector_mode_requires_1536_dimensions() -> None:
    from unittest.mock import MagicMock

    def factory(_dsn: str) -> MagicMock:
        conn = MagicMock()
        conn.cursor.return_value.__enter__ = lambda s: s
        conn.cursor.return_value.__exit__ = MagicMock(return_value=False)
        return conn

    from ncp.stores.pgvector import PgvectorStore

    store = PgvectorStore("postgresql://localhost/ncp_test", connect_factory=factory)
    with pytest.raises(ValueError, match="1536"):
        store.query("test", retrieval_mode="vector", embedding=[0.1] * 100)


def test_pgvector_unknown_mode_raises() -> None:
    from unittest.mock import MagicMock

    def factory(_dsn: str) -> MagicMock:
        return MagicMock()

    from ncp.stores.pgvector import PgvectorStore

    store = PgvectorStore("postgresql://localhost/ncp_test", connect_factory=factory)
    with pytest.raises(ValueError, match="Unknown retrieval_mode"):
        store.query("test", retrieval_mode="bogus")


# ---------------------------------------------------------------------------
# Migration 004 file presence and format
# ---------------------------------------------------------------------------


def test_migration_004_exists() -> None:
    from pathlib import Path
    migration = Path(__file__).parent.parent / "ncp" / "migrations" / "004_add_ivfflat_index.sql"
    assert migration.exists(), "Migration 004 file is missing"
    content = migration.read_text()
    assert "-- UP" in content
    assert "-- DOWN" in content
    assert "ivfflat" in content
    assert "vector_cosine_ops" in content
    assert "lists" in content
