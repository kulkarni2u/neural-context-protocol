# Memo Reuse Benchmark

Measures whether memoization saves money **without serving stale answers**.
It reports the following per arm:

- provider calls avoided
- memo hit rate and **false-hit rate**, where a false hit is a reused answer that is wrong for the current evidence
- verified successes
- tokens and **cost per verified success**

## Workload

`--tasks` tasks are asked for `--rounds` rounds. Each task has its own pipeline
and one evidence chunk (`topicN fact version vK`). Between rounds, the
evidence of every `--mutate-every`th task is corrected: a new version is
written and the old one superseded. A simulated, priced provider (`gpt-4o`
pricing, fixed 120 output tokens) answers with the version it sees in
context. An independent verifier checks each answer against the current
ground truth, which the harness tracks and never reads from NCP. The host
verifies or fails each fresh result through `update_memo_outcome`, the way
a real host would.

## Arms

| Arm | What runs |
|---|---|
| `memo_off` | Every turn calls the provider. |
| `memo_dependency_safe` | `ncp.run` with memoization on: the canonical dependency-fingerprinted key plus the verification gate (`allow_unverified = false`, `min_outcome = 0.5`). |
| `naive_key_counterfactual` | Computed on the memo-on run, not executed. It shows what a key of just `agent\|task\|slot + turn` with no gate (the pre-fix Python path) would have reused, and how often that reuse would be wrong. |

Memo lookups still assemble context, because the fingerprint needs it. So
`avoided_retrieval` is reported as 0. Only provider calls are avoided.

## Run

```bash
python3 benchmarks/memo_reuse/run.py
```

## Gate (`summary.pass`)

- The dependency-safe arm has **zero** false hits, and every request is verified correct.
- Its cost per verified success is lower than `memo_off`.
- The naive counterfactual would have produced at least one false hit, which shows the workload really contains the hazard.

## Result at defaults (12 tasks x 4 rounds, mutate every 3rd)

| Arm | Provider calls | Memo hits | False hits | Verified | Cost / verified success |
|---|---|---|---|---|---|
| memo_off | 48 | 0 | 0 | 48/48 | $0.00143 |
| memo_dependency_safe | 24 | 24 | **0** | 48/48 | $0.00072 (-50%) |
| naive key (counterfactual) | — | 36 would-hit | **12 (33%)** | — | — |

This benchmark found a real bug while it was being built. Write-time dedup
treated `... v2 ...` as a duplicate of `... v1 ...`, so corrections were
silently dropped (see `tests/test_value_correction_dedup.py`). The numbers
above are from after that fix.

Costs use simulated token usage on a synthetic corpus. Read them as a
relative comparison between arms, not as a provider bill.
