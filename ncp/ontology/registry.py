"""Versioned schema packs and structural validation for domain assertions."""

import json
from collections.abc import Iterable
from importlib.resources import files
from pathlib import Path
from typing import Annotated, Literal

from pydantic import Field, StringConstraints, ValidationError, model_validator

from ncp.ontology.models import (
    GLOBAL_NAMESPACE, Contract, Entity, Identifier, PositiveInt, Relation,
)

# Relationship-intent categories a relation type can serve during automatic
# enrichment (see ``ncp.ontology.trigger.classify_intent``). A relation type
# with no category is only reached by mixed/unknown-intent expansion.
RelationIntent = Literal["dependency", "ownership", "deployment"]


class EntityType(Contract):
    required: tuple[Identifier, ...] = ()
    sensitive: tuple[Identifier, ...] = ()
    alias_strategy: Literal["exact"] = "exact"
    # Short label used by the compact context serializer (``svc:checkout``).
    tag: Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9]{0,7}$")] | None = None


class RelationType(Contract):
    sources: tuple[Identifier, ...] = Field(alias="from", min_length=1)
    targets: tuple[Identifier, ...] = Field(alias="to", min_length=1)
    max_outgoing: PositiveInt | None = None
    max_incoming: PositiveInt | None = None
    allow_cycles: bool = Field(default=True, strict=True)
    # Intent categories this relation type answers, e.g. ``dependency`` for
    # "what does X depend on" (outgoing) and "what breaks if X fails" (incoming).
    intents: tuple[RelationIntent, ...] = ()


class SchemaPack(Contract):
    id: Identifier
    version: PositiveInt
    entity_types: dict[Identifier, EntityType] = Field(min_length=1)
    relation_types: dict[Identifier, RelationType] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_endpoints(self) -> "SchemaPack":
        for name, relation in self.relation_types.items():
            if set(relation.sources + relation.targets) - self.entity_types.keys():
                raise ValueError(f"{name} references an unknown entity type")
        return self


BUILTIN_PACK_FILES = (
    "infrastructure.v1.json", "infrastructure.v2.json", "infrastructure.v3.json",
)
# The version a write gets when it names no ``schema_version`` and
# ``[ontology].pack_versions`` pins none. Newer built-in versions are opt-in:
# an entity's version cannot change while live relations reference it, so
# silently defaulting existing graphs to a new version would break them.
DEFAULT_PACK_VERSIONS: dict[str, int] = {"infrastructure": 1}


class PackError(ValueError):
    """A custom schema pack file is unreadable, invalid or conflicts with a registered pack."""


class PackExtends(Contract):
    id: Identifier
    version: PositiveInt


