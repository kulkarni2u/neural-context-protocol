from __future__ import annotations

import shutil
import pytest

CLAUDE_AVAILABLE = shutil.which("claude") is not None
OPENCODE_AVAILABLE = shutil.which("opencode") is not None


def test_crosshost_score_function():
    from benchmarks.efficacy.run import _APPROVED_PATH, _score_response

    # Approved path (fictional, only in NCP context) → success
    assert _score_response(f"I will use {_APPROVED_PATH}")[0] is True
    # Dead-end proposed → failure
    assert _score_response("zenbrix_legacy_bridge is a good option")[0] is False
    # Approved path absent → failure
    assert _score_response("use the standard integration path")[0] is False


@pytest.mark.skipif(
    not (CLAUDE_AVAILABLE and OPENCODE_AVAILABLE),
    reason="requires both claude and opencode CLIs on PATH",
)
def test_crosshost_smoke(tmp_path):
    from benchmarks.crosshost.run import run_crosshost

    artifact = run_crosshost(
        host_a_adapter="claude-cli",
        host_b_adapter="opencode-cli",
        budget=600,
        attempts=1,
        host_a_timeout_seconds=30.0,
        host_b_timeout_seconds=20.0,
        pipeline_id="pipe_crosshost_smoke",
    )
    assert artifact["benchmark"] == "cross_host_shared_context"
    assert artifact["restart_between_hosts"] is True
    assert 0.0 <= artifact["host_b_ncp"]["summary"]["success_rate"] <= 1.0
    assert 0.0 <= artifact["host_b_window"]["summary"]["success_rate"] <= 1.0
