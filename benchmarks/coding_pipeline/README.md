# Coding Pipeline Benchmark

This benchmark compares:

- NCP bounded context assembly
- naive full-history replay

It uses:

- a deterministic 4-role pipeline
- a real SQLite NCP store
- the real assembler and persistence path
- explicit token-unit reporting (`chars_div4` without `tiktoken`,
  `tiktoken/cl100k_base` when available)
- a default 340-token NCP context budget for this deterministic coding scenario

Run it from the repo root:

```bash
python3 benchmarks/coding_pipeline/run.py
```

Useful options:

```bash
python3 benchmarks/coding_pipeline/run.py --turns 40
python3 benchmarks/coding_pipeline/run.py --turns 40 --store-path /tmp/ncp-bench.db
python3 benchmarks/coding_pipeline/run.py --turns 40 --context-token-budget 340
```

The command prints one JSON artifact with:

- per-turn NCP vs naive token counts
- peak/final token counts
- context token budget
- reduction factor
- pass/fail booleans for the current launch-credibility thresholds
