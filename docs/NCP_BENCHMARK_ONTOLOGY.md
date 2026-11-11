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

# Ablations: any extra [ontology] line for the ontology arm
python3 benchmarks/ontology/run.py --extra-toml 'intent_directed = false'
python3 benchmarks/ontology/run.py --extra-toml 'substitute_evidence = false'
```

For the Postgres arm a throwaway Redis is enough, for example
`redis-server --port 6391 --save "" --daemonize yes` with
`NCP_REDIS_URL=redis://127.0.0.1:6391/0`, shut down afterwards.

The output is a JSON artifact (`ncp.bench.ontology.run_ontology_benchmark`).
Each Postgres arm uses a throwaway schema, dropped afterwards. A full run
takes about 15 s. `tests/test_benchmark_ontology.py` runs a two-task slice in
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
| fact recall | Share of ground-truth relations covered. A relation counts as covered when its evidence chunk is in context, or when the relation appears in the `[NCP:ONTOLOGY]` block. A fact shown **only as a graph statement** (verified, with its evidence chunk id in `ev:` but no evidence text) counts as recalled. This is the design's notion of recall (a verified, cited, fetchable fact); whether a model answers as well from the statement as from the prose is not measured. |
| evidence precision / recall | Over surfaced chunk ids, which are `retrieved_chunk_ids` plus the graph block's `ev:` citations |
| tokens to full recall | Prompt tokens at the smallest budget on the ladder that reaches fact recall 1.0. Unset when no budget on the ladder reaches it. The v2 ladder is 100, 150, 200, 300, 400, 600, 800, 1200, 1600, 2400 (v1 started at 200); both arms run the same ladder. |
| **tokens at matched recall** (primary) | Per dependency task and target recall R, each arm is charged its cheapest ladder rung whose fact recall ≥ R. R is the baseline's best recall anywhere on the ladder ("R = baseline max"; tasks where that is 0 are skipped) or 1.0. Only tasks both arms reach count. Reported as the pooled reduction `1 − Σ ontology / Σ baseline`: **positive = the ontology arm needs fewer tokens**, negative = it needs more (−5.9% means 5.9% more tokens). The hypothesis is only evaluated with at least max(3, half the tasks) pairs (4 of 8 here); otherwise it is reported as not measurable. The R = 1.0 figure has no pair minimum and is reported for information only. |
| recall guard | Flags a config that saves tokens by losing recall: a task where the ontology arm has lower fact recall than the baseline at the fixed budget while using fewer tokens, or a lower mean recall with fewer mean tokens. |
| oracle evidence tokens | Tokens needed to show exactly the ground-truth evidence chunks as `[NCP:SUBCONSCIOUS]` lines, i.e. a perfect memory retriever. Computed, not run. |
| unsupported assertion rate | Relations in the graph block that are not verified with live evidence, divided by all relations in the block. Separately, every path's `ev:` citations are checked: each relation on the path must cite its own live evidence chunk, and no superseded or unrelated chunk may be cited (`unsupported_citations`). |
| SQL statements | Statements per call, counted with `sqlite3.Connection.set_trace_callback` on every connection. SQLite only. |
| graph cost | Enricher telemetry: `latency_ms` (trigger plus traversal plus ranking) and `graph_tokens` |

## Why tokens at matched recall is the primary metric

A fixed-budget token count compares two contexts that answer different
amounts of the question. On this corpus the baseline returns about 137
tokens at an 800-token budget because it *under*-retrieves (fact recall
0.38); any arm that adds the missing facts uses more tokens, and any arm that
drops facts uses fewer. "Fewer tokens at a fixed budget" therefore rewards
lost recall. The design hypothesis is about saving tokens for the *same*
answer quality, so the headline compares the cheapest context each arm needs
to reach the same fact recall, on tasks both arms reach, and the recall guard
reports any configuration that gets cheaper by getting worse. The
fixed-budget table is kept for completeness.

An embedding-backed baseline arm was not run: NCP ships no deterministic
offline embedder (`LocalEmbeddingAdapter` needs the optional `fastembed`
extra and a model download, `OpenAIEmbeddingAdapter` needs the network, and a
hash or bag-of-words stand-in would just be another lexical baseline). The
artifact records this under `embedding_baseline.skipped`.

