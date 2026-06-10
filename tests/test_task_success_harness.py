from __future__ import annotations

from pathlib import Path

import pytest

from benchmarks.task_success.run import run_task_success
from benchmarks.task_success.tasks import (
    TASKS,
    get_tasks,
    mentions_dead_end_as_retry,
    score_response,
)


def test_tasks_have_expected_shape() -> None:
    assert len(TASKS) >= 12
    slugs = [task.approved_path for task in TASKS]
    assert len(set(slugs)) == len(slugs)
    for task in TASKS:
        assert 10 <= len(task.turns) <= 16
        assert 1 <= len(task.dead_ends) <= 3
        assert task.question
        assert task.query_text


def test_score_response_honors_negation() -> None:
    task = TASKS[0]
    approved = task.approved_path
    dead_end = task.dead_ends[0]

    assert score_response(f"I will use {approved} and avoid {dead_end}", task)[0] is True
    assert score_response(
        f"I will use {approved} and will not use {dead_end} because it was rejected",
        task,
    )[0] is True

    success, failure_type = score_response(f"let's try {dead_end} instead", task)
    assert success is False

    success, failure_type = score_response("no approved path found in context", task)
    assert success is False
    assert failure_type == "missing_approved_path"


def test_mentions_dead_end_as_retry_negation_window() -> None:
    dead_end = "some_dead_end_slug"
    assert mentions_dead_end_as_retry(f"let's retry {dead_end} now", dead_end) is True
    assert (
        mentions_dead_end_as_retry(f"we will not use {dead_end} again", dead_end)
        is False
    )


def test_get_tasks_limit() -> None:
    assert len(get_tasks(4)) == 4
    assert len(get_tasks()) == len(TASKS)
    with pytest.raises(ValueError):
        get_tasks(0)


def test_run_task_success_mock_artifact_schema(tmp_path: Path) -> None:
    artifact = run_task_success(
        budget=400,
        provider="mock",
        n_tasks=4,
        pipeline_id="pipe_task_success_test",
        store_dir=tmp_path,
    )

    assert artifact["benchmark"] == "task_success"
    assert artifact["provider"] == "mock"
    assert artifact["budget"] == 400
    assert artifact["n_tasks"] == 4
    assert "token_unit" in artifact
    assert "claim" in artifact
    # Honesty requirement: mock claim must not assert model task success.
    assert "model reasoning" in artifact["claim"] or "context adequacy" in artifact["claim"]

    rows = artifact["rows"]
    assert rows

    conditions = {"ncp", "sliding_window", "raw_replay"}
    by_task: dict[str, set[str]] = {}
    for row in rows:
        for key in (
            "task_id",
            "condition",
            "context_tokens",
            "success",
            "response_excerpt",
        ):
            assert key in row
        by_task.setdefault(row["task_id"], set()).add(row["condition"])

    assert len(by_task) == 4
    for task_id, seen_conditions in by_task.items():
        assert seen_conditions == conditions, (task_id, seen_conditions)

    summary = artifact["summary"]
    assert "by_condition" in summary
    for condition in conditions:
        cond_summary = summary["by_condition"][condition]
        assert 0.0 <= cond_summary["success_rate"] <= 1.0
        assert cond_summary["n"] == 4

    # Mock-mode pass gate must be True for the harness to be useful in CI.
    assert summary["pass"] is True


def test_run_task_success_invalid_budget() -> None:
    with pytest.raises(ValueError, match="budget must be >= 1"):
        run_task_success(budget=0, provider="mock", n_tasks=2)
