"""compare_candidates and `ncp refine compare` (v1.9.7, read-only)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from ncp.cli import main
from ncp.config import load_config
from ncp.refine import (
    RefineError,
    apply_refinement,
    compare_candidates,
    find_procedure_head,
    ingest_procedure,
    propose_prune,
)
from ncp.stores.base import BaseStore
from ncp.stores.factory import create_store
from ncp.stores.sqlite import SQLiteStore
from ncp.types import OutcomeRecord

LONG = (
    "Deploy checklist. Run the unit tests before every deploy. Verify the migration plan with the "
    "database owner. Confirm the rollback command works in staging. Announce the deploy window in "
    "the team channel. Watch the dashboards for ten minutes after the deploy finishes."
)
P_A = "Deploy checklist. Run tests, verify migrations, confirm rollback, announce, then watch dashboards."
P_B = "Deploy: tests, migrations, rollback, announce, watch."
P_C = "Deploy checklist: verify then ship."
P_X = "Deploy. Run tests first, then check migrations, then announce and watch the dashboards."
P_W = "Deploy checklist. Tests, verify migrations, rollback ready, announce, watch dashboards closely."


def _outs(
    store: BaseStore, prefix: str, ids: list[str], ok: int, bad: int, *, tokens: int | None = 60,
    split: str | None = None,
) -> None:
    for i in range(ok + bad):
        store.record_outcome(OutcomeRecord(
            outcome_id=f"{prefix}{i}", chunk_ids=ids, success=i < ok, output_tokens=tokens, split=split,
        ))


def _cand(store: BaseStore, content: str) -> str:
    res = propose_prune(store, key="deploy", content=content)
    assert res.new_chunk is not None
    return res.new_chunk.chunk_id


def _setup(store: BaseStore) -> dict[str, str]:
    """Head with an anchor; stale Z (proposed before W was adopted); live X (good), Y (bad)."""
    head = ingest_procedure(store, key="deploy", content=LONG)
    _outs(store, "a", [head.chunk_id], 5, 0, tokens=100)
    z = _cand(store, P_C)
    w = _cand(store, P_W)
    apply_refinement(store, chunk_id=w, force=True)
    _outs(store, "w", [w], 5, 0, tokens=100)
    x = _cand(store, P_X)
    y = _cand(store, P_B)
    _outs(store, "x", [x], 5, 0, tokens=60)
    _outs(store, "y", [y], 3, 2, tokens=60)
    _outs(store, "z", [z], 5, 0, tokens=20)
    return {"head": w, "x": x, "y": y, "z": z}


def test_ranks_best_first_stale_last(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "s.db")
    ids = _setup(store)
    res = compare_candidates(store, key="deploy")
    assert res.head_chunk_id == ids["head"]
    assert [c.chunk_id for c in res.candidates] == [ids["x"], ids["y"], ids["z"]]
    x, y, z = res.candidates
    assert (x.status, x.would_pass_gate, x.success_rate, x.split_used) == ("proposed", True, 1.0, "all")
    assert (y.would_pass_gate, y.success_rate) == (False, 0.6) and y.reasons
    assert z.status == "stale" and any("stale" in r for r in z.reasons)
    assert res.recommended is not None and res.recommended.chunk_id == ids["x"]
    assert res.anchor_total_weight == 5.0 and res.anchor_success_rate == 1.0


def test_compare_is_read_only(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "s.db")
    ids = _setup(store)
    before = [(a.attempt_id, a.status, a.note) for a in store.list_improvement_attempts()]
    compare_candidates(store, key="deploy")
    assert [(a.attempt_id, a.status, a.note) for a in store.list_improvement_attempts()] == before
    assert find_procedure_head(store, key="deploy").chunk_id == ids["head"]  # type: ignore[union-attr]


def test_excludes_other_candidates_outcomes(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "s.db")
    ids = _setup(store)
    # A failing outcome crediting both X and Y belongs to neither trial; one crediting the
    # head and Y is not Y's evidence either.
    _outs(store, "shared", [ids["x"], ids["y"]], 0, 4, tokens=60)
    _outs(store, "headshared", [ids["head"], ids["y"]], 0, 4, tokens=60)
    res = compare_candidates(store, key="deploy")
    by_id = {c.chunk_id: c for c in res.candidates}
    assert by_id[ids["x"]].total_weight == 5.0 and by_id[ids["x"]].success_rate == 1.0
    assert by_id[ids["y"]].total_weight == 5.0 and by_id[ids["y"]].success_rate == 0.6
    # The anchor excludes every candidate too.
    assert res.anchor_total_weight == 5.0


def test_auto_prefers_holdout_per_candidate(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "s.db")
    head = ingest_procedure(store, key="deploy", content=LONG)
    _outs(store, "a", [head.chunk_id], 5, 0, tokens=100, split="holdout")
    _outs(store, "b", [head.chunk_id], 0, 5, tokens=100, split="train")
    x = _cand(store, P_A)
    y = _cand(store, P_B)
    _outs(store, "xt", [x], 5, 0, split="train")       # train-only win
    _outs(store, "xh", [x], 0, 5, split="holdout")
    _outs(store, "yh", [y], 5, 0, split="holdout")     # holdout win
    _outs(store, "yt", [y], 0, 2, split="train")
    auto = compare_candidates(store, key="deploy")
    assert [c.chunk_id for c in auto.candidates] == [y, x]
    assert all(c.split_used == "holdout" for c in auto.candidates)
    assert auto.anchor_split == "holdout"
    pooled = compare_candidates(store, key="deploy", split="all")
    assert {c.split_used for c in pooled.candidates} == {"all"}
    # holdout mode without evidence cannot pass the gate.
    store2 = SQLiteStore(tmp_path / "s2.db")
    h2 = ingest_procedure(store2, key="deploy", content=LONG)
    _outs(store2, "a", [h2.chunk_id], 5, 0, tokens=100)
    c2 = _cand(store2, P_A)
    _outs(store2, "c", [c2], 5, 0)
    res = compare_candidates(store2, key="deploy", split="holdout")
    assert res.candidates[0].would_pass_gate is False
    assert "insufficient holdout evidence" in res.candidates[0].reasons
    assert compare_candidates(store2, key="deploy").candidates[0].would_pass_gate is True


def test_tiebreak_tokens_and_validation(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "s.db")
    head = ingest_procedure(store, key="deploy", content=LONG)
    _outs(store, "a", [head.chunk_id], 5, 0, tokens=100)
    heavy = _cand(store, P_A)
    light = _cand(store, P_B)
    _outs(store, "h", [heavy], 5, 0, tokens=80)
    _outs(store, "l", [light], 5, 0, tokens=40)
    assert [c.chunk_id for c in compare_candidates(store, key="deploy").candidates] == [light, heavy]
    with pytest.raises(RefineError, match="gate_split"):
        compare_candidates(store, key="deploy", split="bogus")
    with pytest.raises(ValueError, match="no procedure"):
        compare_candidates(store, key="missing")


@pytest.fixture()
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    for name in ("NCP_AUTH_TOKEN", "NCP_HOST", "NCP_PORT"):
        monkeypatch.delenv(name, raising=False)
    assert CliRunner().invoke(main, ["init", "--cwd", str(tmp_path), "--store", "sqlite"]).exit_code == 0
    return tmp_path


def test_cli_table_and_json(project: Path) -> None:
    ids = _setup(create_store(load_config(cwd=project)))
    table = CliRunner().invoke(main, ["refine", "compare", "deploy", "--cwd", str(project)])
    assert table.exit_code == 0, table.output
    assert f"Recommended: {ids['x']}" in table.output
    assert f"ncp refine apply {ids['x']}" in table.output
    assert table.output.index(ids["x"]) < table.output.index(ids["y"]) < table.output.index(ids["z"])
    assert "stale" in table.output

    raw = CliRunner().invoke(main, ["refine", "compare", "deploy", "--json", "--gate-split", "all", "--cwd", str(project)])
    assert raw.exit_code == 0, raw.output
    data = json.loads(raw.output)
    assert data["recommended"] == ids["x"] and data["apply_command"] == f"ncp refine apply {ids['x']}"
    assert [c["chunk_id"] for c in data["candidates"]] == [ids["x"], ids["y"], ids["z"]]
    assert data["candidates"][0]["would_pass_gate"] is True and data["candidates"][2]["status"] == "stale"


def test_cli_no_candidates_and_unknown_key(project: Path) -> None:
    store = create_store(load_config(cwd=project))
    ingest_procedure(store, key="deploy", content=LONG)
    empty = CliRunner().invoke(main, ["refine", "compare", "deploy", "--cwd", str(project)])
    assert empty.exit_code == 0 and "No proposed candidates" in empty.output
    bad = CliRunner().invoke(main, ["refine", "compare", "nope", "--cwd", str(project)])
    assert bad.exit_code != 0 and "no procedure" in bad.output
