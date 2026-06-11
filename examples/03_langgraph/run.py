"""LangGraph-style recipe for using NCP as graph memory.

The file runs without LangGraph installed so it can stay in the default CI
suite. The node functions are deliberately shaped like LangGraph node callables:
they accept a state dict and return a partial state update. See README.md in
this directory for the direct StateGraph wiring.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
import json
import tempfile

import ncp
from ncp.adapters.local import LocalAdapter
from ncp.stores.sqlite import SQLiteStore
from ncp.types import SubconsciousChunk, Whisper


PIPELINE_ID = "pipe_langgraph_recipe"


def _agent(agent_id: str, role: str, slot: str, intent: str) -> ncp.api.Agent:
    return ncp.agent(
        id=agent_id,
        role=role,
        owns=[role],
        must_not=["rewrite_graph_control_flow"],
        task="langgraph_recipe",
        slot=slot,
        intent=intent,
        pipeline_id=PIPELINE_ID,
    )


def planner_node(state: dict[str, Any], *, store: SQLiteStore, adapter: LocalAdapter) -> dict[str, Any]:
    planner = _agent("planner", "plan", "graph_node", "prepare_executor")
    response = ncp.run(
        agent=planner,
        turn=f"Plan the next graph step for: {state['task']}",
        adapter=adapter,
        store=store,
    )
    ncp.write_memory(
        SubconsciousChunk(
            chunk_id="sub_langgraph_plan",
            layer="semantic",
            content="langgraph_recipe executor should implement the planner step with bounded context",
            src="synthesis",
            written_by="planner",
            pipeline_id=PIPELINE_ID,
            relevance=0.95,
        ),
        store=store,
    )
    ncp.emit(
        Whisper(
            whisper_id="wsp_langgraph_plan_ready",
            from_agent="planner",
            target="executor",
            whisper_type="share",
            payload={
                "slice": "planner_step_ready",
                "files": [],
                "ask": "Use the planner memory in the executor node.",
            },
            confidence=0.9,
            pipeline_id=PIPELINE_ID,
        ),
        store=store,
    )
    return {"planner_result": response.content.splitlines()[0]}


def executor_node(state: dict[str, Any], *, store: SQLiteStore, adapter: LocalAdapter) -> dict[str, Any]:
    executor = _agent("executor", "build", "graph_node", "use_planner_context")
    context = ncp.get_context(agent=executor, store=store)
    pending_before_turn = store.peek_whispers(
        agent_id="executor",
        pipeline_id=PIPELINE_ID,
        max_items=10,
    )
    store.acknowledge_whispers(
        [whisper.whisper_id for whisper in pending_before_turn],
        agent_id="executor",
    )
    response = ncp.run(
        agent=executor,
        turn="Use the planner memory and produce the bounded graph result.",
        adapter=adapter,
        store=store,
    )
    pending_after_turn = store.peek_whispers(
        agent_id="executor",
        pipeline_id=PIPELINE_ID,
        max_items=10,
    )
    ncp.write_memory(
        SubconsciousChunk(
            chunk_id="sub_langgraph_result",
            layer="semantic",
            content="langgraph_recipe critic should verify executor produced a bounded result",
            src="tool_result",
            written_by="executor",
            pipeline_id=PIPELINE_ID,
            relevance=0.92,
        ),
        store=store,
    )
    return {
        "executor_result": response.content.splitlines()[0],
        "executor_context_has_plan": "planner step" in context or "planner memory" in context,
        "pending_whispers_acknowledged": not pending_after_turn,
    }


def critic_node(state: dict[str, Any], *, store: SQLiteStore, adapter: LocalAdapter) -> dict[str, Any]:
    critic = _agent("critic", "review", "graph_node", "check_bounded_result")
    context = ncp.get_context(agent=critic, store=store)
    response = ncp.run(
        agent=critic,
        turn="Review whether the graph used NCP memory instead of replaying the transcript.",
        adapter=adapter,
        store=store,
    )
    return {
        "critic_result": response.content.splitlines()[0],
        "critic_context_has_result": "bounded result" in context,
    }


def run_recipe() -> dict[str, Any]:
    with tempfile.TemporaryDirectory(prefix="ncp_langgraph_") as tmp:
        project_root = Path(tmp)
        (project_root / ".git").mkdir()
        ncp.configure(cwd=project_root)
        store = SQLiteStore(project_root / ".ncp" / "store.db")
        adapter = LocalAdapter()

        state: dict[str, Any] = {"task": "Ship a bounded graph handoff"}
        for node in (planner_node, executor_node, critic_node):
            state.update(node(state, store=store, adapter=adapter))

        return {
            "mode": "langgraph_recipe",
            "nodes": ["planner", "executor", "critic"],
            "planner_result": state["planner_result"],
            "executor_result": state["executor_result"],
            "critic_result": state["critic_result"],
            "executor_context_has_plan": state["executor_context_has_plan"],
            "critic_context_has_result": state["critic_context_has_result"],
            "pending_whispers_acknowledged": state["pending_whispers_acknowledged"],
            "turn_records": store.status()["turn_record_count"],
        }


def main() -> None:
    print(json.dumps(run_recipe(), indent=2))


if __name__ == "__main__":
    main()
