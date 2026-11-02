"""Postgres parity for the cross-pipeline overview (capability D) and the whisper reads it needs.

Always-run tests use fake connections and fake Redis. The ``Live*`` classes run
only when ``NCP_TASKS_PG_DSN`` points at a Postgres with the ``vector``
extension available (and, for whisper facts, ``NCP_TASKS_REDIS_URL`` at a
Redis); they exercise the real ``PgvectorStore._init_db`` and
``MigrationRunner``.

See docs/specs/2026-11-02-ncp-coordination-primitives-design.md sections 5.9 and 6.5.
"""

from __future__ import annotations

import importlib.util
import json
import os
import shutil
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from ncp.stores import overview_sql
from ncp.stores.base import NCPStoreError
from ncp.stores.migrations import MigrationRunner
from ncp.stores.overview_facts import live_whispers, newest_results, whisper_activity
from ncp.stores.pgvector import PGVECTOR_SCHEMA_TEMPLATE, PgvectorStore
from ncp.stores.redis_coordination import AsyncRedisCoordination, RedisCoordination
from ncp.stores.sqlite import SQLiteStore
from ncp.types import DecisionRecord, SubconsciousChunk, TurnRecord, Whisper

NOW = 1_800_000_000.0
STATUSES = {"open": 0, "claimed": 0, "done": 0, "failed": 0, "cancelled": 0}


# ---------------------------------------------------------------------------
# Fakes


class _FakeRedis:
    def __init__(self) -> None:
        self.hashes: dict[str, dict[str, str]] = {}
        self.sorted_sets: dict[str, dict[str, float]] = {}
        self.writes = 0

    def hset(self, key: str, mapping: dict[str, str]) -> None:
        self.writes += 1
        self.hashes.setdefault(key, {}).update(mapping)

    def hgetall(self, key: str) -> dict[str, str]:
        return dict(self.hashes.get(key, {}))

    def expire(self, key: str, ttl_seconds: int) -> None:
        return None

    def zadd(self, key: str, mapping: dict[str, float]) -> None:
        self.writes += 1
        self.sorted_sets.setdefault(key, {}).update(mapping)

    def zrange(self, key: str, start: int, end: int) -> list[str]:
        members = [m for m, _ in sorted(self.sorted_sets.get(key, {}).items(), key=lambda kv: kv[1])]
        return members[start:] if end == -1 else members[start : end + 1]

    def zrem(self, key: str, member: str) -> int:
        self.writes += 1
        return int(self.sorted_sets.get(key, {}).pop(member, None) is not None)

    def delete(self, key: str) -> int:
        self.writes += 1
        return int(self.hashes.pop(key, None) is not None)


class _AsyncFakeRedis:
    def __init__(self) -> None:
        self.inner = _FakeRedis()
        self.hashes = self.inner.hashes  # lets the coordination's key scan find payloads

    def __getattr__(self, name: str) -> Any:
        method = getattr(self.inner, name)

        async def call(*args: Any, **kwargs: Any) -> Any:
            return method(*args, **kwargs)

        return call


def _whisper(pipeline_id: str | None, *, wtype: str = "nudge", age: float = 5.0, **kw: Any) -> Whisper:
    payloads = {"alert": json.dumps({"alert_code": "stalled", "description": "worker stalled"})}
    fields: dict[str, Any] = {
        "from_agent": "worker",
        "target": "router",
        "whisper_type": wtype,
        "payload": payloads.get(wtype, "ping"),
        "confidence": 0.9,
        "pipeline_id": pipeline_id,
        "created_at": NOW - age,
        "ttl_seconds": 3600,
    }
    fields.update(kw)
    return Whisper(**fields)


def _result(pipeline_id: str, summary: str, *, status: str = "succeeded", age: float = 5.0, **kw: Any) -> Whisper:
    return _whisper(
        pipeline_id,
        wtype="result",
        age=age,
        payload=json.dumps({"status": status, "summary": summary, "result_chunk_ids": []}),
        **{"thread_id": f"thread_{pipeline_id}", **kw},
    )


# ---------------------------------------------------------------------------
# Redis read methods (fake client)


