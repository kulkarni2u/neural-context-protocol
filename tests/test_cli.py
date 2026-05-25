from pathlib import Path
import json

from click.testing import CliRunner

from ncp.cli import main
from ncp.stores.sqlite import SQLiteStore
from ncp.types import SubconsciousChunk, Whisper


def test_cli_init_creates_config_and_claude_md(tmp_path: Path) -> None:
    runner = CliRunner()

    result = runner.invoke(main, ["init", "--cwd", str(tmp_path)])

    assert result.exit_code == 0
    assert (tmp_path / ".ncp" / "config.toml").exists()
    assert (tmp_path / "CLAUDE.md").exists()


def test_cli_status_renders_table(tmp_path: Path) -> None:
    runner = CliRunner()
    runner.invoke(main, ["init", "--cwd", str(tmp_path)])

    result = runner.invoke(main, ["status", "--cwd", str(tmp_path)])

    assert result.exit_code == 0
    assert "NCP Status" in result.output
    assert "Chunks" in result.output


def test_cli_status_json_and_cost_command(tmp_path: Path) -> None:
    runner = CliRunner()
    runner.invoke(main, ["init", "--cwd", str(tmp_path)])
    store = SQLiteStore(tmp_path / ".ncp" / "store.db")
    store.write(
        SubconsciousChunk(
            chunk_id="sub_status",
            layer="semantic",
            content="status example chunk",
            src="tool_result",
            pipeline_id="pipe_cli",
        )
    )
    store.log_cost_raw(
        agent_id="planner",
        model="claude-sonnet",
        input_tokens=120,
        output_tokens=18,
        cost_usd=0.021,
        pipeline_id="pipe_cli",
        turn_id="turn_cost_cli",
        latency_ms=180,
    )

    status_result = runner.invoke(
        main,
        ["status", "--cwd", str(tmp_path), "--pipeline-id", "pipe_cli", "--json-output"],
    )
    cost_result = runner.invoke(
        main,
        ["cost", "--cwd", str(tmp_path), "--pipeline-id", "pipe_cli", "--json-output"],
    )

    assert status_result.exit_code == 0
    status_payload = json.loads(status_result.output)
    assert status_payload["pipeline_id"] == "pipe_cli"
    assert status_payload["overview"]["chunk_count"] == 1
    assert status_payload["layer_counts"]["semantic"] == 1

    assert cost_result.exit_code == 0
    cost_payload = json.loads(cost_result.output)
    assert cost_payload["pipeline_id"] == "pipe_cli"
    assert cost_payload["summary"]["cost_usd_total"] == 0.021
    assert cost_payload["recent_entries"][0]["turn_id"] == "turn_cost_cli"


def test_cli_explain_renders_narrative_and_json(tmp_path: Path) -> None:
    runner = CliRunner()
    runner.invoke(main, ["init", "--cwd", str(tmp_path)])
    store = SQLiteStore(tmp_path / ".ncp" / "store.db")
    store.write(
        SubconsciousChunk(
            chunk_id="sub_explain",
            layer="semantic",
            content="explain example chunk",
            src="tool_result",
            pipeline_id="pipe_explain",
        )
    )
    store.log_cost_raw(
        agent_id="planner",
        model="claude-sonnet",
        input_tokens=90,
        output_tokens=12,
        cost_usd=0.015,
        pipeline_id="pipe_explain",
        turn_id="turn_explain",
        latency_ms=120,
    )

    text_result = runner.invoke(
        main,
        ["explain", "--cwd", str(tmp_path), "--pipeline-id", "pipe_explain"],
    )
    json_result = runner.invoke(
        main,
        ["explain", "--cwd", str(tmp_path), "--pipeline-id", "pipe_explain", "--json-output"],
    )

    assert text_result.exit_code == 0
    assert "NCP Explain" in text_result.output
    assert "highest-cost agent so far: planner" in text_result.output
    assert "layer distribution:" in text_result.output

    assert json_result.exit_code == 0
    payload = json.loads(json_result.output)
    assert payload["pipeline_id"] == "pipe_explain"
    assert payload["facts"]["chunk_count"] == 1
    assert payload["facts"]["top_agent"] == "planner"
    assert payload["cost"]["summary"]["cost_usd_total"] == 0.015


