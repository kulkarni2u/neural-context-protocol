#!/usr/bin/env python3
"""Retrieval quality benchmark — BM25 recall@k on a 24-chunk labeled set."""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import json
from pathlib import Path
import tempfile

from ncp.benchmarks import token_unit
from ncp.stores.sqlite import SQLiteStore
from ncp.types import SubconsciousChunk


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class LabeledQuery:
    query_text: str
    relevant_chunk_ids: list[str]
    irrelevant_chunk_ids: list[str]


# ---------------------------------------------------------------------------
# Labeled set definitions
# ---------------------------------------------------------------------------

_SIGNAL_CHUNKS: list[tuple[str, str, str]] = [
    # (chunk_id, layer, content)
    # Constraint chunks
    (
        "ret_constraint_01",
        "semantic",
        "oauth authentication constraint requires token-based access for all API endpoints",
    ),
    (
        "ret_constraint_02",
        "semantic",
        "rate limiting constraint maximum 100 requests per minute enforced at gateway",
    ),
    (
        "ret_constraint_03",
        "semantic",
        "data retention constraint records must be purged after 90 days per compliance policy",
    ),
    (
        "ret_constraint_04",
        "semantic",
        "encryption constraint all data at rest must use AES-256 cipher standard",
    ),
    # Decision chunks
    (
        "ret_decision_01",
        "procedural",
        "architecture decision chose microservices over monolith for scalability requirements",
    ),
    (
        "ret_decision_02",
        "procedural",
        "database decision chose postgresql for ACID compliance requirements and reliability",
    ),
    (
        "ret_decision_03",
        "procedural",
        "caching decision chose redis for session storage performance and low latency",
    ),
    (
        "ret_decision_04",
        "procedural",
        "deployment decision chose kubernetes for container orchestration at scale",
    ),
    # Dead-end chunks
    (
        "ret_dead_01",
        "episodic",
        "dead end api/v1 endpoint deprecated and removed from service after migration",
    ),
    (
        "ret_dead_02",
        "episodic",
        "dead end oauth_basic approach rejected due to security vulnerabilities discovered in audit",
    ),
    (
        "ret_dead_03",
        "episodic",
        "dead end synchronous processing abandoned due to timeout failures under high load",
    ),
    (
        "ret_dead_04",
        "episodic",
        "dead end monolithic deployment failed due to scaling bottlenecks at peak traffic",
    ),
]

_DISTRACTOR_CONTENT = [
    "filler implementation detail for routine processing step {n}",
    "generic utility function handles boilerplate for step {n}",
    "background task scheduler tick interval configured for step {n}",
    "logging configuration output verbosity level set for step {n}",
]

_LABELED_QUERIES: list[LabeledQuery] = [
    LabeledQuery(
        query_text="authentication oauth token security",
        relevant_chunk_ids=["ret_constraint_01"],
        irrelevant_chunk_ids=["ret_dead_01", "ret_dead_02"],
    ),
    LabeledQuery(
        query_text="rate limit throttle requests per minute",
        relevant_chunk_ids=["ret_constraint_02"],
        irrelevant_chunk_ids=[],
    ),
    LabeledQuery(
        query_text="data retention purge records expiry",
        relevant_chunk_ids=["ret_constraint_03"],
        irrelevant_chunk_ids=[],
    ),
    LabeledQuery(
        query_text="encryption data at rest AES",
        relevant_chunk_ids=["ret_constraint_04"],
        irrelevant_chunk_ids=[],
    ),
    LabeledQuery(
        query_text="microservices architecture scalability",
        relevant_chunk_ids=["ret_decision_01"],
        irrelevant_chunk_ids=["ret_dead_04"],
    ),
    LabeledQuery(
        query_text="postgresql database ACID transactions",
        relevant_chunk_ids=["ret_decision_02"],
        irrelevant_chunk_ids=[],
    ),
    LabeledQuery(
        query_text="redis session cache performance",
        relevant_chunk_ids=["ret_decision_03"],
        irrelevant_chunk_ids=[],
    ),
    LabeledQuery(
        query_text="kubernetes container deployment orchestration",
        relevant_chunk_ids=["ret_decision_04"],
        irrelevant_chunk_ids=[],
    ),
    LabeledQuery(
        query_text="api v1 deprecated endpoint removed",
        relevant_chunk_ids=["ret_dead_01"],
        irrelevant_chunk_ids=[],
    ),
    LabeledQuery(
        query_text="oauth basic security vulnerabilities rejected",
        relevant_chunk_ids=["ret_dead_02"],
        irrelevant_chunk_ids=[],
    ),
    LabeledQuery(
        query_text="synchronous timeout failures abandoned",
        relevant_chunk_ids=["ret_dead_03"],
        irrelevant_chunk_ids=[],
    ),
    LabeledQuery(
        query_text="monolithic scaling bottleneck failed",
        relevant_chunk_ids=["ret_dead_04"],
        irrelevant_chunk_ids=[],
    ),
]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _plant_chunks(store: SQLiteStore, pipeline_id: str) -> None:
    """Write all 24 chunks (12 signal + 12 distractor) into the store."""
    for chunk_id, layer, content in _SIGNAL_CHUNKS:
        store.write(
            SubconsciousChunk(
                chunk_id=chunk_id,
                layer=layer,  # type: ignore[arg-type]
                content=content,
                src="tool_result",
                pipeline_id=pipeline_id,
                base_trust=0.85,
                generation=0,
                written_by="benchmark",
            )
        )

    for n in range(1, 13):
        template = _DISTRACTOR_CONTENT[(n - 1) % len(_DISTRACTOR_CONTENT)]
        store.write(
            SubconsciousChunk(
                chunk_id=f"ret_distractor_{n:02d}",
                layer="reasoning_trace",  # type: ignore[arg-type]
                content=template.format(n=n),
                src="synthesis",
                pipeline_id=pipeline_id,
                base_trust=0.5,
                generation=0,
                written_by="benchmark",
            )
        )


