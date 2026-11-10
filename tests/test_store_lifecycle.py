"""Store GC / lifecycle fixes: trust-aware hard GC, [pipeline] GC config
wiring, whisper ack ownership, Redis/SQLite whisper leaks, soft-GC expiry
indexes, and ``vacuum`` retention for append-only tables.

Postgres and Redis are exercised through scripted fakes (no live servers).
"""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path
from typing import Any

import anyio
import pytest

from ncp.assembler import Assembler
import copy

from ncp.config import DEFAULT_CONFIG, NCPConfig
from ncp.stores.base import BaseStore, resolve_gc_limits
from ncp.stores.factory import create_store
from ncp.stores.migrations import MigrationRunner
from ncp.stores.pgvector import PGVECTOR_SCHEMA_TEMPLATE, PgvectorStore
from ncp.stores.pgvector_async import AsyncPgvectorStore
from ncp.stores.redis_coordination import AsyncRedisCoordination, RedisCoordination
from ncp.stores.sqlite import SQLiteStore
from ncp.types import ConsciousBlock, HandoffPayload, NCPResponse, SubconsciousChunk, Whisper


def _chunk(chunk_id: str, *, src: str = "tool_result", trust: float = 0.7, pipeline_id: str | None = "pipe") -> SubconsciousChunk:
    return SubconsciousChunk(
        chunk_id=chunk_id,
        layer="semantic",
        content=f"distinct content for {chunk_id}",
        src=src,
        base_trust=trust,
        pipeline_id=pipeline_id,
    )


def _ids(store: SQLiteStore, sql: str, params: tuple[object, ...] = ()) -> set[str]:
    with sqlite3.connect(store.path) as connection:
        return {str(row[0]) for row in connection.execute(sql, params)}


def _working_ids(store: SQLiteStore, pipeline_id: str | None) -> set[str]:
    if pipeline_id is None:
        return _ids(store, "SELECT chunk_id FROM chunks WHERE zone = 'working' AND pipeline_id IS NULL")
    return _ids(store, "SELECT chunk_id FROM chunks WHERE zone = 'working' AND pipeline_id = ?", (pipeline_id,))


# ---------------------------------------------------------------------------
# 1. Trust-aware hard GC (SQLite)
# ---------------------------------------------------------------------------


def test_hard_gc_never_evicts_user_verified_and_prefers_low_trust(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "store.db", max_working_chunks=5, gc_threshold=3)
    # Oldest first: a FIFO would evict uv_1, uv_2 and low_1.
    for chunk in (
        _chunk("uv_1", src="user_verified", trust=0.2),
        _chunk("uv_2", src="user_verified", trust=0.2),
        _chunk("low_1", trust=0.05),
        _chunk("low_2", trust=0.05),
        _chunk("high_1", trust=0.95),
    ):
        assert store.write(chunk, allow_duplicate=True)
    assert len(_working_ids(store, "pipe")) == 5  # at the cap, no GC yet

    assert store.write(_chunk("high_2", trust=0.95), allow_duplicate=True)

    remaining = _working_ids(store, "pipe")
    assert {"uv_1", "uv_2"} <= remaining
    assert not remaining & {"low_1", "low_2"}
    assert len(remaining) == 3  # evicted down to gc_threshold


def test_hard_gc_keeps_user_verified_even_when_they_alone_exceed_threshold(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "store.db", max_working_chunks=2, gc_threshold=1)
    for index in range(3):
        store.write(_chunk(f"uv_{index}", src="user_verified"), allow_duplicate=True)
    assert _working_ids(store, "pipe") == {"uv_0", "uv_1", "uv_2"}


def test_hard_gc_with_no_pipeline_is_scoped_to_null_pipeline(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "store.db", max_working_chunks=3, gc_threshold=2)
    for index in range(3):
        store.write(_chunk(f"a_{index}", pipeline_id="pipe_a"), allow_duplicate=True)
    for index in range(2):
        store.write(_chunk(f"none_{index}", pipeline_id=None), allow_duplicate=True)
    # Used to count 5 working chunks across every pipeline and evict pipe_a's.
    assert _working_ids(store, "pipe_a") == {"a_0", "a_1", "a_2"}
    assert _working_ids(store, None) == {"none_0", "none_1"}

    for index in range(2, 4):
        store.write(_chunk(f"none_{index}", pipeline_id=None), allow_duplicate=True)
    assert len(_working_ids(store, None)) == 2
    assert _working_ids(store, "pipe_a") == {"a_0", "a_1", "a_2"}


