"""PostgreSQL persistence for the opt-in domain ontology (design §10 PR 5).

Same public API and semantics as :class:`~ncp.ontology.stores.sqlite.SQLiteOntologyStore`
(the reference implementation; see its module docstring for the scope and
validity rules). Differences are confined to storage mechanics:

* **Placement.** The ``ontology_*`` tables live in the NCP ``PgvectorStore``'s
  schema with its ``table_prefix`` (``{schema}.{prefix}ontology_entities``,
  ...), so per-test or per-deployment schemas isolate the graph exactly like
  memory. They are created by an additive, versioned migration that runs only
  when this store is constructed; a plain ``PgvectorStore`` keeps its schema.
  Nothing here writes the pgvector tables; the ``chunks`` table is only read
  to check that cited evidence is still live.
* **Ordering and round-trips.** Key columns use ``COLLATE "C"`` (byte order,
  as in SQLite) and attributes are stored as ``JSON`` (verbatim canonical
  text), so ordered graph results and attribute payloads match SQLite exactly.
  PostgreSQL text cannot hold NUL: lookups by a NUL-containing key match
  nothing (as on SQLite), and writing one raises ``ValueError``.
* **Concurrency.** Reads run in one ``REPEATABLE READ, READ ONLY`` transaction
  (a consistent snapshot across the traversal's statements). Writes run in
  ``READ COMMITTED`` behind transaction-scoped advisory locks, so every check
  sees the latest committed state once the lock is held:

  - relation writes (assert / revoke / invalidate) take one lock on the
    ``(tenant, namespace)`` write scope -- the scope that bounds cardinality
    counting, cycle detection, idempotency keys and evidence invalidation --
    so those checks are race-free while different namespaces write in
    parallel (SQLite instead serializes every writer); ``assert_relation``
    also locks its ``relation_id`` (a global key) and holds ``FOR SHARE``
    locks on both endpoints;
  - entity upserts lock the ``entity_id`` and the row ``FOR UPDATE``.

  Locks are always taken in that order (scope, then relation id, then
  endpoint rows), so writers cannot deadlock. Lock waits are bounded by
  ``lock_timeout``.
* **Append-only events.** A trigger rejects ``UPDATE``, ``DELETE`` and
  ``TRUNCATE`` on ``ontology_events``.
* **Deadlines.** A read ``deadline`` sets ``SET LOCAL statement_timeout`` to
  the remaining budget inside the read transaction and is re-checked before
  every statement; either way the read raises
  :class:`~ncp.ontology.stores.sqlite.OntologyTimeoutError`.
* **Evidence liveness** is evaluated against ``{schema}.{prefix}chunks`` with
  the same predicate as SQLite (not superseded, ``valid_to``/``expiry`` in the
  future, ``valid_from`` reached, caller-readable scope). Without that table
  no chunk is live: chunk evidence fails closed.

``psycopg`` (3.x) is imported lazily, so SQLite-only installs never need it.
"""

from __future__ import annotations

import hashlib
import json
import struct
import time
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Protocol
from uuid import UUID

from ncp.ontology.models import (
    GLOBAL_NAMESPACE, AuthContext, Entity, EntityMatch, EvidenceRef, GraphQuery,
    GraphResult, Relation, normalize_key,
)
from ncp.ontology.registry import RelationType, SchemaRegistry
from ncp.ontology.stores._traversal import breadth_first
from ncp.ontology.stores.sqlite import (
    _SAME_SCHEMA, _SCHEMA_UPGRADE_CONFLICT, OntologyConflictError, OntologyNotFoundError,
    OntologyTimeoutError, _check_deadline, _require, _require_write, _ts,
)

if TYPE_CHECKING:
    from ncp.stores.pgvector import PgvectorStore
    from ncp.stores.pgvector_async import AsyncPgvectorStore

_ACTIVE = (
    "r.status != 'revoked' AND r.valid_from <= %(now)s"
    " AND (r.valid_until IS NULL OR r.valid_until > %(now)s)"
)
_IDENTIFIER_CHARS = frozenset("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_")
_SCHEMA_V1 = (
    """CREATE TABLE {s}.{p}ontology_entities (
        entity_id TEXT COLLATE "C" PRIMARY KEY,
        tenant_id TEXT COLLATE "C" NOT NULL,
        namespace TEXT COLLATE "C" NOT NULL,
        schema_pack TEXT COLLATE "C" NOT NULL,
        schema_version INTEGER NOT NULL,
        entity_type TEXT COLLATE "C" NOT NULL,
        canonical_key TEXT COLLATE "C" NOT NULL,
        display_name TEXT NOT NULL,
        attributes JSON NOT NULL,
        revision INTEGER NOT NULL,
        created_at TIMESTAMPTZ NOT NULL,
        updated_at TIMESTAMPTZ NOT NULL,
        deleted_at TIMESTAMPTZ,
        CONSTRAINT {p}ontology_entities_identity_key
            UNIQUE (tenant_id, namespace, schema_pack, entity_type, canonical_key)
    )""",
    """CREATE TABLE {s}.{p}ontology_aliases (
        tenant_id TEXT COLLATE "C" NOT NULL,
        namespace TEXT COLLATE "C" NOT NULL,
        entity_type TEXT COLLATE "C" NOT NULL,
        alias TEXT COLLATE "C" NOT NULL,
        entity_id TEXT COLLATE "C" NOT NULL
            REFERENCES {s}.{p}ontology_entities(entity_id) ON DELETE CASCADE,
        PRIMARY KEY (tenant_id, namespace, entity_type, alias, entity_id)
    )""",
    "CREATE INDEX {p}ontology_aliases_entity_idx ON {s}.{p}ontology_aliases(entity_id)",
    "CREATE INDEX {p}ontology_aliases_alias_idx ON {s}.{p}ontology_aliases(tenant_id, alias)",
    """CREATE TABLE {s}.{p}ontology_relations (
        relation_id TEXT COLLATE "C" PRIMARY KEY,
        tenant_id TEXT COLLATE "C" NOT NULL,
        namespace TEXT COLLATE "C" NOT NULL,
        src_entity_id TEXT COLLATE "C" NOT NULL REFERENCES {s}.{p}ontology_entities(entity_id),
        dst_entity_id TEXT COLLATE "C" NOT NULL REFERENCES {s}.{p}ontology_entities(entity_id),
        relation_type TEXT COLLATE "C" NOT NULL,
        status TEXT NOT NULL CHECK (status IN ('candidate', 'verified', 'disputed', 'revoked')),
        confidence DOUBLE PRECISION NOT NULL,
        valid_from TIMESTAMPTZ NOT NULL,
        valid_until TIMESTAMPTZ,
        observed_at TIMESTAMPTZ NOT NULL,
        created_by TEXT NOT NULL,
        asserted_by TEXT NOT NULL,
        revision INTEGER NOT NULL,
        schema_pack TEXT COLLATE "C" NOT NULL,
        schema_version INTEGER NOT NULL,
        attributes JSON NOT NULL,
        created_at TIMESTAMPTZ NOT NULL,
        updated_at TIMESTAMPTZ NOT NULL
    )""",
    "CREATE INDEX {p}ontology_relations_src_idx"
    " ON {s}.{p}ontology_relations(tenant_id, namespace, src_entity_id, relation_type)",
    "CREATE INDEX {p}ontology_relations_dst_idx"
    " ON {s}.{p}ontology_relations(tenant_id, namespace, dst_entity_id, relation_type)",
    """CREATE TABLE {s}.{p}ontology_relation_evidence (
        relation_id TEXT COLLATE "C" NOT NULL
            REFERENCES {s}.{p}ontology_relations(relation_id) ON DELETE CASCADE,
        evidence_type TEXT COLLATE "C" NOT NULL,
        evidence_id TEXT COLLATE "C" NOT NULL,
        source_revision TEXT COLLATE "C" NOT NULL,
        observed_at TIMESTAMPTZ NOT NULL,
        status TEXT NOT NULL DEFAULT 'valid' CHECK (status IN ('valid', 'invalidated')),
        invalidated_reason TEXT,
        invalidated_at TIMESTAMPTZ,
        PRIMARY KEY (relation_id, evidence_type, evidence_id, source_revision)
    )""",
    "CREATE INDEX {p}ontology_evidence_ref_idx"
    " ON {s}.{p}ontology_relation_evidence(evidence_type, evidence_id, status)",
    """CREATE TABLE {s}.{p}ontology_write_requests (
        tenant_id TEXT COLLATE "C" NOT NULL,
        namespace TEXT COLLATE "C" NOT NULL,
        idempotency_key TEXT COLLATE "C" NOT NULL,
        fingerprint TEXT NOT NULL,
        relation_id TEXT COLLATE "C" NOT NULL REFERENCES {s}.{p}ontology_relations(relation_id),
        principal_id TEXT NOT NULL,
        created_at TIMESTAMPTZ NOT NULL,
        PRIMARY KEY (tenant_id, namespace, idempotency_key)
    )""",
    """CREATE TABLE {s}.{p}ontology_events (
        event_id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
        tenant_id TEXT COLLATE "C" NOT NULL,
        namespace TEXT COLLATE "C" NOT NULL,
        object_kind TEXT NOT NULL,
        object_id TEXT COLLATE "C" NOT NULL,
        event_type TEXT NOT NULL,
        revision INTEGER NOT NULL,
        principal_id TEXT NOT NULL,
        reason TEXT,
        created_at TIMESTAMPTZ NOT NULL
    )""",
    """CREATE FUNCTION {s}.{p}ontology_events_append_only() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN RAISE EXCEPTION 'ontology_events is append-only'; END
        $$""",
    "CREATE TRIGGER {p}ontology_events_no_change BEFORE UPDATE OR DELETE"
    " ON {s}.{p}ontology_events FOR EACH ROW"
    " EXECUTE FUNCTION {s}.{p}ontology_events_append_only()",
    "CREATE TRIGGER {p}ontology_events_no_truncate BEFORE TRUNCATE"
    " ON {s}.{p}ontology_events FOR EACH STATEMENT"
    " EXECUTE FUNCTION {s}.{p}ontology_events_append_only()",
)
# Append-only: later versions add entries, never edit shipped ones.
_MIGRATIONS: tuple[tuple[int, tuple[str, ...]], ...] = ((1, _SCHEMA_V1),)


