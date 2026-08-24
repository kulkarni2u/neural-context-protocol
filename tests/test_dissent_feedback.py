"""Tests for dissent-driven trust penalties and their propagation along caused_by."""

import json
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from ncp.config import NCPConfig
from ncp.mcp.server import make_handlers, _handle_request
from ncp.stores.calibration import FeedbackRow, compute_feedback_updates
from ncp.stores.sqlite import SQLiteStore
from ncp.types import SubconsciousChunk


# ── pure helper: penalties and net deltas ─────────────────────────────────────


def test_dissent_applies_direct_penalty() -> None:
    rows = [FeedbackRow(chunk_id="bad", base_trust=0.7, retrieval_count=0, dissent_count=3)]
    result = compute_feedback_updates(rows, feedback_weight=0.15, propagation_factor=0.5, dissent_weight=0.2)

    by_id = {cid: trust for trust, cid in result.updates}
    assert abs(by_id["bad"] - 0.5) < 1e-9  # 0.7 - 0.2 (full penalty at 3 dissents)
    assert result.change_log[0]["reason"] == "dissent_penalty"
    assert result.change_log[0]["dissent_count"] == 3


def test_dissent_penalty_propagates_to_cause() -> None:
    rows = [
        FeedbackRow(chunk_id="effect", base_trust=0.7, retrieval_count=0, dissent_count=3, caused_by="cause"),
        FeedbackRow(chunk_id="cause", base_trust=0.7, retrieval_count=0, dissent_count=0),
    ]
    result = compute_feedback_updates(rows, feedback_weight=0.15, propagation_factor=0.5, dissent_weight=0.2)

    by_id = {cid: trust for trust, cid in result.updates}
    # effect: 0.7 - 0.2 = 0.5 ; cause debited 0.2*0.5 = 0.1 → 0.6
    assert abs(by_id["effect"] - 0.5) < 1e-9
    assert abs(by_id["cause"] - 0.6) < 1e-9
    reasons = {e["chunk_id"]: e["reason"] for e in result.change_log}
    assert reasons["cause"] == "trust_propagation"


def test_retrieval_and_dissent_net_out() -> None:
    # Retrieved 10x (+0.15) and disputed 3x (-0.2) → net -0.05.
    rows = [FeedbackRow(chunk_id="mixed", base_trust=0.6, retrieval_count=10, dissent_count=3)]
    result = compute_feedback_updates(rows, feedback_weight=0.15, propagation_factor=0.5, dissent_weight=0.2)

    by_id = {cid: trust for trust, cid in result.updates}
    assert abs(by_id["mixed"] - 0.55) < 1e-9
    entry = result.change_log[0]
    assert entry["reason"] == "mixed_feedback"
    assert entry["retrieval_count"] == 10
    assert entry["dissent_count"] == 3


def test_penalty_floors_at_zero() -> None:
    rows = [FeedbackRow(chunk_id="bad", base_trust=0.1, retrieval_count=0, dissent_count=3)]
    result = compute_feedback_updates(rows, feedback_weight=0.15, propagation_factor=0.5, dissent_weight=0.2)

    by_id = {cid: trust for trust, cid in result.updates}
    assert by_id["bad"] == 0.0  # clamped, not negative


def test_dissent_disabled_when_weight_zero() -> None:
    rows = [FeedbackRow(chunk_id="bad", base_trust=0.7, retrieval_count=0, dissent_count=5)]
    result = compute_feedback_updates(rows, feedback_weight=0.15, propagation_factor=0.5, dissent_weight=0.0)

    assert result.updates == []
    assert result.skipped == 1


# ── SQLite integration ────────────────────────────────────────────────────────


def _chunk(chunk_id: str, content: str, **kw: object) -> SubconsciousChunk:
    base: dict = {
        "chunk_id": chunk_id,
        "layer": "episodic",
        "content": content,
        "src": "agent_inferred",
        "written_by": "test",
        "base_trust": 0.7,
        "pipeline_id": "pipe_1",
    }
    base.update(kw)
    return SubconsciousChunk(**base)


def test_record_dissent_increments_counter(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "store.db")
    store.write(_chunk("disputed", "some claim"))

    assert store.record_dissent("disputed") is True
    assert store.record_dissent("disputed") is True
    assert store.record_dissent("ctx://sub/disputed") is True  # prefix tolerated

    chunk = next(c for c in store.get_working_zone(pipeline_id="pipe_1") if c.chunk_id == "disputed")
    assert chunk.dissent_count == 3


