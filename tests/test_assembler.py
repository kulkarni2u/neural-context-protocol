from pathlib import Path

import pytest

from ncp.assembler import Assembler
from ncp.config import load_config
from ncp.tokens import estimate_tokens
from ncp.stores.sqlite import SQLiteStore
from ncp.types import BudgetContext, ConsciousBlock, NCPResponse, SubconsciousChunk, TurnRecord, Whisper


def _make_conscious(**overrides: object) -> ConsciousBlock:
    base = {
        "agent_id": "executor",
        "role": "build",
        "owns": ["implementation"],
        "must_not": ["planning"],
        "task": "implement_store",
        "slot": "assemble_context",
        "intent": "build_local_dogfood",
        "pipeline_id": "pipe_1",
    }
    base.update(overrides)
    return ConsciousBlock(**base)


def test_assembler_builds_context_from_store_and_queue(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "store.db")
    store.write(
        SubconsciousChunk(
            chunk_id="sub_store",
            layer="procedural",
            content="implement_store assemble_context persists chunks and resolves whispers",
            src="tool_result",
            pipeline_id="pipe_1",
        )
    )
    store.emit_whisper(
        Whisper(
            from_agent="critic",
            target="executor",
            whisper_type="nudge",
            payload="verify_restart_path",
            confidence=0.9,
            pipeline_id="pipe_1",
        )
    )

    assembler = Assembler(store=store)
    result = assembler.assemble(
        conscious=_make_conscious(),
        budget=BudgetContext(ctx_used=0.2, steps_completed=1, steps_total=3, elapsed_seconds=10),
    )

    assert "[NCP:BUDGET]" in result.context
    assert "[NCP:CONSCIOUS]" in result.context
    assert "[NCP:SUBCONSCIOUS]" in result.context
    assert "[NCP:WHISPERS]" in result.context
    assert any(chunk.chunk_id == "sub_store" for chunk in result.chunks)
    assert any(whisper.payload == "verify_restart_path" for whisper in result.whispers)


def test_assembler_resolves_recent_refs_and_reduces_critical_pressure(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "store.db")
    for index in range(4):
        store.write(
            SubconsciousChunk(
                chunk_id=f"sub_{index}",
                layer="episodic",
                content=f"retrieved chunk {index}",
                src="tool_result",
                pipeline_id="pipe_1",
            )
        )
    for index in range(3):
        store.emit_whisper(
            Whisper(
                from_agent="planner",
                target="executor",
                whisper_type="nudge",
                payload=f"nudge_{index}",
                confidence=0.9,
                pipeline_id="pipe_1",
            )
        )

    assembler = Assembler(store=store)
    record = assembler.post_turn(
        conscious=_make_conscious(),
        response=NCPResponse(
            content="done",
            input_tokens=10,
            output_tokens=20,
            cost_usd=0.01,
            model="claude_sonnet",
            pipeline_id="pipe_1",
            turn_id="turn_recent",
            latency_ms=100,
        ),
        result_summary="recent_summary",
        result_full="recent_full",
    )
    result = assembler.assemble(
        conscious=_make_conscious(
            recent=[f"r:sub/{record.turn_id}"],
            slot_age=6,
            slot_confidence=0.4,
            drift_score=0.35,
        ),
        budget=BudgetContext(pressure="critical"),
    )

    assert len(result.chunks) == 2
    assert len(result.whispers) == 2
    assert any(chunk.chunk_id.startswith("recent_") for chunk in result.chunks)
    assert result.whispers[0].whisper_type == "alert"
    assert result.whispers[1].whisper_type == "nudge"
    assert result.pending_whisper_ids == [result.whispers[1].whisper_id]


