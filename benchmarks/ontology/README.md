# Ontology Benchmark

Compares baseline NCP (no `[ontology]` section) with NCP + ontology
(`enabled = true`, default limits). Both arms use the same `ncp_get_context`
handler on a fixed synthetic infrastructure corpus with dependency tasks and
non-graph control tasks. Reported metrics: p50/p95 latency, prompt tokens,
fact and evidence recall/precision, tokens to reach full recall, unsupported
assertion rate, SQL statements per call (SQLite) and graph cost.

```bash
python3 benchmarks/ontology/run.py                      # SQLite
NCP_PGVECTOR_DSN=... NCP_REDIS_URL=... python3 benchmarks/ontology/run.py   # + Postgres
python3 benchmarks/ontology/run.py --max-graph-tokens 150 --repeats 15
```

Methodology, measured results and the verdict for each design hypothesis
are in [`docs/NCP_BENCHMARK_ONTOLOGY.md`](../../docs/NCP_BENCHMARK_ONTOLOGY.md).
