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
follow-up; findings 9-11 came from a follow-up bug-bounty-style hunt over the
dogfood harness and provider adapters). Full suite after integration:
**1095 passed, 29 skipped** (the skips are all pgvector/psycopg/redis/
optional-provider-extra tests correctly skipping for lack of a live service
or optional dependency in this environment), plus one pre-existing, unrelated
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
| 9 — `_read_message` can hang forever on a truncated/malformed frame | Blocking pipe reads now run under a hard deadline; oversized `Content-Length` is rejected before reading; a related `close()`-ordering deadlock uncovered while fixing this was also fixed |
| 10 — `MCPHTTPClient` leaks its subprocess on a readiness-probe timeout | `start()` now tears the just-spawned process down via `close()` before re-raising when `_wait_until_ready()` fails |
| 11 — `AnthropicAdapter.stream()` lets raw provider errors escape | The real HTTP request (inside `with stream_ctx as stream:`) is now wrapped in the same error-mapping `call()` uses, so streaming failures raise `NCPAdapterError`/`NCPAdapterTimeoutError` too |

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

## 9. `_read_message` in the dogfood harness can hang forever on a truncated/malformed JSON-RPC frame, leaking the subprocess

**Status: Fixed.**

- **Location:** `ncp/dogfood.py`, `_read_message` (module-level helper used
  by `MCPStdioClient.request()`)
- **Type:** untraced-path / resource-leak
- **Severity:** silent — no exception, no log line; the calling process (e.g.
  `ncp dogfood`, or a CI job invoking it) just hangs forever with a leaked
  child process
- **Evidence:**

  `line = stream.readline()` (the header loop) and
  `body = stream.read(int(content_length))` both block on a raw pipe with no
  timeout. If a server process sends a `Content-Length` header larger than
  the bytes it actually writes (crash mid-write, OOM-kill between header and
  body flush, or any non-conforming server), `stream.read(n)` loops
  internally until it gets `n` bytes or EOF — and since the child's stdout
  stays open, EOF never comes. The call blocks indefinitely, and since this
  happens inside `with MCPStdioClient(...) as client:`, `__exit__`/`close()`
  (which would terminate the subprocess) never runs.

  Confirmed live with a fake child that writes `Content-Length: 100000` plus
  10 bytes of body, then sleeps: on the pre-fix code, `MCPStdioClient.
  request()` was still blocked after 8+ seconds (killed by an external
  timeout for the repro to terminate at all), and the child process was
  still alive afterward — a genuine, unbounded hang with a leaked child.

