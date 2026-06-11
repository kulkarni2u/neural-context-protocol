"""LangGraph integration example: NCP as the memory layer under a graph.

This is the "S3" example from docs/NCP_OPTIMIZATION_PLAN.md: a runnable
LangGraph pipeline that demonstrates NCP "sitting underneath LangGraph".

There are NO API keys and NO real model calls here. Each LangGraph node
(``planner``, ``executor``, ``reviewer``) is a small deterministic Python
function that stands in for an LLM call. Wherever a real model would be
invoked, the code is marked with a comment::

    # >>> real model call would go here <<<

The point of the example is the *memory contract*, not the text generation:

1. Each node calls ``Assembler.assemble`` to pull a small, bounded slice of
   shared context out of a single SQLite-backed ``SQLiteStore`` -- the same
   store every node and every round shares.
2. Each node does its (deterministic) "work".
3. Each node calls ``Assembler.post_turn`` to log a ``TurnRecord``, advance
   its own ``recent`` ring, and write exactly one durable
   ``SubconsciousChunk`` summarizing what it did.
4. The executor->reviewer handoff additionally emits a ``share`` whisper
   carrying a ``HandoffPayload``-shaped dict (``{"ask": ..., "files": [...]}``)
   so the reviewer's *next* assembly receives it from the whisper queue.

The LangGraph ``PipelineState`` (a TypedDict) stays intentionally tiny: it
only carries ids, a round counter, and the last short message passed between
nodes. All of the actual "history" -- plans, results, reviews, handoffs --
lives in NCP's SQLite store, not in the graph state. Each node prints its
estimated context size (via ``ncp.tokens.estimate_tokens``) every round to
make that boundedness visible.
"""

from __future__ import annotations

from pathlib import Path
import json
import tempfile

from langgraph.graph import END, StateGraph

from ncp.assembler import Assembler
from ncp.stores.sqlite import SQLiteStore
from ncp.tokens import estimate_tokens
from ncp.types import (
    BudgetContext,
    ConsciousBlock,
    NCPResponse,
    SubconsciousChunk,
    Whisper,
)

try:
    from typing import TypedDict
except ImportError:  # pragma: no cover - py<3.8 fallback, unused on 3.11+
    from typing_extensions import TypedDict


PIPELINE_ID = "pipe_langgraph_demo"
TOTAL_ROUNDS = 2


class PipelineState(TypedDict):
    """Tiny LangGraph state: ids and the last message only.

    Everything else (plans, build notes, reviews, handoff requests) is
    persisted to and retrieved from the shared NCP ``SQLiteStore`` -- the
    graph state is not where the history lives.
    """

    pipeline_id: str
    round: int
    total_rounds: int
    last_message: str
    context_tokens: dict[str, int]
    whisper_delivered: bool


def _agent(agent_id: str, role: str, owns: list[str], task: str, slot: str, intent: str) -> ConsciousBlock:
    return ConsciousBlock(
        agent_id=agent_id,
        role=role,
        owns=owns,
        must_not=[],
        task=task,
        slot=slot,
        intent=intent,
        pipeline_id=PIPELINE_ID,
    )


def _post_turn(
    *,
    assembler: Assembler,
    conscious: ConsciousBlock,
    assembly_pending_whisper_ids: list[str],
    result_summary: str,
    chunk_id: str,
    chunk_layer: str = "semantic",
) -> None:
    """Shared NCP turn-contract tail: log the turn and write one memory chunk."""

    response = NCPResponse(
        content=result_summary,
        input_tokens=estimate_tokens(result_summary),
        output_tokens=estimate_tokens(result_summary),
        cost_usd=0.0,
        model="deterministic-langgraph-node",
        pipeline_id=conscious.pipeline_id,
        turn_id=f"turn_{conscious.agent_id}_{conscious.task}",
        latency_ms=1,
    )
    assembler.post_turn(
        conscious=conscious,
        response=response,
        result_summary=result_summary,
        result_full=result_summary,
        ack_whisper_ids=assembly_pending_whisper_ids,
        memory_chunks=[
            SubconsciousChunk(
                chunk_id=chunk_id,
                layer=chunk_layer,
                content=result_summary,
                src="tool_result",
                written_by=conscious.agent_id,
                pipeline_id=conscious.pipeline_id,
            )
        ],
    )


