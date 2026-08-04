from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import time

import pytest

from ncp.config import NCPConfig
from ncp.stores.base import NCPStoreUnavailableError
from ncp.stores.sqlite import SQLiteStore
from ncp.types import ConsciousBlock, DissentPayload, NCPResponse, SubconsciousChunk, TurnRecord, Whisper


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


def test_sqlite_connections_set_busy_timeout(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "store.db")

    with store._connect() as connection:
        timeout_ms = connection.execute("PRAGMA busy_timeout").fetchone()[0]

    assert timeout_ms >= 5000


def test_sqlite_embedding_column_added_via_schema_migration(tmp_path: Path) -> None:
    store_path = tmp_path / "store.db"
    SQLiteStore(store_path)

    import sqlite3
    connection = sqlite3.connect(store_path)
    cols = {row[1] for row in connection.execute("PRAGMA table_info(chunks)").fetchall()}
    connection.close()

    assert "embedding" in cols, f"columns: {cols}"


def test_sqlite_embedding_defaults_to_none_when_adapter_not_configured(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "store.db")
    chunk = SubconsciousChunk(
        chunk_id="sub_emb_none",
        layer="semantic",
        content="default embedding test",
        src="tool_result",
    )
    assert store.write(chunk) is True

    import sqlite3
    connection = sqlite3.connect(store.path)
    connection.row_factory = sqlite3.Row
    row = connection.execute(
        "SELECT embedding FROM chunks WHERE chunk_id = ?", ("sub_emb_none",)
    ).fetchone()
    connection.close()
    assert row["embedding"] is None


def test_sqlite_embedding_stores_and_retrieves_blob(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "store.db")
    vec = [0.1, 0.2, 0.3]
    chunk = SubconsciousChunk(
        chunk_id="sub_emb_stored",
        layer="semantic",
        content="embedding blob round-trip",
        src="tool_result",
        embedding=vec,
    )
    assert store.write(chunk) is True

    import sqlite3
    connection = sqlite3.connect(store.path)
    connection.row_factory = sqlite3.Row
    row = connection.execute(
        "SELECT embedding FROM chunks WHERE chunk_id = ?", ("sub_emb_stored",)
    ).fetchone()
    connection.close()
    import json
    loaded = json.loads(row["embedding"].decode("utf-8"))
    assert loaded == vec


def test_sqlite_vector_mode_ranks_by_cosine(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "store.db")
    store.write(SubconsciousChunk(
        chunk_id="sub_close", layer="semantic",
        content="close match", src="tool_result",
        embedding=[0.9, 0.1, 0.0],
    ))
    store.write(SubconsciousChunk(
        chunk_id="sub_far", layer="semantic",
        content="far match", src="tool_result",
        embedding=[0.1, 0.9, 0.0],
    ))
    results = store.query(
        "unrelated query text",
        k=2,
        min_score=0.0,
        retrieval_mode="vector",
        embedding=[0.95, 0.05, 0.0],
    )

    assert [chunk.chunk_id for chunk in results] == ["sub_close", "sub_far"]


class _TinySemanticAdapter:
    def embed(self, text: str) -> list[float]:
        lowered = text.lower()
        if any(term in lowered for term in ("feline", "cat", "sofa", "couch")):
            return [1.0, 0.0, 0.0]
        if any(term in lowered for term in ("automobile", "brake", "vehicle")):
            return [0.0, 1.0, 0.0]
        return [0.0, 0.0, 1.0]


def test_sqlite_local_embeddings_retrieve_zero_lexical_overlap_paraphrase(
    tmp_path: Path,
) -> None:
    disabled = SQLiteStore(tmp_path / "disabled.db")
    enabled = SQLiteStore(tmp_path / "enabled.db", embedding_adapter=_TinySemanticAdapter())
    for store in (disabled, enabled):
        store.write(
            SubconsciousChunk(
                chunk_id="sub_animal",
                layer="semantic",
                content="feline lounges on sofa",
                src="tool_result",
            )
        )
        store.write(
            SubconsciousChunk(
                chunk_id="sub_vehicle",
                layer="semantic",
                content="automobile brake repair",
                src="tool_result",
            )
        )

    assert disabled.query("cat rests couch", k=2, min_score=0.01) == []

    results = enabled.query("cat rests couch", k=2, min_score=0.01)
    assert results
    assert results[0].chunk_id == "sub_animal"
    assert results[0].embedding == [1.0, 0.0, 0.0]


