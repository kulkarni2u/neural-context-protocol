# NCP Credibility Execution Plan

This document turns `NCP_CREDIBILITY_HANDOFF.md` into an execution sequence we
can actually run.

The goal is narrow:

- keep the `1.0.0` runtime story intact
- harden the comparative evidence
- stop overclaiming until the stronger evidence exists

This is not a product rescue plan. It is a benchmark and credibility plan.

## Ground Truth

Already proven:

- NCP runtime is real
- MCP surface is real
- SQLite default path is real
- pgvector + Redis scalable path is real
- Claude/OpenCode cross-host memory and whisper loops are real

Not yet proven strongly enough:

- efficacy against realistic baselines
- retrieval recall under pressure
- real-agent success at matched budget
- non-circular quality benchmark deltas

## Execution Order

We should execute in this order:

1. `WO-0` — honest scoping in docs
2. `WO-1` — realistic baselines in the token benchmark
3. `WO-2` — needle / recall eval
4. `WO-3` — real CLI agents at fixed budget
5. `WO-5` — silent-drop signal + net economics
6. `WO-4` — de-circularize MACE

Why this order:

- `WO-0` makes the public story honest immediately
- `WO-1` and `WO-2` establish whether the evidence line is even worth scaling up
- `WO-3` is the decisive eval, but only after recall is understood
- `WO-5` improves interpretability of all later results
- `WO-4` matters, but it is less load-bearing than recall and real-agent efficacy

## Branch Strategy

Primary branch:

- `bench-credibility`

Recommended work branches:

- `bench-credibility/wo-0-docs`
- `bench-credibility/wo-1-baselines`
- `bench-credibility/wo-2-needle`
- `bench-credibility/wo-3-efficacy`
- `bench-credibility/wo-5-economics`
- `bench-credibility/wo-4-mace`

Rules:

- each WO lands as its own commit or short branch
- rebase onto the latest `bench-credibility` before merging the next WO
- do not mix runtime feature work into these branches

## Role Split

Best use of agents:

- **Claude**
  - `WO-0`
  - `WO-1`
  - benchmark structure and doc truthfulness

- **OpenCode**
  - skeptical review lane for `WO-1` and `WO-2`
  - then implementation/review for `WO-4`

- **Codex**
  - orchestrator and integrator
  - owner of `WO-2`, `WO-3`, and `WO-5`
  - final evidence synthesis

Why:

- Claude is strongest on careful framing and structured benchmark refactors
- OpenCode is strongest as a bounded skeptical reviewer
- Codex should own the integration-heavy slices where repo truth and execution
  discipline matter most

## Acceptance Gates

We only advance when the previous gate is satisfied.

### Gate A — after `WO-0`

- README and benchmark docs clearly separate:
  - demonstrated
  - not yet independently validated
- no headline benchmark number appears without naming the baseline inline

### Gate B — after `WO-1`

- token benchmark includes:
  - `raw_replay`
  - `sliding_window`
  - `rolling_summary`
- tokenizer unit is explicit
- docs stop implying that raw replay is a realistic competitor

### Gate C — after `WO-2`

- recall is measured by `chunk_id`, not substring
- recall curves exist for NCP and sliding-window at matched budget
- we know whether NCP actually preserves important older constraints better

This is the most important gate.

### Gate D — after `WO-3`

- real providers run at matched budget
- success rate is reported for NCP vs sliding-window
- host discipline is held constant across both conditions

This is the decisive public-facing eval.

### Gate E — after `WO-5`

- high-relevance evictions are visible
- benchmark output reports net savings, not just prompt-side savings

### Gate F — after `WO-4`

- MACE baseline is measured, not hardcoded
- MACE scoring is no longer based on literal injected substrings

## Practical Commands

Start the lane:

```bash
cd /Users/sweethome/Work/neural-context-protocol
git checkout -b bench-credibility
```

Recommended first wave:

```bash
git checkout -b bench-credibility/wo-0-docs
git checkout -b bench-credibility/wo-1-baselines
git checkout -b bench-credibility/wo-2-needle
```

Core verification rhythm:

```bash
python3 -m pytest -q
python3 benchmarks/coding_pipeline/run.py --turns 40
python3 benchmarks/research_pipeline/run.py --turns 36
```

## Reporting Format

For each WO, report:

- what changed
- exact commands run
- pass/fail results
- whether the result strengthens or weakens the NCP claim

Important:

- a weak benchmark result is still a successful WO if it is honest and reproducible
- do not tune the benchmark until it flatters the product

## Stop Line

This execution pass is complete when:

- docs are honest
- benchmark baselines are realistic
- recall is measured
- real-agent matched-budget efficacy is measured
- benchmark economics include overhead
- MACE no longer relies on circular scoring

At that point we can rewrite the headline benchmark claims with confidence, or
lower them if the evidence says we should.
