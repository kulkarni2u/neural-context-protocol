from __future__ import annotations

from pathlib import Path
import json
import subprocess
import sys


REPO_ROOT = Path(__file__).resolve().parents[1]


def _run_example(path: Path) -> dict[str, object]:
    completed = subprocess.run(
        [sys.executable, str(path)],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
    )
    return json.loads(completed.stdout)


def test_quickstart_example_runs() -> None:
    payload = _run_example(REPO_ROOT / "examples" / "01_quickstart.py")

    assert payload["response_first_line"] == "local_adapter_response"
    assert payload["turn_records"] == 1
    assert payload["cost_usd_total"] == 0.0


def test_multi_agent_example_runs() -> None:
    payload = _run_example(REPO_ROOT / "examples" / "02_multi_agent.py")

    assert payload["planner_first_line"] == "local_adapter_response"
    assert payload["executor_first_line"] == "local_adapter_response"
    assert payload["critic_first_line"] == "local_adapter_response"
    assert payload["executor_context_has_plan"] is True
    assert payload["executor_context_has_whisper"] is True
    assert payload["turn_records"] == 3


def test_claude_code_example_files_exist() -> None:
    example_dir = REPO_ROOT / "examples" / "06_claude_code"

    assert (example_dir / "CLAUDE.md").exists()
    config = json.loads((example_dir / "mcp_servers.json").read_text())
    assert config["ncp"]["command"] == "ncp"
    assert config["ncp"]["args"] == ["serve"]
    assert "ncp_get_context" in (example_dir / "README.md").read_text()


def test_codex_cli_example_files_exist() -> None:
    example_dir = REPO_ROOT / "examples" / "07_codex_cli"

    config = json.loads((example_dir / "mcp_servers.json").read_text())
    assert config["mcpServers"]["ncp"]["command"] == "ncp"
    assert config["mcpServers"]["ncp"]["args"] == ["serve"]
    assert "ncp_write_memory" in (example_dir / "README.md").read_text()
