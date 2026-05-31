# Changelog

All notable changes to Neural Context Protocol will be documented in this file.

## [0.14.x] - 2026-05-30

Two slices completing the 0.14.x line. No breaking changes.

### Added / Changed

- **`AsyncPgvectorStore.async_consolidate()`** (`ncp/stores/pgvector_async.py`): full async
  parity with sync `PgvectorStore.consolidate()`. Loads live chunks with async SELECT, filters
  by `trust_floor`, clusters with `cluster_by_tags()`, finds merge candidates via
  `find_merge_candidates()` (BM25 / SequenceMatcher), then for each merge group:
  async DELETE loser, INSERT tombstone (forward_ref, expires_at=+86400s), UPDATE keeper
  (generation+1, supersedes). Emits `consolidation_ready` whisper via
  `_async_emit_consolidation_whisper()` when `merged > 0` and not `dry_run`. Returns
  `ConsolidationReport`. 8 new tests.
- **`AsyncPgvectorStore.async_calibrate()`** (`ncp/stores/pgvector_async.py`): full async
  parity with sync `PgvectorStore.calibrate()`. Two modes — manual (chunk_id + trust →
  direct UPDATE) and batch (decay: `new_trust = base_trust * decay_factor` for old/high-trust
  gen-0 chunks; feedback: `new_trust = base_trust + feedback_weight * min(1.0, rc/10)` for
  chunks with `retrieval_count > 0`). `user_verified` chunks are always protected. Returns
  `CalibrationReport`. 8 new tests.
- Suite: `540 passed, 8 skipped`

## [0.11.x] - 2026-05-30

Two slices completing the 0.11.x line. No breaking changes.

### Added / Changed

- **`diversity_limit` wire-through** (`ncp/assembler.py`, `ncp/api.py`, `ncp/mcp/server.py`,
  `.ncp/run.py`): `diversity_limit: int | None = None` threaded from
  `Assembler._retrieve_chunks` → `_prepare_assembly` → `assemble`/`assemble_incremental` →
  `api.get_context/run/stream` → MCP `_handle_get_context`/`_handle_fetch` → `store.query`.
  `ncp_get_context` and `ncp_fetch` inputSchema updated. `.ncp/run.py get_context` and `fetch`
  both extract and forward. `None` means "store uses own default (2)". 14 new tests.
- **`_is_duplicate` self-match fix** (`ncp/stores/sqlite.py`, `pgvector.py`,
  `pgvector_async.py`): added `AND chunk_id != ?/%s` to WHERE clause in all three stores.
  Idempotent upsert of an existing chunk now proceeds correctly instead of being silently
  rejected as a self-duplicate. 5 new tests + fake-cursor update in `test_future_stores.py`.
- Suite: `498 passed, 8 skipped`

## [0.10.x] - 2026-05-30

Two slices completing the 0.10.x line. No breaking changes.

### Added / Changed

- **Configurable `diversity_limit`** (`ncp/stores/base.py`, `sqlite.py`, `pgvector.py`,
  `pgvector_async.py`): `BaseStore.query()` and all implementations now accept
  `diversity_limit: int = 2`. Replaces the hardcoded per-author cap. Default preserves
  existing behavior. Guard `max(1, diversity_limit)` prevents zero/negative misuse.
  New: 15 tests in `tests/test_diversity_limit.py` covering SQLite, PgvectorStore
  (hybrid + trust_recency + vector), and AsyncPgvectorStore behavioral + signature.
- **Vector-mode diversity loop** (`ncp/stores/pgvector.py`): `_query_vector` now applies
  the same author-diversity pass as hybrid/trust_recency. SQL LIMIT changed from
  `max(1, k)` to `max(1, k*4)` unconditionally to give the diversity loop enough
  candidates. Results respect `diversity_limit` per author before the final `[:k]` cap.
- Suite: `479 passed, 8 skipped`

