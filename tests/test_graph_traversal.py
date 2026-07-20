"""WI-G2/G3: bounded multi-hop edge expansion and multi-hop trust propagation.

Covers:
- Assembler._expand_edges: 2-hop caused_by expansion with decay**h relevance,
  cycle safety, edge_max_hops=1 parity with the legacy 1-hop implementation,
  typed-edge traversal for a non-caused_by type, and stale caused_by edge row
  vs current scalar preference.
- compute_feedback_updates: 2-hop propagation amount (factor**h), cycle
  safety, and multi-children-one-ancestor accumulation, plus SQLiteStore
  integration for the config-wired propagation_max_hops and the
  caused_by-edge-row fallback when a chunk has no caused_by scalar.
"""

from __future__ import annotations

from pathlib import Path
from uuid import uuid4

from ncp.assembler import Assembler
from ncp.config import NCPConfig
from ncp.stores.calibration import FeedbackRow, compute_feedback_updates
from ncp.stores.sqlite import SQLiteStore
from ncp.types import ChunkEdge, SubconsciousChunk


def _chunk(chunk_id: str, **overrides: object) -> SubconsciousChunk:
    # Content embeds a random suffix so same-pipeline/layer/zone chunks in a
    # test never trip SQLiteStore._is_duplicate's near-duplicate-content
    # filter (unrelated to graph engineering, but bites fixtures with
    # similar chunk_id-based content strings).
    kwargs: dict = {
        "chunk_id": chunk_id,
        "layer": "semantic",
        "content": f"chunk body {chunk_id} :: {uuid4().hex}",
        "src": "tool_result",
        "written_by": "tester",
        "pipeline_id": "pipe_1",
    }
    kwargs.update(overrides)
    return SubconsciousChunk(**kwargs)


def _config(**retrieval_overrides: object) -> NCPConfig:
    return NCPConfig(values={"retrieval": dict(retrieval_overrides)}, project_root=Path("."))


# ---------------------------------------------------------------------------
# WI-G2: multi-hop edge expansion (Assembler._expand_edges)
# ---------------------------------------------------------------------------


def test_two_hop_expansion_pulls_grandparent_with_decay_squared(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "store.db")
    store.write(_chunk("grandparent"))
    store.write(_chunk("parent", caused_by="grandparent"))
    store.write(_chunk("child", caused_by="parent"))

    config = _config(edge_max_hops=2, edge_expansion_types=["caused_by"])
    assembler = Assembler(store=store, config=config)
    child = store.get_chunks_by_ids(["child"])[0].model_copy(update={"relevance": 0.8})

    expanded = {c.chunk_id: c for c in assembler._expand_edges([child], limit=10)}

    assert "parent" in expanded
    assert "grandparent" in expanded
    assert abs(expanded["parent"].relevance - 0.8 * 0.7) < 1e-9
    assert abs(expanded["grandparent"].relevance - 0.8 * 0.7**2) < 1e-9


def test_edge_max_hops_one_matches_legacy_behavior(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "store.db")
    store.write(_chunk("grandparent"))
    store.write(_chunk("parent", caused_by="grandparent"))
    store.write(_chunk("child", caused_by="parent"))

    config = _config(edge_max_hops=1, edge_expansion_types=["caused_by"])
    assembler = Assembler(store=store, config=config)
    child = store.get_chunks_by_ids(["child"])[0].model_copy(update={"relevance": 0.8})

    expanded = {c.chunk_id: c for c in assembler._expand_edges([child], limit=10)}

    assert "parent" in expanded
    assert "grandparent" not in expanded  # legacy: only 1 hop, ever


