#!/usr/bin/env python3
"""Ontology benchmark: baseline NCP vs NCP + ontology (latency, tokens, evidence)."""

from __future__ import annotations

import argparse
import json
import os
import tempfile

from ncp.bench.ontology import DEFAULT_BUDGET, ENABLED_TOML, run_ontology_benchmark


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--backends", default=None,
        help="Comma list of sqlite,postgres (default: sqlite, plus postgres when "
             "NCP_PGVECTOR_DSN is set).",
    )
    parser.add_argument("--dsn", default=os.environ.get("NCP_PGVECTOR_DSN"),
                        help="PostgreSQL DSN for the postgres backend.")
    parser.add_argument("--redis-url", default=os.environ.get("NCP_REDIS_URL"),
                        help="Redis URL the pgvector store needs for whispers.")
    parser.add_argument("--repeats", type=int, default=15, help="Timed rounds over all tasks.")
    parser.add_argument("--budget", type=int, default=DEFAULT_BUDGET,
                        help="max_tokens for the fixed-budget pass and latency rounds.")
    parser.add_argument("--max-graph-tokens", type=int, default=None,
                        help="Override [ontology].max_graph_tokens for the ontology arm.")
    args = parser.parse_args()
    if args.backends:
        backends = tuple(item.strip() for item in args.backends.split(",") if item.strip())
    else:
        backends = ("sqlite", "postgres") if args.dsn else ("sqlite",)
    toml = ENABLED_TOML
    if args.max_graph_tokens is not None:
        toml += f"max_graph_tokens = {args.max_graph_tokens}\n"
    with tempfile.TemporaryDirectory(prefix="ncp-bench-ontology-") as tmpdir:
        artifact = run_ontology_benchmark(
            workdir=tmpdir, backends=backends, budget=args.budget, repeats=args.repeats,
            dsn=args.dsn, redis_url=args.redis_url, ontology_toml=toml,
        )
    print(json.dumps(artifact, indent=2))


if __name__ == "__main__":
    main()