- **Fix:**
  1. Blocking pipe reads (`readline()`/`read(n)`) now run on a background
     daemon thread with a hard deadline (`_read_with_timeout`, default
     `_DEFAULT_MCP_READ_TIMEOUT_SECONDS = 30.0`, threaded through
     `MCPStdioClient(read_timeout_seconds=...)`). A daemon thread was used
     instead of `concurrent.futures.ThreadPoolExecutor` deliberately:
     `ThreadPoolExecutor`'s worker threads are joined by an `atexit` hook
     even when never explicitly shut down, so a call that legitimately never
     returns (the exact failure mode here) would reintroduce the same hang
     at interpreter exit — confirmed by direct repro before settling on the
     daemon-thread + `queue.Queue` approach. On timeout, a plain
     `RuntimeError` is raised instead of hanging, so the surrounding `with`
     block's `__exit__` now runs.
  2. Added a `Content-Length` sanity cap (`_MAX_MCP_CONTENT_LENGTH =
     10_485_760`, matching the existing server-side cap in
     `ncp/mcp/server.py`'s `_read_message`) so an absurd header is rejected
     before any read is attempted.
  3. While verifying (1) end-to-end, found and fixed a second, related bug
     it exposed: `MCPStdioClient.close()`/`MCPHTTPClient.close()` closed the
     process's pipes *before* terminating the still-alive child. If a
     `_read_with_timeout` background thread was still blocked inside
     `stream.read()` on that pipe (holding the `BufferedReader`'s internal
     lock for the duration of the blocking read), `pipe.close()` from the
     main thread would then block acquiring that same lock — deadlocking
     `close()` itself, since the process (whose termination would unblock
     the read via EOF) was never reached. Reordered both `close()` methods
     to terminate/kill the process first, then close the pipes, so the
     pending read unblocks (EOF/broken pipe) before `close()` needs the
     lock. Confirmed via repro: pre-reorder, `close()` after a read timeout
     hung indefinitely; post-reorder, it returns in under 10ms.

  Regression tests: `tests/test_dogfood.py::
  test_stdio_client_read_message_times_out_on_truncated_frame_and_reaps_process`,
  `::test_stdio_client_read_message_rejects_absurd_content_length`.

---

## 10. `MCPHTTPClient` leaks its subprocess when the readiness probe times out

**Status: Fixed.**

- **Location:** `ncp/dogfood.py`, `MCPHTTPClient.start()` and
  `_wait_until_ready()`
- **Type:** resource-leak
- **Severity:** silent — `RuntimeError` is raised as expected, but the
  spawned process is orphaned, still running and holding its bound port
- **Evidence:**

  `start()` (called from `__enter__`) does `self._process =
  subprocess.Popen(...)` then `self._wait_until_ready()`. If the server
  doesn't answer `/healthz` within `timeout_seconds` (default 5.0s) but
  hasn't exited, `_wait_until_ready()` raises `RuntimeError`. Because this
  happens inside `__enter__`, Python's `with` statement never calls
  `__exit__` (it only calls `__exit__` if `__enter__` succeeds) — so
  `close()` (which terminates/kills the child) never runs.

  Confirmed live: pointing `server_cmd` at a process that never answers
  `/healthz` raises the expected `RuntimeError` after the probe deadline,
  but `os.kill(pid, 0)` confirms the child is still alive afterward — a real
  leaked, port-holding process, not just a slow failure.

- **Fix:** `start()` now wraps `self._wait_until_ready()` in a
  `try/except Exception` that calls `self.close()` (reusing the same
  terminate/kill teardown `__exit__` already uses, rather than duplicating
  it) before re-raising the original exception. `close()` is idempotent
  (`self._process is None` short-circuits) so this is safe to call even if
  the process happened to exit on its own by then.

  Regression test: `tests/test_dogfood.py::
  test_http_client_start_terminates_process_when_readiness_probe_times_out`.

---

## 11. `AnthropicAdapter.stream()` lets real provider errors escape unwrapped, breaking the adapter's own error contract

**Status: Fixed.**

- **Location:** `ncp/adapters/anthropic.py`, `stream()`, contrast with
  `call()`
- **Type:** adapter-gap
- **Severity:** silent-ish — not a hang, but a contract violation: any code
  written against `except NCPAdapterError` (the documented adapter contract)
  silently fails to catch real streaming failures
- **Evidence:**

  The `anthropic` SDK's `Messages.stream()` only builds a deferred request
  object (`MessageStreamManager`) — the actual HTTP call happens inside
  `MessageStreamManager.__enter__` (the `with stream_ctx as stream:` line).
  Pre-fix, `AnthropicAdapter.stream()` only wrapped the `.stream()` call
  itself (which just builds the manager) in `self._run_provider_call(...)` —
  the real network request, in the `with` block, was outside that wrapper.
  So any real failure during streaming (auth failure, rate limit, connection
  error, timeout) raised the SDK's raw `anthropic.APIConnectionError`/
  `APIStatusError`/`APITimeoutError` instead of this codebase's own
  `NCPAdapterError`/`NCPAdapterTimeoutError`.

  Confirmed live: pointing the adapter at an unroutable address,
  `adapter.call(...)` correctly raised `NCPAdapterError: Anthropic call
  failed: Connection error.`, while `list(adapter.stream(...))` raised the
  raw, unwrapped `anthropic.APIConnectionError`. Also reproduced
  deterministically with a mocked `MessageStreamManager` whose `__enter__`
  raises `anthropic.APIConnectionError`/`APITimeoutError`: pre-fix, both
  leaked as raw `anthropic.*` exceptions out of `list(adapter.stream(...))`.

  (`OpenAIAdapter.stream()` does not have this bug — its SDK performs the
  HTTP request synchronously inside the wrapped `.create(stream=True)` call,
  so it was left untouched.)

- **Fix:** the `with stream_ctx as stream:` entry and the streaming loop
  that follows are now wrapped in the same `NCPAdapterError`/
  `NCPAdapterTimeoutError` mapping `_run_provider_call` already applies to
  `call()` (`except timeout_types -> NCPAdapterTimeoutError`, `except
  Exception -> NCPAdapterError`, both with the same `"Anthropic ... "`
  message shape), so a streaming failure now produces the same exception
  type and message shape as the equivalent non-streaming failure.

  Regression tests: `tests/test_adapters.py::TestAnthropicAdapter::
  test_stream_wraps_errors_raised_on_manager_entry`,
  `TestErrorSemantics::test_anthropic_stream_timeout_raises`.

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

**Finding 9 — `_read_message` hang / leaked subprocess:**

```python
# fake_server.py: writes a Content-Length header far larger than the body
# it actually sends, then sleeps forever without closing stdout.
#   sys.stdout.buffer.write(b"Content-Length: 100000\r\n\r\n")
#   sys.stdout.buffer.write(b"0123456789")
#   sys.stdout.buffer.flush()
#   time.sleep(9999)

client = MCPStdioClient(store_path=..., cwd=..., server_cmd=[sys.executable, "fake_server.py"])
client.start()
client.initialize()  # pre-fix: blocks forever; post-fix (read_timeout_seconds=N):
                      #   raises RuntimeError("Timed out after Ns waiting for MCP message data")
                      #   within N seconds, and the with-block's __exit__ reaps the child.
```

**Finding 10 — `MCPHTTPClient` leaked subprocess on readiness timeout:**

```python
# fake_http_server.py: never answers /healthz (e.g. time.sleep(9999)).
client = MCPHTTPClient(store_path=..., cwd=..., server_cmd=[sys.executable, "fake_http_server.py"])
try:
    client.start()
except RuntimeError:
    pass
# pre-fix: os.kill(pid, 0) succeeds -- child still alive (leaked).
# post-fix: os.kill(pid, 0) raises ProcessLookupError -- child was reaped by close().
```

**Finding 11 — `AnthropicAdapter.stream()` unwrapped provider error:**

```python
class FakeStreamManager:
    def __enter__(self):
        raise anthropic.APIConnectionError(request=MagicMock())
    def __exit__(self, *a):
        return False

with patch.object(adapter._client.messages, "stream") as mock_stream:
    mock_stream.return_value = FakeStreamManager()
    list(adapter.stream("ctx", "hi"))
# pre-fix: raises raw anthropic.APIConnectionError
# post-fix: raises NCPAdapterError("Anthropic call failed: Connection error.")
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
