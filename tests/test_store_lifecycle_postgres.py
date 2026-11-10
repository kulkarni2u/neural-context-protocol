"""Live Postgres checks for the store lifecycle changes (trust-aware hard GC,
vacuum, migration 019). tests/test_store_lifecycle.py covers the same logic
against scripted fakes; these run the real SQL.

Gated like the other live pgvector tests: NCP_RUN_PGVECTOR_INTEGRATION=1 and
NCP_PGVECTOR_DSN.
"""

from __future__ import annotations

import importlib.util
import os
import time
from typing import Any, Iterator
from uuid import uuid4

import pytest

from ncp.stores.pgvector import PgvectorStore
from ncp.types import SubconsciousChunk

LIVE_DSN = os.getenv("NCP_PGVECTOR_DSN")

pytestmark = pytest.mark.skipif(
    os.getenv("NCP_RUN_PGVECTOR_INTEGRATION") != "1"
    or not LIVE_DSN
    or importlib.util.find_spec("psycopg") is None,
    reason="set NCP_RUN_PGVECTOR_INTEGRATION=1 and NCP_PGVECTOR_DSN to run live pgvector tests",
)

VACUUM_TABLES = {
    "conscious_log",
    "cost_log",
    "embedding_cost_log",
    "drift_history",
    "turn_chunks",
    "outcomes",
    "outcome_applications",
    "dissent_log",
    "tasks",
    "chunk_edges",
}


@pytest.fixture
def schema() -> Iterator[tuple[Any, str]]:
    import psycopg

    name = f"ncp_lc_{uuid4().hex[:8]}"
    admin = psycopg.connect(LIVE_DSN, autocommit=True)
    admin.execute(f"CREATE SCHEMA {name}")
    try:
        yield admin, name
    finally:
        admin.execute(f"DROP SCHEMA {name} CASCADE")
        admin.close()


def _store(schema_name: str, **kwargs: Any) -> PgvectorStore:
    import psycopg

    return PgvectorStore(
        LIVE_DSN,
        schema=schema_name,
        table_prefix="t_",
        connect_factory=lambda dsn: psycopg.connect(dsn),
        **kwargs,
    )


def _chunk(chunk_id: str, *, src: str = "tool_result", trust: float = 0.7, pipeline_id: str | None = "pipe") -> SubconsciousChunk:
    return SubconsciousChunk(
        chunk_id=chunk_id,
        layer="semantic",
        content=f"distinct content for {chunk_id}",
        src=src,
        base_trust=trust,
        pipeline_id=pipeline_id,
    )


def _working_ids(admin: Any, schema_name: str, pipeline_id: str | None) -> set[str]:
    if pipeline_id is None:
        rows = admin.execute(
            f"SELECT chunk_id FROM {schema_name}.t_chunks WHERE zone = 'working' AND pipeline_id IS NULL"
        ).fetchall()
    else:
        rows = admin.execute(
            f"SELECT chunk_id FROM {schema_name}.t_chunks WHERE zone = 'working' AND pipeline_id = %s",
            (pipeline_id,),
        ).fetchall()
    return {str(row[0]) for row in rows}


def test_hard_gc_keeps_user_verified_and_evicts_low_trust(schema: tuple[Any, str]) -> None:
    admin, name = schema
    store = _store(name, max_working_chunks=5, gc_threshold=3)
    for chunk in (
        _chunk("uv_1", src="user_verified", trust=0.2),
        _chunk("uv_2", src="user_verified", trust=0.2),
        _chunk("low_1", trust=0.05),
        _chunk("low_2", trust=0.05),
        _chunk("high_1", trust=0.95),
    ):
        assert store.write(chunk, allow_duplicate=True)
    assert len(_working_ids(admin, name, "pipe")) == 5

    assert store.write(_chunk("high_2", trust=0.95), allow_duplicate=True)

    remaining = _working_ids(admin, name, "pipe")
    assert {"uv_1", "uv_2"} <= remaining
    assert not remaining & {"low_1", "low_2"}
    assert len(remaining) == 3


