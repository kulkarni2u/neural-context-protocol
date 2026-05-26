from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import pytest

from ncp.config import NCPConfig
from ncp.stores.factory import create_store
from ncp.stores.pgvector import PgvectorStore, infra_hint as pgvector_hint
from ncp.stores.redis import RedisStore, infra_hint as redis_hint
from ncp.stores.redis_coordination import RedisCoordination
from ncp.stores.sqlite import SQLiteStore
from ncp.types import ConsciousBlock, NCPResponse, SubconsciousChunk, TurnRecord, Whisper


@dataclass
class _MemoryPgDB:
    chunks: dict[str, dict[str, object]] = field(default_factory=dict)
    tombstones: dict[str, dict[str, object]] = field(default_factory=dict)
    whispers: dict[str, dict[str, object]] = field(default_factory=dict)
    turn_records: dict[str, dict[str, object]] = field(default_factory=dict)
    conscious_log: list[dict[str, object]] = field(default_factory=list)
    cost_log: dict[str, dict[str, object]] = field(default_factory=dict)


class _FakeCursor:
    def __init__(self, db: _MemoryPgDB) -> None:
        self._db = db
        self._rows: list[dict[str, object]] = []
        self.executed: list[str] = []
        self.closed = False
        self.description = None

    def execute(self, sql: str, params: tuple[object, ...] | None = None) -> None:
        normalized = " ".join(sql.split())
        self.executed.append(normalized)
        self._rows = []
        params = params or ()

        if "CREATE EXTENSION IF NOT EXISTS vector;" in sql:
            return
        if "SELECT src FROM" in normalized and "chunks WHERE chunk_id = %s" in normalized:
            chunk_id = str(params[0])
            row = self._db.chunks.get(chunk_id)
            self._rows = [] if row is None else [{"src": row["src"]}]
            return
        if "SELECT content FROM" in normalized and "COALESCE(pipeline_id, '') = COALESCE(%s, '')" in normalized:
            zone, layer, pipeline_id = params
            self._rows = [
                {"content": row["content"]}
                for row in self._db.chunks.values()
                if row["zone"] == zone and row["layer"] == layer and (row["pipeline_id"] or "") == (pipeline_id or "")
            ]
            return
        if "INSERT INTO" in normalized and "chunks (" in normalized:
            self._insert_chunk(params)
            return
        if "SELECT * FROM" in normalized and "chunks WHERE" in normalized and "ORDER BY created_at DESC" in normalized:
            self._rows = self._select_chunks(normalized, params, desc=True)
            return
        if "SELECT COUNT(*) AS count FROM" in normalized and "chunks WHERE" in normalized and "zone" in normalized:
            self._rows = [{"count": len(self._select_chunks(normalized, params, desc=False))}]
            return
        if "SELECT chunk_id FROM" in normalized and "chunks WHERE" in normalized and "ORDER BY created_at ASC" in normalized:
            limit = int(params[-1])
            self._rows = [{"chunk_id": row["chunk_id"]} for row in self._select_chunks(normalized, params[:-1], desc=False)[:limit]]
            return
        if "DELETE FROM" in normalized and "chunks WHERE chunk_id = %s" in normalized:
            self._db.chunks.pop(str(params[0]), None)
            return
        if "DELETE FROM" in normalized and "WHERE expires_at <= %s" in normalized:
            expires_at = float(params[0])
            if "tombstones" in normalized:
                self._delete_expired(self._db.tombstones, expires_at)
            elif "whispers" in normalized:
                self._delete_expired(self._db.whispers, expires_at)
            elif "turn_records" in normalized:
                self._delete_expired(self._db.turn_records, expires_at)
            return
        if "INSERT INTO" in normalized and "turn_records (" in normalized:
            self._db.turn_records[str(params[0])] = {
                "turn_id": params[0],
                "agent_id": params[1],
                "pipeline_id": params[2],
                "task": params[3],
                "slot": params[4],
                "result": params[5],
                "result_full": params[6],
                "created_at": params[7],
                "expires_at": params[8],
            }
            return
        if "SELECT * FROM" in normalized and "turn_records WHERE turn_id = %s" in normalized:
            row = self._db.turn_records.get(str(params[0]))
            self._rows = [] if row is None else [row]
            return
        if "INSERT INTO" in normalized and "conscious_log (" in normalized:
            self._db.conscious_log.append(
                {
                    "agent_id": params[0],
                    "pipeline_id": params[1],
                    "snapshot_hash": params[2],
                    "snapshot_json": params[3],
                    "logged_at": params[4],
                }
            )
            return
        if "SELECT agent_id, snapshot_json FROM" in normalized and "conscious_log" in normalized:
            pipeline_id = params[0]
            rows = [row for row in self._db.conscious_log if row["pipeline_id"] == pipeline_id]
            self._rows = sorted(rows, key=lambda row: float(row["logged_at"]), reverse=True)
            return
        if "INSERT INTO" in normalized and "cost_log (" in normalized:
            self._db.cost_log[str(params[0])] = {
                "turn_id": params[0],
                "pipeline_id": params[1],
                "agent_id": params[2],
                "model": params[3],
                "input_tokens": params[4],
                "output_tokens": params[5],
                "cache_read_tokens": params[6],
                "cost_usd": params[7],
                "latency_ms": params[8],
                "logged_at": params[9],
            }
            return
        if "SELECT COUNT(*) AS count FROM" in normalized:
            self._rows = [{"count": self._count_table_rows(normalized, params)}]
            return
        if "SELECT COUNT(DISTINCT pipeline_id) AS count FROM" in normalized:
            self._rows = [{"count": self._count_distinct_pipelines()}]
            return
        if "SELECT COALESCE(SUM(cost_usd), 0.0) AS total FROM" in normalized:
            self._rows = [{"total": self._sum_cost(params)}]
            return
        if "SELECT MAX(" in normalized and " AS latest FROM " in normalized:
            self._rows = [{"latest": self._max_latest(normalized, params)}]
            return
        if "SELECT layer, COUNT(*) AS count FROM" in normalized and "GROUP BY layer" in normalized:
            self._rows = self._layer_counts(params)
            return
        if "SELECT pipeline_id, COUNT(*) AS chunk_count, MAX(created_at) AS last_chunk_at FROM" in normalized:
            self._rows = self._recent_pipelines(params)
            return
        if "COALESCE(SUM(cost_usd), 0.0) AS cost_usd_total" in normalized and "AVG(latency_ms)" in normalized:
            self._rows = [self._cost_summary_row(params)]
            return
        if "SELECT agent_id, COUNT(*) AS turns, COALESCE(SUM(cost_usd), 0.0) AS cost_usd_total FROM" in normalized:
            self._rows = self._cost_group_rows("agent_id", params)
            return
        if "SELECT model, COUNT(*) AS turns, COALESCE(SUM(cost_usd), 0.0) AS cost_usd_total FROM" in normalized:
            self._rows = self._cost_group_rows("model", params)
            return
        if "SELECT turn_id, pipeline_id, agent_id, model, input_tokens, output_tokens," in normalized:
            self._rows = self._recent_cost_rows(params)
            return

        raise AssertionError(f"Unhandled fake pgvector SQL: {normalized}")

    def executemany(self, sql: str, params: list[tuple[object, ...]]) -> None:
        for item in params:
            self.execute(sql, item)

    def fetchall(self) -> list[dict[str, object]]:
        return list(self._rows)

    def fetchone(self) -> dict[str, object] | None:
        if not self._rows:
            return None
        return self._rows[0]

    def close(self) -> None:
        self.closed = True

    def _insert_chunk(self, params: tuple[object, ...]) -> None:
        self._db.chunks[str(params[0])] = {
            "chunk_id": params[0],
            "pipeline_id": params[1],
            "scope": params[2],
            "zone": params[3],
            "layer": params[4],
            "chunk_type": params[5],
            "content": params[6],
            "src": params[7],
            "written_by": params[8],
            "caused_by": params[9],
            "conscious_hash": params[10],
            "evidence_id": params[11],
            "version": params[12],
            "supersedes": params[13],
            "source_refs": params[14],
            "schema_version": params[15],
            "created_at": params[16],
            "base_trust": params[17],
            "generation": params[18],
            "result_confidence": params[19],
            "result_attempts": params[20],
            "conditions": params[21],
            "valid_while": params[22],
            "expiry": params[23],
            "owner": params[24],
            "meta": params[25],
        }

    def _select_chunks(
        self,
        normalized: str,
        params: tuple[object, ...],
        *,
        desc: bool,
    ) -> list[dict[str, object]]:
        if params:
            zone = params[0]
            index = 1
        elif "zone = 'working'" in normalized:
            zone = "working"
            index = 0
        else:
            raise AssertionError(f"Unhandled chunk-selection shape: {normalized}")
        layer = None
        pipeline_id = None
        scope = None
        if "layer = %s" in normalized:
            layer = params[index]
            index += 1
        if "(pipeline_id = %s OR scope = 'global')" in normalized:
            pipeline_id = params[index]
            index += 1
        if "scope = %s" in normalized:
            scope = params[index]
        rows: list[dict[str, object]] = []
        for row in self._db.chunks.values():
            if row["zone"] != zone:
                continue
            if layer is not None and row["layer"] != layer:
                continue
            if pipeline_id is None:
                if row["pipeline_id"] is not None and row["scope"] != "global":
                    continue
            else:
                if row["pipeline_id"] != pipeline_id and row["scope"] != "global":
                    continue
            if scope is not None and row["scope"] != scope:
                continue
            rows.append(row)
        return sorted(rows, key=lambda row: float(row["created_at"]), reverse=desc)

    def _delete_expired(self, table: dict[str, dict[str, object]], expires_at: float) -> None:
        for key, row in list(table.items()):
            if float(row["expires_at"]) <= expires_at:
                del table[key]

    def _count_table_rows(self, normalized: str, params: tuple[object, ...]) -> int:
        table = self._table_from_sql(normalized)
        rows = self._rows_for_table(table)
        if " WHERE pipeline_id = %s" in normalized:
            pipeline_id = params[0] if params else None
            rows = [row for row in rows if row.get("pipeline_id") == pipeline_id]
        return len(rows)

    def _count_distinct_pipelines(self) -> int:
        return len({row["pipeline_id"] for row in self._db.chunks.values() if row.get("pipeline_id") is not None})

    def _sum_cost(self, params: tuple[object, ...]) -> float:
        rows = list(self._db.cost_log.values())
        if params:
            pipeline_id = params[0]
            rows = [row for row in rows if row["pipeline_id"] == pipeline_id]
        return float(sum(float(row["cost_usd"]) for row in rows))

    def _max_latest(self, normalized: str, params: tuple[object, ...]) -> float | None:
        table = self._table_from_sql(normalized)
        column = normalized.split("SELECT MAX(", 1)[1].split(")", 1)[0]
        rows = self._rows_for_table(table)
        if params and table in {"chunks", "turn_records", "cost_log", "conscious_log"}:
            pipeline_id = params[0]
            rows = [row for row in rows if row.get("pipeline_id") == pipeline_id]
        values = [float(row[column]) for row in rows if row.get(column) is not None]
        return max(values) if values else None

    def _layer_counts(self, params: tuple[object, ...]) -> list[dict[str, object]]:
        rows = list(self._db.chunks.values())
        if params:
            pipeline_id = params[0]
            rows = [row for row in rows if row["pipeline_id"] == pipeline_id]
        counts: dict[str, int] = {}
        for row in rows:
            counts[str(row["layer"])] = counts.get(str(row["layer"]), 0) + 1
        return [
            {"layer": layer, "count": count}
            for layer, count in sorted(counts.items(), key=lambda item: (-item[1], item[0]))
        ]

    def _recent_pipelines(self, params: tuple[object, ...]) -> list[dict[str, object]]:
        rows = [row for row in self._db.chunks.values() if row["pipeline_id"] is not None]
        if params:
            pipeline_id = params[0]
            rows = [row for row in rows if row["pipeline_id"] == pipeline_id]
        grouped: dict[str, list[dict[str, object]]] = {}
        for row in rows:
            grouped.setdefault(str(row["pipeline_id"]), []).append(row)
        payload = [
            {
                "pipeline_id": pipeline_id,
                "chunk_count": len(group_rows),
                "last_chunk_at": max(float(row["created_at"]) for row in group_rows),
            }
            for pipeline_id, group_rows in grouped.items()
        ]
        return sorted(payload, key=lambda row: float(row["last_chunk_at"]), reverse=True)[:5]

    def _cost_summary_row(self, params: tuple[object, ...]) -> dict[str, object]:
        rows = list(self._db.cost_log.values())
        if params:
            pipeline_id = params[0]
            rows = [row for row in rows if row["pipeline_id"] == pipeline_id]
        entry_count = len(rows)
        avg_latency = (
            sum(float(row["latency_ms"] or 0) for row in rows) / entry_count if entry_count else 0.0
        )
        return {
            "cost_usd_total": float(sum(float(row["cost_usd"]) for row in rows)),
            "input_tokens_total": int(sum(int(row["input_tokens"]) for row in rows)),
            "output_tokens_total": int(sum(int(row["output_tokens"]) for row in rows)),
            "cache_read_tokens_total": int(sum(int(row["cache_read_tokens"]) for row in rows)),
            "entry_count": entry_count,
            "avg_latency_ms": float(avg_latency),
        }

    def _cost_group_rows(self, group_by: str, params: tuple[object, ...]) -> list[dict[str, object]]:
        rows = list(self._db.cost_log.values())
        if params:
            pipeline_id = params[0]
            rows = [row for row in rows if row["pipeline_id"] == pipeline_id]
        grouped: dict[str, dict[str, object]] = {}
        for row in rows:
            key = str(row[group_by])
            bucket = grouped.setdefault(key, {group_by: key, "turns": 0, "cost_usd_total": 0.0})
            bucket["turns"] = int(bucket["turns"]) + 1
            bucket["cost_usd_total"] = float(bucket["cost_usd_total"]) + float(row["cost_usd"])
        return sorted(grouped.values(), key=lambda row: (-float(row["cost_usd_total"]), str(row[group_by])))

    def _recent_cost_rows(self, params: tuple[object, ...]) -> list[dict[str, object]]:
        rows = list(self._db.cost_log.values())
        if len(params) > 1:
            pipeline_id = params[0]
            limit = int(params[1])
            rows = [row for row in rows if row["pipeline_id"] == pipeline_id]
        else:
            limit = int(params[0])
        ordered = sorted(rows, key=lambda row: float(row["logged_at"]), reverse=True)
        return ordered[:limit]

    def _table_from_sql(self, normalized: str) -> str:
        if " FROM " not in normalized:
            raise AssertionError(f"Unable to determine table for SQL: {normalized}")
        table_name = normalized.split(" FROM ", 1)[1].split()[0].split(".")[-1]
        if table_name.endswith("chunks"):
            return "chunks"
        if table_name.endswith("tombstones"):
            return "tombstones"
        if table_name.endswith("turn_records"):
            return "turn_records"
        if table_name.endswith("conscious_log"):
            return "conscious_log"
        if table_name.endswith("cost_log"):
            return "cost_log"
        raise AssertionError(f"Unknown fake pgvector table for SQL: {normalized}")

    def _rows_for_table(self, table: str) -> list[dict[str, object]]:
        if table == "chunks":
            return list(self._db.chunks.values())
        if table == "tombstones":
            return list(self._db.tombstones.values())
        if table == "turn_records":
            return list(self._db.turn_records.values())
        if table == "conscious_log":
            return list(self._db.conscious_log)
        if table == "cost_log":
            return list(self._db.cost_log.values())
        raise AssertionError(f"Unsupported fake pgvector table: {table}")


