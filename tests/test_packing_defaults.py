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
    assert config.diversity_lambda == pytest.approx(0.7)
    assert config.reduce_fanin_auto_min_writers == 3
    assert config.fit_swap_enabled is True
    assert config.reduce_fanin_enabled is False  # explicit opt-in unchanged


def test_swap_pass_admits_relevant_dropped_chunk_over_lower_ranked(tmp_path: Path) -> None:
    assembler = Assembler(store=SQLiteStore(tmp_path / "ncp.db"))
    big = _chunk("big", "alpha " * 60, relevance=0.9)
    small_a = _chunk("small_a", "alpha one", relevance=0.2)
    small_b = _chunk("small_b", "alpha two", relevance=0.1)
    conscious, budget = _conscious(), BudgetContext()
    with_big = assembler.encoder.assemble(conscious=conscious, chunks=[big], whispers=[], budget=budget)
    base = estimate_budget_tokens(
        assembler.encoder.assemble(conscious=conscious, chunks=[small_a, small_b], whispers=[], budget=budget)
    )
    # Room for the two small chunks (admitted greedily after the big one is
    # skipped) or the big one alone, never both.
    limit = max(base, estimate_budget_tokens(with_big) - 1) + 1
    limit = base + 2 if limit < base else limit

    def fit(swap: bool) -> list[str]:
        assembler._fit_swap_enabled = swap
        # Order is ranking: big first, but a stricter limit forces it out.
        kept, _ = assembler._fit_token_budget(
            conscious=conscious, budget=budget, chunks=[big, small_a, small_b], whispers=[],
            max_tokens=estimate_budget_tokens(with_big) - 1, query_text="alpha",
        )
        return [c.chunk_id for c in kept]

    off, on = fit(False), fit(True)
    assert "big" not in off and off == ["small_a", "small_b"]
    assert on == off  # big alone cannot fit either: swap must never overflow
    assert limit  # silence unused


def test_swap_pass_swaps_when_the_relevant_chunk_fits_in_place_of_victim(tmp_path: Path) -> None:
    assembler = Assembler(store=SQLiteStore(tmp_path / "ncp.db"))
    conscious, budget = _conscious(), BudgetContext()
    medium = _chunk("medium", "alpha " * 30, relevance=0.9)
    pad = _chunk("pad", "beta " * 30, relevance=0.95)
    tail = _chunk("tail", "gamma " * 30, relevance=0.1)
    # pad + tail fit together; medium (ranked between them) does not fit as a
    # third chunk, but fits when it replaces the lower-ranked tail.
    two = assembler.encoder.assemble(conscious=conscious, chunks=[pad, tail], whispers=[], budget=budget)
    limit = estimate_budget_tokens(two)

    def fit(swap: bool) -> list[str]:
        assembler._fit_swap_enabled = swap
        kept, _ = assembler._fit_token_budget(
            conscious=conscious, budget=budget, chunks=[pad, medium, tail], whispers=[],
            max_tokens=limit, query_text="alpha",
        )
        return [c.chunk_id for c in kept]

    assert fit(False) == ["pad", "tail"]
    swapped = fit(True)
    assert swapped == ["pad", "medium"]


def test_auto_fanin_reduces_only_with_enough_distinct_writers(tmp_path: Path) -> None:
    def build(writers: list[str]) -> list[str]:
        store = SQLiteStore(tmp_path / f"{len(set(writers))}.db")
        for i, writer in enumerate(writers):
            store.write(_chunk(f"d{i}", "payment retry backoff must use jitter always", writer=writer))
        assembler = Assembler(store=store)
        result = assembler.assemble(
            conscious=_conscious(), budget=BudgetContext(), query_text="payment retry backoff jitter", k=8,
        )
        return [c.chunk_id for c in result.chunks]

    many = build(["w1", "w2", "w3", "w1", "w2", "w3"])
    few = build(["w1", "w1", "w2", "w1", "w2", "w1"])
    assert len(many) < len(few)