## [0.9.x] - 2026-05-30

Two slices completing the 0.9.x line. No breaking changes.

### Added / Changed

- **`AsyncPgvectorStore` dedup/GC parity** (`ncp/stores/pgvector_async.py`):
  `async_write` now executes all 8 steps of sync `write()`: validate → `_async_soft_gc` →
  `_async_assert_src_immutable` → `_async_is_duplicate` → INSERT/upsert → `_async_hard_gc`.
  Returns `False` (no-op) when content similarity > 0.92 in the same zone/layer/pipeline.
  ON CONFLICT SET now updates all 26 columns (was 4). `max_working_chunks=500`,
  `gc_threshold=400` added to `__init__`. `_async_hard_gc` uses `executemany` matching sync
  batch-delete behavior. New: `tests/test_async_pgvector_dedup_gc.py` (8 tests).
- **Native async Redis whispers** (`ncp/stores/redis_coordination.py`,
  `ncp/stores/pgvector_async.py`): `AsyncRedisCoordination` class added using
  `redis.asyncio` — eliminates `anyio.to_thread.run_sync` shim entirely from
  `AsyncPgvectorStore`. `async_emit_whisper` and `async_drain_whispers` now delegate to
  `_acoordination.emit_whisper/drain_whispers` directly. `AsyncPgvectorStore` accepts
  `redis_url=` and `coordination=` kwargs; raises `NCPStoreUnavailableError` when whispers
  are called without Redis configured. New: `tests/test_async_redis_coordination.py`
  (10 tests).
- Suite: `464 passed, 8 skipped`

## [0.8.x] - 2026-05-30

Two slices completing the 0.8.x line. No breaking changes.

### Added / Changed

- **Caller-controlled `k` through assembler** (`assembler.py`, `api.py`, `mcp/server.py`):
  `assemble(k=N)`, `assemble_incremental(k=N)`, `api.get_context(k=N)`, `api.run(k=N)`,
  `api.stream(k=N)` now forward k to the store. Default (`k=None`) preserves existing
  pressure-based logic (k=2 critical, k=4 otherwise). Negative k clamped to 1.
  `ncp_get_context` MCP tool schema adds optional `k` integer property.
  `.ncp/run.py fetch` k cap also removed (max(1,k) instead of min(k,4)).
- **`AsyncPgvectorStore`** (`ncp/stores/pgvector_async.py`): new `BaseStore` subclass
  using `psycopg_pool.AsyncConnectionPool`. Eliminates `anyio.to_thread.run_sync` on
  the hot async path (`async_write`, `async_query`, `async_log_turn_record`,
  `async_log_conscious`, `async_log_cost`, `async_resolve_recent_ref`). Pool opens
  lazily on first `_aconnect()` call. Sync abstract methods raise `NotImplementedError`.
  Whisper methods (`async_emit_whisper`, `async_drain_whispers`) retain thread shim
  since they delegate to Redis coordination.

### Verified

- Full test suite: 446 passed, 8 skipped, ruff clean
- New `tests/test_assembler_k_forwarding.py` (6 tests)
- New `tests/test_async_pgvector_store.py` (9 tests)

## [0.7.x] - 2026-05-30

Two post-0.7.0 slices completing the 0.7.x line. No breaking changes.

### Added / Changed

- **Caller-controlled `k`** (`PgvectorStore`, `SQLiteStore`, MCP server): removed the
  hardcoded `min(k, 4)` cap from all retrieval paths. `store.query(k=N)` now returns up to
  N results for any N ≥ 1. Diversity-per-author cap (`diversity_limit=2`) and the
  reranker recall buffer (`k × 4`) are preserved. `mcp/server.py` updated to pass the
  caller's `k` through instead of capping at 4.
