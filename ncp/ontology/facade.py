"""Python facade over the opt-in ontology, shared by in-process hosts and MCP tools.

``OntologyFacade.from_config`` builds an enabled facade only when
``[ontology] enabled = true``, the provider is ``builtin`` and the NCP store
is SQLite or PostgreSQL (``PgvectorStore``); otherwise it is a *disabled*
facade whose ``capabilities()`` is ``{"enabled": False, "version": "1"}`` and
whose other operations raise ``OntologyError(code="disabled")``.

Every operation takes a trusted :class:`AuthContext`. Hosts build it with
:func:`auth_context_for` from the authenticated principal (never from tool
arguments). Failures surface as :class:`OntologyError` with a stable ``code``:

``conflict``  idempotency / optimistic-concurrency precondition failed
``forbidden`` the caller lacks a capability or targets a scope it may not write
``invalid``   malformed input or a schema/pack/state rule rejected it
``not_found`` missing *or out of the caller's readable scope* (indistinguishable)
``disabled``  the ontology is off for this server
``limit``     the request exceeds a hard bound that cannot be clamped

Successful operations return JSON-ready dicts with ``"ok": True``;
:meth:`OntologyError.to_result` gives the matching ``{"ok": False, ...}`` shape.
"""

from __future__ import annotations

import json
import threading
import weakref
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import TYPE_CHECKING
from uuid import NAMESPACE_URL, UUID, uuid5

from pydantic import ValidationError

from ncp.ontology.models import (
    GLOBAL_NAMESPACE, AuthContext, Capability, Entity, GraphQuery, Relation, Scope,
)
from ncp.ontology.policy import OntologyConfig
from ncp.ontology.redaction import redact_attributes
from ncp.ontology.registry import SchemaRegistry
from ncp.ontology.stores.postgres import PostgresOntologyStore
from ncp.ontology.stores.sqlite import (
    OntologyConflictError, OntologyNotFoundError, SQLiteOntologyStore,
)

if TYPE_CHECKING:
    from ncp.config import NCPConfig
    from ncp.principals import Principal

ONTOLOGY_API_VERSION = "1"
# NCP has no tenant dimension yet (design §14.1): one implicit tenant per store.
DEFAULT_TENANT = "default"
UNAUTHENTICATED_PRINCIPAL = "local"

ONTOLOGY_TOOL_NAMES = (
    "ncp_ontology_capabilities",
    "ncp_ontology_query",
    "ncp_ontology_explain",
    "ncp_ontology_assert",
    "ncp_ontology_revoke",
)

ERROR_CODES = frozenset({"conflict", "forbidden", "invalid", "not_found", "disabled", "limit"})

# Hard request bounds that are rejected (``limit``) rather than clamped.
MAX_RELATION_TYPES = 100
MAX_EVIDENCE_REFS = 32
MAX_ATTRIBUTES_BYTES = 16_384
MAX_ALIASES = 64
MAX_CANDIDATES = 20

_ENTITY_FIELDS = frozenset({
    "entity_type", "canonical_key", "display_name", "aliases", "attributes",
    "schema_pack", "schema_version", "deleted_at",
})
_RELATION_FIELDS = frozenset({
    "relation_id", "src_entity_id", "dst_entity_id", "relation_type", "status", "confidence",
    "valid_from", "valid_until", "observed_at", "attributes", "evidence", "schema_pack",
    "schema_version",
})


class OntologyError(Exception):
    """A structured ontology failure; ``code`` is one of :data:`ERROR_CODES`."""

    def __init__(self, code: str, message: str) -> None:
        if code not in ERROR_CODES:
            raise ValueError(f"unknown ontology error code: {code}")
        super().__init__(message)
        self.code = code
        self.message = message

    def to_result(self) -> dict[str, object]:
        return {"ok": False, "error": {"code": self.code, "message": self.message}}


