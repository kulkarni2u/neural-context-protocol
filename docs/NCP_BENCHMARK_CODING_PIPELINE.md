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
- `chars_div4` token accounting (deterministic default across environments)
  - set `NCP_TOKEN_UNIT=tiktoken` to count with cl100k_base when tiktoken and
    its encoding data are available; the artifact reports the active unit
- an explicit NCP context budget of `340` estimated tokens for this deterministic
  coding scenario

## Command

Run it from the repo root:

```bash
python3 benchmarks/coding_pipeline/run.py --turns 40
```

## Current result

Observed on June 9, 2026:

- token unit: `chars_div4`
- context token budget: `340`
- peak NCP input tokens: `370`
- peak raw replay input tokens: `3426`
- peak sliding-window input tokens: `383`
- peak rolling-summary input tokens: `2096`
- final NCP input tokens: `261`
- final raw replay input tokens: `3426`
- final sliding-window input tokens: `377`
- final rolling-summary input tokens: `2096`
- reduction factor vs raw replay at the final turn: `13.13x`
- reduction factor vs sliding window at the final turn: `1.44x`
- reduction factor vs rolling summary at the final turn: `8.03x`
- final-turn savings vs raw replay: `3165`
- estimated assembly overhead token-equivalent (total across all turns): `533.33`
- net total token-equivalent savings vs raw replay: `56230.67`
- benchmark pass gate: `true`

### Plain RAG arm (token accounting only)

A `plain_rag` baseline now also appears under `summary.baselines`: BM25 over the
raw transcript entries, queried with the same text NCP's assembler gets, packed
best-first into the same explicit context ceiling (340 tokens) NCP is capped at.
Measured with `python3 benchmarks/coding_pipeline/run.py --turns 40`:

| Arm | Peak input tokens | Final input tokens | Final vs NCP |
|---|---:|---:|---:|
| ncp | 371 | 260 | 1.00x |
| plain_rag | 377 | 358 | 1.38x |
| sliding_window | 383 | 377 | 1.45x |
| rolling_summary | 2096 | 2096 | 8.06x |
| raw_replay | 3426 | 3426 | 13.18x |

This is a token-count comparison only and says nothing about whether plain
RAG's context is adequate or worse. Because both arms are capped at the same
ceiling, plain RAG necessarily lands close to NCP; NCP's lower final-turn count
reflects it not filling the ceiling, not retrieval quality. The existing pass
gate is unchanged and does not involve plain RAG. The research pipeline
benchmark gets the same arm (40 turns: ncp peak 404 / final 272, plain_rag
peak 406 / final 258, so plain RAG is slightly smaller at the final turn there).

## Interpretation

This is a stronger bounded-context result than the earlier single-baseline
snapshot:

- NCP stays far below raw replay as turn depth grows
- NCP now beats the simple sliding-window baseline on the peak-token pass gate
  for this benchmark budget
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
- context token budget
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