## Measured results

Measured on November 11, 2026: Linux x86_64 container with 4 vCPUs, Python
3.13.18, SQLite 3.53.1, PostgreSQL 16.15 and Redis 7.0.15 on loopback, token
unit `chars_div4`, 15 timed rounds. All quality numbers are deterministic and
reproduce exactly on a re-run; read latency as ±5 ms.

**v1** is the shipped PR 6 behaviour (commit 82e4fb6): every relation type in
both directions, per-path `path:N score:… Type:key -REL-> Type:key` lines,
the slice filled up to `max_graph_tokens`. **v2** is this change:
intent-directed traversal, marginal-value stopping, the fact-grouped `v:2`
block and graph-first substitution, all at default config. The v1 rows under
the new metrics were measured by running the v2 benchmark harness against
the v1 code.

### Headline: tokens at matched recall (dependency tasks, default ladder)

Sign convention for every "reduction" on this page: `1 − ontology / baseline`,
so **positive = fewer tokens than the baseline**, negative = more.

| | SQLite v1 | SQLite v2 | Postgres v1 | Postgres v2 |
|---|---:|---:|---:|---:|
| R = baseline max: pairs (of 8) | 5 | 5 | 1 | 1 |
| baseline tokens (mean) | 106.0 | 106.0 | 132.0 | 132.0 |
| ontology tokens (mean) | 112.2 | **84.4** | 98.0 | 88.0 |
| **reduction** (positive = fewer tokens) | −5.9% | **+20.4%** | (+25.8%, 1 pair) | (+33.3%, 1 pair) |
| R = 1.0: pairs (below the 4-pair minimum) | 2 | 2 | 0 | 0 |
| R = 1.0: reduction (2 tasks, informational) | −35.4% | +25.3% | — | — |
| recall guard (tokens cut by losing recall) | no | no | no | no |

SQLite v2 per task at R = baseline max (baseline → ontology tokens):
checkout 138 → 97 (R 0.18), shipping 98 → 93 (R 0.33), pricing 96 → 84
(R 0.5), fraud-check 100 → 74 (R 1.0), inventory 98 → 74 (R 1.0). The other
three tasks have baseline recall 0 everywhere on the ladder, so there is
nothing to match.

Ladder sensitivity (SQLite, reduction at R = baseline max / R = 1.0; both
arms always run the same ladder):

| Ladder | v1 | v2 |
|---|---:|---:|
| PR 6 ladder (200, 300, … 2400) | −30.4% / −26.8% | **+1.0%** / +14.1% |
| default (100, 150, 200, … 2400) | −5.9% / −35.4% | **+20.4%** / +25.3% |
| finer (60, 70, …, 170, 175, 200, … 2400) | +2.1% / −23.7% | +29.4% / +25.8% |
| dense (50, 55, …, 395, 400, … 2400) | +1.9% / −23.7% | +30.6% / +25.8% |

The result exists only on the low-budget frontier. With the PR 6 ladder the
cheapest rung is 200, where the baseline is already saturated (about 138
tokens) and the ontology arm spends the freed budget on more memory, so v2
saves only 1.0%. Finer ladders converge on v2 ≈ +30% and v1 ≈ +2%, so the
ladder change measures the frontier more precisely rather than favouring an
arm (v1 does not reach 15% on any ladder). The default ladder is the
reported number.

### Dependency tasks (8 tasks, default config, fixed budget 800)

