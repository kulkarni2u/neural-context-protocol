"""Tests for the harness-agnostic decision loop example (examples/12)."""

from __future__ import annotations

import importlib.util
import socket
import types
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]


def _load_example() -> types.ModuleType:
    path = REPO_ROOT / "examples" / "12_decision_loop.py"
    spec = importlib.util.spec_from_file_location("ncp_example_12_decision_loop", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_example_runs_and_reuses_an_unchanged_state() -> None:
    outcome = _load_example().main()

    # Round 2 repeats round 1's state exactly, so it must reuse rather than
    # re-decide. If this ever regresses to [] the reuse path is dead again.
    assert outcome["reused_rounds"] == [2]
    # Three decisions for four rounds: the reused round records nothing new.
    assert outcome["decisions_recorded"] == 3
    assert outcome["provider_calls"] == 0


def test_example_makes_no_network_calls(monkeypatch: pytest.MonkeyPatch) -> None:
    module = _load_example()

    def _no_sockets(*args: object, **kwargs: object) -> None:
        raise AssertionError("the decision loop example opened a network connection")

    monkeypatch.setattr(socket, "socket", _no_sockets)
    monkeypatch.setattr(socket, "create_connection", _no_sockets)
    assert module.main()["provider_calls"] == 0


def test_backends_are_interchangeable() -> None:
    """The whole claim: NCP cannot tell a rule from a human from a model."""
    module = _load_example()
    state = {"evidence": [{"chunk_id": "c1", "content": "all checks passed"}]}
    questions = [{"key": "choice", "type": "enum", "options": ["continue", "escalate", "stop"]}]

    assert module.rule_backend(state, questions) == "continue"
    assert module.human_backend(state, questions) == "continue"
    with pytest.raises(NotImplementedError):
        module.model_backend(state, questions)


def test_rule_backend_escalates_without_evidence() -> None:
    module = _load_example()
    assert module.rule_backend({"evidence": []}, [{"key": "choice", "type": "enum"}]) == "escalate"
