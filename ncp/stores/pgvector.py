"""pgvector-backed durable store implementation for the 0.2.0 rollout."""

from __future__ import annotations

from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from difflib import SequenceMatcher
import json
from pathlib import Path
from typing import Any
import time

from rank_bm25 import BM25Okapi

from ncp.stores.base import BaseStore, NCPStoreUnavailableError
from ncp.stores.redis_coordination import RedisCoordination
from ncp.types import ConsciousBlock, NCPResponse, SubconsciousChunk, TurnRecord, Whisper


PGVECTOR_SCHEMA_TEMPLATE = """
CREATE EXTENSION IF NOT EXISTS vector;
CREATE SCHEMA IF NOT EXISTS {schema};

CREATE TABLE IF NOT EXISTS {schema}.{prefix}chunks (
    chunk_id TEXT PRIMARY KEY,
    pipeline_id TEXT,
    scope TEXT DEFAULT 'pipeline',
    zone TEXT DEFAULT 'working',
    layer TEXT NOT NULL,
    chunk_type TEXT DEFAULT 'prose',
    content TEXT NOT NULL,
    src TEXT NOT NULL,
    written_by TEXT DEFAULT 'system',
    caused_by TEXT,
    conscious_hash TEXT,
    evidence_id TEXT,
    version INTEGER DEFAULT 1,
    supersedes TEXT,
    source_refs JSONB DEFAULT '[]'::jsonb,
    schema_version INTEGER DEFAULT 1,
    created_at DOUBLE PRECISION NOT NULL,
    base_trust DOUBLE PRECISION DEFAULT 0.7,
    generation INTEGER DEFAULT 0,
    result_confidence DOUBLE PRECISION,
    result_attempts INTEGER,
    conditions JSONB DEFAULT '[]'::jsonb,
    valid_while TEXT,
    expiry DOUBLE PRECISION,
    owner TEXT,
    meta JSONB DEFAULT '{{}}'::jsonb
);

CREATE TABLE IF NOT EXISTS {schema}.{prefix}tombstones (
    chunk_id TEXT PRIMARY KEY,
    forward_ref TEXT,
    tombstoned_at DOUBLE PRECISION NOT NULL,
    expires_at DOUBLE PRECISION NOT NULL
);

CREATE TABLE IF NOT EXISTS {schema}.{prefix}whispers (
    whisper_id TEXT PRIMARY KEY,
    pipeline_id TEXT,
    from_agent TEXT NOT NULL,
    target TEXT NOT NULL,
    whisper_type TEXT NOT NULL,
    payload TEXT NOT NULL,
    confidence DOUBLE PRECISION NOT NULL,
    ref TEXT,
    created_at DOUBLE PRECISION NOT NULL,
    expires_at DOUBLE PRECISION NOT NULL
);

CREATE TABLE IF NOT EXISTS {schema}.{prefix}turn_records (
    turn_id TEXT PRIMARY KEY,
    agent_id TEXT NOT NULL,
    pipeline_id TEXT,
    task TEXT NOT NULL,
    slot TEXT NOT NULL,
    result TEXT NOT NULL,
    result_full TEXT NOT NULL,
    created_at DOUBLE PRECISION NOT NULL,
    expires_at DOUBLE PRECISION NOT NULL
);

CREATE TABLE IF NOT EXISTS {schema}.{prefix}conscious_log (
    log_id BIGSERIAL PRIMARY KEY,
    agent_id TEXT NOT NULL,
    pipeline_id TEXT,
    snapshot_hash TEXT NOT NULL,
    snapshot_json JSONB NOT NULL,
    logged_at DOUBLE PRECISION NOT NULL
);

CREATE TABLE IF NOT EXISTS {schema}.{prefix}cost_log (
    turn_id TEXT PRIMARY KEY,
    pipeline_id TEXT,
    agent_id TEXT NOT NULL,
    model TEXT NOT NULL,
    input_tokens INTEGER NOT NULL,
    output_tokens INTEGER NOT NULL,
    cache_read_tokens INTEGER DEFAULT 0,
    cost_usd DOUBLE PRECISION NOT NULL,
    latency_ms INTEGER,
    logged_at DOUBLE PRECISION NOT NULL
);

CREATE INDEX IF NOT EXISTS {prefix}idx_chunks_pipeline
    ON {schema}.{prefix}chunks(pipeline_id, scope, zone);
CREATE INDEX IF NOT EXISTS {prefix}idx_chunks_layer
    ON {schema}.{prefix}chunks(layer);
CREATE INDEX IF NOT EXISTS {prefix}idx_chunks_created
    ON {schema}.{prefix}chunks(created_at);
CREATE INDEX IF NOT EXISTS {prefix}idx_whispers_target
    ON {schema}.{prefix}whispers(target, expires_at);
CREATE INDEX IF NOT EXISTS {prefix}idx_whispers_pipeline
    ON {schema}.{prefix}whispers(pipeline_id, expires_at);
CREATE INDEX IF NOT EXISTS {prefix}idx_turns_agent
    ON {schema}.{prefix}turn_records(agent_id, pipeline_id);
CREATE INDEX IF NOT EXISTS {prefix}idx_conscious_agent
    ON {schema}.{prefix}conscious_log(agent_id, logged_at);
CREATE INDEX IF NOT EXISTS {prefix}idx_cost_pipeline
    ON {schema}.{prefix}cost_log(pipeline_id, logged_at);
"""


