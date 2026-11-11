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
| Schema pack | A versioned, immutable set of entity types (with required attributes) and relation types (allowed endpoint types, cardinality, cycles). `infrastructure` v1 (the default): `Service`, `Database`, `API`, `KafkaTopic`, `Incident`, `Team`; `DEPENDS_ON`, `PRODUCES`, `CONSUMES`, `EXPOSES`, `OWNS`. `infrastructure` v2 (opt-in, a superset; see [Schema pack v2](#schema-pack-v2-repos-modules-tables)) adds `Repo`, `Module`, `Table` and `CONTAINS`, `BUILDS`, `READS`, `WRITES`. |
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
| `pack_versions` | `{}` | pack id → registered version | Version used by writes that name no `schema_version`, e.g. `{ infrastructure = 2 }`. Unpinned packs stay on their stable default (`infrastructure` → 1), so new built-in versions never apply silently. An unregistered pin is a startup error. |
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
| `promotion.auto` | `false` | bool | `ncp ontology seed` writes candidates that meet the rules below as verified (see [Promoting candidates](#promoting-candidates)). |
| `promotion.min_confidence` | `0.6` | 0.0–1.0 | Candidates below this are held back. |
| `promotion.require_resolved` | `true` | bool | Hold back `unresolved:*` / `pattern:*` destinations. |
| `promotion.require_counterpart` | `false` | bool | PRODUCES needs a CONSUMES on the same destination (and vice versa), in the same manifest or store. |
| `promotion.types` | `["PRODUCES", "CONSUMES", "ROUTES_TO"]` | relation types | Only these types are auto-promoted. |
| `promotion.verified_confidence` | `0.9` | 0.0–1.0 | Promoted relations get at least this confidence. |
| `trigger_max_ms` | `10` | 1–1000 | Time budget for the automatic trigger (entity resolution and decision). |
| `graph_timeout_ms` | `50` | 1–5000 | Time budget for traversal and evidence mapping during enrichment. On Postgres it becomes `SET LOCAL statement_timeout`. |
| `fallback_to_memory` | `true` | bool | With `fail_mode = "fallback"`, return ordinary context on timeout or backend error. When false, those failures fail the call. |
| `unauthenticated_capabilities` | `["read", "write"]` | subset of `read`, `write`, `verify`, `admin` | Capabilities for callers without a principal (stdio, or HTTP without `[principals]`). Granting `verify` or `admin` here gives them to every local caller; do this only on a loopback demo. |

## Seeding the graph

NCP does not extract graph facts from memory writes, so a new ontology is
empty. Populate it from authoritative sources (entity registry, service
catalog, build files, CODEOWNERS, deploy manifests) with two commands:

- `ncp ontology scan` reads a repository and **writes a manifest**. It never
  touches the store.
- `ncp ontology seed` **loads one or more manifests** into the store,
  idempotently, and keeps the graph in sync with them.

```bash
ncp ontology scan . -o .ncp/ontology.json --pipeline-id shop   # review the file
ncp ontology seed .ncp/ontology.json --dry-run                 # show what would change
ncp ontology seed .ncp/ontology.json --prune                   # apply and sync
```

Run both from the job that changes the sources (CI on merge, or a cron), so
the graph follows the repo.

### Manifest format

A manifest is `.json`, `.toml`, or `.yaml`/`.yml` (YAML needs PyYAML):

```toml
pipeline = "shop"                  # target namespace (or --pipeline-id)
source = "entity_registry"         # asserting principal seed:<source>; the prune scope
observed_at = "2026-11-01T00:00:00Z"   # optional; defaults to first-seen time
# schema_version = 2               # optional; otherwise [ontology].pack_versions, else v1

[[entities]]
type = "Service"                   # must exist in the schema pack version used
key = "order-api"
aliases = ["orders-api"]
attributes = { environment = "prod" }   # `name` defaults to the key

[[entities]]
type = "KafkaTopic"
key = "order.events"
attributes = { cluster = "main" }

[[relations]]
src = "order-api"                  # a key, or "Type:key" when a key is ambiguous
type = "PRODUCES"
dst = "KafkaTopic:order.events"
evidence = "order-api publishes OrderPlaced to order.events (registry row 12)."
# attestation = "registry.yaml#L12"     # and/or an external reference
# revision = "a1b2c3"              # attestation source revision (default "1")
# status = "candidate"             # default "verified"
# confidence = 0.95
```

### How `seed` works

- **Validated up front, all-or-nothing.** Entity types, required attributes,
  relation triples (`Service -PRODUCES-> KafkaTopic`), endpoints and evidence
  are checked against the schema pack before anything is written. Errors name
  the file and item (`checkout.toml relations[2].dst: ...`).
- **Endpoints.** A relation endpoint is matched against the manifests'
  entities first, then against **entities already in the store** (the target
  namespace, then `global`). A manifest can therefore point at
  `Service:payments` without redeclaring it. Referenced entities are never
  written. A relation's source must be in the target namespace; its target
  may be `global`. A bare key that matches several existing types must be
  written as `Type:key`.
- **Every relation cites evidence.** `evidence` text is written as an
  ordinary memory chunk (`semantic`, `tool_result`, in the target pipeline)
  and cited as `chunk` evidence; `attestation` is cited as
  `external_attestation`. A `verified` relation needs at least one.
- **Idempotent, diff-based.** Ids derive from the namespace, the `source`
  and the triple. Re-running unchanged manifests writes nothing; a changed
  relation is updated in place (revision + 1). Two sources asserting the same
  edge own separate relations.
- **Staying in sync.** Relations this `source` seeded earlier that the
  manifests no longer list are reported as *stale*. With `--prune` they are
  closed (`valid_until` = now): they leave traversal and context at once, keep
  their history, and reopen if they come back. Relations of other sources, and
  those agents asserted over MCP, are never touched. Entities are not pruned.
- **Trust.** `seed` runs in-process with direct store access, so it asserts
  as a trusted operator with `verify` (and `admin` for `global`). `verified`
  means "this manifest said so": seed only from sources you would trust
  unreviewed and use `--status candidate` for anything else. To seed a remote
  `ncp serve` instead, call `ncp_ontology_assert` with a `verify` principal,
  as in `examples/13_ontology/seed_graph.py`.

### Several manifests in one run (monorepos)

```bash
ncp ontology seed modules/*/ontology.toml --source monorepo --prune
```

Several manifests are **merged into one run under one `source`**, so:

- cross-manifest references resolve (`checkout` can depend on `payments`
  declared in another module's file);
- `--prune` sees all of them together, so an edge that moves between modules
  is not pruned;
- each manifest's `observed_at` applies to its own relations;
- `pipeline`, `source`, `schema_pack` and `schema_version` must agree where
  set (`--pipeline-id` / `--source` override);
- an entity declared in several files must be declared identically, and an
  identical relation is kept once. Anything else that collides is an error
  naming both files.

Seeding modules **separately** under one shared `source` with `--prune`
would make each run close the others' edges. Either seed them together
(above) or give each module its own `source`.

### Scanning a repository

`ncp ontology scan [ROOT] -o FILE` is a reference exporter for common
layouts. It lists files with `git ls-files` (so `.gitignore` applies), or a
filtered directory walk outside git, and always skips `node_modules`,
`vendor`, `build`, `dist`, `target` and virtualenvs.

| Reads | Produces | Status |
|---|---|---|
| `package.json`, `pyproject.toml`, `go.mod`, `Cargo.toml`, `pom.xml`, `build.gradle(.kts)` | one `Module` per directory (key = repo path, aliases = package names), `Repo CONTAINS Module`, and in-repo dependencies as `Module DEPENDS_ON Module`. Workspace roots, Maven aggregators and Gradle root projects are not modules. | verified |
| `Chart.yaml`, k8s `Deployment`/`StatefulSet`/`DaemonSet`, `Dockerfile` | a `Service` per module (named by the chart, then the workload, then the directory) and `Module BUILDS Service` | verified |
| `CODEOWNERS` (root, `.github/`, `docs/`, `.gitlab/`; last match wins) | `Team OWNS Module` and `Team OWNS Service` | verified |
| Kafka clients: Spring `@KafkaListener` / `KafkaTemplate.send` / `ProducerRecord`, kafka-python / confluent / aiokafka, kafkajs, kafka-go / sarama, Spring Cloud Stream bindings | `PRODUCES` / `CONSUMES` `KafkaTopic` (literals, Spring `${...}` placeholders and string constants are resolved, see below; test files skipped unless `--include-tests`) | candidate (`--heuristics verified\|off`) |
| `ncp-ontology.{toml,json,yaml,yml}` anywhere | hand-written facts (databases, tables, external APIs) merged in; they win over a scanned entity with the same type and key | as written |

Every scanned relation cites `attestation = "path#Lnn"`. Options:
`--pipeline-id`, `--source` (default: the directory name), `--repo-name`,
`--environment` (service attribute, default `prod`), `--kafka-cluster`
(default `main`), `--schema-version 1|2` (default 2) and `--heuristics`.

With `--schema-version 1` the scan emits only v1 types: modules and repos
are dropped, module dependencies are lifted to `Service DEPENDS_ON Service`
where both modules build a service, and Kafka edges and ownership attach to
services.

The scan is static: it follows no code paths, and CODEOWNERS matching
approximates GitHub's gitignore-style rules. Review the manifest before
seeding it as verified. A repo with its own conventions can skip `scan` and
emit a manifest from its own script instead.

#### Spring topics: placeholders and constants

In Java, Kotlin and Scala sources the topic argument of `@KafkaListener`
(`topics`, `topicPattern`), `<kafkaTemplate>.send(...)` and
`new ProducerRecord<>(...)` may be a literal, a `"${key}"` /
`"${key:default}"` placeholder, a constant (`NAME` or `Class.NAME`, from
`static final String`, Kotlin `const val`, or `val` in an `object` /
`companion object`), a `@Value("${key}")` field of the same file, or a `+`
concatenation of those. Placeholders are looked up in
`application*.properties|yml|yaml` and `bootstrap*` files (scalars only,
nested YAML flattened to dotted keys; test paths skipped unless
`--include-tests`), first in the owning module and then repo-wide (a
repo-wide match must be unambiguous). `application.*` wins over
`application-<profile>.*`; if only profile files define a key, the
alphabetically first profile is used and recorded as the topic's `profile`
attribute. Values nest (up to 4 levels, cycle-guarded) and comma-separated
values expand to several topics.

Resolved topics carry `resolved_from` = `literal`, `property:<key>`,
`default:<key>` (the `${key:default}` default was used) or `constant:<NAME>`.
A topic that cannot be resolved (unknown property, SpEL `#{...}`, a local
variable, a constant defined elsewhere) becomes a `KafkaTopic` keyed
`unresolved:<expression>` (e.g. `unresolved:messaging.orders-topic`)
with attributes `unresolved = true` and `expression`; its edge is always
`candidate`, even with `--heuristics verified`. A non-plain `topicPattern`
becomes `pattern:<regex>` with a `pattern` attribute. Expressions the scan
does not understand (method calls, ...) produce no topic and show up as
unmatched in the coverage report.

#### Coverage report and warnings

`summary["kafka"]` (and a table printed by `ncp ontology scan`; `--json`
prints the whole summary as JSON, with the manifest under `manifest` unless
`-o` is given) counts what the scan saw: `files_examined`,
`kafka_like_sites` (call sites: each `@KafkaListener`, each Kafka
`.send(...)` and each `ProducerRecord(...)` occurrence, plus any other line
that looks like a client call, e.g. `subscribe`), `matched_sites` (sites that
yielded at least one resolved topic; `resolved_via_property` /
`resolved_via_constant` break these down), `unresolved_placeholders` (number
of `unresolved:*` topic elements emitted; a matched site can contribute, e.g.
`topics = {Topics.A, "${missing}"}`), `unmatched_sites` (sites that yielded
nothing at all), `skipped_no_owner` (files with Kafka usage that no module,
or with `--schema-version 1` no service, owns, so they are not attributed;
their sites count as unmatched) and up to 5 `unmatched_examples` as
`path:line: snippet`. A site that yielded only unresolved topics is neither
matched nor unmatched, so `matched_sites + unmatched_sites <=
kafka_like_sites`. The scan
warns when Kafka-like sites exist but no topic was extracted, when more than
half of the sites are unmatched, and when placeholders stayed unresolved,
naming the likely cause.

#### No store needed

`scan` only reads files: it opens no store or database and builds no
embedding adapter, so it works with the ontology disabled. So does
`ncp ontology seed --dry-run` while `[ontology]` is disabled: it validates
the manifest against the built-in schema in a throw-away in-memory database
(no project store, no embedding adapter) and reports every entity and
relation as new; references to entities that already exist in a store are not
checked. A real seed, or a dry run with the ontology enabled (which diffs
against the store), is unchanged.

### Reviewing a scan: the ontology wiki

A manifest is hard to verify as JSON. `ncp ontology wiki` renders it as linked
markdown pages (files only, no store needed, deterministic output):

```bash
ncp ontology scan . -o scan.json --summary-out scan-summary.json
ncp ontology wiki scan.json --summary scan-summary.json \
    --compare previous-scan.json --out .ncp/ontology-wiki
```

- `index.md`: totals, warnings, what changed, links to every page.
- `services/`, `modules/`: per service, what it produces and consumes (status,
  confidence, `resolved_from`, `via_module`, evidence `path#L12`), dependencies.
- `destinations/`: one page per topic, queue or exchange, with producers and
  consumers side by side. Flags: `⚠ no consumer in repo`, `⚠ no producer in
  repo`, `⚠ unresolved name`, `pattern subscription`; `pattern:` subscriptions
  that match the name are listed as potential consumers.
- `messaging.md` (matrix, problems first), `coverage.md` (per-broker scan
  coverage and every unresolved value), `compare.md` (only with `--compare`:
  added/removed entities and relations, status/confidence changes, destinations
  whose producers or consumers changed).
- `candidates.md`: a checklist of candidate relations grouped by service.

```markdown
## Producers vs consumers

| Producers | Consumers |
| --- | --- |
| [Service:billing](../services/billing.md) (candidate, 0.60, billing/K.java#L12) | [Service:shop](../services/shop.md) (candidate, 0.60, shop/L.java#L40) |
```

Workflow: scan, build the wiki, read the `compare.md` and the flagged
destinations, tick (`[x]`) the lines in `candidates.md` you verified, then
`ncp ontology promote --from-checklist candidates.md`. Checklist lines look like
``- [ ] `Service:billing -PRODUCES-> KafkaTopic:orders.v1` — conf 0.60 · `billing/K.java#L12` ``;
do not edit the text inside the first backticks.

### Promoting candidates

`ncp ontology scan` emits heuristic messaging edges as `candidate` relations
(Kafka edges 0.6, `unresolved:*` / `pattern:*` destinations 0.4). **Context
assembly only follows verified relations** (automatic enrichment is always
verified-only, and `[ontology] verified_only = false` does not change that), so
a candidate never reaches a prompt until it is promoted. Workflow:

1. `ncp ontology scan . -o manifest.json`, then `ncp ontology seed manifest.json`
   (candidates are stored, not used).
2. Review: `ncp ontology promote --pipeline-id shop --list` (id, confidence,
   evidence), or tick lines in the generated wiki checklist.
3. Promote what you checked, or let rules do it.

```bash
ncp ontology promote --pipeline-id shop --list [--json]
ncp ontology promote --pipeline-id shop <relation-id>...             # by id (or 8+ char prefix)
ncp ontology promote --pipeline-id shop --match 'Service:order-* -PRODUCES-> *' --min-confidence 0.6
ncp ontology promote --pipeline-id shop --type PRODUCES --type CONSUMES
ncp ontology promote --pipeline-id shop --from-checklist review.md   # ticked '- [x] `src -TYPE-> dst`' lines
ncp ontology promote --pipeline-id shop --auto --dry-run             # apply [ontology.promotion] rules
ncp ontology promote --pipeline-id shop --all --include-unresolved
ncp ontology demote  --pipeline-id shop <relation-id>... --reason "topic renamed"
```

Selectors combine with AND; `--all` takes every candidate. Refs are
`Type:key` as in manifests (`--match` is an `fnmatch` glob against
`"<src> -<TYPE>-> <dst>"`). Destinations whose key starts with `unresolved:` or
`pattern:` are held back (and counted in the output) unless
`--include-unresolved` is passed. `--dry-run` writes nothing. The exit code is 1
when nothing was selected, unless `--allow-empty`.

Promotion re-asserts the same relation as `verified`, keeping its evidence
and attestation (a candidate with none gets `promoted:cli`), raising
confidence to `max(confidence, verified_confidence)` and recording
`promoted_by` (`cli` or `auto:<rule>`) and `promoted_at` attributes. The CLI
acts as the local operator `cli:promote` with `read`/`write`/`verify` in the
`--pipeline-id` namespace (plus `admin` for `global`), the same trust model as
`seed`; nothing is checked against `[principals]`. Over MCP use
`ncp_ontology_assert` with a principal holding `verify`. `demote` re-asserts
verified relations as `candidate` (evidence kept, `demote_reason` recorded), so
they leave context immediately; use the revoke tool to withdraw one entirely.

A later `ncp ontology seed` of a manifest that still lists the relation as
`candidate` writes it back to candidate (the seeder owns what it asserted).
Use `--auto-promote`, or edit the manifest's `status`, to keep it verified.

**Auto-promote.** With `[ontology.promotion] auto = true` (or
`ncp ontology seed --auto-promote`; `--no-auto-promote` overrides the config)
`seed` writes candidates that meet the rules as `verified` with attestation
`auto-promote:<rule>` and reports how many were promoted and held back (and
why). `--status` overrides win. `ncp ontology promote --auto` applies the same
rules to candidates already in the store. Rules are evaluated by the pure
function `ncp.ontology.promotion.select_promotions`.

### Schema pack v2 (repos, modules, tables)

`infrastructure` v2 is a strict superset of v1:

| Added | Relation types |
|---|---|
| `Repo`, `Module`, `Table` (each requires `name`) | `CONTAINS` (`Repo`→`Module`, `Database`→`Table`, acyclic), `BUILDS` (`Module`→`Service`, deployment intent), `READS` / `WRITES` (`Service`/`Module`→`Table`, dependency intent); `DEPENDS_ON`, `PRODUCES`, `CONSUMES` also accept `Module` sources (and `DEPENDS_ON` `Module` targets); `OWNS` reaches `Repo`, `Module`, `Database`, `Table` |

v2 is **opt-in**: writes that name no `schema_version` stay on v1 unless
`[ontology].pack_versions = { infrastructure = 2 }`. A manifest can also set
`schema_version = 2` itself, as `scan` does. An entity's version cannot
change while live relations reference it, so a namespace already seeded on
v1 cannot be re-seeded as v2 in place: `seed` refuses up front and says so.
Use a new namespace for v2, or revoke the v1 relations first.

### From Python

`ncp.ontology.seed` exposes `load_manifest`, `parse_manifest`,
`merge_manifests` and `seed_ontology(facade, manifest,
write_chunk=memory_chunk_writer(store))`, which returns a `SeedReport`.
`ncp.ontology.scan.scan_repository(root, ...)` returns the manifest and a
summary.

## Tools

All six tools are listed only when `enabled = true` under the `full` tool
profile. The full arguments and result shapes are in
[`NCP_HTTP_API.md`](./NCP_HTTP_API.md#ontology-tools-opt-in).

| Tool | Capability | Purpose |
|---|---|---|
| `ncp_ontology_capabilities` | none | Enabled flag, packs (with types), limits and tool names |
| `ncp_ontology_query` | `read` | Resolve one seed exactly, then traverse with bounds. Ambiguous seeds come back as candidates. |
| `ncp_ontology_explain` | `read` | One relation with all its evidence (liveness) and validity |
| `ncp_ontology_assert` | `write` (`verify` for verified relations) | Upsert an entity, or assert a relation idempotently |
| `ncp_ontology_revoke` | `write` (own relation) or `admin` | Revoke a relation (audited, idempotent) |
| `ncp_ontology_evidence` | `read` | Exact-id text of cited evidence chunks (at most 16) that are still live and readable in `pipeline_id` (`global`: global-scope chunks only); any other id is listed in `missing` |

The read tools (`query`, `explain`, `evidence`) return current state only and
reject an `as_of` argument with `invalid`.

**Schema versions.** An entity's id does not include its `schema_version`,
so upserting an entity with a new version keeps its id. Such an upgrade is
rejected with `conflict` while any non-revoked relation still touches the
entity (the message carries no count or ids). Revoke those relations, upgrade
both endpoints, then re-assert them under the new version. The check counts
relations in every namespace: a `global` entity cannot be upgraded while any
pipeline still has a non-revoked relation to it. A `global` admin cannot see
or revoke those from `global`; an `admin` principal allowed in each such
pipeline finds them with `ncp_ontology_query` (`pipeline_id` set to that
pipeline) and revokes them there with `ncp_ontology_revoke`. A relation whose
versions disagree still counts toward `max_outgoing`/`max_incoming` and cycle
checks until it is revoked, because it becomes live again if its endpoint
returns to the relation's version. As a defensive
check, traversal, query and automatic context never return a relation whose
pack version differs from either endpoint's current version. `explain` still
shows such a relation, with `active: false`, `schema_consistent: false` and
`inactive_reason: "schema_version_mismatch"`.

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
  its text stays in memory. A cited id in `ev:` can be dereferenced with
  `ncp_ontology_evidence` (`chunk_ids`, same `pipeline_id`), and the relation
  with `ncp_ontology_explain` using `ontology.paths[].relation_ids`. A cited
  chunk is **not** guaranteed to stay fetchable: once it expires, is
  superseded or is out of the caller's scope it comes back in `missing`.
- **Historical requests.** A request with `as_of` gets no ontology enrichment:
  no `[NCP:ONTOLOGY]` block, no paths or entities in metadata, no
  substitution. Its metadata reports `reason_code: "as_of_unsupported"`.
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
  `ncp_ontology_assert`, or a declarative manifest loaded with
  `ncp ontology seed` (optionally produced by the static `ncp ontology scan`
  of a repository; see [Seeding the graph](#seeding-the-graph)).
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
- **No point-in-time graph.** Graph reads only know the current state. A
  context request with `as_of` therefore skips enrichment entirely
  (`reason_code: "as_of_unsupported"`), so historical context never mixes in
  present-day facts. The explicit read tools reject `as_of` with `invalid`.
  Future work: propagate the snapshot through entity, relation and evidence
  reads, with explicit validity and history semantics. Filtering relation
  timestamps alone is not enough while endpoint versions and evidence
  liveness are read from current state.
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
- **Citation ids are not search keys for `ncp_fetch`.** `ncp_fetch` searches
  content, not ids. Dereference a cited id with `ncp_ontology_evidence`,
  which may report it `missing` once the chunk is no longer live or
  readable.
- **No Deployment type.** Neither `infrastructure` v1 nor v2 has a Deployment
  entity. Store environment and version as Service attributes or in
  evidence chunks.

## Rollout and disable guidance

1. **Disabled (default).** Ship the code with no `[ontology]` section. This
   is a byte-for-byte no-op and creates no tables.
2. **Read-only pilot.** Set `enabled = true`, `retrieval_mode = "off"`, and
   keep the default `unauthenticated_capabilities`. Explicit tools only.
   Ingest the graph with `ncp ontology seed` (see
   [Seeding the graph](#seeding-the-graph)), or over MCP with one `verify`
   principal, as in `examples/13_ontology/seed_graph.py`.
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