# ---------------------------------------------------------------------------
# 1. Trust-aware hard GC (Postgres, scripted fake connections)
# ---------------------------------------------------------------------------


class _ScriptedCursor:
    def __init__(self, conn: "_ScriptedConn") -> None:
        self._conn = conn
        self._rows: list[dict[str, Any]] = []
        self.description = None
        self.rowcount = 0

    def execute(self, sql: str, params: tuple[object, ...] = ()) -> None:
        normalized = " ".join(sql.split())
        self._conn.executed.append((normalized, tuple(params)))
        self._rows = []
        if normalized.startswith("SELECT COUNT(*) AS count"):
            self._rows = [{"count": self._conn.count}]
        elif normalized.startswith("SELECT chunk_id, created_at"):
            self._rows = list(self._conn.candidates)
        elif normalized.startswith("DELETE FROM") and " t WHERE " in normalized:
            self.rowcount = self._conn.delete_rowcount

    def executemany(self, sql: str, seq: list[tuple[object, ...]]) -> None:
        self._conn.deleted.extend(str(item[0]) for item in seq)

    def fetchone(self) -> dict[str, Any] | None:
        return self._rows[0] if self._rows else None

    def fetchall(self) -> list[dict[str, Any]]:
        return list(self._rows)

    def close(self) -> None:
        return None


class _ScriptedConn:
    def __init__(self, *, count: int = 0, candidates: list[dict[str, Any]] | None = None, delete_rowcount: int = 0) -> None:
        self.count = count
        self.candidates = candidates or []
        self.delete_rowcount = delete_rowcount
        self.executed: list[tuple[str, tuple[object, ...]]] = []
        self.deleted: list[str] = []

    def cursor(self) -> _ScriptedCursor:
        return _ScriptedCursor(self)

    def commit(self) -> None:
        return None

    def rollback(self) -> None:
        return None

    def close(self) -> None:
        return None


def _default_config() -> NCPConfig:
    return NCPConfig(values=copy.deepcopy(DEFAULT_CONFIG), project_root=Path("."))


def _pg_store(conn: _ScriptedConn | None = None, **kwargs: Any) -> PgvectorStore:
    init_conn = _ScriptedConn()
    store = PgvectorStore(
        "postgresql://fake/ncp", connect_factory=lambda _dsn: init_conn, config=_default_config(), **kwargs
    )
    if conn is not None:
        store._connect_factory = lambda _dsn: conn  # type: ignore[method-assign]
    return store


def _candidates(now: float) -> list[dict[str, Any]]:
    return [
        {"chunk_id": "old_low", "created_at": now - 3600, "base_trust": 0.05, "generation": 0, "written_at_drift": 0.0},
        {"chunk_id": "new_high", "created_at": now, "base_trust": 0.95, "generation": 0, "written_at_drift": None},
        {"chunk_id": "mid", "created_at": now - 60, "base_trust": 0.5, "generation": 0, "written_at_drift": 0.0},
    ]


def test_pgvector_hard_gc_excludes_user_verified_and_evicts_lowest_score() -> None:
    store = _pg_store(max_working_chunks=4, gc_threshold=3)
    conn = _ScriptedConn(count=5, candidates=_candidates(time.time()))
    store._hard_gc(conn, pipeline_id="pipe")

    selects = [sql for sql, _ in conn.executed if sql.startswith("SELECT chunk_id, created_at")]
    assert len(selects) == 1 and "src != 'user_verified'" in selects[0]
    assert "pipeline_id = %s" in selects[0]
    assert conn.deleted == ["old_low", "mid"]  # overflow = 5 - 3


def test_pgvector_hard_gc_null_pipeline_clause_and_under_cap_noop() -> None:
    store = _pg_store(max_working_chunks=4, gc_threshold=3)
    conn = _ScriptedConn(count=4, candidates=_candidates(time.time()))
    store._hard_gc(conn, pipeline_id=None)
    counts = [(sql, params) for sql, params in conn.executed if sql.startswith("SELECT COUNT(*)")]
    assert counts and "pipeline_id IS NULL" in counts[0][0] and counts[0][1] == ()
    assert conn.deleted == []


def _async_store(**kwargs: Any) -> AsyncPgvectorStore:
    # Constructing normally needs psycopg_pool; the GC helpers only need these.
    store = AsyncPgvectorStore.__new__(AsyncPgvectorStore)
    store.schema = "ncp"
    store.table_prefix = "ncp_"
    store.max_working_chunks, store.gc_threshold = resolve_gc_limits(
        kwargs.get("max_working_chunks"), kwargs.get("gc_threshold"), None
    )
    return store


