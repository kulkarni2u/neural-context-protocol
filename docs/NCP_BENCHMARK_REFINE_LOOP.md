# Refine loop benchmark

A deterministic, offline simulation of the v1.9.7 self-improvement loop
(repair, then guarded prune) on a temporary SQLite store. No model is called:
outcomes are scripted so every guard fires exactly once. See
[NCP_SELF_IMPROVEMENT.md](NCP_SELF_IMPROVEMENT.md) for the feature itself.

```bash
python benchmarks/refine_loop/run.py
```

The command prints a JSON artifact. `summary.checks` holds twelve booleans and
`summary.pass` is true only when all of them are. CI fails the build otherwise.

## What each check proves

| Check | Proves |
|---|---|
| `ingest_seed` | A procedure can be ingested and becomes the head. |
| `repair_applied` | Recurring failures yield a repair proposal that, after a trial, is adopted as the new (longer) head. |
| `prune_refused_without_anchor` | A prune is refused while the head has fewer than `[refine].min_anchor_outcomes` outcomes. |
| `good_prune_applied` | A shorter candidate with a success rate at or above the gate and fewer output tokens is adopted. |
| `bad_prune_gated_and_rejected` | A candidate whose success rate is below `gate_ratio` x anchor rate raises `RefineGateError`, stays off the head, and can be rejected. |
| `stale_apply_refused` | Applying a candidate whose predecessor is no longer the head is refused, even with `force`. |
| `rollback_marks_rolled_back` | Rollback restores the earlier content and marks the attempt `rolled_back`. |
| `block_lists_every_attempt` | The `[NCP:IMPROVEMENT]` block lists every attempt, including rejected and rolled-back ones. |
| `prune_shorter_than_repair` | After the prune the head is shorter than after the repair. |
| `holdout_gate_blocks_train_only_win` | A candidate that passes on pooled outcomes but fails on the `holdout` split is refused by the default (`auto`) gate with `split_used=holdout`. |
| `compare_ranks_best_first` | `compare_candidates` ranks the better of two prune candidates first and a stale candidate last. |
| `auto_calibration_changed_trust` | Recording outcomes through the MCP `ncp_record_outcome` handler changes a chunk's `base_trust` without an operator running `ncp calibrate`. |

## Reported only

`paired_comparison` runs `ncp.eval.paired_comparison` over a fixed synthetic
before/after series (mean difference, 95% t-interval, wins/losses, exact sign
test). It illustrates the helper and is not part of the gate.

## Determinism

The artifact contains no timestamps or generated ids, so two runs are equal.
`tests/test_benchmark_refine_loop.py` asserts this.