def test_sqlite_store_preserves_default_behavior_when_no_embedding(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "store.db")
    store.write(SubconsciousChunk(
        chunk_id="sub_default_hybrid", layer="semantic",
        content="standard retrieval content for testing",
        src="tool_result",
    ))
    results = store.query("standard retrieval", k=4, min_score=0.0)
    assert len(results) == 1
    assert results[0].chunk_id == "sub_default_hybrid"
    assert results[0].relevance > 0.0


def test_sqlite_rewrite_preserves_feedback_counters_and_created_at(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "store.db")
    original = SubconsciousChunk(
        chunk_id="sub_rewrite",
        layer="semantic",
        content="first write about retry accounting",
        src="tool_result",
        pipeline_id="pipe_1",
    )
    replacement = SubconsciousChunk(
        chunk_id="sub_rewrite",
        layer="semantic",
        content="replacement write about billing reconciliation",
        src="tool_result",
        pipeline_id="pipe_1",
        base_trust=0.8,
    )
    assert store.write(original) is True
    with store._connect() as connection:
        connection.execute(
            "UPDATE chunks SET created_at = ?, retrieval_count = ?, dissent_count = ? WHERE chunk_id = ?",
            (123.0, 7, 2, "sub_rewrite"),
        )

    assert store.write(replacement) is True

    with store._connect() as connection:
        row = connection.execute(
            "SELECT content, base_trust, created_at, retrieval_count, dissent_count "
            "FROM chunks WHERE chunk_id = ?",
            ("sub_rewrite",),
        ).fetchone()
    assert row["content"] == replacement.content
    assert row["base_trust"] == pytest.approx(0.8)
    assert row["created_at"] == pytest.approx(123.0)
    assert row["retrieval_count"] == 7
    assert row["dissent_count"] == 2


def test_sqlite_concurrent_unique_writes_do_not_raise_store_unavailable(tmp_path: Path) -> None:
    store_path = tmp_path / "store.db"
    SQLiteStore(store_path)

    def write_one(index: int) -> bool:
        store = SQLiteStore(store_path)
        return store.write(
            SubconsciousChunk(
                chunk_id=f"sub_concurrent_{index}",
                layer="semantic",
                content=" ".join(f"unique_concurrent_{index}_{token}" for token in range(20)),
                src="tool_result",
                pipeline_id="pipe_1",
            )
        )

    with ThreadPoolExecutor(max_workers=8) as executor:
        results = list(executor.map(write_one, range(24)))

    assert all(results)
    restarted = SQLiteStore(store_path)
    with restarted._connect() as connection:
        row = connection.execute(
            "SELECT COUNT(*) AS count FROM chunks WHERE pipeline_id = ?",
            ("pipe_1",),
        ).fetchone()
    assert row["count"] == 24


def test_sqlite_store_query_filters_zero_score_noise_and_uses_effective_score(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "store.db")
    store.write(
        SubconsciousChunk(
            chunk_id="sub_low_trust",
            layer="semantic",
            content="bearer token failure handling shared ranking content",
            src="tool_result",
            pipeline_id="pipe_1",
            written_by="executor",
            base_trust=0.4,
        )
    )
    store.write(
        SubconsciousChunk(
            chunk_id="sub_high_trust",
            layer="procedural",
            content="bearer token failure handling shared ranking content",
            src="tool_result",
            pipeline_id="pipe_1",
            written_by="planner",
            base_trust=0.9,
        )
    )
    store.write(
        SubconsciousChunk(
            chunk_id="sub_noise",
            layer="semantic",
            content="database schema migration notes",
            src="tool_result",
            pipeline_id="pipe_1",
            written_by="critic",
        )
    )

    results = store.query("bearer token failure", pipeline_id="pipe_1", k=4)
    off_topic = store.query("unrelated astronomy orbit", pipeline_id="pipe_1", k=4)
    blank_query = store.query("   ", pipeline_id="pipe_1", k=4)

    assert [chunk.chunk_id for chunk in results] == ["sub_high_trust", "sub_low_trust"]
    assert all(chunk.relevance > 0.0 for chunk in results)
    assert off_topic == []
    assert blank_query[0].chunk_id == "sub_high_trust"
    assert {chunk.chunk_id for chunk in blank_query} >= {"sub_low_trust", "sub_noise"}


