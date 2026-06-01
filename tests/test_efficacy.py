from __future__ import annotations
import shutil
import pytest

CLAUDE_AVAILABLE = shutil.which("claude") is not None
CODEX_AVAILABLE = shutil.which("codex") is not None
OPENCODE_AVAILABLE = shutil.which("opencode") is not None


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


def test_efficacy_artifact_uses_honest_control_contract():
    from benchmarks.efficacy.run import run_efficacy

    artifact = run_efficacy(
        continuation_adapter="local",
        budget=600,
        attempts=1,
        pipeline_id="pipe_efficacy_contract",
    )

    assert artifact["benchmark"] == "window_control_efficacy"
    assert artifact["comparison_contract"] == "ncp_vs_fixed_sliding_window_control"


@pytest.mark.skipif(not CLAUDE_AVAILABLE, reason="claude CLI not on PATH")
def test_efficacy_claude_smoke(tmp_path):
    from benchmarks.efficacy.run import run_efficacy
    artifact = run_efficacy(
        continuation_adapter="claude-cli",
        budget=600,
        attempts=1,
        adapter_timeout_seconds=30.0,
        pipeline_id="pipe_efficacy_claude_smoke",
    )
    assert artifact["benchmark"] == "window_control_efficacy"
    assert artifact["provider"] == "claude-cli"
    assert "ncp" in artifact
    assert "sliding_window" in artifact
    assert 0.0 <= artifact["ncp"]["summary"]["success_rate"] <= 1.0


@pytest.mark.skipif(not OPENCODE_AVAILABLE, reason="opencode CLI not on PATH")
def test_efficacy_opencode_smoke(tmp_path):
    from benchmarks.efficacy.run import run_efficacy
    artifact = run_efficacy(
        continuation_adapter="opencode-cli",
        budget=600,
        attempts=1,
        adapter_timeout_seconds=20.0,
        pipeline_id="pipe_efficacy_opencode_smoke",
    )
    assert artifact["benchmark"] == "window_control_efficacy"
    assert 0.0 <= artifact["ncp"]["summary"]["success_rate"] <= 1.0


@pytest.mark.skipif(not CODEX_AVAILABLE, reason="codex CLI not on PATH")
def test_efficacy_codex_smoke(tmp_path):
    from benchmarks.efficacy.run import run_efficacy
    artifact = run_efficacy(
        continuation_adapter="codex-cli",
        budget=600,
        attempts=1,
        adapter_timeout_seconds=25.0,
        pipeline_id="pipe_efficacy_codex_smoke",
    )
    assert artifact["benchmark"] == "window_control_efficacy"
    assert 0.0 <= artifact["ncp"]["summary"]["success_rate"] <= 1.0
