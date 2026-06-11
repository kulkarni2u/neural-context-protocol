"""Deterministic local demo for the NCP value loop."""

from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from tempfile import TemporaryDirectory
import json
from typing import Iterator

from ncp.assembler import Assembler
from ncp.stores.sqlite import SQLiteStore
from ncp.tokens import estimate_tokens
from ncp.types import BudgetContext, ConsciousBlock, NCPResponse, SubconsciousChunk, Whisper


DEMO_TURNS = [
    ("planner", "plan_auth_refresh", "Define the retry contract for auth refresh and hand it to the executor."),
    ("executor", "build_auth_refresh", "Implement the auth refresh retry contract and note edge cases."),
    ("critic", "review_auth_refresh", "Review auth refresh for missed retry and telemetry risks."),
    ("planner", "plan_stream_resume", "Plan stream resume logic using prior review constraints."),
    ("executor", "build_stream_resume", "Implement stream resume logic with bounded retry state."),
    ("critic", "review_stream_resume", "Review stream resume behavior and close the handoff loop."),
]


def run_demo(
    *,
    pipeline_id: str = "demo_pipeline",
    store_path: Path | None = None,
    context_token_budget: int = 340,
) -> dict[str, object]:
    """Run a no-API deterministic 3-agent demo and return a report payload."""

    with _demo_store(store_path) as store:
        assembler = Assembler(store=store)
        raw_transcript: list[str] = []
        recent_by_agent: dict[str, list[str]] = {}
        turn_rows: list[dict[str, object]] = []
        handoffs: list[dict[str, object]] = []

        for turn_number, (agent_id, task, prompt) in enumerate(DEMO_TURNS, start=1):
            role = "review" if agent_id == "critic" else "build"
            conscious = ConsciousBlock(
                agent_id=agent_id,
                role=role,
                owns=[role],
                must_not=[],
                task=task,
                slot="demo",
                intent="show_ncp_value",
                pipeline_id=pipeline_id,
                recent=recent_by_agent.get(agent_id, []),
            )
            raw_prompt = "\n\n".join([*raw_transcript, f"{agent_id}: {prompt}"])
            raw_tokens = estimate_tokens(raw_prompt)
            assembled = assembler.assemble(
                conscious=conscious,
                budget=BudgetContext(ctx_used=min(0.9, turn_number / 10)),
                query_text=prompt,
                max_tokens=context_token_budget,
            )
            delivered_whispers = [
                {
                    "from": whisper.from_agent,
                    "to": whisper.target,
                    "type": whisper.whisper_type,
                    "payload": whisper.payload,
                }
                for whisper in assembled.whispers
                if whisper.whisper_type not in {"alert", "world_check", "sensor"}
            ]
            handoffs.extend(delivered_whispers)
            ncp_tokens = estimate_tokens(f"{assembled.context}\n{agent_id}: {prompt}")
            result_summary = _demo_summary(turn_number=turn_number, agent_id=agent_id, prompt=prompt)
            response = NCPResponse(
                content=result_summary,
                input_tokens=ncp_tokens,
                output_tokens=estimate_tokens(result_summary),
                cost_usd=0.0,
                model="deterministic-demo",
                pipeline_id=pipeline_id,
                turn_id=f"demo_turn_{turn_number}",
                latency_ms=1,
            )
            assembler.post_turn(
                conscious=conscious,
                response=response,
                result_summary=result_summary,
                result_full=result_summary,
                ack_whisper_ids=assembled.pending_whisper_ids,
                memory_chunks=[
                    SubconsciousChunk(
                        chunk_id=f"demo_chunk_{turn_number}",
                        layer="semantic",
                        content=result_summary,
                        src="tool_result",
                        written_by=agent_id,
                        pipeline_id=pipeline_id,
                    )
                ],
            )
            recent_by_agent[agent_id] = [f"r:sub/demo_turn_{turn_number}", *recent_by_agent.get(agent_id, [])][:5]
            raw_transcript.append(f"{agent_id}: {prompt}\n{result_summary}")
            _emit_demo_handoff(store=store, pipeline_id=pipeline_id, turn_number=turn_number, agent_id=agent_id)
            turn_rows.append(
                {
                    "turn": turn_number,
                    "agent_id": agent_id,
                    "raw_replay_tokens": raw_tokens,
                    "ncp_tokens": ncp_tokens,
                    "savings_tokens": raw_tokens - ncp_tokens,
                }
            )

        final_row = turn_rows[-1]
        return {
            "mode": "deterministic_demo",
            "pipeline_id": pipeline_id,
            "turn_rows": turn_rows,
            "handoffs": handoffs,
            "summary": {
                "turns": len(turn_rows),
                "final_raw_replay_tokens": final_row["raw_replay_tokens"],
                "final_ncp_tokens": final_row["ncp_tokens"],
                "final_savings_tokens": final_row["savings_tokens"],
                "whisper_handoff_delivered": bool(handoffs),
            },
        }


@contextmanager
def _demo_store(store_path: Path | None) -> Iterator[SQLiteStore]:
    if store_path is not None:
        yield SQLiteStore(store_path)
        return
    with TemporaryDirectory(prefix="ncp-demo-") as tmp:
        yield SQLiteStore(Path(tmp) / "demo.db")


def _emit_demo_handoff(
    *,
    store: SQLiteStore,
    pipeline_id: str,
    turn_number: int,
    agent_id: str,
) -> None:
    if turn_number == 1 and agent_id == "planner":
        store.emit_whisper(
            Whisper(
                whisper_id="demo_handoff_planner_executor",
                from_agent="planner",
                target="executor",
                whisper_type="share",
                payload=json.dumps({"ask": "Build the retry contract from the planner summary.", "files": ["demo/auth.py"]}),
                confidence=0.9,
                pipeline_id=pipeline_id,
            )
        )
    if turn_number == 2 and agent_id == "executor":
        store.emit_whisper(
            Whisper(
                whisper_id="demo_handoff_executor_critic",
                from_agent="executor",
                target="critic",
                whisper_type="request",
                payload=json.dumps({"ask": "Review retry behavior and telemetry naming.", "files": ["demo/auth.py"]}),
                confidence=0.9,
                pipeline_id=pipeline_id,
            )
        )


def _demo_summary(*, turn_number: int, agent_id: str, prompt: str) -> str:
    repeated_detail = " ".join(
        [
            "decision: bounded context keeps the cross-agent state compact",
            "constraint: handoffs remain explicit and attributable",
            "evidence: deterministic demo compares raw replay against NCP assembly",
        ]
    )
    return f"turn:{turn_number} agent:{agent_id} prompt:{prompt} {repeated_detail}"
