"""AsyncPgvectorStore — native async pgvector store using psycopg3 + psycopg_pool.

Eliminates the `anyio.to_thread.run_sync` shim for the hot async path used by
`Assembler.post_turn_async` (async_write, async_log_turn_record, async_log_conscious,
async_log_cost) and the read path (async_query, async_resolve_recent_ref).

Whisper methods (async_emit_whisper, async_drain_whispers) still delegate to the
underlying Redis coordination, which has its own async semantics.

Sync abstract methods all raise NotImplementedError — this store is async-native.
Use PgvectorStore for synchronous callers.
"""

from __future__ import annotations

import json
import math
import time
from contextlib import asynccontextmanager
from difflib import SequenceMatcher
from typing import Any, AsyncIterator

import anyio

from ncp.stores.base import BaseStore, NCPStoreUnavailableError
from ncp.stores.pgvector import (
    DEFAULT_RETRIEVAL_POLICY,
    PGVECTOR_SCHEMA_TEMPLATE,
    _validate_identifier,
)
from ncp.stores.redis_coordination import AsyncRedisCoordination
from ncp.stores.retrieval import (
    apply_diversity_limit,
    build_lexical_candidates,
    normalize_result_limit,
    score_trust_recency_candidate,
    score_vector_distance,
)
from ncp.stores.consolidation import cluster_by_tags, find_merge_candidates
from ncp.types import (
    CalibrationReport,
    ConsciousBlock,
    ConsolidationReport,
    NCPResponse,
    SubconsciousChunk,
    TurnRecord,
    Whisper,
)


