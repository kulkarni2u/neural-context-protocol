# Changelog

All notable changes to Neural Context Protocol will be documented in this file.

## [Unreleased]

Correctness, MCP-parity, and credibility overhaul from the protocol review
(`docs/NCP_OPTIMIZATION_PLAN.md`). Suggested next release: `1.1.0` (behavior
changes below).

### Changed (behavior)

- **Python floor raised to 3.11** (`pyproject.toml`): the package already used
  `typing.Self` and could not import on 3.10. CI now verifies importability at
  the minimum supported version.
- **Token counting is deterministic by default** (`ncp/tokens.py`): chars/4
  everywhere; set `NCP_TOKEN_UNIT=tiktoken` to opt in to cl100k_base counting.
  Benchmark verdicts no longer depend on whether tiktoken's encoding could be
  downloaded.
- **Whisper TTL default raised 60s → 1800s** (`ncp/types.py`); `ttl_seconds`
  exposed on `ncp_emit_whisper` and configurable via `[whispers]`.
- **Whisper delivery is at-least-once** (`ncp/assembler.py`): assembly peeks
  instead of draining; whispers are acknowledged in `post_turn` via
  `ack_whisper_ids`. Unacked whispers redeliver. `acknowledge_whispers` gains
  an `agent_id` keyword.
- **Broadcast whispers reach every pipeline agent** (`ncp/stores/sqlite.py`,
  `redis_coordination.py`): per-recipient delivery tracking replaces
  delete-on-first-drain.
- **Recent refs no longer crowd out retrieval** (`ncp/assembler.py`): recent
  turn refs are scored through the retrieval policy and capped at
  `recent_slot_budget` (default 2) so retrieved chunks keep their slots.
- **Pidgin wire format** (`ncp/encoder.py`, spec §1): `[NCP:BUDGET]` moved to
  the end for prompt-cache-friendly ordering; empty conscious fields omitted;
  whisper ages bucketed; JSON whisper payloads rendered as `key:value` lines.

### Added

- **Token budgets enforced at assembly** (`ncp/assembler.py`): `max_tokens`
  on `assemble()`/`ncp_get_context`; `context_token_budget` config (840).
- **`ncp_post_turn` MCP tool + server-side conscious state**
  (`ncp/mcp/server.py`): recent-ref continuity, drift tracking, cost logging,
  and budget pressure now work through MCP alone; `ncp_get_context` returns
  `pending_whisper_ids` and eviction/fetch telemetry.
- **Trust through MCP**: `base_trust` param on `ncp_write_memory` with
  src-derived defaults; `written_at_drift` stamped from the latest conscious
  snapshot.
- **HTTP server hardening** (`ncp/mcp/server.py`, `ncp/cli.py`): bearer-token
  auth (`--auth-token` / `NCP_AUTH_TOKEN` / `[server].auth_token`, generated
  by `ncp init`), CORS allowlist, 10 MB body cap, non-loopback warning.
- **SQLite FTS5 retrieval** (`ncp/stores/sqlite.py`): persistent BM25 index
  replaces per-query corpus rebuild.
- **Store retention** (`[retention] max_working_chunks_per_pipeline`):
  optional write-time eviction of lowest trust/recency-scored working-zone
  chunks; disabled by default.
- **Task-success benchmark** (`benchmarks/task_success/`): 12 tasks scored at
  a matched token budget; keyless mock mode measures context adequacy
  (NCP 1.00 vs sliding window 0.00 at budget 400); live-provider mode for
  real task success. CI gates on the coding-pipeline and needle benchmarks.
- **`ncp demo`**: deterministic 3-agent pipeline showing per-turn savings.
- **LangGraph example** (`examples/03_langgraph/`), **HTTP API contract doc**
  (`docs/NCP_HTTP_API.md`), **prompt-injection threat model** (spec §5.1 and
  generated turn contracts), `AGENTS.md` conventions.

### Fixed

- README/benchmark numbers regenerated from the current code and made
  internally consistent; the coding benchmark's pass gate is green again
  (13.13x vs raw replay at the final turn, `chars_div4`).
