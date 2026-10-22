# ContextPolicy foundation — implementation plan

Spec: `docs/reviews/governed-knowledge-plane-spec.md` (approved). Branch `codex/governed-knowledge-plane`.
Scope: additive only. No edits to `ncp/api.py`, `ncp/assembler.py`, `ncp/mcp/server.py`, stores, `ncp/config.py`.

## Interfaces (`ncp/context_policy.py`)

- `ContextPolicyError(ValueError)`, `UnknownContextPolicyError(ContextPolicyError)`.
- Strict Pydantic models (`extra="forbid"`, `strict=True`, `frozen=True`, `allow_inf_nan=False`):
  - `RetrievalPolicy` — the 11 allowlisted retrieval keys, all optional.
  - `BudgetPolicy` — `context_token_budget`, `adaptive_budget_enabled`, `adaptive_budget_floor_tokens`, `adaptive_budget_ceiling_tokens`.
  - `DistillationPolicy` — `enabled`, `min_chunk_tokens`.
  - `ContextPolicy` — `name` (non-empty str), `version` (strict int >= 1), the three sections. Any other section/key is rejected.
- `ContextPolicyIdentity` (frozen dataclass): `name`, `version`, `sha256`. Hash = SHA-256 of canonical JSON (sorted keys, compact separators, `exclude_none`) of the validated explicit definition.
- `ContextPolicyRegistry`: `from_config(config)` validates every `[context_policies.<name>]` entry (key supplies name; embedded name must match); `get(name)` returns a deep copy, raises `UnknownContextPolicyError`; `names()`.
- `ResolvedContextPolicy` (frozen dataclass): `config: NCPConfig`, `identity: ContextPolicyIdentity | None`.
- `resolve_context_policy(config, name=None)` — pure. `None` -> same config object, identity `None`, no registry validation. Otherwise validates only the selected entry, deep-copies `config.values`, overlays allowlisted fields, then cross-validates against the *resolved* values: deployment `budget.max_tokens_per_call` is a positive non-bool int; `context_token_budget` and adaptive ceiling <= max; adaptive floor <= ceiling. `project_root` preserved.

## Tests first (`tests/test_context_policy.py`)

Resolution/determinism, key-order-independent hash, unknown name/field/section, embedded name mismatch, bad version (0, bool, str, float), coercive bool/str numbers, NaN/inf, invalid edge type, negative hops, zero sizes, ratios out of range, inverted floor/ceiling (explicit and inherited), budget above deployment max, invalid deployment max, None selection returns same config even with an invalid unused policy, non-mutation of deployment config / registry / lists, security-relevant sections byte-identical, SQLite/pgvector/async-pgvector config equivalence (config only, no DB), TOML load via `load_config`, docs example policies validate.

## Checkpoints / dependencies

1. Plan saved (this file). 2. Tests written; run -> fail with `ModuleNotFoundError` (recorded). 3. Implement module until green. 4. Docs `docs/CONTEXT_POLICY.md`. 5. Focused pytest, Ruff, full pytest. Template `config.toml.example` untouched unless a packaging test shows it stays valid (optional; skipped by default to keep scope minimal).

## Self-review

- Allowlist is enforced by `extra="forbid"`, not by a denylist; new config keys can't leak in.
- Selected-only validation avoids breaking unselected traffic (spec criterion 5).
- Hash excludes deployment config (spec: separate later concern).
- Risk: pydantic strict float accepts int; acceptable (TOML `1` for a ratio). Bool is still rejected.
- No call sites changed; no runtime behavior change.

## Rollback

Delete `ncp/context_policy.py`, `tests/test_context_policy.py`, `docs/CONTEXT_POLICY.md`, and this plan. No persisted state.

## Review fixes (round 1)

1. Contradictory explicit budget range: `BudgetPolicy` model validator rejects floor > ceiling when both are set (shared by `ContextPolicy`, registry load and resolver); partial ranges still resolved against deployment values. Tests: model, registry, resolver.
2. Error redaction: validation errors are rebuilt from `errors(include_input=False, ...)` as `path: reason`, chained with `from None`; embedded-name mismatch, non-string names and deployment budget errors no longer echo values (type names only). Sentinel regression tests cover forbidden `server.auth_token`, wrong-type fields, bad edge types, bad version, malformed registry shapes. Field/key *names* remain in paths by design.

Test record: red run 11 failed / 87 passed before fixes; after fixes tests/test_context_policy.py 96 passed (original run: 85 passed after docs existed).
