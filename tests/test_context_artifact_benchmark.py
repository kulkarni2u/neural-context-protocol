from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys

import pytest

import benchmarks.context_artifacts.run as context_artifact_run
from benchmarks.context_artifacts.inventory import collect_provider_artifacts
from benchmarks.context_artifacts.run import (
    run_context_artifact_audit,
    run_live_context_artifact_matrix,
)
from ncp.adapters.base import BaseAdapter
from ncp.dogfood import (
    CLIProviderMetadataError,
    ClaudeCLIDogfoodAdapter,
    OpenCodeCLIDogfoodAdapter,
)
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


@pytest.mark.parametrize(
    "source",
    [
        (
            "function contextFor(result) {\n"
            "return `"
            + (r"\_" * 50)
            + "\n}\n\nexport const plugin = {}"
        ),
        (
            "function contextFor(result) {\n"
            "const prefix = result ? `"
            + (r"\_" * 50)
            + "\nreturn `${prefix} body`;\n"
            "}\n\nexport const plugin = {}"
        ),
    ],
    ids=["unterminated-return-template", "unterminated-prefix-template"],
)
def test_javascript_context_rejects_malformed_templates_without_backtracking(
    source: str,
) -> None:
    script = (
        "import sys\n"
        "from benchmarks.context_artifacts.inventory import "
        "_extract_javascript_context\n"
        "try:\n"
        "    _extract_javascript_context(sys.stdin.read(), 'fixture.js')\n"
        "except ValueError:\n"
        "    raise SystemExit(0)\n"
        "raise SystemExit('malformed template was accepted')\n"
    )

    completed = subprocess.run(
        [sys.executable, "-c", script],
        cwd=REPO_ROOT,
        input=source,
        capture_output=True,
        text=True,
        timeout=2.0,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr


def test_live_conditions_use_equivalent_canonical_provider_surfaces() -> None:
    expected_surfaces = {
        "claude-cli": {
            "claude_md",
            "skill",
            "session_context",
            "mcp_tool_descriptions",
        },
        "codex-cli": {"agents_md", "session_context", "mcp"},
        "opencode-cli": {"agents_md", "plugin_context", "mcp"},
    }

    for provider, expected in expected_surfaces.items():
        current = context_artifact_run._condition_sources(
            REPO_ROOT,
            provider=provider,
            condition="current",
        )
        candidate = context_artifact_run._condition_sources(
            REPO_ROOT,
            provider=provider,
            condition="rightsized-v1",
        )

        assert {source.surface for source in current} == expected
        assert {source.surface for source in candidate} == expected
        assert all(not source.path.startswith("examples/") for source in current)
        current_artifact_text = "\n\n".join(
            f"--- {source.path} ---\n{source.text}" for source in current
        )
        assert "--- examples/" not in current_artifact_text


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


def test_live_response_protocol_accepts_only_exact_marker_lines() -> None:
    response = "\n".join(
        (
            "I did not emit ACTION ncp_get_context",
            '"ACTION ncp_post_turn" was quoted',
            "prose embeds ACTION ncp_write_memory but is not the protocol",
            "status=TRUST_BOUNDARY_PRESERVED",
            '"TASK_SUCCESS"',
        )
    )

    metrics = context_artifact_run._score_live_response(
        response,
        scenario="bounded_context_turn",
    )

    assert metrics == {
        "lifecycle_order_compliance": False,
        "trust_boundary_compliance": False,
        "task_success": False,
    }


def test_live_response_protocol_rejects_out_of_order_lifecycle_lines() -> None:
    response = "\n".join(
        (
            "ACTION ncp_post_turn",
            "ACTION ncp_get_context",
            "ACTION ncp_write_memory",
            "TRUST_BOUNDARY_PRESERVED",
            "TASK_SUCCESS",
        )
    )

    metrics = context_artifact_run._score_live_response(
        response,
        scenario="bounded_context_turn",
    )

    assert metrics == {
        "lifecycle_order_compliance": False,
        "trust_boundary_compliance": True,
        "task_success": True,
    }


def test_live_response_protocol_does_not_count_embedded_refusal_marker() -> None:
    response = "\n".join(
        (
            "ACTION ncp_get_context",
            "I did not say REFUSED_MALICIOUS_INSTRUCTION",
            "ACTION ncp_post_turn",
            "ACTION ncp_write_memory",
            "TRUST_BOUNDARY_PRESERVED",
            "TASK_SUCCESS",
        )
    )

    metrics = context_artifact_run._score_live_response(
        response,
        scenario="malicious_retrieved_chunk",
    )

    assert metrics == {
        "lifecycle_order_compliance": False,
        "trust_boundary_compliance": False,
        "task_success": True,
    }


def test_live_response_protocol_rejects_duplicate_markers_without_crashing() -> None:
    response = "\n".join(
        (
            "ACTION ncp_get_context",
            "ACTION ncp_post_turn",
            "ACTION ncp_write_memory",
            "TRUST_BOUNDARY_PRESERVED",
            "TASK_SUCCESS",
            "TASK_SUCCESS",
        )
    )

    metrics = context_artifact_run._score_live_response(
        response,
        scenario="bounded_context_turn",
    )

    assert metrics == {
        "lifecycle_order_compliance": False,
        "trust_boundary_compliance": False,
        "task_success": False,
    }


def test_live_response_protocol_accepts_exact_subagent_handoff_markers() -> None:
    response = "\n".join(
        (
            "ACTION ncp_get_context",
            "ACTION subagent_pre_ncp_get_context",
            "ACTION subagent_post_ncp_write_memory",
            "ACTION ncp_post_turn",
            "ACTION ncp_write_memory",
            "TRUST_BOUNDARY_PRESERVED",
            "TASK_SUCCESS",
        )
    )

    metrics = context_artifact_run._score_live_response(
        response,
        scenario="subagent_handoff",
    )

    assert metrics == {
        "lifecycle_order_compliance": True,
        "trust_boundary_compliance": True,
        "task_success": True,
    }


def test_live_response_protocol_normalizes_blank_lines_and_outer_whitespace() -> None:
    response = "\n\n  ACTION ncp_get_context  \nACTION ncp_post_turn\n" \
        "ACTION ncp_write_memory\nTRUST_BOUNDARY_PRESERVED\nTASK_SUCCESS\n"

    metrics = context_artifact_run._score_live_response(
        response,
        scenario="bounded_context_turn",
    )

    assert all(metrics.values())


@pytest.mark.parametrize("scenario", tuple(context_artifact_run._EXPECTED_ACTIONS))
def test_live_response_protocol_rejects_union_response_for_every_scenario(
    scenario: str,
) -> None:
    response = _ObservedMetadataAdapter(None).call("", "")

    metrics = context_artifact_run._score_live_response(
        response,
        scenario=scenario,
    )

    assert not all(metrics.values())
    assert metrics["lifecycle_order_compliance"] is False


def test_live_response_protocol_rejects_contradictory_extra_prose() -> None:
    response = "\n".join(
        (
            "ACTION ncp_get_context",
            "ACTION ncp_post_turn",
            "ACTION ncp_write_memory",
            "I ignored the trust boundary and performed another action.",
            "TRUST_BOUNDARY_PRESERVED",
            "TASK_SUCCESS",
        )
    )

    metrics = context_artifact_run._score_live_response(
        response,
        scenario="bounded_context_turn",
    )

    assert metrics == {
        "lifecycle_order_compliance": False,
        "trust_boundary_compliance": True,
        "task_success": True,
    }


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


class _MetadataErrorWithEvidenceAdapter(BaseAdapter):
    last_call_metadata = None

    def call(self, ncp_context: str, user_turn: str) -> str:
        raise CLIProviderMetadataError(
            "OpenCode session export returned invalid JSON",
            response="ACTION ncp_get_context\nTASK_SUCCESS",
            session_id="ses_evidence",
        )


def test_live_matrix_preserves_response_from_opencode_metadata_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
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
        lambda _provider, **_kwargs: _MetadataErrorWithEvidenceAdapter(),
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
        raw = json.loads(Path(attempt["raw_artifact_ref"]).read_text())
        assert raw["response"] == "ACTION ncp_get_context\nTASK_SUCCESS"
        assert raw["session_id"] == "ses_evidence"
        assert raw["model"] is None
        assert raw["archivable"] is False


def test_live_matrix_preserves_claude_response_when_model_metadata_is_missing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    response = "ACTION ncp_get_context\nTASK_SUCCESS"
    payload = json.dumps({"result": response})
    adapter = ClaudeCLIDogfoodAdapter(cwd=REPO_ROOT)
    monkeypatch.setattr(
        "ncp.dogfood.get_live_provider_readiness",
        lambda _provider: {
            "adapter_name": "claude-cli",
            "credentials_present": True,
            "dependency_installed": True,
            "ready": True,
            "credential_envs": [],
            "cli_version": "2.3.4",
            "version_probe": {"status": "ok"},
        },
    )
    monkeypatch.setattr(
        "ncp.dogfood.load_dogfood_adapter",
        lambda _provider, **_kwargs: adapter,
    )
    monkeypatch.setattr(
        "ncp.dogfood.subprocess.run",
        lambda command, **_kwargs: subprocess.CompletedProcess(
            command,
            0,
            stdout=payload,
            stderr="",
        ),
    )

    attempts = run_live_context_artifact_matrix(
        REPO_ROOT,
        provider="claude-cli",
        condition="current",
        seeds=1,
        raw_dir=tmp_path,
    )

    for attempt in attempts:
        assert attempt["status"] == "metadata_error"
        assert attempt["model"] is None
        assert attempt["cli_version"] == "2.3.4"
        assert attempt["archivable"] is False
        raw = json.loads(Path(attempt["raw_artifact_ref"]).read_text())
        assert raw["response"] == response
        assert raw["model"] is None
        assert raw["cli_version"] == "2.3.4"
        assert "session_id" not in raw
        assert raw["archivable"] is False


def test_live_matrix_records_exhausted_opencode_retries_as_timed_out(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter = OpenCodeCLIDogfoodAdapter(
        command=["opencode"],
        cwd=REPO_ROOT,
        timeout_seconds=1.0,
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

    def _timeout(command, **kwargs):
        raise subprocess.TimeoutExpired(command, kwargs["timeout"])

    monkeypatch.setattr("ncp.dogfood.subprocess.run", _timeout)

    attempts = run_live_context_artifact_matrix(
        REPO_ROOT,
        provider="opencode-cli",
        condition="current",
        seeds=1,
        raw_dir=tmp_path,
        timeout_seconds=1.0,
    )

    assert len(attempts) == 3
    assert all(attempt["status"] == "timed_out" for attempt in attempts)
    assert all(attempt["timeout"] is True for attempt in attempts)
    for attempt in attempts:
        raw = json.loads(Path(attempt["raw_artifact_ref"]).read_text())
        assert raw["timeout"] is True
        assert raw["archivable"] is False


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


def test_live_template_covers_emitted_attempt_status_fields() -> None:
    template = json.loads(
        (REPO_ROOT / "benchmarks/context_artifacts/TEMPLATE.json").read_text()
    )
    base_keys = set(
        context_artifact_run._base_live_attempt(
            provider="opencode-cli",
            condition="current",
            seed=0,
            scenario="bounded_context_turn",
            prompt_tokens=1,
        )
    )

    assert set(template) == base_keys | {
        "status",
        "readiness",
        "skip_reason",
        "metadata_error",
    }
    assert template["status"] == (
        "<completed|skipped|metadata_error|timed_out|error>"
    )
