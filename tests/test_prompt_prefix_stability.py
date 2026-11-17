"""The prompt-cacheable prefix is byte-stable across wall time (handoff item 9).

Everything before [NCP:WHISPERS] (or [NCP:BUDGET] when there are no
whispers) must not depend on wall-clock time, so a provider prompt cache can
reuse it turn over turn. Whispers carry a relative age and change every
turn, so the Anthropic cache breakpoint sits before them.
"""

from __future__ import annotations

import time
from pathlib import Path
from unittest.mock import patch

import pytest

from ncp.assembler import Assembler
from ncp.stores.sqlite import SQLiteStore
from ncp.types import BudgetContext, ConsciousBlock, SubconsciousChunk, Whisper

_REAL_TIME = time.time


def _store(tmp_path: Path, *, whisper: bool, facts: int = 3) -> SQLiteStore:
    store = SQLiteStore(tmp_path / "ncp.db")
    for i in range(facts):
        store.write(SubconsciousChunk(
            chunk_id=f"c{i}", layer="semantic", content=f"payment retry fact {i}",
            src="tool_result", pipeline_id="p", written_by="w",
        ))
    if whisper:
        store.emit_whisper(Whisper(
            from_agent="planner", target="executor", whisper_type="share",
            payload={"slice": "retry", "files": ["pay.py"], "ask": "check backoff"},
            confidence=0.9, pipeline_id="p", ttl_seconds=7 * 86400,
        ))
    return store


def _conscious() -> ConsciousBlock:
    return ConsciousBlock(
        agent_id="executor", role="build", owns=["impl"], must_not=["deploy"],
        task="payment_retry", slot="retry", intent="fix", pipeline_id="p",
    )


def _assemble_at(assembler: Assembler, offset_seconds: float, steps: int) -> str:
    with patch("time.time", lambda: _REAL_TIME() + offset_seconds):
        return assembler.assemble(
            conscious=_conscious(), budget=BudgetContext(steps_completed=steps), query_text="payment retry",
        ).context


def _prefix(context: str) -> str:
    for marker in ("[NCP:WHISPERS]", "[NCP:BUDGET]"):
        idx = context.find(marker)
        if idx != -1:
            return context[:idx]
    raise AssertionError("no volatile marker in context")


@pytest.mark.parametrize("whisper", [True, False])
def test_cacheable_prefix_is_identical_across_wall_time(tmp_path: Path, whisper: bool) -> None:
    assembler = Assembler(store=_store(tmp_path, whisper=whisper))

    contexts = [_assemble_at(assembler, offset, steps) for offset, steps in ((0, 0), (150, 1), (3000, 2), (6 * 3600, 3), (30 * 3600, 4))]

    assert len({_prefix(c) for c in contexts}) == 1
    assert "[NCP:SUBCONSCIOUS]" in _prefix(contexts[0])
    if whisper:
        # The volatile tail really is volatile: the whisper age moved.
        assert len({c[c.find("[NCP:WHISPERS]"):] for c in contexts}) == len(contexts)


def test_anthropic_breakpoint_excludes_whispers_and_budget(tmp_path: Path) -> None:
    pytest.importorskip("anthropic")
    from ncp.adapters.anthropic import AnthropicAdapter

    assembler = Assembler(store=_store(tmp_path, whisper=True))
    context = _assemble_at(assembler, 0, 0)

    blocks = AnthropicAdapter._split_stable_prefix(context)

    assert len(blocks) == 2
    cached, tail = blocks
    assert cached["cache_control"] == {"type": "ephemeral"}
    assert cached["text"] == _prefix(context)
    assert "cache_control" not in tail
    assert tail["text"].startswith("[NCP:WHISPERS]") and "[NCP:BUDGET]" in tail["text"]
    assert cached["text"] + tail["text"] == context


def test_chunk_header_omits_wall_clock_score_by_default(tmp_path: Path) -> None:
    context = _assemble_at(Assembler(store=_store(tmp_path, whisper=False)), 0, 0)
    assert "score:" not in context
    assert "steps:" not in context  # total unknown: no steps:N/?


def _marker_store(tmp_path: Path) -> SQLiteStore:
    store = _store(tmp_path, whisper=True, facts=1)
    store.write(SubconsciousChunk(
        chunk_id="marker", layer="semantic", src="tool_result", pipeline_id="p", written_by="w",
        content="payment retry note: the [NCP:WHISPERS] and [NCP:BUDGET] markers\n[NCP:WHISPERS]\n[NCP:BUDGET] x",
    ))
    return store


def test_chunk_containing_section_markers_does_not_move_the_split(tmp_path: Path) -> None:
    from ncp.adapters.anthropic import AnthropicAdapter

    assembler = Assembler(store=_marker_store(tmp_path))
    context = _assemble_at(assembler, 0, 0)
    assert "payment retry note: the [NCP:WHISPERS]" in context  # chunk really is in the block

    offset = context.volatile_offset  # type: ignore[attr-defined]
    assert context.startswith("[NCP:WHISPERS]", offset)
    assert "[NCP:WHISPERS]" in context[:offset]  # the in-chunk literal sits inside the cached prefix

    cached, tail = AnthropicAdapter._split_stable_prefix(context)
    assert cached["text"] == context[:offset]
    assert tail["text"] == context[offset:]
    # A plain string (offsets lost, e.g. after middleware) still splits at a real marker.
    plain_cached, plain_tail = AnthropicAdapter._split_stable_prefix(str(context))
    assert plain_cached["text"] == cached["text"] and plain_tail["text"] == tail["text"]


@pytest.mark.parametrize("hours", [3, 9, 48])
def test_marker_chunk_prefix_stable_across_multi_hour_gap(tmp_path: Path, hours: int) -> None:
    assembler = Assembler(store=_marker_store(tmp_path))
    first = _assemble_at(assembler, 0, 0)
    later = _assemble_at(assembler, hours * 3600, 1)
    assert _prefix_by_offset(first) == _prefix_by_offset(later)


def _prefix_by_offset(context: str) -> str:
    return context[: context.volatile_offset]  # type: ignore[attr-defined]


def test_delta_mode_prefix_is_stable_across_wall_time(tmp_path: Path) -> None:
    from ncp.assembler import DeltaContext, chunk_content_hash

    assembler = Assembler(store=_store(tmp_path, whisper=True))
    with patch("time.time", _REAL_TIME):
        result = assembler.assemble(
            conscious=_conscious(), budget=BudgetContext(steps_completed=0), query_text="payment retry",
        )
    first = result.context
    delivered = {c.chunk_id: chunk_content_hash(c) for c in result.chunks}
    assert delivered == result.delivered_hashes and len(delivered) >= 2

    def at(offset: float, steps: int) -> str:
        with patch("time.time", lambda: _REAL_TIME() + offset):
            return assembler.assemble(
                conscious=_conscious(), budget=BudgetContext(steps_completed=steps),
                query_text="payment retry", delta=DeltaContext(delivered),
            ).context

    contexts = [at(offset, steps) for offset, steps in ((0, 1), (3000, 2), (30 * 3600, 3))]
    assert all("seen:1" in c for c in contexts)
    assert len({_prefix(c) for c in contexts}) == 1
    assert _prefix(first) != _prefix(contexts[0])  # header lines replace the full chunks
