import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest

from ncp.ontology.models import (
    AuthContext, Entity, EvidenceRef, GraphQuery, Relation, Scope,
)
from ncp.ontology.registry import SchemaPack, SchemaRegistry
from ncp.ontology.stores import OntologyConflictError, SQLiteOntologyStore
from ncp.stores.sqlite import SQLiteStore
from ncp.types import SubconsciousChunk

NOW = datetime.now(timezone.utc) - timedelta(days=1)
PROD = Scope(tenant_id="acme", namespace="prod")
STAGING = Scope(tenant_id="acme", namespace="staging")
GLOBAL = Scope(tenant_id="acme", namespace="global")
OTHER_TENANT = Scope(tenant_id="other", namespace="prod")
ALL = frozenset({"read", "write", "verify", "admin"})


def auth(scope=PROD, caps=frozenset({"read", "write", "verify"}), principal="catalog"):
    return AuthContext(principal_id=principal, scope=scope, capabilities=caps)


def registry():
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


def service(name, scope=PROD):
    return Entity(
        scope=scope, entity_type="Service", canonical_key=name, display_name=name,
        aliases=(f"{name}-svc",), attributes={"name": name, "environment": scope.namespace},
        schema_pack="infrastructure",
    )


def database(name, scope=PROD):
    return Entity(
        scope=scope, entity_type="Database", canonical_key=name, display_name=name,
        attributes={"name": name}, schema_pack="infrastructure",
    )


def node(name, scope=PROD):
    return Entity(scope=scope, entity_type="Node", canonical_key=name, display_name=name,
                  schema_pack="graph")


def rel(src, dst, relation_type="DEPENDS_ON", **overrides):
    return Relation.model_validate({
        "relation_id": uuid4(), "scope": src.scope, "src_entity_id": src.entity_id,
        "dst_entity_id": dst.entity_id, "relation_type": relation_type, "confidence": 0.9,
        "valid_from": NOW, "observed_at": NOW, "created_by": "catalog",
        "schema_pack": src.schema_pack, **overrides,
    })


def chunk_ref(chunk_id):
    return EvidenceRef(evidence_type="chunk", evidence_id=chunk_id, source_revision="1",
                       observed_at=NOW)


def attestation(name="cmdb"):
    return EvidenceRef(evidence_type="external_attestation", evidence_id=name,
                       source_revision="1", observed_at=NOW)


@pytest.fixture
def store(tmp_path):
    return SQLiteOntologyStore(tmp_path / "ncp.db", registry())


def put(store, *entities, scope_auth=None):
    for entity in entities:
        store.upsert_entity(entity, auth=scope_auth or auth(entity.scope, ALL))


def tables(path):
    with sqlite3.connect(path) as connection:
        return {name for (name,) in connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        )}


def count(path, sql, params=()):
    with sqlite3.connect(path) as connection:
        return connection.execute(sql, params).fetchone()[0]


def test_tables_are_opt_in_and_idempotent(tmp_path):
    path = tmp_path / "ncp.db"
    SQLiteStore(path)
    assert not {name for name in tables(path) if name.startswith("ontology_")}
    ncp_tables = tables(path)

    first = SQLiteOntologyStore.inherit(SQLiteStore(path), registry())
    put(first, service("api"))
    SQLiteOntologyStore(path, registry())
    created = tables(path) - ncp_tables
    assert {"ontology_entities", "ontology_aliases", "ontology_relations",
            "ontology_relation_evidence", "ontology_write_requests", "ontology_events",
            "ontology_schema_versions"} <= created
    assert count(path, "SELECT COUNT(*) FROM ontology_schema_versions") == 1
    assert count(path, "SELECT COUNT(*) FROM ontology_entities") == 1
    with pytest.raises(sqlite3.DatabaseError, match="append-only"):
        with sqlite3.connect(path) as connection:
            connection.execute("DELETE FROM ontology_events")


def test_existing_connection_is_supported(tmp_path):
    connection = sqlite3.connect(tmp_path / "shared.db", check_same_thread=False)
    shared = SQLiteOntologyStore(connection, registry())
    entity = shared.upsert_entity(service("api"), auth=auth())
    assert shared.get_entity(entity.entity_id, auth=auth()) == entity
    assert connection.row_factory is None and not connection.in_transaction


