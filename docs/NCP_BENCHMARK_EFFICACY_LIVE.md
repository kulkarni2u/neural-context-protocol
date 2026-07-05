# Provider-Real Efficacy Benchmark

This benchmark asks a narrower question than the headline token-accounting
benchmarks:

> At the same requested context budget, does a provider receive enough usable
> context to name the approved path and avoid rejected paths?

It compares three context strategies over the shared task-success task set:

- `ncp`: retrieve and assemble context through NCP.
- `sliding_window`: keep only the newest raw transcript turns that fit.
- `rolling_summary`: keep a deterministic extractive summary of older turns plus
  recent turns.

The harness is intentionally conservative. Mock mode is deterministic and
keyless; it measures whether the required facts survived into the prompt, not
whether a model reasoned well. Anthropic mode calls a live provider and reports
estimated prompt tokens and USD cost per task. If `ANTHROPIC_API_KEY` is absent,
the run records a clear skip artifact instead of silently pretending coverage.

## Run It

Deterministic CI-safe run:

```bash
python3 benchmarks/efficacy/run.py --provider mock --seeds 2
```

Live Anthropic run:

```bash
ANTHROPIC_API_KEY=... python3 benchmarks/efficacy/run.py \
  --provider anthropic \
  --budget 400 \
  --seeds 5 \
  --adapter-timeout-seconds 30
```

The default artifact path is:

```text
benchmarks/efficacy/efficacy_results.json
```

## Artifact Shape

The JSON artifact includes:

- `benchmark`: `provider_real_efficacy`
- `provider`: `mock` or `anthropic`
- `budget`, `seeds`, `n_tasks`, and `token_unit`
- `conditions`: `ncp`, `sliding_window`, `rolling_summary`
- `config`: retrieval and summary knobs used by the harness
- `rows`: one row per task, seed, and condition with success, failure type,
  prompt tokens, context tokens, model, cost, cost source, and a response excerpt
- `summary.by_condition`: success rate, mean prompt tokens, mean USD cost per
  task, and sample count

## Interpretation

Use mock mode as a regression check for context adequacy. It is useful because it
is deterministic and can catch retrieval or budget regressions without API keys.
It is not evidence that a production model will complete the task.

Use live mode as provider-real evidence, but keep claims bounded:

- one provider is not a universal provider claim
- this task family measures exact constraint retention, not broad software
  engineering skill
- token counts use the repository token estimator, so artifacts always record
  `token_unit`
- live costs are priced through the repository pricing table and are only as
  current as that table

The strongest honest wording is: "NCP improves context adequacy on this matched
budget benchmark, and live provider runs can measure whether that context
advantage translates into task success and cost."

## Migration Note

Older versions of this harness accepted `--continuation-adapter` and `--attempts`
for a single sliding-window control scenario. The CAP-E4 harness replaces that
surface with `--provider` and `--seeds`, and adds a rolling-summary control.