def _identifier(value: str, *, field: str) -> str:
    cleaned = value.strip()
    if not cleaned or any(char not in _IDENTIFIER_CHARS for char in cleaned):
        raise ValueError(f"{field} must contain only letters, digits, and underscores")
    return cleaned


def _has_nul(value: Any) -> bool:
    if isinstance(value, str):
        return "\x00" in value
    if isinstance(value, dict):
        return any(_has_nul(item) for item in value.values())
    if isinstance(value, (list, tuple)):
        return any(_has_nul(item) for item in value)
    return False


def _check_params(params: Any) -> None:
    # PostgreSQL text cannot hold NUL (SQLite can): reject it as invalid input
    # instead of letting psycopg.DataError escape the store's error contract.
    # Lookups drop NUL keys up front, since nothing stored can match them.
    if _has_nul(params):
        raise ValueError("ontology text values cannot contain NUL characters")


def _utc(value: datetime | None) -> datetime | None:
    return None if value is None else value.astimezone(timezone.utc)


def _attrs(value: Any) -> Any:
    # JSON columns hand back the stored canonical text already parsed; a
    # driver configured to return raw text is handled too.
    return json.loads(value) if isinstance(value, (str, bytes)) else value


class ConnectionSource(Protocol):
    """Anything with psycopg_pool's ``getconn``/``putconn`` shape."""

    def getconn(self) -> Any: ...

    def putconn(self, connection: Any) -> None: ...


class _DirectSource:
    """One fresh connection per operation (no pool available or injected factory)."""

    def __init__(self, connect: Callable[[], Any]) -> None:
        self._connect = connect

    def getconn(self) -> Any:
        return self._connect()

    def putconn(self, connection: Any) -> None:
        connection.close()


def _source_for(store: Any) -> ConnectionSource:
    """The connection source of an NCP ``PgvectorStore`` (its pool when it has one)."""
    pool = getattr(store, "_pool", None)
    if pool is not None:
        return pool
    return _DirectSource(store._connect_with_retry)


class _Tx:
    """Cursor helpers bound to one open transaction (dict rows, deadline checks)."""

    def __init__(self, connection: Any, deadline: float | None) -> None:
        from psycopg.rows import dict_row, tuple_row

        self.connection = connection
        self.deadline = deadline
        self._dict_row = dict_row
        self._tuple_row = tuple_row

    def rows(self, sql: str, params: Any = None) -> list[dict[str, Any]]:
        _check_deadline(self.deadline)
        _check_params(params)
        with self.connection.cursor(row_factory=self._dict_row) as cursor:
            cursor.execute(sql, params)
            return cursor.fetchall() if cursor.description is not None else []

    def row(self, sql: str, params: Any = None) -> dict[str, Any] | None:
        rows = self.rows(sql, params)
        return rows[0] if rows else None

    def tuples(self, sql: str, params: Any = None) -> list[tuple[Any, ...]]:
        _check_deadline(self.deadline)
        _check_params(params)
        with self.connection.cursor(row_factory=self._tuple_row) as cursor:
            cursor.execute(sql, params)
            return cursor.fetchall() if cursor.description is not None else []

    def execute(self, sql: str, params: Any = None) -> int:
        _check_deadline(self.deadline)
        _check_params(params)
        with self.connection.cursor() as cursor:
            cursor.execute(sql, params)
            return cursor.rowcount