def _validation_message(exc: ValidationError) -> str:
    # Field locations and messages only: never echo the rejected input values.
    parts = []
    for error in exc.errors(include_url=False, include_input=False):
        location = ".".join(str(item) for item in error.get("loc", ())) or "value"
        parts.append(f"{location}: {error.get('msg', 'invalid')}")
    return "; ".join(parts[:5]) or "invalid input"


@contextmanager
def _translate_errors() -> Iterator[None]:
    """Map store/contract exceptions onto :class:`OntologyError` codes."""
    try:
        yield
    except OntologyError:
        raise
    except OntologyConflictError as exc:
        raise OntologyError("conflict", str(exc)) from None
    except OntologyNotFoundError as exc:
        raise OntologyError("not_found", str(exc)) from None
    except PermissionError as exc:
        raise OntologyError("forbidden", str(exc)) from None
    except ValidationError as exc:
        raise OntologyError("invalid", _validation_message(exc)) from None
    except ValueError as exc:
        raise OntologyError("invalid", str(exc)) from None


def auth_context_for(
    principal: Principal | None,
    namespace: str,
    *,
    config: OntologyConfig,
    tenant_id: str = DEFAULT_TENANT,
) -> AuthContext:
    """Map the transport's authenticated principal onto an ontology ``AuthContext``.

    With a principal: read+write on pipelines in its allowlist (any pipeline
    when it has none); ``global`` is readable by everyone and writable only
    with ``admin``; ``verify``/``admin`` follow the principal's capabilities.
    Without one (stdio, or HTTP without ``[principals]``): the configured
    ``unauthenticated_capabilities`` as principal ``local``.
    Raises ``OntologyError("forbidden")`` for a pipeline outside the allowlist.
    """
    from ncp.principals import CAP_ADMIN, CAP_VERIFY

    with _translate_errors():
        scope = Scope(tenant_id=tenant_id, namespace=namespace)
    if principal is None:
        return AuthContext(
            principal_id=UNAUTHENTICATED_PRINCIPAL, scope=scope,
            capabilities=frozenset(config.unauthenticated_capabilities),
        )
    capabilities: set[Capability] = {"read"}
    is_admin = principal.has(CAP_ADMIN)
    if scope.namespace == GLOBAL_NAMESPACE:
        if is_admin:
            capabilities.add("write")
    elif principal.pipeline_allowed(scope.namespace):
        capabilities.add("write")
    else:
        raise OntologyError(
            "forbidden", f"principal {principal.name!r} may not access pipeline {scope.namespace!r}",
        )
    if principal.has(CAP_VERIFY):
        capabilities.add("verify")
    if is_admin:
        capabilities.add("admin")
    return AuthContext(
        principal_id=principal.name, scope=scope, capabilities=frozenset(capabilities),
    )


def _uuid(value: object, field: str) -> UUID:
    if isinstance(value, UUID):
        return value
    if not isinstance(value, str):
        raise OntologyError("invalid", f"{field} must be a UUID string")
    try:
        return UUID(value)
    except ValueError:
        raise OntologyError("invalid", f"{field} must be a UUID string") from None


def _bounded_json(value: object, field: str) -> None:
    try:
        size = len(json.dumps(value, sort_keys=True))
    except (TypeError, ValueError):
        raise OntologyError("invalid", f"{field} must be JSON") from None
    if size > MAX_ATTRIBUTES_BYTES:
        raise OntologyError("limit", f"{field} exceeds {MAX_ATTRIBUTES_BYTES} bytes")


def _limit(value: object, field: str, ceiling: int, *, minimum: int) -> tuple[int, bool]:
    """Clamp a requested bound to the configured ceiling: ``(value, clamped)``."""
    if value is None:
        return ceiling, False
    if isinstance(value, bool) or not isinstance(value, int):
        raise OntologyError("invalid", f"{field} must be an integer")
    if value < minimum:
        raise OntologyError("invalid", f"{field} must be >= {minimum}")
    return (ceiling, True) if value > ceiling else (value, False)


