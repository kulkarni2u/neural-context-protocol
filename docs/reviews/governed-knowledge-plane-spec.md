# ContextPolicy foundation — proposed Sarathi specification

Status: approved by the user; implementation partner requested: Claude.
Baseline: origin/main d59f878, fetched 2026-10-21 (America/Los_Angeles).
Workspace: /Users/sweethome/.codex/worktrees/governed-knowledge-plane/neural-context-protocol
Branch: codex/governed-knowledge-plane

## Goal

Deliver the first independently reviewable part of WI-1701: a validated named ContextPolicy registry and a pure configuration resolver. Existing context requests remain unchanged. This foundation does not yet enforce authorization or select policies on Python/MCP requests.

## Approaches considered

1. **Recommended: foundation followed by integration.** Add strict types, registry and pure resolution now; integrate all request paths in the next slice. This makes compatibility and policy boundaries independently testable without a storage migration.
2. Implement the whole WI-1701 at once. Gives immediately usable policies, but combines registry design with Python/MCP budget differences, store filtering, streaming, and metadata compatibility.
3. Reuse existing RetrievalPolicy as the entire policy. Too narrow: that object governs scoring, not context assembly, budget, lifecycle or authorization. Keep it as an existing scoring primitive.

## Proposed approach

Add ncp/context_policy.py, with ContextPolicy, ContextPolicyRegistry, and a pure resolve_context_policy(config, name) function. Follow the existing Pydantic and SchemaRegistry conventions without changing their contracts. Registry entries live under [context_policies.<name>] in the loaded TOML configuration. Policy version is a positive integer. The registry key supplies the policy name; reject an inconsistent embedded name. Return deterministic name/version/SHA-256 identity plus a detached effective NCPConfig for selected policies; no selection returns the existing config and no policy identity.

Use an explicit allowlist of existing context-assembly settings for the first version:

- retrieval: diversity_lambda, edge_expansion, edge_expansion_decay, edge_max_hops, edge_expansion_types, reduce_fanin_enabled, reduce_fanin_min_cluster, reduce_fanin_similarity_threshold, reduce_fanin_contradict_floor, reduce_fanin_overfetch, fallback_to_trust_recency_enabled.
- budget: context_token_budget, adaptive_budget_enabled, adaptive_budget_floor_tokens, adaptive_budget_ceiling_tokens.
- distillation: enabled, min_chunk_tokens.

Reject unsupported keys, coercive booleans, non-finite numbers, invalid graph edge types, negative depths, invalid positive sizes, out-of-range ratios, and inverted budget ranges. Resolve partial overrides against deployment config before cross-field validation. Preserve the inherited project root. Canonical hash covers validated explicit policy definition including name/version, not secrets or unrelated deployment config; deployment-effective fingerprinting is a separate later concern.

Policy definitions cannot override identity, signature/grounding, handoff, store/backend, embedding/provider, memoization, pipeline-dollar governance, scoring trust weights, or other sections. This restriction prevents the foundation from offering unsupported enforcement guarantees. No user-supplied callback or provider call is permitted.

For selected policies only, budgets must respect deployment budget.max_tokens_per_call. Reject a policy whose resolved context budget or adaptive ceiling exceeds that maximum, or whose resolved adaptive floor exceeds its ceiling. Policies cannot edit max_tokens_per_call itself. Reject invalid deployment maxima at selected-policy resolution. Do not change legacy no-selection semantics; current runtime treats explicit MCP max_tokens as overriding adaptation and does not universally enforce this configuration key. The next integration slice must cap explicit request budgets for selected policies and retain the old path for unselected requests.

Examples use current config key names. Include a neutral balanced-v1 example and an explicitly described evidence-oriented tuning example. Do not label the latter an authorization, provenance, signature, or minimum-trust guarantee: source-layer allowlists and minimum trust need actual retrieval integration first. No automatically selected policy.

## File responsibilities

- ncp/context_policy.py: strict models, registry loading, canonical identity and pure resolution.
- tests/test_context_policy.py: model/registry/resolver regression tests.
- tests/test_config.py: only if config-loading coverage needs extending; generic TOML loader already preserves new sections.
- docs/CONTEXT_POLICY.md: foundation availability, configuration, validation, examples, and explicit integration boundary.
- ncp/templates/config.toml.example: commented optional examples, only if packaging coverage confirms the template remains valid.

Do not add production call sites to ncp/api.py, ncp/assembler.py, ncp/mcp/server.py or stores in this slice. No storage migration, HTTP schema change, runtime default change, new dependency, or release/version bump.

## Success criteria

1. Valid policies resolve deterministically; semantically equivalent key order gives the same policy hash.
2. Unknown names and unknown fields fail clearly; no silent fallback.
3. Strict validation rejects malformed, non-finite, unsafe, or inconsistent values.
4. Resolving one policy cannot mutate deployment config, registry entries, nested lists, or another request's result.
5. None selection preserves configuration identity and all existing runtime behavior, even if an unused policy definition is invalid; validate policies on explicit registry load/selection.
6. Security-relevant deployment settings remain byte-for-byte unchanged by resolution.
7. Selected-policy budget bounds cannot exceed deployment maxima; incompatible inherited ranges fail explicitly.
8. Tests cover equivalent loaded configuration with SQLite, pgvector and async-pgvector configuration settings, without claiming live database coverage for a store-independent module.
9. New targeted tests pass and the full existing suite has no new failures. Record environment skips and distinguish baseline failures.
10. Documentation clearly states that request selection and enforcement are not implemented by this slice.

## Risks and later integration obligations

- A policy overlay must be request-local; mutating the process-level cached config would leak settings between agents.
- Settings are captured in both Assembler and MCP closures; applying an overlay to only one would be incomplete.
- Graph, recent refs, hydration, direct fetches, decision packets, memoization, skill-cache reads, and whispers must be audited before any authorization claim.
- A minimum-trust gate is not equivalent to reweighting trust in ranking.
- Inline registry is sufficient initially; file-based includes and policy inheritance add avoidable ambiguity.
- Policy hashing alone does not identify effective deployment behavior. Explainability should eventually include both identities.

## Reversibility

The foundation is additive and disconnected from runtime request paths. Revert its module/tests/docs to roll back. No persisted rows, schema changes, external calls, or default behavior require recovery.

## Self-review

Goal, alternatives, risks, success criteria and reversibility are specified. This is one bounded foundation slice. Runtime integration and all remaining work items are explicitly separate; no implication that a registry alone delivers governed knowledge flow. No unresolved placeholders. User approved this specification and requested implementation via Sarathi with Claude as implementation partner.
