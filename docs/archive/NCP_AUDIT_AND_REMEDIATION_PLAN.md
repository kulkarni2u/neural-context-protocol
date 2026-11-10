# NCP Audit & Remediation Plan

> **Purpose.** This document is a full audit of Neural Context Protocol (NCP) as
> of `v1.2.1`, plus a prioritized, agent-executable remediation plan. It is
> written to be handed to a **Sarathi-orchestrated implementation pipeline**
> (local Claude + Codex + OpenCode agents) over the NCP bus itself.
>
> **How to consume this.** Each work item (`WI-###`) is self-contained: it has a
> scope, file anchors, an approach, acceptance criteria, and a verification
> command. Line numbers are anchored to symbols (function/class names) because
> exact lines drift — always locate the symbol, not the raw line. Do **not**
> batch unrelated work items into one commit. One WI → one focused change →
> its verification must pass before moving on.
>
> **Ground truth for agents:** run `pip install -e '.[dev,providers]'` +
> `pip install pytest-asyncio` first. Baseline before you start: `pytest -q`
> should show all pass except environment-only skips. If a WI's verification
> command fails *before* you start, record that — it may be a pre-existing gap.

---

## 0. Executive summary

NCP is a **real, working, well-engineered local MCP memory server** whose
**public claims materially overstate the runtime guarantees it provides.** The
plumbing is solid (installs cleanly, six-tool MCP surface works end to end, SQL
is parameterized, migrations have real rollback, cross-process whispers work).
The differentiators the README sells — *trust-aware transport, cryptographic
identity, drift detection, reputation-weighted retrieval* — are currently
**decorative or honor-system**, and **every headline benchmark number is a
designed demonstration rather than a measured property.**

The remediation splits into four tracks:

| Track | Theme | Why it matters |
|-------|-------|----------------|
| **A. Truth-in-advertising** | Make the README/docs match the code | Biggest reputational liability; cheapest to fix |
| **B. Correctness** | Fix bugs that corrupt trust/budget/data | The features that *do* exist are buggy |
| **C. Concurrency & ops** | Survive the concurrent load NCP advertises | Fails under exactly its target workload |
| **D. Make claims real** | Wire identity/trust/reputation into the runtime | Convert "decorative" into "true" |

Tracks A–C are near-term and low-risk. Track D is the roadmap that would let
the marketing claims stand.

---

## 1. Findings (evidence)

Severity: **P0** = ship-blocking / actively misleading, **P1** = real bug,
**P2** = design smell / hygiene.

### 1.1 Claims vs. reality (Track A)

| ID | Claim in README | Reality | Verdict |
|----|-----------------|---------|---------|
| F-A1 | "13.13x token reduction" | Unbounded raw-replay strawman ÷ fixed 340-token cap. Ratio scales purely with turn count (~3.4x@10, ~13x@40, ~26x@80). No model called; no quality metric. Their honest baseline (sliding window) is **1.44x**. | Misleading |
| F-A2 | "Task success +1.00" | Mock provider string-scans context for a planted slug; scorer checks the slug is present — circular. Budget is chosen so the baseline *must* fail; at budget 850 the baseline also scores 1.00. | Misleading |
| F-A3 | Stale headline MACE score | Reproduces today at **0.8915** (matches committed `benchmarks/mace/results/ncp.json`). README and `benchmarks/mace/README.md` were stale. D2-D4 are hard-coded string-match stub agents, trivially 1.0. | **False / stale** |
| F-A4 | "Cross-host handoff 0.8 success" | One process, no MCP server, shared tempdir SQLite. Control is engineered noise-only so it *cannot* succeed; the 0.2 miss was a CLI timeout, not a memory failure. | Misleading |
| F-A5 | "Real cryptographic Ed25519 identities" / "trust attaches to who wrote it" | Keys generated and **never read again**. No `sign()`/`verify()` call anywhere in `ncp/`. `written_by`/`from` are unauthenticated caller strings; revocation consulted nowhere. | Decorative |
| F-A6 | "Trust-aware transport… agent knows how much to believe" | `base_trust` is caller-supplied; any client can claim `src=user_verified` → 0.95 + calibration immunity. | Misleading |
| F-A7 | "Drift detection" | `drift_score` is **never computed** — it is whatever the client sends. `CoherenceChecker` only thresholds the self-reported number at 0.3. Drift discount is inert (0.0) in practice. | Misleading |
| F-A8 | "Beta-posterior reputation… bus can down-weight" | Computed and displayed by `ncp reputation`, but **no retrieval path reads it.** Weights nothing. | Partially true |

