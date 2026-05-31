# MACE — Multi-Agent Context Efficiency Benchmark

MACE is the benchmark suite for multi-agent context coordination efficiency.
It lives inside the NCP repo and is reproducible from a clean checkout.

## What it measures

| Dimension | What it measures |
|---|---|
| D1 | token cost as pipeline depth increases |
| D2 | context handoff quality between agents |
| D3 | dead-end path prevention |
| D4 | goal change propagation speed |

Each dimension scores `0.0` to `1.0`. Higher is better. The composite MACE
score is the weighted mean of the dimensions that were run.

## Run it

```bash
python benchmarks/mace/run.py
python benchmarks/mace/run.py --dims d1,d2
python benchmarks/mace/run.py --turns 60
python benchmarks/mace/run.py --compare benchmarks/mace/results/community/TEMPLATE.json
```

Outputs:

- `benchmarks/mace/results/ncp.json`
- `benchmarks/mace/results/baseline.json`
- `benchmarks/mace/results/traces/ncp_trace.json`

## Current canonical result

Canonical run:

```bash
python benchmarks/mace/run.py --turns 40
```

Observed NCP result on the current codebase:

- Composite MACE score: `0.9608`
- D1 Token Efficiency: `0.8695`
- D2 Handoff Quality: `1.0000`
- D3 Dead-end Prevention: `1.0000`
- D4 Goal Coherence: `1.0000`

D1 primary checkpoint at turn 40:

- baseline tokens: `1927`
- NCP tokens: `110`
- reduction ratio: `17.52x`
- reduction percentage: `94.3%`

## Scoring

- D1 score of `1.0` = `20x` token reduction at the primary checkpoint
- D2 score of `1.0` = all three handoff checks pass
- D3 score of `1.0` = zero dead-end retries
- D4 score of `1.0` = all agents update within one turn after the goal change

Weights:

- D1: `0.30`
- D2: `0.25`
- D3: `0.25`
- D4: `0.20`

## Design notes

- D1 is wired to the existing `benchmarks/coding_pipeline/` benchmark. No
  duplication.
- D2–D4 run on the real NCP store and assembler path with deterministic agent
  outputs so the suite is reproducible without provider credentials.
- Community comparison artifacts should follow
  `benchmarks/mace/results/community/TEMPLATE.json`.
