"""``forget``: operator-requested deletion on the SQLite store (and the base default)."""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path

import pytest

from ncp.stores.base import FORGET_TOMBSTONE_EXPIRES_AT, BaseStore
from ncp.stores.sqlite import SQLiteStore
from ncp.types import SubconsciousChunk


def _chunk(
    chunk_id: str,
    *,
    content: str | None = None,
    pipeline_id: str | None = "pipe",
    written_by: str = "alice",
    scope: str = "pipeline",
    src: str = "tool_result",
    raw_ref: str | None = None,
) -> SubconsciousChunk:
    return SubconsciousChunk(
        chunk_id=chunk_id,
        layer="semantic",
        content=content or f"zebra{chunk_id} unique statement about {chunk_id}",
        src=src,
        written_by=written_by,
        pipeline_id=pipeline_id,
        scope=scope,
        raw_ref=raw_ref,
    )


def _rows(store: SQLiteStore, sql: str, params: tuple[object, ...] = ()) -> list[tuple]:
    with sqlite3.connect(store.path) as conn:
        return [tuple(r) for r in conn.execute(sql, params)]


def _chunk_ids(store: SQLiteStore) -> set[str]:
    return {r[0] for r in _rows(store, "SELECT chunk_id FROM chunks")}


@pytest.fixture()
def store(tmp_path: Path) -> SQLiteStore:
    return SQLiteStore(tmp_path / "store.db")


def test_forget_requires_a_selector(store: SQLiteStore) -> None:
    with pytest.raises(ValueError, match="at least one"):
        store.forget()
    with pytest.raises(ValueError, match="at least one"):
        store.forget_candidates(chunk_ids=[])


def test_base_store_default_names_the_backend() -> None:
    class Dummy(BaseStore):
        def __init__(self) -> None:  # pragma: no cover
            pass

    Dummy.__abstractmethods__ = frozenset()
    with pytest.raises(NotImplementedError, match="forget is not implemented for Dummy"):
        Dummy().forget(chunk_ids=["x"])
    with pytest.raises(NotImplementedError, match="forget is not implemented for Dummy"):
        Dummy().forget_candidates(chunk_ids=["x"])


def test_forget_removes_chunk_from_every_retrieval_path(store: SQLiteStore) -> None:
    store.write(_chunk("secret", content="the launch codes are swordfish seven"), allow_duplicate=True)
    store.write(_chunk("other", content="unrelated note on gardening tomatoes"), allow_duplicate=True)
    assert [c.chunk_id for c in store.query("swordfish launch codes", pipeline_id="pipe")] == ["secret"]

    counts = store.forget(chunk_ids=["secret"])

    assert counts["chunks"] == 1 and counts["tombstones"] == 1
    assert store.query("swordfish launch codes", pipeline_id="pipe") == []
    recency = store.query("swordfish", pipeline_id="pipe", retrieval_mode="trust_recency")
    assert "secret" not in {c.chunk_id for c in recency}
    assert store.get_chunks_by_ids(["secret", "other"])[0].chunk_id == "other"
    assert all(c.chunk_id != "secret" for c in store.get_working_zone(pipeline_id="pipe"))
    assert _rows(store, "SELECT chunk_id FROM chunks_fts WHERE chunks_fts MATCH 'swordfish'") == []
    assert store.resolve_ref("secret") == "ctx://dead-end/missing"
    assert _chunk_ids(store) == {"other"}


def test_forget_deletes_raw_ref_backup_edges_and_links(store: SQLiteStore) -> None:
    store.write(_chunk("backup", content="verbatim original credentials hunter2 plus noise"), allow_duplicate=True)
    store.write(_chunk("atom", content="credentials are hunter2", raw_ref="backup"), allow_duplicate=True)
    store.write(_chunk("keep", content="keeper statement about parsley"), allow_duplicate=True)
    with sqlite3.connect(store.path) as conn:
        conn.executemany(
            "INSERT OR REPLACE INTO chunk_edges (edge_id, src_chunk_id, dst_chunk_id, edge_type, created_at)"
            " VALUES (?, ?, ?, 'derived_from', ?)",
            [("e1", "atom", "backup", 1.0), ("e2", "keep", "atom", 1.0), ("e3", "keep", "keep", 1.0)],
        )
        conn.execute("INSERT INTO chunk_aliases VALUES ('old_atom', 'atom', 'equivalent_consolidation', 'pipe', 1.0)")
        conn.execute("INSERT INTO outcome_applications VALUES ('o1', 'atom', 1.0)")
        conn.execute("INSERT INTO dissent_log VALUES ('atom', 'x', 1.0)")
        conn.execute(
            "INSERT INTO memo_entries (signature, task, chunk_ids, created_at) VALUES"
            " ('sig1', 't', '[\"atom\", \"keep\"]', 1.0), ('sig2', 't', '[\"keep\"]', 1.0)"
        )
    store.link_turn_chunks("turn1", ["atom", "backup", "keep"], relation="retrieved")

    preview = store.forget_candidates(chunk_ids=["atom"])
    assert [(c["chunk_id"], c["kind"]) for c in preview] == [("atom", "chunk"), ("backup", "raw_ref")]
    dry = store.forget(chunk_ids=["atom"], dry_run=True)
    assert _chunk_ids(store) == {"backup", "atom", "keep"}
    counts = store.forget(chunk_ids=["atom"])

    assert counts == dry
    assert counts["chunks"] == 1 and counts["raw_ref_chunks"] == 1
    assert counts["chunk_edges"] == 2 and counts["turn_chunks"] == 2
    assert counts["chunk_aliases"] == 1 and counts["outcome_applications"] == 1
    assert counts["dissent_log"] == 1 and counts["memo_entries"] == 1 and counts["tombstones"] == 2
    assert _chunk_ids(store) == {"keep"}
    assert _rows(store, "SELECT edge_id FROM chunk_edges") == [("e3",)]
    assert _rows(store, "SELECT chunk_id FROM turn_chunks") == [("keep",)]
    assert _rows(store, "SELECT * FROM chunk_aliases") == []
    assert _rows(store, "SELECT signature FROM memo_entries") == [("sig2",)]
    assert _rows(store, "SELECT chunk_id FROM chunks_fts WHERE chunks_fts MATCH 'hunter2'") == []
    assert store.query("hunter2 credentials", pipeline_id="pipe") == []