def test_cycle_in_edges_terminates(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "store.db")
    store.write(_chunk("a", caused_by="b"))
    store.write(_chunk("b", caused_by="a"))

    config = _config(edge_max_hops=5, edge_expansion_types=["caused_by"])
    assembler = Assembler(store=store, config=config)
    a = store.get_chunks_by_ids(["a"])[0].model_copy(update={"relevance": 0.9})

    # Must terminate rather than looping a <-> b forever.
    expanded = assembler._expand_edges([a], limit=10)

    ids = {c.chunk_id for c in expanded}
    assert ids == {"b"}  # a is already present; the cycle stops after one hop back


def test_typed_edge_traversal_for_non_caused_by_type(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "store.db")
    store.write(_chunk("child2"))
    store.write(_chunk("other"))
    store.add_chunk_edges(
        [ChunkEdge(src_chunk_id="child2", dst_chunk_id="other", edge_type="supports", weight=0.5)]
    )

    config = _config(edge_max_hops=1, edge_expansion_types=["supports"])
    assembler = Assembler(store=store, config=config)
    child2 = store.get_chunks_by_ids(["child2"])[0].model_copy(update={"relevance": 1.0})

    expanded = {c.chunk_id: c for c in assembler._expand_edges([child2], limit=10)}

    assert "other" in expanded
    assert abs(expanded["other"].relevance - 1.0 * 0.7 * 0.5) < 1e-9


def test_stale_caused_by_edge_row_vs_scalar_preference(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "store.db")
    store.write(_chunk("old_target"))
    store.write(_chunk("new_target"))
    # First write backfills an edge row a -> old_target.
    store.write(_chunk("a", caused_by="old_target"))
    # Rewriting a with a different cause updates the scalar, but the earlier
    # edge row a -> old_target is never retracted (additive-only backfill).
    store.write(_chunk("a", content=f"a rewritten :: {uuid4().hex}", caused_by="new_target"))

    config = _config(edge_max_hops=1, edge_expansion_types=["caused_by"])
    assembler = Assembler(store=store, config=config)
    a = store.get_chunks_by_ids(["a"])[0].model_copy(update={"relevance": 1.0})

    expanded = {c.chunk_id: c for c in assembler._expand_edges([a], limit=10)}

    assert "new_target" in expanded  # current scalar wins
    assert "old_target" not in expanded  # stale edge row skipped


# ---------------------------------------------------------------------------
# WI-G3: multi-hop trust propagation (compute_feedback_updates)
# ---------------------------------------------------------------------------


def test_two_hop_propagation_amount_is_factor_squared() -> None:
    rows = [
        FeedbackRow(chunk_id="effect", base_trust=0.6, retrieval_count=10, caused_by="parent"),
        FeedbackRow(chunk_id="parent", base_trust=0.6, retrieval_count=0, caused_by="grandparent"),
        FeedbackRow(chunk_id="grandparent", base_trust=0.6, retrieval_count=0),
    ]
    result = compute_feedback_updates(
        rows, feedback_weight=0.15, propagation_factor=0.5, propagation_max_hops=2
    )

    by_id = {cid: trust for trust, cid in result.updates}
    # effect: +0.15 -> 0.75; parent: +0.15*0.5 -> 0.675;
    # grandparent: +0.15*0.5**2 -> 0.6375
    assert by_id["effect"] == 0.75
    assert abs(by_id["parent"] - 0.675) < 1e-9
    assert abs(by_id["grandparent"] - 0.6375) < 1e-9


def test_propagation_max_hops_one_reproduces_legacy_behavior() -> None:
    rows = [
        FeedbackRow(chunk_id="effect", base_trust=0.6, retrieval_count=10, caused_by="parent"),
        FeedbackRow(chunk_id="parent", base_trust=0.6, retrieval_count=0, caused_by="grandparent"),
        FeedbackRow(chunk_id="grandparent", base_trust=0.6, retrieval_count=0),
    ]
    result = compute_feedback_updates(
        rows, feedback_weight=0.15, propagation_factor=0.5, propagation_max_hops=1
    )

    by_id = {cid: trust for trust, cid in result.updates}
    assert "grandparent" not in by_id  # default hop bound: no 2nd-hop credit


