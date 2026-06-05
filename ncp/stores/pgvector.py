"""pgvector-backed durable store implementation for the 0.2.0 rollout."""

from __future__ import annotations

from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from difflib import SequenceMatcher
import atexit
import json
import math
from pathlib import Path
from typing import Any
import time

from ncp.config import NCPConfig
from ncp.stores.base import BaseStore, NCPStoreUnavailableError
from ncp.stores.consolidation import cluster_by_tags, find_merge_candidates
from ncp.stores.redis_coordination import RedisCoordination
from ncp.stores.retrieval import (
    DEFAULT_RETRIEVAL_POLICY,
    RetrievalPolicy,
    apply_diversity_limit,
    build_lexical_candidates,
    normalize_result_limit,
    score_trust_recency_candidate,
    score_vector_distance,
)
from ncp.types import CalibrationReport, ConsolidationReport, ConsciousBlock, NCPResponse, SubconsciousChunk, TurnRecord, Whisper


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
    meta JSONB DEFAULT '{{}}'::jsonb,
    embedding vector(1536),
    retrieval_count INTEGER DEFAULT 0,
    last_retrieved_at DOUBLE PRECISION,
    written_at_drift DOUBLE PRECISION DEFAULT 0.0
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
CREATE INDEX IF NOT EXISTS {prefix}idx_chunks_embedding
    ON {schema}.{prefix}chunks
    USING ivfflat (embedding vector_cosine_ops)
    WITH (lists = 100);
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

CREATE TABLE IF NOT EXISTS {schema}.{prefix}drift_history (
    session_id TEXT NOT NULL,
    turn INTEGER NOT NULL,
    drift_score DOUBLE PRECISION NOT NULL,
    ts DOUBLE PRECISION NOT NULL
);

CREATE INDEX IF NOT EXISTS {prefix}idx_drift_session
    ON {schema}.{prefix}drift_history(session_id, turn);
