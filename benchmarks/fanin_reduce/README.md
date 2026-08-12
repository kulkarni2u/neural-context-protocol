# Fan-in Reduction Benchmark

This benchmark compares three ways a synthesis agent can read the output of
many parallel workers writing into one pipeline:

- `raw_dump`: every worker's raw content concatenated, no bound at all
- `ncp_default`: NCP's existing bounded top-k retrieval, reducer off
- `ncp_reduced`: the same retrieval, overfetched and deterministically
  reduced (near-duplicate merge, malformed drop, contradiction flag) before
  the same top-k cap

It uses:

- a deterministic 40-worker / 4-topic synthetic corpus (hand-paraphrased
  claims, not randomly generated) with a controlled fraction of genuine
  contradictions and malformed outputs
- a real SQLite NCP store and the real assembler/retrieval path
- explicit token-unit reporting (`chars_div4` without `tiktoken`,
  `tiktoken/cl100k_base` when available)
- `ncp.costs.calculate_cost` against the configured pricing table for the
  synthesis-model cost delta

Run it from the repo root:

```bash
python3 benchmarks/fanin_reduce/run.py
```

Useful options:

```bash
python3 benchmarks/fanin_reduce/run.py --workers 40
python3 benchmarks/fanin_reduce/run.py --workers 80 --k 24
python3 benchmarks/fanin_reduce/run.py --store-path /tmp/ncp-bench.db
```

See [`docs/NCP_BENCHMARK_FANIN_REDUCE.md`](../../docs/NCP_BENCHMARK_FANIN_REDUCE.md)
for the full methodology, current numbers, and an honest account of the
reducer's known false-positive rate on contradiction flagging.