- **psycopg3 driver upgrade** (`PgvectorStore`): replaced EOL `psycopg2-binary` with
  `psycopg[binary]` and `psycopg-pool`. Pool construction switches from
  `ThreadedConnectionPool(min, max, dsn)` to `ConnectionPool(conninfo=dsn, min_size=min,
  max_size=max, open=True)`. `close()` calls `pool.close()` (psycopg3 API) instead of
  `closeall()`. Synchronous behaviour and the `anyio.to_thread.run_sync` async shim are
  unchanged.

### Dependency changes

- `[pgvector]` extra: `psycopg2-binary` removed; `psycopg[binary]` + `psycopg-pool` added.

### Verified

- Full test suite: 431 passed, 8 skipped, ruff clean
- All existing pool tests updated to patch `psycopg_pool.ConnectionPool`
- New `tests/test_query_k_semantics.py` (6 tests) and `tests/test_psycopg3_upgrade.py` (4 tests)

## [0.6.x] - 2026-05-28

Three post-0.6.0 slices completing the 0.6.x line. No breaking changes.

### Added

- **IVF-FLAT index** (`migration 004`): `CREATE INDEX ... USING ivfflat (embedding vector_cosine_ops) WITH (lists = 100)` on `{prefix}chunks`. Matches the `<=>` cosine operator used by `retrieval_mode="vector"`. Reversible via DOWN section.
- **`ivfflat_probes` on `PgvectorStore`**: new constructor param `ivfflat_probes: int = 10`; `_query_vector` prepends `SET LOCAL ivfflat.probes = %s` before every ANN SELECT, scoped to the transaction so it cannot leak across pool connections.
- **`log_cost` CLI command** in `.ncp/run.py`: exposes `log_cost_raw` to external callers (Sarathi, scripts) via `python3 .ncp/run.py log_cost '{"agent_id":...,"model":...,"input_tokens":...,"output_tokens":...}'`. Turn ID auto-generated if omitted. Output visible in `ncp cost`.
- **Embedding provider integration** (`ncp/adapters/embedding.py`): `BaseEmbeddingAdapter` (contract + `_validate_dims`), `OpenAIEmbeddingAdapter` (`text-embedding-3-small`, 1536 dims), `LocalEmbeddingAdapter` (`sentence-transformers`, model-configurable). Both do lazy imports — zero dependency footprint unless enabled.
- **Auto-embed on write** (`PgvectorStore`): if `embedding_adapter` is set and `chunk.embedding is None`, calls `adapter.embed(chunk.content)` and attaches the vector before the DB upsert.
- **Auto-embed on query** (`PgvectorStore._query_vector`): if `embedding_adapter` is set and no `embedding` is passed, auto-embeds the query text instead of raising `ValueError`.
- **Embedding config section**: `[embedding]` in `DEFAULT_CONFIG` with `enabled = false`, `provider = "local"`, `model = "sentence-transformers/all-MiniLM-L6-v2"`. Three `NCPConfig` properties (`embedding_enabled`, `embedding_provider`, `embedding_model`) and three env overrides (`NCP_EMBEDDING_ENABLED`, `NCP_EMBEDDING_PROVIDER`, `NCP_EMBEDDING_MODEL`).
- **Factory wiring**: `ncp/stores/factory.py` builds and injects the embedding adapter from config into `PgvectorStore` when `embedding.enabled = true`.

### Verified

- Full test suite: 421 passed, 8 skipped, ruff clean
- SQLite store: unchanged, still raises `ValueError` for `retrieval_mode="vector"`
- Existing callers passing `embedding=` explicitly: unaffected (adapter skipped when embedding already present)

## [0.6.0] - 2026-05-27

Streaming assembly milestone. `ncp_get_context` now supports opt-in NDJSON
streaming for progressive context delivery and elimination of timeout risk on
large assemblies.

### Added