class TestRedisScanAndPeek:
    def _sync(self) -> tuple[RedisCoordination, _FakeRedis]:
        client = _FakeRedis()
        return RedisCoordination("redis://fake", client_factory=lambda url: client), client

    def test_scan_returns_every_target_newest_first_and_never_writes(self) -> None:
        coordination, client = self._sync()
        coordination.emit_whisper(_whisper("p1", age=30))
        coordination.emit_whisper(_whisper("p2", age=10, target="someone_else"))
        coordination.emit_whisper(_result("p1", "ok", age=20))
        coordination.emit_whisper(_whisper("p1", age=5, target="*"))
        before = (client.writes, json.dumps(client.hashes, sort_keys=True), json.dumps(client.sorted_sets, sort_keys=True))
        found = coordination.scan_whispers()
        assert [w.created_at for w in found] == [NOW - 5, NOW - 10, NOW - 20, NOW - 30]
        assert {w.target for w in found} == {"router", "someone_else", "*"}
        assert [w.whisper_type for w in coordination.scan_whispers(whisper_type="result")] == ["result"]
        after = (client.writes, json.dumps(client.hashes, sort_keys=True), json.dumps(client.sorted_sets, sort_keys=True))
        assert after == before

    def test_scan_skips_empty_payloads(self) -> None:
        coordination, client = self._sync()
        coordination.emit_whisper(_whisper("p1"))
        client.hashes[f"{coordination.whisper_payload_prefix}:wsp_gone"] = {}
        assert len(coordination.scan_whispers()) == 1

    @pytest.mark.anyio
    async def test_async_peek_matches_sync_and_does_not_acknowledge(self) -> None:
        sync_co, _ = self._sync()
        client = _AsyncFakeRedis()
        async_co = AsyncRedisCoordination("redis://fake", client_factory=lambda url: client)
        broadcast = _whisper("p1", target="*", age=4)
        delivered = _whisper("p1", target="*", age=3)
        whispers = [
            _whisper("p1", wtype="alert", age=9),
            _whisper("p1", age=8),
            _whisper("p1", age=7, confidence=0.1),  # below the floor
            _whisper("p2", age=6),  # other pipeline
            _whisper("p1", age=5, target="someone_else"),
            broadcast,
            delivered,
            _result("p1", "ok", age=2, confidence=0.1),  # exempt from the floor
        ]
        for w in whispers:
            sync_co.emit_whisper(w)
            await async_co.emit_whisper(w)
        await async_co._async_acknowledge_whispers([delivered.whisper_id], agent_id="router")
        sync_co.acknowledge_whispers([delivered.whisper_id], agent_id="router")

        expected = sync_co.peek_whispers(agent_id="router", pipeline_id="p1", max_items=200)
        got = await async_co.peek_whispers(agent_id="router", pipeline_id="p1", max_items=200)
        assert [w.whisper_id for w in got] == [w.whisper_id for w in expected]
        assert [w.whisper_type for w in got] == ["alert", "result", "nudge", "nudge"]
        assert delivered.whisper_id not in {w.whisper_id for w in got}
        # Peeking twice returns the same thing: nothing was acknowledged.
        again = await async_co.peek_whispers(agent_id="router", pipeline_id="p1", max_items=200)
        assert [w.whisper_id for w in again] == [w.whisper_id for w in got]
        assert await async_co.get_whisper(whispers[0].whisper_id) is not None

    @pytest.mark.anyio
    async def test_async_scan_is_read_only(self) -> None:
        client = _AsyncFakeRedis()
        co = AsyncRedisCoordination("redis://fake", client_factory=lambda url: client)
        await co.emit_whisper(_whisper("p1", age=2))
        await co.emit_whisper(_result("p1", "ok", age=1))
        writes = client.inner.writes
        assert [w.whisper_type for w in await co.scan_whispers()] == ["result", "nudge"]
        assert [w.whisper_type for w in await co.scan_whispers(whisper_type="result")] == ["result"]
        assert client.inner.writes == writes


class TestWhisperFactHelpers:
    def test_live_whispers_applies_ttl(self) -> None:
        fresh, old = _whisper("p1", age=10), _whisper("p1", age=7200)
        assert live_whispers([fresh, old], NOW) == [fresh]

    def test_activity_and_newest_results(self) -> None:
        ws = [
            _whisper("p1", age=50), _whisper(None, age=1), _result("p1", "old", age=40),
            _result("p1", "new", status="failed", age=9), _result("p2", "other", age=3),
        ]
        assert whisper_activity(ws) == {"p1": NOW - 9, "p2": NOW - 3}
        assert newest_results(ws) == {"p1": ("failed", "new", NOW - 9), "p2": ("succeeded", "other", NOW - 3)}


# ---------------------------------------------------------------------------
# Statement builders and assembly (no database)


class TestOverviewSql:
    def test_statements_cover_union_and_mirror_sqlite_rules(self) -> None:
        stmts = overview_sql.statements(now=NOW, cutoff=NOW - 100, include_tasks=True, stale_open_seconds=1800)
        assert set(stmts) == {"activity", "decisions", "drift", "dissent", "task_counts", "expiring", "stale", "failed"}
        activity = stmts["activity"][0]
        for table in ("chunks", "turn_records", "decisions", "tasks"):
            assert f"{{schema}}.{{prefix}}{table}" in activity
        assert stmts["task_counts"][1] == (NOW - 100,)
        assert "status IN ('open', 'claimed') OR updated_at >= %s" in stmts["task_counts"][0]
        assert stmts["expiring"][1] == (NOW + 60.0,)
        assert "lease_expires_at <= %s" in stmts["expiring"][0]
        assert stmts["stale"][1] == (NOW - 1800,) and "updated_at <= %s" in stmts["stale"][0]
        assert stmts["dissent"][1] == (NOW - 86400.0,)
        for sql, _ in stmts.values():
            assert sql.format(schema="s", prefix="p_")  # formats cleanly

    def test_no_window_uses_unbounded_terminal_floor(self) -> None:
        stmts = overview_sql.statements(now=NOW, cutoff=None, include_tasks=True, stale_open_seconds=0)
        assert stmts["task_counts"][1][0] < -1e17
        assert "stale" not in stmts  # 0 disables the flag

    def test_include_tasks_false_drops_task_statements(self) -> None:
        stmts = overview_sql.statements(now=NOW, cutoff=None, include_tasks=False, stale_open_seconds=1800)
        assert set(stmts) == {"activity", "decisions", "drift", "dissent"}
        assert "{prefix}tasks" not in stmts["activity"][0]

    def test_select_pipelines_merges_whisper_activity_and_filters(self) -> None:
        rows = [{"pipeline_id": "pr_1", "ts": NOW - 500}, {"pipeline_id": "old", "ts": NOW - 9000}]
        got = overview_sql.select_pipelines(
            rows, {"pr_1": NOW - 5, "pr_2": NOW - 50}, wanted=None, prefix="pr_", cutoff=NOW - 1000
        )
        assert got == {"pr_1": NOW - 5, "pr_2": NOW - 50}
        assert overview_sql.select_pipelines(rows, {}, wanted={"old"}, prefix=None, cutoff=None) == {"old": NOW - 9000}

    def test_build_rows_shape_and_defaults(self) -> None:
        facts = {
            "task_counts": [{"pipeline_id": "p1", "status": "failed", "n": 2}],
            "failed": [{"pipeline_id": "p1", "error": "boom", "updated_at": NOW - 20}],
            "decisions": [{"pipeline_id": "p1", "schema_id": "s", "choice": json.dumps("block")}],
            "drift": [{"session_id": "p1", "drift_score": 0.25, "ts": NOW}],
            "dissent": [{"pipeline_id": "p1", "n": 3}],
        }
        rows = overview_sql.build_rows(
            {"p1": NOW - 1, "p2": NOW - 2}, facts, include_tasks=True, viewer="router",
            inbox={"p1": {"alert": 1}}, result_whisper={"p1": ("succeeded", "ok", NOW - 5)},
        )
        p1, p2 = rows
        assert p1["tasks"] == {**STATUSES, "failed": 2} and p1["inbox"] == {"alert": 1}
        assert p1["last_result"]["source"] == "whisper"  # at NOW-5 is newer than the failed task at NOW-20
        assert p1["last_decision"] == {"schema_id": "s", "choice": "block"}
        assert p1["drift"] == 0.25 and p1["dissent_24h"] == 3
        assert p2["tasks"] == STATUSES and p2["last_result"] is None and p2["inbox"] == {}


