"""Prune proposals, the apply gate, stale-head check, reject and rollback (v1.9.7)."""

from __future__ import annotations

from pathlib import Path

import pytest

from ncp.refine import (
    RefineError,
    RefineGateError,
    apply_refinement,
    find_procedure_head,
    ingest_procedure,
    propose_prune,
    propose_refinement,
    reject_refinement,
    rollback_refinement,
)
from ncp.stores.base import BaseStore
from ncp.stores.sqlite import SQLiteStore
from ncp.types import OutcomeRecord

LONG = (
    "Deploy checklist. Run the unit tests before every deploy. Verify the migration plan with the "
    "database owner. Confirm the rollback command works in staging. Announce the deploy window in "
    "the team channel. Watch the dashboards for ten minutes after the deploy finishes."
)
SHORT = "Deploy checklist. Run tests, verify migrations, confirm rollback, announce, then watch dashboards."


def _out(
    store: SQLiteStore,
    oid: str,
    chunk_id: str,
    success: bool,
    *,
    task: str | None = None,
    tokens: int | None = None,
    model: str | None = None,
    note: str | None = None,
) -> None:
    store.record_outcome(
        OutcomeRecord(
            outcome_id=oid, chunk_ids=[chunk_id], success=success, task=task,
            output_tokens=tokens, model=model, note=note,
        )
    )


def _anchored(tmp_path: Path, *, n: int = 5, tokens: int = 100) -> tuple[SQLiteStore, str]:
    store = SQLiteStore(tmp_path / "store.db")
    head = ingest_procedure(store, key="deploy", content=LONG)
    for i in range(n):
        _out(store, f"a{i}", head.chunk_id, True, tokens=tokens)
    return store, head.chunk_id


def _prune(store: SQLiteStore, **kw: object) -> str:
    result = propose_prune(store, key="deploy", content=SHORT, **kw)  # type: ignore[arg-type]
    assert result.new_chunk is not None
    return result.new_chunk.chunk_id


def _attempt(store: BaseStore, chunk_id: str):  # type: ignore[no-untyped-def]
    found = store.list_improvement_attempts(candidate_chunk_id=chunk_id)
    assert len(found) == 1
    return found[0]


def test_repair_records_attempt_with_default_problem(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "store.db")
    head = ingest_procedure(store, key="rule", content="Always validate input.")
    for i in range(3):
        _out(store, f"o{i}", head.chunk_id, False, note="skipped validation step")
    result = propose_refinement(store, key="rule", predicted=["t1"], rationale="why", model="m1")
    assert result.new_chunk is not None
    att = _attempt(store, result.new_chunk.chunk_id)
    assert att.kind == "repair" and att.status == "proposed"
    assert att.predicted == ["t1"] and att.rationale == "why" and att.model == "m1"
    problem = store.get_improvement_problem(att.problem_id)
    assert problem is not None and problem.name == "recurring failures in rule" and problem.scope == "rule"


def test_repair_with_named_problem(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "store.db")
    head = ingest_procedure(store, key="rule", content="Always validate input.")
    for i in range(3):
        _out(store, f"o{i}", head.chunk_id, False, note="skipped validation step")
    result = propose_refinement(store, key="rule", problem_name="Validation  skipped")
    assert result.new_chunk is not None
    problem = store.get_improvement_problem(_attempt(store, result.new_chunk.chunk_id).problem_id)
    assert problem is not None and problem.name == "Validation  skipped"


def test_repair_dry_run_records_no_attempt(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "store.db")
    head = ingest_procedure(store, key="rule", content="Always validate input.")
    for i in range(3):
        _out(store, f"o{i}", head.chunk_id, False, note="skipped validation step")
    propose_refinement(store, key="rule", dry_run=True)
    assert store.list_improvement_attempts() == []


def test_unknown_problem_id_rejected(tmp_path: Path) -> None:
    store, _ = _anchored(tmp_path)
    with pytest.raises(RefineError, match="unknown improvement problem"):
        propose_prune(store, key="deploy", content=SHORT, problem_id="prob_nope")


def test_prune_refused_without_anchor(tmp_path: Path) -> None:
    store, _ = _anchored(tmp_path, n=2)
    with pytest.raises(RefineError, match="no anchor: head has 2 outcomes"):
        propose_prune(store, key="deploy", content=SHORT)
    assert store.list_improvement_attempts() == []


def test_prune_refused_if_not_shorter(tmp_path: Path) -> None:
    store, _ = _anchored(tmp_path)
    with pytest.raises(RefineError, match="shorter"):
        propose_prune(store, key="deploy", content=LONG)


