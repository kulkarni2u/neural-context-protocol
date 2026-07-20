"""WI-P3: multi-hop outcome credit assignment (CAP-T3 extension).

Investigation finding: outcome-driven trust deltas already take the exact
same propagation path as retrieval/dissent deltas. ``compute_feedback_updates``
(ncp/stores/calibration.py) folds ``outcome_evidence`` into each row's net
``direct[cid]`` delta *before* the multi-hop propagation loop runs (calibration.py
~L100-121), and that loop (~L123-142) propagates ``direct.get(row.chunk_id, 0.0)``
verbatim up to ``propagation_max_hops`` hops at ``propagation_factor ** hop`` --
it has no branch that treats outcome-sourced deltas differently from
retrieval/dissent-sourced ones. So no propagation logic needed to change.

This file proves that behavior end-to-end (recorded outcomes on a grandchild
moving a grandparent's trust by delta*factor**2, for both success and
failure, plus cycle safety) and covers the new attribution surfacing added
here: ``FeedbackResult.outcome_propagated`` / ``CalibrationReport.outcome_propagated``
and the ``reason="outcome_propagation"`` change-log tag, which let a caller
distinguish outcome-driven propagation from retrieval/dissent-driven
propagation -- previously both were tagged identically as
``"trust_propagation"`` with no way to tell them apart.
"""

from __future__ import annotations

from pathlib import Path
from uuid import uuid4

from click.testing import CliRunner

from ncp.cli import main
from ncp.stores.calibration import FeedbackRow, compute_feedback_updates
from ncp.stores.sqlite import SQLiteStore
from ncp.types import OutcomeRecord, SubconsciousChunk


def _chunk(chunk_id: str, **overrides: object) -> SubconsciousChunk:
    kwargs: dict = {
        "chunk_id": chunk_id,
        "layer": "semantic",
        "content": f"chunk body {chunk_id} :: {uuid4().hex}",
        "src": "tool_result",
        "written_by": "tester",
        "pipeline_id": "pipe_1",
        "base_trust": 0.6,
    }
    kwargs.update(overrides)
    return SubconsciousChunk(**kwargs)


# ---------------------------------------------------------------------------
# Unit level: compute_feedback_updates with outcome_evidence + multi-hop
# ---------------------------------------------------------------------------


def test_two_hop_outcome_success_propagation_is_factor_squared() -> None:
    rows = [
        FeedbackRow(chunk_id="grandchild", base_trust=0.5, retrieval_count=0, caused_by="parent"),
        FeedbackRow(chunk_id="parent", base_trust=0.5, retrieval_count=0, caused_by="grandparent"),
        FeedbackRow(chunk_id="grandparent", base_trust=0.5, retrieval_count=0),
    ]
    result = compute_feedback_updates(
        rows,
        feedback_weight=0.15,
        propagation_factor=0.5,
        propagation_max_hops=2,
        outcome_evidence={"grandchild": 0.4},
    )
    by_id = {cid: trust for trust, cid in result.updates}
    # grandchild: +0.4 -> 0.9; parent: +0.4*0.5 -> 0.7; grandparent: +0.4*0.25 -> 0.6
    assert abs(by_id["grandchild"] - 0.9) < 1e-9
    assert abs(by_id["parent"] - 0.7) < 1e-9
    assert abs(by_id["grandparent"] - 0.6) < 1e-9

    reasons = {entry["chunk_id"]: entry["reason"] for entry in result.change_log}
    assert reasons["grandchild"] == "outcome_success"
    assert reasons["parent"] == "outcome_propagation"
    assert reasons["grandparent"] == "outcome_propagation"
    assert result.outcome_propagated == 2