# ---------------------------------------------------------------------------
# Sync store with a fake connection


def _pg_responder(facts: dict[str, list[dict[str, Any]]]) -> Any:
    keys = [
        ("MAX(ts) AS ts", "activity"), ("DISTINCT ON (pipeline_id) pipeline_id, schema_id", "decisions"),
        ("DISTINCT ON (session_id)", "drift"), ("dissent_log", "dissent"),
        ("GROUP BY pipeline_id, status", "task_counts"), ("lease_expires_at IS NOT NULL", "expiring"),
        ("status = 'open' AND updated_at", "stale"), ("DISTINCT ON (pipeline_id) pipeline_id, error", "failed"),
    ]

    def respond(sql: str) -> list[dict[str, Any]]:
        for needle, name in keys:
            if needle in sql:
                return facts.get(name, [])
        return []  # expire UPDATE ... RETURNING

    return respond


def _sync_pg(facts: dict[str, list[dict[str, Any]]], **kw: Any) -> tuple[PgvectorStore, MagicMock]:
    conn = MagicMock()
    cursor = conn.cursor.return_value
    seen: dict[str, str] = {"sql": ""}
    respond = _pg_responder(facts)

    def execute(sql: str, params: Any = None) -> None:
        seen["sql"] = sql

    cursor.execute.side_effect = execute
    cursor.fetchall.side_effect = lambda: respond(seen["sql"])
    cursor.fetchone.return_value = None
    cursor.description = []
    store = PgvectorStore("postgresql://localhost/test", connect_factory=lambda dsn: conn, **kw)
    cursor.execute.reset_mock()
    return store, conn


def _executed(conn: MagicMock) -> list[str]:
    return [c.args[0] for c in conn.cursor.return_value.execute.call_args_list]


FACTS: dict[str, list[dict[str, Any]]] = {
    "activity": [{"pipeline_id": "alpha", "ts": NOW - 100}, {"pipeline_id": "decided", "ts": NOW - 30}],
    "task_counts": [
        {"pipeline_id": "alpha", "status": "open", "n": 2},
        {"pipeline_id": "alpha", "status": "failed", "n": 1},
    ],
    "expiring": [{"pipeline_id": "alpha", "n": 1}],
    "stale": [{"pipeline_id": "alpha", "n": 1}],
    "failed": [{"pipeline_id": "alpha", "error": "boom", "updated_at": NOW - 20}],
    "decisions": [{"pipeline_id": "decided", "schema_id": "review.verdict", "choice": json.dumps("block")}],
    "drift": [{"session_id": "alpha", "drift_score": 0.12, "ts": NOW - 10}],
    "dissent": [{"pipeline_id": "alpha", "n": 2}],
}