def test_prune_dry_run_and_records_anchor(tmp_path: Path) -> None:
    store, head_id = _anchored(tmp_path)
    dry = propose_prune(store, key="deploy", content=SHORT, dry_run=True)
    assert dry.new_chunk is None and store.list_improvement_attempts() == []
    cand_id = _prune(store, predicted=["t1"])
    att = _attempt(store, cand_id)
    assert att.kind == "prune" and att.anchor_rate == 1.0 and att.anchor_output_tokens == 100
    problem = store.get_improvement_problem(att.problem_id)
    assert problem is not None and problem.name == "token cost of deploy"
    cand = store.get_chunks_by_ids([cand_id])[0]
    assert cand.supersedes == head_id and cand.base_trust == 0.4 and cand.generation == 1
    head = find_procedure_head(store, key="deploy")
    assert head is not None and head.chunk_id == head_id  # proposing never adopts


def test_gate_pass_applies_and_resolves_attempt(tmp_path: Path) -> None:
    store, _ = _anchored(tmp_path)
    cand_id = _prune(store, predicted=["t1", "t2"])
    for i in range(5):
        _out(store, f"c{i}", cand_id, True, task="t1", tokens=60)
    adopted = apply_refinement(store, chunk_id=cand_id)
    assert adopted.chunk_id == cand_id
    head = find_procedure_head(store, key="deploy")
    assert head is not None and head.chunk_id == cand_id
    att = _attempt(store, cand_id)
    assert att.status == "applied" and att.accepted and att.resolved_at is not None
    assert att.fixed == ["t1"] and att.missed == ["t2"]
    assert att.passed == 5 and att.total == 5 and att.mean_output_tokens == 60


def test_gate_fails_on_low_success_rate(tmp_path: Path) -> None:
    store, head_id = _anchored(tmp_path)
    cand_id = _prune(store)
    for i in range(3):
        _out(store, f"c{i}", cand_id, True, tokens=60)
    for i in range(2):
        _out(store, f"f{i}", cand_id, False, tokens=60)
    with pytest.raises(RefineGateError) as err:
        apply_refinement(store, chunk_id=cand_id)
    assert err.value.details["anchor_rate"] == 1.0
    assert err.value.details["candidate_rate"] == pytest.approx(0.6)
    assert err.value.details["n"] == 5
    assert _attempt(store, cand_id).status == "proposed"
    head = find_procedure_head(store, key="deploy")
    assert head is not None and head.chunk_id == head_id


def test_gate_fails_on_too_few_trial_outcomes(tmp_path: Path) -> None:
    store, _ = _anchored(tmp_path)
    cand_id = _prune(store)
    _out(store, "c0", cand_id, True, tokens=60)
    with pytest.raises(RefineGateError):
        apply_refinement(store, chunk_id=cand_id)
    apply_refinement(store, chunk_id=cand_id, min_trial_outcomes=1)


def test_gate_fails_when_tokens_not_lower(tmp_path: Path) -> None:
    store, _ = _anchored(tmp_path)
    cand_id = _prune(store)
    for i in range(5):
        _out(store, f"c{i}", cand_id, True, tokens=100)
    with pytest.raises(RefineGateError, match="tokens"):
        apply_refinement(store, chunk_id=cand_id)


def test_gate_is_model_filtered(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "store.db")
    head = ingest_procedure(store, key="deploy", content=LONG)
    for i in range(5):
        _out(store, f"a{i}", head.chunk_id, True, model="m1")
        _out(store, f"b{i}", head.chunk_id, False, model="m2")
    cand_id = _prune(store, model="m1")
    assert _attempt(store, cand_id).anchor_rate == 1.0
    for i in range(5):
        _out(store, f"c{i}", cand_id, True, model="m1")
        _out(store, f"d{i}", cand_id, False, model="m2")
    apply_refinement(store, chunk_id=cand_id)  # m2 failures are ignored


def test_force_bypasses_gate(tmp_path: Path) -> None:
    store, _ = _anchored(tmp_path)
    cand_id = _prune(store)
    apply_refinement(store, chunk_id=cand_id, force=True)
    att = _attempt(store, cand_id)
    assert att.status == "applied" and att.accepted


def test_stale_candidate_refused_even_with_force(tmp_path: Path) -> None:
    store, head_id = _anchored(tmp_path)
    first = _prune(store)
    second_content = "Deploy checklist. Tests, migrations, rollback, announce, watch."
    second = propose_prune(store, key="deploy", content=second_content).new_chunk
    assert second is not None
    apply_refinement(store, chunk_id=first, force=True)
    for force in (False, True):
        with pytest.raises(RefineError, match="stale"):
            apply_refinement(store, chunk_id=second.chunk_id, force=force)
    head = find_procedure_head(store, key="deploy")
    assert head is not None and head.chunk_id == first
    assert head_id != first


def test_reject_marks_attempt_and_keeps_candidate(tmp_path: Path) -> None:
    store, _ = _anchored(tmp_path)
    cand_id = _prune(store, predicted=["t1"])
    _out(store, "c0", cand_id, False, task="t1", tokens=70)
    att = reject_refinement(store, chunk_id=cand_id, reason="regressed")
    assert att.status == "rejected" and not att.accepted and att.note == "regressed"
    assert att.resolved_at is not None and att.passed == 0 and att.total == 1
    assert att.missed == ["t1"]
    assert store.list_chunks(layer="procedural", limit=50)["chunks"]
    with pytest.raises(RefineError, match="already rejected"):
        reject_refinement(store, chunk_id=cand_id)


