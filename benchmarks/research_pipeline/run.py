#!/usr/bin/env python3
"""Run the research-style bounded-context benchmark for NCP."""

from __future__ import annotations

import argparse
import json
import tempfile
from pathlib import Path

from ncp.benchmarks import run_research_pipeline_benchmark


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--turns", type=int, default=36, help="Number of turns in the benchmark pipeline.")
    parser.add_argument(
        "--pipeline-id",
        default="bench_research_pipeline",
        help="Pipeline identifier recorded in the benchmark artifact.",
    )
    parser.add_argument(
        "--store-path",
        type=Path,
        default=None,
        help="Optional SQLite store path. Defaults to a temporary benchmark store.",
    )
    args = parser.parse_args()

    if args.store_path is not None:
        artifact = run_research_pipeline_benchmark(
            store_path=args.store_path,
            turns=args.turns,
            pipeline_id=args.pipeline_id,
        )
        print(json.dumps(artifact, indent=2))
        return

    with tempfile.TemporaryDirectory(prefix="ncp-research-bench-") as tmpdir:
        store_path = Path(tmpdir) / "research-pipeline.db"
        artifact = run_research_pipeline_benchmark(
            store_path=store_path,
            turns=args.turns,
            pipeline_id=args.pipeline_id,
        )
        print(json.dumps(artifact, indent=2))


if __name__ == "__main__":
    main()
