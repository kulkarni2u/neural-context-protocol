from __future__ import annotations

from pathlib import Path
import subprocess

import ncp
import pytest
from ncp.dogfood import (
    ClaudeCLIDogfoodAdapter,
    CodexCLIDogfoodAdapter,
    OpenCodeCLIDogfoodAdapter,
    _build_provider_continuation_turn,
    _build_provider_fetch_contract_turn,
    _extract_opencode_text,
    get_live_provider_readiness,
    load_dogfood_adapter,
    run_adapter_continuation_dogfood_loop,
    run_canonical_dogfood_loop,
    run_canonical_http_dogfood_loop,
    run_live_adapter_continuation_attempt,
    run_repeatability_dogfood_loop,
)

REPO_ROOT = Path(__file__).resolve().parents[1]


def test_canonical_dogfood_loop_runs_against_real_stdio_server(tmp_path: Path) -> None:
    project = tmp_path / "repo"
    (project / ".git").mkdir(parents=True)

    artifact = run_canonical_dogfood_loop(
        store_path=project / ".ncp" / "store.db",
        cwd=REPO_ROOT,
        pipeline_id="pipe_test_dogfood",
    )

    assert artifact["transport"] == "stdio_mcp"
    assert artifact["provider_roles"] == {
        "planner": "claude",
        "executor": "opencode",
        "critic": "codex",
    }
    assert artifact["restart_persistence_ok"] is True
    assert "ncp_fetch:results" in str(artifact["first_pass"]["fetch_result"])
    assert "continued_after_fetch" in str(artifact["first_pass"]["continued_response"])
    assert "dogfood contract persists across restart" in str(artifact["first_pass"]["continued_response"])
    assert "dogfood contract persists across restart" in str(artifact["restart_pass"]["fetch_result"])
    assert artifact["summary"]["first_fetch_ok"] is True
    assert artifact["summary"]["continuation_ok"] is True


def test_public_package_exports_dogfood_runner() -> None:
    assert callable(ncp.run_canonical_dogfood_loop)
    assert callable(ncp.run_canonical_http_dogfood_loop)


def test_canonical_http_dogfood_loop_runs_against_real_http_server(tmp_path: Path) -> None:
    project = tmp_path / "repo"
    (project / ".git").mkdir(parents=True)

    artifact = run_canonical_http_dogfood_loop(
        store_path=project / ".ncp" / "store.db",
        cwd=REPO_ROOT,
        pipeline_id="pipe_test_http_dogfood",
    )

    assert artifact["transport"] == "http_sse_mcp"
    assert artifact["provider_roles"] == {
        "planner": "claude",
        "executor": "opencode",
        "critic": "codex",
    }
    assert artifact["restart_persistence_ok"] is True
    assert "event: endpoint" in str(artifact["sse_handshake"])
    assert "/mcp" in str(artifact["sse_handshake"])
    assert "ncp_fetch:results" in str(artifact["first_pass"]["fetch_result"])
    assert artifact["summary"]["continuation_ok"] is True


def test_adapter_continuation_loop_runs_with_local_contract_adapter(tmp_path: Path) -> None:
    project = tmp_path / "repo"
    (project / ".git").mkdir(parents=True)

    artifact = run_adapter_continuation_dogfood_loop(
        adapter=load_dogfood_adapter("local"),
        store_path=project / ".ncp" / "store.db",
        cwd=REPO_ROOT,
        pipeline_id="pipe_test_adapter_dogfood",
    )

    assert artifact["mode"] == "adapter_continuation"
    assert artifact["adapter"] == "DogfoodLocalAdapter"
    assert "NCP_FETCH_REQUEST" in str(artifact["first_pass"]["first_provider_response"])
    assert "NCP_FINAL" in str(artifact["first_pass"]["second_provider_response"])
    assert artifact["continuation_ok"] is True


