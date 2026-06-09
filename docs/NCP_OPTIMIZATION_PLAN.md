# NCP Optimization Plan — Make the MCP Plug-and-Play and Honest

**Status:** Ready for implementation
**Source:** Expert code review of `main` (commit `54488d6`), 2026-06-09
**Goal:** Close the gap between what the README boasts and what the code delivers,
so that a host connecting over MCP gets bounded, trust-weighted, multi-agent
memory out of the box — with no Python API required.

This document is self-contained: it includes the review findings, measured
baseline numbers, a prioritized TODO checklist, and a tech design for each work
item with file/line references, schema diffs, and acceptance criteria.

---

## 0. Review summary (context for the implementer)

The codebase is real and well-tested (478 core tests pass; the ~35 failures in
a clean environment are missing optional provider SDKs, not logic bugs). The
assembler, BM25 hybrid retrieval, SQLite/pgvector/Redis stores, and the MCP
stdio+HTTP/SSE server all work. However:

### Measured baseline (this environment, current code)

```bash
python3 benchmarks/coding_pipeline/run.py
```

| Metric | README claims | Actual (current code) |
|---|---|---|
| Reduction vs raw replay | 17.52x | **10.64x** |
| Beats sliding window | implied yes | **false** (peak 462 vs 383 tok) |
| Benchmark `pass` gate | — | **false** |
| Token unit | (word_split, June 1 run) | `chars_div4` |

```bash
python3 benchmarks/needle/run.py --turns 24 --needles 6 --budget 4
# Reproduces as advertised: NCP recall 0.50 vs sliding window 0.00
```

### Root-cause findings (each maps to a work item below)

| # | Finding | Where | Work item |
|---|---------|-------|-----------|
| F1 | Recent-turn refs crowd out retrieved chunks: by turn 5 all chunk slots hold the agent's own last-4 turn summaries; high-trust retrieved facts are silently evicted. NCP degenerates into a sliding window of its own outputs — this is why the benchmark loses to `SlidingWindowBaseline`. | `ncp/assembler.py:106-112`, `ncp/assembler.py:243` | WI-1 |
| F2 | The "~840 token bound" is not enforced anywhere. Caps are count-based (4 chunks × 2000 chars, 3 whispers × 600 chars ≈ worst case ~2.5–3k tokens). Only `assemble_incremental` takes `max_tokens` (word-split proxy); the MCP path never passes it. | `ncp/assembler.py:127-159`, `ncp/mcp/server.py:160-210` | WI-2 |
| F3 | MCP surface is stateless: `ncp_get_context` builds a fresh `ConsciousBlock` every call with `recent=[]`, `drift_score=0`, `slot_age=0`, and `BudgetContext()` defaults. So recent-ref continuity, drift alerts, budget-pressure caps, and cost logging never function for MCP hosts. There is no post-turn MCP tool. | `ncp/mcp/server.py:160-210` | WI-3 |
| F4 | "Context Trust" is inert via MCP: `ncp_write_memory` schema exposes no `base_trust`/`written_at_drift`, so every chunk gets the 0.7 default and trust-weighted ranking is a constant. | `ncp/mcp/server.py:67-81`, `ncp/types.py:184-185` | WI-4 |
| F5 | Broadcast whispers (`target='*'`) are consume-once: `drain_whispers` deletes on read, so only the first agent to drain sees a pipeline broadcast. | `ncp/stores/sqlite.py:469-493`, `ncp/stores/redis_coordination.py:141-156` | WI-5 |
| F6 | Whisper delivery is at-most-once: the assembler drains (deletes) whispers during assembly, *before* the provider call. A failed turn loses the signals permanently. | `ncp/assembler.py:113-116, 360-366` | WI-6 |
| F7 | Default whisper TTL is 60s and `ncp_emit_whisper` exposes no TTL parameter. Real MCP agents take minutes between turns → most whispers expire undelivered. | `ncp/types.py:330`, `ncp/mcp/server.py:83-97` | WI-7 |
| F8 | System coherence alerts eat the whisper drain cap (`drain_cap = whisper_cap - len(alerts)`), starving real agent messages. | `ncp/assembler.py:113` | WI-8 |
| F9 | SQLite retrieval rebuilds `BM25Okapi` over the full pipeline corpus on every query — O(corpus) CPU per turn, grows forever. | `ncp/stores/sqlite.py:263-340`, `ncp/stores/retrieval.py:194-213` | WI-9 |
| F10 | Pidgin encoding is cache-hostile and carries dead weight: volatile `[NCP:BUDGET]` block first, per-whisper `age:Ns` recomputed every call, per-chunk float scores that change with age, empty `tried:[] failed:[]` fields, whisper payloads as escaped embedded JSON. | `ncp/encoder.py` | WI-10 |
| F11 | Thread-safety: `last_session_id` and the in-memory `sessions` dict are shared mutable state across `ThreadingHTTPServer` threads with no lock. | `ncp/mcp/server.py:155-169, 238-263` | WI-11 |
| F12 | Redis whisper peek is O(N) round trips (one `HGETALL` per id, no pipelining); `whisper_stats` SCANs every payload key. | `ncp/stores/redis_coordination.py:79-139` | WI-12 |
| F13 | README numbers are stale and internally inconsistent (1,927/174 = 11.1x, not 17.52x; the "80,000 tokens at turn 50" hero claim has no artifact; benchmarks are synthetic with no model in the loop). | `README.md:12-22, 155-172`, `docs/NCP_BENCHMARK_CODING_PIPELINE.md` | WI-13 |
| F14 | `estimate_tokens` silently falls back to chars/4 when tiktoken can't download its encoding (common in sandboxes) — benchmark units shift between runs/environments. | `ncp/benchmarks.py:63-93` | WI-13 |