def test_recent_refs_do_not_crowd_out_matching_retrieved_chunk(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "recent-budget.db")
    assembler = Assembler(store=store)
    store.write(
        SubconsciousChunk(
            chunk_id="golden_fact",
            layer="semantic",
            content="golden_fact async vector mode uses ivfflat_probes and cosine ordering",
            src="user_verified",
            pipeline_id="pipe_1",
            written_by="reviewer",
            relevance=0.95,
        )
    )
    recent_refs: list[str] = []
    for index in range(5):
        record = assembler.post_turn(
            conscious=_make_conscious(),
            response=NCPResponse(
                content=f"recent summary {index}",
                input_tokens=10,
                output_tokens=5,
                cost_usd=0.0,
                model="benchmark_local",
                pipeline_id="pipe_1",
                turn_id=f"turn_recent_budget_{index}",
                latency_ms=1,
            ),
            result_summary=f"recent own-turn summary {index} without the golden retrieval terms",
            result_full=f"recent own-turn summary {index} without the golden retrieval terms",
        )
        recent_refs.insert(0, f"r:sub/{record.turn_id}")

    result = assembler.assemble(
        conscious=_make_conscious(recent=recent_refs),
        budget=BudgetContext(pressure="medium"),
        query_text="golden_fact ivfflat_probes cosine ordering",
    )

    assert len(result.chunks) <= 4
    assert sum(chunk.chunk_id.startswith("recent_") for chunk in result.chunks) <= 2
    assert any(chunk.chunk_id == "golden_fact" for chunk in result.chunks)


def test_stale_recent_refs_lose_to_higher_scored_retrieved_chunks(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "recent-policy.db")
    assembler = Assembler(store=store)
    store.log_turn_record(
        TurnRecord(
            turn_id="turn_stale_a",
            agent_id="executor",
            pipeline_id="pipe_1",
            task="implement_store",
            slot="assemble_context",
            result="old own-turn summary without policy retrieval keywords",
            result_full="old own-turn summary without policy retrieval keywords",
            created_at=1.0,
        )
    )
    store.log_turn_record(
        TurnRecord(
            turn_id="turn_stale_b",
            agent_id="executor",
            pipeline_id="pipe_1",
            task="implement_store",
            slot="assemble_context",
            result="another old summary with unrelated status notes",
            result_full="another old summary with unrelated status notes",
            created_at=2.0,
        )
    )

    def _query(*args: object, **kwargs: object) -> list[SubconsciousChunk]:
        return [
            SubconsciousChunk(
                chunk_id="retrieved_policy_fact",
                layer="semantic",
                content="retrieval policy should score recent refs instead of reserving slots",
                src="user_verified",
                pipeline_id="pipe_1",
                relevance=0.9,
            ),
            SubconsciousChunk(
                chunk_id="retrieved_budget_fact",
                layer="semantic",
                content="recent slot budget is a cap when retrieved chunks are stronger",
                src="tool_result",
                pipeline_id="pipe_1",
                relevance=0.8,
            ),
        ]

    store.query = _query  # type: ignore[method-assign]

    result = assembler.assemble(
        conscious=_make_conscious(recent=["r:sub/turn_stale_a", "r:sub/turn_stale_b"]),
        budget=BudgetContext(pressure="medium"),
        query_text="retrieval policy score recent refs budget",
        k=2,
    )

    assert [chunk.chunk_id for chunk in result.chunks] == ["retrieved_policy_fact", "retrieved_budget_fact"]


def test_assembler_reports_evicted_high_relevance_chunks(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "evict.db")
    assembler = Assembler(store=store)
    recent_refs: list[str] = []
    for index in range(3):
        record = assembler.post_turn(
            conscious=_make_conscious(),
            response=NCPResponse(
                content=f"result_{index}",
                input_tokens=10,
                output_tokens=5,
                cost_usd=0.0,
                model="gpt_4_1",
                pipeline_id="pipe_1",
                turn_id=f"turn_evict_{index}",
                latency_ms=1,
            ),
            result_summary=f"needle constraint_{index} preserve this exact benchmark fact",
            result_full=f"needle constraint_{index} preserve this exact benchmark fact",
        )
        recent_refs.append(f"r:sub/{record.turn_id}")

    result = assembler.assemble(
        conscious=_make_conscious(recent=recent_refs),
        budget=BudgetContext(pressure="medium"),
        query_text="needle constraint preserve exact benchmark fact",
        k=1,
    )

    assert len(result.chunks) == 1
    assert result.evicted_high_relevance
    evicted_ids = {chunk_id for chunk_id, _ in result.evicted_high_relevance}
    assert evicted_ids
    assert all(relevance >= 0.5 for _, relevance in result.evicted_high_relevance)