def test_cli_serve_passes_explicit_cwd(monkeypatch: object, tmp_path: Path) -> None:
    called: dict[str, object] = {}

    def fake_serve_http(
        *,
        host: str,
        port: int,
        store_path: Path | None = None,
        cwd: Path | None = None,
    ) -> None:
        called["host"] = host
        called["port"] = port
        called["store_path"] = store_path
        called["cwd"] = cwd

    monkeypatch.setattr("ncp.mcp.server.serve_http", fake_serve_http)
    runner = CliRunner()

    result = runner.invoke(main, ["serve", "--cwd", str(tmp_path)])

    assert result.exit_code == 0
    assert called["host"] == "127.0.0.1"
    assert called["port"] == 4242
    assert called["cwd"] == tmp_path
    assert called["store_path"] is None


def test_cli_serve_passes_network_options(monkeypatch: object, tmp_path: Path) -> None:
    called: dict[str, object] = {}

    def fake_serve_http(
        *,
        host: str,
        port: int,
        store_path: Path | None = None,
        cwd: Path | None = None,
    ) -> None:
        called["host"] = host
        called["port"] = port
        called["store_path"] = store_path
        called["cwd"] = cwd

    monkeypatch.setattr("ncp.mcp.server.serve_http", fake_serve_http)
    runner = CliRunner()

    result = runner.invoke(
        main,
        ["serve", "--host", "0.0.0.0", "--port", "4545", "--cwd", str(tmp_path)],
    )

    assert result.exit_code == 0
    assert called == {
        "host": "0.0.0.0",
        "port": 4545,
        "store_path": None,
        "cwd": tmp_path,
    }


def test_cli_serve_stdio_hidden_command(monkeypatch: object, tmp_path: Path) -> None:
    called: dict[str, object] = {}

    def fake_serve(*, store_path: Path | None = None, cwd: Path | None = None) -> None:
        called["store_path"] = store_path
        called["cwd"] = cwd

    monkeypatch.setattr("ncp.mcp.server.serve", fake_serve)
    runner = CliRunner()

    result = runner.invoke(main, ["serve-stdio", "--cwd", str(tmp_path)])

    assert result.exit_code == 0
    assert called == {"store_path": None, "cwd": tmp_path}


def test_cli_status_reports_store_unavailable_cleanly(tmp_path: Path) -> None:
    runner = CliRunner()
    runner.invoke(main, ["init", "--cwd", str(tmp_path)])
    broken_path = tmp_path / "broken-db-dir"
    broken_path.mkdir()

    result = runner.invoke(
        main,
        ["status", "--cwd", str(tmp_path)],
        env={"NCP_STORE_PATH": str(broken_path)},
    )

    assert result.exit_code != 0
    assert "SQLite store unavailable" in result.output


def test_cli_status_reports_pgvector_rollout_boundary(tmp_path: Path) -> None:
    runner = CliRunner()
    runner.invoke(main, ["init", "--cwd", str(tmp_path)])

    result = runner.invoke(
        main,
        ["status", "--cwd", str(tmp_path)],
        env={"NCP_STORE_TYPE": "pgvector"},
    )

    assert result.exit_code != 0
    assert "currently supports sqlite only" in result.output


def test_cli_emit_writes_whisper(tmp_path: Path) -> None:
    runner = CliRunner()
    runner.invoke(main, ["init", "--cwd", str(tmp_path)])

    result = runner.invoke(
        main,
        [
            "emit",
            "--cwd",
            str(tmp_path),
            "--from-agent",
            "planner",
            "--to",
            "executor",
            "--type",
            "nudge",
            "--payload",
            "check_tests",
        ],
    )

    assert result.exit_code == 0
    store = SQLiteStore(tmp_path / ".ncp" / "store.db")
    whispers = store.drain_whispers(agent_id="executor")
    assert [whisper.payload for whisper in whispers] == ["check_tests"]


