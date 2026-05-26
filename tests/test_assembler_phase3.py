"""Phase 3 assembler tests: cold start, async post-turn, diversity, middleware."""

from pathlib import Path

import pytest

from ncp.assembler import Assembler
from ncp.middleware.base import Middleware, MiddlewarePipeline
from ncp.stores.sqlite import SQLiteStore
from ncp.types import BudgetContext, ConsciousBlock, NCPResponse, SubconsciousChunk


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


@pytest.mark.asyncio
async def test_assembler_post_turn_async_logs_cost_and_memory(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "store.db")
    assembler = Assembler(store=store)
    response = NCPResponse(
        content="done",
        input_tokens=50,
        output_tokens=25,
        cost_usd=0.02,
        model="gpt_4o",
        pipeline_id="pipe_1",
        turn_id="turn_async",
        latency_ms=200,
    )

    record = await assembler.post_turn_async(
        conscious=_make_conscious(),
        response=response,
        result_summary="summary",
        result_full="full output",
        memory_chunks=[
            SubconsciousChunk(
                chunk_id="sub_async",
                layer="semantic",
                content="async memory chunk",
                src="synthesis",
                pipeline_id="pipe_1",
            )
        ],
    )

    assert record.turn_id == "turn_async"
    assert store.resolve_recent_ref("r:sub/turn_async") is not None
    results = store.query("async memory chunk", pipeline_id="pipe_1")
    assert any(chunk.chunk_id == "sub_async" for chunk in results)
    assert store.status()["cost_usd_total"] == 0.02


def test_assembler_cold_start_generates_summary_chunk(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "store.db")
    assembler = Assembler(store=store)
    result = assembler.assemble(
        conscious=_make_conscious(task="fresh_pipeline", slot="init"),
        budget=BudgetContext(),
    )

    assert len(result.chunks) >= 1
    cold_chunks = [c for c in result.chunks if c.chunk_id.startswith("cold_")]
    assert len(cold_chunks) == 1
    assert "pipeline_summary" in cold_chunks[0].content


def test_assembler_uses_middleware_pre_assemble_hook(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "store.db")

    class _ModifyConscious(Middleware):
        def pre_assemble(
            self,
            conscious: ConsciousBlock,
            budget: BudgetContext,
        ) -> tuple[ConsciousBlock, BudgetContext] | None:
            modified = conscious.model_copy(update={"task": "modified_by_mw"})
            return modified, budget

    middleware = MiddlewarePipeline([_ModifyConscious()])
    assembler = Assembler(store=store, middleware=middleware)
    result = assembler.assemble(
        conscious=_make_conscious(task="original", slot="test"),
        budget=BudgetContext(),
    )

    assert "task:original" not in result.context
    assert "task:modified_by_mw" in result.context


def test_assembler_uses_middleware_post_assemble_hook(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "store.db")

    class _AnnotateContext(Middleware):
        def post_assemble(self, context: str) -> str | None:
            return context + "\n[NCP:MW_ANNOTATION]"

    middleware = MiddlewarePipeline([_AnnotateContext()])
    assembler = Assembler(store=store, middleware=middleware)
    result = assembler.assemble(
        conscious=_make_conscious(),
        budget=BudgetContext(),
    )

    assert "[NCP:MW_ANNOTATION]" in result.context


def test_assembler_middleware_pre_write_applied_during_post_turn(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "store.db")
    events: list[str] = []

    class _CapturePreWrite(Middleware):
        def pre_write(self, chunk: SubconsciousChunk) -> SubconsciousChunk | None:
            events.append(chunk.chunk_id)
            return chunk

    assembler = Assembler(store=store, middleware=MiddlewarePipeline([_CapturePreWrite()]))
    response = NCPResponse(
        content="ok",
        input_tokens=10,
        output_tokens=10,
        cost_usd=0.0,
        model="test",
        turn_id="turn_mw_prewrite",
        latency_ms=0,
    )

    assembler.post_turn(
        conscious=_make_conscious(),
        response=response,
        result_summary="s",
        result_full="f",
        memory_chunks=[
            SubconsciousChunk(
                chunk_id="sub_mw_write", layer="episodic", content="mw path", src="synthesis"
            )
        ],
    )

    assert "sub_mw_write" in events


def test_assembler_diversity_enforced_via_store_query(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "store.db", max_working_chunks=100, gc_threshold=80)
    topics = ["authentication flow bearer token", "rate limiting 429 retry",
              "caching strategy redis ttl", "database schema migration",
              "logging structured json format"]
    for i in range(5):
        author = "agent_a" if i < 3 else "agent_b"
        store.write(
            SubconsciousChunk(
                chunk_id=f"sub_div_{i}",
                layer="semantic",
                content=topics[i],
                src="tool_result",
                written_by=author,
                pipeline_id="pipe_1",
            )
        )

    results = store.query("authentication rate database", pipeline_id="pipe_1", k=10)
    author_counts: dict[str, int] = {}
    for chunk in results:
        author_counts[chunk.written_by] = author_counts.get(chunk.written_by, 0) + 1

    assert all(count <= 2 for count in author_counts.values())
    assert len(results) >= 3


