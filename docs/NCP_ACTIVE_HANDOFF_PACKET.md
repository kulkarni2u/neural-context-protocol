# NCP End-to-End Handoff Packet

This document is the current restart packet for the NCP project. Use it as the
primary context for Claude, OpenCode, or another bounded coding agent instead
of replaying long chat history.

## Current State

Current local repo state:

- Version: `0.6.0`
- GitHub branch: `main`
- Suite: `574 passed, 8 skipped`
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
- Handoff timeout failures now surface as clean NCP errors with runner name,
  timeout budget, and prompt size instead of raw Python tracebacks
- NCP handoff workflow validated end to end across bounded implementation and
  review lanes

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
- Suite: `393 passed, 8 skipped`

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
- **`log_cost` CLI**: `.ncp/run.py log_cost` exposes `log_cost_raw` to external callers so scripts and host runtimes can post token usage into `ncp cost`.
- **Embedding provider integration**: `ncp/adapters/embedding.py` — `BaseEmbeddingAdapter`, `OpenAIEmbeddingAdapter` (`text-embedding-3-small`), `LocalEmbeddingAdapter` (`sentence-transformers`). `PgvectorStore` auto-embeds on `write()` and `_query_vector()` when `embedding_adapter` is configured. `[embedding]` config section + env overrides. Factory wires adapter from config.
- Suite: `421 passed, 8 skipped`

## What Shipped In 0.7.x

- **Caller-controlled `k`** (`PgvectorStore`, `SQLiteStore`, MCP server): removed `min(k, 4)` from all
  retrieval paths. `store.query(k=N)` now returns up to N results for any N. `mcp/server.py` cap
  also removed. Diversity-per-author (`diversity_limit=2`) and reranker recall buffer (`k × 4`) preserved.
  New: `tests/test_query_k_semantics.py` (6 tests).
- **psycopg3 driver upgrade** (`PgvectorStore`): `psycopg2-binary` → `psycopg[binary]` +
  `psycopg-pool`. Pool: `ThreadedConnectionPool(min,max,dsn)` → `ConnectionPool(conninfo=dsn,
  min_size=min, max_size=max, open=True)`. `close()`: `closeall()` → `close()`. Async shim unchanged.
  `test_pgvector_pool.py` updated; new `tests/test_psycopg3_upgrade.py` (4 tests).
- Suite: `431 passed, 8 skipped`

## What Shipped In 0.8.x

- **Assembler k-forwarding**: `assemble(k=N)`, `assemble_incremental(k=N)`, `api.get_context(k=N)`,
  `api.run(k=N)`, `api.stream(k=N)` forward k to `store.query`. Default (`k=None`) preserves
  pressure logic (k=2 critical, k=4 otherwise). Negative k clamped to 1. `ncp_get_context` MCP tool
  schema adds optional `k`. `.ncp/run.py fetch` k cap also removed. New:
  `tests/test_assembler_k_forwarding.py` (6 tests).
- **`AsyncPgvectorStore`** (`ncp/stores/pgvector_async.py`): new `BaseStore` subclass using
  `psycopg_pool.AsyncConnectionPool`. Eliminates `anyio.to_thread.run_sync` on the hot async path
  (`async_write`, `async_query`, `async_log_turn_record`, `async_log_conscious`, `async_log_cost`,
  `async_resolve_recent_ref`). Lazy pool open on first `_aconnect()`. Sync methods raise
  `NotImplementedError`. New: `tests/test_async_pgvector_store.py` (9 tests).
- Suite: `446 passed, 8 skipped`

## What Shipped In 0.9.x

- **`AsyncPgvectorStore` dedup/GC parity**: `async_write` now performs all 8 steps of sync
  `write()` — `_async_soft_gc`, `_async_assert_src_immutable`, `_async_is_duplicate`,
  INSERT/upsert with full 26-column ON CONFLICT SET, `_async_hard_gc` (using `executemany`).
  Returns `False` when content similarity > 0.92. `max_working_chunks`/`gc_threshold`
  configurable on `__init__`. 8 new tests.
- **Native async Redis whispers**: `AsyncRedisCoordination` (in `redis_coordination.py`)
  uses `redis.asyncio` — eliminates all `anyio.to_thread.run_sync` from the whisper path.
  `AsyncPgvectorStore` accepts `redis_url=`/`coordination=`; raises `NCPStoreUnavailableError`
  without Redis. 10 new tests.
- Suite: `464 passed, 8 skipped`

## What Shipped In 0.10.x

- **Configurable `diversity_limit`**: `BaseStore.query(diversity_limit=N)` parameter added to all
  implementations. Default=2 preserves existing behavior. `max(1, diversity_limit)` guard prevents
  zero/negative misuse. 15 new tests across all retrieval modes and stores.