def test_entity_revisions_and_conflicts(store):
    first = store.upsert_entity(service("api"), auth=auth(), expected_version=0)
    assert first.revision == 1 and first.created_at is not None
    second = store.upsert_entity(service("api"), auth=auth(), expected_version=1)
    assert second.revision == 2 and second.created_at == first.created_at
    with pytest.raises(OntologyConflictError, match="expected 1, found 2"):
        store.upsert_entity(service("api"), auth=auth(), expected_version=1)
    with pytest.raises(OntologyConflictError):
        store.upsert_entity(service("api"), auth=auth(), expected_version=0)
    assert store.upsert_entity(service("api"), auth=auth()).revision == 3
    stored = store.get_entity(first.entity_id, auth=auth())
    assert stored is not None and stored.revision == 3 and stored.aliases == ("api-svc",)
    with pytest.raises(ValueError, match="missing required attribute"):
        store.upsert_entity(service("api").model_copy(update={"attributes": {}}), auth=auth())


def test_aliases_may_be_ambiguous(store, tmp_path):
    put(store, service("a").model_copy(update={"aliases": ("pay",)}),
        service("b").model_copy(update={"aliases": ("pay",)}))
    assert count(tmp_path / "ncp.db",
                 "SELECT COUNT(*) FROM ontology_aliases WHERE alias = 'pay'") == 2


def test_idempotent_assert(store, tmp_path):
    api, db = service("api"), database("db")
    put(store, api, db)
    relation = rel(api, db)
    first = store.assert_relation(relation, auth=auth(), idempotency_key="k1")
    assert first.revision == 1 and first.status == "candidate"
    assert store.assert_relation(relation, auth=auth(), idempotency_key="k1") == first
    with pytest.raises(OntologyConflictError, match="different payload"):
        store.assert_relation(relation.model_copy(update={"confidence": 0.5}),
                              auth=auth(), idempotency_key="k1")
    assert count(tmp_path / "ncp.db", "SELECT COUNT(*) FROM ontology_relations") == 1


def test_evidence_failure_is_atomic(store, tmp_path):
    SQLiteStore(tmp_path / "ncp.db")  # NCP chunks table now exists
    api, db = service("api"), database("db")
    put(store, api, db)
    with pytest.raises(ValueError, match="evidence chunk is not available"):
        store.assert_relation(
            rel(api, db, status="verified", evidence=(attestation(), chunk_ref("sub_missing"))),
            auth=auth(), idempotency_key="k",
        )
    path = tmp_path / "ncp.db"
    for table in ("ontology_relations", "ontology_relation_evidence", "ontology_write_requests"):
        assert count(path, f"SELECT COUNT(*) FROM {table}") == 0


def test_capability_gating(store):
    api, db = service("api"), database("db")
    put(store, api, db)
    writer = auth(caps=frozenset({"read", "write"}))
    verified = rel(api, db, status="verified", evidence=(attestation(),))
    with pytest.raises(PermissionError, match="verify"):
        store.assert_relation(verified, auth=writer, idempotency_key="k")
    candidate = store.assert_relation(
        verified.model_copy(update={"status": "candidate", "evidence": ()}),
        auth=writer, idempotency_key="k0",
    )
    promoted = store.assert_relation(verified, auth=auth(), idempotency_key="k1")
    assert (candidate.revision, promoted.revision, promoted.status) == (1, 2, "verified")
    with pytest.raises(PermissionError, match="verify"):  # demoting is a verification act
        store.assert_relation(verified.model_copy(update={"status": "disputed"}),
                              auth=writer, idempotency_key="k2")
    with pytest.raises(PermissionError, match="write"):
        store.upsert_entity(service("x"), auth=auth(caps=frozenset({"read"})))
    with pytest.raises(PermissionError, match="read"):
        store.get_entity(api.entity_id, auth=auth(caps=frozenset({"write"})))
    with pytest.raises(PermissionError, match="own scope"):
        store.upsert_entity(service("x", STAGING), auth=auth())


