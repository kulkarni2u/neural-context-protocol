"""Regression tests: per-(outcome, chunk) consumption and full caused_by ancestry."""

from __future__ import annotations

from pathlib import Path

import pytest

from ncp.stores.calibration import scope_outcomes_to_rows, settled_outcome_ids
from ncp.stores.sqlite import SQLiteStore
from ncp.types import ChunkEdge, OutcomeRecord, SubconsciousChunk


def _store(tmp_path: Path) -> SQLiteStore:
    return SQLiteStore(tmp_path / "ncp.db")


def _chunk(cid: str, pipeline: str, **kw: object) -> SubconsciousChunk:
    kwargs: dict = {
        "chunk_id": cid,
        "layer": "semantic",
        "content": f"body of {cid}",
        "src": "tool_result",
        "written_by": "tester",
        "pipeline_id": pipeline,
        "base_trust": 0.5,
    }
    kwargs.update(kw)
    return SubconsciousChunk(**kwargs)


def _trust(store: SQLiteStore, cid: str) -> float:
    with store._connect() as c:
        return float(c.execute("SELECT base_trust FROM chunks WHERE chunk_id=?", (cid,)).fetchone()[0])


def _consumed(store: SQLiteStore, oid: str) -> int:
    with store._connect() as c:
        return int(c.execute("SELECT consumed FROM outcomes WHERE outcome_id=?", (oid,)).fetchone()[0])


def _applications(store: SQLiteStore) -> set[tuple[str, str]]:
    with store._connect() as c:
        return {(r[0], r[1]) for r in c.execute("SELECT outcome_id, chunk_id FROM outcome_applications")}


def _two_pipelines(store: SQLiteStore) -> None:
    store.write(_chunk("a1", "A"))
    store.write(_chunk("b1", "B"))


