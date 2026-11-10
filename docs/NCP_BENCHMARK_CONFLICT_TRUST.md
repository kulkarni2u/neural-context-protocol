# NCP Conflicting and Low-Trust Memory Benchmark
## Does provenance pick the right claim when wording cannot?

The other benchmarks plant one true fact and ask whether it survives into a
bounded context. Plain BM25 retrieval solves that as well as NCP does. This
benchmark plants a true fact and contradicting claims that are the same
sentence with a different value, so a lexical retriever scores them
identically. What separates them is provenance: source trust, a `supersedes`
correction, recorded outcomes, dissent, or repetition by one author.

It is deterministic, uses no LLM and no network, and measures what reaches the
context, not whether a model then answers correctly.

## Command

```bash
python3 benchmarks/conflict_trust/run.py --k 1 --summary-only
```

`--k` is the chunk budget given to every bounded arm. `--seed`,
`--distractors` and `--fillers` vary the setup. Without `--summary-only` the
output includes one row per task.

## Arms

- `ncp`: the assembler's bounded context.
- `plain_rag`: BM25 over every written text, `k` chunks. Its documents are
  presented in a seeded random order, so a BM25 tie is broken at random and
  not for or against the true fact.
- `sliding_window`: the last `k` texts written.
- `raw_replay`: every text written, in order.

## Scenarios (12 facts each, 84 tasks)

| Scenario | Setup | Signal that identifies the true claim |
|---|---|---|
| `no_conflict` | Only the true fact | none needed (control) |
| `low_trust_distractor` | True fact is `tool_result`; four `agent_inferred` guesses contradict it | source trust |
| `superseded` | An old value, then a correction with `supersedes` | the supersedes edge |
| `outcome_feedback` | Two equal-trust claims; outcomes recorded for and against; `calibrate` | outcomes |
| `dissent` | Two equal-trust claims; three reviewers dissent against the wrong one; `calibrate` | dissent |
| `repetition_flood` | One low-trust author repeats the wrong claim four times | trust and dedup |
| `misplaced_trust` | The wrong claim is `user_verified`, the true one `agent_inferred`, nothing corrects it | none: trust points the wrong way |

Six unrelated filler notes are written after the claims in every task.

## Metrics

- `top1_correct`: the first claim in the context is the true fact. Models a
  reader that acts on the highest-ranked claim.
- `clean`: the true fact is present and no contradicting claim is.
- `answer_present` / `conflict_present`: the components of `clean`.

## Result at k = 1 (seed 7)

`top1_correct`, which at `k = 1` equals `clean`:

| Scenario | NCP | plain RAG | sliding window | raw replay |
|---|---:|---:|---:|---:|
| `no_conflict` (control) | 1.00 | 1.00 | 0.00 | 1.00 |
| `low_trust_distractor` | 0.75 | 0.00 | 0.00 | 0.25 |
| `superseded` | 0.83 | 0.42 | 0.00 | 0.00 |
| `outcome_feedback` | 0.83 | 0.50 | 0.00 | 0.17 |
| `dissent` | 1.00 | 0.42 | 0.00 | 0.67 |
| `repetition_flood` | 0.92 | 0.33 | 0.00 | 0.08 |
| **Five resolvable scenarios** | **0.87** | **0.33** | 0.00 | 0.23 |
| `misplaced_trust` | 0.08 | 0.58 | 0.00 | 0.67 |

Across seeds 1, 2, 3, 7 and 11 the resolvable figure is 0.83 to 0.90 for NCP
and 0.33 to 0.52 for plain RAG. Plain RAG's figure is what a random choice
among equally scored claims gives; raw replay always contains the answer and
always contains the contradiction too.

## What this shows

Where the store holds a signal about which claim is right, NCP puts the true
claim first far more often than a lexical retriever does. This is the first
benchmark in the repository where NCP beats plain RAG, and the difference comes
from the features the other benchmarks do not exercise.

## What it also shows

**NCP does not score 1.00, and the cause is a defect.** In 8 of the 84 tasks
the store refused to write the true fact at all. Write-time near-duplicate
detection (`is_near_duplicate` in `ncp/stores/consolidation.py`) treats two
sentences that differ only in a non-numeric value as duplicates, so
"...is on Tuesday morning." is dropped when "...is on Friday morning." is
already stored. `write()` returns `False` and the contradiction is never
recorded. This happens even when the new chunk declares that it `supersedes`
the stored one, so a correction can be silently discarded. Numeric values are
not affected. Every NCP miss in the `superseded`, `outcome_feedback` and
`repetition_flood` scenarios, and two of the three in `low_trust_distractor`,
is this. The summary reports it as
`tasks_where_ncp_dropped_the_true_fact_at_write`.

**Trust weighting follows trust, not truth.** In `misplaced_trust` the wrong
claim is the trusted one and nothing corrects it, and NCP surfaces it in 11 of
12 tasks while plain RAG, choosing at random, is right about half the time. A
store that has been told something false with high trust is worse off than one
with no trust signal.

**A larger budget lets the contradiction back in.** At `k = 2` and `k = 3` NCP
still ranks the true claim first (0.88), but its context then also holds a
contradicting claim in most tasks: `clean` falls to 0.23, against 0.00 for
plain RAG. NCP labels each chunk with `src` and `trust`; this benchmark does
not measure whether a model uses those labels. `superseded` is the exception:
the replaced chunk stays out at every budget.

## Limits

- The facts are synthetic, single-sentence and in English, and each conflict
  is an exact template match. Real contradictions are rarely this tidy.
- It measures context composition. No model is asked the question.
- Plain RAG here is BM25. An embedding retriever would be in the same position
  on these tasks, since the claims are near-identical in wording, but that is
  not measured.
- One seed is gated in CI; the range above comes from five seeds.
