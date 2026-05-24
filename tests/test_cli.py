from pathlib import Path
import json

from click.testing import CliRunner

from ncp.cli import main
from ncp.stores.sqlite import SQLiteStore


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


def test_cli_serve_passes_explicit_cwd(monkeypatch: object, tmp_path: Path) -> None:
    called: dict[str, object] = {}

    def fake_serve(*, store_path: Path | None = None, cwd: Path | None = None) -> None:
        called["store_path"] = store_path
        called["cwd"] = cwd

    monkeypatch.setattr("ncp.mcp.server.serve", fake_serve)
    runner = CliRunner()

    result = runner.invoke(main, ["serve", "--cwd", str(tmp_path)])

    assert result.exit_code == 0
    assert called["cwd"] == tmp_path
    assert called["store_path"] is None


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
    assert payload["mode"] == "adapter_continuation"
    assert payload["adapter"] == "DogfoodLocalAdapter"
    assert payload["continuation_ok"] is True


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