def test_sqlite_store_duplicate_write_is_skipped(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "store.db")
    chunk = SubconsciousChunk(
        layer="episodic",
        content="same content for duplicate detection",
        src="synthesis",
    )

    assert store.write(chunk) is True
    assert store.write(chunk.model_copy(update={"chunk_id": "sub_duplicate"})) is False


def test_sqlite_store_dedup_scan_is_bounded_for_non_working_zones(tmp_path: Path) -> None:
    """docs/NCP_SILENT_DISCONNECT_AUDIT.md finding 10: _is_duplicate's scan
    must not be unbounded for "proven"/"global" zones, which (unlike
    "working") are never capped by _hard_gc/retention -- an unbounded scan
    there made every write to a long-lived non-working pipeline strictly
    slower forever. This asserts the bound is real by proving it changes
    behavior: a near-duplicate pushed outside the (small, configured)
    lookback window is no longer detected, while one still inside the
    window is."""
    config = NCPConfig(values={"retention": {"dedup_scan_limit": 5}}, project_root=Path("."))
    store = SQLiteStore(tmp_path / "store.db", config=config)
    far_future_expiry = time.time() + 100_000
    # Mutually dissimilar topics (SequenceMatcher ratio well under the 0.92
    # near-duplicate cutoff between any two) so only an exact/near-exact
    # repeat of the *same* topic ever counts as a duplicate below.
    topics = [
        "mountain glacier erosion patterns over centuries",
        "vintage jazz recording studio acoustics",
        "coral reef bleaching ocean temperature correlation",
        "medieval manuscript illumination pigment chemistry",
        "high altitude cargo drone battery thermal limits",
        "sourdough starter fermentation yeast culture",
        "urban rail transit signal interlocking design",
        "desert cactus root water storage adaptation",
        "orbital debris tracking radar cross section",
        "handmade ceramic glaze firing temperature curves",
        "beekeeping colony winter survival strategies",
        "volcanic ash soil fertility agricultural impact",
    ]

    def _filler(i: int) -> SubconsciousChunk:
        return SubconsciousChunk(
            chunk_id=f"sub_filler_{i}",
            layer="semantic",
            content=topics[i],
            src="tool_result",
            pipeline_id="pipe_dedup",
            zone="global",
            expiry=far_future_expiry,
        )

    first = _filler(0)
    assert store.write(first) is True
    # Push `first` outside the 5-row scan window with unrelated writes.
    for i in range(1, 12):
        assert store.write(_filler(i)) is True
    last = _filler(11)

    # A near-duplicate of the now-11-writes-old `first` chunk is outside the
    # bounded scan window -- write succeeds (not silently treated as dup).
    stale_near_duplicate = first.model_copy(update={"chunk_id": "sub_stale_dup"})
    assert store.write(stale_near_duplicate) is True

    # A near-duplicate of the most recent chunk is still inside the window
    # -- dedup detection is not just disabled, it still works where it should.
    fresh_near_duplicate = last.model_copy(update={"chunk_id": "sub_fresh_dup"})
    assert store.write(fresh_near_duplicate) is False


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
            whisper_type="nudge",
            payload="low_conf_signal",
            confidence=0.2,
            pipeline_id="pipe_1",
        )
    )

    drained = store.drain_whispers(agent_id="executor", pipeline_id="pipe_1")

    assert [whisper.payload for whisper in drained] == ["check_tests"]
    assert store.drain_whispers(agent_id="executor", pipeline_id="pipe_1") == []


def test_broadcast_whisper_delivery_is_per_recipient(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "broadcast.db")
    store.emit_whisper(
        Whisper(
            whisper_id="wsp_broadcast",
            from_agent="planner",
            target="*",
            whisper_type="share",
            payload='{"ask":"review"}',
            confidence=0.9,
            pipeline_id="pipe_1",
        )
    )

    assert [w.whisper_id for w in store.drain_whispers(agent_id="executor", pipeline_id="pipe_1")] == ["wsp_broadcast"]
    assert store.drain_whispers(agent_id="executor", pipeline_id="pipe_1") == []
    assert [w.whisper_id for w in store.drain_whispers(agent_id="reviewer", pipeline_id="pipe_1")] == ["wsp_broadcast"]


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