---

## 1. TODO checklist (priority order)

### P0 — Correctness: make the core claim true again
- [ ] **WI-1** Fix recent-refs vs retrieval budget split in the assembler
- [ ] **WI-2** Enforce a real token budget in `assemble()`; expose `max_tokens` via MCP
- [ ] **WI-13** Refresh README + benchmark docs from current code; fix inconsistencies; pin token unit

### P1 — Plug-and-play MCP parity (the protocol must work through MCP alone)
- [ ] **WI-3** Server-side conscious state: persist/load per `(pipeline_id, agent_id)`; add `ncp_post_turn` tool
- [ ] **WI-4** Trust through MCP: src-derived default `base_trust`; optional `base_trust` param on `ncp_write_memory`
- [ ] **WI-7** Raise default whisper TTL; expose `ttl_seconds` on `ncp_emit_whisper`

### P2 — Agent-to-agent reliability
- [ ] **WI-5** Per-recipient broadcast delivery (delivery-cursor table / Redis consumer groups)
- [ ] **WI-6** At-least-once whispers: peek at assemble, acknowledge at post-turn
- [ ] **WI-8** Separate slot budget for system alerts vs agent whispers
- [ ] **WI-11** Thread-safety for MCP session state
- [ ] **WI-12** Pipeline Redis whisper reads; fix `whisper_stats` scan

### P3 — Efficiency polish
- [ ] **WI-9** SQLite FTS5-backed retrieval (replace per-query BM25Okapi rebuild)
- [ ] **WI-10** Pidgin trimming + cache-friendly block ordering

Suggested PR slicing: PR1 = WI-1 + WI-2 + WI-13 (flips the benchmark gate back
to `pass: true` and makes docs honest). PR2 = WI-3 + WI-4 + WI-7. PR3 = WI-5 +
WI-6 + WI-8 + WI-11 + WI-12. PR4 = WI-9 + WI-10.

---

## 2. Tech design per work item

### WI-1 — Recent/retrieved budget split (P0, the most important fix)

**Current behavior** (`ncp/assembler.py:106-112`): `_prepare_assembly` builds
`deduped = [*recent_chunks, *subconscious]` then truncates to `chunk_cap` (4).
`post_turn` keeps `recent` at up to 5 refs (`assembler.py:243`), each of which
resolves to a relevance-1.0 chunk. Steady state: recent fills every slot,
retrieval contributes nothing. Reproduction (verified):

```
turn 5: chunks=['recent_t3','recent_t2','recent_t1','recent_t0']   # golden_fact evicted
```

Worse, the evicted retrieved chunk often does not appear in
`evicted_high_relevance` because eviction tracking inspects `deduped[chunk_cap:]`,
which holds the *recent* overflow, not the retrieved chunk.

**Design:**
1. Add config knobs in `NCPConfig` (`ncp/config.py`), with assembler fallbacks
   matching the existing cap pattern (`assembler.py:63-68`):
   - `recent_slot_budget` (default **2**)
   - retrieval keeps the remainder: `chunk_cap - min(len(recent), recent_slot_budget)`.