def test_global_namespace_rules(store):
    shared_db = database("shared", GLOBAL)
    with pytest.raises(PermissionError, match="admin"):
        store.upsert_entity(shared_db, auth=auth(GLOBAL))
    with pytest.raises(PermissionError, match="own scope"):  # pipeline admins stay local
        store.upsert_entity(shared_db, auth=auth(PROD, ALL))
    put(store, shared_db)
    assert store.get_entity(shared_db.entity_id, auth=auth(STAGING)) is not None
    assert store.get_entity(shared_db.entity_id, auth=auth(OTHER_TENANT)) is None

    api = service("api")
    put(store, api)
    edge = store.assert_relation(rel(api, shared_db), auth=auth(), idempotency_key="k")
    assert edge.scope == PROD

    global_svc = service("gateway", GLOBAL)
    put(store, global_svc)
    with pytest.raises(ValueError, match="not available"):  # pipeline target invisible
        store.assert_relation(rel(global_svc, api), auth=auth(GLOBAL, ALL), idempotency_key="g")
    staging_api = service("api", STAGING)
    put(store, staging_api)
    with pytest.raises(ValueError, match="not available"):
        store.assert_relation(rel(api, staging_api), auth=auth(), idempotency_key="x")
    store.assert_relation(rel(global_svc, shared_db), auth=auth(GLOBAL, ALL),
                          idempotency_key="g2")


def test_scope_isolation_in_reads(store):
    api, db = service("api"), database("db")
    put(store, api, db)
    store.assert_relation(rel(api, db), auth=auth(), idempotency_key="k")
    query = GraphQuery(seeds=(api.entity_id,), verified_only=False)
    assert len(store.traverse(query, auth=auth()).paths) == 1
    for scope in (STAGING, OTHER_TENANT, GLOBAL):
        assert store.get_entity(api.entity_id, auth=auth(scope)) is None
        assert store.traverse(query, auth=auth(scope)) == store.traverse(
            GraphQuery(seeds=(uuid4(),), verified_only=False), auth=auth(scope),
        )

    # A global seed only exposes edges from the caller's own namespace.
    shared_db = database("shared", GLOBAL)
    staging_api = service("api", STAGING)
    put(store, shared_db, staging_api)
    store.assert_relation(rel(api, shared_db), auth=auth(), idempotency_key="p")
    store.assert_relation(rel(staging_api, shared_db), auth=auth(STAGING), idempotency_key="s")
    seen = store.traverse(GraphQuery(seeds=(shared_db.entity_id,), verified_only=False),
                          auth=auth(STAGING))
    assert [path.relations[0].scope for path in seen.paths] == [STAGING]


def chain_store(store, size):
    nodes = [node(f"n{index}") for index in range(size)]
    put(store, *nodes)
    for left, right in zip(nodes, nodes[1:]):
        store.assert_relation(rel(left, right, "LINKS"), auth=auth(),
                              idempotency_key=f"{left.canonical_key}-{right.canonical_key}")
    return nodes


def test_traversal_limits_warn(store):
    nodes = chain_store(store, 6)
    seed = GraphQuery(seeds=(nodes[0].entity_id,), verified_only=False)
    full = store.traverse(seed.model_copy(update={"max_hops": 5}), auth=auth())
    assert len(full.paths) == 5 and not full.warnings
    assert full.paths[-1].nodes == tuple(item.entity_id for item in nodes)

    hops = store.traverse(seed.model_copy(update={"max_hops": 2}), auth=auth())
    assert len(hops.paths) == 2 and any("max_hops" in item for item in hops.warnings)
    edges = store.traverse(seed.model_copy(update={"max_hops": 5, "max_edges": 3}), auth=auth())
    assert len(edges.paths) == 3 and any("max_edges" in item for item in edges.warnings)
    limited = store.traverse(seed.model_copy(update={"max_hops": 5, "max_nodes": 3}), auth=auth())
    assert len(limited.paths) == 2 and any("max_nodes" in item for item in limited.warnings)
    filtered = store.traverse(seed.model_copy(update={"relation_types": ("PARENT",)}),
                              auth=auth())
    assert filtered.paths == ()


def test_cycles_terminate_and_order_is_stable(store):
    a, b, c = node("a"), node("b"), node("c")
    put(store, a, b, c)
    for index, (src, dst) in enumerate(((a, b), (b, c), (c, a), (a, a))):
        store.assert_relation(rel(src, dst, "LINKS"), auth=auth(), idempotency_key=f"k{index}")
    query = GraphQuery(seeds=(a.entity_id,), max_hops=5, verified_only=False)
    result = store.traverse(query, auth=auth())
    assert len(result.paths) == 4 and not result.warnings
    assert result == store.traverse(query, auth=auth())
    first_hop = [path.relations[-1].relation_id for path in result.paths if len(path.nodes) == 2]
    assert first_hop == sorted(first_hop, key=str)