def test_reject_without_attempt_raises(tmp_path: Path) -> None:
    store, head_id = _anchored(tmp_path)
    with pytest.raises(RefineError, match="no improvement attempt"):
        reject_refinement(store, chunk_id=head_id)


def test_rollback_marks_attempt_rolled_back(tmp_path: Path) -> None:
    store, _ = _anchored(tmp_path)
    cand_id = _prune(store)
    apply_refinement(store, chunk_id=cand_id, force=True)
    reverted = rollback_refinement(store, key="deploy")
    assert reverted.content == LONG
    att = _attempt(store, cand_id)
    assert att.status == "rolled_back" and att.resolved_at is not None


def test_unsupported_store_is_skipped_silently(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store = SQLiteStore(tmp_path / "store.db")
    head = ingest_procedure(store, key="rule", content="Always validate input.")
    for i in range(3):
        _out(store, f"o{i}", head.chunk_id, False, note="skipped validation step")

    def boom(*a: object, **k: object) -> None:
        raise NotImplementedError("improvement records require the sqlite or pgvector store")

    for name in (
        "upsert_improvement_problem", "get_improvement_problem", "record_improvement_attempt",
        "update_improvement_attempt", "list_improvement_attempts",
    ):
        monkeypatch.setattr(store, name, boom)
    result = propose_refinement(store, key="rule")
    assert result.new_chunk is not None
    apply_refinement(store, chunk_id=result.new_chunk.chunk_id)
    rollback_refinement(store, key="rule")
    with pytest.raises(RefineError):
        reject_refinement(store, chunk_id=result.new_chunk.chunk_id)


def test_prune_without_outcome_stats_raises_refine_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store, _ = _anchored(tmp_path)

    def boom(*a: object, **k: object) -> None:
        raise NotImplementedError("nope")

    monkeypatch.setattr(store, "outcome_stats", boom)
    with pytest.raises(RefineError):
        propose_prune(store, key="deploy", content=SHORT)


# ── review fixes: gate bypass, trial contamination, attempt status ───────────


def test_manual_attempt_cannot_bypass_prune_gate(tmp_path: Path) -> None:
    import time

    from ncp.stores.improvement_common import new_attempt_id
    from ncp.types import ImprovementAttempt, ImprovementProblem

    store, _ = _anchored(tmp_path)
    cand_id = _prune(store)
    store.upsert_improvement_problem(
        ImprovementProblem(problem_id="prob_x", scope="deploy", name="n", opened_at=time.time())
    )
    time.sleep(0.01)
    store.record_improvement_attempt(
        ImprovementAttempt(
            attempt_id=new_attempt_id(), problem_id="prob_x", kind="manual", what="w",
            candidate_chunk_id=cand_id, created_at=time.time(),
        )
    )
    with pytest.raises(RefineGateError):
        apply_refinement(store, chunk_id=cand_id)


def test_shared_outcomes_do_not_count_for_candidate_trial(tmp_path: Path) -> None:
    store, head_id = _anchored(tmp_path)
    cand_id = _prune(store)
    for i in range(5):  # outcomes crediting head AND candidate are no trial evidence
        store.record_outcome(
            OutcomeRecord(outcome_id=f"s{i}", chunk_ids=[head_id, cand_id], success=True, output_tokens=10)
        )
    with pytest.raises(RefineGateError):
        apply_refinement(store, chunk_id=cand_id)


def test_anchor_recomputed_at_apply_time_excludes_candidate(tmp_path: Path) -> None:
    store, head_id = _anchored(tmp_path)
    cand_id = _prune(store)
    for i in range(5):
        _out(store, f"c{i}", cand_id, True, tokens=50)
    # Head degrades after the proposal; a stale stored anchor of 1.0 would block 5/5 trial.
    for i in range(5):
        _out(store, f"h{i}", head_id, False, tokens=100)
    apply_refinement(store, chunk_id=cand_id)
    att = _attempt(store, cand_id)
    assert att.status == "applied" and att.anchor_rate == 0.5


def test_apply_refused_when_attempt_not_proposed_even_with_force(tmp_path: Path) -> None:
    store, _ = _anchored(tmp_path)
    cand_id = _prune(store)
    reject_refinement(store, chunk_id=cand_id)
    with pytest.raises(RefineError, match="already rejected"):
        apply_refinement(store, chunk_id=cand_id, force=True)


def test_apply_refused_when_candidate_has_no_source_key(tmp_path: Path) -> None:
    from ncp.types import SubconsciousChunk

    store, head_id = _anchored(tmp_path)
    store.write(SubconsciousChunk(
        chunk_id="orphan", layer="procedural", content="orphan procedure text", src="tool_result",
        written_by="x", supersedes=head_id, source_refs=[],
    ))
    with pytest.raises(RefineError, match="cannot verify head"):
        apply_refinement(store, chunk_id="orphan")
