"""Tests for 0.14.x Slice 1: AsyncPgvectorStore.async_consolidate() parity.

Spec for OpenCode implementer:
- PgvectorStore.consolidate() is fully implemented; AsyncPgvectorStore.consolidate()
  raises NotImplementedError via self._not_implemented("consolidate").
- Add async_consolidate(**kwargs) → ConsolidationReport with full async parity:
  1. SELECT all live chunks (NOT IN tombstones), optionally filtered by pipeline_id
  2. Filter by trust_floor; cluster with cluster_by_tags() from ncp.stores.consolidation
  3. find_merge_candidates() on each cluster (BM25/SequenceMatcher helpers — already exist)
  4. For each (keeper, losers):
     - If not dry_run: DELETE loser, INSERT tombstone (forward_ref=keeper.chunk_id,
       tombstoned_at=time.time(), expires_at=time.time()+86400), UPDATE keeper
       (generation=keeper.generation+1, supersedes=json.dumps([loser_ids]))
     - Append to report.merge_log regardless of dry_run
     - Increment report.merged, report.tombstoned
  5. Count non-merged cluster chunks in report.skipped
  6. If not dry_run and report.merged > 0: emit consolidation_ready whisper via
     _async_emit_consolidation_whisper (new private helper, try/except swallows errors)
  7. report.duration_seconds = time.monotonic() - started; return report

All tests RED before implementation. GREEN after.
"""

from __future__ import annotations

import time
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


def _make_chunk_row(
    chunk_id: str = "c1",
    layer: str = "episodic",
    content: str = "hello world test content",
    src: str = "agent_inferred",
    written_by: str = "test_agent",
    generation: int = 0,
    base_trust: float = 0.75,
    pipeline_id: str | None = "pipe1",
    zone: str = "working",
    scope: str = "pipeline",
    created_at: float | None = None,
    retrieval_count: int = 0,
) -> dict:
    return {
        "chunk_id": chunk_id,
        "layer": layer,
        "content": content,
        "src": src,
        "written_by": written_by,
        "generation": generation,
        "base_trust": base_trust,
        "pipeline_id": pipeline_id,
        "zone": zone,
        "scope": scope,
        "chunk_type": "prose",
        "schema_version": 1,
        "supersedes": None,
        "created_at": created_at or (time.time() - 7200),
        "retrieval_count": retrieval_count,
        "caused_by": None,
        "conscious_hash": None,
        "evidence_id": None,
        "result_confidence": None,
        "result_attempts": None,
        "valid_while": None,
        "expiry": None,
        "owner": None,
    }


# ---------------------------------------------------------------------------
# Test 1: returns ConsolidationReport
# ---------------------------------------------------------------------------

@pytest.mark.anyio
async def test_async_consolidate_returns_report() -> None:
    """async_consolidate must return a ConsolidationReport, not raise NotImplementedError."""
    from ncp.types import ConsolidationReport

    store = _make_store()
    # fetchall returns empty → nothing to merge
    store._test_cursor.fetchall = AsyncMock(return_value=[])

    report = await store.async_consolidate()

    assert isinstance(report, ConsolidationReport), (
        f"Expected ConsolidationReport, got {type(report)}"
    )
    assert report.merged == 0
    assert report.tombstoned == 0
    assert report.dry_run is False
    assert report.duration_seconds >= 0.0


# ---------------------------------------------------------------------------
# Test 2: dry_run=True — no writes
# ---------------------------------------------------------------------------

@pytest.mark.anyio
async def test_async_consolidate_dry_run_no_writes() -> None:
    """dry_run=True must not execute DELETE, INSERT or UPDATE statements."""
    # Two rows with identical content → merge candidates (SequenceMatcher ratio=1.0)
    shared_content = "duplicate chunk content that will be merged by similarity"
    rows = [
        _make_chunk_row(chunk_id="c1", content=shared_content, base_trust=0.8),
        _make_chunk_row(chunk_id="c2", content=shared_content, base_trust=0.6),
    ]
    store = _make_store()
    store._test_cursor.fetchall = AsyncMock(return_value=rows)

    await store.async_consolidate(dry_run=True)

    execute_calls = [str(c) for c in store._test_cursor.execute.call_args_list]
    write_calls = [c for c in execute_calls if any(k in c for k in ("DELETE", "INSERT", "UPDATE"))]
    assert write_calls == [], (
        f"dry_run=True must not execute writes; got: {write_calls}"
    )


# ---------------------------------------------------------------------------
# Test 3: dry_run=False → performs DELETE and INSERT tombstone
# ---------------------------------------------------------------------------

@pytest.mark.anyio
async def test_async_consolidate_performs_deletes_and_tombstones() -> None:
    """dry_run=False must DELETE loser chunk and INSERT tombstone record."""
    shared_content = "duplicate chunk content that will be merged by similarity"
    rows = [
        _make_chunk_row(chunk_id="keeper1", content=shared_content, base_trust=0.8),
        _make_chunk_row(chunk_id="loser1", content=shared_content, base_trust=0.5),
    ]
    store = _make_store()
    store._test_cursor.fetchall = AsyncMock(return_value=rows)

    await store.async_consolidate(dry_run=False)

    sql_calls = [str(c) for c in store._test_cursor.execute.call_args_list]
    delete_calls = [c for c in sql_calls if "DELETE" in c]
    insert_calls = [c for c in sql_calls if "INSERT" in c and "tombstone" in c.lower()]

    assert delete_calls, "Expected DELETE call for loser chunk"
    assert insert_calls, "Expected INSERT tombstone call for loser chunk"


