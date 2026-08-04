# NCP Silent-Disconnect Audit

## Scope and method

This audit looks for the "pipeline_id" bug class: code that exists, type-checks,
and looks correct in isolation, but is never actually wired into the live data
flow — so it fails silently (wrong/missing data, no exception, no test
failure) rather than loudly.

Every finding below was confirmed at the AST/grep level (write site vs read
site, traced by hand through every producer and consumer of the field/path in
`ncp/`) and, where practical, verified by running the real code — not by
reading docstrings or the protocol spec. Scripts used for verification are
inlined per finding; they were run against the actual `ncp.mcp.server.
make_handlers()` dispatch table (the same handler functions the stdio/HTTP MCP
server calls), not a mock.

Two names from the audit brief that motivated this pass — `Sarathi`'s
transport layer and a `context_adapter.py` module — do not exist in this repo
as such; `Sarathi` appears only as an example orchestrator name in `README.md`.
The equivalent role in this codebase is played by `ncp/mcp/server.py`'s
`make_handlers()` closures, which is where this audit focused.

Findings are ranked silent-first, most-impactful first within that bucket.

## Status: all findings fixed

Every finding and the addendum below has a corresponding fix merged on this
branch (finding 8 was reported after the initial pass, added and fixed in a
follow-up; findings 9-12 came from a separate bug-bounty-style pass over the
core assembly/retrieval logic and were fixed together). Full suite after
integration: **1095 passed, 32 skipped** (the skips are all
pgvector/psycopg/redis/optional-provider-extra tests correctly skipping for
lack of a live service in this environment), plus one pre-existing, unrelated
failure (`tests/test_enhancements.py::test_reranker_cohere_mocked` — `cohere`
isn't in the `dev` extras) confirmed present identically before any of these
changes.

| Finding | Fix |
|---|---|
| 1 — `ncp_post_turn` trust/drift gap | `_handle_post_turn` now derives `base_trust` via `_trust_from_args(item)` and sets `written_at_drift` from `conscious.drift_score`, matching `_handle_write_memory` |
| 2 — Silent eviction | `AssemblyResult` gained unconditional `evicted_chunk_count`/`evicted_whisper_count`, surfaced in `ncp_get_context`'s telemetry alongside the existing relevance/confidence-filtered lists |
| 3 — Embedding spend invisible | New `embedding_cost_log` table + `log_embedding_cost()`/`embedding_cost_summary()` on all three stores (sqlite, pgvector, pgvector_async), wired into `/api/cost` |
| 4 — Benchmark cost accounting | `assembly_overhead()`'s `embed_tokens` is now measured via a per-store running counter (`embedding_tokens_estimate()`) instead of hardcoded `0` |
| 5 — Dead `ConsciousBlock` fields | `escalate_to`/`calibration_id`/`intent_anchor` now threaded through `_build_conscious_from_args` the same way `recent`/`tried`/`failed` etc. are; `intent_anchor` derives `sha256(task+intent)` on turn 0 |
| 6 — Dead `SubconsciousChunk` fields | `owner`/`valid_while`/`evidence_id`/`conditions`/`result_confidence`/`result_attempts`/`caused_by`/`chunk_type` now exposed on `ncp_write_memory`'s input schema and threaded into the write path |
| 7 — No live-process coverage for whisper/post_turn | New shared dogfood scenario exercises `ncp_emit_whisper` + `ncp_post_turn` over both the real stdio and HTTP/SSE MCP transports |
| Addendum — features off by default | `adaptive_budget_enabled` and `distillation_enabled` now default `true`; `ncp_get_context` telemetry gained an `active_features` block |
| 8 — Silent trust/recency retrieval fallback ("frozen injects") | Fallback is now `retrieval.fallback_to_trust_recency_enabled`-gated (default `true`, unchanged behavior) instead of hardcoded, and firing it is surfaced as `telemetry.retrieval_used_fallback` |
| 9 — Cold-start bootstrap crashes `assemble()` on a long task/intent | `_cold_start_bootstrap` now truncates the interpolated content to fit `SubconsciousChunk`'s 2000-char cap instead of raising a `ValidationError` |
| 10 — Superseded facts reappear when `edge_expansion` is disabled | `_suppress_superseded` now runs unconditionally in `_prepare_assembly`, no longer gated on the unrelated `edge_expansion` flag |
| 11 — Malformed `world_check` whisper blocks later valid drift signals | `_apply_drift_feedback`'s `break` now only fires once a signal is actually applied; out-of-range `detected_drift` falls through to the next whisper |
| 12 — `RetrievalPolicy.score()` can exceed its documented `[0, 1]` range | `bm25_normalized` is now double-clamped (`max(0.0, min(1.0, ...))`, matching `score_with_vector`'s existing clamp) |

---

## 1. `ncp_post_turn`'s batch chunk-write path drops trust/drift enrichment

**Status: Fixed.**

- **Location:** `ncp/mcp/server.py:1266-1275` (`_handle_post_turn`), vs.
  `ncp/mcp/server.py:1131-1162` (`_handle_write_memory`) and
  `ncp/mcp/server.py:1691-1700` (`_trust_from_args`)
- **Type:** adapter-gap
- **Severity:** silent — no error, retrieval ranking is just quietly wrong
- **Evidence:**

  `_handle_write_memory` derives `base_trust` from the chunk's declared `src`
  via `_trust_from_args()` (`user_verified` → 0.95, `tool_result` → 0.80, …)
  and sets `written_at_drift` from the caller's last known conscious drift.
  `_handle_post_turn`'s per-item loop builds a much smaller `chunk_kwargs`:

  ```python
  chunk_kwargs: dict = {
      "content": str(item["content"]),
      "layer": str(item["layer"]),
      "src": str(item["src"]),
      "written_by": str(item.get("written_by") or conscious.agent_id),
      "pipeline_id": conscious.pipeline_id,
  }
  ```

  `base_trust` and `written_at_drift` are never set, so every chunk written
  through `ncp_post_turn`'s `memory_chunks` — the batch write path a turn
  actually uses to persist memory — falls back to the bare Pydantic defaults
  (`base_trust=0.7`, `written_at_drift=0.0`) regardless of `src`, even though
  the exact same `src` value would produce a different, correct trust score
  through `ncp_write_memory`. `base_trust` and `written_at_drift` both feed
  directly into `RetrievalPolicy.score` (BM25 + recency +
  `w_trust*base_trust` + generation penalty, and the drift-based discount in
  `ncp/stores/retrieval.py:91-102`), so this silently flattens/mis-ranks a
  large share of real memory.

  Verified by writing the same `src="user_verified"` content through both
  tools and reading it back from the store:

  ```python
  # via ncp_write_memory  -> base_trust: 0.95  (correct per _trust_from_args)
  # via ncp_post_turn     -> base_trust: 0.70  (flattened default, src ignored)
  ```

- **Fix:** call `_trust_from_args(item)` and compute `written_at_drift` the
  same way `_handle_write_memory` does, inside the `chunk_kwargs` loop in
  `_handle_post_turn`.

---

## 2. Token-budget eviction can be 100% silent even in the response telemetry

**Status: Fixed.**

- **Location:** `ncp/assembler.py:143-147`, `:700-750` (`_fit_token_budget`);
  `ncp/mcp/server.py:831-850` (`_context_telemetry`)
- **Type:** untraced-path
- **Severity:** silent
- **Evidence:**

  `Assembler._fit_token_budget` greedily fits chunks/whispers into the token
  budget; anything that doesn't fit (and can't be distilled) is simply left
  out of the returned list — there is no log line, no event, and **no
  eviction table in any store schema** (`sqlite.py`, `pgvector.py`,
  `pgvector_async.py` all lack one; `grep -n "CREATE TABLE" ncp/stores/
  sqlite.py` shows `chunks`, `tombstones`, `whispers`, `turn_records`,
  `conscious_log`, `cost_log`, `drift_history`, `identities`, `reputation`,
  `outcomes`, `memo_entries`, `memo_stats`, `chunk_edges` — nothing for
  evictions).

  The only observability is `evicted_high_relevance` /
  `evicted_whispers` in `ncp_get_context`'s telemetry, and that list is
  filtered to `relevance >= 0.5` / `confidence >= 0.6`
  (`ncp/assembler.py:146,156`). Anything evicted below those thresholds
  leaves **no trace anywhere** — not even a count.

  Repro against the real `ncp_get_context` handler: wrote 8 chunks whose
  content is clearly on-topic for the query, then requested context with a
  deliberately tiny `max_tokens=120`:

  ```
  context returned: only [NCP:CONSCIOUS] + [NCP:BUDGET] blocks
                     (zero [NCP:SUBCONSCIOUS] entries — full eviction)
  telemetry: {'evicted_high_relevance_count': 0, 'evicted_whispers_count': 0, ...}
  ```

  A response with **all** relevant content evicted is byte-for-byte
  indistinguishable, from the caller's point of view, from a response where
  nothing relevant ever existed. (In this repro the surviving chunks'
  `relevance` landed just under the 0.5 cutoff once retrieval was scored
  against 8 similar candidates — BM25 IDF dilution — which is exactly the
  kind of real-world case the threshold is supposed to catch and doesn't.)

- **Fix:** track and surface a total eviction count (or at minimum a boolean
  `any_evicted`) independent of the relevance/confidence gate, so a caller
  can tell "empty because nothing existed" from "empty because the budget cut
  everything."

---

## 3. Production embedding spend is invisible to NCP's own cost accounting

**Status: Fixed.**

- **Location:** `ncp/stores/pgvector.py:419-421,610-611,748-749`;
  `ncp/stores/pgvector_async.py:295-299,681-685,802-806`;
  `ncp/stores/sqlite.py:162-174` (`cost_log` schema)
- **Type:** accounting-gap
- **Severity:** silent
- **Evidence:**

  When a `pgvector`/`pgvector_async` store is configured with a real
  `embedding_adapter`, `store.write()` and `store.query()` both call
  `self._embedding_adapter.embed(...)` — a real, billable API call for any
  hosted embedding provider. This happens on essentially every write and
  every retrieval.

  `cost_log` (`ncp/stores/sqlite.py:162-174`) has no column for it:

  ```sql
  CREATE TABLE IF NOT EXISTS cost_log (
      turn_id TEXT PRIMARY KEY, pipeline_id TEXT, agent_id TEXT NOT NULL,
      model TEXT NOT NULL, input_tokens INTEGER NOT NULL,
      output_tokens INTEGER NOT NULL, cache_read_tokens INTEGER DEFAULT 0,
      cost_usd REAL NOT NULL, latency_ms INTEGER, logged_at REAL NOT NULL,
      cost_source TEXT NOT NULL DEFAULT 'measured'
  );
  ```

  This table is only ever populated from `ncp_post_turn`'s
  `NCPResponse.cost_usd` — i.e. the orchestrator-supplied *conversational*
  model cost. There is no write path from any embedding adapter into
  `cost_log` or anywhere else. `ncp/costs.py`'s `assembly_overhead()` does
  model an embedding-cost term (`embed_token_cost_usd`), but it is
  benchmark-only code (see finding 4) — never called from the live
  write/query paths.

  Net effect: a production deployment paying real per-call embedding costs
  has no NCP-side signal that those costs exist at all, let alone how large
  they are relative to the LLM-turn cost it does track.

- **Fix:** log embedding calls (adapter name, char/token count, and price if
  known) to a new `cost_log` column or a parallel `embedding_cost_log` table
  at the two call sites in `pgvector.py`/`pgvector_async.py`, and fold that
  into any pipeline cost/efficiency reporting.

---

## 4. The headline compression-ratio figures are a pure read-side count; the one write-side netting mechanism is neutered by a hardcoded zero

**Status: Fixed.**

- **Location:** `ncp/benchmarks.py:264-270`; `ncp/costs.py:74-98`
  (`assembly_overhead`)
- **Type:** accounting-gap
- **Severity:** silent
- **Evidence:**

  The headline "Nx" reduction figures quoted in `README.md` and
  `docs/NCP_BENCHMARK_*.md` (13.13x, 8.03x, 1.44x, 12.27x, …; the project's
  own `docs/NCP_OPTIMIZATION_PLAN.md:57` already flags an even older
  "17.52x" figure as stale/inconsistent) come from:

  ```python
  reduction_factor = round(final_naive / final_ncp, 2) if final_ncp else 0.0
  ```

  — a pure ratio of two `estimate_tokens()` counts (`chars/4`), with no
  adjustment for any cost incurred to produce the smaller side. The
  `material_reduction` and `pass` gates (`ncp/benchmarks.py:303-309`) are
  both computed from this unadjusted `reduction_factor`.

  There **is** a deliberate netting attempt —
  `assembly_overhead()`/`AssemblyOverheadBreakdown`
  (`ncp/costs.py:74-98`), which has a real `embed_token_cost_usd` term meant
  to represent embedding spend — and its output (`net_total_token_equivalent
  _vs_raw_replay`) is even printed in `docs/NCP_BENCHMARK_CODING_PIPELINE.md`
  ("net total token-equivalent savings vs raw replay: `56230.67`"). But its
  one and only call site hardcodes the input to zero:

  ```python
  overhead = assembly_overhead(embed_tokens=0, retrieval_ops=turns, whisper_writes=0)
  ```

  `embed_tokens=0` is accurate for this specific benchmark (`SQLiteStore`
  with no embedding adapter configured, so no embeddings are actually
  generated in the coding/research pipeline benchmarks) — but the value is a
  literal, not derived from the store's actual embedding usage. If this same
  helper were reused for a `pgvector`-backed run (where embeddings genuinely
  are generated per write/query, see finding 3), the "net" figure would
  still silently report zero embedding cost. The field exists, the formula
  exists, the number is even printed in a doc — but nothing in the codebase
  can make it non-zero.

  Separately: even where it's computed, `net_total_token_equivalent_vs_raw_
  replay` is a secondary line in the `economics` sub-block, not the number
  gated on (`pass`) or the number headlined in `README.md`'s comparison
  table — those still use the gross, unadjusted `reduction_factor`.

- **Fix:** derive `embed_tokens` from the store's actual embedding call count
  (0 is fine when no adapter is configured, but it should be measured, not
  assumed) so the term can ever be non-zero when it matters, and use the
  netted figure — not just the gross ratio — as the number that's gated on
  and headlined.

---

## 5. `ConsciousBlock.calibration_id` / `intent_anchor` / `escalate_to` are fully dead — contradicting the protocol spec's own claim

**Status: Fixed.**

- **Location:** `ncp/types.py:82` (`intent_anchor`), `:88` (`escalate_to`),
  `:96` (`calibration_id`); `docs/NCP_PROTOCOL_SPEC.md:96,105,115`
- **Type:** dead-write / dead-read
- **Severity:** silent
- **Evidence:**

  All three fields have zero references anywhere in `ncp/` outside
  `ncp/types.py` itself:

  ```
  $ grep -rn '\bcalibration_id\b\|\bintent_anchor\b\|\bescalate_to\b' ncp/*.py ncp/mcp/*.py ncp/stores/*.py
  (no output)
  ```

  No producer ever sets them (they stay at their `None` default forever), no
  consumer ever reads them, and `ncp/encoder.py`'s `_encode_conscious` (the
  only place `ConsciousBlock` fields get serialized into the wire format)
  never emits them either.

  This isn't merely undocumented-and-unused — the protocol spec explicitly
  claims otherwise:

  ```
  intent_anchor   str?  = None    sha256 of original intent at turn 0
  escalate_to     str?      = None
  calibration_id  str?  = None    field present, logic shipped in 0.4.0
  ```

  "logic shipped in 0.4.0" is not true of the code as it stands: there is no
  sha256-of-intent computation anywhere (the only `sha256(...)` calls in
  `ncp/assembler.py:333,371` hash the whole conscious block for
  `conscious_hash`, an unrelated, correctly-wired field), and
  `ncp/stores/calibration.py`'s `CalibrationReport` never writes a
  correlating id back onto any `ConsciousBlock`.

- **Fix:** either wire these up (hash `task+intent` at turn 0 into
  `intent_anchor` in `_build_conscious_from_args`; tag chunks/conscious
  snapshots with the calibration pass id that last touched them) or remove
  the fields and correct the spec — as written, anyone reading `types.py` or
  the spec reasonably believes these are live.

---

## 6. Seven `SubconsciousChunk` fields are pure DB round-trips — never set by any producer, never read by any consumer

**Status: Fixed.**

- **Location:** `ncp/types.py:191,197-203,210` (`evidence_id`,
  `result_confidence`, `result_attempts`, `conditions`, `valid_while`,
  `owner`, `schema_version`); store layer in `ncp/stores/sqlite.py:391-451`,
  `ncp/stores/pgvector.py:442-507`, `ncp/stores/pgvector_async.py:320-383`
- **Type:** dead-write (round-trip only)
- **Severity:** silent
- **Evidence:** every one of these fields is declared in the schema, present
  in every `INSERT`/`UPDATE ... EXCLUDED`/`SELECT` in all three store
  backends, and reconstructed on read from the DB row — but no producer in
  `ncp/mcp/server.py`, `ncp/memory.py`, `ncp/batch.py`,
  `ncp/agent_handoff.py`, `ncp/benchmarks.py`, `ncp/demo.py`, or `ncp/eval.py`
  ever sets a real value for `owner`, `valid_while`, `schema_version`,
  `result_confidence`, `result_attempts`, or `evidence_id` (`conditions` is
  set only inside `ncp/eval.py`'s own synthetic scenario replay, never by any
  live write path), and no consumer reads any of them for scoring, filtering,
  or rendering logic (`grep` for `.field` usage outside the store INSERT/
  SELECT boilerplate returns nothing).

  Verified against the real `ncp_write_memory` handler → SQLite store →
  query round trip:

  ```
  'owner'              : None
  'valid_while'        : None
  'schema_version'     : 1
  'result_confidence'  : None
  'result_attempts'    : None
  'evidence_id'        : None
  'conditions'         : []
  ```

  (Contrast with `SubconsciousChunk.distilled` and `.raw_ref`, which *are*
  wired: `ncp/assembler.py:783` sets `distilled=True` via `model_copy`, and
  `ncp/mcp/server.py:1148-1160` sets `raw_ref` on the filtered-content path —
  both round-trip correctly *and* get read back for real behavior in
  `ncp/encoder.py:164-169`. The seven fields above have the write-and-read
  side of that pattern missing entirely.)

- **Fix:** either expose these on the `ncp_write_memory`/`ncp_post_turn`
  input schemas and thread them into the handler `kwargs`, or drop the
  columns/fields — as-is they look load-bearing (they're validated,
  persisted, and reconstructed on every read) but carry zero information.

---

## 7. `ncp_emit_whisper` and `ncp_post_turn` are never exercised by the repo's own live-process integration harness

**Status: Fixed.**

- **Location:** `ncp/dogfood.py` (real subprocess JSON-RPC client, run via
  `ncp dogfood` / `ncp/cli.py:1068-1149`)
- **Type:** untraced-path
- **Severity:** silent (absence of exercise, not a broken assertion)
- **Evidence:**

  All 5 core tools are wired into the dispatch table
  (`make_handlers`, `ncp/mcp/server.py:754-1587`) and reachable through the
  real stdio/HTTP JSON-RPC server — none is "defined with no call sites" in
  the strict sense. All 5 are also exercised in unit tests via direct,
  in-process handler-dict calls (`tests/test_mcp_server.py` and others).

  But the one piece of code in this repo that actually spawns the real MCP
  server as a subprocess and speaks JSON-RPC framing to it end-to-end
  (`ncp/dogfood.py`, invoked manually via `ncp dogfood` — not run in CI;
  `.github/workflows/ci.yml` has no reference to it) only ever calls
  `ncp_write_memory`, `ncp_get_context`, and `ncp_fetch`:

  ```
  $ grep -n '"ncp_write_memory"\|"ncp_get_context"\|"ncp_fetch"\|"ncp_emit_whisper"\|"ncp_post_turn"' ncp/dogfood.py
  ... ncp_write_memory (5x) / ncp_get_context (5x) / ncp_fetch (2x) ...
  # ncp_emit_whisper, ncp_post_turn: zero matches
  ```

  Same story in `examples/*.py` and `scripts/*.py`: none reference any of
  the 5 MCP tool names at all — they go through the separate `ncp/api.py`
  SDK (`agent`/`run`/`get_context`/`write_memory`/`emit`), which calls
  `Assembler`/`store` directly and bypasses `mcp/server.py`'s handlers
  entirely (no content filtering, no edge inference, no drift override, no
  adaptive budget, no memoization dedup, no signature verification). So
  every example in the repo demonstrates the SDK surface, not the MCP
  protocol surface.

  Net effect: a protocol-framing, response-shape, or wire-encoding bug
  specific to `ncp_emit_whisper` or `ncp_post_turn` would pass unit tests
  (which call the handler function directly, in-process) but has no
  real-subprocess coverage anywhere in this repo to catch it before a host
  integration does.

- **Fix:** add a dogfood scenario that emits/drains a whisper via
  `ncp_emit_whisper` and closes a turn via `ncp_post_turn` over the real
  subprocess stdio transport, the same way the existing scenarios do for the
  other three tools.

---

## 8. Retrieval's trust/recency fallback silently produces query-blind, near-identical injects

**Status: Fixed.**

- **Location:** `ncp/assembler.py:500` (`_retrieve_chunks`, was a hardcoded
  `fallback_to_trust_recency=True`); `ncp/stores/sqlite.py:710-733` (the
  fallback path); `ncp/stores/retrieval.py:149-165` (`score_no_bm25`)
- **Type:** adapter-gap / untraced-path
- **Severity:** silent — no error, and the response is indistinguishable from
  a genuine relevance-ranked result
- **Evidence:** flagged externally by a user triaging retrieval bugs against
  this repo (attributed to "NCP core," not their integration) and confirmed
  here by tracing the actual code and reproducing it.

  `SQLiteStore.query()`'s `fallback_to_trust_recency` parameter defaults to
  `False` at the store layer, with an explicit comment: *"Off by default to
  preserve the hybrid filtering contract."* But `Assembler._retrieve_chunks`
  — the only code path any real `ncp_get_context`/`ncp_post_turn` call goes
  through — unconditionally passed `fallback_to_trust_recency=True` on every
  single call, silently overriding that contract for 100% of real usage.

  When the primary hybrid pass (BM25 + optional vector) finds zero
  candidates for a query — which happens whenever the query's vocabulary has
  no lexical overlap with stored content, an easy case to hit — the fallback
  ranks every other chunk by `score_no_bm25`: `w_recency*recency +
  w_trust*base_trust`, gated by generation/drift penalties. This formula
  contains **no reference to the query text at all**. So for any pipeline
  where hybrid retrieval keeps drawing blanks (e.g. terse or vocabulary-poor
  task/slot text), every turn injects the *same* top-trust/most-recent
  chunks regardless of what was actually asked — "frozen injects" — and
  nothing in the response distinguished this from a real relevance-ranked
  result: same wire format, same-looking `score:`/`trust:` fields.

  Verified against the real `ncp_get_context` handler: seeded 3 chunks about
  unrelated topics, then queried with three different, mutually unrelated
  strings designed to share no vocabulary with the content or each other:

  ```
  query 0: 'zzz_unrelated_topic_one'      -> [NCP:SUBCONSCIOUS] block: apple, banana
  query 1: 'qqq_totally_different_topic'  -> [NCP:SUBCONSCIOUS] block: apple, banana  (identical)
  query 2: 'xxx_yet_another_topic'        -> [NCP:SUBCONSCIOUS] block: apple, banana  (identical)
  ```

  All three `[NCP:SUBCONSCIOUS]` blocks were byte-for-byte identical despite
  the queries sharing no vocabulary — and the response gave no indication
  this had happened.

  (Note: the sync `PgvectorStore.query()` accepts the same
  `fallback_to_trust_recency` parameter in its signature but never
  implements the fallback logic at all — so this specific "frozen injects"
  pollution is SQLite-specific today; on pgvector the parameter is
  currently a silent no-op instead, which is a smaller, different gap left
  out of scope here.)

- **Fix:**
  1. Added `retrieval.fallback_to_trust_recency_enabled` to `ncp/config.py`
     (default `True`, preserving prior behavior) and threaded it through
     `Assembler.__init__`/`_retrieve_chunks` instead of the hardcoded
     literal, so integrators who'd rather get an honest empty result (which
     gracefully degrades to the pre-existing `_cold_start_bootstrap` marker,
     not a bare empty block) can disable it.
  2. Added a `retrieval_fallback_count()` running counter to `SQLiteStore`,
     incremented whenever the fallback actually returns candidates; the
     assembler diffs it around each `_retrieve_chunks` call to know whether
     *this turn's* retrieval used it, threaded through `AssemblyResult.
     retrieval_used_fallback` into `ncp_get_context`'s telemetry as
     `retrieval_used_fallback: bool` — so a caller/agent can now tell
     "this context is genuinely query-relevant" from "this is trust/recency
     filler, treat it accordingly" instead of the two being indistinguishable.

---

## 9. Cold-start bootstrap crashes `Assembler.assemble()` on a legitimate long task/intent

**Status: Fixed.**

- **Location:** `ncp/assembler.py:476-495` (`_cold_start_bootstrap`), vs.
  `ncp/types.py:265-270` (`SubconsciousChunk._content_within_limit`)
- **Type:** untraced-path — a validator boundary two layers away from the
  code that violates it
- **Severity:** loud where it fires (an unhandled exception, not silent data
  loss), but the trigger condition itself is silent: nothing signals that a
  normal, valid `task`/`intent` pair is close to blowing up the very first
  turn of a pipeline
- **Evidence:**

  On a pipeline's first turn (zero retrievable chunks), `_prepare_assembly`
  calls `_cold_start_bootstrap`, which builds a synthetic filler chunk by
  f-string-interpolating the conscious block directly into `content`:

  ```python
  content=f"pipeline_summary agent:{conscious.agent_id} task:{conscious.task} intent:{conscious.intent}"
  ```

  `SubconsciousChunk.content` has a hard `field_validator` cap of 2000
  characters (`ncp/types.py:265-270`). `ConsciousBlock.task`/`.slot`/`.intent`
  have no length limit at all — only a no-whitespace check
  (`_validate_no_spaces`). Nothing prevents a caller from legitimately
  passing a long `task` or `intent` (e.g. an orchestrator forwarding a
  detailed multi-paragraph goal on turn 0, before any memory exists to
  narrow it down). Once `len(agent_id) + len(task) + len(intent)` exceeds
  roughly 1980 characters, `SubconsciousChunk(...)` raises a pydantic
  `ValidationError` that propagates out of `assemble()` uncaught — the
  entire `ncp_get_context` call fails instead of just omitting/truncating
  the synthetic filler chunk that exists purely to give a cold pipeline
  *some* signal.

  Reproduced directly:

  ```python
  conscious = ConsciousBlock(agent_id="agentA", role="worker", owns=[], must_not=[],
      task="x"*1200, slot="s", intent="y"*1200, pipeline_id="pipe_empty_cold2")
  asm.assemble(conscious=conscious, budget=BudgetContext(), query_text="anything", max_tokens=5000)
  # -> pydantic.ValidationError: content must be <= 2000 characters
  ```

- **Fix:** build the interpolated string first and truncate it to 1900
  characters plus a trailing `...[truncated]` marker before constructing the
  `SubconsciousChunk` if it would otherwise exceed the 2000-char cap, so a
  legitimate long task/intent still yields a (truncated) cold-start signal
  instead of an exception. Regression coverage:
  `tests/test_assembler.py::test_cold_start_bootstrap_truncates_long_task_intent_instead_of_crashing`
  and `::test_cold_start_bootstrap_leaves_short_task_intent_untouched` (the
  common case must render byte-for-byte unchanged).

---

## 10. Superseded/retracted facts silently reappear whenever `retrieval.edge_expansion` is disabled

**Status: Fixed.**

- **Location:** `ncp/assembler.py:122-127` (`_prepare_assembly`)
- **Type:** adapter-gap — a correctness mechanism gated on an unrelated
  feature flag purely by accident of code placement
- **Severity:** silent — no error, and a retracted fact and its correction
  both get injected into context with identical scores and no signal which
  one is authoritative
- **Evidence:**

  `_suppress_superseded` — the mechanism that drops a chunk once its
  declared successor (`supersedes`) is also present in the candidate set —
  was only ever called from inside `if self._edge_expansion:`:

  ```python
  if self._edge_expansion:
      expanded = self._expand_edges([*recent_chunks, *subconscious], limit=chunk_cap, as_of=as_of)
      subconscious = [*subconscious, *expanded]
      recent_chunks, subconscious = self._suppress_superseded(recent_chunks, subconscious)
  ```

  Supersession suppression has nothing conceptually to do with multi-hop
  edge-hop expansion — it's a distinct correctness guarantee (never surface
  a fact alongside its own retraction) that happened to be wired inside the
  same `if` block as an unrelated performance/recall feature. `edge_expansion`
  is a supported, non-default-off config toggle
  (`retrieval.edge_expansion`, `NCP_EDGE_EXPANSION` env var); any integrator
  who disables it for cost/latency reasons silently loses fact-supersession
  entirely as a side effect.

  Confirmed live: writing `fact_v1`, then `fact_v2` with
  `supersedes="fact_v1"`, and assembling with `retrieval.edge_expansion=False`
  returned **both** chunks, scored identically, directly contradicting each
  other in the same context — with no indication either was stale.

- **Fix:** moved the `_suppress_superseded(recent_chunks, subconscious)` call
  out of the `if self._edge_expansion:` block so it always runs on
  `[*recent_chunks, *subconscious]` regardless of the edge-expansion setting;
  when edge expansion is off, it now runs against the same list it would
  have without the (skipped) edge-expanded append, so disabling
  edge_expansion still disables *only* edge expansion. Regression coverage:
  `tests/test_assembler.py::test_supersession_suppressed_even_when_edge_expansion_disabled`.

---

## 11. A malformed `world_check` whisper permanently blocks every subsequent valid drift signal in the same batch

**Status: Fixed.**

- **Location:** `ncp/assembler.py:905-926` (`_apply_drift_feedback`)
- **Type:** untraced-path — a range check that guards the wrong thing
- **Severity:** silent — no error, and a real drift event goes completely
  undetected for the rest of that turn's whisper queue
- **Evidence:**

  The loop over drained whispers unconditionally `break`s after processing
  the first well-formed `world_check` whisper (valid JSON/dict payload
  containing a `detected_drift` key) — regardless of whether
  `detected_drift` actually fell inside `[0.0, 1.0]`:

  ```python
  detected_drift = float(data["detected_drift"])
  if 0.0 <= detected_drift <= 1.0:
      conscious = conscious.model_copy(update={"drift_score": detected_drift})
  break  # <-- fired even when the range check above was false
  ```

  `WorldCheckPayload.detected_drift` has no range validator of its own, so
  an out-of-range value (e.g. a buggy or miscalibrated sensor emitting
  `5.0`) is legitimate, well-formed input that reaches this code. One such
  whisper permanently blocks every subsequent `world_check` whisper in that
  turn's drained batch from ever updating drift, since the loop has already
  exited.

  Reproduced directly:

  ```python
  w1 = Whisper(whisper_type="world_check", payload={"anchor_intent": "i", "detected_drift": 5.0}, ...)   # out of range
  w2 = Whisper(whisper_type="world_check", payload={"anchor_intent": "i", "detected_drift": 0.85}, ...)  # valid
  asm._apply_drift_feedback(conscious, [w1, w2]).drift_score
  # -> 0.0 (unchanged default), expected 0.85
  ```

- **Fix:** moved the `break` inside the `if 0.0 <= detected_drift <= 1.0:`
  branch so it only fires once a signal was actually applied; an
  out-of-range value now falls through to `continue` and the next whisper is
  checked. Regression coverage:
  `tests/test_assembler.py::test_apply_drift_feedback_skips_out_of_range_signal_and_applies_next_valid`
  and `::test_apply_drift_feedback_stops_at_first_applied_valid_signal` (the
  early-exit-on-success behavior is preserved, not just removed).

---

## 12. `RetrievalPolicy.score()` can return values outside its documented `[0, 1]` range

**Status: Fixed.**

- **Location:** `ncp/stores/retrieval.py:84-109` (`RetrievalPolicy.score`)
- **Type:** contract violation in a shared primitive — not observably wrong
  today, but load-bearing for the rest of the ranking pipeline
- **Severity:** silent — no error, no test previously caught it; every live
  call site happens to pass pre-normalized `bm25_normalized` today, so
  nothing currently downstream breaks, but the guarantee itself was false
- **Evidence:**

  `score()`'s docstring states "Returns a value in `[0, 1]`." `base_trust`
  is double-clamped (`max(0.0, min(1.0, base_trust))`), and the sibling
  `score_with_vector()` even double-clamps `bm25_normalized` itself
  (`max(0.0, min(1.0, bm25_normalized))`) before delegating to `score()` for
  the no-vector case. But `score()`'s own `bm25_normalized` handling only
  lower-clamped:

  ```python
  fused = (
      self.w_lexical * max(0.0, bm25_normalized)  # no upper bound
      + self.w_recency * recency
      + self.w_trust * max(0.0, min(1.0, base_trust))
  )
  ```

  Reproduced directly: `RetrievalPolicy().score(bm25_normalized=5.0,
  age_seconds=0, base_trust=1.0, generation=0)` returned `3.0`, not the
  documented `<= 1.0`.

- **Fix:** clamp `bm25_normalized` the same way `score_with_vector` already
  does: `max(0.0, min(1.0, bm25_normalized))`. Regression coverage:
  `tests/test_retrieval_policy.py::test_score_clamps_bm25_normalized_above_one`.

---

## Secondary notes (not independent findings, lower severity)

- **`SubconsciousChunk.caused_by` (scalar) has no input on the two most-used
  core write tools.** Neither `ncp_write_memory` nor `ncp_post_turn` expose
  `caused_by` in their `inputSchema` (`ncp/mcp/server.py:121-188,366-407`);
  the only ways to set the scalar are the non-core `ncp_record_decision`
  tool, or `ncp_write_memory`'s `edges` array (which creates a `ChunkEdge`
  row that `ncp/stores/graph.py:35` `resolve_caused_by_fallback` lets
  calibration fall back to). `ncp/types.py:34-37`'s own comment says the
  scalar column is supposed to be "authoritative" and the edge row an
  "additive... mirror" — in practice, for the 5 core tools, it's the
  reverse: the mirror is the only thing that ever gets populated. Not a
  functional break (the fallback works and is exercised by
  `ncp/stores/calibration.py`), but worth noting since it inverts the
  documented authority relationship.

- **`SubconsciousChunk.chunk_type`** is fully wired and used for real
  chunking logic in `ncp/memory.py`/`ncp/chunker.py` (the `ncp_remember`
  tool path), but is not exposed on `ncp_write_memory`'s or
  `ncp_post_turn`'s input schema either — every chunk written through the 5
  core tools defaults to `chunk_type="prose"` regardless of actual content
  shape, even though the type is genuinely used elsewhere in the codebase.

---

## Verification scripts

The following were run against a real `SQLiteStore` + `ncp.mcp.server.
make_handlers()` (the actual dispatch table backing the stdio/HTTP MCP
server) inside a throwaway venv (`pip install -e .`), not against mocks.

**Finding 6 — dead-field round trip:**

```python
from ncp.stores.sqlite import SQLiteStore
from ncp.mcp.server import make_handlers

store = SQLiteStore(db_path)
handlers = make_handlers(store)
resp = handlers["ncp_write_memory"]({
    "content": "the sky is blue", "layer": "semantic", "src": "user_verified",
    "written_by": "test_agent", "pipeline_id": "pipe_1", "base_trust": 0.9,
})
chunk = [c for c in store.query(text="sky is blue", k=5, pipeline_id="pipe_1")
         if c.chunk_id == resp["chunk_id"]][0]
for field in ["owner", "valid_while", "schema_version", "result_confidence",
              "result_attempts", "evidence_id", "conditions"]:
    print(field, getattr(chunk, field))
# -> all default/None/[] after a full write/query round trip
```

**Finding 1 — post_turn trust gap:**

```python
r1 = handlers["ncp_write_memory"]({"content": "...", "layer": "semantic",
    "src": "user_verified", "written_by": "agentA", "pipeline_id": "pipe_cmp"})
r2 = handlers["ncp_post_turn"]({"agent_id": "agentA", "role": "worker",
    "task": "t1", "slot": "s1", "intent": "i1", "pipeline_id": "pipe_cmp",
    "result_summary": "done", "result_full": "done in full",
    "memory_chunks": [{"content": "...", "layer": "semantic", "src": "user_verified"}]})
# base_trust via ncp_write_memory: 0.95   (src-derived, correct)
# base_trust via ncp_post_turn:    0.70   (flattened default)
```

**Finding 2 — silent eviction:**

```python
for i in range(8):
    handlers["ncp_write_memory"]({"content": f"topic_alpha detail {i} " * 8,
        "layer": "semantic", "src": "user_verified", "written_by": "writer",
        "pipeline_id": "pipe_evict", "base_trust": 0.9})
resp = handlers["ncp_get_context"]({"agent_id": "reader", "role": "analyst",
    "task": "investigate_topic_alpha", "slot": "reviewing",
    "intent": "find_relevant_facts", "pipeline_id": "pipe_evict",
    "max_tokens": 120})
# resp["context"] has zero [NCP:SUBCONSCIOUS] entries (full eviction)
# resp["telemetry"]["evicted_high_relevance_count"] == 0
```

---

## Addendum: token-efficiency features are opt-in and off by default, with no signal that they're off

**Status: Fixed.**

These aren't wrong-behavior bugs in the pipeline_id sense — nothing is
mis-wired — but they sit in the same blind spot: a host running the default
configuration has no way to discover, short of reading `ncp/config.py`, that
the mechanisms most directly aimed at reducing token spend are inert.

- **Every token-efficiency knob defaults to off.** Confirmed directly against
  `ncp/config.py`'s property defaults:

  | Feature | Config key | Default |
  |---|---|---|
  | Distillation (trim, don't drop, chunks that don't fit) | `distillation.enabled` | `False` |
  | Adaptive budget (shrink budget on easy/low-drift turns) | `budget.adaptive_budget_enabled` | `False` |
  | Memoization (skip re-running already-done work) | `memoization.enabled` | `False` |
  | Computed drift (replace self-reported honor system) | `drift.drift_computed_enabled` | `False` |
  | Rerank | `retrieval.rerank_enabled` | `False` |

  `ncp_get_context`'s response telemetry never indicates that a feature which
  would have reduced this turn's token cost is disabled — there is no
  `distillation_would_have_saved_n_tokens` field or equivalent. A plain
  `ncp.configure()` with no `ncp.toml` gets the least token-efficient
  configuration NCP supports, silently.

- **Token counting is a `chars/4` heuristic unless `NCP_TOKEN_UNIT=tiktoken`
  is set** (`ncp/tokens.py:1-6`). `context_token_budget` is therefore an
  estimate of the target model's real token count, not a measurement of it —
  it can silently over- or under-shoot depending on how far the model's real
  tokenizer diverges from chars/4 on the actual content, and (per finding 2
  above) there's no telemetry that would reveal an overshoot after the fact.

- **Passing an explicit `k` to `ncp_get_context` bypasses the pressure-based
  auto-shrink tiers entirely.** `Assembler._assembly_caps`
  (`ncp/assembler.py:825-837`) only consults `chunk_cap_high`/
  `chunk_cap_critical` when `k is None`:

  ```python
  def _assembly_caps(self, *, budget: BudgetContext, k: int | None) -> tuple[int, int]:
      if k is not None:
          return max(1, k), self._whisper_cap_default
      if budget.pressure == "critical":
          return self._chunk_cap_critical, self._whisper_cap_critical
      if budget.pressure == "high":
          return self._chunk_cap_high, self._whisper_cap_high
      return self._chunk_cap_default, self._whisper_cap_default
  ```

  An integration that always sets `k` explicitly never gets the automatic
  tightening under budget pressure that the default path provides.

**Fix:** surface the active feature-flag set (which of the five above are
on/off) in `ncp_get_context`'s telemetry block, and default `distillation` and
`adaptive_budget` to `True` — they're the two mechanisms whose entire purpose
is bounding token spend, and neither has a correctness downside for being on.