- **Streaming `ncp_get_context`**: passing `"stream": true` in tool arguments
  switches the response to progressive section delivery. HTTP transport returns
  `Content-Type: application/x-ndjson` with one JSON line per section
  (`{"type":"ncp_chunk","section":"...","index":N,"text":"..."}`) followed by the
  full JSON-RPC response as the final line. Stdio transport emits one
  Content-Length-framed `ncp/stream_chunk` JSON-RPC notification per section
  before the final response frame. Clients that do not handle notifications
  receive the final response unchanged.
- `Assembler.apply_post_middleware(text: str) -> str`: public method wrapping
  `MiddlewarePipeline.post_assemble`; used by the streaming path to apply
  middleware to the joined section text without calling `assemble()` twice.
- `StreamResponse` dataclass in `ncp/mcp/server.py`: sentinel return type from
  `_handle_get_context` that carries `sections`, `handler_result`, and
  `request_id`; detected by both transport layers to switch to streaming mode.

### Verified

- Full test suite: 393 passed, 8 skipped, ruff clean
- Non-streaming callers: zero behavior change (`stream` defaults to `false`)
- Sections emitted in order: `budget_header`, `conscious`, `subconscious` (one
  per fitting chunk), `whispers` (if any)

## [0.5.0] - 2026-05-26

Production readiness and embedding milestone. Three slices across pgvector and
both stores; no breaking changes to existing callers.

### Added

- **pgvector connection pooling** (`PgvectorStore`): `ThreadedConnectionPool` is
  created by default when no `connect_factory` is injected; `_connect()` checks
  out and returns connections via `getconn()`/`putconn()` instead of
  opening/closing a TCP connection per call; `min_pool_connections=2` and
  `max_pool_connections=10` are configurable constructor params; `close()` method
  drains the pool; passing an explicit `connect_factory` disables pooling (unit
  test path unchanged)
- **Embedding storage + ANN retrieval** (`SubconsciousChunk`, `PgvectorStore`,
  migration 003): `SubconsciousChunk` gains optional `embedding: list[float] |
  None = None` field validated to 1536 dimensions; `PgvectorStore.write()` stores
  the embedding when provided; migration 003 adds nullable `vector(1536)` column;
  `retrieval_mode="vector"` on `PgvectorStore.query()` issues `ORDER BY embedding
  <=> %s::vector LIMIT k` and converts cosine distance to score via
  `1/(1+distance)`; SQLite raises `ValueError` for `"vector"` mode with a clear
  message pointing to pgvector
- `BaseStore.query()`, `SQLiteStore.query()`, and `PgvectorStore.query()` gain
  `embedding: list[float] | None = None` parameter (default `None`; backward
  compatible); `"vector"` added to `_VALID_RETRIEVAL_MODES` in both stores

### Changed

- `SupportsAssemblyStore` Protocol removed from `ncp/assembler.py`;
  `Assembler.__init__` now types `store: BaseStore` directly; existing
  structural-duck-type test stubs in `test_assembler_phase3.py` are annotated
  with `# type: ignore[arg-type]` to document the intentional deviation

### Verified

- Full test suite: 388 passed, 8 skipped
- Ruff: zero lint errors
- All three slices implemented with dedicated test files:
  `tests/test_pgvector_pool.py` (7 tests), `tests/test_embedding_ann.py`
  (11 tests)

## [0.4.0] - 2026-05-26

Release hardening and retrieval quality milestone. All four slices landed on
both SQLite and pgvector; no breaking changes to existing callers.

### Added

- **pgvector schema migrations** (`ncp/stores/migrations.py`, `ncp/migrations/`):
  `MigrationRunner` with advisory lock, SHA-256 checksums, UP/DOWN sections,
  idempotent apply, version-ordered rollback, and `ncp migrate` CLI commands
  (`check`, `apply [--dry-run]`, `rollback <version> [--dry-run]`)
- **Migration 001**: baseline pgvector schema (chunks, whispers, turns, costs,
  schema_versions tracking table)
- **Migration 002**: `retrieval_count` and `last_retrieved_at` columns added to
  the chunks table
