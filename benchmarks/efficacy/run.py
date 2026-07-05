#!/usr/bin/env python3
"""Provider efficacy benchmark at matched context budget.

This harness compares three context-construction conditions over the existing
task-success task set:

- ``ncp``: NCP assembly from a SQLite store.
- ``sliding_window``: most recent raw turns that fit the same budget.
- ``rolling_summary``: a deterministic extractive rolling summary plus recent
  turns, fit to the same budget.

``--provider mock`` is deterministic and keyless; it measures context adequacy,
not model reasoning. ``--provider anthropic`` is live and skips clearly when
``ANTHROPIC_API_KEY`` is unset.
"""

from __future__ import annotations

import argparse
import json
import os
import random
import statistics
import tempfile
from pathlib import Path
from typing import Any

from benchmarks.task_success.tasks import Task, get_tasks, score_response
from ncp.api import agent as make_conscious
from ncp.assembler import Assembler
from ncp.costs import calculate_cost
from ncp.dogfood import load_dogfood_adapter
from ncp.stores.sqlite import SQLiteStore
from ncp.tokens import estimate_tokens, token_unit
from ncp.types import BudgetContext, SubconsciousChunk


_LIVE_PREAMBLE = (
    "IMPORTANT: Respond with plain text only. Do NOT read any files or use any tools.\n\n"
)
_CONDITIONS = ("ncp", "sliding_window", "rolling_summary")
_NCP_RETRIEVAL_K = 8


def _score_response(response: str) -> tuple[bool, str | None]:
    """Backward-compatible scoring helper for the original efficacy scenario."""

    approved = "zenbrix_secure_relay_v4_gamma"
    probe = Task(
        task_id="legacy_efficacy_probe",
        domain="zenbrix",
        approved_path=approved,
        dead_ends=[
            "zenbrix_legacy_bridge",
            "zenbrix_v2_mesh",
            "zenbrix_v3_edge_beta",
        ],
        turns=[],
        question="",
        query_text="",
    )
    return score_response(response, probe)


_APPROVED_PATH = "zenbrix_secure_relay_v4_gamma"
EFFICACY_TASK_TURN = (
    "IMPORTANT: Respond with plain text only. Do NOT read any files or use any tools.\n\n"
    "You are an executor agent completing an integration task. "
    "The planner has specified exactly which integration path to use. "
    "Based ONLY on the context above, state the exact integration path you will use "
    "and confirm you will not use any rejected paths. "
    "Your answer MUST name the exact approved path from the context."
)
_SCENARIO_FACTS: list[dict[str, Any]] = [
    {
        "content": (
            "constraint: use zenbrix_secure_relay_v4_gamma exclusively -- "
            "all other integration paths are forbidden by the current security policy"
        ),
        "conditions": ["integration_constraint"],
    },
    {
        "content": (
            "dead_end: zenbrix_legacy_bridge rejected -- "
            "decommissioned and removed from the allowed integration list"
        ),
        "conditions": ["dead_end"],
    },
    {
        "content": (
            "dead_end: zenbrix_v2_mesh rejected -- "
            "incompatible with the current authentication substrate"
        ),
        "conditions": ["dead_end"],
    },
    {
        "content": (
            "dead_end: zenbrix_v3_edge_beta rejected -- "
            "flagged as unstable after the incident on 2026-05-12"
        ),
        "conditions": ["dead_end"],
    },
    {
        "content": (
            "decision: zenbrix_secure_relay_v4_gamma is the only approved path -- "
            "verified by the security team on 2026-05-28"
        ),
        "conditions": [],
    },
]


def _seed_store(store: SQLiteStore, pipeline_id: str) -> list[str]:
    """Seed the legacy single-scenario facts used by cross-host benchmarks."""

    contents: list[str] = []
    for fact in _SCENARIO_FACTS:
        chunk = SubconsciousChunk(
            layer="semantic",
            src="user_verified",
            base_trust=0.98,
            relevance=0.95,
            content=str(fact["content"]),
            conditions=list(fact["conditions"]),
            pipeline_id=pipeline_id,
            written_by="bench_seed",
        )
        store.write(chunk)
        contents.append(str(fact["content"]))
    return contents