def _query_with_diversity(
    store: SQLiteStore,
    query_text: str,
    k: int,
    pipeline_id: str,
    diversity_limit: int,
) -> list[str]:
    results = store.query(
        query_text,
        k=k,
        pipeline_id=pipeline_id,
        diversity_limit=diversity_limit,
    )
    return [chunk.chunk_id for chunk in results]


def _count_constraint_chunks(chunk_ids: list[str]) -> int:
    constraint_ids = {"ret_constraint_01", "ret_constraint_02", "ret_constraint_03", "ret_constraint_04"}
    return sum(1 for cid in chunk_ids if cid in constraint_ids)


# ---------------------------------------------------------------------------
# Main benchmark function
# ---------------------------------------------------------------------------


def retrieval_quality(
    *,
    store_path: str | Path,
    k: int = 4,
    pipeline_id: str = "bench_retrieval_quality",
) -> dict[str, object]:
    """Run labeled retrieval harness and return metrics artifact."""
    store = SQLiteStore(store_path)
    _plant_chunks(store, pipeline_id)

    per_query_results: list[dict[str, object]] = []

    for lq in _LABELED_QUERIES:
        retrieved_ids = _query_with_diversity(
            store, lq.query_text, k, pipeline_id, diversity_limit=2
        )
        relevant_set = set(lq.relevant_chunk_ids)
        retrieved_set = set(retrieved_ids)

        hits = retrieved_set & relevant_set
        precision_at_k = len(hits) / k if k > 0 else 0.0
        recall_at_k = len(hits) / len(relevant_set) if relevant_set else 0.0

        # Find rank of first relevant chunk (1-indexed)
        relevant_rank: int | None = None
        for rank, cid in enumerate(retrieved_ids, start=1):
            if cid in relevant_set:
                relevant_rank = rank
                break

        per_query_results.append(
            {
                "query_text": lq.query_text,
                "relevant_chunk_ids": lq.relevant_chunk_ids,
                "retrieved_chunk_ids": retrieved_ids,
                "precision_at_k": round(precision_at_k, 4),
                "recall_at_k": round(recall_at_k, 4),
                "relevant_rank": relevant_rank,
            }
        )

    # Diversity cap test — use query 1 (authentication oauth token security)
    diversity_query = _LABELED_QUERIES[0].query_text
    ids_div1 = _query_with_diversity(store, diversity_query, k, pipeline_id, diversity_limit=1)
    ids_div2 = _query_with_diversity(store, diversity_query, k, pipeline_id, diversity_limit=2)
    # Default: SQLiteStore default is diversity_limit=2; use a large value for "no cap"
    ids_div_default = _query_with_diversity(store, diversity_query, k, pipeline_id, diversity_limit=100)

    # Summary metrics
    precisions = [float(row["precision_at_k"]) for row in per_query_results]
    recalls = [float(row["recall_at_k"]) for row in per_query_results]
    ranks = [row["relevant_rank"] for row in per_query_results if row["relevant_rank"] is not None]

    mean_precision = sum(precisions) / len(precisions) if precisions else 0.0
    mean_recall = sum(recalls) / len(recalls) if recalls else 0.0
    mean_rank = sum(ranks) / len(ranks) if ranks else None
    perfect_recall = sum(1 for r in recalls if r >= 1.0)

    return {
        "benchmark": "retrieval_quality",
        "pipeline_id": pipeline_id,
        "k": k,
        "token_unit": token_unit(),
        "labeled_set": {
            "total_chunks": 24,
            "queries": 12,
        },
        "per_query_results": per_query_results,
        "summary": {
            "mean_precision_at_k": round(mean_precision, 4),
            "mean_recall_at_k": round(mean_recall, 4),
            "mean_relevant_rank": round(mean_rank, 4) if mean_rank is not None else None,
            "queries_with_perfect_recall": perfect_recall,
            "diversity_cap_test": {
                "diversity_limit_1": {
                    "constraint_chunks_in_top_k": _count_constraint_chunks(ids_div1)
                },
                "diversity_limit_2": {
                    "constraint_chunks_in_top_k": _count_constraint_chunks(ids_div2)
                },
                "diversity_limit_default": {
                    "constraint_chunks_in_top_k": _count_constraint_chunks(ids_div_default)
                },
            },
        },
    }


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--k", type=int, default=4, help="Number of results to retrieve per query")
    parser.add_argument("--pipeline-id", default="bench_retrieval_quality")
    parser.add_argument("--store-path", type=Path, default=None)
    args = parser.parse_args()

    if args.store_path is not None:
        artifact = retrieval_quality(
            store_path=args.store_path,
            k=args.k,
            pipeline_id=args.pipeline_id,
        )
        print(json.dumps(artifact, indent=2))
        return

    with tempfile.TemporaryDirectory(prefix="ncp-ret-bench-") as tmpdir:
        artifact = retrieval_quality(
            store_path=Path(tmpdir) / "ret.db",
            k=args.k,
            pipeline_id=args.pipeline_id,
        )
        print(json.dumps(artifact, indent=2))


if __name__ == "__main__":
    main()
