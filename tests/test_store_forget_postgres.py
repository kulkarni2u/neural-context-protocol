"""Live Postgres checks for ``PgvectorStore.forget``; the SQLite twin is tests/test_store_forget.py.

Gated like the other live pgvector tests: NCP_RUN_PGVECTOR_INTEGRATION=1 and
NCP_PGVECTOR_DSN.
"""

from __future__ import annotations

import importlib.util
import os
from typing import Any, Iterator
from uuid import uuid4

import pytest

from ncp.stores.base import FORGET_TOMBSTONE_EXPIRES_AT
from ncp.stores.pgvector import PgvectorStore
from ncp.types import SubconsciousChunk

LIVE_DSN = os.getenv("NCP_PGVECTOR_DSN")

pytestmark = pytest.mark.skipif(
    os.getenv("NCP_RUN_PGVECTOR_INTEGRATION") != "1"
    or not LIVE_DSN
    or importlib.util.find_spec("psycopg") is None,
    reason="set NCP_RUN_PGVECTOR_INTEGRATION=1 and NCP_PGVECTOR_DSN to run live pgvector tests",
)


@pytest.fixture
def schema() -> Iterator[tuple[Any, str]]:
    import psycopg

    name = f"ncp_fg_{uuid4().hex[:8]}"
    admin = psycopg.connect(LIVE_DSN, autocommit=True)
    admin.execute(f"CREATE SCHEMA {name}")
    try:
        yield admin, name
    finally:
        admin.execute(f"DROP SCHEMA {name} CASCADE")
        admin.close()


@pytest.fixture
def store(schema: tuple[Any, str]) -> PgvectorStore:
    import psycopg

    return PgvectorStore(
        LIVE_DSN,
        schema=schema[1],
        table_prefix="t_",
        connect_factory=lambda dsn: psycopg.connect(dsn),
    )


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


def _q(schema: tuple[Any, str], sql: str, *params: object) -> list[tuple]:
    admin, name = schema
    return admin.execute(sql.replace("{s}", f"{name}.t_"), params).fetchall()


def _ids(schema: tuple[Any, str]) -> set[str]:
    return {r[0] for r in _q(schema, "SELECT chunk_id FROM {s}chunks")}


def _write(store: PgvectorStore, chunk: SubconsciousChunk) -> None:
    assert store.write(chunk, allow_duplicate=True)


