#!/usr/bin/env python3
"""Task-success benchmark — context adequacy at a matched token budget.

For each scripted task (see ``tasks.py``), three context-construction
conditions are evaluated at a matched token budget ``B``:

- ``ncp``: the scripted turns are written into a fresh SQLite store as chunks
  (varying ``base_trust``/``src`` by writer, as in real usage), and the final
  question's context is produced by ``Assembler.assemble(..., max_tokens=B)``.
- ``sliding_window``: the raw transcript's most-recent entries that fit
  within ``B`` estimated tokens (a fixed recency window, no retrieval).
- ``raw_replay``: the full, unbounded transcript — a reference condition
  exempt from the budget, clearly labeled "unbounded" in the artifact.

A model response is then obtained per condition:

- ``--provider mock`` (default, used in CI): a deterministic stand-in that
  scans the supplied context for the task's planted approved-path slug and
  answers "I will use <slug> and will not use any rejected paths" if found,
  else "no approved path found in context". Mock mode therefore measures
  CONTEXT ADEQUACY at the matched budget — whether the needed fact survived
  into the context — NOT model reasoning quality. This is the honest framing
  for a keyless CI benchmark.
- ``--provider anthropic|openai|...``: routes through
  ``ncp.dogfood.load_dogfood_adapter``, including a plain-text-only
  instruction preamble, guarded by ``get_live_provider_readiness``.
"""

from __future__ import annotations

import argparse
import json
import tempfile
from pathlib import Path
from typing import Any

from ncp.api import agent as make_conscious
from ncp.assembler import Assembler
from ncp.dogfood import get_live_provider_readiness, load_dogfood_adapter
from ncp.stores.sqlite import SQLiteStore
from ncp.tokens import estimate_tokens, token_unit
from ncp.types import BudgetContext, SubconsciousChunk

from benchmarks.task_success.tasks import Task, get_tasks, score_response


_LIVE_PREAMBLE = (
    "IMPORTANT: Respond with plain text only. Do NOT read any files or use any tools.\n\n"
)


# ---------------------------------------------------------------------------
# Context construction per condition
# ---------------------------------------------------------------------------

def _ncp_context(task: Task, budget: int, pipeline_id: str, store_path: Path) -> tuple[str, int]:
    """Write scripted turns as chunks, then assemble the final-question context."""

    store = SQLiteStore(store_path)
    for index, turn in enumerate(task.turns, start=1):
        chunk = SubconsciousChunk(
            layer=turn.layer,  # type: ignore[arg-type]
            src=turn.src,  # type: ignore[arg-type]
            base_trust=turn.base_trust,
            relevance=turn.relevance,
            content=turn.content,
            conditions=list(turn.conditions),
            pipeline_id=pipeline_id,
            written_by=f"agent_turn_{index:02d}",
        )
        store.write(chunk)

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
    budget_ctx = BudgetContext(ctx_used=0.7, pressure="medium")
    assembly = assembler.assemble(
        conscious=conscious,
        budget=budget_ctx,
        query_text=task.query_text,
        k=8,
        max_tokens=budget,
    )
    context = assembly.context
    return context, estimate_tokens(context)


def _sliding_window_context(task: Task, budget: int) -> tuple[str, int]:
    """Most-recent transcript entries that fit within ``budget`` tokens."""

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


def _raw_replay_context(task: Task) -> tuple[str, int]:
    """Full unbounded transcript — reference condition, exempt from the budget."""

    context = "\n".join(turn.content for turn in task.turns)
    return context, estimate_tokens(context)


# ---------------------------------------------------------------------------
# Mock provider — measures context adequacy, not model reasoning
# ---------------------------------------------------------------------------

def _mock_response(context: str, task: Task) -> str:
    """Deterministic stand-in: answer using only the supplied context.

    Scans for the planted approved-path slug. This makes mock mode measure
    whether the needed fact was surfaced within the budget, NOT whether a
    model can reason about it.
    """

    if task.approved_path in context.lower():
        return f"I will use {task.approved_path} and will not use any rejected paths"
    return "no approved path found in context"


def _live_response(adapter: Any, context: str, task: Task) -> str:
    full_turn = f"{_LIVE_PREAMBLE}[Context]\n{context}\n\n[Task]\n{task.question}"
    return adapter.call(ncp_context="", user_turn=full_turn)


# ---------------------------------------------------------------------------
# Per-task, per-condition evaluation
# ---------------------------------------------------------------------------

_CONDITIONS = ("ncp", "sliding_window", "raw_replay")


def _run_task(
    task: Task,
    *,
    budget: int,
    provider: str,
    adapter: Any | None,
    store_dir: Path,
    pipeline_id_prefix: str,
) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []

    for condition in _CONDITIONS:
        if condition == "ncp":
            store_path = store_dir / f"{task.task_id}_ncp.db"
            context, context_tokens = _ncp_context(
                task,
                budget=budget,
                pipeline_id=f"{pipeline_id_prefix}_{task.task_id}_ncp",
                store_path=store_path,
            )
            unbounded = False
        elif condition == "sliding_window":
            context, context_tokens = _sliding_window_context(task, budget=budget)
            unbounded = False
        else:  # raw_replay
            context, context_tokens = _raw_replay_context(task)
            unbounded = True

        if provider == "mock":
            response = _mock_response(context, task)
        else:
            assert adapter is not None
            response = _live_response(adapter, context, task)

        success, failure_type = score_response(response, task)
        excerpt = response[:200]

        rows.append(
            {
                "task_id": task.task_id,
                "condition": condition,
                "context_tokens": context_tokens,
                "unbounded": unbounded,
                "success": success,
                "failure_type": failure_type,
                "response_excerpt": excerpt,
            }
        )

    return rows


# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------

def _summarize_condition(rows: list[dict[str, object]], condition: str) -> dict[str, object]:
    condition_rows = [row for row in rows if row["condition"] == condition]
    if not condition_rows:
        return {"success_rate": 0.0, "n": 0, "median_context_tokens": 0.0}
    successes = sum(1 for row in condition_rows if row["success"])
    tokens = sorted(int(row["context_tokens"]) for row in condition_rows)
    mid = len(tokens) // 2
    if len(tokens) % 2:
        median_tokens = float(tokens[mid])
    else:
        median_tokens = (tokens[mid - 1] + tokens[mid]) / 2.0
    return {
        "success_rate": successes / len(condition_rows),
        "n": len(condition_rows),
        "median_context_tokens": median_tokens,
    }


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def run_task_success(
    *,
    budget: int = 400,
    provider: str = "mock",
    n_tasks: int | None = None,
    adapter_timeout_seconds: float | None = None,
    pipeline_id: str = "bench_task_success",
    store_dir: str | Path | None = None,
) -> dict[str, object]:
    """Run the task-success benchmark and return a structured artifact."""

    if budget < 1:
        raise ValueError("budget must be >= 1")

    tasks = get_tasks(n_tasks)

    adapter: Any | None = None
    readiness: dict[str, object] | None = None
    if provider != "mock":
        readiness = get_live_provider_readiness(provider)
        if not readiness.get("ready", False):
            raise RuntimeError(
                f"Provider '{provider}' is not ready for live task-success runs: "
                f"{readiness}. Set the required credentials/env vars or use "
                "--provider mock."
            )
        adapter = load_dogfood_adapter(provider, timeout_seconds=adapter_timeout_seconds)

    rows: list[dict[str, object]] = []

    def _collect(target_dir: Path) -> None:
        for task in tasks:
            rows.extend(
                _run_task(
                    task,
                    budget=budget,
                    provider=provider,
                    adapter=adapter,
                    store_dir=target_dir,
                    pipeline_id_prefix=pipeline_id,
                )
            )

    if store_dir is not None:
        target = Path(store_dir)
        target.mkdir(parents=True, exist_ok=True)
        _collect(target)
    else:
        with tempfile.TemporaryDirectory(prefix="ncp-task-success-") as tmpdir:
            _collect(Path(tmpdir))

    summary_by_condition = {
        condition: _summarize_condition(rows, condition) for condition in _CONDITIONS
    }

    ncp_rate = float(summary_by_condition["ncp"]["success_rate"])
    sliding_rate = float(summary_by_condition["sliding_window"]["success_rate"])

    pass_gate: bool | None
    if provider == "mock":
        pass_gate = ncp_rate >= sliding_rate and ncp_rate >= 0.75
    else:
        pass_gate = None  # gate is mock-mode only

    return {
        "benchmark": "task_success",
        "claim": (
            "context adequacy at a matched token budget: whether the planted "
            "approved-path fact survives into the assembled context, "
            "NOT a measurement of model reasoning quality"
            if provider == "mock"
            else "live task success at a matched token budget"
        ),
        "provider": provider,
        "budget": budget,
        "n_tasks": len(tasks),
        "token_unit": token_unit(),
        "rows": rows,
        "summary": {
            "by_condition": summary_by_condition,
            "pass": pass_gate,
            "pass_gate_description": (
                "ncp success rate >= sliding_window success rate AND "
                "ncp success rate >= 0.75 (mock mode only)"
            ),
        },
    }


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--budget",
        type=int,
        default=400,
        help="Matched context token budget for ncp/sliding_window (default: 400)",
    )
    parser.add_argument(
        "--provider",
        default="mock",
        help="mock (default, keyless) or a dogfood adapter name: anthropic, openai, …",
    )
    parser.add_argument(
        "--tasks",
        type=int,
        default=None,
        dest="n_tasks",
        help="Limit to the first N tasks (default: all)",
    )
    parser.add_argument(
        "--adapter-timeout-seconds",
        type=float,
        default=None,
        dest="adapter_timeout_seconds",
        help="Adapter call timeout in seconds for live providers",
    )
    parser.add_argument(
        "--pipeline-id",
        default="bench_task_success",
        dest="pipeline_id",
        help="Pipeline ID prefix for store records (default: bench_task_success)",
    )
    parser.add_argument(
        "--store-path",
        type=Path,
        default=None,
        dest="store_path",
        help="Directory to hold per-task SQLite stores (default: tempdir)",
    )
    args = parser.parse_args()

    artifact = run_task_success(
        budget=args.budget,
        provider=args.provider,
        n_tasks=args.n_tasks,
        adapter_timeout_seconds=args.adapter_timeout_seconds,
        pipeline_id=args.pipeline_id,
        store_dir=args.store_path,
    )
    print(json.dumps(artifact, indent=2))


if __name__ == "__main__":
    main()
