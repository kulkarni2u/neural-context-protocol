# NCP Matched-Budget Efficacy Benchmark
## Groundwork for real-agent comparative evaluation

This document is intentionally a benchmark contract, not a claimed result.

The current repository proves runtime correctness and bounded-context behavior.
It does **not** yet prove that real providers succeed more often with NCP than
with realistic alternative context strategies at the same budget.

This document defines the next eval needed to answer that question honestly.

## Goal

Measure whether real providers complete the same task more reliably when given:

1. NCP-bounded context
2. a fixed sliding-window baseline
3. a rolling-summary baseline

All three strategies must run under the same approximate prompt budget.

## Providers

The initial provider set should be:

- `claude-cli`
- `codex-cli`
- `opencode-cli`

If one provider is unavailable on the test machine, record that explicitly in
the artifact rather than silently dropping it.

## Task shape

Use a deterministic two-call continuation task first.

The initial workload should stay close to the current dogfood continuation path:

- call 1: provider must request the missing fact or act on the bounded context
- call 2: provider must complete correctly after retrieval/continuation

This keeps the first efficacy slice narrow enough to interpret.

## Budget rule

All strategies must stay within the same approximate input budget.

Initial rule:

- choose a target budget from the observed NCP prompt size on the task
- clamp alternative strategies to that same budget band
- report the actual input-token estimate for every call

If token accounting uses `word_split`, the artifact must say so explicitly.

## Strategies

### NCP

- `ncp_get_context`
- optional `ncp_fetch`
- `ncp_write_memory`

### Sliding window

- no NCP retrieval
- provide only the latest fixed window of prior turn material

### Rolling summary

- no NCP retrieval
- provide a deterministic rolling summary plus the latest recent turns

## Success criteria

Each attempt should record:

- contract success
- final task success
- total estimated input tokens
- whether the provider needed a second chance / retry
- any failure category:
  - timeout
  - malformed response
  - retrieval miss
  - context loss
  - wrong final answer

## Minimum artifact schema

Each run should emit:

- provider
- strategy
- attempts
- token_unit
- target_budget
- attempt rows:
  - prompt_tokens_call_1
  - prompt_tokens_call_2
  - total_prompt_tokens
  - contract_success
  - task_success
  - failure_type
- summary:
  - success_rate
  - median_prompt_tokens
  - timeout_rate

## Interpretation rule

Do not publish a strong claim from a single-provider or single-task result.

The first honest claim threshold is:

- at least 3 providers
- at least 1 shared task shape
- at least 10 attempts per provider/strategy pair

## Current status

As of June 1, 2026:

- runtime proof exists
- bounded baseline proof exists
- retrieval-pressure proof exists
- the first live matched-budget real-agent result now exists for `claude-cli`

### Current live result

Command used:

```bash
python3 benchmarks/efficacy/run.py \
  --continuation-adapter claude-cli \
  --budget 600 \
  --attempts 5 \
  --adapter-timeout-seconds 30
```

Observed result:

- token unit: `word_split`
- NCP success rate: `0.8`
- sliding-window success rate: `0.0`
- NCP median prompt tokens: `132`
- sliding-window median prompt tokens: `656`
- timeout rate:
  - NCP: `0.2`
  - sliding-window: `0.0`

Interpretation:

- this is the first live provider-backed result showing differentiated task
  success in favor of NCP on the current harness
- it is still early evidence, not final comparative proof
- the run covers one provider and one task shape only
- the rolling-summary control is still pending implementation in the live
  benchmark harness

That makes the status stronger than "groundwork only", but still short of the
full threshold defined above.
