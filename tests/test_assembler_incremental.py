"""Tests for Assembler.assemble_incremental() — Slice 3."""

from __future__ import annotations

from pathlib import Path


from ncp.assembler import Assembler
from ncp.stores.sqlite import SQLiteStore
from ncp.types import BudgetContext, ConsciousBlock, SubconsciousChunk, Whisper


def _store(tmp_path: Path) -> SQLiteStore:
    return SQLiteStore(tmp_path / "store.db")


def _conscious(**overrides: object) -> ConsciousBlock:
    base: dict = {
        "agent_id": "tester",
        "role": "verify",
        "owns": ["tests"],
        "must_not": [],
        "task": "run_tests",
        "slot": "unit_test",
        "intent": "verify_incremental",
        "pipeline_id": "pipe_test",
    }
    base.update(overrides)
    return ConsciousBlock(**base)


def _budget(pressure: str = "low") -> BudgetContext:
    return BudgetContext(ctx_used=0.1, steps_completed=1, steps_total=5, elapsed_seconds=5, pressure=pressure)


def _chunk(content: str, chunk_id: str = "c1") -> SubconsciousChunk:
    return SubconsciousChunk(
        chunk_id=chunk_id,
        layer="semantic",
        content=content,
        src="agent_inferred",
        pipeline_id="pipe_test",
    )


# ── label ordering ────────────────────────────────────────────────────────────

def test_first_section_is_budget_header(tmp_path: Path) -> None:
    store = _store(tmp_path)
    assembler = Assembler(store=store)
    sections = list(assembler.assemble_incremental(conscious=_conscious(), budget=_budget()))
    assert sections[0][0] == "budget_header"
    assert "[NCP:BUDGET]" in sections[0][1]


def test_second_section_is_conscious(tmp_path: Path) -> None:
    store = _store(tmp_path)
    assembler = Assembler(store=store)
    sections = list(assembler.assemble_incremental(conscious=_conscious(), budget=_budget()))
    assert sections[1][0] == "conscious"
    assert "[NCP:CONSCIOUS]" in sections[1][1]


def test_subconscious_sections_come_before_whispers(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.write(_chunk("run_tests unit_test verify_incremental important content here"))
    store.emit_whisper(
        Whisper(
            from_agent="critic",
            target="tester",
            whisper_type="nudge",
            payload="check edge cases",
            confidence=0.9,
            pipeline_id="pipe_test",
        )
    )
    assembler = Assembler(store=store)
    sections = list(assembler.assemble_incremental(conscious=_conscious(), budget=_budget()))
    labels = [s[0] for s in sections]
    if "subconscious" in labels and "whispers" in labels:
        assert labels.index("subconscious") < labels.index("whispers")


def test_whispers_section_is_last_when_present(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.emit_whisper(
        Whisper(
            from_agent="critic",
            target="tester",
            whisper_type="nudge",
            payload="final check",
            confidence=0.9,
            pipeline_id="pipe_test",
        )
    )
    assembler = Assembler(store=store)
    sections = list(assembler.assemble_incremental(conscious=_conscious(), budget=_budget()))
    labels = [s[0] for s in sections]
    assert labels[-1] == "whispers"
    assert "[NCP:WHISPERS]" in sections[-1][1]


# ── no-whispers case ──────────────────────────────────────────────────────────

def test_no_whispers_section_when_queue_empty(tmp_path: Path) -> None:
    # Drift state is encoded in the conscious block, not as a whisper.
    # When the whisper queue is empty, no [NCP:WHISPERS] section is emitted.
    store = _store(tmp_path)
    assembler = Assembler(store=store)
    sections = list(assembler.assemble_incremental(conscious=_conscious(), budget=_budget()))
    labels = [s[0] for s in sections]
    assert "whispers" not in labels
    assert any(label == "subconscious" for label in labels)


# ── budget enforcement ────────────────────────────────────────────────────────

def test_budget_cap_truncates_subconscious_chunks(tmp_path: Path) -> None:
    store = _store(tmp_path)
    # Write 4 chunks with lots of words so they hit the token cap
    for i in range(4):
        store.write(_chunk(
            " ".join(["run_tests unit_test verify_incremental"] + [f"word{j}" for j in range(40)]),
            chunk_id=f"big_{i}",
        ))
    assembler = Assembler(store=store)
    # tight budget: only room for budget+conscious, no subconscious
    sections_tight = list(assembler.assemble_incremental(
        conscious=_conscious(), budget=_budget(), max_tokens=20,
    ))
    sub_tight = [s for s in sections_tight if s[0] == "subconscious"]

    # generous budget: all chunks fit
    sections_generous = list(assembler.assemble_incremental(
        conscious=_conscious(), budget=_budget(), max_tokens=10_000,
    ))
    sub_generous = [s for s in sections_generous if s[0] == "subconscious"]

    assert len(sub_tight) < len(sub_generous)


def test_budget_header_and_conscious_always_emitted_at_tight_limit(tmp_path: Path) -> None:
    store = _store(tmp_path)
    assembler = Assembler(store=store)
    sections = list(assembler.assemble_incremental(
        conscious=_conscious(), budget=_budget(), max_tokens=1,
    ))
    labels = [s[0] for s in sections]
    assert "budget_header" in labels
    assert "conscious" in labels


# ── consistency with assemble() ───────────────────────────────────────────────

def test_incremental_sections_contain_all_four_markers(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.write(_chunk("run_tests unit_test verify_incremental content matters"))
    store.emit_whisper(Whisper(
        from_agent="critic", target="tester", whisper_type="nudge",
        payload="check_something", confidence=0.9, pipeline_id="pipe_test",
    ))
    assembler = Assembler(store=store)
    sections = list(assembler.assemble_incremental(
        conscious=_conscious(), budget=_budget(), max_tokens=10_000,
    ))
    labels = [s[0] for s in sections]
    assert "budget_header" in labels
    assert "conscious" in labels
    assert "subconscious" in labels
    assert "whispers" in labels
    # Joined output contains all NCP section markers exactly once
    joined = "\n\n".join(text for _, text in sections)
    for marker in ("[NCP:BUDGET]", "[NCP:CONSCIOUS]", "[NCP:SUBCONSCIOUS]", "[NCP:WHISPERS]"):
        assert joined.count(marker) == 1


def test_subconscious_section_has_single_ncp_header(tmp_path: Path) -> None:
    store = _store(tmp_path)
    for i in range(3):
        store.write(_chunk(f"run_tests unit_test verify_incremental content_{i}", chunk_id=f"ch_{i}"))
    assembler = Assembler(store=store)
    sections = list(assembler.assemble_incremental(
        conscious=_conscious(), budget=_budget(), max_tokens=10_000,
    ))
    sub_sections = [s for s in sections if s[0] == "subconscious"]
    # All chunks are in exactly one subconscious section with one header
    assert len(sub_sections) == 1
    assert sub_sections[0][1].count("[NCP:SUBCONSCIOUS]") == 1
