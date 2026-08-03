#!/usr/bin/env python3
"""HotpotQA-style multi-hop benchmark — context adequacy at a matched budget.

See ``tasks.py`` for the full scope note. In short: this is a synthetic,
same-shape stand-in for HotpotQA's distractor setting (bridge and comparison
multi-hop questions over ~12 candidate paragraphs, 2 of which are needed to
answer), used because this environment's egress policy blocks
huggingface.co, arxiv.org, and cmu.edu, so neither the official HotpotQA
dataset nor PlugMem's own eval harness (github.com/TIMAN-group/PlugMem) could
be fetched. It is NOT a reproduction of PlugMem's reported numbers.

For each task, three context-construction conditions are evaluated at a
matched token budget ``B`` (identical mechanism to
``benchmarks/task_success``):

- ``ncp``: paragraphs are written into a fresh SQLite store as chunks, and
  the final context is produced by ``Assembler.assemble(..., max_tokens=B)``
  -- i.e. NCP's real BM25 + recency + trust retrieval decides what survives.
- ``sliding_window``: the most-recent paragraphs (by list order) that fit
  within ``B`` estimated tokens -- recency only, no retrieval.
- ``raw_replay``: all paragraphs, unbounded -- reference condition, exempt
  from the budget.

Success is context adequacy (``tasks.score_context``): do both gold
paragraphs' facts survive into the assembled context? This does not measure
whether a model can combine the two facts into a correct answer -- only
whether the facts needed to do so were not dropped by the budget.
"""

from __future__ import annotations

import argparse
import json
import tempfile
from pathlib import Path

from ncp.eval import EvalTurn
from ncp.eval import ncp_condition as _eval_ncp_condition
from ncp.eval import raw_replay_condition as _eval_raw_replay_condition
from ncp.eval import sliding_window_condition as _eval_sliding_window_condition
from ncp.tokens import token_unit

from benchmarks.hotpotqa_style.tasks import Task, get_tasks, score_context

_CONDITIONS = ("ncp", "sliding_window", "raw_replay")


def _to_eval_turns(task: Task) -> list[EvalTurn]:
    return [
        EvalTurn(
            content=paragraph.content,
            src="tool_result",
            base_trust=0.6,
            relevance=0.2,
            layer="episodic",
        )
        for paragraph in task.paragraphs
    ]


def _run_task(
    task: Task,
    *,
    budget: int,
    k: int,
    store_dir: Path,
    pipeline_id_prefix: str,
) -> list[dict[str, object]]:
    turns = _to_eval_turns(task)
    rows: list[dict[str, object]] = []

    for condition in _CONDITIONS:
        if condition == "ncp":
            result = _eval_ncp_condition(
                turns,
                budget=budget,
                query_text=task.query_text,
                pipeline_id=f"{pipeline_id_prefix}_{task.task_id}_ncp",
                store_path=store_dir / f"{task.task_id}_ncp.db",
                agent_id="bench_executor",
                owns=["research"],
                task=f"{task.task_id}_question",
                intent="answer_multi_hop_question",
                k=k,
            )
        elif condition == "sliding_window":
            result = _eval_sliding_window_condition(turns, budget=budget)
        else:  # raw_replay
            result = _eval_raw_replay_condition(turns)

        success, missing = score_context(result.context, task)
        rows.append(
            {
                "task_id": task.task_id,
                "kind": task.kind,
                "condition": condition,
                "context_tokens": result.context_tokens,
                "unbounded": result.unbounded,
                "success": success,
                "missing": missing,
            }
        )

    return rows


