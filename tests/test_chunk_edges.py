"""WI-G1: typed edge substrate (chunk_edges).

Covers:
- SQLiteStore.add_chunk_edges: upsert + (src, dst, edge_type) uniqueness.
- SQLiteStore.get_chunk_edges: direction ("out"/"in"/"both") and edge_type filter.
- Write-time backfill: writing a chunk with caused_by/supersedes set auto-upserts
  the matching chunk_edges rows (both single-id and JSON-list supersedes shapes).
- SQLiteStore.graph_data: node/edge shape, plus the legacy caused_by/supersedes
  column fallback for chunks that predate real edge rows (deduped).
- MCP ncp_write_memory 'edges' arg: happy path and unknown-type rejection.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from uuid import uuid4

import pytest

from ncp.mcp.server import _handle_request, make_handlers
from ncp.stores.sqlite import SQLiteStore
from ncp.types import ChunkEdge, SubconsciousChunk


def _req(method: str, params: object | None = None, req_id: int = 1) -> dict:
    payload: dict = {"jsonrpc": "2.0", "id": req_id, "method": method}
    if params is not None:
        payload["params"] = params
    return payload


def _call(name: str, arguments: dict | None = None, req_id: int = 1) -> dict:
    params: dict = {"name": name}
    if arguments is not None:
        params["arguments"] = arguments
    return _req("tools/call", params, req_id)


def _content(response_str: str) -> object:
    payload = json.loads(response_str)["result"]
    return json.loads(payload["content"][0]["text"])


def _error(response_str: str) -> dict:
    return json.loads(response_str).get("error", {})


def _chunk(chunk_id: str, **overrides: object) -> SubconsciousChunk:
    # Content embeds a random suffix so same-pipeline/layer/zone chunks in a
    # test never trip SQLiteStore._is_duplicate's near-duplicate-content
    # filter (SequenceMatcher ratio > 0.92) -- that filter is unrelated to
    # graph engineering and would otherwise silently drop test chunks whose
    # ids differ by only a character or two (e.g. "a" vs "b").
    kwargs: dict = {
        "chunk_id": chunk_id,
        "layer": "semantic",
        "content": f"chunk body {chunk_id} :: {uuid4().hex}",
        "src": "tool_result",
        "written_by": "tester",
    }
    kwargs.update(overrides)
    return SubconsciousChunk(**kwargs)


# ---------------------------------------------------------------------------
# add_chunk_edges: upsert + uniqueness
# ---------------------------------------------------------------------------


class TestAddChunkEdges:
    def test_add_chunk_edges_persists_rows(self, tmp_path: Path) -> None:
        store = SQLiteStore(tmp_path / "test.db")
        store.write(_chunk("a"))
        store.write(_chunk("b"))
        written = store.add_chunk_edges(
            [ChunkEdge(src_chunk_id="a", dst_chunk_id="b", edge_type="supports")]
        )
        assert written == 1
        edges = store.get_chunk_edges(["a"])
        assert len(edges) == 1
        assert edges[0].src_chunk_id == "a"
        assert edges[0].dst_chunk_id == "b"
        assert edges[0].edge_type == "supports"

    def test_add_chunk_edges_empty_list_is_noop(self, tmp_path: Path) -> None:
        store = SQLiteStore(tmp_path / "test.db")
        assert store.add_chunk_edges([]) == 0

    def test_add_chunk_edges_upserts_on_conflict(self, tmp_path: Path) -> None:
        store = SQLiteStore(tmp_path / "test.db")
        store.write(_chunk("a"))
        store.write(_chunk("b"))
        store.add_chunk_edges(
            [ChunkEdge(src_chunk_id="a", dst_chunk_id="b", edge_type="supports", weight=1.0)]
        )
        store.add_chunk_edges(
            [ChunkEdge(src_chunk_id="a", dst_chunk_id="b", edge_type="supports", weight=0.4)]
        )
        edges = store.get_chunk_edges(["a"])
        assert len(edges) == 1
        assert edges[0].weight == 0.4

    def test_different_edge_types_between_same_pair_coexist(self, tmp_path: Path) -> None:
        store = SQLiteStore(tmp_path / "test.db")
        store.write(_chunk("a"))
        store.write(_chunk("b"))
        store.add_chunk_edges(
            [
                ChunkEdge(src_chunk_id="a", dst_chunk_id="b", edge_type="supports"),
                ChunkEdge(src_chunk_id="a", dst_chunk_id="b", edge_type="contradicts"),
            ]
        )
        edges = store.get_chunk_edges(["a"])
        assert {e.edge_type for e in edges} == {"supports", "contradicts"}


# ---------------------------------------------------------------------------
# get_chunk_edges: direction + edge_type filter
# ---------------------------------------------------------------------------


class TestGetChunkEdges:
    def _seed(self, store: SQLiteStore) -> None:
        for cid in ("a", "b", "c"):
            store.write(_chunk(cid))
        store.add_chunk_edges(
            [
                ChunkEdge(src_chunk_id="a", dst_chunk_id="b", edge_type="supports"),
                ChunkEdge(src_chunk_id="c", dst_chunk_id="a", edge_type="refines"),
                ChunkEdge(src_chunk_id="a", dst_chunk_id="c", edge_type="derived_from"),
            ]
        )

    def test_direction_out_matches_src(self, tmp_path: Path) -> None:
        store = SQLiteStore(tmp_path / "test.db")
        self._seed(store)
        edges = store.get_chunk_edges(["a"], direction="out")
        assert {(e.src_chunk_id, e.dst_chunk_id) for e in edges} == {("a", "b"), ("a", "c")}

    def test_direction_in_matches_dst(self, tmp_path: Path) -> None:
        store = SQLiteStore(tmp_path / "test.db")
        self._seed(store)
        edges = store.get_chunk_edges(["a"], direction="in")
        assert {(e.src_chunk_id, e.dst_chunk_id) for e in edges} == {("c", "a")}

    def test_direction_both_matches_either_endpoint(self, tmp_path: Path) -> None:
        store = SQLiteStore(tmp_path / "test.db")
        self._seed(store)
        edges = store.get_chunk_edges(["a"], direction="both")
        assert len(edges) == 3

    def test_edge_type_filter(self, tmp_path: Path) -> None:
        store = SQLiteStore(tmp_path / "test.db")
        self._seed(store)
        edges = store.get_chunk_edges(["a"], direction="both", edge_types=["refines"])
        assert len(edges) == 1
        assert edges[0].edge_type == "refines"

    def test_unknown_direction_raises(self, tmp_path: Path) -> None:
        store = SQLiteStore(tmp_path / "test.db")
        self._seed(store)
        with pytest.raises(ValueError):
            store.get_chunk_edges(["a"], direction="sideways")

    def test_empty_chunk_ids_returns_empty(self, tmp_path: Path) -> None:
        store = SQLiteStore(tmp_path / "test.db")
        self._seed(store)
        assert store.get_chunk_edges([]) == []

    def test_limit_is_respected(self, tmp_path: Path) -> None:
        store = SQLiteStore(tmp_path / "test.db")
        self._seed(store)
        edges = store.get_chunk_edges(["a"], direction="both", limit=1)
        assert len(edges) == 1


# ---------------------------------------------------------------------------
# Write-time backfill from caused_by / supersedes
# ---------------------------------------------------------------------------


class TestWriteTimeBackfill:
    def test_caused_by_backfills_edge(self, tmp_path: Path) -> None:
        store = SQLiteStore(tmp_path / "test.db")
        store.write(_chunk("parent"))
        store.write(_chunk("child", caused_by="parent"))
        edges = store.get_chunk_edges(["child"], direction="out")
        assert len(edges) == 1
        assert edges[0].dst_chunk_id == "parent"
        assert edges[0].edge_type == "caused_by"

    def test_supersedes_single_id_backfills_edge(self, tmp_path: Path) -> None:
        store = SQLiteStore(tmp_path / "test.db")
        store.write(_chunk("old"))
        store.write(_chunk("new", supersedes="old"))
        edges = store.get_chunk_edges(["new"], direction="out")
        assert len(edges) == 1
        assert edges[0].dst_chunk_id == "old"
        assert edges[0].edge_type == "supersedes"

    def test_supersedes_json_list_backfills_multiple_edges(self, tmp_path: Path) -> None:
        store = SQLiteStore(tmp_path / "test.db")
        store.write(_chunk("loser_1"))
        store.write(_chunk("loser_2"))
        store.write(_chunk("keeper", supersedes=json.dumps(["loser_1", "loser_2"])))
        edges = store.get_chunk_edges(["keeper"], direction="out", edge_types=["supersedes"])
        assert {e.dst_chunk_id for e in edges} == {"loser_1", "loser_2"}

    def test_rewrite_backfills_new_edge_without_retracting_stale_one(self, tmp_path: Path) -> None:
        """Backfill is additive: it upserts the edge implied by the *current*
        write, but a prior write's edge to a now-stale target is not deleted
        (there's no way to know an old scalar value to retract it by -- only
        the current chunk row is available at write time)."""
        store = SQLiteStore(tmp_path / "test.db")
        store.write(_chunk("parent"))
        store.write(_chunk("other_parent"))
        store.write(_chunk("child", caused_by="parent"))
        store.write(_chunk("child", caused_by="other_parent"))
        edges = store.get_chunk_edges(["child"], direction="out", edge_types=["caused_by"])
        assert {e.dst_chunk_id for e in edges} == {"parent", "other_parent"}

    def test_no_caused_by_or_supersedes_writes_no_edges(self, tmp_path: Path) -> None:
        store = SQLiteStore(tmp_path / "test.db")
        store.write(_chunk("solo"))
        assert store.get_chunk_edges(["solo"], direction="both") == []


# ---------------------------------------------------------------------------
# graph_data: shape + legacy-column fallback
# ---------------------------------------------------------------------------


class TestGraphData:
    def test_shape_has_nodes_and_edges(self, tmp_path: Path) -> None:
        store = SQLiteStore(tmp_path / "test.db")
        store.write(_chunk("a", pipeline_id="pipe_1", content="alpha node content"))
        store.write(_chunk("b", pipeline_id="pipe_1", caused_by="a", content="bravo node content, a different sentence"))
        data = store.graph_data(pipeline_id="pipe_1")
        assert set(data.keys()) == {"nodes", "edges"}
        node_ids = {n["chunk_id"] for n in data["nodes"]}
        assert node_ids == {"a", "b"}
        node = next(n for n in data["nodes"] if n["chunk_id"] == "a")
        assert set(node.keys()) == {"chunk_id", "layer", "base_trust", "src", "written_by", "summary"}
        assert node["summary"] == "alpha node content"
        edge = next(e for e in data["edges"] if e["src"] == "b")
        assert edge == {"src": "b", "dst": "a", "type": "caused_by", "weight": 1.0}

    def test_pipeline_id_scopes_nodes(self, tmp_path: Path) -> None:
        store = SQLiteStore(tmp_path / "test.db")
        store.write(_chunk("a", pipeline_id="pipe_1"))
        store.write(_chunk("b", pipeline_id="pipe_2"))
        data = store.graph_data(pipeline_id="pipe_1")
        assert {n["chunk_id"] for n in data["nodes"]} == {"a"}

    def test_no_pipeline_id_returns_all_nodes(self, tmp_path: Path) -> None:
        store = SQLiteStore(tmp_path / "test.db")
        store.write(_chunk("a", pipeline_id="pipe_1"))
        store.write(_chunk("b", pipeline_id="pipe_2"))
        data = store.graph_data()
        assert {n["chunk_id"] for n in data["nodes"]} == {"a", "b"}

    def test_summary_truncated_to_80_chars(self, tmp_path: Path) -> None:
        store = SQLiteStore(tmp_path / "test.db")
        long_content = "x" * 200
        store.write(_chunk("a", content=long_content))
        data = store.graph_data()
        node = next(n for n in data["nodes"] if n["chunk_id"] == "a")
        assert node["summary"] == long_content[:80]
        assert len(node["summary"]) == 80

    def test_legacy_caused_by_fallback_when_edge_row_missing(self, tmp_path: Path) -> None:
        store = SQLiteStore(tmp_path / "test.db")
        store.write(_chunk("parent"))
        store.write(_chunk("child", caused_by="parent"))
        # Simulate data written before the edge table existed: strip the
        # backfilled row so only the legacy chunks.caused_by column remains.
        with sqlite3.connect(store.path) as conn:
            conn.execute("DELETE FROM chunk_edges")
        data = store.graph_data()
        assert {"src": "child", "dst": "parent", "type": "caused_by", "weight": 1.0} in data["edges"]

    def test_real_edge_row_dedupes_against_legacy_column(self, tmp_path: Path) -> None:
        store = SQLiteStore(tmp_path / "test.db")
        store.write(_chunk("parent"))
        store.write(_chunk("child", caused_by="parent"))
        data = store.graph_data()
        caused_by_edges = [
            e for e in data["edges"] if e["src"] == "child" and e["dst"] == "parent" and e["type"] == "caused_by"
        ]
        assert len(caused_by_edges) == 1

    def test_legacy_supersedes_json_list_fallback(self, tmp_path: Path) -> None:
        store = SQLiteStore(tmp_path / "test.db")
        store.write(_chunk("loser_1"))
        store.write(_chunk("loser_2"))
        store.write(_chunk("keeper", supersedes=json.dumps(["loser_1", "loser_2"])))
        with sqlite3.connect(store.path) as conn:
            conn.execute("DELETE FROM chunk_edges")
        data = store.graph_data()
        supersedes_targets = {e["dst"] for e in data["edges"] if e["src"] == "keeper" and e["type"] == "supersedes"}
        assert supersedes_targets == {"loser_1", "loser_2"}


# ---------------------------------------------------------------------------
# MCP ncp_write_memory 'edges' argument
# ---------------------------------------------------------------------------


class TestMcpWriteMemoryEdges:
    def test_edges_happy_path(self, tmp_path: Path) -> None:
        store = SQLiteStore(tmp_path / "test.db")
        handlers = make_handlers(store)
        store.write(_chunk("existing_dst"))

        resp = _handle_request(
            _call("ncp_write_memory", {
                "content": "new chunk with an edge",
                "layer": "semantic",
                "src": "tool_result",
                "written_by": "executor",
                "chunk_id": "new_src",
                "edges": [
                    {"dst": "existing_dst", "type": "supports", "weight": 0.9},
                ],
            }),
            handlers,
        )
        result = _content(resp)
        assert result["written"] is True
        assert result["chunk_id"] == "new_src"
        assert result["edges_written"] == 1

        edges = store.get_chunk_edges(["new_src"], direction="out")
        assert len(edges) == 1
        assert edges[0].dst_chunk_id == "existing_dst"
        assert edges[0].edge_type == "supports"
        assert edges[0].weight == 0.9
        assert edges[0].created_by == "executor"

    def test_edges_default_weight(self, tmp_path: Path) -> None:
        store = SQLiteStore(tmp_path / "test.db")
        handlers = make_handlers(store)
        store.write(_chunk("dst"))

        _handle_request(
            _call("ncp_write_memory", {
                "content": "chunk",
                "layer": "semantic",
                "src": "tool_result",
                "chunk_id": "src",
                "edges": [{"dst": "dst", "type": "refines"}],
            }),
            handlers,
        )
        edges = store.get_chunk_edges(["src"], direction="out")
        assert edges[0].weight == 1.0

    def test_unknown_edge_type_is_rejected(self, tmp_path: Path) -> None:
        store = SQLiteStore(tmp_path / "test.db")
        handlers = make_handlers(store)
        store.write(_chunk("dst"))

        resp = _handle_request(
            _call("ncp_write_memory", {
                "content": "chunk with bad edge",
                "layer": "semantic",
                "src": "tool_result",
                "chunk_id": "src",
                "edges": [{"dst": "dst", "type": "not_a_real_type"}],
            }),
            handlers,
        )
        error = _error(resp)
        assert error["code"] == -32603
        assert "not_a_real_type" in error["message"]
        # Rejected before persistence: the chunk write must not have happened.
        assert store.status()["chunk_count"] == 1  # only the pre-seeded "dst" chunk

    def test_write_memory_without_edges_arg_still_works(self, tmp_path: Path) -> None:
        store = SQLiteStore(tmp_path / "test.db")
        handlers = make_handlers(store)
        resp = _handle_request(
            _call("ncp_write_memory", {
                "content": "no edges here",
                "layer": "semantic",
                "src": "tool_result",
            }),
            handlers,
        )
        result = _content(resp)
        assert result["written"] is True
        assert "edges_written" not in result