class _AsyncCursor:
    def __init__(self, conn: "_AsyncConn") -> None:
        self._conn = conn
        self._rows: list[dict[str, Any]] = []
        self.description = None

    async def __aenter__(self) -> "_AsyncCursor":
        return self

    async def __aexit__(self, *exc: object) -> bool:
        return False

    async def execute(self, sql: str, params: tuple[object, ...] = ()) -> None:
        normalized = " ".join(sql.split())
        self._conn.executed.append((normalized, tuple(params)))
        self._rows = []
        if normalized.startswith("SELECT COUNT(*) AS count"):
            self._rows = [{"count": self._conn.count}]
        elif normalized.startswith("SELECT chunk_id, created_at"):
            self._rows = list(self._conn.candidates)

    async def executemany(self, sql: str, seq: list[tuple[object, ...]]) -> None:
        self._conn.deleted.extend(str(item[0]) for item in seq)

    async def fetchone(self) -> dict[str, Any] | None:
        return self._rows[0] if self._rows else None

    async def fetchall(self) -> list[dict[str, Any]]:
        return list(self._rows)


class _AsyncConn:
    def __init__(self, *, count: int, candidates: list[dict[str, Any]]) -> None:
        self.count = count
        self.candidates = candidates
        self.executed: list[tuple[str, tuple[object, ...]]] = []
        self.deleted: list[str] = []

    def cursor(self) -> _AsyncCursor:
        return _AsyncCursor(self)


def test_async_pgvector_hard_gc_mirrors_sync_policy() -> None:
    store = _async_store(max_working_chunks=4, gc_threshold=3)
    conn = _AsyncConn(count=5, candidates=_candidates(time.time()))
    anyio.run(lambda: store._async_hard_gc(conn, pipeline_id=None))

    selects = [sql for sql, _ in conn.executed if sql.startswith("SELECT chunk_id, created_at")]
    assert len(selects) == 1 and "src != 'user_verified'" in selects[0]
    assert "pipeline_id IS NULL" in selects[0]
    assert conn.deleted == ["old_low", "mid"]


# ---------------------------------------------------------------------------
# 2. [pipeline].max_working_chunks / gc_threshold wiring
# ---------------------------------------------------------------------------


def _config(tmp_path: Path, **pipeline: int) -> NCPConfig:
    values: dict[str, Any] = {
        "store": {"type": "sqlite", "path": str(tmp_path / ".ncp" / "store.db")},
        "embedding": {"enabled": False},
        "providers": {"pricing": {}},
    }
    if pipeline:
        values["pipeline"] = dict(pipeline)
    return NCPConfig(values=values, project_root=tmp_path)


def test_default_pipeline_config_matches_previous_hardcoded_store_defaults(tmp_path: Path) -> None:
    cfg = _default_config()
    assert (cfg.pipeline_max_working_chunks, cfg.pipeline_gc_threshold) == (500, 400)
    store = create_store(_config(tmp_path))
    assert (store.max_working_chunks, store.gc_threshold) == (500, 400)
    bare = SQLiteStore(tmp_path / "bare.db")
    assert (bare.max_working_chunks, bare.gc_threshold) == (500, 400)


def test_create_store_passes_pipeline_gc_config_to_sqlite(tmp_path: Path) -> None:
    store = create_store(_config(tmp_path, max_working_chunks=4, gc_threshold=2))
    assert (store.max_working_chunks, store.gc_threshold) == (4, 2)
    for index in range(5):
        store.write(_chunk(f"c_{index}"), allow_duplicate=True)
    assert len(_working_ids(store, "pipe")) == 2


def test_store_constructor_reads_passed_config_and_explicit_args_win(tmp_path: Path) -> None:
    cfg = _config(tmp_path, max_working_chunks=50, gc_threshold=10)
    assert (SQLiteStore(tmp_path / "a.db", config=cfg).max_working_chunks) == 50
    explicit = SQLiteStore(tmp_path / "b.db", config=cfg, max_working_chunks=7, gc_threshold=3)
    assert (explicit.max_working_chunks, explicit.gc_threshold) == (7, 3)
    pg = _pg_store()  # default config
    assert (pg.max_working_chunks, pg.gc_threshold) == (500, 400)
    assert resolve_gc_limits(None, None, cfg) == (50, 10)


def test_gc_threshold_is_clamped_to_max_working_chunks(tmp_path: Path) -> None:
    cfg = _config(tmp_path, max_working_chunks=10, gc_threshold=99)
    assert cfg.pipeline_gc_threshold == 10
    assert resolve_gc_limits(5, 9, None) == (5, 5)
    assert resolve_gc_limits(5, -1, None) == (5, 0)