def test_record_dissent_missing_chunk_returns_false(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "store.db")
    assert store.record_dissent("nonexistent") is False


def test_calibrate_penalizes_disputed_chunk_and_cause(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "store.db")
    store.write(_chunk("cause_chunk", "root analysis"))
    store.write(_chunk("effect_chunk", "disputed conclusion", caused_by="cause_chunk"))

    for _ in range(3):
        store.record_dissent("effect_chunk")

    report = store.calibrate(feedback_mode=True, propagation_factor=0.5, dissent_weight=0.2)
    assert report.feedback_adjusted >= 2

    zone = {c.chunk_id: c for c in store.get_working_zone(pipeline_id="pipe_1")}
    assert zone["effect_chunk"].base_trust < 0.7  # penalized
    assert zone["cause_chunk"].base_trust < 0.7   # cause debited via propagation
    assert zone["cause_chunk"].base_trust > zone["effect_chunk"].base_trust  # cause penalized less


# ── MCP end-to-end ────────────────────────────────────────────────────────────


def _call(name: str, arguments: dict) -> dict:
    return {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
            "params": {"name": name, "arguments": arguments}}


def _content(response_str: str) -> dict:
    r = json.loads(response_str)["result"]
    return json.loads(r["content"][0]["text"])


def test_emit_dissent_whisper_with_ref_records_dissent(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "store.db")
    store.write(_chunk("target_chunk", "the disputed claim"))
    handlers = make_handlers(store)

    result = _content(_handle_request(
        _call("ncp_emit_whisper", {
            "from": "reviewer",
            "target": "fixer",
            "type": "dissent",
            "payload": json.dumps({"issue": "wrong guard", "alternatives": ["use Optional"]}),
            "confidence": 0.9,
            "pipeline_id": "pipe_1",
            "ref": "target_chunk",
        }),
        handlers,
    ))

    assert result["emitted"] is True
    assert result["dissent_recorded"] is True
    chunk = next(c for c in store.get_working_zone(pipeline_id="pipe_1") if c.chunk_id == "target_chunk")
    assert chunk.dissent_count == 1


def test_non_dissent_whisper_does_not_record(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "store.db")
    store.write(_chunk("target_chunk", "a claim"))
    handlers = make_handlers(store)

    result = _content(_handle_request(
        _call("ncp_emit_whisper", {
            "from": "a",
            "target": "b",
            "type": "nudge",
            "payload": "fyi",
            "confidence": 0.9,
            "ref": "target_chunk",
        }),
        handlers,
    ))

    assert "dissent_recorded" not in result
    chunk = next(c for c in store.get_working_zone(pipeline_id="pipe_1") if c.chunk_id == "target_chunk")
    assert chunk.dissent_count == 0


# ── CAP-T5: dissent dedup + reputation gating (SQLite) ─────────────────────────


def test_record_dissent_identity_none_is_backward_compatible(tmp_path: Path) -> None:
    """A positional-only call (no identity_id) keeps the pre-CAP-T5 behavior:
    always increments, no dedup -- existing direct callers are unaffected."""
    store = SQLiteStore(tmp_path / "store.db")
    store.write(_chunk("disputed", "some claim"))

    assert store.record_dissent("disputed") is True
    assert store.record_dissent("disputed") is True
    assert store.record_dissent("disputed") is True

    chunk = next(c for c in store.get_working_zone(pipeline_id="pipe_1") if c.chunk_id == "disputed")
    assert chunk.dissent_count == 3


def test_record_dissent_same_identity_dedups(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "store.db")
    store.write(_chunk("disputed", "some claim"))

    assert store.record_dissent("disputed", identity_id="reviewer_a") is True
    assert store.record_dissent("disputed", identity_id="reviewer_a") is True
    assert store.record_dissent("disputed", identity_id="reviewer_a") is True

    chunk = next(c for c in store.get_working_zone(pipeline_id="pipe_1") if c.chunk_id == "disputed")
    assert chunk.dissent_count == 1  # only the first dissent from this identity counted

    with store._connect() as conn:
        rows = conn.execute(
            "SELECT chunk_id, identity_id FROM dissent_log WHERE chunk_id = ?", ("disputed",)
        ).fetchall()
    assert len(rows) == 1  # dedup key row exists exactly once, not once per call


