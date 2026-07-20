"""CAP-C7/WI-P2: automatic write-time edge inference.

Covers:
- [graph] config block: defaults, TOML file + NCP_* env overrides.
- ncp.stores.graph.infer_edges_for_chunk (pure helper): self/raw-chunk/empty-
  content exclusion, already-existing-edge exclusion, threshold, weight ==
  ratio, created_by marker, deterministic max_edges cap.
- SQLiteStore.write() wiring: disabled by default (zero behavior change),
  enabled infers `refines` edges after backfill, threshold respected,
  infer_max_edges cap, no duplicate edge on rewrite.
- MCP ncp_write_memory: `edges_inferred` field absent when disabled, present
  (0 allowed) when enabled.
"""

from __future__ import annotations

from difflib import SequenceMatcher
import json
from pathlib import Path

import pytest

from ncp.config import NCPConfig, load_config
from ncp.mcp.server import _handle_request, make_handlers
from ncp.stores.graph import infer_edges_for_chunk
from ncp.stores.sqlite import SQLiteStore
from ncp.types import ChunkEdge, SubconsciousChunk

# Four variants share enough structure with BASE to clear the default 0.6
# similarity threshold while staying under SQLiteStore._is_duplicate's 0.92
# near-duplicate cutoff (which would otherwise silently drop the write
# before inference ever runs -- see phase-1 bus gotcha). SIM_LOW is
# deliberately unrelated so it never clears the threshold.
BASE = (
    "The nightly build pipeline failed at the deploy step because the "
    "config file was missing required fields for the staging environment"
)
SIM_1 = (
    "The nightly build pipeline failed at the deploy step because the "
    "config file lacked required fields for staging"
)
SIM_2 = (
    "The nightly build pipeline crashed at the deploy step since the "
    "config file was missing required staging settings"
)
SIM_3 = (
    "Build pipeline failed during deploy because config file missing "
    "required fields staging environment setup"
)
SIM_4 = (
    "The nightly deploy pipeline failed at the build step because the "
    "settings file was missing optional flags for prod"
)
SIM_LOW = (
    "Completely unrelated content discussing quantum entanglement and "
    "particle spin correlations across distant labs"
)


def _ratio(a: str, b: str) -> float:
    return SequenceMatcher(None, a, b).ratio()


def _chunk(chunk_id: str, content: str, **overrides: object) -> SubconsciousChunk:
    kwargs: dict = {
        "chunk_id": chunk_id,
        "layer": "semantic",
        "content": content,
        "src": "tool_result",
        "written_by": "tester",
        "pipeline_id": "pipe_1",
    }
    kwargs.update(overrides)
    return SubconsciousChunk(**kwargs)


def _graph_config(**graph_overrides: object) -> NCPConfig:
    return NCPConfig(values={"graph": dict(graph_overrides)}, project_root=Path("."))


# ---------------------------------------------------------------------------
# [graph] config block
# ---------------------------------------------------------------------------


class TestGraphConfig:
    def test_defaults(self, tmp_path: Path) -> None:
        config = load_config(cwd=tmp_path, env={})
        assert config.infer_edges is False
        assert config.infer_similarity_threshold == 0.6
        assert config.infer_scan_limit == 50
        assert config.infer_max_edges == 3

    def test_file_and_env_overrides(self, tmp_path: Path) -> None:
        project = tmp_path / "repo"
        (project / ".git").mkdir(parents=True)
        (project / ".ncp").mkdir()
        (project / ".ncp" / "config.toml").write_text(
            "[graph]\ninfer_edges = true\n"
            "infer_similarity_threshold = 0.75\n"
            "infer_scan_limit = 20\n"
            "infer_max_edges = 5\n"
        )

        config = load_config(cwd=project)
        assert config.infer_edges is True
        assert config.infer_similarity_threshold == 0.75
        assert config.infer_scan_limit == 20
        assert config.infer_max_edges == 5

        config = load_config(
            cwd=project,
            env={
                "NCP_INFER_EDGES": "false",
                "NCP_INFER_SIMILARITY_THRESHOLD": "0.5",
                "NCP_INFER_SCAN_LIMIT": "10",
                "NCP_INFER_MAX_EDGES": "1",
            },
        )
        assert config.infer_edges is False
        assert config.infer_similarity_threshold == 0.5
        assert config.infer_scan_limit == 10
        assert config.infer_max_edges == 1

    def test_env_override_enables_from_default_off(self, tmp_path: Path) -> None:
        config = load_config(cwd=tmp_path, env={"NCP_INFER_EDGES": "true"})
        assert config.infer_edges is True


# ---------------------------------------------------------------------------
# infer_edges_for_chunk: pure helper
# ---------------------------------------------------------------------------


