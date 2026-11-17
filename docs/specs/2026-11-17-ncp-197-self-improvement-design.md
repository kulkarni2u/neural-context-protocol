# NCP v1.9.7 — Improvement records, guarded pruning, outcome context

Status: approved for implementation (2026-11-17)
Sources:
- SIM — "Self-Improvement Memory Enables Continual Recursive Self-Improvement", arXiv 2611.18856
- EfficientSkill — "Learning Skills for Token-Efficient Multi-Turn Agents", arXiv 2611.19823

## Why

1. SIM: the optimizer's own history is what makes continued improvement work.
   That history is problems → attempts, each with a rationale, predicted vs.
   observed outcome, and evaluation context (model/benchmark). Problem names
   alone do not help reliably. The history must survive after an edit is
   replaced.
2. EfficientSkill: accuracy-only repair bloats procedures. Pruning works only
   *after* a working anchor exists, under an accuracy gate
   (≥ 0.9 × anchor rate) with lower token cost. Pruning from a cold start
   stalls.

NCP today:
- `ncp/refine.py` only appends notes.
- Outcomes carry no cost, model, task or source.
- Trust changes only when an operator runs `ncp calibrate`.
- The adaptive budget can shrink slots that have never succeeded.
- Batch calibrate decays user-registered skills.
- `apply_refinement` does not check that the predecessor is still the head.

## Scope (all in v1.9.7)

| ID | Item |
|---|---|
| R1 | Improvement records: problem → attempts store, tables (not chunks, so never decayed/consolidated), aliases for merges |
| R2 | Outcome context: model, task, slot, pipeline_id, benchmark, eval_source, output/input tokens, turns |
| R3 | Auto-calibrate (feedback mode, debounced) after `ncp_record_outcome`; `self_report_weight` knob |
| R4 | Prune proposals with anchor requirement + apply gate; repair/prune attempts logged as R1 attempts with predicted/fixed/missed |
| R5 | `apply_refinement` stale-head check; `reject_refinement`; rollback marks attempt `rolled_back` |
| R6 | `ncp_improvement_log` MCP tool + `ncp improvements` CLI; `[NCP:IMPROVEMENT]` block rendered untruncated (warn over cap), flags context mismatch vs. caller model |
| R7 | Adaptive budget does not shrink below requested for task/slot with no successful outcome (`[budget].protect_unproven_slots`) |
| R8 | `ncp_skill get` `detail:"outline"` — headings + first line + code reduced to def/class signatures |
| R9 | Batch gen-0 decay exempts user-registered skills |
| R10 | `paired_comparison` helper; `refine_loop` benchmark + CI gate |
| R11 | Docs (new `docs/NCP_SELF_IMPROVEMENT.md`, HTTP API catalog, AGENTS.md rules), CHANGELOG, version 1.9.7 |

Out of scope:
- Model-conditioned trust inside retrieval scoring. Context matching applies
  to refine gates and the improvement block only. Reason: per-model trust in
  the ranker needs per-model trust state; that is a separate design.
- An LLM proposer inside NCP. Prune content is supplied by the caller (agent
  or operator), consistent with refine's no-model design.
- Redis store support for outcomes/improvements (Redis has none today).

## Interfaces (binding for all workers)

### Types (`ncp/types.py`)

`OutcomeRecord` new optional fields (all default `None`, except
`eval_source`):
- `pipeline_id: str | None`
- `task: str | None`
- `slot: str | None`
- `model: str | None`
- `benchmark: str | None`
- `eval_source: Literal["self_report","verifier","human"] = "self_report"`
- `output_tokens: int | None`, `input_tokens: int | None`, `turns: int | None`
  (each ≥ 0)

```python
class ImprovementProblem(NCPModel):
    problem_id: str          # "prob_" + sha256(f"{pipeline_id or ''}|{scope}|{normalized name}")[:16]
    pipeline_id: str | None = None
    scope: str               # procedure key or task slug
    name: str                # root-cause hypothesis, <= 300 chars
    opened_at: float
    alias_of: str | None = None

class ImprovementAttempt(NCPModel):
    attempt_id: str          # "att_<ms>_<uuid12>"
    problem_id: str
    kind: Literal["repair","prune","manual"]
    status: Literal["proposed","applied","rejected","rolled_back"] = "proposed"
    accepted: bool = False
    what: str                # change summary, <= 500 chars
    rationale: str | None = None   # <= 1000 chars
    files: list[str] = []
    candidate_chunk_id: str | None = None
    predicted: list[str] = []      # task ids expected to be fixed
    fixed: list[str] = []
    missed: list[str] = []
    passed: float | None = None    # weighted success count observed
    total: float | None = None     # weighted outcome count observed
    mean_output_tokens: float | None = None
    anchor_rate: float | None = None
    anchor_output_tokens: float | None = None
    model: str | None = None
    benchmark: str | None = None
    note: str | None = None        # e.g. reject reason
    created_at: float
    resolved_at: float | None = None

class OutcomeStats(NCPModel):
    n: int; total_weight: float; success_weight: float
    success_rate: float | None          # None when total_weight == 0
    mean_output_tokens: float | None    # over outcomes that report it
    mean_turns: float | None
    tasks_succeeded: list[str]; tasks_failed: list[str]  # sorted, distinct, from outcome.task
```