def test_http_adapter_continuation_loop_runs_with_local_contract_adapter(tmp_path: Path) -> None:
    project = tmp_path / "repo"
    (project / ".git").mkdir(parents=True)

    artifact = run_adapter_continuation_dogfood_loop(
        adapter=load_dogfood_adapter("local"),
        store_path=project / ".ncp" / "store.db",
        cwd=REPO_ROOT,
        pipeline_id="pipe_test_http_adapter_dogfood",
        transport="http",
    )

    assert artifact["transport"] == "http_sse_mcp"
    assert artifact["mode"] == "adapter_continuation"
    assert artifact["adapter"] == "DogfoodLocalAdapter"
    assert artifact["continuation_ok"] is True


def test_live_provider_readiness_reports_missing_credentials(monkeypatch: object) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    readiness = get_live_provider_readiness("anthropic")

    assert readiness["adapter_name"] == "anthropic"
    assert readiness["dependency_installed"] is True
    assert readiness["credentials_present"] is False
    assert readiness["ready"] is False


def test_live_provider_attempt_returns_honest_missing_credentials_artifact(
    monkeypatch: object,
    tmp_path: Path,
) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    project = tmp_path / "repo"
    (project / ".git").mkdir(parents=True)

    artifact = run_live_adapter_continuation_attempt(
        "anthropic",
        store_path=project / ".ncp" / "store.db",
        cwd=REPO_ROOT,
        pipeline_id="pipe_test_live_missing",
    )

    assert artifact["mode"] == "live_adapter_attempt"
    assert artifact["adapter_name"] == "anthropic"
    assert artifact["attempted"] is False
    assert artifact["status"] == "missing_credentials"


