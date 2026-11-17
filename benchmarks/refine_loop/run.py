#!/usr/bin/env python3
"""Refine loop benchmark: repair -> prune with gates, improvement records and auto-calibration."""

from __future__ import annotations

import argparse
import json
import tempfile

from ncp.bench.refine_loop import run_refine_loop_benchmark


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="ncp-bench-refine-") as tmpdir:
        artifact = run_refine_loop_benchmark(workdir=tmpdir)
    print(json.dumps(artifact, indent=2))


if __name__ == "__main__":
    main()
