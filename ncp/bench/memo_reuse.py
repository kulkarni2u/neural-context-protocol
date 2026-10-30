"""Memo reuse benchmark: cost per verified success and false-hit rate.

A deterministic, offline workload that repeats the same tasks over rounds
while some tasks' evidence changes between rounds -- the case where reusing
a cached answer is wrong. A simulated priced provider answers from the
evidence it is shown, and an independent verifier checks every answer
against the current ground truth (never against NCP's own view).

Arms, all on the real ``ncp.run`` path and a real SQLite store:

- ``memo_off``: every turn calls the provider.
- ``memo_dependency_safe``: memoization on, with the canonical dependency
  fingerprint and the verification gate; the host verifies (or fails) each
  result exactly as a real host would via ``update_memo_outcome``.
- ``naive_key_counterfactual``: what a key of just agent|task|slot + turn
  with no verification gate (the pre-fix Python path) *would* have served
  on the dependency-safe run. Computed, not executed -- reported to show the
  hazard the workload contains.

Provider cost uses ``ncp.costs.calculate_cost`` on the simulated usage.
Memo lookups still assemble context (the fingerprint needs it), so no
retrieval is avoided; only provider calls are.
"""

from __future__ import annotations

import re
from pathlib import Path

import ncp
from ncp.adapters.base import BaseAdapter, TokenUsage
from ncp.config import load_config
from ncp.costs import calculate_cost
from ncp.stores.sqlite import SQLiteStore
from ncp.tokens import estimate_tokens, token_unit
from ncp.types import SubconsciousChunk

_MODEL = "gpt-4o"
_OUTPUT_TOKENS = 120
_VERSION_RE = re.compile(r"topic(\d+) fact version v(\d+)")


class _EvidenceAnsweringAdapter(BaseAdapter):
    """Answers with the fact version it sees in context; reports fixed usage."""

    def __init__(self) -> None:
        self._model = _MODEL
        self.calls = 0

    def call(self, ncp_context: str, user_turn: str) -> str:
        self.calls += 1
        topic = re.search(r"topic(\d+)", user_turn)
        seen = {m.group(1): m.group(2) for m in _VERSION_RE.finditer(ncp_context)}
        version = seen.get(topic.group(1)) if topic else None
        self.last_usage = TokenUsage(
            input_tokens=estimate_tokens(ncp_context + "\n" + user_turn),
            output_tokens=_OUTPUT_TOKENS,
        )
        return f"topic{topic.group(1) if topic else '?'}:v{version or '?'}"


def _arm_metrics(*, requests: int, provider_calls: int, memo_hits: int, false_hits: int,
                 verified: int, input_tokens: int, output_tokens: int, cost: float) -> dict[str, object]:
    return {
        "requests": requests,
        "provider_calls": provider_calls,
        "avoided_provider_calls": requests - provider_calls,
        "avoided_retrieval": 0,
        "memo_hits": memo_hits,
        "memo_hit_rate": round(memo_hits / requests, 4) if requests else 0.0,
        "false_hits": false_hits,
        "false_hit_rate": round(false_hits / memo_hits, 4) if memo_hits else 0.0,
        "verified_successes": verified,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "cost_usd": round(cost, 6),
        "cost_per_verified_success_usd": round(cost / verified, 8) if verified else None,
    }


