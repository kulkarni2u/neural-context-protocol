# Harness and Subagent Overhead Benchmark

Deterministic, in-process (SQLite, mock provider, `chars_div4`) measurement of
the characters the harness spends around a turn: tool catalogs per profile,
`ncp_post_turn` arguments, handoff returns, cross-turn delta resends, prefix
stability, budget accuracy and skill dispatch. Each "before" is the new code with
the old behavior switched on.

```bash
python3 benchmarks/harness_overhead/run.py
```

The run exits non-zero from CI when `summary.pass` is false. See
[`docs/NCP_BENCHMARK_HARNESS_OVERHEAD.md`](../../docs/NCP_BENCHMARK_HARNESS_OVERHEAD.md)
for the table, methodology and caveats (structural counts, not provider tokens or dollars).
