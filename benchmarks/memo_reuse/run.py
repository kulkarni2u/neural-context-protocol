#!/usr/bin/env python3
"""Memo reuse benchmark: cost per verified success and memo false-hit rate."""

from __future__ import annotations

import argparse
import json
import tempfile

from ncp.bench.memo_reuse import run_memo_reuse_benchmark


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tasks", type=int, default=12, help="Distinct tasks, each in its own pipeline.")
    parser.add_argument("--rounds", type=int, default=4, help="Times every task is asked.")
    parser.add_argument(
        "--mutate-every", type=int, default=3,
        help="Between rounds, the evidence of every Nth task is corrected (v1 -> v2 ...).",
    )
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="ncp-bench-memo-") as tmpdir:
        artifact = run_memo_reuse_benchmark(
            workdir=tmpdir, tasks=args.tasks, rounds=args.rounds, mutate_every=args.mutate_every,
        )
    print(json.dumps(artifact, indent=2))


if __name__ == "__main__":
    main()
