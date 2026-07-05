"""Tests for CAP-T3 outcome-calibrated reputation."""

from __future__ import annotations

import json
from pathlib import Path

from ncp.mcp.server import make_handlers, _handle_request
from ncp.stores.calibration import (
    FeedbackRow,
    compute_feedback_updates,
    compute_outcome_evidence,
    rollup_reputation,
)
from ncp.stores.sqlite import SQLiteStore
from ncp.types import OutcomeRecord, SubconsciousChunk


def _chunk(chunk_id: str, content: str, **kw: object) -> SubconsciousChunk:
    base: dict = {
        "chunk_id": chunk_id,
        "layer": "episodic",
        "content": content,
        "src": "agent_inferred",
        "written_by": "test",
        "base_trust": 0.7,
        "pipeline_id": "pipe_1",
    }
    base.update(kw)
    return SubconsciousChunk(**base)


def _call(name: str, arguments: dict) -> dict:
    return {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
            "params": {"name": name, "arguments": arguments}}


def _content(response_str: str) -> dict:
    r = json.loads(response_str)["result"]
    return json.loads(r["content"][0]["text"])


# ---------------------------------------------------------------------------
# compute_outcome_evidence unit tests
# ---------------------------------------------------------------------------


def test_outcome_evidence_success_delta() -> None:
    outcomes = [
        OutcomeRecord(outcome_id="o1", chunk_ids=["c1"], success=True, weight=1.0),
    ]
    ev = compute_outcome_evidence(outcomes)
    assert ev == {"c1": 1.0}


def test_outcome_evidence_failure_delta() -> None:
    outcomes = [
        OutcomeRecord(outcome_id="o2", chunk_ids=["c1"], success=False, weight=1.0),
    ]
    ev = compute_outcome_evidence(outcomes)
    assert ev == {"c1": -1.0}


def test_outcome_evidence_multi_chunk() -> None:
    outcomes = [
        OutcomeRecord(outcome_id="o1", chunk_ids=["c1", "c2"], success=True, weight=0.5),
        OutcomeRecord(outcome_id="o2", chunk_ids=["c2"], success=False, weight=0.3),
    ]
    ev = compute_outcome_evidence(outcomes)
    assert ev == {"c1": 0.5, "c2": 0.2}


def test_outcome_evidence_empty() -> None:
    assert compute_outcome_evidence([]) == {}


# ---------------------------------------------------------------------------
# compute_feedback_updates with outcome_evidence
# ---------------------------------------------------------------------------


def test_outcome_evidence_as_primary_signal() -> None:
    rows = [FeedbackRow(chunk_id="c1", base_trust=0.5, retrieval_count=0)]
    result = compute_feedback_updates(
        rows, feedback_weight=0.15, propagation_factor=0.0,
        outcome_evidence={"c1": 0.3},
    )
    by_id = {cid: trust for trust, cid in result.updates}
    assert abs(by_id["c1"] - 0.8) < 1e-9
    assert result.change_log[0]["reason"] == "outcome_success"
    assert result.change_log[0]["outcome_evidence"] == 0.3


def test_outcome_failure_decreases_trust() -> None:
    rows = [FeedbackRow(chunk_id="c1", base_trust=0.7, retrieval_count=0)]
    result = compute_feedback_updates(
        rows, feedback_weight=0.15, propagation_factor=0.0,
        outcome_evidence={"c1": -0.4},
    )
    by_id = {cid: trust for trust, cid in result.updates}
    assert abs(by_id["c1"] - 0.3) < 1e-9
    assert result.change_log[0]["reason"] == "outcome_failure"
    assert result.change_log[0]["outcome_evidence"] == -0.4


def test_outcome_with_usage_prior_weight_scales_retrieval() -> None:
    rows = [FeedbackRow(chunk_id="c1", base_trust=0.5, retrieval_count=10)]
    result = compute_feedback_updates(
        rows, feedback_weight=0.15, propagation_factor=0.0,
        usage_prior_weight=0.0, outcome_evidence={"c1": 0.2},
    )
    by_id = {cid: trust for trust, cid in result.updates}
    assert abs(by_id["c1"] - 0.7) < 1e-9  # 0.5 + 0.2 (no retrieval boost)


def test_outcome_with_default_usage_prior_full_retrieval() -> None:
    rows = [FeedbackRow(chunk_id="c1", base_trust=0.5, retrieval_count=10)]
    result = compute_feedback_updates(
        rows, feedback_weight=0.15, propagation_factor=0.0,
        usage_prior_weight=1.0, outcome_evidence={"c1": 0.2},
    )
    by_id = {cid: trust for trust, cid in result.updates}
    assert abs(by_id["c1"] - 0.85) < 1e-9  # 0.5 + 0.15 + 0.2


