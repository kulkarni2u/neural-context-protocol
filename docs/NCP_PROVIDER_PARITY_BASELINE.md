# NCP Provider Parity Baseline
## Live MCP dogfood parity snapshot for Claude, Codex, and OpenCode

This document records the current parity baseline on the shared MCP dogfood
path.

It is not a marketing claim.
It is the current engineering truth for the live CLI-backed continuation path.
It proves provider interoperability on the bounded continuation slice, not
comparative agent efficacy.

## Harness version

- transport: internal stdio compatibility path via `ncp serve-stdio`
- scenario: continuation adapter repeatability mode
- attempts per provider: `5`
- per-call timeout: provider-specific, recorded below

Commands used:

```bash
python3 -m ncp.cli dogfood --cwd /tmp/project --continuation-adapter claude-cli --attempts 5 --adapter-timeout-seconds 20
python3 -m ncp.cli dogfood --cwd /tmp/project --continuation-adapter codex-cli --attempts 5 --adapter-timeout-seconds 20
python3 -m ncp.cli dogfood --cwd /tmp/project --continuation-adapter opencode-cli --attempts 5 --adapter-timeout-seconds 18
```

## Current results

| Provider | Attempts | Contract success | Continuation success | Stable | Working timeout |
|---|---:|---:|---:|---|---:|
| `claude-cli` | 5 | 5/5 | 5/5 | Yes | `20s` |
| `codex-cli` | 5 | 5/5 | 5/5 | Yes | `20s` |
| `opencode-cli` | 5 | 5/5 | 5/5 | Yes | `18s` |

## Interpretation

### Claude

- Claude follows the strict continuation contract reliably on the current
  harness shape.
- The working baseline for this slice is the tightened Claude-specific prompt
  plus a `20s` per-call timeout.

### OpenCode

- OpenCode returned contract-shaped and semantically correct output on all 5
  attempts in this baseline.
- The working baseline for this slice is the current JSON contract path plus an
  `18s` per-call timeout.

### Codex

- Codex follows the strict continuation contract reliably on the current
  harness shape.
- The working baseline for this slice is the `codex exec` non-interactive path
  plus a `20s` per-call timeout.

## What this means for parity

At the moment:

- all three providers are operational on the live MCP dogfood path
- all three providers pass this bounded parity slice cleanly
- the parity claim is limited to this exact continuation scenario and these
  explicit timeout budgets

What it does **not** mean:

- it does not prove NCP improves task success versus alternative context strategies
- it does not prove quality retention under compression
- it does not replace matched-budget efficacy benchmarking
- retrieval quality is now separately measured in `benchmarks/retrieval/`

More precisely:

- Claude, Codex, and OpenCode all pass the bounded `ncp_fetch` continuation
  slice on the live MCP dogfood path

## Current claim

The honest claim today is:

- Claude, Codex, and OpenCode all support the bounded `ncp_fetch`
  continuation pattern on the live MCP dogfood path
- current baseline is `5/5` contract success and `5/5` continuation success
  for all three providers on the current harness version

## Retrieval Quality Baseline

This section covers BM25 recall@k on a labeled 24-chunk set, measured
independently of provider parity.

### What it measures

BM25-based recall and precision at rank k, evaluated against 12 labeled queries
over a 24-chunk corpus (12 signal chunks spanning constraint / decision /
dead-end categories, plus 12 distractors). Each query has a known ground-truth
set of relevant chunk IDs; recall@k and precision@k are computed from the
intersection of retrieved and relevant chunks.

This is the SQLite BM25-only path. The pgvector path (when configured) would
add vector similarity scoring on top of BM25; the numbers below reflect the
BM25-only result.

### Command

```bash
python3 benchmarks/retrieval/run.py --k 4
```

### Results

| Metric | Value |
|---|---|
| k | 4 |
| Labeled queries | 12 |
| Signal chunks | 12 (4 constraint, 4 decision, 4 dead-end) |
| Distractor chunks | 12 |
| mean_precision_at_4 | 0.25 |
| mean_recall_at_4 | 1.00 |
| mean_relevant_rank | 1.00 |
| queries_with_perfect_recall | 12 |

Observed on June 1, 2026 with:

```bash
python3 benchmarks/retrieval/run.py --k 4
```

### Notes

- BM25 term-overlap scoring drives recall here; pgvector cosine similarity
  would further improve ranking on semantically similar but lexically distinct
  queries
- The diversity cap test records how many constraint-category chunks appear in
  top-k at `diversity_limit=1`, `diversity_limit=2`, and uncapped
  (`diversity_limit=100`); a tighter cap should not increase the count vs no cap

## Cross-host Shared Context Baseline

This section covers a live cross-host benchmark where:

- Host A uses `claude-cli`
- Host B uses `opencode-cli`
- Host B either reads the shared NCP store or receives only a sliding-window
  transcript control

### Command

```bash
python3 benchmarks/crosshost/run.py \
  --host-a-adapter claude-cli \
  --host-b-adapter opencode-cli \
  --budget 600 \
  --attempts 5 \
  --host-a-timeout-seconds 30 \
  --host-b-timeout-seconds 20
```

### Current result

Observed on June 1, 2026:

| Metric | Value |
|---|---|
| token unit | `word_split` |
| Host B NCP success rate | `0.8` |
| Host B window success rate | `0.0` |
| delta_success_rate | `+0.8` |
| Host B NCP median prompt tokens | `132` |
| Host B window median prompt tokens | `656` |
| timeout note | attempt 2 inherited a `host_a_timeout` |

### Interpretation

- this is the first live cross-host result showing differentiated success from
  the shared NCP substrate over a window-only control
- the evidence is still narrow:
  - one provider pairing
  - one task shape
  - one Host A timeout inside the 5-attempt run