def test_record_dissent_different_identities_each_count(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "store.db")
    store.write(_chunk("disputed", "some claim"))

    assert store.record_dissent("disputed", identity_id="reviewer_a") is True
    assert store.record_dissent("disputed", identity_id="reviewer_b") is True
    assert store.record_dissent("disputed", identity_id="reviewer_c") is True

    chunk = next(c for c in store.get_working_zone(pipeline_id="pipe_1") if c.chunk_id == "disputed")
    assert chunk.dissent_count == 3


def test_record_dissent_with_identity_missing_chunk_returns_false(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "store.db")
    assert store.record_dissent("nonexistent", identity_id="reviewer_a") is False


def _config_with_dissent_threshold(threshold: float) -> NCPConfig:
    return NCPConfig(
        values={"whispers": {"dissent_min_author_reputation": threshold}},
        project_root=Path("."),
    )


def _insert_reputation(store: SQLiteStore, identity_id: str, alpha: float, beta: float) -> None:
    with store._connect() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO reputation (identity_id, alpha, beta, obs_count, last_updated)"
            " VALUES (?, ?, ?, 1, ?)",
            (identity_id, alpha, beta, time.time()),
        )


def test_dissent_reputation_gating_disabled_by_default(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "store.db")
    store.write(_chunk("disputed", "some claim"))
    _insert_reputation(store, "low_rep", 1.0, 99.0)  # confidence 0.01

    assert store.record_dissent("disputed", identity_id="low_rep") is True

    chunk = next(c for c in store.get_working_zone(pipeline_id="pipe_1") if c.chunk_id == "disputed")
    assert chunk.dissent_count == 1  # threshold is 0.0 (off) -> counts as always


def test_dissent_reputation_gating_suppresses_below_threshold(tmp_path: Path) -> None:
    config = _config_with_dissent_threshold(0.5)
    store = SQLiteStore(tmp_path / "store.db", config=config)
    store.write(_chunk("disputed", "some claim"))
    _insert_reputation(store, "low_rep", 1.0, 99.0)  # confidence 0.01 < 0.5

    assert store.record_dissent("disputed", identity_id="low_rep") is True  # dedup-recorded

    chunk = next(c for c in store.get_working_zone(pipeline_id="pipe_1") if c.chunk_id == "disputed")
    assert chunk.dissent_count == 0  # but does not debit trust

    # Recorded in dissent_log even though it didn't count -- a repeat call
    # from the same low-rep identity is still a no-op, not a second attempt.
    with store._connect() as conn:
        rows = conn.execute(
            "SELECT 1 FROM dissent_log WHERE chunk_id = ? AND identity_id = ?", ("disputed", "low_rep")
        ).fetchall()
    assert len(rows) == 1


def test_dissent_reputation_gating_counts_above_threshold(tmp_path: Path) -> None:
    config = _config_with_dissent_threshold(0.5)
    store = SQLiteStore(tmp_path / "store.db", config=config)
    store.write(_chunk("disputed", "some claim"))
    _insert_reputation(store, "high_rep", 95.0, 5.0)  # confidence 0.95 >= 0.5

    assert store.record_dissent("disputed", identity_id="high_rep") is True

    chunk = next(c for c in store.get_working_zone(pipeline_id="pipe_1") if c.chunk_id == "disputed")
    assert chunk.dissent_count == 1


def test_dissent_reputation_gating_unknown_identity_passes_default_prior(tmp_path: Path) -> None:
    """Unknown identity uses the uniform Beta(1,1)=0.5 prior, same as whisper gating."""
    config = _config_with_dissent_threshold(0.3)
    store = SQLiteStore(tmp_path / "store.db", config=config)
    store.write(_chunk("disputed", "some claim"))

    assert store.record_dissent("disputed", identity_id="never_seen") is True

    chunk = next(c for c in store.get_working_zone(pipeline_id="pipe_1") if c.chunk_id == "disputed")
    assert chunk.dissent_count == 1


# ── CAP-T5: dissent dedup via the MCP tool (ncp_emit_whisper) ──────────────────


def test_emit_whisper_dissent_dedups_same_sender(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "store.db")
    store.write(_chunk("target_chunk", "the disputed claim"))
    handlers = make_handlers(store)

    for _ in range(3):
        result = _content(_handle_request(
            _call("ncp_emit_whisper", {
                "from": "reviewer",
                "target": "fixer",
                "type": "dissent",
                "payload": json.dumps({"issue": "wrong guard", "alternatives": ["use Optional"]}),
                "confidence": 0.9,
                "pipeline_id": "pipe_1",
                "ref": "target_chunk",
            }),
            handlers,
        ))
        assert result["dissent_recorded"] is True

    chunk = next(c for c in store.get_working_zone(pipeline_id="pipe_1") if c.chunk_id == "target_chunk")
    assert chunk.dissent_count == 1  # same sender, 3 calls -> only the first counted