**Common root cause:** identity, trust, drift, and reputation are all keyed on
**unauthenticated, self-reported strings**, and the impressive numbers are
**arithmetic demonstrations**, not measurements.

### 1.2 Correctness bugs (Track B)

| ID | Sev | Finding | Anchor |
|----|-----|---------|--------|
| F-B1 | P0 | **Calibration feedback is not idempotent.** Boost is computed from *cumulative* `retrieval_count`/`dissent_count` and never reset/deltaed; every `calibrate --feedback` re-applies the same history → trust walks monotonically to 1.0/0.0. Reputation rollup inherits the corruption. | `stores/calibration.py` `compute_*` (~line 53–121, `retrieval_count` at :74/:81); `stores/sqlite.py::calibrate()` |
| F-B2 | P0 | **MCP path ignores the config token budget.** `Assembler(store=store)` built with no `config=`, so `context_token_budget` is never applied over the protocol. Unless the client passes `max_tokens`, **no token budgeting happens on the bus.** | `ncp/mcp/server.py:310`, `:433` |
| F-B3 | P1 | **3-fetch cap is decorative.** `k = max(1, int(args.get("k", 2)))` has no upper clamp despite "max 4" schema; session key is client-supplied so a fresh `session_id` resets the cap; Redis `remaining` hardcoded; `sessions` dict never pruned. | `ncp/mcp/server.py:481`, `_fetch_budget_remaining` :267, `DEFAULT_FETCH_SESSION_ID` :43 |
| F-B4 | P1 | **Chunk `expiry` written but never enforced** in SQLite reads/GC — expired "proven" facts served forever, while `types.py` *requires* the field at write time. | `stores/sqlite.py` query/GC paths; `types.py` expiry validation |
| F-B5 | P1 | **Silent data loss.** Near-dup writes (>0.92 sim) dropped and reported as success; `Whisper.dissent_target` has no SQLite/pgvector column so it is dropped; `world_check` whisper missing `detected_drift` silently resets drift to 0. | `stores/sqlite.py::_is_duplicate`, whisper schema; `assembler.py` world_check handler |
| F-B6 | P1 | **`effective_score` double-counts** trust, recency, and generation penalty (applied in `RetrievalPolicy.score` *and* again in `types.py::effective_score`) → score shown to the model is deflated and incomparable. | `ncp/types.py:213`, `stores/retrieval.py:54` |
| F-B7 | P2 | BM25 normalized per-result-set max (top hit always 1.0) yet fixed thresholds (0.01, 0.5, 0.6) applied to it. Drift discount is a cliff (1.0→0.69 across drift 0.30→0.31). | `stores/retrieval.py` |

### 1.3 Concurrency & ops (Track C)

| ID | Sev | Finding | Anchor |
|----|-----|---------|--------|
| F-C1 | P0 | **No `PRAGMA busy_timeout`.** Under `ThreadingHTTPServer` + concurrent writers → immediate `SQLITE_BUSY` → `NCPStoreUnavailableError`. Fails under the multi-agent load NCP advertises. | `stores/sqlite.py::_connect` (:237, add after the existing PRAGMAs) |
| F-C2 | P1 | **Check-then-act races** in `write()` (no `BEGIN IMMEDIATE`); `INSERT OR REPLACE` on same-`src` rewrite silently resets `created_at`/`retrieval_count`/`dissent_count` (feedback history wiped). `consolidate()` reads a stale cross-connection snapshot. | `stores/sqlite.py::write`, `::consolidate` |
| F-C3 | P0 (DX) | **Broken authenticated quickstart.** `ncp init` unconditionally bakes `auth_token = secrets.token_urlsafe(32)` into config, but **no** shipped client config carries an `Authorization` header → documented flow 401s against its own server. | `ncp/cli.py:651`; `examples/06_claude_code/mcp_servers.json`, `examples/07_codex_cli/mcp_servers.json`, `ncp/templates/provider_hooks/claude/mcp_servers.json` |
| F-C4 | P1 | **No CI coverage of pgvector/redis.** CI installs `.[dev,providers]` (no psycopg/redis); every durable-tier test is `importorskip`-ed away. Headline durable tier untested in CI. | `.github/workflows/ci.yml` |
| F-C5 | P1 | **No `LICENSE` file** despite MIT badge, `license = "MIT"` in pyproject, and footer. | repo root |

