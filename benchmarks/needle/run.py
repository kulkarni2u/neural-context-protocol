#!/usr/bin/env python3
"""Needle recall benchmark for bounded-context retrieval."""

from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass
import json
from pathlib import Path
import tempfile

from ncp.assembler import Assembler
from ncp.api import agent
from ncp.benchmarks import estimate_tokens, token_unit
from ncp.stores.sqlite import SQLiteStore
from ncp.types import BudgetContext, ConsciousBlock, SubconsciousChunk


@dataclass(slots=True)
class Needle:
    chunk_id: str
    planted_at_turn: int
    kind: str
    query_text: str
    content: str


_KINDS = ("constraint", "decision", "dead_end")


def _build_needle(index: int) -> Needle:
    kind = _KINDS[index % len(_KINDS)]
    slug = f"{kind}_{index + 1}"
    return Needle(
        chunk_id=f"needle_{index + 1:02d}",
        planted_at_turn=index + 1,
        kind=kind,
        query_text=f"recover prior {kind} {slug} and preserve it in the current turn",
        content=(
            f"{kind} {slug} must remain active for the rest of the pipeline; "
            f"this is a planted recall benchmark fact for turn {index + 1:02d}."
        ),
    )


def _make_filler_chunk(turn: int, *, pipeline_id: str) -> SubconsciousChunk:
    return SubconsciousChunk(
        chunk_id=f"filler_{turn:02d}",
        layer="semantic" if turn % 2 else "episodic",
        content=(
            f"filler turn {turn:02d} resolves a local implementation detail and should not "
            "match any planted recall needle."
        ),
        src="synthesis",
        pipeline_id=pipeline_id,
        written_by=f"agent_{turn % 4}",
        relevance=0.15,
        base_trust=0.55,
    )


def _make_conscious(*, pipeline_id: str, turn: int, needle: Needle) -> ConsciousBlock:
    return agent(
        id="recall_auditor",
        role="review",
        owns=["verification"],
        must_not=["shipping"],
        task=f"needle_recall_turn_{turn:02d}",
        slot=needle.kind,
        intent="recover_planted_fact",
        pipeline_id=pipeline_id,
        recent=[],
        steps_completed=max(0, turn - 1),
        steps_total=turn,
    )


def needle_recall(
    *,
    store_path: str | Path,
    turns: int,
    k_needles: int = 8,
    budget: int = 4,
    pipeline_id: str = "bench_needle_recall",
) -> dict[str, object]:
    if turns < 2:
        raise ValueError("turns must be >= 2")
    if k_needles < 1:
        raise ValueError("k_needles must be >= 1")
    if budget < 1:
        raise ValueError("budget must be >= 1")
    if turns <= k_needles:
        raise ValueError("turns must be > k_needles to produce a recall curve")

    store = SQLiteStore(store_path)
    assembler = Assembler(store=store)
    needles = [_build_needle(index) for index in range(k_needles)]
    planted_order: list[str] = []
    content_by_chunk_id: dict[str, str] = {}
    recall_curve: list[dict[str, object]] = []
    first_evicted_turn: dict[str, int | None] = {needle.chunk_id: None for needle in needles}

    for turn in range(1, turns + 1):
        if turn <= k_needles:
            needle = needles[turn - 1]
            chunk = SubconsciousChunk(
                chunk_id=needle.chunk_id,
                layer="semantic",
                content=needle.content,
                src="user_verified",
                pipeline_id=pipeline_id,
                written_by="benchmark",
                relevance=1.0,
                base_trust=0.98,
                conditions=[needle.kind],
            )
        else:
            chunk = _make_filler_chunk(turn, pipeline_id=pipeline_id)
        store.write(chunk)
        planted_order.append(chunk.chunk_id)
        content_by_chunk_id[chunk.chunk_id] = chunk.content

        if turn < k_needles:
            continue

        ncp_hits = 0
        sliding_hits = 0
        per_needle: list[dict[str, object]] = []
        sliding_window_ids = planted_order[-budget:]
        sliding_window_text = "\n".join(content_by_chunk_id[chunk_id] for chunk_id in sliding_window_ids)

        for needle in needles:
            conscious = _make_conscious(pipeline_id=pipeline_id, turn=turn, needle=needle)
            assembly = assembler.assemble(
                conscious=conscious,
                budget=BudgetContext(
                    ctx_used=min(0.95, turn / max(turns, 1)),
                    steps_completed=max(0, turn - 1),
                    steps_total=turns,
                    elapsed_seconds=float(turn * 9),
                    pressure="critical" if turn >= turns - 3 else "medium",
                ),
                query_text=needle.query_text,
                k=budget,
            )
            retrieved_ids = [chunk.chunk_id for chunk in assembly.chunks]
            ncp_present = needle.chunk_id in retrieved_ids
            sliding_present = needle.chunk_id in sliding_window_ids
            if ncp_present:
                ncp_hits += 1
            elif first_evicted_turn[needle.chunk_id] is None:
                first_evicted_turn[needle.chunk_id] = turn
            if sliding_present:
                sliding_hits += 1
            per_needle.append(
                {
                    "chunk_id": needle.chunk_id,
                    "kind": needle.kind,
                    "ncp_present": ncp_present,
                    "sliding_window_present": sliding_present,
                    "retrieved_chunk_ids": retrieved_ids,
                }
            )

        ncp_recall = ncp_hits / len(needles)
        sliding_recall = sliding_hits / len(needles)
        recall_curve.append(
            {
                "turn": turn,
                "ncp_recall": round(ncp_recall, 4),
                "sliding_window_recall": round(sliding_recall, 4),
                "ncp_hits": ncp_hits,
                "sliding_window_hits": sliding_hits,
                "window_chunk_budget": budget,
                "window_token_estimate": estimate_tokens(sliding_window_text),
                "needles": per_needle,
            }
        )

    final_row = recall_curve[-1]
    ncp_final = float(final_row["ncp_recall"])
    sliding_final = float(final_row["sliding_window_recall"])
    reported_deficit = ncp_final < sliding_final

    return {
        "benchmark": "needle_recall",
        "pipeline_id": pipeline_id,
        "turns": turns,
        "budget": {
            "mode": "chunk_budget",
            "chunks": budget,
            "token_unit": token_unit(),
        },
        "needles": [asdict(needle) for needle in needles],
        "recall_curve": recall_curve,
        "summary": {
            "recall_at_final": {
                "ncp": round(ncp_final, 4),
                "sliding_window": round(sliding_final, 4),
            },
            "ncp_beats_sliding_window": ncp_final >= sliding_final,
            "reported_deficit": reported_deficit,
            "first_evicted_turn": first_evicted_turn,
        },
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--turns", type=int, default=48)
    parser.add_argument("--needles", type=int, default=8)
    parser.add_argument("--budget", type=int, default=4)
    parser.add_argument("--pipeline-id", default="bench_needle_recall")
    parser.add_argument("--store-path", type=Path, default=None)
    args = parser.parse_args()

    if args.store_path is not None:
        artifact = needle_recall(
            store_path=args.store_path,
            turns=args.turns,
            k_needles=args.needles,
            budget=args.budget,
            pipeline_id=args.pipeline_id,
        )
        print(json.dumps(artifact, indent=2))
        return

    with tempfile.TemporaryDirectory(prefix="ncp-needle-bench-") as tmpdir:
        artifact = needle_recall(
            store_path=Path(tmpdir) / "needle.db",
            turns=args.turns,
            k_needles=args.needles,
            budget=args.budget,
            pipeline_id=args.pipeline_id,
        )
        print(json.dumps(artifact, indent=2))


if __name__ == "__main__":
    main()
