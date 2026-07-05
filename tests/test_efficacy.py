from __future__ import annotations

import json

import pytest


def test_efficacy_scoring_honors_constraints():
    from benchmarks.efficacy.run import _APPROVED_PATH, _score_response
    approved = _APPROVED_PATH
    dead_end = "zenbrix_legacy_bridge"
    # Success: names the approved path, no dead-ends
    assert _score_response(f"I will use {approved} as instructed")[0] is True
    assert _score_response(f"the integration proceeds via {approved}")[0] is True
    assert _score_response(
        f"I will use {approved} and will not use {dead_end} because it was rejected."
    )[0] is True
    # Failure: dead-end retried
    assert _score_response(f"let us try {dead_end} for the integration")[0] is False
    assert _score_response("zenbrix_v2_mesh is available")[0] is False
    # Failure: approved path missing
    assert _score_response("use the recommended secure integration path")[0] is False


def test_efficacy_mock_artifact_uses_provider_real_contract(tmp_path):
    from benchmarks.efficacy.run import run_efficacy

    artifact_path = tmp_path / "efficacy.json"
    artifact = run_efficacy(
        provider="mock",
        budget=400,
        seeds=1,
        n_tasks=2,
        pipeline_id="pipe_efficacy_mock_contract",
        artifact_path=artifact_path,
    )

    assert artifact["benchmark"] == "provider_real_efficacy"
    assert artifact["provider"] == "mock"
    assert artifact["budget"] == 400
    assert artifact["seeds"] == 1
    assert artifact["n_tasks"] == 2
    assert artifact["conditions"] == ["ncp", "sliding_window", "rolling_summary"]
    assert artifact["config"]["ncp_retrieval_k"] == 8
    assert len(artifact["rows"]) == 6
    assert {row["model"] for row in artifact["rows"]} == {"mock"}
    assert {row["cost_source"] for row in artifact["rows"]} == {"not_applicable"}
    assert {row["cost_usd"] for row in artifact["rows"]} == {0.0}

    by_condition = artifact["summary"]["by_condition"]
    assert set(by_condition) == {"ncp", "sliding_window", "rolling_summary"}
    for summary in by_condition.values():
        assert 0.0 <= summary["success_rate"] <= 1.0
        assert summary["mean_tokens"] > 0
        assert summary["mean_cost_usd_per_task"] == 0.0
        assert summary["n"] == 2

    saved = json.loads(artifact_path.read_text())
    assert saved["benchmark"] == artifact["benchmark"]
    assert saved["summary"] == artifact["summary"]


def test_efficacy_anthropic_skips_without_api_key(monkeypatch, tmp_path):
    from benchmarks.efficacy.run import run_efficacy

    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    artifact_path = tmp_path / "anthropic-skip.json"
    artifact = run_efficacy(
        provider="anthropic",
        seeds=1,
        n_tasks=1,
        artifact_path=artifact_path,
    )

    assert artifact["benchmark"] == "provider_real_efficacy"
    assert artifact["provider"] == "anthropic"
    assert artifact["skipped"] is True
    assert "ANTHROPIC_API_KEY" in artifact["skip_reason"]
    assert json.loads(artifact_path.read_text())["skipped"] is True


def test_efficacy_ncp_context_contains_approved_path(tmp_path):
    from benchmarks.efficacy.run import _ncp_context
    from benchmarks.task_success.tasks import get_tasks

    task = get_tasks(1)[0]
    context, tokens = _ncp_context(
        task=task,
        budget=400,
        pipeline_id="pipe_efficacy_ncp_context",
        store_path=tmp_path / "ncp.db",
    )

    assert task.approved_path in context
    assert tokens <= 400


def test_efficacy_rolling_summary_obeys_budget():
    from benchmarks.efficacy.run import _rolling_summary_context
    from benchmarks.task_success.tasks import get_tasks
    from ncp.tokens import estimate_tokens

    task = get_tasks(1)[0]
    context, tokens = _rolling_summary_context(task, budget=120)

    assert tokens <= 120
    assert tokens == estimate_tokens(context)


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"provider": "openai"}, "provider must be"),
        ({"budget": 0}, "budget must be"),
        ({"seeds": 0}, "seeds must be"),
    ],
)
def test_efficacy_rejects_invalid_inputs(kwargs, message):
    from benchmarks.efficacy.run import run_efficacy

    with pytest.raises(ValueError, match=message):
        run_efficacy(**kwargs)
