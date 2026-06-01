# NCP Coding Pipeline Benchmark
## Bounded-context result against raw replay, sliding window, and rolling summary

This document records the first runnable benchmark artifact for NCP.

It currently measures:

- NCP bounded context assembly
- raw full-history replay as a floor
- a fixed-entry sliding-window baseline
- a simple rolling-summary baseline

The benchmark uses:

- a deterministic 4-role pipeline
- a real SQLite store
- the real assembler and post-turn persistence path
- `word_split` token accounting in this environment
  - if `tiktoken` is installed, the benchmark automatically reports that
    instead

## Command

Run it from the repo root:

```bash
python3 benchmarks/coding_pipeline/run.py --turns 40
```

## Current result

Observed on June 1, 2026:

- peak NCP input tokens: `174`
- peak raw replay input tokens: `1927`
- peak sliding-window input tokens: `212`
- peak rolling-summary input tokens: `1176`
- final NCP input tokens: `110`
- final raw replay input tokens: `1927`
- final sliding-window input tokens: `212`
- final rolling-summary input tokens: `1176`
- reduction factor vs raw replay at the final turn: `17.52x`
- reduction factor vs sliding window at the final turn: `1.93x`
- reduction factor vs rolling summary at the final turn: `10.69x`
- final-turn savings vs raw replay: `1817`
- estimated assembly overhead token-equivalent (total across all turns): `533.33`
- net total token-equivalent savings vs raw replay: see artifact `economics.net_total_token_equivalent_vs_raw_replay`

## Interpretation

This is a stronger bounded-context result than the earlier single-baseline
snapshot:

- NCP stays far below raw replay as turn depth grows
- NCP still beats the simple sliding-window baseline at the same benchmark
  checkpoint
- NCP substantially beats the simple rolling-summary baseline
- the turn-40 path remains comfortably under the current `<= 2000` launch gate
- the benchmark is deterministic and rerunnable from the repo

What it does **not** show yet:

- whether quality is retained at matched budget
- whether a real model succeeds more often with NCP context
- whether these exact baseline settings are the strongest reasonable competing
  strategies

This is still only one benchmark shape.
The complementary research-style benchmark now exists in
`docs/NCP_BENCHMARK_RESEARCH_PIPELINE.md`.

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

- on this coding-pipeline benchmark, NCP keeps context materially more bounded
  than raw replay, the current fixed sliding-window baseline, and the current
  rolling-summary baseline