def test_eviction_telemetry_reports_retrieved_chunk_evicted_by_tight_recent_budget(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "retrieved-evicted.db")
    assembler = Assembler(store=store)
    store.write(
        SubconsciousChunk(
            chunk_id="retrieved_high_relevance",
            layer="semantic",
            content="retrieved_high_relevance retention telemetry golden keyword",
            src="user_verified",
            pipeline_id="pipe_1",
            relevance=0.95,
        )
    )
    recent_refs: list[str] = []
    record = assembler.post_turn(
        conscious=_make_conscious(),
        response=NCPResponse(
            content="recent",
            input_tokens=5,
            output_tokens=5,
            cost_usd=0.0,
            model="benchmark_local",
            pipeline_id="pipe_1",
            turn_id="turn_evicted_retrieved",
            latency_ms=1,
        ),
        result_summary="retrieved_high_relevance retention telemetry golden keyword unique_recent_signal",
        result_full="retrieved_high_relevance retention telemetry golden keyword unique_recent_signal",
    )
    recent_refs.insert(0, f"r:sub/{record.turn_id}")

    def _query(*args: object, **kwargs: object) -> list[SubconsciousChunk]:
        return [
            SubconsciousChunk(
                chunk_id="retrieved_high_relevance",
                layer="semantic",
                content="retrieved_high_relevance retention telemetry golden keyword",
                src="user_verified",
                pipeline_id="pipe_1",
                relevance=0.8,
            )
        ]

    store.query = _query  # type: ignore[method-assign]

    result = assembler.assemble(
        conscious=_make_conscious(recent=recent_refs),
        budget=BudgetContext(pressure="medium"),
        query_text="unique_recent_signal",
        k=1,
    )

    evicted_ids = {chunk_id for chunk_id, _ in result.evicted_high_relevance}
    assert "retrieved_high_relevance" in evicted_ids


def test_assembler_does_not_report_low_relevance_chunks_as_evicted(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "evict-low.db")
    assembler = Assembler(store=store)
    store.write(
        SubconsciousChunk(
            chunk_id="low_rel_chunk",
            layer="episodic",
            content="low relevance filler that should not appear in evicted_high_relevance",
            src="synthesis",
            pipeline_id="pipe_1",
            written_by="agent",
            relevance=0.2,
        )
    )
    recent_refs: list[str] = []
    for index in range(3):
        record = assembler.post_turn(
            conscious=_make_conscious(),
            response=NCPResponse(
                content=f"result_{index}",
                input_tokens=10,
                output_tokens=5,
                cost_usd=0.0,
                model="gpt_4_1",
                pipeline_id="pipe_1",
                turn_id=f"turn_lowrel_{index}",
                latency_ms=1,
            ),
            result_summary=f"constraint_{index} preserve this fact",
            result_full=f"constraint_{index} preserve this fact",
        )
        recent_refs.append(f"r:sub/{record.turn_id}")

    result = assembler.assemble(
        conscious=_make_conscious(recent=recent_refs),
        budget=BudgetContext(pressure="medium"),
        k=1,
    )

    evicted_ids = {chunk_id for chunk_id, _ in result.evicted_high_relevance}
    assert "low_rel_chunk" not in evicted_ids


def test_assembler_reports_empty_evicted_whispers_when_nothing_dropped(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "no-evict.db")
    assembler = Assembler(store=store)

    result = assembler.assemble(
        conscious=_make_conscious(),
        budget=BudgetContext(pressure="medium"),
    )

    assert result.evicted_whispers == []


def test_assemble_max_tokens_bounds_rendered_context(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "token-budget.db")
    for index in range(4):
        store.write(
            SubconsciousChunk(
                chunk_id=f"large_{index}",
                layer="semantic",
                content=" ".join(["implement_store assemble_context"] + [f"term_{index}_{j}" for j in range(120)]),
                src="tool_result",
                pipeline_id="pipe_1",
            )
        )
    assembler = Assembler(store=store)

    result = assembler.assemble(
        conscious=_make_conscious(),
        budget=BudgetContext(pressure="medium"),
        query_text="implement_store assemble_context",
        max_tokens=200,
    )

    assert estimate_tokens(result.context) <= 200
    assert "[NCP:BUDGET]" in result.context
    assert "[NCP:CONSCIOUS]" in result.context


