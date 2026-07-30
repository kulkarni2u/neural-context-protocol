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
    monkeypatch.setattr(
        "ncp.dogfood.subprocess.run",
        lambda command, **kwargs: subprocess.CompletedProcess(
            command,
            0,
            stdout="1.17.15\n",
            stderr="",
        ),
    )
    readiness = get_live_provider_readiness("opencode-cli")

    assert readiness["adapter_name"] == "opencode-cli"
    assert readiness["dependency_installed"] is True
    assert readiness["credentials_present"] is True
    assert readiness["ready"] is True
    assert readiness["cli_version"] == "1.17.15"
    assert readiness["version_probe"] == {"status": "ok"}


def test_cli_adapter_readiness_rejects_nonzero_version_probe_and_sanitizes_stderr(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "ncp.dogfood.shutil.which",
        lambda name: "/usr/bin/codex" if name == "codex" else None,
    )
    monkeypatch.setattr(
        "ncp.dogfood.subprocess.run",
        lambda command, **kwargs: subprocess.CompletedProcess(
            command,
            1,
            stdout="",
            stderr=(
                "Error: spawn /missing/codex ENOENT\n"
                "token=super-secret-value"
            ),
        ),
    )

    readiness = get_live_provider_readiness("codex-cli")

    assert readiness["dependency_installed"] is True
    assert readiness["ready"] is False
    assert readiness["cli_version"] is None
    assert readiness["version_probe"]["status"] == "nonzero_exit"
    assert readiness["version_probe"]["returncode"] == 1
    assert "ENOENT" in readiness["version_probe"]["diagnostic"]
    assert "super-secret-value" not in readiness["version_probe"]["diagnostic"]


def test_cli_adapter_readiness_rejects_timed_out_version_probe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "ncp.dogfood.shutil.which",
        lambda name: "/usr/bin/claude" if name == "claude" else None,
    )

    def _time_out(command, **kwargs):
        raise subprocess.TimeoutExpired(command, kwargs["timeout"])

    monkeypatch.setattr("ncp.dogfood.subprocess.run", _time_out)

    readiness = get_live_provider_readiness("claude-cli")

    assert readiness["ready"] is False
    assert readiness["cli_version"] is None
    assert readiness["version_probe"]["status"] == "timed_out"
    assert readiness["version_probe"]["timeout_seconds"] == 5.0


def test_cli_adapter_readiness_rejects_empty_version(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "ncp.dogfood.shutil.which",
        lambda name: "/usr/bin/opencode" if name == "opencode" else None,
    )
    monkeypatch.setattr(
        "ncp.dogfood.subprocess.run",
        lambda command, **kwargs: subprocess.CompletedProcess(
            command,
            0,
            stdout=" \n",
            stderr="",
        ),
    )

    readiness = get_live_provider_readiness("opencode-cli")

    assert readiness["ready"] is False
    assert readiness["cli_version"] is None
    assert readiness["version_probe"]["status"] == "empty_version"


def test_cli_adapter_readiness_reports_missing_executable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("ncp.dogfood.shutil.which", lambda _name: None)

    readiness = get_live_provider_readiness("claude-cli")

    assert readiness["dependency_installed"] is False
    assert readiness["ready"] is False
    assert readiness["version_probe"] == {
        "status": "missing_executable",
        "diagnostic": "claude executable not found on PATH",
    }


def test_extract_opencode_text_uses_last_text_event() -> None:
    output = "\n".join([
        json_line({"type": "step_start", "part": {"id": "a"}}),
        json_line({"type": "text", "part": {"text": "NCP_FETCH_REQUEST\nquery:first"}}),
        json_line({"type": "text", "part": {"text": "NCP_FINAL\ncontent:done"}}),
    ])

    assert _extract_opencode_text(output) == "NCP_FINAL\ncontent:done"


def test_claude_cli_adapter_parses_json_result_and_resolved_model(
    tmp_path: Path,
) -> None:
    payload = json_line(
        {
            "type": "result",
            "result": "NCP_FINAL\ncontent:done",
            "modelUsage": {
                "claude-sonnet-5": {
                    "inputTokens": 10,
                    "outputTokens": 5,
                }
            },
        }
    )
    adapter = ClaudeCLIDogfoodAdapter(
        command=["python3", "-c", f"print({payload!r})"],
        cwd=tmp_path,
    )

    result = adapter.call("ctx", "turn")

    assert result == "NCP_FINAL\ncontent:done"
    assert adapter.last_call_metadata == {"model": "claude-sonnet-5"}


