#!/usr/bin/env python3
"""High-fanout (N workers -> 1 synthesizer) fan-in reduction benchmark for NCP."""

from __future__ import annotations

import argparse
import json
import tempfile
from pathlib import Path

from ncp.benchmarks import run_fanin_reduce_benchmark


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workers", type=int, default=40, help="Number of parallel workers writing to the pipeline.")
    parser.add_argument(
        "--pipeline-id",
        default="bench_fanin_reduce",
        help="Pipeline identifier recorded in the benchmark artifact.",
    )
    parser.add_argument(
        "--store-path",
        type=Path,
        default=None,
        help="Optional SQLite store path. Defaults to a temporary benchmark store.",
    )
    parser.add_argument("--k", type=int, default=16, help="Chunk cap the synthesis agent reads at.")
    parser.add_argument(
        "--context-token-budget",
        type=int,
        default=4000,
        help="Estimated token ceiling for the synthesis agent's assembled NCP context block.",
    )
    args = parser.parse_args()

    if args.store_path is not None:
        artifact = run_fanin_reduce_benchmark(
            store_path=args.store_path,
            workers=args.workers,
            pipeline_id=args.pipeline_id,
            k=args.k,
            context_token_budget=args.context_token_budget,
        )
        print(json.dumps(artifact, indent=2))
        return

    with tempfile.TemporaryDirectory(prefix="ncp-bench-") as tmpdir:
        store_path = Path(tmpdir) / "fanin-reduce.db"
        artifact = run_fanin_reduce_benchmark(
            store_path=store_path,
            workers=args.workers,
            pipeline_id=args.pipeline_id,
            k=args.k,
            context_token_budget=args.context_token_budget,
        )
        print(json.dumps(artifact, indent=2))


if __name__ == "__main__":
    main()