class TestSyncPgOverview:
    def _coordination(self) -> tuple[RedisCoordination, _FakeRedis]:
        client = _FakeRedis()
        return RedisCoordination("redis://fake", client_factory=lambda url: client), client

    def test_native_override_not_the_generic_default(self) -> None:
        from ncp.stores.base import BaseStore

        assert PgvectorStore.overview_rows is not BaseStore.overview_rows

    def test_rows_from_grouped_sql_without_redis(self) -> None:
        store, conn = _sync_pg(FACTS)
        rows = {r["pipeline_id"]: r for r in store.overview_rows(now=NOW, viewer="router")}
        assert set(rows) == {"alpha", "decided"}
        alpha = rows["alpha"]
        assert alpha["last_activity"] == NOW - 100
        assert alpha["tasks"] == {**STATUSES, "open": 2, "failed": 1}
        assert (alpha["expiring_claims"], alpha["stale_open"], alpha["dissent_24h"]) == (1, 1, 2)
        assert alpha["drift"] == 0.12
        assert alpha["last_result"] == {"source": "task", "status": "failed", "summary": "boom", "at": NOW - 20}
        assert alpha["inbox"] == {}
        assert rows["decided"]["last_decision"] == {"schema_id": "review.verdict", "choice": "block"}
        assert rows["decided"]["tasks"] == STATUSES
        assert [r["pipeline_id"] for r in store.overview_rows(now=NOW)] == ["decided", "alpha"]  # newest first

    def test_expiry_runs_first_over_all_pipelines(self) -> None:
        store, conn = _sync_pg(FACTS)
        store.overview_rows(now=NOW)
        statements = _executed(conn)
        assert statements[0].startswith("UPDATE ncp.ncp_tasks SET") and "pipeline_id = %s" not in statements[0]
        assert any("UNION ALL" in s and "ncp.ncp_decisions" in s for s in statements)

    def test_include_tasks_false_skips_expiry_and_task_queries(self) -> None:
        store, conn = _sync_pg(FACTS)
        rows = store.overview_rows(now=NOW, include_tasks=False)
        assert all("tasks" not in r and "stale_open" not in r for r in rows)
        assert not any("ncp.ncp_tasks" in s for s in _executed(conn))
        assert {r["pipeline_id"]: r["last_result"] for r in rows}["alpha"] is None

    def test_unavailable_ledger_degrades_to_no_task_fields(self) -> None:
        store, _ = _sync_pg(FACTS)
        with patch.object(store, "expire_task_leases", side_effect=NCPStoreError("task ledger not supported")):
            rows = store.overview_rows(now=NOW)
        assert rows and all("tasks" not in r for r in rows)

    def test_window_and_filters(self) -> None:
        store, conn = _sync_pg(FACTS)
        assert [r["pipeline_id"] for r in store.overview_rows(now=NOW, since=60)] == ["decided"]
        assert [r["pipeline_id"] for r in store.overview_rows(now=NOW, pipeline_prefix="al")] == ["alpha"]
        assert [r["pipeline_id"] for r in store.overview_rows(now=NOW, pipelines=["decided", "x"])] == ["decided"]
        window_params = [c.args[1] for c in conn.cursor.return_value.execute.call_args_list if c.args[0].count("updated_at >= %s")]
        assert window_params and window_params[0] == (NOW - 60,)

    def test_whisper_facts_from_redis_peek_only(self) -> None:
        coordination, client = self._coordination()
        store, _ = _sync_pg(FACTS, coordination=coordination)
        for w in (
            _whisper("alpha", wtype="alert"), _whisper("alpha"), _whisper("alpha", target="someone_else"),
            _whisper("alpha", confidence=0.2), _result("alpha", "fixed it", age=2),
            _whisper("redis_only", age=1), _whisper("decided", target="*"),
        ):
            coordination.emit_whisper(w)
        state = (client.writes, json.dumps(client.hashes, sort_keys=True), json.dumps(client.sorted_sets, sort_keys=True))
        rows = {r["pipeline_id"]: r for r in store.overview_rows(now=NOW, viewer="router")}
        assert rows["alpha"]["inbox"] == {"alert": 1, "nudge": 1, "result": 1}
        assert rows["decided"]["inbox"] == {"nudge": 1}
        # newest queued result (any target) beats the older failed task
        assert rows["alpha"]["last_result"] == {
            "source": "whisper", "status": "succeeded", "summary": "fixed it", "at": NOW - 2,
        }
        assert rows["alpha"]["last_activity"] == NOW - 2  # whisper activity counts, as on SQLite
        assert rows["redis_only"]["last_activity"] == NOW - 1
        assert (client.writes, json.dumps(client.hashes, sort_keys=True), json.dumps(client.sorted_sets, sort_keys=True)) == state
        # no viewer: no inbox, but last_result still read
        no_viewer = {r["pipeline_id"]: r for r in store.overview_rows(now=NOW)}
        assert no_viewer["alpha"]["inbox"] == {} and no_viewer["alpha"]["last_result"]["source"] == "whisper"

    def test_expired_whispers_ignored(self) -> None:
        coordination, _ = self._coordination()
        store, _ = _sync_pg(FACTS, coordination=coordination)
        coordination.emit_whisper(_result("alpha", "stale news", age=7200))  # ttl 3600
        row = {r["pipeline_id"]: r for r in store.overview_rows(now=NOW, viewer="router")}["alpha"]
        assert row["last_result"]["source"] == "task" and row["inbox"] == {}

    def test_redis_failure_degrades_to_sql_only(self) -> None:
        coordination = MagicMock()
        coordination.scan_whispers.side_effect = RuntimeError("redis down")
        coordination.peek_whispers.side_effect = RuntimeError("redis down")
        store, _ = _sync_pg(FACTS, coordination=coordination)
        rows = {r["pipeline_id"]: r for r in store.overview_rows(now=NOW, viewer="router")}
        assert rows["alpha"]["inbox"] == {} and rows["alpha"]["last_result"]["source"] == "task"

    def test_empty_database(self) -> None:
        store, _ = _sync_pg({})
        assert store.overview_rows(now=NOW, viewer="router") == []


# ---------------------------------------------------------------------------
# Async store with a fake pool


