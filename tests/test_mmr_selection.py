from __future__ import annotations

from pathlib import Path

from ncp.assembler import Assembler
from ncp.config import load_config
from ncp.stores.retrieval import apply_mmr_selection
from ncp.stores.sqlite import SQLiteStore
from ncp.types import BudgetContext, ConsciousBlock, SubconsciousChunk


def _chunk(chunk_id: str, content: str, relevance: float) -> SubconsciousChunk:
    return SubconsciousChunk(
        chunk_id=chunk_id,
        layer="semantic",
        content=content,
        src="tool_result",
        written_by="test",
        relevance=relevance,
    )


def _pool() -> list[SubconsciousChunk]:
    return [
        _chunk("dup_a", "authentication bearer token refresh failure", 0.95),
        _chunk("dup_b", "authentication bearer token refresh failure", 0.92),
        _chunk("dup_c", "authentication bearer token refresh failure", 0.90),
        _chunk("distinct_rate", "rate limiting retry backoff budget", 0.80),
        _chunk("distinct_schema", "database migration rollback safety", 0.78),
    ]


def _make_conscious() -> ConsciousBlock:
    return ConsciousBlock(
        agent_id="executor",
        role="build",
        owns=["implementation"],
        must_not=[],
        task="auth_retry",
        slot="mmr",
        intent="select_context",
        pipeline_id="pipe_mmr",
    )


def _config(tmp_path: Path, diversity_lambda: float):
    project = tmp_path / f"repo_{str(diversity_lambda).replace('.', '_')}"
    (project / ".git").mkdir(parents=True)
    (project / ".ncp").mkdir()
    (project / ".ncp" / "config.toml").write_text(
        f"[retrieval]\ndiversity_lambda = {diversity_lambda}\n"
    )
    return load_config(cwd=project)


def test_mmr_lambda_1_preserves_top_relevance_order() -> None:
    selected = apply_mmr_selection(
        _pool(),
        query_text="authentication bearer token",
        diversity_lambda=1.0,
        chunk_cap=3,
    )

    assert [chunk.chunk_id for chunk in selected] == ["dup_a", "dup_b", "dup_c"]


def test_mmr_lambda_07_selects_one_duplicate_and_two_distinct() -> None:
    selected = apply_mmr_selection(
        _pool(),
        query_text="authentication bearer token",
        diversity_lambda=0.7,
        chunk_cap=3,
    )

    assert [chunk.chunk_id for chunk in selected] == [
        "dup_a",
        "distinct_rate",
        "distinct_schema",
    ]


def test_mmr_uses_embedding_similarity_when_available() -> None:
    chunks = [
        _chunk("near_a", "alpha words", 0.95).model_copy(update={"embedding": [1.0, 0.0]}),
        _chunk("near_b", "different words", 0.92).model_copy(update={"embedding": [0.98, 0.02]}),
        _chunk("far", "different words", 0.80).model_copy(update={"embedding": [0.0, 1.0]}),
    ]

    selected = apply_mmr_selection(
        chunks,
        query_text="alpha",
        diversity_lambda=0.7,
        chunk_cap=2,
    )

    assert [chunk.chunk_id for chunk in selected] == ["near_a", "far"]


def test_assembler_default_lambda_keeps_duplicate_top_behavior(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "default.db")
    store.query = lambda *args, **kwargs: _pool()  # type: ignore[method-assign]

    result = Assembler(store=store).assemble(
        conscious=_make_conscious(),
        budget=BudgetContext(),
        k=3,
    )

    assert [chunk.chunk_id for chunk in result.chunks] == ["dup_a", "dup_b", "dup_c"]


def test_assembler_configured_lambda_selects_diverse_set(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "configured.db")
    store.query = lambda *args, **kwargs: _pool()  # type: ignore[method-assign]

    result = Assembler(store=store, config=_config(tmp_path, 0.7)).assemble(
        conscious=_make_conscious(),
        budget=BudgetContext(),
        k=3,
    )

    assert [chunk.chunk_id for chunk in result.chunks] == [
        "dup_a",
        "distinct_rate",
        "distinct_schema",
    ]
    assert len(result.chunks) == 3


def test_assembler_mmr_does_not_widen_explicit_k(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "bounded.db")
    store.query = lambda *args, **kwargs: _pool()  # type: ignore[method-assign]

    result = Assembler(store=store, config=_config(tmp_path, 0.7)).assemble(
        conscious=_make_conscious(),
        budget=BudgetContext(),
        k=2,
    )

    assert len(result.chunks) == 2
