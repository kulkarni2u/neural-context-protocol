"""Tests for 0.7.x Slice 2: psycopg2 → psycopg3 driver upgrade.

Design: patch both drivers at sys.modules so no actual DB connections are made.
Tests fail against the current psycopg2-based code and pass once the migration
replaces psycopg2 with psycopg + psycopg_pool.
"""

from __future__ import annotations

import sys
from unittest.mock import MagicMock, patch

import pytest

pytest.importorskip("psycopg", reason="pgvector extra not installed")

from ncp.stores.pgvector import PgvectorStore, _default_pgvector_connect  # noqa: E402


def _make_mock_conn() -> MagicMock:
    conn = MagicMock()
    conn.cursor.return_value.fetchone.return_value = None
    conn.cursor.return_value.fetchall.return_value = []
    conn.cursor.return_value.description = []
    return conn


def _psycopg2_pool_patch(mock_conn: MagicMock) -> MagicMock:
    """Return a mock that can replace psycopg2.pool.ThreadedConnectionPool."""
    mock_pool = MagicMock()
    mock_pool.getconn.return_value = mock_conn
    return mock_pool


# ---------------------------------------------------------------------------
# _default_pgvector_connect must call psycopg.connect (v3)
# ---------------------------------------------------------------------------

def test_default_connect_uses_psycopg3_connect() -> None:
    """`_default_pgvector_connect` must call psycopg.connect, not psycopg2.connect."""
    mock_psycopg = MagicMock()
    mock_conn = _make_mock_conn()
    mock_psycopg.connect.return_value = mock_conn

    # Inject psycopg v3 module; block psycopg2 for this invocation.
    with patch.dict(sys.modules, {"psycopg": mock_psycopg, "psycopg2": None}):
        result = _default_pgvector_connect("postgresql://localhost/test")

    mock_psycopg.connect.assert_called_once_with("postgresql://localhost/test")
    assert result is mock_conn


# ---------------------------------------------------------------------------
# Pool must use psycopg_pool.ConnectionPool(conninfo=..., min_size=..., max_size=...)
# ---------------------------------------------------------------------------

def test_pool_created_with_psycopg3_connection_pool() -> None:
    """Pool init must call psycopg_pool.ConnectionPool, not psycopg2 ThreadedConnectionPool."""
    mock_conn = _make_mock_conn()
    mock_psycopg2_pool = _psycopg2_pool_patch(mock_conn)
    mock_psycopg3_pool = MagicMock()
    mock_psycopg3_pool.getconn.return_value = mock_conn
    mock_psycopg_pool_mod = MagicMock()
    mock_psycopg_pool_mod.ConnectionPool.return_value = mock_psycopg3_pool

    with patch("psycopg2.pool.ThreadedConnectionPool", return_value=mock_psycopg2_pool):
        with patch.dict(sys.modules, {"psycopg_pool": mock_psycopg_pool_mod}):
            PgvectorStore(
                "postgresql://localhost/ncp_test",
                connect_factory=None,
                min_pool_connections=2,
                max_pool_connections=8,
            )

    mock_psycopg_pool_mod.ConnectionPool.assert_called_once_with(
        conninfo="postgresql://localhost/ncp_test",
        min_size=2,
        max_size=8,
        open=True,
    )


def test_pool_defaults_min2_max10_with_psycopg3() -> None:
    """Default pool sizes 2/10 must be forwarded as min_size/max_size."""
    mock_conn = _make_mock_conn()
    mock_psycopg2_pool = _psycopg2_pool_patch(mock_conn)
    mock_psycopg3_pool = MagicMock()
    mock_psycopg3_pool.getconn.return_value = mock_conn
    mock_psycopg_pool_mod = MagicMock()
    mock_psycopg_pool_mod.ConnectionPool.return_value = mock_psycopg3_pool

    with patch("psycopg2.pool.ThreadedConnectionPool", return_value=mock_psycopg2_pool):
        with patch.dict(sys.modules, {"psycopg_pool": mock_psycopg_pool_mod}):
            PgvectorStore("postgresql://localhost/ncp_test", connect_factory=None)

    _, kwargs = mock_psycopg_pool_mod.ConnectionPool.call_args
    assert kwargs.get("min_size") == 2
    assert kwargs.get("max_size") == 10


# ---------------------------------------------------------------------------
# close() must call pool.close(), not pool.closeall()
# ---------------------------------------------------------------------------

def test_close_calls_pool_close_not_closeall() -> None:
    """store.close() must call pool.close() (psycopg3 API), not pool.closeall()."""
    mock_conn = _make_mock_conn()
    mock_psycopg2_pool = _psycopg2_pool_patch(mock_conn)
    mock_psycopg3_pool = MagicMock()
    mock_psycopg3_pool.getconn.return_value = mock_conn
    mock_psycopg_pool_mod = MagicMock()
    mock_psycopg_pool_mod.ConnectionPool.return_value = mock_psycopg3_pool

    with patch("psycopg2.pool.ThreadedConnectionPool", return_value=mock_psycopg2_pool):
        with patch.dict(sys.modules, {"psycopg_pool": mock_psycopg_pool_mod}):
            store = PgvectorStore("postgresql://localhost/ncp_test", connect_factory=None)

    store.close()

    mock_psycopg3_pool.close.assert_called_once()
    mock_psycopg3_pool.closeall.assert_not_called()
