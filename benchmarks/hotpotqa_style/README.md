# HotpotQA-style multi-hop benchmark

Context adequacy at a matched token budget on a synthetic, HotpotQA-shaped
multi-hop QA task: 15 scripted questions (8 "bridge", 7 "comparison"), each
with two gold facts buried among ~20 near-topic and filler paragraphs.

## What this is NOT

This is not the official HotpotQA dataset, and not PlugMem's own eval
harness. PlugMem (arXiv 2603.03296, ICML 2026,
[github.com/TIMAN-group/PlugMem](https://github.com/TIMAN-group/PlugMem))
reports results on the real HotpotQA distractor split. Fetching that dataset
(`huggingface.co/datasets/hotpot_qa`, `hotpotqa.github.io`,
`curtis.ml.cmu.edu`) and cloning PlugMem's own repo to run its harness both
require hosts this environment's egress policy denied when this benchmark
was built — huggingface.co, arxiv.org, and cmu.edu are not on the allowlist
(github.com is). So this benchmark reproduces the *shape* of HotpotQA's
distractor setting with fictional entities instead of the dataset itself:
no contamination risk, keyless, deterministic, and runnable in CI, following
the same convention already used by `benchmarks/task_success` and
`benchmarks/needle`.

Treat the numbers below as evidence NCP's retrieval surfaces multi-hop facts
under a token budget better than pure recency — not as a comparison point
against PlugMem's published accuracy numbers. See `tasks.py` for the full
task construction and scoring rationale.

## What it measures

Three context-construction conditions at the same token budget `B`:

- `ncp` — paragraphs written as chunks to a real SQLite store; the final
  context comes from `Assembler.assemble(..., max_tokens=B)` (real BM25 +
  recency + trust retrieval).
- `sliding_window` — the most recent paragraphs (by list order) that fit in
  `B` tokens. Recency only, no retrieval.
- `raw_replay` — every paragraph, unbounded. Reference condition.

Success is context adequacy: do **both** gold paragraphs' facts survive into
the assembled context? (`tasks.score_context`). This checks whether the
facts needed to answer were not dropped by the budget — not whether a model
can combine them into a correct answer.

## Reproduced result

```bash
python3 benchmarks/hotpotqa_style/run.py
```

At the default budget (300 tokens, `chars_div4` unit), on all 15 tasks:

| Condition | Success rate | Median context tokens |
|---|---:|---:|
| `ncp` | **100%** | 279 |
| `sliding_window` | **0%** | 277 |
| `raw_replay` (unbounded) | 100% | 778 |

At a matched budget of ~278 tokens, NCP's relevance-based retrieval finds
both gold paragraphs on every task; a pure recency window finds neither on
any task, because both gold paragraphs are deliberately planted early/mid
transcript, behind filler content, where recency drops them. `raw_replay`
confirms the facts are answerable at all — it just costs 2.8x the tokens to
get there. The split holds on both question kinds (`bridge`: 8/8 tasks,
`comparison`: 7/7 tasks — see `by_kind_and_condition` in the JSON output).

This budget is a knife-edge by construction — every distractor and filler
paragraph is short, so a slightly larger budget lets `sliding_window` sweep
in enough of the transcript to accidentally pass. Sweep it yourself:

```bash
python3 benchmarks/hotpotqa_style/run.py --budget 150   # ncp 60%, sliding_window 0%
python3 benchmarks/hotpotqa_style/run.py --budget 350   # ncp 100%, sliding_window 47%
```

## Options

```bash
python3 benchmarks/hotpotqa_style/run.py --budget 300 --k 6
python3 benchmarks/hotpotqa_style/run.py --tasks 5              # limit to first N tasks
python3 benchmarks/hotpotqa_style/run.py --store-path /tmp/hp.db
```

The command prints one JSON artifact with per-task, per-condition rows
(`context_tokens`, `success`, `missing` facts) and a `summary` block
(`by_condition`, `by_kind_and_condition`, pass gate).
