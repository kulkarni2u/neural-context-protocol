"""Tests for ncp calibrate — trust calibration/decay tooling."""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from ncp.stores.sqlite import SQLiteStore
from ncp.types import CalibrationReport, SubconsciousChunk


def _chunk(
    chunk_id: str,
    content: str,
    *,
    layer: str = "semantic",
    zone: str = "working",
    pipeline_id: str | None = "pipe_test",
    base_trust: float = 0.7,
    src: str = "agent_inferred",
    generation: int = 0,
) -> SubconsciousChunk:
    return SubconsciousChunk(
        chunk_id=chunk_id,
        content=content,
        layer=layer,
        zone=zone,
        pipeline_id=pipeline_id,
        src=src,
        written_by="test_agent",
        base_trust=base_trust,
        generation=generation,
        chunk_type="prose",
        scope="pipeline",
    )


def _write_aged_chunk(
    store: SQLiteStore,
    chunk: SubconsciousChunk,
    *,
    age_seconds: float = 0.0,
) -> None:
    """Write a chunk then manually back-date created_at to simulate age."""
    store.write(chunk)
    if age_seconds > 0.0:
        import sqlite3
        conn = sqlite3.connect(store.path)
        conn.execute(
            "UPDATE chunks SET created_at = ? WHERE chunk_id = ?",
            (time.time() - age_seconds, chunk.chunk_id),
        )
        conn.commit()
        conn.close()


# ---------------------------------------------------------------------------
# CalibrationReport dataclass
# ---------------------------------------------------------------------------


def test_calibration_report_defaults() -> None:
    report = CalibrationReport()
    assert report.adjusted == 0
    assert report.protected == 0
    assert report.skipped == 0
    assert report.duration_seconds == 0.0
    assert report.dry_run is False
    assert report.pipeline_id is None
    assert report.change_log == []


def test_calibration_report_fields_present() -> None:
    report = CalibrationReport(
        adjusted=3,
        protected=1,
        skipped=2,
        dry_run=True,
        pipeline_id="pipe_x",
        change_log=[{"chunk_id": "c1", "old_trust": 0.7, "new_trust": 0.595, "reason": "batch_decay"}],
    )
    assert report.adjusted == 3
    assert report.protected == 1
    assert report.skipped == 2
    assert report.dry_run is True
    assert report.pipeline_id == "pipe_x"
    assert len(report.change_log) == 1


# ---------------------------------------------------------------------------
# Empty store is a no-op
# ---------------------------------------------------------------------------


def test_calibrate_empty_store_is_noop(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "store.db")
    report = store.calibrate(pipeline_id="pipe_empty")
    assert report.adjusted == 0
    assert report.protected == 0
    assert report.skipped == 0
    assert isinstance(report.duration_seconds, float)
    assert report.duration_seconds >= 0.0


# ---------------------------------------------------------------------------
# Batch decay — eligible chunks get decayed
# ---------------------------------------------------------------------------


def test_batch_decay_adjusts_eligible_chunks(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "store.db")
    # Old chunk with base_trust > 0.5 and generation == 0 → eligible
    old_chunk = _chunk("old1", "stale knowledge about deployment", base_trust=0.8)
    _write_aged_chunk(store, old_chunk, age_seconds=20000)

    report = store.calibrate(pipeline_id="pipe_test", decay_factor=0.85)

    assert report.adjusted == 1
    assert report.skipped == 0
    assert report.protected == 0
    assert report.dry_run is False
    assert len(report.change_log) == 1
    entry = report.change_log[0]
    assert entry["chunk_id"] == "old1"
    assert abs(entry["new_trust"] - 0.8 * 0.85) < 1e-9
    assert entry["reason"] == "batch_decay"

    # Verify actual DB update
    import sqlite3
    conn = sqlite3.connect(store.path)
    row = conn.execute("SELECT base_trust FROM chunks WHERE chunk_id = 'old1'").fetchone()
    conn.close()
    assert abs(row[0] - 0.8 * 0.85) < 1e-9


def test_batch_decay_skips_young_chunks(tmp_path: Path) -> None:
    """Chunks younger than recency_half_life_seconds are skipped."""
    store = SQLiteStore(tmp_path / "store.db")
    young_chunk = _chunk("young1", "fresh knowledge just written today", base_trust=0.8)
    store.write(young_chunk)  # age ≈ 0 seconds

    report = store.calibrate(pipeline_id="pipe_test", recency_half_life_seconds=14400)

    assert report.adjusted == 0
    assert report.skipped == 1


def test_batch_decay_skips_low_trust_chunks(tmp_path: Path) -> None:
    """Chunks with base_trust <= 0.5 are not eligible for decay."""
    store = SQLiteStore(tmp_path / "store.db")
    low_trust = _chunk("low1", "low trust chunk content here", base_trust=0.4)
    _write_aged_chunk(store, low_trust, age_seconds=20000)

    report = store.calibrate(pipeline_id="pipe_test")

    assert report.adjusted == 0
    assert report.skipped == 1