class _FakeConnection:
    def __init__(self, db: _MemoryPgDB) -> None:
        self._db = db
        self.cursors: list[_FakeCursor] = []
        self.committed = False
        self.closed = False

    def cursor(self) -> _FakeCursor:
        cursor = _FakeCursor(self._db)
        self.cursors.append(cursor)
        return cursor

    def commit(self) -> None:
        self.committed = True

    def rollback(self) -> None:
        return

    def close(self) -> None:
        self.closed = True


def _pg_connect_factory(db: _MemoryPgDB):
    def _connect(_: str) -> _FakeConnection:
        return _FakeConnection(db)

    return _connect


class _FakeRedisClient:
    def __init__(self) -> None:
        self.hashes: dict[str, dict[str, str]] = {}
        self.sorted_sets: dict[str, dict[str, float]] = {}
        self.expirations: dict[str, int] = {}

    def hset(self, key: str, mapping: dict[str, str]) -> None:
        self.hashes[key] = dict(mapping)

    def hgetall(self, key: str) -> dict[str, str]:
        return dict(self.hashes.get(key, {}))

    def expire(self, key: str, ttl_seconds: int) -> None:
        self.expirations[key] = ttl_seconds

    def zadd(self, key: str, mapping: dict[str, float]) -> None:
        bucket = self.sorted_sets.setdefault(key, {})
        bucket.update(mapping)

    def zrange(self, key: str, start: int, end: int) -> list[str]:
        entries = sorted(self.sorted_sets.get(key, {}).items(), key=lambda item: item[1])
        values = [member for member, _score in entries]
        if end == -1:
            return values[start:]
        return values[start : end + 1]

    def zrem(self, key: str, member: str) -> int:
        bucket = self.sorted_sets.get(key, {})
        if member in bucket:
            del bucket[member]
            return 1
        return 0

    def delete(self, key: str) -> int:
        existed = key in self.hashes
        self.hashes.pop(key, None)
        return int(existed)


