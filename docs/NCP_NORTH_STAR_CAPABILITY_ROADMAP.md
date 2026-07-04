# NCP North-Star Capability Roadmap

> **Goal.** Make NCP the best-in-class agent-to-agent communication substrate,
> measured by one thing: **more useful work per token dollar** across a
> multi-agent system, with trust you can rely on.
>
> **Relationship to the audit.** `NCP_AUDIT_AND_REMEDIATION_PLAN.md` makes NCP
> *correct and honest* (fix bugs, align claims). This document makes it
> *powerful and differentiated* (new capability). They compose: remediation is
> the floor, this is the ceiling. Ship the P0 remediation items first — a
> powerful feature on a corrupt trust loop or an unenforced budget is worthless.
>
> **How to consume this.** Capabilities are grouped by the three pillars the
> product is judged on: **Context (C)**, **Trust (T)**, **Economics (E)**. Each
> `CAP-*` has: *why it matters*, *what best-in-class looks like*, *approach*,
> *impact*, *effort*, and *dependencies*. Effort is S/M/L. This is a roadmap,
> not a sprint — sequence it via the "Execution waves" section.

---

## 0. The thesis, stated precisely

NCP's defensible wedge is a combination **no competitor owns end to end**:

> **Trust-weighted, provenance-authenticated shared memory with real
> token-economic accounting, exposed as one MCP endpoint every agent connects
> to as a peer.**

- Letta/MemGPT, mem0, Zep → memory, but no authenticated trust or cost economics.
- Google A2A → agent transport, but no shared trusted memory.
- LangGraph / CrewAI → orchestration, but context is the orchestrator's problem.

To *own* that wedge, all three legs must be real:

| Pillar | Today (shallow) | Best-in-class (the bar) |
|--------|-----------------|-------------------------|
| **Context** | greedy top-k BM25 + recency; caps | token-optimal selection (no redundancy) + query-aware distillation + semantic work-memoization |
| **Trust** | self-declared floats; keys unused; reputation unwired | signed authorship + grounded claims + outcome-calibrated reputation that actually weights retrieval |
| **Economics** | fabricated telemetry (word counts, `cost=0`) | real per-provider token/USD accounting + adaptive budget + cached work + measured quality-at-budget |

The rest of this doc is how to get each leg there.

---

## Pillar C — Context: maximize work per token

**Principle:** every token in an assembled context must earn its place. The
enemy is not size — it is *redundancy* and *dilution*. The biggest token wins
come from (a) never selecting two chunks that say the same thing, (b) sending
the model a distilled form of what it needs, and (c) not re-deriving what the
bus already knows.

### CAP-C1 · Redundancy-aware selection (MMR / submodular) — impact: HIGH, effort: M
- **Why:** greedy top-k fills a bounded window with near-duplicates — pure
  waste. This is the single highest-ROI token lever in the whole product.
- **Best-in-class:** select the subset that maximizes *marginal* relevance —
  each added chunk must contribute information not already covered. Maximal
  Marginal Relevance (λ·relevance − (1−λ)·max_sim_to_selected) or a submodular
  coverage objective under the token budget.
- **Approach:** in the assembler's fit step, replace "sort by score, take until
  full" with an MMR/greedy-submodular pass over candidate chunks using the
  existing similarity machinery (`consolidation._bm25_similarity`, or embeddings
  when enabled). Config: `[retrieval].diversity_lambda`.
- **Impact:** fits ~1.3–2× more *distinct* information in the same budget on
  redundant corpora (the common multi-agent case). Directly improves the
  matched-budget efficacy benchmark.
- **Deps:** none hard; better with CAP-C4 (embeddings).

### CAP-C2 · Query-aware distillation at assembly time — impact: HIGH, effort: L
- **Why:** today a chunk is either included whole or truncated. Truncation loses
  the wrong tokens; whole-inclusion wastes budget on framing. Best-in-class
  sends the model the *answer-bearing* span, not the raw blob.
- **Best-in-class:** two-stage — extractive (sentence-level relevance to the
  query, cheap/local) then optional abstractive (a small model distills a
  cluster to N tokens with provenance preserved). Distillation is cached on the
  chunk so it's paid once, not per read.
- **Approach:** add a `distill` step between retrieval and encode; store the
  distilled form as a derived chunk linked to the source (reuse the `raw_ref`
  pattern in reverse — `distilled_from`). Deterministic extractive default;
  abstractive behind `[distillation].model` like consolidation already is.
- **Impact:** the core "more work per token" claim, made real and measurable.
- **Deps:** CAP-E1 (real telemetry to prove the win), CAP-C6.

### CAP-C3 · Semantic work-memoization ("don't recompute") — impact: VERY HIGH, effort: L
- **Why:** the biggest token save isn't compressing context — it's *skipping a
  model call entirely* when an equivalent sub-task was already solved. NCP has
  the memory substrate to do this and doesn't.