class TestInferEdgesForChunkHelper:
    def test_excludes_self(self) -> None:
        chunk = _chunk("new", BASE)
        edges = infer_edges_for_chunk(chunk, [chunk], threshold=0.0, max_edges=5)
        assert edges == []

    def test_excludes_raw_backup_chunks(self) -> None:
        chunk = _chunk("new", BASE)
        raw_candidate = _chunk("raw_new_123", BASE)
        edges = infer_edges_for_chunk(chunk, [raw_candidate], threshold=0.0, max_edges=5)
        assert edges == []

    def test_excludes_empty_content_candidates(self) -> None:
        chunk = _chunk("new", BASE)
        empty_candidate = SubconsciousChunk(
            chunk_id="empty", layer="semantic", content="", src="tool_result"
        )
        edges = infer_edges_for_chunk(chunk, [empty_candidate], threshold=0.0, max_edges=5)
        assert edges == []

    def test_empty_new_chunk_content_yields_no_edges(self) -> None:
        chunk = SubconsciousChunk(chunk_id="new", layer="semantic", content="", src="tool_result")
        candidate = _chunk("old", BASE)
        edges = infer_edges_for_chunk(chunk, [candidate], threshold=0.0, max_edges=5)
        assert edges == []

    def test_excludes_existing_dst_ids(self) -> None:
        chunk = _chunk("new", BASE)
        candidate = _chunk("old", SIM_1)
        edges = infer_edges_for_chunk(
            chunk, [candidate], threshold=0.6, max_edges=5, existing_dst_ids={"old"}
        )
        assert edges == []

    def test_threshold_respected(self) -> None:
        chunk = _chunk("new", BASE)
        low = _chunk("low", SIM_LOW)
        ratio = _ratio(BASE, SIM_LOW)
        assert ratio < 0.6
        edges = infer_edges_for_chunk(chunk, [low], threshold=0.6, max_edges=5)
        assert edges == []

    def test_weight_equals_ratio_and_edge_shape(self) -> None:
        chunk = _chunk("new", BASE)
        candidate = _chunk("old", SIM_1)
        expected_ratio = _ratio(BASE, SIM_1)
        edges = infer_edges_for_chunk(chunk, [candidate], threshold=0.6, max_edges=5)
        assert len(edges) == 1
        edge = edges[0]
        assert edge.src_chunk_id == "new"
        assert edge.dst_chunk_id == "old"
        assert edge.edge_type == "refines"
        assert edge.weight == pytest.approx(expected_ratio)
        assert edge.created_by == "ncp:inferred"

    def test_max_edges_cap_keeps_highest_ratio(self) -> None:
        chunk = _chunk("new", BASE)
        candidates = [
            _chunk("s1", SIM_1),
            _chunk("s2", SIM_2),
            _chunk("s3", SIM_3),
            _chunk("s4", SIM_4),
        ]
        ratios = {c.chunk_id: _ratio(BASE, c.content) for c in candidates}
        # Sanity: all four clear the threshold, s4 is the weakest match.
        assert all(r >= 0.6 for r in ratios.values())
        assert ratios["s4"] == min(ratios.values())

        edges = infer_edges_for_chunk(chunk, candidates, threshold=0.6, max_edges=3)
        assert len(edges) == 3
        assert {e.dst_chunk_id for e in edges} == {"s1", "s2", "s3"}

    def test_max_edges_zero_yields_no_edges(self) -> None:
        chunk = _chunk("new", BASE)
        candidate = _chunk("old", SIM_1)
        edges = infer_edges_for_chunk(chunk, [candidate], threshold=0.6, max_edges=0)
        assert edges == []

    def test_deterministic_tie_break_on_dst_id(self) -> None:
        chunk = _chunk("new", BASE)
        # Same content on two different ids -> identical ratio; the cap must
        # pick deterministically rather than depending on input order.
        candidates = [_chunk("zzz", SIM_1), _chunk("aaa", SIM_1)]
        edges = infer_edges_for_chunk(chunk, candidates, threshold=0.6, max_edges=1)
        assert len(edges) == 1
        assert edges[0].dst_chunk_id == "aaa"


# ---------------------------------------------------------------------------
# SQLiteStore.write() wiring
# ---------------------------------------------------------------------------