2. In `_prepare_assembly`:
   ```python
   recent_kept = recent_chunks[:recent_budget]
   retrieved_kept = [c for c in subconscious if c.chunk_id not in {r.chunk_id for r in recent_kept}]
   combined = (recent_kept + retrieved_kept)[:chunk_cap]
   ```
   Under `critical` pressure, drop recent to 1 slot before dropping retrieved.
3. Fix eviction telemetry: compute `evicted_high_relevance` from the union of
   candidates minus `combined`, not from a positional slice.
4. Optional follow-up (not required for the gate): score recent refs through
   `RetrievalPolicy` instead of hardcoding `relevance=1.0`
   (`assembler.py:291-308`) so a stale own-turn summary can lose to a
   high-trust retrieved fact.

**Tests:** extend `tests/test_assembler.py` — after ≥5 post-turns for one
agent, an assembly with a query matching a planted high-trust chunk MUST
include that chunk. Add a regression test for the eviction-telemetry fix.

**Acceptance:** `python3 benchmarks/coding_pipeline/run.py` reports
`beats_sliding_window: true` and `pass: true`.

---

### WI-2 — Enforce the token budget (P0)

**Current:** `assemble()` has no token limit; the bound is count×max-size.
`assemble_incremental` enforces `max_tokens` with `len(text.split())`.

**Design:**
1. Add `max_tokens: int | None` to `Assembler.assemble()`. After computing
   `combined_chunks`, greedily keep chunks (recent first, then retrieved, in
   rank order) while `estimate_tokens(running_context) <= max_tokens`. Budget
   header + conscious block are always emitted; whispers get a reserved
   sub-budget (e.g. 25%) so chunks can't starve them.
2. Move `estimate_tokens` out of `ncp/benchmarks.py` into a small
   `ncp/tokens.py` (benchmarks import from there; avoids the assembler
   importing the benchmarks module). Keep tiktoken-if-available, chars/4
   fallback (see WI-13 for determinism note).
3. MCP: add `max_tokens` (integer, optional) to the `ncp_get_context` input
   schema (`ncp/mcp/server.py:43-65`) and thread it through both the streaming
   and non-streaming paths. Default from config: `context_token_budget`
   (suggest **840** to match the README story, configurable in
   `.ncp/config.toml`).
4. Unify `assemble_incremental` to use the same estimator instead of
   word-split.

**Tests:** assembly with `max_tokens=200` never exceeds 200 estimated tokens
and still contains budget+conscious blocks; MCP `ncp_get_context` honors the
param (extend `tests/test_mcp_server*.py`).

**Acceptance:** README can say "bounded to a configurable token budget
(default 840), enforced at assembly" — truthfully.

---

### WI-3 — Server-side conscious state + `ncp_post_turn` (P1)

**Current:** `_handle_get_context` (`ncp/mcp/server.py:160-210`) constructs
`ConsciousBlock` purely from call args. `recent`, `tried`, `failed`,
`drift_score`, `slot_age` are always defaults. `post_turn` (which logs turn
records, updates `recent`, persists conscious snapshots, logs cost) is never
reachable from MCP. The store already has `log_conscious` and conscious
snapshot tables — they're write-only via the Python API today.

**Design:**
1. Add `BaseStore.load_latest_conscious(pipeline_id, agent_id) -> ConsciousBlock | None`
   (SQLite + pgvector; the snapshot rows already exist via `log_conscious`,
   keyed by the hash — add an indexed lookup by `(pipeline_id, agent_id)`
   ordered by recency; new migration `005_conscious_lookup.sql` if an index is
   needed).
2. `_handle_get_context`: after building the block from args, hydrate
   persistent fields from the latest snapshot when present:
   `recent`, `tried`, `failed`, `drift_score`, `slot_age`, `goal_version`,
   `steps_completed`. Explicit args win over loaded state.
3. New MCP tool **`ncp_post_turn`**:
   ```json
   {
     "name": "ncp_post_turn",
     "inputSchema": {
       "properties": {
         "agent_id": {"type": "string"},
         "pipeline_id": {"type": "string"},
         "result_summary": {"type": "string", "description": "<= 2000 chars, becomes the fetchable turn record"},
         "result_full": {"type": "string"},
         "input_tokens": {"type": "integer"}, "output_tokens": {"type": "integer"},
         "model": {"type": "string"},
         "tried": {"type": "array", "items": {"type": "string"}},
         "failed": {"type": "array", "items": {"type": "string"}}
       },
       "required": ["agent_id", "result_summary"]
     }
   }
   ```
   Handler: load latest conscious (or reconstruct), build an `NCPResponse`
   (cost 0.0 when unknown), call `assembler.post_turn(...)`. Returns
   `{"turn_id": ...}`. This is what makes `recent:` refs, `ncp cost`, and
   `ncp status` work for MCP hosts.