def test_token_budget_reports_high_relevance_chunks_it_drops(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "token-budget-eviction.db")
    chunks = [
            SubconsciousChunk(
                chunk_id=f"large_high_rel_{index}",
                layer="semantic",
                content=" ".join(["implement_store assemble_context"] + [f"term_{index}_{j}" for j in range(160)]),
                src="tool_result",
                pipeline_id="pipe_1",
                relevance=0.95,
            )
        for index in range(2)
    ]

    def _query(*args: object, **kwargs: object) -> list[SubconsciousChunk]:
        return chunks

    store.query = _query  # type: ignore[method-assign]
    assembler = Assembler(store=store)

    result = assembler.assemble(
        conscious=_make_conscious(),
        budget=BudgetContext(pressure="medium"),
        query_text="implement_store assemble_context",
        max_tokens=200,
    )

    evicted_ids = {chunk_id for chunk_id, _ in result.evicted_high_relevance}
    assert evicted_ids


def test_assembler_reports_evicted_whispers_without_draining_queue(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "whispers.db")
    store.emit_whisper(
        Whisper(
            from_agent="planner",
            target="executor",
            whisper_type="nudge",
            payload="follow_up_review",
            confidence=0.9,
            pipeline_id="pipe_1",
        )
    )

    assembler = Assembler(store=store)
    result = assembler.assemble(
        conscious=_make_conscious(slot_age=6, slot_confidence=0.4, drift_score=0.35),
        budget=BudgetContext(pressure="critical"),
    )

    assert len(result.whispers) == 2
    assert result.whispers[0].whisper_type == "alert"
    assert result.whispers[1].payload == "follow_up_review"
    assert result.pending_whisper_ids == [result.whispers[1].whisper_id]
    assert result.evicted_whispers
    assert any(confidence >= 0.6 for _, confidence in result.evicted_whispers)

    remaining = store.drain_whispers(agent_id="executor", pipeline_id="pipe_1", max_items=5)
    assert any(whisper.payload == "follow_up_review" for whisper in remaining)


def test_assembler_post_turn_logs_cost_and_memory_chunks(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "store.db")
    assembler = Assembler(store=store)
    response = NCPResponse(
        content="done",
        input_tokens=50,
        output_tokens=25,
        cost_usd=0.02,
        model="gpt_4_1",
        pipeline_id="pipe_1",
        turn_id="turn_post",
        latency_ms=200,
    )

    record = assembler.post_turn(
        conscious=_make_conscious(),
        response=response,
        result_summary="summary",
        result_full="full output",
        memory_chunks=[
            SubconsciousChunk(
                chunk_id="sub_memory",
                layer="semantic",
                content="remember this output",
                src="synthesis",
                pipeline_id="pipe_1",
            )
        ],
    )

    assert record.turn_id == "turn_post"
    assert store.resolve_recent_ref("r:sub/turn_post") is not None
    assert any(chunk.chunk_id == "sub_memory" for chunk in store.query("remember output", pipeline_id="pipe_1"))
    assert store.status()["cost_usd_total"] == 0.02


def test_apply_post_middleware_invokes_registered_transformations(tmp_path: Path) -> None:
    from ncp.middleware.base import Middleware, MiddlewarePipeline

    class _TagMiddleware(Middleware):
        def post_assemble(self, context: str) -> str:
            return context + "[TAGGED]"

    store = SQLiteStore(tmp_path / "test.db")
    pipeline = MiddlewarePipeline()
    pipeline.add(_TagMiddleware())
    assembler = Assembler(store=store, middleware=pipeline)
    result = assembler.apply_post_middleware("hello ncp world")
    assert result == "hello ncp world[TAGGED]"


# ---------------------------------------------------------------------------
# Regression: cold-start bootstrap must truncate, not crash, on a long
# task/intent (silent-disconnect audit finding 9)
# ---------------------------------------------------------------------------


def test_cold_start_bootstrap_truncates_long_task_intent_instead_of_crashing(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "store.db")
    assembler = Assembler(store=store)
    conscious = ConsciousBlock(
        agent_id="agentA",
        role="worker",
        owns=[],
        must_not=[],
        task="x" * 1200,
        slot="s",
        intent="y" * 1200,
        pipeline_id="pipe_empty_cold2",
    )

    # Must not raise a pydantic ValidationError even though the raw
    # interpolated f-string would exceed SubconsciousChunk's 2000-char cap.
    result = assembler.assemble(
        conscious=conscious,
        budget=BudgetContext(),
        query_text="anything",
        max_tokens=5000,
    )

    assert len(result.chunks) == 1
    cold_chunk = result.chunks[0]
    assert len(cold_chunk.content) <= 2000
    assert cold_chunk.content.startswith("pipeline_summary agent:agentA")