def test_propagation_cycle_is_safe() -> None:
    rows = [
        FeedbackRow(chunk_id="a", base_trust=0.6, retrieval_count=10, caused_by="b"),
        FeedbackRow(chunk_id="b", base_trust=0.6, retrieval_count=0, caused_by="a"),
    ]
    result = compute_feedback_updates(
        rows, feedback_weight=0.15, propagation_factor=0.5, propagation_max_hops=5
    )

    by_id = {cid: trust for trust, cid in result.updates}
    # a: direct +0.15 -> 0.75; b: propagated one hop +0.075 -> 0.675; the
    # cycle must not loop credit back into a a second time.
    assert abs(by_id["a"] - 0.75) < 1e-9
    assert abs(by_id["b"] - 0.675) < 1e-9


def test_multi_children_one_ancestor_accumulates() -> None:
    rows = [
        FeedbackRow(chunk_id="child1", base_trust=0.5, retrieval_count=10, caused_by="ancestor"),
        FeedbackRow(chunk_id="child2", base_trust=0.5, retrieval_count=10, caused_by="ancestor"),
        FeedbackRow(chunk_id="ancestor", base_trust=0.5, retrieval_count=0),
    ]
    result = compute_feedback_updates(
        rows, feedback_weight=0.15, propagation_factor=0.5, propagation_max_hops=1
    )

    by_id = {cid: trust for trust, cid in result.updates}
    # ancestor accumulates propagated credit from both children: 0.075 * 2
    assert abs(by_id["ancestor"] - 0.65) < 1e-9


def test_sqlite_calibrate_propagates_two_hops(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "store.db")
    store.write(_chunk("grandparent", base_trust=0.6, content=f"grandparent_root_content {uuid4().hex}"))
    store.write(
        _chunk(
            "parent", base_trust=0.6, caused_by="grandparent", content=f"parent_mid_content {uuid4().hex}"
        )
    )
    store.write(
        _chunk(
            "effect",
            base_trust=0.6,
            caused_by="parent",
            content=f"effect_leaf_frequently_retrieved {uuid4().hex}",
        )
    )

    for _ in range(10):
        store.query("effect_leaf_frequently_retrieved", k=4, min_score=0.0, pipeline_id="pipe_1")

    store.calibrate(feedback_mode=True, feedback_weight=0.15, propagation_factor=0.5, propagation_max_hops=2)

    zone = {c.chunk_id: c for c in store.get_working_zone(pipeline_id="pipe_1")}
    assert zone["grandparent"].base_trust > 0.6  # 2-hop credit reached it
    assert zone["parent"].base_trust > zone["grandparent"].base_trust  # closer hop, more credit
    assert zone["effect"].base_trust > zone["parent"].base_trust


def test_sqlite_calibrate_propagates_via_edge_row_when_scalar_absent(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "store.db")
    store.write(_chunk("cause_chunk", base_trust=0.6, content=f"root_cause_content {uuid4().hex}"))
    store.write(
        _chunk(
            "effect_chunk",
            base_trust=0.6,
            content=f"effect_frequently_retrieved_content {uuid4().hex}",
        )
    )
    # No caused_by scalar set; the relationship exists only as a typed edge
    # (e.g. added via the MCP ncp_write_memory 'edges' arg).
    store.add_chunk_edges(
        [ChunkEdge(src_chunk_id="effect_chunk", dst_chunk_id="cause_chunk", edge_type="caused_by")]
    )

    for _ in range(10):
        store.query("effect_frequently_retrieved_content", k=4, min_score=0.0, pipeline_id="pipe_1")

    store.calibrate(feedback_mode=True, feedback_weight=0.15, propagation_factor=0.5, propagation_max_hops=1)

    zone = {c.chunk_id: c for c in store.get_working_zone(pipeline_id="pipe_1")}
    assert zone["cause_chunk"].base_trust > 0.6