4. Budget pressure via MCP: add optional `ctx_used` (0.0–1.0) and
   `steps_completed`/`steps_total` to `ncp_get_context`; map `ctx_used` ≥ 0.7 →
   `high`, ≥ 0.85 → `critical` (reuse whatever thresholds exist in
   `ncp/api.py`; check before inventing new ones).
5. Update the turn contract templates (`ncp init` CLAUDE.md output,
   `examples/06_claude_code/CLAUDE.md`, `examples/07_codex_cli/README.md`) to:
   get_context → work → post_turn (+ optional write_memory for durable facts).

**Tests:** MCP round-trip test — two `ncp_get_context` calls separated by an
`ncp_post_turn` must show the first turn's `r:sub/...` ref resolved in the
second context.

---

### WI-4 — Trust that works through MCP (P1)

**Design:**
1. Source-derived default trust in `SubconsciousChunk` (or at MCP handler
   level to avoid changing Python-API behavior):
   `user_verified=0.95, tool_result=0.85, agent_inferred=0.6, synthesis=0.7, subcon_retrieved=0.7`.
   Implement as: if caller did not pass `base_trust`, look up by `src`.
   Pydantic approach: make `base_trust` default `None` → resolve in a
   `model_validator`; keep 0.7 as final fallback. Audit tests that assert 0.7.
2. Add optional `base_trust` (number 0–1) to `ncp_write_memory` schema.
3. Stamp `written_at_drift` server-side in the MCP write handler from the
   latest persisted conscious snapshot's `drift_score` (depends on WI-3 step 1;
   if absent, 0.0 as today).

**Tests:** writing via MCP with `src=user_verified` then `src=agent_inferred`
and querying with a neutral query ranks the user_verified chunk first (equal
recency/lexical).

---

### WI-5 — Broadcast delivery to all pipeline members (P2)

**Current:** delete-on-drain in both backends → `target='*'` reaches one agent.

**Design (SQLite/pgvector):**
1. New table via migration:
   ```sql
   CREATE TABLE whisper_deliveries (
     whisper_id TEXT NOT NULL,
     agent_id   TEXT NOT NULL,
     delivered_at REAL NOT NULL,
     PRIMARY KEY (whisper_id, agent_id)
   );
   ```
2. `drain_whispers(agent_id=...)` semantics change for broadcast rows only:
   - targeted whispers (`target == agent_id`): delete on ack (unchanged).
   - broadcast (`target='*'`): insert into `whisper_deliveries` instead of
     deleting; exclude already-delivered ids for this agent in
     `_select_whispers`. Row is physically removed by TTL GC
     (`_soft_gc` already deletes expired rows — keep that as the cleanup path).
