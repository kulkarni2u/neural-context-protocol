from pathlib import Path

from ncp.assembler import Assembler
from ncp.stores.sqlite import SQLiteStore
from ncp.types import BudgetContext, ConsciousBlock, NCPResponse, SubconsciousChunk, Whisper


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
            content="store persists chunks and resolves whispers",
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
    assert len(result.whispers) == 1
    assert any(chunk.chunk_id.startswith("recent_") for chunk in result.chunks)
    assert result.whispers[0].whisper_type == "alert"


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
