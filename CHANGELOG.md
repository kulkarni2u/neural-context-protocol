# Changelog

All notable changes to Neural Context Protocol will be documented in this file.

## [Unreleased]

### Added

- **WI-P3: deterministic fan-in reduction for high-fanout bursts**
  (`ncp/stores/consolidation.py::reduce_candidates`, `[retrieval].reduce_fanin_enabled`,
  off by default): when many parallel workers write overlapping findings
  into one pipeline, the existing bounded chunk-cap retrieval could fill
  its budget with several near-duplicate restatements of the same claim
  instead of that many distinct ones. When enabled, retrieval overfetches
  past the normal cap, then within any high-fanout `(layer, zone,
  pipeline_id)` cluster: merges near-duplicate claims to the highest-trust
  version (reusing the same clustering `ncp consolidate` already uses),
  drops malformed (empty) candidates, and flags surviving same-topic claims
  that diverge as contradictions — gated on a reversal-cue check (`"already
  fixed"`, `"no longer reproduces"`, ...), not similarity alone, since
  paraphrase variance and genuine contradictions score similarly close on
  short technical claims. Contradictions are surfaced as an additive
  `note:contradicts` line in the assembled context (`PidginEncoder.assemble`'s
  new `contradiction_notes` parameter) for the reading agent to reason
  about; NCP never resolves one itself. `AssemblyResult` gained
  `fanin_merged_count`, `fanin_contradictions`, and
  `fanin_dropped_malformed_count`; `ncp_get_context`'s telemetry and
  `active_features` surface the same, additively. New benchmark
  (`benchmarks/fanin_reduce/`, `ncp.benchmarks.run_fanin_reduce_benchmark`):
  on a deterministic 40-worker/4-topic corpus, 25% of NCP's own bounded
  top-k retrieval slots are near-duplicates of another slot in the same
  result with the reducer off; enabling it merges those away and cuts
  tokens 13% against an unbounded raw dump of all 40 workers (see
  `docs/NCP_BENCHMARK_FANIN_REDUCE.md`). Config: `[retrieval]`
  (`reduce_fanin_enabled`, `reduce_fanin_min_cluster`,
  `reduce_fanin_similarity_threshold`, `reduce_fanin_contradict_floor`,
  `reduce_fanin_overfetch`).
- **Claude Code plugin package** (`claude-plugin/`, `.claude-plugin/marketplace.json`):
  packages the existing MCP registration, `SessionStart` hook, and `/ncp`
  skill from `examples/06_claude_code` as an installable Claude Code plugin
  (`/plugin marketplace add kulkarni2u/neural-context-protocol` then
  `/plugin install ncp@neural-context-protocol`), so zero-touch setup no
  longer requires copying files by hand. The manual `examples/06_claude_code`
  path still works unchanged.
- **GitHub Copilot example** (`examples/11_copilot/`): `.vscode/mcp.json`
  registration and a `.github/copilot-instructions.md` turn contract for
  Copilot Chat's agent mode, matching the pattern of the Codex CLI/OpenCode
  examples. Copilot has no hook/autostart mechanism, so `ncp serve` must
  already be running.