- Fetch-session state race in the threaded HTTP server; hardcoded MCP
  serverInfo version; hardcoded OpenCode reviewer model; early HTTP error
  responses no longer lose the response to a TCP reset on unread bodies;
  Redis whisper reads pipelined and stats scan bounded.

## [1.0.4] - 2026-06-06

Docs-sync release so the public install story matches the shipped CLI.

### Added / Changed

- **Public pgvector setup path** (`README.md`): replace the repo-only
  `./scripts/infra_up.sh` example with the installed `ncp infra up` command
  for managed local Postgres + Redis, and keep a separate bring-your-own
  example for external infrastructure.
- **Release surface coherence** (`CHANGELOG.md`,
  `docs/NCP_V1_RELEASE_CHECKLIST.md`): add missing `1.0.2` and `1.0.3`
  changelog entries and align the checklist with the current stable release
  line.
- **Version metadata alignment** (`pyproject.toml`, `ncp/version.py`,
  `ncp/mcp/server.py`): bump package and MCP server version strings to `1.0.4`.

## [1.0.3] - 2026-06-05

Patch release focused on pgvector shutdown reliability.

### Fixed

- **Python interpreter shutdown cleanup** (`ncp/stores/pgvector.py`): register
  `pool.close()` with `atexit` so pgvector-backed runs do not raise
  `PythonFinalizationError` during interpreter teardown.
- **Version metadata alignment** (`pyproject.toml`, `ncp/version.py`,
  `ncp/mcp/server.py`): bump package and MCP server version strings to `1.0.3`.

## [1.0.2] - 2026-06-04

Public-install ergonomics and credibility follow-up release.

### Added / Changed

- **Interactive setup wizard** (`ncp/cli.py`): `ncp init` now walks users
  through store backend, infra mode, container engine, ports, and credentials,
  can generate `.ncp/compose.yaml`, optionally start containers, and apply
  migrations for a working pgvector + Redis setup from the CLI.
- **Installed infra lifecycle commands** (`ncp/cli.py`): added `ncp infra up`
  and `ncp infra down` so packaged installs can manage the generated local
  Postgres + Redis stack without depending on repo-only helper scripts.
- **Safer non-interactive pgvector setup** (`ncp/cli.py`): non-TTY
  `ncp init --store pgvector` now defaults to bring-your-own infra instead of
  auto-starting managed containers, and BYO non-interactive setup no longer
  auto-runs migrations.
- **Cursor provider support** (`ncp/adapters/cursor.py`, `ncp/dogfood.py`):
  added Cursor CLI and Cloud Agent adapters to the dogfood/provider surface.
- **Provider permission and review-tooling cleanup** (`ncp/agent_handoff.py`,
  `ncp/dogfood.py`, `scripts/claude_review_stream.py`): replaced broad
  permission bypasses with narrower allowed-tool grants and made tool sets more
  configurable for partner and review flows.
- **Credibility and retrieval follow-ups** (`ncp/benchmarks.py`,
  `ncp/config.py`, `ncp/assembler.py`, `ncp/chunker.py`, `ncp/stores/`):
  improved token counting, retrieval fallback behavior, adaptive budgeting,
  chunking stability, whisper delivery visibility, and generation-penalty
  configurability.

## [1.0.1] - 2026-06-02

Credibility-hardening patch release. No product-surface breaking changes.

### Added / Changed

- **Drift sensor metric** (`ncp/coherence.py`, `ncp/assembler.py`, `ncp/types.py`,
  `ncp/stores/`): upgraded `drift_score` from a threshold alert into a full sensor
  metric. Every turn emits a `sensor`-type whisper (`drift_score_sample`) with the
  raw drift reading; a feedback loop in `_prepare_assembly` drains `world_check`
  whispers and back-propagates `detected_drift` to the next turn's
  `ConsciousBlock.drift_score`; retrieval scores are discounted by
  `written_at_drift` when drift > 0.3; `SubconsciousChunk` has a new
  `written_at_drift` field persisted in both SQLite and pgvector schemas; all
  stores expose a `drift_history` table and `log_drift_history()` method for
  time-series tracking.

