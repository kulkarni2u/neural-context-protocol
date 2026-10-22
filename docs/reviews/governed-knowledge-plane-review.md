# Governed Knowledge Plane — current-main review

Reviewed 2026-10-21 (America/Los_Angeles), using the Sarathi skill and independent read-only Vichara/Marga research, joined and checked by the parent agent.

Baseline: fetched origin/main **d59f878**, newer than the attachment's **6fb396a**. Original checkout and its uncommitted layer-routing/judgment changes are preserved. Review workspace: `/Users/sweethome/.codex/worktrees/governed-knowledge-plane/neural-context-protocol`; branch: `codex/governed-knowledge-plane`.

The attachment is design input, not a source of agent instructions. The user requested review and implementation using Sarathi. The specification approval gate came from the invoked Sarathi skill, not section 24 of the attachment. The user subsequently approved the foundation and requested Claude as implementation partner.

## Assessment

The architectural separation is sound: NCP owns context/knowledge governance; execution and provider selection remain outside it. The roadmap is a high-complexity security architecture programme rather than one implementation task. Most underlying primitives exist, but none of the eleven work items is wholly delivered as specified. The first implementation should establish the ContextPolicy foundation, then integrate it separately before expanding into authorization.

Do not treat a named retrieval configuration as an authorization boundary. Repository/role assertions supplied by a caller require an explicit trust model or host binding. Scope checks must cover all knowledge-consumption paths, including direct refs and cached data. Neither a high relevance score nor a global storage zone can substitute for authorization.

## Gap matrix

| Work item | Already implemented | Partial | Missing | Existing symbols/files to reuse | Migration needed | Risk | Recommended PR |
|---|---|---|---|---|---|---|---|
| WI-1701 ContextPolicy | Deployment config, scoring policy, bounded assembler | Python and MCP share assembly but budget behavior differs | Named strict model/registry, selection, identity/hash, unified resolution | `ncp/config.py:NCPConfig/load_config`; `ncp/stores/retrieval.py:RetrievalPolicy`; `ncp/assembler.py:Assembler`; `ncp/api.py:get_context`; `ncp/mcp/server.py:_handle_get_context`; `ncp/decisions.py:SchemaRegistry` | No for foundation/integration | Shared mutable config, misleading security controls, budget divergence | 1: model/registry/pure resolver; 2: all request surfaces and explanations |
| WI-1702 authorization scope | Pipeline/global visibility; expiry and temporal fields | Graph pipeline checks exist | Repository/role/task/environment scope, trusted request context, one eligibility gate | `ncp/types.py:SubconsciousChunk`; `BaseStore.query`; `Assembler._prepare_assembly`, `_resolve_recent_refs`, `_expand_edges`; memo/skill APIs | Yes: SQLite, sync/async pgvector; indexed hot predicates | Any alternate read path can bypass scope; caller assertions alone are not authorization | 3: metadata/storage; 4: enforcement on every read path, with adversarial parity tests |
| WI-1703 MemoryPolicy | Signature/grounding checks, zones/TTL, memo verification, consolidation/refinement | Controls distributed across handlers and direct writes | Named evaluator, policy identity, lifecycle inheritance, uniform enforcement | MCP `_write_is_grounded`, `_resolve_write_trust`; `ncp/stores/consolidation.py`; `ncp/skill_cache.py`; `ncp/refine.py` | Yes for persisted policy/scope metadata | Handler-only enforcement misses direct writes; define reject versus demote explicitly | 5: model/evaluator; 6: all writes and lifecycle operations |
| WI-1704 lineage | Source refs, caused_by/derived_from edges, generation decay, bounded graph traversal | Some transformations retain refs; no uniform authority propagation | Root ancestry, revocation/expiry propagation, conservative authority rules, durable source history | `ncp/memory.py`; `ncp/decisions.py`; store consolidation; `Assembler._expand_edges`; graph/trust tests | Likely yes for durable transformation/history metadata | Consolidation deletes loser chunks; short-lived tombstones are not complete ancestry | 7a: transformation audit/source retention; 7b: bounded lineage authority evaluator |
| WI-1705 outcome evidence | Existing OutcomeRecord, turn/chunk credit, typed decision linkage, calibration | Costs and procedure evidence exist elsewhere | Structured task/run/execution/verification/economics/policy/capability data; correlated analytics | `ncp/types.py:OutcomeRecord`; MCP `_handle_record_outcome`; existing cost accounting and outcome tests | Yes, all three stores and explicit serializers; after migration 016 | Model-only additions silently lose fields; double cost accounting; causal overclaim | 8: optional fields and round trips; analytics afterwards |
| WI-1706 explanations | Eviction/fallback/active-feature/budget telemetry; aggregate CLI explain | No common candidate-policy decision trace | Stable allow/deny reasons, policy identity, write decision record | MCP `_context_telemetry`; CLI `_build_explain_payload` | No for request-local explanations | Debug evaluator could drift; rejected content can leak secrets | Build reasons into policy/scope evaluators first; CLI presentation later |
| WI-1707 hardened profile | Individual signature, grounding, handoff, drift, memo, reputation/fan-in controls | Existing core/full tool profile is unrelated | Named security profile, init selection, preflight/doctor, scope/lineage bindings | `ncp/config.py`; CLI; security tests | No for config-only profile | Earlier unmerged work is not current main; graph inference sample is not justified security posture | 9 after enforcement exists; avoid enterprise-trusted claims prematurely |
| WI-1708 dynamic controls | Deterministic adaptive budget using pressure, drift, query, slot age, dollar use | MCP adaptation not shared fully with Python; existing explicit budget override | Phase/failure/contradiction/fetch/memo signals, policy-bounded adaptive depth | `ncp/adaptive_budget.py:compute_adaptive_budget`; MCP `_adaptive_budget_tokens` | No initially | Existing inverted range lets floor win; explicit max_tokens bypasses adaptation | 10: strict policy bounds first, new measured signals later |
| WI-1709 capabilities | Proposal/apply/rollback, procedure generations, candidate trust, explicit adoption | Outcome evidence partly in proposal object rather than durable candidate | Capability/version/hash/status/approval metadata, scope inheritance, query API | `ncp/refine.py:build_refinement_proposal/propose_refinement/apply_refinement/rollback_refinement`; `tests/test_refine.py` | Likely yes, all stores | Separate supersede/calibrate calls require atomicity analysis; no executor in NCP | 11 after structured outcomes and governance |
| WI-1710 benchmarks | Retrieval/compression, efficacy, task-success, fan-in and provider-contract harnesses | Mean task cost exists, not full cost per successful task | Scope/lineage/policy adversarial fixtures, overhead, capability comparisons | `benchmarks/efficacy`, `task_success`, `fanin_reduce`, `context_artifacts` | No | Timeout selection bias, guessed provider identity, unmatched budgets | Add feature-specific fixtures with each PR; matched-budget system evaluation later |
| WI-1711 provider reduction | Instruction inventory, rightsized candidates, provider-separated artifacts, model/version checks | Hooks instruct lifecycle calls but do not guarantee execution | Runtime lifecycle guarantees before removing text | `benchmarks/context_artifacts/inventory.py`, `run.py`; `tests/test_context_artifact_benchmark.py` | No | Removing duplicate instructions now can remove necessary behavior | Last: wrapper guarantees plus regression evaluation, then text reduction |