def _add_pipeline_noise(
    store: SQLiteStore,
    pipeline_id: str,
    n_turns: int = 20,
) -> list[str]:
    """Write filler chunks after legacy facts so recency windows lose constraints."""

    noise: list[str] = []
    for index in range(1, n_turns + 1):
        content = (
            f"turn {index:02d}: routine implementation progress -- "
            "the executor is validating endpoint configuration, verifying TLS certificate "
            "bindings, checking exponential-backoff retry logic, and confirming the "
            "service layer dispatch chain; the planner has not issued new constraints "
            "this turn; all checks nominal; proceeding to the next pipeline stage"
        )
        store.write(
            SubconsciousChunk(
                layer="episodic",
                src="agent_inferred",
                base_trust=0.4,
                relevance=0.05,
                content=content,
                pipeline_id=pipeline_id,
                written_by="bench_noise",
            )
        )
        noise.append(content)
    return noise


def _build_window_context(transcript: list[str], budget: int) -> str:
    selected: list[str] = []
    used = 0
    for entry in reversed(transcript):
        tokens = estimate_tokens(entry)
        if used + tokens > budget:
            break
        selected.append(entry)
        used += tokens
    return "\n".join(reversed(selected)) if selected else ""


def _ncp_context(task: Task, budget: int, pipeline_id: str, store_path: Path) -> tuple[str, int]:
    store = SQLiteStore(store_path)
    for index, turn in enumerate(task.turns, start=1):
        store.write(
            SubconsciousChunk(
                layer=turn.layer,  # type: ignore[arg-type]
                src=turn.src,  # type: ignore[arg-type]
                base_trust=turn.base_trust,
                relevance=turn.relevance,
                content=turn.content,
                conditions=list(turn.conditions),
                pipeline_id=pipeline_id,
                written_by=f"agent_turn_{index:02d}",
            )
        )

    assembler = Assembler(store=store)
    conscious = make_conscious(
        id="bench_executor",
        role="pravaha",
        owns=[task.domain],
        must_not=["rejected_paths"],
        task=f"{task.task_id}_question",
        slot="build",
        intent="select_approved_path",
        pipeline_id=pipeline_id,
    )
    assembly = assembler.assemble(
        conscious=conscious,
        budget=BudgetContext(ctx_used=0.7, pressure="medium"),
        query_text=task.query_text,
        k=_NCP_RETRIEVAL_K,
        max_tokens=budget,
    )
    return assembly.context, estimate_tokens(assembly.context)


def _sliding_window_context(task: Task, budget: int) -> tuple[str, int]:
    selected: list[str] = []
    used = 0
    for turn in reversed(task.turns):
        tokens = estimate_tokens(turn.content)
        if used + tokens > budget:
            break
        selected.append(turn.content)
        used += tokens
    context = "\n".join(reversed(selected)) if selected else ""
    return context, estimate_tokens(context)


def _compress_turn(turn_text: str, max_words: int = 22) -> str:
    words = turn_text.split()
    if len(words) <= max_words:
        return turn_text
    return " ".join(words[:max_words])


def _rolling_summary_context(task: Task, budget: int) -> tuple[str, int]:
    recent_turns = task.turns[-4:]
    older_turns = task.turns[:-4]
    summary_lines = [
        f"summary turn {index + 1}: {_compress_turn(turn.content)}"
        for index, turn in enumerate(older_turns)
    ]
    recent_lines = [turn.content for turn in recent_turns]
    candidates = [*summary_lines, *recent_lines]

    selected: list[str] = []
    used = 0
    for entry in candidates:
        tokens = estimate_tokens(entry)
        if used + tokens > budget:
            continue
        selected.append(entry)
        used += tokens
    context = "\n".join(selected)
    return context, estimate_tokens(context)


def _mock_response(context: str, task: Task) -> str:
    if task.approved_path in context.lower():
        return f"I will use {task.approved_path} and will not use any rejected paths"
    return "no approved path found in context"


def _live_response(adapter: Any, context: str, task: Task) -> str:
    full_turn = f"{_LIVE_PREAMBLE}[Context]\n{context}\n\n[Task]\n{task.question}"
    return adapter.call(ncp_context="", user_turn=full_turn)