- **Retrieval feedback calibration** (`calibrate(feedback_mode=True)`): every
  `query()` call increments `retrieval_count` and stamps `last_retrieved_at`; a
  new `feedback_mode` pass in `calibrate()` boosts `base_trust` proportional to
  retrieval count (saturates at 10 retrievals, default +15% max, `dry_run`
  supported); `CalibrationReport` extended with `feedback_adjusted` field and
  change-log entries with `reason="retrieval_feedback"`
- **Incremental assembly** (`Assembler.assemble_incremental()`): generator that
  yields `(label, section_text)` pairs in priority order
  (`budget_header → conscious → subconscious → whispers`) with an optional
  `max_tokens` cap enforced via word-split proxy; budget/conscious sections always
  emitted; `assemble()` refactored to call shared `_prepare_assembly()` helper
- **Non-BM25 retrieval mode** (`retrieval_mode` parameter on `BaseStore.query()`):
  `"hybrid"` (default, existing BM25 + recency + trust) and `"trust_recency"`
  (skips BM25 and term-overlap filter, scores by recency + trust with renormalized
  weights); `RetrievalPolicy.score_no_bm25()` added; unknown mode values raise
  `ValueError`; `SupportsAssemblyStore` Protocol updated

### Changed

- `BaseStore.query()` gains `retrieval_mode: str = "hybrid"` — default behavior
  unchanged; existing callers require no modification
- `SubconsciousChunk` gains `retrieval_count: int = 0` and
  `last_retrieved_at: float | None = None` fields
- `CalibrationReport` gains `feedback_adjusted: int = 0` field
- `Assembler.assemble()` now delegates setup to `_prepare_assembly()`;
  output is identical to the previous implementation

### Verified

- Full test suite: 370 passed, 8 skipped
- Ruff: zero lint errors
- OpenCode (deepseek-v4-flash-free) reviewed all 4 implementation slices; one
  structural fix applied per review (Slice 3: multiple `[NCP:SUBCONSCIOUS]`
  headers; Slice 4: unknown-mode silent fallthrough)

## [0.3.0] - 2026-05-25

Operator tooling and maintenance milestone. SQLite remains the default runtime;
all new commands work on both SQLite and pgvector.

### Added

- `ncp consolidate` command: tag pre-clustering + BM25/SequenceMatcher similarity
  merge, trust_floor pre-filter, dry_run flag, `consolidation_ready` whisper on
  completion; `ConsolidationReport` dataclass
- `ncp calibrate` command: batch trust decay (protects `user_verified` chunks)
  and manual pinpoint override; `CalibrationReport` dataclass
- `ncp viz` command: 5-panel operator view — chunk distribution by layer/zone,
  age brackets, top chunks by trust, pipeline summary, whisper queue breakdown
- `ncp batch` command: JSONL file-in / results-out batch processor; runs against
  the local store without a live MCP server; supports write_memory, emit_whisper,
  query, consolidate, calibrate ops; `--dry-run` and `--stop-on-error` flags
- `BaseStore` ABC extended: `consolidate()`, `calibrate()`, `viz_data()` are now
  `@abstractmethod` — both SQLiteStore and PgvectorStore implement all three
- `[consolidation]` config section: `similarity_threshold`, `trust_floor`,
  opt-in `model_provider`/`model`

### Verified

- Full test suite: 306 passed, 6 skipped
- OpenCode (deepseek-v4-flash-free) reviewed all 4 implementation slices

## [0.2.0] - 2026-05-25

Storage and retrieval milestone. SQLite remains the default runtime;
pgvector + Redis is the production-oriented durable path.

### Added

- `store.type = "pgvector"` durable store: chunk writes/query, working-zone reads,
  recent-ref turn logging, conscious snapshots, cost logging, goal-version reads,
  `ncp status`, `ncp cost`, `ncp explain`