def _summarize(rows: list[dict[str, object]], *, key: str, value: str | None = None) -> dict[str, object]:
    subset = [row for row in rows if value is None or row[key] == value]
    if not subset:
        return {"success_rate": 0.0, "n": 0, "median_context_tokens": 0.0, "info_density_per_1k_tokens": 0.0}
    successes = sum(1 for row in subset if row["success"])
    tokens = sorted(int(row["context_tokens"]) for row in subset)
    mid = len(tokens) // 2
    median_tokens = float(tokens[mid]) if len(tokens) % 2 else (tokens[mid - 1] + tokens[mid]) / 2.0
    success_rate = successes / len(subset)
    info_density = (success_rate / median_tokens) * 1000 if median_tokens > 0 else 0.0
    return {
        "success_rate": round(success_rate, 4),
        "n": len(subset),
        "median_context_tokens": median_tokens,
        "info_density_per_1k_tokens": round(info_density, 4),
    }


def run_hotpotqa_style(
    *,
    budget: int = 300,
    k: int = 6,
    n_tasks: int | None = None,
    pipeline_id: str = "bench_hotpotqa_style",
    store_dir: str | Path | None = None,
) -> dict[str, object]:
    """Run the HotpotQA-style multi-hop benchmark and return a structured artifact."""

    if budget < 1:
        raise ValueError("budget must be >= 1")
    if k < 1:
        raise ValueError("k must be >= 1")

    tasks = get_tasks(n_tasks)
    rows: list[dict[str, object]] = []

    def _collect(target_dir: Path) -> None:
        for task in tasks:
            rows.extend(
                _run_task(
                    task,
                    budget=budget,
                    k=k,
                    store_dir=target_dir,
                    pipeline_id_prefix=pipeline_id,
                )
            )

    if store_dir is not None:
        target = Path(store_dir)
        target.mkdir(parents=True, exist_ok=True)
        _collect(target)
    else:
        with tempfile.TemporaryDirectory(prefix="ncp-hotpotqa-style-") as tmpdir:
            _collect(Path(tmpdir))

    by_condition = {condition: _summarize(rows, key="condition", value=condition) for condition in _CONDITIONS}
    by_kind_condition = {
        f"{kind}/{condition}": _summarize(
            [row for row in rows if row["kind"] == kind], key="condition", value=condition
        )
        for kind in ("bridge", "comparison")
        for condition in _CONDITIONS
    }

    ncp_rate = float(by_condition["ncp"]["success_rate"])
    sliding_rate = float(by_condition["sliding_window"]["success_rate"])
    pass_gate = ncp_rate >= sliding_rate

    return {
        "benchmark": "hotpotqa_style",
        "claim": (
            "context adequacy at a matched token budget on a synthetic, "
            "HotpotQA-shaped multi-hop QA task (bridge + comparison "
            "questions over distractor paragraphs) -- whether both facts "
            "needed to answer survive into the assembled context, NOT a "
            "reproduction of the official HotpotQA dataset or PlugMem's "
            "own reported numbers"
        ),
        "budget": budget,
        "k": k,
        "n_tasks": len(tasks),
        "token_unit": token_unit(),
        "rows": rows,
        "summary": {
            "by_condition": by_condition,
            "by_kind_and_condition": by_kind_condition,
            "pass": pass_gate,
            "pass_gate_description": "ncp success rate >= sliding_window success rate at the same token budget",
        },
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--budget", type=int, default=300, help="Matched context token budget (default: 300)")
    parser.add_argument("--k", type=int, default=6, help="Max chunks NCP retrieval considers (default: 6)")
    parser.add_argument("--tasks", type=int, default=None, dest="n_tasks", help="Limit to the first N tasks")
    parser.add_argument("--pipeline-id", default="bench_hotpotqa_style", dest="pipeline_id")
    parser.add_argument("--store-path", type=Path, default=None, dest="store_path")
    args = parser.parse_args()

    artifact = run_hotpotqa_style(
        budget=args.budget,
        k=args.k,
        n_tasks=args.n_tasks,
        pipeline_id=args.pipeline_id,
        store_dir=args.store_path,
    )
    print(json.dumps(artifact, indent=2))


if __name__ == "__main__":
    main()