def test_outcome_evidence_defaults_to_no_signal() -> None:
    rows = [FeedbackRow(chunk_id="c1", base_trust=0.5, retrieval_count=10)]
    result = compute_feedback_updates(
        rows, feedback_weight=0.15, propagation_factor=0.0,
    )
    by_id = {cid: trust for trust, cid in result.updates}
    assert abs(by_id["c1"] - 0.65) < 1e-9  # 0.5 + 0.15 (default behavior)


# ---------------------------------------------------------------------------
# rollup_reputation with outcome_evidence
# ---------------------------------------------------------------------------


def test_rollup_includes_outcome_evidence() -> None:
    change_log = [
        {"chunk_id": "c1", "old_trust": 0.5, "new_trust": 0.5, "reason": "outcome_success"},
    ]
    chunk_author = {"c1": "author_a"}
    prior = {"author_a": (2.0, 2.0)}
    outcome_evidence = {"c1": 0.5}
    updates = rollup_reputation(
        change_log, chunk_author, prior,
        gain=2.0, forget=1.0, K_CONF=20,
        outcome_evidence=outcome_evidence,
    )
    assert len(updates) == 1
    u = updates[0]
    assert u.identity_id == "author_a"
    assert u.positive_evidence > 0.0


# ---------------------------------------------------------------------------
# SQLite store integration tests
# ---------------------------------------------------------------------------


def test_record_outcome_persists(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "store.db")
    out = OutcomeRecord(outcome_id="o1", chunk_ids=["c1"], success=True, weight=0.5)
    assert store.record_outcome(out) is True
    with store._connect() as conn:
        loaded = store._load_unconsumed_outcomes(conn)
    assert len(loaded) == 1
    assert loaded[0].outcome_id == "o1"
    assert loaded[0].chunk_ids == ["c1"]
    assert loaded[0].success is True
    assert loaded[0].weight == 0.5


def test_record_outcome_failure(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "store.db")
    out = OutcomeRecord(outcome_id="o2", chunk_ids=["c1"], success=False, weight=1.0)
    assert store.record_outcome(out) is True
    with store._connect() as conn:
        loaded = store._load_unconsumed_outcomes(conn)
    assert loaded[0].success is False


def test_calibrate_consumes_outcomes(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "store.db")
    store.write(_chunk("c1", "content"))
    out = OutcomeRecord(outcome_id="o1", chunk_ids=["c1"], success=True, weight=0.3)
    store.record_outcome(out)
    store.calibrate(feedback_mode=True, feedback_weight=0.15)
    with store._connect() as conn:
        loaded = store._load_unconsumed_outcomes(conn)
    assert len(loaded) == 0


def test_calibrate_outcome_raises_trust(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "store.db")
    store.write(_chunk("c1", "content"))
    out = OutcomeRecord(outcome_id="o1", chunk_ids=["c1"], success=True, weight=0.3)
    store.record_outcome(out)
    store.calibrate(feedback_mode=True, feedback_weight=0.15)
    chunk = store.get_chunks_by_ids(["c1"])[0]
    assert chunk.base_trust > 0.7


def test_outcomes_marked_consumed_not_loaded_again(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "store.db")
    store.write(_chunk("c1", "content"))
    store.record_outcome(OutcomeRecord(outcome_id="o1", chunk_ids=["c1"], success=True))
    store.calibrate(feedback_mode=True)
    store.calibrate(feedback_mode=True)
    with store._connect() as conn:
        loaded = store._load_unconsumed_outcomes(conn)
    assert len(loaded) == 0


# ---------------------------------------------------------------------------
# MCP end-to-end tests
# ---------------------------------------------------------------------------


def test_ncp_record_outcome_tool(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "store.db")
    store.write(_chunk("c1", "test content"))
    handlers = make_handlers(store)
    result = _content(_handle_request(
        _call("ncp_record_outcome", {
            "chunk_ids": ["c1"],
            "success": True,
            "weight": 0.5,
        }),
        handlers,
    ))
    assert result["recorded"] is True
    assert "outcome_id" in result


def test_ncp_record_outcome_fails_without_success(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "store.db")
    handlers = make_handlers(store)
    result = json.loads(_handle_request(
        _call("ncp_record_outcome", {"chunk_ids": ["c1"]}),
        handlers,
    ))
    assert "error" in result


def test_ncp_record_outcome_with_turn_id(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "store.db")
    store.write(_chunk("c1", "content", caused_by="turn_1"))
    store.write(_chunk("c2", "more", conscious_hash="turn_1"))
    handlers = make_handlers(store)
    result = _content(_handle_request(
        _call("ncp_record_outcome", {
            "turn_id": "turn_1",
            "success": True,
        }),
        handlers,
    ))
    assert result["recorded"] is True
    with store._connect() as conn:
        loaded = store._load_unconsumed_outcomes(conn)
    assert len(loaded) == 1
    assert len(loaded[0].chunk_ids) == 2
    assert "c1" in loaded[0].chunk_ids
    assert "c2" in loaded[0].chunk_ids