# ---------------------------------------------------------------------------
# 3. Whisper ack ownership
# ---------------------------------------------------------------------------


def _whisper(whisper_id: str, *, target: str = "bob", sender: str = "alice", ttl: int = 600) -> Whisper:
    return Whisper(
        whisper_id=whisper_id,
        from_agent=sender,
        target=target,
        whisper_type="nudge",
        payload=f"payload {whisper_id}",
        confidence=0.9,
        pipeline_id="pipe",
        ttl_seconds=ttl,
    )


def test_sqlite_targeted_whisper_ack_requires_target_or_sender(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "store.db")
    store.emit_whisper(_whisper("w_target"))
    store.emit_whisper(_whisper("w_sender"))

    assert store.acknowledge_whispers(["w_target", "w_sender"], agent_id="mallory") == 0
    assert store.whisper_pending("w_target") and store.whisper_pending("w_sender")

    assert store.acknowledge_whispers(["w_target"], agent_id="bob") == 1
    assert store.acknowledge_whispers(["w_sender"], agent_id="alice") == 1
    assert not store.whisper_pending("w_target") and not store.whisper_pending("w_sender")


def test_sqlite_broadcast_ack_semantics_unchanged(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "store.db")
    store.emit_whisper(_whisper("w_all", target="*"))
    assert store.acknowledge_whispers(["w_all"], agent_id=None) == 0
    assert store.acknowledge_whispers(["w_all"], agent_id="mallory") == 1
    assert store.peek_whispers(agent_id="mallory", pipeline_id="pipe") == []
    assert [w.whisper_id for w in store.peek_whispers(agent_id="bob", pipeline_id="pipe")] == ["w_all"]


def test_sqlite_unattributed_targeted_ack_still_deletes(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "store.db")
    store.emit_whisper(_whisper("w_lib"))
    assert store.acknowledge_whispers(["w_lib"]) == 1


def test_post_turn_cannot_ack_another_agents_whisper(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "store.db")
    store.emit_whisper(_whisper("w_for_bob"))
    assembler = Assembler(store=store)
    conscious = ConsciousBlock(
        agent_id="mallory", role="build", owns=["impl"], must_not=["plan"],
        task="t", slot="s", intent="advance", pipeline_id="pipe",
    )
    response = NCPResponse(
        content="done", input_tokens=1, output_tokens=1, cost_usd=0.0, latency_ms=1,
        model="m", pipeline_id="pipe", turn_id="turn_x",
    )
    assembler.post_turn(
        conscious=conscious,
        response=response,
        result_summary="done",
        result_full="done",
        ack_whisper_ids=["w_for_bob"],
    )
    assert store.whisper_pending("w_for_bob")
    assert [w.whisper_id for w in store.drain_whispers(agent_id="bob", pipeline_id="pipe")] == ["w_for_bob"]


def test_reply_thread_survives_and_target_ack_works(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "store.db")
    request = Whisper(
        whisper_id="w_req", from_agent="alice", target="bob", whisper_type="share",
        payload=HandoffPayload(ask="please review"), confidence=0.9, pipeline_id="pipe",
    )
    store.emit_whisper(request)
    reply = Whisper(
        whisper_id="w_rep", from_agent="bob", target="alice", whisper_type="nudge",
        payload="reviewed", confidence=0.9, pipeline_id="pipe", in_reply_to="w_req",
    )
    store.emit_whisper(reply)
    assert store.acknowledge_whispers(["w_req"], agent_id="bob") == 1
    drained = store.drain_whispers(agent_id="alice", pipeline_id="pipe")
    assert [w.whisper_id for w in drained] == ["w_rep"]
    assert drained[0].thread_id == "w_req"


# ---------------------------------------------------------------------------
# 3 + 4. Redis ack ownership, delivery TTL and index pruning (fakes)
# ---------------------------------------------------------------------------


