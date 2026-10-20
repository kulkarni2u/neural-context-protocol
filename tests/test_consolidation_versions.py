"""Consolidation must respect version chains (superseded chunks are historical)."""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from ncp.stores.sqlite import SQLiteStore
from ncp.types import SubconsciousChunk

CONTENT = "auth token refresh logic handles expiry and rotation for sessions"


def _chunk(cid: str, trust: float, content: str = CONTENT) -> SubconsciousChunk:
    return SubconsciousChunk(
        chunk_id=cid, content=content, layer="semantic", src="tool_result",
        written_by="a", base_trust=trust, pipeline_id="p", zone="working",
    )


def _store(tmp_path: Path, chunks: list[tuple[str, float]]) -> SQLiteStore:
    store = SQLiteStore(tmp_path / "s.db")
    for cid, trust in chunks:
        store.write(_chunk(cid, trust), allow_duplicate=True)
    return store


def _row(store: SQLiteStore, cid: str):
    with sqlite3.connect(store.path) as c:
        c.row_factory = sqlite3.Row
        return c.execute("SELECT chunk_id, superseded_by FROM chunks WHERE chunk_id = ?", (cid,)).fetchone()


@pytest.mark.parametrize("old_trust,new_trust", [(0.9, 0.5), (0.5, 0.9)])
def test_superseded_pair_not_merged(tmp_path: Path, old_trust: float, new_trust: float) -> None:
    store = _store(tmp_path, [("old", old_trust), ("new", new_trust)])
    store.supersede("old", "new")
    report = store.consolidate(pipeline_id="p")
    assert report.merged == 0 and report.tombstoned == 0
    assert report.skipped >= 1
    assert [c.chunk_id for c in store.get_chunks_by_ids(["old", "new"])] == ["new"]
    row = _row(store, "old")
    assert row is not None and row["superseded_by"] == "new"
    assert _row(store, "new") is not None


def test_loser_merge_repoints_history(tmp_path: Path) -> None:
    store = _store(tmp_path, [("H", 0.6), ("X", 0.5), ("K", 0.9)])
    store.supersede("H", "X")
    report = store.consolidate(pipeline_id="p")
    assert report.merged == 1
    assert report.merge_log[0]["kept"] == "K" and report.merge_log[0]["merged"] == ["X"]
    assert _row(store, "H")["superseded_by"] == "K"
    assert _row(store, "X") is None
    assert [c.chunk_id for c in store.get_chunks_by_ids(["K"])] == ["K"]


def test_dry_run_does_not_mutate(tmp_path: Path) -> None:
    store = _store(tmp_path, [("H", 0.6), ("X", 0.5), ("K", 0.9)])
    store.supersede("H", "X")
    report = store.consolidate(pipeline_id="p", dry_run=True)
    assert report.merged == 1
    assert _row(store, "H")["superseded_by"] == "X"
    assert _row(store, "X") is not None and _row(store, "K") is not None


def test_expired_valid_to_is_historical() -> None:
    from ncp.stores.consolidation import split_current_and_historical

    now = time.time()
    live = _chunk("a", 0.5)
    expired = _chunk("b", 0.5).model_copy(update={"valid_to": now - 10})
    future = _chunk("c", 0.5).model_copy(update={"valid_to": now + 1000})
    cur, hist = split_current_and_historical([live, expired, future], now=now)
    assert [c.chunk_id for c in cur] == ["a", "c"]
    assert [c.chunk_id for c in hist] == ["b"]


@pytest.mark.anyio
async def test_async_consolidate_skips_superseded_and_repoints() -> None:
    pytest.importorskip("psycopg")
    pytest.importorskip("psycopg_pool")
    from test_async_consolidate import _make_chunk_row, _make_store

    rows = [
        {**_make_chunk_row(chunk_id="H", content=CONTENT, base_trust=0.99), "superseded_by": "X"},
        {**_make_chunk_row(chunk_id="X", content=CONTENT, base_trust=0.5), "superseded_by": None},
        {**_make_chunk_row(chunk_id="K", content=CONTENT, base_trust=0.9), "superseded_by": None},
    ]
    store = _make_store()
    store._test_cursor.fetchall = AsyncMock(return_value=rows)
    report = await store.async_consolidate()
    assert report.merge_log[0]["kept"] == "K" and report.merge_log[0]["merged"] == ["X"]
    calls = [c.args for c in store._test_cursor.execute.call_args_list]
    deletes = [a for a in calls if "DELETE" in str(a[0])]
    assert [a[1] for a in deletes] == [("X",)]
    repoints = [a for a in calls if "SET superseded_by" in str(a[0])]
    assert [a[1] for a in repoints] == [("K", "X")]