- **Realistic pipeline baselines** (`ncp/bench/baselines.py`,
  `ncp/benchmarks.py`): coding and research pipeline benchmarks now report
  three deterministic baseline families instead of only raw replay:
  `raw_replay`, `sliding_window`, and `rolling_summary`.
- **Explicit token unit reporting** (`ncp/benchmarks.py`, `ncp/__init__.py`):
  benchmark artifacts now record whether token counts came from `tiktoken` or
  the fallback `word_split` heuristic in the current environment.
- **Needle recall benchmark** (`benchmarks/needle/run.py`): added a
  retrieval-pressure eval that compares NCP recall against an equal-budget
  sliding window and reports first-eviction timing per planted fact.
- **Assembly-overhead economics** (`ncp/costs.py`, `ncp/benchmarks.py`):
  benchmark artifacts now report a first-pass assembly-overhead estimate and a
  net token-equivalent savings figure instead of treating prompt savings as
  free.
- **Assembler silent-drop visibility** (`ncp/assembler.py`): assembly results
  now expose evicted high-relevance chunks and evicted whispers so drop
  behavior can be inspected explicitly in credibility-focused tests.
- **Docs honesty pass** (`README.md`,
  `docs/NCP_BENCHMARK_CODING_PIPELINE.md`,
  `docs/NCP_BENCHMARK_RESEARCH_PIPELINE.md`,
  `docs/NCP_BENCHMARK_NEEDLE_RECALL.md`,
  `docs/NCP_BENCHMARK_MATCHED_BUDGET_EFFICACY.md`): benchmark docs now separate runtime
  truth from unresolved efficacy questions and document the current benchmark
  gaps more explicitly.
- **WO-3 groundwork** (`docs/NCP_BENCHMARK_MATCHED_BUDGET_EFFICACY.md`,
  `benchmarks/efficacy/TEMPLATE.json`): added the first explicit matched-budget
  real-agent efficacy contract and artifact template without claiming results
  that have not been run yet.
- **Live provider-backed benchmark harnesses** (`benchmarks/efficacy/run.py`,
  `benchmarks/crosshost/run.py`, `benchmarks/retrieval/run.py`): added real
  benchmark execution paths for sliding-window control efficacy, cross-host shared
  context, and labeled retrieval quality, plus focused regression coverage in
  `tests/test_efficacy.py`, `tests/test_crosshost.py`, `tests/test_baselines.py`,
  and `tests/test_retrieval_policy.py`.
- **Scoring fix for rejected-path mentions** (`benchmarks/efficacy/run.py`):
  the live efficacy scorer now distinguishes "mentions a rejected path to avoid
  it" from "proposes a rejected path", preventing false negatives when a model
  correctly says it will not use a dead-end path.
- **Current live evidence** (`README.md`,
  `docs/NCP_BENCHMARK_MATCHED_BUDGET_EFFICACY.md`,
  `docs/NCP_PROVIDER_PARITY_BASELINE.md`):
  - sliding-window control efficacy with `claude-cli`: `NCP 0.8` vs `window 0.0`
  - cross-host shared context with `claude-cli -> opencode-cli`: `NCP 0.8` vs
    `window 0.0`

## [1.0.0] - 2026-06-01

First stable public release of Neural Context Protocol.

This release rolls the `0.2.0` through `0.16.x` development lines into a
coherent V1 product surface:

- local-first SQLite runtime
- scalable pgvector + Redis runtime
- HTTP/SSE MCP runtime
- bounded retrieval, `ncp_fetch`, and whispers
- operator tooling: `status`, `cost`, `explain`, `viz`, `batch`,
  `consolidate`, `calibrate`
- live Podman-backed pgvector + Redis validation
- end-to-end provider handoff proof across Claude and OpenCode

Verification at release cut:

- `575 passed, 8 skipped`
- `python -m build` passes
- live Podman pgvector + Redis integration: `6 passed`

### Added / Changed

