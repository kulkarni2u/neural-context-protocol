"""Live-Postgres coverage for the v1.9.7 improvement-record SQL (pgvector).

Skipped unless ``NCP_RUN_PGVECTOR_INTEGRATION=1`` (the CI pgvector job sets it).
"""

from __future__ import annotations

import importlib.util
import os
import time
from uuid import uuid4

import pytest

from ncp.stores.improvement_common import make_problem_id, new_attempt_id
from ncp.types import ImprovementAttempt, ImprovementProblem, OutcomeRecord

pytestmark = pytest.mark.skipif(
    os.getenv("NCP_RUN_PGVECTOR_INTEGRATION") != "1",
    reason="set NCP_RUN_PGVECTOR_INTEGRATION=1 to run live pgvector integration tests",
)


@pytest.fixture()
def store():  # type: ignore[no-untyped-def]
    if importlib.util.find_spec("psycopg") is None:
        pytest.skip("psycopg is not installed; install neural-context-protocol[pgvector] first")
    from ncp.stores.pgvector import PgvectorStore

    schema = f"ncp_imp_{uuid4().hex[:8]}"
    return PgvectorStore(
        os.getenv("NCP_PGVECTOR_DSN", "postgresql://postgres:postgres@127.0.0.1:5432/ncp"),
        schema=schema,
        table_prefix="it_",
        redis_url=os.getenv("NCP_REDIS_URL", "redis://127.0.0.1:6379/0"),
        redis_stream=f"ncp:it:{schema}:whispers",
    )


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


def _out(i: int, chunks: list[str], success: bool, **kw: object) -> OutcomeRecord:
    return OutcomeRecord(outcome_id=f"o{i}", chunk_ids=chunks, success=success, **kw)  # type: ignore[arg-type]


def test_outcome_stats_model_and_exclude(store) -> None:  # type: ignore[no-untyped-def]
    store.record_outcome(_out(1, ["a"], True, model="m1", task="t1", output_tokens=10))
    store.record_outcome(_out(2, ["a", "b"], False, model="m2", task="t2", output_tokens=30))
    store.record_outcome(_out(3, ["b"], True, task="t3"))
    assert store.outcome_stats(chunk_ids=[]).n == 0
    allm = store.outcome_stats(chunk_ids=["a"])
    assert allm.n == 2 and allm.success_rate == 0.5 and allm.mean_output_tokens == 20
    m1 = store.outcome_stats(chunk_ids=["a"], model="m1")
    assert m1.n == 1 and m1.success_rate == 1.0 and m1.tasks_succeeded == ["t1"]
    assert store.outcome_stats(chunk_ids=["a", "b"]).n == 3
    assert store.outcome_stats(chunk_ids=["a"], exclude_chunk_ids=["b"]).n == 1
    assert store.outcome_stats(chunk_ids=["a", "b"], exclude_chunk_ids=["b"]).n == 1
    assert store.outcome_stats(chunk_ids=["a"], model="m2", exclude_chunk_ids=["b"]).n == 0


def test_has_success_outcome(store) -> None:  # type: ignore[no-untyped-def]
    store.record_outcome(_out(1, ["a"], True, task="fix", slot="s", pipeline_id="p"))
    store.record_outcome(_out(2, ["a"], False, task="other", slot="s", pipeline_id="p"))
    assert store.has_success_outcome(task="fix") is True
    assert store.has_success_outcome(task="fix", slot="s", pipeline_id="p") is True
    assert store.has_success_outcome(task="fix", pipeline_id="q") is False
    assert store.has_success_outcome(task="other") is False


def test_problem_upsert_is_idempotent_and_lists(store) -> None:  # type: ignore[no-untyped-def]
    first = store.upsert_improvement_problem(_problem())
    again = store.upsert_improvement_problem(_problem())
    assert first.problem_id == again.problem_id
    store.upsert_improvement_problem(_problem(pipeline="q"))
    assert [p.problem_id for p in store.list_improvement_problems(pipeline_id="p")] == [first.problem_id]
    assert store.get_improvement_problem("prob_nope") is None


def test_merge_alias_and_cross_pipeline_refused(store) -> None:  # type: ignore[no-untyped-def]
    src = store.upsert_improvement_problem(_problem("a"))
    dst = store.upsert_improvement_problem(_problem("b"))
    att = store.record_improvement_attempt(_attempt(src.problem_id))
    store.merge_improvement_problems(src_problem_id=src.problem_id, dst_problem_id=dst.problem_id)
    resolved = store.get_improvement_problem(src.problem_id)
    assert resolved is not None and resolved.problem_id == dst.problem_id
    assert [a.attempt_id for a in store.list_improvement_attempts(problem_id=dst.problem_id)] == [att.attempt_id]
    other = store.upsert_improvement_problem(_problem("c", pipeline="q"))
    with pytest.raises(ValueError, match="across pipelines"):
        store.merge_improvement_problems(src_problem_id=dst.problem_id, dst_problem_id=other.problem_id)
    with pytest.raises(ValueError, match="unknown improvement problem"):
        store.merge_improvement_problems(src_problem_id="prob_nope", dst_problem_id=dst.problem_id)


def test_attempts_record_get_update_list(store) -> None:  # type: ignore[no-untyped-def]
    prob = store.upsert_improvement_problem(_problem())
    with pytest.raises(ValueError):
        store.record_improvement_attempt(_attempt("prob_missing"))
    att = store.record_improvement_attempt(
        _attempt(prob.problem_id, candidate_chunk_id="cand", predicted=["t1"], kind="prune")
    )
    got = store.get_improvement_attempt(att.attempt_id)
    assert got is not None and got.kind == "prune" and got.predicted == ["t1"]
    assert store.get_improvement_attempt("att_nope") is None
    updated = store.update_improvement_attempt(
        att.attempt_id, status="applied", accepted=True, fixed=["t1"], passed=3.0, anchor_rate=0.9
    )
    assert updated.status == "applied" and updated.accepted and updated.fixed == ["t1"]
    assert updated.passed == 3.0 and updated.anchor_rate == 0.9
    assert [a.attempt_id for a in store.list_improvement_attempts(candidate_chunk_id="cand")] == [att.attempt_id]
    with pytest.raises(ValueError):
        store.update_improvement_attempt("att_missing", status="applied")
    with pytest.raises(ValueError):
        store.update_improvement_attempt(att.attempt_id, status="bogus")