def test_cardinality_and_acyclic_types(store):
    a, b, c, d = (node(name) for name in "abcd")
    put(store, a, b, c, d)
    store.assert_relation(rel(a, b, "ONE"), auth=auth(), idempotency_key="1")
    with pytest.raises(ValueError, match="max_outgoing"):
        store.assert_relation(rel(a, c, "ONE"), auth=auth(), idempotency_key="2")
    store.assert_relation(rel(c, b, "ONE"), auth=auth(), idempotency_key="3")
    with pytest.raises(ValueError, match="max_incoming"):
        store.assert_relation(rel(d, b, "ONE"), auth=auth(), idempotency_key="4")

    store.assert_relation(rel(a, b, "PARENT"), auth=auth(), idempotency_key="p1")
    store.assert_relation(rel(b, c, "PARENT"), auth=auth(), idempotency_key="p2")
    with pytest.raises(ValueError, match="cycles"):
        store.assert_relation(rel(c, a, "PARENT"), auth=auth(), idempotency_key="p3")
    with pytest.raises(ValueError, match="cycles"):
        store.assert_relation(rel(d, d, "PARENT"), auth=auth(), idempotency_key="p4")
    store.assert_relation(rel(c, a, "LINKS"), auth=auth(), idempotency_key="p5")


def live_chunk(ncp, content, pipeline="prod"):
    chunk = SubconsciousChunk(layer="semantic", content=content, src="tool_result",
                              pipeline_id=pipeline)
    assert ncp.write(chunk)
    return chunk.chunk_id


def verified_paths(store):
    return store.traverse(GraphQuery(seeds=(service("api").entity_id,)), auth=auth()).paths


def test_stale_chunk_evidence_is_excluded(tmp_path):
    ncp = SQLiteStore(tmp_path / "ncp.db")
    store = SQLiteOntologyStore.inherit(ncp, registry())
    api, db, cache = service("api"), database("db"), database("cache")
    put(store, api, db, cache)
    old = live_chunk(ncp, "api depends on the orders database for checkout writes")
    new = live_chunk(ncp, "api moved checkout writes to a new ledger service in november")
    gone = live_chunk(ncp, "api reads session tokens from the cache cluster")
    store.assert_relation(rel(api, db, status="verified", evidence=(chunk_ref(old),)),
                          auth=auth(), idempotency_key="db")
    store.assert_relation(rel(api, cache, status="verified", evidence=(chunk_ref(gone),)),
                          auth=auth(), idempotency_key="cache")
    assert len(verified_paths(store)) == 2

    assert ncp.supersede(old, new)
    ncp.forget(chunk_ids=[gone])
    assert verified_paths(store) == ()
    all_paths = store.traverse(GraphQuery(seeds=(api.entity_id,), verified_only=False),
                               auth=auth()).paths
    assert len(all_paths) == 2  # still stored, just no longer verified-live

    other = live_chunk(ncp, "staging pipeline note about database failover", pipeline="staging")
    with pytest.raises(ValueError, match="evidence chunk is not available"):
        store.assert_relation(rel(api, db, status="verified", evidence=(chunk_ref(other),)),
                              auth=auth(), idempotency_key="foreign")


def test_invalidate_evidence_demotes_idempotently(tmp_path):
    ncp = SQLiteStore(tmp_path / "ncp.db")
    store = SQLiteOntologyStore.inherit(ncp, registry())
    api, db, cache = service("api"), database("db"), database("cache")
    put(store, api, db, cache)
    first = live_chunk(ncp, "api writes orders into the primary database")
    second = live_chunk(ncp, "cache cluster fronts api session reads")
    only = store.assert_relation(rel(api, db, status="verified", evidence=(chunk_ref(first),)),
                                 auth=auth(), idempotency_key="a")
    backed = store.assert_relation(
        rel(api, cache, status="verified", evidence=(chunk_ref(first), chunk_ref(second))),
        auth=auth(), idempotency_key="b",
    )
    assert store.invalidate_evidence([first], auth=auth(STAGING), reason="wrong scope") == 0
    assert store.invalidate_evidence([first], auth=auth(), reason="forgotten") == 2
    assert store.invalidate_evidence([first], auth=auth(), reason="forgotten") == 0
    paths = store.traverse(GraphQuery(seeds=(api.entity_id,), verified_only=False),
                           auth=auth()).paths
    state = {path.relations[0].relation_id: path.relations[0] for path in paths}
    assert (state[only.relation_id].status, state[only.relation_id].revision) == ("candidate", 2)
    assert state[backed.relation_id].status == "verified"
    assert [ref.evidence_id for ref in state[backed.relation_id].evidence] == [second]
    assert count(tmp_path / "ncp.db",
                 "SELECT COUNT(*) FROM ontology_events WHERE event_type = 'demoted'") == 1