def test_pgvector_store_initializes_schema_with_fake_connection() -> None:
    db = _MemoryPgDB()
    connection = _FakeConnection(db)

    store = PgvectorStore(
        "postgresql://postgres:postgres@127.0.0.1:5432/ncp",
        schema="ncp_test",
        table_prefix="demo_",
        connect_factory=lambda dsn: connection,
    )

    assert isinstance(store, PgvectorStore)
    assert connection.committed is True
    assert connection.closed is True
    assert "CREATE EXTENSION IF NOT EXISTS vector;" in connection.cursors[0].executed[0]
    assert "CREATE SCHEMA IF NOT EXISTS ncp_test;" in connection.cursors[0].executed[0]
    assert "demo_chunks" in connection.cursors[0].executed[0]


def test_pgvector_store_write_query_and_restart_with_fake_connection() -> None:
    db = _MemoryPgDB()
    factory = _pg_connect_factory(db)
    store = PgvectorStore(
        "postgresql://postgres:postgres@127.0.0.1:5432/ncp",
        connect_factory=factory,
    )
    chunk = SubconsciousChunk(
        chunk_id="sub_auth",
        layer="procedural",
        content="authentication handler validates bearer tokens and returns 401 on failure",
        src="tool_result",
        pipeline_id="pipe_1",
        written_by="executor",
    )

    assert store.write(chunk) is True

    restarted = PgvectorStore(
        "postgresql://postgres:postgres@127.0.0.1:5432/ncp",
        connect_factory=factory,
    )
    results = restarted.query("bearer token failure", pipeline_id="pipe_1")

    assert [result.chunk_id for result in results] == ["sub_auth"]
    assert results[0].pipeline_id == "pipe_1"


