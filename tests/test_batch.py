from pathlib import Path
import json

from click.testing import CliRunner

from ncp.batch import run_batch
from ncp.cli import main
from ncp.stores.sqlite import SQLiteStore
from ncp.types import SubconsciousChunk


def _store(tmp_path: Path) -> SQLiteStore:
    return SQLiteStore(tmp_path / "batch_store.db")


def test_write_memory_op_writes_chunk_and_returns_chunk_id(tmp_path: Path) -> None:
    store = _store(tmp_path)
    ops = [
        {"op": "write_memory", "content": "hello world", "layer": "semantic", "src": "agent_inferred", "written_by": "batch_test"}
    ]
    results = run_batch(ops, store)
    assert len(results) == 1
    r = results[0]
    assert r["op"] == "write_memory"
    assert r["ok"] is True
    assert isinstance(r["chunk_id"], str)
    assert r["chunk_id"].startswith("sub_")
    assert r["written"] is True


def test_query_op_returns_results(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.write(SubconsciousChunk(content="authentication token abc123", layer="semantic", src="agent_inferred", written_by="test", pipeline_id="pipe_q"))
    ops = [{"op": "query", "text": "authentication", "k": 3, "pipeline_id": "pipe_q"}]
    results = run_batch(ops, store)
    assert len(results) == 1
    r = results[0]
    assert r["op"] == "query"
    assert r["ok"] is True
    assert isinstance(r["results"], list)
    assert len(r["results"]) > 0
    for item in r["results"]:
        assert "chunk_id" in item
        assert "relevance" in item
        assert "content" in item


def test_emit_whisper_op_enqueues_whisper(tmp_path: Path) -> None:
    store = _store(tmp_path)
    ops = [
        {"op": "emit_whisper", "from_agent": "ci", "to": "claude", "whisper_type": "nudge", "payload": "run tests", "confidence": 0.9}
    ]
    results = run_batch(ops, store)
    assert len(results) == 1
    assert results[0]["op"] == "emit_whisper"
    assert results[0]["ok"] is True
    whispers = store.drain_whispers(agent_id="claude")
    assert len(whispers) == 1
    assert whispers[0].payload == "run tests"


def test_consolidate_op_returns_counts(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.write(SubconsciousChunk(content="dup content A", layer="semantic", src="agent_inferred", written_by="test"))
    store.write(SubconsciousChunk(content="dup content A", layer="semantic", src="agent_inferred", written_by="test"))
    ops = [{"op": "consolidate", "pipeline_id": None}]
    results = run_batch(ops, store)
    assert len(results) == 1
    r = results[0]
    assert r["op"] == "consolidate"
    assert r["ok"] is True
    assert r["merged"] >= 0
    assert r["tombstoned"] >= 0
    assert r["clusters_scanned"] >= 0


def test_calibrate_op_returns_counts(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.write(SubconsciousChunk(content="calibrate me", layer="semantic", src="user_verified", written_by="test"))
    ops = [{"op": "calibrate", "pipeline_id": None}]
    results = run_batch(ops, store)
    assert len(results) == 1
    r = results[0]
    assert r["op"] == "calibrate"
    assert r["ok"] is True
    assert r["adjusted"] >= 0
    assert r["protected"] >= 0


def test_unknown_op_returns_ok_false(tmp_path: Path) -> None:
    store = _store(tmp_path)
    ops = [{"op": "nonexistent_op"}]
    results = run_batch(ops, store)
    assert len(results) == 1
    r = results[0]
    assert r["op"] == "nonexistent_op"
    assert r["ok"] is False
    assert r["error"] == "unknown op"


def test_dry_run_does_not_write(tmp_path: Path) -> None:
    store = _store(tmp_path)
    ops = [
        {"op": "write_memory", "content": "dry run test", "layer": "semantic", "src": "agent_inferred", "written_by": "batch_test"},
        {"op": "emit_whisper", "from_agent": "ci", "to": "claude", "whisper_type": "nudge", "payload": "dry run whisper"},
    ]
    results = run_batch(ops, store, dry_run=True)
    assert len(results) == 2
    assert results[0]["written"] is False
    assert store.drain_whispers(agent_id="claude") == []


def test_stop_on_error_halts_after_first_error(tmp_path: Path) -> None:
    store = _store(tmp_path)
    ops = [
        {"op": "nonexistent"},
        {"op": "write_memory", "content": "should not write", "layer": "semantic", "src": "agent_inferred", "written_by": "test"},
    ]
    results = run_batch(ops, store, stop_on_error=True)
    assert len(results) == 1
    assert results[0]["ok"] is False


def test_empty_input_returns_empty_output(tmp_path: Path) -> None:
    store = _store(tmp_path)
    results = run_batch([], store)
    assert results == []


def test_malformed_json_line_handled_in_cli(tmp_path: Path) -> None:
    runner = CliRunner()
    input_file = tmp_path / "bad.jsonl"
    input_file.write_text('{"op": "write_memory", "content": "ok", "layer": "semantic", "src": "agent_inferred", "written_by": "test"}\nnot valid json\n')
    result = runner.invoke(
        main,
        ["batch", str(input_file), "--cwd", str(tmp_path)],
    )

    assert result.exit_code == 0
    lines = result.output.strip().split("\n")
    assert len(lines) == 2
    assert json.loads(lines[0])["ok"] is True
    assert json.loads(lines[1])["ok"] is False


def test_stdin_input(tmp_path: Path) -> None:
    runner = CliRunner()
    input_lines = '{"op": "write_memory", "content": "stdin test", "layer": "semantic", "src": "tool_result", "written_by": "test"}\n' \
                  '{"op": "query", "text": "stdin", "k": 2}\n'

    result = runner.invoke(
        main,
        ["batch", "--cwd", str(tmp_path)],
        input=input_lines,
    )

    assert result.exit_code == 0
    lines = result.output.strip().split("\n")
    assert len(lines) == 2
    assert json.loads(lines[0])["op"] == "write_memory"
    assert json.loads(lines[0])["ok"] is True
    assert json.loads(lines[1])["op"] == "query"
    assert json.loads(lines[1])["ok"] is True


def test_output_file_writes_results(tmp_path: Path) -> None:
    runner = CliRunner()
    input_file = tmp_path / "in.jsonl"
    output_file = tmp_path / "out.jsonl"
    input_file.write_text('{"op": "write_memory", "content": "output test", "layer": "semantic", "src": "agent_inferred", "written_by": "test"}\n')

    result = runner.invoke(
        main,
        ["batch", str(input_file), "--output", str(output_file), "--cwd", str(tmp_path)],
    )

    assert result.exit_code == 0
    assert output_file.exists()
    lines = output_file.read_text().strip().split("\n")
    assert len(lines) == 1
    assert json.loads(lines[0])["ok"] is True