def entity_summary(entity: Entity) -> dict[str, object]:
    """Identity fields only; attributes are never serialized into graph results."""
    return {
        "entity_id": str(entity.entity_id),
        "entity_type": entity.entity_type,
        "canonical_key": entity.canonical_key,
        "display_name": entity.display_name,
        "namespace": entity.scope.namespace,
        "schema_pack": entity.schema_pack,
        "schema_version": entity.schema_version,
    }


class OntologyFacade:
    """Capability discovery plus scoped query/explain/assert/revoke.

    Construct with :meth:`from_config` (or :meth:`disabled`); ``store`` is
    ``None`` exactly when the facade is disabled.
    """

    def __init__(
        self,
        config: OntologyConfig,
        store: SQLiteOntologyStore | PostgresOntologyStore | None,
        *,
        disabled_reason: str | None = None,
    ) -> None:
        self.config = config
        self.store = store
        self.disabled_reason = disabled_reason if store is None else None
        if store is not None:
            available = {pack.id for pack in store.registry.packs()}
            unknown = sorted(set(config.packs) - available)
            if unknown:
                raise ValueError(f"[ontology].packs names unregistered schema packs: {unknown}")

    @classmethod
    def disabled(cls, reason: str = "ontology is disabled") -> OntologyFacade:
        return cls(OntologyConfig(), None, disabled_reason=reason)

    @classmethod
    def from_config(
        cls,
        config: NCPConfig | OntologyConfig | None,
        store: object,
        *,
        registry: SchemaRegistry | None = None,
    ) -> OntologyFacade:
        """Provider lifecycle: enabled only for ``builtin`` over an NCP SQLite
        or (sync) pgvector store.

        An unknown provider name is a configuration error (raised), never a
        dynamic import. Any other store -- including the async-only
        ``AsyncPgvectorStore``, whose runtime never assembles context (use
        ``AsyncPostgresOntologyStore`` from async hosts) -- yields a disabled
        facade.
        """
        from ncp.stores.pgvector import PgvectorStore
        from ncp.stores.sqlite import SQLiteStore

        if config is None:
            return cls.disabled()
        ontology = config if isinstance(config, OntologyConfig) else config.ontology
        if not ontology.enabled or ontology.provider == "disabled":
            return cls.disabled()
        if ontology.provider != "builtin":
            raise ValueError(
                f"[ontology].provider {ontology.provider!r} is not available; "
                "expected 'builtin' or 'disabled'"
            )
        if isinstance(store, (SQLiteOntologyStore, PostgresOntologyStore)):
            return cls(ontology, store)
        if isinstance(store, PgvectorStore):
            registry = registry or SchemaRegistry.builtin()
            return cls(ontology, PostgresOntologyStore.inherit(store, registry))
        if not isinstance(store, SQLiteStore):
            backend = type(store).__name__
            return cls.disabled(f"ontology storage is not available for the {backend} backend")
        registry = registry or SchemaRegistry.builtin()
        return cls(ontology, SQLiteOntologyStore.inherit(store, registry))

    # -- discovery -------------------------------------------------------

    @property
    def enabled(self) -> bool:
        return self.store is not None

    def _require_enabled(self) -> SQLiteOntologyStore | PostgresOntologyStore:
        if self.store is None:
            raise OntologyError("disabled", self.disabled_reason or "ontology is disabled")
        return self.store

    def capabilities(self) -> dict[str, object]:
        if self.store is None:
            return {"enabled": False, "version": ONTOLOGY_API_VERSION}
        config = self.config
        packs = []
        for pack in self.store.registry.packs():
            if pack.id not in config.packs:
                continue
            packs.append({
                "id": pack.id,
                "version": pack.version,
                "entity_types": {
                    name: {"required": list(definition.required)}
                    for name, definition in sorted(pack.entity_types.items())
                },
                "relation_types": {
                    name: {
                        "from": list(definition.sources),
                        "to": list(definition.targets),
                        "max_outgoing": definition.max_outgoing,
                        "max_incoming": definition.max_incoming,
                        "allow_cycles": definition.allow_cycles,
                    }
                    for name, definition in sorted(pack.relation_types.items())
                },
            })
        return {
            "enabled": True,
            "version": ONTOLOGY_API_VERSION,
            "provider": config.provider,
            "packs": packs,
            "limits": {
                "max_hops": config.max_hops,
                "max_nodes": config.max_nodes,
                "max_edges": config.max_edges,
                "max_relation_types": MAX_RELATION_TYPES,
                "max_evidence_refs": MAX_EVIDENCE_REFS,
            },
            "retrieval_mode": config.retrieval_mode,
            "verified_only": config.verified_only,
            "tools": list(ONTOLOGY_TOOL_NAMES),
        }

    # -- helpers ---------------------------------------------------------

    def _pack_version(self, pack_id: object, version: object) -> tuple[str, int]:
        store = self._require_enabled()
        if pack_id is None:
            pack_id = self.config.packs[0] if self.config.packs else None
        if not isinstance(pack_id, str) or pack_id not in self.config.packs:
            raise OntologyError("invalid", "schema_pack is not enabled for this server")
        if version is None:
            latest = store.registry.latest_version(pack_id)
            if latest is None:
                raise OntologyError("invalid", "schema_pack is not enabled for this server")
            return pack_id, latest
        if isinstance(version, bool) or not isinstance(version, int):
            raise OntologyError("invalid", "schema_version must be an integer")
        return pack_id, version

    @staticmethod
    def _reject_unknown(payload: Mapping[str, object], allowed: frozenset[str], what: str) -> None:
        unknown = sorted(set(payload) - allowed)
        if unknown:
            raise OntologyError("invalid", f"{what} has unsupported fields: {unknown}")

    @staticmethod
    def _relation_view(relation: Relation, auth: AuthContext) -> dict[str, object]:
        """JSON relation for explicit reads; credential-like attribute keys are
        redacted unless the caller is an admin."""
        view = relation.model_dump(mode="json")
        if "admin" not in auth.capabilities:
            view["attributes"] = redact_attributes(view["attributes"])
        return view

    # -- read ------------------------------------------------------------

    def query(
        self,
        auth: AuthContext,
        *,
        entity_id: object = None,
        entity_type: object = None,
        key: object = None,
        schema_pack: object = None,
        relation_types: Sequence[object] | None = None,
        max_hops: object = None,
        max_nodes: object = None,
        max_edges: object = None,
        verified_only: object = None,
    ) -> dict[str, object]:
        """Resolve one seed exactly, then traverse within the configured limits.

        The seed is ``entity_id`` or ``key`` (exact canonical key, then exact
        alias; optional ``entity_type``/``schema_pack`` narrow it). Several
        matches return ``resolved: False`` with the candidates; the facade
        never guesses. A missing or out-of-scope seed is ``not_found``.
        """
        store = self._require_enabled()
        if (entity_id is None) == (key is None):
            raise OntologyError("invalid", "pass exactly one of entity_id or key")
        for value, field in ((entity_type, "entity_type"), (schema_pack, "schema_pack"),
                             (key, "key")):
            if value is not None and not isinstance(value, str):
                raise OntologyError("invalid", f"{field} must be a string")
        types: list[str] = []
        for item in relation_types or ():
            if not isinstance(item, str) or not item.strip():
                raise OntologyError("invalid", "relation_types must be non-empty strings")
            types.append(item.strip())
        if len(types) > MAX_RELATION_TYPES:
            raise OntologyError("limit", f"relation_types exceeds {MAX_RELATION_TYPES} entries")
        if verified_only is None:
            verified_only = self.config.verified_only
        if not isinstance(verified_only, bool):
            raise OntologyError("invalid", "verified_only must be a boolean")
        warnings: list[str] = []
        if not verified_only and self.config.verified_only and not (
            {"verify", "admin"} & auth.capabilities
        ):
            # Unverified (candidate/disputed) edges are only for callers who
            # could verify them, unless the server itself opts out.
            verified_only = True
            warnings.append("forced: verified_only=true (requires the verify or admin capability)")
        limits: dict[str, int] = {}
        for field, value, ceiling, minimum in (
            ("max_hops", max_hops, self.config.max_hops, 0),
            ("max_nodes", max_nodes, self.config.max_nodes, 1),
            ("max_edges", max_edges, self.config.max_edges, 1),
        ):
            limits[field], clamped = _limit(value, field, ceiling, minimum=minimum)
            if clamped:
                warnings.append(f"clamped: {field} to {ceiling}")
        applied = {**limits, "verified_only": verified_only}

        with _translate_errors():
            if entity_id is not None:
                seed_id = _uuid(entity_id, "entity_id")
                seed = store.get_entities([seed_id], auth=auth).get(str(seed_id))
                if seed is None or seed.deleted_at is not None:
                    raise OntologyError("not_found", "entity is not available")
            else:
                assert isinstance(key, str)
                match, matched_on = store.resolve_entities(
                    key, auth=auth, entity_type=entity_type,  # type: ignore[arg-type]
                    schema_pack=schema_pack,  # type: ignore[arg-type]
                    limit=MAX_CANDIDATES,
                )
                if not match.entity_ids:
                    raise OntologyError("not_found", "entity is not available")
                if match.resolved_id is None:
                    ids = match.entity_ids[:MAX_CANDIDATES]
                    found = store.get_entities(ids, auth=auth)
                    return {
                        "ok": True,
                        "resolved": False,
                        "ambiguity": {
                            "matched_key": match.matched_key,
                            "matched_on": matched_on,
                            "candidates": [
                                entity_summary(found[str(item)]) for item in ids
                                if str(item) in found
                            ],
                            "truncated": len(match.entity_ids) > MAX_CANDIDATES,
                        },
                        "paths": [],
                        "entities": {},
                        "warnings": warnings,
                        "limits": applied,
                    }
                # The seed may vanish between the two read transactions.
                seed = store.get_entities([match.resolved_id], auth=auth).get(
                    str(match.resolved_id)
                )
                if seed is None or seed.deleted_at is not None:
                    raise OntologyError("not_found", "entity is not available")
            request = GraphQuery(
                seeds=(seed.entity_id,), relation_types=tuple(types),
                verified_only=verified_only, **limits,
            )
            result = store.traverse(request, auth=auth)
            node_ids = {str(node) for path in result.paths for node in path.nodes}
            node_ids.add(str(seed.entity_id))
            entities = store.get_entities(sorted(node_ids), auth=auth)
        return {
            "ok": True,
            "resolved": True,
            "seed": entity_summary(seed),
            "paths": [
                {
                    "nodes": [str(node) for node in path.nodes],
                    "relations": [self._relation_view(relation, auth) for relation in path.relations],
                }
                for path in result.paths
            ],
            "entities": {
                entity_id: entity_summary(entity) for entity_id, entity in sorted(entities.items())
            },
            "warnings": warnings + list(result.warnings),
            "limits": applied,
        }

    def explain(self, auth: AuthContext, relation_id: object) -> dict[str, object]:
        """Relation, all evidence (with liveness) and validity, read-only and scoped."""
        store = self._require_enabled()
        target_id = _uuid(relation_id, "relation_id")
        with _translate_errors():
            explained = store.explain_relation(target_id, auth=auth)
            if explained is None:
                raise OntologyError("not_found", "relation is not available")
            relation, evidence = explained
            endpoints = store.get_entities(
                [relation.src_entity_id, relation.dst_entity_id], auth=auth,
            )
        now = datetime.now(timezone.utc)
        in_window = relation.valid_from <= now and (
            relation.valid_until is None or relation.valid_until > now
        )
        active = relation.status != "revoked" and in_window
        supported = any(item["live"] for item in evidence)
        source = endpoints.get(str(relation.src_entity_id))
        target = endpoints.get(str(relation.dst_entity_id))
        return {
            "ok": True,
            "relation": self._relation_view(relation, auth),
            "evidence": evidence,
            "validity": {
                "status": relation.status,
                "in_validity_window": in_window,
                "active": active,
                "evidence_supported": supported,
                "verified_and_supported": relation.status == "verified" and supported and active,
            },
            "source": None if source is None else entity_summary(source),
            "target": None if target is None else entity_summary(target),
        }

    # -- write -----------------------------------------------------------

    def assert_entity(
        self,
        auth: AuthContext,
        entity: Mapping[str, object],
        *,
        expected_version: object = None,
    ) -> dict[str, object]:
        """Create or update an entity in the caller's own scope."""
        store = self._require_enabled()
        if not isinstance(entity, Mapping):
            raise OntologyError("invalid", "entity must be an object")
        self._reject_unknown(entity, _ENTITY_FIELDS, "entity")
        if expected_version is not None and (
            isinstance(expected_version, bool) or not isinstance(expected_version, int)
        ):
            raise OntologyError("invalid", "expected_version must be an integer")
        aliases = entity.get("aliases", ())
        if isinstance(aliases, (list, tuple)) and len(aliases) > MAX_ALIASES:
            raise OntologyError("limit", f"aliases exceeds {MAX_ALIASES} entries")
        _bounded_json(entity.get("attributes", {}), "attributes")
        pack, version = self._pack_version(entity.get("schema_pack"), entity.get("schema_version"))
        payload = {**entity, "scope": auth.scope, "schema_pack": pack, "schema_version": version}
        with _translate_errors():
            stored = store.upsert_entity(
                Entity.model_validate(payload), auth=auth,
                expected_version=expected_version,  # type: ignore[arg-type]
            )
        return {
            "ok": True,
            "kind": "entity",
            "entity": {
                **entity_summary(stored),
                "aliases": list(stored.aliases),
                "attributes": stored.attributes,
                "revision": stored.revision,
                "created_at": None if stored.created_at is None else stored.created_at.isoformat(),
                "updated_at": None if stored.updated_at is None else stored.updated_at.isoformat(),
                "deleted_at": None if stored.deleted_at is None else stored.deleted_at.isoformat(),
            },
        }

    def assert_relation(
        self,
        auth: AuthContext,
        relation: Mapping[str, object],
        *,
        idempotency_key: object,
    ) -> dict[str, object]:
        """Assert a relation (with evidence refs) in the caller's own scope.

        ``created_by`` is always the authenticated principal. ``observed_at``
        is required and ``valid_from`` defaults to it, so the payload (and its
        idempotency fingerprint) is the same on replay. Without an explicit
        ``relation_id`` one is derived from the scope and ``idempotency_key``;
        an explicit one must name a relation already in the caller's scope.
        """
        store = self._require_enabled()
        if not isinstance(relation, Mapping):
            raise OntologyError("invalid", "relation must be an object")
        self._reject_unknown(relation, _RELATION_FIELDS, "relation")
        if not isinstance(idempotency_key, str) or not idempotency_key.strip():
            raise OntologyError("invalid", "idempotency_key is required")
        key = idempotency_key.strip()
        evidence = relation.get("evidence", ())
        if isinstance(evidence, (list, tuple)) and len(evidence) > MAX_EVIDENCE_REFS:
            raise OntologyError("limit", f"evidence exceeds {MAX_EVIDENCE_REFS} references")
        _bounded_json(relation.get("attributes", {}), "attributes")
        pack, version = self._pack_version(
            relation.get("schema_pack"), relation.get("schema_version"),
        )
        if relation.get("observed_at") is None:
            raise OntologyError("invalid", "relation.observed_at is required")
        derived = uuid5(NAMESPACE_URL, "ncp:ontology:v1:relation:" + json.dumps(
            [auth.scope.tenant_id, auth.scope.namespace, key],
        ))
        relation_id = relation.get("relation_id")
        if relation_id is None:
            relation_id = derived
        else:
            # An explicit relation_id only re-asserts a relation already in the
            # caller's own scope (or names its derived id). Creating arbitrary
            # ids would let a caller probe other scopes' relation ids (an
            # out-of-scope id was not_found, an unused one was created) and
            # squat the id another scope derives from its idempotency key.
            relation_id = _uuid(relation_id, "relation.relation_id")
            with _translate_errors():
                known = relation_id == derived or store.relation_in_scope(relation_id, auth=auth)
            if not known:
                raise OntologyError("not_found", "relation_id is not available")
        payload = {
            "valid_from": relation["observed_at"],
            **relation,
            "relation_id": relation_id,
            "scope": auth.scope,
            "created_by": auth.principal_id,
            "schema_pack": pack,
            "schema_version": version,
        }
        with _translate_errors():
            stored = store.assert_relation(
                Relation.model_validate(payload), auth=auth, idempotency_key=key,
            )
        return {"ok": True, "kind": "relation", "relation": stored.model_dump(mode="json")}

    def revoke_relation(
        self, auth: AuthContext, relation_id: object, *, reason: object,
    ) -> dict[str, object]:
        store = self._require_enabled()
        target_id = _uuid(relation_id, "relation_id")
        if not isinstance(reason, str) or not reason.strip():
            raise OntologyError("invalid", "reason is required")
        with _translate_errors():
            stored = store.revoke_relation(target_id, auth=auth, reason=reason.strip())
        return {"ok": True, "kind": "relation", "relation": stored.model_dump(mode="json")}