def test_cli_handoff_claude_consumes_and_emits_follow_up(
    tmp_path: Path,
    monkeypatch: object,
) -> None:
    runner = CliRunner()
    runner.invoke(main, ["init", "--cwd", str(tmp_path)])
    store = SQLiteStore(tmp_path / ".ncp" / "store.db")
    store.emit_whisper(
        Whisper(
            from_agent="codex",
            target="claude",
            whisper_type="share",
            payload="implement wrapper review flow",
            confidence=0.95,
            pipeline_id="pipe_handoff_cli",
        )
    )

    monkeypatch.setattr(
        "ncp.agent_handoff.run_claude_partner",
        lambda **_: "claude finished the slice and handed it off",
    )

    result = runner.invoke(
        main,
        [
            "handoff",
            "claude",
            "--cwd",
            str(tmp_path),
            "--pipeline-id",
            "pipe_handoff_cli",
            "--emit-to",
            "opencode",
        ],
    )

    assert result.exit_code == 0
    assert "claude finished the slice and handed it off" in result.output
    assert store.peek_whispers(agent_id="claude", pipeline_id="pipe_handoff_cli") == []
    follow_up = store.drain_whispers(agent_id="opencode", pipeline_id="pipe_handoff_cli")
    assert [whisper.payload for whisper in follow_up] == ["claude finished the slice and handed it off"]


def test_cli_handoff_opencode_requires_json_and_emits_follow_up(
    tmp_path: Path,
    monkeypatch: object,
) -> None:
    runner = CliRunner()
    runner.invoke(main, ["init", "--cwd", str(tmp_path)])
    store = SQLiteStore(tmp_path / ".ncp" / "store.db")
    store.emit_whisper(
        Whisper(
            from_agent="claude",
            target="opencode",
            whisper_type="share",
            payload="review wrapper repo binding",
            confidence=0.95,
            pipeline_id="pipe_handoff_cli",
        )
    )

    monkeypatch.setattr(
        "ncp.agent_handoff.run_opencode_reviewer",
        lambda **_: """```json
{"verdict":"pass","findings":[],"recommended_next_steps":[],"summary":"clean"}
```""",
    )

    result = runner.invoke(
        main,
        [
            "handoff",
            "opencode",
            "--cwd",
            str(tmp_path),
            "--pipeline-id",
            "pipe_handoff_cli",
            "--emit-to",
            "claude",
        ],
    )

    assert result.exit_code == 0
    assert '"verdict":"pass"' in result.output
    assert store.peek_whispers(agent_id="opencode", pipeline_id="pipe_handoff_cli") == []
    follow_up = store.drain_whispers(agent_id="claude", pipeline_id="pipe_handoff_cli")
    assert len(follow_up) == 1
    assert '"summary":"clean"' in follow_up[0].payload


def test_cli_handoff_reports_missing_queue_cleanly(tmp_path: Path) -> None:
    runner = CliRunner()
    runner.invoke(main, ["init", "--cwd", str(tmp_path)])

    result = runner.invoke(
        main,
        ["handoff", "claude", "--cwd", str(tmp_path), "--pipeline-id", "pipe_empty"],
    )

    assert result.exit_code != 0
    assert "No pending NCP handoffs for claude." in result.output


def test_cli_emit_reports_store_unavailable_cleanly(tmp_path: Path) -> None:
    runner = CliRunner()
    runner.invoke(main, ["init", "--cwd", str(tmp_path)])
    broken_path = tmp_path / "broken-emit-dir"
    broken_path.mkdir()

    result = runner.invoke(
        main,
        [
            "emit",
            "--cwd",
            str(tmp_path),
            "--from-agent",
            "planner",
            "--to",
            "executor",
            "--type",
            "nudge",
            "--payload",
            "check_tests",
        ],
        env={"NCP_STORE_PATH": str(broken_path)},
    )

    assert result.exit_code != 0
    assert "SQLite store unavailable" in result.output


def test_cli_emit_reports_pgvector_boundary_cleanly(tmp_path: Path) -> None:
    runner = CliRunner()
    runner.invoke(main, ["init", "--cwd", str(tmp_path)])

    result = runner.invoke(
        main,
        [
            "emit",
            "--cwd",
            str(tmp_path),
            "--from-agent",
            "planner",
            "--to",
            "executor",
            "--type",
            "nudge",
            "--payload",
            "check_tests",
        ],
        env={"NCP_STORE_TYPE": "pgvector"},
    )

    assert result.exit_code != 0
    assert "currently supports sqlite only" in result.output
    assert "0.2.0 rollout" in result.output