# ---------------------------------------------------------------------------
# Test 4: dry_run=False → updates keeper generation and supersedes
# ---------------------------------------------------------------------------

@pytest.mark.anyio
async def test_async_consolidate_updates_keeper_generation() -> None:
    """dry_run=False must UPDATE keeper: generation+1, supersedes=[loser_ids]."""
    shared_content = "another duplicate content string for merge"
    rows = [
        _make_chunk_row(chunk_id="keeper2", content=shared_content, base_trust=0.9, generation=0),
        _make_chunk_row(chunk_id="loser2", content=shared_content, base_trust=0.4),
    ]
    store = _make_store()
    store._test_cursor.fetchall = AsyncMock(return_value=rows)

    await store.async_consolidate(dry_run=False)

    sql_calls = [str(c) for c in store._test_cursor.execute.call_args_list]
    update_calls = [c for c in sql_calls if "UPDATE" in c and ("generation" in c or "supersedes" in c)]
    assert update_calls, (
        f"Expected UPDATE on keeper with new generation/supersedes; execute calls: {sql_calls}"
    )


# ---------------------------------------------------------------------------
# Test 5: emits consolidation_ready whisper on merge
# ---------------------------------------------------------------------------

@pytest.mark.anyio
async def test_async_consolidate_emits_whisper_on_merge() -> None:
    """dry_run=False with merged>0 must emit a consolidation_ready whisper."""
    shared_content = "whisper test content"
    rows = [
        _make_chunk_row(chunk_id="wk1", content=shared_content, base_trust=0.8),
        _make_chunk_row(chunk_id="wl1", content=shared_content, base_trust=0.5),
    ]
    store = _make_store()
    store._test_cursor.fetchall = AsyncMock(return_value=rows)

    with patch.object(store, "_async_emit_consolidation_whisper", new_callable=AsyncMock) as mock_emit:
        await store.async_consolidate(dry_run=False)

    mock_emit.assert_awaited_once()


# ---------------------------------------------------------------------------
# Test 6: no whisper emitted on dry_run=True
# ---------------------------------------------------------------------------

@pytest.mark.anyio
async def test_async_consolidate_no_whisper_on_dry_run() -> None:
    """dry_run=True must not emit any whisper even when candidates exist."""
    shared_content = "no whisper dry run content"
    rows = [
        _make_chunk_row(chunk_id="wk2", content=shared_content, base_trust=0.8),
        _make_chunk_row(chunk_id="wl2", content=shared_content, base_trust=0.5),
    ]
    store = _make_store()
    store._test_cursor.fetchall = AsyncMock(return_value=rows)

    with patch.object(store, "_async_emit_consolidation_whisper", new_callable=AsyncMock) as mock_emit:
        await store.async_consolidate(dry_run=True)

    mock_emit.assert_not_awaited()


# ---------------------------------------------------------------------------
# Test 7: trust_floor filters low-trust chunks
# ---------------------------------------------------------------------------

@pytest.mark.anyio
async def test_async_consolidate_trust_floor_filters_low_trust() -> None:
    """Chunks with base_trust < trust_floor must be counted in skipped, not merged."""
    from ncp.types import ConsolidationReport

    shared_content = "low trust content that should be filtered"
    rows = [
        _make_chunk_row(chunk_id="lc1", content=shared_content, base_trust=0.05),
        _make_chunk_row(chunk_id="lc2", content=shared_content, base_trust=0.08),
    ]
    store = _make_store()
    store._test_cursor.fetchall = AsyncMock(return_value=rows)

    report = await store.async_consolidate(trust_floor=0.10)

    assert isinstance(report, ConsolidationReport)
    assert report.merged == 0, "Low-trust chunks must not be merged"
    assert report.skipped >= 2, f"Both low-trust chunks should be skipped; got skipped={report.skipped}"


# ---------------------------------------------------------------------------
# Test 8: report counts — merged, tombstoned, clusters_scanned
# ---------------------------------------------------------------------------

@pytest.mark.anyio
async def test_async_consolidate_report_counts() -> None:
    """Report must correctly count merged=1, tombstoned=1, clusters_scanned=1."""
    from ncp.types import ConsolidationReport

    shared_content = "count test content string"
    rows = [
        _make_chunk_row(chunk_id="rc_keeper", content=shared_content, base_trust=0.9),
        _make_chunk_row(chunk_id="rc_loser", content=shared_content, base_trust=0.4),
    ]
    store = _make_store()
    store._test_cursor.fetchall = AsyncMock(return_value=rows)

    report = await store.async_consolidate(dry_run=False)

    assert isinstance(report, ConsolidationReport)
    assert report.merged >= 1, f"Expected merged >= 1, got {report.merged}"
    assert report.tombstoned >= 1, f"Expected tombstoned >= 1, got {report.tombstoned}"
    assert report.clusters_scanned >= 1, f"Expected clusters_scanned >= 1, got {report.clusters_scanned}"
    assert len(report.merge_log) >= 1, f"Expected merge_log to have entries, got {report.merge_log}"