def _async_pg(facts: dict[str, list[dict[str, Any]]], **kw: Any) -> tuple[Any, MagicMock]:
    pytest.importorskip("psycopg_pool", reason="pgvector extra not installed")
    from ncp.stores.pgvector_async import AsyncPgvectorStore

    pool = MagicMock()
    conn = MagicMock()
    conn.commit = AsyncMock()
    conn.rollback = AsyncMock()
    cursor = MagicMock()
    seen: dict[str, str] = {"sql": ""}
    respond = _pg_responder(facts)

    async def execute(sql: str, params: Any = None) -> None:
        seen["sql"] = sql

    async def fetchall() -> list[dict[str, Any]]:
        return respond(seen["sql"])

    cursor.execute = AsyncMock(side_effect=execute)
    cursor.fetchall = AsyncMock(side_effect=fetchall)
    cursor.description = []
    cursor.__aenter__ = AsyncMock(return_value=cursor)
    cursor.__aexit__ = AsyncMock(return_value=False)
    conn.cursor.return_value = cursor
    pool.open = AsyncMock()
    pool.connection.return_value.__aenter__ = AsyncMock(return_value=conn)
    pool.connection.return_value.__aexit__ = AsyncMock(return_value=False)
    with patch("psycopg_pool.AsyncConnectionPool", return_value=pool):
        store = AsyncPgvectorStore("postgresql://localhost/test", **kw)
    store._apool = pool
    return store, cursor


@pytest.mark.anyio
class TestAsyncPgOverview:
    async def test_native_override_and_sql_rows(self) -> None:
        from ncp.stores.pgvector_async import AsyncPgvectorStore

        store, cursor = _async_pg(FACTS)
        assert "async_viz_data" not in AsyncPgvectorStore.async_overview_rows.__code__.co_names
        rows = {r["pipeline_id"]: r for r in await store.async_overview_rows(now=NOW, viewer="router")}
        assert set(rows) == {"alpha", "decided"}
        alpha = rows["alpha"]
        assert alpha["tasks"] == {**STATUSES, "open": 2, "failed": 1}
        assert (alpha["expiring_claims"], alpha["stale_open"], alpha["dissent_24h"]) == (1, 1, 2)
        assert alpha["drift"] == 0.12 and rows["decided"]["last_decision"]["choice"] == "block"
        sqls = [c.args[0] for c in cursor.execute.call_args_list]
        assert sqls[0].startswith("UPDATE ncp.ncp_tasks SET")

    async def test_include_tasks_false(self) -> None:
        store, cursor = _async_pg(FACTS)
        rows = await store.async_overview_rows(now=NOW, include_tasks=False)
        assert rows and all("tasks" not in r for r in rows)
        assert not any("ncp.ncp_tasks" in c.args[0] for c in cursor.execute.call_args_list)

    async def test_window_and_filters(self) -> None:
        store, _ = _async_pg(FACTS)
        assert [r["pipeline_id"] for r in await store.async_overview_rows(now=NOW, since=60)] == ["decided"]
        assert [r["pipeline_id"] for r in await store.async_overview_rows(now=NOW, pipeline_prefix="al")] == ["alpha"]

    async def test_whisper_facts_via_async_peek(self) -> None:
        client = _AsyncFakeRedis()
        coordination = AsyncRedisCoordination("redis://fake", client_factory=lambda url: client)
        store, _ = _async_pg(FACTS, coordination=coordination)
        for w in (
            _whisper("alpha", wtype="alert"), _whisper("alpha"), _whisper("alpha", target="someone_else"),
            _result("alpha", "fixed it", age=2), _whisper("redis_only", age=1),
        ):
            await coordination.emit_whisper(w)
        writes = client.inner.writes
        rows = {r["pipeline_id"]: r for r in await store.async_overview_rows(now=NOW, viewer="router")}
        assert rows["alpha"]["inbox"] == {"alert": 1, "nudge": 1, "result": 1}
        assert rows["alpha"]["last_result"]["summary"] == "fixed it"
        assert rows["redis_only"]["last_activity"] == NOW - 1
        assert client.inner.writes == writes  # peeked, never acknowledged

    async def test_redis_failure_degrades(self) -> None:
        coordination = MagicMock()
        coordination.scan_whispers = AsyncMock(side_effect=RuntimeError("down"))
        coordination.peek_whispers = AsyncMock(side_effect=RuntimeError("down"))
        store, _ = _async_pg(FACTS, coordination=coordination)
        rows = {r["pipeline_id"]: r for r in await store.async_overview_rows(now=NOW, viewer="router")}
        assert rows["alpha"]["inbox"] == {} and rows["alpha"]["last_result"]["source"] == "task"


# ---------------------------------------------------------------------------
# Live Postgres (skipped unless NCP_TASKS_PG_DSN is set)

LIVE_DSN = os.getenv("NCP_TASKS_PG_DSN")
LIVE_REDIS = os.getenv("NCP_TASKS_REDIS_URL")
MIGRATIONS = Path(__file__).resolve().parents[1] / "ncp" / "migrations"


def _live_available() -> bool:
    return bool(LIVE_DSN) and importlib.util.find_spec("psycopg") is not None


def _vector_available() -> bool:
    if not _live_available():
        return False
    import psycopg

    try:
        with psycopg.connect(LIVE_DSN, autocommit=True) as conn:
            return conn.execute("SELECT 1 FROM pg_available_extensions WHERE name = 'vector'").fetchone() is not None
    except Exception:
        return False