- **Portable Agent Plugin** (`agent-plugin/`): packages NCP to the
  vendor-neutral [Agent Plugins 1.0.0](https://agent-plugins.org) standard
  (`plugin.json` + `mcp.json` + `skills/ncp-core`, `skills/ncp-multi-agent`)
  so the same directory works with any compliant client, not just Claude
  Code. Validated against the published `plugin.schema.json`/
  `mcp.schema.json`. Declares `streamable-http` only — NCP's `serve-stdio`
  is `hidden=True` in the CLI and documented as an internal
  tests/dogfood transport, not a stable public interface, so it's called
  out as a gap rather than shipped as if supported. Complements, and does
  not replace, `claude-plugin/`: the Claude Code plugin keeps its native
  `SessionStart` hook/autostart, which the portable format has no
  equivalent for. See `agent-plugin/README.md` for the full gap list (no
  stdio transport, no autostart, no portable auth-header mechanism).
- **CAP-C8: evidence-backed procedural self-refinement** (`ncp/refine.py`,
  `ncp refine ingest|show|propose|apply|rollback`): NCP's calibration loop
  reweights trust on stored memory but never touches the instructions an
  agent operates under. This closes that gap for a single named "procedure"
  (one chunk-sized block of operating instructions — content is capped at
  2000 chars protocol-wide, so a whole multi-KB contract file isn't the
  refinable unit). `ncp refine propose` deterministically folds recurring
  `ncp_record_outcome` failure notes into an additive, evidence-cited
  proposal (no model call, never edits or removes existing text) and writes
  it as a new low-trust candidate chunk linked to its predecessor via
  `supersedes` — writing a candidate never adopts it. `ncp refine apply`
  is the explicit human-gated adoption step: it promotes the candidate via
  the existing CAP-C5 `supersede()` machinery and existing `calibrate`
  manual-override trust promotion, optionally writing the adopted content to
  a file. `ncp refine rollback` reverts to the prior version by writing a
  *new* generation with the old content — nothing is ever deleted or
  rewritten in place. New additive `BaseStore.list_outcomes` (SQLite) backs
  evidence gathering; `list_chunks` now also returns `generation` and
  `source_refs` so a procedure's version chain can be read even after
  `get_chunks_by_ids`'s bitemporal "current" view has hidden a superseded
  link; `write()` gained an opt-in `allow_duplicate` flag (default `false`)
  so an intentional byte-faithful rollback isn't rejected by the
  noise-suppression dedup guard. Config: `[refine]`
  (`min_failed_outcomes`, `max_bullets`, `promote_trust`).

## [1.4.2] - 2026-08-04

Silent-disconnect audit and a follow-up bug-bounty pass across the whole
codebase (MCP protocol/security, storage, core assembly/retrieval logic, and
adapters/CLI/SDK/dogfood harness). Eighteen findings total, all fixed and
verified with a live repro before/after; full details, evidence, and repro
scripts for each are in `docs/NCP_SILENT_DISCONNECT_AUDIT.md`.

### Security

- **Cross-pipeline authorization bypass via `supersedes`, `edges`, and
  chunk_id reuse** (`ncp/stores/sqlite.py`, `ncp/stores/pgvector.py`,
  `ncp/stores/pgvector_async.py`): `supersede()` and `add_chunk_edges()` had
  no ownership check at all — any caller could pass an arbitrary `chunk_id`
  from a *different pipeline* via `ncp_write_memory`'s `supersedes`/`edges`
  params and silently retire or attach a hostile edge to someone else's
  chunk, ungated by signature verification even with
  `[identity].require_signatures` enabled. Separately, `write()`'s only
  anti-tamper check was that `src` couldn't change for a reused `chunk_id`
  — `written_by` was unchecked, so reusing a known `chunk_id` (returned
  from every write, visible in every retrieval result) could overwrite
  another chunk's content and author in place while keeping full trust.
  `supersede()`/`add_chunk_edges()` now require matching `pipeline_id`;
  `write()`'s immutability check now covers `written_by`. (finding 9)
- **Stdio DoS and an auth-token timing side channel**
  (`ncp/mcp/server.py`): one syntactically valid but non-object JSON
  message (e.g. a bare array) used to be treated the same as a genuine
  framing/header error and permanently killed the whole MCP server
  process, even though the stream stayed perfectly in sync — every
  subsequent message, including well-formed ones, went unanswered. The
  HTTP bearer-token check also used `==` instead of a constant-time
  comparison. Fixed via a dedicated `_MCPMessageShapeError` (treated like
  a recoverable parse error, not stream desync) and
  `hmac.compare_digest`. (finding 10)
- **Redis-backed `ncp_fetch` budget was bypassable under concurrency**
  (`ncp/stores/redis_coordination.py`): `claim_fetch_slot` did a
  read-then-write with no atomicity, so concurrent callers sharing a
  session could both pass the `max 3 per session` check. Now claims via a
  single atomic `HINCRBY` with a compensating revert on overflow.
  (finding 15)

### Fixed

- **`ncp_post_turn`'s batch memory-write path silently corrupted trust
  scores** (`ncp/mcp/server.py`): chunks written via `memory_chunks`
  always got the bare Pydantic default `base_trust=0.7` regardless of
  `src`, instead of the `src`-derived score `ncp_write_memory` computes —
  flattening/mis-ranking a large share of real memory in retrieval.
  (finding 1)
- **Token-budget eviction could report zero evictions while dropping
  everything** (`ncp/assembler.py`, `ncp/mcp/server.py`): the existing
  `evicted_high_relevance`/`evicted_whispers` telemetry is filtered to
  `relevance >= 0.5`/`confidence >= 0.6`; a response with *all* relevant
  content evicted could be indistinguishable from one where nothing
  relevant ever existed. Added unconditional `evicted_chunk_count`/
  `evicted_whisper_count` telemetry fields. (finding 2)
- **Silent, query-blind "frozen injects" from the retrieval fallback**
  (`ncp/assembler.py`, `ncp/stores/sqlite.py`): `Assembler._retrieve_chunks`
  hardcoded `fallback_to_trust_recency=True` on every call, silently
  overriding the store's own off-by-default contract; when hybrid
  retrieval found nothing, the fallback ranked purely by
  trust/recency/generation/drift with zero reference to the query text, so
  any pipeline that drew hybrid blanks got the same top-trust chunks
  injected every turn regardless of what was asked, with no signal this
  had happened. Now gated by `retrieval.fallback_to_trust_recency_enabled`
  (default `true`, unchanged behavior; disabling it degrades to the
  existing cold-start marker, not a bare empty block) and surfaced as
  `telemetry.retrieval_used_fallback`. (finding 8)
- **`ConsciousBlock.calibration_id`/`intent_anchor`/`escalate_to` were
  fully dead** (`ncp/mcp/server.py`), contradicting the protocol spec's
  claim that `calibration_id`'s "logic shipped in 0.4.0" — zero references
  anywhere outside `types.py`. Now threaded through
  `_build_conscious_from_args` the same way `recent`/`tried`/`failed` are;
  `intent_anchor` derives `sha256(task+intent)` on turn 0. (finding 5)
- **Seven `SubconsciousChunk` fields were pure DB round-trips**
  (`ncp/mcp/server.py`): `owner`/`valid_while`/`evidence_id`/`conditions`/
  `result_confidence`/`result_attempts` (plus `caused_by`/`chunk_type` from
  the secondary notes) were validated, persisted, and reconstructed on
  every read but never settable by any of the 5 core tools. Now exposed on
  `ncp_write_memory`'s input schema and threaded into the write path.
  (finding 6)
- **Cold-start bootstrap crashed `Assembler.assemble()` on a legitimate
  long task/intent** (`ncp/assembler.py`): `_cold_start_bootstrap`
  f-string-interpolated `task`/`intent` directly into a chunk with a hard
  2000-char cap; a long-but-valid task/intent on a pipeline's first turn
  raised an unhandled `ValidationError` instead of yielding a truncated
  cold-start signal. (finding 11)
- **Superseded/retracted facts silently reappeared whenever
  `retrieval.edge_expansion` was disabled** (`ncp/assembler.py`):
  supersession suppression was only called from inside the
  edge-expansion branch by accident of code placement, so disabling that
  unrelated feature flag silently disabled fact-supersession too — a
  retracted fact and its correction could both surface in the same
  context with no signal which was authoritative. (finding 12)
- **A malformed `world_check` whisper permanently blocked every
  subsequent valid drift signal in the same batch** (`ncp/assembler.py`):
  `_apply_drift_feedback` unconditionally broke out of the whisper loop
  after the first well-formed `world_check` whisper, regardless of
  whether its `detected_drift` was actually in range — one out-of-range
  signal (e.g. a miscalibrated sensor) silently discarded every real
  drift event queued behind it. (finding 13)
- **`RetrievalPolicy.score()` could exceed its documented `[0, 1]` range**
  (`ncp/stores/retrieval.py`): `bm25_normalized` was only lower-clamped,
  unlike every other input and unlike the sibling `score_with_vector()`.
  Not observably wrong today (every live call site passes pre-normalized
  values) but a real contract violation in a shared, load-bearing
  primitive. Now double-clamped to match. (finding 14)
- **Unbounded per-write duplicate-detection scan** (`ncp/stores/sqlite.py`,
  `ncp/stores/pgvector.py`, `ncp/stores/pgvector_async.py`): every
  `write()` scanned *all* same-`(zone, layer, pipeline_id)` rows with no
  limit; `proven`/`global` zones are never count-capped the way `working`
  is, so a long-lived non-working-zone pipeline made every subsequent
  write strictly slower without bound (confirmed live: ~2ms → 330ms+ over
  2400 writes). Now bounded via a new `retention.dedup_scan_limit` config
  knob (default `200`), mirroring the existing bounded scan already used
  for edge inference. (finding 15)
- **Dogfood harness's JSON-RPC reader could hang forever with a leaked
  subprocess** (`ncp/dogfood.py`): `_read_message`'s blocking pipe reads
  had no timeout, so a `Content-Length` header larger than the bytes
  actually sent (crash mid-write, non-conforming server) blocked
  indefinitely with no way for the surrounding `with` block to reap the
  child. Now runs under a hard deadline via a background daemon thread. A
  related `close()`-ordering deadlock (pipes closed before the process was
  terminated, blocking against a still-reading background thread) found
  while verifying this fix was fixed in the same pass. (finding 16)
- **`MCPHTTPClient` leaked its subprocess when the readiness probe timed
  out** (`ncp/dogfood.py`): the failure happened inside `__enter__`, so
  Python's `with` statement never called `__exit__`/`close()` and the
  spawned process was orphaned, still holding its bound port. `start()`
  now tears the process down before re-raising. (finding 17)
- **`AnthropicAdapter.stream()` let raw provider errors escape unwrapped**
  (`ncp/adapters/anthropic.py`): the actual HTTP request happens inside
  the SDK's `with stream_ctx as stream:` entry, which was outside the
  error-wrapping `_run_provider_call` used, so streaming failures raised
  raw `anthropic.*` exceptions instead of this codebase's
  `NCPAdapterError`/`NCPAdapterTimeoutError` — silently breaking any
  caller written against `except NCPAdapterError`. Now wrapped the same
  way `call()` already is. (finding 18)

### Added

- **Embedding-call cost logging** (`ncp/stores/sqlite.py`,
  `ncp/stores/pgvector.py`, `ncp/stores/pgvector_async.py`): a new
  `embedding_cost_log` table plus `log_embedding_cost()`/
  `embedding_cost_summary()` on all three stores, logged at every
  embedding-adapter call site and wired into `/api/cost` — this spend was
  previously invisible to NCP's own cost accounting entirely. (finding 3)
- **`ncp_get_context` telemetry gains `active_features`**
  (`ncp/mcp/server.py`): surfaces whether `distillation_enabled`,
  `adaptive_budget_enabled`, `memoization_enabled`,
  `drift_computed_enabled`, and `rerank_enabled` are currently on, so a
  caller can tell why a token-efficiency mechanism isn't kicking in
  instead of guessing.
- **Real-subprocess dogfood coverage for `ncp_emit_whisper`/
  `ncp_post_turn`** (`ncp/dogfood.py`): these two tools previously had no
  coverage from the repo's own live-process JSON-RPC harness, only
  in-process unit tests; a new shared scenario exercises both over the
  real stdio and HTTP/SSE transports. (finding 7)

### Changed

- **`distillation.enabled` and `budget.adaptive_budget_enabled` now
  default to `true`** (`ncp/config.py`): both are pure token-efficiency
  mechanisms with no correctness downside for being on, but previously
  required explicit opt-in with no signal they were off by default.
- **Benchmark cost accounting no longer hardcodes embedding spend to
  zero** (`ncp/benchmarks.py`, `ncp/stores/*.py`): `assembly_overhead()`'s
  `embed_tokens` is now a real per-store measurement
  (`embedding_tokens_estimate()`) instead of a literal `0`, so the
  reported "net token-equivalent savings" figure can reflect real
  embedding cost when a benchmark run uses one. (finding 4)

## [1.4.1] - 2026-08-03

### Added

- **Read-only memory visualization web UI** (`ncp/ui/`, `ncp serve`):
  `ncp serve` now hosts a vanilla HTML/CSS/JS UI at `/ui` — no build step, no
  external requests — with a per-agent turn timeline showing whisper
  traffic, a filterable chunk browser (trust badges, tombstone/supersede
  state), a whisper inbox with TTL countdowns, store/cost stats, and an
  interactive memory graph tab rendering `caused_by`/`supersedes`/typed
  chunk edges as inline SVG via a deterministic, seeded force layout (no
  external libraries). Backed by new read-only `GET /api/status|chunks|
  turns|whispers|graph|cost` endpoints — plain JSON, gated by the same
  bearer token as `/mcp`, not exposed as MCP tools — and three additive
  `BaseStore` read helpers (`list_chunks`, `list_whispers`, `list_turns`).
  Documented in `docs/NCP_HTTP_API.md` and the README's "Web UI" section.
- **Public `ncp.eval` module for matched-budget evals**: the matched-budget
  context construction (`ncp`, `sliding_window`, `raw_replay` conditions) and
  negation-aware term scoring that `benchmarks/task_success` and
  `benchmarks/efficacy` use internally are now importable from the installed
  `ncp` package (`ncp.eval.matched_budget_conditions`,
  `ncp.eval.term_appears_unnegated`) instead of living only in `benchmarks/`,
  which does not ship with `pip install`. `benchmarks/task_success/tasks.py`
  re-exports the prior `mentions_dead_end_as_retry`/`NEGATION_MARKERS` names
  for backward compatibility; both benchmarks now share one implementation.
- **Verified, repo-bound handoff lifecycle** (`ncp handoff`, `[handoff]`): the
  Claude and OpenCode wrappers resolve and bind provider execution to the NCP
  project workspace. The wrapper, not the provider model, loads bounded context
  in-process, supplies it separately from untrusted handoff data, persists a
  bounded/redacted completion record, and only then acknowledges source
  whispers. An optional follow-up is emitted only after completion succeeds.
  `[handoff].require_verified` is available with a **default of `false`** (and
  `NCP_HANDOFF_REQUIRE_VERIFIED` override). When enabled—or when
  `[identity].require_signatures` is enabled—only verified pending whispers are
  admitted as handoffs; filtered whispers remain unacknowledged.
- **Configuration-aware MCP catalog** (`[tools].profile`): `"full"` (the
  **default**) advertises the generally available NCP tools, while `"core"`
  exposes only the bounded per-turn lifecycle: `ncp_get_context`,
  `ncp_write_memory`, `ncp_emit_whisper`, `ncp_post_turn`, and `ncp_fetch`.
  The same catalog is used by HTTP and stdio, and tools outside it are not
  callable.
- **Memoization surface alignment** (`ncp_lookup_memo`, `ncp_record_memo`):
  when `[memoization].enabled = false` (the default), memo tools are neither
  advertised nor callable. Direct handler use returns the documented disabled
  result without mutating entries or telemetry. `ncp_record_memo` now accepts
  the same optional `context` used by lookup when deriving its signature, so a
  recorded contextual memo can be retrieved with the matching task and context.
- **Additive structured whispers** (`ncp_emit_whisper`): callers may send
  validated `structured-v1` objects for typed handoff, dissent, alert, and
  world-check payloads. Legacy strings—including the established plain-text and
  JSON normalization paths—remain supported for the current major version;
  structured objects are stored canonically without removing existing callers.
- **Executable-aware provider evidence** (`ncp.dogfood`,
  `benchmarks/context_artifacts`): provider readiness now probes the actual CLI
  executable and records a sanitized version-probe result. Archivable live
  attempts require observed resolved model and CLI metadata; unavailable,
  failed, timed-out, or metadata-incomplete attempts are marked non-archivable
  rather than treated as successful evidence.
- **HotpotQA-style multi-hop benchmark** (`benchmarks/hotpotqa_style/`): a
  synthetic, HotpotQA-shaped multi-hop QA benchmark (8 "bridge" + 7
  "comparison" questions, each needing two gold facts recovered from ~20
  filler/distractor paragraphs), built via the public `ncp.eval` matched-budget
  harness. Not the official HotpotQA dataset or PlugMem's own eval harness —
  huggingface.co, arxiv.org, and cmu.edu are outside this project's reachable
  hosts, so the benchmark reproduces the distractor-setting *shape* with
  fictional entities instead. At the default 300-token matched budget: `ncp`
  100% success (median 279 tokens) vs. `sliding_window` 0% (median 277 tokens)
  vs. unbounded `raw_replay` 100% (778 tokens) — see
  `benchmarks/hotpotqa_style/README.md` for the full scope note and budget
  sweep. Added to the README benchmark table and reproducible-commands list.
- **Subagent token-efficiency & accuracy checklist** (`AGENTS.md`,
  `examples/06_claude_code/skills/ncp/SKILL.md`): documents the levers beyond
  the mandatory `ncp_get_context`/`ncp_write_memory` dispatch template that
  actually reduce token spend and improve retrieval accuracy for dispatched
  subagents — a task-specific `intent`/`query_text` instead of the parent's
  broad task, distilled write-backs instead of raw tool output, correct
  `layer` tagging, `caused_by`/`derived_from` edges when building on a prior
  chunk, sizing the subagent's context budget above the ~50-60 token
  `[NCP:CONSCIOUS]`/`[NCP:BUDGET]` protocol overhead floor, and closing the
  loop with `ncp_record_outcome` once the subagent's work is validated.

### Fixed

- **Path-injection and header-injection hardening on the new `/ui`/`/api`
  surface** (`ncp/mcp/server.py`): addresses four CodeQL alerts (3 high
  path-injection, 1 medium HTTP response splitting) introduced by the web
  UI work above. `_serve_ui_asset` now reduces the request to a bare
  filename (`PurePosixPath().name`, dropping any directory component or
  traversal sequence) and matches it by name against the static root's own
  directory listing, so the path actually opened always originates from the
  filesystem, never from the request — closing the pathlib absolute-join
  case where `root / "/etc/x.css"` resolved to `/etc/x.css`. `_cors_origin`
  now returns the matching operator-configured allowlist entry instead of
  echoing the request's `Origin` header back, so the emitted header value
  can only be a configured string. The static asset directory is flat by
  contract; nested paths now 404.
- **Handoff/whisper/tool-gating review fixes** (`ncp/stores/sqlite.py`,
  `ncp/stores/redis_coordination.py`, `ncp/agent_handoff.py`,
  `ncp/mcp/server.py`): found during review of the verified-handoff work
  above. Broadcast whispers (`target: "*"`) now always go through
  per-recipient delivery tracking instead of an ambiguous no-`agent_id`
  acknowledge falling through to a global delete that would consume the
  broadcast for every recipient; the verified-fetch retry loop in
  `load_handoffs` is capped so an unsigned-whisper flood can't force
  unbounded growth of a single `peek_whispers` call; and `tools_for_config`
  now returns the core tool subset (not the full unfiltered catalog) when
  called with no config.

### Performance

- **`SqliteStore.query()` now uses a single connection per call**
  (`ncp/stores/sqlite.py`): the hybrid/vector/trust_recency retrieval path
  previously opened up to three separate SQLite connections per call (row
  load, reputation blending, retrieval-count update), each paying PRAGMA
  setup cost; `_query_vector` and `_fts_lexical_candidates` now take the
  caller's connection instead of opening their own. The FTS-unavailable
  fallback now catches `sqlite3.Error` directly (equivalent to the prior
  `NCPStoreUnavailableError` wrapping) so fallback behavior is unchanged.
- **Cross-encoder/Cohere client reuse in `Reranker`** (`ncp/stores/rerank.py`):
  `_rerank_local`'s `sentence-transformers` `CrossEncoder` and
  `_rerank_cohere`'s `cohere.Client` are now built once and cached on the
  instance instead of being reconstructed on every `rerank()` call. Local
  cross-encoder loads can take hundreds of ms to seconds; with reranking
  enabled this previously repeated on every single retrieval.
- **O(n²) re-encoding removed from token-budget fitting**
  (`ncp/assembler.py`, `ncp/encoder.py`): `_fit_token_budget` and
  `_fit_whispers_to_budget` previously re-encoded the entire assembled
  context from scratch (conscious + all already-fitted chunks/whispers +
  budget) for every remaining candidate chunk/whisper. `PidginEncoder` gains
  `assemble_from_parts()` plus per-item `_encode_chunk_entry()` /
  `_encode_whisper_entry()` helpers so already-fitted entries and the
  invariant conscious/budget text are encoded once and reused; output is
  byte-for-byte identical. Runs on every `ncp_get_context` call.
- **Incremental MMR selection** (`ncp/stores/retrieval.py`):
  `apply_mmr_selection`'s greedy loop previously rescored every remaining
  candidate against *every* already-selected chunk on every round,
  rebuilding a `BM25Okapi` index per pairwise BM25 fallback comparison. Since
  MMR only needs the max similarity to the selected set, and
  `max(running_max, new_sim)` is identical to recomputing from scratch, each
  round now only scores candidates against the newest selection. Produces
  identical selections with far fewer similarity computations.

### Changed

- **Provider-template right-sizing remains unshipped.** The proposed Claude
  template cleanup was evaluated against its exact post-change wording and was
  reverted after the live gate produced secure refusals, missing lifecycle
  markers, and failures/timeouts. The tracked provider templates therefore
  remain at the Task 8A baseline. No failing live evidence was archived as
  passing evidence. Provider cleanup and an evaluator redesign remain pending.

## [1.4.0] - 2026-07-23

### Added

- **Typed chunk relationship graph** (`ncp/types.py`, `ncp/stores/graph.py`,
  `ncp/stores/base.py`, `ncp/stores/sqlite.py`, `ncp/stores/pgvector.py`,
  `ncp/stores/pgvector_async.py`): new `ChunkEdge` pydantic type and closed-set
  `ChunkEdgeType` enum (caused_by, supersedes, supports, contradicts, refines,
  derived_from). Normalized `chunk_edges` table stores directed edges between
  chunks with weight and authorship. Write-time backfill auto-upserts edges
  matching legacy `caused_by`/`supersedes` scalar columns (authoritative-compatible;
  stale additive-only edge rows are skipped when they disagree with current
  scalar). Store API adds `add_chunk_edges()`/`get_chunk_edges()`/`graph_data()`
  (sync + async variants) with safe no-op defaults on optional stores.
- **`edges` parameter on `ncp_write_memory`** (`ncp/mcp/server.py`): optional
  array of `{dst: chunk_id, type: edge_type, weight?: float}` validated against
  closed edge-type set; unknown types rejected as -32603 error before store write.
  Written via `add_chunk_edges` after chunk write; response includes `edges_written` count.
- **Multi-hop edge expansion in retrieval** (`ncp/assembler.py`, `ncp/config.py`):
  bounded BFS from initially-retrieved chunks up to `[retrieval].edge_max_hops`
  (**default 1**) along types in `[retrieval].edge_expansion_types` (**default
  ["caused_by"]**), with per-hop relevance decay via `edge_expansion_decay`.
  Visited set prevents cycles; budget never widens (expansion redistributes within
  existing token/chunk cap). When `caused_by` is configured, stale edge rows
  (those disagreeing with current chunk scalar) are skipped; legacy-column
  fallback resolves missing-scalar cases. Env overrides: `NCP_EDGE_MAX_HOPS`,
  `NCP_EDGE_EXPANSION_TYPES` (CSV).
- **Multi-hop trust propagation in calibration** (`ncp/stores/calibration.py`,
  all three backends): feedback-driven trust deltas propagate up to
  `[retrieval].propagation_max_hops` (**default 1**) along `caused_by` edges,
  scaled by `propagation_factor ** hop` (default factor 0.5). Cycle-safe via
  per-originator visited set. `user_verified` chunks remain protected.
  New `--propagation-max-hops` kwarg on `ncp calibrate` CLI. Env override:
  `NCP_PROPAGATION_MAX_HOPS`.
- **`ncp graph` CLI command** (`ncp/cli.py`): export typed chunk relationships
  as JSON or Graphviz DOT. Options: `--format json|dot` (default json),
  `--output PATH` (stdout if omitted), `--pipeline-id` scope, `--limit` max nodes.
  JSON includes node list, edge list, and stats (node/edge counts, per-type
  breakdown). DOT renders nodes with trust-band fillcolors (green ≥0.8, amber
  0.5–0.8, red <0.5) and layer labels; edge styles by type (solid for
  caused_by, dashed for supersedes, dotted for others). Stale `caused_by` edges
  filtered before output for "current view" semantics. Summary stats printed to
  stderr as table; stdout is pure JSON/DOT.
- **pgvector migration `012_add_chunk_edges.sql`** (`ncp/migrations/`): versioned
  schema upgrade for pgvector deployments, adding `chunk_edges` table and indexes.
  Managed via `ncp migrate apply/rollback`.
- **Automatic edge inference at write time** (CAP-C7) (`ncp/config.py`,
  `ncp/stores/graph.py`, `ncp/stores/sqlite.py`, `ncp/stores/pgvector.py`,
  `ncp/stores/pgvector_async.py`, `ncp/mcp/server.py`): new `[graph]` config
  block (`infer_edges` bool, **default false**; `infer_similarity_threshold`
  default 0.6; `infer_scan_limit` default 50; `infer_max_edges` default 3;
  env overrides `NCP_INFER_EDGES`, `NCP_INFER_SIMILARITY_THRESHOLD`,
  `NCP_INFER_SCAN_LIMIT`, `NCP_INFER_MAX_EDGES`). When enabled, `write()`/
  `async_write()` scan up to `infer_scan_limit` most-recent same-pipeline
  chunks after backfill and score them against the new chunk's content with a
  deterministic `difflib.SequenceMatcher` ratio (no model calls); matches
  `>= infer_similarity_threshold` become `refines` edges (weight = ratio,
  `created_by = "ncp:inferred"`), capped at `infer_max_edges` by highest
  ratio, excluding self, low-trust `raw_*` backup chunks, empty content, and
  edges that already exist. `ncp_write_memory` response gains an
  `edges_inferred` count when the flag is enabled (field absent when
  disabled, preserving exact legacy behavior by default).
- **Multi-hop outcome credit attribution** (CAP-T3 extension) (`ncp/stores/calibration.py`,
  `ncp/types.py`, all three backends, `ncp/cli.py`): confirmed and made
  visible that outcome-driven trust deltas (from `ncp_record_outcome` /
  `calibrate --feedback`) already take the identical multi-hop `caused_by`
  propagation path as retrieval/dissent deltas (`propagation_max_hops`,
  `propagation_factor ** hop`, cycle-safe, `user_verified` protected) —
  `compute_feedback_updates` folds outcome evidence into each chunk's net
  delta before the shared propagation loop runs, so no separate code path
  was needed. Added attribution surfacing: change-log entries for
  propagated credit are now tagged `reason="outcome_propagation"` when the
  originating delta included outcome evidence, distinct from
  `"trust_propagation"` for retrieval/dissent-only propagation. New
  `FeedbackResult.outcome_propagated` / `CalibrationReport.outcome_propagated`
  counts, and a new "via outcome propagation" row in `ncp calibrate --feedback`
  table output.
- **Temporal graph export** (`ncp/stores/base.py`, `ncp/stores/sqlite.py`,
  `ncp/stores/pgvector.py`, `ncp/stores/pgvector_async.py`, `ncp/cli.py`):
  `graph_data()` gains a keyword-only `as_of` (epoch seconds, **default
  `None`** = current view) filtering nodes with the same CAP-C5 point-in-time
  visibility rule as the other `as_of` query paths and excluding edge rows
  created after `as_of`. New `ncp graph --as-of <epoch|ISO-8601>` (naive
  datetimes treated as UTC); the CLI's stale-`caused_by` filter resolves node
  scalars from the same as-of view, so the export answers "what did the memory
  graph look like at time T."

## [1.3.0] - 2026-07-06

Audit remediation from `docs/NCP_AUDIT_AND_REMEDIATION_PLAN.md`. One work item
(`WI-###`) per commit; each addresses a finding (`F-*`) from the audit.

### Chore

- **Stop committing the efficacy benchmark artifact**
  (`benchmarks/efficacy/efficacy_results.json`, `.gitignore`): remove the
  generated artifact from version control so the git tree stays clean after
  every `run.py` invocation. The smoke test now asserts the artifact is
  *written* rather than matching a committed file. (HK-003)

### Security

- **Opt-in Ed25519 signing and verification of chunk/whisper authorship**
  (`ncp/identity.py`, `ncp/config.py`, `ncp/mcp/server.py`, `ncp/types.py`,
  `ncp/encoder.py`, `ncp/stores/sqlite.py`, `ncp/stores/pgvector.py`,
  `ncp/migrations/008_add_verification_status.sql`): `sign()`/`verify_signature()`
  operate over a canonical `written_by | sha256(content) | pipeline_id` payload;
  `ncp_write_memory` and `ncp_emit_whisper` accept an optional `signature`,
  verify it on ingest, persist the result to a new additive `verified` column
  (pgvector migration `008`), honor `revoked_at`, and expose the verified marker
  in fetch results and the pidgin encoding. Gated behind
  `[identity].require_signatures` (**default false**) so unsigned writes keep
  working exactly as before; enforcement only rejects unverifiable writes/emits
  when the flag is enabled. (CAP-T1, WI-013)

### Added

- **Bi-temporal memory** (`ncp/types.py`, `ncp/stores/base.py`,
  `ncp/stores/bitemporal.py`, `ncp/stores/sqlite.py`, `ncp/stores/pgvector.py`,
  `ncp/stores/pgvector_async.py`, `ncp/assembler.py`, `ncp/mcp/server.py`,
  `ncp/migrations/011_add_bitemporal_columns.sql`): chunks gain nullable
  `valid_from`/`valid_to` (valid time -- when a fact was/is true in the
  world, independent of `created_at`'s transaction time) and `superseded_by`
  (honest supersedence: the replaced chunk is never deleted, only marked).
  `ncp_write_memory` accepts optional `valid_from`/`valid_to` (ISO-8601 or
  epoch seconds) and `supersedes` (an existing `chunk_id`); superseding sets
  `old.superseded_by = new.chunk_id` and `old.valid_to = new.valid_from`
  (or "now"). `ncp_get_context` gains an optional `as_of` param: omitted, it
  returns the currently-valid view (excludes superseded chunks and chunks
  whose `valid_to` has passed); given, it returns the point-in-time view as
  of that transaction time (recorded by then, not superseded by a chunk
  itself recorded by then, valid at that instant) -- "what did we believe as
  of turn N." Implemented identically across `SQLiteStore`, `PgvectorStore`,
  and `AsyncPgvectorStore`. Backward compatible: writes without the new
  params, and all pre-existing rows (`NULL` in the new columns), behave
  exactly as before. See `docs/NCP_PROTOCOL_SPEC.md` §4f. (CAP-C5)
- **`recent_turns` parity for pgvector** (`ncp/stores/pgvector.py`,
  `ncp/stores/pgvector_async.py`): `PgvectorStore.recent_turns` and
  `AsyncPgvectorStore.async_recent_turns` (native async, no thread-pool
  shim) now query `turn_records` for real, mirroring
  `SQLiteStore.recent_turns` exactly (most-recent `limit` rows for the
  pipeline, returned oldest-first). Computed drift (CAP-T5,
  `ncp/drift.py`) previously silently degraded to `0.0` on pgvector
  deployments via `BaseStore`'s empty-list default; it now sees real turn
  history there too.
- **Computed drift signal** (`ncp/drift.py`, `ncp/mcp/server.py`,
  `ncp/config.py`, `ncp/stores/base.py`, `ncp/stores/sqlite.py`):
  `ConsciousBlock.drift_score` was previously pure honor system — an agent
  (or a `world_check` whisper) could assert any value and nothing verified
  it. `ncp_get_context` can now compute drift itself from observable turn
  history: a deterministic Jaccard/BM25-token-overlap score between the
  current task/slot text and a sliding window (`drift_window_turns`,
  **default `5`**) of the pipeline's recent turn summaries (`0.0` = on-topic,
  `1.0` = fully drifted), optionally blended with local-embedding cosine
  distance (`drift_use_embeddings`, **default `false`**; falls back silently
  to the lexical score when `fastembed` is not installed — never a hard
  dependency). Gated by `[drift].drift_computed_enabled` (**default
  `false`**); when enabled, the computed value overrides `drift_score`
  *before* CAP-E3 tiering and CAP-C6 adaptive budget consume it, and the
  response gains a `drift` block (`score`/`method`/`self_reported`/
  `window_turns`) so the divergence between the claimed and computed values
  is visible. Disabled (default) reproduces legacy self-reported behavior
  byte-for-byte. Zero prior turns, empty task text, and very long histories
  all degrade to a safe, deterministic `0.0`/clamped result rather than
  erroring. See the formula in `ncp/drift.py` and
  `docs/NCP_PROTOCOL_SPEC.md` §4e. (CAP-T5, WI-016)
- **Adaptive per-turn context token budget** (`ncp/adaptive_budget.py`,
  `ncp/mcp/server.py`, `ncp/config.py`): `ncp_get_context` can scale its
  effective token budget to turn difficulty instead of always spending
  `[budget].context_token_budget`. When `[budget].adaptive_budget_enabled`
  is true (**default `false`**) and the caller omits `max_tokens`, the
  budget is computed from query length, drift score, budget pressure, and
  slot cadence (all already observable before assembly runs), scaled
  `0.5x`-`1.5x` of the requested budget, further pulled down under CAP-E2 $
  budget pressure, and always clamped to
  `[adaptive_budget_floor_tokens, adaptive_budget_ceiling_tokens]`
  (**defaults `300`/`2000`**). The response gains a `budget_tokens`
  (`requested`/`adjusted`/`reason_factors`) block, present only when the
  feature is enabled. An explicit caller `max_tokens` is always honored
  as-is regardless of this setting. Disabled (default) reproduces legacy
  behavior byte-for-byte. See the formula in `ncp/adaptive_budget.py` and
  `docs/NCP_PROTOCOL_SPEC.md` §4d. (CAP-C6)
- **Model-tiering advisory signal** (`ncp/tiering.py`, `ncp/mcp/server.py`,
  `ncp/config.py`): `ncp_get_context` responses gain a top-level `tier_hint`
  (`"light"`/`"standard"`/`"deep"`), `complexity_signal` (0.0-1.0), and a
  `factors` block exposing every raw input (query length, retrieved chunk
  count/author diversity, drift score, budget pressure, cold-start flag) that
  fed the deterministic formula documented in `ncp/tiering.py` and
  `docs/NCP_PROTOCOL_SPEC.md` §4c. NCP does not route models itself — this
  only hands an orchestrator a defensible, auditable signal for downshifting
  cheap turns to a smaller model. Gated by `[tiering].tier_hints_enabled`
  (**default `true`**). (CAP-E3)
- **Per-pipeline cost governor** (`ncp/budget.py`, `ncp/mcp/server.py`,
  `ncp/config.py`): opt-in `[budget].pipeline_budget_usd` ceiling, classified
  against cumulative recorded spend from the existing CAP-E1 `cost_log` (never
  an estimate) via `budget_warn_fraction` (**default `0.8`**) and
  `budget_enforcement` (**default `"warn"`**: `"off"` | `"warn"` | `"block"`).
  `ncp_get_context` and `ncp_post_turn` surface a `budget` block
  (`spent_usd`/`budget_usd`/`fraction_used`/`status`) whenever a budget is
  configured; in `"block"` mode an already-exceeded pipeline gets a structured
  `{"budget_exceeded": true, ...}` refusal from `ncp_get_context` instead of an
  exception, before any assembly work runs. Unset `pipeline_budget_usd`
  (default) keeps the governor fully off. (CAP-E2)
- **Outcome-calibrated reputation** (`ncp/types.py`, `ncp/stores/base.py`,
  `ncp/stores/calibration.py`, `ncp/stores/sqlite.py`, `ncp/stores/pgvector.py`,
  `ncp/stores/pgvector_async.py`, `ncp/mcp/server.py`,
  `ncp/migrations/009_add_outcomes_table.sql`): new `ncp_record_outcome` MCP
  tool persists task success/failure evidence (`OutcomeRecord`, additive
  `outcomes` table, pgvector migration `009`) keyed by `turn_id` or explicit
  `chunk_ids`. `ncp calibrate --feedback` consumes unconsumed outcomes as the
  primary trust signal (with `[retrieval].usage_prior_weight` scaling the old
  retrieval-count prior) and marks them consumed, so re-running with no new
  outcomes is idempotent. Implemented across all three store backends. (CAP-T3)
- **Reputation-weighted retrieval and whisper gating** (`ncp/stores/retrieval.py`,
  `ncp/stores/sqlite.py`, `ncp/stores/pgvector.py`, `ncp/stores/pgvector_async.py`,
  `ncp/config.py`): `[retrieval].reputation_weight` (**default `0.0`**, off)
  blends each author's Beta-reputation confidence into chunk `base_trust` at
  query time — `(1-w)·base_trust + w·(alpha/(alpha+beta))`, unknown authors
  unchanged — and `[whispers].min_author_reputation` (**default `0.0`**, off)
  makes `drain_whispers` drop whispers whose author's reputation confidence is
  below the threshold. Defaults preserve exact prior behavior. (CAP-T4)
- **Semantic work memoization** (`ncp/stores/memo.py`, `ncp/stores/base.py`,
  `ncp/stores/sqlite.py`, `ncp/stores/pgvector.py`, `ncp/stores/pgvector_async.py`,
  `ncp/mcp/server.py`, `ncp/config.py`): opt-in `ncp_lookup_memo` /
  `ncp_record_memo` MCP tools keyed by a normalized task+context SHA-256
  signature (additive `memo_entries` table). Lookups are gated by
  `[memoization]` config (`enabled` **default `false`**, staleness
  `max_age_hours`, `min_outcome` floor, `allow_unverified`) and are
  advisory-only: the host decides whether a returned memo lets it skip its
  model call. (CAP-C3)
- **Sprint 4.1 cleanup** (`ncp/stores/pgvector_async.py`,
  `ncp/stores/retrieval.py`, `ncp/stores/sqlite.py`, `ncp/stores/pgvector.py`,
  `ncp/cli.py`, `ncp/mcp/server.py`, `ncp/migrations/010_add_memo_telemetry.sql`):
  port CAP-T4 reputation-weighted retrieval to `AsyncPgvectorStore.async_query`
  for sync/async ranking parity; consolidate the blending formula into a single
  shared `blend_trust` helper used by every backend; and surface memoization
  telemetry — memo hits, misses, and estimated tokens saved
  (`SUM(hit_count · output_tokens_est)`, an estimate) — in `ncp status` and
  `ncp_lookup_memo` responses via an additive `output_tokens_est` column and
  `memo_stats` counter table (pgvector migration `010`). Status output is
  unchanged unless memoization is enabled or has data. (S4.1)
- **Local semantic embeddings for the SQLite tier** (`ncp/adapters/embedding.py`,
  `ncp/stores/sqlite.py`, `ncp/stores/factory.py`, `ncp/config.py`): add an
  opt-in `local-embeddings` extra backed by fastembed, persist SQLite chunk
  embeddings in an additive BLOB column, and use brute-force cosine fusion for
  SQLite hybrid/vector retrieval when `[embedding].enabled = true`. Defaults
  stay off; local embeddings fail fast with an install hint when the optional
  extra is missing. (CAP-C4)
- **Redundancy-aware MMR context selection** (`ncp/assembler.py`,
  `ncp/stores/retrieval.py`, `ncp/stores/consolidation.py`, `ncp/config.py`):
  add opt-in Maximal Marginal Relevance selection before token-budget fitting,
  using BM25 similarity by default and embedding cosine when chunk embeddings
  are present. `[retrieval].diversity_lambda` defaults to `1.0` so existing
  relevance ordering is unchanged; `0.7` is documented as a starting point for
  reducing near-duplicate context. (CAP-C1)
- **Query-aware extractive distillation** (`ncp/distill.py`, `ncp/assembler.py`,
  `ncp/encoder.py`, `ncp/config.py`): add opt-in assembly-time distillation for
  oversized chunks that fail the context budget, selecting query-relevant
  sentences/lines without mutating stored content. Distilled chunks carry a
  `distilled:1` pidgin marker, and `[distillation].enabled` defaults to
  `false` so existing fit/drop behavior is unchanged. (CAP-C2)
- **Provider-real matched-budget efficacy benchmark** (`benchmarks/efficacy/`,
  `docs/NCP_BENCHMARK_EFFICACY_LIVE.md`): replace the old single-scenario
  window-control harness with a task-set benchmark that compares `ncp`,
  `sliding_window`, and `rolling_summary` at the same requested budget. The
  default `mock` provider is deterministic and keyless; `anthropic` is live and
  writes an explicit skip artifact when `ANTHROPIC_API_KEY` is unset. The
  harness replaces the old `--continuation-adapter`/`--attempts` flags with
  `--provider`/`--seeds`. (CAP-E4)
- **Real per-provider token and USD cost accounting** (`ncp/api.py`,
  `ncp/adapters/base.py`, `ncp/adapters/*.py`, `ncp/costs.py`, `ncp/types.py`,
  `ncp/stores/sqlite.py`): adapters now capture the provider response's real
  `input`/`output`/`cache_read` token usage (`BaseAdapter.last_usage`), which
  `_build_response` prices via the `[providers]` table instead of fabricating
  telemetry from whitespace word counts and a hardcoded `cost_usd=0.0`. The
  local/mock path stays a `chars/4` estimate flagged via a new additive
  `cost_source` field (`"measured"`/`"estimated"`; defaults to `"measured"`,
  persisted to `cost_log`). Turn ids gain a `uuid4` suffix
  (`turn_{ms}_{uuid}`) so two rapid turns no longer overwrite each other's
  `cost_log` row under `INSERT OR REPLACE`. (CAP-E1)
- **MIT `LICENSE` file** (`LICENSE`): add the MIT license text with the correct
  copyright holder to match the badge, `pyproject` `license` metadata, and README
  footer. (WI-012, F-C5)

### Fixed

- **Migration rollback survives self-dropping DOWN sections**
  (`ncp/stores/migrations.py`): `MigrationRunner.rollback` now deletes the
  `ncp_schema_versions` row *before* executing the DOWN SQL, so a DOWN that
  drops its own schema/version table (e.g. `DROP SCHEMA ... CASCADE`) no longer
  raises `UndefinedTable` on the follow-up DELETE. Both statements share one
  transaction, so success commits together and a failed DOWN rolls back and
  restores the version row.
- **No score double-counting in `effective_score`** (`ncp/types.py`): the
  pidgin/display score now equals the single-application retrieval relevance from
  `RetrievalPolicy.score` instead of re-multiplying recency, trust, and the
  generation penalty a second time, so the displayed score stays comparable to
  the ranking score. (WI-008, F-B6)
- **Idempotent calibration feedback** (`ncp/stores/calibration.py`,
  `ncp/stores/sqlite.py`, `ncp/stores/pgvector.py`,
  `ncp/stores/pgvector_async.py`): `ncp calibrate --feedback` now computes trust
  boosts from the *delta* since the last pass via per-chunk
  retrieval/dissent watermarks, so re-running it with no new activity no longer
  walks `base_trust` monotonically toward 1.0/0.0. Reputation rollup consumes the
  same deltas. (WI-003, F-B1)
- **MCP path honors the config token budget** (`ncp/mcp/server.py`): both
  `Assembler` constructions now receive the loaded `config`, so
  `context_token_budget` applies over `/mcp` even when the client omits
  `max_tokens`. (WI-004, F-B2)
- **SQLite write hardening** (`ncp/stores/sqlite.py`): add `PRAGMA busy_timeout`
  and wrap the check-then-insert `write()` path in `BEGIN IMMEDIATE`; a same-`src`
  rewrite now preserves `created_at`/`retrieval_count`/`dissent_count` instead of
  clobbering them, so concurrent writers no longer raise `SQLITE_BUSY` and
  feedback history survives. (WI-009, F-C1/F-C2)
- **Unauthenticated loopback quickstart** (`ncp/cli.py`,
  `ncp/templates/config.toml.example`): `ncp init` no longer auto-mints an
  `auth_token` for loopback SQLite, so the documented "init → copy config →
  connect" flow no longer 401s against its own server. (WI-010, F-C3)

- **Bounded `ncp_fetch` reads** (`ncp/mcp/server.py`,
  `ncp/stores/redis_coordination.py`): clamp the per-fetch `k` to the schema max
  (4) so a `k=500` request serves at most 4 chunks; report the real Redis-mode
  `fetch_budget_remaining` instead of a hardcoded 3 (new
  `RedisCoordination.fetch_budget_remaining`); and bound the in-memory fetch
  session table with an LRU cap + TTL so rotating `session_id`s can't grow it
  without limit. The per-session cap redesign is deferred (needs verified
  identity, CAP-T1). (WI-005, F-B3)

- **Enforced chunk expiry at read and GC** (`ncp/stores/sqlite.py`,
  `ncp/stores/pgvector.py`, `ncp/stores/pgvector_async.py`): all retrieval read
  paths (`query`, FTS/lexical, vector, `get_chunks_by_ids`, working-zone loads)
  now exclude chunks whose `expiry` has passed (`expiry IS NULL OR expiry >
  now`), and soft/hard GC physically reclaim expired chunks — so an expired
  "proven" fact is no longer served forever. (WI-006, F-B4)

- **Stopped silent data loss** (`ncp/assembler.py`, `ncp/stores/sqlite.py`,
  `ncp/stores/pgvector.py`, `ncp/migrations/007_add_whisper_dissent_target.sql`):
  (a) `Assembler._write_with_retry` now returns whether the chunk was persisted,
  so a deduplication-suppressed write is reported instead of treated as success;
  (b) `dissent_target` is now a real column on the SQLite and pgvector `whispers`
  schema (new migration `007`) and round-trips through emit/read; (c) a
  `world_check` whisper missing `detected_drift` is skipped instead of silently
  zeroing the agent's drift. (WI-007, F-B5)

- **Dedup-suppressed writes surfaced end to end from `post_turn`**
  (`ncp/types.py`, `ncp/assembler.py`, `ncp/mcp/server.py`): `post_turn` and
  `post_turn_async` now collect the chunk_ids whose memory write returned
  `False` into a new `TurnRecord.suppressed_chunk_ids` field, `ncp_post_turn`
  accepts `memory_chunks` and reports `suppressed_chunk_ids` in its response —
  so a host learns which chunks the store's dedup check dropped. (WI-007a, F-B5)

### Security

- **Escaped pidgin wire delimiters** (`ncp/encoder.py`): chunk/whisper content is
  neutralized before assembly so stored text cannot forge `[NCP:...]` section
  headers or counterfeit `src:`/`trust:`/`from:` provenance in another agent's
  assembled context. (WI-014, F-S1)

### Changed

- **Honest benchmark claims** (`README.md`, `benchmarks/mace/README.md`): replace
  the stale headline MACE score with the reproduced composite (0.8915), lead the
  coding-pipeline table with the sliding-window comparison, and annotate each
  benchmark row with a one-line honest caveat instead of deleting it. (WI-001,
  F-A3)
- **Reconcile trust/identity/drift/cost language with reality** (`README.md`,
  `docs/NCP_PROTOCOL_SPEC.md`): document opt-in Ed25519 authorship signing
  (`[identity].require_signatures`, default false) and the `verified` wire marker,
  mark `base_trust`/`drift_score` as self-reported advisory inputs (computed drift
  is future work WI-016), state that reputation is computed/displayed but does not
  yet weight retrieval (CAP-T4/WI-015), and clarify cost telemetry is measured for
  provider adapters and estimated for local/mock; remaining gaps point at the
  north-star roadmap. (WI-002)

### Docs

- **Document the WI-003 counter-reset side effect** (`ncp/cli.py` `trust-drift`
  help, `README.md` operator commands): note that because `ncp calibrate
  --feedback` consumes and resets the per-chunk retrieval/dissent watermarks,
  `ncp trust-drift`'s "most retrieved" view reflects activity since the last
  calibration, not lifetime totals. (HK-002)

### CI

- **pgvector + redis integration job** (`.github/workflows/ci.yml`): add a
  `pgvector-redis` job that runs the durable/coordination tier against
  `pgvector/pgvector:pg16` and `redis:7-alpine` service containers (image tags
  matching `compose.yaml`) with `NCP_RUN_PGVECTOR_INTEGRATION=1`, so the
  previously `importorskip`-ed pgvector store, migration, retrieval-feedback, and
  async coordination tests execute on every PR instead of silently skipping.
  (WI-011, F-C4)

## [1.2.1] - 2026-06-30

### Added

- **Provider session-start setup** (`ncp init`, `ncp/templates/provider_hooks/`):
  interactive setup now detects installed Claude Code, Codex CLI, and OpenCode
  CLIs and asks whether to install the matching NCP hook/setup files. Claude
  gets `.claude` hook and skill assets, Codex gets `.codex/hooks.json` plus a
  session-start hook, and OpenCode gets a project plugin at
  `.opencode/plugins/ncp.js`. Non-interactive setup remains unchanged.
- **Codex and OpenCode examples** (`examples/07_codex_cli/`,
  `examples/09_opencode/`): add runnable session-start hook/plugin examples and
  tests that verify their setup contracts.

### Changed

- **README positioning** (`README.md`): sharpen the public framing around NCP
  as an agent-to-agent memory bus over MCP and document the new provider setup
  flow.

## [1.2.0] - 2026-06-24

### Added

- `POST /mcp` now content-negotiates responses via the `Accept` header: clients requesting `text/event-stream` get the JSON-RPC result as an SSE `message` event (with `ncp_chunk` events when `stream: true`), making `/mcp` a spec-compliant stateless Streamable HTTP MCP endpoint. JSON responses remain the default.
- Add n8n integration example (`examples/08_n8n/`) with an HTTP Request node turn-lifecycle workflow and MCP Client Tool node setup over the Streamable HTTP transport.
- **Ingestion-time content filtering** (`ncp/chunker.py`): `ncp_write_memory` now runs deterministic noise reduction (ANSI strip, consecutive-line dedup with counts, progress-bar/timing boilerplate removal, JSON null/empty pruning) before chunking, so stored chunks carry signal not framing. The response reports `filtered`, `reduction_ratio`, and a `raw_ref`.
- **Reversible `raw_ref` backreferences** (`ncp/types.py`): when filtering reduces content, the unfiltered original is stored as a low-trust chunk and linked from the filtered chunk's new `raw_ref` field, retrievable on demand via `ncp_fetch`. Surfaced in the pidgin wire format and fetch results.
- **Signal-filtering benchmark + doc** (`benchmarks/compression/run.py`, `docs/NCP_BENCHMARK_COMPRESSION.md`): deterministic benchmark measuring ingestion-time noise reduction on a fixed corpus of noisy agent payloads (`chars_div4`). Reports 33% aggregate token reduction (537 → 360), ranging from 68% on duplicate-heavy logs and 59% on null/empty-heavy JSON down to 5% on CLI output and 2% on already-dense stack traces; pass gate is aggregate reduction >= 0.20. Reproduce with `python3 benchmarks/compression/run.py`.
- **1-hop edge-expansion retrieval** (`ncp/assembler.py`, `retrieval.edge_expansion`, default on): after top-k retrieval, the assembler pulls in `caused_by` causal parents (decayed inherited relevance) and suppresses chunks whose superseding chunk is already present. Neighbors compete inside the existing `chunk_cap`/token budget — expansion never widens the assembled context. Adds `BaseStore.get_chunks_by_ids` (graceful empty-list default).
- **Trust propagation along `caused_by` edges** (`ncp/stores/calibration.py`): `calibrate(feedback_mode=True)` now propagates a fraction (`retrieval.trust_propagation_factor`, default 0.5) of a chunk's retrieval-feedback boost one hop to its causal parent, crediting a cause for effects that proved useful. Shared, backend-agnostic helper used by the SQLite, pgvector, and async pgvector stores; `user_verified` parents stay protected.
- **Dissent-driven trust penalties** (`ncp/stores/calibration.py`, `ncp/types.py`): feedback calibration now applies a *net* trust delta per chunk — the retrieval boost minus a dissent penalty (`retrieval.dissent_weight`, default 0.2, saturating at 3 dissents) — and propagates the net delta along `caused_by`, so a cause is debited when its effects are disputed and credited when they prove useful. A new `dissent_count` is incremented via `store.record_dissent(chunk_id)` (and `async_record_dissent`); `ncp_emit_whisper` gains an optional `ref`, and a `dissent` whisper carrying a chunk `ref` debits that chunk. New pgvector migration `005_add_dissent_tracking.sql`.
- **`ncp calibrate --feedback`** (`ncp/cli.py`): the self-improvement pass (retrieval boosts, dissent penalties, and `caused_by` propagation) is now runnable from the CLI, previously reachable only programmatically. New `--feedback-weight`, `--propagation-factor`, and `--dissent-weight` overrides (defaulting from `[retrieval]` config), `--dry-run` preview, and a report that surfaces `Feedback adjusted` counts.
- **`ncp trust-drift`** (`ncp/cli.py`, `ncp/stores/sqlite.py`): trust-drift observability command showing which chunks are gaining trust (most retrieved), which are losing trust (most dissented), trust distribution across bands, feedback activity summary, and recent drift timeline. Supports `--pipeline-id` scoping and `--json-output`. Backed by a new `trust_drift_data()` store method (with async counterpart).
- **`ncp_record_decision` MCP tool** (`ncp/mcp/server.py`): structured decision trace capture. Records what was decided, alternatives considered, rationale, evidence refs, outcome status, and searchable tags as `reasoning_trace` chunks with `caused_by` edges for graph traversal. Foundation for queryable precedents.
- **`ncp precedents` CLI command** (`ncp/cli.py`, `ncp/stores/sqlite.py`): query past decisions by relevance with `--tag` and `--outcome` filters. Answers "show me past decisions like this one and how they turned out." Backed by `query_precedents()` store method (with async counterpart).
- **Local identity and reputation rollups** (`ncp/identity.py`, `ncp/stores/calibration.py`, `ncp/cli.py`): create/list/revoke local Ed25519 identities, store public keys in SQLite/pgvector, keep private keys in a local keystore, and roll feedback trust deltas into per-agent Beta posterior reputation scores. `ncp reputation` shows the current local reputation table.

### Changed

- **README positioning** (`README.md`): refocus the public story on NCP as a Context Engineering Protocol, memory bus, and multi-agent context protocol for durable context, trust, learning, and token capital efficiency instead of leading with compression or token savings.
- **Documentation alignment** (`docs/NCP_SETUP.md`, `docs/NCP_PROTOCOL_SPEC.md`, `docs/NCP_ACTIVE_HANDOFF_PACKET.md`): prefer installed `ncp infra up/down` setup commands, describe turn records as bounded summaries instead of compressed summaries, and mark the old V1 handoff packet as archival.

## [1.1.0] - 2026-06-11

Correctness, MCP-parity, and credibility overhaul from the protocol review
(`docs/NCP_OPTIMIZATION_PLAN.md`). Minor version bump for the behavior changes
below.

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
  currently yields composite `0.8915` with D1 `0.6384`, D2 `1.0000`,
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