def _price_response(
    *,
    provider: str,
    prompt_tokens: int,
    output_tokens: int,
) -> tuple[str, float, str]:
    if provider == "mock":
        return "mock", 0.0, "not_applicable"
    model = "claude-sonnet-4-20250514"
    try:
        cost = calculate_cost(
            model=model,
            input_tokens=prompt_tokens,
            output_tokens=output_tokens,
        ).total_cost_usd
    except KeyError:
        cost = 0.0
    return model, cost, "estimated_from_token_estimator"


def _condition_context(task: Task, condition: str, budget: int, pipeline_id: str, store_dir: Path) -> tuple[str, int]:
    if condition == "ncp":
        return _ncp_context(task, budget, pipeline_id, store_dir / f"{pipeline_id}.db")
    if condition == "sliding_window":
        return _sliding_window_context(task, budget)
    if condition == "rolling_summary":
        return _rolling_summary_context(task, budget)
    raise ValueError(f"unknown condition {condition!r}")


def _run_one(
    *,
    task: Task,
    condition: str,
    seed: int,
    budget: int,
    provider: str,
    adapter: Any | None,
    store_dir: Path,
    pipeline_id: str,
) -> dict[str, object]:
    context, context_tokens = _condition_context(
        task,
        condition,
        budget,
        f"{pipeline_id}_seed_{seed}_{task.task_id}_{condition}",
        store_dir,
    )
    prompt = f"[Context]\n{context}\n\n[Task]\n{task.question}"
    prompt_tokens = estimate_tokens(prompt)
    if provider == "mock":
        response = _mock_response(context, task)
    else:
        assert adapter is not None
        response = _live_response(adapter, context, task)
    success, failure_type = score_response(response, task)
    model, cost_usd, cost_source = _price_response(
        provider=provider,
        prompt_tokens=prompt_tokens,
        output_tokens=estimate_tokens(response),
    )
    return {
        "seed": seed,
        "task_id": task.task_id,
        "condition": condition,
        "success": success,
        "failure_type": failure_type,
        "context_tokens": context_tokens,
        "prompt_tokens": prompt_tokens,
        "cost_usd": cost_usd,
        "cost_source": cost_source,
        "model": model,
        "response_excerpt": response[:200],
    }


def _summarize(rows: list[dict[str, object]]) -> dict[str, dict[str, object]]:
    by_condition: dict[str, dict[str, object]] = {}
    for condition in _CONDITIONS:
        subset = [row for row in rows if row["condition"] == condition]
        if not subset:
            by_condition[condition] = {
                "success_rate": 0.0,
                "mean_tokens": 0.0,
                "mean_cost_usd_per_task": 0.0,
                "n": 0,
            }
            continue
        by_condition[condition] = {
            "success_rate": sum(1 for row in subset if row["success"]) / len(subset),
            "mean_tokens": statistics.mean(int(row["prompt_tokens"]) for row in subset),
            "mean_cost_usd_per_task": statistics.mean(float(row["cost_usd"]) for row in subset),
            "n": len(subset),
        }
    return by_condition


def _anthropic_skip(provider: str) -> str | None:
    if provider == "anthropic" and not os.environ.get("ANTHROPIC_API_KEY"):
        return "ANTHROPIC_API_KEY is unset; live Anthropic efficacy run skipped"
    return None


