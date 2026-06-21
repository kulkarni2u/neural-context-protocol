"""Tests for the trust-drift observability surface."""

from pathlib import Path
import json

from click.testing import CliRunner

from ncp.cli import main
from ncp.stores.sqlite import SQLiteStore
from ncp.types import SubconsciousChunk


def _make_store(tmp_path: Path) -> SQLiteStore:
    return SQLiteStore(tmp_path / ".ncp" / "store.db")


def _seed_chunks(store: SQLiteStore, pipeline_id: str = "pipe_td") -> None:
    store.write(
        SubconsciousChunk(
            chunk_id="rising_1",
            layer="episodic",
            content="frequently retrieved analysis",
            src="agent_inferred",
            base_trust=0.8,
            pipeline_id=pipeline_id,
        )
    )
    store.write(
        SubconsciousChunk(
            chunk_id="rising_2",
            layer="semantic",
            content="another retrieved chunk content",
            src="tool_result",
            base_trust=0.7,
            pipeline_id=pipeline_id,
        )
    )
    store.write(
        SubconsciousChunk(
            chunk_id="falling_1",
            layer="episodic",
            content="disputed analysis with errors",
            src="agent_inferred",
            base_trust=0.5,
            pipeline_id=pipeline_id,
        )
    )
    store.write(
        SubconsciousChunk(
            chunk_id="untouched_1",
            layer="procedural",
            content="neutral chunk no feedback",
            src="user_verified",
            base_trust=0.9,
            pipeline_id=pipeline_id,
        )
    )

    for _ in range(5):
        store.query("frequently retrieved analysis", k=4, min_score=0.0, pipeline_id=pipeline_id)
    for _ in range(2):
        store.query("another retrieved chunk", k=4, min_score=0.0, pipeline_id=pipeline_id)

    store.record_dissent("falling_1")
    store.record_dissent("falling_1")
    store.record_dissent("falling_1")


# ── Store method tests ──────────────────────────────────────────────────


def test_trust_drift_data_returns_expected_structure(tmp_path: Path) -> None:
    store = _make_store(tmp_path)
    _seed_chunks(store)

    data = store.trust_drift_data(pipeline_id="pipe_td")

    assert "trust_distribution" in data
    assert "rising" in data
    assert "falling" in data
    assert "feedback_summary" in data
    assert "drift_timeline" in data


def test_trust_drift_data_rising_sorted_by_retrieval_count(tmp_path: Path) -> None:
    store = _make_store(tmp_path)
    _seed_chunks(store)

    data = store.trust_drift_data(pipeline_id="pipe_td")
    rising = data["rising"]

    assert len(rising) >= 2
    assert rising[0]["retrieval_count"] >= rising[1]["retrieval_count"]


def test_trust_drift_data_falling_sorted_by_dissent_count(tmp_path: Path) -> None:
    store = _make_store(tmp_path)
    _seed_chunks(store)

    data = store.trust_drift_data(pipeline_id="pipe_td")
    falling = data["falling"]

    assert len(falling) >= 1
    assert falling[0]["chunk_id"].startswith("falling_1")
    assert falling[0]["dissent_count"] == 3


def test_trust_drift_data_feedback_summary_counts(tmp_path: Path) -> None:
    store = _make_store(tmp_path)
    _seed_chunks(store)

    data = store.trust_drift_data(pipeline_id="pipe_td")
    fs = data["feedback_summary"]

    assert fs["total_chunks"] == 4
    assert fs["with_retrievals"] >= 2
    assert fs["with_dissents"] >= 1
    assert fs["total_chunks"] == fs["with_retrievals"] + fs["untouched"] or fs["total_chunks"] >= fs["with_dissents"]


def test_trust_drift_data_trust_distribution_covers_bands(tmp_path: Path) -> None:
    store = _make_store(tmp_path)
    _seed_chunks(store)

    data = store.trust_drift_data(pipeline_id="pipe_td")
    dist = data["trust_distribution"]

    total = sum(d["count"] for d in dist)
    assert total == 4
    bands = {d["band"] for d in dist}
    assert len(bands) >= 1


