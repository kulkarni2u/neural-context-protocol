"""Declarative ontology seeding: load a manifest of entities and relations into
one namespace, idempotently, with evidence for every relation.

NCP never extracts graph facts from memory writes, so the ontology starts
empty. This module is the supported way to populate it from an authoritative
source (an entity registry, a service catalog, CODEOWNERS, deploy manifests)
that a harness exports as a small JSON/TOML/YAML manifest::

    pipeline = "shop"
    source = "entity_registry"            # becomes the asserting principal seed:<source>
    observed_at = "2026-11-01T00:00:00Z"   # optional; defaults to first-seen time

    [[entities]]
    type = "Service"
    key = "order-api"
    aliases = ["orders-api"]
    attributes = { environment = "prod" }

    [[entities]]
    type = "KafkaTopic"
    key = "order.events"

    [[relations]]
    src = "order-api"                 # entity key, or "Type:key" when ambiguous
    type = "PRODUCES"
    dst = "KafkaTopic:order.events"
    evidence = "order-api publishes OrderPlaced to order.events (registry row 12)."
    # or / and: attestation = "entity-registry.yaml#order-api"

Guarantees:

* **All-or-nothing validation.** The manifest is checked against the enabled
  schema pack (types, relation triples, endpoints, evidence) before anything
  is written; a bad manifest writes nothing.
* **Every relation cites evidence.** ``evidence`` text is written as an
  ordinary NCP memory chunk (``semantic`` / ``tool_result``) and cited as
  ``chunk`` evidence; ``attestation`` is cited as ``external_attestation``.
  ``verified`` relations need at least one of the two.
* **Idempotent and diff-based.** Relation ids are derived from
  ``(namespace, source, src, type, dst)``; a re-run with an unchanged manifest writes
  nothing, a changed relation is updated in place (revision + 1).
* **Prune keeps the graph in sync.** With ``prune=True`` a relation that this
  source asserted earlier but that is no longer in the manifest is *closed*
  (``valid_until`` = now): it leaves traversal and context immediately, keeps
  its history, and re-adding it to the manifest reopens it. Relations asserted
  by any other principal are never touched.

The seeder runs in-process as a trusted local operator (it already has direct
access to the store), so it asserts with ``read``/``write``/``verify``
(``admin`` too for the ``global`` namespace). Over MCP, use
``ncp_ontology_assert`` with a principal that holds ``verify`` instead.
"""

from __future__ import annotations

import hashlib
import json
import tomllib
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Literal
from uuid import NAMESPACE_URL, UUID, uuid5

from pydantic import AwareDatetime, Field, JsonValue, ValidationError, model_validator

from ncp.ontology.facade import (
    DEFAULT_TENANT, OntologyError, OntologyFacade, _translate_errors, _validation_message,
)
from ncp.ontology.models import (
    GLOBAL_NAMESPACE, AuthContext, Contract, Entity, EvidenceRef, Identifier, Relation, Scope,
)

if TYPE_CHECKING:
    from ncp.ontology.registry import SchemaPack

DEFAULT_SOURCE = "ontology_seeder"
DEFAULT_CONFIDENCE = 0.95
SEED_CHUNK_PREFIX = "ont-seed"
MAX_SEED_ENTITIES = 10_000
MAX_SEED_RELATIONS = 50_000

SeedStatus = Literal["verified", "candidate"]


class SeedEntity(Contract):
    type: Identifier
    key: Identifier
    display_name: Identifier | None = None
    aliases: tuple[Identifier, ...] = ()
    attributes: dict[str, JsonValue] = Field(default_factory=dict)


class SeedRelation(Contract):
    src: Identifier
    type: Identifier
    dst: Identifier
    status: SeedStatus = "verified"
    confidence: Annotated[float, Field(strict=False, ge=0, le=1)] = DEFAULT_CONFIDENCE
    evidence: Annotated[str, Field(min_length=1, max_length=8_000)] | None = None
    attestation: Identifier | None = None
    revision: Identifier | None = None
    observed_at: AwareDatetime | None = None
    attributes: dict[str, JsonValue] = Field(default_factory=dict)

    @model_validator(mode="after")
    def requires_evidence(self) -> SeedRelation:
        if self.status == "verified" and not (
            (self.evidence and self.evidence.strip()) or self.attestation
        ):
            raise ValueError(
                "a verified relation needs 'evidence' text or an 'attestation' reference"
            )
        return self