- **Best-in-class:** each turn/decision is keyed by a *semantic task signature*
  (role + task + slot + salient inputs). Before an agent runs, the bus checks
  for a prior high-trust result with a matching signature and returns it
  (with provenance + a "reused" marker) instead of re-running the provider.
- **Approach:** add `ncp_lookup_result(signature)` (or fold into
  `ncp_get_context` as a `memoize: true` hint); store turn outcomes keyed by a
  content hash of the normalized signature; gate reuse on trust + freshness +
  optional embedding similarity. Expose hit/miss in cost telemetry.
- **Impact:** step-function token savings on pipelines with repeated sub-work
  (the multi-agent norm). This is the headline number worth having.
- **Deps:** CAP-T3 (grounded/outcome trust so you only reuse *good* results),
  CAP-E1.

### CAP-C4 · Cheap embeddings on by default — impact: MED, effort: M
- **Why:** pure BM25 misses paraphrase — agents rarely phrase the same fact the
  same way. For agent-to-agent recall this is a real gap; embeddings are off by
  default so the default experience is lexical-only.
- **Best-in-class:** a small, fast, local embedding model on by default (no API
  key), with BM25 retained in the fusion. Vector path exists only in pgvector
  today — bring a lightweight local option to the SQLite tier.
- **Approach:** add a local embedding provider (e.g. a small sentence model or a
  hashing/`fastembed`-style option), wire into `RetrievalPolicy.score_with_vector`
  for SQLite via a stored blob + brute-force cosine (fine at working-set sizes).
  Keep it optional-but-default-on with a clean off switch.
- **Impact:** materially better recall of paraphrased memory; enables CAP-C1/C3
  to use semantic (not just lexical) similarity.
- **Deps:** none.

### CAP-C5 · Bi-temporal / validity-aware memory — impact: MED, effort: M
- **Why:** facts go stale. `expiry` exists but is unenforced (audit F-B4), and
  there's no notion of "this was true then, superseded now." Zep's edge is
  exactly this; NCP should match it.
- **Best-in-class:** each chunk carries `valid_from`/`valid_to`; retrieval
  prefers currently-valid facts and can answer "what did we believe at turn N."
  Supersession (`supersedes`/`superseded_by`) already half-exists — complete it.
- **Approach:** enforce expiry at read/GC (remediation WI-006), add validity
  fields, prefer valid + non-superseded in scoring, expose an "as-of" query.
- **Impact:** stops serving stale "proven" facts; enables trustworthy long-lived
  pipelines.
- **Deps:** WI-006.

### CAP-C6 · Adaptive per-turn context budget — impact: MED, effort: M
- **Why:** a fixed budget over-spends on easy turns and starves hard ones.
  Best-in-class spends context where uncertainty is highest.
- **Best-in-class:** budget = f(task difficulty / retrieval-score mass /
  dissent presence). Easy, high-confidence turns get a small budget; contested
  or novel turns get more — total spend across the pipeline drops for the same
  or better outcomes.
- **Approach:** compute a difficulty signal (top-k score concentration, presence
  of dissent whispers, drift) and scale the effective budget between
  `[budget]` floor/ceiling. Log the chosen budget in telemetry.
- **Impact:** lower aggregate tokens at equal quality; a clean story for the
  economics benchmark.
- **Deps:** CAP-E1, real drift (WI-016).

---

## Pillar T — Trust: make it real, then make it weigh

**Principle:** a trust score is only worth the verification behind it. Today
trust is self-declared and unused at runtime. Best-in-class trust rests on three
things NCP must actually implement: **who wrote it (authenticated), what grounds
it (evidence), and did it prove out (outcomes).**

### CAP-T1 · Authenticated authorship (sign & verify) — impact: HIGH, effort: M
- **Why:** the Ed25519 keys are generated and never used (audit F-A5). Any
  client writes as any identity, claims any trust, spam-dissents any chunk.
  Without this, every other trust feature is theater.
- **Best-in-class:** writes and whispers are signed with the author's key;
  the bus verifies on ingest and rejects/down-trusts unverifiable authorship;
  revoked identities can't write.
- **Approach:** remediation WI-013 — sign `(written_by | content-hash |
  pipeline)`, verify on write/drain, honor `revoked_at`, make `resolve_identity`
  return the *verified* identity.
- **Impact:** converts "cryptographic identity" from decorative to true; the
  precondition for trust-weighted retrieval to mean anything.
- **Deps:** none (uses existing keystore).

### CAP-T2 · Grounded claims (evidence-linked trust) — impact: HIGH, effort: M
- **Why:** a chunk claiming `src=tool_result` should have to *point at* the tool
  result. Today `src` is a self-asserted label worth 0.95. Trust should derive
  from grounding, not declaration.