def test_pgvector_store_query_filters_zero_score_noise_and_uses_effective_score() -> None:
    db = _MemoryPgDB()
    store = PgvectorStore(
        "postgresql://postgres:postgres@127.0.0.1:5432/ncp",
        connect_factory=_pg_connect_factory(db),
    )
    store.write(
        SubconsciousChunk(
            chunk_id="sub_low_trust",
            layer="semantic",
            content="bearer token failure handling shared ranking content",
            src="tool_result",
            pipeline_id="pipe_1",
            written_by="executor",
            base_trust=0.4,
        )
    )
    store.write(
        SubconsciousChunk(
            chunk_id="sub_high_trust",
            layer="procedural",
            content="bearer token failure handling shared ranking content",
            src="tool_result",
            pipeline_id="pipe_1",
            written_by="planner",
            base_trust=0.9,
        )
    )
    store.write(
        SubconsciousChunk(
            chunk_id="sub_noise",
            layer="semantic",
            content="database schema migration notes",
            src="tool_result",
            pipeline_id="pipe_1",
            written_by="critic",
        )
    )

    results = store.query("bearer token failure", pipeline_id="pipe_1", k=4)
    off_topic = store.query("unrelated astronomy orbit", pipeline_id="pipe_1", k=4)
    blank_query = store.query("   ", pipeline_id="pipe_1", k=4)

    assert [chunk.chunk_id for chunk in results] == ["sub_high_trust", "sub_low_trust"]
    assert all(chunk.relevance > 0.0 for chunk in results)
    assert off_topic == []
    assert blank_query[0].chunk_id == "sub_high_trust"
    assert {chunk.chunk_id for chunk in blank_query} >= {"sub_low_trust", "sub_noise"}


