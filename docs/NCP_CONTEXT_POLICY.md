# Context policies

`ncp/context_policy.py` provides a validated registry of named context policies and a pure
resolver. Context assembly selects a policy per request by name: `ncp_get_context` takes a
`context_policy` argument, and `ncp.api.get_context`, `run` and `stream` take a `context_policy`
keyword. No policy is selected automatically, and requests that do not name one behave exactly as
before.

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

## Precedence

A selected policy is applied last, on top of the fully loaded config:

1. built-in defaults (`DEFAULT_CONFIG`)
2. the TOML file
3. `NCP_*` environment overrides
4. the selected policy's explicit overrides

So a policy value beats an `NCP_*` environment variable for the same setting. Settings a policy
leaves unset keep whatever the first three layers produced, and the budget bounds below are
checked against that merged result.

## Validation

Values are strictly typed: strings are never converted (`"true"`, `"0.5"`), booleans accept only
`true`/`false` (not `1`), numbers reject booleans, and integer fields reject floats. The one
widening allowed is an integer for a float field (`diversity_lambda = 1` resolves to `1.0`). No
NaN/infinity, ratios in `[0, 1]`, hops `>= 0`, sizes positive, edge types from the closed set.
`edge_expansion_types` must be non-empty (disable expansion with `edge_expansion = false` or
`edge_max_hops = 0`) and is normalized to a sorted, de-duplicated list, so order and repeats do
not change the resolved config or the identity hash.
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

With a policy selected, `resolved.config` is a deep copy and is safe to modify. With `None`,
`resolved.config` **is** the caller's config object, not a copy: treat it as read-only, because a
change to it changes the deployment config for every later request.

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

## Selecting a policy per request

```bash
curl -s http://127.0.0.1:4242/mcp -H 'Content-Type: application/json' -d '{
  "jsonrpc": "2.0", "id": 1, "method": "tools/call",
  "params": {"name": "ncp_get_context", "arguments": {
    "agent_id": "executor", "role": "build",
    "task": "fix_payment_bug", "slot": "payment", "intent": "advance",
    "context_policy": "balanced-v1"
  }}}'
```

```python
from ncp import api

context = api.get_context(agent=agent, context_policy="balanced-v1")
```

What a selected policy changes for that request only:

- The policy is resolved onto a detached copy of the deployment config for each request. The
  Assembler and the MCP server's adaptive budget and `active_features` telemetry all read that
  copy, so one agent's policy never leaks into another agent's request or into the cached config.
- An explicit `max_tokens` is capped at `budget.max_tokens_per_call`. Without a policy, an
  explicit `max_tokens` is still honored as-is.
- The `ncp_get_context` response (and the streamed `handler_result`) carries a `context_policy`
  block: `name`, `version`, `sha256`, `max_tokens_cap`, and `max_tokens_capped_from` when the
  request's `max_tokens` was lowered.

Errors: an unknown name raises `UnknownContextPolicyError`, an invalid definition or an
out-of-bounds resolved budget raises `ContextPolicyError` (both `ValueError`s; over MCP they are
JSON-RPC errors with the sanitized message). Naming a policy on an MCP server running without a
loaded config is rejected.

## Not covered

- Only context assembly consults a policy. `ncp_fetch`, decision packets, memoization,
  skill-cache reads and whispers ignore it.
- Policies tune retrieval; they are not an authorization, provenance or minimum-trust boundary.
  Before any such claim, every read path above needs auditing, and a minimum-trust gate is not
  the same as reweighting trust in ranking.
- There is no deployment-wide default policy; each request must name one.
- The policy hash identifies the policy definition, not effective deployment behavior; request
  explanations should eventually report both.
