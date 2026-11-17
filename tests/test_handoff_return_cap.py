"""`ncp handoff --return summary|full` bounds what the parent receives."""

from __future__ import annotations

from pathlib import Path

import pytest
from click.testing import CliRunner

from ncp.cli import main
from ncp.stores.sqlite import SQLiteStore
from ncp.types import Whisper

BIG = ("word " * 4000).strip()  # ~20 KB
PIPE = "pipe_return_cap"


def _setup(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, response: str) -> SQLiteStore:
    CliRunner().invoke(main, ["init", "--cwd", str(tmp_path)])
    store = SQLiteStore(tmp_path / ".ncp" / "store.db")
    store.emit_whisper(
        Whisper(
            from_agent="codex",
            target="claude",
            whisper_type="nudge",
            payload="do the thing",
            confidence=0.95,
            pipeline_id=PIPE,
        )
    )
    monkeypatch.setattr("ncp.agent_handoff.run_claude_partner", lambda **_: response)
    return store


def _invoke(tmp_path: Path, *extra: str):
    return CliRunner().invoke(
        main,
        ["handoff", "claude", "--cwd", str(tmp_path), "--pipeline-id", PIPE, *extra],
    )


def test_summary_is_bounded_and_links_full_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = _setup(tmp_path, monkeypatch, BIG)
    result = _invoke(tmp_path)

    assert result.exit_code == 0
    out = result.output.rstrip("\n")
    assert len(out) <= 2000
    chunks = store.get_working_zone(pipeline_id=PIPE, layer="episodic")
    summary = next(c for c in chunks if c.raw_ref)
    assert summary.raw_ref in out
    raw = next(c for c in chunks if c.chunk_id == summary.raw_ref)
    assert raw.content == BIG


def test_max_return_chars_respected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _setup(tmp_path, monkeypatch, BIG)
    result = _invoke(tmp_path, "--max-return-chars", "500")

    assert result.exit_code == 0
    out = result.output.rstrip("\n")
    assert len(out) <= 500
    assert "full_output_chunk_id" in out


def test_return_full_is_unchanged_output(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _setup(tmp_path, monkeypatch, "short answer")
    result = _invoke(tmp_path, "--return", "full")

    assert result.exit_code == 0
    assert result.output == "short answer\n"


def test_raw_chunk_is_redacted(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store = _setup(tmp_path, monkeypatch, "token: sk-live-supersecret done")
    result = _invoke(tmp_path)

    assert result.exit_code == 0
    assert "sk-live-supersecret" not in result.output
    for chunk in store.get_working_zone(pipeline_id=PIPE, layer="episodic"):
        assert "sk-live-supersecret" not in chunk.content
