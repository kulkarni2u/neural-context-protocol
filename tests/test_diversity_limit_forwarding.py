"""Tests for 0.11.x Slice 1: diversity_limit wire-through assembler/API/MCP/run.py.

All tests RED before implementation, GREEN after.
"""

from __future__ import annotations

import inspect
from pathlib import Path
from unittest.mock import MagicMock

from ncp.types import ConsciousBlock


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_store_spy() -> tuple[MagicMock, list[dict]]:
    """Return (mock_store, call_log) where call_log captures store.query kwargs."""
    call_log: list[dict] = []

    def _query(text, **kwargs):
        call_log.append(kwargs)
        return []

    store = MagicMock()
    store.query = MagicMock(side_effect=_query)
    store.peek_whispers = MagicMock(return_value=[])
    store.acknowledge_whispers = MagicMock(return_value=0)
    store.get_pipeline_goal_versions = MagicMock(return_value={})
    store.coordination = None  # prevent MCP server from treating mock attrs as coordination
    return store, call_log


def _conscious() -> ConsciousBlock:
    return ConsciousBlock(
        agent_id="test_agent",
        role="build",
        owns=[],
        must_not=[],
        task="implement_feature",
        slot="bounded_context",
        intent="test",
    )


# ---------------------------------------------------------------------------
# Slice 1a: assembler methods have diversity_limit param
# ---------------------------------------------------------------------------

def test_retrieve_chunks_has_diversity_limit_param() -> None:
    """Assembler._retrieve_chunks must accept diversity_limit."""
    from ncp.assembler import Assembler

    sig = inspect.signature(Assembler._retrieve_chunks)
    assert "diversity_limit" in sig.parameters, (
        "Assembler._retrieve_chunks missing diversity_limit parameter"
    )


def test_assemble_has_diversity_limit_param() -> None:
    """Assembler.assemble must accept diversity_limit."""
    from ncp.assembler import Assembler

    sig = inspect.signature(Assembler.assemble)
    assert "diversity_limit" in sig.parameters, (
        "Assembler.assemble missing diversity_limit parameter"
    )


def test_assemble_incremental_has_diversity_limit_param() -> None:
    """Assembler.assemble_incremental must accept diversity_limit."""
    from ncp.assembler import Assembler

    sig = inspect.signature(Assembler.assemble_incremental)
    assert "diversity_limit" in sig.parameters, (
        "Assembler.assemble_incremental missing diversity_limit parameter"
    )


# ---------------------------------------------------------------------------
# Slice 1b: assembler forwards diversity_limit to store.query
# ---------------------------------------------------------------------------

def test_assemble_forwards_diversity_limit_to_store(tmp_path: Path) -> None:
    """assemble(diversity_limit=1) must forward diversity_limit=1 to store.query."""
    from ncp.assembler import Assembler
    from ncp.types import BudgetContext

    store, call_log = _make_store_spy()
    assembler = Assembler(store=store)

    assembler.assemble(
        conscious=_conscious(),
        budget=BudgetContext(pressure="low"),
        query_text="authentication bearer token",
        diversity_limit=1,
    )

    assert call_log, "store.query was never called"
    assert call_log[0].get("diversity_limit") == 1, (
        f"Expected diversity_limit=1 forwarded to store.query, got: {call_log[0]}"
    )


def test_assemble_default_diversity_limit_not_forwarded(tmp_path: Path) -> None:
    """assemble() without diversity_limit must not pass diversity_limit to store.query
    (letting the store use its own default)."""
    from ncp.assembler import Assembler
    from ncp.types import BudgetContext

    store, call_log = _make_store_spy()
    assembler = Assembler(store=store)

    assembler.assemble(
        conscious=_conscious(),
        budget=BudgetContext(pressure="low"),
        query_text="authentication bearer token",
        # no diversity_limit
    )

    assert call_log, "store.query was never called"
    assert "diversity_limit" not in call_log[0], (
        f"diversity_limit should not be forwarded when not set, got: {call_log[0]}"
    )


def test_assemble_incremental_forwards_diversity_limit(tmp_path: Path) -> None:
    """assemble_incremental(diversity_limit=3) must forward to store.query."""
    from ncp.assembler import Assembler
    from ncp.types import BudgetContext

    store, call_log = _make_store_spy()
    assembler = Assembler(store=store)

    list(assembler.assemble_incremental(
        conscious=_conscious(),
        budget=BudgetContext(pressure="low"),
        query_text="authentication",
        diversity_limit=3,
    ))

    assert call_log, "store.query was never called"
    assert call_log[0].get("diversity_limit") == 3, (
        f"Expected diversity_limit=3 forwarded, got: {call_log[0]}"
    )