- **Vector-mode diversity loop**: `_query_vector` now applies the same per-author diversity pass as
  hybrid/trust_recency. SQL LIMIT changed to `k*4` unconditionally (was `k` without reranker) to
  give the loop enough candidates. Results respect `diversity_limit` per author.
- Suite: `479 passed, 8 skipped`

## What Shipped In 0.11.x

- **`diversity_limit` wire-through**: threaded end-to-end from assembler → api → MCP tools
  (`ncp_get_context`, `ncp_fetch`) → `.ncp/run.py` `get_context`/`fetch`. `None` uses store
  default (2). 14 new tests including behavioral MCP handler call-through tests.
- **`_is_duplicate` self-match fix**: `AND chunk_id != ?/%s` added to WHERE clause in all
  three stores (SQLite, PgvectorStore, AsyncPgvectorStore). Idempotent upserts of existing
  chunks now succeed instead of being silently dropped. 5 new tests.
- Suite: `498 passed, 8 skipped`

## What Shipped In 0.14.x

- **`AsyncPgvectorStore.async_consolidate()`**: full async parity with sync `consolidate()`.
  SELECT all live chunks (not in tombstones), filter by `trust_floor`, cluster with
  `cluster_by_tags()`, run `find_merge_candidates()` per cluster (BM25/SequenceMatcher). For each
  merge group: async DELETE loser, INSERT tombstone (forward_ref → keeper, expires_at +86400s),
  UPDATE keeper (generation+1, supersedes=JSON list). Emits `consolidation_ready` whisper via
  `_async_emit_consolidation_whisper()` when merged > 0 and not dry_run. Returns
  `ConsolidationReport`. 8 new tests.
- **`AsyncPgvectorStore.async_calibrate()`**: full async parity with sync `calibrate()`.
  Manual mode: SELECT chunk by chunk_id, UPDATE base_trust. Batch decay mode: SELECT all chunks,
  apply `base_trust * decay_factor` to eligible (old/high-trust/gen-0). Feedback mode: boost
  `base_trust` by `feedback_weight * min(1.0, retrieval_count/10)` for retrieved chunks.
  `user_verified` src always protected. Returns `CalibrationReport`. 8 new tests.
- Suite: `540 passed, 8 skipped`

## Active Line: 0.16.x (next)

The `0.15.x` line is complete, and the first five `0.16.x` retrieval slices
are now in: shared vector-aware retrieval scoring plus sync/async pgvector
hybrid tie-break parity, then shared retrieval-contract helpers for blank-query
fallback, zero-overlap gating, normalized result caps, and diversity trimming,
then shared lexical candidate generation for the hybrid path, then shared
non-lexical scoring helpers for trust/recency and vector distance, then
assembler retrieval-cap cleanup so chunk/whisper pressure limits are derived in
one place and forwarded consistently (`574 passed, 8 skipped`, ruff clean).
Suggested next priorities:

- **Candidate-generation boundary cleanup**: now that caps are explicit, decide
  whether trust/recency candidate generation should be abstracted further so
  SQLite, sync pgvector, and async pgvector stay aligned without widening the
  current retrieval contract
- **SQLite parity decision**: decide whether SQLite should stay lexical-only or
  grow a more explicit trust/recency candidate-generation helper path too
- **Async reporting consumption**: thread the new async status/cost/viz parity
  into any async operator or service paths that still rely on sync-only access

## Known Architectural Gaps (carried forward)

- SQLite still has lexical-only retrieval while pgvector sync/async now have a
  vector-assisted hybrid tie-break path
- The scoring math and assembly caps are shared now, but candidate-generation
  responsibilities across SQLite and pgvector are still not fully unified

## Recommended Agent Roles

- **Claude**: bounded implementation/planning partner
- **OpenCode**: bounded reviewer (`opencode/deepseek-v4-flash-free`)
- **NCP**: handoff transport and shared bounded context
- **Task runner / host process**: task and evidence tracking

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

> Read `docs/NCP_ACTIVE_HANDOFF_PACKET.md` first. The first five `0.16.x`
> retrieval slices are already in (`574 passed, 8 skipped`, ruff clean):
> shared vector-aware scoring, shared retrieval-contract helpers, shared
> lexical candidate generation, and shared non-lexical scoring helpers.
> Continue `0.16.x` with the next narrow retrieval architecture slice: make the
> assembler/store retrieval boundary more explicit while keeping current
> behavior stable. Use a multi-agent task runner when helpful, and keep NCP as
> the default communication spine in every subagent instruction.
