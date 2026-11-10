"""SQLite persistence for the opt-in domain ontology.

The ontology tables live beside (never inside) the NCP memory tables and are
only created when this store is constructed, so a plain ``SQLiteStore`` keeps
its schema. Domain relations are distinct from ``chunk_edges``; this module
never writes ``chunks`` or ``chunk_edges`` and only reads ``chunks`` to check
that cited evidence is still live.

Scope rules: an ``AuthContext`` reads its own namespace plus ``global`` in its
tenant, and writes only its own scope. Writes into ``global`` additionally need
``admin``; asserting or keeping a relation ``verified`` needs ``verify``.
Out-of-scope rows behave exactly like missing rows.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
from collections.abc import Iterator, Sequence
from contextlib import contextmanager, nullcontext
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any
from uuid import UUID

from ncp.ontology.models import (
    GLOBAL_NAMESPACE, AuthContext, Capability, Entity, EntityMatch, EvidenceRef, GraphPath,
    GraphQuery, GraphResult, Relation, Scope, normalize_key,
)
from ncp.ontology.registry import RelationType, SchemaRegistry

if TYPE_CHECKING:
    from ncp.stores.sqlite import SQLiteStore


class OntologyConflictError(ValueError):
    """An optimistic-concurrency or idempotency precondition did not hold."""


class OntologyNotFoundError(ValueError):
    """A referenced object is missing or outside the caller's readable scope.

    The two cases are deliberately indistinguishable.
    """


_TS_FORMAT = "%Y-%m-%dT%H:%M:%S.%fZ"  # fixed width, so text order is time order
_IN_BATCH = 500
_ACTIVE = "r.status != 'revoked' AND (r.valid_until IS NULL OR r.valid_until > :now)"
# NCP chunk liveness: deleted/forgotten/consolidated-away rows are gone,
# superseded rows carry superseded_by, bitemporal and TTL expiry by timestamp.
_LIVE_CHUNK = (
    "c.superseded_by IS NULL AND (c.valid_to IS NULL OR c.valid_to > :now_epoch)"
    " AND (c.expiry IS NULL OR c.expiry > :now_epoch)"
    " AND (IFNULL(c.scope, 'pipeline') = 'global' OR c.pipeline_id = {namespace})"
)

_SCHEMA_V1 = (
    """CREATE TABLE ontology_entities (
        entity_id TEXT PRIMARY KEY,
        tenant_id TEXT NOT NULL,
        namespace TEXT NOT NULL,
        schema_pack TEXT NOT NULL,
        schema_version INTEGER NOT NULL,
        entity_type TEXT NOT NULL,
        canonical_key TEXT NOT NULL,
        display_name TEXT NOT NULL,
        attributes TEXT NOT NULL,
        revision INTEGER NOT NULL,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        deleted_at TEXT,
        UNIQUE (tenant_id, namespace, schema_pack, entity_type, canonical_key)
    )""",
    """CREATE TABLE ontology_aliases (
        tenant_id TEXT NOT NULL,
        namespace TEXT NOT NULL,
        entity_type TEXT NOT NULL,
        alias TEXT NOT NULL,
        entity_id TEXT NOT NULL REFERENCES ontology_entities(entity_id) ON DELETE CASCADE,
        PRIMARY KEY (tenant_id, namespace, entity_type, alias, entity_id)
    )""",
    "CREATE INDEX idx_ontology_aliases_entity ON ontology_aliases(entity_id)",
    """CREATE TABLE ontology_relations (
        relation_id TEXT PRIMARY KEY,
        tenant_id TEXT NOT NULL,
        namespace TEXT NOT NULL,
        src_entity_id TEXT NOT NULL REFERENCES ontology_entities(entity_id),
        dst_entity_id TEXT NOT NULL REFERENCES ontology_entities(entity_id),
        relation_type TEXT NOT NULL,
        status TEXT NOT NULL CHECK (status IN ('candidate', 'verified', 'disputed', 'revoked')),
        confidence REAL NOT NULL,
        valid_from TEXT NOT NULL,
        valid_until TEXT,
        observed_at TEXT NOT NULL,
        created_by TEXT NOT NULL,
        asserted_by TEXT NOT NULL,
        revision INTEGER NOT NULL,
        schema_pack TEXT NOT NULL,
        schema_version INTEGER NOT NULL,
        attributes TEXT NOT NULL,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )""",
    "CREATE INDEX idx_ontology_relations_src"
    " ON ontology_relations(tenant_id, namespace, src_entity_id, relation_type)",
    "CREATE INDEX idx_ontology_relations_dst"
    " ON ontology_relations(tenant_id, namespace, dst_entity_id, relation_type)",
    """CREATE TABLE ontology_relation_evidence (
        relation_id TEXT NOT NULL REFERENCES ontology_relations(relation_id) ON DELETE CASCADE,
        evidence_type TEXT NOT NULL,
        evidence_id TEXT NOT NULL,
        source_revision TEXT NOT NULL,
        observed_at TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'valid' CHECK (status IN ('valid', 'invalidated')),
        invalidated_reason TEXT,
        invalidated_at TEXT,
        PRIMARY KEY (relation_id, evidence_type, evidence_id, source_revision)
    )""",
    "CREATE INDEX idx_ontology_evidence_ref"
    " ON ontology_relation_evidence(evidence_type, evidence_id, status)",
    """CREATE TABLE ontology_write_requests (
        tenant_id TEXT NOT NULL,
        namespace TEXT NOT NULL,
        idempotency_key TEXT NOT NULL,
        fingerprint TEXT NOT NULL,
        relation_id TEXT NOT NULL REFERENCES ontology_relations(relation_id),
        principal_id TEXT NOT NULL,
        created_at TEXT NOT NULL,
        PRIMARY KEY (tenant_id, namespace, idempotency_key)
    )""",
    """CREATE TABLE ontology_events (
        event_id INTEGER PRIMARY KEY AUTOINCREMENT,
        tenant_id TEXT NOT NULL,
        namespace TEXT NOT NULL,
        object_kind TEXT NOT NULL,
        object_id TEXT NOT NULL,
        event_type TEXT NOT NULL,
        revision INTEGER NOT NULL,
        principal_id TEXT NOT NULL,
        reason TEXT,
        created_at TEXT NOT NULL
    )""",
    """CREATE TRIGGER ontology_events_no_update BEFORE UPDATE ON ontology_events
        BEGIN SELECT RAISE(ABORT, 'ontology_events is append-only'); END""",
    """CREATE TRIGGER ontology_events_no_delete BEFORE DELETE ON ontology_events
        BEGIN SELECT RAISE(ABORT, 'ontology_events is append-only'); END""",
)
# Append-only: later versions add entries, never edit shipped ones.
_MIGRATIONS: tuple[tuple[int, tuple[str, ...]], ...] = ((1, _SCHEMA_V1),)


def _ts(value: datetime) -> str:
    return value.astimezone(timezone.utc).strftime(_TS_FORMAT)


def _dt(value: str | None) -> datetime | None:
    if value is None:
        return None
    return datetime.strptime(value, _TS_FORMAT).replace(tzinfo=timezone.utc)


def _require(auth: AuthContext, capability: Capability) -> None:
    if capability not in auth.capabilities:
        raise PermissionError(f"ontology operation requires the {capability!r} capability")


def _require_write(auth: AuthContext, scope: Scope) -> None:
    _require(auth, "write")
    if scope != auth.scope:
        raise PermissionError("ontology writes must target the caller's own scope")
    if scope.namespace == GLOBAL_NAMESPACE:
        _require(auth, "admin")


class SQLiteOntologyStore:
    """``OntologyStore`` over SQLite; see the module docstring for scope rules.

    ``target`` is a database path (a fresh connection per operation, safe
    across threads) or an existing ``sqlite3.Connection`` (serialized by a
    lock; foreign keys are switched on for it). Every operation runs in one
    explicit transaction.
    """

    def __init__(
        self, target: str | Path | sqlite3.Connection, registry: SchemaRegistry,
        *, timeout: float = 5.0,
    ) -> None:
        self.registry = registry
        self._timeout = timeout
        self._shared: sqlite3.Connection | None = None
        self._lock = threading.RLock()
        if isinstance(target, sqlite3.Connection):
            if target.in_transaction:
                raise RuntimeError("ontology store needs a connection without an open transaction")
            self._shared = target
            self.path: Path | None = None
            target.execute("PRAGMA foreign_keys=ON")
        else:
            self.path = Path(target)
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self._open() as connection:
                connection.execute("PRAGMA journal_mode=WAL")
        self._migrate()

    @classmethod
    def inherit(cls, store: SQLiteStore, registry: SchemaRegistry) -> SQLiteOntologyStore:
        """``store = "inherit"``: share the NCP SQLite store's database file."""
        return cls(store.path, registry)

    # -- connection and transaction plumbing ---------------------------------

    @contextmanager
    def _open(self) -> Iterator[sqlite3.Connection]:
        assert self.path is not None
        connection = sqlite3.connect(self.path, timeout=self._timeout, isolation_level=None)
        try:
            connection.execute("PRAGMA foreign_keys=ON")
            connection.execute(f"PRAGMA busy_timeout={int(self._timeout * 1000)}")
            yield connection
        finally:
            connection.close()

    @contextmanager
    def _transaction(self, *, write: bool) -> Iterator[sqlite3.Connection]:
        guard = self._lock if self._shared is not None else nullcontext()
        with guard:
            opened = nullcontext(self._shared) if self._shared is not None else self._open()
            with opened as connection:
                if connection.in_transaction:
                    raise RuntimeError("ontology store connection has an open transaction")
                connection.execute("BEGIN IMMEDIATE" if write else "BEGIN")
                try:
                    yield connection
                except BaseException:
                    if connection.in_transaction:
                        connection.execute("ROLLBACK")
                    raise
                try:
                    connection.execute("COMMIT")
                except BaseException:
                    # A failed COMMIT (e.g. SQLITE_BUSY) leaves the transaction
                    # open; a shared connection would otherwise stay wedged.
                    if connection.in_transaction:
                        connection.execute("ROLLBACK")
                    raise

    @staticmethod
    def _plain(connection: sqlite3.Connection) -> sqlite3.Cursor:
        """Tuple rows regardless of the (possibly shared) connection's row factory."""
        cursor = connection.cursor()
        cursor.row_factory = None
        return cursor

    @staticmethod
    def _rows(
        connection: sqlite3.Connection, sql: str, params: Sequence[Any] | dict[str, Any] = (),
    ) -> list[sqlite3.Row]:
        cursor = connection.cursor()
        cursor.row_factory = sqlite3.Row  # per cursor: a shared connection keeps its own
        return cursor.execute(sql, params).fetchall()

    def _row(
        self, connection: sqlite3.Connection, sql: str,
        params: Sequence[Any] | dict[str, Any] = (),
    ) -> sqlite3.Row | None:
        rows = self._rows(connection, sql, params)
        return rows[0] if rows else None

    def _migrate(self) -> None:
        with self._transaction(write=True) as connection:
            connection.execute(
                "CREATE TABLE IF NOT EXISTS ontology_schema_versions"
                " (version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)"
            )
            current = self._plain(connection).execute(
                "SELECT IFNULL(MAX(version), 0) FROM ontology_schema_versions"
            ).fetchone()[0]
            for version, statements in _MIGRATIONS:
                if version <= current:
                    continue
                for statement in statements:
                    connection.execute(statement)
                connection.execute(
                    "INSERT INTO ontology_schema_versions (version, applied_at) VALUES (?, ?)",
                    (version, _ts(datetime.now(timezone.utc))),
                )

    @staticmethod
    def _event(
        connection: sqlite3.Connection, scope: Scope, *, kind: str, object_id: str,
        event_type: str, revision: int, auth: AuthContext, now: str, reason: str | None = None,
    ) -> None:
        connection.execute(
            "INSERT INTO ontology_events (tenant_id, namespace, object_kind, object_id,"
            " event_type, revision, principal_id, reason, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (scope.tenant_id, scope.namespace, kind, object_id, event_type, revision,
             auth.principal_id, reason, now),
        )

    # -- evidence liveness ---------------------------------------------------

    @staticmethod
    def _has_chunks_table(connection: sqlite3.Connection) -> bool:
        return connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'chunks'"
        ).fetchone() is not None

    def _valid_evidence_sql(self, connection: sqlite3.Connection) -> str:
        """``EXISTS`` clause over alias ``r``: one valid row whose chunk is still live.

        Without an NCP ``chunks`` table no chunk can be live, so chunk evidence
        never counts (fail closed), matching the assert-time rule.
        """
        chunk_ok = "e.evidence_type != 'chunk'"
        if self._has_chunks_table(connection):
            live = _LIVE_CHUNK.format(namespace="r.namespace")
            chunk_ok = (
                "e.evidence_type != 'chunk' OR EXISTS (SELECT 1 FROM chunks c"
                f" WHERE c.chunk_id = e.evidence_id AND {live})"
            )
        return (
            "EXISTS (SELECT 1 FROM ontology_relation_evidence e"
            f" WHERE e.relation_id = r.relation_id AND e.status = 'valid' AND ({chunk_ok}))"
        )

    def _chunk_live(
        self, connection: sqlite3.Connection, chunk_id: str, namespace: str, now: datetime,
    ) -> bool:
        if not self._has_chunks_table(connection):
            return False  # nothing to reconcile against: fail closed
        live = _LIVE_CHUNK.format(namespace=":namespace")
        return self._row(
            connection, f"SELECT 1 FROM chunks c WHERE c.chunk_id = :chunk_id AND {live}",
            {"chunk_id": chunk_id, "namespace": namespace, "now_epoch": now.timestamp()},
        ) is not None

    # -- loading -------------------------------------------------------------

    def _load_entity(
        self, connection: sqlite3.Connection, entity_id: UUID | str, auth: AuthContext,
    ) -> Entity | None:
        row = self._row(
            connection,
            "SELECT * FROM ontology_entities WHERE entity_id = ? AND tenant_id = ?"
            " AND namespace IN (?, ?)",
            (str(entity_id), auth.scope.tenant_id, auth.scope.namespace, GLOBAL_NAMESPACE),
        )
        if row is None:
            return None
        aliases = [alias for (alias,) in self._plain(connection).execute(
            "SELECT alias FROM ontology_aliases WHERE entity_id = ? ORDER BY alias",
            (row["entity_id"],),
        )]
        return Entity.model_validate({
            "scope": {"tenant_id": row["tenant_id"], "namespace": row["namespace"]},
            "entity_type": row["entity_type"],
            "canonical_key": row["canonical_key"],
            "display_name": row["display_name"],
            "aliases": aliases,
            "attributes": json.loads(row["attributes"]),
            "schema_pack": row["schema_pack"],
            "schema_version": row["schema_version"],
            "revision": row["revision"],
            "created_at": _dt(row["created_at"]),
            "updated_at": _dt(row["updated_at"]),
            "deleted_at": _dt(row["deleted_at"]),
        })

    def _relation_from_row(self, connection: sqlite3.Connection, row: sqlite3.Row) -> Relation:
        evidence = [
            EvidenceRef(
                evidence_type=item["evidence_type"], evidence_id=item["evidence_id"],
                source_revision=item["source_revision"],
                observed_at=_dt(item["observed_at"]),  # type: ignore[arg-type]
            )
            for item in self._rows(
                connection,
                "SELECT * FROM ontology_relation_evidence WHERE relation_id = ?"
                " AND status = 'valid' ORDER BY evidence_type, evidence_id, source_revision",
                (row["relation_id"],),
            )
        ]
        return Relation.model_validate({
            "relation_id": row["relation_id"],
            "scope": {"tenant_id": row["tenant_id"], "namespace": row["namespace"]},
            "src_entity_id": row["src_entity_id"],
            "dst_entity_id": row["dst_entity_id"],
            "relation_type": row["relation_type"],
            "status": row["status"],
            "confidence": float(row["confidence"]),
            "valid_from": _dt(row["valid_from"]),
            "valid_until": _dt(row["valid_until"]),
            "observed_at": _dt(row["observed_at"]),
            "created_by": row["created_by"],
            "revision": row["revision"],
            "schema_pack": row["schema_pack"],
            "schema_version": row["schema_version"],
            "attributes": json.loads(row["attributes"]),
            "evidence": evidence,
        })

    def _load_relation(
        self, connection: sqlite3.Connection, relation_id: UUID | str, auth: AuthContext,
    ) -> Relation | None:
        row = self._row(
            connection,
            "SELECT * FROM ontology_relations WHERE relation_id = ? AND tenant_id = ?"
            " AND namespace IN (?, ?)",
            (str(relation_id), auth.scope.tenant_id, auth.scope.namespace, GLOBAL_NAMESPACE),
        )
        return None if row is None else self._relation_from_row(connection, row)

    # -- OntologyStore -------------------------------------------------------

    def upsert_entity(
        self, entity: Entity, *, auth: AuthContext, expected_version: int | None = None,
    ) -> Entity:
        """Create or overwrite ``entity``; the store owns revision and timestamps.

        ``expected_version`` is the revision the caller last saw (0 means
        "must not exist yet"); a mismatch raises ``OntologyConflictError``.
        """
        _require_write(auth, entity.scope)
        self.registry.validate_entity(entity)
        if expected_version is not None and expected_version < 0:
            raise ValueError("expected_version must be >= 0")
        entity_id = str(entity.entity_id)
        now = datetime.now(timezone.utc)
        stamp = _ts(now)
        with self._transaction(write=True) as connection:
            row = self._row(
                connection,
                "SELECT revision, created_at FROM ontology_entities WHERE entity_id = ?",
                (entity_id,),
            )
            current = 0 if row is None else int(row["revision"])
            if expected_version is not None and expected_version != current:
                raise OntologyConflictError(
                    f"entity revision conflict: expected {expected_version}, found {current}"
                )
            revision = current + 1
            created = stamp if row is None else str(row["created_at"])
            values = (
                entity.schema_version, entity.display_name,
                json.dumps(entity.attributes, sort_keys=True), revision, stamp,
                None if entity.deleted_at is None else _ts(entity.deleted_at),
            )
            if row is None:
                connection.execute(
                    "INSERT INTO ontology_entities (entity_id, tenant_id, namespace, schema_pack,"
                    " entity_type, canonical_key, created_at, schema_version, display_name,"
                    " attributes, revision, updated_at, deleted_at)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (entity_id, entity.scope.tenant_id, entity.scope.namespace,
                     entity.schema_pack, entity.entity_type, entity.canonical_key, created,
                     *values),
                )
            else:
                connection.execute(
                    "UPDATE ontology_entities SET schema_version = ?, display_name = ?,"
                    " attributes = ?, revision = ?, updated_at = ?, deleted_at = ?"
                    " WHERE entity_id = ?",
                    (*values, entity_id),
                )
            connection.execute("DELETE FROM ontology_aliases WHERE entity_id = ?", (entity_id,))
            connection.executemany(
                "INSERT INTO ontology_aliases (tenant_id, namespace, entity_type, alias, entity_id)"
                " VALUES (?, ?, ?, ?, ?)",
                [(entity.scope.tenant_id, entity.scope.namespace, entity.entity_type, alias,
                  entity_id) for alias in entity.aliases],
            )
            self._event(
                connection, entity.scope, kind="entity", object_id=entity_id,
                event_type="created" if row is None else "updated", revision=revision,
                auth=auth, now=stamp,
            )
        return entity.model_copy(update={
            "revision": revision, "created_at": _dt(created), "updated_at": _dt(stamp),
        })

    def get_entity(self, entity_id: UUID, *, auth: AuthContext) -> Entity | None:
        _require(auth, "read")
        with self._transaction(write=False) as connection:
            return self._load_entity(connection, entity_id, auth)

    def assert_relation(
        self, relation: Relation, *, auth: AuthContext, idempotency_key: str,
    ) -> Relation:
        """Validate and persist ``relation`` with its evidence in one transaction.

        Re-asserting an existing ``relation_id`` updates its mutable fields and
        adds evidence (revision + 1). A replayed ``idempotency_key`` returns
        the relation's current state when the payload matches, and raises
        ``OntologyConflictError`` when it does not.
        """
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
        stamp = _ts(now)
        with self._transaction(write=True) as connection:
            request = self._row(
                connection,
                "SELECT fingerprint, relation_id FROM ontology_write_requests"
                " WHERE tenant_id = ? AND namespace = ? AND idempotency_key = ?",
                (tenant, namespace, key),
            )
            if request is not None:
                if request["fingerprint"] != fingerprint:
                    raise OntologyConflictError(
                        "idempotency_key was already used with a different payload"
                    )
                stored = self._load_relation(connection, request["relation_id"], auth)
                assert stored is not None  # same scope as the request row
                return stored

            source = self._load_entity(connection, relation.src_entity_id, auth)
            target = self._load_entity(connection, relation.dst_entity_id, auth)
            if source is None or target is None:
                raise OntologyNotFoundError("relation endpoints are not available")
            self.registry.validate_relation(relation, source=source, target=target)
            definition = self.registry.relation_definition(relation)

            existing = self._row(
                connection, "SELECT * FROM ontology_relations WHERE relation_id = ?",
                (relation_id,),
            )
            mutable = (
                relation.status, relation.confidence, _ts(relation.valid_from),
                None if relation.valid_until is None else _ts(relation.valid_until),
                _ts(relation.observed_at), relation.schema_version,
                json.dumps(relation.attributes, sort_keys=True), stamp,
            )
            if existing is None:
                revision = 1
                connection.execute(
                    "INSERT INTO ontology_relations (relation_id, tenant_id, namespace,"
                    " src_entity_id, dst_entity_id, relation_type, schema_pack, created_by,"
                    " asserted_by, created_at, revision, status, confidence, valid_from,"
                    " valid_until, observed_at, schema_version, attributes, updated_at)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (relation_id, tenant, namespace, str(relation.src_entity_id),
                     str(relation.dst_entity_id), relation.relation_type, relation.schema_pack,
                     relation.created_by, auth.principal_id, stamp, revision, *mutable),
                )
            else:
                self._check_update(existing, relation, auth)
                revision = int(existing["revision"]) + 1
                connection.execute(
                    "UPDATE ontology_relations SET status = ?, confidence = ?, valid_from = ?,"
                    " valid_until = ?, observed_at = ?, schema_version = ?, attributes = ?,"
                    " updated_at = ?, revision = ? WHERE relation_id = ?",
                    (*mutable, revision, relation_id),
                )
            self._check_cardinality(connection, relation, definition, stamp)
            if not definition.allow_cycles:
                self._check_acyclic(connection, relation, stamp)

            for ref in relation.evidence:
                if ref.evidence_type == "chunk" and not self._chunk_live(
                    connection, ref.evidence_id, namespace, now,
                ):
                    raise ValueError("evidence chunk is not available")
                connection.execute(
                    "INSERT OR IGNORE INTO ontology_relation_evidence (relation_id, evidence_type,"
                    " evidence_id, source_revision, observed_at) VALUES (?, ?, ?, ?, ?)",
                    (relation_id, ref.evidence_type, ref.evidence_id, ref.source_revision,
                     _ts(ref.observed_at)),
                )
            if relation.status == "verified" and self._row(
                connection,
                "SELECT 1 FROM ontology_relations r WHERE r.relation_id = :relation_id"
                f" AND {self._valid_evidence_sql(connection)}",
                {"relation_id": relation_id, "now_epoch": now.timestamp()},
            ) is None:
                raise ValueError("verified relations require valid, live evidence")

            connection.execute(
                "INSERT INTO ontology_write_requests (tenant_id, namespace, idempotency_key,"
                " fingerprint, relation_id, principal_id, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?)",
                (tenant, namespace, key, fingerprint, relation_id, auth.principal_id, stamp),
            )
            self._event(
                connection, relation.scope, kind="relation", object_id=relation_id,
                event_type="asserted" if existing is None else "updated", revision=revision,
                auth=auth, now=stamp,
            )
            stored = self._load_relation(connection, relation_id, auth)
            assert stored is not None
            return stored

    @staticmethod
    def _check_update(existing: sqlite3.Row, relation: Relation, auth: AuthContext) -> None:
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
        if existing["status"] == "verified":
            _require(auth, "verify")  # changing a verified fact is a verification act

    def _check_cardinality(
        self, connection: sqlite3.Connection, relation: Relation, definition: RelationType,
        now: str,
    ) -> None:
        # Counts stay inside the relation's own namespace: incoming edges to a
        # global entity from other pipelines must not be observable here.
        for column, limit, label in (
            ("src_entity_id", definition.max_outgoing, "max_outgoing"),
            ("dst_entity_id", definition.max_incoming, "max_incoming"),
        ):
            if limit is None:
                continue
            endpoint = relation.src_entity_id if column == "src_entity_id" else relation.dst_entity_id
            row = self._row(
                connection,
                "SELECT COUNT(*) AS n FROM ontology_relations r WHERE r.tenant_id = :tenant"
                f" AND r.namespace = :namespace AND r.{column} = :endpoint"
                f" AND r.relation_type = :relation_type AND {_ACTIVE}",
                {"tenant": relation.scope.tenant_id, "namespace": relation.scope.namespace,
                 "endpoint": str(endpoint), "relation_type": relation.relation_type, "now": now},
            )
            assert row is not None
            if int(row["n"]) > limit:
                raise ValueError(f"relation type {relation.relation_type} exceeds {label}")

    def _check_acyclic(self, connection: sqlite3.Connection, relation: Relation, now: str) -> None:
        # Pipeline relations can only re-enter a pipeline entity through
        # pipeline relations (global relations target global entities), so
        # the relation's own namespace bounds the search.
        row = self._row(
            connection,
            "WITH RECURSIVE reach(entity_id) AS (SELECT :start UNION"
            " SELECT r.dst_entity_id FROM ontology_relations r"
            " JOIN reach ON r.src_entity_id = reach.entity_id"
            " WHERE r.tenant_id = :tenant AND r.namespace = :namespace"
            f" AND r.relation_type = :relation_type AND {_ACTIVE})"
            " SELECT 1 FROM reach WHERE entity_id = :goal LIMIT 1",
            {"start": str(relation.dst_entity_id), "goal": str(relation.src_entity_id),
             "tenant": relation.scope.tenant_id, "namespace": relation.scope.namespace,
             "relation_type": relation.relation_type, "now": now},
        )
        if row is not None:
            raise ValueError("relation type does not allow cycles")

    def traverse(self, request: GraphQuery, *, auth: AuthContext) -> GraphResult:
        """Breadth-first expansion over visible, active relations in both directions.

        Each reached relation yields one ``GraphPath``: the BFS-tree chain from
        a seed to the relation's far endpoint. ``nodes[i]`` and ``nodes[i+1]``
        are joined by ``relations[i]``, which may point either way. Edges at
        each node are ordered by (relation_type, relation_id).
        """
        _require(auth, "read")
        now = datetime.now(timezone.utc)
        warnings: list[str] = []
        paths: list[GraphPath] = []
        with self._transaction(write=False) as connection:
            seeds = self._visible_seeds(connection, request.seeds, auth)
            if len(seeds) > request.max_nodes:
                seeds = seeds[:request.max_nodes]
                warnings.append(f"truncated: seeds exceed max_nodes={request.max_nodes}")
            edge_sql, params = self._edge_query(connection, request, auth, now)
            chains: dict[str, tuple[tuple[UUID, ...], tuple[Relation, ...]]] = {
                seed: ((UUID(seed),), ()) for seed in seeds
            }
            seen_edges: set[str] = set()
            frontier = list(seeds)
            truncated = False
            for _ in range(request.max_hops):
                next_frontier: list[str] = []
                for node in frontier:
                    for row in self._rows(connection, edge_sql, {**params, "node": node}):
                        if row["relation_id"] in seen_edges:
                            continue
                        if len(seen_edges) >= request.max_edges:
                            warnings.append(f"truncated: max_edges={request.max_edges} reached")
                            truncated = True
                            break
                        other = (
                            row["dst_entity_id"] if row["src_entity_id"] == node
                            else row["src_entity_id"]
                        )
                        if other not in chains and len(chains) >= request.max_nodes:
                            if not any("max_nodes" in item for item in warnings):
                                warnings.append(
                                    f"truncated: max_nodes={request.max_nodes} reached"
                                )
                            continue
                        seen_edges.add(row["relation_id"])
                        nodes, relations = chains[node]
                        chain = (
                            nodes + (UUID(other),),
                            relations + (self._relation_from_row(connection, row),),
                        )
                        paths.append(GraphPath(nodes=chain[0], relations=chain[1]))
                        if other not in chains:
                            chains[other] = chain
                            next_frontier.append(other)
                    if truncated:
                        break
                frontier = next_frontier
                if truncated or not frontier:
                    break
            else:
                if frontier and any(
                    row["relation_id"] not in seen_edges
                    for node in frontier
                    for row in self._rows(connection, edge_sql, {**params, "node": node})
                ):
                    warnings.append(f"truncated: max_hops={request.max_hops} reached")
        return GraphResult(paths=tuple(paths), warnings=tuple(warnings))

    def _visible_seeds(
        self, connection: sqlite3.Connection, seeds: Sequence[UUID], auth: AuthContext,
    ) -> list[str]:
        requested = list(dict.fromkeys(str(seed) for seed in seeds))
        if not requested:
            return []
        marks = ",".join("?" * len(requested))
        visible = {entity_id for (entity_id,) in self._plain(connection).execute(
            f"SELECT entity_id FROM ontology_entities WHERE entity_id IN ({marks})"
            " AND tenant_id = ? AND namespace IN (?, ?) AND deleted_at IS NULL",
            (*requested, auth.scope.tenant_id, auth.scope.namespace, GLOBAL_NAMESPACE),
        )}
        return [seed for seed in requested if seed in visible]

    def _edge_query(
        self, connection: sqlite3.Connection, request: GraphQuery, auth: AuthContext,
        now: datetime,
    ) -> tuple[str, dict[str, Any]]:
        params: dict[str, Any] = {
            "tenant": auth.scope.tenant_id, "namespace": auth.scope.namespace,
            "global": GLOBAL_NAMESPACE, "now": _ts(now), "now_epoch": now.timestamp(),
        }
        def visible(alias: str) -> str:
            return f"{alias}.tenant_id = :tenant AND {alias}.namespace IN (:namespace, :global)"

        clauses = [
            visible("r"), visible("s"), visible("d"),
            "s.deleted_at IS NULL", "d.deleted_at IS NULL", _ACTIVE,
        ]
        if request.relation_types:
            names = [f":type{index}" for index in range(len(request.relation_types))]
            clauses.append(f"r.relation_type IN ({', '.join(names)})")
            params.update({name[1:]: value for name, value in zip(names, request.relation_types)})
        if request.verified_only:
            clauses.append(f"r.status = 'verified' AND {self._valid_evidence_sql(connection)}")
        sql = (
            "SELECT r.* FROM ontology_relations r"
            " JOIN ontology_entities s ON s.entity_id = r.src_entity_id"
            " JOIN ontology_entities d ON d.entity_id = r.dst_entity_id"
            " WHERE r.relation_id IN ("
            f"SELECT x.relation_id FROM ontology_relations x WHERE {visible('x')}"
            " AND x.src_entity_id = :node"
            f" UNION SELECT y.relation_id FROM ontology_relations y WHERE {visible('y')}"
            " AND y.dst_entity_id = :node)"
            f" AND {' AND '.join(clauses)}"
            " ORDER BY r.relation_type, r.relation_id"
        )
        return sql, params

    # -- scoped read helpers (facade) ----------------------------------------

    def get_entities(
        self, entity_ids: Sequence[UUID | str], *, auth: AuthContext,
    ) -> dict[str, Entity]:
        """Visible entities by id, in one read transaction; invisible ids are absent."""
        _require(auth, "read")
        found: dict[str, Entity] = {}
        with self._transaction(write=False) as connection:
            for entity_id in dict.fromkeys(str(item) for item in entity_ids):
                entity = self._load_entity(connection, entity_id, auth)
                if entity is not None:
                    found[entity_id] = entity
        return found

    def resolve_entities(
        self, key: str, *, auth: AuthContext, entity_type: str | None = None,
        schema_pack: str | None = None, limit: int = 20,
    ) -> tuple[EntityMatch, str]:
        """Exact lookup of ``key`` among visible, live entities.

        Canonical-key matches win; aliases are consulted only when no
        canonical key matches. Returns the match (``entity_ids`` sorted, at most
        ``limit + 1`` so callers can tell truncation) and which column matched
        (``"canonical_key"``, ``"alias"`` or ``"none"``). Never picks a winner
        among several matches.
        """
        _require(auth, "read")
        normalized = normalize_key(key)
        if not 1 <= len(normalized) <= 256:
            raise ValueError("lookup key must contain 1 to 256 characters")
        params: dict[str, Any] = {
            "tenant": auth.scope.tenant_id, "namespace": auth.scope.namespace,
            "global": GLOBAL_NAMESPACE, "key": normalized, "limit": limit + 1,
        }
        filters = ""
        if entity_type is not None:
            filters += " AND e.entity_type = :entity_type"
            params["entity_type"] = entity_type
        if schema_pack is not None:
            filters += " AND e.schema_pack = :schema_pack"
            params["schema_pack"] = schema_pack
        visible = (
            "e.tenant_id = :tenant AND e.namespace IN (:namespace, :global)"
            " AND e.deleted_at IS NULL" + filters
        )
        with self._transaction(write=False) as connection:
            for column, sql in (
                ("canonical_key",
                 f"SELECT e.entity_id FROM ontology_entities e WHERE {visible}"
                 " AND e.canonical_key = :key ORDER BY e.entity_id LIMIT :limit"),
                ("alias",
                 "SELECT DISTINCT e.entity_id FROM ontology_aliases a"
                 " JOIN ontology_entities e ON e.entity_id = a.entity_id"
                 f" WHERE {visible} AND a.alias = :key ORDER BY e.entity_id LIMIT :limit"),
            ):
                ids = [UUID(entity_id) for (entity_id,) in self._plain(connection).execute(
                    sql, params,
                )]
                if ids:
                    return EntityMatch(entity_ids=tuple(ids), matched_key=normalized), column
        return EntityMatch(entity_ids=(), matched_key=normalized), "none"

    def explain_relation(
        self, relation_id: UUID, *, auth: AuthContext,
    ) -> tuple[Relation, list[dict[str, Any]]] | None:
        """A visible relation plus every evidence row (valid or not) with liveness.

        ``None`` when the relation is missing or outside the caller's scope.
        Each evidence row carries ``status`` (``valid``/``invalidated``) and
        ``live``: valid, and for chunk evidence the chunk is still live.
        """
        _require(auth, "read")
        now = datetime.now(timezone.utc)
        with self._transaction(write=False) as connection:
            relation = self._load_relation(connection, relation_id, auth)
            if relation is None:
                return None
            evidence: list[dict[str, Any]] = []
            for row in self._rows(
                connection,
                "SELECT * FROM ontology_relation_evidence WHERE relation_id = ?"
                " ORDER BY evidence_type, evidence_id, source_revision",
                (str(relation_id),),
            ):
                live = row["status"] == "valid" and (
                    row["evidence_type"] != "chunk"
                    or self._chunk_live(
                        connection, row["evidence_id"], relation.scope.namespace, now,
                    )
                )
                evidence.append({
                    "evidence_type": row["evidence_type"],
                    "evidence_id": row["evidence_id"],
                    "source_revision": row["source_revision"],
                    "observed_at": row["observed_at"],
                    "status": row["status"],
                    "invalidated_reason": row["invalidated_reason"],
                    "invalidated_at": row["invalidated_at"],
                    "live": live,
                })
            return relation, evidence

    def invalidate_evidence(
        self, chunk_ids: Sequence[str], *, auth: AuthContext, reason: str,
    ) -> int:
        """Invalidate chunk evidence in the caller's scope; returns relations touched.

        A verified relation left without valid evidence is demoted to
        ``candidate`` (revision + 1). Repeating the call changes nothing and
        returns 0.
        """
        _require_write(auth, auth.scope)
        if not reason.strip():
            raise ValueError("reason is required")
        ids = sorted({str(chunk_id) for chunk_id in chunk_ids})
        stamp = _ts(datetime.now(timezone.utc))
        scope = (auth.scope.tenant_id, auth.scope.namespace)
        affected: set[str] = set()
        with self._transaction(write=True) as connection:
            for start in range(0, len(ids), _IN_BATCH):
                batch = ids[start:start + _IN_BATCH]
                marks = ",".join("?" * len(batch))
                where = (
                    f"evidence_type = 'chunk' AND status = 'valid' AND evidence_id IN ({marks})"
                    " AND relation_id IN (SELECT relation_id FROM ontology_relations"
                    " WHERE tenant_id = ? AND namespace = ?)"
                )
                affected.update(relation_id for (relation_id,) in self._plain(connection).execute(
                    f"SELECT DISTINCT relation_id FROM ontology_relation_evidence WHERE {where}",
                    (*batch, *scope),
                ))
                connection.execute(
                    "UPDATE ontology_relation_evidence SET status = 'invalidated',"
                    f" invalidated_reason = ?, invalidated_at = ? WHERE {where}",
                    (reason, stamp, *batch, *scope),
                )
            for relation_id in sorted(affected):
                row = self._row(
                    connection,
                    "SELECT status, revision, (SELECT COUNT(*) FROM ontology_relation_evidence e"
                    " WHERE e.relation_id = r.relation_id AND e.status = 'valid') AS valid"
                    " FROM ontology_relations r WHERE relation_id = ?",
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
                    connection.execute(
                        "UPDATE ontology_relations SET status = 'candidate', revision = ?,"
                        " updated_at = ? WHERE relation_id = ?",
                        (revision, stamp, relation_id),
                    )
                self._event(
                    connection, auth.scope, kind="relation", object_id=relation_id,
                    event_type=event_type, revision=revision, auth=auth, now=stamp, reason=reason,
                )
        return len(affected)

    def revoke_relation(self, relation_id: UUID, *, auth: AuthContext, reason: str) -> Relation:
        """Mark a relation revoked (idempotent). Revoking another principal's needs admin."""
        _require_write(auth, auth.scope)  # before the lookup: no existence oracle
        if not reason.strip():
            raise ValueError("reason is required")
        stamp = _ts(datetime.now(timezone.utc))
        with self._transaction(write=True) as connection:
            row = self._row(
                connection,
                "SELECT * FROM ontology_relations WHERE relation_id = ? AND tenant_id = ?"
                " AND namespace = ?",
                (str(relation_id), auth.scope.tenant_id, auth.scope.namespace),
            )
            if row is None:
                raise OntologyNotFoundError("relation is not available")
            if row["asserted_by"] != auth.principal_id:
                _require(auth, "admin")
            if row["status"] != "revoked":
                revision = int(row["revision"]) + 1
                connection.execute(
                    "UPDATE ontology_relations SET status = 'revoked', revision = ?,"
                    " updated_at = ? WHERE relation_id = ?",
                    (revision, stamp, str(relation_id)),
                )
                self._event(
                    connection, auth.scope, kind="relation", object_id=str(relation_id),
                    event_type="revoked", revision=revision, auth=auth, now=stamp, reason=reason,
                )
            stored = self._load_relation(connection, relation_id, auth)
            assert stored is not None
            return stored