def _redis_available() -> bool:
    if not LIVE_REDIS or importlib.util.find_spec("redis") is None:
        return False
    import redis

    try:
        return bool(redis.Redis.from_url(LIVE_REDIS).ping())
    except Exception:
        return False


needs_pg = pytest.mark.skipif(
    not _live_available(), reason="set NCP_TASKS_PG_DSN (and install psycopg) to run live Postgres tests"
)
needs_pgvector = pytest.mark.skipif(
    not _vector_available(), reason="needs NCP_TASKS_PG_DSN on a server where CREATE EXTENSION vector works"
)
needs_redis = pytest.mark.skipif(not _redis_available(), reason="set NCP_TASKS_REDIS_URL to a reachable Redis")


def _new_schema() -> tuple[Any, str]:
    import psycopg

    schema = f"ncp_ov_{uuid4().hex[:8]}"
    admin = psycopg.connect(LIVE_DSN, autocommit=True)
    admin.execute(f"CREATE SCHEMA {schema}")
    return admin, schema


def _pg_store(schema: str, **kw: Any) -> PgvectorStore:
    """A real store. Its normal ``_init_db`` (full template, ``CREATE EXTENSION vector``) runs."""
    import psycopg

    return PgvectorStore(
        LIVE_DSN, schema=schema, table_prefix="t_", connect_factory=lambda dsn: psycopg.connect(dsn), **kw
    )


def _columns(admin: Any, schema: str, table: str) -> list[tuple[Any, ...]]:
    return admin.execute(
        "SELECT column_name, data_type, is_nullable, column_default FROM information_schema.columns"
        " WHERE table_schema = %s AND table_name = %s ORDER BY ordinal_position",
        (schema, table),
    ).fetchall()


def _index_defs(admin: Any, schema: str, table: str) -> list[str]:
    rows = admin.execute(
        "SELECT indexdef FROM pg_indexes WHERE schemaname = %s AND tablename = %s ORDER BY indexname",
        (schema, table),
    ).fetchall()
    return sorted(r[0].replace(schema, "S") for r in rows)


@needs_pgvector
class TestRealPgvectorSchema:
    def test_full_init_db_creates_every_template_table(self) -> None:
        admin, schema = _new_schema()
        try:
            store = _pg_store(schema)
            tables = {
                r[0]
                for r in admin.execute(
                    "SELECT table_name FROM information_schema.tables WHERE table_schema = %s", (schema,)
                )
            }
            declared = {
                name.split(".")[-1].replace("{prefix}", "t_")
                for name in __import__("re").findall(r"CREATE TABLE IF NOT EXISTS (\S+)", PGVECTOR_SCHEMA_TEMPLATE)
            }
            assert declared <= tables and "t_tasks" in tables and "t_chunks" in tables
            ext = admin.execute("SELECT extversion FROM pg_extension WHERE extname = 'vector'").fetchone()
            assert ext is not None
            assert store.overview_rows() == []
        finally:
            admin.execute(f"DROP SCHEMA {schema} CASCADE")
            admin.close()

    def test_migrating_from_017_to_018_matches_fresh_template(self, tmp_path: Path) -> None:
        import psycopg

        fresh_admin, fresh_schema = _new_schema()
        mig_admin, mig_schema = _new_schema()
        try:
            _pg_store(fresh_schema)  # template path

            old_dir = tmp_path / "upto_017"
            old_dir.mkdir()
            for path in sorted(MIGRATIONS.glob("*.sql")):
                if int(path.stem.split("_")[0]) <= 17:
                    shutil.copy(path, old_dir / path.name)
            with psycopg.connect(LIVE_DSN) as conn:
                runner = MigrationRunner(conn, schema=mig_schema, prefix="t_", migrations_dir=old_dir)
                runner.bootstrap()
                assert [m["version"] for m in runner.apply_all()] == list(range(1, 18))
            assert not mig_admin.execute("SELECT to_regclass(%s)", (f"{mig_schema}.t_tasks",)).fetchone()[0]

            with psycopg.connect(LIVE_DSN) as conn:
                runner = MigrationRunner(conn, schema=mig_schema, prefix="t_")
                assert [m.version for m in runner.pending()] == [18]
                assert runner.apply_all() == [{"version": 18, "name": "018_add_tasks"}]
                assert runner.pending() == [] and runner.check().mismatches == []

            assert _columns(mig_admin, mig_schema, "t_tasks") == _columns(fresh_admin, fresh_schema, "t_tasks")
            assert _index_defs(mig_admin, mig_schema, "t_tasks") == _index_defs(fresh_admin, fresh_schema, "t_tasks")
            assert len(_index_defs(mig_admin, mig_schema, "t_tasks")) >= 4
            # The migrated database serves the ledger and the overview.
            migrated = _pg_store(mig_schema)
            assert migrated.overview_rows() == []
        finally:
            for admin, schema in ((fresh_admin, fresh_schema), (mig_admin, mig_schema)):
                admin.execute(f"DROP SCHEMA {schema} CASCADE")
                admin.close()


def _raw(store: Any, sql: str, params: tuple[Any, ...] = ()) -> None:
    """Run one statement on either backend. ``?``-free: use ``%s`` and ``{t}`` for the table prefix."""
    if isinstance(store, SQLiteStore):
        with store._connect() as connection:
            connection.execute(sql.replace("%s", "?").replace("{t}", ""), params)
        return
    with store._connect() as connection:
        cursor = connection.cursor()
        try:
            cursor.execute(store._sql(sql.replace("{t}", "{schema}.{prefix}")), params)
        finally:
            store._close_cursor(cursor)


