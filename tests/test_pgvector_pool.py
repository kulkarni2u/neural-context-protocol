"""Tests for PgvectorStore connection pooling (0.5.x Slice 1)."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

pytest.importorskip("psycopg2")

from ncp.stores.pgvector import PgvectorStore


def _make_mock_conn() -> MagicMock:
    conn = MagicMock()
    conn.cursor.return_value.__enter__ = lambda s: s
    conn.cursor.return_value.__exit__ = MagicMock(return_value=False)
    conn.cursor.return_value.fetchone.return_value = None
    conn.cursor.return_value.fetchall.return_value = []
    return conn


def test_pool_created_when_no_factory_provided() -> None:
    """When connect_factory is None, a ThreadedConnectionPool is wired up."""
    mock_conn = _make_mock_conn()
    mock_pool = MagicMock()
    mock_pool.getconn.return_value = mock_conn

    with patch("psycopg2.pool.ThreadedConnectionPool", return_value=mock_pool) as pool_cls:
        store = PgvectorStore(
            "postgresql://localhost/ncp_test",
            connect_factory=None,
            min_pool_connections=1,
            max_pool_connections=5,
        )
        pool_cls.assert_called_once_with(1, 5, "postgresql://localhost/ncp_test")
        assert store._pool is mock_pool


def test_no_pool_when_factory_provided() -> None:
    """When connect_factory is given, no pool is created."""
    mock_conn = _make_mock_conn()

    def factory(dsn: str) -> MagicMock:
        return mock_conn

    store = PgvectorStore("postgresql://localhost/ncp_test", connect_factory=factory)
    assert store._pool is None


def test_connection_returned_to_pool_on_success() -> None:
    """After a successful _connect() use, putconn is called instead of close."""
    mock_conn = _make_mock_conn()
    mock_pool = MagicMock()
    mock_pool.getconn.return_value = mock_conn

    with patch("psycopg2.pool.ThreadedConnectionPool", return_value=mock_pool):
        store = PgvectorStore("postgresql://localhost/ncp_test", connect_factory=None)

    mock_pool.reset_mock()
    mock_conn.reset_mock()

    with store._connect() as conn:
        assert conn is mock_conn

    mock_pool.putconn.assert_called_once_with(mock_conn)
    mock_conn.close.assert_not_called()


def test_connection_returned_to_pool_after_exception() -> None:
    """On error inside _connect(), putconn still returns connection to pool."""
    from ncp.stores.base import NCPStoreUnavailableError

    mock_conn = _make_mock_conn()
    mock_pool = MagicMock()
    mock_pool.getconn.return_value = mock_conn

    with patch("psycopg2.pool.ThreadedConnectionPool", return_value=mock_pool):
        store = PgvectorStore("postgresql://localhost/ncp_test", connect_factory=None)

    mock_pool.reset_mock()
    mock_conn.reset_mock()

    with pytest.raises(NCPStoreUnavailableError):
        with store._connect():
            raise RuntimeError("injected failure")

    mock_pool.putconn.assert_called_once_with(mock_conn)


def test_close_drains_pool() -> None:
    """close() calls closeall() on the underlying pool."""
    mock_conn = _make_mock_conn()
    mock_pool = MagicMock()
    mock_pool.getconn.return_value = mock_conn

    with patch("psycopg2.pool.ThreadedConnectionPool", return_value=mock_pool):
        store = PgvectorStore("postgresql://localhost/ncp_test", connect_factory=None)

    store.close()
    mock_pool.closeall.assert_called_once()
    assert store._pool is None


def test_close_is_idempotent() -> None:
    """Calling close() twice does not raise."""
    mock_conn = _make_mock_conn()
    mock_pool = MagicMock()
    mock_pool.getconn.return_value = mock_conn

    with patch("psycopg2.pool.ThreadedConnectionPool", return_value=mock_pool):
        store = PgvectorStore("postgresql://localhost/ncp_test", connect_factory=None)

    store.close()
    store.close()  # second call should not raise


def test_pool_uses_min_max_pool_defaults() -> None:
    """Default min/max pool sizes are 2 and 10 respectively."""
    mock_conn = _make_mock_conn()
    mock_pool = MagicMock()
    mock_pool.getconn.return_value = mock_conn

    with patch("psycopg2.pool.ThreadedConnectionPool", return_value=mock_pool) as pool_cls:
        PgvectorStore("postgresql://localhost/ncp_test")
        pool_cls.assert_called_once_with(2, 10, "postgresql://localhost/ncp_test")