Problem name normalization: strip, collapse whitespace, casefold. The same
normalized name in the same `(pipeline_id, scope)` is the same problem
(deterministic merge, as in SIM D.1).

### Store (`ncp/stores/base.py`; implement in `sqlite.py` and `pgvector.py`; `pgvector_async.py` via existing async patterns, or `NotImplementedError` with a clear message if the pattern does not fit — report which)

Base defaults raise
`NotImplementedError("improvement records require the sqlite or pgvector store")`,
except `has_success_outcome`, which returns `False`.

- `upsert_improvement_problem(problem) -> ImprovementProblem` — returns the
  existing row if the id exists; follows `alias_of`.
- `get_improvement_problem(problem_id) -> ImprovementProblem | None` —
  follows `alias_of`.
- `list_improvement_problems(*, pipeline_id=None, scope=None, limit=100) -> list[ImprovementProblem]` —
  excludes aliased rows.
- `merge_improvement_problems(*, src_problem_id, dst_problem_id) -> None` —
  sets `src.alias_of = dst` and re-points `src`'s attempts to `dst`. Attempts
  are never deleted.
- `record_improvement_attempt(attempt) -> ImprovementAttempt`
- `update_improvement_attempt(attempt_id, **fields) -> ImprovementAttempt`
  (whitelisted fields only)
- `list_improvement_attempts(*, problem_id=None, candidate_chunk_id=None, limit=200) -> list[ImprovementAttempt]` —
  ordered by `created_at`.
- `outcome_stats(*, chunk_ids, model=None) -> OutcomeStats` — filtering
  happens in SQL over all matching outcomes (no 2000-row window). Use
  `json_each` on sqlite and `jsonb` on pgvector. `model=None` means all
  models.
- `has_success_outcome(*, pipeline_id=None, task, slot=None) -> bool`

Schema:
- SQLite: inline `SCHEMA`, plus the ALTER try-list for the new outcomes
  columns.
- Pgvector: `ncp/migrations/020_self_improvement.sql` (UP/DOWN) adding the
  outcomes columns and the tables `improvement_problems` and
  `improvement_attempts`. List columns are JSON.
- Index: `outcomes(task, slot)` and `improvement_attempts(problem_id)`,
  `(candidate_chunk_id)`.

### Calibration

- `[calibration].self_report_weight` (float in [0,1], default 1.0 = unchanged
  behavior). In feedback calibration, an outcome's effective weight is
  `weight × self_report_weight` when `eval_source == "self_report"`.
- `[calibration].auto_on_outcome` (default true) and
  `auto_min_interval_seconds` (default 60). After a successful
  `ncp_record_outcome`, if the interval has elapsed for that pipeline
  (in-process monotonic clock, keyed by `pipeline_id or ""`), call
  `store.calibrate(feedback_mode=True, pipeline_id=...)`.
  - It must not run the batch gen-0 decay. If `feedback_mode=True` currently
    also decays, add a keyword (e.g. `apply_decay: bool = True`) and pass
    `False`.
  - Failures are logged, never raised to the client.
  - The response gains `"calibrated": bool`.
  - Stores without calibrate (or with an async `NotImplementedError`) are
    skipped.
- R9: the batch gen-0 decay skips chunks that are user-registered skills,
  using the marker `skill_cache` writes for `registered_by="user"`. Apply this
  in sqlite, pgvector and pgvector_async.

### MCP `ncp_record_outcome`

Schema adds the optional properties `pipeline_id`, `task`, `slot`, `model`,
`benchmark`, `eval_source` (enum), `output_tokens`, `input_tokens`, `turns`
(integers ≥ 0). If `task`/`slot`/`pipeline_id` are omitted, the handler fills
them from the session's last `ncp_get_context`, using the same server-held
mechanism as `retrieved_chunk_ids`. It validates types the same way as the
existing fields.

### Refine (`ncp/refine.py`)

Exceptions: `RefineError` (existing or new base) and
`RefineGateError(RefineError)`. The latter carries
`.details: dict` (anchor_rate, candidate_rate, n, token means, required).

