"""Holdout split: outcome tagging, split-filtered stats and the apply gate (v1.9.7)."""

from __future__ import annotations

from pathlib import Path

import pytest
from click.testing import CliRunner

from ncp.cli import main
from ncp.config import load_config
from ncp.refine import (
    RefineError,
    RefineGateError,
    apply_refinement,
    find_procedure_head,
    ingest_procedure,
    propose_prune,
    propose_refinement,
)
from ncp.stores.factory import create_store
from ncp.stores.sqlite import SQLiteStore
from ncp.types import OutcomeRecord

LONG = (
    "Deploy checklist. Run the unit tests before every deploy. Verify the migration plan with the "
    "database owner. Confirm the rollback command works in staging. Announce the deploy window in "
    "the team channel. Watch the dashboards for ten minutes after the deploy finishes."
)
SHORT = "Deploy checklist. Run tests, verify migrations, confirm rollback, announce, then watch dashboards."


def _out(
    store: SQLiteStore, oid: str, chunk_id: str, success: bool, *,
    split: str | None = None, tokens: int | None = None, weight: float = 1.0, note: str | None = None,
) -> None:
    store.record_outcome(OutcomeRecord(
        outcome_id=oid, chunk_ids=[chunk_id], success=success, split=split, output_tokens=tokens,
        weight=weight, note=note,
    ))


def _many(store: SQLiteStore, prefix: str, chunk_id: str, n: int, success: bool, **kw: object) -> None:
    for i in range(n):
        _out(store, f"{prefix}{i}", chunk_id, success, **kw)  # type: ignore[arg-type]


def _train_only_win(tmp_path: Path, *, holdout_anchor: int = 5, holdout_trial: int = 5) -> tuple[SQLiteStore, str, str]:
    """Head weak on train, strong on holdout; candidate the reverse. Pooled it passes the gate."""
    store = SQLiteStore(tmp_path / "s.db")
    head = ingest_procedure(store, key="deploy", content=LONG)
    _many(store, "ha", head.chunk_id, holdout_anchor, True, split="holdout", tokens=100)
    _many(store, "hb", head.chunk_id, 5, False, split="train", tokens=100)
    cand = propose_prune(store, key="deploy", content=SHORT)
    assert cand.new_chunk is not None
    cid = cand.new_chunk.chunk_id
    _many(store, "ct", cid, 5, True, split="train", tokens=60)
    _many(store, "ch", cid, holdout_trial, False, split="holdout", tokens=60)
    return store, head.chunk_id, cid


def _attempt(store: SQLiteStore, cid: str):  # type: ignore[no-untyped-def]
    (att,) = store.list_improvement_attempts(candidate_chunk_id=cid)
    return att


def test_outcome_split_roundtrip_and_length_bound(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "s.db")
    _out(store, "o1", "c", True, split="holdout")
    _out(store, "o2", "c", True)
    assert store.get_outcome("o1").split == "holdout"  # type: ignore[union-attr]
    assert store.get_outcome("o2").split is None  # type: ignore[union-attr]
    with pytest.raises(ValueError):
        OutcomeRecord(chunk_ids=["c"], success=True, split="x" * 33)


def test_outcome_stats_split_filter(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "s.db")
    _many(store, "t", "c", 3, True, split="train", tokens=10)
    _many(store, "h", "c", 2, False, split="holdout", tokens=30)
    _out(store, "u", "c", True)
    assert store.outcome_stats(chunk_ids=["c"]).n == 6
    train = store.outcome_stats(chunk_ids=["c"], split="train")
    assert (train.n, train.success_rate, train.mean_output_tokens) == (3, 1.0, 10.0)
    hold = store.outcome_stats(chunk_ids=["c"], split="holdout")
    assert (hold.n, hold.success_rate, hold.mean_output_tokens) == (2, 0.0, 30.0)
    assert store.outcome_stats(chunk_ids=["c"], split="nope").n == 0


def test_auto_uses_holdout_when_sufficient(tmp_path: Path) -> None:
    store, head_id, cid = _train_only_win(tmp_path)
    with pytest.raises(RefineGateError) as err:
        apply_refinement(store, chunk_id=cid)
    assert err.value.details["split_used"] == "holdout"
    assert find_procedure_head(store, key="deploy").chunk_id == head_id  # type: ignore[union-attr]


def test_all_mode_ignores_split_and_notes_it(tmp_path: Path) -> None:
    store, _, cid = _train_only_win(tmp_path)
    apply_refinement(store, chunk_id=cid, gate_split="all")  # pooled evidence passes
    att = _attempt(store, cid)
    assert att.status == "applied" and att.note == "gate split: all"