class SeedManifest(Contract):
    pipeline: Identifier | None = None
    source: Identifier = DEFAULT_SOURCE
    schema_pack: Identifier | None = None
    schema_version: Annotated[int, Field(strict=True, ge=1)] | None = None
    observed_at: AwareDatetime | None = None
    entities: Annotated[tuple[SeedEntity, ...], Field(max_length=MAX_SEED_ENTITIES)] = ()
    relations: Annotated[tuple[SeedRelation, ...], Field(max_length=MAX_SEED_RELATIONS)] = ()


@dataclass
class SeedReport:
    """What a seed run did (or, with ``dry_run``, would do)."""

    namespace: str
    principal_id: str
    dry_run: bool
    entities_created: int = 0
    entities_updated: int = 0
    entities_unchanged: int = 0
    relations_created: int = 0
    relations_updated: int = 0
    relations_unchanged: int = 0
    relations_reopened: int = 0
    relations_pruned: int = 0
    evidence_chunks: int = 0
    stale: list[str] = field(default_factory=list)
    pruned: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, object]:
        return {
            "namespace": self.namespace,
            "principal_id": self.principal_id,
            "dry_run": self.dry_run,
            "entities": {"created": self.entities_created, "updated": self.entities_updated,
                         "unchanged": self.entities_unchanged},
            "relations": {"created": self.relations_created, "updated": self.relations_updated,
                          "unchanged": self.relations_unchanged,
                          "reopened": self.relations_reopened, "pruned": self.relations_pruned},
            "evidence_chunks": self.evidence_chunks,
            "stale": list(self.stale),
            "pruned": list(self.pruned),
            "warnings": list(self.warnings),
        }


# -- manifest loading ----------------------------------------------------------


def load_manifest(path: str | Path) -> SeedManifest:
    """Parse a ``.json``, ``.toml`` or ``.yaml``/``.yml`` manifest.

    YAML needs PyYAML installed; JSON and TOML use the standard library.
    Raises :class:`OntologyError` (``invalid``) for unreadable or malformed input.
    """
    file = Path(path)
    suffix = file.suffix.lower()
    try:
        text = file.read_text(encoding="utf-8")
    except OSError as exc:
        raise OntologyError("invalid", f"cannot read manifest {file}: {exc.strerror}") from None
    try:
        if suffix == ".json":
            raw = json.loads(text)
        elif suffix == ".toml":
            raw = tomllib.loads(text)
        elif suffix in (".yaml", ".yml"):
            try:
                import yaml  # type: ignore[import-untyped]
            except ImportError:
                raise OntologyError(
                    "invalid", "YAML manifests need PyYAML (pip install pyyaml); "
                    "or use .json / .toml",
                ) from None
            raw = yaml.safe_load(text)
        else:
            raise OntologyError("invalid", "manifest must be .json, .toml, .yaml or .yml")
    except OntologyError:
        raise
    except Exception as exc:  # json/toml/yaml decode errors
        raise OntologyError("invalid", f"cannot parse manifest {file}: {exc}") from None
    return parse_manifest(raw)


def parse_manifest(raw: object) -> SeedManifest:
    if not isinstance(raw, Mapping):
        raise OntologyError("invalid", "manifest must be an object/table at the top level")
    try:
        return SeedManifest.model_validate(dict(raw))
    except ValidationError as exc:
        raise OntologyError("invalid", _validation_message(exc)) from None


# -- planning ----------------------------------------------------------------------


@dataclass(frozen=True)
class _PlannedRelation:
    spec: SeedRelation
    src: Entity
    dst: Entity
    relation_id: UUID
    chunk_id: str | None
    evidence_text: str | None


def seed_principal(source: str) -> str:
    return f"seed:{source}"


def seed_relation_id(
    scope: Scope, principal_id: str, src: Entity, relation_type: str, dst: Entity,
) -> UUID:
    """Stable per (scope, source, triple): each source owns its own assertion."""
    return uuid5(NAMESPACE_URL, "ncp:ontology:v1:seed:" + json.dumps([
        scope.tenant_id, scope.namespace, principal_id, str(src.entity_id), relation_type,
        str(dst.entity_id),
    ]))


def _digest(*parts: object) -> str:
    return hashlib.sha256(
        json.dumps(parts, sort_keys=True, separators=(",", ":"), default=str).encode(),
    ).hexdigest()


