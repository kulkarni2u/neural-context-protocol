"""SQLite-backed NCP store."""

from __future__ import annotations

from collections.abc import Sequence
from contextlib import contextmanager
from dataclasses import replace as dataclass_replace
from difflib import SequenceMatcher
import json
from pathlib import Path
import sqlite3
import time

from ncp.config import NCPConfig
from ncp.stores.base import BaseStore, NCPStoreUnavailableError
from ncp.stores.calibration import (
    FeedbackRow,
    ReputationUpdate,
    compute_feedback_updates,
    rollup_reputation,
)
from ncp.stores.consolidation import cluster_by_tags, find_merge_candidates
from ncp.stores.graph import (
    backfill_edges_for_chunk,
    infer_edges_for_chunk,
    parse_supersedes_ids,
    resolve_caused_by_fallback,
)
from ncp.stores.retrieval import (
    DEFAULT_RETRIEVAL_POLICY,
    RetrievalPolicy,
    apply_diversity_limit,
    blend_trust,
    build_lexical_candidates,
    normalize_query_terms,
    normalize_result_limit,
    score_trust_recency_candidate,
)
from ncp.tokens import estimate_tokens
from ncp.types import (
    CalibrationReport,
    ChunkEdge,
    ConsolidationReport,
    ConsciousBlock,
    NCPResponse,
    OutcomeRecord,
    SubconsciousChunk,
    TurnRecord,
    Whisper,
)


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
    written_at_drift REAL DEFAULT 0.0,
    dissent_count INTEGER DEFAULT 0,
    verified INTEGER DEFAULT 0,
    embedding BLOB,
    valid_from REAL,
    valid_to REAL,
    superseded_by TEXT
);

CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(
    chunk_id UNINDEXED,
    content,
    content='chunks',
    content_rowid='rowid'
);

CREATE TRIGGER IF NOT EXISTS chunks_ai AFTER INSERT ON chunks BEGIN
    INSERT INTO chunks_fts(rowid, chunk_id, content) VALUES (new.rowid, new.chunk_id, new.content);
END;

CREATE TRIGGER IF NOT EXISTS chunks_ad AFTER DELETE ON chunks BEGIN
    INSERT INTO chunks_fts(chunks_fts, rowid, chunk_id, content) VALUES ('delete', old.rowid, old.chunk_id, old.content);
END;

CREATE TRIGGER IF NOT EXISTS chunks_au AFTER UPDATE ON chunks BEGIN
    INSERT INTO chunks_fts(chunks_fts, rowid, chunk_id, content) VALUES ('delete', old.rowid, old.chunk_id, old.content);
    INSERT INTO chunks_fts(rowid, chunk_id, content) VALUES (new.rowid, new.chunk_id, new.content);
END;

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
    dissent_target TEXT,
    verified INTEGER DEFAULT 0,
    created_at REAL NOT NULL,
    expires_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS whisper_deliveries (
    whisper_id TEXT NOT NULL,
    agent_id TEXT NOT NULL,
    delivered_at REAL NOT NULL,
    PRIMARY KEY (whisper_id, agent_id)
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
    logged_at REAL NOT NULL,
    cost_source TEXT NOT NULL DEFAULT 'measured'
);

CREATE INDEX IF NOT EXISTS idx_chunks_pipeline ON chunks(pipeline_id, scope, zone);
CREATE INDEX IF NOT EXISTS idx_chunks_layer ON chunks(layer);
CREATE INDEX IF NOT EXISTS idx_chunks_created ON chunks(created_at);
CREATE INDEX IF NOT EXISTS idx_whispers_target ON whispers(target, expires_at);
CREATE INDEX IF NOT EXISTS idx_whispers_pipeline ON whispers(pipeline_id, expires_at);
CREATE INDEX IF NOT EXISTS idx_whisper_deliveries_agent ON whisper_deliveries(agent_id, whisper_id);
CREATE INDEX IF NOT EXISTS idx_turns_agent ON turn_records(agent_id, pipeline_id);
CREATE INDEX IF NOT EXISTS idx_conscious_agent ON conscious_log(agent_id, logged_at);
CREATE INDEX IF NOT EXISTS idx_conscious_pipeline_agent ON conscious_log(pipeline_id, agent_id, logged_at);
CREATE INDEX IF NOT EXISTS idx_cost_pipeline ON cost_log(pipeline_id, logged_at);