def test_pgvector_store_duplicate_write_is_skipped() -> None:
    db = _MemoryPgDB()
    store = PgvectorStore(
        "postgresql://postgres:postgres@127.0.0.1:5432/ncp",
        connect_factory=_pg_connect_factory(db),
    )
    chunk = SubconsciousChunk(
        layer="episodic",
        content="same content for duplicate detection",
        src="synthesis",
    )

    assert store.write(chunk) is True
    assert store.write(chunk.model_copy(update={"chunk_id": "sub_duplicate"})) is False


def test_pgvector_store_src_is_immutable_for_existing_chunk_id() -> None:
    db = _MemoryPgDB()
    store = PgvectorStore(
        "postgresql://postgres:postgres@127.0.0.1:5432/ncp",
        connect_factory=_pg_connect_factory(db),
    )
    chunk = SubconsciousChunk(
        chunk_id="sub_src_lock",
        layer="semantic",
        content="immutable source check",
        src="tool_result",
    )
    store.write(chunk)

    with pytest.raises(ValueError, match="src is immutable"):
        store.write(chunk.model_copy(update={"src": "synthesis"}))


def test_pgvector_store_working_zone_turns_and_goal_versions() -> None:
    db = _MemoryPgDB()
    store = PgvectorStore(
        "postgresql://postgres:postgres@127.0.0.1:5432/ncp",
        connect_factory=_pg_connect_factory(db),
    )
    store.write(
        SubconsciousChunk(
            chunk_id="sub_working",
            layer="semantic",
            content="working chunk",
            src="tool_result",
            pipeline_id="pipe_1",
        )
    )
    store.write(
        SubconsciousChunk(
            chunk_id="sub_global",
            layer="semantic",
            content="global chunk",
            src="tool_result",
            scope="global",
            zone="global",
            expiry=9999999999.0,
        )
    )
    record = TurnRecord(
        turn_id="turn_alpha",
        agent_id="planner",
        pipeline_id="pipe_1",
        task="refactor_auth",
        slot="identify_dead_code",
        result="short summary",
        result_full="longer result body",
        created_at=100.0,
        expires_at=200.0,
    )
    conscious = ConsciousBlock(
        agent_id="planner",
        role="decompose",
        owns=["planning"],
        must_not=["shipping"],
        task="refactor_auth",
        slot="identify_dead_code",
        intent="reduce_complexity",
        pipeline_id="pipe_1",
        goal_version=3,
    )
    response = NCPResponse(
        content="done",
        input_tokens=100,
        output_tokens=20,
        cost_usd=0.05,
        model="claude_sonnet",
        pipeline_id="pipe_1",
        turn_id="turn_cost",
        latency_ms=800,
    )

    store.log_turn_record(record)
    store.log_conscious(conscious, snapshot_hash="hash_123")
    store.log_cost(agent_id="planner", response=response)

    working = store.get_working_zone(pipeline_id="pipe_1", layer="semantic")
    resolved = store.resolve_recent_ref("r:sub/turn_alpha")
    versions = store.get_pipeline_goal_versions(pipeline_id="pipe_1")

    assert [chunk.chunk_id for chunk in working] == ["sub_working"]
    assert resolved is not None
    assert resolved.result_full == "longer result body"
    assert versions == {"planner": 3}
    assert db.cost_log["turn_cost"]["cost_usd"] == 0.05