# -- facade cache ----------------------------------------------------------------

_CACHE_LOCK = threading.Lock()
_FACADE_CACHE: weakref.WeakKeyDictionary[object, tuple[OntologyConfig, OntologyFacade]] = (
    weakref.WeakKeyDictionary()
)


def cached_facade(config: NCPConfig | OntologyConfig | None, store: object) -> OntologyFacade:
    """``OntologyFacade.from_config`` memoized per NCP store object.

    Building an enabled facade opens the ontology store and runs its
    migration, so per-call construction (``ncp.api`` helpers build handlers
    on every call) is avoided. A changed ``[ontology]`` section rebuilds the
    entry; a disabled section never touches the cache or the database.
    """
    if config is None:
        return OntologyFacade.disabled()
    ontology = config if isinstance(config, OntologyConfig) else config.ontology
    if not ontology.enabled or ontology.provider == "disabled":
        return OntologyFacade.disabled()
    try:
        entry = _FACADE_CACHE.get(store)
    except TypeError:  # unhashable / not weak-referenceable store: no caching
        return OntologyFacade.from_config(ontology, store)
    if entry is not None and entry[0] == ontology:
        return entry[1]
    with _CACHE_LOCK:
        entry = _FACADE_CACHE.get(store)
        if entry is not None and entry[0] == ontology:
            return entry[1]
        facade = OntologyFacade.from_config(ontology, store)
        _FACADE_CACHE[store] = (ontology, facade)
        return facade