CREATE TABLE IF NOT EXISTS drift_history (
    session_id TEXT NOT NULL,
    turn INTEGER NOT NULL,
    drift_score REAL NOT NULL,
    ts REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_drift_session ON drift_history(session_id, turn);

CREATE TABLE IF NOT EXISTS identities (
    identity_id TEXT PRIMARY KEY,
    public_key TEXT NOT NULL,
    alg TEXT NOT NULL DEFAULT 'ed25519',
    label TEXT,
    created_at REAL NOT NULL,
    revoked_at REAL
);

CREATE TABLE IF NOT EXISTS reputation (
    identity_id TEXT PRIMARY KEY,
    alpha REAL NOT NULL DEFAULT 1.0,
    beta REAL NOT NULL DEFAULT 1.0,
    obs_count INTEGER NOT NULL DEFAULT 0,
    last_updated REAL NOT NULL DEFAULT 0.0
);

CREATE INDEX IF NOT EXISTS idx_reputation_updated ON reputation(last_updated);

CREATE TABLE IF NOT EXISTS outcomes (
    outcome_id TEXT PRIMARY KEY,
    turn_id TEXT,
    chunk_ids TEXT NOT NULL DEFAULT '[]',
    success INTEGER NOT NULL,
    weight REAL NOT NULL DEFAULT 1.0,
    note TEXT,
    created_at REAL NOT NULL,
    consumed INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS memo_entries (
    signature TEXT PRIMARY KEY,
    task TEXT NOT NULL,
    result_summary TEXT,
    chunk_ids TEXT NOT NULL DEFAULT '[]',
    outcome REAL DEFAULT 0.0,
    verified INTEGER DEFAULT 0,
    created_at REAL NOT NULL,
    last_hit_at REAL NOT NULL DEFAULT 0.0,
    hit_count INTEGER NOT NULL DEFAULT 0,
    output_tokens_est INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS memo_stats (
    stat_key TEXT PRIMARY KEY,
    stat_value INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS chunk_edges (
    edge_id TEXT PRIMARY KEY,
    src_chunk_id TEXT NOT NULL,
    dst_chunk_id TEXT NOT NULL,
    edge_type TEXT NOT NULL,
    weight REAL NOT NULL DEFAULT 1.0,
    created_at REAL NOT NULL,
    created_by TEXT,
    UNIQUE(src_chunk_id, dst_chunk_id, edge_type)
);

CREATE INDEX IF NOT EXISTS idx_chunk_edges_src ON chunk_edges(src_chunk_id);
CREATE INDEX IF NOT EXISTS idx_chunk_edges_dst ON chunk_edges(dst_chunk_id);
"""


class SQLiteStore(BaseStore):
    """Project-local SQLite store with BM25 retrieval and dogfood primitives."""

    def __init__(
        self,
        path: str | Path,
        *,
        max_working_chunks: int = 500,
        gc_threshold: int = 400,
        max_working_chunks_per_pipeline: int = 0,
        retrieval_policy: RetrievalPolicy | None = None,
        config: NCPConfig | None = None,
        embedding_adapter: object | None = None,
    ) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.max_working_chunks = max_working_chunks
        self.gc_threshold = gc_threshold
        self.max_working_chunks_per_pipeline = max_working_chunks_per_pipeline
        self.retention_evictions = 0
        # CAP-C7/WI-P2: count of `refines` edges inferred+persisted by the most
        # recent write() call (0 when disabled or none matched). Reset at the
        # top of every write() -- read immediately after by the MCP layer for
        # the `edges_inferred` response field; not meaningful across concurrent
        # writers on the same store instance.
        self.last_write_inferred_edge_count = 0
        self._embedding_adapter = embedding_adapter

        from ncp.stores.rerank import Reranker
        from ncp.config import load_config
        try:
            cfg = config or load_config()
            self.config = cfg
            self.reranker = Reranker(cfg)
            self.retrieval_policy = retrieval_policy or RetrievalPolicy(
                generation_penalty_base=cfg.retrieval_generation_penalty_base
            )
            self.reputation_gain = cfg.reputation_gain
            self.reputation_forget = cfg.reputation_forget
            self.reputation_confidence_k = cfg.reputation_confidence_k
        except Exception:
            self.config = None
            self.retrieval_policy = retrieval_policy or DEFAULT_RETRIEVAL_POLICY
            self.reputation_gain = 4.0
            self.reputation_forget = 0.99
            self.reputation_confidence_k = 20

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
            connection = sqlite3.connect(self.path, timeout=5.0)
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA journal_mode=WAL;")
            connection.execute("PRAGMA synchronous=NORMAL;")
            connection.execute("PRAGMA foreign_keys=ON;")
            connection.execute("PRAGMA busy_timeout=5000;")
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
                "ALTER TABLE chunks ADD COLUMN dissent_count INTEGER DEFAULT 0",
                "ALTER TABLE whispers ADD COLUMN dissent_target TEXT",  # WI-007(b)
                "ALTER TABLE cost_log ADD COLUMN cost_source TEXT NOT NULL DEFAULT 'measured'",  # CAP-E1
                "ALTER TABLE chunks ADD COLUMN verified INTEGER DEFAULT 0",  # CAP-T1/WI-013
                "ALTER TABLE whispers ADD COLUMN verified INTEGER DEFAULT 0",  # CAP-T1/WI-013
                "ALTER TABLE chunks ADD COLUMN embedding BLOB",  # CAP-C4
                "ALTER TABLE memo_entries ADD COLUMN output_tokens_est INTEGER NOT NULL DEFAULT 0",  # S4.1 memo telemetry
                "ALTER TABLE chunks ADD COLUMN valid_from REAL",  # CAP-C5
                "ALTER TABLE chunks ADD COLUMN valid_to REAL",  # CAP-C5
                "ALTER TABLE chunks ADD COLUMN superseded_by TEXT",  # CAP-C5
                "CREATE TABLE IF NOT EXISTS drift_history (session_id TEXT NOT NULL, turn INTEGER NOT NULL, drift_score REAL NOT NULL, ts REAL NOT NULL)",
                "CREATE INDEX IF NOT EXISTS idx_drift_session ON drift_history(session_id, turn)",
                "CREATE TABLE IF NOT EXISTS identities (identity_id TEXT PRIMARY KEY, public_key TEXT NOT NULL, alg TEXT NOT NULL DEFAULT 'ed25519', label TEXT, created_at REAL NOT NULL, revoked_at REAL)",
                "CREATE TABLE IF NOT EXISTS reputation (identity_id TEXT PRIMARY KEY, alpha REAL NOT NULL DEFAULT 1.0, beta REAL NOT NULL DEFAULT 1.0, obs_count INTEGER NOT NULL DEFAULT 0, last_updated REAL NOT NULL DEFAULT 0.0)",
                "CREATE INDEX IF NOT EXISTS idx_reputation_updated ON reputation(last_updated)",
                "INSERT INTO chunks_fts(chunks_fts) VALUES ('rebuild')",
            ):
                try:
                    connection.execute(ddl)
                except Exception:
                    pass  # column already exists

    def write(self, chunk: SubconsciousChunk) -> bool:
        self.last_write_inferred_edge_count = 0
        chunk = self._validate_chunk_for_write(chunk)
        if self._embedding_adapter is not None and chunk.embedding is None:
            chunk = chunk.model_copy(
                update={"embedding": self._embedding_adapter.embed(chunk.content)}
            )
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._soft_gc(connection)
            self._assert_src_immutable(connection, chunk)
            if self._is_duplicate(connection, chunk):
                return False
            connection.execute(
                """
                INSERT INTO chunks (
                    chunk_id, pipeline_id, scope, zone, layer, chunk_type, content, src,
                    written_by, caused_by, conscious_hash, evidence_id, version, supersedes,
                    source_refs, schema_version, created_at, base_trust, generation,
                    result_confidence, result_attempts, conditions, valid_while, expiry, owner, meta,
                    written_at_drift, verified, embedding, valid_from, valid_to, superseded_by
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(chunk_id) DO UPDATE SET
                    pipeline_id = excluded.pipeline_id,
                    scope = excluded.scope,
                    zone = excluded.zone,
                    layer = excluded.layer,
                    chunk_type = excluded.chunk_type,
                    content = excluded.content,
                    src = excluded.src,
                    written_by = excluded.written_by,
                    caused_by = excluded.caused_by,
                    conscious_hash = excluded.conscious_hash,
                    evidence_id = excluded.evidence_id,
                    version = excluded.version,
                    supersedes = excluded.supersedes,
                    source_refs = excluded.source_refs,
                    schema_version = excluded.schema_version,
                    base_trust = excluded.base_trust,
                    generation = excluded.generation,
                    result_confidence = excluded.result_confidence,
                    result_attempts = excluded.result_attempts,
                    conditions = excluded.conditions,
                    valid_while = excluded.valid_while,
                    expiry = excluded.expiry,
                    owner = excluded.owner,
                    meta = excluded.meta,
                    written_at_drift = excluded.written_at_drift,
                    verified = excluded.verified,
                    embedding = excluded.embedding,
                    valid_from = excluded.valid_from
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
                    json.dumps({"raw_ref": chunk.raw_ref} if chunk.raw_ref else {}),
                    chunk.written_at_drift,
                    1 if chunk.verified else 0,
                    self._encode_embedding(chunk.embedding),
                    chunk.valid_from,
                    chunk.valid_to,
                    chunk.superseded_by,
                ),
            )
            backfilled_edges = backfill_edges_for_chunk(chunk)
            if backfilled_edges:
                self._upsert_chunk_edges(connection, backfilled_edges)
            if self.config is not None and self.config.infer_edges:
                inferred_edges = infer_edges_for_chunk(
                    chunk,
                    self._edge_inference_candidates(
                        connection, chunk, limit=self.config.infer_scan_limit
                    ),
                    threshold=self.config.infer_similarity_threshold,
                    max_edges=self.config.infer_max_edges,
                    existing_dst_ids=self._existing_refines_dst_ids(connection, chunk.chunk_id),
                )
                if inferred_edges:
                    self._upsert_chunk_edges(connection, inferred_edges)
                self.last_write_inferred_edge_count = len(inferred_edges)
            self._hard_gc(connection, pipeline_id=chunk.pipeline_id)
            if self.max_working_chunks_per_pipeline > 0 and chunk.zone == "working":
                self._enforce_retention(connection, pipeline_id=chunk.pipeline_id)
            return True

    def _edge_inference_candidates(
        self, connection: sqlite3.Connection, chunk: SubconsciousChunk, *, limit: int
    ) -> list[SubconsciousChunk]:
        """Bounded, most-recent-first scan of same-pipeline chunks (CAP-C7/WI-P2).

        Exclusion (self, raw backups, empty content, existing edges) is owned
        by ``infer_edges_for_chunk`` -- this only bounds the scan so inference
        never issues an unbounded table read.
        """
        rows = connection.execute(
            """
            SELECT * FROM chunks
            WHERE IFNULL(pipeline_id, '') = IFNULL(?, '')
            ORDER BY created_at DESC
            LIMIT ?
            """,
            (chunk.pipeline_id, max(0, int(limit))),
        ).fetchall()
        return [self._row_to_chunk(row) for row in rows]

    @staticmethod
    def _existing_refines_dst_ids(connection: sqlite3.Connection, src_chunk_id: str) -> set[str]:
        rows = connection.execute(
            "SELECT dst_chunk_id FROM chunk_edges WHERE src_chunk_id = ? AND edge_type = 'refines'",
            (src_chunk_id,),
        ).fetchall()
        return {str(row["dst_chunk_id"]) for row in rows}

    @staticmethod
    def _cosine_similarity(a: list[float], b: list[float]) -> float:
        if len(a) != len(b):
            raise ValueError(
                f"dimension mismatch: query has {len(a)} dims, stored has {len(b)} dims"
            )
        dot = sum(av * bv for av, bv in zip(a, b, strict=True))
        norm_a = sum(av * av for av in a) ** 0.5
        norm_b = sum(bv * bv for bv in b) ** 0.5
        if norm_a == 0.0 or norm_b == 0.0:
            return 0.0
        return dot / (norm_a * norm_b)

    @staticmethod
    def _encode_embedding(value: list[float] | None) -> bytes | None:
        if value is None:
            return None
        return json.dumps([float(item) for item in value]).encode("utf-8")

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
        as_of: float | None = None,
    ) -> list[SubconsciousChunk]:
        _VALID_RETRIEVAL_MODES = ("hybrid", "trust_recency", "vector")
        if retrieval_mode not in _VALID_RETRIEVAL_MODES:
            raise ValueError(
                f"Unknown retrieval_mode {retrieval_mode!r}; expected one of {_VALID_RETRIEVAL_MODES}"
            )
        if (
            embedding is None
            and self._embedding_adapter is not None
            and retrieval_mode in {"hybrid", "vector"}
        ):
            embedding = self._embedding_adapter.embed(text)
        if retrieval_mode == "vector" and embedding is None:
            raise ValueError("retrieval_mode='vector' requires an embedding or embedding adapter")

        with self._connect() as connection:
            rows = self._load_query_rows(
                connection,
                layer=layer,
                pipeline_id=pipeline_id,
                scope=scope,
                zone=zone,
                as_of=as_of,
            )
        if not rows:
            return []

        policy = self.retrieval_policy
        now = time.time()

        # CAP-T4: reputation-weighted retrieval blending
        blended_trust: dict[str, float] | None = None
        if self.config is not None and self.config.reputation_weight > 0.0:
            authors = {str(r["written_by"]) for r in rows}
            with self._connect() as connection:
                rep_data = self._load_reputation(connection, authors)
            rw = self.config.reputation_weight
            blended_trust = {
                str(row["chunk_id"]): blend_trust(
                    float(row["base_trust"]),
                    rep_data.get(str(row["written_by"])),
                    rw,
                )
                for row in rows
            }

        def _bt(row: object) -> float:
            if blended_trust is not None:
                return blended_trust.get(str(row["chunk_id"]), float(row["base_trust"]))
            return float(row["base_trust"])

        if retrieval_mode == "vector":
            return self._query_vector(
                rows, embedding=embedding, k=k, min_score=min_score,
                policy=policy, now=now, diversity_limit=diversity_limit,
                blended_trust=blended_trust,
            )

        candidates: list[SubconsciousChunk] = []

        if retrieval_mode == "trust_recency":
            for row in rows:
                age_seconds = max(0.0, now - float(row["created_at"]))
                score = policy.score_no_bm25(
                    age_seconds=age_seconds,
                    base_trust=_bt(row),
                    generation=int(row["generation"]),
                    written_at_drift=float(row["written_at_drift"]) if row["written_at_drift"] is not None else 0.0,
                )
                if score < min_score:
                    continue
                chunk = self._row_to_chunk(row)
                chunk.relevance = max(0.0, min(1.0, score))
                candidates.append(chunk)
        elif embedding is not None:
            lexical_candidates = build_lexical_candidates(
                text,
                [str(row["content"]) for row in rows],
            )
            for row, lexical_candidate in zip(rows, lexical_candidates, strict=True):
                row_embedding = self._decode_embedding(
                    row["embedding"] if "embedding" in row.keys() else None
                )
                vector_normalized: float | None = None
                if row_embedding is not None:
                    sim = self._cosine_similarity(embedding, row_embedding)
                    vector_normalized = max(0.0, min(1.0, sim))
                if lexical_candidate.lexical_signal is None and vector_normalized is None:
                    continue
                age_seconds = max(0.0, now - float(row["created_at"]))
                hybrid_score = policy.score_with_vector(
                    bm25_normalized=lexical_candidate.lexical_signal or 0.0,
                    vector_normalized=vector_normalized,
                    age_seconds=age_seconds,
                    base_trust=_bt(row),
                    generation=int(row["generation"]),
                    written_at_drift=float(row["written_at_drift"]) if row["written_at_drift"] is not None else 0.0,
                )
                if hybrid_score < min_score:
                    continue
                chunk = self._row_to_chunk(row)
                chunk.relevance = max(0.0, min(1.0, hybrid_score))
                candidates.append(chunk)
        else:
            for row, lexical_signal in self._fts_lexical_candidates(
                connection_rows=rows,
                text=text,
                layer=layer,
                pipeline_id=pipeline_id,
                scope=scope,
                zone=zone,
                as_of=as_of,
            ):
                age_seconds = max(0.0, now - float(row["created_at"]))
                hybrid_score = policy.score(
                    bm25_normalized=lexical_signal,
                    age_seconds=age_seconds,
                    base_trust=_bt(row),
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

        # Two-pass fallback: when explicitly requested and the primary hybrid pass
        # returns nothing, fall back to trust/recency-only scoring so callers get
        # the highest-trust context rather than a silent empty result.
        # Off by default to preserve the hybrid filtering contract.
        if fallback_to_trust_recency and retrieval_mode == "hybrid" and len(candidates) == 0:
            already_included = {c.chunk_id for c in candidates}
            fallback_candidates: list[SubconsciousChunk] = []
            for row in rows:
                if str(row["chunk_id"]) in already_included:
                    continue
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
                chunk.relevance = max(0.0, min(1.0, score * 0.5))
                fallback_candidates.append(chunk)
            fallback_candidates.sort(key=lambda c: c.relevance, reverse=True)
            ranked = ranked + fallback_candidates

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

    @staticmethod
    def _decode_embedding(value: object) -> list[float] | None:
        if value is None:
            return None
        if isinstance(value, list):
            return value
        if isinstance(value, memoryview):
            value = value.tobytes()
        if isinstance(value, bytes):
            value = value.decode("utf-8")
        try:
            parsed = json.loads(str(value))
            if isinstance(parsed, list) and all(isinstance(v, (int, float)) for v in parsed):
                return [float(v) for v in parsed]
        except (json.JSONDecodeError, TypeError, ValueError):
            pass
        return None

    def _query_vector(
        self,
        rows: list[sqlite3.Row],
        *,
        embedding: list[float] | None,
        k: int,
        min_score: float,
        policy: RetrievalPolicy,
        now: float,
        diversity_limit: int,
        blended_trust: dict[str, float] | None = None,
    ) -> list[SubconsciousChunk]:
        if embedding is None:
            raise ValueError("retrieval_mode='vector' requires an embedding or embedding adapter")
        candidates: list[SubconsciousChunk] = []
        for row in rows:
            row_embedding = self._decode_embedding(
                row["embedding"] if "embedding" in row.keys() else None
            )
            if row_embedding is None:
                continue
            sim = self._cosine_similarity(embedding, row_embedding)
            vector_normalized = max(0.0, min(1.0, sim))
            cid = str(row["chunk_id"])
            bt = blended_trust.get(cid, float(row["base_trust"])) if blended_trust else float(row["base_trust"])
            age_seconds = max(0.0, now - float(row["created_at"]))
            score = policy.score_with_vector(
                bm25_normalized=0.0,
                vector_normalized=vector_normalized,
                age_seconds=age_seconds,
                base_trust=bt,
                generation=int(row["generation"]),
                vector_mix=1.0,
                written_at_drift=float(row["written_at_drift"]) if row["written_at_drift"] is not None else 0.0,
            )
            if score < min_score:
                continue
            chunk = self._row_to_chunk(row)
            chunk.relevance = max(0.0, min(1.0, score))
            candidates.append(chunk)

        ranked = sorted(candidates, key=lambda c: c.relevance, reverse=True)
        result_limit = normalize_result_limit(k)
        results = apply_diversity_limit(
            ranked,
            k=result_limit,
            diversity_limit=diversity_limit,
            author_getter=lambda chunk: str(chunk.written_by),
        )
        if results:
            now_val = time.time()
            placeholders = ",".join("?" * len(results))
            with self._connect() as connection:
                connection.execute(
                    f"UPDATE chunks SET retrieval_count = retrieval_count + 1,"
                    f" last_retrieved_at = ? WHERE chunk_id IN ({placeholders})",
                    [now_val] + [c.chunk_id for c in results],
                )
            for chunk in results:
                chunk.retrieval_count += 1
                chunk.last_retrieved_at = now_val
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
        as_of: float | None = None,
    ) -> Sequence[SubconsciousChunk]:
        with self._connect() as connection:
            rows = self._load_query_rows(
                connection,
                layer=layer,
                pipeline_id=pipeline_id,
                scope=None,
                zone="working",
                as_of=as_of,
            )
        return [self._row_to_chunk(row) for row in rows]

    def supersede(
        self,
        old_chunk_id: str,
        new_chunk_id: str,
        *,
        valid_to: float | None = None,
    ) -> bool:
        """CAP-C5: mark ``old_chunk_id`` as honestly replaced by ``new_chunk_id``.

        The old row is never deleted -- only ``superseded_by`` and
        ``valid_to`` are updated in place, so a later ``as_of`` query can
        still recover it.
        """
        resolved_valid_to = time.time() if valid_to is None else valid_to
        with self._connect() as connection:
            cursor = connection.execute(
                "UPDATE chunks SET superseded_by = ?, valid_to = ? WHERE chunk_id = ?",
                (new_chunk_id, resolved_valid_to, old_chunk_id),
            )
            return cursor.rowcount > 0

    def record_dissent(self, chunk_id: str) -> bool:
        normalized = chunk_id.removeprefix("ctx://sub/")
        with self._connect() as connection:
            cursor = connection.execute(
                "UPDATE chunks SET dissent_count = dissent_count + 1 WHERE chunk_id = ?",
                (normalized,),
            )
            return cursor.rowcount > 0

    def record_outcome(self, outcome: OutcomeRecord) -> bool:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            chunk_ids = outcome.chunk_ids
            # Resolve turn_id to chunk_ids if turn_id is provided and chunk_ids is empty
            if outcome.turn_id and not chunk_ids:
                rows = connection.execute(
                    "SELECT chunk_id FROM chunks WHERE caused_by = ? OR conscious_hash = ?",
                    (outcome.turn_id, outcome.turn_id),
                ).fetchall()
                chunk_ids = [str(r["chunk_id"]) for r in rows]
            connection.execute(
                """
                INSERT INTO outcomes (outcome_id, turn_id, chunk_ids, success, weight, note, created_at, consumed)
                VALUES (?, ?, ?, ?, ?, ?, ?, 0)
                """,
                (
                    outcome.outcome_id,
                    outcome.turn_id,
                    json.dumps(chunk_ids),
                    1 if outcome.success else 0,
                    outcome.weight,
                    outcome.note,
                    outcome.created_at,
                ),
            )
            return True

    def record_memo(
        self,
        signature: str,
        task: str,
        chunk_ids: list[str],
        result_summary: str | None = None,
        output_tokens_est: int | None = None,
    ) -> bool:
        if output_tokens_est is None:
            # No real token count available: estimate over the stored result.
            output_tokens_est = estimate_tokens(result_summary) if result_summary else 0
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                INSERT INTO memo_entries
                    (signature, task, result_summary, chunk_ids, outcome, verified, created_at,
                     last_hit_at, hit_count, output_tokens_est)
                VALUES (?, ?, ?, ?, 0.0, 0, ?, 0.0, 0, ?)
                ON CONFLICT(signature) DO UPDATE SET
                    task = excluded.task,
                    result_summary = excluded.result_summary,
                    chunk_ids = excluded.chunk_ids,
                    output_tokens_est = excluded.output_tokens_est
                """,
                (signature, task, result_summary, json.dumps(chunk_ids), time.time(), max(0, int(output_tokens_est))),
            )
            return True

    def lookup_memo(self, signature: str) -> dict | None:
        """Look up a memo, unconditionally counting a hit if found and fresh.

        See ``BaseStore.lookup_memo`` for why callers that apply further
        usability gating (e.g. ``ncp_lookup_memo``'s outcome/verified checks)
        should prefer ``peek_memo`` + ``record_memo_hit``/``record_memo_miss``.
        """
        memo = self.peek_memo(signature)
        if memo is None:
            self._bump_memo_miss()
            return None
        self.record_memo_hit(signature)
        return memo

    def peek_memo(self, signature: str) -> dict | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM memo_entries WHERE signature = ?",
                (signature,),
            ).fetchone()
        if row is None:
            return None
        # Check staleness
        max_age = self.config.memoization_max_age_hours if self.config is not None else 24
        age_seconds = time.time() - float(row["created_at"])
        if age_seconds > max_age * 3600:
            return None
        return dict(row)

    def record_memo_hit(self, signature: str) -> None:
        with self._connect() as connection:
            connection.execute(
                "UPDATE memo_entries SET hit_count = hit_count + 1, last_hit_at = ? WHERE signature = ?",
                (time.time(), signature),
            )

    def record_memo_miss(self) -> None:
        self._bump_memo_miss()

    def _bump_memo_miss(self) -> None:
        """Increment the S4.1 memoization miss counter."""
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO memo_stats (stat_key, stat_value) VALUES ('misses', 1)"
                " ON CONFLICT(stat_key) DO UPDATE SET stat_value = stat_value + 1"
            )

    def memo_stats(self) -> dict[str, int]:
        """Aggregate memoization telemetry: hits, misses, entries, tokens saved.

        ``estimated_tokens_saved`` is an estimate: SUM(hit_count * output_tokens_est).
        """
        with self._connect() as connection:
            row = connection.execute(
                "SELECT COUNT(*) AS entry_count,"
                " COALESCE(SUM(hit_count), 0) AS hits,"
                " COALESCE(SUM(hit_count * output_tokens_est), 0) AS tokens_saved"
                " FROM memo_entries"
            ).fetchone()
            miss_row = connection.execute(
                "SELECT stat_value FROM memo_stats WHERE stat_key = 'misses'"
            ).fetchone()
        return {
            "hits": int(row["hits"]),
            "misses": int(miss_row["stat_value"]) if miss_row is not None else 0,
            "entry_count": int(row["entry_count"]),
            "estimated_tokens_saved": int(row["tokens_saved"]),
        }

    def update_memo_outcome(self, signature: str, outcome: float, verified: bool = False) -> bool:
        with self._connect() as connection:
            cursor = connection.execute(
                "UPDATE memo_entries SET outcome = ?, verified = ? WHERE signature = ?",
                (outcome, 1 if verified else 0, signature),
            )
            return cursor.rowcount > 0

    def _load_unconsumed_outcomes(self, connection: sqlite3.Connection) -> list[OutcomeRecord]:
        rows = connection.execute(
            "SELECT * FROM outcomes WHERE consumed = 0 ORDER BY created_at ASC"
        ).fetchall()
        results: list[OutcomeRecord] = []
        for row in rows:
            results.append(OutcomeRecord(
                outcome_id=str(row["outcome_id"]),
                turn_id=row["turn_id"],
                chunk_ids=json.loads(str(row["chunk_ids"])),
                success=bool(row["success"]),
                weight=float(row["weight"]),
                note=row["note"],
                created_at=float(row["created_at"]),
                consumed=bool(row["consumed"]),
            ))
        return results

    def _mark_outcomes_consumed(
        self, connection: sqlite3.Connection, outcome_ids: list[str]
    ) -> None:
        if not outcome_ids:
            return
        placeholders = ",".join("?" for _ in outcome_ids)
        connection.execute(
            f"UPDATE outcomes SET consumed = 1 WHERE outcome_id IN ({placeholders})",
            outcome_ids,
        )

    def get_chunks_by_ids(
        self, ids: Sequence[str], *, as_of: float | None = None
    ) -> list[SubconsciousChunk]:
        unique_ids = [cid for cid in dict.fromkeys(ids) if cid]
        if not unique_ids:
            return []
        placeholders = ",".join("?" * len(unique_ids))
        bitemporal_sql, bitemporal_params = self._bitemporal_clause(as_of)
        with self._connect() as connection:
            rows = connection.execute(
                f"SELECT * FROM chunks WHERE chunk_id IN ({placeholders})"
                " AND chunk_id NOT IN (SELECT chunk_id FROM tombstones)"
                " AND (expiry IS NULL OR expiry > ?)"  # WI-006
                f" AND ({bitemporal_sql})",
                [*unique_ids, time.time(), *bitemporal_params],
            ).fetchall()
        return [self._row_to_chunk(row) for row in rows]

    @staticmethod
    def _upsert_chunk_edges(connection: sqlite3.Connection, edges: Sequence[ChunkEdge]) -> int:
        for edge in edges:
            connection.execute(
                """
                INSERT INTO chunk_edges (
                    edge_id, src_chunk_id, dst_chunk_id, edge_type, weight, created_at, created_by
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(src_chunk_id, dst_chunk_id, edge_type) DO UPDATE SET
                    weight = excluded.weight,
                    created_at = excluded.created_at,
                    created_by = excluded.created_by
                """,
                (
                    edge.edge_id,
                    edge.src_chunk_id,
                    edge.dst_chunk_id,
                    edge.edge_type,
                    edge.weight,
                    edge.created_at,
                    edge.created_by,
                ),
            )
        return len(edges)

    def add_chunk_edges(self, edges: Sequence[ChunkEdge]) -> int:
        if not edges:
            return 0
        with self._connect() as connection:
            return self._upsert_chunk_edges(connection, edges)

    def get_chunk_edges(
        self,
        chunk_ids: Sequence[str],
        *,
        edge_types: Sequence[str] | None = None,
        direction: str = "out",
        limit: int = 200,
    ) -> list[ChunkEdge]:
        if direction not in ("out", "in", "both"):
            raise ValueError(f"Unknown direction {direction!r}; expected 'out', 'in', or 'both'")
        unique_ids = [cid for cid in dict.fromkeys(chunk_ids) if cid]
        if not unique_ids:
            return []
        placeholders = ",".join("?" * len(unique_ids))
        params: list[object] = []
        if direction == "out":
            where = f"src_chunk_id IN ({placeholders})"
            params.extend(unique_ids)
        elif direction == "in":
            where = f"dst_chunk_id IN ({placeholders})"
            params.extend(unique_ids)
        else:
            where = f"(src_chunk_id IN ({placeholders}) OR dst_chunk_id IN ({placeholders}))"
            params.extend(unique_ids)
            params.extend(unique_ids)
        if edge_types:
            type_placeholders = ",".join("?" * len(edge_types))
            where += f" AND edge_type IN ({type_placeholders})"
            params.extend(edge_types)
        capped_limit = max(0, int(limit))
        with self._connect() as connection:
            rows = connection.execute(
                f"SELECT * FROM chunk_edges WHERE {where} ORDER BY created_at DESC LIMIT ?",
                [*params, capped_limit],
            ).fetchall()
        return [self._row_to_chunk_edge(row) for row in rows]

    @staticmethod
    def _row_to_chunk_edge(row: sqlite3.Row) -> ChunkEdge:
        return ChunkEdge(
            edge_id=str(row["edge_id"]),
            src_chunk_id=str(row["src_chunk_id"]),
            dst_chunk_id=str(row["dst_chunk_id"]),
            edge_type=str(row["edge_type"]),
            weight=float(row["weight"]),
            created_at=float(row["created_at"]),
            created_by=row["created_by"],
        )

    def graph_data(
        self,
        *,
        pipeline_id: str | None = None,
        limit: int = 500,
        as_of: float | None = None,
    ) -> dict[str, object]:
        """See ``BaseStore.graph_data`` for the node/edge shape.

        ``as_of`` (CAP-C5, epoch seconds): ``None`` (default) is exactly the
        pre-WI-P4 behavior -- no bi-temporal filtering, matching the other
        ``as_of=None`` query paths' "no history awareness" baseline that
        ``graph_data`` has always had. When given, nodes are filtered with
        the same point-in-time visibility rule as ``get_chunks_by_ids``/
        ``query`` (see ``_bitemporal_clause``), and edges recorded after
        ``as_of`` are excluded.
        """
        capped_limit = max(0, int(limit))
        with self._connect() as connection:
            clauses = ["chunk_id NOT IN (SELECT chunk_id FROM tombstones)"]
            params: list[object] = []
            if pipeline_id is not None:
                clauses.append("pipeline_id = ?")
                params.append(pipeline_id)
            if as_of is not None:
                bitemporal_sql, bitemporal_params = self._bitemporal_clause(as_of)
                clauses.append(f"({bitemporal_sql})")
                params.extend(bitemporal_params)
            chunk_rows = connection.execute(
                "SELECT chunk_id, layer, base_trust, src, written_by, content, caused_by, supersedes"
                f" FROM chunks WHERE {' AND '.join(clauses)}"
                " ORDER BY created_at DESC LIMIT ?",
                [*params, capped_limit],
            ).fetchall()

            node_ids = [str(r["chunk_id"]) for r in chunk_rows]
            edge_rows: list[sqlite3.Row] = []
            if node_ids:
                placeholders = ",".join("?" * len(node_ids))
                edge_where = f"(src_chunk_id IN ({placeholders}) OR dst_chunk_id IN ({placeholders}))"
                edge_params: list[object] = [*node_ids, *node_ids]
                if as_of is not None:
                    edge_where += " AND created_at <= ?"
                    edge_params.append(as_of)
                edge_rows = connection.execute(
                    "SELECT src_chunk_id, dst_chunk_id, edge_type, weight FROM chunk_edges"
                    f" WHERE {edge_where}",
                    edge_params,
                ).fetchall()

        nodes = [
            {
                "chunk_id": str(r["chunk_id"]),
                "layer": str(r["layer"]),
                "base_trust": float(r["base_trust"]),
                "src": str(r["src"]),
                "written_by": str(r["written_by"]),
                "summary": str(r["content"])[:80],
            }
            for r in chunk_rows
        ]

        edges: list[dict[str, object]] = []
        seen: set[tuple[str, str, str]] = set()
        for row in edge_rows:
            key = (str(row["src_chunk_id"]), str(row["dst_chunk_id"]), str(row["edge_type"]))
            seen.add(key)
            edges.append({"src": key[0], "dst": key[1], "type": key[2], "weight": float(row["weight"])})

        # Legacy-column fallback: chunks whose caused_by/supersedes predate the
        # edge table get a synthesized edge, unless a real row already covers it.
        for r in chunk_rows:
            cid = str(r["chunk_id"])
            cause = r["caused_by"]
            if cause:
                key = (cid, str(cause), "caused_by")
                if key not in seen:
                    seen.add(key)
                    edges.append({"src": key[0], "dst": key[1], "type": key[2], "weight": 1.0})
            for target in parse_supersedes_ids(r["supersedes"]):
                key = (cid, target, "supersedes")
                if key not in seen:
                    seen.add(key)
                    edges.append({"src": key[0], "dst": key[1], "type": key[2], "weight": 1.0})

        return {"nodes": nodes, "edges": edges}

    def emit_whisper(self, whisper: Whisper) -> None:
        whisper = Whisper.model_validate(whisper.model_dump())
        with self._connect() as connection:
            self._soft_gc(connection)
            connection.execute(
                """
                INSERT OR REPLACE INTO whispers (
                    whisper_id, pipeline_id, from_agent, target, whisper_type,
                    payload, confidence, ref, dissent_target, verified, created_at, expires_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                    whisper.dissent_target,
                    1 if whisper.verified else 0,
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

            # CAP-T4: whisper gating by author reputation
            if drained and self.config is not None and self.config.whisper_min_author_reputation > 0.0:
                threshold = self.config.whisper_min_author_reputation
                authors = {w.from_agent for w in drained}
                rep_data = self._load_reputation(connection, authors)
                filtered: list[Whisper] = []
                for w in drained:
                    result = rep_data.get(w.from_agent)
                    if result is not None:
                        alpha, beta = result
                        if alpha / (alpha + beta) >= threshold:
                            filtered.append(w)
                    else:
                        # Unknown author — uniform prior Beta(1,1) = 0.5
                        if 0.5 >= threshold:
                            filtered.append(w)
                drained = filtered

            if drained:
                self._acknowledge_whispers(connection, drained, agent_id=agent_id)
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

    def acknowledge_whispers(self, whisper_ids: Sequence[str], *, agent_id: str | None = None) -> int:
        """Delete already-processed whispers by id."""

        if not whisper_ids:
            return 0
        with self._connect() as connection:
            rows = connection.execute(
                f"SELECT * FROM whispers WHERE whisper_id IN ({','.join('?' for _ in whisper_ids)})",
                tuple(whisper_ids),
            ).fetchall()
            return self._acknowledge_whispers(
                connection,
                [self._row_to_whisper(row) for row in rows],
                agent_id=agent_id,
            )

    def whisper_pending(self, whisper_id: str) -> bool:
        """Return True if the whisper is still queued and not yet expired."""
        now = time.time()
        with self._connect() as connection:
            row = connection.execute(
                "SELECT 1 FROM whispers WHERE whisper_id = ? AND expires_at > ?",
                (whisper_id, now),
            ).fetchone()
        return row is not None

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

    def recent_turns(self, *, pipeline_id: str | None, limit: int = 20) -> list[TurnRecord]:
        """CAP-T5: most recent turn records for a pipeline, oldest-first."""
        capped_limit = max(0, int(limit))
        if capped_limit == 0:
            return []
        with self._connect() as connection:
            if pipeline_id is None:
                rows = connection.execute(
                    "SELECT * FROM turn_records WHERE pipeline_id IS NULL ORDER BY created_at DESC LIMIT ?",
                    (capped_limit,),
                ).fetchall()
            else:
                rows = connection.execute(
                    "SELECT * FROM turn_records WHERE pipeline_id = ? ORDER BY created_at DESC LIMIT ?",
                    (pipeline_id, capped_limit),
                ).fetchall()
        records = [TurnRecord(**dict(row)) for row in rows]
        records.reverse()
        return records

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

    def load_latest_conscious(self, *, pipeline_id: str | None, agent_id: str) -> ConsciousBlock | None:
        with self._connect() as connection:
            if pipeline_id is None:
                row = connection.execute(
                    """
                    SELECT snapshot_json FROM conscious_log
                    WHERE agent_id = ? AND pipeline_id IS NULL
                    ORDER BY logged_at DESC
                    LIMIT 1
                    """,
                    (agent_id,),
                ).fetchone()
            else:
                row = connection.execute(
                    """
                    SELECT snapshot_json FROM conscious_log
                    WHERE agent_id = ? AND pipeline_id = ?
                    ORDER BY logged_at DESC
                    LIMIT 1
                    """,
                    (agent_id, pipeline_id),
                ).fetchone()
        if row is None:
            return None
        try:
            return ConsciousBlock.model_validate_json(str(row["snapshot_json"]))
        except ValueError:
            return None

    def log_cost(self, *, agent_id: str, response: NCPResponse) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                INSERT OR REPLACE INTO cost_log (
                    turn_id, pipeline_id, agent_id, model, input_tokens, output_tokens,
                    cache_read_tokens, cost_usd, latency_ms, logged_at, cost_source
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                    getattr(response, "cost_source", "measured"),
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
        cost_source: str = "estimated",
    ) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                INSERT OR REPLACE INTO cost_log (
                    turn_id, pipeline_id, agent_id, model, input_tokens, output_tokens,
                    cache_read_tokens, cost_usd, latency_ms, logged_at, cost_source
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                    cost_source,
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
            if not dry_run:
                connection.execute("BEGIN IMMEDIATE")
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
        propagation_factor: float = 0.5,
        dissent_weight: float = 0.2,
        propagation_max_hops: int = 1,
    ) -> CalibrationReport:
        """Re-score base_trust on live chunks.

        Manual mode: chunk_id + trust sets a specific chunk's base_trust directly.
        Batch mode: pipeline_id applies decay to eligible chunks.
        Feedback mode: applies a net trust delta per chunk (retrieval boost minus
        dissent penalty) and propagates a fraction (``propagation_factor``) of it
        up to ``propagation_max_hops`` hops along ``caused_by`` ancestry (default
        1 hop, matching legacy behavior).
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
                "SELECT chunk_id, base_trust, src, written_by, generation, created_at,"
                " retrieval_count, caused_by, dissent_count FROM chunks"
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
                feedback_rows: list[FeedbackRow] = []
                chunk_author: dict[str, str] = {}
                consumed_feedback_ids: list[str] = []
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
                        written_by = str(row["written_by"])
                        chunk_author[cid] = self.resolve_identity(written_by, pipeline_id=pipeline_id)
                        feedback_rows.append(
                            FeedbackRow(
                                chunk_id=cid,
                                base_trust=base_trust,
                                retrieval_count=rc,
                                caused_by=row["caused_by"],
                                dissent_count=int(row["dissent_count"]) if row["dissent_count"] is not None else 0,
                            )
                        )

                # CAP-T3: initialize outcome tracking
                consumed_outcome_ids: list[str] = []
                if feedback_mode and feedback_rows:
                    # WI-G3: chunks without a caused_by scalar (e.g. edges added
                    # via the MCP edges arg only) still get an ancestor via a
                    # caused_by edge row, so multi-hop propagation can walk them.
                    missing_parent_ids = [row.chunk_id for row in feedback_rows if not row.caused_by]
                    if missing_parent_ids and propagation_factor > 0.0 and propagation_max_hops > 0:
                        fallback_edges = self.get_chunk_edges(
                            missing_parent_ids, edge_types=["caused_by"], direction="out"
                        )
                        fallback_parent = resolve_caused_by_fallback(fallback_edges)
                        if fallback_parent:
                            feedback_rows = [
                                dataclass_replace(row, caused_by=fallback_parent[row.chunk_id])
                                if row.chunk_id in fallback_parent
                                else row
                                for row in feedback_rows
                            ]

                    outcomes = self._load_unconsumed_outcomes(connection)
                    outcome_evidence = None
                    if outcomes:
                        from ncp.stores.calibration import compute_outcome_evidence
                        outcome_evidence = compute_outcome_evidence(outcomes)
                        consumed_outcome_ids = [o.outcome_id for o in outcomes]

                    fb = compute_feedback_updates(
                        feedback_rows,
                        feedback_weight=feedback_weight,
                        propagation_factor=propagation_factor,
                        dissent_weight=dissent_weight,
                        usage_prior_weight=self.config.usage_prior_weight if self.config is not None else 1.0,
                        outcome_evidence=outcome_evidence,
                        propagation_max_hops=propagation_max_hops,
                    )
                    updates.extend(fb.updates)
                    report.change_log.extend(fb.change_log)
                    report.feedback_adjusted += fb.adjusted
                    report.outcome_propagated += fb.outcome_propagated
                    report.skipped += fb.skipped
                    consumed_feedback_ids = fb.consumed_chunk_ids

                    prior = self._load_reputation(
                        connection,
                        {chunk_author[cid] for cid in chunk_author},
                    )
                    rep_updates = rollup_reputation(
                        fb.change_log,
                        chunk_author,
                        prior,
                        gain=self.reputation_gain,
                        forget=self.reputation_forget,
                        K_CONF=self.reputation_confidence_k,
                        outcome_evidence=outcome_evidence,
                    )
                    report.change_log.extend(
                        {
                            "identity_id": update.identity_id,
                            "reason": "reputation_rollup",
                            "alpha": update.new_alpha,
                            "beta": update.new_beta,
                            "obs_delta": update.obs_delta,
                        }
                        for update in rep_updates
                    )
                else:
                    rep_updates = ()

                if not dry_run and updates:
                    for new_trust, cid in updates:
                        connection.execute(
                            "UPDATE chunks SET base_trust = ? WHERE chunk_id = ?",
                            (new_trust, cid),
                        )
                if feedback_mode and not dry_run:
                    self._upsert_reputation_updates(connection, rep_updates, now=now)
                    if consumed_feedback_ids:
                        placeholders = ",".join("?" for _ in consumed_feedback_ids)
                        connection.execute(
                            f"UPDATE chunks SET retrieval_count = 0, dissent_count = 0 "
                            f"WHERE chunk_id IN ({placeholders})",
                            consumed_feedback_ids,
                        )
                    if consumed_outcome_ids:
                        self._mark_outcomes_consumed(connection, consumed_outcome_ids)

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

    def resolve_identity(
        self,
        agent_id: str,
        *,
        pipeline_id: str | None = None,
        signature: str | None = None,
        content: str | None = None,
    ) -> str:
        """Resolve the authenticated author of a write.

        When a ``signature`` and ``content`` are supplied and the signature
        verifies against the stored (non-revoked) public key for ``agent_id``,
        the verified identity is returned. Otherwise the claimed ``agent_id`` is
        returned unchanged (the unsigned/legacy path), preserving prior behavior.
        """

        if signature is not None and content is not None:
            from ncp.identity import canonical_authorship_payload

            payload = canonical_authorship_payload(agent_id, content, pipeline_id)
            if self.verify_authorship(agent_id, payload, signature):
                return agent_id
        return agent_id

    def verify_authorship(
        self, identity_id: str, payload: bytes | str, signature: str
    ) -> bool:
        """Verify a signature over ``payload`` for ``identity_id``.

        Returns ``False`` for unknown or revoked identities and for any
        signature that does not verify against the stored public key.
        """

        from ncp.identity import verify_signature

        with self._connect() as connection:
            row = connection.execute(
                "SELECT public_key, revoked_at FROM identities WHERE identity_id = ?",
                (identity_id,),
            ).fetchone()
        if row is None:
            return False
        if row["revoked_at"] is not None:
            return False
        return verify_signature(str(row["public_key"]), payload, signature)

    def register_identity(
        self,
        *,
        identity_id: str,
        public_key: str,
        label: str | None,
        alg: str = "ed25519",
    ) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO identities (identity_id, public_key, alg, label, created_at, revoked_at)
                VALUES (?, ?, ?, ?, ?, NULL)
                ON CONFLICT(identity_id) DO UPDATE SET
                    public_key = excluded.public_key,
                    alg = excluded.alg,
                    label = excluded.label
                """,
                (identity_id, public_key, alg, label, time.time()),
            )

    def list_identities(self) -> list[dict[str, object]]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT identity_id, public_key, alg, label, created_at, revoked_at
                FROM identities
                ORDER BY created_at DESC
                """
            ).fetchall()
        return [dict(row) for row in rows]

    def revoke_identity(self, identity_id: str) -> bool:
        with self._connect() as connection:
            cursor = connection.execute(
                "UPDATE identities SET revoked_at = ? WHERE identity_id = ? AND revoked_at IS NULL",
                (time.time(), identity_id),
            )
            return cursor.rowcount > 0

    def list_reputation(self, *, top: int = 20) -> list[dict[str, object]]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT r.identity_id, i.label, r.alpha, r.beta, r.obs_count, r.last_updated
                FROM reputation r
                LEFT JOIN identities i ON i.identity_id = r.identity_id
                ORDER BY (r.alpha / (r.alpha + r.beta)) DESC, r.obs_count DESC
                LIMIT ?
                """,
                (top,),
            ).fetchall()

        result: list[dict[str, object]] = []
        for row in rows:
            alpha = float(row["alpha"])
            beta = float(row["beta"])
            obs_count = int(row["obs_count"])
            result.append(
                {
                    "identity_id": str(row["identity_id"]),
                    "label": row["label"],
                    "alpha": alpha,
                    "beta": beta,
                    "score": alpha / (alpha + beta),
                    "confidence": obs_count / (obs_count + self.reputation_confidence_k),
                    "obs_count": obs_count,
                    "last_updated": float(row["last_updated"]),
                }
            )
        return result

    def _load_reputation(
        self,
        connection: sqlite3.Connection,
        identity_ids: set[str],
    ) -> dict[str, tuple[float, float]]:
        if not identity_ids:
            return {}
        placeholders = ",".join("?" for _ in identity_ids)
        rows = connection.execute(
            f"SELECT identity_id, alpha, beta FROM reputation WHERE identity_id IN ({placeholders})",
            tuple(sorted(identity_ids)),
        ).fetchall()
        return {
            str(row["identity_id"]): (float(row["alpha"]), float(row["beta"]))
            for row in rows
        }

    def _upsert_reputation_updates(
        self,
        connection: sqlite3.Connection,
        updates: tuple[ReputationUpdate, ...],
        *,
        now: float,
    ) -> None:
        for update in updates:
            connection.execute(
                """
                INSERT INTO reputation(identity_id, alpha, beta, obs_count, last_updated)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(identity_id) DO UPDATE SET
                    alpha = excluded.alpha,
                    beta = excluded.beta,
                    obs_count = reputation.obs_count + excluded.obs_count,
                    last_updated = excluded.last_updated
                """,
                (
                    update.identity_id,
                    update.new_alpha,
                    update.new_beta,
                    update.obs_delta,
                    now,
                ),
            )

    def status(self) -> dict[str, int | float]:
        now = time.time()
        with self._connect() as connection:
            chunks = connection.execute("SELECT COUNT(*) AS count FROM chunks").fetchone()["count"]
            whispers = connection.execute("SELECT COUNT(*) AS count FROM whispers").fetchone()["count"]
            turns = connection.execute("SELECT COUNT(*) AS count FROM turn_records").fetchone()["count"]
            costs = connection.execute("SELECT COALESCE(SUM(cost_usd), 0.0) AS total FROM cost_log").fetchone()["total"]
            below_conf = connection.execute(
                "SELECT COUNT(*) AS count FROM whispers"
                " WHERE expires_at > ? AND whisper_type NOT IN ('alert', 'world_check') AND confidence < 0.60",
                (now,),
            ).fetchone()["count"]
        return {
            "chunk_count": int(chunks),
            "whisper_count": int(whispers),
            "whispers_below_min_confidence": int(below_conf),
            "turn_record_count": int(turns),
            "cost_usd_total": float(costs),
            "retention_evictions": int(self.retention_evictions),
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
                "whispers_below_min_confidence": int(
                    connection.execute(
                        "SELECT COUNT(*) AS count FROM whispers"
                        " WHERE expires_at > ? AND whisper_type NOT IN ('alert', 'world_check')"
                        " AND confidence < 0.60"
                        + (" AND pipeline_id = ?" if pipeline_id is not None else ""),
                        (time.time(), pipeline_id) if pipeline_id is not None else (time.time(),),
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

    def query_precedents(
        self,
        query: str,
        *,
        pipeline_id: str | None = None,
        k: int = 5,
        tags: list[str] | None = None,
        outcome: str | None = None,
    ) -> list[dict[str, object]]:
        chunks = self.query(
            text=query,
            k=k * 3,
            min_score=0.0,
            layer="reasoning_trace",
            pipeline_id=pipeline_id,
            fallback_to_trust_recency=True,
        )
        results: list[dict[str, object]] = []
        for chunk in chunks:
            content = chunk.content
            parsed = self._parse_decision_content(content)
            if outcome is not None and parsed.get("outcome") != outcome:
                continue
            if tags:
                chunk_tags = parsed.get("tags", [])
                if not any(t in chunk_tags for t in tags):
                    continue
            results.append({
                "chunk_id": chunk.chunk_id,
                "decision": parsed.get("decision", ""),
                "rationale": parsed.get("rationale", ""),
                "alternatives": parsed.get("alternatives", []),
                "outcome": parsed.get("outcome", "unknown"),
                "evidence_refs": chunk.source_refs,
                "tags": parsed.get("tags", []),
                "base_trust": chunk.base_trust,
                "relevance": chunk.relevance,
                "created_at": chunk.age_seconds,
                "caused_by": chunk.caused_by,
                "retrieval_count": chunk.retrieval_count,
                "dissent_count": chunk.dissent_count,
            })
            if len(results) >= k:
                break
        return results

    @staticmethod
    def _parse_decision_content(content: str) -> dict[str, object]:
        result: dict[str, object] = {}
        for line in content.split("\n"):
            line = line.strip()
            if line.startswith("decision: "):
                result["decision"] = line[len("decision: "):]
            elif line.startswith("rationale: "):
                result["rationale"] = line[len("rationale: "):]
            elif line.startswith("alternatives: "):
                result["alternatives"] = [a.strip() for a in line[len("alternatives: "):].split("|")]
            elif line.startswith("outcome: "):
                result["outcome"] = line[len("outcome: "):]
            elif line.startswith("evidence: "):
                result["evidence_refs"] = line[len("evidence: "):].split()
            elif line.startswith("tags: "):
                result["tags"] = line[len("tags: "):].split()
        return result

    def trust_drift_data(
        self,
        *,
        pipeline_id: str | None = None,
        top_k: int = 10,
    ) -> dict[str, object]:
        now = time.time()
        live_clause = "chunk_id NOT IN (SELECT chunk_id FROM tombstones)"
        pipe_filter = " AND pipeline_id = ?" if pipeline_id is not None else ""
        params: list[object] = [pipeline_id] if pipeline_id is not None else []

        with self._connect() as connection:
            band_rows = connection.execute(
                f"""
                SELECT
                    CASE
                        WHEN base_trust < 0.3 THEN 'low (0-0.3)'
                        WHEN base_trust < 0.6 THEN 'mid (0.3-0.6)'
                        WHEN base_trust < 0.8 THEN 'good (0.6-0.8)'
                        ELSE 'high (0.8-1.0)'
                    END AS band,
                    COUNT(*) AS count,
                    AVG(base_trust) AS avg_trust
                FROM chunks
                WHERE {live_clause}{pipe_filter}
                GROUP BY band
                ORDER BY band
                """,
                params,
            ).fetchall()
            trust_distribution = [
                {
                    "band": str(r["band"]),
                    "count": int(r["count"]),
                    "avg_trust": round(float(r["avg_trust"]), 4),
                }
                for r in band_rows
            ]

            rising_rows = connection.execute(
                f"""
                SELECT chunk_id, layer, pipeline_id, base_trust, retrieval_count,
                       dissent_count, created_at
                FROM chunks
                WHERE {live_clause}{pipe_filter} AND retrieval_count > 0
                ORDER BY retrieval_count DESC, base_trust DESC
                LIMIT ?
                """,
                [*params, top_k],
            ).fetchall()
            rising = [
                {
                    "chunk_id": str(r["chunk_id"])[:16],
                    "layer": str(r["layer"]),
                    "pipeline_id": r["pipeline_id"],
                    "base_trust": float(r["base_trust"]),
                    "retrieval_count": int(r["retrieval_count"]),
                    "dissent_count": int(r["dissent_count"]) if r["dissent_count"] else 0,
                    "age_seconds": round(now - float(r["created_at"]), 1),
                }
                for r in rising_rows
            ]

            falling_rows = connection.execute(
                f"""
                SELECT chunk_id, layer, pipeline_id, base_trust, retrieval_count,
                       dissent_count, created_at
                FROM chunks
                WHERE {live_clause}{pipe_filter} AND dissent_count > 0
                ORDER BY dissent_count DESC, base_trust ASC
                LIMIT ?
                """,
                [*params, top_k],
            ).fetchall()
            falling = [
                {
                    "chunk_id": str(r["chunk_id"])[:16],
                    "layer": str(r["layer"]),
                    "pipeline_id": r["pipeline_id"],
                    "base_trust": float(r["base_trust"]),
                    "retrieval_count": int(r["retrieval_count"]) if r["retrieval_count"] else 0,
                    "dissent_count": int(r["dissent_count"]),
                    "age_seconds": round(now - float(r["created_at"]), 1),
                }
                for r in falling_rows
            ]

            summary_row = connection.execute(
                f"""
                SELECT
                    COUNT(*) AS total_chunks,
                    SUM(CASE WHEN retrieval_count > 0 THEN 1 ELSE 0 END) AS with_retrievals,
                    SUM(CASE WHEN dissent_count > 0 THEN 1 ELSE 0 END) AS with_dissents,
                    SUM(CASE WHEN retrieval_count > dissent_count AND retrieval_count > 0 THEN 1 ELSE 0 END) AS net_positive,
                    SUM(CASE WHEN dissent_count > retrieval_count AND dissent_count > 0 THEN 1 ELSE 0 END) AS net_negative,
                    SUM(CASE WHEN retrieval_count = 0 AND dissent_count = 0 THEN 1 ELSE 0 END) AS untouched
                FROM chunks
                WHERE {live_clause}{pipe_filter}
                """,
                params,
            ).fetchone()
            feedback_summary = {
                "total_chunks": int(summary_row["total_chunks"] or 0),
                "with_retrievals": int(summary_row["with_retrievals"] or 0),
                "with_dissents": int(summary_row["with_dissents"] or 0),
                "net_positive": int(summary_row["net_positive"] or 0),
                "net_negative": int(summary_row["net_negative"] or 0),
                "untouched": int(summary_row["untouched"] or 0),
            }

            drift_rows = connection.execute(
                "SELECT session_id, turn, drift_score, ts FROM drift_history"
                " ORDER BY ts DESC LIMIT 20"
            ).fetchall()
            drift_timeline = [
                {
                    "session_id": str(r["session_id"]),
                    "turn": int(r["turn"]),
                    "drift_score": float(r["drift_score"]),
                    "ts": float(r["ts"]),
                }
                for r in drift_rows
            ]

        return {
            "trust_distribution": trust_distribution,
            "rising": rising,
            "falling": falling,
            "feedback_summary": feedback_summary,
            "drift_timeline": drift_timeline,
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

    @staticmethod
    def _bitemporal_clause(as_of: float | None) -> tuple[str, list[object]]:
        """CAP-C5: build a SQL fragment + params enforcing bi-temporal visibility.

        Default view (``as_of=None``): the currently-valid view -- excludes
        chunks that are superseded (period) or whose ``valid_to`` has
        already passed "now".

        ``as_of`` view (epoch seconds): chunks recorded (``created_at``) by
        that transaction time, not superseded by a chunk itself recorded by
        that time, and valid (``valid_from``/``valid_to``) at that instant.
        Supersession always happens transactionally together with the
        superseding chunk's write, so the superseding chunk's own
        ``created_at`` is the transaction time the supersession took effect.
        """
        if as_of is None:
            return (
                "chunks.superseded_by IS NULL AND (chunks.valid_to IS NULL OR chunks.valid_to > ?)",
                [time.time()],
            )
        return (
            "chunks.created_at <= ?"
            " AND (chunks.superseded_by IS NULL OR NOT EXISTS ("
            "     SELECT 1 FROM chunks AS s"
            "     WHERE s.chunk_id = chunks.superseded_by AND s.created_at <= ?"
            " ))"
            " AND (chunks.valid_from IS NULL OR chunks.valid_from <= ?)"
            " AND (chunks.valid_to IS NULL OR chunks.valid_to > ?)",
            [as_of, as_of, as_of, as_of],
        )

    def _load_query_rows(
        self,
        connection: sqlite3.Connection,
        *,
        layer: str | None,
        pipeline_id: str | None,
        scope: str | None,
        zone: str,
        as_of: float | None = None,
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
        # WI-006: never surface chunks whose expiry has passed.
        clauses.append("(expiry IS NULL OR expiry > ?)")
        params.append(time.time())
        bitemporal_sql, bitemporal_params = self._bitemporal_clause(as_of)
        clauses.append(f"({bitemporal_sql})")
        params.extend(bitemporal_params)
        return connection.execute(
            f"SELECT * FROM chunks WHERE {' AND '.join(clauses)} ORDER BY created_at DESC",
            params,
        ).fetchall()

    def _fts_lexical_candidates(
        self,
        *,
        connection_rows: list[sqlite3.Row],
        text: str,
        layer: str | None,
        pipeline_id: str | None,
        scope: str | None,
        zone: str,
        as_of: float | None = None,
    ) -> list[tuple[sqlite3.Row, float]]:
        terms = sorted(normalize_query_terms(text))
        if not terms:
            return [(row, 1.0) for row in connection_rows]

        quoted_terms = []
        for term in terms:
            escaped = term.replace('"', '""')
            quoted_terms.append(f'"{escaped}"')
        match_query = " OR ".join(quoted_terms)
        clauses = ["chunks.zone = ?", "chunks_fts MATCH ?"]
        params: list[object] = [zone, match_query]
        if layer is not None:
            clauses.append("chunks.layer = ?")
            params.append(layer)
        if pipeline_id is None:
            clauses.append("(chunks.pipeline_id IS NULL OR chunks.scope = 'global')")
        else:
            clauses.append("(chunks.pipeline_id = ? OR chunks.scope = 'global')")
            params.append(pipeline_id)
        if scope is not None:
            clauses.append("chunks.scope = ?")
            params.append(scope)
        # WI-006: exclude expired chunks from lexical retrieval.
        clauses.append("(chunks.expiry IS NULL OR chunks.expiry > ?)")
        params.append(time.time())
        # CAP-C5: bi-temporal visibility (see _bitemporal_clause).
        bitemporal_sql, bitemporal_params = self._bitemporal_clause(as_of)
        clauses.append(f"({bitemporal_sql})")
        params.extend(bitemporal_params)

        try:
            with self._connect() as connection:
                fts_rows = connection.execute(
                    f"""
                    SELECT chunks.*, bm25(chunks_fts) AS fts_rank
                    FROM chunks_fts
                    JOIN chunks ON chunks_fts.rowid = chunks.rowid
                    WHERE {' AND '.join(clauses)}
                    ORDER BY fts_rank ASC
                    """,
                    params,
                ).fetchall()
        except NCPStoreUnavailableError:
            lexical_candidates = build_lexical_candidates(
                text,
                [str(row["content"]) for row in connection_rows],
            )
            return [
                (row, float(candidate.lexical_signal))
                for row, candidate in zip(connection_rows, lexical_candidates, strict=True)
                if candidate.lexical_signal is not None
            ]

        if not fts_rows:
            return []
        raw_scores = [max(0.0, -float(row["fts_rank"])) for row in fts_rows]
        max_score = max(raw_scores)
        if max_score <= 0.0:
            normalized = [0.0] * len(raw_scores)
        else:
            normalized = [score / max_score for score in raw_scores]
        return [(row, score) for row, score in zip(fts_rows, normalized, strict=True)]

    def _row_to_chunk(self, row: sqlite3.Row) -> SubconsciousChunk:
        created_at = float(row["created_at"])
        meta = json.loads(row["meta"]) if row["meta"] else {}
        embedding = self._decode_embedding(row["embedding"] if "embedding" in row.keys() else None)
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
            raw_ref=meta.get("raw_ref"),
            age_seconds=max(0.0, time.time() - created_at),
            retrieval_count=int(row["retrieval_count"]) if row["retrieval_count"] is not None else 0,
            last_retrieved_at=float(row["last_retrieved_at"]) if row["last_retrieved_at"] is not None else None,
            dissent_count=int(row["dissent_count"]) if row["dissent_count"] is not None else 0,
            verified=bool(row["verified"]) if "verified" in row.keys() and row["verified"] is not None else False,
            embedding=embedding,
            valid_from=float(row["valid_from"]) if "valid_from" in row.keys() and row["valid_from"] is not None else None,
            valid_to=float(row["valid_to"]) if "valid_to" in row.keys() and row["valid_to"] is not None else None,
            superseded_by=row["superseded_by"] if "superseded_by" in row.keys() else None,
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
            dissent_target=row["dissent_target"] if "dissent_target" in row.keys() else None,
            created_at=float(row["created_at"]),
            ttl_seconds=ttl_seconds,
            pipeline_id=row["pipeline_id"],
            verified=bool(row["verified"]) if "verified" in row.keys() and row["verified"] is not None else False,
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
        clauses = [
            "expires_at > ?",
            "target IN (?, '*')",
            "NOT (target = '*' AND EXISTS (SELECT 1 FROM whisper_deliveries d WHERE d.whisper_id = whispers.whisper_id AND d.agent_id = ?))",
        ]
        params: list[object] = [time.time(), agent_id, agent_id]
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

    def _acknowledge_whispers(
        self,
        connection: sqlite3.Connection,
        whispers: Sequence[Whisper],
        *,
        agent_id: str | None,
    ) -> int:
        acknowledged = 0
        now = time.time()
        for whisper in whispers:
            if whisper.target == "*" and agent_id is not None:
                cursor = connection.execute(
                    """
                    INSERT OR IGNORE INTO whisper_deliveries (whisper_id, agent_id, delivered_at)
                    VALUES (?, ?, ?)
                    """,
                    (whisper.whisper_id, agent_id, now),
                )
                acknowledged += int(cursor.rowcount > 0)
                continue
            cursor = connection.execute(
                "DELETE FROM whispers WHERE whisper_id = ?",
                (whisper.whisper_id,),
            )
            acknowledged += int(cursor.rowcount > 0)
            connection.execute("DELETE FROM whisper_deliveries WHERE whisper_id = ?", (whisper.whisper_id,))
        return acknowledged

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
        # WI-006: reclaim chunks whose expiry has passed.
        connection.execute("DELETE FROM chunks WHERE expiry IS NOT NULL AND expiry <= ?", (now,))

    def _hard_gc(self, connection: sqlite3.Connection, *, pipeline_id: str | None) -> None:
        # WI-006: drop expired chunks before evaluating overflow capacity.
        connection.execute(
            "DELETE FROM chunks WHERE expiry IS NOT NULL AND expiry <= ?", (time.time(),)
        )
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

    def _enforce_retention(self, connection: sqlite3.Connection, *, pipeline_id: str | None) -> None:
        cap = self.max_working_chunks_per_pipeline
        if pipeline_id is None:
            clause = "pipeline_id IS NULL AND zone = 'working'"
            params: list[object] = []
        else:
            clause = "pipeline_id = ? AND zone = 'working'"
            params = [pipeline_id]
        count = connection.execute(
            f"SELECT COUNT(*) AS count FROM chunks WHERE {clause}",
            params,
        ).fetchone()["count"]
        if int(count) <= cap:
            return
        rows = connection.execute(
            f"SELECT chunk_id, created_at, base_trust, generation, written_at_drift"
            f" FROM chunks WHERE {clause}",
            params,
        ).fetchall()
        now = time.time()
        scored = sorted(
            rows,
            key=lambda row: score_trust_recency_candidate(
                self.retrieval_policy,
                created_at=float(row["created_at"]),
                now=now,
                base_trust=float(row["base_trust"]),
                generation=int(row["generation"]),
                written_at_drift=float(row["written_at_drift"]) if row["written_at_drift"] is not None else 0.0,
            ),
        )
        overflow = int(count) - cap
        evict_ids = [str(row["chunk_id"]) for row in scored[:overflow]]
        if evict_ids:
            connection.executemany(
                "DELETE FROM chunks WHERE chunk_id = ?",
                [(chunk_id,) for chunk_id in evict_ids],
            )
            self.retention_evictions += len(evict_ids)
