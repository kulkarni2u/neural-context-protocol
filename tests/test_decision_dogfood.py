"""The decision workflow dogfood loop: the gate that proves the feature works.

D01-D12 assert that the code matches the spec. This asserts that the loop a
host would actually run produces usable numbers -- that escalate fires when it
should and stays quiet when it should not, that a recorded decision comes back
as a reuse candidate, and that state_hash survives the decisions written along
the way.
"""

from __future__ import annotations

import socket
from pathlib import Path

import pytest

from ncp.dogfood import DECISION_WORKFLOW_SLOTS, run_decision_workflow_dogfood_loop


@pytest.fixture(scope="module")
def artifact() -> dict:
    return run_decision_workflow_dogfood_loop(turns=12)


def test_workflow_records_every_turn(artifact: dict) -> None:
    assert artifact["turns"] == 12
    assert artifact["decisions_recorded"] == 12


def test_type_error_rate_is_zero(artifact: dict) -> None:
    """A type error on a registered schema is a protocol bug, not a tuning knob."""
    assert artifact["type_error_rate"] == 0.0
    assert artifact["summary"]["type_errors"] == 0


def test_state_hash_is_stable_across_the_whole_run(artifact: dict) -> None:
    """Regression guard: ncp_record_decision writes a trace chunk every turn.

    Letting reasoning_trace back into compile's evidence made each compile see
    a different evidence set than the last, which moved state_hash and killed
    precedent reuse outright. This is the assertion that caught it.
    """
    assert artifact["state_hash_stable"] is True
    assert artifact["unstable_slots"] == []


def test_precedent_reuse_actually_hits(artifact: dict) -> None:
    """The ncp_lookup_memo lesson: a reuse key nobody measured never fired.

    The first pass over N slots cannot hit -- there is no precedent yet -- so
    the ceiling for 12 turns over 4 slots is 8/12. Anything near zero means the
    reuse path is dead again.
    """
    slots = len(DECISION_WORKFLOW_SLOTS)
    ceiling = (12 - slots) / 12
    assert artifact["precedent_hit_rate"] == pytest.approx(ceiling, abs=0.01)


def test_clean_workflow_does_not_escalate(artifact: dict) -> None:
    """Escalate must be a signal, not a constant.

    Fresh tool_result evidence sits at trust 0.80, well clear of the 0.55
    floor. If a healthy workflow escalates, hosts learn to ignore the flag.
    """
    assert artifact["escalate_rate"] == 0.0
    assert artifact["escalate_reasons"] == {}
    assert artifact["joint_confidence_mean"] >= artifact["escalate_min_confidence"]


def test_degraded_workflow_escalates_with_reasons(artifact: dict) -> None:
    """And the other direction: a dead escalate path must not read as healthy."""
    assert artifact["degraded_escalate_rate"] == 1.0
    reasons = artifact["degraded_escalate_reasons"]
    for expected in ("high_drift", "critical_budget", "open_schema", "low_joint_confidence"):
        assert reasons.get(expected) == len(DECISION_WORKFLOW_SLOTS)


def test_loop_makes_no_provider_calls(monkeypatch: pytest.MonkeyPatch) -> None:
    def _no_sockets(*args: object, **kwargs: object) -> None:
        raise AssertionError("the decision dogfood loop opened a network connection")

    monkeypatch.setattr(socket, "socket", _no_sockets)
    monkeypatch.setattr(socket, "create_connection", _no_sockets)
    result = run_decision_workflow_dogfood_loop(turns=4)
    assert result["provider_calls"] == 0
    assert result["summary"]["pass"] is True


def test_loop_accepts_an_explicit_store_path(tmp_path: Path) -> None:
    result = run_decision_workflow_dogfood_loop(
        store_path=tmp_path / "decisions.db", cwd=tmp_path, turns=4
    )
    assert result["summary"]["pass"] is True
    assert (tmp_path / "decisions.db").exists()


def test_summary_passes(artifact: dict) -> None:
    assert artifact["summary"]["pass"] is True
