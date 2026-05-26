from __future__ import annotations

import importlib.util
import json
import os
from uuid import uuid4

import pytest

from ncp.mcp.server import make_handlers, _handle_request
from ncp.stores.pgvector import PgvectorStore
from ncp.types import ConsciousBlock, NCPResponse, SubconsciousChunk, TurnRecord, Whisper


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
        redis_url=os.getenv("NCP_REDIS_URL", "redis://127.0.0.1:6379/0"),
    )


def _call(name: str, arguments: dict | None = None, req_id: int = 1) -> dict:
    params: dict = {"name": name}
    if arguments is not None:
        params["arguments"] = arguments
    return {"jsonrpc": "2.0", "id": req_id, "method": "tools/call", "params": params}


def _content(response_str: str) -> object:
    payload = json.loads(response_str)["result"]
    return json.loads(payload["content"][0]["text"])


def _error(response_str: str) -> dict:
    return json.loads(response_str).get("error", {})


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


def test_pgvector_live_status_and_cost_reporting() -> None:
    store = _pgvector_store()
    store.write(
        SubconsciousChunk(
            chunk_id="sub_live_status",
            layer="semantic",
            content="live reporting chunk",
            src="tool_result",
            pipeline_id="pipe_live_report",
        )
    )
    store.log_turn_record(
        TurnRecord(
            turn_id="turn_live_report",
            agent_id="planner",
            pipeline_id="pipe_live_report",
            task="reporting",
            slot="status",
            result="summary",
            result_full="full summary",
            created_at=100.0,
            expires_at=200.0,
        )
    )
    store.log_conscious(
        ConsciousBlock(
            agent_id="planner",
            role="decompose",
            owns=["planning"],
            must_not=["shipping"],
            task="reporting",
            slot="status",
            intent="verify_live_reporting",
            pipeline_id="pipe_live_report",
            goal_version=2,
        ),
        snapshot_hash="hash_live_report",
    )
    store.log_cost(
        agent_id="planner",
        response=NCPResponse(
            content="done",
            input_tokens=90,
            output_tokens=11,
            cost_usd=0.02,
            model="claude-sonnet",
            pipeline_id="pipe_live_report",
            turn_id="turn_live_cost",
            latency_ms=140,
        ),
    )
    store.emit_whisper(
        Whisper(
            whisper_id=f"wsp_live_report_{uuid4().hex[:8]}",
            from_agent="claude",
            target="opencode",
            whisper_type="share",
            payload="check live reporting path",
            confidence=0.95,
            pipeline_id="pipe_live_report",
        )
    )

    detail = store.status_detail(pipeline_id="pipe_live_report")
    costs = store.cost_summary(pipeline_id="pipe_live_report", limit=5)

    assert detail["overview"]["chunk_count"] == 1
    assert detail["overview"]["whisper_count"] == 1
    assert detail["overview"]["turn_record_count"] == 1
    assert detail["overview"]["conscious_snapshot_count"] == 1
    assert detail["layer_counts"] == {"semantic": 1}
    assert detail["recent_pipelines"][0]["pipeline_id"] == "pipe_live_report"
    assert costs["summary"]["cost_usd_total"] == 0.02
    assert costs["summary"]["entry_count"] == 1
    assert costs["by_agent"][0]["agent_id"] == "planner"
    assert costs["by_model"][0]["model"] == "claude-sonnet"
    assert costs["recent_entries"][0]["turn_id"] == "turn_live_cost"


def test_pgvector_live_redis_coordination_for_whispers_and_fetch_sessions() -> None:
    if importlib.util.find_spec("redis") is None:
        pytest.skip("redis client is not installed; install neural-context-protocol[redis] first")

    store = _pgvector_store()
    store.emit_whisper(
        Whisper(
            whisper_id=f"wsp_live_{uuid4().hex[:8]}",
            from_agent="claude",
            target="opencode",
            whisper_type="share",
            payload="handoff the live pgvector slice",
            confidence=0.95,
            pipeline_id="pipe_live_coord",
        )
    )

    peeked = store.peek_whispers(agent_id="opencode", pipeline_id="pipe_live_coord")
    drained = store.drain_whispers(agent_id="opencode", pipeline_id="pipe_live_coord")

    store.write(
        SubconsciousChunk(
            chunk_id="sub_live_coord",
            layer="semantic",
            content="coordination fetch query result",
            src="tool_result",
            written_by="executor",
            pipeline_id="pipe_live_coord",
        )
    )
    handlers = make_handlers(store)
    _handle_request(
        _call(
            "ncp_get_context",
            {
                "agent_id": "builder",
                "role": "build",
                "owns": [],
                "must_not": [],
                "task": "coordination",
                "slot": "fetch",
                "intent": "verify",
                "pipeline_id": "pipe_live_coord",
                "session_id": "live_coord_sess",
            },
        ),
        handlers,
    )
    for _ in range(3):
        _handle_request(_call("ncp_fetch", {"query": "coordination fetch", "session_id": "live_coord_sess"}), handlers)

    err = _handle_request(_call("ncp_fetch", {"query": "coordination fetch", "session_id": "live_coord_sess"}, req_id=99), handlers)

    assert [whisper.payload for whisper in peeked] == ["handoff the live pgvector slice"]
    assert [whisper.payload for whisper in drained] == ["handoff the live pgvector slice"]
    assert _error(err)["message"] == "Tool error: ncp_fetch limit reached: max 3 per session"
