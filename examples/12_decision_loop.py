"""Harness-agnostic decision loop: compile -> backend -> record -> outcome.

NCP's typed decision contract (spec §4h) has no preferred host. This example is
deliberately written without a framework: no LangGraph, no MCP client, no
orchestrator. It is the three calls any harness makes, in the order it makes
them, with the deciding function left as a parameter -- because the whole point
is that NCP does not care what decides.

Run it:

    python examples/12_decision_loop.py

The same loop over the other two integration surfaces the integration guide
documents:

* **MCP client** -- call ``ncp_compile_decision_query``, then
  ``ncp_record_decision``, then ``ncp_record_outcome`` as tools. Identical
  arguments and identical result shapes; the library functions used below are
  thin wrappers over the very same handlers.
* **Raw HTTP JSON-RPC** -- POST the same three tool names to ``/mcp``. See
  ``docs/NCP_HTTP_API.md`` for the exact envelopes. This is the path for
  harnesses that do not embed Python at all (n8n, a Go service, a shell
  pipeline).

What a harness gets out of this: the decision slots in a pipeline -- classify,
route, score, choose -- stop costing a frontier model call each, become
durable and queryable instead of prose to re-parse, and start reusing their own
history when the same situation recurs.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
import json
import tempfile

import ncp
from ncp.config import load_config
from ncp.stores.sqlite import SQLiteStore
from ncp.types import ConsciousBlock, DecisionRecord, SubconsciousChunk

PIPELINE_ID = "pipe_decision_example"
SCHEMA_ID = "ncp.slot.continue_or_escalate"

# A decision backend: (state, questions) -> choice. Swap any of these in and
# nothing else in the loop changes -- that is the contract.
Backend = Callable[[dict, list], object]


def rule_backend(state: dict, questions: list) -> object:
    """A plain rule. No model, no network, microseconds."""
    evidence = state["evidence"]
    if not evidence:
        return "escalate"
    failing = [e for e in evidence if "fail" in e["content"].lower()]
    return "escalate" if len(failing) > len(evidence) / 2 else "continue"


def human_backend(state: dict, questions: list) -> object:
    """A person. Same shape; NCP cannot tell the difference and does not try."""
    options = questions[0].get("options", [])
    print(f"    [human] {len(state['evidence'])} pieces of evidence; options={options}")
    return options[0] if options else "continue"


def model_backend(state: dict, questions: list) -> object:
    """Where a JSON-mode or schema-constrained model call would go.

    The compiled packet is already the prompt: `state` is bounded context and
    `questions` is the output contract. Nothing needs re-parsing out of pidgin.
    """
    raise NotImplementedError("wire your own provider call here")


def resolve_slot(
    store: SQLiteStore,
    config: object,
    *,
    agent: ConsciousBlock,
    schema_id: str,
    backend: Backend,
) -> tuple[object, str]:
    """One decision slot, end to end. Returns (choice, how_it_was_resolved)."""

    # 1. COMPILE. Pure store reads and arithmetic -- no provider call happens
    #    here, by contract.
    packet = ncp.compile_decision_query(
        agent=agent, schema_id=schema_id, store=store, config=config
    )

    # 2. REUSE, when NCP has seen this exact state decided before. `escalate`
    #    being false means nothing is telling us to think harder about it.
    #    NCP surfaces the candidate and stops there; applying it is the
    #    harness's call, which is why this `if` lives in your code and not
    #    inside the bus.
    if not packet["escalate"] and "suggested_choice" in packet:
        return packet["suggested_choice"], f"reused ({packet['suggested_basis']})"

    # 3. ESCALATE, when the packet says the cheap path is not safe. The reasons
    #    are a closed set and never name a model: what to call is your policy.
    if packet["escalate"]:
        reasons = ", ".join(packet["escalate_reasons"])
        print(f"    escalate: {reasons} (jc={packet['joint_confidence']})")
        # A real harness would call a generating model with normal
        # ncp.get_context pidgin here. This example decides anyway, to keep
        # the loop runnable end to end.

    # 4. DECIDE with whatever you use.
    choice = backend(packet["state"], packet["questions"])

    # 5. RECORD. A choice you do not record cannot become anyone's precedent,
    #    including your own next turn.
    decision = DecisionRecord(
        schema_id=schema_id,
        slot=agent.slot,
        pipeline_id=PIPELINE_ID,
        agent_id=agent.agent_id,
        choice=choice,
        confidence=float(packet["joint_confidence"]),
        confidence_source="backend_claimed",
        backend="rule",
        state_hash=str(packet["state_hash"]),
        chunk_ids=[e["chunk_id"] for e in packet["state"]["evidence"]],
    )
    ncp.record_decision(decision, store=store, config=config)
    return choice, f"decided (decision_id={decision.decision_id})"


def _agent(task: str, slot: str) -> ConsciousBlock:
    # `task` is part of the state identity state_hash covers, so it names the
    # DECISION, not the iteration. "deploy_gate_round_4" would be a brand new
    # state every round and could never match a precedent.
    return ConsciousBlock(
        agent_id="decider",
        role="operator",
        owns=["deploy"],
        must_not=["rollback"],
        task=task,
        slot=slot,
        intent="keep-the-pipeline-moving",
        pipeline_id=PIPELINE_ID,
    )


def main() -> dict[str, object]:
    with tempfile.TemporaryDirectory(prefix="ncp_decisions_") as tmp:
        root = Path(tmp)
        config = load_config(cwd=root)
        store = SQLiteStore(root / "store.db")

        for content in (
            "the smoke suite passed on the release candidate build",
            "latency at p99 held under the agreed ceiling for thirty minutes",
        ):
            store.write(SubconsciousChunk(
                layer="semantic", content=content, src="tool_result",
                written_by="ci", pipeline_id=PIPELINE_ID,
            ))

        agent = _agent(task="deploy_gate", slot="promote-or-hold")
        results: list[dict[str, object]] = []

        print("round 1 — nothing decided yet, so the backend runs")
        choice, how = resolve_slot(
            store, config, agent=agent, schema_id=SCHEMA_ID, backend=rule_backend
        )
        print(f"    -> {choice}  [{how}]")
        results.append({"round": 1, "choice": choice, "how": how})

        print("round 2 — same state, so the prior decision comes back")
        choice, how = resolve_slot(
            store, config, agent=agent, schema_id=SCHEMA_ID, backend=rule_backend
        )
        print(f"    -> {choice}  [{how}]")
        results.append({"round": 2, "choice": choice, "how": how})

        print("round 3 — the world changed, so it is a different state again")
        store.write(SubconsciousChunk(
            layer="semantic",
            content="the canary deployment reported a failing health check twice",
            src="tool_result", written_by="ci", pipeline_id=PIPELINE_ID,
        ))
        choice, how = resolve_slot(
            store, config, agent=agent, schema_id=SCHEMA_ID, backend=rule_backend
        )
        print(f"    -> {choice}  [{how}]")
        results.append({"round": 3, "choice": choice, "how": how})

        print("round 4 — a human decides a slot with no evidence behind it")
        empty = _agent(task="policy_exception", slot="grant-or-deny")
        choice, how = resolve_slot(
            store, config, agent=empty, schema_id=SCHEMA_ID, backend=human_backend
        )
        print(f"    -> {choice}  [{how}]")
        results.append({"round": 4, "choice": choice, "how": how})

        decisions = store.query_decisions(pipeline_id=PIPELINE_ID, k=100)
        return {
            "rounds": results,
            "decisions_recorded": len(decisions),
            "reused_rounds": [r["round"] for r in results if str(r["how"]).startswith("reused")],
            "backends_demonstrated": ["rule", "human"],
            "provider_calls": 0,
        }


if __name__ == "__main__":
    outcome = main()
    print()
    print("=== outcome ===")
    print(json.dumps(outcome, indent=2))