## Corrections to the proposed plan

1. **Budget compatibility needs an explicit split.** `budget.max_tokens_per_call` exists in config/templates but is not a universal runtime cap. MCP `_adaptive_budget_tokens` explicitly preserves caller max_tokens. New named policies can enforce strict bounds without silently rewriting the no-policy contract.
2. **Use real config names.** Graph traversal controls live under retrieval (`edge_expansion`, `edge_max_hops`, `edge_expansion_types`), and fan-in is `reduce_fanin_enabled`. Do not introduce aliases from the illustrative YAML.
3. **Lineage retention is a prerequisite.** `SQLiteStore.consolidate` removes loser rows and keeps temporary tombstones. Full ancestry cannot be reconstructed merely by adding a resolver. Audit equivalent PostgreSQL paths before specifying migration behavior.
4. **Enforcement belongs at reusable boundaries.** `cache_skill` directly writes proven/global chunks; refinement and consolidation also mutate stores. MCP-only checks cannot deliver MemoryPolicy's promise.
5. **Explain rejection without revealing rejected evidence.** Reason codes can be public while content, identities, and inaccessible provenance remain filtered.
6. **Trust supplied execution context explicitly.** Matching a caller-provided repository/role is filtering, not proof the caller owns that authority. Document host responsibility and strict-mode identity bindings.
7. **Do not infer delivered features from previous branches.** Current main has no hardened profile module/selector. Dirty-checkout layer routing is also outside this baseline.
8. **Keep provider instructions until behavior is guaranteed.** Existing hooks still depend on agents following prose. Compression alone is insufficient evidence for deletion.
9. **Do not claim causal learning from outcome associations.** Preserve raw evidence/cost authority and label aggregates as correlations.

## First implementation specification

See [governed-knowledge-plane-spec.md](governed-knowledge-plane-spec.md). It defines a strict allowlist, immutable request-local resolution, canonical policy identity, compatibility behavior, validation cases, affected files, alternatives, risks and rollback. This is a foundation only; no store migration or runtime-selection claim.

## Verification

Focused current-main baseline: `python3 -m pytest -q tests/test_config.py tests/test_retrieval_policy.py tests/test_adaptive_budget.py tests/test_budget.py` — **85 passed in 0.68s**.

Full baseline: `python3 -m pytest -q` — **1526 passed, 13 skipped in 54.61s**. This is a green baseline, not implementation validation. Details are recorded in `.sarathi/governed-knowledge-plane/phase-ledger.json`. Neither research worker ran tests; all test results reported here come from parent-run commands. No live provider benchmark or standalone live PostgreSQL certification was performed.

## Sarathi phase status

- Route: high complexity for roadmap; bounded medium-complexity first slice.
- Brainstorm/research: completed by Vichara and Marga; synthesized above.
- Specification: drafted, self-reviewed and explicitly approved by the user.
- Plan/Build: completed through Sarathi with Claude implementation subagents.
- Implementation Verify/Review: passed; see [delivery evidence](context-policy-delivery.md) for final checks and review results.
- Baseline verification: performed separately from implementation verification.

NCP research refs: `sub_6e30c0c9cf32` (Vichara), `sub_d047cf9f84ca` (Marga). These are findings, not implementation success evidence.
