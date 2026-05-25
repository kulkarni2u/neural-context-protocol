"""pgvector-backed durable store scaffolding for NCP 0.2.0."""

from __future__ import annotations

from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from ncp.stores.base import BaseStore, NCPStoreUnavailableError
from ncp.types import NCPResponse, SubconsciousChunk, TurnRecord, Whisper


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
    """Schema-initializing durable-store skeleton for the pgvector rollout."""

    def __init__(
        self,
        dsn: str,
        *,
        schema: str = "ncp",
        table_prefix: str = "ncp_",
        connect_factory: Callable[[str], Any] | None = None,
    ) -> None:
        self.dsn = dsn
        self.schema = _validate_identifier(schema, field="schema")
        self.table_prefix = _validate_identifier(table_prefix, field="table_prefix")
        self._connect_factory = connect_factory or _default_pgvector_connect
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
                close = getattr(cursor, "close", None)
                if callable(close):
                    close()

    def write(self, chunk: SubconsciousChunk) -> bool:
        raise NotImplementedError(
            "PgvectorStore schema initialization is live, but chunk persistence is not implemented yet."
        )

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
        raise NotImplementedError(
            "PgvectorStore retrieval is not implemented yet. The 0.2.0 kickoff only establishes schema creation."
        )

    def emit_whisper(self, whisper: Whisper) -> None:
        raise NotImplementedError(
            "Redis-backed whisper coordination is still pending; pgvector does not own this path yet."
        )

    def drain_whispers(
        self,
        *,
        agent_id: str,
        pipeline_id: str | None = None,
        max_items: int = 3,
        min_confidence: float = 0.60,
    ) -> list[Whisper]:
        raise NotImplementedError(
            "Redis-backed whisper coordination is still pending; pgvector does not own this path yet."
        )

    def get_working_zone(
        self,
        *,
        pipeline_id: str | None = None,
        layer: str | None = None,
    ) -> Sequence[SubconsciousChunk]:
        raise NotImplementedError("PgvectorStore working-zone reads are not implemented yet.")

    def log_turn_record(self, record: TurnRecord) -> None:
        raise NotImplementedError("PgvectorStore turn-record persistence is not implemented yet.")

    def resolve_recent_ref(self, ref: str) -> TurnRecord | None:
        raise NotImplementedError("PgvectorStore recent-ref resolution is not implemented yet.")

    def log_cost(self, *, agent_id: str, response: NCPResponse) -> None:
        raise NotImplementedError("PgvectorStore cost logging is not implemented yet.")

    def log_conscious(self, conscious: object, *, snapshot_hash: str) -> None:
        raise NotImplementedError("PgvectorStore conscious snapshot logging is not implemented yet.")

    def get_pipeline_goal_versions(
        self,
        *,
        pipeline_id: str,
        current_agent: str | None = None,
    ) -> dict[str, int]:
        raise NotImplementedError("PgvectorStore goal-version reads are not implemented yet.")


def infra_hint(project_root: str | Path) -> str:
    root = Path(project_root)
    return (
        f"Start local Postgres/pgvector with {root / 'scripts' / 'infra_up.sh'} and set "
        "NCP_PGVECTOR_DSN plus NCP_STORE_TYPE=pgvector when you want to exercise the schema bootstrap path."
    )
