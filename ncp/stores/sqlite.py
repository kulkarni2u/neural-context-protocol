"""SQLite-backed NCP store."""

from __future__ import annotations

from collections.abc import Sequence
from contextlib import contextmanager
from difflib import SequenceMatcher
import json
from pathlib import Path
import sqlite3
import time

from rank_bm25 import BM25Okapi

from ncp.stores.base import BaseStore, NCPStoreUnavailableError
from ncp.types import ConsciousBlock, NCPResponse, SubconsciousChunk, TurnRecord, Whisper


SCHEMA = """
CREATE TABLE IF NOT EXISTS chunks (
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
    source_refs TEXT DEFAULT '[]',
    schema_version INTEGER DEFAULT 1,
    created_at REAL NOT NULL,
    base_trust REAL DEFAULT 0.7,
    generation INTEGER DEFAULT 0,
    result_confidence REAL,
    result_attempts INTEGER,
    conditions TEXT DEFAULT '[]',
    valid_while TEXT,
    expiry REAL,
    owner TEXT,
    meta TEXT DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS tombstones (
    chunk_id TEXT PRIMARY KEY,
    forward_ref TEXT,
    tombstoned_at REAL NOT NULL,
    expires_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS whispers (
    whisper_id TEXT PRIMARY KEY,
    pipeline_id TEXT,
    from_agent TEXT NOT NULL,
    target TEXT NOT NULL,
    whisper_type TEXT NOT NULL,
    payload TEXT NOT NULL,
    confidence REAL NOT NULL,
    ref TEXT,
    created_at REAL NOT NULL,
    expires_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS turn_records (
    turn_id TEXT PRIMARY KEY,
    agent_id TEXT NOT NULL,
    pipeline_id TEXT,
    task TEXT NOT NULL,
    slot TEXT NOT NULL,
    result TEXT NOT NULL,
    result_full TEXT NOT NULL,
    created_at REAL NOT NULL,
    expires_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS conscious_log (
    log_id INTEGER PRIMARY KEY AUTOINCREMENT,
    agent_id TEXT NOT NULL,
    pipeline_id TEXT,
    snapshot_hash TEXT NOT NULL,
    snapshot_json TEXT NOT NULL,
    logged_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS cost_log (
    turn_id TEXT PRIMARY KEY,
    pipeline_id TEXT,
    agent_id TEXT NOT NULL,
    model TEXT NOT NULL,
    input_tokens INTEGER NOT NULL,
    output_tokens INTEGER NOT NULL,
    cache_read_tokens INTEGER DEFAULT 0,
    cost_usd REAL NOT NULL,
    latency_ms INTEGER,
    logged_at REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_chunks_pipeline ON chunks(pipeline_id, scope, zone);
CREATE INDEX IF NOT EXISTS idx_chunks_layer ON chunks(layer);
CREATE INDEX IF NOT EXISTS idx_chunks_created ON chunks(created_at);
CREATE INDEX IF NOT EXISTS idx_whispers_target ON whispers(target, expires_at);
CREATE INDEX IF NOT EXISTS idx_whispers_pipeline ON whispers(pipeline_id, expires_at);
CREATE INDEX IF NOT EXISTS idx_turns_agent ON turn_records(agent_id, pipeline_id);
CREATE INDEX IF NOT EXISTS idx_conscious_agent ON conscious_log(agent_id, logged_at);
CREATE INDEX IF NOT EXISTS idx_cost_pipeline ON cost_log(pipeline_id, logged_at);
"""