def test_two_hop_outcome_failure_propagation_is_factor_squared() -> None:
    rows = [
        FeedbackRow(chunk_id="grandchild", base_trust=0.7, retrieval_count=0, caused_by="parent"),
        FeedbackRow(chunk_id="parent", base_trust=0.7, retrieval_count=0, caused_by="grandparent"),
        FeedbackRow(chunk_id="grandparent", base_trust=0.7, retrieval_count=0),
    ]
    result = compute_feedback_updates(
        rows,
        feedback_weight=0.15,
        propagation_factor=0.5,
        propagation_max_hops=2,
        outcome_evidence={"grandchild": -0.4},
    )
    by_id = {cid: trust for trust, cid in result.updates}
    # grandchild: -0.4 -> 0.3; parent: -0.4*0.5 -> 0.5; grandparent: -0.4*0.25 -> 0.6
    assert abs(by_id["grandchild"] - 0.3) < 1e-9
    assert abs(by_id["parent"] - 0.5) < 1e-9
    assert abs(by_id["grandparent"] - 0.6) < 1e-9

    reasons = {entry["chunk_id"]: entry["reason"] for entry in result.change_log}
    assert reasons["grandchild"] == "outcome_failure"
    assert reasons["parent"] == "outcome_propagation"
    assert reasons["grandparent"] == "outcome_propagation"
    assert result.outcome_propagated == 2


def test_outcome_propagation_cycle_is_safe() -> None:
    rows = [
        FeedbackRow(chunk_id="a", base_trust=0.5, retrieval_count=0, caused_by="b"),
        FeedbackRow(chunk_id="b", base_trust=0.5, retrieval_count=0, caused_by="a"),
    ]
    result = compute_feedback_updates(
        rows,
        feedback_weight=0.15,
        propagation_factor=0.5,
        propagation_max_hops=5,
        outcome_evidence={"a": 0.4},
    )
    by_id = {cid: trust for trust, cid in result.updates}
    # a: direct +0.4 -> 0.9; b: propagated one hop +0.2 -> 0.7; the cycle
    # must not loop credit back into a a second time.
    assert abs(by_id["a"] - 0.9) < 1e-9
    assert abs(by_id["b"] - 0.7) < 1e-9
    assert result.outcome_propagated == 1


def test_outcome_evidence_absent_never_marked_outcome_propagation() -> None:
    """Retrieval-only propagation must still be tagged trust_propagation."""
    rows = [
        FeedbackRow(chunk_id="effect", base_trust=0.6, retrieval_count=10, caused_by="parent"),
        FeedbackRow(chunk_id="parent", base_trust=0.6, retrieval_count=0),
    ]
    result = compute_feedback_updates(
        rows, feedback_weight=0.15, propagation_factor=0.5, propagation_max_hops=1
    )
    reasons = {entry["chunk_id"]: entry["reason"] for entry in result.change_log}
    assert reasons["parent"] == "trust_propagation"
    assert result.outcome_propagated == 0


def test_mixed_outcome_and_retrieval_sources_ancestor_tagged_outcome() -> None:
    """An ancestor fed by both an outcome-sourced child and a retrieval-only
    child is tagged outcome_propagation (it did receive outcome-origin credit)."""
    rows = [
        FeedbackRow(chunk_id="outcome_child", base_trust=0.5, retrieval_count=0, caused_by="ancestor"),
        FeedbackRow(chunk_id="retrieval_child", base_trust=0.5, retrieval_count=10, caused_by="ancestor"),
        FeedbackRow(chunk_id="ancestor", base_trust=0.5, retrieval_count=0),
    ]
    result = compute_feedback_updates(
        rows,
        feedback_weight=0.15,
        propagation_factor=0.5,
        propagation_max_hops=1,
        outcome_evidence={"outcome_child": 0.3},
    )
    reasons = {entry["chunk_id"]: entry["reason"] for entry in result.change_log}
    assert reasons["ancestor"] == "outcome_propagation"
    assert result.outcome_propagated == 1


# ---------------------------------------------------------------------------
# SQLiteStore integration: ncp_record_outcome -> calibrate --feedback
# ---------------------------------------------------------------------------