def test_live_provider_attempt_preserves_live_mode_on_success(
    monkeypatch: object,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(
        "ncp.dogfood.get_live_provider_readiness",
        lambda name: {
            "adapter_name": name,
            "credentials_present": True,
            "dependency_installed": True,
            "ready": True,
            "credential_envs": [],
        },
    )
    monkeypatch.setattr("ncp.dogfood.load_dogfood_adapter", lambda name, **kwargs: object())
    monkeypatch.setattr(
        "ncp.dogfood.run_adapter_continuation_dogfood_loop",
        lambda **kwargs: {
            "mode": "adapter_continuation",
            "adapter": "FakeAdapter",
            "continuation_ok": True,
        },
    )
    project = tmp_path / "repo"
    (project / ".git").mkdir(parents=True)

    artifact = run_live_adapter_continuation_attempt(
        "opencode-cli",
        store_path=project / ".ncp" / "store.db",
        cwd=REPO_ROOT,
        pipeline_id="pipe_test_live_success",
    )

    assert artifact["mode"] == "live_adapter_attempt"
    assert artifact["attempted"] is True
    assert artifact["status"] == "success"
    assert artifact["continuation_ok"] is True


def test_repeatability_runner_aggregates_live_attempts(monkeypatch: object, tmp_path: Path) -> None:
    attempts = iter([
        {"attempted": True, "status": "success", "continuation_ok": True},
        {
            "attempted": True,
            "status": "error",
            "error_type": "TimeoutExpired",
            "error_message": "timed out",
        },
        {"attempted": True, "status": "success", "continuation_ok": True},
    ])
    monkeypatch.setattr(
        "ncp.dogfood.run_live_adapter_continuation_attempt",
        lambda *args, **kwargs: next(attempts),
    )
    project = tmp_path / "repo"
    (project / ".git").mkdir(parents=True)

    artifact = run_repeatability_dogfood_loop(
        "opencode-cli",
        store_path=project / ".ncp" / "store.db",
        attempts=3,
        cwd=REPO_ROOT,
        pipeline_id="pipe_test_repeatability",
        adapter_timeout_seconds=18.0,
    )

    assert artifact["mode"] == "repeatability_run"
    assert artifact["adapter_name"] == "opencode-cli"
    assert artifact["adapter_timeout_seconds"] == 18.0
    assert len(artifact["attempts_detail"]) == 3
    assert artifact["attempts_detail"][1]["status"] == "error"
    assert artifact["attempts_detail"][1]["error_type"] == "TimeoutExpired"
    assert artifact["summary"]["completed_attempts"] == 3
    assert artifact["summary"]["successes"] == 2
    assert artifact["summary"]["errors"] == 1
    assert artifact["summary"]["continuation_successes"] == 2
    assert artifact["summary"]["stable"] is False


def test_repeatability_runner_short_circuits_missing_credentials(monkeypatch: object, tmp_path: Path) -> None:
    monkeypatch.setattr(
        "ncp.dogfood.run_live_adapter_continuation_attempt",
        lambda *args, **kwargs: {
            "attempted": False,
            "status": "missing_credentials",
            "readiness": {"credentials_present": False},
        },
    )
    project = tmp_path / "repo"
    (project / ".git").mkdir(parents=True)

    artifact = run_repeatability_dogfood_loop(
        "anthropic",
        store_path=project / ".ncp" / "store.db",
        attempts=5,
        cwd=REPO_ROOT,
        pipeline_id="pipe_test_repeatability_missing",
    )

    assert artifact["short_circuit_reason"] == "missing_credentials"
    assert len(artifact["attempts_detail"]) == 1
    assert artifact["summary"]["completed_attempts"] == 1
    assert artifact["summary"]["missing_credentials"] == 1


def test_cli_adapter_readiness_uses_binary_presence(monkeypatch: object) -> None:
    monkeypatch.setattr("ncp.dogfood.shutil.which", lambda name: "/usr/bin/fake" if name == "opencode" else None)
    readiness = get_live_provider_readiness("opencode-cli")

    assert readiness["adapter_name"] == "opencode-cli"
    assert readiness["dependency_installed"] is True
    assert readiness["credentials_present"] is True
    assert readiness["ready"] is True


def test_extract_opencode_text_uses_last_text_event() -> None:
    output = "\n".join([
        json_line({"type": "step_start", "part": {"id": "a"}}),
        json_line({"type": "text", "part": {"text": "NCP_FETCH_REQUEST\nquery:first"}}),
        json_line({"type": "text", "part": {"text": "NCP_FINAL\ncontent:done"}}),
    ])

    assert _extract_opencode_text(output) == "NCP_FINAL\ncontent:done"


def test_claude_cli_adapter_returns_stdout_text(tmp_path: Path) -> None:
    adapter = ClaudeCLIDogfoodAdapter(command=["python3", "-c", "print('NCP_FINAL\\ncontent:done')"], cwd=tmp_path)
    result = adapter.call("ctx", "turn")
    assert result == "NCP_FINAL\ncontent:done"


def test_claude_cli_adapter_default_command_adds_repo_dir(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    def _fake_run(command, **kwargs):
        captured["command"] = command
        captured["cwd"] = kwargs.get("cwd")
        return subprocess.CompletedProcess(command, 0, stdout="NCP_FINAL\ncontent:done", stderr="")

    monkeypatch.setattr("ncp.dogfood.subprocess.run", _fake_run)
    adapter = ClaudeCLIDogfoodAdapter(cwd=tmp_path)

    result = adapter.call("ctx", "turn")

    assert result == "NCP_FINAL\ncontent:done"
    command = captured["command"]
    assert isinstance(command, list)
    assert "--add-dir" in command
    assert command[command.index("--add-dir") + 1] == str(tmp_path)
    assert captured["cwd"] == tmp_path


def test_codex_cli_adapter_reads_output_last_message_file(tmp_path: Path) -> None:
    script = (
        "import pathlib, sys; "
        "args = sys.argv[1:]; "
        "out = pathlib.Path(args[args.index('-o') + 1]); "
        "prompt = args[-1]; "
        "out.write_text(prompt)"
    )
    adapter = CodexCLIDogfoodAdapter(
        command=["python3", "-c", script],
        cwd=tmp_path,
    )
    result = adapter.call("ctx", "NCP_FINAL\ncontent:done")
    assert result == "NCP_FINAL\ncontent:done"


def test_opencode_cli_adapter_parses_json_events(tmp_path: Path) -> None:
    payload = json_line({"type": "text", "part": {"text": "NCP_FINAL\ncontent:done"}})
    adapter = OpenCodeCLIDogfoodAdapter(
        command=["python3", "-c", f"print({payload!r})"],
        cwd=tmp_path,
    )
    result = adapter.call("ctx", "turn")
    assert result == "NCP_FINAL\ncontent:done"


def test_opencode_cli_adapter_default_command_sets_dir(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = json_line({"type": "text", "part": {"text": "NCP_FINAL\ncontent:done"}})
    captured: dict[str, object] = {}

    def _fake_run(command, **kwargs):
        captured["command"] = command
        captured["cwd"] = kwargs.get("cwd")
        return subprocess.CompletedProcess(command, 0, stdout=payload, stderr="")

    monkeypatch.setattr("ncp.dogfood.subprocess.run", _fake_run)
    adapter = OpenCodeCLIDogfoodAdapter(cwd=tmp_path)

    result = adapter.call("ctx", "turn")

    assert result == "NCP_FINAL\ncontent:done"
    command = captured["command"]
    assert isinstance(command, list)
    assert "--dir" in command
    assert command[command.index("--dir") + 1] == str(tmp_path)
    assert captured["cwd"] == tmp_path


def test_claude_provider_prompt_is_tightened() -> None:
    adapter = ClaudeCLIDogfoodAdapter(command=["true"])

    fetch_prompt = _build_provider_fetch_contract_turn(adapter, "turn")
    continuation_prompt = _build_provider_continuation_turn(
        adapter,
        "turn",
        "ncp_fetch:results k:1 | dogfood contract persists across restart",
    )

    assert "query:dogfood restart contract" in fetch_prompt
    assert "TURN:" not in fetch_prompt
    assert "content:dogfood contract persists across restart" in continuation_prompt
    assert "TURN:" not in continuation_prompt


def test_codex_provider_prompts_are_tightened() -> None:
    adapter = CodexCLIDogfoodAdapter(command=["true"])

    fetch_prompt = _build_provider_fetch_contract_turn(adapter, "turn")
    continuation_prompt = _build_provider_continuation_turn(
        adapter,
        "turn",
        "ncp_fetch:results k:1 | dogfood contract persists across restart",
    )

    assert "query:dogfood restart contract" in fetch_prompt
    assert "TURN:" not in fetch_prompt
    assert "content:dogfood contract persists across restart" in continuation_prompt
    assert "TURN:" not in continuation_prompt


def test_opencode_provider_continuation_prompt_is_tightened() -> None:
    adapter = OpenCodeCLIDogfoodAdapter(command=["true"])

    continuation_prompt = _build_provider_continuation_turn(
        adapter,
        "turn",
        "ncp_fetch:results k:1 | dogfood contract persists across restart",
    )

    assert "Return only this exact contract" in continuation_prompt
    assert "content:dogfood contract persists across restart" in continuation_prompt
    assert "TURN:" not in continuation_prompt


def test_codex_cli_readiness_uses_binary_presence(monkeypatch: object) -> None:
    monkeypatch.setattr("ncp.dogfood.shutil.which", lambda name: "/usr/bin/fake" if name == "codex" else None)
    readiness = get_live_provider_readiness("codex-cli")

    assert readiness["adapter_name"] == "codex-cli"
    assert readiness["dependency_installed"] is True
    assert readiness["credentials_present"] is True
    assert readiness["ready"] is True


def json_line(payload: dict[str, object]) -> str:
    import json

    return json.dumps(payload)
