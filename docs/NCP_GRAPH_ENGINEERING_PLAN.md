# NCP Graph Engineering: Phase 1

> **Goal.** Engineer the relationships between memories as first-class graph structure so retrieval, trust, and reasoning can flow along edges instead of treating memory as a flat scored pool.

## What is graph engineering in NCP terms

Most memory buses treat stored chunks as a flat, scored collection. NCP Phase 1 promotes that into a **typed, multi-hop graph**. Every chunk can now have typed directional edges to other chunks, and retrieval, trust propagation, and export can traverse those edges under fine-grained control.

The payoff: causality, supersession, and support relationships become queryable at retrieval time instead of implicit in chunk order. An agent can ask "give me the evidence chain" or "what did this decision cause" — and the bus answers by walking edges, not re-ranking the flat pool.

## What shipped: Phase 1

### Typed edge model

Chunks are now linked via a closed-set edge type catalog:

| Edge type | Semantics | Direction |
|-----------|-----------|-----------|
| `caused_by` | This chunk was caused by another (it is the effect). | src ← dst: "src caused_by dst" means dst is the cause |
| `supersedes` | This chunk replaces an older one. | src → dst: "src supersedes dst" means src is the replacement |
| `supports` | This chunk provides evidence for another. | src → dst: "src supports dst" means src is evidence |
| `contradicts` | This chunk disputes another. | src → dst: "src contradicts dst" means they disagree |
| `refines` | This chunk adds detail to another. | src → dst: "src refines dst" means src clarifies dst |
| `derived_from` | This chunk was computed from another (e.g., a summary of a trace). | src → dst: "src derived_from dst" means dst was the input |

**Direction convention:** An edge `src → dst` with type `T` reads as "src `T` dst". Because `caused_by` represents the inverse relationship (the edge points *from* the effect *to* the cause), `src caused_by dst` reads "dst is the cause of src."

### Substrate and write-time backfill

The **`chunk_edges` table** stores edges as a normalized 1st-class relation:

```sql
CREATE TABLE chunk_edges (
  edge_id TEXT PRIMARY KEY,
  src_chunk_id TEXT NOT NULL,
  dst_chunk_id TEXT NOT NULL,
  edge_type TEXT NOT NULL,
  weight REAL NOT NULL DEFAULT 1.0,
  created_at REAL NOT NULL,
  created_by TEXT NULL,
  UNIQUE(src_chunk_id, dst_chunk_id, edge_type)
);
-- plus indexes on src_chunk_id and dst_chunk_id
```

When you write a chunk with legacy `caused_by` or `supersedes` fields, NCP **automatically backfills** a matching edge row (upsert on conflict). Legacy columns remain authoritative — you can keep writing the old way and edges are created for you. The backfill is **additive-only**: rewriting a chunk with a different `caused_by` target creates a new edge but does not retract the old one (the edge table is append-only, not destructive). Multi-hop traversal and the `ncp graph` command are aware of this and skip stale edges.

### Config knobs: bounded multi-hop traversal

Under `[retrieval]` in `.ncp/config.toml`:

```toml
[retrieval]
edge_max_hops = 1                          # max hops from retrieved chunks (default: 1)
edge_expansion_types = ["caused_by"]       # which edge types to follow (default: ["caused_by"])
propagation_max_hops = 1                   # max hops for trust propagation (default: 1)
```

Env var overrides: `NCP_EDGE_MAX_HOPS`, `NCP_EDGE_EXPANSION_TYPES` (CSV), `NCP_PROPAGATION_MAX_HOPS`.

**Defaults preserve legacy behavior exactly.** With `edge_max_hops=1` and `edge_expansion_types=["caused_by"]`, the new code is behaviorally equivalent to the 1-hop `caused_by` expansion that shipped before Phase 1 (verified by the pre-existing assembler and calibration test suites, unmodified).

### Multi-hop edge expansion in retrieval

When `ncp_get_context` assembles context, it runs **bounded breadth-first search (BFS)** from the initially-retrieved chunk set:

- Follow edges up to `edge_max_hops` hops, limited to types in `edge_expansion_types`.
- Each hop inherits relevance from the prior hop, multiplied by `edge_expansion_decay` (config ~0.7), so distant chunks matter less.
- Visited set prevents cycles.
- **Budget is never widened** — expanded chunks compete for the existing token budget and chunk cap; expansion is redistribution, not addition.
- When `caused_by` is in the configured types, the traversal first checks the chunk's current scalar `caused_by` column. If an edge row exists for that (src, dst) pair but disagrees with the scalar, the row is skipped as stale (the chunk was rewritten to point elsewhere). Only non-stale rows and absent-scalar cases reach traversal candidates.

Stale edges are skipped, not deleted, so `graph_data()` can export the full history; downstream consumers like the `ncp graph` CLI filter them for "current view" semantics.

### Multi-hop trust propagation in calibration

When `ncp calibrate --feedback` runs, trust deltas (from retrieval boosts and dissent penalties) now propagate **multi-hop along `caused_by` edges**:

- Walk up to `propagation_max_hops` hops via `caused_by` edges.
- At hop `h`, the credit/debit is scaled by `propagation_factor ** h` (default factor 0.5, so hop 1 = 0.5, hop 2 = 0.25, etc.).
- Cycle-safe via per-originator visited set.
- `user_verified` chunks are protected from automatic adjustment (pre-existing behavior, now applies multi-hop too).