def test_sqlite_grandchild_outcome_success_credits_grandparent(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "store.db")
    store.write(_chunk("grandparent"))
    store.write(_chunk("parent", caused_by="grandparent"))
    store.write(_chunk("grandchild", caused_by="parent"))

    store.record_outcome(
        OutcomeRecord(outcome_id="o1", chunk_ids=["grandchild"], success=True, weight=0.4)
    )
    report = store.calibrate(
        feedback_mode=True, propagation_factor=0.5, propagation_max_hops=2
    )

    zone = {c.chunk_id: c for c in store.get_working_zone(pipeline_id="pipe_1")}
    assert abs(zone["grandchild"].base_trust - 0.6 - 0.4) < 1e-9
    assert abs(zone["parent"].base_trust - 0.6 - 0.4 * 0.5) < 1e-9
    assert abs(zone["grandparent"].base_trust - 0.6 - 0.4 * 0.25) < 1e-9
    assert report.outcome_propagated == 2

    # Outcome consumed exactly once.
    with store._connect() as connection:
        assert len(store._load_unconsumed_outcomes(connection)) == 0


def test_sqlite_grandchild_outcome_failure_debits_grandparent(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "store.db")
    store.write(_chunk("grandparent"))
    store.write(_chunk("parent", caused_by="grandparent"))
    store.write(_chunk("grandchild", caused_by="parent"))

    store.record_outcome(
        OutcomeRecord(outcome_id="o1", chunk_ids=["grandchild"], success=False, weight=0.4)
    )
    report = store.calibrate(
        feedback_mode=True, propagation_factor=0.5, propagation_max_hops=2
    )

    zone = {c.chunk_id: c for c in store.get_working_zone(pipeline_id="pipe_1")}
    assert abs(zone["grandchild"].base_trust - (0.6 - 0.4)) < 1e-9
    assert abs(zone["parent"].base_trust - (0.6 - 0.4 * 0.5)) < 1e-9
    assert abs(zone["grandparent"].base_trust - (0.6 - 0.4 * 0.25)) < 1e-9
    assert report.outcome_propagated == 2


def test_sqlite_outcome_propagation_cycle_safe(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "store.db")
    store.write(_chunk("a", caused_by="b"))
    store.write(_chunk("b", caused_by="a"))

    store.record_outcome(OutcomeRecord(outcome_id="o1", chunk_ids=["a"], success=True, weight=0.4))
    report = store.calibrate(feedback_mode=True, propagation_factor=0.5, propagation_max_hops=5)

    zone = {c.chunk_id: c for c in store.get_working_zone(pipeline_id="pipe_1")}
    assert abs(zone["a"].base_trust - 0.6 - 0.4) < 1e-9
    assert abs(zone["b"].base_trust - 0.6 - 0.2) < 1e-9  # one hop only, no loop back into a
    assert report.outcome_propagated == 1


def test_sqlite_user_verified_ancestor_not_credited(tmp_path: Path) -> None:
    """user_verified protection is unchanged: a protected ancestor is excluded
    from feedback_rows entirely, so outcome-driven propagation cannot reach it."""
    store = SQLiteStore(tmp_path / "store.db")
    store.write(_chunk("parent", src="user_verified"))
    store.write(_chunk("child", caused_by="parent"))

    store.record_outcome(OutcomeRecord(outcome_id="o1", chunk_ids=["child"], success=True, weight=0.4))
    report = store.calibrate(feedback_mode=True, propagation_factor=0.5, propagation_max_hops=2)

    zone = {c.chunk_id: c for c in store.get_working_zone(pipeline_id="pipe_1")}
    assert zone["parent"].base_trust == 0.6  # untouched
    assert abs(zone["child"].base_trust - 0.6 - 0.4) < 1e-9
    assert report.outcome_propagated == 0


