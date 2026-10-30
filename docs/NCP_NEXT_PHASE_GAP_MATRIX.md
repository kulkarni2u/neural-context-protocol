# NCP Next-Phase Gap Matrix

Assessment of current `main` (post-v1.7.0, PR #58) plus branch
`claude/pending-handoff-fixes` against two inputs:

- **Handoff** — *NCP Pending Implementation Handoff* (reviewed commit `570872a`).
- **Plan** — *NCP Next-Phase Implementation Recommendations* (v1.7+).

Status: **Done** (on this branch, tested) · **Partial** · **Missing**.

## 1. Pending-handoff items

| # | Item | Status | Where | Notes / remaining gap |
|---|------|--------|-------|-----------------------|
| 1 | Async PG outcome row access | Done | `stores/base.py::merge_outcome_chunk_rows`, all 3 stores | Live sync+async PG parity for linked/legacy/combined/empty/duplicate; outcome inserts idempotent on `outcome_id`. In the CI pgvector job. |
| 2 | Python evidence attribution | Done | `api.py::_finish_turn`, `AssemblyResult.evidence_sources` | Post-trim ids only; origins tracked; memo hit links nothing and records the cached result's evidence apart. |
| 3 | Outcomes through consolidation | Done | `chunk_aliases` + `calibration.apply_outcome_aliases`, migration 017 | Equivalent merges only; prior loser applications carried; never across supersession/contradiction. **Not covered:** concurrent PG calibrations racing on the same outcome (that race predates this work). |
| 4a | Memo key context | Done (Python path) | `stores/memo.py::compute_turn_memo_signature` | MCP `ncp_lookup_memo` still keys on host-supplied `task`+`context`; adding pipeline/model there would change existing host signatures, so it needs a decision. |
| 4b | Memo reuse bypasses gates | Done | `memo_ineligibility` used by MCP + run/stream | Behavior change: Python reuse now needs verification by default. |
| 4c | Replacement keeps verification | Done | memo upsert SQL in all 3 stores | |
| 4d | Post-turn batch bypasses checks | Done | `mcp/server.py::_validated_batch_chunk` | Batch path still does not noise-filter or create `raw_ref`. |
| 4e | Fan-in merges before contradiction | Done | `consolidation.py::claim_polarity`, `_mergeable` | Polarity is a cue heuristic. Value conflicts ("Monday" vs "Friday") are still not detected. |
| 5 | Provider cache/usage accounting | Done | `adapters/base.py::TokenUsage`, openai/anthropic/gemini | Cohere/Mistral/Ollama report no cache fields, so nothing to split there. |
| 6 | Streaming usage + whisper acks | Done | `openai.py` `include_usage`; `_finish_turn` acks | Anthropic already read final usage; usage now resets per request. |
| 7 | ContextPolicy runtime | Mostly done (PR #58 + identity on `NCPResponse`) | `context_policy.py`, `api.py` | `ncp_fetch`, decisions, memo and whisper reads ignore policy. The policy is **not** an authorization boundary (see Plan P0-A). |
| 8 | Final response budgets / compact MCP mode | Missing | — | Next candidate: cap serialized `ncp_get_context` output, trim telemetry first, add `truncated` flag and opt-in `compact`. |
| 9 | Stable prompt-cache prefixes | Partial | `encoder.py` puts `[NCP:BUDGET]` last; Anthropic caches the prefix | Not yet audited for timestamps or volatile fields earlier in the prefix, and no prefix-stability test. |
| 10 | Cost-per-success / memo fast-path benchmarks | Missing | `benchmarks/task_success` exists | No benchmark reports memo false-hit rate or cost per verified success. |

## 2. Next-phase plan (P0–P2)

| Recommendation | Status | Existing implementation | Actual gap | Smallest viable change | Risk | Tests | PR |
|---|---|---|---|---|---|---|---|
| P0-A ContextPolicy as **authorization scope** | Missing (only ranking/budget tuning exists) | `context_policy.py` (retrieval/budget/distillation overrides); pipeline scoping via `pipeline_id`/`scope='global'` in store SQL | No repo/task-family/role/environment applicability; no trust floor or provenance requirement; graph expansion, `ncp_fetch`, memo and skill-cache reads are not policy-scoped | Add an `access` section (allowed pipelines, min trust, required `src`/verified) enforced as a **pre-ranking filter** in one shared helper used by retrieval, `_expand_edges`, `ncp_fetch` and memo lookup | High: wrong filter = silent recall loss; must stay opt-in | Per-path tests showing a high-relevance out-of-scope chunk never surfaces, including via graph expansion | **1st** |
| P0-B MemoryPolicy | Missing | Pieces: `require_signatures`, `require_grounded_high_trust`, `src` trust table, consolidation, `refine.py` | No single policy for write eligibility, scope inheritance, promotion, sharing, expiry | Start with an invariant check: derived chunks (`caused_by`/consolidation keeper) cannot exceed the max trust or widen the scope of their sources | Medium | Derivation, consolidation and promotion tests | 2nd |
| P0-C Provenance & lineage | Partial | `caused_by`, `evidence_id`, `raw_ref`, `supersedes`, chunk edges, `chunk_aliases` (new) | No `explain(chunk_id)` that walks to source evidence and reports expired/superseded/contradicted sources | Read-only `ncp explain` / tool walking existing edges | Low | Walk/expiry/supersession tests | 3rd |
| P0-D Cross-session poisoning suite | Partial | `test_protocol_security_posture.py` checks docs; data-not-instructions note in AGENTS.md | No end-to-end inject → persist/consolidate → new session → retrieve → assert no authority gain/scope escape | Test-only PR, built after P0-A so scope escapes have something to assert against | Low | The suite itself | With 1st/2nd |
| P1-A Structured outcome evidence | Partial | `OutcomeRecord` (turn/chunk ids, success, weight, note); evidence attribution (new) | No agent/model/policy version, verification source, tokens/cost/latency; no reported-vs-verified distinction | Add optional fields + `verification` enum (`agent_reported`/`independent`), with a migration | Low, additive | Round-trip on 3 backends | 4th |
| P1-B Versioned procedures | Partial | `refine.py` procedure chains, propose/apply/rollback | No capability id/version, status lifecycle or outcome history | Add status + version metadata on procedure heads | Low | Lifecycle tests | 5th |
| P1-C Failure-aware offline eval | Missing | `ncp/eval.py`, benchmarks | No failure attribution taxonomy | Offline-only report over recorded outcomes + evidence attribution | Low | Fixture-based | Later |
| P1 Native-memory benchmark | Missing | `benchmarks/task_success`, matched-budget efficacy | No native-memory or token-matched-filler arms, no CIs | Add arms to the existing task-success harness | Low (benchmark only) | Reproducible run | Later |
| P1 High-fan-in benchmark | Partial | `benchmarks/fanin_reduce` | No concurrency, provenance-integrity or scoped-retrieval checks | Extend the existing harness | Low | — | Later |
| P2 Retrieval/token efficiency | Partial | MMR, distillation, budget fitting | See handoff 8–10 | — | — | — | After P0 |
| P2 Provider interoperability | Partial | Provider adapters, MCP schemas | Tool schemas are not versioned contracts (no schema snapshot test) | Snapshot test over `TOOLS` | Low | Snapshot | Later |
| Storage parity | Ongoing | SQLite / sync / async PG parity tests | Each change on this branch ships 3-backend tests | — | — | — | — |

## 3. Recommended next PR (not started; needs approval)

**P0-A: policy-scoped access filter, enforced before ranking.**

- *Approach:* an optional `access` section on a context policy, with
  `pipelines`, `min_trust`, `require_verified` and `allowed_src`. It compiles
  to one deterministic predicate. Every read path applies it to candidates
  before scoring: hybrid retrieval, edge expansion, recent refs, `ncp_fetch`
  and memo lookup. No policy means no filter, so default behavior does not
  change.
- *Acceptance:* a chunk that fails the predicate never appears in context,
  `ncp_fetch` results, expansion results or memo hits. This holds even when
  it is the top lexical/vector match or a direct graph neighbour. Tests run
  on all three backends.
- *Compatibility:* additive config. The policy `sha256` changes only for
  policies that set `access`.