The effect: a cause is credited for effects that proved useful, and debited when effects drew dissent, even if the link is multi-hop.

### MCP write API

`ncp_write_memory` now accepts an optional `edges` parameter:

```json
{
  "edges": [
    {"dst": "chunk_id_of_parent", "type": "caused_by", "weight": 1.0},
    {"dst": "chunk_id_of_support", "type": "supports"}
  ]
}
```

Each edge is written via `store.add_chunk_edges()` after the chunk write. Validation happens before any store write — unknown edge types are rejected as a JSON-RPC error. Weight defaults to 1.0.

### Store API

All three backends (SQLite, pgvector sync, pgvector async) implement:

- `add_chunk_edges(edges: list[ChunkEdge]) -> int` — upsert edges, return count written.
- `get_chunk_edges(chunk_ids, *, edge_types=None, direction="out", limit=200) -> list[ChunkEdge]` — fetch edges by src/dst filter and type.
- `graph_data(*, pipeline_id=None, limit=500) -> dict` — export nodes + edges for visualization, including legacy-column fallback.

SQLite adds the table inline to the schema; pgvector gets versioned migration `012_add_chunk_edges.sql`.

### CLI export

```bash
ncp graph --pipeline-id pipe_demo --format json --limit 500
ncp graph --format dot > graph.dot
ncp graph --format dot --output graph.dot
```

Outputs JSON or Graphviz DOT. JSON shape:

```json
{
  "nodes": [
    {"chunk_id": "...", "layer": "episodic", "base_trust": 0.8, ...}
  ],
  "edges": [
    {"src": "...", "dst": "...", "type": "caused_by", "weight": 1.0}
  ],
  "stats": {
    "node_count": 42,
    "edge_count": 18,
    "edges_by_type": {"caused_by": 15, "supports": 3}
  }
}
```

DOT format uses chunk IDs as node labels (first 12 chars), layers as subtitles, and **trust-band fillcolors** (green for ≥0.8, amber for 0.5–0.8, red for <0.5). Edge styles: solid for `caused_by`, dashed for `supersedes`, dotted for others. Stale `caused_by` edges are filtered before export so the CLI shows the current view.

Summary statistics (node count, edge count, per-type breakdown) are printed to stderr as a table; stdout is pure JSON/DOT.

## Compatibility stance

- **All defaults are conservative.** `edge_max_hops=1`, `edge_expansion_types=["caused_by"]`, `propagation_max_hops=1` keep the new multi-hop code behaviorally equivalent to the single-hop behavior that shipped before this phase.
- **Legacy columns are authoritative.** You can keep writing chunks with `caused_by` and `supersedes` scalars; edge rows are auto-created on write.
- **Stale edges are survivable.** When a chunk is rewritten with a different `caused_by` target, the old edge row stays in the table (additive-only). Traversal and calibration skip stale rows by checking the current scalar. Export filters them for "current view" semantics.
- **No breaking changes.** Existing code that does not use edges or the new config knobs works unchanged.

## Future phases (envisioned, not shipped)

### CAP-C7 · Automatic edge inference at write time

Detect causal and support relationships automatically when a chunk is written (e.g., via embeddings or a small model), creating edges without manual specification. Trades ingest latency for unsupervised graph enrichment.

### CAP-T3 extension · Outcome credit assignment

Walk the graph multi-hop to attribute outcome success/failure to the evidence and decisions that informed it, not just the immediate parent. Foundation for "this evidence led to a bad decision, deprioritize similar evidence" reasoning.

### Temporal graph queries

Bi-temporal graph queries: "what was the relationship state as of turn N" (combining the existing `as_of` point-in-time view with typed edges).

### Graph-aware consolidation

When merging redundant chunks, detect and merge edges too (e.g., if chunk A and chunk B both have a `caused_by` edge to C, the consolidation target keeps one).

## Implementation notes for operators

- **Performance:** edge expansion adds a per-hop `get_chunk_edges()` call inside the retrieval loop. With `edge_max_hops=1` (default), this is one extra query per retrieval, bounded by the chunk cap. Budget is never widened, so expansion is redistribution, not runaway.
- **Stale edge handling:** the multi-hop traversal and calibration propagation skip stale edges (edge row where the dst disagrees with the chunk's current `caused_by` scalar). This is safe because the new code checks the scalar first. `graph_data()` surfaces stale rows for audit; downstream consumers filter if they want "current view" semantics.
- **Backwards compatibility:** if a chunk has no `caused_by` scalar but does have an incoming edge row from a rewrite, traversal falls back to the edge table to resolve the parent. If it has a scalar, the scalar is authoritative and edges must agree or be skipped.
- **Cost:** `ncp calibrate --feedback` with `propagation_max_hops > 1` walks more edges, so calibration is slower. Start with `propagation_max_hops=1` (default) and raise only if you want multi-hop credit assignment.

## Related docs

- `ChunkEdge` model, edge types, and direction semantics: `ncp/types.py` (protocol-spec section for edges is a documentation follow-up).
- [North-star roadmap](./NCP_NORTH_STAR_CAPABILITY_ROADMAP.md) — CAP-T3 (outcome-calibrated reputation) and the capability pillars this plan extends. CAP-C7 is proposed in this document and does not yet appear in the roadmap.