def test_emit_whisper_dissent_counts_distinct_senders(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "store.db")
    store.write(_chunk("target_chunk", "the disputed claim"))
    handlers = make_handlers(store)

    for sender in ("reviewer_a", "reviewer_b", "reviewer_c"):
        _content(_handle_request(
            _call("ncp_emit_whisper", {
                "from": sender,
                "target": "fixer",
                "type": "dissent",
                "payload": json.dumps({"issue": "wrong guard", "alternatives": ["use Optional"]}),
                "confidence": 0.9,
                "pipeline_id": "pipe_1",
                "ref": "target_chunk",
            }),
            handlers,
        ))

    chunk = next(c for c in store.get_working_zone(pipeline_id="pipe_1") if c.chunk_id == "target_chunk")
    assert chunk.dissent_count == 3


# ── CAP-T5: PgvectorStore parity (mocked connection, no live Postgres) ─────────


class _FakePgDissentBackend:
    """Minimal in-memory stand-in for the handful of SQL statements
    PgvectorStore.record_dissent issues, keyed by statement prefix. Anything
    else (e.g. the bulk schema-init script) is a no-op -- this only needs to
    prove the dedup + reputation-gating *logic*, not full pgvector fidelity."""

    def __init__(self, chunk_ids: set[str], reputation: dict[str, tuple[float, float]] | None = None) -> None:
        self.chunks = set(chunk_ids)
        self.dissent_log: set[tuple[str, str]] = set()
        self.dissent_count: dict[str, int] = dict.fromkeys(chunk_ids, 0)
        self.reputation = reputation or {}

    def execute(self, sql: str, params: tuple) -> tuple[int, list[dict]]:
        s = sql.strip()
        if s.startswith("SELECT 1 FROM"):
            (chunk_id,) = params
            return (1, [{"?column?": 1}]) if chunk_id in self.chunks else (0, [])
        if s.startswith("INSERT INTO") and "dissent_log" in s:
            chunk_id, identity_id, _created_at = params
            key = (chunk_id, identity_id)
            if key in self.dissent_log:
                return (0, [])
            self.dissent_log.add(key)
            return (1, [])
        if s.startswith("SELECT identity_id, alpha, beta"):
            rows = [
                {"identity_id": iid, "alpha": a, "beta": b}
                for iid in params
                if (rep := self.reputation.get(iid)) is not None
                for a, b in [rep]
            ]
            return (len(rows), rows)
        if "dissent_count = dissent_count + 1" in s:
            (chunk_id,) = params
            if chunk_id in self.chunks:
                self.dissent_count[chunk_id] += 1
                return (1, [])
            return (0, [])
        return (0, [])  # schema-init / anything else: no-op


def _pgvector_store_with_fake_backend(backend: _FakePgDissentBackend, *, config: NCPConfig | None = None):
    from ncp.stores.pgvector import PgvectorStore

    def _cursor_factory():
        cursor = MagicMock()
        cursor.__enter__ = MagicMock(return_value=cursor)
        cursor.__exit__ = MagicMock(return_value=False)

        def _execute(sql, params=()):
            rowcount, rows = backend.execute(sql, params)
            cursor.rowcount = rowcount
            cursor.fetchall = MagicMock(return_value=rows)
            cursor.fetchone = MagicMock(return_value=rows[0] if rows else None)

        cursor.execute = MagicMock(side_effect=_execute)
        cursor.description = []
        return cursor

    mock_conn = MagicMock()
    mock_conn.cursor = MagicMock(side_effect=_cursor_factory)
    mock_conn.__enter__ = MagicMock(return_value=mock_conn)
    mock_conn.__exit__ = MagicMock(return_value=False)

    store = PgvectorStore(
        "postgresql://localhost/test",
        connect_factory=lambda _dsn: mock_conn,
        config=config,
    )
    return store


def test_pgvector_record_dissent_dedups_same_identity() -> None:
    backend = _FakePgDissentBackend({"disputed"})
    store = _pgvector_store_with_fake_backend(backend)

    assert store.record_dissent("disputed", identity_id="reviewer_a") is True
    assert store.record_dissent("disputed", identity_id="reviewer_a") is True
    assert store.record_dissent("disputed", identity_id="reviewer_a") is True

    assert backend.dissent_count["disputed"] == 1


