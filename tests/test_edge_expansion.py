"""Tests for 1-hop edge-expansion retrieval and supersession suppression."""

from pathlib import Path

from ncp.assembler import Assembler
from ncp.config import load_config
from ncp.stores.sqlite import SQLiteStore
from ncp.types import BudgetContext, ConsciousBlock, SubconsciousChunk


def _conscious(**overrides: object) -> ConsciousBlock:
    base = {
        "agent_id": "fixer",
        "role": "build",
        "owns": ["implementation"],
        "must_not": ["planning"],
        "task": "fix_npe",
        "slot": "apply_guard",
        "intent": "resolve_bug",
        "pipeline_id": "pipe_1",
    }
    base.update(overrides)
    return ConsciousBlock(**base)


def _budget() -> BudgetContext:
    return BudgetContext(ctx_used=0.2, steps_completed=1, steps_total=3, elapsed_seconds=5)


def test_get_chunks_by_ids_returns_live_chunks(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "store.db")
    store.write(SubconsciousChunk(chunk_id="a", layer="episodic", content="alpha", src="tool_result"))
    store.write(SubconsciousChunk(chunk_id="b", layer="episodic", content="beta", src="tool_result"))

    fetched = store.get_chunks_by_ids(["a", "b", "missing"])

    assert {c.chunk_id for c in fetched} == {"a", "b"}


def test_get_chunks_by_ids_empty_input(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "store.db")
    assert store.get_chunks_by_ids([]) == []


def test_edge_expansion_pulls_in_caused_by_neighbor(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "store.db")
    # The cause: an analyzer chunk that the query won't lexically match.
    store.write(
        SubconsciousChunk(
            chunk_id="cause_chunk",
            layer="episodic",
            content="stack trace at line 142 retryCount null ACH trial tier",
            src="tool_result",
            pipeline_id="pipe_1",
        )
    )
    # The effect: a distilled fix chunk that DOES match the query and links to its cause.
    store.write(
        SubconsciousChunk(
            chunk_id="fix_chunk",
            layer="episodic",
            content="apply_guard fix_npe null guard applied",
            src="agent_inferred",
            caused_by="cause_chunk",
            pipeline_id="pipe_1",
        )
    )

    assembler = Assembler(store=store)
    result = assembler.assemble(conscious=_conscious(), budget=_budget())

    ids = {chunk.chunk_id for chunk in result.chunks}
    assert "fix_chunk" in ids
    assert "cause_chunk" in ids  # pulled in via caused_by edge, not lexical match


def test_edge_expansion_disabled_by_config(tmp_path: Path) -> None:
    project = tmp_path / "repo"
    (project / ".git").mkdir(parents=True)
    config = load_config(env={"NCP_EDGE_EXPANSION": "false", "NCP_STORE_PATH": str(project / "s.db")})
    store = SQLiteStore(project / "s.db")
    store.write(
        SubconsciousChunk(
            chunk_id="cause_chunk",
            layer="episodic",
            content="unrelated lexical content here",
            src="tool_result",
            pipeline_id="pipe_1",
        )
    )
    store.write(
        SubconsciousChunk(
            chunk_id="fix_chunk",
            layer="episodic",
            content="apply_guard fix_npe null guard applied",
            src="agent_inferred",
            caused_by="cause_chunk",
            pipeline_id="pipe_1",
        )
    )

    assembler = Assembler(store=store, config=config)
    result = assembler.assemble(conscious=_conscious(), budget=_budget())

    ids = {chunk.chunk_id for chunk in result.chunks}
    assert "fix_chunk" in ids
    assert "cause_chunk" not in ids  # expansion off → cause not pulled in


def test_supersession_suppresses_stale_chunk(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "store.db")
    # Stale chunk that matches the query lexically.
    store.write(
        SubconsciousChunk(
            chunk_id="stale",
            layer="episodic",
            content="apply_guard fix_npe old approach",
            src="agent_inferred",
            pipeline_id="pipe_1",
        )
    )
    # Newer chunk that supersedes the stale one and also matches the query.
    store.write(
        SubconsciousChunk(
            chunk_id="fresh",
            layer="episodic",
            content="apply_guard fix_npe new approach",
            src="agent_inferred",
            supersedes="stale",
            pipeline_id="pipe_1",
        )
    )

    assembler = Assembler(store=store)
    result = assembler.assemble(conscious=_conscious(), budget=_budget())

    ids = {chunk.chunk_id for chunk in result.chunks}
    assert "fresh" in ids
    assert "stale" not in ids


def test_supersession_handles_json_list_form(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "store.db")
    for cid in ("old_a", "old_b"):
        store.write(
            SubconsciousChunk(
                chunk_id=cid,
                layer="episodic",
                content=f"apply_guard fix_npe {cid}",
                src="agent_inferred",
                pipeline_id="pipe_1",
            )
        )
    store.write(
        SubconsciousChunk(
            chunk_id="merged",
            layer="episodic",
            content="apply_guard fix_npe merged result",
            src="agent_inferred",
            supersedes='["old_a", "old_b"]',
            pipeline_id="pipe_1",
        )
    )

    assembler = Assembler(store=store)
    result = assembler.assemble(conscious=_conscious(), budget=_budget())

    ids = {chunk.chunk_id for chunk in result.chunks}
    assert "merged" in ids
    assert "old_a" not in ids
    assert "old_b" not in ids


def test_edge_expansion_respects_chunk_cap(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "store.db")
    # Many matching chunks each pointing at a distinct cause.
    for index in range(6):
        store.write(
            SubconsciousChunk(
                chunk_id=f"cause_{index}",
                layer="episodic",
                content=f"distinct cause content {index}",
                src="tool_result",
                pipeline_id="pipe_1",
            )
        )
        store.write(
            SubconsciousChunk(
                chunk_id=f"fix_{index}",
                layer="episodic",
                content="apply_guard fix_npe variant",
                src="agent_inferred",
                caused_by=f"cause_{index}",
                pipeline_id="pipe_1",
            )
        )

    assembler = Assembler(store=store)
    result = assembler.assemble(conscious=_conscious(), budget=_budget())

    # Default chunk_cap is 4; expansion must not exceed the bounded cap.
    assert len(result.chunks) <= 4
