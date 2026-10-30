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


def _store(tmp_path: Path, *, whisper: bool) -> SQLiteStore:
    store = SQLiteStore(tmp_path / "ncp.db")
    for i in range(3):
        store.write(SubconsciousChunk(
            chunk_id=f"c{i}", layer="semantic", content=f"payment retry fact {i}",
            src="tool_result", pipeline_id="p", written_by="w",
        ))
    if whisper:
        store.emit_whisper(Whisper(
            from_agent="planner", target="executor", whisper_type="share",
            payload={"slice": "retry", "files": ["pay.py"], "ask": "check backoff"},
            confidence=0.9, pipeline_id="p", ttl_seconds=7200,
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

    contexts = [_assemble_at(assembler, offset, steps) for offset, steps in ((0, 0), (150, 1), (3000, 2))]

    assert len({_prefix(c) for c in contexts}) == 1
    assert "[NCP:SUBCONSCIOUS]" in _prefix(contexts[0])
    if whisper:
        # The volatile tail really is volatile: the whisper age moved.
        assert len({c[c.find("[NCP:WHISPERS]"):] for c in contexts}) == 3


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