def test_calibrating_a_does_not_consume_b_outcome(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _two_pipelines(store)
    store.record_outcome(OutcomeRecord(outcome_id="o1", chunk_ids=["b1"], success=True, weight=0.2))
    store.calibrate(pipeline_id="A", feedback_mode=True)
    assert _consumed(store, "o1") == 0
    assert _trust(store, "b1") == pytest.approx(0.5)
    store.calibrate(pipeline_id="B", feedback_mode=True)
    assert _trust(store, "b1") == pytest.approx(0.7)
    assert _consumed(store, "o1") == 1


def test_spanning_outcome_applies_per_pipeline_without_double_apply(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _two_pipelines(store)
    store.record_outcome(OutcomeRecord(outcome_id="o1", chunk_ids=["a1", "b1"], success=True, weight=0.2))
    store.calibrate(pipeline_id="A", feedback_mode=True)
    assert _trust(store, "a1") == pytest.approx(0.7)
    assert _trust(store, "b1") == pytest.approx(0.5)
    assert _consumed(store, "o1") == 0
    assert _applications(store) == {("o1", "a1")}
    store.calibrate(pipeline_id="A", feedback_mode=True)
    assert _trust(store, "a1") == pytest.approx(0.7)
    store.calibrate(pipeline_id="B", feedback_mode=True)
    assert _trust(store, "b1") == pytest.approx(0.7)
    assert _trust(store, "a1") == pytest.approx(0.7)
    assert _consumed(store, "o1") == 1
    store.calibrate(pipeline_id="B", feedback_mode=True)
    store.calibrate(feedback_mode=True)
    assert _trust(store, "a1") == pytest.approx(0.7)
    assert _trust(store, "b1") == pytest.approx(0.7)


def test_global_calibration_applies_and_consumes_everything(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _two_pipelines(store)
    store.record_outcome(OutcomeRecord(outcome_id="o1", chunk_ids=["a1", "b1"], success=True, weight=0.2))
    store.calibrate(feedback_mode=True)
    assert _trust(store, "a1") == pytest.approx(0.7)
    assert _trust(store, "b1") == pytest.approx(0.7)
    assert _consumed(store, "o1") == 1


def test_dry_run_writes_nothing(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _two_pipelines(store)
    store.record_outcome(OutcomeRecord(outcome_id="o1", chunk_ids=["a1"], success=True, weight=0.2))
    report = store.calibrate(pipeline_id="A", feedback_mode=True, dry_run=True)
    assert report.feedback_adjusted >= 1
    assert _consumed(store, "o1") == 0
    assert _applications(store) == set()
    assert _trust(store, "a1") == pytest.approx(0.5)


def test_outcome_on_user_verified_or_tombstoned_chunk_is_consumed(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.write(_chunk("a1", "A"))
    store.write(_chunk("v1", "A", src="user_verified", base_trust=0.9))
    store.write(_chunk("t1", "A"))
    store.tombstone("t1")
    store.record_outcome(OutcomeRecord(outcome_id="ov", chunk_ids=["v1"], success=True, weight=0.2))
    store.record_outcome(OutcomeRecord(outcome_id="ot", chunk_ids=["t1"], success=True, weight=0.2))
    store.record_outcome(OutcomeRecord(outcome_id="om", chunk_ids=["a1", "missing"], success=True, weight=0.2))
    store.calibrate(pipeline_id="A", feedback_mode=True)
    assert _consumed(store, "ov") == 1
    assert _consumed(store, "ot") == 1
    assert _consumed(store, "om") == 1
    assert _trust(store, "v1") == pytest.approx(0.9)


def test_empty_chunk_outcome_is_consumed(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.write(_chunk("a1", "A"))
    store.record_outcome(OutcomeRecord(outcome_id="oe", chunk_ids=[], success=True, weight=0.2))
    store.calibrate(pipeline_id="A", feedback_mode=True)
    assert _consumed(store, "oe") == 1


def _edge_chain(store: SQLiteStore, n: int) -> None:
    for i in range(n):
        store.write(_chunk(f"child{i}", "P"), allow_duplicate=True)
        store.write(_chunk(f"parent{i}", "P"), allow_duplicate=True)
    store.add_chunk_edges([
        ChunkEdge(
            src_chunk_id=f"child{i}",
            dst_chunk_id=f"parent{i}",
            edge_type="caused_by",
            created_at=1000.0 + i,
        )
        for i in range(n)
    ])


def test_calibrate_uses_oldest_caused_by_edge_beyond_200(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _edge_chain(store, 201)
    store.record_outcome(OutcomeRecord(chunk_ids=["child0"], success=True, weight=0.2))
    store.calibrate(pipeline_id="P", feedback_mode=True)
    assert _trust(store, "child0") == pytest.approx(0.7)
    assert _trust(store, "parent0") == pytest.approx(0.6)


def test_get_chunk_edges_limit_none_is_unlimited_and_batched(tmp_path: Path) -> None:
    store = _store(tmp_path)
    n = 1200  # exceeds both the 200 default and the 500-id batch size
    edges = [
        ChunkEdge(src_chunk_id=f"c{i}", dst_chunk_id=f"p{i}", edge_type="caused_by", created_at=1000.0 + i)
        for i in range(n)
    ]
    with store._connect() as connection:  # bypass chunk-existence/ownership filtering
        store._upsert_chunk_edges(connection, edges)
    ids = [f"c{i}" for i in range(n)]
    assert len(store.get_chunk_edges(ids)) == 200
    got = store.get_chunk_edges(ids, limit=None)
    assert len(got) == n
    assert got[0].created_at >= got[-1].created_at
    assert len(store.get_chunk_edges(ids, limit=7)) == 7


def test_scope_and_settled_helpers() -> None:
    out = OutcomeRecord(outcome_id="o", chunk_ids=["a", "b", "c"], success=True)
    scoped = scope_outcomes_to_rows([out], {"a", "b"}, {("o", "a")})
    assert [o.chunk_ids for o in scoped] == [["b"]]
    assert scope_outcomes_to_rows([out], {"z"}, set()) == []
    applied = {("o", "a"), ("o", "b")}
    assert settled_outcome_ids([out], applied, {"a", "b", "c"}) == []
    assert settled_outcome_ids([out], applied, {"a", "b"}) == ["o"]
