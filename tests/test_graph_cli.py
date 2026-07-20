"""WI-G4: `ncp graph` CLI command.

Covers:
- JSON export to stdout and to --output file, with correct nodes/edges/stats shape.
- DOT export is well-formed Graphviz (starts with digraph, balanced braces,
  contains the expected edge line).
- --pipeline-id scoping.
- Stale caused_by edge rows (left behind by an additive-only rewrite, see
  WI-G1/WI-G4 gotchas) are dropped from the export in favor of the chunk's
  current caused_by scalar.
- Empty store produces valid empty JSON/DOT output.
"""

from __future__ import annotations

import json
from pathlib import Path
from uuid import uuid4

from click.testing import CliRunner

from ncp.cli import main
from ncp.stores.sqlite import SQLiteStore
from ncp.types import SubconsciousChunk


def _chunk(chunk_id: str, **overrides: object) -> SubconsciousChunk:
    # Random content suffix avoids SQLiteStore._is_duplicate's near-duplicate
    # filter tripping across same-zone/layer/pipeline test fixtures.
    kwargs: dict = {
        "chunk_id": chunk_id,
        "layer": "semantic",
        "content": f"chunk body {chunk_id} :: {uuid4().hex}",
        "src": "tool_result",
        "written_by": "tester",
    }
    kwargs.update(overrides)
    return SubconsciousChunk(**kwargs)


def _init_store(tmp_path: Path) -> SQLiteStore:
    CliRunner().invoke(main, ["init", "--cwd", str(tmp_path)])
    return SQLiteStore(tmp_path / ".ncp" / "store.db")


def _count_braces(text: str) -> None:
    assert text.count("{") == text.count("}")


class TestGraphJsonExport:
    def test_json_to_stdout_has_expected_shape(self, tmp_path: Path) -> None:
        store = _init_store(tmp_path)
        store.write(_chunk("parent"))
        store.write(_chunk("child", caused_by="parent"))

        result = CliRunner().invoke(main, ["graph", "--cwd", str(tmp_path)])

        assert result.exit_code == 0
        payload = json.loads(result.stdout)
        assert set(payload.keys()) == {"nodes", "edges", "stats"}
        node_ids = {n["chunk_id"] for n in payload["nodes"]}
        assert node_ids == {"parent", "child"}
        assert {"src": "child", "dst": "parent", "type": "caused_by", "weight": 1.0} in payload["edges"]
        assert payload["stats"]["node_count"] == 2
        assert payload["stats"]["edge_count"] == 1
        assert payload["stats"]["edges_by_type"] == {"caused_by": 1}

    def test_summary_written_to_stderr_not_stdout(self, tmp_path: Path) -> None:
        store = _init_store(tmp_path)
        store.write(_chunk("a"))

        result = CliRunner().invoke(main, ["graph", "--cwd", str(tmp_path)])

        assert result.exit_code == 0
        # stdout must be pure JSON -- the human summary lives on stderr.
        json.loads(result.stdout)
        assert "Graph Summary" in result.stderr or "NCP Graph" in result.stderr

    def test_json_to_output_file(self, tmp_path: Path) -> None:
        store = _init_store(tmp_path)
        store.write(_chunk("a"))
        store.write(_chunk("b", caused_by="a"))
        out_file = tmp_path / "graph.json"

        result = CliRunner().invoke(
            main, ["graph", "--cwd", str(tmp_path), "--output", str(out_file)]
        )

        assert result.exit_code == 0
        payload = json.loads(out_file.read_text())
        assert payload["stats"]["node_count"] == 2
        assert payload["stats"]["edge_count"] == 1
        # Writing to a file must not also dump the JSON to stdout.
        assert result.stdout.strip() == ""

    def test_pipeline_id_scopes_nodes(self, tmp_path: Path) -> None:
        store = _init_store(tmp_path)
        store.write(_chunk("a", pipeline_id="pipe1"))
        store.write(_chunk("b", pipeline_id="pipe2"))

        result = CliRunner().invoke(
            main, ["graph", "--cwd", str(tmp_path), "--pipeline-id", "pipe1"]
        )

        assert result.exit_code == 0
        payload = json.loads(result.stdout)
        assert {n["chunk_id"] for n in payload["nodes"]} == {"a"}
        assert payload["stats"]["node_count"] == 1

    def test_empty_store_produces_valid_empty_json(self, tmp_path: Path) -> None:
        _init_store(tmp_path)

        result = CliRunner().invoke(main, ["graph", "--cwd", str(tmp_path)])

        assert result.exit_code == 0
        payload = json.loads(result.stdout)
        assert payload == {
            "nodes": [],
            "edges": [],
            "stats": {"node_count": 0, "edge_count": 0, "edges_by_type": {}},
        }

    def test_stale_caused_by_edge_dropped_after_rewrite(self, tmp_path: Path) -> None:
        store = _init_store(tmp_path)
        store.write(_chunk("parent1"))
        store.write(_chunk("parent2"))
        store.write(_chunk("child", caused_by="parent1"))
        # Additive-only backfill (WI-G1): rewriting "child" to a new parent
        # adds a fresh chunk_edges row but leaves the stale one pointing at
        # parent1 behind.
        store.write(_chunk("child", caused_by="parent2"))

        # Confirm the stale row really is present in the raw store first.
        raw = store.graph_data()
        raw_caused_by = [e for e in raw["edges"] if e["src"] == "child" and e["type"] == "caused_by"]
        assert {e["dst"] for e in raw_caused_by} == {"parent1", "parent2"}

        result = CliRunner().invoke(main, ["graph", "--cwd", str(tmp_path)])

        assert result.exit_code == 0
        payload = json.loads(result.stdout)
        caused_by_edges = [
            e for e in payload["edges"] if e["src"] == "child" and e["type"] == "caused_by"
        ]
        assert caused_by_edges == [{"src": "child", "dst": "parent2", "type": "caused_by", "weight": 1.0}]


