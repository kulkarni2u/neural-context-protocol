from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys

from benchmarks.context_artifacts.inventory import collect_provider_artifacts
from benchmarks.context_artifacts.run import (
    run_context_artifact_audit,
    run_live_context_artifact_matrix,
)


REPO_ROOT = Path(__file__).resolve().parents[1]


def test_inventory_keeps_providers_separate() -> None:
    inventory = collect_provider_artifacts(REPO_ROOT)

    assert set(inventory) == {"claude", "codex", "opencode"}
    assert all(item.token_count > 0 for items in inventory.values() for item in items)


def test_inventory_counts_shared_tool_descriptions_once_per_provider() -> None:
    inventory = collect_provider_artifacts(REPO_ROOT)

    for provider, items in inventory.items():
        shared = [item for item in items if item.surface == "mcp_tool_descriptions"]
        assert len(shared) == 1, provider
        assert shared[0].provider == provider


def test_audit_reports_tokens_and_lifecycle_coverage_per_provider() -> None:
    artifact = run_context_artifact_audit(REPO_ROOT)

    assert artifact["benchmark"] == "provider_context_artifacts"
    assert set(artifact["providers"]) == {"claude", "codex", "opencode"}
    for row in artifact["providers"].values():
        assert {
            "artifact_tokens",
            "lifecycle_calls",
            "trust_boundary_present",
        } <= set(row)
        assert row["artifact_tokens"] > 0
        assert row["lifecycle_calls"]["ncp_get_context"] > 0
        assert row["lifecycle_calls"]["ncp_write_memory"] > 0
        assert row["trust_boundary_present"] is True


def test_candidate_comparison_keeps_provider_deltas_and_safety_gates_separate() -> None:
    artifact = run_context_artifact_audit(REPO_ROOT, candidate_name="rightsized-v1")

    comparison = artifact["comparison"]
    assert comparison["candidate"] == "rightsized-v1"
    assert set(comparison["providers"]) == {"claude", "codex", "opencode"}
    for row in comparison["providers"].values():
        assert row["delta"]["artifact_tokens"] < 0
        assert row["candidate"]["trust_boundary_present"] is True
        assert row["quality_gates"] == {
            "endpoint_liveness_present": True,
            "lifecycle_coverage_preserved": True,
            "trust_boundary_preserved": True,
        }
        assert row["assessment"] == "live_evaluation_required"


def test_cli_writes_the_same_deterministic_candidate_artifact(tmp_path: Path) -> None:
    output = tmp_path / "context-artifacts.json"
    command = [
        sys.executable,
        "benchmarks/context_artifacts/run.py",
        "--repo-root",
        ".",
        "--candidate",
        "rightsized-v1",
        "--output",
        str(output),
    ]

    completed = subprocess.run(
        command,
        cwd=REPO_ROOT,
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0, completed.stderr
    assert json.loads(output.read_text()) == run_context_artifact_audit(
        REPO_ROOT,
        candidate_name="rightsized-v1",
    )


def test_live_matrix_records_structured_skips_without_provider_substitution(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("PATH", "")

    attempts = run_live_context_artifact_matrix(
        REPO_ROOT,
        provider="claude-cli",
        condition="current",
        seeds=3,
        raw_dir=tmp_path,
    )

    assert len(attempts) == 9
    assert {attempt["provider"] for attempt in attempts} == {"claude-cli"}
    assert {attempt["scenario"] for attempt in attempts} == {
        "bounded_context_turn",
        "malicious_retrieved_chunk",
        "subagent_handoff",
    }
    for attempt in attempts:
        assert {
            "provider",
            "model",
            "condition",
            "seed",
            "prompt_tokens",
            "lifecycle_order_compliance",
            "trust_boundary_compliance",
            "task_success",
            "timeout",
            "raw_artifact_ref",
        } <= set(attempt)
        assert attempt["status"] == "skipped"
        assert attempt["skip_reason"] == "provider_unavailable"
        assert attempt["raw_artifact_ref"] is None