def _default_pgvector_connect(dsn: str) -> Any:
    try:
        import psycopg2
    except ModuleNotFoundError as exc:  # pragma: no cover - depends on optional extra
        raise NCPStoreUnavailableError(
            "pgvector support requires psycopg2. Install it with: pip install 'neural-context-protocol[pgvector]'"
        ) from exc
    return psycopg2.connect(dsn)


def _validate_identifier(value: str, *, field: str) -> str:
    cleaned = value.strip()
    if not cleaned:
        raise ValueError(f"{field} cannot be empty")
    allowed = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_")
    if any(char not in allowed for char in cleaned):
        raise ValueError(f"{field} must contain only letters, digits, and underscores")
    return cleaned


class PgvectorStore(BaseStore):
    """Postgres/pgvector-backed durable store for the 0.2.0 rollout."""

    def __init__(
        self,
        dsn: str,
        *,
        schema: str = "ncp",
        table_prefix: str = "ncp_",
        connect_factory: Callable[[str], Any] | None = None,
        redis_url: str | None = None,
        redis_stream: str = "ncp:whispers",
        coordination: RedisCoordination | None = None,
        max_working_chunks: int = 500,
        gc_threshold: int = 400,
    ) -> None:
        self.dsn = dsn
        self.schema = _validate_identifier(schema, field="schema")
        self.table_prefix = _validate_identifier(table_prefix, field="table_prefix")
        self._connect_factory = connect_factory or _default_pgvector_connect
        self.coordination = coordination or (
            RedisCoordination(redis_url, stream=redis_stream) if redis_url else None
        )
        self.max_working_chunks = max_working_chunks
        self.gc_threshold = gc_threshold
        self._init_db()

    @contextmanager
    def _connect(self) -> Iterator[Any]:
        try:
            connection = self._connect_factory(self.dsn)
        except NCPStoreUnavailableError:
            raise
        except Exception as exc:  # pragma: no cover - depends on runtime driver
            raise NCPStoreUnavailableError(
                f"pgvector store unavailable at {self.dsn}: {exc}"
            ) from exc
        try:
            yield connection
            connection.commit()
        except Exception as exc:  # pragma: no cover - depends on runtime driver
            try:
                connection.rollback()
            except Exception:
                pass
            if isinstance(exc, (AssertionError, TypeError, ValueError)):
                raise
            raise NCPStoreUnavailableError(
                f"pgvector store operation failed at {self.dsn}: {exc}"
            ) from exc
        finally:
            try:
                connection.close()
            except Exception:
                pass

    def _init_db(self) -> None:
        schema_sql = PGVECTOR_SCHEMA_TEMPLATE.format(
            schema=self.schema,
            prefix=self.table_prefix,
        )
        with self._connect() as connection:
            cursor = connection.cursor()
            try:
                cursor.execute(schema_sql)
            finally:
                self._close_cursor(cursor)

    def write(self, chunk: SubconsciousChunk) -> bool:
        chunk = self._validate_chunk_for_write(chunk)
        with self._connect() as connection:
            self._soft_gc(connection)
            self._assert_src_immutable(connection, chunk)
            if self._is_duplicate(connection, chunk):
                return False
            cursor = connection.cursor()
            try:
                cursor.execute(
                    self._sql(
                        """
                        INSERT INTO {schema}.{prefix}chunks (
                            chunk_id, pipeline_id, scope, zone, layer, chunk_type, content, src,
                            written_by, caused_by, conscious_hash, evidence_id, version, supersedes,
                            source_refs, schema_version, created_at, base_trust, generation,
                            result_confidence, result_attempts, conditions, valid_while, expiry, owner, meta
                        ) VALUES (
                            %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                            %s, %s, %s, %s, %s, %s, %s, %s, %s
                        )
                        ON CONFLICT (chunk_id) DO UPDATE SET
                            pipeline_id = EXCLUDED.pipeline_id,
                            scope = EXCLUDED.scope,
                            zone = EXCLUDED.zone,
                            layer = EXCLUDED.layer,
                            chunk_type = EXCLUDED.chunk_type,
                            content = EXCLUDED.content,
                            src = EXCLUDED.src,
                            written_by = EXCLUDED.written_by,
                            caused_by = EXCLUDED.caused_by,
                            conscious_hash = EXCLUDED.conscious_hash,
                            evidence_id = EXCLUDED.evidence_id,
                            version = EXCLUDED.version,
                            supersedes = EXCLUDED.supersedes,
                            source_refs = EXCLUDED.source_refs,
                            schema_version = EXCLUDED.schema_version,
                            created_at = EXCLUDED.created_at,
                            base_trust = EXCLUDED.base_trust,
                            generation = EXCLUDED.generation,
                            result_confidence = EXCLUDED.result_confidence,
                            result_attempts = EXCLUDED.result_attempts,
                            conditions = EXCLUDED.conditions,
                            valid_while = EXCLUDED.valid_while,
                            expiry = EXCLUDED.expiry,
                            owner = EXCLUDED.owner,
                            meta = EXCLUDED.meta
                        """
                    ),
                    (
                        chunk.chunk_id,
                        chunk.pipeline_id,
                        chunk.scope,
                        chunk.zone,
                        chunk.layer,
                        chunk.chunk_type,
                        chunk.content,
                        chunk.src,
                        chunk.written_by,
                        chunk.caused_by,
                        chunk.conscious_hash,
                        chunk.evidence_id,
                        1,
                        chunk.supersedes,
                        json.dumps(chunk.source_refs),
                        chunk.schema_version,
                        time.time(),
                        chunk.base_trust,
                        chunk.generation,
                        chunk.result_confidence,
                        chunk.result_attempts,
                        json.dumps(chunk.conditions),
                        chunk.valid_while,
                        chunk.expiry,
                        chunk.owner,
                        json.dumps({}),
                    ),
                )
            finally:
                self._close_cursor(cursor)
            self._hard_gc(connection, pipeline_id=chunk.pipeline_id)
            return True

    def query(
        self,
        text: str,
        *,
        k: int = 4,
        layer: str | None = None,
        pipeline_id: str | None = None,
        scope: str | None = None,
        zone: str = "working",
    ) -> list[SubconsciousChunk]:
        with self._connect() as connection:
            rows = self._load_query_rows(
                connection,
                layer=layer,
                pipeline_id=pipeline_id,
                scope=scope,
                zone=zone,
            )
        if not rows:
            return []

        corpus = [str(row["content"]).split() for row in rows]
        bm25 = BM25Okapi(corpus)
        scores = bm25.get_scores(text.split())
        paired = sorted(zip(scores, rows, strict=True), key=lambda item: item[0], reverse=True)

        diversity_limit = 2
        author_count: dict[str, int] = {}
        results: list[SubconsciousChunk] = []
        for score, row in paired:
            author = str(row["written_by"])
            if author_count.get(author, 0) >= diversity_limit:
                continue
            author_count[author] = author_count.get(author, 0) + 1
            chunk = self._row_to_chunk(row)
            chunk.relevance = max(0.0, float(score))
            results.append(chunk)
            if len(results) >= max(1, min(k, 4)):
                break
        return results

    def emit_whisper(self, whisper: Whisper) -> None:
        if self.coordination is None:
            raise NCPStoreUnavailableError(
                "pgvector whisper coordination requires Redis. Set NCP_REDIS_URL and ensure Redis is reachable."
            )
        self.coordination.emit_whisper(whisper)

    def drain_whispers(
        self,
        *,
        agent_id: str,
        pipeline_id: str | None = None,
        max_items: int = 3,
        min_confidence: float = 0.60,
    ) -> list[Whisper]:
        if self.coordination is None:
            raise NCPStoreUnavailableError(
                "pgvector whisper coordination requires Redis. Set NCP_REDIS_URL and ensure Redis is reachable."
            )
        return self.coordination.drain_whispers(
            agent_id=agent_id,
            pipeline_id=pipeline_id,
            max_items=max_items,
            min_confidence=min_confidence,
        )

    def peek_whispers(
        self,
        *,
        agent_id: str,
        pipeline_id: str | None = None,
        max_items: int = 3,
        min_confidence: float = 0.60,
    ) -> list[Whisper]:
        if self.coordination is None:
            raise NCPStoreUnavailableError(
                "pgvector whisper coordination requires Redis. Set NCP_REDIS_URL and ensure Redis is reachable."
            )
        return self.coordination.peek_whispers(
            agent_id=agent_id,
            pipeline_id=pipeline_id,
            max_items=max_items,
            min_confidence=min_confidence,
        )

    def acknowledge_whispers(self, whisper_ids: Sequence[str]) -> int:
        if self.coordination is None:
            raise NCPStoreUnavailableError(
                "pgvector whisper coordination requires Redis. Set NCP_REDIS_URL and ensure Redis is reachable."
            )
        return self.coordination.acknowledge_whispers(list(whisper_ids))

    def get_working_zone(
        self,
        *,
        pipeline_id: str | None = None,
        layer: str | None = None,
    ) -> Sequence[SubconsciousChunk]:
        with self._connect() as connection:
            rows = self._load_query_rows(
                connection,
                layer=layer,
                pipeline_id=pipeline_id,
                scope=None,
                zone="working",
            )
        return [self._row_to_chunk(row) for row in rows]

    def log_turn_record(self, record: TurnRecord) -> None:
        with self._connect() as connection:
            cursor = connection.cursor()
            try:
                cursor.execute(
                    self._sql(
                        """
                        INSERT INTO {schema}.{prefix}turn_records (
                            turn_id, agent_id, pipeline_id, task, slot, result, result_full, created_at, expires_at
                        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                        ON CONFLICT (turn_id) DO UPDATE SET
                            agent_id = EXCLUDED.agent_id,
                            pipeline_id = EXCLUDED.pipeline_id,
                            task = EXCLUDED.task,
                            slot = EXCLUDED.slot,
                            result = EXCLUDED.result,
                            result_full = EXCLUDED.result_full,
                            created_at = EXCLUDED.created_at,
                            expires_at = EXCLUDED.expires_at
                        """
                    ),
                    (
                        record.turn_id,
                        record.agent_id,
                        record.pipeline_id,
                        record.task,
                        record.slot,
                        record.result,
                        record.result_full,
                        record.created_at,
                        record.expires_at,
                    ),
                )
            finally:
                self._close_cursor(cursor)

    def resolve_recent_ref(self, ref: str) -> TurnRecord | None:
        if not ref.startswith("r:sub/"):
            return None
        turn_id = ref.split("/", 1)[1]
        with self._connect() as connection:
            cursor = connection.cursor()
            try:
                cursor.execute(
                    self._sql("SELECT * FROM {schema}.{prefix}turn_records WHERE turn_id = %s"),
                    (turn_id,),
                )
                row = self._fetchone(cursor)
            finally:
                self._close_cursor(cursor)
        return None if row is None else TurnRecord(**row)

    def log_cost(self, *, agent_id: str, response: NCPResponse) -> None:
        self.log_cost_raw(
            agent_id=agent_id,
            model=response.model,
            input_tokens=response.input_tokens,
            output_tokens=response.output_tokens,
            cost_usd=response.cost_usd,
            pipeline_id=response.pipeline_id,
            turn_id=response.turn_id,
            latency_ms=response.latency_ms,
        )

    def log_cost_raw(
        self,
        *,
        agent_id: str,
        model: str,
        input_tokens: int,
        output_tokens: int,
        cost_usd: float,
        pipeline_id: str | None = None,
        turn_id: str,
        latency_ms: int = 0,
    ) -> None:
        with self._connect() as connection:
            cursor = connection.cursor()
            try:
                cursor.execute(
                    self._sql(
                        """
                        INSERT INTO {schema}.{prefix}cost_log (
                            turn_id, pipeline_id, agent_id, model, input_tokens, output_tokens,
                            cache_read_tokens, cost_usd, latency_ms, logged_at
                        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                        ON CONFLICT (turn_id) DO UPDATE SET
                            pipeline_id = EXCLUDED.pipeline_id,
                            agent_id = EXCLUDED.agent_id,
                            model = EXCLUDED.model,
                            input_tokens = EXCLUDED.input_tokens,
                            output_tokens = EXCLUDED.output_tokens,
                            cache_read_tokens = EXCLUDED.cache_read_tokens,
                            cost_usd = EXCLUDED.cost_usd,
                            latency_ms = EXCLUDED.latency_ms,
                            logged_at = EXCLUDED.logged_at
                        """
                    ),
                    (
                        turn_id,
                        pipeline_id,
                        agent_id,
                        model,
                        input_tokens,
                        output_tokens,
                        0,
                        cost_usd,
                        latency_ms,
                        time.time(),
                    ),
                )
            finally:
                self._close_cursor(cursor)

    def log_conscious(self, conscious: ConsciousBlock, *, snapshot_hash: str) -> None:
        with self._connect() as connection:
            cursor = connection.cursor()
            try:
                cursor.execute(
                    self._sql(
                        """
                        INSERT INTO {schema}.{prefix}conscious_log (
                            agent_id, pipeline_id, snapshot_hash, snapshot_json, logged_at
                        ) VALUES (%s, %s, %s, %s, %s)
                        """
                    ),
                    (
                        conscious.agent_id,
                        conscious.pipeline_id,
                        snapshot_hash,
                        conscious.model_dump_json(),
                        time.time(),
                    ),
                )
            finally:
                self._close_cursor(cursor)

    def get_pipeline_goal_versions(
        self,
        *,
        pipeline_id: str,
        current_agent: str | None = None,
    ) -> dict[str, int]:
        with self._connect() as connection:
            cursor = connection.cursor()
            try:
                cursor.execute(
                    self._sql(
                        """
                        SELECT agent_id, snapshot_json FROM {schema}.{prefix}conscious_log
                        WHERE pipeline_id = %s
                        ORDER BY logged_at DESC
                        """
                    ),
                    (pipeline_id,),
                )
                rows = self._fetchall(cursor)
            finally:
                self._close_cursor(cursor)
        versions: dict[str, int] = {}
        seen_agents: set[str] = set()
        for row in rows:
            agent = str(row["agent_id"])
            if agent in seen_agents:
                continue
            if current_agent is not None and agent == current_agent:
                continue
            seen_agents.add(agent)
            try:
                payload = row["snapshot_json"]
                if isinstance(payload, str):
                    snapshot = json.loads(payload)
                elif isinstance(payload, dict):
                    snapshot = payload
                else:
                    continue
                versions[agent] = int(snapshot.get("goal_version", 1))
            except (json.JSONDecodeError, TypeError, ValueError):
                continue
        return versions

    def _sql(self, statement: str) -> str:
        return statement.format(schema=self.schema, prefix=self.table_prefix)

    def _fetchall(self, cursor: Any) -> list[dict[str, Any]]:
        rows = cursor.fetchall()
        return [self._normalize_row(row, getattr(cursor, "description", None)) for row in rows]

    def _fetchone(self, cursor: Any) -> dict[str, Any] | None:
        row = cursor.fetchone()
        if row is None:
            return None
        return self._normalize_row(row, getattr(cursor, "description", None))

    def _normalize_row(
        self,
        row: Any,
        description: Sequence[Sequence[Any]] | None,
    ) -> dict[str, Any]:
        if isinstance(row, dict):
            return row
        mapping = getattr(row, "_mapping", None)
        if mapping is not None:
            return dict(mapping)
        if description is None:
            raise TypeError("cursor description is required to normalize pgvector rows")
        columns = [str(column[0]) for column in description]
        return {column: row[index] for index, column in enumerate(columns)}

    def _load_query_rows(
        self,
        connection: Any,
        *,
        layer: str | None,
        pipeline_id: str | None,
        scope: str | None,
        zone: str,
    ) -> list[dict[str, Any]]:
        clauses = ["zone = %s"]
        params: list[object] = [zone]
        if layer is not None:
            clauses.append("layer = %s")
            params.append(layer)
        if pipeline_id is None:
            clauses.append("(pipeline_id IS NULL OR scope = 'global')")
        else:
            clauses.append("(pipeline_id = %s OR scope = 'global')")
            params.append(pipeline_id)
        if scope is not None:
            clauses.append("scope = %s")
            params.append(scope)
        cursor = connection.cursor()
        try:
            cursor.execute(
                self._sql(
                    f"SELECT * FROM {{schema}}.{{prefix}}chunks WHERE {' AND '.join(clauses)} ORDER BY created_at DESC"
                ),
                tuple(params),
            )
            return self._fetchall(cursor)
        finally:
            self._close_cursor(cursor)

    def _row_to_chunk(self, row: dict[str, Any]) -> SubconsciousChunk:
        created_at = float(row["created_at"])
        return SubconsciousChunk(
            chunk_id=str(row["chunk_id"]),
            layer=str(row["layer"]),
            content=str(row["content"]),
            src=str(row["src"]),
            written_by=str(row["written_by"]),
            caused_by=row["caused_by"],
            conscious_hash=row["conscious_hash"],
            evidence_id=row["evidence_id"],
            generation=int(row["generation"]),
            base_trust=float(row["base_trust"]),
            result_confidence=row["result_confidence"],
            result_attempts=row["result_attempts"],
            conditions=self._decode_json_list(row["conditions"]),
            valid_while=row["valid_while"],
            expiry=row["expiry"],
            owner=row["owner"],
            chunk_type=str(row["chunk_type"]),
            pipeline_id=row["pipeline_id"],
            scope=str(row["scope"]),
            zone=str(row["zone"]),
            schema_version=int(row["schema_version"]),
            supersedes=row["supersedes"],
            source_refs=self._decode_json_list(row["source_refs"]),
            age_seconds=max(0.0, time.time() - created_at),
        )

    def _decode_json_list(self, value: Any) -> list[str]:
        if isinstance(value, list):
            return [str(item) for item in value]
        if value in (None, ""):
            return []
        if isinstance(value, str):
            return [str(item) for item in json.loads(value)]
        raise TypeError(f"Unsupported JSON list payload: {type(value)!r}")

    def _with_runtime_age(self, chunk: SubconsciousChunk) -> SubconsciousChunk:
        return chunk.model_copy(update={"age_seconds": max(0.0, chunk.age_seconds)})

    def _validate_chunk_for_write(self, chunk: SubconsciousChunk) -> SubconsciousChunk:
        validated = SubconsciousChunk.model_validate(chunk.model_dump())
        return self._with_runtime_age(validated)

    def _assert_src_immutable(self, connection: Any, chunk: SubconsciousChunk) -> None:
        cursor = connection.cursor()
        try:
            cursor.execute(
                self._sql("SELECT src FROM {schema}.{prefix}chunks WHERE chunk_id = %s"),
                (chunk.chunk_id,),
            )
            row = self._fetchone(cursor)
        finally:
            self._close_cursor(cursor)
        if row is None:
            return
        existing_src = str(row["src"])
        if existing_src != chunk.src:
            raise ValueError(
                f"src is immutable for chunk_id={chunk.chunk_id}: existing={existing_src} new={chunk.src}"
            )

    def _is_duplicate(self, connection: Any, chunk: SubconsciousChunk) -> bool:
        cursor = connection.cursor()
        try:
            cursor.execute(
                self._sql(
                    """
                    SELECT content FROM {schema}.{prefix}chunks
                    WHERE zone = %s AND layer = %s AND COALESCE(pipeline_id, '') = COALESCE(%s, '')
                    """
                ),
                (chunk.zone, chunk.layer, chunk.pipeline_id),
            )
            rows = self._fetchall(cursor)
        finally:
            self._close_cursor(cursor)
        for row in rows:
            similarity = SequenceMatcher(None, chunk.content, str(row["content"])).ratio()
            if similarity > 0.92:
                return True
        return False

    def _soft_gc(self, connection: Any) -> None:
        now = time.time()
        for table in ("tombstones", "whispers", "turn_records"):
            cursor = connection.cursor()
            try:
                cursor.execute(
                    self._sql(f"DELETE FROM {{schema}}.{{prefix}}{table} WHERE expires_at <= %s"),
                    (now,),
                )
            finally:
                self._close_cursor(cursor)

    def _hard_gc(self, connection: Any, *, pipeline_id: str | None) -> None:
        clauses = ["zone = 'working'"]
        params: list[object] = []
        if pipeline_id is not None:
            clauses.append("pipeline_id = %s")
            params.append(pipeline_id)
        count_cursor = connection.cursor()
        try:
            count_cursor.execute(
                self._sql(
                    f"SELECT COUNT(*) AS count FROM {{schema}}.{{prefix}}chunks WHERE {' AND '.join(clauses)}"
                ),
                tuple(params),
            )
            row = self._fetchone(count_cursor)
        finally:
            self._close_cursor(count_cursor)
        count = int(row["count"]) if row is not None else 0
        if count <= self.max_working_chunks:
            return
        overflow = count - self.gc_threshold
        stale_cursor = connection.cursor()
        try:
            stale_cursor.execute(
                self._sql(
                    f"""
                    SELECT chunk_id FROM {{schema}}.{{prefix}}chunks
                    WHERE {' AND '.join(clauses)}
                    ORDER BY created_at ASC
                    LIMIT %s
                    """
                ),
                (*params, overflow),
            )
            rows = self._fetchall(stale_cursor)
        finally:
            self._close_cursor(stale_cursor)
        if not rows:
            return
        delete_cursor = connection.cursor()
        try:
            delete_cursor.executemany(
                self._sql("DELETE FROM {schema}.{prefix}chunks WHERE chunk_id = %s"),
                [(str(row["chunk_id"]),) for row in rows],
            )
        finally:
            self._close_cursor(delete_cursor)

    def _close_cursor(self, cursor: Any) -> None:
        close = getattr(cursor, "close", None)
        if callable(close):
            close()


def infra_hint(project_root: str | Path) -> str:
    root = Path(project_root)
    return (
        f"Start local Postgres/pgvector with {root / 'scripts' / 'infra_up.sh'} and set "
        "NCP_PGVECTOR_DSN plus NCP_STORE_TYPE=pgvector when you want to exercise the durable store path."
    )