- `propose_refinement(..., problem_id=None, problem_name=None, rationale=None, predicted=None, model=None)`
  - When a candidate is written (not dry-run), it records a `repair` attempt.
    The problem is `problem_id`, else `problem_name`, else the default name
    `f"recurring failures in {key}"` (scope = key).
  - `what` = the proposal rationale summary; `candidate_chunk_id` = the
    candidate.
  - Improvement storage that raises `NotImplementedError` is skipped silently.
- `propose_prune(store, *, key, content, pipeline_id=None, problem_id=None, problem_name=None, rationale=None, predicted=None, model=None, min_anchor_outcomes=5, dry_run=False) -> RefinementResult`
  - Anchor = `outcome_stats` over the head's chunk id (model-filtered when
    `model` is given). It requires `total_weight >= min_anchor_outcomes`,
    else `RefineError("no anchor: head has N outcomes; collect outcomes or run a repair first")`.
  - `content` must be strictly shorter (chars) than the head content, else
    `RefineError`. Content is ≤ 2000 chars.
  - The candidate is written exactly like a repair candidate (procedural,
    `src="synthesis"`, trust 0.4, supersedes head, generation + 1,
    `supersedes` edge, `written_by="ncp:refine"`).
  - Records a `prune` attempt with `anchor_rate` and `anchor_output_tokens`.
    The problem default name is `f"token cost of {key}"`.
- `apply_refinement(store, *, chunk_id, promote_trust=0.80, write_to=None, force=False, gate_ratio=0.9, min_trial_outcomes=5)`
  - Stale-head check: `candidate.supersedes` must equal the current head's
    chunk_id, else `RefineError`. This applies even with `force`.
  - Prune attempts are gated on the candidate's `outcome_stats` (filtered by
    `attempt.model` when set):
    - `total_weight >= min_trial_outcomes`
    - `success_rate >= gate_ratio × anchor_rate`
    - when both token means are known, candidate mean < anchor mean
    - on failure: `RefineGateError`, unless `force`
  - Repair attempts are not gated (unchanged behavior).
  - On success: attempt status `applied`, `accepted=True`, `resolved_at`.
    Observed `passed`/`total`/`mean_output_tokens` come from candidate stats;
    `fixed = predicted ∩ tasks_succeeded`; `missed = predicted − fixed`.
    Applies to both kinds.
- `reject_refinement(store, *, chunk_id, reason=None) -> ImprovementAttempt`
  — attempt status `rejected` and observed stats filled. The candidate chunk
  is left in place.
- `rollback_refinement` — additionally marks the attempt whose
  `candidate_chunk_id` was the reverted head as `rolled_back` (if one exists).
- Records are append-only in spirit: never delete attempts.

### Improvement block (`ncp/improvement.py`, new)

`render_improvement_block(problems_with_attempts, *, scope=None, current_model=None, warn_chars=8000) -> tuple[str, dict]`

Returns text and telemetry `{"chars": int, "oversize": bool, "problems": n, "attempts": m}`.
It never truncates. Deterministic format:

```
[NCP:IMPROVEMENT] scope:<scope|*> problems:<n> attempts:<m>
<problem_id> <name>
  - <status> <kind> <what> | why:<rationale> | predicted:<a,b> fixed:<a> missed:<b> | pass:<passed>/<total> out_tok:<int> | ctx:model=<m>,bench=<b>[ !model]
```

- Omit empty fields.
- `!model` marks an attempt whose model is set and differs from
  `current_model`.
- Problems are ordered by `opened_at`; attempts by `created_at`.

Also `build_improvement_view(store, *, pipeline_id=None, scope=None) -> list[tuple[ImprovementProblem, list[ImprovementAttempt]]]`.

### MCP `ncp_improvement_log`

Actions:
- `get` (`scope?`, `pipeline_id?`, `model?`) → `{block, telemetry}`
- `list_problems`
- `record_attempt` — for external optimization loops, e.g. edits to
  AGENTS.md or harness code.
  - Requires `problem_id` or `problem_name` (must cite), plus `scope`,
    `what`, `kind="manual"`.
  - Optional: `rationale`, `predicted`, `files`, `model`, `benchmark`,
    `candidate_chunk_id`.
- `resolve_attempt` — `attempt_id`, `status`, plus any of `fixed`, `missed`,
  `passed`, `total`, `mean_output_tokens`, `note`.
- `merge` — `src_problem_id`, `dst_problem_id`.

Wiring:
- Add the tool to the standard and full tool profiles, following how other
  tools are wired (verify profile wiring; the research noted `ncp_skill` may
  be missing from STANDARD).