class PostgresOntologyStore:
    """``OntologyStore`` over PostgreSQL; see the module docstring.

    ``dsn`` is used when no ``connection_source`` is given (a private
    ``psycopg_pool.ConnectionPool``, or one connection per operation without
    psycopg_pool). ``connection_source`` is any object with ``getconn()`` /
    ``putconn(conn)``, e.g. the NCP ``PgvectorStore``'s pool via
    :meth:`inherit`. Connections are borrowed per operation; each operation is
    one explicit transaction and the connection is returned idle.
    """

    def __init__(
        self,
        dsn: str,
        registry: SchemaRegistry,
        *,
        schema: str = "ncp",
        table_prefix: str = "ncp_",
        connection_source: ConnectionSource | None = None,
        lock_timeout: float = 5.0,
        max_pool_connections: int = 4,
    ) -> None:
        self.dsn = dsn
        self.registry = registry
        self.schema = _identifier(schema, field="schema")
        self.table_prefix = _identifier(table_prefix, field="table_prefix")
        self._lock_timeout_ms = max(1, int(lock_timeout * 1000))
        self._owned_pool: Any = None
        if connection_source is None:
            connection_source = self._private_source(dsn, max_pool_connections)
        self._source = connection_source
        try:
            self._migrate()
        except BaseException:
            self.close()  # a failed construction must not leak its private pool
            raise

    def _private_source(self, dsn: str, max_size: int) -> ConnectionSource:
        try:
            from psycopg_pool import ConnectionPool
        except ImportError:  # pragma: no cover - psycopg_pool not installed
            import psycopg

            return _DirectSource(lambda: psycopg.connect(dsn))
        self._owned_pool = ConnectionPool(conninfo=dsn, min_size=1, max_size=max_size, open=True)
        return self._owned_pool

    @classmethod
    def inherit(cls, store: PgvectorStore, registry: SchemaRegistry) -> PostgresOntologyStore:
        """``store = "inherit"``: the NCP pgvector store's database, schema,
        table prefix and connection pool."""
        return cls(
            store.dsn, registry, schema=store.schema, table_prefix=store.table_prefix,
            connection_source=_source_for(store),
        )

    def close(self) -> None:
        """Close the private pool, if this store opened one (an inherited pool
        belongs to the NCP store)."""
        if self._owned_pool is not None:
            self._owned_pool.close()
            self._owned_pool = None

    # -- naming --------------------------------------------------------------

    def _t(self, name: str) -> str:
        return f"{self.schema}.{self.table_prefix}ontology_{name}"

    @property
    def chunks_table(self) -> str:
        return f"{self.schema}.{self.table_prefix}chunks"

    def _lock_key(self, *parts: str) -> int:
        # Unquoted identifiers fold to lower case in PostgreSQL ("NCP.x" is
        # ncp.x), so the lock identity folds too: one graph, one key.
        identity = json.dumps([self.schema.lower(), self.table_prefix.lower(), "ontology", *parts])
        return struct.unpack(">q", hashlib.sha256(identity.encode()).digest()[:8])[0]

    def _lock(self, tx: _Tx, *parts: str) -> None:
        tx.execute("SELECT pg_advisory_xact_lock(%s)", (self._lock_key(*parts),))

    def _lock_scope(self, tx: _Tx, tenant: str, namespace: str) -> None:
        self._lock(tx, "scope", tenant, namespace)

    # -- connection and transaction plumbing ---------------------------------

    @contextmanager
    def _connection(self) -> Iterator[Any]:
        connection = self._source.getconn()
        try:
            if connection.info.transaction_status != 0:  # not IDLE
                raise RuntimeError("ontology store connection has an open transaction")
            yield connection
        finally:
            try:
                if connection.info.transaction_status != 0 and not connection.closed:
                    connection.rollback()
            finally:
                self._source.putconn(connection)

    @contextmanager
    def _transaction(self, *, write: bool, deadline: float | None = None) -> Iterator[_Tx]:
        """One explicit transaction. ``deadline`` (``time.monotonic()``, reads
        only) bounds every statement and raises :class:`OntologyTimeoutError`."""
        from psycopg import errors

        if deadline is not None:
            if write:
                raise ValueError("deadlines apply to read transactions only")
            _check_deadline(deadline)
        with self._connection() as connection:
            try:
                with connection.transaction():
                    tx = _Tx(connection, None)
                    if write:
                        tx.execute("SET TRANSACTION ISOLATION LEVEL READ COMMITTED")
                        tx.execute(f"SET LOCAL lock_timeout = {self._lock_timeout_ms}")
                    else:
                        tx.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
                    if deadline is not None:
                        remaining = max(1, int((deadline - time.monotonic()) * 1000))
                        tx.execute(f"SET LOCAL statement_timeout = {remaining}")
                        tx.deadline = deadline
                    yield tx
            except errors.QueryCanceled:
                if deadline is not None:
                    raise OntologyTimeoutError("ontology read exceeded its deadline") from None
                raise

    def _migrate(self) -> None:
        with self._transaction(write=True) as tx:
            # The lock serializes concurrent first constructions (CREATE ... IF
            # NOT EXISTS alone races in the catalog).
            self._lock(tx, "migrate")
            tx.execute(f"CREATE SCHEMA IF NOT EXISTS {self.schema}")
            tx.execute(
                f"CREATE TABLE IF NOT EXISTS {self._t('schema_versions')}"
                " (version INTEGER PRIMARY KEY, applied_at TIMESTAMPTZ NOT NULL)"
            )
            current = tx.tuples(
                f"SELECT COALESCE(MAX(version), 0) FROM {self._t('schema_versions')}"
            )[0][0]
            for version, statements in _MIGRATIONS:
                if version <= current:
                    continue
                for statement in statements:
                    tx.execute(statement.format(s=self.schema, p=self.table_prefix))
                tx.execute(
                    f"INSERT INTO {self._t('schema_versions')} (version, applied_at)"
                    " VALUES (%s, %s)",
                    (version, datetime.now(timezone.utc)),
                )

    @staticmethod
    def initialized(store: PgvectorStore) -> bool:
        """Read-only probe: whether ``store``'s schema already holds the ontology tables.

        Creates nothing, so automatic enrichment can skip an uninitialized
        store without migrating it. Any database error reads as ``False``.
        """
        try:
            source = _source_for(store)
            connection = source.getconn()
        except Exception:
            return False
        try:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT to_regclass(%s) IS NOT NULL",
                    (f"{store.schema}.{store.table_prefix}ontology_schema_versions",),
                )
                row = cursor.fetchone()
            connection.rollback()
            return bool(row and row[0])
        except Exception:
            try:
                connection.rollback()
            except Exception:
                pass
            return False
        finally:
            source.putconn(connection)

    def _event(
        self, tx: _Tx, scope: Any, *, kind: str, object_id: str, event_type: str, revision: int,
        auth: AuthContext, now: datetime, reason: str | None = None,
    ) -> None:
        tx.execute(
            f"INSERT INTO {self._t('events')} (tenant_id, namespace, object_kind, object_id,"
            " event_type, revision, principal_id, reason, created_at)"
            " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)",
            (scope.tenant_id, scope.namespace, kind, object_id, event_type, revision,
             auth.principal_id, reason, now),
        )

    # -- evidence liveness ---------------------------------------------------

    def _live_chunk_sql(self, tx: _Tx, namespace: str) -> str | None:
        """Liveness predicate over alias ``c``; ``None`` without a chunks table.

        ``namespace`` is an SQL expression (a column or a placeholder).
        """
        columns = {name for (name,) in tx.tuples(
            "SELECT attname FROM pg_attribute WHERE attrelid = to_regclass(%s)"
            " AND attnum > 0 AND NOT attisdropped",
            (self.chunks_table,),
        )}
        if not columns:
            return None
        clauses = [f"(COALESCE(c.scope, 'pipeline') = 'global' OR c.pipeline_id = {namespace})"]
        if "superseded_by" in columns:
            clauses.append("c.superseded_by IS NULL")
        if "valid_to" in columns:
            clauses.append("(c.valid_to IS NULL OR c.valid_to > %(now_epoch)s)")
        if "expiry" in columns:
            clauses.append("(c.expiry IS NULL OR c.expiry > %(now_epoch)s)")
        if "valid_from" in columns:
            clauses.append("(c.valid_from IS NULL OR c.valid_from <= %(now_epoch)s)")
        return " AND ".join(clauses)

    def _valid_evidence_sql(self, tx: _Tx) -> str:
        """``EXISTS`` clause over alias ``r``: one valid row whose chunk is still live."""
        chunk_ok = "e.evidence_type != 'chunk'"
        live = self._live_chunk_sql(tx, "r.namespace")
        if live is not None:
            chunk_ok = (
                f"e.evidence_type != 'chunk' OR EXISTS (SELECT 1 FROM {self.chunks_table} c"
                f" WHERE c.chunk_id = e.evidence_id AND {live})"
            )
        return (
            f"EXISTS (SELECT 1 FROM {self._t('relation_evidence')} e"
            f" WHERE e.relation_id = r.relation_id AND e.status = 'valid' AND ({chunk_ok}))"
        )

    def _chunk_live(self, tx: _Tx, chunk_id: str, namespace: str, now: datetime) -> bool:
        if _has_nul(chunk_id):
            return False  # no stored chunk id can contain NUL
        live = self._live_chunk_sql(tx, "%(namespace)s")
        if live is None:
            return False  # nothing to reconcile against: fail closed
        return tx.row(
            f"SELECT 1 FROM {self.chunks_table} c WHERE c.chunk_id = %(chunk_id)s AND {live}",
            {"chunk_id": chunk_id, "namespace": namespace, "now_epoch": now.timestamp()},
        ) is not None

    # -- loading -------------------------------------------------------------

    def _load_entity(
        self, tx: _Tx, entity_id: UUID | str, auth: AuthContext, *, lock: bool = False,
    ) -> Entity | None:
        row = tx.row(
            f"SELECT * FROM {self._t('entities')} WHERE entity_id = %s AND tenant_id = %s"
            " AND namespace IN (%s, %s)" + (" FOR SHARE" if lock else ""),
            (str(entity_id), auth.scope.tenant_id, auth.scope.namespace, GLOBAL_NAMESPACE),
        )
        if row is None:
            return None
        aliases = [alias for (alias,) in tx.tuples(
            f"SELECT alias FROM {self._t('aliases')} WHERE entity_id = %s ORDER BY alias",
            (row["entity_id"],),
        )]
        return self._entity_from_row(row, aliases)

    @staticmethod
    def _entity_from_row(row: dict[str, Any], aliases: Sequence[str]) -> Entity:
        return Entity.model_validate({
            "scope": {"tenant_id": row["tenant_id"], "namespace": row["namespace"]},
            "entity_type": row["entity_type"],
            "canonical_key": row["canonical_key"],
            "display_name": row["display_name"],
            "aliases": aliases,
            "attributes": _attrs(row["attributes"]),
            "schema_pack": row["schema_pack"],
            "schema_version": row["schema_version"],
            "revision": row["revision"],
            "created_at": _utc(row["created_at"]),
            "updated_at": _utc(row["updated_at"]),
            "deleted_at": _utc(row["deleted_at"]),
        })

    def _relation_from_row(
        self, tx: _Tx, row: dict[str, Any], *, evidence: Sequence[dict[str, Any]] | None = None,
    ) -> Relation:
        if evidence is None:
            evidence = tx.rows(
                f"SELECT * FROM {self._t('relation_evidence')} WHERE relation_id = %s"
                " AND status = 'valid' ORDER BY evidence_type, evidence_id, source_revision",
                (row["relation_id"],),
            )
        refs = [
            EvidenceRef(
                evidence_type=item["evidence_type"], evidence_id=item["evidence_id"],
                source_revision=item["source_revision"],
                observed_at=_utc(item["observed_at"]),  # type: ignore[arg-type]
            )
            for item in evidence
        ]
        return Relation.model_validate({
            "relation_id": row["relation_id"],
            "scope": {"tenant_id": row["tenant_id"], "namespace": row["namespace"]},
            "src_entity_id": row["src_entity_id"],
            "dst_entity_id": row["dst_entity_id"],
            "relation_type": row["relation_type"],
            "status": row["status"],
            "confidence": float(row["confidence"]),
            "valid_from": _utc(row["valid_from"]),
            "valid_until": _utc(row["valid_until"]),
            "observed_at": _utc(row["observed_at"]),
            "created_by": row["created_by"],
            "revision": row["revision"],
            "schema_pack": row["schema_pack"],
            "schema_version": row["schema_version"],
            "attributes": _attrs(row["attributes"]),
            "evidence": refs,
        })

    def _load_relation(
        self, tx: _Tx, relation_id: UUID | str, auth: AuthContext,
    ) -> Relation | None:
        row = tx.row(
            f"SELECT * FROM {self._t('relations')} WHERE relation_id = %s AND tenant_id = %s"
            " AND namespace IN (%s, %s)",
            (str(relation_id), auth.scope.tenant_id, auth.scope.namespace, GLOBAL_NAMESPACE),
        )
        return None if row is None else self._relation_from_row(tx, row)

    # -- OntologyStore -------------------------------------------------------

    def upsert_entity(
        self, entity: Entity, *, auth: AuthContext, expected_version: int | None = None,
    ) -> Entity:
        """Create or overwrite ``entity``; see ``SQLiteOntologyStore.upsert_entity``."""
        _require_write(auth, entity.scope)
        self.registry.validate_entity(entity)
        if expected_version is not None and expected_version < 0:
            raise ValueError("expected_version must be >= 0")
        entity_id = str(entity.entity_id)
        now = datetime.now(timezone.utc)
        with self._transaction(write=True) as tx:
            self._lock(tx, "entity", entity_id)  # also covers the not-yet-existing row
            row = tx.row(
                f"SELECT revision, created_at, schema_version FROM {self._t('entities')}"
                " WHERE entity_id = %s FOR UPDATE",
                (entity_id,),
            )
            current = 0 if row is None else int(row["revision"])
            if expected_version is not None and expected_version != current:
                raise OntologyConflictError(
                    f"entity revision conflict: expected {expected_version}, found {current}"
                )
            # The FOR UPDATE row lock serializes this against relation asserts,
            # which read their endpoints FOR SHARE.
            if row is not None and int(row["schema_version"]) != entity.schema_version and tx.row(
                f"SELECT 1 FROM {self._t('relations')} WHERE (src_entity_id = %(entity_id)s"
                " OR dst_entity_id = %(entity_id)s) AND status != 'revoked'"
                " AND (schema_pack != %(schema_pack)s OR schema_version != %(schema_version)s)"
                " LIMIT 1",
                {"entity_id": entity_id, "schema_pack": entity.schema_pack,
                 "schema_version": entity.schema_version},
            ) is not None:
                raise OntologyConflictError(_SCHEMA_UPGRADE_CONFLICT)
            revision = current + 1
            created = now if row is None else _utc(row["created_at"])
            values = (
                entity.schema_version, entity.display_name,
                json.dumps(entity.attributes, sort_keys=True), revision, now,
                None if entity.deleted_at is None else entity.deleted_at,
            )
            if row is None:
                tx.execute(
                    f"INSERT INTO {self._t('entities')} (entity_id, tenant_id, namespace,"
                    " schema_pack, entity_type, canonical_key, created_at, schema_version,"
                    " display_name, attributes, revision, updated_at, deleted_at)"
                    " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                    (entity_id, entity.scope.tenant_id, entity.scope.namespace,
                     entity.schema_pack, entity.entity_type, entity.canonical_key, created,
                     *values),
                )
            else:
                tx.execute(
                    f"UPDATE {self._t('entities')} SET schema_version = %s, display_name = %s,"
                    " attributes = %s, revision = %s, updated_at = %s, deleted_at = %s"
                    " WHERE entity_id = %s",
                    (*values, entity_id),
                )
            tx.execute(f"DELETE FROM {self._t('aliases')} WHERE entity_id = %s", (entity_id,))
            for alias in entity.aliases:
                tx.execute(
                    f"INSERT INTO {self._t('aliases')} (tenant_id, namespace, entity_type, alias,"
                    " entity_id) VALUES (%s, %s, %s, %s, %s)",
                    (entity.scope.tenant_id, entity.scope.namespace, entity.entity_type, alias,
                     entity_id),
                )
            self._event(
                tx, entity.scope, kind="entity", object_id=entity_id,
                event_type="created" if row is None else "updated", revision=revision,
                auth=auth, now=now,
            )
        return entity.model_copy(update={
            "revision": revision, "created_at": created, "updated_at": now,
        })

    def get_entity(self, entity_id: UUID, *, auth: AuthContext) -> Entity | None:
        _require(auth, "read")
        with self._transaction(write=False) as tx:
            return self._load_entity(tx, entity_id, auth)

    def assert_relation(
        self, relation: Relation, *, auth: AuthContext, idempotency_key: str,
    ) -> Relation:
        """Validate and persist ``relation``; see ``SQLiteOntologyStore.assert_relation``."""
        _require_write(auth, relation.scope)
        if relation.status == "revoked":
            raise ValueError("use revoke_relation to revoke a relation")
        if relation.status == "verified":
            _require(auth, "verify")
        key = idempotency_key.strip()
        if not 1 <= len(key) <= 256:
            raise ValueError("idempotency_key must contain 1 to 256 characters")
        fingerprint = hashlib.sha256(json.dumps(
            relation.model_dump(mode="json"), sort_keys=True, separators=(",", ":"),
        ).encode()).hexdigest()
        relation_id = str(relation.relation_id)
        tenant, namespace = relation.scope.tenant_id, relation.scope.namespace
        now = datetime.now(timezone.utc)
        with self._transaction(write=True) as tx:
            self._lock_scope(tx, tenant, namespace)
            self._lock(tx, "relation", relation_id)
            request = tx.row(
                f"SELECT fingerprint, relation_id FROM {self._t('write_requests')}"
                " WHERE tenant_id = %s AND namespace = %s AND idempotency_key = %s",
                (tenant, namespace, key),
            )
            if request is not None:
                if request["fingerprint"] != fingerprint:
                    raise OntologyConflictError(
                        "idempotency_key was already used with a different payload"
                    )
                stored = self._load_relation(tx, request["relation_id"], auth)
                assert stored is not None  # same scope as the request row
                return stored

            source = self._load_entity(tx, relation.src_entity_id, auth, lock=True)
            target = self._load_entity(tx, relation.dst_entity_id, auth, lock=True)
            if source is None or target is None:
                raise OntologyNotFoundError("relation endpoints are not available")
            self.registry.validate_relation(relation, source=source, target=target)
            definition = self.registry.relation_definition(relation)

            existing = tx.row(
                f"SELECT * FROM {self._t('relations')} WHERE relation_id = %s FOR UPDATE",
                (relation_id,),
            )
            mutable = (
                relation.status, relation.confidence, relation.valid_from, relation.valid_until,
                relation.observed_at, relation.schema_version,
                json.dumps(relation.attributes, sort_keys=True), now,
            )
            if existing is None:
                revision = 1
                tx.execute(
                    f"INSERT INTO {self._t('relations')} (relation_id, tenant_id, namespace,"
                    " src_entity_id, dst_entity_id, relation_type, schema_pack, created_by,"
                    " asserted_by, created_at, revision, status, confidence, valid_from,"
                    " valid_until, observed_at, schema_version, attributes, updated_at)"
                    " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,"
                    " %s, %s, %s)",
                    (relation_id, tenant, namespace, str(relation.src_entity_id),
                     str(relation.dst_entity_id), relation.relation_type, relation.schema_pack,
                     relation.created_by, auth.principal_id, now, revision, *mutable),
                )
            else:
                self._check_update(existing, relation, auth)
                revision = int(existing["revision"]) + 1
                tx.execute(
                    f"UPDATE {self._t('relations')} SET status = %s, confidence = %s,"
                    " valid_from = %s, valid_until = %s, observed_at = %s, schema_version = %s,"
                    " attributes = %s, updated_at = %s, revision = %s WHERE relation_id = %s",
                    (*mutable, revision, relation_id),
                )
            self._check_cardinality(tx, relation, definition, now)
            if not definition.allow_cycles:
                self._check_acyclic(tx, relation, now)

            for ref in relation.evidence:
                if ref.evidence_type == "chunk" and not self._chunk_live(
                    tx, ref.evidence_id, namespace, now,
                ):
                    raise ValueError("evidence chunk is not available")
                tx.execute(
                    f"INSERT INTO {self._t('relation_evidence')} (relation_id, evidence_type,"
                    " evidence_id, source_revision, observed_at) VALUES (%s, %s, %s, %s, %s)"
                    " ON CONFLICT DO NOTHING",
                    (relation_id, ref.evidence_type, ref.evidence_id, ref.source_revision,
                     ref.observed_at),
                )
            if relation.status == "verified" and tx.row(
                f"SELECT 1 FROM {self._t('relations')} r WHERE r.relation_id = %(relation_id)s"
                f" AND {self._valid_evidence_sql(tx)}",
                {"relation_id": relation_id, "now_epoch": now.timestamp()},
            ) is None:
                raise ValueError("verified relations require valid, live evidence")

            tx.execute(
                f"INSERT INTO {self._t('write_requests')} (tenant_id, namespace, idempotency_key,"
                " fingerprint, relation_id, principal_id, created_at)"
                " VALUES (%s, %s, %s, %s, %s, %s, %s)",
                (tenant, namespace, key, fingerprint, relation_id, auth.principal_id, now),
            )
            self._event(
                tx, relation.scope, kind="relation", object_id=relation_id,
                event_type="asserted" if existing is None else "updated", revision=revision,
                auth=auth, now=now,
            )
            stored = self._load_relation(tx, relation_id, auth)
            assert stored is not None
            return stored

    @staticmethod
    def _check_update(existing: dict[str, Any], relation: Relation, auth: AuthContext) -> None:
        """Mirror of ``SQLiteOntologyStore._check_update`` (B1 validity-window rule)."""
        if (existing["tenant_id"], existing["namespace"]) != (
            relation.scope.tenant_id, relation.scope.namespace,
        ):
            raise OntologyNotFoundError("relation_id is not available")
        identity = (
            str(relation.src_entity_id), str(relation.dst_entity_id),
            relation.relation_type, relation.schema_pack,
        )
        if identity != (
            existing["src_entity_id"], existing["dst_entity_id"],
            existing["relation_type"], existing["schema_pack"],
        ):
            raise ValueError("relation endpoints, type and schema pack are immutable")
        if existing["status"] == "revoked":
            raise ValueError("revoked relations cannot be re-asserted")
        # Compared at the stored (microsecond) precision, like SQLite's text form.
        window = (
            _ts(relation.valid_from),
            None if relation.valid_until is None else _ts(relation.valid_until),
        )
        stored = (
            _ts(existing["valid_from"]),
            None if existing["valid_until"] is None else _ts(existing["valid_until"]),
        )
        if window != stored and existing["asserted_by"] != auth.principal_id:
            if "admin" not in auth.capabilities:
                raise PermissionError(
                    "only the original asserter or an admin may change a relation's validity window"
                )
        if existing["status"] == "verified":
            _require(auth, "verify")  # changing a verified fact is a verification act

    def _check_cardinality(
        self, tx: _Tx, relation: Relation, definition: RelationType, now: datetime,
    ) -> None:
        for column, limit, label in (
            ("src_entity_id", definition.max_outgoing, "max_outgoing"),
            ("dst_entity_id", definition.max_incoming, "max_incoming"),
        ):
            if limit is None:
                continue
            endpoint = relation.src_entity_id if column == "src_entity_id" else relation.dst_entity_id
            (count,), = tx.tuples(
                f"SELECT COUNT(*) FROM {self._t('relations')} r WHERE r.tenant_id = %(tenant)s"
                f" AND r.namespace = %(namespace)s AND r.{column} = %(endpoint)s"
                f" AND r.relation_type = %(relation_type)s AND {_ACTIVE}",
                {"tenant": relation.scope.tenant_id, "namespace": relation.scope.namespace,
                 "endpoint": str(endpoint), "relation_type": relation.relation_type, "now": now},
            )
            if int(count) > limit:
                raise ValueError(f"relation type {relation.relation_type} exceeds {label}")

    def _check_acyclic(self, tx: _Tx, relation: Relation, now: datetime) -> None:
        # UNION (not UNION ALL) drops revisited nodes, so the recursion
        # terminates on graphs that already contain cycles.
        found = tx.row(
            f"WITH RECURSIVE reach(entity_id) AS (SELECT CAST(%(start)s AS TEXT) COLLATE \"C\""
            f" UNION SELECT r.dst_entity_id FROM {self._t('relations')} r"
            " JOIN reach ON r.src_entity_id = reach.entity_id"
            " WHERE r.tenant_id = %(tenant)s AND r.namespace = %(namespace)s"
            f" AND r.relation_type = %(relation_type)s AND {_ACTIVE})"
            " SELECT 1 FROM reach WHERE entity_id = %(goal)s LIMIT 1",
            {"start": str(relation.dst_entity_id), "goal": str(relation.src_entity_id),
             "tenant": relation.scope.tenant_id, "namespace": relation.scope.namespace,
             "relation_type": relation.relation_type, "now": now},
        )
        if found is not None:
            raise ValueError("relation type does not allow cycles")

    def traverse(
        self, request: GraphQuery, *, auth: AuthContext, deadline: float | None = None,
    ) -> GraphResult:
        """Breadth-first expansion; see ``SQLiteOntologyStore.traverse`` (same
        order, same direction / relation-type filters, one edge query and one
        evidence query per hop)."""
        _require(auth, "read")
        now = datetime.now(timezone.utc)
        with self._transaction(write=False, deadline=deadline) as tx:
            seeds = self._visible_seeds(tx, request.seeds, auth)
            edge_sql, params = self._edge_query(tx, request, auth, now)

            def fetch_edges(frontier: Sequence[str]) -> list[dict[str, Any]]:
                return tx.rows(edge_sql, {**params, "nodes": list(frontier)})

            def load_relations(rows: Sequence[Any]) -> dict[str, Relation]:
                evidence = self._evidence_rows(tx, [row["relation_id"] for row in rows])
                return {
                    row["relation_id"]: self._relation_from_row(
                        tx, row, evidence=evidence.get(row["relation_id"], []),
                    )
                    for row in rows
                }

            return breadth_first(
                request, seeds, fetch_edges=fetch_edges, load_relations=load_relations,
                check_deadline=lambda: _check_deadline(deadline),
            )

    def _evidence_rows(self, tx: _Tx, relation_ids: Sequence[str]) -> dict[str, list[dict[str, Any]]]:
        """Valid evidence rows of ``relation_ids`` in one query (see the SQLite store)."""
        grouped: dict[str, list[dict[str, Any]]] = {}
        ids = list(dict.fromkeys(relation_ids))
        if not ids:
            return grouped
        for item in tx.rows(
            f"SELECT * FROM {self._t('relation_evidence')} WHERE relation_id = ANY(%s)"
            " AND status = 'valid'"
            " ORDER BY relation_id, evidence_type, evidence_id, source_revision",
            (ids,),
        ):
            grouped.setdefault(item["relation_id"], []).append(item)
        return grouped

    def _visible_seeds(self, tx: _Tx, seeds: Sequence[UUID], auth: AuthContext) -> list[str]:
        requested = list(dict.fromkeys(str(seed) for seed in seeds))
        if not requested:
            return []
        visible = {entity_id for (entity_id,) in tx.tuples(
            f"SELECT entity_id FROM {self._t('entities')} WHERE entity_id = ANY(%s)"
            " AND tenant_id = %s AND namespace IN (%s, %s) AND deleted_at IS NULL",
            (requested, auth.scope.tenant_id, auth.scope.namespace, GLOBAL_NAMESPACE),
        )}
        return [seed for seed in requested if seed in visible]

    def _edge_query(
        self, tx: _Tx, request: GraphQuery, auth: AuthContext, now: datetime,
    ) -> tuple[str, dict[str, Any]]:
        params: dict[str, Any] = {
            "tenant": auth.scope.tenant_id, "namespace": auth.scope.namespace,
            "global": GLOBAL_NAMESPACE, "now": now, "now_epoch": now.timestamp(),
        }

        def visible(alias: str) -> str:
            return (
                f"{alias}.tenant_id = %(tenant)s"
                f" AND {alias}.namespace IN (%(namespace)s, %(global)s)"
            )

        clauses = [
            visible("r"), visible("s"), visible("d"),
            "s.deleted_at IS NULL", "d.deleted_at IS NULL", _ACTIVE, _SAME_SCHEMA,
        ]
        if request.relation_types:
            clauses.append("r.relation_type = ANY(%(relation_types)s)")
            params["relation_types"] = list(request.relation_types)
        if request.verified_only:
            clauses.append(f"r.status = 'verified' AND {self._valid_evidence_sql(tx)}")
        relations = self._t("relations")
        branches = []
        if request.direction in ("both", "out"):
            branches.append(
                f"SELECT x.relation_id FROM {relations} x WHERE {visible('x')}"
                " AND x.src_entity_id = ANY(%(nodes)s)"
            )
        if request.direction in ("both", "in"):
            branches.append(
                f"SELECT y.relation_id FROM {relations} y WHERE {visible('y')}"
                " AND y.dst_entity_id = ANY(%(nodes)s)"
            )
        sql = (
            f"SELECT r.* FROM {relations} r"
            f" JOIN {self._t('entities')} s ON s.entity_id = r.src_entity_id"
            f" JOIN {self._t('entities')} d ON d.entity_id = r.dst_entity_id"
            f" WHERE r.relation_id IN ({' UNION '.join(branches)})"
            f" AND {' AND '.join(clauses)}"
            " ORDER BY r.relation_type, r.relation_id"
        )
        return sql, params

    # -- scoped read helpers (facade) ----------------------------------------

    def get_entities(
        self, entity_ids: Sequence[UUID | str], *, auth: AuthContext,
        deadline: float | None = None,
    ) -> dict[str, Entity]:
        """Visible entities by id, in one read transaction; invisible ids are absent."""
        _require(auth, "read")
        requested = list(dict.fromkeys(str(item) for item in entity_ids))
        if not requested:
            return {}
        with self._transaction(write=False, deadline=deadline) as tx:
            rows = {row["entity_id"]: row for row in tx.rows(
                f"SELECT * FROM {self._t('entities')} WHERE entity_id = ANY(%s)"
                " AND tenant_id = %s AND namespace IN (%s, %s)",
                (requested, auth.scope.tenant_id, auth.scope.namespace, GLOBAL_NAMESPACE),
            )}
            aliases: dict[str, list[str]] = {}
            if rows:
                for entity_id, alias in tx.tuples(
                    f"SELECT entity_id, alias FROM {self._t('aliases')}"
                    " WHERE entity_id = ANY(%s) ORDER BY entity_id, alias",
                    (list(rows),),
                ):
                    aliases.setdefault(entity_id, []).append(alias)
        return {
            entity_id: self._entity_from_row(rows[entity_id], aliases.get(entity_id, []))
            for entity_id in requested if entity_id in rows
        }

    def relation_in_scope(self, relation_id: UUID, *, auth: AuthContext) -> bool:
        """Whether ``relation_id`` names a relation in exactly the caller's scope."""
        _require(auth, "read")
        with self._transaction(write=False) as tx:
            return tx.row(
                f"SELECT 1 FROM {self._t('relations')} WHERE relation_id = %s AND tenant_id = %s"
                " AND namespace = %s",
                (str(relation_id), auth.scope.tenant_id, auth.scope.namespace),
            ) is not None

    def resolve_entities(
        self, key: str, *, auth: AuthContext, entity_type: str | None = None,
        schema_pack: str | None = None, limit: int = 20,
    ) -> tuple[EntityMatch, str]:
        """Exact lookup of ``key``; see ``SQLiteOntologyStore.resolve_entities``."""
        _require(auth, "read")
        normalized = normalize_key(key)
        if not 1 <= len(normalized) <= 256:
            raise ValueError("lookup key must contain 1 to 256 characters")
        if _has_nul(normalized):
            return EntityMatch(entity_ids=(), matched_key=normalized), "none"
        params: dict[str, Any] = {
            "tenant": auth.scope.tenant_id, "namespace": auth.scope.namespace,
            "global": GLOBAL_NAMESPACE, "key": normalized, "limit": limit + 1,
        }
        filters = ""
        if entity_type is not None:
            filters += " AND e.entity_type = %(entity_type)s"
            params["entity_type"] = entity_type
        if schema_pack is not None:
            filters += " AND e.schema_pack = %(schema_pack)s"
            params["schema_pack"] = schema_pack
        visible = (
            "e.tenant_id = %(tenant)s AND e.namespace IN (%(namespace)s, %(global)s)"
            " AND e.deleted_at IS NULL" + filters
        )
        with self._transaction(write=False) as tx:
            for column, sql in (
                ("canonical_key",
                 f"SELECT e.entity_id FROM {self._t('entities')} e WHERE {visible}"
                 " AND e.canonical_key = %(key)s ORDER BY e.entity_id LIMIT %(limit)s"),
                ("alias",
                 f"SELECT DISTINCT e.entity_id FROM {self._t('aliases')} a"
                 f" JOIN {self._t('entities')} e ON e.entity_id = a.entity_id"
                 f" WHERE {visible} AND a.alias = %(key)s ORDER BY e.entity_id LIMIT %(limit)s"),
            ):
                ids = [UUID(entity_id) for (entity_id,) in tx.tuples(sql, params)]
                if ids:
                    return EntityMatch(entity_ids=tuple(ids), matched_key=normalized), column
        return EntityMatch(entity_ids=(), matched_key=normalized), "none"

    def resolve_terms(
        self, terms: Sequence[str], *, auth: AuthContext, schema_packs: Sequence[str] = (),
        limit: int = 20, deadline: float | None = None,
    ) -> dict[str, tuple[tuple[UUID, ...], str]]:
        """Batch exact lookup; see ``SQLiteOntologyStore.resolve_terms``."""
        _require(auth, "read")
        keys = sorted({key for key in (normalize_key(term) for term in terms)
                       if 1 <= len(key) <= 256 and not _has_nul(key)})
        found: dict[str, tuple[tuple[UUID, ...], str]] = {}
        if not keys:
            return found
        params: dict[str, Any] = {
            "tenant": auth.scope.tenant_id, "namespace": auth.scope.namespace,
            "global": GLOBAL_NAMESPACE,
        }
        visible = (
            "e.tenant_id = %(tenant)s AND e.namespace IN (%(namespace)s, %(global)s)"
            " AND e.deleted_at IS NULL"
        )
        if schema_packs:
            visible += " AND e.schema_pack = ANY(%(packs)s)"
            params["packs"] = list(schema_packs)
        with self._transaction(write=False, deadline=deadline) as tx:
            for column, source in (
                ("canonical_key", f"{self._t('entities')} e"),
                ("alias", f"{self._t('aliases')} a JOIN {self._t('entities')} e"
                          " ON e.entity_id = a.entity_id"),
            ):
                pending = [key for key in keys if key not in found]
                if not pending:
                    break
                field = "e.canonical_key" if column == "canonical_key" else "a.alias"
                matched: dict[str, list[str]] = {}
                for key, entity_id in tx.tuples(
                    f"SELECT DISTINCT {field}, e.entity_id FROM {source} WHERE {visible}"
                    f" AND {field} = ANY(%(keys)s) ORDER BY {field}, e.entity_id",
                    {**params, "keys": pending},
                ):
                    ids = matched.setdefault(key, [])
                    if len(ids) <= limit:
                        ids.append(entity_id)
                for key, ids in matched.items():
                    found[key] = (tuple(UUID(item) for item in ids), column)
        return found

    def live_chunk_evidence(
        self, chunk_ids: Sequence[str], *, auth: AuthContext, relation_namespace: str,
        deadline: float | None = None,
    ) -> set[str]:
        """Live, caller-readable subset; see ``SQLiteOntologyStore.live_chunk_evidence``."""
        _require(auth, "read")
        ids = sorted({str(chunk_id) for chunk_id in chunk_ids if not _has_nul(str(chunk_id))})
        live: set[str] = set()
        if not ids:
            return live
        now = datetime.now(timezone.utc)
        with self._transaction(write=False, deadline=deadline) as tx:
            predicate = self._live_chunk_sql(tx, "%(relation_namespace)s")
            if predicate is None:
                return live
            live.update(chunk_id for (chunk_id,) in tx.tuples(
                f"SELECT c.chunk_id FROM {self.chunks_table} c WHERE c.chunk_id = ANY(%(ids)s)"
                f" AND {predicate}"
                " AND (COALESCE(c.scope, 'pipeline') = 'global' OR c.pipeline_id = %(caller)s)",
                {"ids": ids, "relation_namespace": relation_namespace,
                 "caller": auth.scope.namespace, "now_epoch": now.timestamp()},
            ))
        return live

    def evidence_chunks(
        self, chunk_ids: Sequence[str], *, auth: AuthContext, deadline: float | None = None,
    ) -> dict[str, dict[str, Any]]:
        """Live, caller-readable chunk text; see ``SQLiteOntologyStore.evidence_chunks``."""
        _require(auth, "read")
        ids = sorted({str(chunk_id) for chunk_id in chunk_ids if not _has_nul(str(chunk_id))})
        found: dict[str, dict[str, Any]] = {}
        if not ids:
            return found
        now = datetime.now(timezone.utc)
        with self._transaction(write=False, deadline=deadline) as tx:
            predicate = self._live_chunk_sql(tx, "%(caller)s")
            if predicate is None:
                return found
            for row in tx.rows(
                f"SELECT c.chunk_id, c.content, c.layer, c.src FROM {self.chunks_table} c"
                f" WHERE c.chunk_id = ANY(%(ids)s) AND {predicate}",
                {"ids": ids, "caller": auth.scope.namespace, "now_epoch": now.timestamp()},
            ):
                found[row["chunk_id"]] = {
                    "chunk_id": row["chunk_id"], "layer": row["layer"], "src": row["src"],
                    "content": row["content"],
                }
        return found

    def explain_relation(
        self, relation_id: UUID, *, auth: AuthContext,
    ) -> tuple[Relation, list[dict[str, Any]]] | None:
        """Relation plus every evidence row with liveness; see the SQLite twin.

        Timestamps in the evidence rows use the SQLite store's fixed-width
        text form, so both backends return identical dicts.
        """
        _require(auth, "read")
        now = datetime.now(timezone.utc)
        with self._transaction(write=False) as tx:
            relation = self._load_relation(tx, relation_id, auth)
            if relation is None:
                return None
            evidence: list[dict[str, Any]] = []
            for row in tx.rows(
                f"SELECT * FROM {self._t('relation_evidence')} WHERE relation_id = %s"
                " ORDER BY evidence_type, evidence_id, source_revision",
                (str(relation_id),),
            ):
                live = row["status"] == "valid" and (
                    row["evidence_type"] != "chunk"
                    or self._chunk_live(tx, row["evidence_id"], relation.scope.namespace, now)
                )
                evidence.append({
                    "evidence_type": row["evidence_type"],
                    "evidence_id": row["evidence_id"],
                    "source_revision": row["source_revision"],
                    "observed_at": _ts(row["observed_at"]),
                    "status": row["status"],
                    "invalidated_reason": row["invalidated_reason"],
                    "invalidated_at": (
                        None if row["invalidated_at"] is None else _ts(row["invalidated_at"])
                    ),
                    "live": live,
                })
            return relation, evidence

    def invalidate_evidence(
        self, chunk_ids: Sequence[str], *, auth: AuthContext, reason: str,
    ) -> int:
        """Invalidate chunk evidence in the caller's scope; see the SQLite twin."""
        _require_write(auth, auth.scope)
        if not reason.strip():
            raise ValueError("reason is required")
        ids = sorted({str(chunk_id) for chunk_id in chunk_ids if not _has_nul(str(chunk_id))})
        now = datetime.now(timezone.utc)
        tenant, namespace = auth.scope.tenant_id, auth.scope.namespace
        with self._transaction(write=True) as tx:
            self._lock_scope(tx, tenant, namespace)
            affected = sorted({relation_id for (relation_id,) in tx.tuples(
                f"UPDATE {self._t('relation_evidence')} e SET status = 'invalidated',"
                " invalidated_reason = %(reason)s, invalidated_at = %(now)s"
                " WHERE e.evidence_type = 'chunk' AND e.status = 'valid'"
                " AND e.evidence_id = ANY(%(ids)s)"
                f" AND e.relation_id IN (SELECT relation_id FROM {self._t('relations')}"
                " WHERE tenant_id = %(tenant)s AND namespace = %(namespace)s)"
                " RETURNING e.relation_id",
                {"reason": reason, "now": now, "ids": ids, "tenant": tenant,
                 "namespace": namespace},
            )})
            for relation_id in affected:
                row = tx.row(
                    "SELECT status, revision, (SELECT COUNT(*) FROM"
                    f" {self._t('relation_evidence')} e WHERE e.relation_id = r.relation_id"
                    " AND e.status = 'valid') AS valid"
                    f" FROM {self._t('relations')} r WHERE relation_id = %s",
                    (relation_id,),
                )
                assert row is not None
                if row["status"] == "verified":
                    # Changing a verified relation's support is a verification
                    # act; raising rolls back every update in this call.
                    _require(auth, "verify")
                revision = int(row["revision"])
                event_type = "evidence_invalidated"
                if row["status"] == "verified" and int(row["valid"]) == 0:
                    revision += 1
                    event_type = "demoted"
                    tx.execute(
                        f"UPDATE {self._t('relations')} SET status = 'candidate', revision = %s,"
                        " updated_at = %s WHERE relation_id = %s",
                        (revision, now, relation_id),
                    )
                self._event(
                    tx, auth.scope, kind="relation", object_id=relation_id,
                    event_type=event_type, revision=revision, auth=auth, now=now, reason=reason,
                )
        return len(affected)

    def revoke_relation(self, relation_id: UUID, *, auth: AuthContext, reason: str) -> Relation:
        """Mark a relation revoked (idempotent); see ``SQLiteOntologyStore.revoke_relation``."""
        _require_write(auth, auth.scope)  # before the lookup: no existence oracle
        if not reason.strip():
            raise ValueError("reason is required")
        now = datetime.now(timezone.utc)
        with self._transaction(write=True) as tx:
            self._lock_scope(tx, auth.scope.tenant_id, auth.scope.namespace)
            row = tx.row(
                f"SELECT * FROM {self._t('relations')} WHERE relation_id = %s AND tenant_id = %s"
                " AND namespace = %s FOR UPDATE",
                (str(relation_id), auth.scope.tenant_id, auth.scope.namespace),
            )
            if row is None:
                raise OntologyNotFoundError("relation is not available")
            if row["asserted_by"] != auth.principal_id:
                _require(auth, "admin")
            if row["status"] != "revoked":
                revision = int(row["revision"]) + 1
                tx.execute(
                    f"UPDATE {self._t('relations')} SET status = 'revoked', revision = %s,"
                    " updated_at = %s WHERE relation_id = %s",
                    (revision, now, str(relation_id)),
                )
                self._event(
                    tx, auth.scope, kind="relation", object_id=str(relation_id),
                    event_type="revoked", revision=revision, auth=auth, now=now, reason=reason,
                )
            stored = self._load_relation(tx, relation_id, auth)
            assert stored is not None
            return stored


