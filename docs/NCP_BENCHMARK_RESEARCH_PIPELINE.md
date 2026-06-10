# NCP Research Pipeline Benchmark
## Bounded-context result against raw replay, sliding window, and rolling summary

This document records the research-style benchmark artifact for NCP.

It currently measures:

- NCP bounded context assembly
- raw full-history replay as a floor
- a fixed-entry sliding-window baseline
- a simple rolling-summary baseline

The benchmark uses:

- a deterministic 6-role research pipeline
- a real SQLite store
- the real assembler and post-turn persistence path
- `chars_div4` token accounting in this environment
  - if `tiktoken` is installed, the benchmark automatically reports that
    instead

## Command

Run it from the repo root:

```bash
python3 benchmarks/research_pipeline/run.py --turns 36
```

## Current result

Observed on June 9, 2026:

- peak NCP input tokens: `388`
- peak raw replay input tokens: `3277`
- peak sliding-window input tokens: `412`
- peak rolling-summary input tokens: `1912`
- final NCP input tokens: `267`
- final raw replay input tokens: `3277`
- final sliding-window input tokens: `409`
- final rolling-summary input tokens: `1912`
- reduction factor vs raw replay at the final turn: `12.27x`
- reduction factor vs sliding window at the final turn: `1.53x`
- reduction factor vs rolling summary at the final turn: `7.16x`
- final-turn savings vs raw replay: `3010`
- estimated assembly overhead token-equivalent (total across all turns): `480.0`
- net total token-equivalent savings vs raw replay: `48236.0`

## Interpretation

This is a stronger bounded-context result than the earlier single-baseline
snapshot:

- NCP stays far below raw replay in a tool-heavier research-shaped flow
- NCP still beats the simple sliding-window baseline at the same benchmark
  checkpoint
- NCP substantially beats the simple rolling-summary baseline
- the turn-36 path remains comfortably under the current `<= 2000` launch gate
- the benchmark is deterministic and rerunnable from the repo

What it does **not** show yet:

- whether quality is retained at matched budget
- whether a real model succeeds more often with NCP context
- whether these exact baseline settings are the strongest reasonable competing
  strategies

## Artifact contract

The JSON output includes:

- per-turn token counts
- token unit
- peak/final token counts for:
  - `ncp`
  - `raw_replay`
  - `sliding_window`
  - `rolling_summary`
- assembly-overhead economics summary
- pass/fail booleans

## Current claim

The honest claim supported by this artifact today is:

- on this research-pipeline benchmark, NCP keeps context materially more
  bounded than raw replay, the current fixed sliding-window baseline, and the
  current rolling-summary baseline