def test_trust_drift_data_empty_store(tmp_path: Path) -> None:
    store = _make_store(tmp_path)

    data = store.trust_drift_data()

    assert data["feedback_summary"]["total_chunks"] == 0
    assert data["rising"] == []
    assert data["falling"] == []
    assert data["trust_distribution"] == []


def test_trust_drift_data_drift_timeline(tmp_path: Path) -> None:
    store = _make_store(tmp_path)
    store.log_drift_history(session_id="sess_1", turn=1, drift_score=0.1)
    store.log_drift_history(session_id="sess_1", turn=2, drift_score=0.3)

    data = store.trust_drift_data()
    timeline = data["drift_timeline"]

    assert len(timeline) == 2
    assert timeline[0]["drift_score"] == 0.3
    assert timeline[1]["drift_score"] == 0.1


def test_trust_drift_data_respects_top_k(tmp_path: Path) -> None:
    store = _make_store(tmp_path)
    for i in range(15):
        store.write(
            SubconsciousChunk(
                chunk_id=f"bulk_{i}",
                layer="episodic",
                content=f"bulk chunk number {i} unique content",
                src="agent_inferred",
                base_trust=0.7,
                pipeline_id="pipe_bulk",
            )
        )
        store.query(f"bulk chunk number {i}", k=4, min_score=0.0, pipeline_id="pipe_bulk")

    data = store.trust_drift_data(pipeline_id="pipe_bulk", top_k=5)

    assert len(data["rising"]) <= 5


# ── CLI tests ───────────────────────────────────────────────────────────


def test_cli_trust_drift_renders_table(tmp_path: Path) -> None:
    runner = CliRunner()
    runner.invoke(main, ["init", "--cwd", str(tmp_path)])
    store = _make_store(tmp_path)
    _seed_chunks(store)

    result = runner.invoke(main, ["trust-drift", "--cwd", str(tmp_path)])

    assert result.exit_code == 0
    assert "NCP Trust Drift" in result.output
    assert "Trust Distribution" in result.output
    assert "Rising" in result.output
    assert "Falling" in result.output
    assert "Feedback Summary" in result.output


def test_cli_trust_drift_json_output(tmp_path: Path) -> None:
    runner = CliRunner()
    runner.invoke(main, ["init", "--cwd", str(tmp_path)])
    store = _make_store(tmp_path)
    _seed_chunks(store)

    result = runner.invoke(
        main,
        ["trust-drift", "--cwd", str(tmp_path), "--pipeline-id", "pipe_td", "--json-output"],
    )

    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["pipeline_id"] == "pipe_td"
    assert "trust_distribution" in payload
    assert "rising" in payload
    assert "falling" in payload
    assert payload["feedback_summary"]["total_chunks"] == 4


def test_cli_trust_drift_empty_store(tmp_path: Path) -> None:
    runner = CliRunner()
    runner.invoke(main, ["init", "--cwd", str(tmp_path)])

    result = runner.invoke(main, ["trust-drift", "--cwd", str(tmp_path)])

    assert result.exit_code == 0
    assert "NCP Trust Drift" in result.output


def test_cli_trust_drift_with_pipeline_filter(tmp_path: Path) -> None:
    runner = CliRunner()
    runner.invoke(main, ["init", "--cwd", str(tmp_path)])
    store = _make_store(tmp_path)
    _seed_chunks(store)
    store.write(
        SubconsciousChunk(
            chunk_id="other_pipe",
            layer="episodic",
            content="chunk in different pipeline",
            src="agent_inferred",
            base_trust=0.7,
            pipeline_id="pipe_other",
        )
    )

    result = runner.invoke(
        main,
        ["trust-drift", "--cwd", str(tmp_path), "--pipeline-id", "pipe_td", "--json-output"],
    )

    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["feedback_summary"]["total_chunks"] == 4
