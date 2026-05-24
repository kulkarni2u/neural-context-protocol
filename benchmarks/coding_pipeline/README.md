# Coding Pipeline Benchmark

This benchmark compares:

- NCP bounded context assembly
- naive full-history replay

It uses:

- a deterministic 4-role pipeline
- a real SQLite NCP store
- the real assembler and persistence path
- a fixed word-split token heuristic matching the current runtime

Run it from the repo root:

```bash
python3 benchmarks/coding_pipeline/run.py
```

Useful options:

```bash
python3 benchmarks/coding_pipeline/run.py --turns 40
python3 benchmarks/coding_pipeline/run.py --turns 40 --store-path /tmp/ncp-bench.db
```

The command prints one JSON artifact with:

- per-turn NCP vs naive token counts
- peak/final token counts
- reduction factor
- pass/fail booleans for the current launch-credibility thresholds