- Add it to the `docs/NCP_HTTP_API.md` catalog (test_docs_tool_catalog
  enforces this).

### CLI

- `ncp refine propose` gains `--problem-id`, `--problem`, `--rationale`,
  `--predict` (repeatable) and `--model`.
- New `ncp refine prune KEY --file F` with the same problem flags, plus
  `--min-anchor-outcomes` and `--dry-run`.
- `ncp refine apply` gains `--force`, `--gate-ratio` and
  `--min-trial-outcomes`. A gate failure prints the details and exits 2.
- New `ncp refine reject CHUNK_ID [--reason]`.
- New group `ncp improvements show [--scope] [--pipeline-id] [--model]` and
  `ncp improvements merge SRC DST`.
- New config keys under `[refine]`: `gate_ratio=0.9`,
  `min_trial_outcomes=5`, `min_anchor_outcomes=5`.
- New `[improvement]` section: `warn_chars=8000`.

### Adaptive budget (R7)

- `compute_adaptive_budget(..., unproven: bool = False)`. When `unproven`,
  `scale = max(scale, 1.0)` before the dollar cut. The dollar cut still
  applies.
- In the server, `unproven = config.budget.protect_unproven_slots and not store.has_success_outcome(pipeline_id=, task=, slot=)`.
  Exceptions count as `False`. The default is true.
- Telemetry: `budget_unproven_protected: bool`.

### Skill outline (R8)

- `ncp_skill` action `get` takes an optional `detail: "full" | "outline"`
  (default `"full"`, unchanged).
- Outline renders:
  - every markdown heading line
  - the first non-empty non-heading line under each heading
  - fenced code blocks reduced to lines matching
    `^\s*(async\s+def|def|class)\s` (in Python or untagged fences), followed
    by `  ... (<n> lines; detail:full for code)`
  - other prose lines dropped
- The header carries `detail:outline`.
- CLI `ncp skill get --detail`.
- Pure function `outline_skill(text) -> str` in `ncp/skill_cache.py`.

### Paired comparison + benchmark (R10)

`ncp/eval.py`:
`paired_comparison(a, b) -> PairedResult(n, mean_diff, ci95_low, ci95_high, wins, losses, ties, sign_test_p)`.
- diff = b − a.
- t critical values from an embedded two-sided 95% table for df 1–30, else
  1.96.
- Exact two-sided binomial sign test, excluding ties.
- No scipy.

`ncp/bench/refine_loop.py` + `benchmarks/refine_loop/run.py`: a deterministic
simulation on a temp SQLiteStore.
- **Checks**, each a boolean in the summary:
  1. ingest seed
  2. failures → repair propose → trial outcomes → apply
  3. prune refused before the anchor exists
  4. good prune (shorter, rate ≥ gate, fewer tokens) applied
  5. bad prune (rate below gate) raises `RefineGateError`, then rejected
  6. stale candidate apply refused
  7. rollback marks the attempt `rolled_back`
  8. the improvement block lists every attempt, including rejected and
     rolled-back ones
  9. procedure head chars after the prune < after the repair
  10. auto-calibration changed the trust of a chunk with outcomes
- **Output:** JSON with `summary.pass` = all checks, plus a
  `paired_comparison` over a fixed synthetic paired series (reported only).
- **CI:** add a gate line in `.github/workflows/ci.yml` like the existing
  benchmark gates.
- **Tests:** `tests/test_benchmark_refine_loop.py`.

## Acceptance

- New tests for every item, including:
  - store parity tests for sqlite
  - pgvector tests following existing patterns, mocked or skipped like the
    others
  - a migration discovery test for 020
  - MCP handler tests (schema and server-held defaults)
  - refine gate and stale-head tests
  - outline tests
  - adaptive budget tests
  - an auto-calibrate debounce test
  - a skill-decay exemption test
- Checks that must pass:
  - `python3 -m pytest -p no:cacheprovider tests`
  - `python3 -m ruff check .`
  - `python -m mypy ncp`
  - the new benchmark and the existing CI benchmark gates
- Backward compatibility:
  - Existing outcome rows and old clients keep working (all new fields are
    optional).
  - Existing refine CLI invocations behave the same, except that the
    stale-head check is now enforced.

## Upgrade notes (for CHANGELOG)

- Auto-calibration on outcome is on by default. Set
  `[calibration].auto_on_outcome=false` to restore operator-only calibration.
- The adaptive budget no longer shrinks unproven task/slots. To restore, set
  `[budget].protect_unproven_slots=false`.
- User-registered skills are no longer decayed by batch calibrate.
- `ncp refine apply` refuses stale candidates, and gates prune candidates.