- **Handoff timeout reliability** (`ncp/agent_handoff.py`, `ncp/cli.py`):
  provider subprocess timeouts in `ncp handoff claude` / `ncp handoff opencode`
  now surface as clean NCP-owned errors with runner name, timeout budget, and
  prompt size instead of raw Python tracebacks. OpenCode handoff default timeout
  is now `45s`.
- **Regression coverage**: added timeout-path tests in
  `tests/test_agent_handoff.py` and CLI error-surface coverage in
  `tests/test_cli.py`.
- **Guided init setup** (`ncp/cli.py`): `ncp init` now supports explicit
  `--store sqlite|pgvector` selection, defaults safely to `sqlite` in
  non-interactive runs, and prompts in interactive terminals so first-run setup
  can choose between the local-first SQLite path and the scalable pgvector +
  Redis path.
- **Regression coverage**: added CLI init coverage for default SQLite config
  generation and explicit pgvector initialization.

- **Shared vector-aware retrieval scoring** (`ncp/stores/retrieval.py`):
  `RetrievalPolicy.score_with_vector()` now blends lexical relevance with an
  optional vector-similarity signal while preserving the existing trust,
  recency, and generation weighting. When no vector signal is present, the
  policy falls back to the existing lexical-only score.
- **Sync pgvector hybrid tie-break parity** (`ncp/stores/pgvector.py`):
  `PgvectorStore.query(..., retrieval_mode="hybrid")` now auto-embeds query
  text when an embedding adapter is configured, validates 1536-dimension query
  vectors, computes cosine-normalized similarity from stored embeddings, and
  uses that signal to break lexical ties without changing blank-query fallback
  behavior.
- **Async pgvector hybrid tie-break parity** (`ncp/stores/pgvector_async.py`):
  `AsyncPgvectorStore.async_query(..., retrieval_mode="hybrid")` now mirrors
  the sync behavior, including adapter-driven query embedding, 1536-dimension
  validation, cosine-normalized vector scoring, and shared hybrid ranking.
- **Regression coverage**: added focused sync and async tie-break tests so
  identical lexical candidates are ordered by vector similarity in both
  backends:
  - `tests/test_future_stores.py::test_pgvector_hybrid_query_uses_vector_signal_to_break_lexical_tie`
  - `tests/test_async_vector_mode.py::test_async_hybrid_uses_vector_signal_to_break_lexical_tie`
- **Shared retrieval contract helpers** (`ncp/stores/retrieval.py`): added
  `normalize_query_terms()`, `lexical_signal_for_candidate()`,
  `normalize_result_limit()`, and `apply_diversity_limit()` so blank-query
  fallback, zero-overlap lexical gating, result-cap normalization, and
  author-diversity trimming are defined in one place.
- **Store alignment** (`ncp/stores/sqlite.py`, `pgvector.py`,
  `pgvector_async.py`): SQLite, sync pgvector, async pgvector, and vector-mode
  result trimming now all use the shared retrieval helpers instead of carrying
  separate copies of the same contract.
- **Regression coverage**: added retrieval-policy unit coverage for the new
  shared contract helpers in `tests/test_retrieval_policy.py`.
- **Shared lexical candidate generation** (`ncp/stores/retrieval.py`): added
  `build_lexical_candidates()` plus `normalize_bm25_scores()` so BM25
  normalization, blank-query fallback, and zero-overlap candidate eligibility
  are built once and reused by all hybrid lexical backends.
- **Hybrid lexical path alignment** (`ncp/stores/sqlite.py`, `pgvector.py`,
  `pgvector_async.py`): SQLite, sync pgvector, and async pgvector now consume
  the shared lexical candidate helper instead of each rebuilding BM25 scoring
  and eligibility independently.
- **Regression coverage**: added lexical-helper unit coverage in
  `tests/test_retrieval_policy.py`.
- **Shared non-lexical retrieval helpers** (`ncp/stores/retrieval.py`): added
  `score_trust_recency_candidate()` and `score_vector_distance()` so the
  trust/recency-only and vector-distance scoring math are defined in one place.
