"""R1/R2 store layer: improvement records and outcome context (sqlite)."""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path

import pytest

from ncp.stores.base import BaseStore
from ncp.stores.improvement_common import make_problem_id, normalize_problem_name, new_attempt_id
from ncp.stores.sqlite import SQLiteStore
from ncp.types import ImprovementAttempt, ImprovementProblem, OutcomeRecord


@pytest.fixture
def store(tmp_path: Path) -> SQLiteStore:
    return SQLiteStore(tmp_path / "ncp.db")


def _problem(name: str = "Slow retries", scope: str = "proc/a", pipeline: str | None = "p") -> ImprovementProblem:
    return ImprovementProblem(
        problem_id=make_problem_id(pipeline_id=pipeline, scope=scope, name=name),
        pipeline_id=pipeline, scope=scope, name=name, opened_at=time.time(),
    )


def _attempt(problem_id: str, **kw: object) -> ImprovementAttempt:
    base: dict = {
        "attempt_id": new_attempt_id(), "problem_id": problem_id, "kind": "repair",
        "what": "change", "created_at": time.time(),
    }
    base.update(kw)
    return ImprovementAttempt(**base)


def test_problem_id_is_deterministic_and_normalized() -> None:
    a = make_problem_id(pipeline_id="p", scope="s", name="  Slow   RETRIES ")
    b = make_problem_id(pipeline_id="p", scope="s", name="slow retries")
    assert a == b and a.startswith("prob_") and len(a) == len("prob_") + 16
    assert make_problem_id(pipeline_id="q", scope="s", name="slow retries") != a
    assert normalize_problem_name("  A  b ") == "a b"


def test_upsert_returns_existing_row(store: SQLiteStore) -> None:
    first = store.upsert_improvement_problem(_problem())
    later = _problem()
    later = later.model_copy(update={"name": "different casing", "opened_at": first.opened_at + 100})
    again = store.upsert_improvement_problem(later)
    assert again.problem_id == first.problem_id
    assert again.name == first.name and again.opened_at == first.opened_at
    assert len(store.list_improvement_problems()) == 1


def test_merge_aliases_and_keeps_attempts(store: SQLiteStore) -> None:
    src = store.upsert_improvement_problem(_problem("one"))
    dst = store.upsert_improvement_problem(_problem("two"))
    a1 = store.record_improvement_attempt(_attempt(src.problem_id))
    a2 = store.record_improvement_attempt(_attempt(dst.problem_id))
    store.merge_improvement_problems(src_problem_id=src.problem_id, dst_problem_id=dst.problem_id)
    assert store.get_improvement_problem(src.problem_id).problem_id == dst.problem_id  # type: ignore[union-attr]
    assert [p.problem_id for p in store.list_improvement_problems()] == [dst.problem_id]
    ids = {a.attempt_id for a in store.list_improvement_attempts(problem_id=dst.problem_id)}
    assert ids == {a1.attempt_id, a2.attempt_id}
    # Listing through the alias resolves to the survivor; nothing was deleted.
    assert len(store.list_improvement_attempts(problem_id=src.problem_id)) == 2
    # Upserting the aliased id returns the survivor.
    assert store.upsert_improvement_problem(_problem("one")).problem_id == dst.problem_id
    # Recording against an alias lands on the survivor.
    a3 = store.record_improvement_attempt(_attempt(src.problem_id))
    assert a3.problem_id == dst.problem_id
    store.merge_improvement_problems(src_problem_id=src.problem_id, dst_problem_id=dst.problem_id)  # no-op
    assert len(store.list_improvement_attempts()) == 3


def test_merge_unknown_problem_raises(store: SQLiteStore) -> None:
    p = store.upsert_improvement_problem(_problem())
    with pytest.raises(ValueError):
        store.merge_improvement_problems(src_problem_id="prob_nope", dst_problem_id=p.problem_id)


def test_list_problems_filters(store: SQLiteStore) -> None:
    store.upsert_improvement_problem(_problem("a", scope="s1", pipeline="p"))
    store.upsert_improvement_problem(_problem("b", scope="s2", pipeline="p"))
    store.upsert_improvement_problem(_problem("c", scope="s1", pipeline="q"))
    assert len(store.list_improvement_problems(pipeline_id="p")) == 2
    assert len(store.list_improvement_problems(scope="s1")) == 2
    assert len(store.list_improvement_problems(pipeline_id="p", scope="s1")) == 1
    assert len(store.list_improvement_problems(limit=2)) == 2


