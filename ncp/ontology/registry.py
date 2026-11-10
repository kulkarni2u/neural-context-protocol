"""Versioned schema packs and structural validation for domain assertions."""

from importlib.resources import files
from typing import Literal

from pydantic import Field, model_validator

from ncp.ontology.models import (
    GLOBAL_NAMESPACE, Contract, Entity, Identifier, PositiveInt, Relation,
)


class EntityType(Contract):
    required: tuple[Identifier, ...] = ()
    sensitive: tuple[Identifier, ...] = ()
    alias_strategy: Literal["exact"] = "exact"


class RelationType(Contract):
    sources: tuple[Identifier, ...] = Field(alias="from", min_length=1)
    targets: tuple[Identifier, ...] = Field(alias="to", min_length=1)
    max_outgoing: PositiveInt | None = None
    max_incoming: PositiveInt | None = None
    allow_cycles: bool = Field(default=True, strict=True)


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


class SchemaRegistry:
    def __init__(self) -> None:
        self._packs: dict[tuple[str, int], SchemaPack] = {}

    @classmethod
    def builtin(cls) -> "SchemaRegistry":
        registry = cls()
        resource = files("ncp.ontology").joinpath("packs", "infrastructure.v1.json")
        registry.register(SchemaPack.model_validate_json(resource.read_bytes()))
        return registry

    def register(self, pack: SchemaPack) -> None:
        key = (pack.id, pack.version)
        if key in self._packs:
            raise ValueError("schema pack version is already registered")
        self._packs[key] = pack.model_copy(deep=True)

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
