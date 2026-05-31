"""Tests for 0.14.x Slice 2: AsyncPgvectorStore.async_calibrate() parity.

Spec for Codex implementer:
- PgvectorStore.calibrate() is fully implemented; AsyncPgvectorStore.calibrate()
  raises NotImplementedError via self._not_implemented("calibrate").
- Add async_calibrate(**kwargs) → CalibrationReport with full async parity:

  Manual mode (chunk_id + trust provided):
  1. Validate trust in [0.0, 1.0] — raise ValueError if out of range
  2. SELECT chunk WHERE chunk_id = %s AND NOT IN tombstones; if not found → skipped+1, return
  3. Append to change_log: {chunk_id, old_trust, new_trust=trust, reason="manual_override"}
  4. If not dry_run: UPDATE chunks SET base_trust = %s WHERE chunk_id = %s
  5. report.adjusted += 1; return report

  Batch mode (no chunk_id):
  1. SELECT chunk_id, base_trust, src, generation, created_at, retrieval_count FROM chunks
     NOT IN tombstones; optionally filter by pipeline_id
  2. For each row:
     - If src == "user_verified": report.protected += 1; continue
     - Decay mode (feedback_mode=False):
       eligible = age_seconds > recency_half_life_seconds AND base_trust > 0.5 AND generation == 0
       new_trust = max(0.0, base_trust * decay_factor); reason="batch_decay"
       report.adjusted += 1 if eligible, else report.skipped += 1
     - Feedback mode (feedback_mode=True):
       if retrieval_count > 0:
         boost = feedback_weight * min(1.0, retrieval_count / 10)
         new_trust = min(1.0, base_trust + boost); reason="retrieval_feedback"
         change_log includes retrieval_count key; report.feedback_adjusted += 1
       else report.skipped += 1
  3. If not dry_run and updates: UPDATE chunks SET base_trust=%s WHERE chunk_id=%s for each
  4. report.duration_seconds = time.monotonic() - started; return report
  5. No whisper calls in calibrate().

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
    content: str = "test content",
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
        "created_at": created_at or (time.time() - 3600),
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
# Test 1: returns CalibrationReport
# ---------------------------------------------------------------------------

@pytest.mark.anyio
async def test_async_calibrate_returns_report() -> None:
    """async_calibrate must return a CalibrationReport, not raise NotImplementedError."""
    from ncp.types import CalibrationReport

    store = _make_store()
    store._test_cursor.fetchall = AsyncMock(return_value=[])

    report = await store.async_calibrate()

    assert isinstance(report, CalibrationReport), (
        f"Expected CalibrationReport, got {type(report)}"
    )
    assert report.adjusted == 0
    assert report.feedback_adjusted == 0
    assert report.duration_seconds >= 0.0


# ---------------------------------------------------------------------------
# Test 2: manual mode — updates trust on specific chunk
# ---------------------------------------------------------------------------

@pytest.mark.anyio
async def test_async_calibrate_manual_mode_updates_trust() -> None:
    """Manual mode (chunk_id + trust) must UPDATE base_trust for the specified chunk."""
    store = _make_store()
    row = _make_chunk_row(chunk_id="manualchunk", base_trust=0.5)
    store._test_cursor.fetchone = AsyncMock(return_value=row)

    report = await store.async_calibrate(chunk_id="manualchunk", trust=0.9)

    assert report.adjusted == 1, f"Expected adjusted=1, got {report.adjusted}"
    assert len(report.change_log) == 1
    entry = report.change_log[0]
    assert entry["chunk_id"] == "manualchunk"
    assert entry["new_trust"] == pytest.approx(0.9)
    assert entry["reason"] == "manual_override"

    execute_calls = [str(c) for c in store._test_cursor.execute.call_args_list]
    update_calls = [c for c in execute_calls if "UPDATE" in c and "base_trust" in c]
    assert update_calls, "Expected UPDATE base_trust call for manual mode"


# ---------------------------------------------------------------------------
# Test 3: manual mode dry_run=True — no UPDATE
# ---------------------------------------------------------------------------

@pytest.mark.anyio
async def test_async_calibrate_manual_mode_dry_run() -> None:
    """Manual mode with dry_run=True must NOT execute UPDATE."""
    store = _make_store()
    row = _make_chunk_row(chunk_id="drychunk", base_trust=0.5)
    store._test_cursor.fetchone = AsyncMock(return_value=row)

    report = await store.async_calibrate(chunk_id="drychunk", trust=0.9, dry_run=True)

    assert report.adjusted == 1, "adjusted should be incremented even in dry_run"
    execute_calls = [str(c) for c in store._test_cursor.execute.call_args_list]
    update_calls = [c for c in execute_calls if "UPDATE" in c and "base_trust" in c]
    assert not update_calls, f"dry_run=True must not UPDATE; got: {update_calls}"


# ---------------------------------------------------------------------------
# Test 4: batch decay — eligible old chunk gets decayed
# ---------------------------------------------------------------------------

@pytest.mark.anyio
async def test_async_calibrate_batch_decay_eligible_chunk() -> None:
    """Batch decay must reduce trust on old (age > 4h), high-trust (>0.5), gen-0 chunk."""
    old_created_at = time.time() - 20000  # ~5.5 hours old → eligible
    row = _make_chunk_row(
        chunk_id="oldchunk",
        base_trust=0.8,
        generation=0,
        created_at=old_created_at,
        src="agent_inferred",
    )
    store = _make_store()
    store._test_cursor.fetchall = AsyncMock(return_value=[row])

    report = await store.async_calibrate(decay_factor=0.85)

    assert report.adjusted == 1, f"Expected adjusted=1, got {report.adjusted}"
    assert len(report.change_log) == 1
    entry = report.change_log[0]
    assert entry["chunk_id"] == "oldchunk"
    assert entry["new_trust"] == pytest.approx(0.8 * 0.85, rel=1e-4)
    assert entry["reason"] == "batch_decay"


# ---------------------------------------------------------------------------
# Test 5: batch decay — young chunk is skipped
# ---------------------------------------------------------------------------

@pytest.mark.anyio
async def test_async_calibrate_batch_decay_young_chunk_skipped() -> None:
    """Batch decay must not decay chunks younger than recency_half_life_seconds."""
    recent_created_at = time.time() - 1000  # only ~17 minutes old → not eligible
    row = _make_chunk_row(
        chunk_id="youngchunk",
        base_trust=0.8,
        generation=0,
        created_at=recent_created_at,
    )
    store = _make_store()
    store._test_cursor.fetchall = AsyncMock(return_value=[row])

    report = await store.async_calibrate()  # default recency_half_life_seconds=14400

    assert report.adjusted == 0
    assert report.skipped >= 1


# ---------------------------------------------------------------------------
# Test 6: feedback mode — boosts trust proportional to retrieval_count
# ---------------------------------------------------------------------------

@pytest.mark.anyio
async def test_async_calibrate_feedback_mode_boosts_trust() -> None:
    """feedback_mode=True must boost base_trust using retrieval_count."""
    row = _make_chunk_row(
        chunk_id="retrieved",
        base_trust=0.6,
        retrieval_count=5,
    )
    store = _make_store()
    store._test_cursor.fetchall = AsyncMock(return_value=[row])

    report = await store.async_calibrate(feedback_mode=True, feedback_weight=0.15)

    assert report.feedback_adjusted == 1, f"Expected feedback_adjusted=1, got {report.feedback_adjusted}"
    assert len(report.change_log) == 1
    entry = report.change_log[0]
    expected_boost = 0.15 * min(1.0, 5 / 10)
    expected_new_trust = min(1.0, 0.6 + expected_boost)
    assert entry["new_trust"] == pytest.approx(expected_new_trust, rel=1e-4)
    assert entry["reason"] == "retrieval_feedback"
    assert entry["retrieval_count"] == 5


# ---------------------------------------------------------------------------
# Test 7: user_verified chunks are protected (not modified)
# ---------------------------------------------------------------------------

@pytest.mark.anyio
async def test_async_calibrate_user_verified_protected() -> None:
    """Chunks with src='user_verified' must be skipped and counted in report.protected."""
    old_created_at = time.time() - 20000
    row = _make_chunk_row(
        chunk_id="verified_chunk",
        base_trust=0.9,
        src="user_verified",
        generation=0,
        created_at=old_created_at,
    )
    store = _make_store()
    store._test_cursor.fetchall = AsyncMock(return_value=[row])

    report = await store.async_calibrate()

    assert report.protected == 1, f"Expected protected=1, got {report.protected}"
    assert report.adjusted == 0, "user_verified chunk must not be adjusted"


# ---------------------------------------------------------------------------
# Test 8: invalid trust raises ValueError
# ---------------------------------------------------------------------------

@pytest.mark.anyio
async def test_async_calibrate_invalid_trust_raises() -> None:
    """Manual mode must raise ValueError when trust is outside [0.0, 1.0]."""
    store = _make_store()

    with pytest.raises(ValueError):
        await store.async_calibrate(chunk_id="any", trust=1.5)

    with pytest.raises(ValueError):
        await store.async_calibrate(chunk_id="any", trust=-0.1)
