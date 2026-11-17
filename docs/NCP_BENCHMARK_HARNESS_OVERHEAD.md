# NCP Benchmark: Harness and Subagent Overhead

Measures how many characters the harness itself spends on protocol overhead
around a turn, before and after the v1.9.6 token-efficiency work
([plan](./NCP_TOKEN_EFFICIENCY_PLAN.md)): tool catalogs, the arguments a model
emits for protocol calls, what a handoff returns to its parent, chunks resent
across turns, the stable prefix of the context block, budget accuracy and skill
dispatch.

It is deterministic: in-process, a temporary SQLite store, a mock provider (no
model, no network, no provider calls) and the `chars_div4` unit. It runs in CI
and fails when a gate is missed.

## Command

```bash
python3 benchmarks/harness_overhead/run.py
python3 -m pytest tests/test_harness_overhead_benchmark.py -q
```

Options: `--workers` (8), `--skill-chars` (6000), `--response-chars` (20000),
`--delta-turns` (10).

## How "before" is measured

There is no replay of the old release. Each "before" is the **new code with the
previous behavior switched on**: `--return full`, explicit ids plus `result_full`
on `ncp_post_turn`, the `standard` profile, `delta: false`, `show_score = true`,
inline skill text. A "before" therefore says what a caller who opts out of the
new default pays today, which is what the old default did.

## Result

Observed on 2026-11-17 (`chars_div4`, mock provider, SQLite). Lower is better except the prefix fraction.

| Metric | Before | After | Change |
|---|---:|---:|---:|
| a. Tool catalog characters, `minimal` against `standard` (default) | 15,874 | 5,062 | -68% |
| b. Model-emitted characters per `ncp_post_turn` (ids + `result_full` against summary only) | 1,909 | 257 | -87% |
| c. Characters returned to the parent per handoff, 20 KB response (`--return full` against summary) | 20,677 | 1,999 | -90% |
| d. Chunk characters resent over 10 turns, static 6-chunk store (`delta: false` against `true`) | 20,620 | 2,656 | -87% |
| e. Prefix-stable fraction of the block across 5 turns over 30 simulated hours (`show_score` true against false) | 0.19 | 0.89 | higher is better |
| g. Orchestrator characters to dispatch 8 workers with one 6,000-character workflow skill (inline against `--skill`) | 48,480 | 504 | -99% |

### a. Tool catalog characters per profile

`json.dumps(tools, separators=(",", ":"))` of the tools a server advertises,
with the task and memo tools enabled so each profile is at its largest.

| Profile | Tools | Characters |
|---|---:|---:|
| `minimal` | 2 | 5,062 |
| `core` | 5 | 10,700 |
| `standard` (default) | 10 | 15,874 |
| `full` | 18 | 23,352 |
| `full` + `[skill_cache].enabled` | 19 | 24,190 |

Gate: `minimal < core < standard < full`. The "before" in the result table is
the default `standard` profile and the "after" a connection that asks for
`minimal`; a client that needs more tools pays for more tools.

### b. Protocol-call characters

Both calls are real `ncp_post_turn` requests against the handlers and both are
accepted. "Before" carries 4 retrieved chunk ids,
1 whisper id and a 1,539-character `result_full`;
"after" omits them and the server uses the ids it holds for the session. The
`result_full` size is an assumption (an answer the model would otherwise retype);
a shorter answer shrinks the saving, a longer one grows it.

### c. Handoff return

`ncp handoff claude` through the CLI with a mock provider that returns a
20,000-character response. The summary mode prints at most 2,000
characters plus the chunk id of the full output, which stays fetchable.

### d. Cross-turn delta

Ten `ncp_get_context` calls from one session on a static store, summing the
characters of the chunk section. After the first call, `delta: true` sends
`chunk:<id> seen:1` headers instead of chunk text.

### e. Prefix stability

The fraction of the context block that is byte-identical across five turns
spread over 30 simulated hours, as the longest common prefix divided by the mean
block length. With `show_score` on, the recency-dependent score changes the
first chunk header, so almost nothing is cacheable.

### f. Budget accuracy

A store of 18 mixed prose, code and JSON chunks assembled at four ceilings.
The conservative counter (`estimate_budget_tokens`, margin 0.9 and a dense-text
rate) must never exceed `max_tokens`.

| max_tokens | Conservative count | Ratio | Plain chars/4 count |
|---:|---:|---:|---:|
| 400 | 368 | 0.92 | 206 |
| 800 | 644 | 0.81 | 579 |
| 1600 | 1499 | 0.94 | 842 |
| 3200 | 3161 | 0.99 | 2844 |

Gate: every ratio is at most 1.0. Highest observed: 0.99. There is no "before"
here: it is a safety bound, not a saving. The plain chars/4 column is for
contrast only; a real tokenizer may count code and JSON well above it.

### g. Skill dispatch

One 6,000-character workflow skill registered with `ncp skill cache`, dispatched to
8 workers. "Before" pastes the skill text into each dispatch command
(6,060 characters each), "after" passes `--skill team/deploy`
(63 characters each). The benchmark checks that the worker prompt
still receives the full skill text under a `[NCP:SKILL] skill:team/deploy@014353531c4b` header.
This counts what the orchestrator emits; the worker's prompt is the same size either way.

## Pass gate

Every "after" is at most its "before", the prefix fraction does not fall, the
conservative budget count never exceeds `max_tokens`, `minimal < core < standard
< full`, and both `ncp_post_turn` shapes are accepted. Current result: `pass: true`.

## Caveats

- These are structural character counts under `chars_div4`, not provider
  tokens and not dollars. Code, JSON and non-English text tokenize at more
  tokens per character; a live-provider run is needed for real cost.
- The provider is a mock. Nothing here measures model quality, whether a model
  uses a `seen:1` header correctly, or whether a prompt cache hit happens.
- Each "before" is the new code with an old behavior enabled, so it
  describes the old default but is not a replay of the old release.
- The workloads are small and chosen to be reproducible: a 6-chunk store, a
  20 KB response, a 6 KB skill, a 1.5 KB answer. Savings scale with those sizes,
  so treat the percentages as illustrations of the mechanism, not forecasts.
- Delta only helps a session that keeps its context between turns. A worker that
  starts fresh each turn gets nothing from it and must not set it.
- The by-reference saving in (g) is the orchestrator's output only. The worker
  still receives the skill text.
- (e) measures byte stability, which is what a prefix cache needs; it does not
  show that any provider's cache was hit.
