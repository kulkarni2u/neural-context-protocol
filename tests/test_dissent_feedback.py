"""Tests for dissent-driven trust penalties and their propagation along caused_by."""

import json
from pathlib import Path

from ncp.mcp.server import make_handlers, _handle_request
from ncp.stores.calibration import FeedbackRow, compute_feedback_updates
from ncp.stores.sqlite import SQLiteStore
from ncp.types import SubconsciousChunk


# ── pure helper: penalties and net deltas ─────────────────────────────────────


def test_dissent_applies_direct_penalty() -> None:
    rows = [FeedbackRow(chunk_id="bad", base_trust=0.7, retrieval_count=0, dissent_count=3)]
    result = compute_feedback_updates(rows, feedback_weight=0.15, propagation_factor=0.5, dissent_weight=0.2)

    by_id = {cid: trust for trust, cid in result.updates}
    assert abs(by_id["bad"] - 0.5) < 1e-9  # 0.7 - 0.2 (full penalty at 3 dissents)
    assert result.change_log[0]["reason"] == "dissent_penalty"
    assert result.change_log[0]["dissent_count"] == 3


def test_dissent_penalty_propagates_to_cause() -> None:
    rows = [
        FeedbackRow(chunk_id="effect", base_trust=0.7, retrieval_count=0, dissent_count=3, caused_by="cause"),
        FeedbackRow(chunk_id="cause", base_trust=0.7, retrieval_count=0, dissent_count=0),
    ]
    result = compute_feedback_updates(rows, feedback_weight=0.15, propagation_factor=0.5, dissent_weight=0.2)

    by_id = {cid: trust for trust, cid in result.updates}
    # effect: 0.7 - 0.2 = 0.5 ; cause debited 0.2*0.5 = 0.1 → 0.6
    assert abs(by_id["effect"] - 0.5) < 1e-9
    assert abs(by_id["cause"] - 0.6) < 1e-9
    reasons = {e["chunk_id"]: e["reason"] for e in result.change_log}
    assert reasons["cause"] == "trust_propagation"


def test_retrieval_and_dissent_net_out() -> None:
    # Retrieved 10x (+0.15) and disputed 3x (-0.2) → net -0.05.
    rows = [FeedbackRow(chunk_id="mixed", base_trust=0.6, retrieval_count=10, dissent_count=3)]
    result = compute_feedback_updates(rows, feedback_weight=0.15, propagation_factor=0.5, dissent_weight=0.2)

    by_id = {cid: trust for trust, cid in result.updates}
    assert abs(by_id["mixed"] - 0.55) < 1e-9
    entry = result.change_log[0]
    assert entry["reason"] == "mixed_feedback"
    assert entry["retrieval_count"] == 10
    assert entry["dissent_count"] == 3


def test_penalty_floors_at_zero() -> None:
    rows = [FeedbackRow(chunk_id="bad", base_trust=0.1, retrieval_count=0, dissent_count=3)]
    result = compute_feedback_updates(rows, feedback_weight=0.15, propagation_factor=0.5, dissent_weight=0.2)

    by_id = {cid: trust for trust, cid in result.updates}
    assert by_id["bad"] == 0.0  # clamped, not negative


def test_dissent_disabled_when_weight_zero() -> None:
    rows = [FeedbackRow(chunk_id="bad", base_trust=0.7, retrieval_count=0, dissent_count=5)]
    result = compute_feedback_updates(rows, feedback_weight=0.15, propagation_factor=0.5, dissent_weight=0.0)

    assert result.updates == []
    assert result.skipped == 1


# ── SQLite integration ────────────────────────────────────────────────────────


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


def test_record_dissent_increments_counter(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "store.db")
    store.write(_chunk("disputed", "some claim"))

    assert store.record_dissent("disputed") is True
    assert store.record_dissent("disputed") is True
    assert store.record_dissent("ctx://sub/disputed") is True  # prefix tolerated

    chunk = next(c for c in store.get_working_zone(pipeline_id="pipe_1") if c.chunk_id == "disputed")
    assert chunk.dissent_count == 3


def test_record_dissent_missing_chunk_returns_false(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "store.db")
    assert store.record_dissent("nonexistent") is False


def test_calibrate_penalizes_disputed_chunk_and_cause(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "store.db")
    store.write(_chunk("cause_chunk", "root analysis"))
    store.write(_chunk("effect_chunk", "disputed conclusion", caused_by="cause_chunk"))

    for _ in range(3):
        store.record_dissent("effect_chunk")

    report = store.calibrate(feedback_mode=True, propagation_factor=0.5, dissent_weight=0.2)
    assert report.feedback_adjusted >= 2

    zone = {c.chunk_id: c for c in store.get_working_zone(pipeline_id="pipe_1")}
    assert zone["effect_chunk"].base_trust < 0.7  # penalized
    assert zone["cause_chunk"].base_trust < 0.7   # cause debited via propagation
    assert zone["cause_chunk"].base_trust > zone["effect_chunk"].base_trust  # cause penalized less


# ── MCP end-to-end ────────────────────────────────────────────────────────────


def _call(name: str, arguments: dict) -> dict:
    return {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
            "params": {"name": name, "arguments": arguments}}


def _content(response_str: str) -> dict:
    r = json.loads(response_str)["result"]
    return json.loads(r["content"][0]["text"])


def test_emit_dissent_whisper_with_ref_records_dissent(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "store.db")
    store.write(_chunk("target_chunk", "the disputed claim"))
    handlers = make_handlers(store)

    result = _content(_handle_request(
        _call("ncp_emit_whisper", {
            "from": "reviewer",
            "target": "fixer",
            "type": "dissent",
            "payload": json.dumps({"issue": "wrong guard", "alternatives": ["use Optional"]}),
            "confidence": 0.9,
            "pipeline_id": "pipe_1",
            "ref": "target_chunk",
        }),
        handlers,
    ))

    assert result["emitted"] is True
    assert result["dissent_recorded"] is True
    chunk = next(c for c in store.get_working_zone(pipeline_id="pipe_1") if c.chunk_id == "target_chunk")
    assert chunk.dissent_count == 1


def test_non_dissent_whisper_does_not_record(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "store.db")
    store.write(_chunk("target_chunk", "a claim"))
    handlers = make_handlers(store)

    result = _content(_handle_request(
        _call("ncp_emit_whisper", {
            "from": "a",
            "target": "b",
            "type": "nudge",
            "payload": "fyi",
            "confidence": 0.9,
            "ref": "target_chunk",
        }),
        handlers,
    ))

    assert "dissent_recorded" not in result
    chunk = next(c for c in store.get_working_zone(pipeline_id="pipe_1") if c.chunk_id == "target_chunk")
    assert chunk.dissent_count == 0
