from pathlib import Path

import pytest

from ncp.stores.base import NCPStoreUnavailableError
from ncp.stores.sqlite import SQLiteStore
from ncp.types import ConsciousBlock, NCPResponse, SubconsciousChunk, TurnRecord, Whisper


def test_sqlite_store_initializes_all_tables(tmp_path: Path) -> None:
    store_path = tmp_path / "store.db"
    SQLiteStore(store_path)

    assert store_path.exists()

    import sqlite3

    connection = sqlite3.connect(store_path)
    tables = {
        row[0]
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }
    connection.close()

    assert {"chunks", "tombstones", "whispers", "turn_records", "conscious_log", "cost_log"} <= tables


def test_sqlite_store_write_query_and_restart(tmp_path: Path) -> None:
    store_path = tmp_path / "store.db"
    store = SQLiteStore(store_path)
    chunk = SubconsciousChunk(
        chunk_id="sub_auth",
        layer="procedural",
        content="authentication handler validates bearer tokens and returns 401 on failure",
        src="tool_result",
        pipeline_id="pipe_1",
    )

    assert store.write(chunk) is True
    restarted = SQLiteStore(store_path)
    results = restarted.query("bearer token failure", pipeline_id="pipe_1")

    assert [result.chunk_id for result in results] == ["sub_auth"]
    assert results[0].pipeline_id == "pipe_1"


def test_sqlite_store_duplicate_write_is_skipped(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "store.db")
    chunk = SubconsciousChunk(
        layer="episodic",
        content="same content for duplicate detection",
        src="synthesis",
    )

    assert store.write(chunk) is True
    assert store.write(chunk.model_copy(update={"chunk_id": "sub_duplicate"})) is False


def test_sqlite_store_whisper_drain_filters_and_deletes(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "store.db")
    store.emit_whisper(
        Whisper(
            from_agent="planner",
            target="executor",
            whisper_type="nudge",
            payload="check_tests",
            confidence=0.8,
            pipeline_id="pipe_1",
        )
    )
    store.emit_whisper(
        Whisper(
            from_agent="planner",
            target="executor",
            whisper_type="share",
            payload="low_conf_signal",
            confidence=0.2,
            pipeline_id="pipe_1",
        )
    )

    drained = store.drain_whispers(agent_id="executor", pipeline_id="pipe_1")

    assert [whisper.payload for whisper in drained] == ["check_tests"]
    assert store.drain_whispers(agent_id="executor", pipeline_id="pipe_1") == []


def test_sqlite_store_turn_logging_and_recent_ref_resolution(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "store.db")
    record = TurnRecord(
        turn_id="turn_alpha",
        agent_id="planner",
        pipeline_id="pipe_1",
        task="refactor_auth",
        slot="identify_dead_code",
        result="short summary",
        result_full="longer result body",
        created_at=100.0,
        expires_at=200.0,
    )

    store.log_turn_record(record)
    resolved = store.resolve_recent_ref("r:sub/turn_alpha")

    assert resolved is not None
    assert resolved.result_full == "longer result body"


def test_sqlite_store_tombstone_chain_and_status(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "store.db")
    store.write(
        SubconsciousChunk(
            chunk_id="sub_old",
            layer="semantic",
            content="old chunk body",
            src="user_verified",
            expiry=9999999999.0,
            zone="proven",
        )
    )
    store.tombstone("sub_old", forward_ref="sub_new")
    store.write(
        SubconsciousChunk(
            chunk_id="sub_new",
            layer="semantic",
            content="new chunk body",
            src="user_verified",
            expiry=9999999999.0,
            zone="proven",
        )
    )

    assert store.resolve_ref("ctx://sub/sub_old") == "sub_new"
    assert store.status()["chunk_count"] == 1


def test_sqlite_store_resolve_ref_returns_explicit_dead_end_signal(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "store.db")
    store.tombstone("sub_missing")

    assert store.resolve_ref("ctx://sub/sub_missing") == "ctx://dead-end/missing"


def test_sqlite_store_resolve_ref_returns_max_hops_dead_end(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "store.db")
    for index in range(12):
        store.tombstone(f"sub_{index}", forward_ref=f"sub_{index + 1}")

    assert store.resolve_ref("ctx://sub/sub_0") == "ctx://dead-end/max-hops"


def test_sqlite_store_src_is_immutable_for_existing_chunk_id(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "store.db")
    chunk = SubconsciousChunk(
        chunk_id="sub_src_lock",
        layer="semantic",
        content="immutable source check",
        src="tool_result",
    )
    store.write(chunk)

    with pytest.raises(ValueError, match="src is immutable"):
        store.write(chunk.model_copy(update={"src": "synthesis"}))