def test_sqlite_outcome_credit_idempotent_on_repeated_calibrate(tmp_path: Path) -> None:
    """Outcomes are consumed exactly once: a second calibrate pass with no new
    outcomes must not re-propagate the same credit up the chain again."""
    store = SQLiteStore(tmp_path / "store.db")
    store.write(_chunk("grandparent"))
    store.write(_chunk("parent", caused_by="grandparent"))
    store.write(_chunk("grandchild", caused_by="parent"))
    store.record_outcome(OutcomeRecord(outcome_id="o1", chunk_ids=["grandchild"], success=True, weight=0.4))

    first = store.calibrate(feedback_mode=True, propagation_factor=0.5, propagation_max_hops=2)
    trust_after_first = {
        c.chunk_id: c.base_trust for c in store.get_working_zone(pipeline_id="pipe_1")
    }
    second = store.calibrate(feedback_mode=True, propagation_factor=0.5, propagation_max_hops=2)
    trust_after_second = {
        c.chunk_id: c.base_trust for c in store.get_working_zone(pipeline_id="pipe_1")
    }

    assert first.outcome_propagated == 2
    assert second.outcome_propagated == 0
    assert second.feedback_adjusted == 0
    assert trust_after_second == trust_after_first


def test_sqlite_outcome_propagates_via_edge_row_when_scalar_absent(tmp_path: Path) -> None:
    """WI-G3's edge-row fallback for chunks with no caused_by scalar also
    carries outcome-driven propagation, not just retrieval/dissent."""
    from ncp.types import ChunkEdge

    store = SQLiteStore(tmp_path / "store.db")
    store.write(_chunk("cause_chunk"))
    store.write(_chunk("effect_chunk"))
    store.add_chunk_edges(
        [ChunkEdge(src_chunk_id="effect_chunk", dst_chunk_id="cause_chunk", edge_type="caused_by")]
    )

    store.record_outcome(
        OutcomeRecord(outcome_id="o1", chunk_ids=["effect_chunk"], success=True, weight=0.4)
    )
    report = store.calibrate(feedback_mode=True, propagation_factor=0.5, propagation_max_hops=1)

    zone = {c.chunk_id: c for c in store.get_working_zone(pipeline_id="pipe_1")}
    assert abs(zone["cause_chunk"].base_trust - 0.6 - 0.4 * 0.5) < 1e-9
    assert report.outcome_propagated == 1


# ---------------------------------------------------------------------------
# CLI surfacing: ncp calibrate --feedback attribution
# ---------------------------------------------------------------------------


def test_cli_calibrate_feedback_surfaces_outcome_propagation_count(tmp_path: Path) -> None:
    runner = CliRunner()
    runner.invoke(main, ["init", "--cwd", str(tmp_path)])
    store = SQLiteStore(tmp_path / ".ncp" / "store.db")
    store.write(_chunk("cause_chunk", pipeline_id="pipe_cli"))
    store.write(_chunk("effect_chunk", pipeline_id="pipe_cli", caused_by="cause_chunk"))
    store.record_outcome(
        OutcomeRecord(outcome_id="o1", chunk_ids=["effect_chunk"], success=True, weight=0.4)
    )

    result = runner.invoke(
        main, ["calibrate", "--cwd", str(tmp_path), "--feedback", "--propagation-max-hops", "1"]
    )

    assert result.exit_code == 0
    assert "via outcome propagation" in result.output
    zone = {c.chunk_id: c for c in store.get_working_zone(pipeline_id="pipe_cli")}
    assert zone["cause_chunk"].base_trust > 0.6


def test_cli_calibrate_feedback_zero_outcome_propagation_when_no_outcomes(tmp_path: Path) -> None:
    """Retrieval-only feedback runs still show the row, at zero."""
    runner = CliRunner()
    runner.invoke(main, ["init", "--cwd", str(tmp_path)])
    store = SQLiteStore(tmp_path / ".ncp" / "store.db")
    store.write(_chunk("cause_chunk", pipeline_id="pipe_cli2"))
    store.write(_chunk("effect_chunk", pipeline_id="pipe_cli2", caused_by="cause_chunk"))
    for _ in range(10):
        store.query("chunk body effect_chunk", k=4, min_score=0.0, pipeline_id="pipe_cli2")

    result = runner.invoke(main, ["calibrate", "--cwd", str(tmp_path), "--feedback"])

    assert result.exit_code == 0
    assert "via outcome propagation" in result.output
