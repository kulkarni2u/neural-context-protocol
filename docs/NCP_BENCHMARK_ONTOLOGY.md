# NCP Ontology Benchmark
## Baseline NCP vs NCP + ontology on a dependency-rich corpus

This benchmark checks the design's performance hypotheses for the opt-in
ontology (latency overhead, token use, evidence quality, safety) against
**measured** numbers. All three of the design's targets were stated as
hypotheses, and this page reports which ones held.

## Command

```bash
# SQLite only (no services needed):
python3 benchmarks/ontology/run.py

# SQLite + PostgreSQL (the pgvector store needs Redis and the redis client for whispers):
pip install 'neural-context-protocol[redis]'
NCP_PGVECTOR_DSN=postgresql://postgres:postgres@127.0.0.1:5432/ncp \
NCP_REDIS_URL=redis://127.0.0.1:6379/0 \
python3 benchmarks/ontology/run.py            # --repeats 15 --budget 800 (defaults)

# Sensitivity: a smaller graph slice
python3 benchmarks/ontology/run.py --max-graph-tokens 150
```

The output is a JSON artifact (`ncp.bench.ontology.run_ontology_benchmark`).
Each Postgres arm uses a throwaway schema, dropped afterwards. A full run
takes about 18 s. `tests/test_benchmark_ontology.py` runs a two-task slice in
about 3 s.

## Workload

- **Corpus:** 22 entities (12 services, 4 databases, 2 external APIs, 1
  Kafka topic, 3 teams) and 36 relations (19 `DEPENDS_ON`, 1 `PRODUCES`, 2
  `CONSUMES`, 12 `OWNS`, plus 2 planted hazards). Each relation cites one
  evidence chunk written to NCP memory. The 97 chunks also include 36
  distractors that name the same services (deploy notes, on-call chatter,
  flaky tests), 12 procedure chunks and 12 filler chunks. All chunk
  timestamps are fixed.
- **Hazards (must never surface):** `search -DEPENDS_ON-> payments` is
  asserted as `candidate`. `notifications -DEPENDS_ON-> payments` is
  `verified`, but its only evidence chunk was superseded.
- **Dependency tasks (8):** each asks "what does X depend on", for example
  `task=map_checkout_dependencies slot=checkout
  intent=list_upstream_dependencies_two_hops`. Ground truth is every outgoing
  `DEPENDS_ON`/`PRODUCES` relation within 2 hops of X, with its evidence chunk.
- **Control tasks (6):** procedure lookups that name no entity, such as
  `rotate_tls_certificates` and `log_retention_policy`. Ground truth is that
  topic's two procedure chunks.
- **Arms:** both arms call the same `ncp_get_context` MCP handler with
  unchanged arguments (`pipeline_id`, `max_tokens=800`). Each arm runs over
  its own identically seeded store, so neither arm's retrieval counters can
  affect the other.
  - `baseline` has no `[ontology]` section, which is the shipped default.
  - `ontology` sets `[ontology] enabled = true` and keeps every other default
    (`auto`, `max_hops=2`, `max_graph_tokens=500`, `trigger_max_ms=10`,
    `graph_timeout_ms=50`).

### Metrics

| Metric | Definition |
|---|---|
| latency p50/p95 | Wall-clock time of the handler call. Measured after one warm-up call per task, over 15 rounds (120 dependency and 90 control samples per arm), with tracing off. |
| prompt tokens | `estimate_tokens(context)` (`chars_div4`), measured at `max_tokens=800` |
| fact recall | Share of ground-truth relations covered. A relation counts as covered when its evidence chunk is in context, or when the relation appears in the `[NCP:ONTOLOGY]` block. |
| evidence precision / recall | Over surfaced chunk ids, which are `retrieved_chunk_ids` plus the graph block's `ev:` citations |
| tokens to full recall | Prompt tokens at the smallest budget on the ladder 200…2400 that reaches fact recall 1.0. Unset when no budget on the ladder reaches it. |
| oracle evidence tokens | Tokens needed to show exactly the ground-truth evidence chunks as `[NCP:SUBCONSCIOUS]` lines, i.e. a perfect memory retriever. Computed, not run. |
| unsupported assertion rate | Relations in the graph block that are not verified with live evidence, divided by all relations in the block |
| SQL statements | Statements per call, counted with `sqlite3.Connection.set_trace_callback` on every connection. SQLite only. |
| graph cost | Enricher telemetry: `latency_ms` (trigger plus traversal plus ranking) and `graph_tokens` |