def test_sqlite_store_recent_turns_returns_oldest_first_within_pipeline(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "store.db")
    for index, created_at in enumerate((100.0, 200.0, 300.0, 400.0)):
        store.log_turn_record(
            TurnRecord(
                turn_id=f"turn_{index}",
                agent_id="planner",
                pipeline_id="pipe_recent",
                task="task",
                slot="slot",
                result=f"result_{index}",
                result_full="full",
                created_at=created_at,
                expires_at=created_at + 1000,
            )
        )
    # A turn from a different pipeline must not leak into recent_turns.
    store.log_turn_record(
        TurnRecord(
            turn_id="turn_other_pipeline",
            agent_id="planner",
            pipeline_id="pipe_other",
            task="task",
            slot="slot",
            result="result_other",
            result_full="full",
            created_at=350.0,
            expires_at=1350.0,
        )
    )

    all_turns = store.recent_turns(pipeline_id="pipe_recent", limit=20)
    assert [t.turn_id for t in all_turns] == ["turn_0", "turn_1", "turn_2", "turn_3"]

    limited = store.recent_turns(pipeline_id="pipe_recent", limit=2)
    assert [t.turn_id for t in limited] == ["turn_2", "turn_3"]

    assert store.recent_turns(pipeline_id="pipe_nonexistent", limit=20) == []
    assert store.recent_turns(pipeline_id="pipe_recent", limit=0) == []


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


def test_sqlite_store_written_by_is_immutable_for_existing_chunk_id(tmp_path: Path) -> None:
    """Security fix (docs/NCP_SILENT_DISCONNECT_AUDIT.md finding 9): before
    this fix, reusing an existing chunk_id with a different written_by
    silently overwrote that chunk's content/author in place while keeping
    src (and therefore trust) unchanged -- a forgery vector, since chunk_id
    is returned from every write and appears in every retrieval result."""
    store = SQLiteStore(tmp_path / "store.db")
    chunk = SubconsciousChunk(
        chunk_id="sub_author_lock",
        layer="semantic",
        content="victim's original content",
        src="user_verified",
        written_by="victim_agent",
    )
    store.write(chunk)

    with pytest.raises(ValueError, match="written_by is immutable"):
        store.write(
            chunk.model_copy(update={"content": "forged content", "written_by": "attacker_agent"})
        )

    # The original row must be completely untouched by the rejected write.
    (persisted,) = store.query(text="victim's original content", k=1)
    assert persisted.content == "victim's original content"
    assert persisted.written_by == "victim_agent"

    # A same-author update to their own chunk_id must still work.
    ok = store.write(chunk.model_copy(update={"content": "victim's updated content"}))
    assert ok is True
    (updated,) = store.query(text="victim's updated content", k=1)
    assert updated.written_by == "victim_agent"


def test_sqlite_store_supersede_rejects_cross_pipeline_target(tmp_path: Path) -> None:
    """Security fix (docs/NCP_SILENT_DISCONNECT_AUDIT.md finding 9): before
    this fix, supersede() had no ownership check at all, so any caller could
    pass an arbitrary chunk_id from a different pipeline and silently
    retire/hide it (superseded chunks are excluded from default queries)."""
    store = SQLiteStore(tmp_path / "store.db")
    victim = SubconsciousChunk(
        chunk_id="sub_victim", layer="semantic", content="victim fact",
        src="user_verified", written_by="victim_agent", pipeline_id="pipeline_A",
    )
    store.write(victim)
    attacker_new = SubconsciousChunk(
        chunk_id="sub_attacker_new", layer="semantic", content="attacker fact",
        src="user_verified", written_by="attacker_agent", pipeline_id="pipeline_B",
    )
    store.write(attacker_new)

    assert store.supersede("sub_victim", "sub_attacker_new") is False
    (still_there,) = store.query(text="victim fact", k=1, pipeline_id="pipeline_A")
    assert still_there.superseded_by is None

    # Same-pipeline supersede must still work.
    victim_new = SubconsciousChunk(
        chunk_id="sub_victim_correction", layer="semantic", content="corrected victim fact",
        src="user_verified", written_by="victim_agent", pipeline_id="pipeline_A",
    )
    store.write(victim_new)
    assert store.supersede("sub_victim", "sub_victim_correction") is True


