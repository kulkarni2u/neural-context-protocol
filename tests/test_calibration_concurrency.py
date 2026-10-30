"""Concurrent feedback calibrations on pgvector apply each signal exactly once.

Regression: two passes running at once both read the same unapplied outcomes
and retrieval counts, so reputation was credited once per pass (alpha 3.0
serially vs ~9-13 with six concurrent passes). Trust looked correct only
because the passes overwrote each other with the same value.
"""

from __future__ import annotations

import os
import threading
from uuid import uuid4

import anyio
import pytest

from ncp.types import OutcomeRecord, SubconsciousChunk

pytestmark = pytest.mark.skipif(
    os.getenv("NCP_RUN_PGVECTOR_INTEGRATION") != "1",
    reason="set NCP_RUN_PGVECTOR_INTEGRATION=1 to run live pgvector integration tests",
)

_DSN = os.getenv("NCP_PGVECTOR_DSN", "postgresql://postgres:postgres@127.0.0.1:5432/ncp")
_PASSES = 6


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def _seeded_sync_store():
    pytest.importorskip("psycopg")
    from ncp.stores.pgvector import PgvectorStore

    store = PgvectorStore(_DSN, schema=f"ncp_cc_{uuid4().hex[:8]}", table_prefix="it_")
    store.write(SubconsciousChunk(
        chunk_id="c", layer="semantic", content="concurrency calibration subject",
        src="agent_inferred", pipeline_id="P", written_by="author", base_trust=0.5,
    ))
    for i in range(5):
        store.record_outcome(OutcomeRecord(outcome_id=f"o{i}", chunk_ids=["c"], success=True, weight=0.05))
    return store


def _state(store) -> tuple[float, float, int, int]:
    import psycopg

    with psycopg.connect(_DSN) as conn:
        trust = conn.execute(f"SELECT base_trust FROM {store.schema}.it_chunks").fetchone()[0]
        alpha, obs = conn.execute(f"SELECT alpha, obs_count FROM {store.schema}.it_reputation").fetchone()
        applied = conn.execute(f"SELECT COUNT(*) FROM {store.schema}.it_outcome_applications").fetchone()[0]
    return round(float(trust), 6), round(float(alpha), 6), int(obs), int(applied)


def _serial_baseline() -> tuple[float, float, int, int]:
    store = _seeded_sync_store()
    store.calibrate(pipeline_id="P", feedback_mode=True)
    return _state(store)


def test_concurrent_sync_calibrations_match_serial() -> None:
    expected = _serial_baseline()
    store = _seeded_sync_store()
    barrier = threading.Barrier(_PASSES)

    def run() -> None:
        barrier.wait()
        store.calibrate(pipeline_id="P", feedback_mode=True)

    threads = [threading.Thread(target=run) for _ in range(_PASSES)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert _state(store) == expected


@pytest.mark.anyio
async def test_concurrent_async_and_sync_calibrations_match_serial() -> None:
    pytest.importorskip("psycopg_pool")
    from ncp.stores.pgvector_async import AsyncPgvectorStore

    expected = _serial_baseline()
    sync_store = _seeded_sync_store()
    async_store = AsyncPgvectorStore(_DSN, schema=sync_store.schema, table_prefix="it_", max_pool_connections=_PASSES)
    try:
        async with anyio.create_task_group() as tg:
            for _ in range(_PASSES // 2):
                tg.start_soon(lambda: async_store.async_calibrate(pipeline_id="P", feedback_mode=True))
                tg.start_soon(
                    anyio.to_thread.run_sync,
                    lambda: sync_store.calibrate(pipeline_id="P", feedback_mode=True),
                )
    finally:
        await async_store.close()

    assert _state(sync_store) == expected