- Redis-backed coordination for the pgvector path: whispers, fetch-session state,
  handoff queue
- `ncp handoff claude` and `ncp handoff opencode` commands for whisper-driven
  partner/reviewer orchestration loops
- Hybrid retrieval via `RetrievalPolicy`: fuses BM25 (lexical), recency decay, and
  `base_trust` into a normalized `[0, 1]` score; both SQLiteStore and PgvectorStore
  use the same policy, keeping behavior aligned across backends
- `richer ncp status` output with chunk, tombstone, layer, pipeline, and last-activity
  visibility
- `ncp cost` command with total, per-agent, per-model, and recent-entry rollups
- `ncp explain` command for a short human-readable store summary
- Claude `stream-json` review helper script for bounded review/debug workflows
- 2-attempt connection retry with 100 ms backoff on pgvector and Redis paths

### Changed

- `BaseStore` ABC now declares all methods that both concrete stores implement:
  `log_conscious`, `peek_whispers`, `acknowledge_whispers`, `log_cost_raw`, and
  `get_pipeline_goal_versions` are now `@abstractmethod`
- `HandoffStore` Protocol in `agent_handoff.py` replaced by direct `BaseStore`
  typing; duck-type `hasattr` guard removed
- Retrieval ranking changed from BM25-first + `effective_score` post-sort to explicit
  multi-signal hybrid fusion; zero-overlap guard preserved
- Provider install guidance now points at `neural-context-protocol[providers]`
- Known upstream Cohere warning noise suppressed at the adapter boundary

### Verified

- Full test suite: `236 passed, 6 skipped`
- Live pgvector + Redis integration suite: `6 passed`
- OpenCode review: all 4 implementation slices passed code review

## [0.1.0a1] - 2026-05-24

## 0.1.0a1 - 2026-05-24

Follow-up alpha release to publish under the PyPI-owned project name
`neural-context-protocol`.

### Changed

- PyPI package name changed from `ncp-sdk` to `neural-context-protocol`
- install documentation updated to reflect the published package name

## 0.1.0a0 - 2026-05-24

Initial alpha release candidate for the SQLite-first NCP V1 spine with HTTP/SSE
MCP as the public transport.

### Added

- launch-critical core models in `ncp/types.py`
- pidgin encoder, chunker, assembler, and SQLite store
- local runtime API in `ncp/api.py`
- provider adapters for Anthropic, OpenAI, Ollama, Gemini, Mistral, and Cohere
- HTTP/SSE MCP server and CLI commands: `ncp init`, `ncp serve`, `ncp status`, `ncp emit`, `ncp dogfood`
- deterministic MCP dogfood harness with Claude/OpenCode/Codex continuation support
- provider parity, benchmark, and dogfood documentation under `docs/`
- launch-critical examples for quickstart, multi-agent handoff, Claude Code, and Codex CLI
- wheel and sdist packaging path with installed CLI smoke proof
- minimal GitHub Actions CI for `ruff`, `pytest`, and `build`

### Changed

- adapter failures now surface as NCP-owned configuration, timeout, and response errors
- SQLite unavailability now surfaces as an explicit store error and clean CLI failure
- trust-boundary coverage now rejects structural-field whitespace injection, immutable `src` changes, invalid write bypasses, dissent broadcasts, fetch over-limit misuse, and dead-end ref ambiguity

### Verified

- full test suite: `176 passed`
- package build: wheel and sdist build successfully
- clean install smoke: installed `ncp init` and `ncp status` work from both wheel and sdist
- live host proof: Claude and OpenCode both connect to the same HTTP MCP endpoint, write shared memory, fetch each other's writes, and deliver whispers across hosts

### Known Notes

- `GeminiAdapter` still uses the deprecated `google.generativeai` SDK because `google.genai` is not yet available in the current supported environment
- the Cohere SDK emits upstream Python deprecation warnings during tests, but functional behavior is green
