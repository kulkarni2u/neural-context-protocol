from pathlib import Path

from ncp.coherence import CoherenceChecker
from ncp.stores.sqlite import SQLiteStore
from ncp.types import ConsciousBlock


def _make_conscious(**overrides: object) -> ConsciousBlock:
    base = {
        "agent_id": "executor",
        "role": "build",
        "owns": ["implementation"],
        "must_not": ["planning"],
        "task": "implement_store",
        "slot": "assemble_context",
        "intent": "build_local_dogfood",
        "pipeline_id": "pipe_1",
    }
    base.update(overrides)
    return ConsciousBlock(**base)


def test_coherence_passes_healthy_agent() -> None:
    checker = CoherenceChecker()
    report = checker.check(_make_conscious(slot_age=1, slot_confidence=0.9, drift_score=0.05))
    assert report.passed
    assert len(report.alerts) == 0


def test_coherence_emits_alert_on_slot_stale() -> None:
    checker = CoherenceChecker()
    report = checker.check(_make_conscious(slot_age=6, slot_confidence=0.4))
    assert not report.passed
    assert any(a.whisper_type == "alert" and "slot_confidence_low" in a.payload for a in report.alerts)


def test_coherence_emits_alert_on_high_drift() -> None:
    checker = CoherenceChecker()
    report = checker.check(_make_conscious(drift_score=0.35))
    assert not report.passed
    assert any(a.whisper_type == "alert" and "drift_score_high" in a.payload for a in report.alerts)


def test_coherence_goal_version_mismatch_via_store(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "store.db")
    conscious = _make_conscious(agent_id="executor", goal_version=1)
    other = _make_conscious(agent_id="planner", goal_version=2)

    from ncp.types import NCPResponse

    for agent in [conscious, other]:
        store.log_conscious(
            agent.model_copy(update={"recent": []}),
            snapshot_hash=f"hash_{agent.agent_id}",
        )
        store.log_cost(
            agent_id=agent.agent_id,
            response=NCPResponse(
                content="ok",
                input_tokens=1,
                output_tokens=1,
                cost_usd=0.0,
                model="test",
                turn_id=f"turn_{agent.agent_id}",
                latency_ms=0,
                pipeline_id="pipe_1",
            ),
        )

    checker = CoherenceChecker(store=store)
    report = checker.check(conscious)
    goal_alerts = [a for a in report.alerts if "goal_version_mismatch" in a.payload]
    assert len(goal_alerts) >= 1
    assert "v2" in goal_alerts[0].payload


def test_coherence_goal_version_match_passes() -> None:
    checker = CoherenceChecker()
    report = checker.check(_make_conscious(agent_id="executor", goal_version=1))
    goal_alerts = [a for a in report.alerts if "goal_version" in a.payload]
    assert len(goal_alerts) == 0