### 1.4 Security (Track B/D)

| ID | Sev | Finding | Anchor |
|----|-----|---------|--------|
| F-S1 | P0 | **Wire-format forgery.** `encoder.py` inserts chunk/whisper content with no escaping of `[NCP:...]` section markers or `src:`/`trust:`/`from:` provenance. Confirmed live: stored content can forge a fake `[NCP:WHISPERS]` block with counterfeit provenance — breaks the spec §5.1 "defends the envelope against source-tag forgery" claim. | `ncp/encoder.py` `_encode_subconscious`/`_encode_whispers`/`_indent_block` |
| F-S2 | P1 | **No runtime trust gating of whispers.** Delivery filters only on sender's self-declared `confidence` (a hostile agent sets 1.0). Reputation never consulted at drain time. | `assembler.py` whisper drain; `server.py` emit |
| F-S3 | P2 | Bearer check uses `==` not `hmac.compare_digest`; raw `exc` text leaked in JSON-RPC errors; Ed25519 secret stored unencrypted at rest (doc note). | `ncp/mcp/server.py`, `ncp/identity.py` |

**Positives (do not regress):** parameterized SQL everywhere; loopback/auth
default logic sound; request-size limits on both transports; CORS default-deny;
`0700`/`0600` keystore perms; safe provider-hook shell scripts.

---

## 2. Remediation plan (work items)

Ordered by recommended execution. Each WI is independently shippable.

### Track A — Truth-in-advertising (do first; unblocks honest positioning)

#### WI-001 · Fix stale/misleading benchmark numbers in README + docs — P0
- **Addresses:** F-A1, F-A2, F-A3, F-A4.
- **Scope:** `README.md` Benchmarks section; `benchmarks/mace/README.md`.
- **Approach:**
  1. Regenerate MACE: `python3 benchmarks/mace/run.py`; replace the stale
     headline MACE score with the reproduced composite (currently **0.8915**).
     Grep to confirm no stale literal remains.
  2. Re-order the coding-pipeline table so the **sliding-window** row (1.44x) is
     the lead comparison; explicitly label raw-replay "a floor / worst case,"
     and add a one-line note that the ratio scales with turn count.
  3. For the task-success and cross-host rows, add an inline caveat matching the
     benchmark docs ("context adequacy at a chosen budget, mock provider" /
     "control constructed to be noise-only"). Do not delete the rows — annotate.
- **Acceptance:** No stale headline MACE score anywhere in the repo; every README benchmark row
  has a one-line honest caveat; numbers reproduce from a clean `python3
  benchmarks/*/run.py`.
- **Verify:** grep for the stale headline MACE score; `python3 benchmarks/mace/run.py | grep -i composite`

#### WI-002 · Reconcile identity/trust/drift/reputation language with reality — P0
- **Addresses:** F-A5, F-A6, F-A7, F-A8.
- **Scope:** `README.md` sections "Trust-aware transport", "Agent identity and
  reputation", "Retrieval and self-improving memory"; `docs/NCP_PROTOCOL_SPEC.md`.
