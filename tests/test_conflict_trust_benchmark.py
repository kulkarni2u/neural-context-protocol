"""Conflicting / low-trust memory benchmark: structure, determinism and the CI gate."""

from __future__ import annotations

from pathlib import Path

import pytest

from benchmarks.conflict_trust.run import ARMS, FACTS, METRICS, RESOLVABLE, SCENARIOS, conflict_trust


@pytest.fixture(scope="module")
def artifact(tmp_path_factory: pytest.TempPathFactory) -> dict:
    return conflict_trust(workdir=tmp_path_factory.mktemp("conflict"), k=1)


def test_artifact_shape(artifact: dict) -> None:
    assert artifact["benchmark"] == "conflict_trust"
    assert artifact["tasks"] == len(SCENARIOS) * len(FACTS)
    by_scenario = artifact["summary"]["by_scenario"]
    assert set(by_scenario) == set(SCENARIOS)
    for rates in by_scenario.values():
        assert set(rates) == set(ARMS)
        for metrics in rates.values():
            assert set(metrics) == set(METRICS)
            assert all(0.0 <= value <= 1.0 for value in metrics.values())


def test_every_claim_in_a_task_differs_only_by_value() -> None:
    # The lexical signal must not be able to tell the claims apart.
    for fact in FACTS:
        assert fact.true_value not in fact.wrong_values
        assert len(set(fact.wrong_values)) == len(fact.wrong_values) >= 4
        assert fact.statement.count("{value}") == 1
        assert fact.true_value.lower() not in fact.question.lower().split()


def test_control_scenario_is_solved_by_bounded_retrievers(artifact: dict) -> None:
    control = artifact["summary"]["by_scenario"]["no_conflict"]
    assert control["ncp"]["clean"] == 1.0
    assert control["plain_rag"]["clean"] == 1.0
    assert artifact["summary"]["control_all_bounded_arms_find_answer"] is True


def test_raw_replay_always_contains_the_answer_and_the_conflict(artifact: dict) -> None:
    for scenario in RESOLVABLE:
        replay = artifact["summary"]["by_scenario"][scenario]["raw_replay"]
        assert replay["answer_present"] == 1.0
        assert replay["conflict_present"] == 1.0
        assert replay["clean"] == 0.0


def test_ncp_beats_plain_rag_where_the_store_holds_a_trust_signal(artifact: dict) -> None:
    summary = artifact["summary"]
    assert summary["ncp_beats_plain_rag_on_resolvable"] is True
    assert summary["ncp_top1_minus_plain_rag_top1"] >= 0.4


def test_store_never_drops_the_true_fact_at_write(artifact: dict) -> None:
    # A contradicting claim is the same sentence with a different value. The
    # write-time near-duplicate filter used to discard some of them (including
    # an explicit supersedes correction); it must not.
    assert artifact["summary"]["tasks_where_ncp_dropped_the_true_fact_at_write"] == 0
    assert artifact["summary"]["resolvable"]["ncp"]["top1_correct"] >= 0.95


def test_misplaced_trust_is_reported_as_a_loss_not_hidden(artifact: dict) -> None:
    # Trust weighting follows trust, not truth: when the wrong claim is the
    # trusted one, NCP surfaces it. The benchmark must keep showing that.
    misplaced = artifact["summary"]["by_scenario"]["misplaced_trust"]
    assert misplaced["ncp"]["top1_correct"] < 0.5


def test_deterministic_for_a_seed(tmp_path: Path) -> None:
    first = conflict_trust(workdir=tmp_path / "a", k=1, scenarios=("low_trust_distractor", "superseded"))
    second = conflict_trust(workdir=tmp_path / "b", k=1, scenarios=("low_trust_distractor", "superseded"))
    assert first["summary"] == second["summary"]


def test_rejects_bad_arguments(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        conflict_trust(workdir=tmp_path, k=0)
    with pytest.raises(ValueError):
        conflict_trust(workdir=tmp_path, scenarios=("nope",))
