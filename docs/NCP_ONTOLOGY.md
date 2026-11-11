# NCP Ontology (opt-in domain graph)

The ontology adds an evidence-backed domain graph to NCP. The graph covers
services, databases, APIs, Kafka topics and teams, and the relations between
them. NCP uses it in two ways:

- **Automatic context.** An unchanged `ncp_get_context` call may append an
  `[NCP:ONTOLOGY]` block of verified, evidence-cited paths.
- **Explicit tools.** Hosts can query the graph and inspect the evidence
  behind a relation.

The ontology is **off by default**. Turned off, it is a byte-for-byte no-op:
tool lists, tool responses and context stay the same as before it existed.

The graph is a set of advisory facts. It never authorizes an action, and
harnesses must not treat a path as permission to change anything.

- Tool contract: [`NCP_HTTP_API.md` — Ontology tools](./NCP_HTTP_API.md#ontology-tools-opt-in),
  [Ontology-aware context](./NCP_HTTP_API.md#ontology-aware-context-opt-in),
  [Storage backends](./NCP_HTTP_API.md#ontology-storage-backends)
- Harness examples: [`examples/13_ontology/`](../examples/13_ontology/README.md)
- Benchmark: [`NCP_BENCHMARK_ONTOLOGY.md`](./NCP_BENCHMARK_ONTOLOGY.md)

## Concepts

| Term | Meaning |
|---|---|
| Schema pack | A versioned, immutable set of entity types (with required attributes) and relation types (allowed endpoint types, cardinality, cycles). v1 ships `infrastructure` v1: `Service`, `Database`, `API`, `KafkaTopic`, `Incident`, `Team`; `DEPENDS_ON`, `PRODUCES`, `CONSUMES`, `EXPOSES`, `OWNS`. |
| Entity | `(tenant, namespace, entity_type, canonical_key)` plus aliases and attributes. Ids are deterministic. Attributes are never serialized into automatic context. |
| Relation | A typed edge with `status` (`candidate`, `verified`, `disputed` or `revoked`), `confidence`, a validity window `[valid_from, valid_until)`, `observed_at` and evidence refs. |
| Evidence | Either an NCP memory chunk id (`evidence_type: "chunk"`) or an `external_attestation`. A chunk is live only while it is not superseded or expired, its `valid_from` has passed, and the caller can read it. |
| Namespace | The `pipeline_id`, or `global`. Reads see the namespace plus `global`; writes target exactly one namespace. |

## Enable it

```toml
[ontology]
enabled = true
```

Restart `ncp serve`. MCP hosts that cache tool lists need a refresh before
they show the `ncp_ontology_*` tools. Automatic context works without that
refresh.

### Configuration reference (`[ontology]`)

| Key | Default | Range / values | Meaning |
|---|---|---|---|
| `enabled` | `false` | bool | Master switch. When false, nothing is listed, nothing is created and responses are byte-for-byte legacy. |
| `provider` | `"builtin"` | `builtin`, `disabled` | `disabled` turns the feature off like `enabled = false`. Any other name is a startup error, never a dynamic import. |
| `store` | `"inherit"` | `inherit` | Use the NCP store's own backend (SQLite file, or the pgvector store's schema and prefix). |
| `fail_mode` | `"fallback"` | `fallback`, `closed` | For optional enrichment: `fallback` degrades to ordinary context; `closed` fails the `ncp_get_context` call. |
| `packs` | `["infrastructure"]` | registered pack ids | Packs enabled for writes, queries and automatic context. An unregistered id is a startup error. |
| `retrieval_mode` | `"auto"` | `off`, `auto`, `always` | Automatic enrichment policy (see [Automatic trigger](#automatic-trigger-policy)). |
| `max_hops` | `2` | 0–5 | Traversal depth ceiling. Larger requests are clamped with a `clamped:` warning. |
| `max_nodes` | `30` | 1–100 | Node ceiling per traversal (clamped). |
| `max_edges` | `60` | 1–200 | Edge ceiling per traversal (clamped). |
| `max_graph_tokens` | `500` | 0–4000 | Ceiling (not a target) on the `[NCP:ONTOLOGY]` block. It always comes out of the caller's `max_tokens`; selection usually stops earlier on marginal value. |
| `max_graph_paths` | `16` | 1–100 | Most ranked paths shown per request. Each intent profile has its own cap (dependency/impact 16, ownership/deployment 8, mixed 12), bounded by this value. |
| `min_path_score` | `0.25` | 0.0–1.0 | Selection stops at the first ranked path scoring below this (ranking weights in `ncp/ontology/ranking.py`). |
| `intent_directed` | `true` | bool | Map the request's relationship intent to relation types, direction and hops (see [Intent-directed expansion](#intent-directed-expansion)). `false` walks every type in both directions to `max_hops` (the v1 behaviour). |
| `substitute_evidence` | `true` | bool | Graph-first substitution: a memory chunk cited by a shown fact is not repeated as text (see [Graph-first context](#graph-first-context-block-v2)). |
| `verified_only` | `true` | bool | Default for explicit queries. Automatic context is **always** verified-only. |
| `trigger_max_ms` | `10` | 1–1000 | Time budget for the automatic trigger (entity resolution and decision). |
| `graph_timeout_ms` | `50` | 1–5000 | Time budget for traversal and evidence mapping during enrichment. On Postgres it becomes `SET LOCAL statement_timeout`. |
| `fallback_to_memory` | `true` | bool | With `fail_mode = "fallback"`, return ordinary context on timeout or backend error. When false, those failures fail the call. |
| `unauthenticated_capabilities` | `["read", "write"]` | subset of `read`, `write`, `verify`, `admin` | Capabilities for callers without a principal (stdio, or HTTP without `[principals]`). Granting `verify` or `admin` here gives them to every local caller; do this only on a loopback demo. |

## Tools

All five tools are listed only when `enabled = true` under the `full` tool
profile. The full arguments and result shapes are in
[`NCP_HTTP_API.md`](./NCP_HTTP_API.md#ontology-tools-opt-in).

| Tool | Capability | Purpose |
|---|---|---|
| `ncp_ontology_capabilities` | none | Enabled flag, packs (with types), limits and tool names |
| `ncp_ontology_query` | `read` | Resolve one seed exactly, then traverse with bounds. Ambiguous seeds come back as candidates. |
| `ncp_ontology_explain` | `read` | One relation with all its evidence (liveness) and validity |
| `ncp_ontology_assert` | `write` (`verify` for verified relations) | Upsert an entity, or assert a relation idempotently |
| `ncp_ontology_revoke` | `write` (own relation) or `admin` | Revoke a relation (audited, idempotent) |

Errors come back as `{"ok": false, "error": {"code": ...}}`, where `code` is
one of `conflict`, `forbidden`, `invalid`, `not_found`, `disabled` or
`limit`. An object outside the caller's scope returns `not_found`, exactly
like a missing one. In-process hosts can use the same operations through
`ncp.ontology.facade.OntologyFacade` with an `AuthContext` built by
`auth_context_for(principal, namespace, config=...)`.

## Auth and tenancy model

- **Tenant.** NCP has no tenant dimension yet, so every operation runs in the
  fixed tenant `"default"`. Isolation comes from **namespaces**, i.e.
  pipelines, enforced in every store query.
- **Pipeline vs global.**
  - A pipeline namespace is readable and writable by principals whose
    allowlist includes it, or by any principal without an allowlist.
  - `global` is readable by everyone and writable only with `admin`.
  - Reads always see the caller's pipeline plus `global`.
  - A pipeline outside the allowlist is rejected with `-32003 Forbidden`
    before the tool runs.
- **Capability mapping** (from `[principals]`, never from tool arguments):

  | Principal has | Ontology capabilities |
  |---|---|
  | any principal | `read`; `write` on allowed pipelines |
  | `verify` | `verify`: assert a relation as `verified`, or change a verified one |
  | `admin` | `admin`: write to `global`, revoke anyone's relation, move anyone's validity window, see unredacted attributes; also `write` on `global` |
  | no principal (stdio, or HTTP without `[principals]`) | principal `local` with `unauthenticated_capabilities` |

- **Policy rules:**
  - **B1.** Only the original asserter or an `admin` may change an existing
    relation's `valid_from`/`valid_until` by re-asserting it.
  - **B2.** Future-dated relations, and chunk evidence whose `valid_from` is
    in the future, are inactive. They are skipped by traversal, by
    cardinality checks and by automatic context, but
    `ncp_ontology_explain` still shows them (`in_validity_window: false`).
  - **B3.** The asserter may revoke its own relation, even a verified
    one, without `verify`. Revoking someone else's relation needs `admin`.
  - **B4.** `verified_only: false` on a query is honored only when the
    server's `verified_only` is false or the caller has `verify` or
    `admin`. Otherwise the query is forced back to `true`, with a `forced:`
    warning.
  - **B5.** Query and explain redact relation attributes whose key looks
    like a credential (the key contains, case-insensitively, `password`,
    `passwd`, `passphrase`, `secret`, `token`, `api_key`, `access_key`,
    `private_key`, `credential` or `authorization`) as `[REDACTED]` unless
    the caller has `admin`. Entity attributes are never returned by query
    or explain.
- **Other invariants.**
  - `created_by` is always the authenticated principal.
  - An explicit `relation_id` must already exist in the caller's scope, so
    ids cannot be used to probe or squat other pipelines.
  - Authorization failures never fall back to an unscoped query.

## Automatic trigger policy

Mode precedence:

1. The feature is disabled → `off`.
2. The request passes `"ontology": false` or `{"mode": "off"}` → `off`.
3. An admin passes `{"mode": "auto" | "always"}` → that mode. Non-admins get
   a warning and the override is ignored.
4. Otherwise, `retrieval_mode`.

In `auto`, NCP looks up the n-grams of `task`, `slot` and `intent` (and
literal entity ids) **exactly**, as canonical keys or aliases in the
caller's scope. It expands only when:

- one entity resolves uniquely **and** the request shows relationship
  intent (`depends`, `upstream`, `downstream`, `consumer`, `owner`,
  `impact`, `blast radius`, the pack's relation names, and so on) or
  diagnostic intent (`why`, `lag`, `outage`, `root cause`, `timeout`, and
  so on); **or**
- the entity resolves and two supporting signals agree: its type name
  appears, and a retrieved memory chunk mentions it.

Ambiguous aliases are never guessed (`ambiguous_entity`). A type word on its
own (for example "service") never triggers. `always` expands whenever a seed
resolves, and returns ordinary context on an empty graph.

Expansion is bounded and verified-only. Every relation needs a live,
caller-readable chunk or a valid external attestation. Paths are ranked
deterministically, serialized as compact fact lines without attributes
(block `v:2`, below), and kept within `max_graph_tokens` inside the
caller's budget. The block is shed before memory when the response is cut
to fit, and the conscious block and budget header are never evicted. Each
response's `ontology` metadata reports `schema_version`, `block_version`,
`ontology_triggered`, `reason_code`, `verified_path_count`, `fact_count`,
`intent` (profile name, categories, relation types, direction, hops,
path cap), `substituted_chunk_ids`, `latency_ms`, `graph_tokens`,
`fallback_reason`, `omitted` (including the stop reasons `covered`,
`low_score`, `path_cap`, `token_budget`) and the included paths (by ids,
with each path's score).

### Intent-directed expansion

Once the trigger decides to expand, the same request words pick *what* to
expand. The mapping is deterministic (no LLM) and uses the schema pack's
relation `intents` categories, not hardcoded type names; in
`infrastructure` v1, `DEPENDS_ON`, `PRODUCES` and `CONSUMES` carry
`dependency` and `OWNS` carries `ownership`.

| Request words (examples) | Profile | Relation types | Direction | Hops |
|---|---|---|---|---|
| depends on, dependencies, uses, calls, requires, needs, upstream | `dependency` | `dependency` types | outgoing | `max_hops` |
| impact, blast radius, what breaks, dependents, downstream, consumers, "who/what/which [noun] uses/calls/depends" (unless the word before the verb is the resolved entity: "what checkout depends on" is `dependency`) | `impact` | `dependency` types | incoming | `max_hops` |
| owner, owns, ownership, team, maintainers | `ownership` | `ownership` types | both | 1 |
| deployed, hosted, runs, cluster, region | `deployment` | `deployment` types | both | 1 |
| diagnostic words only (why, slow, outage, lag, …) | `dependency` | `dependency` types | outgoing | `max_hops` |
| none, several categories, or a category with no types in the enabled packs | `mixed` | all | both | `max_hops − 1` (at least 1) |

The `infrastructure` pack has no `deployment` types, so deployment questions
fall back to `mixed`. Both stores filter by relation type and direction in
SQL (`GraphQuery.direction`: `both`, `out`, `in`), and traverse one query
per hop for the whole frontier plus one evidence query per hop.

### Graph-first context (block v:2)

```text
[NCP:ONTOLOGY] v:2 status:verified
svc:checkout -DEPENDS_ON-> svc:payments,svc:cart,db:orders-db ev:ev-dep-03,ev-dep-02,ev-dep-05
svc:checkout -PRODUCES-> topic:order-events ev:ev-prod-00
svc:payments -DEPENDS_ON-> api:card-network-api ev:ev-dep-10+1
svc:billing <-OWNS- team:platform ev:att
```

- Each verified relation of the selected paths is one fact, emitted once.
  Facts with the same anchor (the endpoint nearer the seed), type and
  direction share a line, so shared path prefixes are never repeated.
- `ev:` holds one citation per target, in target order: the chunk shown
  (a chunk already in memory when there is one), `+N` for N more evidence
  refs, `att` for attestation-only support. There is no per-line score.
- Type tags come from the pack's entity types (`Service` → `svc`, `Database`
  → `db`, `KafkaTopic` → `topic`), falling back to the type's initials.
  Keys and ids are folded to a safe character set, so nothing in a label can
  open a line or a `[NCP:…]` header.
- **Marginal-value stopping.** Paths are added in rank order. A path whose
  relations are all shown already adds no new fact and is skipped. Selection
  stops at the first path below `min_path_score` or at the intent's path cap.
  `max_graph_tokens` is only a ceiling.
- **Substitution** (`substitute_evidence`). A memory chunk cited by a shown
  fact loses its `[NCP:SUBCONSCIOUS]` text: the fact stands in for it, and
  its id stays in `ev:`. The freed budget is not refilled; memory keeps its
  usual cap and trim rules. If statements are later shed to fit a tighter
  ceiling (budget refit, `response_max_tokens`), a chunk that loses its last
  citing statement comes back as text, so memory is never lost to keep graph
  data. Only a chunk whose id appears verbatim in `ev:` is substituted: an
  id longer than 64 characters or with characters outside the safe set
  (including `+`, which marks extra refs) is cited folded or truncated, and
  its text stays in memory. The chunk's text can be pulled with `ncp_fetch` using the fact's
  entity names (the chunk id is not a search key), and the relation with
  `ncp_ontology_explain` using `ontology.paths[].relation_ids`.
- **Outcome credit.** A chunk is credited in `retrieved_chunk_ids` when
  memory retrieval selected it *and* its content reached the turn, verbatim
  or as a verified fact it evidences. Substituted chunks are therefore
  credited, exactly as they would be without substitution. Chunks cited
  *only* by the graph (never selected by memory retrieval) are not credited,
  as in v1.

## Backends

- **SQLite:** `ontology_*` tables in the NCP database file, created
  additively on first use. Writers serialize on `BEGIN IMMEDIATE`, and read
  deadlines use a progress handler.
- **PostgreSQL:** `{schema}.{table_prefix}ontology_*` tables in the pgvector
  store's schema, created by a versioned migration under an advisory lock
  the first time the ontology is used. Writes use `READ COMMITTED` with
  advisory locks and a `lock_timeout`. Reads use one `REPEATABLE READ READ
  ONLY` snapshot. Both backends return identical results; this is enforced
  by `tests/test_ontology_parity.py`.
- **Async:** `AsyncPgvectorStore` never assembles context, so the facade
  stays disabled for it. In-process async code can use
  `ncp.ontology.stores.AsyncPostgresOntologyStore`, which runs the sync
  store through `asyncio.to_thread`.
- **Other backends** (memory, Redis-only, and so on): every ontology tool
  answers `disabled`, and context is unchanged.
- **Table creation.** Enrichment never creates tables. Under the `core` tool
  profile the store stays uninitialized (`reason_code:
  "ontology_uninitialized"`) until an ontology tool has run once.

## Failure modes

| Situation | Behavior |
|---|---|
| Feature disabled | Legacy bytes. Ontology tools are `-32601 Tool not found`. |
| Enabled; no entity, ambiguous alias, or entity without intent | No traversal, ordinary context. `reason_code` says why. |
| Empty graph / zero verified paths | Ordinary context, no error |
| Trigger or graph timeout; backend error during enrichment | With `fail_mode = "fallback"` and `fallback_to_memory = true`: ordinary context plus `fallback_reason`. Otherwise the call fails. No graph facts are ever fabricated. |
| Authorization failure | Fails closed (`forbidden` / `-32003`) and is never retried unscoped |
| Evidence superseded, expired or revoked | The relation drops out of verified traversal and automatic context. `explain` shows `evidence_supported: false`. |
| Idempotency key reused with a different payload; revision mismatch | `conflict` |
| Request over a hard bound (more than 100 `relation_types`, more than 32 evidence refs, more than 16 KiB of attributes, more than 64 aliases) | `limit`. Smaller bounds such as hops, nodes and edges are clamped instead. |
| Postgres chunk table missing | Evidence checks fail closed: nothing counts as live. |

## Limits and cost (measured)

Measured on the benchmark corpus with default limits (see
[`NCP_BENCHMARK_ONTOLOGY.md`](./NCP_BENCHMARK_ONTOLOGY.md)):

- Enrichment added +7.4 ms p95 per `ncp_get_context` on SQLite and +15.9 ms
  on Postgres in the v2 run (v1: +11.3 / +34.8 ms). The graph phase now
  averages about 6 ms (SQLite) and 12 ms (Postgres) against the default
  `graph_timeout_ms = 50`.
- Non-graph requests pay about 1.3–2 ms for the trigger; their context is
  unchanged.
- On dependency tasks the `[NCP:ONTOLOGY]` block averages 56 tokens (v1:
  400), fact recall is 1.00 (baseline 0.38) and evidence precision 0.68
  (v1: 0.29).
- Primary token metric, tokens at matched recall: **20.4% fewer** prompt
  tokens than the baseline on SQLite (5 tasks with baseline recall to
  match; 25.3% at full recall on 2 tasks), with no recall loss. On Postgres
  this is not measurable (1 task). The saving is on the low-budget frontier:
  with the PR 6 budget ladder (lowest rung 200) it is 1.0%, and at a fixed
  800-token budget the context is still larger than the baseline's, because
  the baseline omits most facts. Recall counts facts shown only as graph
  statements; answer quality with a model in the loop is not measured.

## Known limitations

- **Fixed tenant.** The tenant is always `"default"`. Pipelines are the only
  isolation boundary.
- **Pack registration deferred.** `ncp_ontology_register_pack` is not
  implemented. Packs are built-in and immutable, and a new version means a
  code change.
- **No automatic extraction.** NCP does not stage candidate relations from
  memory writes asynchronously. Ingestion happens only through explicit
  `ncp_ontology_assert`.
- **Verification.** `verified` status relies on the caller holding
  `verify`. NCP does not check the source deterministically, so keep
  `verify` for trusted ingestion principals.
- **`verified_only` override rules.** A client cannot loosen
  `verified_only` without `verify` or `admin` (B4). Automatic context
  ignores `verified_only` entirely and is always verified-only.
- **Future-dated relations.** These are not counted toward cardinality
  limits until they become active. A planned edge can therefore coexist
  with an active one that would violate `max_outgoing` or `max_incoming`
  once both are active.
- **No point-in-time graph.** `as_of` (point-in-time memory retrieval) is
  not applied to the graph: traversal always uses the current time.
- **NUL characters on Postgres.** Text containing NUL is rejected on
  writes, and lookups by a NUL-containing key match nothing, as on SQLite.
- **Long `table_prefix` on Postgres.** PostgreSQL truncates identifiers
  longer than 63 bytes, and the ontology derives table, index and trigger
  names from `table_prefix`. A very long prefix can be truncated, so keep
  `schema` and `table_prefix` short.
- **Keyword intent.** Intent mapping is keyword-based. A question phrased
  without the listed words falls back to `mixed` (all types, both
  directions, one hop fewer), and "what does X use" vs "who uses X" relies
  on the word order heuristic.
- **Citation ids are not search keys.** A substituted chunk is cited by id,
  but `ncp_fetch` searches content, not ids; fetch by the fact's entity
  names instead.
- **No Deployment type.** The `infrastructure` v1 pack has no Deployment
  entity. Store environment and version as Service attributes or in
  evidence chunks.

## Rollout and disable guidance

1. **Disabled (default).** Ship the code with no `[ontology]` section. This
   is a byte-for-byte no-op and creates no tables.
2. **Read-only pilot.** Set `enabled = true`, `retrieval_mode = "off"`, and
   keep the default `unauthenticated_capabilities`. Explicit tools only.
   Ingest the graph with one `verify` principal, as in
   `examples/13_ontology/seed_graph.py`.
3. **Automatic enrichment.** Switch to `retrieval_mode = "auto"`. Watch the
   `ontology` metadata (`reason_code`, `fallback_reason`, `latency_ms`,
   `graph_tokens`), and tune `max_graph_tokens`, `graph_timeout_ms` and
   `max_hops` against your own budget.
4. **Default-on.** Only after evaluating the change on your own tasks.

To **disable**:

- Set `enabled = false` (or `provider = "disabled"`) and restart. Tables and
  data stay in place, and responses return to legacy bytes.
- For one request, pass `"ontology": false` to `ncp_get_context`.

To **remove the data**:

- SQLite: drop the `ontology_*` tables.
- Postgres: drop the `{schema}.{table_prefix}ontology_*` tables.

There is no `ncp` command for removing the data.
