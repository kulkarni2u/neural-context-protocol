import json
from pathlib import Path
import sys

import pytest
from click.testing import CliRunner

from ncp.agent_handoff import (
    acknowledge_handoffs,
    emit_follow_up_whisper,
    load_sqlite_handoffs,
    parse_json_review,
    run_claude_partner,
    run_opencode_reviewer,
)
from ncp.cli import main
from ncp.stores.sqlite import SQLiteStore
from ncp.types import Whisper


def _seed_whisper(
    store: SQLiteStore,
    *,
    target: str,
    payload: str,
    pipeline_id: str = "pipe_handoff",
    from_agent: str = "codex",
) -> None:
    store.emit_whisper(
        Whisper(
            from_agent=from_agent,
            target=target,
            whisper_type="share",
            payload=payload,
            confidence=0.95,
            pipeline_id=pipeline_id,
        )
    )


def test_load_handoffs_peeks_without_consuming(tmp_path: Path) -> None:
    runner = CliRunner()
    runner.invoke(main, ["init", "--cwd", str(tmp_path)])
    store = SQLiteStore(tmp_path / ".ncp" / "store.db")
    _seed_whisper(store, target="claude", payload="implement pgvector integration")

    resolved_store, handoffs = load_sqlite_handoffs(cwd=tmp_path, agent_id="claude", pipeline_id="pipe_handoff")

    assert resolved_store.path == store.path
    assert [whisper.payload for whisper in handoffs] == ["implement pgvector integration"]
    assert [whisper.payload for whisper in store.peek_whispers(agent_id="claude", pipeline_id="pipe_handoff")] == [
        "implement pgvector integration"
    ]


def test_claude_partner_acknowledges_after_success_and_can_emit_follow_up(tmp_path: Path) -> None:
    runner = CliRunner()
    runner.invoke(main, ["init", "--cwd", str(tmp_path)])
    store = SQLiteStore(tmp_path / ".ncp" / "store.db")
    _seed_whisper(store, target="claude", payload="tighten the pgvector rollout boundary")

    resolved_store, handoffs = load_sqlite_handoffs(cwd=tmp_path, agent_id="claude", pipeline_id="pipe_handoff")
    response = run_claude_partner(
        cwd=tmp_path,
        agent_id="claude",
        handoffs=handoffs,
        command=[sys.executable, "-c", "print('implemented and ready for review')"],
    )
    deleted = acknowledge_handoffs(resolved_store, handoffs)
    emit_follow_up_whisper(
        cwd=tmp_path,
        from_agent="claude",
        target="opencode",
        pipeline_id="pipe_handoff",
        payload=response,
    )

    assert response == "implemented and ready for review"
    assert deleted == 1
    assert resolved_store.peek_whispers(agent_id="claude", pipeline_id="pipe_handoff") == []
    follow_up = resolved_store.drain_whispers(agent_id="opencode", pipeline_id="pipe_handoff")
    assert [whisper.payload for whisper in follow_up] == ["implemented and ready for review"]


def test_opencode_failure_does_not_consume_handoff(tmp_path: Path) -> None:
    runner = CliRunner()
    runner.invoke(main, ["init", "--cwd", str(tmp_path)])
    store = SQLiteStore(tmp_path / ".ncp" / "store.db")
    _seed_whisper(store, target="opencode", payload="review pgvector cleanup patch")

    _, handoffs = load_sqlite_handoffs(cwd=tmp_path, agent_id="opencode", pipeline_id="pipe_handoff")

    with pytest.raises(RuntimeError, match="boom"):
        run_opencode_reviewer(
            cwd=tmp_path,
            agent_id="opencode",
            handoffs=handoffs,
            command=[sys.executable, "-c", "import sys; sys.stderr.write('boom'); sys.exit(1)"],
        )

    remaining = store.peek_whispers(agent_id="opencode", pipeline_id="pipe_handoff")
    assert [whisper.payload for whisper in remaining] == ["review pgvector cleanup patch"]


def test_opencode_review_parses_json_text_payload(tmp_path: Path) -> None:
    runner = CliRunner()
    runner.invoke(main, ["init", "--cwd", str(tmp_path)])
    store = SQLiteStore(tmp_path / ".ncp" / "store.db")
    _seed_whisper(store, target="opencode", payload="review the handoff")

    _, handoffs = load_sqlite_handoffs(cwd=tmp_path, agent_id="opencode", pipeline_id="pipe_handoff")
    review_text = json.dumps(
        {
            "type": "text",
            "part": {
                "text": json.dumps(
                    {
                        "verdict": "pass",
                        "findings": [],
                        "recommended_next_steps": ["merge it"],
                        "summary": "clean slice",
                    }
                )
            },
        }
    )
    response = run_opencode_reviewer(
        cwd=tmp_path,
        agent_id="opencode",
        handoffs=handoffs,
        command=[sys.executable, "-c", f"print({review_text!r})"],
    )

    payload = parse_json_review(response)

    assert payload["verdict"] == "pass"
    assert payload["summary"] == "clean slice"


def test_parse_json_review_accepts_fenced_json() -> None:
    payload = parse_json_review(
        """```json
{"verdict":"pass","findings":[],"recommended_next_steps":["merge"],"summary":"clean"}
```"""
    )

    assert payload["verdict"] == "pass"
    assert payload["summary"] == "clean"
