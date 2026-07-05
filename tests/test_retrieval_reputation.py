"""Tests for CAP-T4: reputation-weighted retrieval + whisper gating."""

from __future__ import annotations

from pathlib import Path
from typing import Callable
import json
import time

import pytest

from ncp.stores.retrieval import apply_reputation_weight
from ncp.types import SubconsciousChunk, Whisper


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_chunk(*, written_by: str = "alice", base_trust: float = 0.7) -> SubconsciousChunk:
    return SubconsciousChunk(
        layer="semantic",
        content="test content",
        src="tool_result",
        written_by=written_by,
        base_trust=base_trust,
    )


# ---------------------------------------------------------------------------
# apply_reputation_weight
# ---------------------------------------------------------------------------

class TestApplyReputationWeight:

    def test_weight_zero_is_pass_through(self):
        chunks = [_make_chunk(written_by="alice", base_trust=0.7)]
        lookup: Callable[[str], float | None] = lambda _: 0.9
        result = apply_reputation_weight(chunks, lookup, reputation_weight=0.0)
        assert result[0].base_trust == 0.7

    def test_weight_one_overrides_fully(self):
        chunks = [_make_chunk(written_by="alice", base_trust=0.2)]
        lookup: Callable[[str], float | None] = lambda _: 0.95
        result = apply_reputation_weight(chunks, lookup, reputation_weight=1.0)
        assert result[0].base_trust == pytest.approx(0.95)

    def test_blend_half(self):
        chunks = [_make_chunk(written_by="alice", base_trust=0.6)]
        lookup: Callable[[str], float | None] = lambda _: 0.8
        result = apply_reputation_weight(chunks, lookup, reputation_weight=0.5)
        assert result[0].base_trust == pytest.approx(0.7)

    def test_unknown_author_preserves_base_trust(self):
        chunks = [_make_chunk(written_by="unknown", base_trust=0.5)]
        lookup: Callable[[str], float | None] = lambda _: None
        result = apply_reputation_weight(chunks, lookup, reputation_weight=0.8)
        assert result[0].base_trust == 0.5

    def test_mixed_known_and_unknown_authors(self):
        chunks = [
            _make_chunk(written_by="alice", base_trust=0.3),
            _make_chunk(written_by="bob", base_trust=0.7),
            _make_chunk(written_by="unknown", base_trust=0.9),
        ]
        rep: dict[str, float] = {"alice": 0.9, "bob": 0.2}
        def lookup(author: str) -> float | None:
            return rep.get(author)

        result = apply_reputation_weight(chunks, lookup, reputation_weight=0.5)
        # alice: (1-0.5)*0.3 + 0.5*0.9 = 0.15 + 0.45 = 0.6
        assert result[0].base_trust == pytest.approx(0.6)
        # bob: (1-0.5)*0.7 + 0.5*0.2 = 0.35 + 0.1 = 0.45
        assert result[1].base_trust == pytest.approx(0.45)
        # unknown: unchanged
        assert result[2].base_trust == 0.9

    def test_negative_weight_treated_as_zero(self):
        chunks = [_make_chunk(written_by="alice", base_trust=0.7)]
        lookup: Callable[[str], float | None] = lambda _: 0.3
        result = apply_reputation_weight(chunks, lookup, reputation_weight=-0.1)
        assert result[0].base_trust == 0.7

    def test_clamps_to_unit_interval(self):
        chunks = [_make_chunk(written_by="alice", base_trust=0.1)]
        lookup: Callable[[str], float | None] = lambda _: 1.2
        result = apply_reputation_weight(chunks, lookup, reputation_weight=1.0)
        assert result[0].base_trust == pytest.approx(1.0)


# ---------------------------------------------------------------------------
# Whisper gating — SQLiteStore integration
# ---------------------------------------------------------------------------

class TestSqliteWhisperGating:

    def _insert_reputation(self, store, identity_id: str, alpha: float, beta: float) -> None:
        with store._connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO reputation (identity_id, alpha, beta, obs_count, last_updated)"
                " VALUES (?, ?, ?, 1, ?)",
                (identity_id, alpha, beta, time.time()),
            )

    def _insert_whisper(
        self, store, *, from_agent: str, target: str = "agent_x",
        pipeline_id: str | None = None,
    ) -> str:
        from ncp.types import Whisper
        w = Whisper(
            from_agent=from_agent,
            target=target,
            whisper_type="nudge",
            payload=json.dumps({"msg": "hello"}),
            confidence=1.0,
            pipeline_id=pipeline_id,
        )
        store.emit_whisper(w)
        return w.whisper_id

    def test_gating_disabled_by_default(self):
        from ncp.stores.sqlite import SQLiteStore
        from ncp.config import NCPConfig
        import tempfile, os

        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            db_path = f.name
        try:
            config = NCPConfig(values={}, project_root=Path(db_path).parent)
            store = SQLiteStore(db_path, config=config)
            self._insert_whisper(store, from_agent="low_rep_agent")
            self._insert_reputation(store, "low_rep_agent", 1.0, 99.0)

            # Default min_author_reputation=0.0 -> no filtering -> whisper returned
            drained = store.drain_whispers(agent_id="agent_x", max_items=10)
            assert len(drained) == 1
        finally:
            os.unlink(db_path)

    def test_gating_filters_low_reputation(self):
        from ncp.stores.sqlite import SQLiteStore
        from ncp.config import NCPConfig
        import tempfile, os

        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            db_path = f.name
        try:
            cfg_values = {
                "whispers": {"min_author_reputation": 0.7},
            }
            config = NCPConfig(values=cfg_values, project_root=Path(db_path).parent)
            store = SQLiteStore(db_path, config=config)
            # reputation confidence: alpha/(alpha+beta) = 1/(1+99) = 0.01 < 0.7
            self._insert_reputation(store, "low_rep_agent", 1.0, 99.0)
            self._insert_whisper(store, from_agent="low_rep_agent")

            drained = store.drain_whispers(agent_id="agent_x", max_items=10)
            assert len(drained) == 0
        finally:
            os.unlink(db_path)

    def test_gating_passes_high_reputation(self):
        from ncp.stores.sqlite import SQLiteStore
        from ncp.config import NCPConfig
        import tempfile, os

        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            db_path = f.name
        try:
            cfg_values = {
                "whispers": {"min_author_reputation": 0.7},
            }
            config = NCPConfig(values=cfg_values, project_root=Path(db_path).parent)
            store = SQLiteStore(db_path, config=config)
            # confidence = 95/(95+5) = 0.95 > 0.7
            self._insert_reputation(store, "high_rep_agent", 95.0, 5.0)
            self._insert_whisper(store, from_agent="high_rep_agent")

            drained = store.drain_whispers(agent_id="agent_x", max_items=10)
            assert len(drained) == 1
        finally:
            os.unlink(db_path)

    def test_unknown_author_passes_default_threshold(self):
        """Unknown author has uniform prior Beta(1,1)=0.5; passes 0.3 threshold."""
        from ncp.stores.sqlite import SQLiteStore
        from ncp.config import NCPConfig
        import tempfile, os

        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            db_path = f.name
        try:
            cfg_values = {
                "whispers": {"min_author_reputation": 0.3},
            }
            config = NCPConfig(values=cfg_values, project_root=Path(db_path).parent)
            store = SQLiteStore(db_path, config=config)
            # No reputation record for this author
            self._insert_whisper(store, from_agent="no_reputation_agent")

            drained = store.drain_whispers(agent_id="agent_x", max_items=10)
            assert len(drained) == 1
        finally:
            os.unlink(db_path)

