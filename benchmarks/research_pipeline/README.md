# Research Pipeline Benchmark

This benchmark compares:

- NCP bounded context assembly
- naive full-history replay

It uses:

- a deterministic 6-role research pipeline
- a real SQLite NCP store
- the real assembler and persistence path
- the same word-split token heuristic used by the current runtime

Run it from the repo root:

```bash
python3 benchmarks/research_pipeline/run.py
```

Useful options:

```bash
python3 benchmarks/research_pipeline/run.py --turns 36
python3 benchmarks/research_pipeline/run.py --turns 36 --store-path /tmp/ncp-research-bench.db
```

The command prints one JSON artifact with:

- per-turn NCP vs naive token counts
- peak/final token counts
- reduction factor
- pass/fail booleans for the current launch-credibility thresholds