def _pack(facade: OntologyFacade, pack_id: str, version: int) -> SchemaPack:
    store = facade._require_enabled()
    for pack in store.registry.packs():
        if (pack.id, pack.version) == (pack_id, version):
            return pack
    raise OntologyError("invalid", f"schema pack {pack_id} v{version} is not registered")


def _resolve_endpoint(
    ref: str, entities: Mapping[tuple[str, str], Entity], by_key: Mapping[str, list[Entity]],
    where: str,
) -> Entity:
    if ":" in ref:
        entity_type, _, key = ref.partition(":")
        entity = entities.get((entity_type.strip(), Entity.canonicalize(key)))
        if entity is not None:
            return entity
    matches = by_key.get(Entity.canonicalize(ref), [])
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        types = sorted(entity.entity_type for entity in matches)
        raise OntologyError(
            "invalid", f"{where}: {ref!r} is ambiguous ({', '.join(types)}); use 'Type:key'",
        )
    raise OntologyError("invalid", f"{where}: {ref!r} is not an entity declared in the manifest")


def _plan(
    facade: OntologyFacade, manifest: SeedManifest, scope: Scope, principal_id: str,
) -> tuple[list[Entity], list[_PlannedRelation]]:
    pack_id, version = facade._pack_version(manifest.schema_pack, manifest.schema_version)
    pack = _pack(facade, pack_id, version)

    entities: dict[tuple[str, str], Entity] = {}
    by_key: dict[str, list[Entity]] = {}
    for index, spec in enumerate(manifest.entities):
        where = f"entities[{index}]"
        if spec.type not in pack.entity_types:
            allowed = ", ".join(sorted(pack.entity_types))
            raise OntologyError(
                "invalid", f"{where}: unknown entity type {spec.type!r} (pack {pack_id} "
                f"v{version} has: {allowed})",
            )
        attributes = dict(spec.attributes)
        # Most pack types require a ``name``; default it to the key.
        if "name" in pack.entity_types[spec.type].required and attributes.get("name") is None:
            attributes["name"] = spec.key
        try:
            entity = Entity.model_validate({
                "scope": scope, "entity_type": spec.type, "canonical_key": spec.key,
                "display_name": spec.display_name or spec.key, "aliases": spec.aliases,
                "attributes": attributes, "schema_pack": pack_id, "schema_version": version,
            })
            facade._require_enabled().registry.validate_entity(entity)
        except ValidationError as exc:
            raise OntologyError("invalid", f"{where}: {_validation_message(exc)}") from None
        except ValueError as exc:
            raise OntologyError("invalid", f"{where}: {exc}") from None
        identity = (entity.entity_type, entity.canonical_key)
        if identity in entities:
            raise OntologyError(
                "invalid", f"{where}: duplicate entity {spec.type}:{entity.canonical_key}",
            )
        entities[identity] = entity
        by_key.setdefault(entity.canonical_key, []).append(entity)

    planned: list[_PlannedRelation] = []
    seen: set[UUID] = set()
    for index, item in enumerate(manifest.relations):
        where = f"relations[{index}]"
        definition = pack.relation_types.get(item.type)
        if definition is None:
            allowed = ", ".join(sorted(pack.relation_types))
            raise OntologyError(
                "invalid", f"{where}: unknown relation type {item.type!r} (pack {pack_id} "
                f"v{version} has: {allowed})",
            )
        src = _resolve_endpoint(item.src, entities, by_key, f"{where}.src")
        dst = _resolve_endpoint(item.dst, entities, by_key, f"{where}.dst")
        if src.entity_type not in definition.sources or dst.entity_type not in definition.targets:
            raise OntologyError(
                "invalid", f"{where}: {src.entity_type} -{item.type}-> {dst.entity_type} is not "
                f"allowed (from: {', '.join(definition.sources)}; to: "
                f"{', '.join(definition.targets)})",
            )
        relation_id = seed_relation_id(scope, principal_id, src, item.type, dst)
        if relation_id in seen:
            raise OntologyError(
                "invalid", f"{where}: duplicate relation {item.src} -{item.type}-> {item.dst}",
            )
        seen.add(relation_id)
        text = item.evidence.strip() if item.evidence else None
        chunk_id = (
            f"{SEED_CHUNK_PREFIX}-{_digest(scope.namespace, str(relation_id))[:24]}"
            if text else None
        )
        planned.append(_PlannedRelation(item, src, dst, relation_id, chunk_id, text))
    return list(entities.values()), planned


# -- execution -----------------------------------------------------------------