def test_pgvector_store_status_detail_and_cost_summary_with_coordination() -> None:
    db = _MemoryPgDB()
    redis_client = _FakeRedisClient()
    coordination = RedisCoordination(
        "redis://127.0.0.1:6379/0",
        client_factory=lambda _url: redis_client,
    )
    store = PgvectorStore(
        "postgresql://postgres:postgres@127.0.0.1:5432/ncp",
        connect_factory=_pg_connect_factory(db),
        coordination=coordination,
    )
    store.write(
        SubconsciousChunk(
            chunk_id="sub_alpha",
            layer="semantic",
            content="alpha semantic chunk",
            src="tool_result",
            pipeline_id="pipe_alpha",
        )
    )
    store.write(
        SubconsciousChunk(
            chunk_id="sub_beta",
            layer="procedural",
            content="beta procedural chunk",
            src="tool_result",
            pipeline_id="pipe_beta",
        )
    )
    store.log_turn_record(
        TurnRecord(
            turn_id="turn_alpha",
            agent_id="planner",
            pipeline_id="pipe_alpha",
            task="reporting",
            slot="status",
            result="summary",
            result_full="full summary",
            created_at=100.0,
            expires_at=200.0,
        )
    )
    store.log_conscious(
        ConsciousBlock(
            agent_id="planner",
            role="decompose",
            owns=["planning"],
            must_not=["shipping"],
            task="reporting",
            slot="status",
            intent="explain_store",
            pipeline_id="pipe_alpha",
            goal_version=2,
        ),
        snapshot_hash="hash_alpha",
    )
    store.log_cost(
        agent_id="planner",
        response=NCPResponse(
            content="done",
            input_tokens=80,
            output_tokens=10,
            cost_usd=0.02,
            model="claude-sonnet",
            pipeline_id="pipe_alpha",
            turn_id="turn_cost_alpha",
            latency_ms=150,
        ),
    )
    coordination.emit_whisper(
        Whisper(
            whisper_id="wsp_alpha",
            from_agent="claude",
            target="opencode",
            whisper_type="share",
            payload="review pgvector reporting",
            confidence=0.95,
            pipeline_id="pipe_alpha",
            created_at=250.0,
        )
    )

    detail = store.status_detail()
    filtered = store.status_detail(pipeline_id="pipe_alpha")
    costs = store.cost_summary(pipeline_id="pipe_alpha", limit=5)

    assert detail["overview"]["chunk_count"] == 2
    assert detail["overview"]["pipeline_count"] == 2
    assert detail["overview"]["whisper_count"] == 1
    assert filtered["overview"]["chunk_count"] == 1
    assert filtered["overview"]["whisper_count"] == 1
    assert filtered["overview"]["turn_record_count"] == 1
    assert filtered["overview"]["conscious_snapshot_count"] == 1
    assert filtered["overview"]["last_activity_at"] is not None
    assert float(filtered["overview"]["last_activity_at"]) >= 250.0
    assert filtered["layer_counts"] == {"semantic": 1}
    assert filtered["recent_pipelines"] == [
        {"pipeline_id": "pipe_alpha", "chunk_count": 1, "last_chunk_at": db.chunks["sub_alpha"]["created_at"]}
    ]
    assert costs["summary"]["cost_usd_total"] == 0.02
    assert costs["summary"]["entry_count"] == 1
    assert costs["by_agent"][0]["agent_id"] == "planner"
    assert costs["by_model"][0]["model"] == "claude-sonnet"
    assert costs["recent_entries"][0]["turn_id"] == "turn_cost_alpha"