- **Approach:** Until Track D lands, soften claims to describe *advisory,
  client-asserted* signals. Specifically: state that (a) identities are
  generated but not yet used to sign/verify writes; (b) `base_trust`/`drift` are
  self-reported inputs, not verified; (c) reputation is computed and displayed
  but does not yet weight retrieval. Add a "Roadmap: making these enforced"
  pointer to Track D below.
- **Acceptance:** No sentence implies runtime authentication or computed drift
  that does not exist. A skeptical reader running the code finds the docs match.
- **Verify:** manual doc review against `grep -rn "sign\|verify" ncp/` (should
  show identity keys are not consumed until WI-01x land).

### Track B — Correctness

#### WI-003 · Make calibration feedback idempotent — P0
- **Addresses:** F-B1.
- **Approach:** Persist a per-chunk watermark (`retrieval_count_at_last_calibration`,
  `dissent_count_at_last_calibration`) and compute boosts from the **delta**
  since the last pass, or reset the counters transactionally inside `calibrate`.
  Apply to SQLite, pgvector, and async pgvector stores (shared helper in
  `stores/calibration.py`). Ensure the reputation rollup consumes the same
  deltas so it does not double-count.
- **Acceptance:** Running `ncp calibrate --feedback` N times with no new activity
  produces the **same** trust/reputation as running it once (idempotent).
- **Verify:** new test `tests/test_calibration_idempotent.py` — write chunks,
  record retrievals, calibrate 3×, assert `base_trust` unchanged after pass 1.

#### WI-004 · Enforce the config token budget on the MCP path — P0
- **Addresses:** F-B2.
- **Approach:** Pass the already-loaded `config` into both `Assembler(store=store)`
  constructions (`server.py:310`, `:433`). Confirm `_context_token_budget`
  resolves from config when the client omits `max_tokens`.
- **Acceptance:** With `context_token_budget=200` in config and no `max_tokens`
  in the request, assembled context respects the budget over `/mcp`.
- **Verify:** integration test hitting `ncp_get_context` and asserting
  `estimate_tokens(context) <= budget` (+ small conscious-block allowance).

#### WI-005 · Make `ncp_fetch` bounded reads real — P1
- **Addresses:** F-B3.
- **Approach:** Clamp `k` to the schema max (`k = min(4, max(1, int(...)))`);
  derive the fetch-session key from a *server-trusted* identifier (pipeline +
  connection) rather than a client-supplied `session_id`, or document that the
  cap is advisory; fix the Redis `remaining` to report the real value; prune the
  in-memory `sessions` dict (LRU/TTL).
- **Acceptance:** A client cannot exceed 3 fetches/turn by rotating `session_id`
  or exceed the per-fetch `k` cap; `sessions` does not grow unbounded.
- **Verify:** test issuing 4 fetches with rotating session ids → 4th rejected;
  `k=500` request → served ≤4 chunks.

#### WI-006 · Enforce chunk `expiry` at read and GC — P1
- **Addresses:** F-B4.
- **Approach:** Add `expiry IS NULL OR expiry > :now` to query/FTS/`get_chunks_by_ids`
  read paths; delete expired chunks in `_soft_gc`/`_hard_gc`. Mirror in pgvector.
- **Acceptance:** A chunk past its `expiry` is neither retrieved nor fetched.
- **Verify:** test writing a chunk with `expiry` in the past → absent from query.

#### WI-007 · Stop silent data loss — P1
- **Addresses:** F-B5.
- **Approach:** (a) When `_is_duplicate` suppresses a write, return a signal the
  assembler surfaces (do not treat `False` as success in `_write_with_retry`).
  (b) Add a `dissent_target` column to the SQLite + pgvector `whispers` schema
  (new migration) and round-trip it. (c) In the `world_check` handler, `continue`
  when `detected_drift` is absent instead of defaulting to 0.0.
- **Acceptance:** Suppressed writes are reported; `dissent_target` survives a
  write→read round-trip on SQLite; a `world_check` without `detected_drift` does
  not zero an agent's drift.
- **Verify:** targeted tests for each of the three.

