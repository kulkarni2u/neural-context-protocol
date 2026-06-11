"""Tests for 0.8.x Slice 1: caller-controlled k through assembler and public API."""

from __future__ import annotations

from pathlib import Path
from typing import Any
from ncp.assembler import Assembler
from ncp.stores.sqlite import SQLiteStore
from ncp.types import (
    BudgetContext,
    ConsciousBlock,
    SubconsciousChunk,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _conscious(**overrides: Any) -> ConsciousBlock:
    base: dict[str, Any] = {
        "agent_id": "agent",
        "role": "build",
        "owns": [],
        "must_not": [],
        "task": "implement_task",
        "slot": "assemble",
        "intent": "test_k_forwarding",
        "pipeline_id": "pipe_test",
    }
    base.update(overrides)
    return ConsciousBlock(**base)


def _budget(pressure: str = "low") -> BudgetContext:
    return BudgetContext(pressure=pressure)  # type: ignore[arg-type]


def _store_spy(tmp_path: Path) -> tuple[SQLiteStore, list[int]]:
    """Return a SQLiteStore and a list that records every k passed to query()."""
    store = SQLiteStore(tmp_path / "store.db")
    captured_k: list[int] = []
    original_query = store.query

    def spy_query(text: str, *, k: int = 4, **kwargs: Any) -> list[SubconsciousChunk]:
        captured_k.append(k)
        return original_query(text, k=k, **kwargs)

    store.query = spy_query  # type: ignore[method-assign]
    return store, captured_k


def _store_spy_with_whispers(tmp_path: Path) -> tuple[SQLiteStore, list[int], list[int]]:
    """Return a SQLiteStore plus captured query k values and whisper peek max_items."""
    store, captured_k = _store_spy(tmp_path)
    captured_whisper_caps: list[int] = []
    original_peek = store.peek_whispers

    def spy_peek_whispers(*, max_items: int = 3, **kwargs: Any):
        captured_whisper_caps.append(max_items)
        return original_peek(max_items=max_items, **kwargs)

    store.peek_whispers = spy_peek_whispers  # type: ignore[method-assign]
    return store, captured_k, captured_whisper_caps


# ---------------------------------------------------------------------------
# assemble() must forward k to store.query
# ---------------------------------------------------------------------------

def test_assemble_forwards_k_to_store_query(tmp_path: Path) -> None:
    """assembler.assemble(k=8) must call store.query with k=8."""
    store, captured = _store_spy(tmp_path)
    assembler = Assembler(store=store)

    assembler.assemble(conscious=_conscious(), budget=_budget(), k=8)

    assert captured, "store.query was never called"
    assert captured[0] == 8, f"expected k=8 forwarded to store.query, got {captured[0]}"


def test_assemble_default_k_uses_pressure_logic(tmp_path: Path) -> None:
    """When k is None (default), assembler uses budget-pressure logic (k=4 normal, k=2 critical)."""
    store_n, cap_n = _store_spy(tmp_path / "normal")
    store_c, cap_c = _store_spy(tmp_path / "critical")

    Assembler(store=store_n).assemble(conscious=_conscious(), budget=_budget("low"))
    Assembler(store=store_c).assemble(conscious=_conscious(), budget=_budget("critical"))

    assert cap_n[0] == 4, f"low pressure should use k=4, got {cap_n[0]}"
    assert cap_c[0] == 2, f"critical pressure should use k=2, got {cap_c[0]}"


def test_assemble_default_whisper_cap_uses_pressure_logic(tmp_path: Path) -> None:
    """When k is None, whisper peek cap must follow the same pressure policy."""
    store_n, _, whisper_cap_n = _store_spy_with_whispers(tmp_path / "normal_whispers")
    store_c, _, whisper_cap_c = _store_spy_with_whispers(tmp_path / "critical_whispers")

    Assembler(store=store_n).assemble(conscious=_conscious(), budget=_budget("low"))
    Assembler(store=store_c).assemble(conscious=_conscious(), budget=_budget("critical"))

    assert whisper_cap_n[0] == 3, f"low pressure should peek up to 3 whispers, got {whisper_cap_n[0]}"
    assert whisper_cap_c[0] == 1, (
        f"critical pressure should peek only 1 whisper, got {whisper_cap_c[0]}"
    )


def test_assemble_k_overrides_pressure_default(tmp_path: Path) -> None:
    """Explicit k overrides the pressure-based default even under critical pressure."""
    store, captured = _store_spy(tmp_path)
    assembler = Assembler(store=store)

    assembler.assemble(conscious=_conscious(), budget=_budget("critical"), k=6)

    assert captured[0] == 6, f"explicit k=6 should override critical-pressure default, got {captured[0]}"


def test_assemble_explicit_k_keeps_default_whisper_cap(tmp_path: Path) -> None:
    """Explicit k widens chunk retrieval but should not widen whisper peek above 3."""
    store, _, whisper_caps = _store_spy_with_whispers(tmp_path)
    assembler = Assembler(store=store)

    assembler.assemble(conscious=_conscious(), budget=_budget("critical"), k=6)

    assert whisper_caps[0] == 3, f"explicit k should keep whisper cap at 3, got {whisper_caps[0]}"


# ---------------------------------------------------------------------------
# assemble_incremental() must also forward k
# ---------------------------------------------------------------------------

def test_assemble_incremental_forwards_k(tmp_path: Path) -> None:
    """assembler.assemble_incremental(k=8) must call store.query with k=8."""
    store, captured = _store_spy(tmp_path)
    assembler = Assembler(store=store)

    list(assembler.assemble_incremental(conscious=_conscious(), budget=_budget(), k=8))

    assert captured, "store.query was never called from assemble_incremental"
    assert captured[0] == 8, f"expected k=8, got {captured[0]}"


# ---------------------------------------------------------------------------
# Post-assembly chunk slice must respect k
# ---------------------------------------------------------------------------

def test_assemble_result_chunks_not_capped_at_four(tmp_path: Path) -> None:
    """With k=8 and enough data, result.chunks must contain more than 4 chunks."""
    store = SQLiteStore(tmp_path / "store.db")
    # Write 10 distinct chunks so the store has enough candidates.
    contents = [
        "bearer token oauth session handshake",
        "jwt refresh secret credential store",
        "memory retrieval pipeline subconscious",
        "episodic layer turn history context",
        "auth middleware boundary unauthenticated",
        "semantic cache reasoning trace synthesis",
        "procedural trust calibration assembly",
        "pgvector cosine similarity index search",
        "redis coordination whisper payloads bus",
        "hybrid scoring bm25 recency ranked results",
    ]
    for i, content in enumerate(contents):
        store.write(SubconsciousChunk(
            chunk_id=f"sub_k_{i}",
            layer="episodic",
            content=content,
            src="tool_result",
            written_by=f"agent_{i}",
            pipeline_id="pipe_test",
        ))

    assembler = Assembler(store=store)
    result = assembler.assemble(
        conscious=_conscious(),
        budget=_budget(),
        k=8,
        # Provide query_text with terms that match multiple chunk contents.
        query_text="bearer token auth memory layer trust redis pgvector hybrid scoring",
    )

    assert len(result.chunks) > 4, (
        f"with k=8 and 10 available chunks, expected >4 in result, got {len(result.chunks)}"
    )


# ---------------------------------------------------------------------------
# api.get_context must forward k
# ---------------------------------------------------------------------------

def test_api_get_context_forwards_k(tmp_path: Path) -> None:
    """api.get_context(k=8) must forward k=8 to assembler.assemble."""
    from ncp.api import get_context

    store, captured = _store_spy(tmp_path)

    get_context(agent=_conscious(), store=store, k=8)

    assert captured, "store.query was never called via api.get_context"
    assert captured[0] == 8, f"expected k=8 forwarded, got {captured[0]}"