def test_auto_falls_back_to_all_when_holdout_insufficient(tmp_path: Path) -> None:
    # Candidate has only 3 holdout outcomes (< min_trial_outcomes) -> pooled gate -> passes.
    store, _, cid = _train_only_win(tmp_path, holdout_trial=3)
    apply_refinement(store, chunk_id=cid)
    assert _attempt(store, cid).note == "gate split: all"
    # Head with too little holdout weight also falls back.
    (tmp_path / "b").mkdir()
    store2, _, cid2 = _train_only_win(tmp_path / "b", holdout_anchor=2)
    apply_refinement(store2, chunk_id=cid2)
    assert _attempt(store2, cid2).note == "gate split: all"


def test_holdout_mode_errors_without_evidence(tmp_path: Path) -> None:
    store, head_id, cid = _train_only_win(tmp_path, holdout_trial=2)
    with pytest.raises(RefineGateError, match="holdout evidence") as err:
        apply_refinement(store, chunk_id=cid, gate_split="holdout")
    assert err.value.details["split_used"] == "holdout"
    assert err.value.details["holdout_trial_weight"] == 2.0
    # force bypasses only the gate.
    apply_refinement(store, chunk_id=cid, gate_split="holdout", force=True)
    assert find_procedure_head(store, key="deploy").chunk_id == cid  # type: ignore[union-attr]
    assert _attempt(store, cid).note == "gate split: all"
    assert head_id != cid


def test_passing_holdout_records_split_in_note(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "s.db")
    head = ingest_procedure(store, key="deploy", content=LONG)
    _many(store, "a", head.chunk_id, 5, True, split="holdout", tokens=100)
    cand = propose_prune(store, key="deploy", content=SHORT)
    assert cand.new_chunk is not None
    cid = cand.new_chunk.chunk_id
    _many(store, "c", cid, 5, True, split="holdout", tokens=50)
    apply_refinement(store, chunk_id=cid, gate_split="holdout")
    att = _attempt(store, cid)
    assert att.status == "applied" and att.note == "gate split: holdout" and att.passed == 5.0


def test_repair_is_ungated_but_observed_on_selected_split(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "s.db")
    head = ingest_procedure(store, key="rule", content="Always validate input.")
    _many(store, "f", head.chunk_id, 3, False, note="skipped validation", split="train")
    _many(store, "h", head.chunk_id, 5, True, split="holdout")
    res = propose_refinement(store, key="rule")
    assert res.new_chunk is not None
    cid = res.new_chunk.chunk_id
    _many(store, "rh", cid, 5, False, split="holdout")
    _many(store, "rt", cid, 2, True, split="train")
    apply_refinement(store, chunk_id=cid)  # ungated even though holdout is all failures
    att = _attempt(store, cid)
    assert att.status == "applied" and att.note == "gate split: holdout" and att.total == 5.0


def test_invalid_gate_split_rejected(tmp_path: Path) -> None:
    store, _, cid = _train_only_win(tmp_path)
    with pytest.raises(RefineError, match="gate_split"):
        apply_refinement(store, chunk_id=cid, gate_split="bogus")


def test_config_gate_split_validated(tmp_path: Path) -> None:
    config = load_config(cwd=tmp_path, env={})
    assert config.refine_gate_split == "auto"
    config.values["refine"]["gate_split"] = "HOLDOUT"
    assert config.refine_gate_split == "holdout"
    config.values["refine"]["gate_split"] = "sometimes"
    with pytest.raises(ValueError, match="gate_split"):
        _ = config.refine_gate_split


def test_cli_gate_split_flag_overrides_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("NCP_AUTH_TOKEN", "NCP_HOST", "NCP_PORT"):
        monkeypatch.delenv(name, raising=False)
    assert CliRunner().invoke(main, ["init", "--cwd", str(tmp_path), "--store", "sqlite"]).exit_code == 0
    store = create_store(load_config(cwd=tmp_path))
    head = ingest_procedure(store, key="deploy", content=LONG)
    _many(store, "ha", head.chunk_id, 5, True, split="holdout", tokens=100)
    _many(store, "hb", head.chunk_id, 5, False, split="train", tokens=100)
    cand = propose_prune(store, key="deploy", content=SHORT)
    assert cand.new_chunk is not None
    cid = cand.new_chunk.chunk_id
    _many(store, "ct", cid, 5, True, split="train", tokens=60)
    _many(store, "ch", cid, 5, False, split="holdout", tokens=60)
    blocked = CliRunner().invoke(main, ["refine", "apply", cid, "--cwd", str(tmp_path)])
    assert blocked.exit_code == 2
    assert '"split_used": "holdout"' in blocked.output
    ok = CliRunner().invoke(main, ["refine", "apply", cid, "--gate-split", "all", "--cwd", str(tmp_path)])
    assert ok.exit_code == 0, ok.output
    assert find_procedure_head(store, key="deploy").chunk_id == cid  # type: ignore[union-attr]