def _evidence_refs(item: _PlannedRelation, observed_at: datetime) -> tuple[EvidenceRef, ...]:
    refs: list[EvidenceRef] = []
    if item.chunk_id and item.evidence_text:
        refs.append(EvidenceRef(
            evidence_type="chunk", evidence_id=item.chunk_id,
            source_revision=_digest(item.evidence_text)[:16], observed_at=observed_at,
        ))
    if item.spec.attestation:
        refs.append(EvidenceRef(
            evidence_type="external_attestation", evidence_id=item.spec.attestation,
            source_revision=item.spec.revision or "1", observed_at=observed_at,
        ))
    return tuple(refs)


def _evidence_keys(refs: Sequence[EvidenceRef]) -> set[tuple[str, str, str]]:
    return {(ref.evidence_type, ref.evidence_id, ref.source_revision) for ref in refs}


def _active(relation: Relation, now: datetime) -> bool:
    return relation.status != "revoked" and (
        relation.valid_until is None or relation.valid_until > now
    )


def _same(desired: Relation, existing: Relation) -> bool:
    return (
        desired.status == existing.status
        and desired.confidence == existing.confidence
        and desired.valid_from == existing.valid_from
        and desired.valid_until == existing.valid_until
        and desired.observed_at == existing.observed_at
        and desired.attributes == existing.attributes
        and _evidence_keys(desired.evidence) <= _evidence_keys(existing.evidence)
    )


def _assert(store: object, relation: Relation, auth: AuthContext, existing: Relation | None) -> None:
    # The key binds the payload to the revision it updates, so a payload that
    # matches an *earlier* state (e.g. reopening a pruned relation) is applied
    # again instead of replaying the old write.
    key = "seed:" + _digest(
        relation.model_dump(mode="json"), None if existing is None else existing.revision,
    )[:48]
    with _translate_errors():
        store.assert_relation(relation, auth=auth, idempotency_key=key)  # type: ignore[attr-defined]


