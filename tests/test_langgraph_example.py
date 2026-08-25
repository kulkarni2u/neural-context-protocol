"""Tests for the LangGraph integration example (examples/03_langgraph)."""

from __future__ import annotations

import importlib.util
from pathlib import Path
import types

import pytest

pytest.importorskip("langgraph")

from ncp.stores.sqlite import SQLiteStore  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[1]


def _load_pipeline_module() -> types.ModuleType:
    path = REPO_ROOT / "examples" / "03_langgraph" / "pipeline.py"
    spec = importlib.util.spec_from_file_location("ncp_example_03_langgraph_pipeline", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_langgraph_pipeline_runs_bounded_with_whisper_handoff() -> None:
    module = _load_pipeline_module()

    outcome = module.main()

    assert outcome["whisper_delivered"] is True
    assert outcome["rounds"] >= 2

    final_tokens = outcome["final_context_tokens"]
    assert final_tokens, "expected per-node final context token counts"
    for agent_id, tokens in final_tokens.items():
        assert tokens < 800, f"{agent_id} context grew unbounded: {tokens} tokens"


def test_memoized_work_skips_repeated_work_fn_on_exact_match(tmp_path: Path) -> None:
    """CAP-C3 wiring: the same task+context skips ``work_fn`` on the second call."""

    module = _load_pipeline_module()
    store = SQLiteStore(tmp_path / "store.db")
    calls = []

    def work_fn() -> str:
        calls.append(1)
        return "computed_result"

    first, first_hit = module._memoized_work(store, task="classify_chunk", context="round_1", work_fn=work_fn)
    second, second_hit = module._memoized_work(store, task="classify_chunk", context="round_1", work_fn=work_fn)

    assert first_hit is False
    assert second_hit is True
    assert first == second == "computed_result"
    assert len(calls) == 1, "work_fn must not run again on a memo hit"


def test_memoized_work_does_not_hit_across_different_context(tmp_path: Path) -> None:
    module = _load_pipeline_module()
    store = SQLiteStore(tmp_path / "store.db")

    _, first_hit = module._memoized_work(store, task="classify_chunk", context="round_1", work_fn=lambda: "r1")
    _, second_hit = module._memoized_work(store, task="classify_chunk", context="round_2", work_fn=lambda: "r2")

    assert first_hit is False
    assert second_hit is False