def _put_chunk(store: Any, pipeline_id: str | None, age: float, chunk_id: str) -> None:
    chunk = SubconsciousChunk(
        chunk_id=chunk_id, layer="semantic", content=f"fact {chunk_id}", src="tool_result", pipeline_id=pipeline_id
    )
    store.write(chunk)
    _raw(store, "UPDATE {t}chunks SET created_at = %s WHERE chunk_id = %s", (NOW - age, chunk_id))


def _put_task(store: Any, pipeline_id: str, status: str, age: float, **kw: Any) -> None:
    jsonb = "%s" if isinstance(store, SQLiteStore) else "%s::jsonb"
    fields = {
        "owner": None, "lease_token": None, "lease_expires_at": None, "attempt": 0, "max_attempts": 3,
        "error": None, **kw,
    }
    _raw(
        store,
        "INSERT INTO {t}tasks (task_id, pipeline_id, slot, title, status, created_by, owner, lease_token,"
        " lease_expires_at, attempt, max_attempts, result_chunk_ids, error, version, created_at, updated_at)"
        f" VALUES (%s, %s, 'review', 'title', %s, 'router', %s, %s, %s, %s, %s, {jsonb}, %s, 0, %s, %s)",
        (
            f"tsk_{uuid4().hex[:12]}", pipeline_id, status, fields["owner"], fields["lease_token"],
            fields["lease_expires_at"], fields["attempt"], fields["max_attempts"], "[]", fields["error"],
            NOW - age, NOW - age,
        ),
    )


def populate(store: Any, *, whispers: bool) -> None:
    """The same fixture for every backend."""
    # alpha: busy pipeline touching every fact
    _put_chunk(store, "alpha", 500, "sub_alpha_1")
    _put_chunk(store, "alpha", 400, "sub_alpha_dissented")
    store.log_turn_record(
        TurnRecord(turn_id="turn_alpha", agent_id="w", pipeline_id="alpha", task="t", slot="s",
                   result="r", result_full="rf", created_at=NOW - 300, expires_at=NOW + 10_000)
    )
    _put_task(store, "alpha", "open", 3600)  # stale open
    _put_task(store, "alpha", "open", 10)
    _put_task(store, "alpha", "claimed", 100, owner="w", lease_token="a" * 32, lease_expires_at=NOW + 30, attempt=1)
    _put_task(store, "alpha", "claimed", 100, owner="w", lease_token="b" * 32, lease_expires_at=NOW + 250, attempt=1)
    _put_task(store, "alpha", "failed", 20, error="boom")
    _put_task(store, "alpha", "failed", 200_000, error="ancient")
    _put_task(store, "alpha", "done", 20)
    _put_task(store, "alpha", "cancelled", 15)
    for age, choice in ((100, "approve"), (10, "block")):
        store.record_decision_record(
            DecisionRecord(schema_id="review.verdict", slot="gate", choice=choice, confidence=0.9,
                           pipeline_id="alpha", created_at=NOW - age)
        )
    store.record_dissent("sub_alpha_dissented", identity_id="agent_a")
    _raw(store, "UPDATE {t}dissent_log SET created_at = %s", (NOW - 60,))
    store.log_drift_history(session_id="alpha", turn=1, drift_score=0.5)
    store.log_drift_history(session_id="alpha", turn=2, drift_score=0.12)
    _raw(store, "UPDATE {t}drift_history SET ts = %s WHERE turn = 1", (NOW - 20,))
    _raw(store, "UPDATE {t}drift_history SET ts = %s WHERE turn = 2", (NOW - 10,))
    # beta: a task whose lease already lapsed (lazy expiry must open it) and nothing else
    _put_task(store, "beta", "claimed", 500, owner="w", lease_token="c" * 32, lease_expires_at=NOW - 5, attempt=1)
    # gamma: lapsed at the attempt limit, so expiry fails it with lease_expired
    _put_task(store, "gamma", "claimed", 500, owner="w", lease_token="d" * 32, lease_expires_at=NOW - 5,
              attempt=3, max_attempts=3)
    # delta: decision-only pipeline
    store.record_decision_record(
        DecisionRecord(schema_id="triage.route", slot="route", choice={"to": "build"}, confidence=0.5,
                       pipeline_id="delta", created_at=NOW - 40)
    )
    # epsilon: old chunk (outside a 1h window) and viewer-scoped drift
    _put_chunk(store, "epsilon", 90_000, "sub_epsilon")
    store.log_drift_history(session_id="epsilon:router", turn=1, drift_score=0.33)
    _raw(store, "UPDATE {t}drift_history SET ts = %s WHERE session_id = 'epsilon:router'", (NOW - 5,))
    # rows without a pipeline never appear
    _put_chunk(store, None, 5, "sub_orphan")
    if whispers:
        for w in (
            _whisper("alpha", wtype="alert"), _whisper("alpha"), _whisper("alpha", target="*"),
            _whisper("alpha", target="someone_else"), _whisper("alpha", confidence=0.2),
            _result("alpha", "recovered", age=3), _whisper("delta", age=2, target="*"),
            _whisper("zeta_whispers_only", age=1),
        ):
            store.emit_whisper(w)