def test_sqlite_store_add_chunk_edges_rejects_cross_pipeline_dst(tmp_path: Path) -> None:
    """Security fix (docs/NCP_SILENT_DISCONNECT_AUDIT.md finding 9): before
    this fix, add_chunk_edges() had no ownership check, so any caller could
    attach an edge (e.g. 'contradicts') to a chunk from a different pipeline."""
    from ncp.types import ChunkEdge

    store = SQLiteStore(tmp_path / "store.db")
    victim = SubconsciousChunk(
        chunk_id="sub_edge_victim", layer="semantic", content="victim fact",
        src="user_verified", written_by="victim_agent", pipeline_id="pipeline_A",
    )
    store.write(victim)
    attacker = SubconsciousChunk(
        chunk_id="sub_edge_attacker", layer="semantic", content="attacker fact",
        src="user_verified", written_by="attacker_agent", pipeline_id="pipeline_B",
    )
    store.write(attacker)

    written = store.add_chunk_edges([
        ChunkEdge(src_chunk_id="sub_edge_attacker", dst_chunk_id="sub_edge_victim", edge_type="contradicts")
    ])
    assert written == 0
    assert store.get_chunk_edges(["sub_edge_victim"], direction="in") == []

    # Same-pipeline edges must still work.
    ally = SubconsciousChunk(
        chunk_id="sub_edge_ally", layer="semantic", content="ally fact",
        src="user_verified", written_by="victim_agent", pipeline_id="pipeline_A",
    )
    store.write(ally)
    written_ok = store.add_chunk_edges([
        ChunkEdge(src_chunk_id="sub_edge_ally", dst_chunk_id="sub_edge_victim", edge_type="supports")
    ])
    assert written_ok == 1


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
        payload=DissentPayload(issue="safe"),
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


def test_sqlite_expired_chunk_absent_from_reads_and_gc(tmp_path: Path) -> None:
    # WI-006: a chunk past its expiry must not be retrieved, fetched by id, or
    # listed in the working zone, and GC must reclaim it from the table.
    import sqlite3
    import time

    store_path = tmp_path / "store.db"
    store = SQLiteStore(store_path)

    expired = SubconsciousChunk(
        chunk_id="sub_expired",
        layer="procedural",
        content="stale proven fact about bearer token rotation",
        src="tool_result",
        pipeline_id="pipe_exp",
        expiry=time.time() - 3600,
    )
    live = SubconsciousChunk(
        chunk_id="sub_live",
        layer="procedural",
        content="current fact about bearer token rotation",
        src="tool_result",
        pipeline_id="pipe_exp",
        expiry=time.time() + 3600,
    )
    assert store.write(expired) is True
    assert store.write(live) is True

    # Not retrievable via query.
    ids = [c.chunk_id for c in store.query("bearer token rotation", pipeline_id="pipe_exp")]
    assert "sub_expired" not in ids
    assert "sub_live" in ids

    # Not fetchable by id.
    fetched = {c.chunk_id for c in store.get_chunks_by_ids(["sub_expired", "sub_live"])}
    assert fetched == {"sub_live"}

    # Not present in the working zone.
    zone_ids = {c.chunk_id for c in store.get_working_zone(pipeline_id="pipe_exp")}
    assert "sub_expired" not in zone_ids

    # A soft-GC-triggering write physically reclaims the expired row.
    store.write(SubconsciousChunk(
        chunk_id="sub_trigger", layer="procedural",
        content="unrelated trigger chunk", src="tool_result", pipeline_id="pipe_exp",
    ))
    connection = sqlite3.connect(store_path)
    remaining = {
        row[0]
        for row in connection.execute("SELECT chunk_id FROM chunks").fetchall()
    }
    connection.close()
    assert "sub_expired" not in remaining