- **Best-in-class:** `tool_result`/`user_verified` writes must carry an evidence
  reference (a `raw_ref` to the actual output, or a signed tool attestation).
  Trust is a function of grounding depth; ungrounded high-trust claims are
  rejected or demoted.
- **Approach:** enforce evidence presence for high-trust `src` values at
  `ncp_write_memory`; derive `base_trust` from `src` × grounding, not from the
  client's number. Keep the reversible `raw_ref` machinery that already exists.
- **Impact:** closes the biggest trust hole — you can no longer *assert* your way
  to maximum trust.
- **Deps:** CAP-T1 (so the grounding claim is itself attributable).

### CAP-T3 · Outcome-calibrated reputation (the real self-improving loop) — impact: VERY HIGH, effort: L
- **Why:** calibration currently boosts trust from *retrieval counts* — "used a
  lot" is treated as "correct," and it double-counts (audit F-B1). That's not
  learning. Best-in-class learns from **whether the work succeeded.**
- **Best-in-class:** when a task outcome is known (test passed/failed, reviewer
  accepted/dissented, human verified), back-propagate credit/blame along
  `caused_by` to the chunks and identities that contributed. Reputation is a
  posterior over *outcomes*, not over usage.
- **Approach:** add `ncp_record_outcome(turn_id|chunk_ids, success)`; feed
  outcomes (not retrieval counts) into the Beta rollup; make calibration
  idempotent (WI-003). Retrieval counts become a weak prior at most.
- **Impact:** the genuine self-improving memory the README promises — grounded in
  reality. This is the feature that makes NCP *learn*.
- **Deps:** WI-003 (idempotent calibration), CAP-T1.

### CAP-T4 · Reputation & trust actually weight retrieval + gate whispers — impact: HIGH, effort: M
- **Why:** reputation is computed and displayed but read by *nothing* (audit
  F-A8); whisper delivery trusts self-declared `confidence` (F-S2). Trust with
  no teeth isn't trust.
- **Best-in-class:** author reputation blends into the retrieval trust signal;
  whisper drain applies a reputation floor; a low-reputation author is
  automatically down-weighted everywhere.
- **Approach:** remediation WI-015 — blend reputation into `RetrievalPolicy.score`
  (config-weighted), add a reputation floor at whisper drain.
- **Impact:** makes "trust-aware transport" and "the bus can down-weight it"
  literally true.
- **Deps:** CAP-T3, CAP-T1.

### CAP-T5 · Computed drift + dissent integrity — impact: MED, effort: M
- **Why:** `drift_score` is never computed (F-A7) and dissent is unauthenticated,
  un-deduplicated, sender-recorded (one agent can torch a chunk). Both make the
  trust signal manipulable.
- **Best-in-class:** drift is computed from actual topical divergence; dissent is
  signed, deduped per identity, and weighted by dissenter reputation.
- **Approach:** WI-016 (real drift) + dedupe/authenticate dissent (WI-007 +
  CAP-T1). Drift then feeds CAP-C6 adaptive budgeting.
- **Impact:** trust and drift stop being spoofable honor-system floats.
- **Deps:** CAP-T1, WI-016.

---

## Pillar E — Economics: measure it, then optimize it

**Principle:** you cannot optimize what you fabricate. Cost telemetry is
currently word-counts and `cost_usd=0.0` (audit F-A9). Everything here starts
with making the numbers real, then turning them into levers.

### CAP-E1 · Real per-provider token & cost accounting — impact: HIGH (foundational), effort: M
- **Why:** the "token capital efficiency" thesis is unmeasurable while telemetry
  is faked. Every economics feature depends on this.
- **Best-in-class:** capture actual provider token usage (from the API response)
  and price it per the `[providers]` table; store per-turn input/output/cost with
  stable non-colliding ids; roll up by pipeline/agent/model.
- **Approach:** thread real usage from adapters (they call real providers) into
  `cost_log`; fix the `turn_id` collision + `INSERT OR REPLACE` clobber; for the
  in-process/mock path, mark cost as estimated, not authoritative.
- **Impact:** `ncp cost` becomes trustworthy; unlocks CAP-E2/E3/E4.
- **Deps:** none — do this early.

### CAP-E2 · Cost governor / budget enforcement per pipeline — impact: MED, effort: M
- **Why:** nothing stops a runaway pipeline from burning budget. Best-in-class
  gives operators a hard/soft spend ceiling.
- **Best-in-class:** `[budget].max_usd_per_pipeline` (and per-turn); the bus
  warns at soft cap and can refuse assembly/writes past hard cap, surfacing the
  reason to the host.
