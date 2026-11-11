"""Ontology PR 5: SQLite <-> PostgreSQL parity, plus Postgres-only guarantees.

One scenario suite runs against both backends (``backend`` fixture). Every
scenario asserts its expected semantics *and* returns a normalized transcript;
``test_backends_agree`` runs each scenario on both stores and requires the
transcripts to be identical (ordered graph results, status transitions,
revisions, idempotency/conflict outcomes, error types and messages).

Postgres cases need ``NCP_PGVECTOR_DSN`` pointing at a reachable server with
pgvector; otherwise they skip. Each Postgres test uses its own schema
(``ncp_ont_<uuid>``) and drops it afterwards.
"""

from __future__ import annotations

import asyncio
import functools
import os
import sqlite3
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

import pytest

from ncp.ontology.facade import OntologyFacade, cached_facade, make_tool_handlers
from ncp.ontology.models import (
    AuthContext, Entity, EvidenceRef, GraphQuery, GraphResult, Relation, Scope,
)
from ncp.ontology.policy import OntologyConfig
from ncp.ontology.registry import SchemaPack, SchemaRegistry
from ncp.ontology.stores import (
    AsyncPostgresOntologyStore, OntologyConflictError, OntologyTimeoutError,
    PostgresOntologyStore, SQLiteOntologyStore,
)
from ncp.stores.sqlite import SQLiteStore
from ncp.types import SubconsciousChunk

NOW = (datetime.now(timezone.utc) - timedelta(days=1)).replace(microsecond=123456)
PROD = Scope(tenant_id="acme", namespace="prod")
STAGING = Scope(tenant_id="acme", namespace="staging")
GLOBAL = Scope(tenant_id="acme", namespace="global")
OTHER_TENANT = Scope(tenant_id="other", namespace="prod")
ALL = frozenset({"read", "write", "verify", "admin"})
WRITER = frozenset({"read", "write", "verify"})


# -- Postgres availability -----------------------------------------------------


@functools.cache
def _pg_dsn() -> str | None:
    dsn = os.environ.get("NCP_PGVECTOR_DSN")
    if not dsn:
        return None
    try:
        import psycopg

        with psycopg.connect(dsn, connect_timeout=3) as connection:
            connection.execute("SELECT 1")
    except Exception:
        return None
    return dsn


def _pg_skip_reason() -> str:
    if not os.environ.get("NCP_PGVECTOR_DSN"):
        return "set NCP_PGVECTOR_DSN to run PostgreSQL ontology parity tests"
    return "NCP_PGVECTOR_DSN is set but the server is unreachable"


requires_pg = pytest.mark.skipif(_pg_dsn() is None, reason=_pg_skip_reason())


# -- shared fixtures and helpers -----------------------------------------------


def auth(scope: Scope = PROD, caps: frozenset = WRITER, principal: str = "catalog") -> AuthContext:
    return AuthContext(principal_id=principal, scope=scope, capabilities=caps)


def registry() -> SchemaRegistry:
    reg = SchemaRegistry.builtin()
    reg.register(SchemaPack.model_validate({
        "id": "graph", "version": 1,
        "entity_types": {"Node": {}},
        "relation_types": {
            "LINKS": {"from": ["Node"], "to": ["Node"]},
            "PARENT": {"from": ["Node"], "to": ["Node"], "allow_cycles": False},
            "ONE": {"from": ["Node"], "to": ["Node"], "max_outgoing": 1, "max_incoming": 2},
        },
    }))
    return reg


def service(name: str, scope: Scope = PROD, **extra: Any) -> Entity:
    return Entity(
        scope=scope, entity_type="Service", canonical_key=name, display_name=name,
        aliases=(f"{name}-svc",), attributes={"name": name, "environment": scope.namespace,
                                               "limits": {"cpu": 2.5, "big": 10 ** 20}},
        schema_pack="infrastructure", **extra,
    )


def database(name: str, scope: Scope = PROD) -> Entity:
    return Entity(scope=scope, entity_type="Database", canonical_key=name, display_name=name,
                  attributes={"name": name}, schema_pack="infrastructure")


def node(name: str, scope: Scope = PROD) -> Entity:
    return Entity(scope=scope, entity_type="Node", canonical_key=name, display_name=name,
                  schema_pack="graph")


def rid(name: str) -> UUID:
    return uuid5(NAMESPACE_URL, f"parity:{name}")


def rel(name: str, src: Entity, dst: Entity, relation_type: str = "DEPENDS_ON",
        **overrides: Any) -> Relation:
    return Relation.model_validate({
        "relation_id": rid(name), "scope": src.scope, "src_entity_id": src.entity_id,
        "dst_entity_id": dst.entity_id, "relation_type": relation_type, "confidence": 0.9,
        "valid_from": NOW, "observed_at": NOW, "created_by": "catalog",
        "schema_pack": src.schema_pack, **overrides,
    })


def chunk_ref(chunk_id: str) -> EvidenceRef:
    return EvidenceRef(evidence_type="chunk", evidence_id=chunk_id, source_revision="1",
                       observed_at=NOW)


def attestation(name: str = "cmdb") -> EvidenceRef:
    return EvidenceRef(evidence_type="external_attestation", evidence_id=name,
                       source_revision="1", observed_at=NOW)


class Backend:
    """One NCP memory store plus its ontology store, with raw-SQL helpers.

    ``sql`` takes qmark (``?``) placeholders and ``{t:name}`` / ``{chunks}``
    table markers so scenarios stay backend-neutral.
    """

    name: str
    ncp: Any
    store: SQLiteOntologyStore | PostgresOntologyStore

    def table(self, logical: str) -> str:
        raise NotImplementedError

    def sql(self, statement: str, params: tuple = ()) -> list[tuple]:
        raise NotImplementedError

    def fresh_store(self) -> SQLiteOntologyStore | PostgresOntologyStore:
        """A second ontology store instance over the same database."""
        raise NotImplementedError

    def bare_store(self) -> SQLiteOntologyStore | PostgresOntologyStore:
        """An ontology store in a database/schema with no NCP chunks table."""
        raise NotImplementedError

    def _render(self, statement: str) -> str:
        out = statement.replace("{chunks}", self.table("chunks"))
        while "{t:" in out:
            start = out.index("{t:")
            end = out.index("}", start)
            out = out[:start] + self.table("ontology_" + out[start + 3:end]) + out[end + 1:]
        return out

    def count(self, logical: str, where: str = "", params: tuple = ()) -> int:
        clause = f" WHERE {where}" if where else ""
        return int(self.sql(f"SELECT COUNT(*) FROM {{t:{logical}}}{clause}", params)[0][0])

    def chunk(self, name: str, content: str, pipeline: str | None = "prod", **extra: Any) -> str:
        chunk = SubconsciousChunk(chunk_id=f"sub_{name}", layer="semantic", content=content,
                                  src="tool_result", pipeline_id=pipeline, **extra)
        assert self.ncp.write(chunk)
        return chunk.chunk_id

    def events(self) -> list[tuple]:
        return [tuple(row) for row in self.sql(
            "SELECT object_kind, object_id, event_type, revision, principal_id, reason"
            " FROM {t:events} ORDER BY event_id"
        )]


class SQLiteBackend(Backend):
    name = "sqlite"

    def __init__(self, root: Path) -> None:
        self.root = root
        self.path = root / "ncp.db"
        self.ncp = SQLiteStore(self.path)
        self.store = SQLiteOntologyStore.inherit(self.ncp, registry())

    def table(self, logical: str) -> str:
        return logical

    def sql(self, statement: str, params: tuple = ()) -> list[tuple]:
        with sqlite3.connect(self.path) as connection:
            return connection.execute(self._render(statement), params).fetchall()

    def fresh_store(self) -> SQLiteOntologyStore:
        return SQLiteOntologyStore(self.path, self.store.registry)

    def bare_store(self) -> SQLiteOntologyStore:
        return SQLiteOntologyStore(self.root / "bare.db", registry())

    def close(self) -> None:
        pass