class AsyncPgvectorStore(BaseStore):
    """Async-native pgvector store.

    All eight `async_*` methods use psycopg3 native async I/O instead of
    `anyio.to_thread.run_sync`. Sync abstract methods raise `NotImplementedError`.
    """

    def __init__(
        self,
        dsn: str,
        *,
        schema: str = "ncp",
        table_prefix: str = "ncp_",
        min_pool_connections: int = 2,
        max_pool_connections: int = 10,
        open_pool: bool = False,
        max_working_chunks: int = 500,
        gc_threshold: int = 400,
        redis_url: str | None = None,
        coordination: AsyncRedisCoordination | None = None,
        embedding_adapter: object | None = None,
        ivfflat_probes: int = 10,
    ) -> None:
        self.dsn = dsn
        self.schema = _validate_identifier(schema, field="schema")
        self.table_prefix = _validate_identifier(table_prefix, field="table_prefix")
        self._min_pool = min_pool_connections
        self._max_pool = max_pool_connections
        self.max_working_chunks = max_working_chunks
        self.gc_threshold = gc_threshold
        self._embedding_adapter: object | None = embedding_adapter
        self._ivfflat_probes = ivfflat_probes
        self._apool: Any = None
        self._init_lock = anyio.Lock()
        self._acoordination: AsyncRedisCoordination | None = coordination or (
            AsyncRedisCoordination(redis_url) if redis_url else None
        )

        if open_pool:
            raise ValueError(
                "open_pool=True cannot be used in __init__ (no async context). "
                "Call await store.open() or use it as an async context manager."
            )

        try:
            from psycopg_pool import AsyncConnectionPool as _ACP  # type: ignore[import]
            self._pool_cls = _ACP
        except ImportError as exc:  # pragma: no cover
            raise NCPStoreUnavailableError(
                "AsyncPgvectorStore requires psycopg and psycopg_pool. "
                "Install with: pip install 'neural-context-protocol[pgvector]'"
            ) from exc

    # ------------------------------------------------------------------
    # Pool lifecycle
    # ------------------------------------------------------------------

    async def open(self) -> None:
        """Open the async connection pool and create schema if needed."""
        if self._apool is None:
            self._apool = self._pool_cls(
                conninfo=self.dsn,
                min_size=self._min_pool,
                max_size=self._max_pool,
                open=False,
            )
            await self._apool.open()
            await self._ainit_db()

    async def close(self) -> None:  # type: ignore[override]
        """Close and drain the async pool."""
        if self._apool is not None:
            try:
                await self._apool.close()
            except Exception:
                pass
            finally:
                self._apool = None

    @asynccontextmanager
    async def _aconnect(self) -> AsyncIterator[Any]:
        """Async context manager: borrow a connection, commit on success, rollback on error."""
        if self._apool is None:
            async with self._init_lock:
                if self._apool is None:  # double-checked: only one coroutine opens the pool
                    await self.open()
        async with self._apool.connection() as conn:
            try:
                yield conn
                await conn.commit()
            except Exception:
                await conn.rollback()
                raise

    # ------------------------------------------------------------------
    # Schema init
    # ------------------------------------------------------------------

    async def _ainit_db(self) -> None:
        schema_sql = PGVECTOR_SCHEMA_TEMPLATE.format(
            schema=self.schema,
            prefix=self.table_prefix,
        )
        async with self._aconnect() as conn:
            async with conn.cursor() as cur:
                await cur.execute(schema_sql)

    # ------------------------------------------------------------------
    # SQL helpers (shared with sync store)
    # ------------------------------------------------------------------

    def _sql(self, statement: str) -> str:
        return statement.format(schema=self.schema, prefix=self.table_prefix)

    def _table_name(self, logical_name: str) -> str:
        return f"{self.schema}.{self.table_prefix}{logical_name}"

    def _normalize_row(self, row: Any, description: Any) -> dict[str, Any]:
        if isinstance(row, dict):
            return row
        mapping = getattr(row, "_mapping", None)
        if mapping is not None:
            return dict(mapping)
        if description is None:
            raise TypeError("cursor description required to normalize rows")
        columns = [str(col[0]) for col in description]
        return {col: row[i] for i, col in enumerate(columns)}

    async def _afetchall(self, cursor: Any) -> list[dict[str, Any]]:
        rows = await cursor.fetchall()
        return [self._normalize_row(row, getattr(cursor, "description", None)) for row in rows]

    async def _afetchone(self, cursor: Any) -> dict[str, Any] | None:
        row = await cursor.fetchone()
        if row is None:
            return None
        return self._normalize_row(row, getattr(cursor, "description", None))

    # ------------------------------------------------------------------
    # Overridden async_* methods — native psycopg3 async I/O
    # ------------------------------------------------------------------

    async def async_write(self, chunk: SubconsciousChunk) -> bool:
        """Persist a chunk using native async DB I/O (no thread pool).

        Matches sync write() behavior: soft_gc → src_immutability → dedup →
        INSERT/upsert → hard_gc.
        """
        chunk = self._validate_chunk_for_write(chunk)
        if self._embedding_adapter is not None and chunk.embedding is None:
            _adapter = self._embedding_adapter
            _content = chunk.content
            embedding_vec = await anyio.to_thread.run_sync(
                lambda: _adapter.embed(_content)  # type: ignore[union-attr]
            )
            chunk = chunk.model_copy(update={"embedding": embedding_vec})
        embedding_val = (
            "[" + ",".join(str(f) for f in chunk.embedding) + "]"
            if chunk.embedding is not None
            else None
        )
        async with self._aconnect() as conn:
            await self._async_soft_gc(conn)
            await self._async_assert_src_immutable(conn, chunk)
            if await self._async_is_duplicate(conn, chunk):
                return False
            async with conn.cursor() as cur:
                await cur.execute(
                    self._sql(
                        """
                        INSERT INTO {schema}.{prefix}chunks (
                            chunk_id, pipeline_id, scope, zone, layer, chunk_type, content, src,
                            written_by, caused_by, conscious_hash, evidence_id, version, supersedes,
                            source_refs, schema_version, created_at, base_trust, generation,
                            result_confidence, result_attempts, conditions, valid_while, expiry,
                            owner, meta, embedding
                        ) VALUES (
                            %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                            %s, %s, %s, %s, %s, %s, %s, %s, %s, %s
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
                            embedding = EXCLUDED.embedding
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
                    ),
                )
            await self._async_hard_gc(conn, pipeline_id=chunk.pipeline_id)
        return True

    # ------------------------------------------------------------------
    # Async dedup/GC helpers — native async equivalents of PgvectorStore
    # ------------------------------------------------------------------

    async def _async_soft_gc(self, conn: Any) -> None:
        """Delete expired tombstones, whispers, and turn_records."""
        now = time.time()
        for table in ("tombstones", "whispers", "turn_records"):
            async with conn.cursor() as cur:
                await cur.execute(
                    self._sql(
                        f"DELETE FROM {{schema}}.{{prefix}}{table} WHERE expires_at <= %s"
                    ),
                    (now,),
                )

    async def _async_assert_src_immutable(self, conn: Any, chunk: SubconsciousChunk) -> None:
        """Raise ValueError if src field changes for an existing chunk_id."""
        async with conn.cursor() as cur:
            await cur.execute(
                self._sql(
                    "SELECT src FROM {schema}.{prefix}chunks WHERE chunk_id = %s"
                ),
                (chunk.chunk_id,),
            )
            raw = await cur.fetchone()
            description = cur.description
        if raw is None:
            return
        row = self._normalize_row(raw, description)
        existing_src = str(row["src"])
        if existing_src != chunk.src:
            raise ValueError(
                f"src is immutable for chunk_id={chunk.chunk_id}: "
                f"existing={existing_src} new={chunk.src}"
            )

    async def _async_is_duplicate(self, conn: Any, chunk: SubconsciousChunk) -> bool:
        """Return True if a content-similar chunk exists in the same zone/layer/pipeline."""
        async with conn.cursor() as cur:
            await cur.execute(
                self._sql(
                    """
                    SELECT content FROM {schema}.{prefix}chunks
                    WHERE zone = %s AND layer = %s
                      AND COALESCE(pipeline_id, '') = COALESCE(%s, '')
                      AND chunk_id != %s
                    """
                ),
                (chunk.zone, chunk.layer, chunk.pipeline_id, chunk.chunk_id),
            )
            raw_rows = await cur.fetchall()
            description = cur.description
        rows = [self._normalize_row(r, description) for r in raw_rows]
        for row in rows:
            if SequenceMatcher(None, chunk.content, str(row["content"])).ratio() > 0.92:
                return True
        return False

    async def _async_hard_gc(self, conn: Any, *, pipeline_id: str | None) -> None:
        """Evict oldest working-zone chunks if count exceeds max_working_chunks."""
        clauses = ["zone = 'working'"]
        params: list[object] = []
        if pipeline_id is not None:
            clauses.append("pipeline_id = %s")
            params.append(pipeline_id)
        where = " AND ".join(clauses)

        async with conn.cursor() as cur:
            await cur.execute(
                self._sql(
                    f"SELECT COUNT(*) AS count FROM {{schema}}.{{prefix}}chunks WHERE {where}"
                ),
                tuple(params),
            )
            raw = await cur.fetchone()
            description = cur.description
        row = self._normalize_row(raw, description) if raw is not None else None
        count = int(row["count"]) if row is not None else 0
        if count <= self.max_working_chunks:
            return

        overflow = count - self.gc_threshold
        async with conn.cursor() as cur:
            await cur.execute(
                self._sql(
                    f"""
                    SELECT chunk_id FROM {{schema}}.{{prefix}}chunks
                    WHERE {where}
                    ORDER BY created_at ASC
                    LIMIT %s
                    """
                ),
                (*params, overflow),
            )
            stale_rows = await cur.fetchall()
            description = cur.description

        stale = [self._normalize_row(r, description) for r in stale_rows]
        if not stale:
            return
        async with conn.cursor() as cur:
            await cur.executemany(
                self._sql("DELETE FROM {schema}.{prefix}chunks WHERE chunk_id = %s"),
                [(str(row["chunk_id"]),) for row in stale],
            )

    async def _async_query_vector(
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
                _adapter = self._embedding_adapter
                _text = text
                embedding = await anyio.to_thread.run_sync(
                    lambda: _adapter.embed(_text)  # type: ignore[union-attr]
                )
            else:
                raise ValueError("retrieval_mode='vector' requires an embedding to be provided")
        if len(embedding) != 1536:
            raise ValueError(f"embedding must have 1536 dimensions, got {len(embedding)}")
        embedding_str = "[" + ",".join(str(f) for f in embedding) + "]"

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

        result_limit = normalize_result_limit(k)
        limit = result_limit * 4
        all_params = tuple([embedding_str] + where_params + [embedding_str, limit])

        async with self._aconnect() as conn:
            async with conn.cursor() as cur:
                await cur.execute("SET LOCAL ivfflat.probes = %s", (self._ivfflat_probes,))
                await cur.execute(
                    self._sql(
                        "SELECT *, (embedding <=> %s::vector) AS vec_distance"
                        f" FROM {{schema}}.{{prefix}}chunks"
                        f" WHERE {' AND '.join(where_clauses)}"
                        " ORDER BY embedding <=> %s::vector LIMIT %s"
                    ),
                    all_params,
                )
                raw_rows = await cur.fetchall()
                description = cur.description

        rows = [self._normalize_row(r, description) for r in raw_rows]
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

        results = apply_diversity_limit(
            results,
            k=result_limit,
            diversity_limit=diversity_limit,
            author_getter=lambda chunk: str(chunk.written_by),
        )

        if results:
            now = time.time()
            chunk_ids = [c.chunk_id for c in results]
            async with self._aconnect() as conn:
                async with conn.cursor() as cur:
                    await cur.execute(
                        self._sql(
                            "UPDATE {schema}.{prefix}chunks"
                            " SET retrieval_count = retrieval_count + 1, last_retrieved_at = %s"
                            " WHERE chunk_id = ANY(%s)"
                        ),
                        (now, chunk_ids),
                    )
            for chunk in results:
                chunk.retrieval_count += 1
                chunk.last_retrieved_at = now

        return results

    async def async_query(
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
    ) -> list[SubconsciousChunk]:
        """Query chunks using native async DB I/O; score computation stays synchronous."""
        _VALID_RETRIEVAL_MODES = ("hybrid", "trust_recency", "vector")
        if retrieval_mode not in _VALID_RETRIEVAL_MODES:
            raise ValueError(
                f"Unknown retrieval_mode {retrieval_mode!r}; expected one of {_VALID_RETRIEVAL_MODES}"
            )
        if retrieval_mode == "vector":
            return await self._async_query_vector(
                text=text,
                embedding=embedding,
                k=k,
                min_score=min_score,
                layer=layer,
                pipeline_id=pipeline_id,
                scope=scope,
                zone=zone,
                diversity_limit=diversity_limit,
            )
        if embedding is None and self._embedding_adapter is not None:
            _adapter = self._embedding_adapter
            _text = text
            embedding = await anyio.to_thread.run_sync(
                lambda: _adapter.embed(_text)  # type: ignore[union-attr]
            )
        if embedding is not None and len(embedding) != 1536:
            raise ValueError(f"embedding must have 1536 dimensions, got {len(embedding)}")
        clauses = ["zone = %s"]
        params: list[Any] = [zone]
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

        async with self._aconnect() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    self._sql(
                        f"SELECT * FROM {{schema}}.{{prefix}}chunks"
                        f" WHERE {' AND '.join(clauses)}"
                        " ORDER BY created_at DESC"
                    ),
                    params,
                )
                raw_rows = await cur.fetchall()
                description = cur.description

        rows = [self._normalize_row(r, description) for r in raw_rows]
        if not rows:
            return []

        policy = DEFAULT_RETRIEVAL_POLICY
        now = time.time()
        candidates: list[SubconsciousChunk] = []
        result_limit = normalize_result_limit(k)

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
                age_s = max(0.0, now - float(row["created_at"]))
                if lexical_candidate.lexical_signal is None:
                    continue
                vector_score = self._vector_similarity_score(
                    query_embedding=embedding,
                    row_embedding=row.get("embedding"),
                )
                h = policy.score_with_vector(
                    bm25_normalized=lexical_candidate.lexical_signal,
                    vector_normalized=vector_score,
                    age_seconds=age_s,
                    base_trust=float(row["base_trust"]),
                    generation=int(row["generation"]),
                    written_at_drift=float(row["written_at_drift"]) if row.get("written_at_drift") is not None else 0.0,
                )
                if h < min_score:
                    continue
                chunk = self._row_to_chunk(row)
                chunk.relevance = max(0.0, min(1.0, h))
                candidates.append(chunk)

        ranked = sorted(candidates, key=lambda c: c.relevance, reverse=True)
        results = apply_diversity_limit(
            ranked,
            k=result_limit,
            diversity_limit=diversity_limit,
            author_getter=lambda chunk: str(chunk.written_by),
        )

        if results:
            now = time.time()
            chunk_ids = [c.chunk_id for c in results]
            async with self._aconnect() as conn:
                async with conn.cursor() as cur:
                    await cur.execute(
                        self._sql(
                            "UPDATE {schema}.{prefix}chunks"
                            " SET retrieval_count = retrieval_count + 1, last_retrieved_at = %s"
                            " WHERE chunk_id = ANY(%s)"
                        ),
                        (now, chunk_ids),
                    )
            for chunk in results:
                chunk.retrieval_count += 1
                chunk.last_retrieved_at = now

        return results

    async def async_log_turn_record(self, record: TurnRecord) -> None:
        """Persist a turn record using native async DB I/O."""
        async with self._aconnect() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    self._sql(
                        """
                        INSERT INTO {schema}.{prefix}turn_records (
                            turn_id, agent_id, pipeline_id, task, slot, result, result_full,
                            created_at, expires_at
                        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                        ON CONFLICT (turn_id) DO UPDATE SET
                            agent_id = EXCLUDED.agent_id,
                            result = EXCLUDED.result,
                            result_full = EXCLUDED.result_full
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

    async def async_resolve_recent_ref(self, ref: str) -> TurnRecord | None:
        """Resolve a recent ref using native async DB I/O."""
        if not ref.startswith("r:sub/"):
            return None
        turn_id = ref.split("/", 1)[1]
        async with self._aconnect() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    self._sql("SELECT * FROM {schema}.{prefix}turn_records WHERE turn_id = %s"),
                    (turn_id,),
                )
                raw = await cur.fetchone()
                description = cur.description
        if raw is None:
            return None
        row = self._normalize_row(raw, description)
        return TurnRecord(**row)

    async def async_log_conscious(self, conscious: ConsciousBlock, *, snapshot_hash: str) -> None:
        """Persist a conscious-block snapshot using native async DB I/O."""
        async with self._aconnect() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
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

    async def async_log_drift_history(self, *, session_id: str, turn: int, drift_score: float) -> None:
        """Persist a drift sensor reading using native async DB I/O."""
        async with self._aconnect() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    self._sql(
                        """
                        INSERT INTO {schema}.{prefix}drift_history (session_id, turn, drift_score, ts)
                        VALUES (%s, %s, %s, %s)
                        """
                    ),
                    (session_id, turn, drift_score, time.time()),
                )

    async def async_log_cost(self, *, agent_id: str, response: NCPResponse) -> None:
        """Persist cost telemetry using native async DB I/O."""
        async with self._aconnect() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    self._sql(
                        """
                        INSERT INTO {schema}.{prefix}cost_log (
                            turn_id, pipeline_id, agent_id, model, input_tokens, output_tokens,
                            cache_read_tokens, cost_usd, latency_ms, logged_at
                        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                        ON CONFLICT (turn_id) DO UPDATE SET
                            cost_usd = EXCLUDED.cost_usd,
                            latency_ms = EXCLUDED.latency_ms
                        """
                    ),
                    (
                        response.turn_id,
                        response.pipeline_id,
                        agent_id,
                        response.model,
                        response.input_tokens,
                        response.output_tokens,
                        0,
                        response.cost_usd,
                        response.latency_ms,
                        time.time(),
                    ),
                )

    async def async_status_detail(self, *, pipeline_id: str | None = None) -> dict[str, object]:
        """Return store status using native async DB I/O."""
        async with self._aconnect() as conn:
            overview = {
                "chunk_count": await self._acount_rows(conn, "chunks", pipeline_id=pipeline_id),
                "tombstone_count": await self._acount_rows(conn, "tombstones"),
                "turn_record_count": await self._acount_rows(conn, "turn_records", pipeline_id=pipeline_id),
                "conscious_snapshot_count": await self._acount_rows(conn, "conscious_log", pipeline_id=pipeline_id),
                "cost_entry_count": await self._acount_rows(conn, "cost_log", pipeline_id=pipeline_id),
            }
            overview["pipeline_count"] = await self._acount_distinct_pipelines(conn)
            overview["cost_usd_total"] = await self._asum_cost(conn, pipeline_id=pipeline_id)
            latest_chunk = await self._amax_column(conn, "chunks", "created_at", pipeline_id=pipeline_id)
            latest_turn = await self._amax_column(conn, "turn_records", "created_at", pipeline_id=pipeline_id)
            latest_cost = await self._amax_column(conn, "cost_log", "logged_at", pipeline_id=pipeline_id)
            layer_counts = await self._alayer_counts(conn, pipeline_id=pipeline_id)
            recent_pipelines = await self._arecent_pipelines(conn, pipeline_id=pipeline_id)

        whisper_stats = (
            await self._acoordination.async_whisper_stats(pipeline_id=pipeline_id)
            if self._acoordination is not None
            else {"count": 0, "last_activity_at": None, "by_type": {}}
        )
        overview["whisper_count"] = int(whisper_stats.get("count", 0) or 0)
        activity_candidates = [
            value
            for value in (latest_chunk, latest_turn, latest_cost, whisper_stats.get("last_activity_at"))
            if value is not None
        ]
        overview["last_activity_at"] = max(activity_candidates) if activity_candidates else None
        return {
            "overview": overview,
            "layer_counts": layer_counts,
            "recent_pipelines": recent_pipelines,
        }

    async def async_cost_summary(
        self,
        *,
        pipeline_id: str | None = None,
        limit: int = 10,
    ) -> dict[str, object]:
        """Return cost summary using native async DB I/O."""
        async with self._aconnect() as conn:
            summary = await self._acost_summary_row(conn, pipeline_id=pipeline_id)
            by_agent = await self._acost_group_rows(conn, group_by="agent_id", pipeline_id=pipeline_id)
            by_model = await self._acost_group_rows(conn, group_by="model", pipeline_id=pipeline_id)
            recent_entries = await self._arecent_cost_rows(conn, pipeline_id=pipeline_id, limit=limit)
        return {
            "summary": summary,
            "by_agent": by_agent,
            "by_model": by_model,
            "recent_entries": recent_entries,
        }

    async def async_viz_data(self, *, pipeline_id: str | None = None) -> dict[str, object]:
        """Return operator viz data using native async DB I/O."""
        now = time.time()
        live_filter = f"chunk_id NOT IN (SELECT chunk_id FROM {self._table_name('tombstones')})"
        async with self._aconnect() as conn:
            chunk_distribution = await self._achunk_distribution(
                conn,
                pipeline_id=pipeline_id,
                live_filter=live_filter,
            )
            age_brackets = await self._aage_brackets(
                conn,
                pipeline_id=pipeline_id,
                live_filter=live_filter,
                now=now,
            )
            top_chunks = await self._atop_chunks(
                conn,
                pipeline_id=pipeline_id,
                live_filter=live_filter,
                now=now,
            )
            pipeline_summary = await self._apipeline_summary(
                conn,
                pipeline_id=pipeline_id,
                live_filter=live_filter,
            )

        whisper_queue: dict[str, object]
        if self._acoordination is not None:
            try:
                stats = await self._acoordination.async_whisper_stats(pipeline_id=pipeline_id)
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

    async def async_emit_whisper(self, whisper: Whisper) -> None:
        """Emit whisper via native async Redis coordination (no thread shim)."""
        if self._acoordination is None:
            raise NCPStoreUnavailableError(
                "AsyncPgvectorStore whisper coordination requires Redis. "
                "Pass redis_url= or coordination= to enable whispers."
            )
        await self._acoordination.emit_whisper(whisper)

    async def async_drain_whispers(
        self,
        *,
        agent_id: str,
        pipeline_id: str | None = None,
        max_items: int = 3,
        min_confidence: float = 0.60,
    ) -> list[Whisper]:
        """Drain whispers via native async Redis coordination (no thread shim)."""
        if self._acoordination is None:
            raise NCPStoreUnavailableError(
                "AsyncPgvectorStore whisper coordination requires Redis. "
                "Pass redis_url= or coordination= to enable whispers."
            )
        return await self._acoordination.drain_whispers(
            agent_id=agent_id,
            pipeline_id=pipeline_id,
            max_items=max_items,
            min_confidence=min_confidence,
        )

    # ------------------------------------------------------------------
    # Chunk validation helper
    # ------------------------------------------------------------------

    def _validate_chunk_for_write(self, chunk: SubconsciousChunk) -> SubconsciousChunk:
        validated = SubconsciousChunk.model_validate(chunk.model_dump())
        return validated.model_copy(update={"age_seconds": max(0.0, validated.age_seconds)})

    # ------------------------------------------------------------------
    # Row helper (shared logic from PgvectorStore)
    # ------------------------------------------------------------------

    def _row_to_chunk(self, row: dict[str, Any]) -> SubconsciousChunk:
        created_at = float(row["created_at"])
        return SubconsciousChunk(
            chunk_id=str(row["chunk_id"]),
            layer=str(row["layer"]),
            content=str(row["content"]),
            src=str(row["src"]),
            written_by=str(row["written_by"]),
            caused_by=row.get("caused_by"),
            conscious_hash=row.get("conscious_hash"),
            evidence_id=row.get("evidence_id"),
            generation=int(row.get("generation", 0)),
            base_trust=float(row.get("base_trust", 0.7)),
            written_at_drift=float(row["written_at_drift"]) if row.get("written_at_drift") is not None else 0.0,
            result_confidence=row.get("result_confidence"),
            result_attempts=row.get("result_attempts"),
            conditions=[],
            valid_while=row.get("valid_while"),
            expiry=row.get("expiry"),
            owner=row.get("owner"),
            chunk_type=str(row.get("chunk_type", "prose")),
            pipeline_id=row.get("pipeline_id"),
            scope=str(row.get("scope", "pipeline")),
            zone=str(row.get("zone", "working")),
            schema_version=int(row.get("schema_version", 1)),
            supersedes=row.get("supersedes"),
            source_refs=[],
            age_seconds=max(0.0, time.time() - created_at),
        )

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

    async def _acount_rows(self, connection: Any, table: str, *, pipeline_id: str | None = None) -> int:
        async with connection.cursor() as cursor:
            statement = f"SELECT COUNT(*) AS count FROM {self._table_name(table)}"
            params: tuple[object, ...] = ()
            if pipeline_id is not None and table in {"chunks", "turn_records", "conscious_log", "cost_log"}:
                statement += " WHERE pipeline_id = %s"
                params = (pipeline_id,)
            await cursor.execute(statement, params)
            row = await self._afetchone(cursor)
        return int(row["count"] if row is not None else 0)

    async def _acount_distinct_pipelines(self, connection: Any) -> int:
        async with connection.cursor() as cursor:
            await cursor.execute(f"SELECT COUNT(DISTINCT pipeline_id) AS count FROM {self._table_name('chunks')}")
            row = await self._afetchone(cursor)
        return int(row["count"] if row is not None else 0)

    async def _asum_cost(self, connection: Any, *, pipeline_id: str | None = None) -> float:
        async with connection.cursor() as cursor:
            statement = f"SELECT COALESCE(SUM(cost_usd), 0.0) AS total FROM {self._table_name('cost_log')}"
            params: tuple[object, ...] = ()
            if pipeline_id is not None:
                statement += " WHERE pipeline_id = %s"
                params = (pipeline_id,)
            await cursor.execute(statement, params)
            row = await self._afetchone(cursor)
        return float(row["total"] if row is not None else 0.0)

    async def _amax_column(
        self,
        connection: Any,
        table: str,
        column: str,
        *,
        pipeline_id: str | None = None,
    ) -> float | None:
        async with connection.cursor() as cursor:
            statement = f"SELECT MAX({column}) AS latest FROM {self._table_name(table)}"
            params: tuple[object, ...] = ()
            if pipeline_id is not None and table in {"chunks", "turn_records", "conscious_log", "cost_log"}:
                statement += " WHERE pipeline_id = %s"
                params = (pipeline_id,)
            await cursor.execute(statement, params)
            row = await self._afetchone(cursor)
        if row is None or row["latest"] is None:
            return None
        return float(row["latest"])

    async def _alayer_counts(self, connection: Any, *, pipeline_id: str | None = None) -> dict[str, int]:
        async with connection.cursor() as cursor:
            statement = (
                f"SELECT layer, COUNT(*) AS count FROM {self._table_name('chunks')}"
                + (" WHERE pipeline_id = %s" if pipeline_id is not None else "")
                + " GROUP BY layer ORDER BY count DESC, layer ASC"
            )
            await cursor.execute(statement, () if pipeline_id is None else (pipeline_id,))
            rows = await self._afetchall(cursor)
        return {str(row["layer"]): int(row["count"]) for row in rows}

    async def _arecent_pipelines(self, connection: Any, *, pipeline_id: str | None = None) -> list[dict[str, object]]:
        async with connection.cursor() as cursor:
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
            await cursor.execute(statement, params)
            rows = await self._afetchall(cursor)
        return [
            {
                "pipeline_id": str(row["pipeline_id"]),
                "chunk_count": int(row["chunk_count"]),
                "last_chunk_at": float(row["last_chunk_at"]),
            }
            for row in rows
            if row["pipeline_id"] is not None
        ]

    async def _acost_summary_row(self, connection: Any, *, pipeline_id: str | None = None) -> dict[str, object]:
        async with connection.cursor() as cursor:
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
            await cursor.execute(statement, params)
            row = await self._afetchone(cursor)
        return {
            "cost_usd_total": float(row["cost_usd_total"]),
            "input_tokens_total": int(row["input_tokens_total"]),
            "output_tokens_total": int(row["output_tokens_total"]),
            "cache_read_tokens_total": int(row["cache_read_tokens_total"]),
            "entry_count": int(row["entry_count"]),
            "avg_latency_ms": float(row["avg_latency_ms"]),
        }

    async def _acost_group_rows(
        self,
        connection: Any,
        *,
        group_by: str,
        pipeline_id: str | None = None,
    ) -> list[dict[str, object]]:
        async with connection.cursor() as cursor:
            statement = (
                f"SELECT {group_by}, COUNT(*) AS turns, COALESCE(SUM(cost_usd), 0.0) AS cost_usd_total "
                f"FROM {self._table_name('cost_log')}"
            )
            params: tuple[object, ...] = ()
            if pipeline_id is not None:
                statement += " WHERE pipeline_id = %s"
                params = (pipeline_id,)
            statement += f" GROUP BY {group_by} ORDER BY cost_usd_total DESC, {group_by} ASC"
            await cursor.execute(statement, params)
            rows = await self._afetchall(cursor)
        return [
            {
                group_by: str(row[group_by]),
                "turns": int(row["turns"]),
                "cost_usd_total": float(row["cost_usd_total"]),
            }
            for row in rows
        ]

    async def _arecent_cost_rows(
        self,
        connection: Any,
        *,
        pipeline_id: str | None = None,
        limit: int,
    ) -> list[dict[str, object]]:
        async with connection.cursor() as cursor:
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
            await cursor.execute(statement, tuple(params))
            rows = await self._afetchall(cursor)
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

    async def _achunk_distribution(
        self,
        connection: Any,
        *,
        pipeline_id: str | None,
        live_filter: str,
    ) -> list[dict[str, object]]:
        async with connection.cursor() as cursor:
            if pipeline_id is not None:
                await cursor.execute(
                    f"SELECT layer, zone, COUNT(*) AS count FROM {self._table_name('chunks')}"
                    f" WHERE {live_filter} AND pipeline_id = %s"
                    " GROUP BY layer, zone ORDER BY layer, zone",
                    (pipeline_id,),
                )
            else:
                await cursor.execute(
                    f"SELECT layer, zone, COUNT(*) AS count FROM {self._table_name('chunks')}"
                    f" WHERE {live_filter}"
                    " GROUP BY layer, zone ORDER BY layer, zone"
                )
            rows = await self._afetchall(cursor)
        return [
            {"layer": str(row["layer"]), "zone": str(row["zone"]), "count": int(row["count"])}
            for row in rows
        ]

    async def _aage_brackets(
        self,
        connection: Any,
        *,
        pipeline_id: str | None,
        live_filter: str,
        now: float,
    ) -> list[dict[str, object]]:
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
        async with connection.cursor() as cursor:
            await cursor.execute(bracket_sql, tuple(bracket_params))
            bracket_rows = await self._afetchall(cursor)

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
            async with connection.cursor() as cursor:
                await cursor.execute(tl_sql, tuple(tl_params))
                tl_row = await self._afetchone(cursor)
            if tl_row is not None:
                bracket_top_layer[bracket_label] = str(tl_row["layer"])

        return [
            {
                "bracket": str(row["bracket"]),
                "count": int(row["count"]),
                "avg_trust": round(float(row["avg_trust"]), 4) if row["avg_trust"] is not None else 0.0,
                "top_layer": bracket_top_layer.get(str(row["bracket"]), "-"),
            }
            for row in bracket_rows
        ]

    async def _atop_chunks(
        self,
        connection: Any,
        *,
        pipeline_id: str | None,
        live_filter: str,
        now: float,
    ) -> list[dict[str, object]]:
        async with connection.cursor() as cursor:
            if pipeline_id is not None:
                await cursor.execute(
                    f"SELECT chunk_id, layer, zone, pipeline_id, base_trust, created_at"
                    f" FROM {self._table_name('chunks')}"
                    f" WHERE {live_filter} AND pipeline_id = %s"
                    " ORDER BY base_trust DESC, created_at DESC LIMIT 5",
                    (pipeline_id,),
                )
            else:
                await cursor.execute(
                    f"SELECT chunk_id, layer, zone, pipeline_id, base_trust, created_at"
                    f" FROM {self._table_name('chunks')}"
                    f" WHERE {live_filter}"
                    " ORDER BY base_trust DESC, created_at DESC LIMIT 5"
                )
            rows = await self._afetchall(cursor)
        return [
            {
                "chunk_id": str(row["chunk_id"])[:16],
                "layer": str(row["layer"]),
                "zone": str(row["zone"]),
                "pipeline_id": row["pipeline_id"],
                "base_trust": float(row["base_trust"]),
                "age_seconds": round(now - float(row["created_at"]), 1),
            }
            for row in rows
        ]

    async def _apipeline_summary(
        self,
        connection: Any,
        *,
        pipeline_id: str | None,
        live_filter: str,
    ) -> list[dict[str, object]]:
        async with connection.cursor() as cursor:
            if pipeline_id is not None:
                await cursor.execute(
                    f"SELECT pipeline_id, COUNT(*) AS chunk_count, MAX(created_at) AS last_activity"
                    f" FROM {self._table_name('chunks')}"
                    f" WHERE {live_filter} AND pipeline_id = %s"
                    " GROUP BY pipeline_id ORDER BY last_activity DESC",
                    (pipeline_id,),
                )
            else:
                await cursor.execute(
                    f"SELECT pipeline_id, COUNT(*) AS chunk_count, MAX(created_at) AS last_activity"
                    f" FROM {self._table_name('chunks')}"
                    f" WHERE {live_filter} AND pipeline_id IS NOT NULL"
                    " GROUP BY pipeline_id ORDER BY last_activity DESC LIMIT 20"
                )
            rows = await self._afetchall(cursor)
        return [
            {
                "pipeline_id": str(row["pipeline_id"]),
                "chunk_count": int(row["chunk_count"]),
                "last_activity": float(row["last_activity"]),
            }
            for row in rows
            if row["pipeline_id"] is not None
        ]

    # ------------------------------------------------------------------
    # Async consolidation — full parity with sync consolidate()
    # ------------------------------------------------------------------

    async def async_consolidate(
        self,
        *,
        pipeline_id: str | None = None,
        dry_run: bool = False,
        similarity_threshold: float = 0.65,
        trust_floor: float = 0.10,
    ) -> ConsolidationReport:
        """Merge redundant chunks using async DB I/O. Full parity with consolidate()."""
        started = time.monotonic()
        report = ConsolidationReport(dry_run=dry_run, pipeline_id=pipeline_id)

        async with self._aconnect() as conn:
            async with conn.cursor() as cur:
                if pipeline_id is not None:
                    await cur.execute(
                        self._sql(
                            "SELECT * FROM {schema}.{prefix}chunks"
                            " WHERE chunk_id NOT IN"
                            " (SELECT chunk_id FROM {schema}.{prefix}tombstones)"
                            " AND pipeline_id = %s"
                        ),
                        (pipeline_id,),
                    )
                else:
                    await cur.execute(
                        self._sql(
                            "SELECT * FROM {schema}.{prefix}chunks"
                            " WHERE chunk_id NOT IN"
                            " (SELECT chunk_id FROM {schema}.{prefix}tombstones)"
                        )
                    )
                rows = await cur.fetchall()
                desc = cur.description

        all_chunks = [self._row_to_chunk(self._normalize_row(r, desc)) for r in rows]
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
                    async with self._aconnect() as conn:
                        async with conn.cursor() as cur:
                            for loser_id in loser_ids:
                                await cur.execute(
                                    self._sql(
                                        "DELETE FROM {schema}.{prefix}chunks"
                                        " WHERE chunk_id = %s"
                                    ),
                                    (loser_id,),
                                )
                                await cur.execute(
                                    self._sql(
                                        "INSERT INTO {schema}.{prefix}tombstones"
                                        " (chunk_id, forward_ref, tombstoned_at, expires_at)"
                                        " VALUES (%s, %s, %s, %s) ON CONFLICT DO NOTHING"
                                    ),
                                    (loser_id, keeper.chunk_id, time.time(), time.time() + 86400),
                                )
                            supersedes_json = json.dumps(loser_ids)
                            new_gen = keeper.generation + 1
                            await cur.execute(
                                self._sql(
                                    "UPDATE {schema}.{prefix}chunks"
                                    " SET generation = %s, supersedes = %s"
                                    " WHERE chunk_id = %s"
                                ),
                                (new_gen, supersedes_json, keeper.chunk_id),
                            )
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
            await self._async_emit_consolidation_whisper(pipeline_id=pipeline_id)

        report.duration_seconds = time.monotonic() - started
        return report

    async def _async_emit_consolidation_whisper(self, *, pipeline_id: str | None) -> None:
        """Emit consolidation_ready whisper via async coordination. Silently swallows errors."""
        if self._acoordination is None:
            return
        whisper = Whisper(
            from_agent="ncp_consolidator",
            target="*",
            whisper_type="consolidation_ready",
            payload=f"consolidation_complete pipeline:{pipeline_id or 'all'}",
            confidence=1.0,
            pipeline_id=pipeline_id,
        )
        try:
            await self._acoordination.emit_whisper(whisper)
        except Exception:
            pass

    async def async_calibrate(
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
        """Re-score base_trust on live chunks using async DB I/O. Full parity with calibrate()."""
        started = time.monotonic()
        report = CalibrationReport(dry_run=dry_run, pipeline_id=pipeline_id)

        if chunk_id is not None:
            if trust is None or not (0.0 <= trust <= 1.0):
                raise ValueError(
                    f"trust must be in [0.0, 1.0] when chunk_id is specified, got {trust!r}"
                )
            row = None
            desc = None
            async with self._aconnect() as conn:
                async with conn.cursor() as cur:
                    await cur.execute(
                        self._sql(
                            "SELECT * FROM {schema}.{prefix}chunks"
                            " WHERE chunk_id = %s"
                            " AND chunk_id NOT IN"
                            " (SELECT chunk_id FROM {schema}.{prefix}tombstones)"
                        ),
                        (chunk_id,),
                    )
                    row = await cur.fetchone()
                    desc = cur.description
            if row is None:
                report.skipped += 1
                report.duration_seconds = time.monotonic() - started
                return report
            chunk_obj = self._row_to_chunk(self._normalize_row(row, desc))
            report.change_log.append({
                "chunk_id": chunk_id,
                "old_trust": chunk_obj.base_trust,
                "new_trust": trust,
                "reason": "manual_override",
            })
            if not dry_run:
                async with self._aconnect() as conn:
                    async with conn.cursor() as cur:
                        await cur.execute(
                            self._sql(
                                "UPDATE {schema}.{prefix}chunks"
                                " SET base_trust = %s WHERE chunk_id = %s"
                            ),
                            (trust, chunk_id),
                        )
            report.adjusted += 1
            report.duration_seconds = time.monotonic() - started
            return report

        cutoff_age = recency_half_life_seconds
        async with self._aconnect() as conn:
            async with conn.cursor() as cur:
                if pipeline_id is not None:
                    await cur.execute(
                        self._sql(
                            "SELECT chunk_id, base_trust, src, generation, created_at,"
                            " retrieval_count FROM {schema}.{prefix}chunks"
                            " WHERE chunk_id NOT IN"
                            " (SELECT chunk_id FROM {schema}.{prefix}tombstones)"
                            " AND pipeline_id = %s"
                        ),
                        (pipeline_id,),
                    )
                else:
                    await cur.execute(
                        self._sql(
                            "SELECT chunk_id, base_trust, src, generation, created_at,"
                            " retrieval_count FROM {schema}.{prefix}chunks"
                            " WHERE chunk_id NOT IN"
                            " (SELECT chunk_id FROM {schema}.{prefix}tombstones)"
                        )
                    )
                rows = await cur.fetchall()
                desc = cur.description

        updates: list[tuple[float, str]] = []
        now = time.time()
        for row in rows:
            r = self._normalize_row(row, desc)
            cid = str(r["chunk_id"])
            bt = float(r.get("base_trust", 0.7))
            src = str(r.get("src") or "")
            generation = int(r.get("generation") or 0)
            created_at = float(r.get("created_at") or now)
            rc = int(r.get("retrieval_count") or 0)
            age_seconds = max(0.0, now - created_at)

            if src == "user_verified":
                report.protected += 1
                continue

            if not feedback_mode:
                if age_seconds > cutoff_age and bt > 0.5 and generation == 0:
                    new_trust = max(0.0, bt * decay_factor)
                    report.change_log.append({
                        "chunk_id": cid,
                        "old_trust": bt,
                        "new_trust": new_trust,
                        "reason": "batch_decay",
                    })
                    updates.append((new_trust, cid))
                    report.adjusted += 1
                else:
                    report.skipped += 1
            else:
                if rc > 0:
                    boost = feedback_weight * min(1.0, rc / 10)
                    new_trust = min(1.0, bt + boost)
                    report.change_log.append({
                        "chunk_id": cid,
                        "old_trust": bt,
                        "new_trust": new_trust,
                        "reason": "retrieval_feedback",
                        "retrieval_count": rc,
                    })
                    updates.append((new_trust, cid))
                    report.feedback_adjusted += 1
                else:
                    report.skipped += 1

        if not dry_run and updates:
            async with self._aconnect() as conn:
                async with conn.cursor() as cur:
                    for new_trust, cid in updates:
                        await cur.execute(
                            self._sql(
                                "UPDATE {schema}.{prefix}chunks"
                                " SET base_trust = %s WHERE chunk_id = %s"
                            ),
                            (new_trust, cid),
                        )

        report.duration_seconds = time.monotonic() - started
        return report

    # ------------------------------------------------------------------
    # Sync abstract methods — not supported on async-native store
    # ------------------------------------------------------------------

    def _not_implemented(self, name: str) -> None:
        raise NotImplementedError(
            f"{name} is not available on AsyncPgvectorStore. "
            "Use async_* methods or PgvectorStore for sync access."
        )

    def write(self, chunk: SubconsciousChunk) -> bool:  # type: ignore[override]
        self._not_implemented("write")
        return False  # unreachable

    def query(self, text: str, **kwargs: Any) -> list[SubconsciousChunk]:  # type: ignore[override]
        self._not_implemented("query")
        return []  # unreachable

    def emit_whisper(self, whisper: Whisper) -> None:
        self._not_implemented("emit_whisper")

    def drain_whispers(self, *, agent_id: str, **kwargs: Any) -> list[Whisper]:  # type: ignore[override]
        self._not_implemented("drain_whispers")
        return []

    def peek_whispers(self, *, agent_id: str, **kwargs: Any) -> list[Whisper]:  # type: ignore[override]
        self._not_implemented("peek_whispers")
        return []

    def acknowledge_whispers(self, whisper_ids: Any) -> int:  # type: ignore[override]
        self._not_implemented("acknowledge_whispers")
        return 0

    def whisper_pending(self, whisper_id: str) -> bool:  # type: ignore[override]
        self._not_implemented("whisper_pending")
        return False

    def get_working_zone(self, **kwargs: Any) -> Any:  # type: ignore[override]
        self._not_implemented("get_working_zone")

    def log_turn_record(self, record: TurnRecord) -> None:
        self._not_implemented("log_turn_record")

    def resolve_recent_ref(self, ref: str) -> TurnRecord | None:
        self._not_implemented("resolve_recent_ref")
        return None

    def log_cost(self, *, agent_id: str, response: NCPResponse) -> None:
        self._not_implemented("log_cost")

    def log_cost_raw(self, **kwargs: Any) -> None:  # type: ignore[override]
        self._not_implemented("log_cost_raw")

    def log_conscious(self, conscious: ConsciousBlock, *, snapshot_hash: str) -> None:
        self._not_implemented("log_conscious")

    def get_pipeline_goal_versions(self, *, pipeline_id: str, **kwargs: Any) -> dict[str, int]:  # type: ignore[override]
        self._not_implemented("get_pipeline_goal_versions")
        return {}

    def consolidate(self, **kwargs: Any) -> Any:  # type: ignore[override]
        self._not_implemented("consolidate")

    def calibrate(self, **kwargs: Any) -> Any:  # type: ignore[override]
        self._not_implemented("calibrate")

    def viz_data(self, **kwargs: Any) -> Any:  # type: ignore[override]
        self._not_implemented("viz_data")

    def status_detail(self, **kwargs: Any) -> Any:  # type: ignore[override]
        self._not_implemented("status_detail")

    def cost_summary(self, **kwargs: Any) -> Any:  # type: ignore[override]
        self._not_implemented("cost_summary")
