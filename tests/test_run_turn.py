"""`ncp run-turn` runs one headless turn: assemble, execute, post, ack."""

from __future__ import annotations

from pathlib import Path

from click.testing import CliRunner

from ncp.cli import main
from ncp.stores.sqlite import SQLiteStore
from ncp.types import Whisper

PIPE = "pipe_run_turn"


def _init(tmp_path: Path) -> SQLiteStore:
    CliRunner().invoke(main, ["init", "--cwd", str(tmp_path)])
    return SQLiteStore(tmp_path / ".ncp" / "store.db")


def _run(tmp_path: Path, *extra: str, input: str | None = None):
    return CliRunner().invoke(
        main,
        [
            "run-turn", "--cwd", str(tmp_path), "--agent", "worker", "--pipeline", PIPE,
            "--host", "mock", "--task", "do_thing", "--slot", "build", *extra,
        ],
        input=input,
    )


def test_mock_turn_writes_record_and_acks_whispers(tmp_path: Path) -> None:
    store = _init(tmp_path)
    store.emit_whisper(
        Whisper(
            from_agent="brain",
            target="worker",
            whisper_type="nudge",
            payload="check the parser",
            confidence=0.95,
            pipeline_id=PIPE,
        )
    )
    assert store.peek_whispers(agent_id="worker", pipeline_id=PIPE)

    result = _run(tmp_path, "--prompt", "implement the thing")

    assert result.exit_code == 0, result.output
    assert "mock turn result: implement the thing" in result.output
    turns = store.list_turns(pipeline_id=PIPE)
    assert len(turns) == 1
    assert turns[0].agent_id == "worker"
    assert turns[0].result_full.startswith("mock turn result: implement the thing")
    assert store.peek_whispers(agent_id="worker", pipeline_id=PIPE) == []


def test_instruction_from_stdin(tmp_path: Path) -> None:
    _init(tmp_path)
    result = _run(tmp_path, "--return", "full", input="from stdin")
    assert result.exit_code == 0, result.output
    assert "mock turn result: from stdin" in result.output


def test_summary_output_is_capped_and_full_is_not(tmp_path: Path) -> None:
    store = _init(tmp_path)
    big = ("word " * 4000).strip()

    summary = _run(tmp_path, "--prompt", big, "--max-return-chars", "500")
    assert summary.exit_code == 0, summary.output
    assert len(summary.output.rstrip("\n")) <= 500
    assert "full_output_chunk_id" in summary.output

    full = _run(tmp_path, "--prompt", big, "--return", "full")
    assert full.exit_code == 0, full.output
    assert len(full.output) > 10000
    assert all(len(t.result) <= 600 for t in store.list_turns(pipeline_id=PIPE))


def test_empty_instruction_is_rejected(tmp_path: Path) -> None:
    _init(tmp_path)
    result = _run(tmp_path, "--prompt", "  ")
    assert result.exit_code != 0


def test_completion_and_raw_chunks_carry_the_turn_pipeline(tmp_path: Path) -> None:
    import re

    store = _init(tmp_path)
    result = _run(tmp_path, "--prompt", "implement the thing")
    assert result.exit_code == 0, result.output
    match = re.search(r"full_output_chunk_id\W+(raw_[0-9a-f]+)", result.output)
    assert match, result.output
    raw_id = match.group(1)
    raw = store.get_chunks_by_ids([raw_id])
    assert raw and raw[0].pipeline_id == PIPE
    completion_id = re.search(r"completion_chunk_id\W+(\w+)", result.output)
    assert completion_id, result.output
    completion = store.get_chunks_by_ids([completion_id.group(1)])
    assert completion and completion[0].pipeline_id == PIPE

    # ncp_post_turn result_ref accepts the printed id for this pipeline.
    from ncp.mcp.server import MCP_TOOLS, _handle_request, make_handlers

    raw_resp = _handle_request(
        {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "ncp_post_turn", "arguments": {
            "agent_id": "worker", "role": "pravaha", "task": "do_thing", "slot": "build", "intent": "i",
            "pipeline_id": PIPE, "result_summary": "s", "result_ref": raw_id}}},
        make_handlers(store), tools=MCP_TOOLS,
    )
    import json

    assert "error" not in json.loads(raw_resp)


def test_handoff_completion_pipeline_behavior_unchanged(tmp_path: Path) -> None:
    from ncp.agent_handoff import PreparedHandoff, complete_handoff
    from ncp.config import load_config

    store = _init(tmp_path)
    prepared = PreparedHandoff(
        agent_id="a", config=load_config(cwd=tmp_path), store=store, handoffs=[], context="", workspace=tmp_path,
    )
    done = complete_handoff(prepared, runner="claude", response="out")
    assert store.get_chunks_by_ids([done.raw_chunk_id])[0].pipeline_id is None
