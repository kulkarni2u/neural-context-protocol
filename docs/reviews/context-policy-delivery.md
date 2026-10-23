# ContextPolicy foundation — delivery evidence

Implemented on `codex/governed-knowledge-plane`, based on fetched `origin/main` `d59f878`. At the implementation handoff, changes were local, uncommitted and unpushed in this isolated worktree. The user subsequently authorized committing and pushing the foundation branch. The original checkout is preserved.

## Delivered

- `ncp/context_policy.py`: strict named ContextPolicy models, registry, deterministic definition hash, and a pure request-local configuration resolver.
- `tests/test_context_policy.py`: 118 tests covering validated inputs, registry loading, deterministic identity, no-policy compatibility, detached nested data, security-setting preservation, budget boundaries, malformed-input errors and usable TOML examples.
- `docs/CONTEXT_POLICY.md`: configuration examples, validation and explicit foundation-only API boundary.
- `docs/reviews/context-policy-implementation-plan.md`: implementation decisions and test-first evidence.

No runtime context-request call sites, storage implementations, migrations, package versions or dependencies changed. The optional config template edit was omitted. Backend checks cover configuration preservation, not live PostgreSQL execution.

## Sarathi and model allocation

Approved brainstorm: `b5b1d266368d4edea7bdcbcdbc258611`.
Sarathi task: `c1e86d235c0a4e909c0b836a262220e6`.
Dispatches used Sarathi's native Claude provider bridge. Actual models were verified from Claude transcript telemetry:

| Role | Model | Session |
|---|---|---|
| Foundation and first review fixes | claude-sonnet-5-5 | bbe86adf-ee95-419a-836e-307010639228 |
| Fresh broad spec/quality review | claude-opus-5-5 | 9b8b8859-9506-475c-be80-e5c03ca42459 |
| Mechanical input guards and prose precision | claude-haiku-5-5 | 8bac9f15-ab49-46d4-aae0-13cfe5e37074 |
| Fresh scoped re-review | Sonnet (explicit alias) | 52d8175b-40a7-4db8-87f0-b8d5d38b65be |

The initial Codex review seat reached its account usage limit after reporting two findings; it was not counted as approval. The independent Opus review and final Sonnet scoped review both passed. Implementers did not delegate more agents.

## Review results

Fixed and regression-tested: explicit inverted budget pairs now fail model/registry validation; registry/resolver parse errors redact rejected values; malformed selected config sections and invalid policy-name types raise ContextPolicyError before dictionary operations. No-selection remains an identity-preserving no-op.

Opus: spec compliance PASS; quality PASS with minor follow-ups. Sonnet scoped re-review: PASS, M2 and M3 addressed, no new breakage.

Remaining non-blocking integration considerations: edge types are now normalized (sorted, de-duplicated, non-empty) before hashing; traversal/overfetch caps require runtime integration and benchmarking. Direct Pydantic validation retains Pydantic's normal error behavior; docs distinguish it from sanitized registry/resolver parsing errors. A loose budget-error regex in one test is a minor test-quality observation, not a missing runtime guard.

## Verification

Coordinator ran the final full suite after the code changes:

`python3 -m pytest -q` — **1644 passed, 13 skipped in 54.43s**, exit 0.

`python3 -m ruff check .` — **All checks passed**, exit 0.

Whitespace checks inspected all six implementation/review deliverables. Baseline before implementation was **1526 passed, 13 skipped**. Thus the foundation adds **118 passing tests**, with the same skip count. No live-provider benchmark or database migration validation is claimed.

Machine ledger, dispatch inputs/outputs and test log: `.sarathi/governed-knowledge-plane/`.
Detailed broad and scoped reviews: `.superpowers/sdd/context-policy-implementation-plan/`.

## Next separately scoped slice

Connect policy selection to Python/MCP/HTTP context requests with shared resolution, identity metadata and request-budget clamping only for explicitly selected policies. That integration was not part of this approval.