| | SQLite baseline | SQLite v1 | SQLite v2 | Postgres baseline | Postgres v1 | Postgres v2 |
|---|---:|---:|---:|---:|---:|---:|
| latency p50 (ms) | 6.92 | 16.57 | 13.21 | 7.90 | 33.59 | 21.89 |
| latency p95 (ms) | 7.69 | 20.28 | 15.04 | 9.47 | 48.25 | 25.33 |
| p95 overhead vs baseline (ms) | — | +11.3 | **+7.4** | — | +34.8 | **+15.9** |
| graph `latency_ms` mean | — | 7.74 | 6.11 | — | 23.03 | **11.82** |
| prompt tokens (mean) | 137.0 | 537.9 | **161.8** | 124.3 | 525.1 | **172.8** |
| `[NCP:ONTOLOGY]` tokens (mean) | — | 400.0 | **56.0** | — | 400.0 | **56.0** |
| graph block vs oracle evidence tokens (≈180) | — | +122% | **−68.9%** | — | +122% | **−68.9%** |
| fact recall | 0.377 | 1.000 | **1.000** | 0.011 | 1.000 | **1.000** |
| evidence precision | 0.375 | 0.285 | **0.681** | 0.062 | 0.278 | **0.716** |
| tasks reaching full recall on the ladder | 2 / 8 | 8 / 8 | 8 / 8 | 0 / 8 | 8 / 8 | 8 / 8 |
| tokens to full recall (mean, all 8 tasks) | — | 342 | **112.5** | — | 330 | **110.6** |
| SQL statements per call | 124.3 | 198.5 | **160.8** | n/a | n/a | n/a |
| unsupported relations surfaced | — | 0 / 111 | **0 / 36** | — | 0 / 111 | **0 / 36** |
| unsupported `ev:` citations | — | 0 | **0** | — | 0 | **0** |
| triggered / fallbacks | 0 / 0 | 8 / 0 | 8 / 0 | 0 / 0 | 8 / 0 | 8 / 0 |

