# NCP Matched-Budget Efficacy Benchmark

This page records the benchmark contract. The runnable implementation is
documented in [Provider-Real Efficacy Benchmark](./NCP_BENCHMARK_EFFICACY_LIVE.md).

The current harness compares:

- `ncp`
- `sliding_window`
- `rolling_summary`

All three conditions run against the same task set and the same requested
context budget. The default `mock` provider is deterministic and keyless. The
`anthropic` provider is live and writes a skip artifact when
`ANTHROPIC_API_KEY` is unset.

```bash
python3 benchmarks/efficacy/run.py --provider mock --seeds 2
python3 benchmarks/efficacy/run.py --provider anthropic --budget 400 --seeds 5
```

Interpretation remains bounded: mock mode measures context adequacy, not model
quality; live mode measures one provider on one task family unless expanded.