@pytest.mark.asyncio
async def test_assembler_cold_start_with_async_post_turn(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "store.db")
    assembler = Assembler(store=store)

    result = assembler.assemble(
        conscious=_make_conscious(task="async_cold", slot="init"),
        budget=BudgetContext(),
    )
    assert len(result.chunks) >= 1
    assert any(c.chunk_id.startswith("cold_") for c in result.chunks)

    response = NCPResponse(
        content="async cold start done",
        input_tokens=10,
        output_tokens=5,
        cost_usd=0.001,
        model="test",
        turn_id="turn_cold_async",
        latency_ms=50,
    )
    record = await assembler.post_turn_async(
        conscious=_make_conscious(task="async_cold", slot="init"),
        response=response,
        result_summary="cold done",
        result_full="cold start full output",
    )

    assert store.resolve_recent_ref(f"r:sub/{record.turn_id}") is not None
    assert store.status()["cost_usd_total"] == 0.001


def test_assembler_read_after_write_retry(tmp_path: Path) -> None:
    class _FlakyStore:
        def __init__(self) -> None:
            self.write_count = 0

        def write(self, chunk: SubconsciousChunk) -> bool:
            self.write_count += 1
            if self.write_count == 1:
                raise RuntimeError("temporary write failure")
            return True

        def log_turn_record(self, *args: object, **kwargs: object) -> None: ...
        def log_conscious(self, *args: object, **kwargs: object) -> None: ...
        def log_cost(self, *args: object, **kwargs: object) -> None: ...
        def resolve_recent_ref(self, *args: object, **kwargs: object) -> object: ...
        def drain_whispers(self, *args: object, **kwargs: object) -> list: ...
        def query(self, *args: object, **kwargs: object) -> list: ...
        def get_pipeline_goal_versions(self, *args: object, **kwargs: object) -> dict: ...

    store = _FlakyStore()
    assembler = Assembler(store=store)  # type: ignore[arg-type]

    response = NCPResponse(
        content="ok",
        input_tokens=10,
        output_tokens=10,
        cost_usd=0.0,
        model="test",
        turn_id="turn_retry",
        latency_ms=0,
    )

    assembler.post_turn(
        conscious=_make_conscious(),
        response=response,
        result_summary="s",
        result_full="f",
        memory_chunks=[
            SubconsciousChunk(
                chunk_id="sub_retry", layer="episodic", content="retry test", src="synthesis"
            )
        ],
    )

    assert store.write_count >= 2


def test_assembler_surfaces_write_failure_after_retries() -> None:
    class _FailingStore:
        def write(self, chunk: SubconsciousChunk) -> bool:
            raise RuntimeError("disk unavailable")

        def log_turn_record(self, *args: object, **kwargs: object) -> None: ...
        def log_conscious(self, *args: object, **kwargs: object) -> None: ...
        def log_cost(self, *args: object, **kwargs: object) -> None: ...
        def resolve_recent_ref(self, *args: object, **kwargs: object) -> object: ...
        def drain_whispers(self, *args: object, **kwargs: object) -> list: ...
        def query(self, *args: object, **kwargs: object) -> list: ...
        def get_pipeline_goal_versions(self, *args: object, **kwargs: object) -> dict: ...

    assembler = Assembler(store=_FailingStore())  # type: ignore[arg-type]
    response = NCPResponse(
        content="ok",
        input_tokens=10,
        output_tokens=10,
        cost_usd=0.0,
        model="test",
        turn_id="turn_retry_fail",
        latency_ms=0,
    )

    with pytest.raises(RuntimeError, match="Failed to persist chunk after 3 attempts"):
        assembler.post_turn(
            conscious=_make_conscious(),
            response=response,
            result_summary="s",
            result_full="f",
            memory_chunks=[
                SubconsciousChunk(
                    chunk_id="sub_retry_fail",
                    layer="episodic",
                    content="retry test",
                    src="synthesis",
                )
            ],
        )


def test_assembler_goal_version_coherence_alert(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "store.db")

    from ncp.types import NCPResponse as Resp

    other = _make_conscious(
        agent_id="planner",
        goal_version=2,
        task="delegate",
        slot="planning",
        intent="coordinate",
    )
    store.log_conscious(other, snapshot_hash="hash_planner")
    store.log_cost(
        agent_id="planner",
        response=Resp(
            content="ok",
            input_tokens=1,
            output_tokens=1,
            cost_usd=0.0,
            model="test",
            turn_id="turn_planner",
            latency_ms=0,
            pipeline_id="pipe_1",
        ),
    )

    assembler = Assembler(store=store)
    result = assembler.assemble(
        conscious=_make_conscious(agent_id="executor", goal_version=1, task="align", slot="check"),
        budget=BudgetContext(),
    )

    assert any("goal_version_mismatch" in w.payload for w in result.whispers)