def seed_ontology(
    facade: OntologyFacade,
    manifest: SeedManifest,
    *,
    namespace: str | None = None,
    write_chunk: Callable[[str, str, str, str], None] | None = None,
    prune: bool = False,
    dry_run: bool = False,
    status_override: SeedStatus | None = None,
    now: datetime | None = None,
) -> SeedReport:
    """Apply ``manifest`` to the ontology behind ``facade``.

    ``namespace`` overrides ``manifest.pipeline``. ``write_chunk(chunk_id,
    content, namespace, written_by)`` persists evidence text as an NCP memory
    chunk; it is required when any relation carries ``evidence`` text (and not
    called on a dry run). ``status_override`` forces every relation to one
    status, e.g. ``candidate`` to stage a manifest without verifying it.
    """
    store = facade._require_enabled()
    target = namespace or manifest.pipeline
    if not target:
        raise OntologyError("invalid", "no namespace: set 'pipeline' in the manifest or pass one")
    with _translate_errors():
        scope = Scope(tenant_id=DEFAULT_TENANT, namespace=target)
    principal = seed_principal(manifest.source)
    capabilities = {"read", "write", "verify"}
    if scope.namespace == GLOBAL_NAMESPACE:
        capabilities.add("admin")
    auth = AuthContext(
        principal_id=principal, scope=scope, capabilities=frozenset(capabilities),  # type: ignore[arg-type]
    )
    if status_override is not None:
        manifest = manifest.model_copy(update={"relations": tuple(
            relation.model_copy(update={"status": status_override})
            for relation in manifest.relations
        )})
    entities, planned = _plan(facade, manifest, scope, principal)
    if any(item.evidence_text for item in planned) and write_chunk is None and not dry_run:
        raise OntologyError("invalid", "evidence text needs a memory store to write chunks to")

    clock = now or datetime.now(timezone.utc)
    report = SeedReport(namespace=scope.namespace, principal_id=principal, dry_run=dry_run)

    # Entities: upsert through the facade so its validation/redaction applies.
    with _translate_errors():
        current = store.get_entities([entity.entity_id for entity in entities], auth=auth)  # type: ignore[attr-defined]
    for entity in entities:
        before = current.get(str(entity.entity_id))
        if before is not None and before.deleted_at is None and (
            before.display_name, before.aliases, before.attributes,
        ) == (entity.display_name, entity.aliases, entity.attributes):
            report.entities_unchanged += 1
            continue
        if before is None:
            report.entities_created += 1
        else:
            report.entities_updated += 1
        if not dry_run:
            facade.assert_entity(auth, {
                "entity_type": entity.entity_type, "canonical_key": entity.canonical_key,
                "display_name": entity.display_name, "aliases": list(entity.aliases),
                "attributes": entity.attributes, "schema_pack": entity.schema_pack,
                "schema_version": entity.schema_version,
            })

    with _translate_errors():
        previous = {
            relation.relation_id: relation
            for relation in store.relations_asserted_by(principal, auth=auth)  # type: ignore[attr-defined]
        }

    for item in planned:
        existing = previous.get(item.relation_id)
        observed = item.spec.observed_at or manifest.observed_at or (
            existing.observed_at if existing is not None else clock
        )
        if existing is not None and existing.status == "revoked":
            report.warnings.append(
                f"{item.spec.src} -{item.spec.type}-> {item.spec.dst} was revoked and is skipped",
            )
            continue
        relation = Relation.model_validate({
            "relation_id": item.relation_id, "scope": scope,
            "src_entity_id": item.src.entity_id, "dst_entity_id": item.dst.entity_id,
            "relation_type": item.spec.type, "status": item.spec.status,
            "confidence": item.spec.confidence,
            "valid_from": existing.valid_from if existing is not None else observed,
            "valid_until": None, "observed_at": observed, "created_by": principal,
            "schema_pack": item.src.schema_pack, "schema_version": item.src.schema_version,
            "attributes": item.spec.attributes,
            "evidence": _evidence_refs(item, observed),
        })
        if existing is not None and _same(relation, existing):
            report.relations_unchanged += 1
            continue
        if existing is None:
            report.relations_created += 1
        elif not _active(existing, clock):
            report.relations_reopened += 1
        else:
            report.relations_updated += 1
        if dry_run:
            continue
        if item.chunk_id and item.evidence_text and write_chunk is not None:
            write_chunk(item.chunk_id, item.evidence_text, scope.namespace, principal)
            report.evidence_chunks += 1
        _assert(store, relation, auth, existing)

    wanted = {item.relation_id for item in planned}
    leftover = [
        relation for relation_id, relation in previous.items()
        if relation_id not in wanted and _active(relation, clock)
    ]
    names: dict[str, Entity] = {}
    if leftover:
        with _translate_errors():
            names = store.get_entities(  # type: ignore[attr-defined]
                [item.src_entity_id for item in leftover]
                + [item.dst_entity_id for item in leftover], auth=auth,
            )

    def label(relation: Relation) -> str:
        def name(entity_id: UUID) -> str:
            entity = names.get(str(entity_id))
            return str(entity_id) if entity is None else f"{entity.entity_type}:{entity.canonical_key}"
        return f"{name(relation.src_entity_id)} -{relation.relation_type}-> {name(relation.dst_entity_id)}"

    for existing in leftover:
        relation_id = existing.relation_id
        if not prune:
            report.stale.append(label(existing))
            continue
        report.relations_pruned += 1
        report.pruned.append(label(existing))
        if dry_run:
            continue
        if existing.valid_from >= clock:  # not valid yet: an empty window; revoke instead
            facade.revoke_relation(auth, relation_id, reason="removed from seed manifest")
            continue
        closed = existing.model_copy(update={"valid_until": clock})
        try:
            _assert(store, closed, auth, existing)
        except OntologyError:
            # e.g. its evidence chunk was forgotten, so a verified update is
            # refused: withdraw the claim instead.
            facade.revoke_relation(auth, relation_id, reason="removed from seed manifest")
    return report


def memory_chunk_writer(memory_store: object) -> Callable[[str, str, str, str], None]:
    """``write_chunk`` callback that stores evidence text as an NCP memory chunk."""
    from ncp.types import SubconsciousChunk

    def write(chunk_id: str, content: str, namespace: str, written_by: str) -> None:
        chunk = SubconsciousChunk(
            chunk_id=chunk_id, layer="semantic", content=content, src="tool_result",
            written_by=written_by, pipeline_id=namespace,
            scope="global" if namespace == GLOBAL_NAMESPACE else "pipeline",
        )
        memory_store.write(chunk, allow_duplicate=True)  # type: ignore[attr-defined]

    return write


__all__ = [
    "DEFAULT_SOURCE", "SeedEntity", "SeedManifest", "SeedRelation", "SeedReport",
    "load_manifest", "memory_chunk_writer", "parse_manifest", "seed_ontology",
    "seed_principal", "seed_relation_id",
]