## Measured results

Measured on November 11, 2026: Linux x86_64 container with 4 vCPUs, Python
3.13.18, SQLite 3.53.1, PostgreSQL 16.15 and Redis 7.0.15 on loopback, token
unit `chars_div4`. All quality numbers are deterministic. Latency varies by
roughly ±2 ms between runs on this machine.

### Dependency tasks (8 tasks, default config)

| | SQLite baseline | SQLite + ontology | Postgres baseline | Postgres + ontology |
|---|---:|---:|---:|---:|
| latency p50 (ms) | 7.84 | 16.57 | 8.37 | 33.59 |
| latency p95 (ms) | 9.00 | 20.28 | 13.40 | 48.25 |
| graph `latency_ms` mean | — | 7.74 | — | 23.03 |
| prompt tokens (mean, budget 800) | 137.0 | 537.9 | 124.3 | 525.1 |
| `[NCP:ONTOLOGY]` tokens (mean) | — | 400.0 | — | 400.0 |
| fact recall | 0.377 | **1.000** | 0.011 | **1.000** |
| evidence precision | 0.375 | 0.285 | 0.062 | 0.278 |
| tasks reaching full recall on the ladder | 2 / 8 | 8 / 8 | 0 / 8 | 8 / 8 |
| tokens to full recall (mean, over tasks that reach it) | 142 (2 tasks) | 342 (8 tasks) | — | 330 (8 tasks) |
| SQL statements per call | 124.3 | 198.5 | n/a | n/a |
| unsupported relations surfaced | — | **0 / 111** | — | **0 / 111** |
| triggered / fallbacks | 0 / 0 | 8 / 0 | 0 / 0 | 8 / 0 |

### Control tasks (6 tasks, no entity named)

| | SQLite baseline | SQLite + ontology | Postgres baseline | Postgres + ontology |
|---|---:|---:|---:|---:|
| latency p50 (ms) | 7.63 | 9.12 | 7.41 | 10.45 |
| latency p95 (ms) | 9.25 | 11.39 | 9.21 | 14.86 |
| prompt tokens (mean) | 123.3 | 123.3 | 94.3 | 94.3 |
| evidence recall | 0.417 | 0.417 | 0.000 | 0.000 |
| SQL statements per call | 114.0 | 122.0 | n/a | n/a |
| triggered | 0 | 0 (`no_entity`) | 0 | 0 (`no_entity`) |

On control tasks the context is the same in both arms. The ontology arm
evaluates the trigger, which costs a mean of 1.4 ms (SQLite) or 1.9 ms
(Postgres) and 8 extra SQL statements, then skips. Postgres control recall
is 0.0 in **both** arms: pgvector lexical retrieval without an embedding
adapter does not surface these chunks for these task slugs. The ontology has
no effect on that.

### Sensitivity: `max_graph_tokens = 150`

| Dependency tasks | SQLite + ontology | Postgres + ontology |
|---|---:|---:|
| prompt tokens (mean) | 277.4 | 264.1 |
| `[NCP:ONTOLOGY]` tokens (mean) | 139.5 | 138.9 |
| fact recall | 0.668 | 0.694 |
| evidence precision | 0.365 | 0.366 |
| graph block vs oracle evidence tokens | −22.5% | −22.8% |
| latency p95 overhead (ms) | +10.2 | +34.7 |
| unsupported relations surfaced | 0 / 45 | 0 / 45 |

## Hypotheses vs measurements