def test_forget_removes_chunk_backup_links_and_leaves_tombstone(schema: tuple[Any, str], store: PgvectorStore) -> None:
    _write(store, _chunk("backup", content="verbatim original credentials hunter2 plus noise"))
    _write(store, _chunk("atom", content="credentials are hunter2", raw_ref="backup"))
    _write(store, _chunk("keep", content="keeper statement about parsley"))
    admin, _ = schema
    for sql, params in [
        ("INSERT INTO {s}chunk_edges (edge_id, src_chunk_id, dst_chunk_id, edge_type, created_at) VALUES (%s, %s, %s, 'derived_from', 1.0)", ("e1", "atom", "backup")),
        ("INSERT INTO {s}chunk_edges (edge_id, src_chunk_id, dst_chunk_id, edge_type, created_at) VALUES (%s, %s, %s, 'derived_from', 1.0)", ("e2", "keep", "atom")),
        ("INSERT INTO {s}chunk_edges (edge_id, src_chunk_id, dst_chunk_id, edge_type, created_at) VALUES (%s, %s, %s, 'derived_from', 1.0)", ("e3", "keep", "keep")),
        ("INSERT INTO {s}chunk_aliases VALUES ('old_atom', 'atom', 'equivalent_consolidation', 'pipe', 1.0)", ()),
        ("INSERT INTO {s}outcome_applications VALUES ('o1', 'atom', 1.0)", ()),
        ("INSERT INTO {s}dissent_log VALUES ('atom', 'x', 1.0)", ()),
        ("INSERT INTO {s}memo_entries (signature, task, chunk_ids, created_at) VALUES ('sig1', 't', '[\"atom\", \"keep\"]', 1.0), ('sig2', 't', '[\"keep\"]', 1.0)", ()),
    ]:
        admin.execute(sql.replace("{s}", f"{schema[1]}.t_"), params)
    store.link_turn_chunks("turn1", ["atom", "backup", "keep"], relation="retrieved")

    preview = store.forget_candidates(chunk_ids=["atom"])
    assert [(c["chunk_id"], c["kind"]) for c in preview] == [("atom", "chunk"), ("backup", "raw_ref")]
    dry = store.forget(chunk_ids=["atom"], dry_run=True)
    assert _ids(schema) == {"backup", "atom", "keep"}
    assert _q(schema, "SELECT * FROM {s}tombstones") == []

    counts = store.forget(chunk_ids=["atom"])

    assert counts == dry
    assert counts["chunks"] == 1 and counts["raw_ref_chunks"] == 1
    assert counts["chunk_edges"] == 2 and counts["turn_chunks"] == 2
    assert counts["chunk_aliases"] == 1 and counts["outcome_applications"] == 1
    assert counts["dissent_log"] == 1 and counts["memo_entries"] == 1 and counts["tombstones"] == 2
    assert _ids(schema) == {"keep"}
    assert _q(schema, "SELECT edge_id FROM {s}chunk_edges") == [("e3",)]
    assert _q(schema, "SELECT chunk_id FROM {s}turn_chunks") == [("keep",)]
    assert _q(schema, "SELECT * FROM {s}chunk_aliases") == []
    assert _q(schema, "SELECT signature FROM {s}memo_entries") == [("sig2",)]
    assert sorted(_q(schema, "SELECT chunk_id, forward_ref, expires_at FROM {s}tombstones")) == [
        ("atom", None, FORGET_TOMBSTONE_EXPIRES_AT),
        ("backup", None, FORGET_TOMBSTONE_EXPIRES_AT),
    ]
    assert store.query("hunter2 credentials", pipeline_id="pipe") == []
    assert [c.chunk_id for c in store.get_working_zone(pipeline_id="pipe")] == ["keep"]


def test_forget_refuses_rewrites_and_survives_expiry_sweep(schema: tuple[Any, str], store: PgvectorStore) -> None:
    _write(store, _chunk("gone", content="the vault passphrase is osprey"))
    store.forget(chunk_ids=["gone"])

    assert store.write(_chunk("gone", content="resurrected osprey"), allow_duplicate=True) is False
    _write(store, _chunk("another"))  # runs _soft_gc, which must keep the tombstone
    assert _ids(schema) == {"another"}
    assert [r[0] for r in _q(schema, "SELECT chunk_id FROM {s}tombstones")] == ["gone"]
    assert store.query("osprey", pipeline_id="pipe") == []


def test_forget_selectors_pipeline_global_and_user_verified(schema: tuple[Any, str], store: PgvectorStore) -> None:
    _write(store, _chunk("p1_a", pipeline_id="p1", written_by="alice"))
    _write(store, _chunk("p1_b", pipeline_id="p1", written_by="bob", src="user_verified"))
    _write(store, _chunk("p1_global", pipeline_id="p1", scope="global"))
    _write(store, _chunk("p2_a", pipeline_id="p2", written_by="alice"))
    _write(store, _chunk("null_a", pipeline_id=None))

    with pytest.raises(ValueError, match="at least one"):
        store.forget()
    assert store.forget(chunk_ids=["p1_a"], written_by="bob")["chunks"] == 0  # selectors are ANDed
    assert store.forget(written_by="alice", pipeline_id="p1")["chunks"] == 1
    assert _ids(schema) == {"p1_b", "p1_global", "p2_a", "null_a"}

    counts = store.forget(pipeline_id="p1")  # user_verified goes, global and other pipelines stay
    assert counts["chunks"] == 1
    assert _ids(schema) == {"p1_global", "p2_a", "null_a"}


def test_forget_does_not_delete_a_backup_owned_by_another_pipeline(schema: tuple[Any, str], store: PgvectorStore) -> None:
    _write(store, _chunk("foreign", pipeline_id="p2"))
    _write(store, _chunk("atom", pipeline_id="p1", raw_ref="foreign"))
    assert store.forget(chunk_ids=["atom"])["raw_ref_chunks"] == 0
    assert _ids(schema) == {"foreign"}