3. **Redis:** simplest matching design — per-agent delivered set
   `ncp:whispers:delivered:{agent_id}` (`SADD` on drain, `EXPIRE` = whisper
   TTL + slack); skip members already in the set when peeking `*`. (A full
   Redis Streams consumer-group rewrite is better long-term but is a larger
   change; don't block this fix on it.)

**Tests:** emit one `target='*'` whisper; drain as `agent_a` then `agent_b`
→ both receive it; drain as `agent_a` again → not redelivered.

---

### WI-6 — At-least-once whispers (P2)

**Current:** `_prepare_assembly` → `_drain_whispers` deletes before the
provider call (`ncp/assembler.py:113-116, 360-366`).

**Design:**
1. Assembler switches to `store.peek_whispers(...)` during assembly and
   records the peeked ids on the `AssemblyResult`
   (`pending_whisper_ids: list[str]`).
2. `post_turn` / `post_turn_async` accept `ack_whisper_ids: list[str] | None`
   and call `store.acknowledge_whispers(...)` (for broadcasts this becomes
   "mark delivered" per WI-5).
3. MCP: `ncp_get_context` returns `pending_whisper_ids` in its result;
   `ncp_post_turn` (WI-3) takes them back and acks. If the host never calls
   post_turn, whispers redeliver next turn — at-least-once, with the TTL as
   the upper bound. Note in tool descriptions that duplicate delivery is
   possible; payloads must be idempotent.
4. Keep `drain_whispers` on the store interface for backward compatibility
   (CLI `ncp handoff` paths use peek/ack already — see `ncp/agent_handoff.py:89-107, 287-291`).

**Tests:** assemble without post_turn → second assemble redelivers; with
post_turn ack → not redelivered.

---

### WI-7 — Whisper TTL (P1, two-line core change)

1. `Whisper.ttl_seconds` default: `60` → **`1800`** (`ncp/types.py:330`), and
   make it configurable (`whisper_ttl_default` in config).
2. Add optional `ttl_seconds` (integer ≥ 1) to `ncp_emit_whisper` schema and
   handler (`ncp/mcp/server.py:83-97, 226-236`).
3. Audit tests pinned to 60s.

---

### WI-8 — Alerts must not starve agent whispers (P2)

**Current:** `drain_cap = max(0, whisper_cap - len(coherence_alerts))`
(`ncp/assembler.py:113`); 2 alerts + cap 3 → 1 real message.

**Design:** give system alerts their own small budget (`alert_cap`, default 2)
and keep `whisper_cap` for agent whispers. Encoded order: alerts first, then
agent whispers; `evicted_whispers` telemetry computed per pool. Coherence
sensors (`whisper_type="sensor"`) should not consume either cap (today they're
appended but effectively trimmed — make their exclusion from the wire format
explicit; they're telemetry, not context).

---

### WI-9 — FTS5 retrieval (P3)

**Current:** every `query()` loads all candidate rows and rebuilds `BM25Okapi`
(`ncp/stores/sqlite.py:288-340`).

**Design:**
1. Migration: `CREATE VIRTUAL TABLE chunks_fts USING fts5(content, content='chunks', content_rowid='rowid')`
   + triggers on insert/update/delete of `chunks` (check the actual table/rowid
   names in `ncp/stores/sqlite.py` schema section / `ncp/migrations/001`).
2. `query()` hybrid path: `SELECT rowid, bm25(chunks_fts) FROM chunks_fts WHERE chunks_fts MATCH ? ...`
   with the query sanitized into OR-ed terms (FTS5 syntax is strict — escape
   quotes, drop operators). Normalize bm25 scores into [0,1] (note: FTS5
   `bm25()` returns *lower-is-better* negative-ish values; map via
   `score / min_score` or rank-based normalization) and feed
   `RetrievalPolicy.score()` exactly as today so SQLite and pgvector stay
   aligned.
3. Keep the in-memory BM25Okapi path as fallback for stores created before the
   migration (or run the migration automatically on open — the store already
   has a migrations mechanism, see `ncp/stores/migrations.py`).
4. Preserve existing semantics covered by `tests/test_query_k_semantics.py`,
   `tests/test_vector_retrieval.py`, `tests/test_diversity_limit*.py`,
   `tests/test_retrieval_policy.py` — these are the contract; do not change
   ranking behavior beyond the index swap, and verify the empty-query
   "treat every candidate as eligible" rule (`ncp/stores/retrieval.py:165-181`)
   still holds (FTS MATCH can't express it — branch to trust/recency scan for
   blank queries as today).

**Acceptance:** `query()` no longer materializes the full corpus per call;
add a micro-benchmark or at least an O(N)-shape test with 5k chunks.

---

### WI-10 — Pidgin trimming + cache-friendly ordering (P3)

Changes in `ncp/encoder.py` (bump `ncp_v` handling NOT required — wire format
is presentation, but update `docs/NCP_PROTOCOL_SPEC.md` §6 examples):
1. **Block order:** `[NCP:CONSCIOUS]` (stable identity lines first: id/role/
   owns/must_not), then `[NCP:SUBCONSCIOUS]`, then `[NCP:WHISPERS]`, then
   `[NCP:BUDGET]` last. Rationale: longest stable prefix for provider prompt
   caching; the budget line changes every turn.
2. **Drop empty fields:** omit `tried:[] failed:[]`, `recent:[]`,
   `must_not:[]` lines when empty; omit `goal_version` when 1, `drift_score`
   when 0.00.
3. **Quantize volatile values:** whisper `age:` → buckets (`<1m`, `<10m`,
   `<1h`, `old`); chunk `score:` → one decimal or drop entirely (the model
   doesn't act on 0.43 vs 0.47); keep `trust:` but quantize to one decimal.
4. **Unescape structured whisper payloads:** when payload parses as JSON
   (`HandoffPayload`/`DissentPayload`/etc. are stored as JSON strings — see
   `ncp/types.py:399-416`), render as `k:v` pidgin lines instead of an escaped
   JSON blob: `ask:... files:[a,b] slice:...`.
5. Update golden tests in `tests/test_encoder.py` and any spec-format
   assertions.

**Acceptance:** assembled context for the benchmark scenario shrinks ≥ 10%
with no information loss; re-run coding benchmark and update docs.

---

### WI-11 — MCP session-state thread safety (P2)

`make_handlers` (`ncp/mcp/server.py:155-169`): wrap `sessions` and
`last_session_id` access in a `threading.Lock`. Better: kill the
`last_session_id` fallback entirely — `ncp_fetch` without an
agent/pipeline/session id should use `DEFAULT_FETCH_SESSION_ID` rather than
silently charging the previous caller's budget (current code:
`server.py:239-241`). Document that `ncp_fetch` should pass `session_id` from
`ncp_get_context`'s response.

### WI-12 — Redis efficiency (P2)

`peek_whispers` / `_async_peek_whispers`: batch the per-id `HGETALL` calls in
one pipeline; `whisper_stats`: keep a counter hash updated on emit/ack instead
of SCANning all payload keys (or at minimum cap the scan and document).

---

### WI-13 — Honest, reproducible numbers (P0)

1. **Re-run and re-publish:** after WI-1/WI-2 land, regenerate
   `benchmarks/coding_pipeline` and `research_pipeline` artifacts; update
   `README.md` table and `docs/NCP_BENCHMARK_*.md` "Current result" sections
   from the actual JSON. The benchmark `pass` gate must be true on `main`.
2. **Fix internal inconsistencies:** README pairs peak tokens (174) with a
   final-turn ratio (17.52x). Pick one basis (recommend final-turn for both
   numerator and denominator) and label it.
3. **Remove or re-frame the unbacked hero claim** ("Turn 50: 80,000 tok →
   ~840 tok"). Either reproduce it with a realistic-transcript benchmark
   (turns containing tool output of realistic size — even synthetic 2k-token
   tool dumps would make the raw-replay comparison meaningful) or label the
   block explicitly as an illustration, not a measurement.
4. **Pin the token unit:** vendor an offline tokenizer or always report
   `token_unit` next to every published number (the artifact already carries
   it — surface it in the docs tables). Add a CI job that runs both benchmarks
   and fails if `summary.pass` is false (extend `.github/workflows/ci.yml`).
5. **Label benchmark class:** add one sentence to README Benchmarks section:
   these are deterministic token-accounting benchmarks; quality-at-matched-
   budget lives in `benchmarks/efficacy/` (live provider required).

---

## 3. Non-goals / explicitly out of scope

- No orchestrator features (NCP stays a memory bus).
- No Redis Streams consumer-group rewrite (WI-5 uses the minimal design).
- No new storage backends; no changes to pgvector ANN behavior beyond keeping
  `RetrievalPolicy` parity.
- Do not change the Python API's `drain_whispers` contract (deprecate softly).

## 4. Verification matrix (run after each PR)

```bash
pip install -e . && pip install pytest
python3 -m pytest tests -q                                   # core suite green (optional-dep failures OK if SDKs absent)
python3 benchmarks/coding_pipeline/run.py | python3 -c \
  "import json,sys; s=json.load(sys.stdin)['summary']; \
   assert s['pass'], s; print('coding gate OK', s['reduction_factor'])"
python3 benchmarks/needle/run.py --turns 24 --needles 6 --budget 4 | python3 -c \
  "import json,sys; s=json.load(sys.stdin)['summary']; \
   assert s['ncp_beats_sliding_window']; print('needle gate OK')"
python3 examples/01_quickstart.py && python3 examples/02_multi_agent.py
```

Plus the new regression tests named in each work item.

## 5. Key file map

| Area | Files |
|---|---|
| Assembly pipeline | `ncp/assembler.py`, `ncp/encoder.py`, `ncp/coherence.py` |
| Types/validation | `ncp/types.py` |
| MCP server + tool schemas | `ncp/mcp/server.py` |
| Stores | `ncp/stores/sqlite.py`, `ncp/stores/pgvector*.py`, `ncp/stores/redis_coordination.py`, `ncp/stores/retrieval.py`, `ncp/stores/migrations.py`, `ncp/migrations/` |
| Config | `ncp/config.py`, `ncp/templates/config.toml.example` |
| Benchmarks | `ncp/benchmarks.py`, `ncp/bench/baselines.py`, `benchmarks/` |
| Docs to update | `README.md`, `docs/NCP_PROTOCOL_SPEC.md`, `docs/NCP_BENCHMARK_*.md`, `examples/06_claude_code/`, `examples/07_codex_cli/` |
