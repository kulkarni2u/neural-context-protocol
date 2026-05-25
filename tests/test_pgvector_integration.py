from __future__ import annotations

import importlib.util
import os
from uuid import uuid4

import pytest

from ncp.stores.pgvector import PgvectorStore
from ncp.types import ConsciousBlock, NCPResponse, SubconsciousChunk, TurnRecord


pytestmark = pytest.mark.skipif(
    os.getenv("NCP_RUN_PGVECTOR_INTEGRATION") != "1",
    reason="set NCP_RUN_PGVECTOR_INTEGRATION=1 to run live pgvector integration tests",
)


def _require_psycopg2() -> None:
    if importlib.util.find_spec("psycopg2") is None:
        pytest.skip("psycopg2 is not installed; install neural-context-protocol[pgvector] first")


def _pgvector_store() -> PgvectorStore:
    _require_psycopg2()
    schema = f"ncp_it_{uuid4().hex[:8]}"
    return PgvectorStore(
        os.getenv("NCP_PGVECTOR_DSN", "postgresql://postgres:postgres@127.0.0.1:5432/ncp"),
        schema=schema,
        table_prefix="it_",
    )


def test_pgvector_live_write_query_and_restart() -> None:
    store = _pgvector_store()
    chunk = SubconsciousChunk(
        chunk_id="sub_auth",
        layer="procedural",
        content="authentication handler validates bearer tokens and returns 401 on failure",
        src="tool_result",
        pipeline_id="pipe_live",
        written_by="executor",
    )

    assert store.write(chunk) is True
    assert store.write(chunk.model_copy(update={"chunk_id": "sub_auth_dup"})) is False

    restarted = PgvectorStore(store.dsn, schema=store.schema, table_prefix=store.table_prefix)
    results = restarted.query("bearer token failure", pipeline_id="pipe_live")

    assert [result.chunk_id for result in results] == ["sub_auth"]
    assert [result.chunk_id for result in restarted.get_working_zone(pipeline_id="pipe_live")] == ["sub_auth"]


def test_pgvector_live_src_immutability_and_recent_refs() -> None:
    store = _pgvector_store()
    chunk = SubconsciousChunk(
        chunk_id="sub_src_lock",
        layer="semantic",
        content="immutable source check",
        src="tool_result",
        pipeline_id="pipe_live",
    )
    record = TurnRecord(
        turn_id="turn_alpha",
        agent_id="planner",
        pipeline_id="pipe_live",
        task="refactor_auth",
        slot="identify_dead_code",
        result="short summary",
        result_full="longer result body",
        created_at=100.0,
        expires_at=200.0,
    )

    store.write(chunk)
    store.log_turn_record(record)

    with pytest.raises(ValueError, match="src is immutable"):
        store.write(chunk.model_copy(update={"src": "synthesis"}))

    resolved = store.resolve_recent_ref("r:sub/turn_alpha")

    assert resolved is not None
    assert resolved.result_full == "longer result body"


def test_pgvector_live_conscious_cost_and_goal_versions() -> None:
    store = _pgvector_store()
    planner = ConsciousBlock(
        agent_id="planner",
        role="decompose",
        owns=["planning"],
        must_not=["shipping"],
        task="refactor_auth",
        slot="identify_dead_code",
        intent="reduce_complexity",
        pipeline_id="pipe_live",
        goal_version=3,
    )
    executor = ConsciousBlock(
        agent_id="executor",
        role="build",
        owns=["implementation"],
        must_not=["planning"],
        task="refactor_auth",
        slot="apply_patch",
        intent="land_fix",
        pipeline_id="pipe_live",
        goal_version=5,
    )
    response = NCPResponse(
        content="done",
        input_tokens=100,
        output_tokens=20,
        cost_usd=0.05,
        model="claude_sonnet",
        pipeline_id="pipe_live",
        turn_id="turn_cost",
        latency_ms=800,
    )

    store.log_conscious(planner, snapshot_hash="hash_planner")
    store.log_conscious(executor, snapshot_hash="hash_executor")
    store.log_cost(agent_id="planner", response=response)

    versions = store.get_pipeline_goal_versions(pipeline_id="pipe_live", current_agent="executor")

    assert versions == {"planner": 3}