class SQLiteStore(BaseStore):
    """Project-local SQLite store with BM25 retrieval and dogfood primitives."""

    def __init__(
        self,
        path: str | Path,
        *,
        max_working_chunks: int = 500,
        gc_threshold: int = 400,
    ) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.max_working_chunks = max_working_chunks
        self.gc_threshold = gc_threshold
        self._init_db()

    @contextmanager
    def _connect(self) -> sqlite3.Connection:
        try:
            connection = sqlite3.connect(self.path)
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA journal_mode=WAL;")
            connection.execute("PRAGMA synchronous=NORMAL;")
            connection.execute("PRAGMA foreign_keys=ON;")
            connection.execute("PRAGMA cache_size=-64000;")
        except sqlite3.Error as exc:
            raise NCPStoreUnavailableError(
                f"SQLite store unavailable at {self.path}: {exc}"
            ) from exc
        try:
            yield connection
            connection.commit()
        except sqlite3.Error as exc:
            try:
                connection.rollback()
            except sqlite3.Error:
                pass
            raise NCPStoreUnavailableError(
                f"SQLite store operation failed at {self.path}: {exc}"
            ) from exc
        finally:
            connection.close()

    def _init_db(self) -> None:
        with self._connect() as connection:
            connection.executescript(SCHEMA)

    def write(self, chunk: SubconsciousChunk) -> bool:
        chunk = self._validate_chunk_for_write(chunk)
        with self._connect() as connection:
            self._soft_gc(connection)
            self._assert_src_immutable(connection, chunk)
            if self._is_duplicate(connection, chunk):
                return False
            connection.execute(
                """
                INSERT OR REPLACE INTO chunks (
                    chunk_id, pipeline_id, scope, zone, layer, chunk_type, content, src,
                    written_by, caused_by, conscious_hash, evidence_id, version, supersedes,
                    source_refs, schema_version, created_at, base_trust, generation,
                    result_confidence, result_attempts, conditions, valid_while, expiry, owner, meta
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
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

        query_terms = {term for term in text.lower().split() if term}
        corpus = [row["content"].lower().split() for row in rows]
        bm25 = BM25Okapi(corpus)
        scores = bm25.get_scores(text.split())
        candidates: list[SubconsciousChunk] = []
        for score, row, doc_tokens in zip(scores, rows, corpus, strict=True):
            if query_terms:
                overlap = len(query_terms.intersection(doc_tokens))
                if overlap == 0:
                    continue
                lexical_floor = overlap / len(query_terms)
                relevance = max(float(score), lexical_floor)
            else:
                relevance = 1.0
            if relevance < min_score:
                continue
            chunk = self._row_to_chunk(row)
            chunk.relevance = max(0.0, relevance)
            candidates.append(chunk)
        ranked = sorted(
            candidates,
            key=lambda chunk: (chunk.effective_score, chunk.relevance),
            reverse=True,
        )

        diversity_limit = 2
        author_count: dict[str, int] = {}
        results: list[SubconsciousChunk] = []
        for chunk in ranked:
            author = str(chunk.written_by)
            if author_count.get(author, 0) >= diversity_limit:
                continue
            author_count[author] = author_count.get(author, 0) + 1
            results.append(chunk)
            if len(results) >= max(1, min(k, 4)):
                break
        return results

    def tombstone(self, chunk_id: str, *, forward_ref: str | None = None, ttl_seconds: int = 86400) -> None:
        with self._connect() as connection:
            connection.execute("DELETE FROM chunks WHERE chunk_id = ?", (chunk_id,))
            connection.execute(
                """
                INSERT OR REPLACE INTO tombstones (chunk_id, forward_ref, tombstoned_at, expires_at)
                VALUES (?, ?, ?, ?)
                """,
                (chunk_id, forward_ref, time.time(), time.time() + ttl_seconds),
            )

    def resolve_ref(self, ref: str, *, max_hops: int = 10) -> str | None:
        chunk_id = ref.removeprefix("ctx://sub/")
        hops = 0
        with self._connect() as connection:
            current = chunk_id
            while hops < max_hops:
                row = connection.execute(
                    "SELECT chunk_id FROM chunks WHERE chunk_id = ?",
                    (current,),
                ).fetchone()
                if row is not None:
                    return str(row["chunk_id"])
                tombstone = connection.execute(
                    "SELECT forward_ref FROM tombstones WHERE chunk_id = ?",
                    (current,),
                ).fetchone()
                if tombstone is None or tombstone["forward_ref"] is None:
                    return "ctx://dead-end/missing"
                current = str(tombstone["forward_ref"])
                hops += 1
        return "ctx://dead-end/max-hops"

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

    def emit_whisper(self, whisper: Whisper) -> None:
        whisper = Whisper.model_validate(whisper.model_dump())
        with self._connect() as connection:
            self._soft_gc(connection)
            connection.execute(
                """
                INSERT OR REPLACE INTO whispers (
                    whisper_id, pipeline_id, from_agent, target, whisper_type,
                    payload, confidence, ref, created_at, expires_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    whisper.whisper_id,
                    whisper.pipeline_id,
                    whisper.from_agent,
                    whisper.target,
                    whisper.whisper_type,
                    whisper.payload,
                    whisper.confidence,
                    whisper.ref,
                    whisper.created_at,
                    whisper.created_at + whisper.ttl_seconds,
                ),
            )

    def drain_whispers(
        self,
        *,
        agent_id: str,
        pipeline_id: str | None = None,
        max_items: int = 3,
        min_confidence: float = 0.60,
    ) -> list[Whisper]:
        now = time.time()
        with self._connect() as connection:
            connection.execute("DELETE FROM whispers WHERE expires_at <= ?", (now,))
            drained = self._select_whispers(
                connection,
                agent_id=agent_id,
                pipeline_id=pipeline_id,
                max_items=max_items,
                min_confidence=min_confidence,
            )

            if drained:
                connection.executemany(
                    "DELETE FROM whispers WHERE whisper_id = ?",
                    [(whisper.whisper_id,) for whisper in drained],
                )
            return drained

    def peek_whispers(
        self,
        *,
        agent_id: str,
        pipeline_id: str | None = None,
        max_items: int = 3,
        min_confidence: float = 0.60,
    ) -> list[Whisper]:
        """Return eligible whispers without consuming them."""

        now = time.time()
        with self._connect() as connection:
            connection.execute("DELETE FROM whispers WHERE expires_at <= ?", (now,))
            return self._select_whispers(
                connection,
                agent_id=agent_id,
                pipeline_id=pipeline_id,
                max_items=max_items,
                min_confidence=min_confidence,
            )

    def acknowledge_whispers(self, whisper_ids: Sequence[str]) -> int:
        """Delete already-processed whispers by id."""

        if not whisper_ids:
            return 0
        with self._connect() as connection:
            cursor = connection.executemany(
                "DELETE FROM whispers WHERE whisper_id = ?",
                [(whisper_id,) for whisper_id in whisper_ids],
            )
            return int(cursor.rowcount)

    def log_turn_record(self, record: TurnRecord) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                INSERT OR REPLACE INTO turn_records (
                    turn_id, agent_id, pipeline_id, task, slot, result, result_full, created_at, expires_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
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

    def resolve_recent_ref(self, ref: str) -> TurnRecord | None:
        if not ref.startswith("r:sub/"):
            return None
        turn_id = ref.split("/", 1)[1]
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM turn_records WHERE turn_id = ?", (turn_id,)).fetchone()
        return None if row is None else TurnRecord(**dict(row))

    def log_conscious(self, conscious: ConsciousBlock, *, snapshot_hash: str) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO conscious_log (agent_id, pipeline_id, snapshot_hash, snapshot_json, logged_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    conscious.agent_id,
                    conscious.pipeline_id,
                    snapshot_hash,
                    conscious.model_dump_json(),
                    time.time(),
                ),
            )

    def log_cost(self, *, agent_id: str, response: NCPResponse) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                INSERT OR REPLACE INTO cost_log (
                    turn_id, pipeline_id, agent_id, model, input_tokens, output_tokens,
                    cache_read_tokens, cost_usd, latency_ms, logged_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    response.turn_id,
                    response.pipeline_id,
                    agent_id,
                    response.model,
                    response.input_tokens,
                    response.output_tokens,
                    response.cache_read_tokens,
                    response.cost_usd,
                    response.latency_ms,
                    time.time(),
                ),
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
            connection.execute(
                """
                INSERT OR REPLACE INTO cost_log (
                    turn_id, pipeline_id, agent_id, model, input_tokens, output_tokens,
                    cache_read_tokens, cost_usd, latency_ms, logged_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
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

    def get_pipeline_goal_versions(
        self,
        *,
        pipeline_id: str,
        current_agent: str | None = None,
    ) -> dict[str, int]:
        versions: dict[str, int] = {}
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT agent_id, snapshot_json FROM conscious_log
                WHERE pipeline_id = ?
                ORDER BY logged_at DESC
                """,
                (pipeline_id,),
            ).fetchall()
        seen_agents: set[str] = set()
        for row in rows:
            agent = str(row["agent_id"])
            if agent in seen_agents:
                continue
            if current_agent is not None and agent == current_agent:
                continue
            seen_agents.add(agent)
            try:
                snapshot = json.loads(row["snapshot_json"])
                version = snapshot.get("goal_version", 1)
                versions[agent] = int(version)
            except (json.JSONDecodeError, KeyError, TypeError, ValueError):
                pass
        return versions

    def status(self) -> dict[str, int | float]:
        with self._connect() as connection:
            chunks = connection.execute("SELECT COUNT(*) AS count FROM chunks").fetchone()["count"]
            whispers = connection.execute("SELECT COUNT(*) AS count FROM whispers").fetchone()["count"]
            turns = connection.execute("SELECT COUNT(*) AS count FROM turn_records").fetchone()["count"]
            costs = connection.execute("SELECT COALESCE(SUM(cost_usd), 0.0) AS total FROM cost_log").fetchone()["total"]
        return {
            "chunk_count": int(chunks),
            "whisper_count": int(whispers),
            "turn_record_count": int(turns),
            "cost_usd_total": float(costs),
        }

    def status_detail(self, *, pipeline_id: str | None = None) -> dict[str, object]:
        with self._connect() as connection:
            overview = {
                "chunk_count": int(
                    connection.execute(
                        "SELECT COUNT(*) AS count FROM chunks WHERE pipeline_id = ?"
                        if pipeline_id is not None
                        else "SELECT COUNT(*) AS count FROM chunks",
                        [] if pipeline_id is None else [pipeline_id],
                    ).fetchone()["count"]
                ),
                "tombstone_count": int(
                    connection.execute("SELECT COUNT(*) AS count FROM tombstones").fetchone()["count"]
                ),
                "whisper_count": int(
                    connection.execute(
                        "SELECT COUNT(*) AS count FROM whispers WHERE pipeline_id = ?"
                        if pipeline_id is not None
                        else "SELECT COUNT(*) AS count FROM whispers",
                        [] if pipeline_id is None else [pipeline_id],
                    ).fetchone()["count"]
                ),
                "turn_record_count": int(
                    connection.execute(
                        "SELECT COUNT(*) AS count FROM turn_records WHERE pipeline_id = ?"
                        if pipeline_id is not None
                        else "SELECT COUNT(*) AS count FROM turn_records",
                        [] if pipeline_id is None else [pipeline_id],
                    ).fetchone()["count"]
                ),
                "conscious_snapshot_count": int(
                    connection.execute(
                        "SELECT COUNT(*) AS count FROM conscious_log WHERE pipeline_id = ?"
                        if pipeline_id is not None
                        else "SELECT COUNT(*) AS count FROM conscious_log",
                        [] if pipeline_id is None else [pipeline_id],
                    ).fetchone()["count"]
                ),
                "cost_entry_count": int(
                    connection.execute(
                        "SELECT COUNT(*) AS count FROM cost_log WHERE pipeline_id = ?"
                        if pipeline_id is not None
                        else "SELECT COUNT(*) AS count FROM cost_log",
                        [] if pipeline_id is None else [pipeline_id],
                    ).fetchone()["count"]
                ),
            }
            overview["pipeline_count"] = int(
                connection.execute(
                    "SELECT COUNT(DISTINCT pipeline_id) AS count FROM chunks",
                ).fetchone()["count"]
            )
            overview["cost_usd_total"] = float(
                connection.execute(
                    "SELECT COALESCE(SUM(cost_usd), 0.0) AS total FROM cost_log WHERE pipeline_id = ?"
                    if pipeline_id is not None
                    else "SELECT COALESCE(SUM(cost_usd), 0.0) AS total FROM cost_log",
                    [] if pipeline_id is None else [pipeline_id],
                ).fetchone()["total"]
            )
            latest_chunk = connection.execute(
                "SELECT MAX(created_at) AS latest FROM chunks WHERE pipeline_id = ?"
                if pipeline_id is not None
                else "SELECT MAX(created_at) AS latest FROM chunks",
                [] if pipeline_id is None else [pipeline_id],
            ).fetchone()["latest"]
            latest_whisper = connection.execute(
                "SELECT MAX(created_at) AS latest FROM whispers WHERE pipeline_id = ?"
                if pipeline_id is not None
                else "SELECT MAX(created_at) AS latest FROM whispers",
                [] if pipeline_id is None else [pipeline_id],
            ).fetchone()["latest"]
            latest_turn = connection.execute(
                "SELECT MAX(created_at) AS latest FROM turn_records WHERE pipeline_id = ?"
                if pipeline_id is not None
                else "SELECT MAX(created_at) AS latest FROM turn_records",
                [] if pipeline_id is None else [pipeline_id],
            ).fetchone()["latest"]
            latest_cost = connection.execute(
                "SELECT MAX(logged_at) AS latest FROM cost_log WHERE pipeline_id = ?"
                if pipeline_id is not None
                else "SELECT MAX(logged_at) AS latest FROM cost_log",
                [] if pipeline_id is None else [pipeline_id],
            ).fetchone()["latest"]
            activity_candidates = [
                value for value in (latest_chunk, latest_whisper, latest_turn, latest_cost) if value is not None
            ]
            overview["last_activity_at"] = max(activity_candidates) if activity_candidates else None

            layer_rows = connection.execute(
                """
                SELECT layer, COUNT(*) AS count
                FROM chunks
                WHERE pipeline_id = ?
                GROUP BY layer
                ORDER BY count DESC, layer ASC
                """,
                [pipeline_id],
            ).fetchall() if pipeline_id is not None else connection.execute(
                """
                SELECT layer, COUNT(*) AS count
                FROM chunks
                GROUP BY layer
                ORDER BY count DESC, layer ASC
                """
            ).fetchall()
            layer_counts = {str(row["layer"]): int(row["count"]) for row in layer_rows}

            pipeline_rows = connection.execute(
                """
                SELECT
                    pipeline_id,
                    COUNT(*) AS chunk_count,
                    MAX(created_at) AS last_chunk_at
                FROM chunks
                WHERE pipeline_id IS NOT NULL
                GROUP BY pipeline_id
                ORDER BY last_chunk_at DESC
                LIMIT 5
                """
                if pipeline_id is None
                else """
                SELECT
                    pipeline_id,
                    COUNT(*) AS chunk_count,
                    MAX(created_at) AS last_chunk_at
                FROM chunks
                WHERE pipeline_id = ?
                GROUP BY pipeline_id
                ORDER BY last_chunk_at DESC
                LIMIT 5
                """,
                [] if pipeline_id is None else [pipeline_id],
            ).fetchall()
            recent_pipelines = [
                {
                    "pipeline_id": str(row["pipeline_id"]),
                    "chunk_count": int(row["chunk_count"]),
                    "last_chunk_at": float(row["last_chunk_at"]),
                }
                for row in pipeline_rows
                if row["pipeline_id"] is not None
            ]

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
        clauses: list[str] = []
        params: list[object] = []
        if pipeline_id is not None:
            clauses.append("pipeline_id = ?")
            params.append(pipeline_id)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""

        with self._connect() as connection:
            total_row = connection.execute(
                f"""
                SELECT
                    COALESCE(SUM(cost_usd), 0.0) AS cost_usd_total,
                    COALESCE(SUM(input_tokens), 0) AS input_tokens_total,
                    COALESCE(SUM(output_tokens), 0) AS output_tokens_total,
                    COALESCE(SUM(cache_read_tokens), 0) AS cache_read_tokens_total,
                    COUNT(*) AS entry_count,
                    COALESCE(AVG(latency_ms), 0.0) AS avg_latency_ms
                FROM cost_log
                {where}
                """,
                params,
            ).fetchone()
            by_agent_rows = connection.execute(
                f"""
                SELECT
                    agent_id,
                    COUNT(*) AS turns,
                    COALESCE(SUM(cost_usd), 0.0) AS cost_usd_total
                FROM cost_log
                {where}
                GROUP BY agent_id
                ORDER BY cost_usd_total DESC, agent_id ASC
                """,
                params,
            ).fetchall()
            by_model_rows = connection.execute(
                f"""
                SELECT
                    model,
                    COUNT(*) AS turns,
                    COALESCE(SUM(cost_usd), 0.0) AS cost_usd_total
                FROM cost_log
                {where}
                GROUP BY model
                ORDER BY cost_usd_total DESC, model ASC
                """,
                params,
            ).fetchall()
            recent_rows = connection.execute(
                f"""
                SELECT
                    turn_id,
                    pipeline_id,
                    agent_id,
                    model,
                    input_tokens,
                    output_tokens,
                    cache_read_tokens,
                    cost_usd,
                    latency_ms,
                    logged_at
                FROM cost_log
                {where}
                ORDER BY logged_at DESC
                LIMIT ?
                """,
                [*params, max(1, limit)],
            ).fetchall()

        return {
            "summary": {
                "cost_usd_total": float(total_row["cost_usd_total"]),
                "input_tokens_total": int(total_row["input_tokens_total"]),
                "output_tokens_total": int(total_row["output_tokens_total"]),
                "cache_read_tokens_total": int(total_row["cache_read_tokens_total"]),
                "entry_count": int(total_row["entry_count"]),
                "avg_latency_ms": float(total_row["avg_latency_ms"]),
            },
            "by_agent": [
                {
                    "agent_id": str(row["agent_id"]),
                    "turns": int(row["turns"]),
                    "cost_usd_total": float(row["cost_usd_total"]),
                }
                for row in by_agent_rows
            ],
            "by_model": [
                {
                    "model": str(row["model"]),
                    "turns": int(row["turns"]),
                    "cost_usd_total": float(row["cost_usd_total"]),
                }
                for row in by_model_rows
            ],
            "recent_entries": [
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
                for row in recent_rows
            ],
        }

    def _load_query_rows(
        self,
        connection: sqlite3.Connection,
        *,
        layer: str | None,
        pipeline_id: str | None,
        scope: str | None,
        zone: str,
    ) -> list[sqlite3.Row]:
        clauses = ["zone = ?"]
        params: list[object] = [zone]
        if layer is not None:
            clauses.append("layer = ?")
            params.append(layer)
        if pipeline_id is None:
            clauses.append("(pipeline_id IS NULL OR scope = 'global')")
        else:
            clauses.append("(pipeline_id = ? OR scope = 'global')")
            params.append(pipeline_id)
        if scope is not None:
            clauses.append("scope = ?")
            params.append(scope)
        return connection.execute(
            f"SELECT * FROM chunks WHERE {' AND '.join(clauses)} ORDER BY created_at DESC",
            params,
        ).fetchall()

    def _row_to_chunk(self, row: sqlite3.Row) -> SubconsciousChunk:
        created_at = float(row["created_at"])
        chunk = SubconsciousChunk(
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
            conditions=json.loads(row["conditions"]),
            valid_while=row["valid_while"],
            expiry=row["expiry"],
            owner=row["owner"],
            chunk_type=str(row["chunk_type"]),
            pipeline_id=row["pipeline_id"],
            scope=str(row["scope"]),
            zone=str(row["zone"]),
            schema_version=int(row["schema_version"]),
            supersedes=row["supersedes"],
            source_refs=json.loads(row["source_refs"]),
            age_seconds=max(0.0, time.time() - created_at),
        )
        return chunk

    def _row_to_whisper(self, row: sqlite3.Row) -> Whisper:
        ttl_seconds = max(1, int(round(float(row["expires_at"]) - float(row["created_at"]))))
        return Whisper(
            from_agent=str(row["from_agent"]),
            target=str(row["target"]),
            whisper_type=str(row["whisper_type"]),
            payload=str(row["payload"]),
            confidence=float(row["confidence"]),
            whisper_id=str(row["whisper_id"]),
            ref=row["ref"],
            created_at=float(row["created_at"]),
            ttl_seconds=ttl_seconds,
            pipeline_id=row["pipeline_id"],
        )

    def _select_whispers(
        self,
        connection: sqlite3.Connection,
        *,
        agent_id: str,
        pipeline_id: str | None,
        max_items: int,
        min_confidence: float,
    ) -> list[Whisper]:
        clauses = ["expires_at > ?", "target IN (?, '*')"]
        params: list[object] = [time.time(), agent_id]
        if pipeline_id is None:
            clauses.append("pipeline_id IS NULL")
        else:
            clauses.append("pipeline_id = ?")
            params.append(pipeline_id)
        rows = connection.execute(
            f"""
            SELECT * FROM whispers
            WHERE {' AND '.join(clauses)}
            ORDER BY CASE WHEN whisper_type = 'alert' THEN 0 ELSE 1 END, created_at ASC
            """,
            params,
        ).fetchall()

        selected: list[Whisper] = []
        for row in rows:
            whisper = self._row_to_whisper(row)
            if whisper.whisper_type not in {"alert", "world_check"} and whisper.confidence < min_confidence:
                continue
            selected.append(whisper)
            if len(selected) >= max_items:
                break
        return selected

    def _with_runtime_age(self, chunk: SubconsciousChunk) -> SubconsciousChunk:
        return chunk.model_copy(update={"age_seconds": max(0.0, chunk.age_seconds)})

    def _validate_chunk_for_write(self, chunk: SubconsciousChunk) -> SubconsciousChunk:
        validated = SubconsciousChunk.model_validate(chunk.model_dump())
        return self._with_runtime_age(validated)

    def _assert_src_immutable(self, connection: sqlite3.Connection, chunk: SubconsciousChunk) -> None:
        row = connection.execute(
            "SELECT src FROM chunks WHERE chunk_id = ?",
            (chunk.chunk_id,),
        ).fetchone()
        if row is None:
            return
        existing_src = str(row["src"])
        if existing_src != chunk.src:
            raise ValueError(
                f"src is immutable for chunk_id={chunk.chunk_id}: existing={existing_src} new={chunk.src}"
            )

    def _is_duplicate(self, connection: sqlite3.Connection, chunk: SubconsciousChunk) -> bool:
        rows = connection.execute(
            """
            SELECT content FROM chunks
            WHERE zone = ? AND layer = ? AND IFNULL(pipeline_id, '') = IFNULL(?, '')
            """,
            (chunk.zone, chunk.layer, chunk.pipeline_id),
        ).fetchall()
        for row in rows:
            similarity = SequenceMatcher(None, chunk.content, str(row["content"])).ratio()
            if similarity > 0.92:
                return True
        return False

    def _soft_gc(self, connection: sqlite3.Connection) -> None:
        now = time.time()
        connection.execute("DELETE FROM tombstones WHERE expires_at <= ?", (now,))
        connection.execute("DELETE FROM whispers WHERE expires_at <= ?", (now,))
        connection.execute("DELETE FROM turn_records WHERE expires_at <= ?", (now,))

    def _hard_gc(self, connection: sqlite3.Connection, *, pipeline_id: str | None) -> None:
        clauses = ["zone = 'working'"]
        params: list[object] = []
        if pipeline_id is not None:
            clauses.append("pipeline_id = ?")
            params.append(pipeline_id)
        count = connection.execute(
            f"SELECT COUNT(*) AS count FROM chunks WHERE {' AND '.join(clauses)}",
            params,
        ).fetchone()["count"]
        if int(count) <= self.max_working_chunks:
            return
        overflow = int(count) - self.gc_threshold
        delete_rows = connection.execute(
            f"""
            SELECT chunk_id FROM chunks
            WHERE {' AND '.join(clauses)}
            ORDER BY created_at ASC
            LIMIT ?
            """,
            [*params, overflow],
        ).fetchall()
        if delete_rows:
            connection.executemany(
                "DELETE FROM chunks WHERE chunk_id = ?",
                [(row["chunk_id"],) for row in delete_rows],
            )