"""


def _default_pgvector_connect(dsn: str) -> Any:
    try:
        import psycopg
    except ModuleNotFoundError as exc:  # pragma: no cover - depends on optional extra
        raise NCPStoreUnavailableError(
            "pgvector support requires psycopg. Install it with: pip install 'neural-context-protocol[pgvector]'"
        ) from exc
    return psycopg.connect(dsn)


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
        min_pool_connections: int = 2,
        max_pool_connections: int = 10,
        redis_url: str | None = None,
        redis_stream: str = "ncp:whispers",
        coordination: RedisCoordination | None = None,
        max_working_chunks: int = 500,
        gc_threshold: int = 400,
        ivfflat_probes: int = 10,
        retrieval_policy: RetrievalPolicy | None = None,
        config: NCPConfig | None = None,
        embedding_adapter: object | None = None,
    ) -> None:
        self.dsn = dsn
        self.schema = _validate_identifier(schema, field="schema")
        self.table_prefix = _validate_identifier(table_prefix, field="table_prefix")
        if connect_factory is not None:
            self._connect_factory = connect_factory
            self._pool: Any = None
        else:
            try:
                from psycopg_pool import ConnectionPool as _ConnectionPool  # type: ignore[import]
                self._pool = _ConnectionPool(
                    conninfo=dsn,
                    min_size=min_pool_connections,
                    max_size=max_pool_connections,
                    open=True,
                )
                self._connect_factory = lambda _dsn: self._pool.getconn()
                atexit.register(self.close)
            except ImportError:  # pragma: no cover - psycopg_pool not installed
                self._pool = None
                self._connect_factory = _default_pgvector_connect
        self.coordination = coordination or (
            RedisCoordination(redis_url, stream=redis_stream) if redis_url else None
        )
        self.max_working_chunks = max_working_chunks
        self.gc_threshold = gc_threshold
        self._ivfflat_probes = ivfflat_probes

        from ncp.stores.rerank import Reranker
        from ncp.config import load_config
        try:
            cfg = config or load_config()
            self.reranker = Reranker(cfg)
            self.retrieval_policy = retrieval_policy or RetrievalPolicy(
                generation_penalty_base=cfg.retrieval_generation_penalty_base
            )
        except Exception:
            self.retrieval_policy = retrieval_policy or DEFAULT_RETRIEVAL_POLICY

            class DummyConfig:
                rerank_enabled = False
                rerank_provider = "local"
                rerank_model = None
                values: dict = {}
            self.reranker = Reranker(DummyConfig())  # type: ignore[arg-type]

        self._embedding_adapter = embedding_adapter
        self._init_db()

    @contextmanager
    def _connect(self) -> Iterator[Any]:
        connection = self._connect_with_retry()
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
            if self._pool is not None:
                try:
                    self._pool.putconn(connection)
                except Exception:
                    try:
                        connection.close()
                    except Exception:
                        pass
            else:
                try:
                    connection.close()
                except Exception:
                    pass

    def close(self) -> None:
        """Close all pooled connections. Call when the store is no longer needed."""
        if self._pool is not None:
            try:
                self._pool.close()
            except Exception:
                pass
            self._pool = None

    def _connect_with_retry(self, *, attempts: int = 2, delay_seconds: float = 0.1) -> Any:
        last_exc: Exception | None = None
        for attempt in range(attempts):
            try:
                return self._connect_factory(self.dsn)
            except NCPStoreUnavailableError:
                raise
            except Exception as exc:
                last_exc = exc
                if attempt < attempts - 1:
                    time.sleep(delay_seconds)
        raise NCPStoreUnavailableError(
            f"pgvector store unavailable at {self.dsn} after {attempts} attempts: {last_exc}"
        ) from last_exc

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
        if self._embedding_adapter is not None and chunk.embedding is None:
            chunk = chunk.model_copy(
                update={"embedding": self._embedding_adapter.embed(chunk.content)}
            )
        with self._connect() as connection:
            self._soft_gc(connection)
            self._assert_src_immutable(connection, chunk)
            if self._is_duplicate(connection, chunk):
                return False
            cursor = connection.cursor()
            try:
                embedding_val = (
                    "[" + ",".join(str(f) for f in chunk.embedding) + "]"
                    if chunk.embedding is not None
                    else None
                )
                cursor.execute(
                    self._sql(
                        """
                        INSERT INTO {schema}.{prefix}chunks (
                            chunk_id, pipeline_id, scope, zone, layer, chunk_type, content, src,
                            written_by, caused_by, conscious_hash, evidence_id, version, supersedes,
                            source_refs, schema_version, created_at, base_trust, generation,
                            result_confidence, result_attempts, conditions, valid_while, expiry, owner, meta,
                            embedding, written_at_drift
                        ) VALUES (
                            %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                            %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s
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
                            meta = EXCLUDED.meta,
                            embedding = EXCLUDED.embedding,
                            written_at_drift = EXCLUDED.written_at_drift
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
                        embedding_val,
                        chunk.written_at_drift,
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
        min_score: float = 0.01,
        layer: str | None = None,
        pipeline_id: str | None = None,
        scope: str | None = None,
        zone: str = "working",
        retrieval_mode: str = "hybrid",
        embedding: list[float] | None = None,
        diversity_limit: int = 2,
        fallback_to_trust_recency: bool = False,
    ) -> list[SubconsciousChunk]:
        _VALID_RETRIEVAL_MODES = ("hybrid", "trust_recency", "vector")
        if retrieval_mode not in _VALID_RETRIEVAL_MODES:
            raise ValueError(
                f"Unknown retrieval_mode {retrieval_mode!r}; expected one of {_VALID_RETRIEVAL_MODES}"
            )

        if retrieval_mode == "vector":
            return self._query_vector(
                text=text, embedding=embedding, k=k, min_score=min_score,
                layer=layer, pipeline_id=pipeline_id, scope=scope, zone=zone,
                diversity_limit=diversity_limit,
            )
        if embedding is None and self._embedding_adapter is not None:
            embedding = self._embedding_adapter.embed(text)
        if embedding is not None and len(embedding) != 1536:
            raise ValueError(f"embedding must have 1536 dimensions, got {len(embedding)}")

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

        policy = self.retrieval_policy
        now = time.time()
        candidates: list[SubconsciousChunk] = []

        if retrieval_mode == "trust_recency":
            for row in rows:
                score = score_trust_recency_candidate(
                    policy,
                    created_at=float(row["created_at"]),
                    now=now,
                    base_trust=float(row["base_trust"]),
                    generation=int(row["generation"]),
                    written_at_drift=float(row["written_at_drift"]) if row.get("written_at_drift") is not None else 0.0,
                )
                if score < min_score:
                    continue
                chunk = self._row_to_chunk(row)
                chunk.relevance = max(0.0, min(1.0, score))
                candidates.append(chunk)
        else:
            lexical_candidates = build_lexical_candidates(
                text,
                [str(row["content"]) for row in rows],
            )

            for row, lexical_candidate in zip(rows, lexical_candidates, strict=True):
                if lexical_candidate.lexical_signal is None:
                    continue

                age_seconds = max(0.0, now - float(row["created_at"]))
                vector_score = self._vector_similarity_score(
                    query_embedding=embedding,
                    row_embedding=row.get("embedding"),
                )
                hybrid_score = policy.score_with_vector(
                    bm25_normalized=lexical_candidate.lexical_signal,
                    vector_normalized=vector_score,
                    age_seconds=age_seconds,
                    base_trust=float(row["base_trust"]),
                    generation=int(row["generation"]),
                    written_at_drift=float(row["written_at_drift"]) if row.get("written_at_drift") is not None else 0.0,
                )
                if hybrid_score < min_score:
                    continue
                chunk = self._row_to_chunk(row)
                chunk.relevance = max(0.0, min(1.0, hybrid_score))
                candidates.append(chunk)

        ranked = sorted(candidates, key=lambda c: c.relevance, reverse=True)
        result_limit = normalize_result_limit(k)

        if self.reranker is not None and self.reranker.enabled:
            candidates_to_rerank = ranked[:result_limit * 4]
            ranked = self.reranker.rerank(text, candidates_to_rerank)

        results = apply_diversity_limit(
            ranked,
            k=result_limit,
            diversity_limit=diversity_limit,
            author_getter=lambda chunk: str(chunk.written_by),
        )

        if results:
            now = time.time()
            chunk_ids = [c.chunk_id for c in results]
            with self._connect() as connection:
                cursor = connection.cursor()
                try:
                    cursor.execute(
                        self._sql(
                            "UPDATE {schema}.{prefix}chunks"
                            " SET retrieval_count = retrieval_count + 1, last_retrieved_at = %s"
                            " WHERE chunk_id = ANY(%s)"
                        ),
                        (now, chunk_ids),
                    )
                finally:
                    self._close_cursor(cursor)
            for chunk in results:
                chunk.retrieval_count += 1
                chunk.last_retrieved_at = now

        return results

    def _query_vector(
        self,
        *,
        text: str,
        embedding: list[float] | None,
        k: int,
        min_score: float,
        layer: str | None,
        pipeline_id: str | None,
        scope: str | None,
        zone: str,
        diversity_limit: int = 2,
    ) -> list[SubconsciousChunk]:
        if embedding is None:
            if self._embedding_adapter is not None:
                embedding = self._embedding_adapter.embed(text)
            else:
                raise ValueError("retrieval_mode='vector' requires an embedding to be provided")
        if len(embedding) != 1536:
            raise ValueError(f"embedding must have 1536 dimensions, got {len(embedding)}")
        embedding_str = "[" + ",".join(str(f) for f in embedding) + "]"

        # Build WHERE clause (same filter logic as _load_query_rows)
        where_clauses = ["zone = %s", "embedding IS NOT NULL"]
        where_params: list[object] = [zone]
        if layer is not None:
            where_clauses.append("layer = %s")
            where_params.append(layer)
        if pipeline_id is None:
            where_clauses.append("(pipeline_id IS NULL OR scope = 'global')")
        else:
            where_clauses.append("(pipeline_id = %s OR scope = 'global')")
            where_params.append(pipeline_id)
        if scope is not None:
            where_clauses.append("scope = %s")
            where_params.append(scope)

        # Always fetch k*4 to give the diversity loop enough candidates.
        result_limit = normalize_result_limit(k)
        limit = result_limit * 4
        # Params order: embedding (SELECT), WHERE params, embedding (ORDER BY), LIMIT
        all_params = tuple([embedding_str] + where_params + [embedding_str, limit])

        with self._connect() as connection:
            cursor = connection.cursor()
            try:
                cursor.execute("SET LOCAL ivfflat.probes = %s", (self._ivfflat_probes,))
                cursor.execute(
                    self._sql(
                        "SELECT *, (embedding <=> %s::vector) AS vec_distance"
                        f" FROM {{schema}}.{{prefix}}chunks"
                        f" WHERE {' AND '.join(where_clauses)}"
                        " ORDER BY embedding <=> %s::vector LIMIT %s"
                    ),
                    all_params,
                )
                rows = self._fetchall(cursor)
            finally:
                self._close_cursor(cursor)

        results: list[SubconsciousChunk] = []
        for row in rows:
            score = score_vector_distance(
                None if row.get("vec_distance") is None else float(row["vec_distance"])
            )
            if score < min_score:
                continue
            chunk = self._row_to_chunk(row)
            chunk.relevance = max(0.0, min(1.0, score))
            results.append(chunk)

        if self.reranker is not None and self.reranker.enabled:
            results = self.reranker.rerank(text, results)

        results = apply_diversity_limit(
            results,
            k=result_limit,
            diversity_limit=diversity_limit,
            author_getter=lambda chunk: str(chunk.written_by),
        )

        if results:
            now = time.time()
            chunk_ids = [c.chunk_id for c in results]
            with self._connect() as connection:
                cursor = connection.cursor()
                try:
                    cursor.execute(
                        self._sql(
                            "UPDATE {schema}.{prefix}chunks"
                            " SET retrieval_count = retrieval_count + 1, last_retrieved_at = %s"
                            " WHERE chunk_id = ANY(%s)"
                        ),
                        (now, chunk_ids),
                    )
                finally:
                    self._close_cursor(cursor)
            for chunk in results:
                chunk.retrieval_count += 1
                chunk.last_retrieved_at = now

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

    def whisper_pending(self, whisper_id: str) -> bool:
        """Return True if the whisper is still queued in the local Postgres whispers table."""
        import time as _time
        now = _time.time()
        with self._connect() as connection:
            cursor = connection.execute(
                f"SELECT 1 FROM {self.schema}.{self.table_prefix}whispers"  # noqa: S608
                " WHERE whisper_id = %s AND expires_at > %s",
                (whisper_id, now),
            )
            return cursor.fetchone() is not None

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

    def log_drift_history(self, *, session_id: str, turn: int, drift_score: float) -> None:
        with self._connect() as connection:
            cursor = connection.cursor()
            try:
                cursor.execute(
                    self._sql(
                        """
                        INSERT INTO {schema}.{prefix}drift_history (session_id, turn, drift_score, ts)
                        VALUES (%s, %s, %s, %s)
                        """
                    ),
                    (session_id, turn, drift_score, time.time()),
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

    def consolidate(
        self,
        *,
        pipeline_id: str | None = None,
        dry_run: bool = False,
        similarity_threshold: float = 0.65,
        trust_floor: float = 0.10,
    ) -> ConsolidationReport:
        started = time.monotonic()
        report = ConsolidationReport(dry_run=dry_run, pipeline_id=pipeline_id)

        with self._connect() as connection:
            cursor = connection.cursor()
            try:
                if pipeline_id is not None:
                    cursor.execute(
                        self._sql(
                            "SELECT * FROM {schema}.{prefix}chunks"
                            " WHERE chunk_id NOT IN (SELECT chunk_id FROM {schema}.{prefix}tombstones)"
                            " AND pipeline_id = %s"
                        ),
                        (pipeline_id,),
                    )
                else:
                    cursor.execute(
                        self._sql(
                            "SELECT * FROM {schema}.{prefix}chunks"
                            " WHERE chunk_id NOT IN (SELECT chunk_id FROM {schema}.{prefix}tombstones)"
                        )
                    )
                rows = self._fetchall(cursor)
            finally:
                self._close_cursor(cursor)

        all_chunks = [self._row_to_chunk(row) for row in rows]
        eligible = [c for c in all_chunks if c.base_trust >= trust_floor]
        report.skipped += len(all_chunks) - len(eligible)
        clusters = cluster_by_tags(eligible)
        report.clusters_scanned = len(clusters)

        for cluster in clusters:
            candidates = find_merge_candidates(cluster, similarity_threshold=similarity_threshold)
            for keeper, losers in candidates:
                loser_ids = [c.chunk_id for c in losers]
                report.merge_log.append({
                    "kept": keeper.chunk_id,
                    "merged": loser_ids,
                    "layer": keeper.layer,
                    "zone": keeper.zone,
                    "pipeline_id": keeper.pipeline_id,
                })
                if not dry_run:
                    with self._connect() as connection:
                        cursor = connection.cursor()
                        try:
                            for loser_id in loser_ids:
                                cursor.execute(
                                    self._sql("DELETE FROM {schema}.{prefix}chunks WHERE chunk_id = %s"),
                                    (loser_id,),
                                )
                                cursor.execute(
                                    self._sql(
                                        "INSERT INTO {schema}.{prefix}tombstones (chunk_id, forward_ref, tombstoned_at, expires_at)"
                                        " VALUES (%s, %s, %s, %s) ON CONFLICT DO NOTHING"
                                    ),
                                    (loser_id, keeper.chunk_id, time.time(), time.time() + 86400),
                                )
                            supersedes_json = json.dumps(loser_ids)
                            new_gen = keeper.generation + 1
                            cursor.execute(
                                self._sql(
                                    "UPDATE {schema}.{prefix}chunks SET generation = %s, supersedes = %s WHERE chunk_id = %s"
                                ),
                                (new_gen, supersedes_json, keeper.chunk_id),
                            )
                            connection.commit()
                        finally:
                            self._close_cursor(cursor)
                report.merged += 1
                report.tombstoned += len(loser_ids)

            report.skipped += sum(
                1 for c in cluster
                if not any(
                    c.chunk_id == k.chunk_id or c.chunk_id in [m.chunk_id for m in ls]
                    for k, ls in candidates
                )
            )

        if not dry_run and report.merged > 0:
            self._emit_consolidation_whisper(pipeline_id=pipeline_id)

        report.duration_seconds = time.monotonic() - started
        return report

    def calibrate(
        self,
        *,
        pipeline_id: str | None = None,
        chunk_id: str | None = None,
        trust: float | None = None,
        dry_run: bool = False,
        decay_factor: float = 0.85,
        recency_half_life_seconds: float = 14400,
        feedback_mode: bool = False,
        feedback_weight: float = 0.15,
    ) -> CalibrationReport:
        """Re-score base_trust on live chunks.

        Manual mode: chunk_id + trust sets a specific chunk's base_trust directly.
        Batch mode: pipeline_id applies decay to eligible chunks.
        """
        started = time.monotonic()
        report = CalibrationReport(dry_run=dry_run, pipeline_id=pipeline_id)

        if chunk_id is not None:
            # --- Manual pinpoint override ---
            if trust is None:
                raise ValueError("trust is required when chunk_id is provided")
            if not 0.0 <= trust <= 1.0:
                raise ValueError("trust must be between 0.0 and 1.0")
            with self._connect() as connection:
                cursor = connection.cursor()
                try:
                    cursor.execute(
                        self._sql(
                            "SELECT chunk_id, base_trust, src FROM {schema}.{prefix}chunks"
                            " WHERE chunk_id = %s AND chunk_id NOT IN"
                            " (SELECT chunk_id FROM {schema}.{prefix}tombstones)"
                        ),
                        (chunk_id,),
                    )
                    row = self._fetchone(cursor)
                finally:
                    self._close_cursor(cursor)

                if row is None:
                    report.skipped += 1
                    report.duration_seconds = time.monotonic() - started
                    return report

                old_trust = float(row["base_trust"])
                report.change_log.append({
                    "chunk_id": chunk_id,
                    "old_trust": old_trust,
                    "new_trust": trust,
                    "reason": "manual_override",
                })
                if not dry_run:
                    cursor = connection.cursor()
                    try:
                        cursor.execute(
                            self._sql(
                                "UPDATE {schema}.{prefix}chunks SET base_trust = %s WHERE chunk_id = %s"
                            ),
                            (trust, chunk_id),
                        )
                    finally:
                        self._close_cursor(cursor)
                report.adjusted += 1
        else:
            # --- Batch decay mode ---
            now = time.time()
            cutoff_age = recency_half_life_seconds

            with self._connect() as connection:
                cursor = connection.cursor()
                try:
                    if pipeline_id is not None:
                        cursor.execute(
                            self._sql(
                                "SELECT chunk_id, base_trust, src, generation, created_at,"
                                " retrieval_count FROM {schema}.{prefix}chunks"
                                " WHERE chunk_id NOT IN (SELECT chunk_id FROM {schema}.{prefix}tombstones)"
                                " AND pipeline_id = %s"
                            ),
                            (pipeline_id,),
                        )
                    else:
                        cursor.execute(
                            self._sql(
                                "SELECT chunk_id, base_trust, src, generation, created_at,"
                                " retrieval_count FROM {schema}.{prefix}chunks"
                                " WHERE chunk_id NOT IN (SELECT chunk_id FROM {schema}.{prefix}tombstones)"
                            )
                        )
                    rows = self._fetchall(cursor)
                finally:
                    self._close_cursor(cursor)

                updates: list[tuple[float, str]] = []
                for row in rows:
                    cid = str(row["chunk_id"])
                    src = str(row["src"])
                    base_trust = float(row["base_trust"])
                    generation = int(row["generation"])
                    age_seconds = max(0.0, now - float(row["created_at"]))
                    rc = int(row["retrieval_count"]) if row["retrieval_count"] is not None else 0

                    if src == "user_verified":
                        report.protected += 1
                        continue

                    if not feedback_mode:
                        if age_seconds > cutoff_age and base_trust > 0.5 and generation == 0:
                            new_trust = max(0.0, base_trust * decay_factor)
                            report.change_log.append({
                                "chunk_id": cid, "old_trust": base_trust,
                                "new_trust": new_trust, "reason": "batch_decay",
                            })
                            updates.append((new_trust, cid))
                            report.adjusted += 1
                        else:
                            report.skipped += 1
                    else:
                        if rc > 0:
                            boost = feedback_weight * min(1.0, rc / 10)
                            new_trust = min(1.0, base_trust + boost)
                            report.change_log.append({
                                "chunk_id": cid, "old_trust": base_trust,
                                "new_trust": new_trust, "reason": "retrieval_feedback",
                                "retrieval_count": rc,
                            })
                            updates.append((new_trust, cid))
                            report.feedback_adjusted += 1
                        else:
                            report.skipped += 1

                if not dry_run and updates:
                    update_cursor = connection.cursor()
                    try:
                        for new_trust, cid in updates:
                            update_cursor.execute(
                                self._sql(
                                    "UPDATE {schema}.{prefix}chunks SET base_trust = %s WHERE chunk_id = %s"
                                ),
                                (new_trust, cid),
                            )
                        connection.commit()
                    finally:
                        self._close_cursor(update_cursor)

        report.duration_seconds = time.monotonic() - started
        return report

    def viz_data(self, *, pipeline_id: str | None = None) -> dict[str, object]:
        """Return structured data for the operator viz view."""
        now = time.time()
        live_filter = (
            f"chunk_id NOT IN (SELECT chunk_id FROM {self._table_name('tombstones')})"
        )

        with self._connect() as connection:
            # 1. Chunk distribution: layer x zone counts (live chunks only)
            cursor = connection.cursor()
            try:
                if pipeline_id is not None:
                    cursor.execute(
                        f"SELECT layer, zone, COUNT(*) AS count FROM {self._table_name('chunks')}"
                        f" WHERE {live_filter} AND pipeline_id = %s"
                        " GROUP BY layer, zone ORDER BY layer, zone",
                        (pipeline_id,),
                    )
                else:
                    cursor.execute(
                        f"SELECT layer, zone, COUNT(*) AS count FROM {self._table_name('chunks')}"
                        f" WHERE {live_filter}"
                        " GROUP BY layer, zone ORDER BY layer, zone"
                    )
                dist_rows = self._fetchall(cursor)
            finally:
                self._close_cursor(cursor)

            chunk_distribution = [
                {"layer": str(r["layer"]), "zone": str(r["zone"]), "count": int(r["count"])}
                for r in dist_rows
            ]

            # 2. Age brackets
            bracket_sql = (
                f"SELECT "
                f"  CASE "
                f"    WHEN (%s - created_at) < 3600 THEN '<1h' "
                f"    WHEN (%s - created_at) < 14400 THEN '1-4h' "
                f"    WHEN (%s - created_at) < 86400 THEN '4-24h' "
                f"    ELSE '>24h' "
                f"  END AS bracket, "
                f"  COUNT(*) AS count, "
                f"  AVG(base_trust) AS avg_trust "
                f"FROM {self._table_name('chunks')} "
                f"WHERE {live_filter}"
            )
            bracket_params: list[object] = [now, now, now, now]
            if pipeline_id is not None:
                bracket_sql += " AND pipeline_id = %s"
                bracket_params.append(pipeline_id)
            bracket_sql += " GROUP BY bracket ORDER BY bracket"

            cursor = connection.cursor()
            try:
                cursor.execute(bracket_sql, tuple(bracket_params))
                bracket_rows = self._fetchall(cursor)
            finally:
                self._close_cursor(cursor)

            # Top layer per bracket
            bracket_top_layer: dict[str, str] = {}
            for bracket_label, age_min, age_max in [
                ("<1h", 0, 3600),
                ("1-4h", 3600, 14400),
                ("4-24h", 14400, 86400),
                (">24h", 86400, None),
            ]:
                tl_sql = (
                    f"SELECT layer, COUNT(*) AS cnt FROM {self._table_name('chunks')}"
                    f" WHERE (%s - created_at) >= %s AND {live_filter}"
                )
                tl_params: list[object] = [now, age_min]
                if age_max is not None:
                    tl_sql += " AND (%s - created_at) < %s"
                    tl_params.extend([now, age_max])
                if pipeline_id is not None:
                    tl_sql += " AND pipeline_id = %s"
                    tl_params.append(pipeline_id)
                tl_sql += " GROUP BY layer ORDER BY cnt DESC LIMIT 1"
                cursor = connection.cursor()
                try:
                    cursor.execute(tl_sql, tuple(tl_params))
                    tl_row = self._fetchone(cursor)
                finally:
                    self._close_cursor(cursor)
                if tl_row is not None:
                    bracket_top_layer[bracket_label] = str(tl_row["layer"])

            age_brackets = [
                {
                    "bracket": str(r["bracket"]),
                    "count": int(r["count"]),
                    "avg_trust": round(float(r["avg_trust"]), 4) if r["avg_trust"] is not None else 0.0,
                    "top_layer": bracket_top_layer.get(str(r["bracket"]), "-"),
                }
                for r in bracket_rows
            ]

            # 3. Top chunks by base_trust DESC (live only)
            cursor = connection.cursor()
            try:
                if pipeline_id is not None:
                    cursor.execute(
                        f"SELECT chunk_id, layer, zone, pipeline_id, base_trust, created_at"
                        f" FROM {self._table_name('chunks')}"
                        f" WHERE {live_filter} AND pipeline_id = %s"
                        " ORDER BY base_trust DESC, created_at DESC LIMIT 5",
                        (pipeline_id,),
                    )
                else:
                    cursor.execute(
                        f"SELECT chunk_id, layer, zone, pipeline_id, base_trust, created_at"
                        f" FROM {self._table_name('chunks')}"
                        f" WHERE {live_filter}"
                        " ORDER BY base_trust DESC, created_at DESC LIMIT 5"
                    )
                top_rows = self._fetchall(cursor)
            finally:
                self._close_cursor(cursor)

            top_chunks = [
                {
                    "chunk_id": str(r["chunk_id"])[:16],
                    "layer": str(r["layer"]),
                    "zone": str(r["zone"]),
                    "pipeline_id": r["pipeline_id"],
                    "base_trust": float(r["base_trust"]),
                    "age_seconds": round(now - float(r["created_at"]), 1),
                }
                for r in top_rows
            ]

            # 4. Pipeline summary (live chunks only)
            cursor = connection.cursor()
            try:
                if pipeline_id is not None:
                    cursor.execute(
                        f"SELECT pipeline_id, COUNT(*) AS chunk_count, MAX(created_at) AS last_activity"
                        f" FROM {self._table_name('chunks')}"
                        f" WHERE {live_filter} AND pipeline_id = %s"
                        " GROUP BY pipeline_id ORDER BY last_activity DESC",
                        (pipeline_id,),
                    )
                else:
                    cursor.execute(
                        f"SELECT pipeline_id, COUNT(*) AS chunk_count, MAX(created_at) AS last_activity"
                        f" FROM {self._table_name('chunks')}"
                        f" WHERE {live_filter} AND pipeline_id IS NOT NULL"
                        " GROUP BY pipeline_id ORDER BY last_activity DESC LIMIT 20"
                    )
                pipe_rows = self._fetchall(cursor)
            finally:
                self._close_cursor(cursor)

            pipeline_summary = [
                {
                    "pipeline_id": str(r["pipeline_id"]),
                    "chunk_count": int(r["chunk_count"]),
                    "last_activity": float(r["last_activity"]),
                }
                for r in pipe_rows
                if r["pipeline_id"] is not None
            ]

        # 5. Whisper queue (from Redis coordination if available)
        whisper_queue: dict[str, object]
        if self.coordination is not None:
            try:
                stats = self.coordination.whisper_stats(pipeline_id=pipeline_id)
                by_type = stats.get("by_type", {})
                whisper_queue = {
                    "total": int(stats.get("count", 0)),
                    "by_type": {str(k): int(v) for k, v in by_type.items()} if isinstance(by_type, dict) else {},
                }
            except Exception:
                whisper_queue = {"total": 0, "by_type": {}}
        else:
            whisper_queue = {"total": 0, "by_type": {}}

        return {
            "chunk_distribution": chunk_distribution,
            "age_brackets": age_brackets,
            "top_chunks": top_chunks,
            "pipeline_summary": pipeline_summary,
            "whisper_queue": whisper_queue,
        }

    def _emit_consolidation_whisper(self, *, pipeline_id: str | None) -> None:
        whisper = Whisper(
            from_agent="ncp_consolidator",
            target="*",
            whisper_type="consolidation_ready",
            payload=f"consolidation_complete pipeline:{pipeline_id or 'all'}",
            confidence=1.0,
            pipeline_id=pipeline_id,
        )
        try:
            self.emit_whisper(whisper)
        except Exception:
            pass

    def status_detail(self, *, pipeline_id: str | None = None) -> dict[str, object]:
        with self._connect() as connection:
            overview = {
                "chunk_count": self._count_rows(connection, "chunks", pipeline_id=pipeline_id),
                "tombstone_count": self._count_rows(connection, "tombstones"),
                "turn_record_count": self._count_rows(connection, "turn_records", pipeline_id=pipeline_id),
                "conscious_snapshot_count": self._count_rows(connection, "conscious_log", pipeline_id=pipeline_id),
                "cost_entry_count": self._count_rows(connection, "cost_log", pipeline_id=pipeline_id),
            }
            overview["pipeline_count"] = self._count_distinct_pipelines(connection)
            overview["cost_usd_total"] = self._sum_cost(connection, pipeline_id=pipeline_id)
            latest_chunk = self._max_column(connection, "chunks", "created_at", pipeline_id=pipeline_id)
            latest_turn = self._max_column(connection, "turn_records", "created_at", pipeline_id=pipeline_id)
            latest_cost = self._max_column(connection, "cost_log", "logged_at", pipeline_id=pipeline_id)
            layer_counts = self._layer_counts(connection, pipeline_id=pipeline_id)
            recent_pipelines = self._recent_pipelines(connection, pipeline_id=pipeline_id)

        whisper_stats = (
            self.coordination.whisper_stats(pipeline_id=pipeline_id)
            if self.coordination is not None
            else {"count": 0, "last_activity_at": None}
        )
        overview["whisper_count"] = int(whisper_stats["count"] or 0)
        activity_candidates = [
            value
            for value in (latest_chunk, latest_turn, latest_cost, whisper_stats["last_activity_at"])
            if value is not None
        ]
        overview["last_activity_at"] = max(activity_candidates) if activity_candidates else None
        return {
            "overview": overview,
            "layer_counts": layer_counts,
            "recent_pipelines": recent_pipelines,
        }

    def cost_summary(
        self,
        *,
        pipeline_id: str | None = None,
        limit: int = 10,
    ) -> dict[str, object]:
        with self._connect() as connection:
            summary = self._cost_summary_row(connection, pipeline_id=pipeline_id)
            by_agent = self._cost_group_rows(connection, group_by="agent_id", pipeline_id=pipeline_id)
            by_model = self._cost_group_rows(connection, group_by="model", pipeline_id=pipeline_id)
            recent_entries = self._recent_cost_rows(connection, pipeline_id=pipeline_id, limit=limit)
        return {
            "summary": summary,
            "by_agent": by_agent,
            "by_model": by_model,
            "recent_entries": recent_entries,
        }

    def _sql(self, statement: str) -> str:
        return statement.format(schema=self.schema, prefix=self.table_prefix)

    def _table_name(self, logical_name: str) -> str:
        return f"{self.schema}.{self.table_prefix}{logical_name}"

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

    def _count_rows(self, connection: Any, table: str, *, pipeline_id: str | None = None) -> int:
        cursor = connection.cursor()
        try:
            statement = f"SELECT COUNT(*) AS count FROM {self._table_name(table)}"
            params: tuple[object, ...] = ()
            if pipeline_id is not None and table in {"chunks", "turn_records", "conscious_log", "cost_log"}:
                statement += " WHERE pipeline_id = %s"
                params = (pipeline_id,)
            cursor.execute(statement, params)
            row = self._fetchone(cursor)
        finally:
            self._close_cursor(cursor)
        return int(row["count"] if row is not None else 0)

    def _count_distinct_pipelines(self, connection: Any) -> int:
        cursor = connection.cursor()
        try:
            cursor.execute(f"SELECT COUNT(DISTINCT pipeline_id) AS count FROM {self._table_name('chunks')}")
            row = self._fetchone(cursor)
        finally:
            self._close_cursor(cursor)
        return int(row["count"] if row is not None else 0)

    def _sum_cost(self, connection: Any, *, pipeline_id: str | None = None) -> float:
        cursor = connection.cursor()
        try:
            statement = f"SELECT COALESCE(SUM(cost_usd), 0.0) AS total FROM {self._table_name('cost_log')}"
            params: tuple[object, ...] = ()
            if pipeline_id is not None:
                statement += " WHERE pipeline_id = %s"
                params = (pipeline_id,)
            cursor.execute(statement, params)
            row = self._fetchone(cursor)
        finally:
            self._close_cursor(cursor)
        return float(row["total"] if row is not None else 0.0)

    def _max_column(
        self,
        connection: Any,
        table: str,
        column: str,
        *,
        pipeline_id: str | None = None,
    ) -> float | None:
        cursor = connection.cursor()
        try:
            statement = f"SELECT MAX({column}) AS latest FROM {self._table_name(table)}"
            params: tuple[object, ...] = ()
            if pipeline_id is not None and table in {"chunks", "turn_records", "conscious_log", "cost_log"}:
                statement += " WHERE pipeline_id = %s"
                params = (pipeline_id,)
            cursor.execute(statement, params)
            row = self._fetchone(cursor)
        finally:
            self._close_cursor(cursor)
        if row is None or row["latest"] is None:
            return None
        return float(row["latest"])

    def _layer_counts(self, connection: Any, *, pipeline_id: str | None = None) -> dict[str, int]:
        cursor = connection.cursor()
        try:
            statement = (
                f"SELECT layer, COUNT(*) AS count FROM {self._table_name('chunks')}"
                + (" WHERE pipeline_id = %s" if pipeline_id is not None else "")
                + " GROUP BY layer ORDER BY count DESC, layer ASC"
            )
            cursor.execute(statement, () if pipeline_id is None else (pipeline_id,))
            rows = self._fetchall(cursor)
        finally:
            self._close_cursor(cursor)
        return {str(row["layer"]): int(row["count"]) for row in rows}

    def _recent_pipelines(self, connection: Any, *, pipeline_id: str | None = None) -> list[dict[str, object]]:
        cursor = connection.cursor()
        try:
            statement = (
                "SELECT pipeline_id, COUNT(*) AS chunk_count, MAX(created_at) AS last_chunk_at "
                f"FROM {self._table_name('chunks')} "
            )
            params: tuple[object, ...] = ()
            if pipeline_id is None:
                statement += "WHERE pipeline_id IS NOT NULL "
            else:
                statement += "WHERE pipeline_id = %s "
                params = (pipeline_id,)
            statement += "GROUP BY pipeline_id ORDER BY last_chunk_at DESC LIMIT 5"
            cursor.execute(statement, params)
            rows = self._fetchall(cursor)
        finally:
            self._close_cursor(cursor)
        return [
            {
                "pipeline_id": str(row["pipeline_id"]),
                "chunk_count": int(row["chunk_count"]),
                "last_chunk_at": float(row["last_chunk_at"]),
            }
            for row in rows
            if row["pipeline_id"] is not None
        ]

    def _cost_summary_row(self, connection: Any, *, pipeline_id: str | None = None) -> dict[str, object]:
        cursor = connection.cursor()
        try:
            statement = (
                "SELECT "
                "COALESCE(SUM(cost_usd), 0.0) AS cost_usd_total, "
                "COALESCE(SUM(input_tokens), 0) AS input_tokens_total, "
                "COALESCE(SUM(output_tokens), 0) AS output_tokens_total, "
                "COALESCE(SUM(cache_read_tokens), 0) AS cache_read_tokens_total, "
                "COUNT(*) AS entry_count, "
                "COALESCE(AVG(latency_ms), 0.0) AS avg_latency_ms "
                f"FROM {self._table_name('cost_log')}"
            )
            params: tuple[object, ...] = ()
            if pipeline_id is not None:
                statement += " WHERE pipeline_id = %s"
                params = (pipeline_id,)
            cursor.execute(statement, params)
            row = self._fetchone(cursor)
        finally:
            self._close_cursor(cursor)
        return {
            "cost_usd_total": float(row["cost_usd_total"]),
            "input_tokens_total": int(row["input_tokens_total"]),
            "output_tokens_total": int(row["output_tokens_total"]),
            "cache_read_tokens_total": int(row["cache_read_tokens_total"]),
            "entry_count": int(row["entry_count"]),
            "avg_latency_ms": float(row["avg_latency_ms"]),
        }

    def _cost_group_rows(
        self,
        connection: Any,
        *,
        group_by: str,
        pipeline_id: str | None = None,
    ) -> list[dict[str, object]]:
        cursor = connection.cursor()
        try:
            statement = (
                f"SELECT {group_by}, COUNT(*) AS turns, COALESCE(SUM(cost_usd), 0.0) AS cost_usd_total "
                f"FROM {self._table_name('cost_log')}"
            )
            params: tuple[object, ...] = ()
            if pipeline_id is not None:
                statement += " WHERE pipeline_id = %s"
                params = (pipeline_id,)
            statement += f" GROUP BY {group_by} ORDER BY cost_usd_total DESC, {group_by} ASC"
            cursor.execute(statement, params)
            rows = self._fetchall(cursor)
        finally:
            self._close_cursor(cursor)
        return [
            {
                group_by: str(row[group_by]),
                "turns": int(row["turns"]),
                "cost_usd_total": float(row["cost_usd_total"]),
            }
            for row in rows
        ]

    def _recent_cost_rows(
        self,
        connection: Any,
        *,
        pipeline_id: str | None = None,
        limit: int,
    ) -> list[dict[str, object]]:
        cursor = connection.cursor()
        try:
            statement = (
                "SELECT turn_id, pipeline_id, agent_id, model, input_tokens, output_tokens, "
                "cache_read_tokens, cost_usd, latency_ms, logged_at "
                f"FROM {self._table_name('cost_log')}"
            )
            params: list[object] = []
            if pipeline_id is not None:
                statement += " WHERE pipeline_id = %s"
                params.append(pipeline_id)
            statement += " ORDER BY logged_at DESC LIMIT %s"
            params.append(max(1, limit))
            cursor.execute(statement, tuple(params))
            rows = self._fetchall(cursor)
        finally:
            self._close_cursor(cursor)
        return [
            {
                "turn_id": str(row["turn_id"]),
                "pipeline_id": row["pipeline_id"],
                "agent_id": str(row["agent_id"]),
                "model": str(row["model"]),
                "input_tokens": int(row["input_tokens"]),
                "output_tokens": int(row["output_tokens"]),
                "cache_read_tokens": int(row["cache_read_tokens"]),
                "cost_usd": float(row["cost_usd"]),
                "latency_ms": int(row["latency_ms"] or 0),
                "logged_at": float(row["logged_at"]),
            }
            for row in rows
        ]

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
            written_at_drift=float(row["written_at_drift"]) if row.get("written_at_drift") is not None else 0.0,
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

    def _decode_embedding(self, value: Any) -> list[float] | None:
        if value in (None, ""):
            return None
        if isinstance(value, list):
            return [float(item) for item in value]
        if isinstance(value, tuple):
            return [float(item) for item in value]
        if isinstance(value, str):
            text = value.strip()
            if not text:
                return None
            if text.startswith("[") and text.endswith("]"):
                body = text[1:-1].strip()
                if not body:
                    return []
                return [float(item.strip()) for item in body.split(",")]
        return None

    def _vector_similarity_score(
        self,
        *,
        query_embedding: list[float] | None,
        row_embedding: Any,
    ) -> float | None:
        if query_embedding is None:
            return None
        candidate = self._decode_embedding(row_embedding)
        if not candidate or len(candidate) != len(query_embedding):
            return None
        dot = sum(left * right for left, right in zip(query_embedding, candidate, strict=True))
        query_norm = math.sqrt(sum(value * value for value in query_embedding))
        candidate_norm = math.sqrt(sum(value * value for value in candidate))
        if query_norm == 0.0 or candidate_norm == 0.0:
            return None
        cosine = dot / (query_norm * candidate_norm)
        cosine = max(-1.0, min(1.0, cosine))
        return (cosine + 1.0) / 2.0

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
                      AND chunk_id != %s
                    """
                ),
                (chunk.zone, chunk.layer, chunk.pipeline_id, chunk.chunk_id),
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
