"""Reproducible benchmark helpers for NCP launch-credibility checks."""

from __future__ import annotations

from pathlib import Path

from ncp.bench.baselines import (
    BaselineStrategy,
    RawReplayBaseline,
    RollingSummaryBaseline,
    SlidingWindowBaseline,
)

from ncp.assembler import Assembler
from ncp.api import agent
from ncp.costs import assembly_overhead
from ncp.stores.sqlite import SQLiteStore
from ncp.tokens import estimate_tokens, token_unit
from ncp.types import BudgetContext, NCPResponse, SubconsciousChunk


_CODING_ROLE_ROTATION: list[tuple[str, str, list[str], list[str]]] = [
    ("planner", "plan", ["planning"], ["shipping"]),
    ("executor", "build", ["implementation"], ["review"]),
    ("critic", "review", ["verification"], ["implementation"]),
    ("synthesizer", "synthesize", ["handoff"], ["planning"]),
]

_CODING_TOPICS: list[str] = [
    "auth_refresh",
    "rate_limit_retry",
    "worker_queue_backpressure",
    "stream_resume_logic",
    "sqlite_migration_guard",
    "mcp_stdio_framing",
    "tool_retry_budget",
    "cost_log_rollup",
    "context_budget_pressure",
    "release_cut_scope",
]

_RESEARCH_ROLE_ROTATION: list[tuple[str, str, list[str], list[str]]] = [
    ("lead", "plan", ["hypothesis"], ["implementation"]),
    ("retriever", "retrieve", ["sources"], ["publishing"]),
    ("analyst", "analyze", ["synthesis"], ["guessing"]),
    ("factchecker", "review", ["verification"], ["speculation"]),
    ("writer", "synthesize", ["handoff"], ["retrieval"]),
    ("editor", "review", ["quality"], ["analysis"]),
]

_RESEARCH_TOPICS: list[str] = [
    "market_structure_shift",
    "regulatory_timeline_delta",
    "pricing_signal_breakdown",
    "competitor_launch_wave",
    "supply_chain_constraint",
    "customer_segment_migration",
    "infrastructure_capacity_risk",
    "benchmark_claim_validation",
]


def _baseline_metadata(baseline: BaselineStrategy) -> dict[str, object]:
    metadata: dict[str, object] = {"name": baseline.name}
    if isinstance(baseline, SlidingWindowBaseline):
        metadata["last_entries"] = baseline.last_entries
    if isinstance(baseline, RollingSummaryBaseline):
        metadata["every_k"] = baseline.every_k
        metadata["keep_recent"] = baseline.keep_recent
    return metadata


def run_coding_pipeline_benchmark(
    *,
    store_path: str | Path,
    turns: int = 40,
    pipeline_id: str = "bench_coding_pipeline",
    context_token_budget: int | None = 340,
) -> dict[str, object]:
    """Compare NCP bounded-context growth against naive full-history replay."""

    return _run_pipeline_benchmark(
        store_path=store_path,
        turns=turns,
        pipeline_id=pipeline_id,
        benchmark_name="coding_pipeline",
        role_rotation=_CODING_ROLE_ROTATION,
        topics=_CODING_TOPICS,
        turn_builder=_build_coding_turn,
        result_builder=_make_coding_result_summary,
        critical_window=5,
        context_token_budget=context_token_budget,
    )


def run_research_pipeline_benchmark(
    *,
    store_path: str | Path,
    turns: int = 36,
    pipeline_id: str = "bench_research_pipeline",
    context_token_budget: int | None = None,
) -> dict[str, object]:
    """Compare NCP bounded-context growth against naive replay for a tool-heavy research flow."""

    return _run_pipeline_benchmark(
        store_path=store_path,
        turns=turns,
        pipeline_id=pipeline_id,
        benchmark_name="research_pipeline",
        role_rotation=_RESEARCH_ROLE_ROTATION,
        topics=_RESEARCH_TOPICS,
        turn_builder=_build_research_turn,
        result_builder=_make_research_result_summary,
        critical_window=6,
        context_token_budget=context_token_budget,
    )