class TestGraphDotExport:
    def test_dot_is_well_formed_and_contains_fixture_edge(self, tmp_path: Path) -> None:
        store = _init_store(tmp_path)
        store.write(_chunk("parent"))
        store.write(_chunk("child", caused_by="parent"))

        result = CliRunner().invoke(
            main, ["graph", "--cwd", str(tmp_path), "--format", "dot"]
        )

        assert result.exit_code == 0
        text = result.stdout
        assert text.startswith("digraph")
        _count_braces(text)
        assert '"child" -> "parent"' in text
        assert 'label="caused_by"' in text
        assert 'style=solid' in text

    def test_dot_escapes_quotes_in_ids(self, tmp_path: Path) -> None:
        store = _init_store(tmp_path)
        store.write(_chunk('weird"id'))

        result = CliRunner().invoke(
            main, ["graph", "--cwd", str(tmp_path), "--format", "dot"]
        )

        assert result.exit_code == 0
        text = result.stdout
        _count_braces(text)
        assert '\\"id' in text

    def test_dot_stale_edge_excluded(self, tmp_path: Path) -> None:
        store = _init_store(tmp_path)
        store.write(_chunk("parent1"))
        store.write(_chunk("parent2"))
        store.write(_chunk("child", caused_by="parent1"))
        store.write(_chunk("child", caused_by="parent2"))

        result = CliRunner().invoke(
            main, ["graph", "--cwd", str(tmp_path), "--format", "dot"]
        )

        assert result.exit_code == 0
        text = result.stdout
        _count_braces(text)
        assert '"child" -> "parent2"' in text
        assert '"child" -> "parent1"' not in text

    def test_empty_store_produces_valid_empty_dot(self, tmp_path: Path) -> None:
        _init_store(tmp_path)

        result = CliRunner().invoke(
            main, ["graph", "--cwd", str(tmp_path), "--format", "dot"]
        )

        assert result.exit_code == 0
        text = result.stdout
        assert text.startswith("digraph")
        _count_braces(text)


class TestGraphLimit:
    def test_limit_option_bounds_nodes(self, tmp_path: Path) -> None:
        store = _init_store(tmp_path)
        for i in range(5):
            store.write(_chunk(f"c{i}"))

        result = CliRunner().invoke(
            main, ["graph", "--cwd", str(tmp_path), "--limit", "2"]
        )

        assert result.exit_code == 0
        payload = json.loads(result.stdout)
        assert payload["stats"]["node_count"] == 2
