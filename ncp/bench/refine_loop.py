"""Refine loop benchmark: the repair -> prune loop with its improvement records.

A deterministic, offline simulation on a real temp SQLite store. No model is
called: outcomes are scripted so each guard of the v1.9.7 loop is exercised
exactly once and the result is reproducible.

The loop under test (see docs/NCP_SELF_IMPROVEMENT.md):

1. a procedure is ingested and fails repeatedly -> a repair is proposed, given
   a short trial and applied (the procedure grows);
2. a prune is refused until the head has an anchor, then a good prune (same
   success rate, fewer tokens) is applied while a bad prune (lower success
   rate) hits the gate and is rejected;
3. a stale candidate is refused and a rollback is recorded as history;
4. the improvement block still lists every attempt, and recording outcomes
   through the MCP handler auto-calibrates the trust of the chunks involved.

Each check is a boolean under ``summary.checks``; ``summary.pass`` is their
conjunction. ``paired_comparison`` over a fixed synthetic series is reported
only (it is not part of the gate).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ncp.config import load_config
from ncp.eval import paired_comparison
from ncp.improvement import build_improvement_view, render_improvement_block
from ncp.mcp.server import make_handlers
from ncp.refine import (
    RefineError,
    RefineGateError,
    apply_refinement,
    compare_candidates,
    find_procedure_head,
    ingest_procedure,
    propose_prune,
    propose_refinement,
    reject_refinement,
    rollback_refinement,
)
from ncp.stores.sqlite import SQLiteStore
from ncp.tokens import token_unit
from ncp.types import OutcomeRecord, SubconsciousChunk

_KEY = "deploy"
_SEED = (
    "Deploy checklist. Run the unit tests before every deploy. Verify the migration plan with the "
    "database owner. Confirm the rollback command works in staging. Announce the deploy window in "
    "the team channel. Watch the dashboards for ten minutes after the deploy finishes."
)
_PRUNE_GOOD = "Deploy checklist. Run tests, verify migrations, confirm rollback, announce, then watch dashboards."
_PRUNE_BAD = "Deploy checklist. Tests, migrations, rollback, announce, watch."
_PRUNE_A = "Deploy: tests, migrations, rollback, announce, watch."
_PRUNE_B = "Deploy checklist: verify then ship."
_MODEL = "sim"
_ANCHOR_TOKENS = 100
_PRUNED_TOKENS = 60

# Fixed synthetic paired series (score per task: before vs. after the loop).
_PAIRED_BEFORE = [0.50, 0.55, 0.60, 0.40, 0.65, 0.50, 0.45, 0.70, 0.55, 0.60]
_PAIRED_AFTER = [0.60, 0.60, 0.65, 0.50, 0.65, 0.60, 0.55, 0.75, 0.55, 0.70]


def _outcome(
    store: SQLiteStore, oid: str, chunk_id: str, success: bool, *,
    tokens: int | None = None, task: str | None = None, note: str | None = None, split: str | None = None,
) -> None:
    store.record_outcome(OutcomeRecord(
        outcome_id=oid, chunk_ids=[chunk_id], success=success, output_tokens=tokens, task=task, note=note,
        model=_MODEL, split=split,
    ))


def _raises(exc_type: type[Exception], fn: Any, *args: Any, **kwargs: Any) -> Exception | None:
    try:
        fn(*args, **kwargs)
    except exc_type as exc:
        return exc
    return None


def _head_chars(store: SQLiteStore) -> int:
    head = find_procedure_head(store, key=_KEY)
    return len(head.content) if head is not None else -1


def _holdout_and_compare_checks(config: Any, project: Path) -> dict[str, bool]:
    """Holdout-gate and candidate-comparison checks, on their own store.

    Separate stores keep the attempt history of the main loop (asserted by
    ``block_lists_every_attempt``) untouched and the dedup guard out of the way.
    """
    store = SQLiteStore(project / ".ncp" / "store_holdout.db", config=config)
    checks: dict[str, bool] = {}

    # Holdout gate: the candidate wins on train outcomes only; the head is weak on train
    # (so the pooled rate passes) but strong on holdout, where the candidate fails.
    seed = ingest_procedure(store, key="holdout_proc", content=_SEED)
    for i in range(5):
        _outcome(store, f"ha{i}", seed.chunk_id, True, tokens=_ANCHOR_TOKENS, split="holdout")
        _outcome(store, f"hb{i}", seed.chunk_id, False, tokens=_ANCHOR_TOKENS, split="train")
    cand = propose_prune(store, key="holdout_proc", content=_PRUNE_GOOD, model=_MODEL)
    holdout_ok = False
    if cand.new_chunk is not None:
        cid = cand.new_chunk.chunk_id
        for i in range(5):
            _outcome(store, f"hc{i}", cid, True, tokens=_PRUNED_TOKENS, split="train")
            _outcome(store, f"hd{i}", cid, False, tokens=_PRUNED_TOKENS, split="holdout")
        # Pooled over every outcome the candidate would pass; only the holdout split blocks it.
        pooled = compare_candidates(store, key="holdout_proc", model=_MODEL, split="all")
        gate_err = _raises(RefineGateError, apply_refinement, store, chunk_id=cid)
        head = find_procedure_head(store, key="holdout_proc")
        holdout_ok = (
            pooled.candidates[0].would_pass_gate is True
            and isinstance(gate_err, RefineGateError)
            and gate_err.details.get("split_used") == "holdout"
            and head is not None and head.chunk_id == seed.chunk_id
        )
    checks["holdout_gate_blocks_train_only_win"] = holdout_ok

    # Compare: Z is proposed against the old head and goes stale; X (better) and Y (worse)
    # are proposed against the new head.
    store = SQLiteStore(project / ".ncp" / "store_compare.db", config=config)
    cmp_seed = ingest_procedure(store, key="cmp_proc", content=_SEED)
    for i in range(5):
        _outcome(store, f"ca{i}", cmp_seed.chunk_id, True, tokens=_ANCHOR_TOKENS)
    z = propose_prune(store, key="cmp_proc", content=_PRUNE_B, model=_MODEL)
    w = propose_prune(store, key="cmp_proc", content=_PRUNE_GOOD, model=_MODEL)
    ranked_ok = False
    if z.new_chunk is not None and w.new_chunk is not None:
        apply_refinement(store, chunk_id=w.new_chunk.chunk_id, force=True)
        for i in range(5):
            _outcome(store, f"cw{i}", w.new_chunk.chunk_id, True, tokens=_ANCHOR_TOKENS)
        x = propose_prune(store, key="cmp_proc", content=_PRUNE_A, model=_MODEL)
        y = propose_prune(store, key="cmp_proc", content=_PRUNE_BAD, model=_MODEL)
        if x.new_chunk is not None and y.new_chunk is not None:
            for i in range(5):
                _outcome(store, f"cx{i}", x.new_chunk.chunk_id, True, tokens=_PRUNED_TOKENS)
                _outcome(store, f"cz{i}", z.new_chunk.chunk_id, True, tokens=_PRUNED_TOKENS // 2)
            for i in range(3):
                _outcome(store, f"cy{i}", y.new_chunk.chunk_id, True, tokens=_PRUNED_TOKENS)
            for i in range(2):
                _outcome(store, f"cf{i}", y.new_chunk.chunk_id, False, tokens=_PRUNED_TOKENS)
            result = compare_candidates(store, key="cmp_proc", model=_MODEL)
            order = [c.chunk_id for c in result.candidates]
            ranked_ok = (
                order == [x.new_chunk.chunk_id, y.new_chunk.chunk_id, z.new_chunk.chunk_id]
                and result.candidates[0].would_pass_gate is True
                and result.candidates[1].would_pass_gate is False
                and result.candidates[2].status == "stale"
            )
    checks["compare_ranks_best_first"] = ranked_ok
    return checks


def run_refine_loop_benchmark(*, workdir: str | Path) -> dict[str, object]:
    """Run the scripted loop and return a JSON-serializable artifact with a pass gate."""
    project = Path(workdir) / "refine_loop"
    (project / ".git").mkdir(parents=True)
    config = load_config(cwd=project, env={})
    store = SQLiteStore(project / ".ncp" / "store.db", config=config)
    checks: dict[str, bool] = {}
    metrics: dict[str, object] = {}

    # 1. ingest seed
    seed = ingest_procedure(store, key=_KEY, content=_SEED)
    checks["ingest_seed"] = (
        _head_chars(store) == len(_SEED) and find_procedure_head(store, key=_KEY) is not None
    )

    # 2. failures -> repair propose -> trial outcomes -> apply
    for i, note in enumerate(("skipped migration check", "skipped migration check", "forgot rollback test")):
        _outcome(store, f"f{i}", seed.chunk_id, False, tokens=_ANCHOR_TOKENS, task=f"t{i}", note=note)
    repair = propose_refinement(
        store, key=_KEY, predicted=["t0", "t1"], rationale="notes record the recurring failures", model=_MODEL,
    )
    repair_ok = False
    repaired_chars = -1
    if repair.new_chunk is not None:
        repair_id = repair.new_chunk.chunk_id
        # Two trial outcomes only: fewer than the prune anchor minimum, so check 3 is meaningful.
        for i in range(2):
            _outcome(store, f"r{i}", repair_id, True, tokens=_ANCHOR_TOKENS, task="t0")
        adopted = apply_refinement(store, chunk_id=repair_id)
        head = find_procedure_head(store, key=_KEY)
        repaired_chars = _head_chars(store)
        repair_ok = (
            head is not None and head.chunk_id == repair_id == adopted.chunk_id and repaired_chars > len(_SEED)
        )
    checks["repair_applied"] = repair_ok
    metrics["seed_chars"] = len(_SEED)
    metrics["after_repair_chars"] = repaired_chars

    # 3. prune refused before the anchor exists
    err = _raises(RefineError, propose_prune, store, key=_KEY, content=_PRUNE_GOOD)
    checks["prune_refused_without_anchor"] = (
        err is not None and "no anchor" in str(err) and not isinstance(err, RefineGateError)
    )

    # Anchor: enough successful outcomes on the repaired head.
    for i in range(4):
        _outcome(store, f"a{i}", find_procedure_head(store, key=_KEY).chunk_id, True, tokens=_ANCHOR_TOKENS)  # type: ignore[union-attr]

    # 4. good prune: shorter, success rate >= gate, fewer tokens
    good_ok = False
    good = propose_prune(
        store, key=_KEY, content=_PRUNE_GOOD, predicted=["t0"], rationale="same steps, fewer words", model=_MODEL,
    )
    if good.new_chunk is not None:
        good_id = good.new_chunk.chunk_id
        for i in range(5):
            _outcome(store, f"g{i}", good_id, True, tokens=_PRUNED_TOKENS, task="t0")
        apply_refinement(store, chunk_id=good_id)
        head = find_procedure_head(store, key=_KEY)
        good_ok = head is not None and head.chunk_id == good_id
    pruned_chars = _head_chars(store)
    checks["good_prune_applied"] = good_ok and 0 < pruned_chars < repaired_chars
    metrics["after_prune_chars"] = pruned_chars

    # 5. bad prune: rate below the gate -> RefineGateError, then rejected
    bad_ok = False
    bad = propose_prune(store, key=_KEY, content=_PRUNE_BAD, rationale="aggressive cut", model=_MODEL)
    if bad.new_chunk is not None:
        bad_id = bad.new_chunk.chunk_id
        for i in range(3):
            _outcome(store, f"bg{i}", bad_id, True, tokens=40)
        for i in range(2):
            _outcome(store, f"bf{i}", bad_id, False, tokens=40)
        gate_err = _raises(RefineGateError, apply_refinement, store, chunk_id=bad_id)
        rejected = reject_refinement(store, chunk_id=bad_id, reason="rate below gate")
        head = find_procedure_head(store, key=_KEY)
        bad_ok = (
            isinstance(gate_err, RefineGateError)
            and rejected.status == "rejected"
            and head is not None and head.chunk_id != bad_id
        )
    checks["bad_prune_gated_and_rejected"] = bad_ok

    # 6. stale candidate apply refused (two candidates against the same head)
    stale_ok = False
    a = propose_prune(store, key=_KEY, content=_PRUNE_A, model=_MODEL)
    b = propose_prune(store, key=_KEY, content=_PRUNE_B, model=_MODEL)
    applied_a_id: str | None = None
    if a.new_chunk is not None and b.new_chunk is not None:
        applied_a_id = a.new_chunk.chunk_id
        apply_refinement(store, chunk_id=applied_a_id, force=True)
        stale_err = _raises(RefineError, apply_refinement, store, chunk_id=b.new_chunk.chunk_id, force=True)
        head = find_procedure_head(store, key=_KEY)
        stale_ok = (
            stale_err is not None and "stale" in str(stale_err)
            and head is not None and head.chunk_id == applied_a_id
        )
    checks["stale_apply_refused"] = stale_ok

    # 7. rollback marks the attempt rolled_back
    rollback_ok = False
    if applied_a_id is not None:
        rollback_refinement(store, key=_KEY)
        attempts = store.list_improvement_attempts(candidate_chunk_id=applied_a_id)
        rollback_ok = len(attempts) == 1 and attempts[0].status == "rolled_back"
    checks["rollback_marks_rolled_back"] = rollback_ok

    # 8. the improvement block lists every attempt, including rejected and rolled-back ones
    all_attempts = store.list_improvement_attempts()
    view = build_improvement_view(store)
    block, telemetry = render_improvement_block(view)
    attempt_lines = [line for line in block.splitlines() if line.startswith("  - ")]
    statuses = {att.status for att in all_attempts}
    checks["block_lists_every_attempt"] = (
        len(all_attempts) == 5
        and len(attempt_lines) == len(all_attempts) == telemetry["attempts"]
        and {"applied", "rejected", "rolled_back", "proposed"} <= statuses
        and any(line.startswith("  - rejected ") for line in attempt_lines)
        and any(line.startswith("  - rolled_back ") for line in attempt_lines)
    )
    metrics["attempts"] = len(all_attempts)
    metrics["attempt_statuses"] = sorted(att.status for att in all_attempts)

    # 9. the procedure head is shorter after the prune than after the repair
    checks["prune_shorter_than_repair"] = 0 < pruned_chars < repaired_chars

    # 10. auto-calibration: recording outcomes through the MCP handler moves base_trust
    store.write(SubconsciousChunk(
        chunk_id="calib_fact", layer="semantic", content="auto calibration subject for the refine loop",
        src="tool_result", pipeline_id="bench_refine", written_by="oracle", base_trust=0.5,
    ))
    before = store.get_chunks_by_ids(["calib_fact"])[0].base_trust
    handlers = make_handlers(store, config=config)
    result: dict[str, Any] = {}
    for _ in range(3):
        result = handlers["ncp_record_outcome"](
            {"success": True, "chunk_ids": ["calib_fact"], "ctx": {"eval_source": "verifier"}}
        )
    after = store.get_chunks_by_ids(["calib_fact"])[0].base_trust
    checks["auto_calibration_changed_trust"] = bool(result) and after != before
    metrics["calibrated_trust"] = {"before": round(before, 4), "after": round(after, 4)}

    checks.update(_holdout_and_compare_checks(config, project))

    paired = paired_comparison(_PAIRED_BEFORE, _PAIRED_AFTER)
    return {
        "benchmark": "refine_loop",
        "token_unit": token_unit(),
        "config": {
            "gate_ratio": config.refine_gate_ratio,
            "min_trial_outcomes": config.refine_min_trial_outcomes,
            "min_anchor_outcomes": config.refine_min_anchor_outcomes,
            "anchor_output_tokens": _ANCHOR_TOKENS,
            "pruned_output_tokens": _PRUNED_TOKENS,
        },
        "metrics": metrics,
        "paired_comparison": {
            "description": "fixed synthetic before/after series; reported only, not part of the gate.",
            **{k: (round(v, 6) if isinstance(v, float) else v) for k, v in paired.__dict__.items()},
        },
        "summary": {"checks": checks, "pass": all(checks.values())},
    }
