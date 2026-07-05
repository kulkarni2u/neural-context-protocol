"""WI-007: stop silent data loss (three targeted tests, one per part)."""

from __future__ import annotations

from pathlib import Path

import pytest

from ncp.assembler import Assembler
from ncp.stores.sqlite import SQLiteStore
from ncp.types import ConsciousBlock, NCPResponse, SubconsciousChunk, Whisper


def _store(tmp_path: Path) -> SQLiteStore:
    return SQLiteStore(tmp_path / "store.db")


_DUP_CONTENT = "the deployment pipeline validates artifacts before every release"


def _post_turn_fixtures() -> tuple[ConsciousBlock, NCPResponse, SubconsciousChunk, SubconsciousChunk, SubconsciousChunk]:
    conscious = ConsciousBlock(
        agent_id="a", role="r", owns=[], must_not=[],
        task="t", slot="s", intent="i", pipeline_id="pipe_x",
    )
    response = NCPResponse(
        content="done", input_tokens=1, output_tokens=1, cost_usd=0.0,
        model="m", pipeline_id="pipe_x", turn_id="turn_wi007a", latency_ms=1,
    )
    seed = SubconsciousChunk(
        chunk_id="sub_seed", layer="semantic", content=_DUP_CONTENT,
        src="tool_result", pipeline_id="pipe_x",
    )
    fresh = SubconsciousChunk(
        chunk_id="sub_fresh", layer="semantic",
        content="rollbacks require a signed approval from the release captain",
        src="tool_result", pipeline_id="pipe_x",
    )
    near_dup = SubconsciousChunk(
        chunk_id="sub_dup", layer="semantic", content=_DUP_CONTENT,
        src="tool_result", pipeline_id="pipe_x",
    )
    return conscious, response, seed, fresh, near_dup


def test_write_with_retry_surfaces_dedup_suppression(tmp_path: Path) -> None:
    # WI-007(a): a deduplication-suppressed write must be reported (False),
    # not swallowed as success.
    store = _store(tmp_path)
    asm = Assembler(store=store)
    content = "the deployment pipeline validates artifacts before every release"
    first = SubconsciousChunk(
        chunk_id="sub_first", layer="semantic", content=content,
        src="tool_result", pipeline_id="pipe_x",
    )
    near_dup = SubconsciousChunk(
        chunk_id="sub_dup", layer="semantic", content=content,
        src="tool_result", pipeline_id="pipe_x",
    )
    assert asm._write_with_retry(first) is True
    assert asm._write_with_retry(near_dup) is False


def test_post_turn_reports_suppressed_chunk_ids(tmp_path: Path) -> None:
    # WI-007(a): post_turn surfaces dedup-suppressed chunk_ids on the returned
    # record; the fresh chunk still persists.
    store = _store(tmp_path)
    asm = Assembler(store=store)
    conscious, response, seed, fresh, near_dup = _post_turn_fixtures()
    assert store.write(seed) is True

    record = asm.post_turn(
        conscious=conscious, response=response,
        result_summary="summary", result_full="full",
        memory_chunks=[fresh, near_dup],
    )

    assert record.suppressed_chunk_ids == ["sub_dup"]
    persisted = store.query("rollbacks signed approval release captain", pipeline_id="pipe_x")
    assert any(chunk.chunk_id == "sub_fresh" for chunk in persisted)


@pytest.mark.anyio
async def test_post_turn_async_reports_suppressed_chunk_ids(tmp_path: Path) -> None:
    # WI-007(a): the async path surfaces the same suppression metadata even
    # though start_soon cannot return values.
    store = _store(tmp_path)
    asm = Assembler(store=store)
    conscious, response, seed, fresh, near_dup = _post_turn_fixtures()
    assert store.write(seed) is True

    record = await asm.post_turn_async(
        conscious=conscious, response=response,
        result_summary="summary", result_full="full",
        memory_chunks=[fresh, near_dup],
    )

    assert record.suppressed_chunk_ids == ["sub_dup"]
    persisted = store.query("rollbacks signed approval release captain", pipeline_id="pipe_x")
    assert any(chunk.chunk_id == "sub_fresh" for chunk in persisted)


def test_dissent_target_round_trips_on_sqlite(tmp_path: Path) -> None:
    # WI-007(b): dissent_target survives a whisper write -> read round-trip.
    store = _store(tmp_path)
    whisper = Whisper(
        from_agent="reviewer", target="executor", whisper_type="dissent",
        payload='{"issue": "regression", "alternatives": ["revert"]}',
        confidence=0.9, pipeline_id="pipe_x",
        ref="sub_disputed", dissent_target="sub_disputed",
    )
    store.emit_whisper(whisper)
    peeked = store.peek_whispers(agent_id="executor", pipeline_id="pipe_x")
    assert any(pw.dissent_target == "sub_disputed" for pw in peeked)


def test_world_check_without_detected_drift_preserves_drift(tmp_path: Path) -> None:
    # WI-007(c): a world_check whisper lacking detected_drift must not zero the
    # agent's drift; one that carries it still updates drift.
    store = _store(tmp_path)
    asm = Assembler(store=store)
    conscious = ConsciousBlock(
        agent_id="a", role="r", owns=[], must_not=[],
        task="t", slot="s", intent="i", drift_score=0.42,
    )
    present = Whisper(
        from_agent="sensor", target="a", whisper_type="world_check",
        payload='{"anchor_intent": "ship", "detected_drift": 0.7}', confidence=1.0,
    )
    # A stored payload that lost detected_drift (constructed via model_copy so it
    # bypasses re-validation, mirroring a payload string that lacks the field).
    missing = present.model_copy(update={"payload": '{"anchor_intent": "ship"}'})

    preserved = asm._apply_drift_feedback(conscious, [missing])
    assert preserved.drift_score == 0.42

    updated = asm._apply_drift_feedback(conscious, [present])
    assert updated.drift_score == 0.7
