"""Packing, fan-in and diversity defaults (token-efficiency plan item 8)."""

from __future__ import annotations

from pathlib import Path

import pytest

from ncp.assembler import Assembler
from ncp.config import load_config
from ncp.stores.sqlite import SQLiteStore
from ncp.tokens import estimate_budget_tokens
from ncp.types import BudgetContext, ConsciousBlock, SubconsciousChunk


def _chunk(chunk_id: str, content: str, writer: str = "w", relevance: float = 0.0) -> SubconsciousChunk:
    return SubconsciousChunk(
        chunk_id=chunk_id, layer="semantic", content=content, src="tool_result",
        pipeline_id="p", written_by=writer, relevance=relevance,
    )


def _conscious() -> ConsciousBlock:
    return ConsciousBlock(
        agent_id="a", role="r", owns=[], must_not=[], task="t", slot="s", intent="i", pipeline_id="p",
    )


def test_new_defaults(tmp_path: Path) -> None:
    project = tmp_path / "repo"
    (project / ".git").mkdir(parents=True)
    config = load_config(cwd=project)
    assert config.diversity_lambda == pytest.approx(1.0)  # MMR is opt-in: 0.7 regressed hotpotqa_style recall
    assert config.reduce_fanin_auto_min_writers == 3
    assert config.fit_swap_enabled is True
    assert config.reduce_fanin_enabled is False  # explicit opt-in unchanged


def test_swap_pass_displaces_less_relevant_admitted_chunk(tmp_path: Path) -> None:
    assembler = Assembler(store=SQLiteStore(tmp_path / "ncp.db"))
    conscious, budget = _conscious(), BudgetContext()
    first = _chunk("c_first", "gamma " * 30, relevance=0.2)  # ranked first, barely relevant
    wanted = _chunk("c_want", "alpha " * 30, relevance=0.9)
    last = _chunk("c_last", "delta " * 3, relevance=0.1)
    # first + last fit; wanted does not fit alongside first, but fits instead of it.
    limit = estimate_budget_tokens(
        assembler.encoder.assemble(conscious=conscious, chunks=[first, last], whispers=[], budget=budget)
    )

    def fit(swap: bool) -> list[str]:
        assembler._fit_swap_enabled = swap
        kept, _ = assembler._fit_token_budget(
            conscious=conscious, budget=budget, chunks=[first, wanted, last], whispers=[],
            max_tokens=limit, query_text="alpha",
        )
        return [c.chunk_id for c in kept]

    assert fit(False) == ["c_first", "c_last"]
    assert fit(True) == ["c_want", "c_last"]


def test_swap_pass_never_overflows_or_swaps_below_half_relevance(tmp_path: Path) -> None:
    assembler = Assembler(store=SQLiteStore(tmp_path / "ncp.db"))
    conscious, budget = _conscious(), BudgetContext()
    first = _chunk("first", "beta " * 30, relevance=0.1)
    wanted = _chunk("wanted", "alpha " * 30, relevance=0.4)
    limit = estimate_budget_tokens(
        assembler.encoder.assemble(conscious=conscious, chunks=[first], whispers=[], budget=budget)
    )
    assembler._fit_swap_enabled = True
    kept, _ = assembler._fit_token_budget(
        conscious=conscious, budget=budget, chunks=[first, wanted], whispers=[],
        max_tokens=limit, query_text="alpha",
    )
    assert [c.chunk_id for c in kept] == ["first"]


def test_auto_fanin_trigger_needs_enough_distinct_writers(tmp_path: Path) -> None:
    assembler = Assembler(store=SQLiteStore(tmp_path / "ncp.db"))
    many = [_chunk(f"m{i}", "x", writer=f"w{i % 3}") for i in range(6)]
    few = [_chunk(f"f{i}", "x", writer=f"w{i % 2}") for i in range(6)]
    assert assembler._should_reduce_fanin(many) is True
    assert assembler._should_reduce_fanin(few) is False
    assembler._reduce_fanin_auto_min_writers = 0
    assert assembler._should_reduce_fanin(many) is False
    assembler._reduce_fanin_enabled = True
    assert assembler._should_reduce_fanin(few) is True
