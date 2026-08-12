# NCP Fan-in Reduction Benchmark
## Deterministic dedup/merge/contradiction-flagging for many-workers-to-one-reader bursts

A [public post](https://x.com/gippp69) described a pattern from scaling
multi-agent pipelines: fanning a task out to many parallel workers (e.g. 40
Claude Haiku instances) and feeding their combined raw output straight into
one synthesis model dumps far more tokens on that final call than it needs.
A deterministic reducer sitting between the workers and the synthesizer --
drop malformed entries, group by normalized claim, keep the highest-confidence
version, surface contradictions the raw dump buried -- cut that pipeline's
synthesis cost 86% and latency 78% in the reported numbers.

NCP already had adjacent pieces: deterministic ingestion-time noise filtering
(see [the compression benchmark](./NCP_BENCHMARK_COMPRESSION.md)), a
similarity-clustering "keep the highest-trust version" merge primitive in
`ncp/stores/consolidation.py`, and a `contradicts` edge type in the chunk
graph schema. What was missing was wiring those into the one read path where
a many-to-one fan-in actually happens: `ncp_get_context` for a synthesis
turn. `[retrieval].reduce_fanin_enabled` (off by default) closes that gap --
see `ncp/stores/consolidation.py::reduce_candidates` and its call site in
`Assembler._prepare_assembly`. This benchmark measures it.

## What the reducer does

When enabled, retrieval overfetches `chunk_cap * reduce_fanin_overfetch`
candidates instead of just `chunk_cap` (otherwise the existing top-k cap
would fill with near-duplicates before the reducer ever saw them). The wider
pool is then, within any same-`(layer, zone, pipeline_id)` cluster at or
above `reduce_fanin_min_cluster` in size:

- **malformed-dropped**: empty/whitespace-only candidates are discarded
- **merged**: near-duplicate claims (BM25/SequenceMatcher similarity at or
  above `reduce_fanin_similarity_threshold`) collapse to the highest-`base_trust`
  version, reusing the same `find_merge_candidates`/`select_authoritative`
  logic the `ncp consolidate` maintenance command already uses
- **contradiction-flagged**: surviving same-cluster pairs get flagged when
  similarity clears `reduce_fanin_contradict_floor` (plausibly the same
  topic) but stays below the merge bar, *and* exactly one side carries a
  reversal cue the other lacks ("already fixed", "no longer reproduces",
  "still throws", ...) -- see "Known limitation" below for why cue-gating
  is there and what it does and doesn't catch

The existing chunk-cap/MMR/budget-fit steps still run afterward, on the
*reduced* pool. NCP doesn't decide what a contradiction means -- flagged
pairs are surfaced as a `note:contradicts a<->b` line in the assembled
context for the reading model to reason about, never silently resolved.

## Corpus

Deterministic, hand-authored, no network, no randomness: 40 simulated
parallel workers writing into one pipeline about 4 fixed topics (a
`PaymentProcessor` NPE, a `SessionStore` cache race, a webhook retry-budget
bug, and an auth-token replay issue). Each topic has 8 hand-paraphrased
confirming claims (reworded, never byte-identical -- real parallel workers
don't return identical text, and write-time near-dup suppression at >0.92
`SequenceMatcher` similarity would otherwise collapse them before they even
reached the store) plus one claim that genuinely reverses the finding. Every
13th worker returns nothing (a worker that failed to produce output); every
7th worker per topic writes the reversal instead of a confirming paraphrase.

## Command

```bash
python3 benchmarks/fanin_reduce/run.py
```

## Current result

Observed with 40 workers, `k=16`, `chars_div4` tokens:

| Scenario | Input tokens | Cost (`claude-sonnet-4`, 200 fixed output tokens) | Chunks |
| --- | ---: | ---: | ---: |
| `raw_dump` (no bound at all) | 1079 | $0.006237 | 40 |
| `ncp_default` (bounded top-k, reducer off) | 807 | $0.005421 | 16 |
| `ncp_reduced` (overfetched + reduced, then same cap) | 936 | $0.005808 | 16 |

- token reduction vs `raw_dump`: **13.25%**
- **4 of `ncp_default`'s own 16 returned chunks (25%) are near-duplicates of
  another chunk in that same result** -- context budget spent twice on one
  claim instead of once each on two different ones
- the reducer merges **7** near-duplicates out of the wider overfetched pool
  and flags **11** contradiction pairs, of which **7 (64%)** are genuinely
  about the same topic per the corpus's ground-truth labels

## Interpretation

The headline number is not `ncp_reduced` vs `ncp_default` on raw token
count -- both are capped at the same `k`, so they return the same chunk
count either way, and `ncp_reduced` carries a little extra text for its
contradiction notes. Comparing token totals at a fixed cap mostly measures
*which* chunks won the ranking, not reduction quality.

The number that actually isolates the reducer's effect is
**`wasted_duplicate_slots_in_default`**: running the same reducer as a
read-only diagnostic over `ncp_default`'s own already-capped output. A
quarter of the context budget NCP spends today on this corpus, with the
reducer off, goes to a chunk that says the same thing another chunk in the
same context already said. That's the direct analogue of the pattern the
tweet described, scoped honestly to NCP's bounded-retrieval architecture
rather than to an unbounded raw dump NCP would never actually produce.

The `raw_dump` comparison is the closest match to the tweet's own framing,
and it's real but modest here (13%) because this corpus's simulated worker
outputs are short one-line claims. The tweet's reported 41,200 raw tokens
across 40 workers implies roughly 1,000 tokens per worker -- a much more
verbose payload (paragraphs, not one-liners) where duplication is a larger
fraction of the total. A reducer removes duplicate *content*, not fixed
per-chunk overhead, so the token-reduction percentage should grow with
payload verbosity; this benchmark's flat, low number is a property of the
deliberately terse synthetic corpus, not a ceiling on the mechanism.

## Known limitation: contradiction flagging is a heuristic, not entailment

Plain similarity cannot separate "same claim, different wording" from
"different claim, similar wording" -- an early version of this benchmark
measured that directly: two paraphrases of the same finding and one
paraphrase against its genuine reversal score in overlapping similarity
ranges on this corpus's short technical claims. A pure similarity-band
heuristic over-triggers on real paraphrase variance (an earlier
uncalibrated pass on this same corpus flagged 30 pairs, most of them
harmless paraphrases). Gating on a small set of reversal cue phrases
("already", "no longer", "was fixed", "still throws", ...) is a sharper,
still fully deterministic, still no-model-call signal, but it is a keyword
heuristic: it will miss a contradiction phrased without one of those cues,
and it will still occasionally flag a same-similarity-band pair from a
*different* topic that happens to clear the floor together (4 of the 11
flagged pairs above). This is disclosed, not hidden, in the artifact's
`contradiction_same_topic_fraction` field -- computed only because this
benchmark's corpus has ground-truth topic labels; production data has no
such check available, so treat every flagged pair as "worth a second look,"
not "confirmed disagreement."

## What this does and does not measure

- It **does** measure: deterministic, no-model-call dedup/merge quality on a
  representative high-fanout burst, the fraction of NCP's default bounded
  retrieval that goes to redundant content today, and the honest precision
  of the contradiction-flagging heuristic on a corpus with known ground
  truth.
- It does **not** measure: model quality, whether the synthesis model
  actually uses a surfaced contradiction correctly, or real end-to-end
  latency (no live provider calls are made -- the tweet's 78% latency figure
  is a production measurement this benchmark has no equivalent for; only
  deterministic token/cost accounting is reported here, consistent with the
  rest of NCP's benchmark suite).

## Artifact contract

The JSON output includes:

- `config`: worker count, topic list, `k`, context token budget, synthesis
  model, and the corpus's contradiction/malformed injection rate
- `scenarios`: `raw_dump`, `ncp_default`, `ncp_reduced` -- each with input
  tokens, cost, and chunk count; `ncp_default` also reports
  `wasted_duplicate_slots_in_default`, `ncp_reduced` reports
  `fanin_merged_count`, `fanin_contradictions_count`, and
  `fanin_dropped_malformed_count`
- `summary`: token/cost reduction vs `raw_dump`, the wasted-duplicate-slot
  count and fraction, contradiction count and same-topic fraction, merged
  and malformed-dropped counts, and the `pass` gate

## Reproduce

```bash
python3 -m pytest tests/test_benchmark_fanin_reduce.py -q
```