class AsyncPostgresOntologyStore:
    """Awaitable facade over :class:`PostgresOntologyStore` for async hosts.

    NCP's async runtime (``AsyncPgvectorStore`` / ``Assembler.post_turn_async``)
    only covers the post-turn write path; context assembly -- where ontology
    enrichment runs -- and the MCP ontology tools are synchronous. So this is a
    deliberately thin wrapper: every method runs the synchronous store in a
    worker thread via :func:`asyncio.to_thread` (each call borrows its own
    pooled connection, so concurrent awaits do not share a transaction).
    Results, ordering and errors are exactly the sync store's.
    """

    _METHODS = (
        "upsert_entity", "get_entity", "assert_relation", "traverse", "get_entities",
        "relation_in_scope", "resolve_entities", "resolve_terms", "live_chunk_evidence",
        "evidence_chunks", "explain_relation", "invalidate_evidence", "revoke_relation",
    )

    def __init__(self, store: PostgresOntologyStore) -> None:
        self.sync = store
        self.registry = store.registry

    @classmethod
    def inherit(
        cls, store: AsyncPgvectorStore | PgvectorStore, registry: SchemaRegistry,
    ) -> AsyncPostgresOntologyStore:
        """Share an NCP pgvector store's database, schema and table prefix.

        A sync ``PgvectorStore`` lends its pool; an ``AsyncPgvectorStore``'s
        async pool cannot serve psycopg's sync API, so a small private pool
        is opened against the same DSN (close it with :meth:`close`).
        """
        from ncp.stores.pgvector import PgvectorStore

        if isinstance(store, PgvectorStore):
            return cls(PostgresOntologyStore.inherit(store, registry))
        return cls(PostgresOntologyStore(
            store.dsn, registry, schema=store.schema, table_prefix=store.table_prefix,
        ))

    def close(self) -> None:
        self.sync.close()

    def __getattr__(self, name: str) -> Any:
        if name not in self._METHODS:
            raise AttributeError(name)
        method = getattr(self.sync, name)

        async def call(*args: Any, **kwargs: Any) -> Any:
            import asyncio

            return await asyncio.to_thread(method, *args, **kwargs)

        call.__name__ = name
        return call


__all__ = ["AsyncPostgresOntologyStore", "ConnectionSource", "PostgresOntologyStore"]