def test_pgvector_store_rejects_invalid_identifiers() -> None:
    with pytest.raises(ValueError, match="schema"):
        PgvectorStore(
            "postgresql://postgres:postgres@127.0.0.1:5432/ncp",
            schema="bad-name",
            connect_factory=lambda dsn: _FakeConnection(_MemoryPgDB()),
        )


def test_redis_store_placeholder_is_explicit() -> None:
    with pytest.raises(NotImplementedError, match="planned for NCP 0.2.0"):
        RedisStore("redis://127.0.0.1:6379/0")


def test_redis_coordination_supports_peek_ack_and_fetch_sessions() -> None:
    client = _FakeRedisClient()
    coordination = RedisCoordination(
        "redis://127.0.0.1:6379/0",
        client_factory=lambda _url: client,
    )
    coordination.emit_whisper(
        Whisper(
            whisper_id="wsp_direct",
            from_agent="planner",
            target="executor",
            whisper_type="share",
            payload="review the pgvector slice",
            confidence=0.95,
            pipeline_id="pipe_redis",
        )
    )
    coordination.emit_whisper(
        Whisper(
            whisper_id="wsp_broadcast",
            from_agent="planner",
            target="*",
            whisper_type="alert",
            payload="global notice",
            confidence=0.10,
            pipeline_id="pipe_redis",
        )
    )

    peeked = coordination.peek_whispers(agent_id="executor", pipeline_id="pipe_redis")
    all_stats = coordination.whisper_stats()
    filtered_stats = coordination.whisper_stats(pipeline_id="pipe_redis")
    claimed, pipeline_id = coordination.claim_fetch_slot("session-1", pipeline_id="pipe_redis")

    assert [whisper.whisper_id for whisper in peeked] == ["wsp_broadcast", "wsp_direct"]
    assert all_stats["count"] == 2
    assert filtered_stats["count"] == 2
    assert coordination.acknowledge_whispers(["wsp_direct"]) == 1
    assert [whisper.whisper_id for whisper in coordination.drain_whispers(agent_id="executor", pipeline_id="pipe_redis")] == [
        "wsp_broadcast"
    ]
    assert claimed == 1
    assert pipeline_id == "pipe_redis"
    coordination.reset_fetch_session("session-1", pipeline_id="pipe_redis")
    for _ in range(3):
        coordination.claim_fetch_slot("session-1", pipeline_id="pipe_redis")
    with pytest.raises(ValueError, match="max 3 per session"):
        coordination.claim_fetch_slot("session-1", pipeline_id="pipe_redis")


