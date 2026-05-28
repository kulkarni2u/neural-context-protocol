# NCP End-to-End Handoff Packet

This document is the current restart/orchestration packet for the NCP project.
Use it as the primary context for Claude, OpenCode, or another bounded coding
agent instead of replaying long chat history.

## Current State

Latest release:

- PyPI: `neural-context-protocol==0.6.0` *(pending publish)*
- GitHub: `main` branch
- Suite: `388 passed, 8 skipped`
- Live pgvector + Redis integration suite: `6 passed`

## What Shipped In 0.2.0

### Storage

- `store.type = "pgvector"` durable store: chunk writes/query, working-zone
  reads, recent-ref turn logging, conscious snapshots, cost logging,
  goal-version reads, `ncp status`, `ncp cost`, `ncp explain`
- Redis-backed coordination for the pgvector path: whispers, fetch-session
  state, handoff queue
- 2-attempt connection retry with 100 ms backoff on pgvector and Redis paths

### Retrieval

- `RetrievalPolicy` dataclass: fuses BM25 (w=0.5), recency decay (w=0.3),
  and `base_trust` (w=0.2) into a normalized `[0, 1]` score
- Both `SQLiteStore` and `PgvectorStore` share the same policy — behavior is
  intentionally aligned across backends
- Generation penalty applied multiplicatively in `policy.score()`
- Zero-overlap guard preserved

### Interface / ABC

- `BaseStore` ABC now declares all methods both concrete stores implement:
  `log_conscious`, `peek_whispers`, `acknowledge_whispers`, `log_cost_raw`,
  `get_pipeline_goal_versions` are all `@abstractmethod`
- `HandoffStore` Protocol in `agent_handoff.py` kept only as backward-compat
  alias; duck-type `hasattr` guard removed; all handoff functions typed against
  `BaseStore`
- `SupportsAssemblyStore` Protocol kept for backward compat with duck-typed
  test stubs in `test_assembler_phase3.py`

### Workflow

- `ncp handoff claude` and `ncp handoff opencode` commands for whisper-driven
  partner/reviewer orchestration loops
- Sarathi + NCP handoff orchestration validated end to end

### Docs

- CHANGELOG updated with full 0.2.0 entry
- README updated: version, next-focus, "How NCP Reduces Token Cost" section

## What Shipped In 0.3.0

- `ncp consolidate` — tag pre-cluster + BM25 merge, dry_run, consolidation_ready whisper
- `ncp calibrate` — batch trust decay + manual override, user_verified protection
- `ncp viz` — 5-panel operator view (distribution, age, top chunks, pipelines, whispers)
- `ncp batch` — JSONL batch processor, no MCP server required
- BaseStore ABC: consolidate, calibrate, viz_data all @abstractmethod
- Suite: 306 passed, 6 skipped

## What Shipped In 0.4.0

- **Slice 1 — pgvector schema migrations**: `MigrationRunner` with advisory lock,
  SHA-256 checksums, UP/DOWN sections, `ncp migrate check/apply/rollback` CLI.
  Migration 001 (baseline schema) and 002 (`retrieval_count`, `last_retrieved_at`).
- **Slice 2 — Retrieval feedback calibration**: `query()` increments
  `retrieval_count`/`last_retrieved_at` on every returned chunk;
  `calibrate(feedback_mode=True)` boosts `base_trust` proportional to retrieval
  count (max +15% at 10 retrievals, `dry_run` supported). `CalibrationReport`
  extended with `feedback_adjusted` and change-log entries.
- **Slice 3 — Incremental assembly**: `Assembler.assemble_incremental()` generator
  yields `(label, text)` pairs in priority order; enforces `max_tokens_per_call`
  budget via word-split proxy; `_prepare_assembly()` extracted so `assemble()` and
  `assemble_incremental()` share identical setup logic.
- **Slice 4 — Non-BM25 retrieval mode**: `retrieval_mode` parameter on
  `BaseStore.query()` (`"hybrid"` default, `"trust_recency"` new);
  `RetrievalPolicy.score_no_bm25()` with renormalized weights + div-by-zero guard;
  unknown values raise `ValueError`.
- Suite: 370 passed, 8 skipped. Ruff clean. All four slices OpenCode-reviewed.

## What Shipped In 0.5.0

- **Slice 1 — pgvector connection pooling**: `ThreadedConnectionPool` wired in by
  default; `_connect()` uses `getconn()`/`putconn()` instead of open/close per
  call; `min_pool_connections=2`, `max_pool_connections=10` configurable;
  `close()` drains pool; factory injection still bypasses pooling for tests.