def test_sqlite_store_revalidates_chunk_before_persisting(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "store.db")
    chunk = SubconsciousChunk(
        chunk_id="sub_revalidate",
        layer="semantic",
        content="safe",
        src="tool_result",
    ).model_copy(update={"src": "bad_src"})

    with pytest.raises(ValueError):
        store.write(chunk)


def test_sqlite_store_revalidates_whisper_before_persisting(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "store.db")
    whisper = Whisper(
        from_agent="critic",
        target="executor",
        whisper_type="dissent",
        payload="safe",
        confidence=0.9,
    ).model_copy(update={"target": "*"})

    with pytest.raises(ValueError, match="dissent whispers cannot target '\\*'"):
        store.emit_whisper(whisper)


def test_sqlite_store_wraps_unavailable_path_errors(tmp_path: Path) -> None:
    unavailable_path = tmp_path / "db_dir"
    unavailable_path.mkdir()

    with pytest.raises(NCPStoreUnavailableError, match="SQLite store unavailable"):
        SQLiteStore(unavailable_path)


def test_sqlite_store_logs_conscious_and_cost(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "store.db")
    conscious = ConsciousBlock(
        agent_id="planner",
        role="decompose",
        owns=["planning"],
        must_not=["shipping"],
        task="refactor_auth",
        slot="identify_dead_code",
        intent="reduce_complexity",
        pipeline_id="pipe_1",
    )
    response = NCPResponse(
        content="done",
        input_tokens=100,
        output_tokens=20,
        cost_usd=0.05,
        model="claude_sonnet",
        pipeline_id="pipe_1",
        turn_id="turn_cost",
        latency_ms=800,
    )

    store.log_conscious(conscious, snapshot_hash="hash_123")
    store.log_cost(agent_id="planner", response=response)

    status = store.status()

    assert status["cost_usd_total"] == 0.05


def test_sqlite_store_status_detail_and_cost_summary(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "store.db")
    store.write(
        SubconsciousChunk(
            chunk_id="sub_semantic",
            layer="semantic",
            content="semantic chunk",
            src="tool_result",
            pipeline_id="pipe_alpha",
        )
    )
    store.write(
        SubconsciousChunk(
            chunk_id="sub_procedural",
            layer="procedural",
            content="procedural chunk",
            src="synthesis",
            pipeline_id="pipe_alpha",
        )
    )
    store.emit_whisper(
        Whisper(
            from_agent="planner",
            target="executor",
            whisper_type="nudge",
            payload="check_alpha",
            confidence=0.9,
            pipeline_id="pipe_alpha",
        )
    )
    store.log_turn_record(
        TurnRecord(
            turn_id="turn_alpha",
            agent_id="planner",
            pipeline_id="pipe_alpha",
            task="status_slice",
            slot="inspect",
            result="summary",
            result_full="summary full",
        )
    )
    conscious = ConsciousBlock(
        agent_id="planner",
        role="decompose",
        owns=["planning"],
        must_not=["shipping"],
        task="status_slice",
        slot="inspect",
        intent="show_rollup",
        pipeline_id="pipe_alpha",
    )
    response = NCPResponse(
        content="done",
        input_tokens=150,
        output_tokens=30,
        cost_usd=0.0125,
        model="claude_sonnet",
        pipeline_id="pipe_alpha",
        turn_id="turn_cost_alpha",
        latency_ms=250,
    )
    store.log_conscious(conscious, snapshot_hash="hash_alpha")
    store.log_cost(agent_id="planner", response=response)

    detail = store.status_detail()
    filtered = store.status_detail(pipeline_id="pipe_alpha")
    costs = store.cost_summary(pipeline_id="pipe_alpha", limit=5)

    assert detail["overview"]["chunk_count"] == 2
    assert detail["overview"]["pipeline_count"] == 1
    assert detail["layer_counts"] == {"procedural": 1, "semantic": 1}
    assert detail["recent_pipelines"][0]["pipeline_id"] == "pipe_alpha"
    assert filtered["overview"]["whisper_count"] == 1
    assert costs["summary"]["cost_usd_total"] == 0.0125
    assert costs["summary"]["input_tokens_total"] == 150
    assert costs["by_agent"][0]["agent_id"] == "planner"
    assert costs["by_model"][0]["model"] == "claude_sonnet"
    assert costs["recent_entries"][0]["turn_id"] == "turn_cost_alpha"