| Design hypothesis (§11) | Measured | Verdict |
|---|---|---|
| Graph overhead < 50 ms p95 on a controlled local corpus | p95 delta of **+11.3 ms** on SQLite and **+34.8 ms** on Postgres. The p50 delta was +8.7 ms and +25.2 ms. | **Met** on both backends. Postgres has little headroom: the mean graph `latency_ms` is 23 ms against the default `graph_timeout_ms = 50`. No fallbacks occurred here, but a slower host would see timeouts and therefore degraded (graph-free) context. |
| 15–25% token reduction on dependency-rich tasks | Prompt tokens went **up** by 293% (SQLite) and 323% (Postgres) at a fixed budget. On the 2 tasks where the baseline reaches full recall at all, the ontology arm needed **26.8% more** tokens to reach it. At defaults, the graph block is 2.2× the oracle evidence text. With `max_graph_tokens = 150` it is 22.5% *smaller* than the oracle, but fact recall drops to 0.67. | **Not met.** On this corpus the ontology spends tokens to raise recall; it does not save them. See the analysis below. |
| No meaningful regression on non-graph tasks | Prompt tokens and evidence recall are identical. Latency rose by +2.1 ms p95 (SQLite) and +5.7 ms p95 (Postgres), and SQLite runs +8 SQL statements per call. | **Met** for context quality and tokens. There is a small, measured latency cost from evaluating the trigger. |
| Zero verified-without-evidence assertions | 0 of 111 surfaced relations were unsupported on each backend, and 0 of 45 at the 150-token slice. Neither planted hazard ever surfaced. | **Met** |

### Why tokens went up, not down

- **The baseline under-retrieves rather than over-retrieving.** The baseline
  returns about 135 tokens of memory at an 800-token budget. Its lexical
  ranking matches the slot name, so it picks distractors such as "checkout
  deploy …" over 2-hop evidence that never mentions the seed ("payments
  records every capture in ledger-db"). Raising the budget does not help: 6
  of 8 tasks never reach full recall on SQLite, even at 2400 tokens. The
  15–25% hypothesis assumed a baseline that already pays for the right
  evidence. This one never retrieves it, so the ontology's contribution
  shows up as recall (0.38 → 1.00), not as savings.
- **Traversal answers a broader question than the task asks.** Automatic
  expansion walks both directions and every relation type, so incoming
  edges and `OWNS` edges come back along with the outgoing dependencies.
  That fills the slice: 400 of the 400 available tokens, since
  `min(max_graph_tokens, remaining budget)` here is about 400. It also
  lowers evidence precision (0.285 on SQLite) and makes the block 2.2× the
  oracle text. Restricting automatic expansion to the relation types and
  direction the intent implies, for example outgoing `DEPENDS_ON` for
  "depends on", is the obvious lever. It is not implemented.
- **Compact statements are cheaper per fact.** At a 150-token slice the
  block costs 22.5% less than the oracle evidence text for the facts it
  covers. Ranking then drops the paths beyond the slice, so recall is 0.67.

### Cost accounting

- **SQLite statements:** +74 per enriched call (124 → 198.5). This covers the
  read transaction, entity resolution for candidate terms, the per-hop edge
  query, evidence liveness lookups and entity hydration. The ontology store
  opens its own connection per operation.
- **Postgres:** the graph phase averages 23 ms against SQLite's 7.7 ms.
  Each phase is a `REPEATABLE READ` transaction with several round-trips on
  loopback. Statement counts were not instrumented on Postgres.

## Limits of this benchmark

- The corpus is synthetic and small, with one evidence chunk per relation
  and fixed lexical templates. Absolute numbers will differ on real estates.
- There is no model in the loop. Fact recall measures whether the fact is in
  context, not whether a model uses it correctly.
- Baseline retrieval is lexical: no embedding adapter is configured on
  either backend. An embedding-backed baseline would likely raise baseline
  recall and change the token comparison.
- Latency comes from a single, otherwise idle 4-vCPU container, with
  Postgres and Redis on loopback.