class _FakeRedis:
    def __init__(self) -> None:
        self.hashes: dict[str, dict[str, str]] = {}
        self.sorted_sets: dict[str, dict[str, float]] = {}
        self.expirations: dict[str, int] = {}

    def hset(self, key: str, mapping: dict[str, str]) -> None:
        self.hashes.setdefault(key, {}).update(mapping)

    def hgetall(self, key: str) -> dict[str, str]:
        return dict(self.hashes.get(key, {}))

    def expire(self, key: str, ttl: int) -> None:
        self.expirations[key] = int(ttl)

    def zadd(self, key: str, mapping: dict[str, float]) -> None:
        self.sorted_sets.setdefault(key, {}).update(mapping)

    def zrange(self, key: str, start: int, end: int) -> list[str]:
        members = [m for m, _ in sorted(self.sorted_sets.get(key, {}).items(), key=lambda item: item[1])]
        return members[start:] if end == -1 else members[start : end + 1]

    def zrem(self, key: str, member: str) -> int:
        return int(self.sorted_sets.get(key, {}).pop(member, None) is not None)

    def delete(self, key: str) -> int:
        return int(self.hashes.pop(key, None) is not None)

    def expire_now(self, key: str) -> None:
        """Simulate Redis expiring a key."""
        self.hashes.pop(key, None)


class _AsyncFakeRedis:
    def __init__(self, inner: _FakeRedis) -> None:
        self.inner = inner

    def __getattr__(self, name: str) -> Any:
        method = getattr(self.inner, name)

        async def _call(*args: Any, **kwargs: Any) -> Any:
            return method(*args, **kwargs)

        return _call


def _redis() -> tuple[RedisCoordination, _FakeRedis]:
    client = _FakeRedis()
    return RedisCoordination("redis://fake", client_factory=lambda _url: client), client


def test_redis_targeted_ack_requires_target_or_sender() -> None:
    coordination, client = _redis()
    coordination.emit_whisper(_whisper("w1"))
    coordination.emit_whisper(_whisper("w2"))
    assert coordination.acknowledge_whispers(["w1", "w2"], agent_id="mallory") == 0
    assert "ncp:whispers:payload:w1" in client.hashes
    assert coordination.acknowledge_whispers(["w1"], agent_id="bob") == 1
    assert coordination.acknowledge_whispers(["w2"], agent_id="alice") == 1
    assert "ncp:whispers:payload:w1" not in client.hashes
    assert client.sorted_sets["ncp:whispers:target:bob"] == {}


def test_redis_broadcast_delivery_hash_gets_payload_ttl() -> None:
    coordination, client = _redis()
    coordination.emit_whisper(_whisper("w_all", target="*", ttl=600))
    assert coordination.acknowledge_whispers(["w_all"], agent_id="bob") == 1
    delivery_key = "ncp:whispers:delivered:w_all"
    assert "bob" in client.hashes[delivery_key]
    assert 590 <= client.expirations[delivery_key] <= client.expirations["ncp:whispers:payload:w_all"]
    # Broadcast semantics unchanged: still pending for other agents.
    assert [w.whisper_id for w in coordination.peek_whispers(agent_id="carol", pipeline_id="pipe")] == ["w_all"]
    assert coordination.acknowledge_whispers(["w_all"], agent_id=None) == 0


def test_redis_emit_prunes_expired_index_entries_for_target_that_never_peeks() -> None:
    coordination, client = _redis()
    coordination.emit_whisper(_whisper("old_1"))
    coordination.emit_whisper(_whisper("old_2"))
    coordination.emit_whisper(_whisper("b_old", target="*"))
    coordination.acknowledge_whispers(["b_old"], agent_id="carol")
    for whisper_id in ("old_1", "old_2", "b_old"):
        client.expire_now(f"ncp:whispers:payload:{whisper_id}")

    coordination.emit_whisper(_whisper("fresh"))
    coordination.emit_whisper(_whisper("b_fresh", target="*"))

    assert set(client.sorted_sets["ncp:whispers:target:bob"]) == {"fresh"}
    assert set(client.sorted_sets["ncp:whispers:target:*"]) == {"b_fresh"}
    assert "ncp:whispers:delivered:b_old" not in client.hashes


def test_async_redis_ack_ownership_ttl_and_pruning() -> None:
    inner = _FakeRedis()
    coordination = AsyncRedisCoordination("redis://fake", client_factory=lambda _url: _AsyncFakeRedis(inner))

    async def _run() -> None:
        await coordination.emit_whisper(_whisper("w1"))
        await coordination.emit_whisper(_whisper("w_all", target="*", ttl=600))
        assert await coordination._async_acknowledge_whispers(["w1"], agent_id="mallory") == 0
        assert "ncp:whispers:payload:w1" in inner.hashes
        assert await coordination._async_acknowledge_whispers(["w1"], agent_id="bob") == 1
        # Unattributed broadcast acks no longer delete it for everyone (sync parity).
        assert await coordination._async_acknowledge_whispers(["w_all"], agent_id=None) == 0
        assert "ncp:whispers:payload:w_all" in inner.hashes
        assert await coordination._async_acknowledge_whispers(["w_all"], agent_id="bob") == 1
        assert 590 <= inner.expirations["ncp:whispers:delivered:w_all"] <= 605

        await coordination.emit_whisper(_whisper("old"))
        inner.expire_now("ncp:whispers:payload:old")
        inner.expire_now("ncp:whispers:payload:w_all")
        await coordination.emit_whisper(_whisper("fresh"))
        await coordination.emit_whisper(_whisper("b_fresh", target="*"))
        assert set(inner.sorted_sets["ncp:whispers:target:bob"]) == {"fresh"}
        assert set(inner.sorted_sets["ncp:whispers:target:*"]) == {"b_fresh"}
        assert "ncp:whispers:delivered:w_all" not in inner.hashes

    anyio.run(_run)


