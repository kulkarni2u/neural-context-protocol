"""Exercise real PostgreSQL store methods with only database I/O replaced."""
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from ncp.mcp.server import make_handlers
from ncp.stores.pgvector import PgvectorStore
from ncp.types import OutcomeRecord


def sync_store():
    conn = MagicMock()
    conn.cursor.return_value.fetchall.return_value = []
    conn.cursor.return_value.fetchone.return_value = None
    conn.cursor.return_value.description = []
    return PgvectorStore('postgresql://localhost/test', connect_factory=lambda dsn: conn), conn


def async_store():
    # AsyncPgvectorStore imports psycopg_pool.AsyncConnectionPool at
    # construction, and the patch below has to import the module to patch it.
    # The base `test` CI job installs only .[dev,providers], so skip there and
    # let the pgvector-redis job (which installs the pgvector extra) run these.
    # The sync tests in this module deliberately stay unguarded: PgvectorStore
    # takes a connect_factory and needs no driver at all.
    pytest.importorskip("psycopg_pool", reason="pgvector extra not installed")

    from ncp.stores.pgvector_async import AsyncPgvectorStore
    pool = MagicMock()
    conn = MagicMock()
    conn.commit = AsyncMock()
    conn.rollback = AsyncMock()
    cursor = MagicMock()
    cursor.execute = AsyncMock()
    cursor.fetchall = AsyncMock(return_value=[])
    cursor.description = []
    cursor.__aenter__ = AsyncMock(return_value=cursor)
    cursor.__aexit__ = AsyncMock(return_value=False)
    conn.cursor.return_value = cursor
    pool.open = AsyncMock()
    pool.connection.return_value.__aenter__ = AsyncMock(return_value=conn)
    pool.connection.return_value.__aexit__ = AsyncMock(return_value=False)
    with patch('psycopg_pool.AsyncConnectionPool', return_value=pool):
        store = AsyncPgvectorStore('postgresql://localhost/test')
    return store, cursor


def outcome_row():
    return dict(outcome_id='out_failed', turn_id=None, chunk_ids=json.dumps(['e']),
                success=0, weight=0.5, note='failed', created_at=123.0, consumed=1)


def test_pgvector_reads_exact_failed_outcome():
    store, conn = sync_store()
    row = outcome_row()
    conn.cursor.return_value.fetchall.return_value = [row]
    result = store.get_outcome('out_failed')
    assert isinstance(result, OutcomeRecord)
    assert result.success is False
    assert result.consumed is True
    assert result.chunk_ids == ['e']
    sql, params = conn.cursor.return_value.execute.call_args.args
    assert 'WHERE outcome_id = %s' in sql
    assert params == ('out_failed',)


@pytest.mark.anyio
async def test_async_pgvector_reads_exact_failed_outcome():
    store, cursor = async_store()
    cursor.fetchall.return_value = [outcome_row()]
    result = await store.async_get_outcome('out_failed')
    assert result.success is False
    assert result.consumed is True
    assert result.chunk_ids == ['e']
    sql, params = cursor.execute.call_args.args
    assert 'WHERE outcome_id = %s' in sql
    assert params == ('out_failed',)


def test_pgvector_compile_does_not_embed():
    store, conn = sync_store()
    provider = MagicMock()
    provider.embed.return_value = [0.1] * 1536
    store._embedding_adapter = provider
    result = make_handlers(store)['ncp_compile_decision_query'](dict(
        agent_id='a', task='retry', slot='retry', intent='review',
        schema_id='ncp.slot.binary'))
    assert result['evidence_count'] == 0
    provider.embed.assert_not_called()


@pytest.mark.anyio
async def test_async_pgvector_no_embedding_query():
    store, cursor = async_store()
    provider = MagicMock()
    provider.embed.return_value = [0.1] * 1536
    store._embedding_adapter = provider
    assert await store.async_query('retry', allow_embedding=False) == []
    provider.embed.assert_not_called()


def test_pgvector_missing_outcome_returns_none():
    store, _ = sync_store()
    assert store.get_outcome('missing') is None


@pytest.mark.anyio
async def test_async_pgvector_missing_outcome_returns_none():
    store, _ = async_store()
    assert await store.async_get_outcome('missing') is None


@pytest.mark.parametrize('mode', ['hybrid', 'trust_recency'])
def test_pgvector_normal_query_still_embeds(mode):
    store, _ = sync_store()
    provider = MagicMock()
    provider.embed.return_value = [0.1] * 1536
    store._embedding_adapter = provider
    assert store.query('retry', retrieval_mode=mode) == []
    provider.embed.assert_called_once_with('retry')
