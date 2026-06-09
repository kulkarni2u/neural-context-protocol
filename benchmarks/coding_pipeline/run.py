#!/usr/bin/env python3
"""Run the first reproducible bounded-context benchmark for NCP."""

from __future__ import annotations

import argparse
import json
import tempfile
from pathlib import Path

from ncp.benchmarks import run_coding_pipeline_benchmark


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--turns", type=int, default=40, help="Number of turns in the benchmark pipeline.")
    parser.add_argument(
        "--pipeline-id",
        default="bench_coding_pipeline",
        help="Pipeline identifier recorded in the benchmark artifact.",
    )
    parser.add_argument(
        "--store-path",
        type=Path,
        default=None,
        help="Optional SQLite store path. Defaults to a temporary benchmark store.",
    )
    parser.add_argument(
        "--context-token-budget",
        type=int,
        default=340,
        help="Estimated token ceiling for each assembled NCP context block.",
    )
    args = parser.parse_args()

    if args.store_path is not None:
        artifact = run_coding_pipeline_benchmark(
            store_path=args.store_path,
            turns=args.turns,
            pipeline_id=args.pipeline_id,
            context_token_budget=args.context_token_budget,
        )
        print(json.dumps(artifact, indent=2))
        return

    with tempfile.TemporaryDirectory(prefix="ncp-bench-") as tmpdir:
        store_path = Path(tmpdir) / "coding-pipeline.db"
        artifact = run_coding_pipeline_benchmark(
            store_path=store_path,
            turns=args.turns,
            pipeline_id=args.pipeline_id,
            context_token_budget=args.context_token_budget,
        )
        print(json.dumps(artifact, indent=2))


if __name__ == "__main__":
    main()