def test_revocation(store):
    api, db = service("api"), database("db")
    put(store, api, db)
    edge = store.assert_relation(rel(api, db), auth=auth(), idempotency_key="k")
    with pytest.raises(PermissionError, match="admin"):
        store.revoke_relation(edge.relation_id, auth=auth(principal="intruder"), reason="x")
    with pytest.raises(ValueError, match="not available"):
        store.revoke_relation(edge.relation_id, auth=auth(STAGING, ALL), reason="x")
    revoked = store.revoke_relation(edge.relation_id, auth=auth(), reason="decommissioned")
    assert (revoked.status, revoked.revision) == ("revoked", 2)
    assert store.revoke_relation(edge.relation_id, auth=auth(), reason="again") == revoked
    assert store.traverse(GraphQuery(seeds=(api.entity_id,), verified_only=False),
                          auth=auth()).paths == ()
    with pytest.raises(ValueError, match="revoked"):
        store.assert_relation(edge.model_copy(update={"confidence": 0.1}), auth=auth(),
                              idempotency_key="k2")


def test_expired_relations_and_deleted_endpoints_are_hidden(store):
    api, db, cache = service("api"), database("db"), database("cache")
    put(store, api, db, cache)
    store.assert_relation(rel(api, db, valid_until=NOW + timedelta(hours=1)), auth=auth(),
                          idempotency_key="expired")
    store.assert_relation(rel(api, cache), auth=auth(), idempotency_key="live")
    query = GraphQuery(seeds=(api.entity_id,), verified_only=False)
    assert [path.nodes[-1] for path in store.traverse(query, auth=auth()).paths] == [
        cache.entity_id,
    ]
    store.upsert_entity(cache.model_copy(update={"deleted_at": NOW}), auth=auth())
    assert store.traverse(query, auth=auth()).paths == ()


def test_concurrent_idempotent_asserts(store, tmp_path):
    api, db = service("api"), database("db")
    put(store, api, db)
    relation = rel(api, db)
    barrier = threading.Barrier(2)
    results, errors = [], []

    def worker():
        other = SQLiteOntologyStore(tmp_path / "ncp.db", store.registry)
        barrier.wait()
        try:
            results.append(other.assert_relation(relation, auth=auth(), idempotency_key="same"))
        except Exception as exc:  # pragma: no cover - surfaced by the assert below
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert not errors and len(results) == 2 and results[0] == results[1]
    assert count(tmp_path / "ncp.db", "SELECT COUNT(*) FROM ontology_relations") == 1


# -- review regressions ------------------------------------------------------


def test_chunk_evidence_fails_closed_without_chunks_table(store):
    api, db = service("api"), database("db")
    put(store, api, db)
    with pytest.raises(ValueError, match="evidence chunk is not available"):
        store.assert_relation(rel(api, db, status="verified", evidence=(chunk_ref("sub_made_up"),)),
                              auth=auth(), idempotency_key="k")
    store.assert_relation(rel(api, db, status="verified", evidence=(attestation(),)),
                          auth=auth(), idempotency_key="k2")
    assert len(verified_paths(store)) == 1


def test_stored_chunk_evidence_without_chunks_table_is_not_verified(tmp_path):
    path = tmp_path / "ncp.db"
    ncp = SQLiteStore(path)
    store = SQLiteOntologyStore.inherit(ncp, registry())
    api, db = service("api"), database("db")
    put(store, api, db)
    chunk_id = live_chunk(ncp, "api writes orders into the primary database")
    store.assert_relation(rel(api, db, status="verified", evidence=(chunk_ref(chunk_id),)),
                          auth=auth(), idempotency_key="k")
    with sqlite3.connect(path) as connection:
        connection.execute("ALTER TABLE chunks RENAME TO chunks_moved")
    assert verified_paths(store) == ()


