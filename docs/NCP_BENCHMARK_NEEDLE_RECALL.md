# NCP Needle Recall Benchmark
## Retrieval-pressure eval against an equal-budget sliding window

This benchmark is intentionally uncomfortable.

It is meant to answer a narrower question than the pipeline token benchmarks:

- when important earlier facts are planted and the working budget stays tight,
  does NCP retrieve the right old constraints better than an equal-budget
  sliding window?

## Command

Run it from the repo root:

```bash
python3 benchmarks/needle/run.py --turns 24 --needles 6 --budget 4
```

## Current result

Observed on June 1, 2026:

- token unit: `word_split`
- budget mode: `chunk_budget`
- budget: `4` chunks
- final NCP recall: `0.50`
- final sliding-window recall: `0.00`
- reported deficit: `false`

First needle eviction turns for the sliding-window baseline:

- `needle_01`: retained through the final turn
- `needle_02`: retained through the final turn
- `needle_03`: retained through the final turn
- `needle_04`: evicted at turn `6`
- `needle_05`: evicted at turn `6`
- `needle_06`: evicted at turn `6`

## Interpretation

This is not a flattering benchmark, and that is the point.

The current artifact says:

- NCP beats an equal-budget sliding window on this retrieval-pressure setup
- but NCP still recalls only half of the planted needles at the final turn

That is useful signal.

It means the current retrieval stack is meaningfully better than a simple
window on this task shape, but it is not yet strong enough to claim that
important old constraints are preserved reliably under pressure.

## Artifact contract

The JSON output includes:

- planted needle definitions
- per-turn NCP recall
- per-turn sliding-window recall
- equal-budget window token estimates
- final recall summary
- first-eviction turn per needle

## Current claim

The honest claim supported by this artifact today is:

- on this retrieval-pressure benchmark, NCP outperforms an equal-budget sliding
  window, but still shows meaningful recall loss that should be improved before
  making stronger retention claims