def _run_arm(
    root: Path,
    *,
    memo: bool,
    tasks: int,
    rounds: int,
    mutate_every: int,
) -> tuple[dict[str, object], dict[str, int]]:
    project = root / ("memo_on" if memo else "memo_off")
    (project / ".git").mkdir(parents=True)
    config = load_config(cwd=project)
    config.values["memoization"] = {"enabled": memo, "allow_unverified": False, "min_outcome": 0.5}
    store = SQLiteStore(project / ".ncp" / "store.db", config=config)
    adapter = _EvidenceAnsweringAdapter()

    truth = {i: 1 for i in range(tasks)}
    current_chunk = {}
    for i in range(tasks):
        chunk_id = f"fact_{i}_v1"
        store.write(SubconsciousChunk(
            chunk_id=chunk_id, layer="semantic", content=f"topic{i} fact version v1 for the benchmark",
            src="tool_result", pipeline_id=f"bench_memo_{i}", written_by="oracle",
        ))
        current_chunk[i] = chunk_id

    naive_cache: dict[tuple[str, str, str, str], str] = {}
    naive = {"would_hit": 0, "would_false_hit": 0}
    requests = memo_hits = false_hits = verified = input_tokens = output_tokens = 0
    cost = 0.0
    for round_index in range(rounds):
        for i in range(tasks):
            agent = ncp.agent(
                id="bench_executor", role="build", owns=["answers"], must_not=["deploy"],
                task=f"topic{i}_lookup", slot="current_fact", intent="answer", pipeline_id=f"bench_memo_{i}",
            )
            turn = f"what is the current topic{i} fact version"
            response = ncp.run(agent=agent, turn=turn, adapter=adapter, store=store, config=config)
            requests += 1
            expected = f"topic{i}:v{truth[i]}"
            correct = response.content == expected
            if response.cost_source == "memoized":
                memo_hits += 1
                false_hits += 0 if correct else 1
            else:
                input_tokens += response.input_tokens
                output_tokens += response.output_tokens
                cost += calculate_cost(
                    model=_MODEL, input_tokens=response.input_tokens, output_tokens=response.output_tokens,
                    pricing=config.pricing,
                ).total_cost_usd
            verified += 1 if correct else 0
            signature = response.evidence.memo_signature if response.evidence else None
            if memo and signature and response.cost_source != "memoized":
                # The host's independent verification closes the loop.
                store.update_memo_outcome(signature, 1.0 if correct else 0.0, verified=True)

            naive_key = (agent.agent_id, agent.task, agent.slot, turn)
            if naive_key in naive_cache:
                naive["would_hit"] += 1
                naive["would_false_hit"] += 0 if naive_cache[naive_key] == expected else 1
            else:
                naive_cache[naive_key] = response.content

        if round_index < rounds - 1:
            for i in range(0, tasks, mutate_every):
                truth[i] += 1
                new_id = f"fact_{i}_v{truth[i]}"
                store.write(SubconsciousChunk(
                    chunk_id=new_id, layer="semantic",
                    content=f"topic{i} fact version v{truth[i]} for the benchmark",
                    src="tool_result", pipeline_id=f"bench_memo_{i}", written_by="oracle",
                ))
                store.supersede(current_chunk[i], new_id)
                current_chunk[i] = new_id

    metrics = _arm_metrics(
        requests=requests, provider_calls=adapter.calls, memo_hits=memo_hits, false_hits=false_hits,
        verified=verified, input_tokens=input_tokens, output_tokens=output_tokens, cost=cost,
    )
    return metrics, naive


def run_memo_reuse_benchmark(
    *,
    workdir: str | Path,
    tasks: int = 12,
    rounds: int = 4,
    mutate_every: int = 3,
) -> dict[str, object]:
    """Run both arms and return a JSON-serializable artifact with a pass gate."""
    root = Path(workdir)
    off, _ = _run_arm(root, memo=False, tasks=tasks, rounds=rounds, mutate_every=mutate_every)
    safe, naive = _run_arm(root, memo=True, tasks=tasks, rounds=rounds, mutate_every=mutate_every)
    naive_rate = round(naive["would_false_hit"] / naive["would_hit"], 4) if naive["would_hit"] else 0.0

    off_cps = off["cost_per_verified_success_usd"]
    safe_cps = safe["cost_per_verified_success_usd"]
    summary = {
        "dependency_safe_false_hits": safe["false_hits"],
        "naive_key_would_false_hit": naive["would_false_hit"],
        "cost_per_verified_success_reduction": (
            round(1 - safe_cps / off_cps, 4) if off_cps and safe_cps is not None else None
        ),
        "pass": bool(
            safe["false_hits"] == 0
            and safe["verified_successes"] == safe["requests"]
            and off_cps is not None and safe_cps is not None and safe_cps < off_cps
            and naive["would_false_hit"] > 0
        ),
    }
    return {
        "benchmark": "memo_reuse",
        "token_unit": token_unit(),
        "config": {
            "tasks": tasks,
            "rounds": rounds,
            "mutate_every": mutate_every,
            "model": _MODEL,
            "output_tokens_per_call": _OUTPUT_TOKENS,
            "memoization": {"allow_unverified": False, "min_outcome": 0.5},
        },
        "arms": {
            "memo_off": off,
            "memo_dependency_safe": safe,
            "naive_key_counterfactual": {
                "description": "agent|task|slot + turn key, no verification gate; computed on the memo-on run.",
                **naive,
                "would_false_hit_rate": naive_rate,
            },
        },
        "summary": summary,
    }
