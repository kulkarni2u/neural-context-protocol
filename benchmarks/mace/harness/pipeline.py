"""Deterministic pipeline harness for MACE."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from ncp.api import agent
from ncp.assembler import Assembler
from ncp.stores.sqlite import SQLiteStore
from ncp.types import BudgetContext, ConsciousBlock, NCPResponse, SubconsciousChunk

from .agents import AgentOutput, generate_agent_output


@dataclass(slots=True)
class TurnOutcome:
    """One deterministic benchmark turn."""

    turn: int
    agent_id: str
    conscious: ConsciousBlock
    context: str
    output: AgentOutput
    response: NCPResponse


class MACEPipeline:
    """Run deterministic rounds against the real NCP store and assembler."""

    AGENT_ROLES: dict[str, tuple[str, list[str], list[str]]] = {
        "planner": ("plan", ["planning"], ["shipping"]),
        "executor": ("build", ["implementation"], ["review"]),
        "critic": ("review", ["verification"], ["implementation"]),
    }

    def __init__(self, *, store_path: str | Path, pipeline_id: str = "mace_pipeline") -> None:
        self.pipeline_id = pipeline_id
        self.store = SQLiteStore(store_path)
        self.whisper_bus = self.store
        self.assembler = Assembler(store=self.store)
        self._recent_by_agent: dict[str, list[str]] = {}

    def run_turn(
        self,
        *,
        agent_id: str,
        turn_n: int,
        task: str,
        goal: str,
        goal_version: int = 1,
        query_text: str | None = None,
        tried: list[str] | None = None,
        failed: list[str] | None = None,
        k: int = 4,
    ) -> TurnOutcome:
        """Assemble context, generate deterministic output, and persist the turn."""

        role, owns, must_not = self.AGENT_ROLES[agent_id]
        task_id = task.replace(" ", "_").replace("-", "_").lower()
        conscious = agent(
            id=agent_id,
            role=role,
            owns=owns,
            must_not=must_not,
            task=task_id,
            slot=f"{agent_id}_turn_{turn_n:02d}",
            intent="advance_pipeline",
            pipeline_id=self.pipeline_id,
            goal_version=goal_version,
            recent=self._recent_by_agent.get(agent_id, []),
            tried=tried or [],
            failed=failed or [],
            steps_completed=max(0, turn_n - 1),
            steps_total=40,
        )
        budget = BudgetContext(
            ctx_used=min(0.95, turn_n / 40),
            steps_completed=max(0, turn_n - 1),
            steps_total=40,
            elapsed_seconds=float(turn_n * 7),
            pressure="medium",
        )
        assembly = self.assembler.assemble(
            conscious=conscious,
            budget=budget,
            query_text=query_text or f"{task} {goal}",
            k=k,
        )
        output = generate_agent_output(agent_id=agent_id, context=assembly.context, goal=goal, turn_n=turn_n)
        response = NCPResponse(
            content=output.summary,
            input_tokens=len((assembly.context + "\n" + output.summary).split()),
            output_tokens=len(output.summary.split()),
            cost_usd=0.0,
            model="mace_deterministic",
            pipeline_id=self.pipeline_id,
            turn_id=f"mace_turn_{turn_n:02d}_{agent_id}",
            latency_ms=1,
        )
        memory_chunk = SubconsciousChunk(
            chunk_id=f"mace_chunk_{turn_n:02d}_{agent_id}",
            layer="semantic",
            content=output.summary,
            src="synthesis",
            pipeline_id=self.pipeline_id,
            written_by=agent_id,
            relevance=0.92,
            age_seconds=0.0,
        )
        record = self.assembler.post_turn(
            conscious=assembly.conscious,
            response=response,
            result_summary=output.summary,
            result_full=output.summary,
            memory_chunks=[memory_chunk],
        )
        self._recent_by_agent[agent_id] = [f"r:sub/{record.turn_id}", *self._recent_by_agent.get(agent_id, [])][:5]
        return TurnOutcome(
            turn=turn_n,
            agent_id=agent_id,
            conscious=assembly.conscious,
            context=assembly.context,
            output=output,
            response=response,
        )

    def run_round(
        self,
        *,
        turn_n: int,
        task: str,
        goal: str,
        goal_version: int = 1,
        query_text: str | None = None,
    ) -> list[TurnOutcome]:
        """Run planner/executor/critic once each for a given round."""

        outcomes: list[TurnOutcome] = []
        for agent_id in ["planner", "executor", "critic"]:
            outcomes.append(
                self.run_turn(
                    agent_id=agent_id,
                    turn_n=turn_n,
                    task=task,
                    goal=goal,
                    goal_version=goal_version,
                    query_text=query_text,
                )
            )
        return outcomes

    def close(self) -> None:
        """Close the underlying store if it exposes a close hook."""

        close = getattr(self.store, "close", None)
        if callable(close):
            close()