def test_pgvector_record_dissent_distinct_identities_each_count() -> None:
    backend = _FakePgDissentBackend({"disputed"})
    store = _pgvector_store_with_fake_backend(backend)

    for identity in ("reviewer_a", "reviewer_b"):
        assert store.record_dissent("disputed", identity_id=identity) is True

    assert backend.dissent_count["disputed"] == 2


def test_pgvector_record_dissent_reputation_gated() -> None:
    backend = _FakePgDissentBackend({"disputed"}, reputation={"low_rep": (1.0, 99.0)})
    config = _config_with_dissent_threshold(0.5)
    store = _pgvector_store_with_fake_backend(backend, config=config)

    assert store.record_dissent("disputed", identity_id="low_rep") is True

    assert backend.dissent_count["disputed"] == 0  # below threshold -> dedup-recorded, not counted
    assert ("disputed", "low_rep") in backend.dissent_log


def test_pgvector_record_dissent_identity_none_backward_compatible() -> None:
    backend = _FakePgDissentBackend({"disputed"})
    store = _pgvector_store_with_fake_backend(backend)

    assert store.record_dissent("disputed") is True
    assert store.record_dissent("disputed") is True

    assert backend.dissent_count["disputed"] == 2  # no dedup without identity_id


# ── CAP-T5: AsyncPgvectorStore parity (mocked pool, no live Postgres) ──────────


class _AsyncCursorAdapter:
    """Wraps _FakePgDissentBackend.execute for AsyncPgvectorStore's
    `async with conn.cursor() as cur: await cur.execute(...)` usage."""

    def __init__(self, backend: "_FakePgDissentBackend") -> None:
        self._backend = backend
        self.rowcount = 0
        self._rows: list[dict] = []
        self.description = []

    async def execute(self, sql: str, params: tuple = ()) -> None:
        self.rowcount, self._rows = self._backend.execute(sql, params)

    async def fetchall(self) -> list[dict]:
        return self._rows

    async def fetchone(self) -> dict | None:
        return self._rows[0] if self._rows else None

    async def __aenter__(self) -> "_AsyncCursorAdapter":
        return self

    async def __aexit__(self, *exc: object) -> bool:
        return False


def _async_pgvector_store_with_fake_backend(
    backend: _FakePgDissentBackend, *, config: NCPConfig | None = None
):
    from unittest.mock import patch

    from ncp.stores.pgvector_async import AsyncPgvectorStore

    conn = MagicMock()
    conn.cursor = MagicMock(side_effect=lambda: _AsyncCursorAdapter(backend))
    conn.commit = AsyncMock()
    conn.rollback = AsyncMock()
    conn.__aenter__ = AsyncMock(return_value=conn)
    conn.__aexit__ = AsyncMock(return_value=False)

    pool = MagicMock()
    pool.open = AsyncMock()
    pool.close = AsyncMock()
    pool.connection = MagicMock()
    pool.connection.return_value.__aenter__ = AsyncMock(return_value=conn)
    pool.connection.return_value.__aexit__ = AsyncMock(return_value=False)

    with patch("psycopg_pool.AsyncConnectionPool", return_value=pool):
        return AsyncPgvectorStore("postgresql://localhost/test", config=config)


@pytest.mark.anyio
async def test_async_pgvector_record_dissent_dedups_same_identity() -> None:
    pytest.importorskip("psycopg", reason="psycopg extra not installed")
    pytest.importorskip("psycopg_pool", reason="psycopg_pool extra not installed")
    backend = _FakePgDissentBackend({"disputed"})
    store = _async_pgvector_store_with_fake_backend(backend)

    assert await store.async_record_dissent("disputed", identity_id="reviewer_a") is True
    assert await store.async_record_dissent("disputed", identity_id="reviewer_a") is True

    assert backend.dissent_count["disputed"] == 1


@pytest.mark.anyio
async def test_async_pgvector_record_dissent_reputation_gated() -> None:
    pytest.importorskip("psycopg", reason="psycopg extra not installed")
    pytest.importorskip("psycopg_pool", reason="psycopg_pool extra not installed")
    backend = _FakePgDissentBackend({"disputed"}, reputation={"low_rep": (1.0, 99.0)})
    config = _config_with_dissent_threshold(0.5)
    store = _async_pgvector_store_with_fake_backend(backend, config=config)

    assert await store.async_record_dissent("disputed", identity_id="low_rep") is True

    assert backend.dissent_count["disputed"] == 0


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"