# ---------------------------------------------------------------------------
# 4. SQLite whisper_deliveries orphans
# ---------------------------------------------------------------------------


def test_soft_gc_removes_deliveries_of_expired_whispers(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "store.db")
    store.emit_whisper(_whisper("w_gone", target="*"))
    store.emit_whisper(_whisper("w_live", target="*"))
    store.acknowledge_whispers(["w_gone", "w_live"], agent_id="bob")
    with sqlite3.connect(store.path) as connection:
        connection.execute("UPDATE whispers SET expires_at = 1 WHERE whisper_id = 'w_gone'")
    # Peek's own expiry sweep deletes the whisper but used to leave the receipt.
    store.peek_whispers(agent_id="carol", pipeline_id="pipe")
    store.write(_chunk("trigger_soft_gc"))
    assert _ids(store, "SELECT whisper_id FROM whisper_deliveries") == {"w_live"}


# ---------------------------------------------------------------------------
# 5. Soft-GC expiry indexes
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("statement", "index"),
    [
        ("DELETE FROM chunks WHERE expiry IS NOT NULL AND expiry <= ?", "idx_chunks_expiry"),
        ("DELETE FROM turn_records WHERE expires_at <= ?", "idx_turns_expires"),
        ("DELETE FROM tombstones WHERE expires_at <= ?", "idx_tombstones_expires"),
        ("DELETE FROM whispers WHERE expires_at <= ?", "idx_whispers_expires"),
    ],
)
def test_soft_gc_deletes_use_expiry_indexes(tmp_path: Path, statement: str, index: str) -> None:
    store = SQLiteStore(tmp_path / "store.db")
    with sqlite3.connect(store.path) as connection:
        plan = " ".join(str(row[-1]) for row in connection.execute(f"EXPLAIN QUERY PLAN {statement}", (0.0,)))
    assert index in plan


def test_existing_sqlite_database_gains_expiry_indexes(tmp_path: Path) -> None:
    path = tmp_path / "store.db"
    SQLiteStore(path)
    with sqlite3.connect(path) as connection:
        connection.execute("DROP INDEX idx_chunks_expiry")
        connection.execute("DROP INDEX idx_whispers_expires")
    SQLiteStore(path)
    with sqlite3.connect(path) as connection:
        names = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'index'")}
    assert {"idx_chunks_expiry", "idx_turns_expires", "idx_tombstones_expires", "idx_whispers_expires"} <= names


def test_postgres_migration_019_adds_expiry_indexes_and_template_matches() -> None:
    runner = MigrationRunner(object(), schema="ncp", prefix="ncp_")
    files = {f.version: f for f in runner._discover()}
    assert files[19].name == "019_add_expiry_indexes"
    up, down = MigrationRunner.parse_sections(files[19].path.read_text())
    names = ["idx_chunks_expiry", "idx_turns_expires", "idx_tombstones_expires", "idx_whispers_expires"]
    for name in names:
        assert f"CREATE INDEX IF NOT EXISTS {{prefix}}{name}" in up
        assert f"DROP INDEX IF EXISTS {{schema}}.{{prefix}}{name}" in down
        assert f"{{prefix}}{name}" in PGVECTOR_SCHEMA_TEMPLATE
    substituted = runner._sub(up)
    assert "{schema}" not in substituted and "{prefix}" not in substituted
    PGVECTOR_SCHEMA_TEMPLATE.format(schema="ncp", prefix="ncp_")


# ---------------------------------------------------------------------------
# 6. vacuum()
# ---------------------------------------------------------------------------


OLD = 1_000.0  # far older than any cutoff used below