def test_attempt_record_update_list(store: SQLiteStore) -> None:
    prob = store.upsert_improvement_problem(_problem())
    att = store.record_improvement_attempt(
        _attempt(prob.problem_id, predicted=["t1", "t2"], files=["AGENTS.md"], candidate_chunk_id="c1",
                 created_at=1000.0)
    )
    second = store.record_improvement_attempt(_attempt(prob.problem_id, kind="prune", created_at=2000.0))
    updated = store.update_improvement_attempt(
        att.attempt_id, status="applied", accepted=True, fixed=["t1"], missed=["t2"],
        passed=3.0, total=4.0, mean_output_tokens=120.5, resolved_at=3000.0, note="ok",
    )
    assert updated.status == "applied" and updated.accepted is True
    assert updated.fixed == ["t1"] and updated.missed == ["t2"] and updated.predicted == ["t1", "t2"]
    assert (updated.passed, updated.total, updated.mean_output_tokens) == (3.0, 4.0, 120.5)
    assert updated.files == ["AGENTS.md"] and updated.resolved_at == 3000.0
    ordered = store.list_improvement_attempts(problem_id=prob.problem_id)
    assert [a.attempt_id for a in ordered] == [att.attempt_id, second.attempt_id]
    by_candidate = store.list_improvement_attempts(candidate_chunk_id="c1")
    assert [a.attempt_id for a in by_candidate] == [att.attempt_id]


def test_update_rejects_unknown_fields_and_ids(store: SQLiteStore) -> None:
    prob = store.upsert_improvement_problem(_problem())
    att = store.record_improvement_attempt(_attempt(prob.problem_id))
    for bad in ("problem_id", "kind", "created_at", "bogus"):
        with pytest.raises(ValueError):
            store.update_improvement_attempt(att.attempt_id, **{bad: "x"})
    with pytest.raises(ValueError):
        store.update_improvement_attempt(att.attempt_id, status="bogus")
    with pytest.raises(ValueError):
        store.update_improvement_attempt("att_missing", status="applied")


def test_record_attempt_requires_problem(store: SQLiteStore) -> None:
    with pytest.raises(ValueError):
        store.record_improvement_attempt(_attempt("prob_missing"))


def _out(i: int, chunks: list[str], success: bool, **kw: object) -> OutcomeRecord:
    return OutcomeRecord(outcome_id=f"o{i}", chunk_ids=chunks, success=success, **kw)  # type: ignore[arg-type]


def test_outcome_stats_basic(store: SQLiteStore) -> None:
    store.record_outcome(_out(1, ["c1"], True, weight=1.0, task="t1", output_tokens=100, turns=2))
    store.record_outcome(_out(2, ["c1", "c2"], False, weight=0.5, task="t2", output_tokens=200, turns=4))
    store.record_outcome(_out(3, ["c2"], True, weight=1.0, task="t3"))
    store.record_outcome(_out(4, ["other"], True, weight=1.0, task="t9"))
    s = store.outcome_stats(chunk_ids=["c1"])
    assert s.n == 2 and s.total_weight == pytest.approx(1.5) and s.success_weight == pytest.approx(1.0)
    assert s.success_rate == pytest.approx(1.0 / 1.5)
    assert s.mean_output_tokens == pytest.approx(150.0) and s.mean_turns == pytest.approx(3.0)
    assert s.tasks_succeeded == ["t1"] and s.tasks_failed == ["t2"]
    both = store.outcome_stats(chunk_ids=["c1", "c2"])
    assert both.n == 3 and both.tasks_succeeded == ["t1", "t3"]


def test_outcome_stats_empty_and_no_match(store: SQLiteStore) -> None:
    for s in (store.outcome_stats(chunk_ids=[]), store.outcome_stats(chunk_ids=["nope"])):
        assert s.n == 0 and s.total_weight == 0.0 and s.success_rate is None
        assert s.mean_output_tokens is None and s.mean_turns is None
        assert s.tasks_succeeded == [] and s.tasks_failed == []


def test_outcome_stats_model_filter(store: SQLiteStore) -> None:
    store.record_outcome(_out(1, ["c"], True, model="m1", task="a"))
    store.record_outcome(_out(2, ["c"], False, model="m2", task="b"))
    store.record_outcome(_out(3, ["c"], True, task="c"))  # no model
    assert store.outcome_stats(chunk_ids=["c"]).n == 3
    m1 = store.outcome_stats(chunk_ids=["c"], model="m1")
    assert m1.n == 1 and m1.success_rate == 1.0 and m1.tasks_succeeded == ["a"]
    m2 = store.outcome_stats(chunk_ids=["c"], model="m2")
    assert m2.n == 1 and m2.success_rate == 0.0 and m2.tasks_failed == ["b"]
    assert store.outcome_stats(chunk_ids=["c"], model="zzz").n == 0


def test_outcome_stats_not_truncated_over_2000(store: SQLiteStore) -> None:
    with store._connect() as conn:
        conn.executemany(
            "INSERT INTO outcomes (outcome_id, chunk_ids, success, weight, created_at, consumed)"
            " VALUES (?, ?, ?, 1.0, ?, 1)",
            [(f"bulk{i}", '["big"]', 1 if i % 2 == 0 else 0, float(i)) for i in range(2600)],
        )
    s = store.outcome_stats(chunk_ids=["big"])
    assert s.n == 2600 and s.total_weight == 2600.0 and s.success_weight == 1300.0
    assert s.success_rate == pytest.approx(0.5)