- **Approach:** track cumulative pipeline spend (CAP-E1), enforce at
  `ncp_get_context`/`ncp_post_turn`, expose remaining budget in telemetry.
- **Impact:** makes NCP safe to run unattended; a real operator selling point.
- **Deps:** CAP-E1.

### CAP-E3 · Model-tiering signals (enable cheap-model routing) — impact: HIGH, effort: M
- **Why:** README says "teams want to use smaller models safely" and "better
  engineered context for cheaper model calls." The enabler NCP can own — without
  becoming a router — is *emitting the signal* an orchestrator needs to route.
- **Best-in-class:** each assembled context carries a difficulty/confidence/
  sufficiency signal (retrieval score mass, coverage, dissent, drift). An
  orchestrator (Sarathi) routes easy+high-confidence turns to a small model and
  hard+contested turns to a large one. NCP stays the substrate; it hands the
  router a defensible signal.
- **Approach:** compute and expose a `context_sufficiency` + `difficulty` field
  on `ncp_get_context` responses; document the routing contract; keep routing
  out of NCP.
- **Impact:** turns the "smaller models safely" claim into a concrete mechanism —
  arguably the biggest *dollar* lever (small vs large model is 10–30× cost).
- **Deps:** CAP-C6, CAP-E1.

### CAP-E4 · Honest, provider-real efficacy benchmark as the headline — impact: HIGH, effort: M
- **Why:** current headline numbers are token-accounting demonstrations (audit
  §1.1). Best-in-class proves the thesis with *quality at matched budget* on a
  real provider — the only number that survives scrutiny.
- **Best-in-class:** a benchmark that, at a fixed token budget, measures task
  success with a real model for NCP vs sliding-window vs summary — reporting
  quality *and* cost, with confidence intervals across seeds.
- **Approach:** extend `benchmarks/efficacy/` to run a real provider, matched
  budget, multiple seeds; report success-rate and $/task. Lead the README with
  this and demote the token-ratio table to "accounting floor."
- **Impact:** replaces inflated numbers with a defensible one; the credibility
  anchor for the whole product.
- **Deps:** CAP-E1, remediation WI-001.

---

## Execution waves

Sequence so each wave is shippable and de-risks the next. Remediation P0s are
assumed done first (they're the floor).

**Wave 0 — Foundations (unblocks measurement & trust):**
`CAP-E1` (real telemetry) · `CAP-T1` (signed authorship) · `CAP-C4` (default
embeddings). Nothing downstream is meaningful without these.

**Wave 1 — Token wins you can measure:**
`CAP-C1` (MMR selection) · `CAP-C2` (distillation) · `CAP-E4` (real efficacy
benchmark). This is where "more work per token" becomes a provable claim.

**Wave 2 — The differentiators:**
`CAP-C3` (work-memoization) · `CAP-T3` (outcome calibration) · `CAP-T4`
(reputation weights retrieval). The features no competitor combines.

**Wave 3 — Operational maturity:**
`CAP-E2` (cost governor) · `CAP-E3` (model-tiering signals) · `CAP-C6` (adaptive
budget) · `CAP-C5` (bi-temporal) · `CAP-T2` (grounded claims) · `CAP-T5`
(computed drift + dissent integrity).

**North-star metric to track across every wave:** *task success per dollar at a
fixed matched budget, on a real provider* (from CAP-E4). If a capability doesn't
move that number, it isn't earning its complexity.

---

## What "best in its kind" requires — scorecard

| Capability the market expects | NCP today | After this roadmap |
|-------------------------------|-----------|--------------------|
| Bounded, non-redundant context selection | greedy top-k | MMR/submodular (CAP-C1) |
| Query-aware compression | truncate/whole | distillation (CAP-C2) |
| Skip redundant model work | — | memoization (CAP-C3) |
| Semantic recall by default | lexical-only | embeddings on (CAP-C4) |
| Temporal / validity-aware facts | expiry unenforced | bi-temporal (CAP-C5) |
| Authenticated authorship | keys unused | signed (CAP-T1) |
| Evidence-grounded trust | self-declared | grounded (CAP-T2) |
| Learn from outcomes | usage-counting | outcome-calibrated (CAP-T3) |
| Trust that weighs retrieval | display-only | wired (CAP-T4) |
| Real cost telemetry | fabricated | real (CAP-E1) |
| Spend governance | — | governor (CAP-E2) |
| Cheap-model enablement | claimed | signal-emitting (CAP-E3) |
| Credible efficacy proof | token accounting | provider-real (CAP-E4) |

---

*This roadmap is deliberately opinionated. The engineering under NCP is good
enough to support all of it — the gap is that the three pillars the product is
sold on are currently the three thinnest layers. Close them in the order above
and NCP is not just honest (the audit's job) but genuinely the strongest
agent-to-agent context substrate in its category.*