def _seed_vacuum_rows(store: SQLiteStore) -> None:
    now = time.time()
    store.write(_chunk("live_chunk"), allow_duplicate=True)
    store.write(_chunk("keeper"), allow_duplicate=True)
    with sqlite3.connect(store.path) as c:
        # conscious_log: two old rows for (a, pipe) -> newest kept; one old
        # row for (a, NULL) -> kept as the only/newest; one recent row for b.
        c.executemany(
            "INSERT INTO conscious_log (agent_id, pipeline_id, snapshot_hash, snapshot_json, logged_at) VALUES (?, ?, 'h', '{}', ?)",
            [("a", "pipe", OLD), ("a", "pipe", OLD + 1), ("a", None, OLD), ("b", "pipe", now)],
        )
        c.executemany(
            "INSERT INTO cost_log (turn_id, pipeline_id, agent_id, model, input_tokens, output_tokens, cost_usd, logged_at)"
            " VALUES (?, 'pipe', 'a', 'm', 1, 1, 0.1, ?)",
            [("t_old", OLD), ("t_new", now)],
        )
        c.executemany(
            "INSERT INTO embedding_cost_log (pipeline_id, op, char_count, estimated_tokens, logged_at) VALUES ('pipe', 'write', 1, 1, ?)",
            [(OLD,), (now,)],
        )
        c.executemany(
            "INSERT INTO drift_history (session_id, turn, drift_score, ts) VALUES ('s', ?, 0.1, ?)",
            [(1, OLD), (2, now)],
        )
        c.executemany(
            "INSERT INTO turn_chunks (turn_id, chunk_id, relation, created_at) VALUES (?, ?, 'retrieved', ?)",
            [
                ("turn_old", "live_chunk", OLD),  # old -> removed
                ("turn_new", "live_chunk", now),  # kept
                ("turn_new", "missing_chunk", now),  # orphan -> removed regardless of age
                ("turn_new", "alias_loser", now),  # alias -> kept (credits the keeper)
            ],
        )
        c.execute(
            "INSERT INTO chunk_aliases (alias_id, target_id, reason, pipeline_id, created_at)"
            " VALUES ('alias_loser', 'keeper', 'equivalent_consolidation', 'pipe', ?)",
            (now,),
        )
        c.executemany(
            "INSERT INTO outcomes (outcome_id, turn_id, chunk_ids, success, weight, created_at, consumed) VALUES (?, NULL, '[]', 1, 1.0, ?, ?)",
            [("o_consumed_old", OLD, 1), ("o_unconsumed_old", OLD, 0), ("o_consumed_new", now, 1)],
        )
        c.executemany(
            "INSERT INTO outcome_applications (outcome_id, chunk_id, applied_at) VALUES (?, 'live_chunk', ?)",
            [("o_consumed_old", OLD), ("o_unconsumed_old", OLD), ("o_consumed_new", now)],
        )
        c.executemany(
            "INSERT INTO dissent_log (chunk_id, identity_id, created_at) VALUES (?, 'x', ?)",
            [("live_chunk", OLD), ("missing_chunk", OLD), ("missing_chunk_2", now)],
        )
        c.executemany(
            "INSERT INTO tasks (task_id, pipeline_id, slot, title, status, created_by, parent_task_id, created_at, updated_at)"
            " VALUES (?, 'pipe', 's', 't', ?, 'r', ?, ?, ?)",
            [
                ("tsk_done_old", "done", None, OLD, OLD),
                ("tsk_failed_old", "failed", None, OLD, OLD),
                ("tsk_cancel_new", "cancelled", None, now, now),
                ("tsk_open_old", "open", None, OLD, OLD),
                ("tsk_claimed_old", "claimed", None, OLD, OLD),
                ("tsk_parent_done_old", "done", None, OLD, OLD),
                ("tsk_child_open", "open", "tsk_parent_done_old", now, now),
            ],
        )
        c.executemany(
            "INSERT INTO chunk_edges (edge_id, src_chunk_id, dst_chunk_id, edge_type, created_at) VALUES (?, ?, ?, 'caused_by', ?)",
            [
                ("e_live", "live_chunk", "keeper", OLD),
                ("e_dst_gone", "live_chunk", "missing_chunk", now),
                ("e_src_gone", "missing_chunk", "keeper", now),
                ("e_alias", "alias_loser", "keeper", now),
            ],
        )


EXPECTED_VACUUM = {
    "conscious_log": 1,
    "cost_log": 1,
    "embedding_cost_log": 1,
    "drift_history": 1,
    "turn_chunks": 2,
    "outcomes": 1,
    "outcome_applications": 1,
    "dissent_log": 1,
    "tasks": 2,
    "chunk_edges": 2,
}