def test_forget_leaves_a_permanent_tombstone_and_refuses_rewrites(store: SQLiteStore) -> None:
    store.write(_chunk("gone"), allow_duplicate=True)
    store.forget(chunk_ids=["gone"])
    rows = _rows(store, "SELECT chunk_id, forward_ref, expires_at FROM tombstones")
    assert rows == [("gone", None, FORGET_TOMBSTONE_EXPIRES_AT)]

    # A replayed write is refused, and the expiry sweep keeps the tombstone.
    assert store.write(_chunk("gone", content="resurrected text"), allow_duplicate=True) is False
    store.write(_chunk("another"), allow_duplicate=True)
    assert _chunk_ids(store) == {"another"}
    assert [r[0] for r in _rows(store, "SELECT chunk_id FROM tombstones")] == ["gone"]
    assert store.query("resurrected", pipeline_id="pipe") == []
    # Consolidation cannot bring it back either.
    store.consolidate(dry_run=False)
    assert "gone" not in _chunk_ids(store)


def test_forget_by_pipeline_spares_global_and_other_pipelines(store: SQLiteStore) -> None:
    store.write(_chunk("p1_a", pipeline_id="p1"), allow_duplicate=True)
    store.write(_chunk("p1_b", pipeline_id="p1"), allow_duplicate=True)
    store.write(_chunk("p1_global", pipeline_id="p1", scope="global"), allow_duplicate=True)
    store.write(_chunk("p2_a", pipeline_id="p2"), allow_duplicate=True)
    store.write(_chunk("null_a", pipeline_id=None), allow_duplicate=True)

    counts = store.forget(pipeline_id="p1")

    assert counts["chunks"] == 2
    assert _chunk_ids(store) == {"p1_global", "p2_a", "null_a"}
    assert {r[0] for r in _rows(store, "SELECT chunk_id FROM tombstones")} == {"p1_a", "p1_b"}


def test_forget_agent_and_pipeline_selectors_are_anded(store: SQLiteStore) -> None:
    store.write(_chunk("a_p1", pipeline_id="p1", written_by="alice"), allow_duplicate=True)
    store.write(_chunk("a_p2", pipeline_id="p2", written_by="alice"), allow_duplicate=True)
    store.write(_chunk("b_p1", pipeline_id="p1", written_by="bob"), allow_duplicate=True)

    assert store.forget(written_by="alice", pipeline_id="p1")["chunks"] == 1
    assert _chunk_ids(store) == {"a_p2", "b_p1"}
    assert store.forget(written_by="alice")["chunks"] == 1
    assert _chunk_ids(store) == {"b_p1"}
    # Explicit chunk ids are still narrowed by the other selectors.
    assert store.forget(chunk_ids=["b_p1"], written_by="alice")["chunks"] == 0
    assert _chunk_ids(store) == {"b_p1"}


def test_forget_deletes_user_verified_when_explicitly_selected(store: SQLiteStore) -> None:
    store.write(_chunk("trusted", src="user_verified"), allow_duplicate=True)
    assert store.forget(chunk_ids=["trusted"])["chunks"] == 1
    assert _chunk_ids(store) == set()


def test_forget_ignores_unknown_ids_and_batches_many(store: SQLiteStore) -> None:
    assert store.forget(chunk_ids=["nope"]) == {
        "chunks": 0, "raw_ref_chunks": 0, "chunk_edges": 0, "turn_chunks": 0, "chunk_aliases": 0,
        "outcome_applications": 0, "dissent_log": 0, "memo_entries": 0, "tombstones": 0,
    }
    with sqlite3.connect(store.path) as conn:
        conn.executemany(
            "INSERT INTO chunks (chunk_id, pipeline_id, layer, content, src, created_at) VALUES (?, 'bulk', 'semantic', ?, 'tool_result', ?)",
            [(f"bulk_{i}", f"bulk text {i}", time.time()) for i in range(450)],
        )
    assert store.forget(pipeline_id="bulk")["chunks"] == 450
    assert _chunk_ids(store) == set()
    assert len(_rows(store, "SELECT chunk_id FROM tombstones")) == 450


def test_forget_does_not_delete_a_backup_owned_by_another_pipeline(store: SQLiteStore) -> None:
    store.write(_chunk("foreign", pipeline_id="p2"), allow_duplicate=True)
    store.write(_chunk("atom", pipeline_id="p1", raw_ref="foreign"), allow_duplicate=True)
    counts = store.forget(chunk_ids=["atom"])
    assert counts["raw_ref_chunks"] == 0
    assert _chunk_ids(store) == {"foreign"}