(v1 latency/token columns are the published PR 6 run; v1 tokens to full
recall used the old ladder starting at 200.) At a fixed budget v2 still uses
more tokens than the baseline (18% more on SQLite, 39% more on Postgres;
the artifact's `dependency_fixed_budget_token_reduction` is −0.18), because the
baseline's 137 tokens carry only 38% (SQLite) or 1% (Postgres) of the facts;
v2 carries all of them for +25 / +48 tokens.

### Control tasks (6 tasks, no entity named)

| | SQLite baseline | SQLite v2 | Postgres baseline | Postgres v2 |
|---|---:|---:|---:|---:|
| latency p95 (ms) | 7.87 | 10.33 | 9.12 | 14.62 |
| prompt tokens (mean) | 123.3 | 123.3 | 94.3 | 94.3 |
| evidence recall | 0.417 | 0.417 | 0.000 | 0.000 |
| SQL statements per call | 114.0 | 122.0 | n/a | n/a |
| triggered | 0 | 0 (`no_entity`) | 0 | 0 (`no_entity`) |

Context is byte-identical in both arms. The trigger costs about 1.3 ms
(SQLite) and 1.9 ms (Postgres) mean, as in v1. Postgres control recall is 0.0
in both arms (pgvector lexical retrieval without an embedding adapter does
not surface these chunks); the ontology has no effect on that.

### Where the savings come from (SQLite ablations, default otherwise)

| Ontology arm config | prompt tokens (fixed 800) | graph tokens | evidence precision | matched-recall reduction (R = baseline max / 1.0) |
|---|---:|---:|---:|---:|
| v2 default | 161.8 | 56.0 | 0.681 | +20.4% / +25.3% |
| `intent_directed = false` | 254.0 | 180.4 | 0.297 | +2.1% / −21.2% |
| `substitute_evidence = false` | 194.0 | 56.0 | 0.681 | +20.4% / +25.3% |
| `max_graph_tokens = 150` | 161.8 | 56.0 | 0.681 | +20.4% / +25.3% |
| v1 (for reference) | 537.9 | 400.0 | 0.285 | −5.9% / −35.4% |

- **Intent-directed traversal** is the largest lever: "depends on" now walks
  outgoing `DEPENDS_ON`/`PRODUCES`/`CONSUMES` only, so incoming and `OWNS`
  edges no longer fill the block. With it off, the compact format alone
  takes the block from 400 to 180 tokens, which is not enough to beat the
  baseline at matched recall.
- **The v2 format** (grouping, no scores, short tags, one citation per fact)
  and **marginal-value stopping** make the block 56 tokens for a mean of 4.5
  facts, 69% below the oracle evidence text for the same facts.
  `max_graph_tokens = 150` changes nothing, because selection stops on value
  long before the 500-token ceiling.
- **Substitution** cuts the fixed-budget context by 17% (194 → 162 tokens):
  6 cited memory chunks across the 8 tasks are shown only as facts. It does
  not move the matched-recall numbers, because on the cheapest rungs memory
  has no room anyway.
- `min_path_score = 0.25` never binds on this corpus (path scores are
  0.50–0.72); it is a safety floor, not a tuned value.

### Cost accounting

- **SQLite statements:** +36.5 per enriched call (124.3 → 160.8), down from
  +74 in v1. Traversal now issues one edge query and one evidence query per
  hop for the whole frontier, instead of one edge query per node and one
  evidence query per relation, and `get_entities` loads all endpoints and
  their aliases in two queries instead of two per entity. A unit test pins
  the traversal statement count as independent of frontier width.
- **Postgres:** fewer round-trips halve the graph phase (23.0 → 11.8 ms mean)
  and the p95 overhead (+34.8 → +15.9 ms), leaving headroom under
  `graph_timeout_ms = 50`. Statement counts are not instrumented on Postgres.

## Hypotheses vs measurements

| Design hypothesis (§11) | v1 | v2 | Verdict (v2) |
|---|---|---|---|
| Graph overhead < 50 ms p95 on a controlled local corpus | +11.3 ms SQLite, +34.8 ms Postgres | **+7.4 ms** SQLite, **+15.9 ms** Postgres | **Met** on both backends, with more Postgres headroom. |
| 15–25% token reduction on dependency-rich tasks | Not met (3.9× the baseline's tokens at a fixed budget; −5.9% reduction, i.e. 5.9% more tokens, at matched recall) | **+20.4%** reduction at matched recall on SQLite (5 of 8 tasks); +25.3% at full recall on 2 tasks (below the pair minimum) | **Met on SQLite**, on the primary metric, with no recall loss, on the low-budget frontier only: with the PR 6 ladder (lowest rung 200) it is +1.0%, and at the default `max_tokens = 800` v2 uses 18% more tokens than the baseline. **Not measurable on Postgres**: the lexical pgvector baseline reaches non-zero recall on only 1 of 8 tasks (+33.3% there), below the 3-pair minimum. At a fixed budget tokens still rise (+18% / +39%) because the baseline is missing most facts. |
| No meaningful regression on non-graph tasks | Met | Context identical, +2.5 ms (SQLite) / +5.5 ms (Postgres) p95, +8 SQL statements | **Met** for context quality and tokens; a small measured trigger cost remains. |
| Zero verified-without-evidence assertions | 0 / 111 | **0 / 36**, 0 unsupported `ev:` citations, neither planted hazard surfaced | **Met** |

### Caveats on the token result

- The SQLite headline rests on 5 tasks, and 3 of the 8 dependency tasks have
  no baseline recall to match at all. The result is real for this corpus but
  small-sample.
- It depends on the ladder's granularity (1.0% with the PR 6 ladder, 20.4%
  default, 29.4–30.6% with finer low rungs), because each arm is charged a
  whole rung. Both arms always run the same ladder.
- Recall counts graph statements as recalled facts. A substituted evidence
  chunk may say more than the one relation it backs; that extra content is
  not shown, and its loss is not measured here.
- The cheapest matching ontology contexts are mostly "header + graph block,
  no memory": a model gets verified facts with chunk ids, not the evidence
  prose. Whether that is enough for a given task is not measured here (no
  model in the loop).

## Limits of this benchmark

- The corpus is synthetic and small, with one evidence chunk per relation
  and fixed lexical templates. Absolute numbers will differ on real estates.
- There is no model in the loop. Fact recall measures whether the fact is in
  context, not whether a model uses it correctly.
- Baseline retrieval is lexical: no embedding adapter is configured on
  either backend, and no deterministic offline embedder exists to add one.
  An embedding-backed baseline would likely raise baseline recall and change
  the token comparison (and make the Postgres comparison measurable).
- Latency comes from a single, otherwise idle 4-vCPU container, with
  Postgres and Redis on loopback.