class PostgresBackend(Backend):
    name = "postgres"

    def __init__(self, dsn: str) -> None:
        from ncp.stores.pgvector import PgvectorStore

        self.dsn = dsn
        self.schema = f"ncp_ont_{uuid4().hex[:12]}"
        self.bare_schema = self.schema + "_bare"
        self._owned: list[PostgresOntologyStore] = []
        self.ncp = PgvectorStore(dsn, schema=self.schema, table_prefix="it_",
                                 max_pool_connections=16)
        self.store = PostgresOntologyStore.inherit(self.ncp, registry())

    def table(self, logical: str) -> str:
        return f"{self.schema}.it_{logical}"

    def sql(self, statement: str, params: tuple = ()) -> list[tuple]:
        import psycopg

        with psycopg.connect(self.dsn, autocommit=True) as connection:
            cursor = connection.execute(self._render(statement).replace("?", "%s"), params)
            return cursor.fetchall() if cursor.description is not None else []

    def fresh_store(self) -> PostgresOntologyStore:
        store = PostgresOntologyStore(self.dsn, self.store.registry, schema=self.schema,
                                      table_prefix="it_")
        self._owned.append(store)
        return store

    def bare_store(self) -> PostgresOntologyStore:
        store = PostgresOntologyStore(self.dsn, registry(), schema=self.bare_schema,
                                      table_prefix="it_")
        self._owned.append(store)
        return store

    def close(self) -> None:
        import psycopg

        for store in self._owned:
            store.close()
        self.ncp.close()
        with psycopg.connect(self.dsn, autocommit=True) as connection:
            for schema in (self.schema, self.bare_schema):
                connection.execute(f"DROP SCHEMA IF EXISTS {schema} CASCADE")




FUTURE = NOW + timedelta(days=3)  # fixed instant ~2 days ahead: deterministic across backends


def _epoch(delta: timedelta) -> float:
    return (datetime.now(timezone.utc) + delta).timestamp()


