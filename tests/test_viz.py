"""Tests for ncp viz — operator view command (Slice 3)."""

from __future__ import annotations

import time
from pathlib import Path


from ncp.stores.sqlite import SQLiteStore
from ncp.types import AlertPayload, SubconsciousChunk, Whisper


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_store(tmp_path: Path) -> SQLiteStore:
    return SQLiteStore(tmp_path / "test.db")


def _chunk(
    *,
    content: str,
    layer: str = "episodic",
    zone: str = "working",
    pipeline_id: str | None = "pipe1",
    base_trust: float = 0.7,
    src: str = "agent_inferred",
    written_by: str = "agent_x",
) -> SubconsciousChunk:
    return SubconsciousChunk(
        layer=layer,
        content=content,
        src=src,
        written_by=written_by,
        base_trust=base_trust,
        pipeline_id=pipeline_id,
        zone=zone,
    )


# ---------------------------------------------------------------------------
# Structure tests
# ---------------------------------------------------------------------------

class TestVizDataStructure:
    def test_returns_all_required_keys(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        data = store.viz_data()
        required_keys = {"chunk_distribution", "age_brackets", "top_chunks", "pipeline_summary", "whisper_queue"}
        assert required_keys == set(data.keys())

    def test_empty_store_zero_counts_no_error(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        data = store.viz_data()
        assert data["chunk_distribution"] == []
        assert data["age_brackets"] == []
        assert data["top_chunks"] == []
        assert data["pipeline_summary"] == []
        wq = data["whisper_queue"]
        assert isinstance(wq, dict)
        assert int(wq["total"]) == 0  # type: ignore[arg-type]
        assert wq["by_type"] == {}

    def test_chunk_distribution_item_keys(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        store.write(_chunk(content="alpha chunk content"))
        data = store.viz_data()
        assert len(data["chunk_distribution"]) > 0
        row = data["chunk_distribution"][0]
        assert "layer" in row
        assert "zone" in row
        assert "count" in row

    def test_age_bracket_item_keys(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        store.write(_chunk(content="beta chunk for age bracket"))
        data = store.viz_data()
        assert len(data["age_brackets"]) > 0
        row = data["age_brackets"][0]
        assert "bracket" in row
        assert "count" in row
        assert "avg_trust" in row
        assert "top_layer" in row

    def test_top_chunk_item_keys(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        store.write(_chunk(content="gamma chunk for top chunk keys"))
        data = store.viz_data()
        assert len(data["top_chunks"]) > 0
        row = data["top_chunks"][0]
        assert "chunk_id" in row
        assert "layer" in row
        assert "zone" in row
        assert "pipeline_id" in row
        assert "base_trust" in row
        assert "age_seconds" in row

    def test_whisper_queue_structure(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        data = store.viz_data()
        wq = data["whisper_queue"]
        assert "total" in wq
        assert "by_type" in wq
        assert isinstance(wq["by_type"], dict)


# ---------------------------------------------------------------------------
# Chunk distribution accuracy
# ---------------------------------------------------------------------------

class TestChunkDistribution:
    def test_counts_accurate_after_writes(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        store.write(_chunk(content="episodic chunk one unique", layer="episodic"))
        store.write(_chunk(content="procedural chunk unique different", layer="procedural"))
        store.write(_chunk(content="semantic chunk unique content here", layer="semantic"))

        data = store.viz_data()
        dist = {(r["layer"], r["zone"]): r["count"] for r in data["chunk_distribution"]}
        assert dist[("episodic", "working")] == 1
        assert dist[("procedural", "working")] == 1
        assert dist[("semantic", "working")] == 1

    def test_multiple_zones(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        store.write(_chunk(content="working zone chunk abc", zone="working"))
        # proven zone requires expiry
        proven_chunk = SubconsciousChunk(
            layer="episodic",
            content="proven zone chunk xyz unique",
            src="agent_inferred",
            written_by="agent_x",
            base_trust=0.7,
            pipeline_id="pipe1",
            zone="proven",
            expiry=time.time() + 86400,
        )
        store.write(proven_chunk)

        data = store.viz_data()
        dist = {(r["layer"], r["zone"]): r["count"] for r in data["chunk_distribution"]}
        assert ("episodic", "working") in dist
        assert ("episodic", "proven") in dist

    def test_tombstoned_chunks_excluded(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        c = _chunk(content="chunk to be tombstoned unique abc123")
        store.write(c)
        store.tombstone(c.chunk_id)

        data = store.viz_data()
        total_count = sum(r["count"] for r in data["chunk_distribution"])
        assert total_count == 0

    def test_pipeline_filter(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        store.write(_chunk(content="pipe1 chunk unique abc", pipeline_id="pipe1"))
        store.write(_chunk(content="pipe2 chunk unique def xyz", pipeline_id="pipe2"))

        data = store.viz_data(pipeline_id="pipe1")
        total = sum(r["count"] for r in data["chunk_distribution"])
        assert total == 1


# ---------------------------------------------------------------------------
# Age brackets
# ---------------------------------------------------------------------------

class TestAgeBrackets:
    def test_recent_chunks_in_lt1h_bracket(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        store.write(_chunk(content="fresh chunk under one hour"))
        data = store.viz_data()
        brackets = {r["bracket"]: r["count"] for r in data["age_brackets"]}
        assert brackets.get("<1h", 0) == 1

    def test_bracket_count_matches_chunk_count(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        store.write(_chunk(content="fresh chunk age bracket test one"))
        store.write(_chunk(content="fresh chunk age bracket test two different"))
        data = store.viz_data()
        total_in_brackets = sum(r["count"] for r in data["age_brackets"])
        total_in_dist = sum(r["count"] for r in data["chunk_distribution"])
        assert total_in_brackets == total_in_dist

    def test_avg_trust_reasonable(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        store.write(_chunk(content="high trust chunk unique", base_trust=0.9))
        store.write(_chunk(content="low trust chunk unique here", base_trust=0.1))
        data = store.viz_data()
        assert len(data["age_brackets"]) > 0
        avg_trust = data["age_brackets"][0]["avg_trust"]
        assert 0.0 <= float(avg_trust) <= 1.0

    def test_top_layer_present(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        store.write(_chunk(content="episodic for top layer test unique"))
        data = store.viz_data()
        assert len(data["age_brackets"]) > 0
        bracket = data["age_brackets"][0]
        assert bracket["top_layer"] != ""


# ---------------------------------------------------------------------------
# Top chunks sorting
# ---------------------------------------------------------------------------

class TestTopChunks:
    def test_sorted_by_trust_desc(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        for trust, label in [(0.3, "low"), (0.9, "high"), (0.6, "mid"), (0.1, "vlow"), (0.8, "vhigh")]:
            store.write(_chunk(
                content=f"chunk with trust {trust} label {label} unique content abc",
                base_trust=trust,
                layer="episodic",
            ))
        data = store.viz_data()
        trusts = [float(r["base_trust"]) for r in data["top_chunks"]]
        assert trusts == sorted(trusts, reverse=True), "top_chunks must be sorted by base_trust DESC"

    def test_at_most_5_chunks(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        for i in range(8):
            store.write(_chunk(
                content=f"chunk number {i} with very unique content zyx abc{i}",
                base_trust=0.5 + i * 0.05,
            ))
        data = store.viz_data()
        assert len(data["top_chunks"]) <= 5

    def test_chunk_id_truncated_to_16(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        store.write(_chunk(content="chunk for id truncation test unique"))
        data = store.viz_data()
        for row in data["top_chunks"]:
            assert len(str(row["chunk_id"])) <= 16

    def test_tombstoned_chunks_not_in_top(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        c = _chunk(content="chunk to tombstone unique xyz", base_trust=0.99)
        store.write(c)
        store.tombstone(c.chunk_id)

        data = store.viz_data()
        chunk_ids = [r["chunk_id"] for r in data["top_chunks"]]
        assert c.chunk_id[:16] not in chunk_ids


# ---------------------------------------------------------------------------
# Pipeline summary
# ---------------------------------------------------------------------------

class TestPipelineSummary:
    def test_shows_correct_pipeline_ids(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        store.write(_chunk(content="pipeline alpha chunk unique", pipeline_id="alpha"))
        store.write(_chunk(content="pipeline beta chunk unique diff", pipeline_id="beta"))

        data = store.viz_data()
        pipe_ids = {r["pipeline_id"] for r in data["pipeline_summary"]}
        assert "alpha" in pipe_ids
        assert "beta" in pipe_ids

    def test_chunk_count_per_pipeline(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        store.write(_chunk(content="alpha 1 unique abc", pipeline_id="alpha"))
        store.write(_chunk(content="alpha 2 unique xyz different", pipeline_id="alpha"))
        store.write(_chunk(content="beta only unique def", pipeline_id="beta"))

        data = store.viz_data()
        by_pipe = {r["pipeline_id"]: r["chunk_count"] for r in data["pipeline_summary"]}
        assert by_pipe["alpha"] == 2
        assert by_pipe["beta"] == 1

    def test_empty_when_no_named_pipelines(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        store.write(_chunk(content="no pipeline chunk unique", pipeline_id=None))
        data = store.viz_data()
        assert data["pipeline_summary"] == []

    def test_pipeline_filter_scopes_correctly(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        store.write(_chunk(content="pipe1 only chunk unique abc", pipeline_id="pipe1"))
        store.write(_chunk(content="pipe2 only chunk unique xyz", pipeline_id="pipe2"))

        data = store.viz_data(pipeline_id="pipe1")
        if data["pipeline_summary"]:
            pipe_ids = {r["pipeline_id"] for r in data["pipeline_summary"]}
            assert "pipe2" not in pipe_ids


# ---------------------------------------------------------------------------
# Whisper queue
# ---------------------------------------------------------------------------

class TestWhisperQueue:
    def test_counts_pending_whispers(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        store.emit_whisper(Whisper(
            from_agent="a", target="b", whisper_type="nudge",
            payload="msg1", confidence=0.9, pipeline_id=None,
        ))
        store.emit_whisper(Whisper(
            from_agent="a", target="b", whisper_type="alert",
            payload=AlertPayload(alert_code="msg2", description="different"), confidence=0.95, pipeline_id=None,
        ))
        data = store.viz_data()
        wq = data["whisper_queue"]
        assert int(wq["total"]) == 2  # type: ignore[arg-type]

    def test_by_type_breakdown(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        store.emit_whisper(Whisper(
            from_agent="a", target="b", whisper_type="nudge",
            payload="nudge msg", confidence=0.9, pipeline_id=None,
        ))
        store.emit_whisper(Whisper(
            from_agent="a", target="b", whisper_type="nudge",
            payload="nudge msg 2 different", confidence=0.9, pipeline_id=None,
        ))
        store.emit_whisper(Whisper(
            from_agent="a", target="b", whisper_type="alert",
            payload=AlertPayload(alert_code="alert_msg", description="unique"), confidence=0.95, pipeline_id=None,
        ))
        data = store.viz_data()
        by_type = data["whisper_queue"]["by_type"]
        assert isinstance(by_type, dict)
        assert by_type.get("nudge", 0) == 2
        assert by_type.get("alert", 0) == 1

    def test_empty_when_no_whispers(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        data = store.viz_data()
        wq = data["whisper_queue"]
        assert int(wq["total"]) == 0  # type: ignore[arg-type]
        assert wq["by_type"] == {}
