"""Tests for retrieval-feedback-driven calibration (NCP 0.4.x Slice 2)."""

from __future__ import annotations

import os
import time
from pathlib import Path

import pytest

from ncp.stores.sqlite import SQLiteStore
from ncp.types import SubconsciousChunk


def _store(tmp_path: Path) -> SQLiteStore:
    return SQLiteStore(tmp_path / "feedback_test.db")


def _chunk(content: str, src: str = "agent_inferred", base_trust: float = 0.7) -> SubconsciousChunk:
    return SubconsciousChunk(
        content=content,
        layer="semantic",
        src=src,
        written_by="test",
        base_trust=base_trust,
    )


# ── retrieval_count tracking ──────────────────────────────────────────────────

def test_query_increments_retrieval_count(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.write(_chunk("authentication token management"))

    results = store.query("authentication token", k=4, min_score=0.0)
    assert len(results) > 0

    # re-fetch from DB to verify count was written
    results2 = store.query("authentication token", k=4, min_score=0.0)
    assert results2[0].retrieval_count >= 1


def test_query_increments_count_each_call(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.write(_chunk("machine learning pipeline"))

    store.query("machine learning", k=4, min_score=0.0)
    store.query("machine learning", k=4, min_score=0.0)
    store.query("machine learning", k=4, min_score=0.0)

    results = store.query("machine learning", k=4, min_score=0.0)
    assert results[0].retrieval_count >= 3


def test_query_sets_last_retrieved_at(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.write(_chunk("vector embedding search"))
    before = time.time()

    store.query("vector embedding", k=4, min_score=0.0)

    results = store.query("vector embedding", k=4, min_score=0.0)
    assert results[0].last_retrieved_at is not None
    assert results[0].last_retrieved_at >= before


def test_unretrieved_chunk_has_zero_count(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.write(_chunk("never queried content xyz"))
    # query for something completely different
    store.query("authentication token", k=4, min_score=0.0)

    results = store.query("never queried content xyz", k=4, min_score=0.0)
    # first time retrieved — count should now be 1 (just got retrieved)
    assert results[0].retrieval_count == 1


def test_new_chunk_starts_with_zero_retrieval_count(tmp_path: Path) -> None:
    store = _store(tmp_path)
    chunk = _chunk("brand new chunk")
    store.write(chunk)

    # get_working_zone to inspect without triggering query tracking
    zone = store.get_working_zone()
    match = next((c for c in zone if "brand new" in c.content), None)
    assert match is not None
    assert match.retrieval_count == 0
    assert match.last_retrieved_at is None


# ── calibrate feedback_mode ───────────────────────────────────────────────────

def test_feedback_mode_boosts_retrieved_chunks(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.write(_chunk("frequently retrieved document", base_trust=0.6))

    # Simulate 5 retrievals
    for _ in range(5):
        store.query("frequently retrieved", k=4, min_score=0.0)

    report = store.calibrate(feedback_mode=True, feedback_weight=0.15)
    assert report.feedback_adjusted >= 1
    assert report.adjusted == 0  # decay mode not active

    # trust should have increased
    results = store.query("frequently retrieved", k=4, min_score=0.0)
    assert results[0].base_trust > 0.6


def test_feedback_mode_does_not_touch_zero_retrieval_chunks(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.write(_chunk("never retrieved doc", base_trust=0.7))

    report = store.calibrate(feedback_mode=True)
    assert report.feedback_adjusted == 0
    assert report.skipped >= 1


def test_feedback_mode_protects_user_verified(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.write(_chunk("verified doc", src="user_verified", base_trust=0.9))
    # retrieve it a bunch
    for _ in range(10):
        store.query("verified doc", k=4, min_score=0.0)

    report = store.calibrate(feedback_mode=True)
    assert report.protected >= 1
    assert report.feedback_adjusted == 0


def test_feedback_boost_saturates_at_ten_retrievals(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.write(_chunk("high retrieval chunk", base_trust=0.7))

    for _ in range(20):
        store.query("high retrieval", k=4, min_score=0.0)

    report = store.calibrate(feedback_mode=True, feedback_weight=0.15)
    assert report.feedback_adjusted >= 1

    results = store.query("high retrieval", k=4, min_score=0.0)
    # max boost is +0.15 → 0.7 + 0.15 = 0.85, capped at 1.0
    assert results[0].base_trust <= 1.0
    assert results[0].base_trust >= 0.84


def test_feedback_dry_run_does_not_change_trust(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.write(_chunk("dry run feedback test", base_trust=0.6))

    for _ in range(5):
        store.query("dry run feedback", k=4, min_score=0.0)

    before_results = store.query("dry run feedback", k=4, min_score=0.0)
    trust_before = before_results[0].base_trust

    report = store.calibrate(feedback_mode=True, dry_run=True)
    assert report.feedback_adjusted >= 1
    assert report.dry_run is True

    after_results = store.query("dry run feedback", k=4, min_score=0.0)
    assert after_results[0].base_trust == trust_before


def test_decay_mode_unaffected_by_feedback_params(tmp_path: Path) -> None:
    store = _store(tmp_path)
    # Write an old-ish chunk by backdating — just test that decay mode runs normally
    store.write(_chunk("old decay doc", base_trust=0.8))

    report = store.calibrate(feedback_mode=False, decay_factor=0.85)
    # feedback_adjusted must be 0 in decay mode
    assert report.feedback_adjusted == 0


def test_calibration_report_has_feedback_adjusted_field(tmp_path: Path) -> None:
    store = _store(tmp_path)
    report = store.calibrate(feedback_mode=True)
    assert hasattr(report, "feedback_adjusted")
    assert isinstance(report.feedback_adjusted, int)


def test_change_log_records_retrieval_count(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.write(_chunk("log test chunk", base_trust=0.5))

    for _ in range(3):
        store.query("log test", k=4, min_score=0.0)

    report = store.calibrate(feedback_mode=True)
    assert report.feedback_adjusted >= 1
    fb_entry = next(e for e in report.change_log if e["reason"] == "retrieval_feedback")
    assert fb_entry["retrieval_count"] >= 3
    assert fb_entry["old_trust"] == pytest.approx(0.5, abs=0.01)
    assert fb_entry["new_trust"] > fb_entry["old_trust"]


# ── migration file ────────────────────────────────────────────────────────────

def test_migration_002_has_correct_columns() -> None:
    from importlib import resources
    pkg = resources.files("ncp.migrations")
    files = {Path(str(f)).stem: Path(str(f)) for f in pkg.iterdir() if str(f).endswith(".sql")}
    assert "002_add_retrieval_tracking" in files
    sql = files["002_add_retrieval_tracking"].read_text()
    assert "retrieval_count" in sql
    assert "last_retrieved_at" in sql
    assert "-- DOWN" in sql


# ── integration ───────────────────────────────────────────────────────────────

INTEGRATION = pytest.mark.skipif(
    not os.environ.get("NCP_RUN_PGVECTOR_INTEGRATION"),
    reason="set NCP_RUN_PGVECTOR_INTEGRATION=1 to run live Postgres tests",
)


@INTEGRATION
def test_pgvector_query_increments_retrieval_count(tmp_path: Path) -> None:
    import uuid
    from ncp.stores.pgvector import PgvectorStore

    dsn = os.environ["NCP_PGVECTOR_DSN"]
    schema = f"ncp_test_{uuid.uuid4().hex[:8]}"
    store = PgvectorStore(dsn=dsn, schema=schema, table_prefix="ncp_")
    try:
        store.write(_chunk("pgvector retrieval test chunk"))
        store.query("pgvector retrieval", k=4, min_score=0.0)
        results = store.query("pgvector retrieval", k=4, min_score=0.0)
        assert results[0].retrieval_count >= 1
    finally:
        pass  # schema cleanup handled by test infra
