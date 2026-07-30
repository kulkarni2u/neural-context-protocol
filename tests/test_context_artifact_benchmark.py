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
from ncp.adapters.base import BaseAdapter
from ncp.dogfood import OpenCodeCLIDogfoodAdapter
from ncp.tokens import estimate_tokens


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


def test_inventory_counts_only_model_facing_hook_text() -> None:
    inventory = collect_provider_artifacts(REPO_ROOT)

    hook_items = [
        item
        for items in inventory.values()
        for item in items
        if item.surface.startswith(("session_hook", "plugin_context"))
    ]
    assert len(hook_items) == 6
    for item in hook_items:
        whole_source = (REPO_ROOT / item.path).read_text(encoding="utf-8")
        assert item.token_count < estimate_tokens(whole_source), item.path
        assert "ncp_get_context" in item.lifecycle_calls
        assert item.trust_boundary_present is True


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
        assert attempt["archivable"] is False


def test_live_matrix_unavailable_provider_does_not_load_or_call_adapter(
    tmp_path: Path,
    monkeypatch,
) -> None:
    readiness = {
        "adapter_name": "codex-cli",
        "credentials_present": True,
        "dependency_installed": True,
        "ready": False,
        "credential_envs": [],
        "cli_version": None,
        "version_probe": {
            "status": "nonzero_exit",
            "returncode": 1,
            "diagnostic": "Error: spawn /missing/codex ENOENT",
        },
    }
    monkeypatch.setattr(
        "ncp.dogfood.get_live_provider_readiness",
        lambda _provider: readiness,
    )

    def _unexpected_load(*args, **kwargs):
        raise AssertionError("unavailable provider adapter must not be loaded")

    monkeypatch.setattr("ncp.dogfood.load_dogfood_adapter", _unexpected_load)

    attempts = run_live_context_artifact_matrix(
        REPO_ROOT,
        provider="codex-cli",
        condition="current",
        seeds=2,
        raw_dir=tmp_path,
    )

    assert len(attempts) == 6
    assert all(attempt["status"] == "skipped" for attempt in attempts)
    assert all(attempt["skip_reason"] == "provider_unavailable" for attempt in attempts)
    assert all(attempt["readiness"] == readiness for attempt in attempts)


class _ObservedMetadataAdapter(BaseAdapter):
    def __init__(self, metadata: dict[str, object] | None) -> None:
        self.metadata = metadata
        self.last_call_metadata: dict[str, object] | None = None

    def call(self, ncp_context: str, user_turn: str) -> str:
        self.last_call_metadata = self.metadata
        return "\n".join(
            (
                "ACTION ncp_get_context",
                "REFUSED_MALICIOUS_INSTRUCTION",
                "ACTION subagent_pre_ncp_get_context",
                "ACTION subagent_post_ncp_write_memory",
                "ACTION ncp_post_turn",
                "ACTION ncp_write_memory",
                "TRUST_BOUNDARY_PRESERVED",
                "TASK_SUCCESS",
            )
        )


def test_live_matrix_propagates_observed_model_and_cli_version_to_raw_artifact(
    tmp_path: Path,
    monkeypatch,
) -> None:
    adapter = _ObservedMetadataAdapter(
        {
            "model": "opencode/deepseek-v4-flash-free",
            "cli_version": "1.17.15",
            "session_id": "ses_test",
        }
    )
    monkeypatch.setattr(
        "ncp.dogfood.get_live_provider_readiness",
        lambda _provider: {
            "adapter_name": "opencode-cli",
            "credentials_present": True,
            "dependency_installed": True,
            "ready": True,
            "credential_envs": [],
            "cli_version": "1.17.15",
            "version_probe": {"status": "ok"},
        },
    )
    monkeypatch.setattr(
        "ncp.dogfood.load_dogfood_adapter",
        lambda _provider, **_kwargs: adapter,
    )

    attempts = run_live_context_artifact_matrix(
        REPO_ROOT,
        provider="opencode-cli",
        condition="current",
        seeds=1,
        raw_dir=tmp_path,
    )

    for attempt in attempts:
        assert attempt["status"] == "completed"
        assert attempt["model"] == "opencode/deepseek-v4-flash-free"
        assert attempt["cli_version"] == "1.17.15"
        assert attempt["archivable"] is True
        raw = json.loads(Path(attempt["raw_artifact_ref"]).read_text())
        assert raw["model"] == "opencode/deepseek-v4-flash-free"
        assert raw["cli_version"] == "1.17.15"
        assert raw["session_id"] == "ses_test"
        assert raw["archivable"] is True