def test_claude_cli_adapter_default_command_adds_repo_dir(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}
    payload = json_line(
        {
            "result": "NCP_FINAL\ncontent:done",
            "modelUsage": {"claude-sonnet-5": {}},
        }
    )

    def _fake_run(command, **kwargs):
        captured["command"] = command
        captured["cwd"] = kwargs.get("cwd")
        return subprocess.CompletedProcess(command, 0, stdout=payload, stderr="")

    monkeypatch.setattr("ncp.dogfood.subprocess.run", _fake_run)
    adapter = ClaudeCLIDogfoodAdapter(cwd=tmp_path)

    result = adapter.call("ctx", "turn")

    assert result == "NCP_FINAL\ncontent:done"
    command = captured["command"]
    assert isinstance(command, list)
    assert command[command.index("--model") + 1] == "sonnet"
    assert command[command.index("--output-format") + 1] == "json"
    assert "--add-dir" in command
    assert command[command.index("--add-dir") + 1] == str(tmp_path)
    assert "--" in command
    assert command[-1] == "turn"
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


def test_opencode_cli_adapter_parses_events_and_sanitized_export_metadata(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = json_line(
        {
            "type": "text",
            "sessionID": "ses_test",
            "part": {
                "sessionID": "ses_test",
                "text": "NCP_FINAL\ncontent:done",
            },
        }
    )
    exported = json_line(
        {
            "info": {
                "id": "ses_test",
                "model": {
                    "providerID": "opencode",
                    "id": "deepseek-v4-flash-free",
                },
                "version": "1.17.15",
            },
            "messages": [],
        }
    )
    commands: list[list[str]] = []

    def _fake_run(command, **kwargs):
        commands.append(command)
        output = payload if len(commands) == 1 else exported
        return subprocess.CompletedProcess(command, 0, stdout=output, stderr="")

    monkeypatch.setattr("ncp.dogfood.subprocess.run", _fake_run)
    adapter = OpenCodeCLIDogfoodAdapter(cwd=tmp_path)

    result = adapter.call("ctx", "turn")

    assert result == "NCP_FINAL\ncontent:done"
    assert commands[1] == ["opencode", "export", "--sanitize", "ses_test"]
    assert adapter.last_call_metadata == {
        "model": "opencode/deepseek-v4-flash-free",
        "cli_version": "1.17.15",
        "session_id": "ses_test",
    }


def test_opencode_cli_adapter_default_command_sets_dir(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = json_line(
        {
            "type": "text",
            "sessionID": "ses_test",
            "part": {
                "sessionID": "ses_test",
                "text": "NCP_FINAL\ncontent:done",
            },
        }
    )
    exported = json_line(
        {
            "info": {
                "id": "ses_test",
                "model": {"providerID": "opencode", "id": "model"},
                "version": "1.17.15",
            }
        }
    )
    captured: dict[str, object] = {}
    calls = 0

    def _fake_run(command, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            captured["command"] = command
            captured["cwd"] = kwargs.get("cwd")
            return subprocess.CompletedProcess(command, 0, stdout=payload, stderr="")
        return subprocess.CompletedProcess(command, 0, stdout=exported, stderr="")

    monkeypatch.setattr("ncp.dogfood.subprocess.run", _fake_run)
    adapter = OpenCodeCLIDogfoodAdapter(cwd=tmp_path)

    result = adapter.call("ctx", "turn")

    assert result == "NCP_FINAL\ncontent:done"
    command = captured["command"]
    assert isinstance(command, list)
    assert "--dir" in command
    assert command[command.index("--dir") + 1] == str(tmp_path)
    assert "-m" not in command
    assert "--model" not in command
    assert captured["cwd"] == tmp_path


def test_opencode_cli_adapter_preserves_timeout_type_after_retries(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    commands: list[list[str]] = []

    def _timeout(command, **kwargs):
        commands.append(command)
        raise subprocess.TimeoutExpired(command, kwargs["timeout"])

    monkeypatch.setattr("ncp.dogfood.subprocess.run", _timeout)
    adapter = OpenCodeCLIDogfoodAdapter(
        command=["opencode"],
        cwd=tmp_path,
        timeout_seconds=1.0,
    )

    with pytest.raises(subprocess.TimeoutExpired):
        adapter.call("ctx", "turn")

    assert len(commands) == 2


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
    monkeypatch.setattr(
        "ncp.dogfood.subprocess.run",
        lambda command, **kwargs: subprocess.CompletedProcess(
            command,
            0,
            stdout="codex-cli 0.86.0\n",
            stderr="",
        ),
    )
    readiness = get_live_provider_readiness("codex-cli")

    assert readiness["adapter_name"] == "codex-cli"
    assert readiness["dependency_installed"] is True
    assert readiness["credentials_present"] is True
    assert readiness["ready"] is True
    assert readiness["cli_version"] == "codex-cli 0.86.0"


def json_line(payload: dict[str, object]) -> str:
    import json

    return json.dumps(payload)