def test_has_success_outcome(store: SQLiteStore) -> None:
    assert store.has_success_outcome(task="t") is False
    store.record_outcome(_out(1, ["c"], False, task="t", slot="s", pipeline_id="p"))
    assert store.has_success_outcome(task="t", slot="s", pipeline_id="p") is False
    store.record_outcome(_out(2, ["c"], True, task="t", slot="s", pipeline_id="p"))
    assert store.has_success_outcome(task="t", slot="s", pipeline_id="p") is True
    assert store.has_success_outcome(task="t") is True
    assert store.has_success_outcome(task="t", slot="other") is False
    assert store.has_success_outcome(task="t", slot="s", pipeline_id="q") is False


def test_outcome_context_round_trip(store: SQLiteStore) -> None:
    rec = OutcomeRecord(
        outcome_id="ctx", chunk_ids=["c"], success=True, pipeline_id="p", task="t", slot="s",
        model="m", benchmark="b", eval_source="verifier", output_tokens=10, input_tokens=20, turns=3,
    )
    assert store.record_outcome(rec) is True
    for got in (store.get_outcome("ctx"), store.list_outcomes()[0]):
        assert got is not None
        assert (got.pipeline_id, got.task, got.slot, got.model, got.benchmark) == ("p", "t", "s", "m", "b")
        assert (got.eval_source, got.output_tokens, got.input_tokens, got.turns) == ("verifier", 10, 20, 3)
    with store._connect() as conn:
        loaded = store._load_unconsumed_outcomes(conn)
    assert loaded[0].eval_source == "verifier" and loaded[0].turns == 3


def test_outcome_defaults_and_validation() -> None:
    rec = OutcomeRecord(success=True)
    assert rec.eval_source == "self_report" and rec.model is None and rec.turns is None
    with pytest.raises(ValueError):
        OutcomeRecord(success=True, output_tokens=-1)
    with pytest.raises(ValueError):
        OutcomeRecord(success=True, eval_source="bogus")  # type: ignore[arg-type]


def test_old_database_is_upgraded_and_old_rows_read(tmp_path: Path) -> None:
    path = tmp_path / "old.db"
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE outcomes (
            outcome_id TEXT PRIMARY KEY, turn_id TEXT, chunk_ids TEXT NOT NULL DEFAULT '[]',
            success INTEGER NOT NULL, weight REAL NOT NULL DEFAULT 1.0, note TEXT,
            created_at REAL NOT NULL, consumed INTEGER NOT NULL DEFAULT 0
        );
        INSERT INTO outcomes VALUES ('old1', NULL, '["c"]', 1, 1.0, 'n', 5.0, 0);
        """
    )
    conn.commit()
    conn.close()
    store = SQLiteStore(path)
    old = store.get_outcome("old1")
    assert old is not None and old.eval_source == "self_report" and old.task is None
    assert store.record_outcome(OutcomeRecord(outcome_id="new1", chunk_ids=["c"], success=True, task="t"))
    assert store.outcome_stats(chunk_ids=["c"]).n == 2
    assert store.has_success_outcome(task="t") is True


def test_base_store_defaults() -> None:

    assert BaseStore.has_success_outcome(object(), task="t") is False  # type: ignore[arg-type]
    for name, kwargs in (
        ("get_improvement_problem", {"problem_id": "x"}),
        ("list_improvement_attempts", {}),
        ("outcome_stats", {"chunk_ids": ["c"]}),
    ):
        with pytest.raises(NotImplementedError, match="improvement records require"):
            getattr(BaseStore, name)(object(), **kwargs)


def test_outcome_stats_exclude_chunk_ids(store: SQLiteStore) -> None:
    store.record_outcome(OutcomeRecord(outcome_id="o1", chunk_ids=["a"], success=True))
    store.record_outcome(OutcomeRecord(outcome_id="o2", chunk_ids=["a", "b"], success=True))
    store.record_outcome(OutcomeRecord(outcome_id="o3", chunk_ids=["b"], success=False))
    assert store.outcome_stats(chunk_ids=["a"]).n == 2
    assert store.outcome_stats(chunk_ids=["a"], exclude_chunk_ids=["b"]).n == 1
    assert store.outcome_stats(chunk_ids=["a", "b"], exclude_chunk_ids=["b"]).n == 1
    assert store.outcome_stats(chunk_ids=["a"], exclude_chunk_ids=[]).n == 2


def test_merge_across_pipelines_refused(store: SQLiteStore) -> None:
    src = store.upsert_improvement_problem(_problem(pipeline="pa"))
    dst = store.upsert_improvement_problem(_problem(pipeline="pb"))
    with pytest.raises(ValueError, match="across pipelines"):
        store.merge_improvement_problems(src_problem_id=src.problem_id, dst_problem_id=dst.problem_id)
    got = store.get_improvement_problem(src.problem_id)
    assert got is not None and got.alias_of is None


def test_get_improvement_attempt(store: SQLiteStore) -> None:
    prob = store.upsert_improvement_problem(_problem())
    att = store.record_improvement_attempt(_attempt(prob.problem_id))
    got = store.get_improvement_attempt(att.attempt_id)
    assert got is not None and got.attempt_id == att.attempt_id
    assert store.get_improvement_attempt("att_nope") is None