def test_vacuum_dry_run_counts_without_deleting(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "store.db")
    _seed_vacuum_rows(store)
    before = _ids(store, "SELECT edge_id FROM chunk_edges")
    assert store.vacuum(3600, dry_run=True) == EXPECTED_VACUUM
    assert _ids(store, "SELECT edge_id FROM chunk_edges") == before
    assert store.vacuum(3600, dry_run=True) == EXPECTED_VACUUM


def test_vacuum_deletes_only_safe_rows(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "store.db")
    _seed_vacuum_rows(store)
    assert store.vacuum(older_than_seconds=3600) == EXPECTED_VACUUM
    assert store.vacuum(3600) == {table: 0 for table in EXPECTED_VACUUM}

    with sqlite3.connect(store.path) as c:
        conscious = {(r[0], r[1], r[2]) for r in c.execute("SELECT agent_id, pipeline_id, logged_at FROM conscious_log")}
    assert {(agent, pipe) for agent, pipe, _ in conscious} == {("a", "pipe"), ("a", None), ("b", "pipe")}
    assert ("a", "pipe", OLD + 1) in conscious and ("a", None, OLD) in conscious
    assert _ids(store, "SELECT turn_id FROM cost_log") == {"t_new"}
    assert _ids(store, "SELECT turn_id || ':' || chunk_id FROM turn_chunks") == {
        "turn_new:live_chunk",
        "turn_new:alias_loser",
    }
    assert _ids(store, "SELECT outcome_id FROM outcomes") == {"o_unconsumed_old", "o_consumed_new"}
    assert _ids(store, "SELECT outcome_id FROM outcome_applications") == {"o_unconsumed_old", "o_consumed_new"}
    assert _ids(store, "SELECT chunk_id FROM dissent_log") == {"live_chunk", "missing_chunk_2"}
    assert _ids(store, "SELECT task_id FROM tasks") == {
        "tsk_cancel_new",
        "tsk_open_old",
        "tsk_claimed_old",
        "tsk_parent_done_old",
        "tsk_child_open",
    }
    assert _ids(store, "SELECT edge_id FROM chunk_edges") == {"e_live", "e_alias"}
    assert _working_ids(store, "pipe") == {"live_chunk", "keeper"}  # never touches chunks


def test_vacuum_zero_age_still_keeps_latest_conscious_and_unconsumed(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "store.db")
    _seed_vacuum_rows(store)
    counts = store.vacuum(0)
    assert counts["conscious_log"] == 1  # only the superseded (a, pipe) row; every newest row stays
    assert _ids(store, "SELECT outcome_id FROM outcomes") == {"o_unconsumed_old"}


def test_vacuum_rejects_negative_age(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "store.db")
    with pytest.raises(ValueError, match="older_than_seconds"):
        store.vacuum(-1)


def test_base_store_vacuum_default_names_backend() -> None:
    class _Bare:
        pass

    with pytest.raises(NotImplementedError, match="vacuum is not implemented for _Bare"):
        BaseStore.vacuum(_Bare(), 60)  # type: ignore[arg-type]
    with pytest.raises(NotImplementedError, match="AsyncPgvectorStore"):
        _async_store().vacuum(60)


def test_pgvector_vacuum_issues_mirrored_statements() -> None:
    conn = _ScriptedConn(delete_rowcount=3, count=7)
    store = _pg_store(conn)

    counts = store.vacuum(60, dry_run=True)
    assert counts == {table: 7 for table in EXPECTED_VACUUM}
    assert all(sql.startswith("SELECT COUNT(*) AS count FROM ncp.ncp_") for sql, _ in conn.executed)

    conn.executed.clear()
    counts = store.vacuum(60)
    assert counts == {table: 3 for table in EXPECTED_VACUUM}
    statements = {sql.split()[2]: (sql, params) for sql, params in conn.executed}
    assert set(statements) == {f"ncp.ncp_{table}" for table in EXPECTED_VACUUM}
    conscious_sql, conscious_params = statements["ncp.ncp_conscious_log"]
    assert "IS NOT DISTINCT FROM" in conscious_sql and len(conscious_params) == 1
    assert "t.consumed = 1" in statements["ncp.ncp_outcomes"][0]
    assert "consumed = 0" in statements["ncp.ncp_outcome_applications"][0]
    assert "'cancelled', 'done', 'failed'" in statements["ncp.ncp_tasks"][0]
    assert statements["ncp.ncp_chunk_edges"][1] == ()
    for sql, params in conn.executed:
        assert sql.count("%s") == len(params)
