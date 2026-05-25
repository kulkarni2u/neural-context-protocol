from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import pytest

from ncp.config import NCPConfig
from ncp.stores.factory import create_store
from ncp.stores.pgvector import PgvectorStore, infra_hint as pgvector_hint
from ncp.stores.redis import RedisStore, infra_hint as redis_hint
from ncp.stores.sqlite import SQLiteStore
from ncp.types import ConsciousBlock, NCPResponse, SubconsciousChunk, TurnRecord


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
        if "SELECT COUNT(*) AS count FROM" in normalized and "chunks WHERE" in normalized:
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
        def __init__(self, dsn: str, *, schema: str, table_prefix: str) -> None:
            captured["dsn"] = dsn
            captured["schema"] = schema
            captured["table_prefix"] = table_prefix

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
            "providers": {"pricing": {}},
        },
        project_root=project,
    )

    create_store(config)

    assert captured == {
        "dsn": "postgresql://postgres:postgres@127.0.0.1:5432/ncp",
        "schema": "ncp_test",
        "table_prefix": "demo_",
    }
