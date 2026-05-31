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
import time
from contextlib import asynccontextmanager
from difflib import SequenceMatcher
from typing import Any, AsyncIterator

import anyio

from ncp.stores.base import BaseStore, NCPStoreUnavailableError
from ncp.stores.pgvector import (
    PGVECTOR_SCHEMA_TEMPLATE,
    _validate_identifier,
)
from ncp.stores.redis_coordination import AsyncRedisCoordination
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

        limit = max(1, k * 4)
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
            distance = float(row.get("vec_distance") or 1.0)
            score = 1.0 / (1.0 + distance)
            if score < min_score:
                continue
            chunk = self._row_to_chunk(row)
            chunk.relevance = max(0.0, min(1.0, score))
            results.append(chunk)

        _diversity_cap = max(1, diversity_limit)
        author_count: dict[str, int] = {}
        diverse: list[SubconsciousChunk] = []
        for chunk in results:
            author = str(chunk.written_by)
            if author_count.get(author, 0) >= _diversity_cap:
                continue
            author_count[author] = author_count.get(author, 0) + 1
            diverse.append(chunk)
            if len(diverse) >= max(1, k):
                break
        results = diverse

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

        # Score computation is CPU-bound Python — synchronous is fine.
        from ncp.stores.pgvector import DEFAULT_RETRIEVAL_POLICY
        policy = DEFAULT_RETRIEVAL_POLICY
        now = time.time()
        candidates: list[SubconsciousChunk] = []

        if retrieval_mode == "trust_recency":
            for row in rows:
                age_s = max(0.0, now - float(row["created_at"]))
                score = policy.score_no_bm25(
                    age_seconds=age_s,
                    base_trust=float(row["base_trust"]),
                    generation=int(row["generation"]),
                )
                if score < min_score:
                    continue
                chunk = self._row_to_chunk(row)
                chunk.relevance = max(0.0, min(1.0, score))
                candidates.append(chunk)
        else:
            from rank_bm25 import BM25Okapi  # type: ignore[import]

            query_terms = {t for t in text.lower().split() if t}
            corpus = [row["content"].lower().split() for row in rows]
            bm25 = BM25Okapi(corpus)
            raw_scores = bm25.get_scores(text.split())
            max_bm25 = max(raw_scores) if len(raw_scores) > 0 else 0.0
            norm = [s / max_bm25 for s in raw_scores] if max_bm25 > 0 else [0.0] * len(raw_scores)

            for score, row, tokens in zip(norm, rows, corpus, strict=True):
                if query_terms and not query_terms.intersection(set(tokens)):
                    continue
                age_s = max(0.0, now - float(row["created_at"]))
                h = policy.score(
                    bm25_normalized=score if query_terms else 1.0,
                    age_seconds=age_s,
                    base_trust=float(row["base_trust"]),
                    generation=int(row["generation"]),
                )
                if h < min_score:
                    continue
                chunk = self._row_to_chunk(row)
                chunk.relevance = max(0.0, min(1.0, h))
                candidates.append(chunk)

        ranked = sorted(candidates, key=lambda c: c.relevance, reverse=True)
        _diversity_cap = max(1, diversity_limit)
        author_count: dict[str, int] = {}
        results: list[SubconsciousChunk] = []
        for chunk in ranked:
            author = str(chunk.written_by)
            if author_count.get(author, 0) >= _diversity_cap:
                continue
            author_count[author] = author_count.get(author, 0) + 1
            results.append(chunk)
            if len(results) >= max(1, k):
                break

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