CALLS: list[dict[str, Any]] = [
    {},
    {"viewer": "router"},
    {"viewer": "router", "since": 3600},
    {"viewer": "other", "since": 86400 * 3},
    {"viewer": "router", "include_tasks": False},
    {"viewer": "router", "pipeline_prefix": "al"},
    {"viewer": "router", "pipelines": ["delta", "beta", "missing"]},
    {"viewer": "router", "stale_open_seconds": 0},
    {"viewer": "router", "stale_open_seconds": 50},
]


def _by_pid(rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {r["pipeline_id"]: r for r in rows}


@needs_pgvector
class TestLiveOverview:
    @pytest.fixture
    def pg(self) -> Any:
        admin, schema = _new_schema()
        stream = f"ov_{uuid4().hex[:8]}"
        kw: dict[str, Any] = {"redis_url": LIVE_REDIS, "redis_stream": stream} if _redis_available() else {}
        store = _pg_store(schema, **kw)
        try:
            yield store
        finally:
            store.close()
            admin.execute(f"DROP SCHEMA {schema} CASCADE")
            admin.close()

    def test_sync_rows_without_whispers(self, pg: PgvectorStore) -> None:
        populate(pg, whispers=False)
        rows = _by_pid(pg.overview_rows(now=NOW, viewer="router"))
        assert set(rows) == {"alpha", "beta", "gamma", "delta", "epsilon"}
        alpha = rows["alpha"]
        assert alpha["tasks"] == {"open": 2, "claimed": 2, "done": 1, "failed": 2, "cancelled": 1}
        assert (alpha["expiring_claims"], alpha["stale_open"], alpha["dissent_24h"]) == (1, 1, 1)
        assert alpha["last_decision"] == {"schema_id": "review.verdict", "choice": "block"}
        assert alpha["drift"] == 0.12
        assert alpha["last_activity"] == NOW - 10  # newest decision
        assert rows["beta"]["tasks"]["open"] == 1 and rows["beta"]["tasks"]["claimed"] == 0
        assert rows["gamma"]["last_result"]["summary"] == "lease_expired"
        assert rows["delta"]["last_decision"] == {"schema_id": "triage.route", "choice": '{"to":"build"}'}
        assert rows["epsilon"]["drift"] == 0.33
        assert _by_pid(pg.overview_rows(now=NOW, viewer="other"))["epsilon"]["drift"] is None

    def test_task_ledger_on_fully_initialised_store(self, pg: PgvectorStore) -> None:
        from ncp.types import TaskRecord

        task, created = pg.create_task(
            TaskRecord(pipeline_id="ledger", slot="review", title="t", created_by="router", dedupe_key="k",
                       created_at=NOW - 5, updated_at=NOW - 5)
        )
        assert created and pg.create_task(task.model_copy(update={"task_id": "tsk_other"}))[1] is False
        claimed = pg.claim_task(agent_id="w", task_id=task.task_id, lease_seconds=30, now=NOW)
        assert claimed is not None and claimed.attempt == 1
        row = _by_pid(pg.overview_rows(now=NOW + 1))["ledger"]
        assert row["tasks"]["claimed"] == 1 and row["expiring_claims"] == 1
        done = pg.complete_task(task.task_id, claimed.lease_token, status="done", summary="ok", now=NOW + 2)
        assert done.ok
        assert _by_pid(pg.overview_rows(now=NOW + 3))["ledger"]["tasks"]["done"] == 1

    @needs_redis
    def test_sync_whisper_facts_from_redis(self, pg: PgvectorStore) -> None:
        populate(pg, whispers=True)
        rows = _by_pid(pg.overview_rows(now=NOW, viewer="router"))
        assert rows["alpha"]["inbox"] == {"alert": 1, "nudge": 2, "result": 1}
        assert rows["alpha"]["last_result"] == {
            "source": "whisper", "status": "succeeded", "summary": "recovered", "at": NOW - 3,
        }
        assert rows["zeta_whispers_only"]["last_activity"] == NOW - 1
        before = pg.coordination.scan_whispers()
        pg.overview_rows(now=NOW, viewer="router")
        assert len(pg.coordination.scan_whispers()) == len(before)  # peek only: nothing consumed
        assert _by_pid(pg.overview_rows(now=NOW))["alpha"]["inbox"] == {}

    @pytest.mark.anyio
    async def test_async_matches_sync(self, pg: PgvectorStore) -> None:
        pytest.importorskip("psycopg_pool")
        from ncp.stores.pgvector_async import AsyncPgvectorStore

        populate(pg, whispers=_redis_available())
        kw: dict[str, Any] = {}
        if pg.coordination is not None:
            kw["coordination"] = AsyncRedisCoordination(LIVE_REDIS, stream=pg.coordination.stream)  # type: ignore[attr-defined]
        store = AsyncPgvectorStore(LIVE_DSN, schema=pg.schema, table_prefix=pg.table_prefix, **kw)
        await store.open()
        try:
            for call in CALLS:
                expected = pg.overview_rows(now=NOW, **call)
                assert await store.async_overview_rows(now=NOW, **call) == expected, call
        finally:
            await store.close()

    def test_parity_with_sqlite(self, pg: PgvectorStore, tmp_path: Path) -> None:
        with_whispers = pg.coordination is not None
        lite = SQLiteStore(tmp_path / "parity.db")
        populate(lite, whispers=with_whispers)
        populate(pg, whispers=with_whispers)
        for call in CALLS:
            expected = lite.overview_rows(now=NOW, **call)
            got = pg.overview_rows(now=NOW, **call)
            assert json.dumps(got, sort_keys=True) == json.dumps(expected, sort_keys=True), call