def make_graph(store: SQLiteStore) -> StateGraph:
    """Build the planner -> executor -> reviewer LangGraph over a shared NCP store."""

    assembler = Assembler(store=store)

    def planner_node(state: PipelineState) -> PipelineState:
        round_no = state["round"]
        conscious = _agent(
            agent_id="planner",
            role="plan",
            owns=["planning"],
            task=f"plan_round_{round_no}",
            slot="outline",
            intent="prepare_executor",
        )
        assembly = assembler.assemble(
            conscious=conscious,
            budget=BudgetContext(ctx_used=min(0.9, round_no / 10)),
            query_text=f"plan round {round_no}: {state['last_message']}",
        )

        # >>> real model call would go here <<<
        # e.g. response = llm.invoke(assembly.context + "\n\n" + turn_prompt)
        plan_text = f"plan round {round_no}: bound the executor to one small step"

        context_tokens = estimate_tokens(assembly.context)
        print(f"[round {round_no}] planner   context_tokens={context_tokens}")

        _post_turn(
            assembler=assembler,
            conscious=conscious,
            assembly_pending_whisper_ids=assembly.pending_whisper_ids,
            result_summary=plan_text,
            chunk_id=f"chunk_plan_round_{round_no}",
        )

        new_tokens = dict(state["context_tokens"])
        new_tokens["planner"] = context_tokens
        return {**state, "last_message": plan_text, "context_tokens": new_tokens}

    def executor_node(state: PipelineState) -> PipelineState:
        round_no = state["round"]
        conscious = _agent(
            agent_id="executor",
            role="build",
            owns=["implementation"],
            task=f"build_round_{round_no}",
            slot="execute",
            intent="use_shared_context",
        )
        assembly = assembler.assemble(
            conscious=conscious,
            budget=BudgetContext(ctx_used=min(0.9, round_no / 10)),
            query_text=f"build round {round_no}: {state['last_message']}",
        )

        # >>> real model call would go here <<<
        build_text = f"build round {round_no}: implemented the planner's bounded step"

        context_tokens = estimate_tokens(assembly.context)
        print(f"[round {round_no}] executor  context_tokens={context_tokens}")

        _post_turn(
            assembler=assembler,
            conscious=conscious,
            assembly_pending_whisper_ids=assembly.pending_whisper_ids,
            result_summary=build_text,
            chunk_id=f"chunk_build_round_{round_no}",
        )

        # executor -> reviewer handoff: emit a "share" whisper carrying a
        # HandoffPayload-shaped dict (the `ask` field is required by
        # ncp.types.HandoffPayload).
        handoff_payload = {
            "ask": f"Review round {round_no}'s implementation for missed edge cases.",
            "files": [f"demo/round_{round_no}.py"],
        }
        store.emit_whisper(
            Whisper(
                whisper_id=f"wsp_executor_reviewer_round_{round_no}",
                from_agent="executor",
                target="reviewer",
                whisper_type="share",
                payload=json.dumps(handoff_payload),
                confidence=0.9,
                pipeline_id=PIPELINE_ID,
            )
        )
        print(f"[round {round_no}] executor -> reviewer whisper emitted: {handoff_payload['ask']!r}")

        new_tokens = dict(state["context_tokens"])
        new_tokens["executor"] = context_tokens
        return {**state, "last_message": build_text, "context_tokens": new_tokens}

    def reviewer_node(state: PipelineState) -> PipelineState:
        round_no = state["round"]
        conscious = _agent(
            agent_id="reviewer",
            role="review",
            owns=["review"],
            task=f"review_round_{round_no}",
            slot="review",
            intent="check_handoff_quality",
        )
        assembly = assembler.assemble(
            conscious=conscious,
            budget=BudgetContext(ctx_used=min(0.9, round_no / 10)),
            query_text=f"review round {round_no}: {state['last_message']}",
        )

        whisper_delivered = state["whisper_delivered"]
        for whisper in assembly.whispers:
            if whisper.whisper_type == "share" and whisper.from_agent == "executor":
                payload = whisper.payload
                data = json.loads(payload) if isinstance(payload, str) else payload
                print(f"[round {round_no}] reviewer  received whisper from executor: ask={data.get('ask')!r}")
                whisper_delivered = True

        # >>> real model call would go here <<<
        review_text = f"review round {round_no}: handoff acknowledged, no blocking issues"

        context_tokens = estimate_tokens(assembly.context)
        print(f"[round {round_no}] reviewer  context_tokens={context_tokens}")

        _post_turn(
            assembler=assembler,
            conscious=conscious,
            assembly_pending_whisper_ids=assembly.pending_whisper_ids,
            result_summary=review_text,
            chunk_id=f"chunk_review_round_{round_no}",
        )

        new_tokens = dict(state["context_tokens"])
        new_tokens["reviewer"] = context_tokens
        return {
            **state,
            "last_message": review_text,
            "context_tokens": new_tokens,
            "whisper_delivered": whisper_delivered,
            "round": round_no + 1,
        }

    def route_after_review(state: PipelineState) -> str:
        return "planner" if state["round"] <= state["total_rounds"] else END

    graph = StateGraph(PipelineState)
    graph.add_node("planner", planner_node)
    graph.add_node("executor", executor_node)
    graph.add_node("reviewer", reviewer_node)
    graph.set_entry_point("planner")
    graph.add_edge("planner", "executor")
    graph.add_edge("executor", "reviewer")
    graph.add_conditional_edges("reviewer", route_after_review, {"planner": "planner", END: END})
    return graph


def main() -> dict[str, object]:
    """Run the planner/executor/reviewer LangGraph over a temp SQLite NCP store."""

    with tempfile.TemporaryDirectory(prefix="ncp_langgraph_") as tmp:
        store = SQLiteStore(Path(tmp) / "store.db")
        graph = make_graph(store).compile()

        initial_state: PipelineState = {
            "pipeline_id": PIPELINE_ID,
            "round": 1,
            "total_rounds": TOTAL_ROUNDS,
            "last_message": "kickoff: build a small bounded feature",
            "context_tokens": {},
            "whisper_delivered": False,
        }

        final_state = graph.invoke(initial_state)

        result = {
            "rounds": TOTAL_ROUNDS,
            "final_context_tokens": final_state["context_tokens"],
            "whisper_delivered": final_state["whisper_delivered"],
            "turn_record_count": store.status()["turn_record_count"],
        }
        return result


if __name__ == "__main__":
    outcome = main()
    print()
    print("=== outcome ===")
    print(json.dumps(outcome, indent=2))