#### WI-008 · Fix score double-counting — P1
- **Addresses:** F-B6 (and F-B7 as a follow-on).
- **Approach:** Make the encoded/display score equal the single fused
  `RetrievalPolicy.score` relevance, or clearly a *different, single-application*
  freshness metric — remove the second trust × generation multiplication in
  `types.py::effective_score`. Document which number the pidgin block shows.
- **Acceptance:** The score surfaced to the model is comparable to the retrieval
  ranking score; generation penalty applied exactly once.
- **Verify:** unit test asserting `effective_score` monotonic with and not a
  re-squaring of `relevance`.

### Track C — Concurrency & ops

#### WI-009 · Add SQLite `busy_timeout` + write transactions — P0
- **Addresses:** F-C1, F-C2.
- **Approach:** In `_connect`, add `PRAGMA busy_timeout=5000;` (or config-driven).
  Wrap `write()`'s check-then-insert in `BEGIN IMMEDIATE`. Replace the
  `INSERT OR REPLACE` that clobbers `created_at`/`retrieval_count`/`dissent_count`
  with an upsert that preserves them. Fix `consolidate()` to read + merge in one
  transaction (mirror the correct `BEGIN IMMEDIATE` pattern already in `calibrate`).
- **Acceptance:** Concurrent writers do not raise `SQLITE_BUSY` under a small
  contention test; a same-`src` rewrite preserves feedback counters.
- **Verify:** test spawning N threads each writing to the same store; assert no
  `NCPStoreUnavailableError` and counters preserved.

#### WI-010 · Fix the authenticated quickstart — P0
- **Addresses:** F-C3.
- **Approach:** Preferred: **do not auto-mint a token for loopback SQLite** in
  `ncp init` (only generate one when `--store pgvector` or a non-loopback host is
  requested). Alternative: inject the generated token into the client configs
  `ncp init` writes, and add a `headers: {"Authorization": "Bearer <token>"}`
  block to `examples/06`, `examples/07`, and the templates. Document either way.
- **Acceptance:** The documented "run `ncp init`, copy the config, connect" flow
  works without a 401.
- **Verify:** scripted end-to-end — `ncp init` → `ncp serve` → copy config →
  `tools/list` over `/mcp` returns 200.

#### WI-011 · Add pgvector + redis CI coverage — P1
- **Addresses:** F-C4.
- **Approach:** New CI job with `pgvector/pgvector:pg16` and `redis:7` service
  containers (images already in `compose.yaml`); install `.[pgvector,redis,dev]`;
  set `NCP_RUN_PGVECTOR_INTEGRATION=1`; run the migration + store integration
  tests. Also add `pytest-asyncio` to the `dev` extra so async tests don't skip.
- **Acceptance:** CI exercises the durable/coordination tier on every PR; the
  previously-skipped tests run and pass.
- **Verify:** CI run shows pgvector/redis tests executed (not skipped).

#### WI-012 · Add `LICENSE` file — P0 (trivial)
- **Addresses:** F-C5.
- **Approach:** Add a standard MIT `LICENSE` with the correct copyright holder
  (`@kulkarni2u`) and year. Confirm `pyproject` `license` metadata still matches.
- **Acceptance:** `LICENSE` present at repo root, MIT, correct attribution.
- **Verify:** file exists; matches the badge/pyproject.

### Track D — Make the claims real (roadmap; larger, higher-value)

#### WI-013 · Sign and verify chunk/whisper authorship — P1 (feature)
- **Addresses:** F-A5, F-S1 partially, F-S2.
- **Approach:** Use the Ed25519 key `identity create` already generates. On
  write/emit, sign a canonical `(written_by | content-hash | pipeline)` payload;
  store the signature; verify on read/drain and reject or down-trust unverifiable
  authorship. `resolve_identity` should map to the *verified* identity, not the
  spoofable `agent_id`. Consult `revoked_at`.
- **Acceptance:** A write claiming another identity's `written_by` without a
  valid signature is rejected or flagged untrusted; a revoked identity cannot
  write.
- **Verify:** tests for forged authorship and revoked-key rejection.