def test_cli_dogfood_prints_restart_proof(tmp_path: Path) -> None:
    runner = CliRunner()
    runner.invoke(main, ["init", "--cwd", str(tmp_path)])

    result = runner.invoke(
        main,
        [
            "dogfood",
            "--cwd",
            str(tmp_path),
            "--pipeline-id",
            "pipe_cli_dogfood",
        ],
    )

    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["transport"] == "http_sse_mcp"
    assert payload["restart_persistence_ok"] is True
    assert payload["provider_roles"]["planner"] == "claude"
    assert payload["summary"]["continuation_ok"] is True


def test_cli_dogfood_adapter_mode_prints_continuation_artifact(tmp_path: Path) -> None:
    runner = CliRunner()
    runner.invoke(main, ["init", "--cwd", str(tmp_path)])

    result = runner.invoke(
        main,
        [
            "dogfood",
            "--cwd",
            str(tmp_path),
            "--pipeline-id",
            "pipe_cli_adapter_dogfood",
            "--continuation-adapter",
            "local",
        ],
    )

    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["transport"] == "http_sse_mcp"
    assert payload["mode"] == "adapter_continuation"
    assert payload["adapter"] == "DogfoodLocalAdapter"
    assert payload["continuation_ok"] is True


def test_cli_dogfood_hidden_stdio_transport_still_available(
    tmp_path: Path,
    monkeypatch: object,
) -> None:
    monkeypatch.setattr(
        "ncp.dogfood.run_canonical_dogfood_loop",
        lambda **kwargs: {"transport": "stdio_mcp", "restart_persistence_ok": True},
    )
    runner = CliRunner()
    runner.invoke(main, ["init", "--cwd", str(tmp_path)])

    result = runner.invoke(
        main,
        [
            "dogfood",
            "--cwd",
            str(tmp_path),
            "--transport",
            "stdio",
        ],
    )

    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["transport"] == "stdio_mcp"


def test_cli_dogfood_external_adapter_reports_missing_credentials(
    tmp_path: Path,
    monkeypatch: object,
) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    runner = CliRunner()
    runner.invoke(main, ["init", "--cwd", str(tmp_path)])

    result = runner.invoke(
        main,
        [
            "dogfood",
            "--cwd",
            str(tmp_path),
            "--pipeline-id",
            "pipe_cli_live_missing",
            "--continuation-adapter",
            "anthropic",
        ],
    )

    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["mode"] == "live_adapter_attempt"
    assert payload["adapter_name"] == "anthropic"
    assert payload["attempted"] is False
    assert payload["status"] == "missing_credentials"


def test_cli_help_mentions_cli_backed_continuation_adapters() -> None:
    runner = CliRunner()

    result = runner.invoke(main, ["dogfood", "--help"])

    assert result.exit_code == 0
    assert "claude-cli" in result.output
    assert "codex-cli" in result.output
    assert "opencode-cli" in result.output


def test_cli_dogfood_repeatability_mode_prints_summary_artifact(
    tmp_path: Path,
    monkeypatch: object,
) -> None:
    monkeypatch.setattr(
        "ncp.dogfood.run_repeatability_dogfood_loop",
        lambda *args, **kwargs: {
            "mode": "repeatability_run",
            "adapter_name": "opencode-cli",
            "summary": {"stable": False, "successes": 2},
        },
    )
    runner = CliRunner()
    runner.invoke(main, ["init", "--cwd", str(tmp_path)])

    result = runner.invoke(
        main,
        [
            "dogfood",
            "--cwd",
            str(tmp_path),
            "--pipeline-id",
            "pipe_cli_repeatability",
            "--continuation-adapter",
            "opencode-cli",
            "--attempts",
            "3",
            "--adapter-timeout-seconds",
            "18",
        ],
    )

    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["mode"] == "repeatability_run"
    assert payload["adapter_name"] == "opencode-cli"
    assert payload["summary"]["successes"] == 2


def test_cli_dogfood_attempts_require_continuation_adapter(tmp_path: Path) -> None:
    runner = CliRunner()
    runner.invoke(main, ["init", "--cwd", str(tmp_path)])

    result = runner.invoke(
        main,
        [
            "dogfood",
            "--cwd",
            str(tmp_path),
            "--attempts",
            "2",
        ],
    )

    assert result.exit_code != 0
    assert "--attempts requires --continuation-adapter" in result.output