def norm(value: Any) -> Any:
    """Backend-neutral, comparable form (store-owned timestamps dropped)."""
    if isinstance(value, GraphResult):
        return {
            "paths": [[[str(item) for item in path.nodes], [norm(r) for r in path.relations]]
                      for path in value.paths],
            "warnings": list(value.warnings),
        }
    if isinstance(value, Relation):
        return value.model_dump(mode="json")
    if isinstance(value, Entity):
        return value.model_dump(mode="json", exclude={"created_at", "updated_at"})
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, (set, frozenset)):
        return sorted(norm(item) for item in value)
    if isinstance(value, dict):
        return {str(key): ("<stamp>" if key == "invalidated_at" and item else norm(item))
                for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [norm(item) for item in value]
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    return value


def raised(outcome: Any) -> bool:
    return isinstance(outcome, list) and bool(outcome) and outcome[0] == "raised"


def attempt(call: Callable[[], Any]) -> Any:
    try:
        return norm(call())
    except Exception as exc:  # the outcome (type + message) is the parity signal
        return ["raised", type(exc).__name__, str(exc)]


def put(store: Any, *entities: Entity) -> None:
    for entity in entities:
        store.upsert_entity(entity, auth=auth(entity.scope, ALL))


def all_paths(store: Any, seed: Entity, scope: Scope = PROD, **query: Any) -> GraphResult:
    return store.traverse(GraphQuery(seeds=(seed.entity_id,), verified_only=False, **query),
                          auth=auth(scope, ALL))


def verified(store: Any, seed: Entity, scope: Scope = PROD) -> GraphResult:
    return store.traverse(GraphQuery(seeds=(seed.entity_id,)), auth=auth(scope))


# -- shared scenarios ------------------------------------------------------------
# Each returns a transcript; assertions pin the semantics on every backend.


def sc_entities(b: Backend) -> list:
    s, out = b.store, []
    api = service("api")
    first = s.upsert_entity(api, auth=auth(), expected_version=0)
    second = s.upsert_entity(api.model_copy(update={"display_name": "API"}), auth=auth(),
                             expected_version=1)
    assert (first.revision, second.revision) == (1, 2)
    assert second.created_at == first.created_at
    out += [norm(first), norm(second)]
    out.append(attempt(lambda: s.upsert_entity(api, auth=auth(), expected_version=1)))
    out.append(attempt(lambda: s.upsert_entity(api, auth=auth(), expected_version=0)))
    out.append(attempt(lambda: s.upsert_entity(api, auth=auth(), expected_version=-1)))
    out.append(attempt(lambda: s.upsert_entity(
        api.model_copy(update={"attributes": {}}), auth=auth())))
    stored = s.get_entity(api.entity_id, auth=auth())
    assert stored is not None and stored.attributes == api.attributes  # nested JSON round-trip
    assert stored.created_at == first.created_at and stored.display_name == "API"
    out.append(norm(stored))

    put(s, service("pay-a").model_copy(update={"aliases": ("pay",)}),
        service("pay-b").model_copy(update={"aliases": ("pay",)}), database("orders"))
    assert b.count("aliases", "alias = ?", ("pay",)) == 2  # never auto-merged
    for key in ("API", "api-svc", "pay", "nothing", "  Orders "):
        out.append(attempt(lambda key=key: s.resolve_entities(key, auth=auth())))
    out.append(attempt(lambda: s.resolve_entities("pay", auth=auth(), limit=1)))
    out.append(attempt(lambda: s.resolve_entities("api", auth=auth(), entity_type="Database")))
    out.append(attempt(lambda: s.resolve_entities("   ", auth=auth())))
    out.append(attempt(lambda: s.resolve_terms(
        ["api", "pay", "pay-a-svc", "missing", "orders", "x" * 300], auth=auth(),
        schema_packs=("infrastructure",))))
    out.append(attempt(lambda: s.resolve_terms(["api"], auth=auth(), schema_packs=("graph",))))
    out.append(attempt(lambda: s.get_entities(
        [api.entity_id, rid("missing"), service("api", STAGING).entity_id], auth=auth())))
    out.append(attempt(lambda: s.get_entity(api.entity_id, auth=auth(STAGING))))

    s.upsert_entity(database("orders").model_copy(update={"deleted_at": NOW}), auth=auth())
    out.append(attempt(lambda: s.resolve_entities("orders", auth=auth())))
    out.append(attempt(lambda: s.get_entity(database("orders").entity_id, auth=auth())))
    out.append(b.events())
    return out


def sc_idempotency_and_status(b: Backend) -> list:
    s, out = b.store, []
    api, db, cache = service("api"), database("db"), database("cache")
    put(s, api, db, cache)
    writer = auth(caps=frozenset({"read", "write"}))
    relation = rel("r", api, db)
    first = s.assert_relation(relation, auth=writer, idempotency_key="k1")
    assert (first.revision, first.status) == (1, "candidate")
    assert s.assert_relation(relation, auth=writer, idempotency_key="k1") == first
    out.append(norm(first))
    out.append(attempt(lambda: s.assert_relation(
        relation.model_copy(update={"confidence": 0.5}), auth=writer, idempotency_key="k1")))
    promoted_payload = relation.model_copy(update={"status": "verified",
                                                   "evidence": (attestation(),)})
    out.append(attempt(lambda: s.assert_relation(promoted_payload, auth=writer,
                                                 idempotency_key="k2")))
    promoted = s.assert_relation(promoted_payload, auth=auth(), idempotency_key="k2")
    assert (promoted.revision, promoted.status) == (2, "verified")
    out.append(norm(promoted))
    # Replaying an older key returns the relation's *current* state.
    assert s.assert_relation(relation, auth=writer, idempotency_key="k1") == promoted
    disputed = relation.model_copy(update={"status": "disputed"})
    out.append(attempt(lambda: s.assert_relation(disputed, auth=writer, idempotency_key="k3")))
    out.append(attempt(lambda: s.assert_relation(disputed, auth=auth(), idempotency_key="k3")))
    out.append(attempt(lambda: s.assert_relation(
        relation.model_copy(update={"status": "revoked"}), auth=auth(), idempotency_key="k4")))
    out.append(attempt(lambda: s.assert_relation(
        relation.model_copy(update={"dst_entity_id": cache.entity_id}), auth=auth(),
        idempotency_key="k5")))
    out.append(attempt(lambda: s.assert_relation(rel("ghost", api, database("ghost")),
                                                 auth=auth(), idempotency_key="k6")))
    out.append(attempt(lambda: s.assert_relation(relation, auth=auth(), idempotency_key=" ")))
    out.append(attempt(lambda: s.assert_relation(
        rel("bad", api, cache, "CONTAINS"), auth=auth(), idempotency_key="k7")))
    out.append(attempt(lambda: s.relation_in_scope(rid("r"), auth=auth())))
    out.append(attempt(lambda: s.relation_in_scope(rid("r"), auth=auth(STAGING))))
    assert b.count("relations") == 1 and b.count("write_requests") == 3
    out.append(b.events())
    return out


def sc_traversal_order_and_limits(b: Backend) -> list:
    s, out = b.store, []
    a, c, d, e, f = (node(name) for name in "acdef")
    put(s, a, c, d, e, f)
    edges = [("l0", a, c), ("l1", c, d), ("l2", d, a), ("l3", a, a), ("l4", d, e), ("l5", e, f)]
    for name, src, dst in edges:
        s.assert_relation(rel(name, src, dst, "LINKS"), auth=auth(), idempotency_key=name)
    s.assert_relation(rel("p0", a, d, "PARENT"), auth=auth(), idempotency_key="p0")
    full = all_paths(s, a, max_hops=5)
    assert len(full.paths) == 7 and not full.warnings  # cycles terminate, self-loop once
    assert full == all_paths(s, a, max_hops=5)
    out.append(norm(full))
    for query in ({"max_hops": 1}, {"max_hops": 5, "max_edges": 2},
                  {"max_hops": 5, "max_nodes": 2}, {"relation_types": ("PARENT",)},
                  {"max_hops": 0}):
        out.append(norm(all_paths(s, a, **query)))
    out.append(norm(s.traverse(GraphQuery(seeds=(a.entity_id, c.entity_id, d.entity_id, a.entity_id),
                                          max_nodes=2, verified_only=False), auth=auth())))
    out.append(norm(verified(s, a)))  # all candidates: nothing verified
    out.append(attempt(lambda: s.traverse(GraphQuery(seeds=(a.entity_id,)),
                                          auth=auth(caps=frozenset({"write"})))))
    return out


def sc_traversal_direction_and_type_filters(b: Backend) -> list:
    """Direction / relation-type filters and the batched (one query per hop)
    frontier expansion, including shared edges between frontier nodes."""
    s, out = b.store, []
    a, c, d, e, f = (node(name) for name in "acdef")
    put(s, a, c, d, e, f)
    edges = [("l0", a, c), ("l1", c, d), ("l2", d, a), ("l3", e, c), ("l4", d, d), ("l5", f, d),
             ("l6", c, f)]
    for name, src, dst in edges:
        s.assert_relation(rel(name, src, dst, "LINKS"), auth=auth(), idempotency_key=name)
    s.assert_relation(rel("p0", a, e, "PARENT"), auth=auth(), idempotency_key="p0")
    s.assert_relation(rel("p1", f, a, "PARENT"), auth=auth(), idempotency_key="p1")
    # Two verified edges (external attestation): verified-only + direction.
    for name in ("l0", "l1"):
        s.assert_relation(rel(name, *{"l0": (a, c), "l1": (c, d)}[name], "LINKS",
                              status="verified", evidence=(attestation(),)),
                          auth=auth(), idempotency_key=f"v-{name}")

    def directed(result: GraphResult, direction: str) -> bool:
        for path in result.paths:
            for index, relation in enumerate(path.relations):
                here = path.nodes[index]
                if direction == "out" and relation.src_entity_id != here:
                    return False
                if direction == "in" and relation.dst_entity_id != here:
                    return False
        return True

    for query in (
        {"direction": "out", "max_hops": 3},
        {"direction": "in", "max_hops": 3},
        {"direction": "out", "relation_types": ("LINKS",), "max_hops": 2},
        {"direction": "in", "relation_types": ("PARENT",), "max_hops": 2},
        {"direction": "both", "relation_types": ("LINKS", "PARENT"), "max_hops": 2},
        {"direction": "out", "max_hops": 3, "max_edges": 2},
        {"direction": "in", "max_hops": 3, "max_nodes": 2},
        {"direction": "out", "max_hops": 0},
    ):
        result = all_paths(s, a, **query)
        assert directed(result, query["direction"])
        if "relation_types" in query:
            assert all(r.relation_type in query["relation_types"]
                       for path in result.paths for r in path.relations)
        out.append(norm(result))
    # Several seeds share edges: each relation is still used once.
    multi = s.traverse(GraphQuery(seeds=(a.entity_id, c.entity_id, d.entity_id),
                                  direction="out", max_hops=2, verified_only=False), auth=auth())
    ids = [str(path.relations[-1].relation_id) for path in multi.paths]
    assert len(ids) == len(set(ids))
    out.append(norm(multi))
    verified_out = s.traverse(GraphQuery(seeds=(a.entity_id,), direction="out", max_hops=3),
                              auth=auth())
    verified_in = s.traverse(GraphQuery(seeds=(d.entity_id,), direction="in", max_hops=3),
                             auth=auth())
    assert [len(path.relations) for path in verified_out.paths] == [1, 2]  # a->c->d
    assert [len(path.relations) for path in verified_in.paths] == [1, 2]   # d<-c<-a
    out += [norm(verified_out), norm(verified_in)]
    return out


def sc_cardinality(b: Backend) -> list:
    s, out = b.store, []
    a, c, d, e, g, h = (node(name) for name in "acdegh")
    put(s, a, c, d, e, g, h)
    one = functools.partial(rel, relation_type="ONE")
    out.append(attempt(lambda: s.assert_relation(one("1", a, c), auth=auth(), idempotency_key="1")))
    out.append(attempt(lambda: s.assert_relation(one("2", a, d), auth=auth(), idempotency_key="2")))
    out.append(attempt(lambda: s.assert_relation(one("3", d, c), auth=auth(), idempotency_key="3")))
    out.append(attempt(lambda: s.assert_relation(one("4", e, c), auth=auth(), idempotency_key="4")))
    # Expired, future-dated and revoked relations do not count.
    out.append(attempt(lambda: s.assert_relation(
        one("5", e, g, valid_until=NOW + timedelta(hours=1)), auth=auth(), idempotency_key="5")))
    out.append(attempt(lambda: s.assert_relation(one("6", e, h), auth=auth(), idempotency_key="6")))
    out.append(attempt(lambda: s.assert_relation(
        one("7", g, h, valid_from=FUTURE), auth=auth(), idempotency_key="7")))
    out.append(attempt(lambda: s.assert_relation(one("8", g, d), auth=auth(), idempotency_key="8")))
    s.revoke_relation(rid("1"), auth=auth(), reason="moved")
    out.append(attempt(lambda: s.assert_relation(one("9", a, g), auth=auth(), idempotency_key="9")))
    assert [raised(item) for item in out].count(True) == 2
    # Counts stay inside the namespace: staging edges into a global node are invisible.
    hub = node("hub", GLOBAL)
    put(s, hub)
    staging = [node(f"s{index}", STAGING) for index in range(3)]
    put(s, *staging)
    for index, item in enumerate(staging[:2]):
        s.assert_relation(one(f"s{index}", item, hub), auth=auth(STAGING),
                          idempotency_key=f"x{index}")
    out.append(attempt(lambda: s.assert_relation(one("p", a, hub), auth=auth(),
                                                 idempotency_key="p")))
    out.append(attempt(lambda: s.assert_relation(one("s2", staging[2], hub), auth=auth(STAGING),
                                                 idempotency_key="y")))
    return out


def sc_cycles(b: Backend) -> list:
    s, out = b.store, []
    a, c, d, e, f = (node(name) for name in "acdef")
    put(s, a, c, d, e, f)
    parent = functools.partial(rel, relation_type="PARENT")
    for name, src, dst, extra in (("1", a, c, {}), ("2", c, d, {}), ("3", d, a, {}),
                                  ("4", e, e, {}), ("5", d, e, {}), ("6", a, e, {}),
                                  ("7", e, a, {}),
                                  # an expired edge does not close a cycle
                                  ("8", f, a, {"valid_until": NOW + timedelta(hours=1)}),
                                  ("9", a, f, {})):
        out.append(attempt(lambda name=name, src=src, dst=dst, extra=extra: s.assert_relation(
            parent(name, src, dst, **extra), auth=auth(), idempotency_key=name)))
    out.append(attempt(lambda: s.assert_relation(rel("links", d, a, "LINKS"), auth=auth(),
                                                 idempotency_key="links")))
    assert [raised(item) for item in out] == [
        False, False, True, True, False, False, True, False, False, False,
    ]
    out.append(norm(all_paths(s, a, max_hops=5)))
    return out


def sc_evidence_liveness(b: Backend) -> list:
    s, out = b.store, []
    api = service("api")
    targets = {name: database(name) for name in ("db", "cache", "queue", "shared", "ledger")}
    put(s, api, *targets.values())
    old = b.chunk("old", "api depends on the orders database for checkout writes")
    new = b.chunk("new", "api moved checkout writes to a new ledger service in november")
    gone = b.chunk("gone", "api reads session tokens from the cache cluster")
    expiring = b.chunk("expiring", "api drains the job queue every minute",
                       expiry=_epoch(timedelta(hours=2)))
    shared = b.chunk("shared", "every service reads the shared settings database",
                     pipeline=None, scope="global")
    future = b.chunk("future", "api will adopt the ledger next quarter",
                     valid_from=_epoch(timedelta(days=1)))
    ended = b.chunk("ended", "api used the ledger during the pilot",
                    valid_to=_epoch(-timedelta(hours=1)))
    expired = b.chunk("expired", "api briefly mirrored writes to the ledger",
                      expiry=_epoch(-timedelta(hours=1)))
    foreign = b.chunk("foreign", "staging pipeline note about database failover",
                      pipeline="staging")
    for name, chunk_id in (("db", old), ("cache", gone), ("queue", expiring),
                           ("shared", shared)):
        out.append(attempt(lambda name=name, chunk_id=chunk_id: s.assert_relation(
            rel(name, api, targets[name], status="verified", evidence=(chunk_ref(chunk_id),)),
            auth=auth(), idempotency_key=name)))
    for chunk_id in (future, ended, expired, foreign, "sub_missing"):
        out.append(attempt(lambda chunk_id=chunk_id: s.assert_relation(
            rel("ledger", api, targets["ledger"], status="verified",
                evidence=(chunk_ref(chunk_id),)),
            auth=auth(), idempotency_key=f"ledger-{chunk_id}")))
    assert len(verified(s, api).paths) == 4
    out.append(norm(verified(s, api)))
    everything = [old, new, gone, expiring, shared, future, ended, expired, foreign]
    out.append(attempt(lambda: s.live_chunk_evidence(everything, auth=auth(),
                                                     relation_namespace="prod")))
    out.append(attempt(lambda: s.live_chunk_evidence(everything, auth=auth(STAGING),
                                                     relation_namespace="prod")))
    out.append(attempt(lambda: s.live_chunk_evidence([], auth=auth(),
                                                     relation_namespace="prod")))

    assert b.ncp.supersede(old, new)
    b.ncp.forget(chunk_ids=[gone])
    replacement = b.chunk("replacement", "refunds are batched by the ledger worker nightly")
    # superseded_by alone retires a chunk, even while valid_to is in the future
    assert b.ncp.supersede(expiring, replacement, valid_to=_epoch(timedelta(hours=1)))
    stale = verified(s, api)
    assert [path.nodes[-1] for path in stale.paths] == [targets["shared"].entity_id]
    out.append(norm(stale))
    out.append(len(all_paths(s, api).paths))  # still stored, just not verified-live
    out.append(attempt(lambda: s.explain_relation(rid("db"), auth=auth())))
    out.append(attempt(lambda: s.explain_relation(rid("shared"), auth=auth())))
    out.append(attempt(lambda: s.explain_relation(rid("db"), auth=auth(STAGING))))
    return out


def sc_future_and_validity_window(b: Backend) -> list:
    s, out = b.store, []
    api, db, cache = service("api"), database("db"), database("cache")
    put(s, api, db, cache)
    planned = rel("planned", api, db, valid_from=FUTURE, status="verified",
                  evidence=(attestation(),))
    s.assert_relation(planned, auth=auth(), idempotency_key="planned")
    live = rel("live", api, cache)
    s.assert_relation(live, auth=auth(), idempotency_key="live")
    out.append(norm(all_paths(s, api)))  # the future-dated relation is inactive
    out.append(norm(verified(s, api)))
    explained = s.explain_relation(rid("planned"), auth=auth())
    assert explained is not None and explained[0].valid_from == FUTURE
    out.append(norm(explained))
    later = NOW + timedelta(hours=1)
    mallory = auth(principal="mallory")
    out.append(attempt(lambda: s.assert_relation(  # B1: only the asserter or an admin
        live.model_copy(update={"valid_from": later}), auth=mallory, idempotency_key="m1")))
    out.append(attempt(lambda: s.assert_relation(
        live.model_copy(update={"valid_until": FUTURE}), auth=mallory, idempotency_key="m2")))
    out.append(attempt(lambda: s.assert_relation(  # same window: allowed
        live.model_copy(update={"confidence": 0.4}), auth=mallory, idempotency_key="m3")))
    out.append(attempt(lambda: s.assert_relation(
        live.model_copy(update={"valid_from": later}), auth=auth(principal="root", caps=ALL),
        idempotency_key="admin")))
    out.append(attempt(lambda: s.assert_relation(
        planned.model_copy(update={"valid_from": NOW}), auth=auth(), idempotency_key="owner")))
    out.append(norm(verified(s, api)))  # now active
    return out


def sc_scope_isolation(b: Backend) -> list:
    s, out = b.store, []
    shared_db = database("shared", GLOBAL)
    out.append(attempt(lambda: s.upsert_entity(shared_db, auth=auth(GLOBAL))))
    out.append(attempt(lambda: s.upsert_entity(shared_db, auth=auth(PROD, ALL))))
    out.append(attempt(lambda: s.upsert_entity(service("x"), auth=auth(caps=frozenset({"read"})))))
    out.append(attempt(lambda: s.get_entity(shared_db.entity_id,
                                            auth=auth(caps=frozenset({"write"})))))
    put(s, shared_db)
    out.append(attempt(lambda: s.get_entity(shared_db.entity_id, auth=auth(STAGING))))
    out.append(attempt(lambda: s.get_entity(shared_db.entity_id, auth=auth(OTHER_TENANT))))
    api, staging_api, tenant_api = service("api"), service("api", STAGING), service("api", OTHER_TENANT)
    put(s, api, staging_api, tenant_api, database("db"), database("db", OTHER_TENANT))
    s.assert_relation(rel("prod-db", api, database("db")), auth=auth(), idempotency_key="k")
    s.assert_relation(rel("prod-shared", api, shared_db), auth=auth(), idempotency_key="p")
    s.assert_relation(rel("staging-shared", staging_api, shared_db), auth=auth(STAGING),
                      idempotency_key="k")  # same key, other namespace: independent
    s.assert_relation(rel("tenant", tenant_api, database("db", OTHER_TENANT)),
                      auth=auth(OTHER_TENANT), idempotency_key="k")
    gateway = service("gateway", GLOBAL)
    put(s, gateway)
    out.append(attempt(lambda: s.assert_relation(rel("g", gateway, api), auth=auth(GLOBAL, ALL),
                                                 idempotency_key="g")))
    out.append(attempt(lambda: s.assert_relation(rel("x", api, staging_api), auth=auth(),
                                                 idempotency_key="x")))
    out.append(attempt(lambda: s.assert_relation(rel("g2", gateway, shared_db),
                                                 auth=auth(GLOBAL, ALL), idempotency_key="g2")))
    out.append(attempt(lambda: s.assert_relation(rel("y", staging_api, shared_db), auth=auth(),
                                                 idempotency_key="y")))
    for scope in (PROD, STAGING, OTHER_TENANT, GLOBAL):
        out.append(norm(all_paths(s, api, scope)))
        out.append(norm(all_paths(s, shared_db, scope)))
        out.append(attempt(lambda scope=scope: s.explain_relation(rid("prod-db"),
                                                                  auth=auth(scope))))
        out.append(attempt(lambda scope=scope: s.relation_in_scope(rid("prod-shared"),
                                                                   auth=auth(scope))))
        out.append(attempt(lambda scope=scope: s.resolve_entities("api", auth=auth(scope))))
    return out


def sc_revoke(b: Backend) -> list:
    s, out = b.store, []
    api, db = service("api"), database("db")
    put(s, api, db)
    edge = s.assert_relation(rel("r", api, db, status="verified", evidence=(attestation(),)),
                             auth=auth(), idempotency_key="k")
    out.append(attempt(lambda: s.revoke_relation(edge.relation_id, auth=auth(principal="intruder"),
                                                 reason="x")))
    out.append(attempt(lambda: s.revoke_relation(edge.relation_id, auth=auth(STAGING, ALL),
                                                 reason="x")))
    out.append(attempt(lambda: s.revoke_relation(edge.relation_id, auth=auth(), reason=" ")))
    out.append(attempt(lambda: s.revoke_relation(rid("nope"), auth=auth(GLOBAL, frozenset({"write"})),
                                                 reason="x")))
    # The asserter withdraws its own verified claim without the verify capability.
    revoked = s.revoke_relation(edge.relation_id, auth=auth(caps=frozenset({"write"})),
                                reason="decommissioned")
    assert (revoked.status, revoked.revision) == ("revoked", 2)
    assert s.revoke_relation(edge.relation_id, auth=auth(), reason="again") == revoked
    out.append(norm(revoked))
    out.append(norm(all_paths(s, api)))
    out.append(attempt(lambda: s.assert_relation(
        edge.model_copy(update={"confidence": 0.1}), auth=auth(), idempotency_key="k2")))
    out.append(b.events())
    return out


def sc_invalidate_evidence(b: Backend) -> list:
    s, out = b.store, []
    api, db, cache, queue = service("api"), database("db"), database("cache"), database("queue")
    put(s, api, db, cache, queue)
    first = b.chunk("first", "api writes orders into the primary database")
    second = b.chunk("second", "cache cluster fronts api session reads")
    s.assert_relation(rel("only", api, db, status="verified", evidence=(chunk_ref(first),)),
                      auth=auth(), idempotency_key="a")
    s.assert_relation(rel("backed", api, cache, status="verified",
                          evidence=(chunk_ref(first), chunk_ref(second))),
                      auth=auth(), idempotency_key="b")
    s.assert_relation(rel("candidate", api, queue, evidence=(chunk_ref(first),)), auth=auth(),
                      idempotency_key="c")
    writer = auth(caps=frozenset({"read", "write"}), principal="other")
    out.append(attempt(lambda: s.invalidate_evidence([first], auth=writer, reason="x")))
    assert b.count("relation_evidence", "status = 'invalidated'") == 0  # rolled back
    out.append(attempt(lambda: s.invalidate_evidence([first], auth=auth(), reason=" ")))
    out.append(attempt(lambda: s.invalidate_evidence([first], auth=auth(STAGING),
                                                     reason="wrong scope")))
    out.append(attempt(lambda: s.invalidate_evidence([first, "sub_unknown"], auth=auth(),
                                                     reason="forgotten")))
    out.append(attempt(lambda: s.invalidate_evidence([first], auth=auth(), reason="forgotten")))
    out.append(attempt(lambda: s.invalidate_evidence([], auth=auth(), reason="nothing")))
    out.append(norm(all_paths(s, api)))
    out.append(attempt(lambda: s.explain_relation(rid("only"), auth=auth())))
    assert b.count("events", "event_type = 'demoted'") == 1
    out.append(b.events())
    return out


def sc_fail_closed_without_chunks(b: Backend) -> list:
    out = []
    bare = b.bare_store()
    api, db = service("api"), database("db")
    put(bare, api, db)
    out.append(attempt(lambda: bare.assert_relation(
        rel("r", api, db, status="verified", evidence=(chunk_ref("sub_made_up"),)),
        auth=auth(), idempotency_key="k")))
    out.append(attempt(lambda: bare.assert_relation(
        rel("r", api, db, status="verified", evidence=(attestation(),)), auth=auth(),
        idempotency_key="k2")))
    out.append(attempt(lambda: bare.live_chunk_evidence(["sub_made_up"], auth=auth(),
                                                        relation_namespace="prod")))
    assert len(verified(bare, api).paths) == 1

    # Stored chunk evidence stops counting once the chunks table disappears.
    s = b.store
    put(s, api, db)
    chunk_id = b.chunk("c", "api writes orders into the primary database")
    s.assert_relation(rel("r", api, db, status="verified", evidence=(chunk_ref(chunk_id),)),
                      auth=auth(), idempotency_key="k")
    assert len(verified(s, api).paths) == 1
    b.sql("ALTER TABLE {chunks} RENAME TO " + b.table("chunks_moved").split(".")[-1])
    out.append(norm(verified(s, api)))
    out.append(attempt(lambda: s.explain_relation(rid("r"), auth=auth())))
    return out


def sc_deleted_endpoints_and_expiry(b: Backend) -> list:
    s, out = b.store, []
    api, db, cache = service("api"), database("db"), database("cache")
    put(s, api, db, cache)
    s.assert_relation(rel("expired", api, db, valid_until=NOW + timedelta(hours=1)), auth=auth(),
                      idempotency_key="expired")
    s.assert_relation(rel("live", api, cache), auth=auth(), idempotency_key="live")
    out.append(norm(all_paths(s, api)))
    s.upsert_entity(cache.model_copy(update={"deleted_at": NOW}), auth=auth())
    out.append(norm(all_paths(s, api)))
    out.append(norm(s.traverse(GraphQuery(seeds=(cache.entity_id,), verified_only=False),
                               auth=auth())))
    return out


def sc_nul_lookups_match_nothing(b: Backend) -> list:
    # PostgreSQL text cannot hold NUL; lookups with it simply match nothing,
    # exactly as on SQLite (which has no rows containing it either).
    s, out = b.store, []
    api, db = service("api"), database("db")
    put(s, api, db)
    s.assert_relation(rel("r", api, db), auth=auth(), idempotency_key="k")
    out.append(attempt(lambda: s.resolve_entities("api\x00", auth=auth())))
    out.append(attempt(lambda: s.resolve_terms(["api\x00", "api", "\x00"], auth=auth())))
    out.append(attempt(lambda: s.live_chunk_evidence(["sub_\x00"], auth=auth(),
                                                     relation_namespace="prod")))
    out.append(attempt(lambda: s.invalidate_evidence(["sub_\x00"], auth=auth(), reason="gone")))
    out.append(attempt(lambda: s.assert_relation(
        rel("v", api, db, status="verified", evidence=(chunk_ref("sub_\x00"),)), auth=auth(),
        idempotency_key="v")))
    assert out[1] == {"api": [[str(api.entity_id)], "canonical_key"]}
    return out


SCENARIOS: dict[str, Callable[[Backend], list]] = {
    name.removeprefix("sc_"): value for name, value in dict(globals()).items()
    if name.startswith("sc_")
}


# -- fixtures --------------------------------------------------------------------


@pytest.fixture
def sqlite_backend(tmp_path: Path) -> Iterator[SQLiteBackend]:
    backend = SQLiteBackend(tmp_path)
    yield backend
    backend.close()


@pytest.fixture
def postgres_backend() -> Iterator[PostgresBackend]:
    dsn = _pg_dsn()
    if dsn is None:
        pytest.skip(_pg_skip_reason())
    backend = PostgresBackend(dsn)
    try:
        yield backend
    finally:
        backend.close()


@pytest.fixture(params=["sqlite", pytest.param("postgres", marks=requires_pg)])
def backend(request: pytest.FixtureRequest) -> Backend:
    return request.getfixturevalue(f"{request.param}_backend")


@pytest.mark.parametrize("scenario", sorted(SCENARIOS))
def test_scenario(backend: Backend, scenario: str) -> None:
    SCENARIOS[scenario](backend)


@requires_pg
@pytest.mark.parametrize("scenario", sorted(SCENARIOS))
def test_backends_agree(sqlite_backend: SQLiteBackend, postgres_backend: PostgresBackend,
                        scenario: str) -> None:
    expected = SCENARIOS[scenario](sqlite_backend)
    actual = SCENARIOS[scenario](postgres_backend)
    assert actual == expected


# -- facade parity ---------------------------------------------------------------

OBSERVED = NOW.isoformat()
ENABLED = OntologyConfig(enabled=True)


def _strip_stamps(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: "<stamp>" if key in {"created_at", "updated_at", "invalidated_at"} and item
                else _strip_stamps(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_strip_stamps(item) for item in value]
    return value


def facade_transcript(ncp: Any, principal: Callable[[], Any] = lambda: None) -> list:
    facade = OntologyFacade.from_config(ENABLED, ncp, registry=registry())
    assert facade.enabled
    call = make_tool_handlers(facade, principal)
    out: list = [call["ncp_ontology_capabilities"]({})]
    ids = {}
    for key, entity_type, extra in (("api", "Service", {"aliases": ["gateway"]}),
                                    ("orders", "Database", {}), ("ledger", "Database", {})):
        result = call["ncp_ontology_assert"]({"kind": "entity", "pipeline_id": "p1", "entity": {
            "entity_type": entity_type, "canonical_key": key, "display_name": key,
            "attributes": {"name": key, "environment": "prod"}, **extra,
        }})
        out.append(result)
        ids[key] = result["entity"]["entity_id"]
    for index, target in enumerate(("orders", "ledger")):
        out.append(call["ncp_ontology_assert"]({
            "kind": "relation", "pipeline_id": "p1", "idempotency_key": f"k{index}",
            "relation": {"src_entity_id": ids["api"], "dst_entity_id": ids[target],
                         "relation_type": "DEPENDS_ON", "confidence": 0.8,
                         "observed_at": OBSERVED},
        }))
    out.append(call["ncp_ontology_assert"]({  # replay
        "kind": "relation", "pipeline_id": "p1", "idempotency_key": "k0",
        "relation": {"src_entity_id": ids["api"], "dst_entity_id": ids["orders"],
                     "relation_type": "DEPENDS_ON", "confidence": 0.8, "observed_at": OBSERVED},
    }))
    out.append(call["ncp_ontology_query"]({"pipeline_id": "p1", "key": "gateway",
                                           "verified_only": False}))
    out.append(call["ncp_ontology_query"]({"pipeline_id": "p2", "key": "gateway",
                                           "verified_only": False}))
    relation_id = out[4]["relation"]["relation_id"]
    out.append(call["ncp_ontology_explain"]({"pipeline_id": "p1", "relation_id": relation_id}))
    out.append(call["ncp_ontology_explain"]({"pipeline_id": "p2", "relation_id": relation_id}))
    out.append(call["ncp_ontology_revoke"]({"pipeline_id": "p1", "relation_id": relation_id,
                                            "reason": "moved"}))
    out.append(call["ncp_ontology_query"]({"pipeline_id": "p1", "entity_id": ids["api"],
                                           "verified_only": False}))
    return _strip_stamps(out)


@requires_pg
def test_facade_is_enabled_for_pgvector_and_matches_sqlite(
    sqlite_backend: SQLiteBackend, postgres_backend: PostgresBackend,
) -> None:
    facade = OntologyFacade.from_config(ENABLED, postgres_backend.ncp)
    assert isinstance(facade.store, PostgresOntologyStore)
    assert facade.store.schema == postgres_backend.schema
    expected = facade_transcript(sqlite_backend.ncp)
    assert [item["ok"] for item in expected] == [True] * 8 + [False, True, False, True, True]
    assert facade_transcript(postgres_backend.ncp) == expected


# -- Postgres-only guarantees -------------------------------------------------------


@contextmanager
def pg_ncp_store() -> Iterator[Any]:
    """A bare NCP PgvectorStore in its own schema (no ontology store built)."""
    import psycopg

    from ncp.stores.pgvector import PgvectorStore

    dsn = _pg_dsn()
    assert dsn is not None
    schema = f"ncp_ont_{uuid4().hex[:12]}"
    store = PgvectorStore(dsn, schema=schema, table_prefix="it_")
    try:
        yield store
    finally:
        store.close()
        with psycopg.connect(dsn, autocommit=True) as connection:
            connection.execute(f"DROP SCHEMA IF EXISTS {schema} CASCADE")


def _ontology_tables(store: Any) -> set[str]:
    import psycopg

    with psycopg.connect(store.dsn) as connection:
        return {name for (name,) in connection.execute(
            "SELECT table_name FROM information_schema.tables WHERE table_schema = %s"
            " AND table_name LIKE %s", (store.schema, f"{store.table_prefix}ontology%"),
        )}


@requires_pg
def test_pg_disabled_ontology_creates_nothing() -> None:
    from ncp.ontology.enrichment import build_enricher

    with pg_ncp_store() as ncp:
        assert not OntologyFacade.from_config(OntologyConfig(), ncp).enabled
        assert not cached_facade(OntologyConfig(), ncp).enabled
        assert not OntologyFacade.from_config(
            OntologyConfig(enabled=True, provider="disabled"), ncp).enabled
        assert build_enricher(OntologyConfig(), ncp, request={"mode": "always"}) is None
        enricher = build_enricher(ENABLED, ncp)
        assert enricher is not None
        assert enricher._ontology_store() is None  # probe only: still uninitialized
        assert not PostgresOntologyStore.initialized(ncp)
        assert _ontology_tables(ncp) == set()
        assert OntologyFacade.from_config(ENABLED, ncp).enabled
        assert PostgresOntologyStore.initialized(ncp)
        assert enricher._ontology_store() is not None


@requires_pg
def test_pg_migration_is_scoped_idempotent_and_events_append_only(
    postgres_backend: PostgresBackend,
) -> None:
    import psycopg

    b = postgres_backend
    expected = {f"it_ontology_{name}" for name in (
        "entities", "aliases", "relations", "relation_evidence", "write_requests", "events",
        "schema_versions",
    )}
    assert _ontology_tables(b.ncp) == expected
    PostgresOntologyStore.inherit(b.ncp, registry())
    b.fresh_store()
    assert b.count("schema_versions") == 1
    put(b.store, service("api"))
    assert b.count("events") == 1
    for statement in ("UPDATE {t:events} SET reason = 'x'", "DELETE FROM {t:events}",
                      "TRUNCATE {t:events}"):
        with pytest.raises(psycopg.Error, match="append-only"):
            b.sql(statement)
    assert b.count("events") == 1
    # Another prefix in the same schema is a separate graph.
    other = PostgresOntologyStore(b.dsn, registry(), schema=b.schema, table_prefix="other_")
    try:
        assert other.get_entity(service("api").entity_id, auth=auth()) is None
    finally:
        other.close()


def _race(count: int, work: Callable[[int], Any]) -> tuple[list[Any], list[BaseException]]:
    barrier = threading.Barrier(count)
    results: list[Any] = []
    errors: list[BaseException] = []
    lock = threading.Lock()

    def run(index: int) -> None:
        barrier.wait()
        try:
            value = work(index)
        except BaseException as exc:  # collected and asserted by the caller
            with lock:
                errors.append(exc)
        else:
            with lock:
                results.append(value)

    threads = [threading.Thread(target=run, args=(index,)) for index in range(count)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)
    assert not any(thread.is_alive() for thread in threads), "writers deadlocked"
    return results, errors


@requires_pg
def test_pg_concurrent_cardinality_has_exactly_one_winner(postgres_backend: PostgresBackend) -> None:
    b = postgres_backend
    hub = node("hub")
    targets = [node(f"t{index}") for index in range(8)]
    put(b.store, hub, *targets)
    stores = [b.store, b.fresh_store()]

    results, errors = _race(8, lambda index: stores[index % 2].assert_relation(
        rel(f"one{index}", hub, targets[index], "ONE"), auth=auth(),
        idempotency_key=f"one{index}"))
    assert len(results) == 1 and len(errors) == 7
    assert all(isinstance(exc, ValueError) and "max_outgoing" in str(exc) for exc in errors)
    assert b.count("relations", "relation_type = 'ONE'") == 1

    sinks = [node(f"in{index}") for index in range(6)]
    put(b.store, *sinks)
    sink = node("sink")
    put(b.store, sink)
    results, errors = _race(6, lambda index: stores[index % 2].assert_relation(
        rel(f"in{index}", sinks[index], sink, "ONE"), auth=auth(), idempotency_key=f"in{index}"))
    assert len(results) == 2 and len(errors) == 4  # max_incoming = 2
    assert all("max_incoming" in str(exc) for exc in errors)


@requires_pg
def test_pg_concurrent_cycle_has_exactly_one_winner(postgres_backend: PostgresBackend) -> None:
    b = postgres_backend
    stores = [b.store, b.fresh_store()]
    for round_ in range(5):
        x, y = node(f"x{round_}"), node(f"y{round_}")
        put(b.store, x, y)
        pairs = [(x, y), (y, x)]
        results, errors = _race(2, lambda index: stores[index].assert_relation(
            rel(f"c{round_}-{index}", *pairs[index], "PARENT"), auth=auth(),
            idempotency_key=f"c{round_}-{index}"))
        assert len(results) == 1 and len(errors) == 1
        assert "cycles" in str(errors[0])
    assert b.count("relations", "relation_type = 'PARENT'") == 5


@requires_pg
def test_pg_concurrent_idempotency_and_entity_creation(postgres_backend: PostgresBackend) -> None:
    b = postgres_backend
    api, db = service("api"), database("db")
    put(b.store, api, db)
    stores = [b.store, b.fresh_store()]
    relation = rel("same", api, db)
    results, errors = _race(6, lambda index: stores[index % 2].assert_relation(
        relation, auth=auth(), idempotency_key="same"))
    assert not errors and len({item.model_dump_json() for item in results}) == 1
    assert b.count("relations") == 1 and b.count("write_requests") == 1

    fresh = service("fresh")
    results, errors = _race(6, lambda index: stores[index % 2].upsert_entity(
        fresh, auth=auth(), expected_version=0))
    assert len(results) == 1 and len(errors) == 5
    assert all(isinstance(exc, OntologyConflictError) for exc in errors)
    # Different namespaces write in parallel without waiting on each other's scope lock.
    scopes = [Scope(tenant_id="acme", namespace=f"p{index}") for index in range(4)]
    results, errors = _race(4, lambda index: (
        put(stores[index % 2], node("a", scopes[index]), node("b", scopes[index])),
        stores[index % 2].assert_relation(rel(f"ns{index}", node("a", scopes[index]),
                                              node("b", scopes[index]), "ONE"),
                                          auth=auth(scopes[index]), idempotency_key="k"),
    ))
    assert not errors and len(results) == 4


@requires_pg
def test_pg_errors_roll_back_without_partial_rows(
    postgres_backend: PostgresBackend, monkeypatch: pytest.MonkeyPatch,
) -> None:
    b = postgres_backend
    api, db = service("api"), database("db")
    put(b.store, api, db)
    events = b.count("events")
    live = b.chunk("live", "api writes orders into the primary database")
    with pytest.raises(ValueError, match="evidence chunk is not available"):
        b.store.assert_relation(
            rel("r", api, db, status="verified",
                evidence=(attestation(), chunk_ref(live), chunk_ref("sub_missing"))),
            auth=auth(), idempotency_key="k",
        )
    for table in ("relations", "relation_evidence", "write_requests"):
        assert b.count(table) == 0, table
    assert b.count("events") == events

    def boom(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("injected failure after the writes")

    monkeypatch.setattr(PostgresOntologyStore, "_event", boom)
    with pytest.raises(RuntimeError, match="injected"):
        b.store.upsert_entity(service("cache"), auth=auth())
    monkeypatch.undo()
    assert b.count("entities", "canonical_key = ?", ("cache",)) == 0
    assert b.count("aliases", "alias = ?", ("cache-svc",)) == 0
    # The pooled connection came back idle and usable.
    assert b.store.upsert_entity(service("cache"), auth=auth()).revision == 1
    assert b.store.assert_relation(rel("r", api, db, status="verified",
                                       evidence=(attestation(), chunk_ref(live))),
                                   auth=auth(), idempotency_key="k").revision == 1


@requires_pg
def test_pg_auth_isolation_across_pipelines(postgres_backend: PostgresBackend) -> None:
    from ncp.principals import Principal

    b = postgres_backend
    alice = Principal(name="alice", agent_id=None, pipelines=frozenset({"p1"}),
                      capabilities=frozenset())
    bob = Principal(name="bob", agent_id=None, pipelines=frozenset({"p2"}),
                    capabilities=frozenset())
    current = {"principal": alice}
    call = make_tool_handlers(OntologyFacade.from_config(ENABLED, b.ncp, registry=registry()),
                              lambda: current["principal"])
    ids = {}
    for key, kind in (("api", "Service"), ("orders", "Database")):
        result = call["ncp_ontology_assert"]({"kind": "entity", "pipeline_id": "p1", "entity": {
            "entity_type": kind, "canonical_key": key, "display_name": key,
            "attributes": {"name": key, "environment": "p1"},
        }})
        assert result["ok"], result
        ids[key] = result["entity"]["entity_id"]
    asserted = call["ncp_ontology_assert"]({
        "kind": "relation", "pipeline_id": "p1", "idempotency_key": "k",
        "relation": {"src_entity_id": ids["api"], "dst_entity_id": ids["orders"],
                     "relation_type": "DEPENDS_ON", "confidence": 0.8, "observed_at": OBSERVED},
    })
    assert asserted["ok"], asserted
    relation_id = asserted["relation"]["relation_id"]

    current["principal"] = bob
    for name, args in (
        ("ncp_ontology_query", {"pipeline_id": "p1", "key": "api", "verified_only": False}),
        ("ncp_ontology_explain", {"pipeline_id": "p1", "relation_id": relation_id}),
        ("ncp_ontology_revoke", {"pipeline_id": "p1", "relation_id": relation_id, "reason": "x"}),
    ):
        assert call[name](args)["error"]["code"] == "forbidden"
    assert call["ncp_ontology_explain"]({"pipeline_id": "p2", "relation_id": relation_id})[
        "error"]["code"] == "not_found"
    missing = call["ncp_ontology_query"]({"pipeline_id": "p2", "key": "api",
                                          "verified_only": False})
    assert missing["ok"] is False or not missing.get("paths"), missing
    assert call["ncp_ontology_revoke"]({"pipeline_id": "p2", "relation_id": relation_id,
                                        "reason": "x"})["error"]["code"] == "not_found"
    # Squatting alice's relation id from p2 is refused, and nothing changed.
    squat = call["ncp_ontology_assert"]({
        "kind": "relation", "pipeline_id": "p2", "idempotency_key": "k",
        "relation": {"relation_id": relation_id, "src_entity_id": ids["api"],
                     "dst_entity_id": ids["orders"], "relation_type": "DEPENDS_ON",
                     "confidence": 0.1, "observed_at": OBSERVED},
    })
    assert squat["error"]["code"] == "not_found"
    assert b.count("relations") == 1 and b.count("relations", "namespace = 'p2'") == 0


@requires_pg
def test_pg_read_deadlines_use_statement_timeout(postgres_backend: PostgresBackend) -> None:
    b = postgres_backend
    a, c = node("a"), node("c")
    put(b.store, a, c)
    b.store.assert_relation(rel("l", a, c, "LINKS"), auth=auth(), idempotency_key="l")
    with pytest.raises(OntologyTimeoutError):
        b.store.traverse(GraphQuery(seeds=(a.entity_id,), verified_only=False), auth=auth(),
                         deadline=time.monotonic() - 1)
    with pytest.raises(ValueError, match="read transactions only"):
        with b.store._transaction(write=True, deadline=time.monotonic() + 1):
            pass
    started = time.monotonic()
    with pytest.raises(OntologyTimeoutError):
        with b.store._transaction(write=False, deadline=time.monotonic() + 0.1) as tx:
            tx.tuples("SELECT pg_sleep(5)")
    assert time.monotonic() - started < 2.5  # cancelled server-side, not after 5 s
    # SET LOCAL ended with the transaction: the pooled connection keeps its default.
    with b.store._transaction(write=False) as tx:
        default = tx.tuples("SHOW statement_timeout")[0][0]
    with b.ncp._connect() as connection:
        assert connection.execute("SHOW statement_timeout").fetchone()[0] == default
    generous = b.store.traverse(GraphQuery(seeds=(a.entity_id,), verified_only=False),
                                auth=auth(), deadline=time.monotonic() + 5)
    assert len(generous.paths) == 1


@requires_pg
def test_pg_async_wrapper_matches_sync(postgres_backend: PostgresBackend) -> None:
    from ncp.stores.pgvector_async import AsyncPgvectorStore

    b = postgres_backend
    api, db, cache = service("api"), database("db"), database("cache")
    hub = node("hub")
    targets = [node(f"t{index}") for index in range(4)]
    wrapped = AsyncPostgresOntologyStore.inherit(b.ncp, registry())

    async def scenario() -> list:
        for entity in (api, db, cache, hub, *targets):
            await wrapped.upsert_entity(entity, auth=auth(entity.scope, ALL))
        first = await wrapped.assert_relation(rel("a", api, db), auth=auth(), idempotency_key="a")
        replay = await wrapped.assert_relation(rel("a", api, db), auth=auth(),
                                               idempotency_key="a")
        outcomes = await asyncio.gather(*(
            wrapped.assert_relation(rel(f"o{index}", hub, item, "ONE"), auth=auth(),
                                    idempotency_key=f"o{index}")
            for index, item in enumerate(targets)
        ), return_exceptions=True)
        graph = await wrapped.traverse(GraphQuery(seeds=(api.entity_id,), verified_only=False),
                                       auth=auth())
        explained = await wrapped.explain_relation(rid("a"), auth=auth())
        resolved = await wrapped.resolve_entities("api-svc", auth=auth())
        return [first == replay, sum(not isinstance(item, Exception) for item in outcomes),
                norm(graph), norm(explained), norm(resolved)]

    outcome = asyncio.run(scenario())
    assert outcome[:2] == [True, 1]
    sync = b.store
    assert outcome[2:] == [
        norm(sync.traverse(GraphQuery(seeds=(api.entity_id,), verified_only=False), auth=auth())),
        norm(sync.explain_relation(rid("a"), auth=auth())),
        norm(sync.resolve_entities("api-svc", auth=auth())),
    ]
    with pytest.raises(AttributeError):
        wrapped.not_a_method  # noqa: B018 - only the store's public API is wrapped

    async_ncp = AsyncPgvectorStore(b.dsn, schema=b.schema, table_prefix="it_")
    from_async = AsyncPostgresOntologyStore.inherit(async_ncp, registry())
    try:
        assert from_async.sync.schema == b.schema
        assert asyncio.run(from_async.get_entity(api.entity_id, auth=auth())) == sync.get_entity(
            api.entity_id, auth=auth())
    finally:
        from_async.close()


def test_async_pgvector_store_keeps_facade_disabled() -> None:
    from ncp.stores.pgvector_async import AsyncPgvectorStore

    store = AsyncPgvectorStore("postgresql://unused.invalid/ncp")  # never connects
    facade = OntologyFacade.from_config(ENABLED, store)
    assert not facade.enabled and "AsyncPgvectorStore" in (facade.disabled_reason or "")


@requires_pg
def test_pg_nul_text_is_invalid_input_not_a_driver_error(postgres_backend: PostgresBackend) -> None:
    b = postgres_backend
    nul = Entity(scope=PROD, entity_type="Node", canonical_key="n\x00", display_name="n",
                 schema_pack="graph")
    with pytest.raises(ValueError, match="NUL"):
        b.store.upsert_entity(nul, auth=auth())
    assert b.count("entities") == 0
    call = make_tool_handlers(OntologyFacade.from_config(ENABLED, b.ncp, registry=registry()),
                              lambda: None)
    assert call["ncp_ontology_query"]({"pipeline_id": "p1", "key": "api\x00"})["error"][
        "code"] == "not_found"
    result = call["ncp_ontology_assert"]({"kind": "entity", "pipeline_id": "p1", "entity": {
        "entity_type": "Service", "canonical_key": "api\x00", "display_name": "api",
        "attributes": {"name": "api", "environment": "prod"},
    }})
    assert result["ok"] is False and result["error"]["code"] == "invalid"
    put(b.store, node("ok"))  # the pooled connection is still usable


@requires_pg
def test_pg_lock_keys_fold_identifier_case(postgres_backend: PostgresBackend) -> None:
    import psycopg

    b = postgres_backend
    upper = PostgresOntologyStore(b.dsn, registry(), schema=b.schema.upper(), table_prefix="IT_",
                                  lock_timeout=0.3)
    b._owned.append(upper)
    assert upper._lock_key("scope", "acme", "prod") == b.store._lock_key("scope", "acme", "prod")
    a, c = node("a"), node("c")
    put(b.store, a, c)
    with psycopg.connect(b.dsn) as holder:
        holder.execute("SELECT pg_advisory_xact_lock(%s)",
                       (b.store._lock_key("scope", "acme", "prod"),))
        with pytest.raises(psycopg.errors.LockNotAvailable):
            upper.assert_relation(rel("x", a, c, "LINKS"), auth=auth(), idempotency_key="x")
    assert b.count("relations") == 0


@requires_pg
def test_pg_failed_construction_closes_private_pool(monkeypatch: pytest.MonkeyPatch) -> None:
    opened: list[Any] = []
    original = PostgresOntologyStore._private_source

    def spy(self: PostgresOntologyStore, dsn: str, max_size: int) -> Any:
        source = original(self, dsn, max_size)
        opened.append(source)
        return source

    def fail(self: PostgresOntologyStore) -> None:
        raise RuntimeError("migration failed")

    monkeypatch.setattr(PostgresOntologyStore, "_private_source", spy)
    monkeypatch.setattr(PostgresOntologyStore, "_migrate", fail)
    with pytest.raises(RuntimeError, match="migration failed"):
        PostgresOntologyStore(_pg_dsn(), registry(), schema="ncp_ont_unused")
    assert len(opened) == 1 and opened[0].closed
