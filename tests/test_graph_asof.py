"""WI-P4: temporal graph export -- graph_data(as_of=...) and `ncp graph --as-of`.

Covers:
- as_of=None keeps exact shape parity with a plain graph_data() call.
- A chunk recorded after T is excluded from the as_of=T view.
- An edge row created after T is excluded even when both endpoints predate T.
- A chunk superseded at T' > T is still visible at as_of=T (the supersession
  had not happened yet at T).
- CLI --as-of accepts both epoch and ISO-8601 forms; invalid values produce
  a clean error, not a traceback.

Note: SQLiteStore.write() stamps created_at with the actual transaction time
(bi-temporal transaction time cannot be backdated), so these tests capture T
*between* real writes rather than constructing chunks with fake timestamps.
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from click.testing import CliRunner

from ncp.cli import main
from ncp.stores.sqlite import SQLiteStore
from ncp.types import ChunkEdge, SubconsciousChunk


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


def _tick() -> float:
    """Capture a timestamp strictly after prior writes and before later ones."""
    time.sleep(0.005)
    stamp = time.time()
    time.sleep(0.005)
    return stamp


class TestGraphDataAsOf:
    def test_none_matches_plain_call(self, tmp_path: Path) -> None:
        store = _init_store(tmp_path)
        store.write(_chunk("parent"))
        store.write(_chunk("child", caused_by="parent"))

        assert store.graph_data(as_of=None) == store.graph_data()

    def test_chunk_written_after_t_excluded(self, tmp_path: Path) -> None:
        store = _init_store(tmp_path)
        store.write(_chunk("old"))
        t = _tick()
        store.write(_chunk("new"))

        data = store.graph_data(as_of=t)

        node_ids = {n["chunk_id"] for n in data["nodes"]}
        assert node_ids == {"old"}

    def test_edge_created_after_t_excluded(self, tmp_path: Path) -> None:
        store = _init_store(tmp_path)
        store.write(_chunk("a"))
        store.write(_chunk("b"))
        t = _tick()
        store.add_chunk_edges(
            [ChunkEdge(src_chunk_id="a", dst_chunk_id="b", edge_type="supports")]
        )

        current = store.graph_data()
        asof = store.graph_data(as_of=t)

        assert any(e["type"] == "supports" for e in current["edges"])
        assert not any(e["type"] == "supports" for e in asof["edges"])
        assert {n["chunk_id"] for n in asof["nodes"]} == {"a", "b"}

    def test_chunk_superseded_after_t_still_visible_at_t(self, tmp_path: Path) -> None:
        store = _init_store(tmp_path)
        store.write(_chunk("v1"))
        t = _tick()
        store.write(_chunk("v2"))
        assert store.supersede("v1", "v2")

        asof_nodes = {n["chunk_id"] for n in store.graph_data(as_of=t)["nodes"]}
        current_nodes = {n["chunk_id"] for n in store.graph_data()["nodes"]}

        # At T the supersession had not happened: v1 visible, v2 not yet recorded.
        assert asof_nodes == {"v1"}
        # Default export stays the full graph (tombstones excluded only):
        # superseded nodes remain visible so history can be rendered.
        assert current_nodes == {"v1", "v2"}


class TestGraphCliAsOf:
    def test_epoch_form(self, tmp_path: Path) -> None:
        store = _init_store(tmp_path)
        store.write(_chunk("early"))
        t = _tick()
        store.write(_chunk("late"))

        result = CliRunner().invoke(main, ["graph", "--cwd", str(tmp_path), "--as-of", str(t)])

        assert result.exit_code == 0
        payload = json.loads(result.stdout)
        assert {n["chunk_id"] for n in payload["nodes"]} == {"early"}

    def test_iso_form(self, tmp_path: Path) -> None:
        store = _init_store(tmp_path)
        store.write(_chunk("early"))
        t = _tick()
        store.write(_chunk("late"))

        iso = datetime.fromtimestamp(t, tz=timezone.utc).isoformat()
        result = CliRunner().invoke(main, ["graph", "--cwd", str(tmp_path), "--as-of", iso])

        assert result.exit_code == 0
        payload = json.loads(result.stdout)
        assert {n["chunk_id"] for n in payload["nodes"]} == {"early"}

    def test_invalid_value_clean_error(self, tmp_path: Path) -> None:
        _init_store(tmp_path)

        result = CliRunner().invoke(main, ["graph", "--cwd", str(tmp_path), "--as-of", "not-a-time"])

        assert result.exit_code != 0
        assert "invalid --as-of" in result.output
        assert "Traceback" not in result.output