def _run_pipeline_benchmark(
    *,
    store_path: str | Path,
    turns: int,
    pipeline_id: str,
    benchmark_name: str,
    role_rotation: list[tuple[str, str, list[str], list[str]]],
    topics: list[str],
    turn_builder,
    result_builder,
    critical_window: int,
    context_token_budget: int | None,
) -> dict[str, object]:
    """Run one deterministic benchmark scenario against the real assembler/store."""

    if turns < 1:
        raise ValueError("turns must be >= 1")

    store = SQLiteStore(store_path)
    assembler = Assembler(store=store)
    transcript: list[str] = []
    recent_by_agent: dict[str, list[str]] = {}
    turn_rows: list[dict[str, object]] = []
    baselines: list[BaselineStrategy] = [
        RawReplayBaseline(),
        SlidingWindowBaseline(last_entries=8),
        RollingSummaryBaseline(every_k=4, keep_recent=4),
    ]

    for index in range(turns):
        role_name, role, owns, must_not = role_rotation[index % len(role_rotation)]
        topic = topics[index % len(topics)]
        agent_id = role_name
        turn_number = index + 1
        task = f"{benchmark_name}_{topic}"
        slot = f"{role_name}_{topic}"
        turn = turn_builder(turn_number=turn_number, topic=topic)

        conscious = agent(
            id=agent_id,
            role=role,
            owns=owns,
            must_not=must_not,
            task=task,
            slot=slot,
            intent="advance_pipeline",
            pipeline_id=pipeline_id,
            recent=recent_by_agent.get(agent_id, []),
            steps_completed=index,
            steps_total=turns,
        )
        budget = BudgetContext(
            ctx_used=min(0.95, index / max(turns, 1)),
            steps_completed=index,
            steps_total=turns,
            elapsed_seconds=float(index * 11),
            pressure="critical" if index >= turns - critical_window else "medium",
        )
        assembly = assembler.assemble(
            conscious=conscious,
            budget=budget,
            query_text=f"{topic} pipeline handoff memory",
            max_tokens=context_token_budget,
        )

        ncp_context_tokens = estimate_tokens(assembly.context)
        ncp_input_tokens = estimate_tokens(assembly.context + "\n" + turn)
        baseline_contexts = {
            baseline.name: baseline.context_for(transcript=transcript, turn=turn)
            for baseline in baselines
        }
        baseline_input_tokens = {
            name: estimate_tokens((context + "\n" + turn).strip())
            for name, context in baseline_contexts.items()
        }

        result_summary = result_builder(
            turn_number=turn_number,
            role_name=role_name,
            topic=topic,
        )
        response = NCPResponse(
            content=result_summary,
            input_tokens=ncp_input_tokens,
            output_tokens=estimate_tokens(result_summary),
            cost_usd=0.0,
            model="benchmark_local",
            pipeline_id=pipeline_id,
            turn_id=f"bench_turn_{turn_number:02d}",
            latency_ms=1,
        )
        memory_chunk = SubconsciousChunk(
            chunk_id=f"bench_chunk_{turn_number:02d}",
            layer="semantic" if turn_number % 2 else "episodic",
            content=result_summary,
            src="synthesis",
            pipeline_id=pipeline_id,
            written_by=agent_id,
            relevance=0.92,
        )
        record = assembler.post_turn(
            conscious=conscious,
            response=response,
            result_summary=result_summary,
            result_full=result_summary,
            memory_chunks=[memory_chunk],
        )
        recent_by_agent[agent_id] = [f"r:sub/{record.turn_id}", *recent_by_agent.get(agent_id, [])][:5]
        transcript.extend(
            [
                f"{agent_id} TURN {turn_number:02d}: {turn}",
                f"{agent_id} RESULT {turn_number:02d}: {result_summary}",
            ]
        )

        turn_rows.append(
            {
                "turn": turn_number,
                "agent_id": agent_id,
                "topic": topic,
                "ncp_context_tokens": ncp_context_tokens,
                "ncp_input_tokens": ncp_input_tokens,
                "naive_input_tokens": baseline_input_tokens["raw_replay"],
                "raw_replay_input_tokens": baseline_input_tokens["raw_replay"],
                "sliding_window_input_tokens": baseline_input_tokens["sliding_window"],
                "rolling_summary_input_tokens": baseline_input_tokens["rolling_summary"],
            }
        )

    peak_ncp = max(int(row["ncp_input_tokens"]) for row in turn_rows)
    final_ncp = int(turn_rows[-1]["ncp_input_tokens"])
    baseline_summary: dict[str, dict[str, object]] = {}
    for baseline in baselines:
        token_key = f"{baseline.name}_input_tokens"
        peak_tokens = max(int(row[token_key]) for row in turn_rows)
        final_tokens = int(turn_rows[-1][token_key])
        reduction_factor = round(final_tokens / final_ncp, 2) if final_ncp else 0.0
        baseline_summary[baseline.name] = {
            "peak_tokens": peak_tokens,
            "final_tokens": final_tokens,
            "reduction_factor_vs_ncp": reduction_factor,
            "config": _baseline_metadata(baseline),
        }
    peak_naive = int(baseline_summary["raw_replay"]["peak_tokens"])
    final_naive = int(baseline_summary["raw_replay"]["final_tokens"])
    reduction_factor = round(final_naive / final_ncp, 2) if final_ncp else 0.0
    overhead = assembly_overhead(embed_tokens=0, retrieval_ops=turns, whisper_writes=0)
    total_token_savings_vs_raw = sum(
        int(row["raw_replay_input_tokens"]) - int(row["ncp_input_tokens"])
        for row in turn_rows
    )
    net_total_token_equivalent_vs_raw = round(total_token_savings_vs_raw - overhead.token_equivalent, 2)

    return {
        "benchmark": benchmark_name,
        "pipeline_id": pipeline_id,
        "turns": turns,
        "agents": [role for role, _, _, _ in role_rotation],
        "token_unit": token_unit(),
        "context_token_budget": context_token_budget,
        "turn_rows": turn_rows,
        "summary": {
            "ncp": {
                "peak_tokens": peak_ncp,
                "final_tokens": final_ncp,
            },
            "baselines": baseline_summary,
            "peak_ncp_tokens": peak_ncp,
            "peak_naive_tokens": peak_naive,
            "final_ncp_tokens": final_ncp,
            "final_naive_tokens": final_naive,
            "reduction_factor": reduction_factor,
            "bounded_under_2000": peak_ncp <= 2000,
            "beats_naive": peak_ncp < peak_naive,
            "beats_sliding_window": peak_ncp < int(baseline_summary["sliding_window"]["peak_tokens"]),
            "beats_rolling_summary": peak_ncp < int(baseline_summary["rolling_summary"]["peak_tokens"]),
            "economics": {
                "reference_model": "gpt-4o-mini",
                "total_token_savings_vs_raw_replay": total_token_savings_vs_raw,
                "final_turn_savings_vs_raw_replay": final_naive - final_ncp,
                "assembly_overhead_usd": round(overhead.total_cost_usd, 8),
                "assembly_overhead_token_equivalent": round(overhead.token_equivalent, 2),
                "net_total_token_equivalent_vs_raw_replay": net_total_token_equivalent_vs_raw,
            },
            "material_reduction": reduction_factor >= 3.0,
            "pass": (
                peak_ncp <= 2000
                and peak_ncp < peak_naive
                and peak_ncp < int(baseline_summary["sliding_window"]["peak_tokens"])
                and reduction_factor >= 3.0
            ),
        },
    }


def _build_coding_turn(*, turn_number: int, topic: str) -> str:
    return (
        f"Turn {turn_number:02d}: advance {topic} with one concrete update, "
        "preserve prior constraints, and prepare the next handoff."
    )


def _build_research_turn(*, turn_number: int, topic: str) -> str:
    return (
        f"Turn {turn_number:02d}: inspect {topic}, reconcile the retrieved evidence, "
        "keep attribution clear, and hand the result to the next research role."
    )


def _make_coding_result_summary(*, turn_number: int, role_name: str, topic: str) -> str:
    return (
        f"{role_name} turn {turn_number:02d} resolved {topic} by carrying forward the prior contract, "
        f"recording one concrete decision, and preserving the handoff boundary for the next agent on the pipeline."
    )


def _make_research_result_summary(*, turn_number: int, role_name: str, topic: str) -> str:
    return (
        f"{role_name} turn {turn_number:02d} resolved {topic} by comparing retrieved evidence, "
        "documenting one grounded finding, preserving source attribution, and handing off the verified thread."
    )