def test_invalidating_verified_support_needs_verify(tmp_path):
    ncp = SQLiteStore(tmp_path / "ncp.db")
    store = SQLiteOntologyStore.inherit(ncp, registry())
    api, db, cache = service("api"), database("db"), database("cache")
    put(store, api, db, cache)
    chunk_id = live_chunk(ncp, "api writes orders into the primary database")
    store.assert_relation(rel(api, cache, evidence=(chunk_ref(chunk_id),)), auth=auth(),
                          idempotency_key="candidate")
    store.assert_relation(rel(api, db, status="verified", evidence=(chunk_ref(chunk_id),)),
                          auth=auth(), idempotency_key="verified")
    writer = auth(caps=frozenset({"read", "write"}), principal="other")
    with pytest.raises(PermissionError, match="verify"):
        store.invalidate_evidence([chunk_id], auth=writer, reason="x")
    assert len(verified_paths(store)) == 1  # rolled back, candidate evidence included
    assert count(tmp_path / "ncp.db", "SELECT COUNT(*) FROM ontology_relation_evidence"
                 " WHERE status = 'invalidated'") == 0
    assert store.invalidate_evidence([chunk_id], auth=auth(), reason="forgotten") == 2


def test_shared_connection_with_custom_row_factory(tmp_path):
    connection = sqlite3.connect(tmp_path / "shared.db", check_same_thread=False)
    connection.row_factory = lambda cursor, row: {
        column[0]: value for column, value in zip(cursor.description, row)
    }
    shared = SQLiteOntologyStore(connection, registry())
    api, db = service("api"), database("db")
    put(shared, api, db)
    stored = shared.get_entity(api.entity_id, auth=auth())
    assert stored is not None and stored.aliases == ("api-svc",)
    shared.assert_relation(rel(api, db), auth=auth(), idempotency_key="k")
    paths = shared.traverse(GraphQuery(seeds=(api.entity_id,), verified_only=False),
                            auth=auth()).paths
    assert len(paths) == 1


def test_failed_commit_does_not_wedge_shared_connection(tmp_path):
    path = tmp_path / "shared.db"
    connection = sqlite3.connect(path, timeout=0.1, check_same_thread=False)  # rollback journal
    shared = SQLiteOntologyStore(connection, registry())
    reader = sqlite3.connect(path, isolation_level=None)
    reader.execute("BEGIN")
    reader.execute("SELECT * FROM ontology_entities").fetchall()  # holds SHARED until COMMIT
    with pytest.raises(sqlite3.OperationalError, match="locked"):
        shared.upsert_entity(service("api"), auth=auth())
    assert not connection.in_transaction
    reader.execute("COMMIT")
    assert shared.upsert_entity(service("api"), auth=auth()).revision == 1


def test_revoke_checks_capabilities_before_lookup(store):
    g1, g2 = node("g1", GLOBAL), node("g2", GLOBAL)
    put(store, g1, g2)
    edge = store.assert_relation(rel(g1, g2, "LINKS"), auth=auth(GLOBAL, ALL),
                                 idempotency_key="k")
    non_admin = auth(GLOBAL, frozenset({"write"}))
    for relation_id in (edge.relation_id, uuid4()):
        with pytest.raises(PermissionError, match="admin"):
            store.revoke_relation(relation_id, auth=non_admin, reason="x")


def test_chunk_liveness_columns_and_units(tmp_path):
    ncp = SQLiteStore(tmp_path / "ncp.db")
    store = SQLiteOntologyStore.inherit(ncp, registry())
    api, db = service("api"), database("db")
    put(store, api, db)
    expiring = SubconsciousChunk(layer="semantic", content="api uses the ledger database",
                                 src="tool_result", pipeline_id="prod",
                                 expiry=datetime.now(timezone.utc).timestamp() + 3600)
    assert ncp.write(expiring)
    store.assert_relation(rel(api, db, status="verified", evidence=(chunk_ref(expiring.chunk_id),)),
                          auth=auth(), idempotency_key="k")
    assert len(verified_paths(store)) == 1  # epoch-seconds expiry in the future is live
    newer = live_chunk(ncp, "api moved refunds to a separate ledger in december")
    # superseded_by alone retires the chunk, even while valid_to is in the future
    assert ncp.supersede(expiring.chunk_id, newer,
                         valid_to=datetime.now(timezone.utc).timestamp() + 3600)
    assert verified_paths(store) == ()
