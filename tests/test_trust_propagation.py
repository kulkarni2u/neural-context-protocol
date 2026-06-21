"""Tests for 1-hop trust propagation along caused_by edges during calibration."""

from pathlib import Path

from ncp.stores.calibration import FeedbackRow, compute_feedback_updates
from ncp.stores.sqlite import SQLiteStore
from ncp.types import SubconsciousChunk


# ── pure helper ───────────────────────────────────────────────────────────────


def test_direct_boost_only_when_no_propagation() -> None:
    rows = [FeedbackRow(chunk_id="a", base_trust=0.6, retrieval_count=5, caused_by="b"),
            FeedbackRow(chunk_id="b", base_trust=0.6, retrieval_count=0)]
    result = compute_feedback_updates(rows, feedback_weight=0.15, propagation_factor=0.0)

    by_id = {cid: trust for trust, cid in result.updates}
    assert "a" in by_id  # retrieved → boosted
    assert "b" not in by_id  # no propagation → unchanged
    assert result.adjusted == 1
    assert result.skipped == 1


def test_propagation_credits_caused_by_parent() -> None:
    rows = [FeedbackRow(chunk_id="effect", base_trust=0.6, retrieval_count=10, caused_by="cause"),
            FeedbackRow(chunk_id="cause", base_trust=0.6, retrieval_count=0)]
    result = compute_feedback_updates(rows, feedback_weight=0.15, propagation_factor=0.5)

    by_id = {cid: trust for trust, cid in result.updates}
    # effect: +0.15 → 0.75 ; cause: +0.15*0.5 = 0.075 → 0.675
    assert by_id["effect"] == 0.75
    assert abs(by_id["cause"] - 0.675) < 1e-9
    reasons = {e["chunk_id"]: e["reason"] for e in result.change_log}
    assert reasons["effect"] == "retrieval_feedback"
    assert reasons["cause"] == "trust_propagation"


def test_propagation_skips_missing_parent() -> None:
    rows = [FeedbackRow(chunk_id="effect", base_trust=0.6, retrieval_count=10, caused_by="not_present")]
    result = compute_feedback_updates(rows, feedback_weight=0.15, propagation_factor=0.5)

    by_id = {cid: trust for trust, cid in result.updates}
    assert "effect" in by_id
    assert len(result.updates) == 1  # missing parent gets no credit


def test_chunk_both_retrieved_and_parent_accumulates_both() -> None:
    # mid is retrieved AND is the cause of effect → gets direct + propagated.
    rows = [
        FeedbackRow(chunk_id="effect", base_trust=0.5, retrieval_count=10, caused_by="mid"),
        FeedbackRow(chunk_id="mid", base_trust=0.5, retrieval_count=10, caused_by=None),
    ]
    result = compute_feedback_updates(rows, feedback_weight=0.15, propagation_factor=0.5)

    by_id = {cid: trust for trust, cid in result.updates}
    # mid: direct +0.15 and propagated +0.075 → 0.5 + 0.225 = 0.725
    assert abs(by_id["mid"] - 0.725) < 1e-9


def test_propagation_caps_at_one() -> None:
    rows = [FeedbackRow(chunk_id="effect", base_trust=0.9, retrieval_count=10, caused_by="cause"),
            FeedbackRow(chunk_id="cause", base_trust=0.98, retrieval_count=0)]
    result = compute_feedback_updates(rows, feedback_weight=0.15, propagation_factor=0.5)

    by_id = {cid: trust for trust, cid in result.updates}
    assert by_id["cause"] <= 1.0


# ── SQLite integration ────────────────────────────────────────────────────────


def _chunk(chunk_id: str, content: str, **kw: object) -> SubconsciousChunk:
    base: dict = {
        "chunk_id": chunk_id,
        "layer": "episodic",
        "content": content,
        "src": "agent_inferred",
        "written_by": "test",
        "base_trust": 0.6,
        "pipeline_id": "pipe_1",
    }
    base.update(kw)
    return SubconsciousChunk(**base)


def test_sqlite_propagates_trust_to_cause(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "store.db")
    store.write(_chunk("cause_chunk", "root analysis content"))
    store.write(_chunk("effect_chunk", "frequently retrieved fix", caused_by="cause_chunk"))

    # Retrieve only the effect chunk a bunch of times.
    for _ in range(10):
        store.query("frequently retrieved fix", k=4, min_score=0.0, pipeline_id="pipe_1")

    report = store.calibrate(feedback_mode=True, feedback_weight=0.15, propagation_factor=0.5)
    assert report.feedback_adjusted >= 2  # effect (direct) + cause (propagated)

    zone = {c.chunk_id: c for c in store.get_working_zone(pipeline_id="pipe_1")}
    # The cause was never retrieved but gains trust because its effect proved useful.
    assert zone["cause_chunk"].base_trust > 0.6
    assert zone["effect_chunk"].base_trust > zone["cause_chunk"].base_trust


def test_sqlite_no_propagation_when_factor_zero(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "store.db")
    store.write(_chunk("cause_chunk", "root analysis content"))
    store.write(_chunk("effect_chunk", "frequently retrieved fix", caused_by="cause_chunk"))

    for _ in range(10):
        store.query("frequently retrieved fix", k=4, min_score=0.0, pipeline_id="pipe_1")

    store.calibrate(feedback_mode=True, propagation_factor=0.0)

    zone = {c.chunk_id: c for c in store.get_working_zone(pipeline_id="pipe_1")}
    assert zone["cause_chunk"].base_trust == 0.6  # untouched without propagation


def test_sqlite_propagation_protects_user_verified_parent(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "store.db")
    store.write(_chunk("verified_cause", "verified root", src="user_verified", base_trust=0.9))
    store.write(_chunk("effect_chunk", "frequently retrieved fix", caused_by="verified_cause"))

    for _ in range(10):
        store.query("frequently retrieved fix", k=4, min_score=0.0, pipeline_id="pipe_1")

    store.calibrate(feedback_mode=True, propagation_factor=0.5)

    zone = {c.chunk_id: c for c in store.get_working_zone(pipeline_id="pipe_1")}
    # user_verified parent is excluded from feedback rows → no propagated credit.
    assert zone["verified_cause"].base_trust == 0.9
