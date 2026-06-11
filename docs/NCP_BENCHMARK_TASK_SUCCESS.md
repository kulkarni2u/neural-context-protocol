# NCP Task-Success Benchmark
## Context adequacy at a matched token budget

This benchmark complements the deterministic token-accounting benchmarks
(`benchmarks/coding_pipeline/`, `benchmarks/needle/`) and the single-scenario
live-provider benchmark (`benchmarks/efficacy/`) with a multi-task harness
that evaluates **whether the facts a task needs survive into a context built
under a fixed token budget**, across three context-construction conditions:

- `ncp` — scripted multi-agent turns are written into a fresh SQLite store
  as chunks (varying `base_trust`/`src` per writer, as in real usage), and
  the final-question context is produced by
  `Assembler.assemble(..., max_tokens=B)`.
- `sliding_window` — the raw transcript's most-recent entries that fit within
  `B` estimated tokens. A fixed recency window with no retrieval.
- `raw_replay` — the full, unbounded transcript. A reference condition,
  **exempt from the budget** and labeled `"unbounded": true` in the artifact.

## Tasks

12 deterministic tasks live in `benchmarks/task_success/tasks.py`. Each task:

- defines a unique fictional "approved path" slug (e.g.
  `qorvex_relay_v3_delta`) that cannot be guessed from model training data,
  plus 2-3 "dead end" slugs that were rejected for the same domain;
- plants the approved-path decision and the dead-end rejections in the
  **first few turns** of a 16-turn scripted transcript, followed by filler
  turns describing routine pipeline activity;
- ends with a question turn asking an executor agent to state the approved
  path and confirm it will not propose any rejected path.

Planting the key facts early and padding with filler is deliberate: at the
default budget (400 tokens), a recency-based sliding window fills entirely
with filler and **drops the planted facts**, while NCP's relevance/condition-
based retrieval can still surface them.

## Scoring

Scoring is deterministic and mirrors `benchmarks/efficacy/run.py`:

- **success** = the response names the task's approved-path slug **and**
  does not propose any dead-end slug outside a negation context (e.g.
  "will not use X", "X was rejected", "X is forbidden").
- The negation-window check (`mentions_dead_end_as_retry` in
  `benchmarks/task_success/tasks.py`) is a local reimplementation of
  efficacy's `_mentions_dead_end_as_retry`, kept deliberately self-contained
  so this package has no cross-benchmark import dependency.

## Modes

### Mock mode (default — keyless, used in CI)

```bash
python3 benchmarks/task_success/run.py
```

`--provider mock` (the default) uses a **deterministic stand-in**, not a
real model: it scans the supplied context for the task's planted
approved-path slug and answers either

- `"I will use <slug> and will not use any rejected paths"` if the slug is
  present in the context, or
- `"no approved path found in context"` if it is not.

**What mock mode shows:** whether the planted approved-path fact survived
into a context built under the matched token budget `B`, for each of the
three conditions. This is **context adequacy**, not model task success.

**What mock mode does NOT show:** whether a real model, given an adequate
context, would actually reason correctly, follow the instruction, or avoid
the dead ends. That requires live mode.

A mock-mode `pass` gate is reported: `ncp success rate >= sliding_window
success rate AND ncp success rate >= 0.75`. This gate can run in CI without
API keys.

### Live mode (requires API keys)

```bash
python3 benchmarks/task_success/run.py --provider anthropic
python3 benchmarks/task_success/run.py --provider openai
```

Live mode routes each condition's assembled context through
`ncp.dogfood.load_dogfood_adapter`, including a plain-text-only instruction
preamble (no tool use, no file reads), exactly as `benchmarks/efficacy/run.py`
does. `ncp.dogfood.get_live_provider_readiness` is checked first; if the
provider's credentials or dependencies are missing, the run exits with a
clear error rather than silently falling back to mock.

In live mode the `pass` field is `null` — the mock-mode pass gate is
intentionally not applied to live results (different success-rate
distributions are expected with a reasoning model in the loop).

## Matched-budget rationale

All three conditions are evaluated at the same nominal token budget `B`
(default 400, `--budget`), except `raw_replay` which is reported but exempt
from the budget (and labeled `"unbounded": true`) — it represents the
floor/reference of "what if nothing were dropped." Comparing `ncp` against
`sliding_window` at the *same* `B` isolates the effect of **how** the budget
is spent (relevance-based retrieval vs. fixed recency) rather than *how much*
budget is available.

## Running it

```bash
# default: mock provider, all 12 tasks, budget 400
python3 benchmarks/task_success/run.py

# subset of tasks (useful for fast iteration / tests)
python3 benchmarks/task_success/run.py --tasks 4

# different budget
python3 benchmarks/task_success/run.py --budget 600

# live provider (requires credentials)
python3 benchmarks/task_success/run.py --provider anthropic --budget 600
```

The artifact is printed to stdout as JSON with:

- `benchmark`, `claim`, `provider`, `budget`, `n_tasks`, `token_unit`
- `rows`: one row per `(task_id, condition)` with `context_tokens`,
  `unbounded`, `success`, `failure_type`, `response_excerpt`
- `summary.by_condition`: `success_rate`, `n`, `median_context_tokens` per
  condition
- `summary.pass`: the mock-mode pass gate (`null` in live mode)

## Current result

Observed on June 10, 2026 (mock provider, default budget 400, all 12 tasks):

- token unit: `chars_div4`
- budget: `400`
- `ncp` success rate: `1.0` (12/12), median context tokens `295`
- `sliding_window` success rate: `0.0` (0/12), median context tokens `399`
- `raw_replay` success rate: `1.0` (12/12, unbounded), median context tokens `806`
- pass gate (`ncp >= sliding_window` and `ncp >= 0.75`): `true`

## Interpretation

At the matched 400-token budget, NCP's relevance/condition-based retrieval
recovers the planted approved-path fact for every task, while the
recency-only sliding window — at the same budget — never does, because the
key facts are pushed out of the window by filler turns before the question
turn is reached. `raw_replay` (unbounded) also succeeds on every task, as
expected, since nothing is dropped.

This shows that **the right facts survive into a budget-matched context with
NCP but not with a fixed recency window** — it does not show that a live
model would necessarily act correctly on that context. For a measurement of
live model behavior, run with `--provider anthropic` (or another configured
provider) and inspect the live `success_rate` per condition.
