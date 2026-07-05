"""Tests for CAP-T4: reputation-weighted retrieval + whisper gating."""

from __future__ import annotations

from pathlib import Path
import json
import time

import pytest

from ncp.stores.retrieval import blend_trust


# ---------------------------------------------------------------------------
# blend_trust — the single shared CAP-T4 blending formula used by
# SQLiteStore, PgvectorStore, and AsyncPgvectorStore at query time.
# ---------------------------------------------------------------------------

class TestBlendTrust:

    def test_weight_zero_is_pass_through(self):
        # (alpha, beta) = (9, 1) -> confidence 0.9, ignored at weight 0.
        assert blend_trust(0.7, (9.0, 1.0), 0.0) == 0.7

    def test_weight_one_overrides_fully(self):
        # confidence = 95 / (95 + 5) = 0.95
        assert blend_trust(0.2, (95.0, 5.0), 1.0) == pytest.approx(0.95)

    def test_blend_half(self):
        # confidence = 8 / (8 + 2) = 0.8 -> (1-0.5)*0.6 + 0.5*0.8 = 0.7
        assert blend_trust(0.6, (8.0, 2.0), 0.5) == pytest.approx(0.7)

    def test_unknown_author_preserves_base_trust(self):
        assert blend_trust(0.5, None, 0.8) == 0.5

    def test_mixed_known_and_unknown_authors(self):
        rep: dict[str, tuple[float, float]] = {
            "alice": (9.0, 1.0),  # confidence 0.9
            "bob": (2.0, 8.0),    # confidence 0.2
        }
        # alice: (1-0.5)*0.3 + 0.5*0.9 = 0.15 + 0.45 = 0.6
        assert blend_trust(0.3, rep.get("alice"), 0.5) == pytest.approx(0.6)
        # bob: (1-0.5)*0.7 + 0.5*0.2 = 0.35 + 0.1 = 0.45
        assert blend_trust(0.7, rep.get("bob"), 0.5) == pytest.approx(0.45)
        # unknown: unchanged
        assert blend_trust(0.9, rep.get("unknown"), 0.5) == 0.9

    def test_negative_weight_treated_as_zero(self):
        assert blend_trust(0.7, (3.0, 7.0), -0.1) == 0.7

    def test_clamps_to_unit_interval(self):
        # An out-of-range base_trust cannot push the blend above 1.0.
        assert blend_trust(1.5, (9.0, 1.0), 0.5) == pytest.approx(1.0)

    def test_degenerate_reputation_evidence_is_ignored(self):
        # alpha + beta <= 0 would divide by zero; base_trust passes through.
        assert blend_trust(0.4, (0.0, 0.0), 0.5) == 0.4

    def test_stores_share_the_helper(self):
        """The formula must live in exactly one place: every store backend's
        query path calls ncp.stores.retrieval.blend_trust."""
        import ast
        import inspect

        from ncp.stores import pgvector, pgvector_async, sqlite as sqlite_store

        for module in (sqlite_store, pgvector, pgvector_async):
            source = inspect.getsource(module)
            tree = ast.parse(source)
            calls = [
                node for node in ast.walk(tree)
                if isinstance(node, ast.Call)
                and (
                    (isinstance(node.func, ast.Name) and node.func.id == "blend_trust")
                    or (isinstance(node.func, ast.Attribute) and node.func.attr == "blend_trust")
                )
            ]
            assert calls, f"{module.__name__} must call the shared blend_trust helper"


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
        import os
        import tempfile

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
        import os
        import tempfile

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
        import os
        import tempfile

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
        import os
        import tempfile

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

