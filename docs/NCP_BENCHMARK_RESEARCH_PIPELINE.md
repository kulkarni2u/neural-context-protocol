# NCP Research Pipeline Benchmark
## Second reproducible bounded-context result against naive replay

This document records the research-style benchmark artifact for NCP.

It measures:

- NCP bounded context assembly
- naive full-history replay

The benchmark uses:

- a deterministic 6-role research pipeline
- a real SQLite store
- the real assembler and post-turn persistence path
- the same simple word-split token heuristic used by the current runtime

## Command

Run it from the repo root:

```bash
python3 benchmarks/research_pipeline/run.py --turns 36
```

## Current result

Observed on May 23, 2026:

- peak NCP input tokens: `156`
- peak naive replay input tokens: `1700`
- final NCP input tokens: `104`
- final naive replay input tokens: `1700`
- reduction factor at the final turn: `16.35x`

## Interpretation

This is a credible second bounded-context result:

- NCP stays far below naive replay in a tool-heavier research-shaped flow
- the turn-36 path remains comfortably under the current `<= 2000` launch gate
- the benchmark is deterministic and rerunnable from the repo

## Artifact contract

The JSON output includes:

- per-turn token counts
- peak token counts
- final token counts
- reduction factor
- pass/fail booleans

## Current claim

The honest claim supported by this artifact is:

- on the research-pipeline benchmark, NCP keeps context materially more bounded
  than naive replay
