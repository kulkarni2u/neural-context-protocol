"""SQLite-backed NCP store."""

from __future__ import annotations

from collections.abc import Sequence
from contextlib import contextmanager
from difflib import SequenceMatcher
import json
from pathlib import Path
import sqlite3
import time

from ncp.config import NCPConfig
from ncp.stores.base import BaseStore, NCPStoreUnavailableError
from ncp.stores.consolidation import cluster_by_tags, find_merge_candidates
from ncp.stores.retrieval import (
    DEFAULT_RETRIEVAL_POLICY,
    RetrievalPolicy,
    apply_diversity_limit,
    build_lexical_candidates,
    normalize_result_limit,
)
from ncp.types import CalibrationReport, ConsolidationReport, ConsciousBlock, NCPResponse, SubconsciousChunk, TurnRecord, Whisper


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
    meta TEXT DEFAULT '{}',
    retrieval_count INTEGER DEFAULT 0,
    last_retrieved_at REAL,
    written_at_drift REAL DEFAULT 0.0
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

CREATE TABLE IF NOT EXISTS drift_history (
    session_id TEXT NOT NULL,
    turn INTEGER NOT NULL,
    drift_score REAL NOT NULL,
    ts REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_drift_session ON drift_history(session_id, turn);
"""


class SQLiteStore(BaseStore):
    """Project-local SQLite store with BM25 retrieval and dogfood primitives."""

    def __init__(
        self,
        path: str | Path,
        *,
        max_working_chunks: int = 500,
        gc_threshold: int = 400,
        retrieval_policy: RetrievalPolicy | None = None,
        config: NCPConfig | None = None,
    ) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.max_working_chunks = max_working_chunks
        self.gc_threshold = gc_threshold
        self.retrieval_policy = retrieval_policy or DEFAULT_RETRIEVAL_POLICY

        from ncp.stores.rerank import Reranker
        from ncp.config import load_config
        try:
            cfg = config or load_config()
            self.reranker = Reranker(cfg)
        except Exception:
            class DummyConfig:
                rerank_enabled = False
                rerank_provider = "local"
                rerank_model = None
                values: dict = {}
            self.reranker = Reranker(DummyConfig())  # type: ignore[arg-type]

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
            # Upgrade existing databases that predate retrieval tracking columns.
            for ddl in (
                "ALTER TABLE chunks ADD COLUMN retrieval_count INTEGER DEFAULT 0",
                "ALTER TABLE chunks ADD COLUMN last_retrieved_at REAL",
                "ALTER TABLE chunks ADD COLUMN written_at_drift REAL DEFAULT 0.0",
                "CREATE TABLE IF NOT EXISTS drift_history (session_id TEXT NOT NULL, turn INTEGER NOT NULL, drift_score REAL NOT NULL, ts REAL NOT NULL)",
                "CREATE INDEX IF NOT EXISTS idx_drift_session ON drift_history(session_id, turn)",
            ):
                try:
                    connection.execute(ddl)
                except Exception:
                    pass  # column already exists

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
                    result_confidence, result_attempts, conditions, valid_while, expiry, owner, meta,
                    written_at_drift
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                    chunk.written_at_drift,
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
        retrieval_mode: str = "hybrid",
        embedding: list[float] | None = None,
        diversity_limit: int = 2,
    ) -> list[SubconsciousChunk]:
        _VALID_RETRIEVAL_MODES = ("hybrid", "trust_recency", "vector")
        if retrieval_mode not in _VALID_RETRIEVAL_MODES:
            raise ValueError(
                f"Unknown retrieval_mode {retrieval_mode!r}; expected one of {_VALID_RETRIEVAL_MODES}"
            )
        if retrieval_mode == "vector":
            raise ValueError(
                "retrieval_mode='vector' requires pgvector; SQLite does not support ANN search"
            )

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
                age_seconds = max(0.0, now - float(row["created_at"]))
                score = policy.score_no_bm25(
                    age_seconds=age_seconds,
                    base_trust=float(row["base_trust"]),
                    generation=int(row["generation"]),
                    written_at_drift=float(row["written_at_drift"]) if row["written_at_drift"] is not None else 0.0,
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
                hybrid_score = policy.score(
                    bm25_normalized=lexical_candidate.lexical_signal,
                    age_seconds=age_seconds,
                    base_trust=float(row["base_trust"]),
                    generation=int(row["generation"]),
                    written_at_drift=float(row["written_at_drift"]) if row["written_at_drift"] is not None else 0.0,
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
            placeholders = ",".join("?" * len(results))
            with self._connect() as connection:
                connection.execute(
                    f"UPDATE chunks SET retrieval_count = retrieval_count + 1,"
                    f" last_retrieved_at = ? WHERE chunk_id IN ({placeholders})",
                    [now] + [c.chunk_id for c in results],
                )
            for chunk in results:
                chunk.retrieval_count += 1
                chunk.last_retrieved_at = now

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
            deleted = 0
            for whisper_id in whisper_ids:
                cursor = connection.execute(
                    "DELETE FROM whispers WHERE whisper_id = ?",
                    (whisper_id,),
                )
                deleted += cursor.rowcount
            return deleted

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
            query = "SELECT * FROM chunks WHERE chunk_id NOT IN (SELECT chunk_id FROM tombstones)"
            params: list = []
            if pipeline_id is not None:
                query += " AND pipeline_id = ?"
                params.append(pipeline_id)
            rows = connection.execute(query, params).fetchall()

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
                    supersedes_json = json.dumps(loser_ids)
                    new_gen = keeper.generation + 1
                    with self._connect() as connection:
                        for loser_id in loser_ids:
                            connection.execute("DELETE FROM chunks WHERE chunk_id = ?", (loser_id,))
                            connection.execute(
                                "INSERT OR REPLACE INTO tombstones (chunk_id, forward_ref, tombstoned_at, expires_at)"
                                " VALUES (?, ?, ?, ?)",
                                (loser_id, keeper.chunk_id, time.time(), time.time() + 86400),
                            )
                        connection.execute(
                            "UPDATE chunks SET generation = ?, supersedes = ? WHERE chunk_id = ?",
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
                row = connection.execute(
                    "SELECT chunk_id, base_trust, src FROM chunks"
                    " WHERE chunk_id = ? AND chunk_id NOT IN (SELECT chunk_id FROM tombstones)",
                    (chunk_id,),
                ).fetchone()
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
                    connection.execute(
                        "UPDATE chunks SET base_trust = ? WHERE chunk_id = ?",
                        (trust, chunk_id),
                    )
                report.adjusted += 1
        else:
            # --- Batch decay mode ---
            now = time.time()
            cutoff_age = recency_half_life_seconds

            query = (
                "SELECT chunk_id, base_trust, src, generation, created_at,"
                " retrieval_count FROM chunks"
                " WHERE chunk_id NOT IN (SELECT chunk_id FROM tombstones)"
            )
            params: list = []
            if pipeline_id is not None:
                query += " AND pipeline_id = ?"
                params.append(pipeline_id)

            with self._connect() as connection:
                # Acquire an immediate write lock before reading so the read and
                # subsequent updates happen atomically — no concurrent writer can
                # sneak in between the SELECT and the UPDATEs.
                if not dry_run:
                    connection.execute("BEGIN IMMEDIATE")
                rows = connection.execute(query, params).fetchall()
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
                    for new_trust, cid in updates:
                        connection.execute(
                            "UPDATE chunks SET base_trust = ? WHERE chunk_id = ?",
                            (new_trust, cid),
                        )

        report.duration_seconds = time.monotonic() - started
        return report

    def _emit_consolidation_whisper(self, *, pipeline_id: str | None) -> None:
        from ncp.types import Whisper
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

    def log_drift_history(self, *, session_id: str, turn: int, drift_score: float) -> None:
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO drift_history (session_id, turn, drift_score, ts) VALUES (?, ?, ?, ?)",
                (session_id, turn, drift_score, time.time()),
            )

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

    def viz_data(self, *, pipeline_id: str | None = None) -> dict[str, object]:
        """Return structured data for the operator viz view."""
        now = time.time()
        with self._connect() as connection:
            # 1. Chunk distribution: layer x zone counts (live chunks only)
            live_clause = "chunk_id NOT IN (SELECT chunk_id FROM tombstones)"
            if pipeline_id is not None:
                dist_rows = connection.execute(
                    f"""
                    SELECT layer, zone, COUNT(*) AS count
                    FROM chunks
                    WHERE {live_clause} AND pipeline_id = ?
                    GROUP BY layer, zone
                    ORDER BY layer, zone
                    """,
                    (pipeline_id,),
                ).fetchall()
            else:
                dist_rows = connection.execute(
                    f"""
                    SELECT layer, zone, COUNT(*) AS count
                    FROM chunks
                    WHERE {live_clause}
                    GROUP BY layer, zone
                    ORDER BY layer, zone
                    """
                ).fetchall()
            chunk_distribution = [
                {"layer": str(r["layer"]), "zone": str(r["zone"]), "count": int(r["count"])}
                for r in dist_rows
            ]

            # 2. Age brackets
            bracket_sql = f"""
                SELECT
                    CASE
                        WHEN (? - created_at) < 3600 THEN '<1h'
                        WHEN (? - created_at) < 14400 THEN '1-4h'
                        WHEN (? - created_at) < 86400 THEN '4-24h'
                        ELSE '>24h'
                    END AS bracket,
                    COUNT(*) AS count,
                    AVG(base_trust) AS avg_trust
                FROM chunks
                WHERE {live_clause}
                {"AND pipeline_id = ?" if pipeline_id is not None else ""}
                GROUP BY bracket
                ORDER BY bracket
            """
            bracket_params: list[object] = [now, now, now]
            if pipeline_id is not None:
                bracket_params.append(pipeline_id)
            bracket_rows = connection.execute(bracket_sql, bracket_params).fetchall()

            # Top layer per bracket (separate query)
            bracket_top_layer: dict[str, str] = {}
            for bracket_label, age_min, age_max in [
                ("<1h", 0, 3600),
                ("1-4h", 3600, 14400),
                ("4-24h", 14400, 86400),
                (">24h", 86400, None),
            ]:
                clause = f"(? - created_at) >= ? AND {live_clause}"
                params_layer: list[object] = [now, age_min]
                if age_max is not None:
                    clause += " AND (? - created_at) < ?"
                    params_layer.extend([now, age_max])
                if pipeline_id is not None:
                    clause += " AND pipeline_id = ?"
                    params_layer.append(pipeline_id)
                tl_row = connection.execute(
                    f"""
                    SELECT layer, COUNT(*) AS cnt FROM chunks
                    WHERE {clause}
                    GROUP BY layer ORDER BY cnt DESC LIMIT 1
                    """,
                    params_layer,
                ).fetchone()
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
            if pipeline_id is not None:
                top_rows = connection.execute(
                    f"""
                    SELECT chunk_id, layer, zone, pipeline_id, base_trust, created_at
                    FROM chunks
                    WHERE {live_clause} AND pipeline_id = ?
                    ORDER BY base_trust DESC, created_at DESC
                    LIMIT 5
                    """,
                    (pipeline_id,),
                ).fetchall()
            else:
                top_rows = connection.execute(
                    f"""
                    SELECT chunk_id, layer, zone, pipeline_id, base_trust, created_at
                    FROM chunks
                    WHERE {live_clause}
                    ORDER BY base_trust DESC, created_at DESC
                    LIMIT 5
                    """
                ).fetchall()
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
            if pipeline_id is not None:
                pipe_rows = connection.execute(
                    f"""
                    SELECT pipeline_id, COUNT(*) AS chunk_count, MAX(created_at) AS last_activity
                    FROM chunks
                    WHERE {live_clause} AND pipeline_id = ?
                    GROUP BY pipeline_id
                    ORDER BY last_activity DESC
                    """,
                    (pipeline_id,),
                ).fetchall()
            else:
                pipe_rows = connection.execute(
                    f"""
                    SELECT pipeline_id, COUNT(*) AS chunk_count, MAX(created_at) AS last_activity
                    FROM chunks
                    WHERE {live_clause} AND pipeline_id IS NOT NULL
                    GROUP BY pipeline_id
                    ORDER BY last_activity DESC
                    LIMIT 20
                    """
                ).fetchall()
            pipeline_summary = [
                {
                    "pipeline_id": str(r["pipeline_id"]),
                    "chunk_count": int(r["chunk_count"]),
                    "last_activity": float(r["last_activity"]),
                }
                for r in pipe_rows
                if r["pipeline_id"] is not None
            ]

            # 5. Whisper queue
            if pipeline_id is not None:
                wq_rows = connection.execute(
                    "SELECT whisper_type, COUNT(*) AS cnt FROM whispers WHERE pipeline_id = ? GROUP BY whisper_type",
                    (pipeline_id,),
                ).fetchall()
            else:
                wq_rows = connection.execute(
                    "SELECT whisper_type, COUNT(*) AS cnt FROM whispers GROUP BY whisper_type"
                ).fetchall()
            by_type = {str(r["whisper_type"]): int(r["cnt"]) for r in wq_rows}
            whisper_queue: dict[str, object] = {
                "total": sum(by_type.values()),
                "by_type": by_type,
            }

        return {
            "chunk_distribution": chunk_distribution,
            "age_brackets": age_brackets,
            "top_chunks": top_chunks,
            "pipeline_summary": pipeline_summary,
            "whisper_queue": whisper_queue,
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
            written_at_drift=float(row["written_at_drift"]) if row["written_at_drift"] is not None else 0.0,
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
            retrieval_count=int(row["retrieval_count"]) if row["retrieval_count"] is not None else 0,
            last_retrieved_at=float(row["last_retrieved_at"]) if row["last_retrieved_at"] is not None else None,
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
              AND chunk_id != ?
            """,
            (chunk.zone, chunk.layer, chunk.pipeline_id, chunk.chunk_id),
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