def test_batch_decay_skips_nonzero_generation_chunks(tmp_path: Path) -> None:
    """Chunks with generation > 0 (merged/rewritten) are skipped."""
    store = SQLiteStore(tmp_path / "store.db")
    merged = _chunk("gen1", "merged chunk from consolidation", base_trust=0.8, generation=1)
    _write_aged_chunk(store, merged, age_seconds=20000)

    report = store.calibrate(pipeline_id="pipe_test")

    assert report.adjusted == 0
    assert report.skipped == 1


# ---------------------------------------------------------------------------
# user_verified chunks are protected
# ---------------------------------------------------------------------------


def test_user_verified_chunks_are_protected(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "store.db")
    verified = _chunk("uv1", "user confirmed this is accurate data", src="user_verified", base_trust=0.9)
    _write_aged_chunk(store, verified, age_seconds=20000)

    report = store.calibrate(pipeline_id="pipe_test")

    assert report.protected == 1
    assert report.adjusted == 0

    # Verify DB is untouched
    import sqlite3
    conn = sqlite3.connect(store.path)
    row = conn.execute("SELECT base_trust FROM chunks WHERE chunk_id = 'uv1'").fetchone()
    conn.close()
    assert abs(row[0] - 0.9) < 1e-9


def test_user_verified_protected_while_others_decay(tmp_path: Path) -> None:
    """Mixed batch: verified protected, others decayed."""
    store = SQLiteStore(tmp_path / "store.db")
    verified = _chunk("uv2", "user verified factual statement here", src="user_verified", base_trust=0.9)
    eligible = _chunk("el1", "agent inferred stale conclusion about system", base_trust=0.7)
    _write_aged_chunk(store, verified, age_seconds=20000)
    _write_aged_chunk(store, eligible, age_seconds=20000)

    report = store.calibrate(pipeline_id="pipe_test")

    assert report.protected == 1
    assert report.adjusted == 1


# ---------------------------------------------------------------------------
# Manual pinpoint override
# ---------------------------------------------------------------------------


def test_manual_override_sets_exact_trust(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "store.db")
    c = _chunk("manual1", "some content to override trust value", base_trust=0.7)
    store.write(c)

    report = store.calibrate(chunk_id="manual1", trust=0.3)

    assert report.adjusted == 1
    assert report.skipped == 0
    assert len(report.change_log) == 1
    assert report.change_log[0]["new_trust"] == 0.3
    assert report.change_log[0]["reason"] == "manual_override"

    # Verify actual DB update
    import sqlite3
    conn = sqlite3.connect(store.path)
    row = conn.execute("SELECT base_trust FROM chunks WHERE chunk_id = 'manual1'").fetchone()
    conn.close()
    assert abs(row[0] - 0.3) < 1e-9


def test_manual_override_missing_chunk_is_skipped(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "store.db")
    report = store.calibrate(chunk_id="nonexistent_chunk_xyz", trust=0.5)
    assert report.adjusted == 0
    assert report.skipped == 1


def test_manual_override_requires_trust_value(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "store.db")
    with pytest.raises(ValueError, match="trust is required"):
        store.calibrate(chunk_id="some_chunk_id")


# ---------------------------------------------------------------------------
# Dry-run does NOT modify the store
# ---------------------------------------------------------------------------


def test_dry_run_does_not_modify_store_batch(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "store.db")
    c = _chunk("dry1", "stale content that should be decayed in batch", base_trust=0.8)
    _write_aged_chunk(store, c, age_seconds=20000)

    report = store.calibrate(pipeline_id="pipe_test", dry_run=True)

    assert report.dry_run is True
    assert report.adjusted == 1  # Would-be count logged
    assert len(report.change_log) == 1  # Change logged

    # Verify DB is untouched
    import sqlite3
    conn = sqlite3.connect(store.path)
    row = conn.execute("SELECT base_trust FROM chunks WHERE chunk_id = 'dry1'").fetchone()
    conn.close()
    assert abs(row[0] - 0.8) < 1e-9  # Unchanged


def test_dry_run_does_not_modify_store_manual(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "store.db")
    c = _chunk("dry2", "content for manual dry run override test", base_trust=0.9)
    store.write(c)

    report = store.calibrate(chunk_id="dry2", trust=0.1, dry_run=True)

    assert report.dry_run is True
    assert report.adjusted == 1
    assert report.change_log[0]["new_trust"] == 0.1

    # Verify DB is untouched
    import sqlite3
    conn = sqlite3.connect(store.path)
    row = conn.execute("SELECT base_trust FROM chunks WHERE chunk_id = 'dry2'").fetchone()
    conn.close()
    assert abs(row[0] - 0.9) < 1e-9  # Unchanged


# ---------------------------------------------------------------------------
# Tombstoned chunks are excluded
# ---------------------------------------------------------------------------


def test_tombstoned_chunks_excluded_from_calibration(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "store.db")
    c = _chunk("tomb1", "tombstoned chunk content to exclude", base_trust=0.8)
    _write_aged_chunk(store, c, age_seconds=20000)
    store.tombstone("tomb1")

    report = store.calibrate(pipeline_id="pipe_test")
    assert report.adjusted == 0
