# Context policies (foundation)

**Status: foundation only.** `ncp/context_policy.py` provides a validated registry of named
context policies and a pure resolver. **Request selection and enforcement are not implemented
by this slice**: `ncp/api.py`, the assembler, the MCP server and the stores do not call it, and
no policy is selected automatically. Existing behavior is unchanged.

## Configuration

Policies live in the loaded TOML under `[context_policies.<name>]`. The table key is the policy
name; an embedded `name` is optional but must match the key. `version` is required and must be a
positive integer.

Only these existing settings can be overridden (anything else is rejected):

- `retrieval`: `diversity_lambda`, `edge_expansion`, `edge_expansion_decay`, `edge_max_hops`,
  `edge_expansion_types`, `reduce_fanin_enabled`, `reduce_fanin_min_cluster`,
  `reduce_fanin_similarity_threshold`, `reduce_fanin_contradict_floor`, `reduce_fanin_overfetch`,
  `fallback_to_trust_recency_enabled`.
- `budget`: `context_token_budget`, `adaptive_budget_enabled`, `adaptive_budget_floor_tokens`,
  `adaptive_budget_ceiling_tokens`.
- `distillation`: `enabled`, `min_chunk_tokens`.

Identity, signature/grounding, handoff, store/backend, embedding/provider, memoization,
pipeline-dollar governance, scoring trust weights and `budget.max_tokens_per_call` cannot be set
by a policy.

## Validation

Values are strictly typed: no coercion (`"true"`, `1` for booleans, bool for numbers), no
NaN/infinity, ratios in `[0, 1]`, hops `>= 0`, sizes positive, edge types from the closed set.
An explicitly contradictory pair (`adaptive_budget_floor_tokens` above
`adaptive_budget_ceiling_tokens` in the same policy) is rejected at registry load, without any
deployment config. Partial ranges are deferred to the resolver.

Error sanitization differs by path:

- Registry and resolver parsing errors report field paths and reasons without rejected input
  values. For example, a secret placed under an unsupported key is never echoed. Policy names and
  field names remain visible.
- Direct Pydantic model validation (`ContextPolicy.model_validate`) uses Pydantic's error
  behavior and may include input values. Do not treat direct model errors as sanitized.
- Numeric budget-bound comparisons include the numbers so the failure can be diagnosed.

Partial overrides are merged onto the deployment config first; the resolved result must satisfy
`context_token_budget <= budget.max_tokens_per_call`,
`adaptive_budget_ceiling_tokens <= budget.max_tokens_per_call` and floor `<=` ceiling. An invalid
deployment maximum fails when a policy is selected.

## API

```python
from ncp.context_policy import resolve_context_policy

resolved = resolve_context_policy(config, "balanced-v1")
resolved.config    # detached effective NCPConfig (project_root preserved)
resolved.identity  # name, version, SHA-256 of the explicit policy definition
resolve_context_policy(config, None)  # same config object, identity None, no validation
```

`ContextPolicyRegistry.from_config(config)` validates every entry. Selecting by name validates
only the selected entry. The hash covers the validated policy definition (name, version and
explicit overrides), not deployment config or secrets; a deployment-effective fingerprint is a
later concern.

## Examples

A neutral baseline:

```toml
[context_policies.balanced-v1]
version = 1

[context_policies.balanced-v1.retrieval]
diversity_lambda = 0.7
edge_max_hops = 1

[context_policies.balanced-v1.budget]
context_token_budget = 900
adaptive_budget_floor_tokens = 300
adaptive_budget_ceiling_tokens = 2000
```

An evidence-oriented *tuning* example. It only widens graph expansion and fan-in reduction; it is
**not** an authorization, provenance, signature or minimum-trust guarantee (source-layer
allowlists and minimum trust need retrieval integration first):

```toml
[context_policies.evidence-tuned-v1]
version = 1

[context_policies.evidence-tuned-v1.retrieval]
edge_expansion = true
edge_max_hops = 2
edge_expansion_types = ["caused_by", "supports"]
reduce_fanin_enabled = true
reduce_fanin_overfetch = 8

[context_policies.evidence-tuned-v1.budget]
context_token_budget = 1200
```

## Integration boundary (next slice)

Selection on Python/MCP requests, request-local overlays in both the Assembler and MCP closures,
capping explicit request budgets for selected policies, and any authorization claims are not
part of this slice.
