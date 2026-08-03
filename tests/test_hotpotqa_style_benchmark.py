from __future__ import annotations

from pathlib import Path

import pytest

from benchmarks.hotpotqa_style.run import run_hotpotqa_style
from benchmarks.hotpotqa_style.tasks import TASKS, get_tasks, score_context


def test_tasks_have_expected_shape() -> None:
    assert len(TASKS) >= 12
    ids = [task.task_id for task in TASKS]
    assert len(set(ids)) == len(ids)
    kinds = {task.kind for task in TASKS}
    assert kinds == {"bridge", "comparison"}
    for task in TASKS:
        assert len(task.gold_paragraphs) == 2
        assert len(task.must_include) >= 2
        assert task.question
        assert task.answer


def test_score_context_requires_all_facts() -> None:
    task = TASKS[0]
    full_context = "\n".join(p.content for p in task.paragraphs)
    success, missing = score_context(full_context, task)
    assert success is True
    assert missing == []

    partial_context = task.gold_paragraphs[0].content
    success, missing = score_context(partial_context, task)
    assert success is False
    assert missing


def test_get_tasks_limit() -> None:
    assert len(get_tasks(4)) == 4
    assert len(get_tasks()) == len(TASKS)
    with pytest.raises(ValueError):
        get_tasks(0)


def test_run_hotpotqa_style_artifact_schema(tmp_path: Path) -> None:
    artifact = run_hotpotqa_style(
        budget=300,
        k=6,
        n_tasks=4,
        pipeline_id="pipe_hotpotqa_style_test",
        store_dir=tmp_path,
    )

    assert artifact["benchmark"] == "hotpotqa_style"
    assert artifact["budget"] == 300
    assert artifact["n_tasks"] == 4
    assert "token_unit" in artifact
    # Honesty requirement: claim must not assert parity with PlugMem's own numbers.
    assert "context adequacy" in artifact["claim"]
    assert "PlugMem" in artifact["claim"] or "official HotpotQA" in artifact["claim"]

    rows = artifact["rows"]
    assert rows

    conditions = {"ncp", "sliding_window", "raw_replay"}
    by_task: dict[str, set[str]] = {}
    for row in rows:
        for key in ("task_id", "kind", "condition", "context_tokens", "success", "missing"):
            assert key in row
        by_task.setdefault(row["task_id"], set()).add(row["condition"])

    assert len(by_task) == 4
    for task_id, seen_conditions in by_task.items():
        assert seen_conditions == conditions, (task_id, seen_conditions)

    summary = artifact["summary"]
    assert "by_condition" in summary
    assert "by_kind_and_condition" in summary
    for condition in conditions:
        cond_summary = summary["by_condition"][condition]
        assert 0.0 <= cond_summary["success_rate"] <= 1.0
        assert cond_summary["n"] == 4


def test_run_hotpotqa_style_matched_budget_beats_sliding_window() -> None:
    """At the documented default budget, ncp recall must not trail sliding_window."""

    artifact = run_hotpotqa_style(budget=300, k=6)
    summary = artifact["summary"]
    assert summary["pass"] is True
    ncp_rate = summary["by_condition"]["ncp"]["success_rate"]
    sliding_rate = summary["by_condition"]["sliding_window"]["success_rate"]
    assert ncp_rate >= sliding_rate


def test_run_hotpotqa_style_invalid_budget() -> None:
    with pytest.raises(ValueError, match="budget must be >= 1"):
        run_hotpotqa_style(budget=0, n_tasks=2)


def test_run_hotpotqa_style_invalid_k() -> None:
    with pytest.raises(ValueError, match="k must be >= 1"):
        run_hotpotqa_style(k=0, n_tasks=2)