class SchemaRegistry:
    def __init__(self) -> None:
        self._packs: dict[tuple[str, int], SchemaPack] = {}
        self._sources: dict[tuple[str, int], str] = {}

    @classmethod
    def builtin(cls, pack_paths: Iterable[str | Path] = ()) -> "SchemaRegistry":
        """Built-in packs plus any custom pack files (``[ontology].pack_paths``)."""
        registry = cls()
        for name in BUILTIN_PACK_FILES:
            resource = files("ncp.ontology").joinpath("packs", name)
            registry.register(SchemaPack.model_validate_json(resource.read_bytes()))
        for path in pack_paths:
            registry.load_file(path)
        return registry

    def load_file(self, path: str | Path) -> SchemaPack:
        """Validate a custom pack JSON file and register it.

        The file is either a new pack (``id``/``version``/``entity_types``/
        ``relation_types``) or an extension of a registered one: add
        ``"extends": {"id": ..., "version": ...}`` and an unused ``version``;
        the result is the base plus the new entity/relation types and extra
        ``from``/``to`` endpoints on existing relation types.
        """
        file = Path(path)
        try:
            raw = json.loads(file.read_text(encoding="utf-8"))
        except OSError as exc:
            raise PackError(f"{file}: cannot read pack file: {exc.strerror}") from None
        except ValueError as exc:
            raise PackError(f"{file}: invalid JSON: {exc}") from None
        if not isinstance(raw, dict):
            raise PackError(f"{file}: pack must be a JSON object")
        try:
            pack = self._build(raw)
            key = (pack.id, pack.version)
            if key in self._packs:
                raise PackError(
                    f"schema pack {pack.id} v{pack.version} is already registered "
                    f"({self._sources[key]}); choose an unused version"
                )
        except PackError as exc:
            raise PackError(f"{file}: {exc}") from None
        except ValidationError as exc:
            problems = "; ".join(
                f"{'.'.join(str(part) for part in err['loc']) or 'pack'}: {err['msg']}"
                for err in exc.errors()
            )
            raise PackError(f"{file}: {problems}") from None
        self.register(pack, source=str(file))
        return pack

    def _build(self, raw: dict[str, object]) -> SchemaPack:
        raw = dict(raw)
        extends_raw = raw.pop("extends", None)
        if extends_raw is None:
            return SchemaPack.model_validate(raw)
        extends = PackExtends.model_validate(extends_raw)
        if raw.get("id") != extends.id:
            raise PackError(f"'id' must equal extends.id ({extends.id!r})")
        base = self._packs.get((extends.id, extends.version))
        if base is None:
            raise PackError(f"extends unknown pack {extends.id} v{extends.version}")
        merged = base.model_dump(mode="json", by_alias=True)
        entities = raw.get("entity_types") or {}
        relations = raw.get("relation_types") or {}
        if not isinstance(entities, dict) or not isinstance(relations, dict):
            raise PackError("entity_types and relation_types must be objects")
        for name, spec in entities.items():
            if name in merged["entity_types"]:
                raise PackError(f"entity type {name!r} already exists in the base pack")
            merged["entity_types"][name] = spec
        for name, spec in relations.items():
            current = merged["relation_types"].get(name)
            if current is None:
                merged["relation_types"][name] = spec
                continue
            if not isinstance(spec, dict) or set(spec) - {"from", "to"}:
                raise PackError(
                    f"relation type {name!r} already exists; an extension may only add "
                    "'from'/'to' endpoints"
                )
            for field_name in ("from", "to"):
                extra = spec.get(field_name) or []
                current[field_name] = [*current[field_name],
                                       *(v for v in extra if v not in current[field_name])]
        merged["version"] = raw.get("version")
        return SchemaPack.model_validate(merged)

    def register(self, pack: SchemaPack, *, source: str = "builtin") -> None:
        key = (pack.id, pack.version)
        if key in self._packs:
            raise ValueError("schema pack version is already registered")
        self._packs[key] = pack.model_copy(deep=True)
        self._sources[key] = source

    def source(self, pack_id: str, version: int) -> str:
        """``"builtin"`` or the file path a pack was loaded from."""
        return self._sources[pack_id, version]

    def packs(self) -> tuple[SchemaPack, ...]:
        """Registered packs ordered by (id, version)."""
        return tuple(self._packs[key] for key in sorted(self._packs))

    def has_version(self, pack_id: str, version: int) -> bool:
        return (pack_id, version) in self._packs

    def default_version(self, pack_id: str) -> int | None:
        """The stable default for writes that name no version (see
        :data:`DEFAULT_PACK_VERSIONS`); the latest for packs without one."""
        pinned = DEFAULT_PACK_VERSIONS.get(pack_id)
        if pinned is not None and (pack_id, pinned) in self._packs:
            return pinned
        return self.latest_version(pack_id)

    def latest_version(self, pack_id: str) -> int | None:
        versions = [version for (name, version) in self._packs if name == pack_id]
        return max(versions) if versions else None

    def _pack(self, pack_id: str, version: int) -> SchemaPack:
        try:
            return self._packs[pack_id, version]
        except KeyError:
            raise ValueError("unknown schema pack version") from None

    def relation_definition(self, relation: Relation) -> RelationType:
        definition = self._pack(relation.schema_pack, relation.schema_version).relation_types.get(
            relation.relation_type
        )
        if definition is None:
            raise ValueError("relation triple is not allowed by the schema pack")
        return definition

    def validate_entity(self, entity: Entity) -> None:
        pack = self._pack(entity.schema_pack, entity.schema_version)
        definition = pack.entity_types.get(entity.entity_type)
        if definition is None:
            raise ValueError("unknown entity type")
        for name in definition.required:
            value = entity.attributes.get(name)
            if value is None or (isinstance(value, str) and not value.strip()):
                raise ValueError(f"missing required attribute: {name}")

    def validate_relation(self, relation: Relation, *, source: Entity, target: Entity) -> None:
        """Check local invariants. Store-level checks must run inside the write transaction."""
        # A relation lives in its source's namespace; its target shares that
        # namespace or is global in the same tenant. A global relation
        # therefore only reaches global targets.
        target_global = (
            target.scope.tenant_id == relation.scope.tenant_id
            and target.scope.namespace == GLOBAL_NAMESPACE
        )
        if source.scope != relation.scope:
            raise ValueError("relation source must share its scope")
        if target.scope != relation.scope and not target_global:
            raise ValueError("relation target must share its scope or be global in its tenant")
        if source.entity_id != relation.src_entity_id or target.entity_id != relation.dst_entity_id:
            raise ValueError("relation endpoint IDs do not match")
        pack_key = (relation.schema_pack, relation.schema_version)
        for entity in (source, target):
            if entity.deleted_at is not None:
                raise ValueError("relation endpoint is deleted")
            if (entity.schema_pack, entity.schema_version) != pack_key:
                raise ValueError("relation endpoints must share its schema pack version")
            self.validate_entity(entity)

        definition = self._pack(*pack_key).relation_types.get(relation.relation_type)
        if (
            definition is None
            or source.entity_type not in definition.sources
            or target.entity_type not in definition.targets
        ):
            raise ValueError("relation triple is not allowed by the schema pack")
        if not definition.allow_cycles and source.entity_id == target.entity_id:
            raise ValueError("relation type does not allow cycles")