def run_efficacy(
    *,
    provider: str = "mock",
    budget: int = 400,
    seeds: int = 5,
    n_tasks: int | None = None,
    adapter_timeout_seconds: float | None = None,
    pipeline_id: str = "bench_efficacy",
    artifact_path: str | Path | None = None,
    store_dir: str | Path | None = None,
) -> dict[str, object]:
    if budget < 1:
        raise ValueError("budget must be >= 1")
    if seeds < 1:
        raise ValueError("seeds must be >= 1")
    provider = provider.lower()
    if provider not in {"mock", "anthropic"}:
        raise ValueError("provider must be 'mock' or 'anthropic'")

    skip_reason = _anthropic_skip(provider)
    if skip_reason is not None:
        artifact: dict[str, object] = {
            "benchmark": "provider_real_efficacy",
            "provider": provider,
            "skipped": True,
            "skip_reason": skip_reason,
            "rows": [],
            "summary": {"by_condition": _summarize([])},
        }
        _write_artifact(artifact, artifact_path)
        return artifact

    adapter = None
    if provider == "anthropic":
        adapter = load_dogfood_adapter(provider, timeout_seconds=adapter_timeout_seconds)

    tasks = get_tasks(n_tasks)
    rows: list[dict[str, object]] = []

    def collect(target_dir: Path) -> None:
        for seed in range(1, seeds + 1):
            ordered = list(tasks)
            random.Random(seed).shuffle(ordered)
            for task in ordered:
                for condition in _CONDITIONS:
                    rows.append(
                        _run_one(
                            task=task,
                            condition=condition,
                            seed=seed,
                            budget=budget,
                            provider=provider,
                            adapter=adapter,
                            store_dir=target_dir,
                            pipeline_id=pipeline_id,
                        )
                    )

    if store_dir is None:
        with tempfile.TemporaryDirectory() as tmp:
            collect(Path(tmp))
    else:
        target = Path(store_dir)
        target.mkdir(parents=True, exist_ok=True)
        collect(target)

    artifact = {
        "benchmark": "provider_real_efficacy",
        "claim": (
            "Mock mode measures context adequacy at matched budget; live provider mode "
            "measures model task success and cost at the same budget."
        ),
        "provider": provider,
        "budget": budget,
        "seeds": seeds,
        "n_tasks": len(tasks),
        "conditions": list(_CONDITIONS),
        "config": {
            "ncp_retrieval_k": _NCP_RETRIEVAL_K,
            "rolling_summary_recent_turns": 4,
            "rolling_summary_max_words_per_older_turn": 22,
        },
        "token_unit": token_unit(),
        "rows": rows,
        "summary": {"by_condition": _summarize(rows)},
    }
    _write_artifact(artifact, artifact_path)
    return artifact


def _write_artifact(artifact: dict[str, object], artifact_path: str | Path | None) -> None:
    if artifact_path is None:
        return
    path = Path(artifact_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(artifact, indent=2, sort_keys=True) + "\n")
    artifact["artifact_path"] = str(path)


def _print_table(artifact: dict[str, object]) -> None:
    if artifact.get("skipped"):
        print(f"SKIPPED: {artifact['skip_reason']}")
        return
    summary = artifact["summary"]["by_condition"]  # type: ignore[index]
    print("condition        success_rate  mean_tokens  cost_per_task_usd")
    for condition in _CONDITIONS:
        row = summary[condition]
        print(
            f"{condition:<16} "
            f"{float(row['success_rate']):>12.2f} "
            f"{float(row['mean_tokens']):>12.1f} "
            f"{float(row['mean_cost_usd_per_task']):>18.6f}"
        )


def main() -> None:
    parser = argparse.ArgumentParser(description="Provider-real efficacy benchmark.")
    parser.add_argument("--provider", choices=["mock", "anthropic"], default="mock")
    parser.add_argument("--budget", type=int, default=400)
    parser.add_argument("--seeds", type=int, default=5)
    parser.add_argument("--n-tasks", type=int, default=None)
    parser.add_argument("--adapter-timeout-seconds", type=float, default=None)
    parser.add_argument("--pipeline-id", default="bench_efficacy")
    parser.add_argument(
        "--artifact",
        default=str(Path(__file__).with_name("efficacy_results.json")),
        help="Path for the machine-readable JSON artifact.",
    )
    args = parser.parse_args()

    artifact = run_efficacy(
        provider=args.provider,
        budget=args.budget,
        seeds=args.seeds,
        n_tasks=args.n_tasks,
        adapter_timeout_seconds=args.adapter_timeout_seconds,
        pipeline_id=args.pipeline_id,
        artifact_path=args.artifact,
    )
    _print_table(artifact)
    if artifact.get("artifact_path"):
        print(f"JSON artifact: {artifact['artifact_path']}")


if __name__ == "__main__":
    main()
