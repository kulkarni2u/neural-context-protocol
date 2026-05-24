# NCP Coding Pipeline Benchmark
## First reproducible bounded-context result against naive replay

This document records the first runnable benchmark artifact for NCP.

It measures:

- NCP bounded context assembly
- naive full-history replay

The benchmark uses:

- a deterministic 4-role pipeline
- a real SQLite store
- the real assembler and post-turn persistence path
- the same simple word-split token heuristic used by the current runtime

## Command

Run it from the repo root:

```bash
python3 benchmarks/coding_pipeline/run.py --turns 40
```

## Current result

Observed on May 23, 2026:

- peak NCP input tokens: `174`
- peak naive replay input tokens: `1927`
- final NCP input tokens: `110`
- final naive replay input tokens: `1927`
- reduction factor at the final turn: `17.52x`

## Interpretation

This is a credible first bounded-context result:

- NCP stays far below naive replay as turn depth grows
- the turn-40 path remains comfortably under the current `<= 2000` launch gate
- the benchmark is deterministic and rerunnable from the repo

This is still only one benchmark shape.
The complementary research-style benchmark now exists in
`docs/NCP_BENCHMARK_RESEARCH_PIPELINE.md`.

## Artifact contract

The JSON output includes:

- per-turn token counts
- peak token counts
- final token counts
- reduction factor
- pass/fail booleans

## Current claim

The honest claim supported by this artifact is:

- on the first coding-pipeline benchmark, NCP keeps context materially more
  bounded than naive replay