def test_hard_gc_without_pipeline_leaves_other_pipelines_alone(schema: tuple[Any, str]) -> None:
    admin, name = schema
    store = _store(name, max_working_chunks=3, gc_threshold=2)
    for index in range(3):
        store.write(_chunk(f"a_{index}", pipeline_id="pipe_a"), allow_duplicate=True)
    for index in range(4):
        store.write(_chunk(f"none_{index}", pipeline_id=None), allow_duplicate=True)
    assert _working_ids(admin, name, "pipe_a") == {"a_0", "a_1", "a_2"}
    assert len(_working_ids(admin, name, None)) == 2


def test_vacuum_runs_and_removes_only_dangling_and_aged_rows(schema: tuple[Any, str]) -> None:
    admin, name = schema
    store = _store(name)
    store.write(_chunk("live_a"), allow_duplicate=True)
    store.write(_chunk("live_b"), allow_duplicate=True)

    # Every statement in the plan must parse and run on a real server.
    assert store.vacuum(3600, dry_run=True) == {table: 0 for table in VACUUM_TABLES}

    old = time.time() - 7200
    edges = f"{name}.t_chunk_edges"
    columns = {
        row[0]
        for row in admin.execute(
            "SELECT column_name FROM information_schema.columns WHERE table_schema = %s AND table_name = 't_chunk_edges'",
            (name,),
        ).fetchall()
    }
    assert {"edge_id", "src_chunk_id", "dst_chunk_id", "edge_type", "created_at"} <= columns
    for edge_id, src, dst in (("e_live", "live_a", "live_b"), ("e_dangling", "live_a", "gone")):
        admin.execute(
            f"INSERT INTO {edges} (edge_id, src_chunk_id, dst_chunk_id, edge_type, created_at)"
            " VALUES (%s, %s, %s, 'caused_by', %s)",
            (edge_id, src, dst, old),
        )
    admin.execute(
        f"INSERT INTO {name}.t_turn_chunks (turn_id, chunk_id, relation, created_at)"
        " VALUES ('t_old', 'live_a', 'retrieved', %s), ('t_new', 'live_a', 'retrieved', %s)",
        (old, time.time()),
    )

    expected = {table: 0 for table in VACUUM_TABLES} | {"chunk_edges": 1, "turn_chunks": 1}
    assert store.vacuum(3600, dry_run=True) == expected
    assert store.vacuum(3600) == expected
    assert store.vacuum(3600) == {table: 0 for table in VACUUM_TABLES}

    assert {r[0] for r in admin.execute(f"SELECT edge_id FROM {edges}").fetchall()} == {"e_live"}
    assert {r[0] for r in admin.execute(f"SELECT turn_id FROM {name}.t_turn_chunks").fetchall()} == {"t_new"}
    assert _working_ids(admin, name, "pipe") == {"live_a", "live_b"}


def test_fresh_schema_has_expiry_indexes(schema: tuple[Any, str]) -> None:
    admin, name = schema
    _store(name)
    indexes = {
        row[0]
        for row in admin.execute("SELECT indexname FROM pg_indexes WHERE schemaname = %s", (name,)).fetchall()
    }
    assert {
        "t_idx_chunks_expiry",
        "t_idx_turns_expires",
        "t_idx_tombstones_expires",
        "t_idx_whispers_expires",
    } <= indexes


def test_retention_cap_keeps_user_verified(schema: tuple[Any, str]) -> None:
    admin, name = schema
    store = _store(name, max_working_chunks_per_pipeline=3)
    for index in range(2):
        store.write(_chunk(f"uv_{index}", src="user_verified", trust=0.05), allow_duplicate=True)
    for index in range(4):
        store.write(_chunk(f"sub_{index}", trust=0.5 + index * 0.1), allow_duplicate=True)

    remaining = _working_ids(admin, name, "pipe")
    assert {"uv_0", "uv_1"} <= remaining
    assert len(remaining) == 3