class TestSQLiteWriteInference:
    def test_disabled_by_default_no_inference(self, tmp_path: Path) -> None:
        store = SQLiteStore(tmp_path / "store.db")
        store.write(_chunk("old", SIM_1))
        store.write(_chunk("new", BASE))
        assert store.last_write_inferred_edge_count == 0
        assert store.get_chunk_edges(["new"], edge_types=["refines"]) == []

    def test_enabled_infers_refines_edge_with_weight_equal_ratio(self, tmp_path: Path) -> None:
        config = _graph_config(infer_edges=True)
        store = SQLiteStore(tmp_path / "store.db", config=config)
        store.write(_chunk("old", SIM_1))
        store.write(_chunk("new", BASE))

        assert store.last_write_inferred_edge_count == 1
        edges = store.get_chunk_edges(["new"], edge_types=["refines"])
        assert len(edges) == 1
        edge = edges[0]
        assert edge.src_chunk_id == "new"
        assert edge.dst_chunk_id == "old"
        assert edge.weight == pytest.approx(_ratio(BASE, SIM_1))
        assert edge.created_by == "ncp:inferred"

    def test_threshold_respected_dissimilar_content_no_edge(self, tmp_path: Path) -> None:
        config = _graph_config(infer_edges=True)
        store = SQLiteStore(tmp_path / "store.db", config=config)
        store.write(_chunk("low", SIM_LOW))
        store.write(_chunk("new", BASE))

        assert store.last_write_inferred_edge_count == 0
        assert store.get_chunk_edges(["new"], edge_types=["refines"]) == []

    def test_infer_max_edges_cap(self, tmp_path: Path) -> None:
        config = _graph_config(infer_edges=True, infer_max_edges=2)
        store = SQLiteStore(tmp_path / "store.db", config=config)
        store.write(_chunk("s1", SIM_1))
        store.write(_chunk("s2", SIM_2))
        store.write(_chunk("s3", SIM_3))
        store.write(_chunk("s4", SIM_4))
        store.write(_chunk("new", BASE))

        assert store.last_write_inferred_edge_count == 2
        edges = store.get_chunk_edges(["new"], edge_types=["refines"])
        assert len(edges) == 2
        ratios = {c: _ratio(BASE, content) for c, content in (("s1", SIM_1), ("s2", SIM_2), ("s3", SIM_3), ("s4", SIM_4))}
        expected = set(sorted(ratios, key=lambda k: -ratios[k])[:2])
        assert {e.dst_chunk_id for e in edges} == expected

    def test_no_duplicate_edge_when_one_already_exists(self, tmp_path: Path) -> None:
        config = _graph_config(infer_edges=True)
        store = SQLiteStore(tmp_path / "store.db", config=config)
        store.write(_chunk("old", SIM_1))
        store.write(_chunk("new", BASE))
        assert store.last_write_inferred_edge_count == 1

        # Rewriting the same chunk_id with identical content re-runs
        # inference; the (src, dst, "refines") pair already exists so it
        # must not be duplicated or double-counted.
        store.write(_chunk("new", BASE))
        assert store.last_write_inferred_edge_count == 0
        edges = store.get_chunk_edges(["new"], edge_types=["refines"])
        assert len(edges) == 1

    def test_infer_scan_limit_is_configurable(self, tmp_path: Path) -> None:
        config = _graph_config(infer_edges=True, infer_scan_limit=1)
        store = SQLiteStore(tmp_path / "store.db", config=config)
        assert store.config is not None
        assert store.config.infer_scan_limit == 1

    def test_env_override_enables_inference(self, tmp_path: Path) -> None:
        config = load_config(cwd=tmp_path, env={"NCP_INFER_EDGES": "true"})
        store = SQLiteStore(tmp_path / "store.db", config=config)
        store.write(_chunk("old", SIM_1))
        store.write(_chunk("new", BASE))
        assert store.last_write_inferred_edge_count == 1


# ---------------------------------------------------------------------------
# MCP ncp_write_memory: edges_inferred field
# ---------------------------------------------------------------------------


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


def _content(response_str: str) -> dict:
    payload = json.loads(response_str)["result"]
    return json.loads(payload["content"][0]["text"])


class TestMcpWriteMemoryEdgesInferred:
    def test_field_absent_when_disabled(self, tmp_path: Path) -> None:
        config = load_config(cwd=tmp_path, env={"NCP_STORE_PATH": str(tmp_path / "store.db")})
        store = SQLiteStore(config.store_path, config=config)
        handlers = make_handlers(store, config=config)

        resp = _handle_request(
            _call(
                "ncp_write_memory",
                {"content": BASE, "layer": "semantic", "src": "tool_result", "chunk_id": "new"},
            ),
            handlers,
        )
        result = _content(resp)
        assert "edges_inferred" not in result

    def test_field_present_zero_when_no_match(self, tmp_path: Path) -> None:
        config = load_config(
            cwd=tmp_path,
            env={"NCP_STORE_PATH": str(tmp_path / "store.db"), "NCP_INFER_EDGES": "true"},
        )
        store = SQLiteStore(config.store_path, config=config)
        handlers = make_handlers(store, config=config)

        resp = _handle_request(
            _call(
                "ncp_write_memory",
                {"content": BASE, "layer": "semantic", "src": "tool_result", "chunk_id": "new"},
            ),
            handlers,
        )
        result = _content(resp)
        assert result["edges_inferred"] == 0

    def test_field_present_nonzero_when_similar_chunk_exists(self, tmp_path: Path) -> None:
        config = load_config(
            cwd=tmp_path,
            env={"NCP_STORE_PATH": str(tmp_path / "store.db"), "NCP_INFER_EDGES": "true"},
        )
        store = SQLiteStore(config.store_path, config=config)
        handlers = make_handlers(store, config=config)

        _handle_request(
            _call(
                "ncp_write_memory",
                {"content": SIM_1, "layer": "semantic", "src": "tool_result", "chunk_id": "old"},
            ),
            handlers,
        )
        resp = _handle_request(
            _call(
                "ncp_write_memory",
                {"content": BASE, "layer": "semantic", "src": "tool_result", "chunk_id": "new"},
            ),
            handlers,
        )
        result = _content(resp)
        assert result["edges_inferred"] == 1
        edges = store.get_chunk_edges(["new"], edge_types=["refines"])
        assert len(edges) == 1
        assert edges[0].dst_chunk_id == "old"
