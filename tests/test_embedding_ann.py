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


def test_chunk_accepts_any_nonzero_dimension_embedding() -> None:
    # Any non-empty embedding is accepted (pgvector validates dimensions at query time)
    chunk = SubconsciousChunk(layer="semantic", content="hello", src="synthesis", embedding=[0.1] * 10)
    assert len(chunk.embedding) == 10  # type: ignore[arg-type]


def test_chunk_rejects_zero_dimension_embedding() -> None:
    with pytest.raises(Exception, match="empty"):
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


# ---------------------------------------------------------------------------
# ivfflat.probes wired into _query_vector
# ---------------------------------------------------------------------------


def _make_probes_store(probes: int = 10):
    from unittest.mock import MagicMock
    from ncp.stores.pgvector import PgvectorStore

    mock_conn = MagicMock()
    mock_cursor = MagicMock()
    mock_cursor.fetchall.return_value = []
    mock_cursor.description = None
    mock_conn.cursor.return_value = mock_cursor

    def factory(_dsn: str) -> MagicMock:
        return mock_conn

    store = PgvectorStore(
        "postgresql://localhost/ncp_test",
        connect_factory=factory,
        ivfflat_probes=probes,
    )
    mock_cursor.execute.reset_mock()  # discard _init_db execute calls
    return store, mock_cursor


def test_pgvector_query_vector_sets_probes_default() -> None:
    store, mock_cursor = _make_probes_store(probes=10)
    store.query("test", retrieval_mode="vector", embedding=[0.1] * 1536)

    first_call = mock_cursor.execute.call_args_list[0]
    sql, params = first_call[0][0], first_call[0][1]
    assert "SET LOCAL ivfflat.probes" in sql
    assert params == (10,)


def test_pgvector_query_vector_custom_probes() -> None:
    store, mock_cursor = _make_probes_store(probes=5)
    store.query("test", retrieval_mode="vector", embedding=[0.1] * 1536)

    first_call = mock_cursor.execute.call_args_list[0]
    sql, params = first_call[0][0], first_call[0][1]
    assert "SET LOCAL ivfflat.probes" in sql
    assert params == (5,)


# ---------------------------------------------------------------------------
# PgvectorStore auto-embed via embedding_adapter
# ---------------------------------------------------------------------------


def _make_store_with_adapter(adapter=None):
    from unittest.mock import MagicMock
    from ncp.stores.pgvector import PgvectorStore

    mock_conn = MagicMock()
    mock_cursor = MagicMock()
    mock_cursor.fetchall.return_value = []
    mock_cursor.fetchone.return_value = None
    mock_cursor.description = None
    mock_conn.cursor.return_value = mock_cursor

    def factory(_dsn: str) -> MagicMock:
        return mock_conn

    store = PgvectorStore(
        "postgresql://localhost/ncp_test",
        connect_factory=factory,
        embedding_adapter=adapter,
    )
    mock_cursor.execute.reset_mock()
    return store, mock_conn, mock_cursor


def _mock_embedding_adapter(vector=None):
    from unittest.mock import MagicMock
    adapter = MagicMock()
    adapter.embed.return_value = vector or [0.3] * 1536
    return adapter


def test_pgvector_write_auto_embeds_when_adapter_set() -> None:
    from ncp.types import SubconsciousChunk
    adapter = _mock_embedding_adapter()
    store, _, _ = _make_store_with_adapter(adapter=adapter)
    chunk = SubconsciousChunk(layer="semantic", content="hello", src="synthesis")
    assert chunk.embedding is None
    store.write(chunk)
    adapter.embed.assert_called_once_with("hello")


def test_pgvector_write_skips_adapter_when_embedding_present() -> None:
    from ncp.types import SubconsciousChunk
    adapter = _mock_embedding_adapter()
    store, _, _ = _make_store_with_adapter(adapter=adapter)
    chunk = SubconsciousChunk(
        layer="semantic", content="hello", src="synthesis", embedding=[0.1] * 1536
    )
    store.write(chunk)
    adapter.embed.assert_not_called()


def test_pgvector_query_vector_auto_embeds_when_adapter_set() -> None:
    adapter = _mock_embedding_adapter()
    store, _, mock_cursor = _make_store_with_adapter(adapter=adapter)
    store.query("find something", retrieval_mode="vector")
    adapter.embed.assert_called_once_with("find something")


def test_pgvector_query_vector_still_raises_without_adapter_and_no_embedding() -> None:
    store, _, _ = _make_store_with_adapter(adapter=None)
    with pytest.raises(ValueError, match="embedding"):
        store.query("find something", retrieval_mode="vector")