- **Non-lexical path alignment** (`ncp/stores/pgvector.py`,
  `ncp/stores/pgvector_async.py`): sync and async pgvector retrieval now both
  consume the shared trust/recency and vector-distance helpers instead of
  carrying duplicate scoring math.
- **Regression coverage**: added non-lexical helper unit coverage in
  `tests/test_retrieval_policy.py`.
- **Assembler retrieval-cap boundary cleanup** (`ncp/assembler.py`): chunk and
  whisper caps are now derived once via a shared `_assembly_caps()` helper, so
  pressure-based retrieval limits are forwarded consistently to both
  `store.query()` and `drain_whispers()` instead of being decided once at query
  time and again during post-query trimming. When coherence alerts fully occupy
  the whisper budget, queued whispers now remain pending instead of being
  destructively drained and silently dropped.
- **Regression coverage**: added assembler whisper-cap forwarding coverage in
  `tests/test_assembler_k_forwarding.py`.
- **Public docs cleanup for V1 RC** (`README.md`, `docs/NCP_SETUP.md`,
  `docs/NCP_MCP_DOGFOOD_LOOP.md`, `docs/NCP_ACTIVE_HANDOFF_PACKET.md`):
  setup now documents SQLite vs pgvector + Redis as the two supported runtime
  modes; the README has been rewritten as an NCP-first landing page with
  architecture diagrams; stale orchestration-centric framing has been reduced to
  optional integration-example language; and the active handoff packet has been
  renamed to reflect its real scope.
- **Verification**: suite now passes at `575 passed, 8 skipped`.

## [0.15.x] - 2026-05-31

MACE benchmark plus async pgvector observability parity. No breaking changes.

### Added / Changed

- **MACE benchmark suite** (`benchmarks/mace/`): new reproducible benchmark for
  multi-agent context coordination efficiency with four dimensions:
  token efficiency, handoff quality, dead-end prevention, and goal coherence.
- **D1 integration**: wired to the existing coding pipeline benchmark so token
  efficiency reuses the established data source instead of duplicating a second
  token-growth harness.
- **Deterministic D2-D4 harness** (`benchmarks/mace/harness/`): runs against
  the real NCP SQLite store + assembler path, avoiding provider credentials
  while still exercising chunk retrieval, whisper delivery, conscious-state
  propagation, and dead-end memory.
- **Result artifacts**: `run.py` now writes `benchmarks/mace/results/ncp.json`,
  `baseline.json`, and `traces/ncp_trace.json`, plus a community submission
  template.
- **Docs**: README benchmark section now points to MACE as the end-to-end
  benchmark entry point.
- **Canonical benchmark run**: `python benchmarks/mace/run.py --turns 40`
  currently yields composite `0.9608` with D1 `0.8695`, D2 `1.0000`,
  D3 `1.0000`, D4 `1.0000`.
- **`AsyncPgvectorStore` observability parity** (`ncp/stores/pgvector_async.py`):
  added native async `async_status_detail()`, `async_cost_summary()`, and
  `async_viz_data()` so the async pgvector path now has the same status/cost/viz
  surface as the sync pgvector store without falling back to
  `anyio.to_thread.run_sync`.
- **`AsyncRedisCoordination.async_whisper_stats()`** (`ncp/stores/redis_coordination.py`):
  added native async whisper queue stats with `count`, `last_activity_at`, and
  `by_type`; sync `whisper_stats()` now exposes the same `by_type` rollup.
- **`BaseStore` async reporting wrappers** (`ncp/stores/base.py`): added
  `async_status_detail()`, `async_cost_summary()`, and `async_viz_data()` for
  backend parity.
- **Verification**: suite now passes at `546 passed, 8 skipped`.

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
- **`log_cost` CLI command** in `.ncp/run.py`: exposes `log_cost_raw` to external callers (scripts, host runtimes) via `python3 .ncp/run.py log_cost '{"agent_id":...,"model":...,"input_tokens":...,"output_tokens":...}'`. Turn ID auto-generated if omitted. Output visible in `ncp cost`.
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
