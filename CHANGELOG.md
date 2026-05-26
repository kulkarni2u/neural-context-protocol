# Changelog

All notable changes to Neural Context Protocol will be documented in this file.

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