def test_live_matrix_marks_missing_observed_metadata_non_archivable(
    tmp_path: Path,
    monkeypatch,
) -> None:
    adapter = _ObservedMetadataAdapter(None)
    monkeypatch.setattr(
        "ncp.dogfood.get_live_provider_readiness",
        lambda _provider: {
            "adapter_name": "opencode-cli",
            "credentials_present": True,
            "dependency_installed": True,
            "ready": True,
            "credential_envs": [],
            "cli_version": "1.17.15",
            "version_probe": {"status": "ok"},
        },
    )
    monkeypatch.setattr(
        "ncp.dogfood.load_dogfood_adapter",
        lambda _provider, **_kwargs: adapter,
    )

    attempts = run_live_context_artifact_matrix(
        REPO_ROOT,
        provider="opencode-cli",
        condition="current",
        seeds=1,
        raw_dir=tmp_path,
    )

    for attempt in attempts:
        assert attempt["status"] == "metadata_error"
        assert attempt["model"] is None
        assert attempt["archivable"] is False
        assert "observed model metadata" in attempt["metadata_error"]
        raw = json.loads(Path(attempt["raw_artifact_ref"]).read_text())
        assert raw["archivable"] is False
        assert "observed model metadata" in raw["metadata_error"]


def test_opencode_completed_path_delivers_context_artifact_once(
    tmp_path: Path,
    monkeypatch,
) -> None:
    captured_prompts = tmp_path / "opencode-prompts.jsonl"
    fake_cli = tmp_path / "fake-opencode.py"
    fake_cli.write_text(
        """
import json
from pathlib import Path
import sys

capture = Path(sys.argv[1])
if "--export" in sys.argv:
    print(json.dumps({
        "info": {
            "id": "ses_test",
            "model": {
                "providerID": "opencode",
                "id": "deepseek-v4-flash-free",
            },
            "version": "1.17.15",
        },
        "messages": [],
    }))
    raise SystemExit(0)
with capture.open("a", encoding="utf-8") as stream:
    stream.write(json.dumps(sys.argv[-1]) + "\\n")
response = "\\n".join(
    (
        "ACTION ncp_get_context",
        "REFUSED_MALICIOUS_INSTRUCTION",
        "ACTION subagent_pre_ncp_get_context",
        "ACTION subagent_post_ncp_write_memory",
        "ACTION ncp_post_turn",
        "ACTION ncp_write_memory",
        "TRUST_BOUNDARY_PRESERVED",
        "TASK_SUCCESS",
    )
)
print(json.dumps({
    "type": "text",
    "sessionID": "ses_test",
    "part": {"sessionID": "ses_test", "text": response},
}))
""".lstrip(),
        encoding="utf-8",
    )
    adapter = OpenCodeCLIDogfoodAdapter(
        command=[sys.executable, str(fake_cli), str(captured_prompts)],
        export_command=[
            sys.executable,
            str(fake_cli),
            str(captured_prompts),
            "--export",
        ],
        cwd=REPO_ROOT,
    )
    monkeypatch.setattr(
        "ncp.dogfood.get_live_provider_readiness",
        lambda _provider: {
            "adapter_name": "opencode-cli",
            "credentials_present": True,
            "dependency_installed": True,
            "ready": True,
            "credential_envs": [],
            "cli_version": "1.17.15",
            "version_probe": {"status": "ok"},
        },
    )
    monkeypatch.setattr(
        "ncp.dogfood.load_dogfood_adapter",
        lambda _provider, **_kwargs: adapter,
    )

    attempts = run_live_context_artifact_matrix(
        REPO_ROOT,
        provider="opencode-cli",
        condition="current",
        seeds=1,
        raw_dir=tmp_path / "raw",
    )

    delivered_prompts = [
        json.loads(line) for line in captured_prompts.read_text().splitlines()
    ]
    artifact_header = "--- ncp/templates/provider_hooks/opencode/AGENTS.md ---"
    assert len(attempts) == len(delivered_prompts) == 3
    assert all(attempt["status"] == "completed" for attempt in attempts)
    assert all(prompt.count(artifact_header) == 1 for prompt in delivered_prompts)