# ---------------------------------------------------------------------------
# Slice 1c: api.py functions have diversity_limit param
# ---------------------------------------------------------------------------

def test_api_get_context_has_diversity_limit_param() -> None:
    """api.get_context must accept diversity_limit."""
    from ncp import api

    sig = inspect.signature(api.get_context)
    assert "diversity_limit" in sig.parameters, (
        "api.get_context missing diversity_limit parameter"
    )


def test_api_run_has_diversity_limit_param() -> None:
    """api.run must accept diversity_limit."""
    from ncp import api

    sig = inspect.signature(api.run)
    assert "diversity_limit" in sig.parameters, (
        "api.run missing diversity_limit parameter"
    )


def test_api_stream_has_diversity_limit_param() -> None:
    """api.stream must accept diversity_limit."""
    from ncp import api

    sig = inspect.signature(api.stream)
    assert "diversity_limit" in sig.parameters, (
        "api.stream missing diversity_limit parameter"
    )


def test_api_get_context_forwards_diversity_limit(tmp_path: Path) -> None:
    """api.get_context(diversity_limit=1) must reach store.query with diversity_limit=1."""
    from ncp import api
    from ncp.types import BudgetContext

    store, call_log = _make_store_spy()
    api.get_context(
        agent=_conscious(),
        budget=BudgetContext(pressure="low"),
        query_text="bearer token authentication",
        store=store,
        diversity_limit=1,
    )

    assert call_log, "store.query was never called"
    assert call_log[0].get("diversity_limit") == 1, (
        f"Expected diversity_limit=1 forwarded through api.get_context, got: {call_log[0]}"
    )


# ---------------------------------------------------------------------------
# Slice 1d: MCP server extracts and forwards diversity_limit
# ---------------------------------------------------------------------------

def test_mcp_get_context_schema_has_diversity_limit() -> None:
    """ncp_get_context MCP tool inputSchema must include diversity_limit."""
    from ncp.mcp.server import MCP_TOOLS

    get_ctx_tool = next((t for t in MCP_TOOLS if t["name"] == "ncp_get_context"), None)
    assert get_ctx_tool is not None
    schema_props = get_ctx_tool.get("inputSchema", {}).get("properties", {})
    assert "diversity_limit" in schema_props, (
        "ncp_get_context inputSchema missing diversity_limit property"
    )


def test_mcp_fetch_schema_has_diversity_limit() -> None:
    """ncp_fetch MCP tool inputSchema must include diversity_limit."""
    from ncp.mcp.server import MCP_TOOLS

    fetch_tool = next((t for t in MCP_TOOLS if t["name"] == "ncp_fetch"), None)
    assert fetch_tool is not None
    schema_props = fetch_tool.get("inputSchema", {}).get("properties", {})
    assert "diversity_limit" in schema_props, (
        "ncp_fetch inputSchema missing diversity_limit property"
    )


# ---------------------------------------------------------------------------
# Slice 1e: MCP handler behavioral — diversity_limit actually forwarded
# ---------------------------------------------------------------------------

def test_mcp_get_context_handler_forwards_diversity_limit() -> None:
    """_handle_get_context must extract diversity_limit from args and forward to store.query."""
    from ncp.mcp.server import make_handlers

    store, call_log = _make_store_spy()
    handlers = make_handlers(store)

    handlers["ncp_get_context"]({
        "agent_id": "test_agent",
        "role": "build",
        "task": "implement_feature",
        "slot": "bounded_context",
        "intent": "test",
        "diversity_limit": 3,
    })

    assert call_log, "store.query was never called"
    assert call_log[0].get("diversity_limit") == 3, (
        f"Expected diversity_limit=3 forwarded from _handle_get_context, got: {call_log[0]}"
    )


def test_mcp_fetch_handler_forwards_diversity_limit() -> None:
    """_handle_fetch must extract diversity_limit from args and forward to store.query."""
    from ncp.mcp.server import make_handlers

    store, call_log = _make_store_spy()
    handlers = make_handlers(store)

    handlers["ncp_fetch"]({
        "query": "bearer token authentication",
        "diversity_limit": 1,
    })

    assert call_log, "store.query was never called"
    assert call_log[0].get("diversity_limit") == 1, (
        f"Expected diversity_limit=1 forwarded from _handle_fetch, got: {call_log[0]}"
    )