def test_future_store_hints_point_to_local_scripts(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    assert "infra_up.sh" in pgvector_hint(root)
    assert "infra_up.sh" in redis_hint(root)


def test_create_store_selects_sqlite(tmp_path: Path) -> None:
    project = tmp_path / "repo"
    project.mkdir()
    config = NCPConfig(
        values={
            "store": {"type": "sqlite", "path": str(project / ".ncp" / "store.db")},
            "providers": {"pricing": {}},
        },
        project_root=project,
    )

    store = create_store(config)

    assert isinstance(store, SQLiteStore)


def test_create_store_selects_pgvector(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    captured: dict[str, str] = {}

    class _FakePgvectorStore:
        def __init__(self, dsn: str, *, schema: str, table_prefix: str, redis_url: str, redis_stream: str) -> None:
            captured["dsn"] = dsn
            captured["schema"] = schema
            captured["table_prefix"] = table_prefix
            captured["redis_url"] = redis_url
            captured["redis_stream"] = redis_stream

    monkeypatch.setattr("ncp.stores.factory.PgvectorStore", _FakePgvectorStore)
    project = tmp_path / "repo"
    project.mkdir()
    config = NCPConfig(
        values={
            "store": {"type": "pgvector", "path": str(project / ".ncp" / "store.db")},
            "pgvector": {
                "dsn": "postgresql://postgres:postgres@127.0.0.1:5432/ncp",
                "schema": "ncp_test",
                "table_prefix": "demo_",
            },
            "redis": {"url": "redis://127.0.0.1:6379/0", "stream": "ncp:whispers"},
            "providers": {"pricing": {}},
        },
        project_root=project,
    )

    create_store(config)

    assert captured == {
        "dsn": "postgresql://postgres:postgres@127.0.0.1:5432/ncp",
        "schema": "ncp_test",
        "table_prefix": "demo_",
        "redis_url": "redis://127.0.0.1:6379/0",
        "redis_stream": "ncp:whispers",
    }


def test_pgvector_store_delegates_whispers_to_redis_coordination() -> None:
    db = _MemoryPgDB()
    client = _FakeRedisClient()
    store = PgvectorStore(
        "postgresql://postgres:postgres@127.0.0.1:5432/ncp",
        connect_factory=_pg_connect_factory(db),
        coordination=RedisCoordination("redis://127.0.0.1:6379/0", client_factory=lambda _url: client),
    )
    whisper = Whisper(
        whisper_id="wsp_pg",
        from_agent="claude",
        target="opencode",
        whisper_type="share",
        payload="handoff the pgvector fix",
        confidence=0.92,
        pipeline_id="pipe_pg",
    )

    store.emit_whisper(whisper)
    peeked = store.peek_whispers(agent_id="opencode", pipeline_id="pipe_pg")

    assert [item.whisper_id for item in peeked] == ["wsp_pg"]
    assert store.acknowledge_whispers(["wsp_pg"]) == 1
    assert store.drain_whispers(agent_id="opencode", pipeline_id="pipe_pg") == []
