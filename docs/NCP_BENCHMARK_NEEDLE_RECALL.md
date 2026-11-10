# NCP Needle Recall Benchmark
## Retrieval-pressure eval against an equal-budget sliding window

This benchmark is intentionally uncomfortable.

It is meant to answer a narrower question than the pipeline token benchmarks:

- when important earlier facts are planted and the working budget stays tight,
  does NCP retrieve the right old constraints better than an equal-budget
  sliding window?

## Command

Run it from the repo root:

```bash
python3 benchmarks/needle/run.py --turns 24 --needles 6 --budget 4
```

## Current result

Observed on June 1, 2026:

- token unit: `word_split`
- budget mode: `chunk_budget`
- budget: `4` chunks
- final NCP recall: `0.50`
- final sliding-window recall: `0.00`
- reported deficit: `false`

First needle eviction turns (NCP retrieval — first turn where NCP failed to retrieve the needle):

- `needle_01`: retained through the final turn
- `needle_02`: retained through the final turn
- `needle_03`: retained through the final turn
- `needle_04`: evicted at turn `6`
- `needle_05`: evicted at turn `6`
- `needle_06`: evicted at turn `6`

### Plain RAG arm (added v1.8.x)

A `plain_rag` arm (`ncp.bench.baselines.PlainRagBaseline`) now runs alongside
NCP and the sliding window: each planted/filler chunk text is one document, BM25
ranks them against the same per-needle query NCP receives, and results are
packed best-first into the same token budget the sliding window consumed that
turn (101 tokens at 24 turns / budget 4). No trust, recency, dedup, whispers or
consolidation.

Measured with `python3 benchmarks/needle/run.py --turns 24 --needles 6 --budget 4`
(final-turn recall, 6 needles):

| Arm | Final recall |
|---|---:|
| ncp | 1.00 |
| plain_rag | 1.00 |
| sliding_window | 0.00 |

The same ordering held for every setting tried (24/6/2, 48/8/4, 48/8/8, 16/8/3
turns/needles/budget): NCP 1.00, plain RAG 1.00, sliding window 0.00.
**Plain RAG ties NCP here.** The honest reading: this benchmark is lexical.
Every needle query shares a unique slug (`constraint_1`, ...) with exactly one
chunk, and the fillers share none, so any lexical retriever solves it. It
shows that NCP's retrieval is not worse than BM25 and beats recency; it does
not exercise trust weighting, contradiction handling, or distinguishing
near-duplicate candidates. (The `0.50` figures above were measured on an older
revision; the current code reproduces `1.00` for NCP.) New summary keys:
`recall_at_final.plain_rag`, `ncp_beats_plain_rag`,
`ncp_ties_or_beats_plain_rag`; per-turn `plain_rag_recall`,
`plain_rag_hits`, `plain_rag_budget_tokens`. No CI gate asserts NCP beats plain
RAG, because it does not.

## Interpretation

This is not a flattering benchmark, and that is the point.

The current artifact says:

- NCP beats an equal-budget sliding window on this retrieval-pressure setup
- but NCP still recalls only half of the planted needles at the final turn

That is useful signal.

It means the current retrieval stack is meaningfully better than a simple
window on this task shape, but it is not yet strong enough to claim that
important old constraints are preserved reliably under pressure.

## Methodology note

Each needle is retrieved with its own targeted query (`query_text=needle.query_text`).
This is the standard recall-at-k methodology for retrieval evals: it tests whether
the retrieval system can surface a specific item when asked for it directly.

In production, a single `get_context` call assembles up to `k` chunks from one query.
This benchmark measures single-needle retrieval capability, not multi-constraint
recall in one shot. At `budget=4` and `k_needles=6`, NCP makes 6 separate targeted
queries per turn rather than one unified query. Readers should interpret the recall
number as a ceiling on what targeted retrieval can achieve, not as what a single
assembly call would return.

## Artifact contract

The JSON output includes:

- planted needle definitions
- per-turn NCP recall
- per-turn sliding-window recall
- equal-budget window token estimates
- final recall summary
- first-eviction turn per needle

## Current claim

The honest claim supported by this artifact today is:

- on this retrieval-pressure benchmark, NCP outperforms an equal-budget sliding
  window, but still shows meaningful recall loss that should be improved before
  making stronger retention claims
