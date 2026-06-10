"""Tests for the LangGraph integration example (examples/03_langgraph)."""

from __future__ import annotations

import importlib.util
from pathlib import Path
import types

import pytest

pytest.importorskip("langgraph")

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