- **Slice 2 — SupportsAssemblyStore collapsed**: Protocol removed from
  `ncp/assembler.py`; `Assembler.__init__` now typed `store: BaseStore`; test
  stubs in `test_assembler_phase3.py` annotated `# type: ignore[arg-type]`.
- **Slice 3 — Embedding storage + ANN retrieval**: `SubconsciousChunk` gains
  `embedding: list[float] | None = None` (validated to 1536 dims); migration 003
  adds nullable `vector(1536)` column; `PgvectorStore.write()` stores embeddings;
  `retrieval_mode="vector"` queries via `<=>` cosine operator; SQLite raises
  `ValueError` for vector mode; all three stores gain `embedding` param on
  `query()`.
- Suite: `388 passed, 8 skipped`

## What Shipped In 0.6.0

- **Streaming `ncp_get_context`**: `"stream": true` in tool arguments switches
  both transports to progressive delivery. HTTP returns `Content-Type:
  application/x-ndjson` with one `{"type":"ncp_chunk","section":"...","index":N,"text":"..."}`
  line per section followed by the full JSON-RPC response as the final line.
  Stdio emits one Content-Length-framed `ncp/stream_chunk` notification per
  section before the final response. Non-streaming callers unaffected.
- `Assembler.apply_post_middleware(text: str) -> str`: public wrapper around
  `MiddlewarePipeline.post_assemble`; used by the streaming path to apply
  middleware to joined sections without calling `assemble()` twice.
- `StreamResponse` dataclass in `ncp/mcp/server.py`: sentinel return type
  carrying `sections`, `handler_result`, `request_id`; detected by both
  transport layers to switch modes.
- Suite: `393 passed, 8 skipped`

## What Shipped In 0.6.x

- **Migration 004 — IVF-FLAT index**: `CREATE INDEX ... USING ivfflat (embedding vector_cosine_ops) WITH (lists = 100)` on the embedding column. `PgvectorStore` gains `ivfflat_probes: int = 10`; `SET LOCAL ivfflat.probes` prepended before every ANN SELECT.
- **`log_cost` CLI**: `.ncp/run.py log_cost` exposes `log_cost_raw` to external callers so Sarathi and scripts can post token usage into `ncp cost`.
- **Embedding provider integration**: `ncp/adapters/embedding.py` — `BaseEmbeddingAdapter`, `OpenAIEmbeddingAdapter` (`text-embedding-3-small`), `LocalEmbeddingAdapter` (`sentence-transformers`). `PgvectorStore` auto-embeds on `write()` and `_query_vector()` when `embedding_adapter` is configured. `[embedding]` config section + env overrides. Factory wires adapter from config.
- Suite: `421 passed, 8 skipped`

## Active Line: 0.7.x (next)

The `0.6.x` line is complete. Suggested next priorities:

- Query result count semantics: relax the max-4 cap on hybrid/trust-recency paths; let callers control `k` fully
- Async-native pgvector path: replace `anyio.to_thread.run_sync` shim with a native async connection

## Known Architectural Gaps (carried forward)

- query result count semantics still opinionated inside store methods (max 4
  returned by hybrid/trust_recency paths; vector path honours `k` up to 4)

## Recommended Agent Roles

- **Claude**: bounded implementation/planning partner
- **OpenCode**: bounded reviewer (`opencode/deepseek-v4-flash-free`)
- **NCP**: handoff transport and shared bounded context
- **Sarathi**: task/evidence tracking

## Recommended Orchestration Loop

1. Emit a bounded NCP whisper to Claude with slice, target files, acceptance
   criteria
2. Let Claude produce a bounded proposal or implementation
3. Emit the resulting bounded handoff to OpenCode for review
4. Apply only the accepted fixes
5. Run focused tests then full suite
6. Update docs
7. Commit and push

## Suggested Prompt For The Next Orchestrator

> Read `docs/NCP_0_2_0_HANDOFF_PACKET.md` first. The `0.6.x` line is complete
> (421 passed, 8 skipped, ruff clean). Starting `0.7.x`. Two suggested
> priorities: (1) relax the max-4 query result cap on hybrid and trust-recency
> retrieval paths — callers should control `k` fully; (2) async-native pgvector
> path — replace the `anyio.to_thread.run_sync` shim in `async_write` /
> `async_query` with a real async connection (e.g. `psycopg` v3 async).
> Keep SQLite and pgvector aligned. Use NCP handoffs for bounded coordination.
> Prefer correctness and tests over broad refactors.
