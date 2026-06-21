# NCP Content Compression Benchmark
## Deterministic ingestion-time noise reduction on representative noisy payloads

When an agent calls `ncp_write_memory`, NCP does not store the raw tool result
verbatim. The MCP handler (`ncp/mcp/server.py`, `_handle_write_memory`) runs the
content through `filter_content` (`ncp/chunker.py`) first, storing the cleaned
text as the memory chunk and preserving the original as a low-trust `raw_ref`
chunk only when filtering actually reduced the content.

That filter was previously unmeasured. This benchmark measures it.

## What the filter does

`filter_content` applies deterministic, lossless-in-intent noise reduction:

- strips ANSI escape sequences
- collapses runs of 3+ blank lines into a single blank line
- deduplicates consecutive duplicate lines, annotating the collapse with `(×count)`
- strips tool-output boilerplate (progress-bar lines, `real`/`user`/`sys`
  timing lines) for prose/code content
- prunes top-level `null`, `""`, and `[]` fields from JSON tool results

It removes framing, not signal. The numbers below are therefore an honest floor
on how much raw noise NCP strips before storage, not a summarization claim.

## Corpus

The corpus is fixed, hand-authored, and deterministic — no network, no API keys,
no randomness. Four categories, chosen to mirror what agents actually write to
memory:

| payload_id | category | what it is |
| --- | --- | --- |
| `cli_ansi_progress` | `cli_output` | Package-install CLI output: ANSI color codes, progress bars, `real`/`user`/`sys` timing lines |
| `verbose_retry_log` | `duplicate_log` | Worker log with a long run of identical retry warnings plus repeated cache-miss lines |
| `json_null_empty` | `json_result` | Tool-result JSON with `null` and empty-collection fields that carry no signal |
| `stacktrace_blank_runs` | `stack_trace` | Python stack trace padded with runs of 3+ blank lines between frames |

## Command

Run it from the repo root:

```bash
python3 benchmarks/compression/run.py
```

It prints a JSON artifact to stdout. An optional `--pass-threshold` flag
overrides the gate.

## Current result

Observed on June 21, 2026 (`token_unit: chars_div4`):

- aggregate char reduction: **33.04%** (2158 → 1445 chars)
- aggregate token reduction: **32.96%** (537 → 360 tokens)
- pass-gate threshold: **0.20** aggregate token reduction
- `pass`: **true**

Per-category token reduction:

| category | char reduction | token reduction |
| --- | --- | --- |
| `cli_output` | 5.07% | 4.81% |
| `duplicate_log` | 67.72% | 68.02% |
| `json_result` | 59.63% | 59.26% |
| `stack_trace` | 2.50% | 2.02% |

## Interpretation

The result is deliberately honest, not flattering.

Two categories compress heavily: consecutive-duplicate dedup turns a 9-line
retry storm into a single annotated line (68% reduction), and JSON null/empty
pruning removes dead fields (59% reduction).

Two categories compress only modestly. The CLI output keeps most of its bytes
because the filter strips ANSI codes and timing lines but conservatively leaves
the human-readable progress text and download lines in place. The stack trace
only loses its blank-line padding (3+ blank-line runs collapse to a single blank
line), which is a small fraction of its content. These low numbers are kept in
the corpus on purpose — the filter is conservative, and the benchmark reports
that conservatism rather than hiding it.

The aggregate (~33%) is the realistic mixed-workload signal: when an agent
writes a stream of noisy tool results, roughly a third of the tokens are framing
that NCP strips before storage.

The pass gate is set at `0.20` — comfortably below the measured `0.3296` — so it
is a real, passing floor that will catch regressions in the filter without being
brittle to small corpus tweaks.

## What this does and does not measure

- It **does** measure: deterministic ingestion-time noise reduction on
  representative noisy inputs (ANSI, progress bars, timing lines, duplicate log
  lines, null/empty JSON fields, blank-line runs).
- It does **not** measure: model quality, retrieval quality, or semantic
  summarization. The filter is lossless in intent. This is a reproducible
  measurement of how much raw tool-output noise NCP removes before storage —
  nothing more.

## Artifact contract

The JSON output includes:

- `benchmark`, `token_unit`, and a `config` block (pass threshold, payload count)
- `payloads`: per-payload rows with raw/filtered chars and tokens and both
  reduction ratios
- `summary`: aggregate raw/filtered chars and tokens, aggregate char and token
  reduction ratios, a `by_category` breakdown, the `pass_threshold`, and the
  `pass` gate

## Reproduce the test gate

```bash
python3 -m pytest tests/test_benchmark_compression.py -q
```