#### WI-014 · Escape wire-format delimiters in the encoder — P0 (security, ship independently)
- **Addresses:** F-S1.
- **Approach:** Before assembling, neutralize any content/payload line matching
  `^\s*\[NCP:` and the `wsp`/`chunk:`/`src:`/`trust:` grammar (e.g. prefix-escape
  or fence). Applies to `_encode_subconscious` and `_encode_whispers`.
- **Acceptance:** Stored content containing a fake `[NCP:WHISPERS]` block or
  forged `src:`/`trust:` lines cannot inject counterfeit section headers or
  provenance into another agent's assembled context.
- **Verify:** test writing a poison payload → assert the assembled block escapes
  it and the real section count is unchanged. *(This one is small and
  high-ROI — a pipeline can land it before the larger Track D items.)*

#### WI-015 · Feed reputation into retrieval + gate whispers — P1 (feature)
- **Addresses:** F-A6, F-A8, F-S2.
- **Approach:** At `RetrievalPolicy.score` time, blend the author's reputation
  posterior mean into the trust signal (config-weighted, default off→on with a
  small weight). At whisper drain, apply a reputation floor in addition to
  self-declared `confidence`.
- **Acceptance:** A low-reputation author's chunks rank lower and its
  high-`confidence` whispers can be gated; a config flag controls the effect.
- **Verify:** test showing identical chunks re-ranked by author reputation.

#### WI-016 · Compute a real drift signal — P2 (feature)
- **Addresses:** F-A7.
- **Approach:** Replace the honor-system `drift_score` with a computed signal —
  e.g. token/goal-overlap (or embedding cosine when enabled) between the current
  turn's intent and the pipeline's anchor/recent turns. Keep the client-supplied
  value as an override, but default to computed.
- **Acceptance:** `drift_score` moves in response to actual topical divergence in
  a synthetic pipeline, with no client-supplied drift.
- **Verify:** test feeding a coherent then a divergent turn sequence → drift rises.

---

## 3. Suggested Sarathi pipeline shape

Recommended agent/role split over the NCP bus (one `pipeline_id`, bounded
handoffs — dogfoods the protocol):

1. **`planner` (Claude)** — reads this doc, publishes one distilled scope chunk
   per WI to the bus (`layer: procedural`), sets acceptance criteria as memory.
2. **`implementer` (Codex or OpenCode)** — picks a WI, reads bounded context,
   opens the anchored files fresh, applies the change, runs the WI's verify
   command, publishes the outcome.
3. **`reviewer` (the other of Codex/OpenCode)** — reads the fix outcome, runs
   the full `pytest -q`, emits a `dissent` whisper with the specific failure if
   verification regresses, else acknowledges.

**Ordering for the pipeline:** land Track A (WI-001, WI-002) and the trivial/
high-ROI items (WI-012 LICENSE, WI-014 encoder escaping, WI-010 quickstart)
first — they are low-risk and immediately reduce liability. Then Track B
correctness (WI-003 → WI-008), then Track C ops (WI-009, WI-011), then Track D
features (WI-013, WI-015, WI-016) as a follow-on epic.

**Guardrails for every WI:**
- One WI per commit; commit message references the WI id and finding id.
- The WI's `Verify` step must pass, and full `pytest -q` must not regress,
  before the reviewer acknowledges.
- Do not weaken or delete an existing test to make a change pass.
- Do not regress the "Positives" list in §1.4.

---

## 4. Verification baseline (run before starting)

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -e '.[dev,providers]' pytest-asyncio
pytest -q                 # expect all pass except environment-only skips
python3 benchmarks/mace/run.py            # composite currently 0.8915
python3 benchmarks/coding_pipeline/run.py # reproduces the ratios in §1.1
grep -rn "<stale headline MACE score>" .        # WI-001 target: should become empty
grep -rn "sign\|verify" ncp/   # WI-013 target: identity keys unused today
```

---

*Audit performed on `v1.2.1`. Findings are advisory engineering assessments,
verified against the code and by running the benchmarks/tests where noted. The
underlying engineering is sound enough that it does not need inflated numbers —
closing Tracks A–C makes the project honest, and Track D makes the headline
claims true.*