# -- MCP tool handlers ---------------------------------------------------------

PrincipalProvider = Callable[[], "Principal | None"]


def _namespace(args: Mapping[str, object], *, write: bool) -> str:
    raw = args.get("pipeline_id")
    if raw is None or raw == "":
        if write:
            raise OntologyError(
                "invalid", "pipeline_id is required for writes; pass 'global' for the global namespace",
            )
        return GLOBAL_NAMESPACE
    if not isinstance(raw, str):
        raise OntologyError("invalid", "pipeline_id must be a string")
    return raw


def make_tool_handlers(
    facade: OntologyFacade | Callable[[], OntologyFacade], current_principal: PrincipalProvider,
) -> dict[str, Callable[[dict[str, object]], dict[str, object]]]:
    """MCP handlers. The principal comes only from ``current_principal`` (a
    trusted transport channel), never from tool arguments. Every failure is
    returned as ``{"ok": False, "error": {...}}``; nothing falls back to an
    unscoped call.

    ``facade`` may be a zero-argument provider, resolved on the first call:
    building the handler table then never opens (or migrates) the ontology
    store, e.g. under the core tool profile where these tools are unlisted.
    """
    if isinstance(facade, OntologyFacade):
        fixed = facade

        def provider() -> OntologyFacade:
            return fixed
    else:
        provider = facade

    def guarded(
        operation: Callable[[dict[str, object]], dict[str, object]],
    ) -> Callable[[dict[str, object]], dict[str, object]]:
        def handler(args: dict[str, object]) -> dict[str, object]:
            try:
                return operation(args)
            except OntologyError as exc:
                return exc.to_result()

        handler.__name__ = operation.__name__
        return handler

    def auth(args: Mapping[str, object], *, write: bool) -> AuthContext:
        provider()._require_enabled()  # disabled beats any auth/argument error
        return auth_context_for(
            current_principal(), _namespace(args, write=write), config=provider().config,
        )

    def capabilities(args: dict[str, object]) -> dict[str, object]:
        return {"ok": True, **provider().capabilities()}

    def query(args: dict[str, object]) -> dict[str, object]:
        context = auth(args, write=False)
        relation_types = args.get("relation_types")
        if relation_types is not None and not isinstance(relation_types, list):
            raise OntologyError("invalid", "relation_types must be an array")
        return provider().query(
            context,
            entity_id=args.get("entity_id"),
            entity_type=args.get("entity_type"),
            key=args.get("key"),
            schema_pack=args.get("schema_pack"),
            relation_types=relation_types,
            max_hops=args.get("max_hops"),
            max_nodes=args.get("max_nodes"),
            max_edges=args.get("max_edges"),
            verified_only=args.get("verified_only"),
        )

    def explain(args: dict[str, object]) -> dict[str, object]:
        return provider().explain(auth(args, write=False), args.get("relation_id"))

    def assert_(args: dict[str, object]) -> dict[str, object]:
        kind = args.get("kind")
        if kind not in ("entity", "relation"):
            provider()._require_enabled()
            raise OntologyError("invalid", "kind must be 'entity' or 'relation'")
        context = auth(args, write=True)
        if kind == "entity":
            entity = args.get("entity")
            if not isinstance(entity, dict):
                raise OntologyError("invalid", "entity must be an object")
            return provider().assert_entity(
                context, entity, expected_version=args.get("expected_version"),
            )
        relation = args.get("relation")
        if not isinstance(relation, dict):
            raise OntologyError("invalid", "relation must be an object")
        return provider().assert_relation(
            context, relation, idempotency_key=args.get("idempotency_key"),
        )

    def revoke(args: dict[str, object]) -> dict[str, object]:
        return provider().revoke_relation(
            auth(args, write=True), args.get("relation_id"), reason=args.get("reason"),
        )

    return {
        "ncp_ontology_capabilities": guarded(capabilities),
        "ncp_ontology_query": guarded(query),
        "ncp_ontology_explain": guarded(explain),
        "ncp_ontology_assert": guarded(assert_),
        "ncp_ontology_revoke": guarded(revoke),
    }


__all__ = [
    "DEFAULT_TENANT", "ERROR_CODES", "ONTOLOGY_API_VERSION", "ONTOLOGY_TOOL_NAMES",
    "OntologyError", "OntologyFacade", "auth_context_for", "cached_facade", "entity_summary",
    "make_tool_handlers",
]