def test_cold_start_bootstrap_leaves_short_task_intent_untouched(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "store.db")
    assembler = Assembler(store=store)
    conscious = _make_conscious(pipeline_id="pipe_empty_cold_short")

    result = assembler.assemble(
        conscious=conscious,
        budget=BudgetContext(),
        query_text="anything",
        max_tokens=5000,
    )

    assert len(result.chunks) == 1
    assert result.chunks[0].content == (
        f"pipeline_summary agent:{conscious.agent_id} task:{conscious.task} intent:{conscious.intent}"
    )


# ---------------------------------------------------------------------------
# Regression: supersession suppression must run regardless of edge_expansion
# (silent-disconnect audit finding 9)
# ---------------------------------------------------------------------------


def test_supersession_suppressed_even_when_edge_expansion_disabled(tmp_path: Path) -> None:
    project = tmp_path / "repo"
    (project / ".git").mkdir(parents=True)
    config = load_config(env={"NCP_EDGE_EXPANSION": "false", "NCP_STORE_PATH": str(project / "s.db")})
    store = SQLiteStore(project / "s.db")
    store.write(
        SubconsciousChunk(
            chunk_id="fact_v1",
            layer="episodic",
            content="apply_guard fix_npe old fact",
            src="agent_inferred",
            pipeline_id="pipe_1",
        )
    )
    store.write(
        SubconsciousChunk(
            chunk_id="fact_v2",
            layer="episodic",
            content="apply_guard fix_npe new fact",
            src="agent_inferred",
            supersedes="fact_v1",
            pipeline_id="pipe_1",
        )
    )

    assembler = Assembler(store=store, config=config)
    result = assembler.assemble(
        conscious=_make_conscious(
            agent_id="fixer",
            task="fix_npe",
            slot="apply_guard",
            intent="resolve_bug",
            pipeline_id="pipe_1",
        ),
        budget=BudgetContext(ctx_used=0.2, steps_completed=1, steps_total=3, elapsed_seconds=5),
    )

    ids = {chunk.chunk_id for chunk in result.chunks}
    assert "fact_v2" in ids
    assert "fact_v1" not in ids


# ---------------------------------------------------------------------------
# Regression: an out-of-range world_check whisper must not block later,
# well-formed world_check whispers from updating drift_score
# (silent-disconnect audit finding 9)
# ---------------------------------------------------------------------------


def test_apply_drift_feedback_skips_out_of_range_signal_and_applies_next_valid(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "store.db")
    assembler = Assembler(store=store)
    conscious = _make_conscious(drift_score=0.0)

    out_of_range = Whisper(
        from_agent="sensorA",
        target="executor",
        whisper_type="world_check",
        payload={"anchor_intent": "i", "detected_drift": 5.0},
        confidence=0.9,
        pipeline_id="pipe_1",
    )
    valid = Whisper(
        from_agent="sensorB",
        target="executor",
        whisper_type="world_check",
        payload={"anchor_intent": "i", "detected_drift": 0.85},
        confidence=0.9,
        pipeline_id="pipe_1",
    )

    updated = assembler._apply_drift_feedback(conscious, [out_of_range, valid])

    assert updated.drift_score == pytest.approx(0.85)


def test_apply_drift_feedback_stops_at_first_applied_valid_signal(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "store.db")
    assembler = Assembler(store=store)
    conscious = _make_conscious(drift_score=0.0)

    first_valid = Whisper(
        from_agent="sensorA",
        target="executor",
        whisper_type="world_check",
        payload={"anchor_intent": "i", "detected_drift": 0.4},
        confidence=0.9,
        pipeline_id="pipe_1",
    )
    second_valid = Whisper(
        from_agent="sensorB",
        target="executor",
        whisper_type="world_check",
        payload={"anchor_intent": "i", "detected_drift": 0.9},
        confidence=0.9,
        pipeline_id="pipe_1",
    )

    updated = assembler._apply_drift_feedback(conscious, [first_valid, second_valid])

    # First well-formed, in-range signal wins; loop still stops there.
    assert updated.drift_score == pytest.approx(0.4)
