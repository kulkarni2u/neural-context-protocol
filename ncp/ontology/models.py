"""Validated data contracts for domain ontology providers."""

from __future__ import annotations

import json
import unicodedata
from typing import Annotated, Literal
from uuid import NAMESPACE_URL, UUID, uuid5

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, JsonValue, StringConstraints
from pydantic import field_validator, model_validator

Identifier = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=256)]
PositiveInt = Annotated[int, Field(strict=True, ge=1)]
RetrievalMode = Literal["off", "auto", "always"]


def normalize_key(value: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", value).casefold().split())


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)


class Scope(Contract):
    tenant_id: Identifier
    namespace: Identifier


class AuthContext(Contract):
    """Trusted host-supplied authorization, never constructed from tool arguments."""

    principal_id: Identifier
    scope: Scope
    capabilities: frozenset[Literal["read", "write", "admin"]] = frozenset()


class Entity(Contract):
    scope: Scope
    entity_type: Identifier
    canonical_key: Identifier
    display_name: Identifier
    aliases: tuple[Identifier, ...] = ()
    attributes: dict[str, JsonValue] = Field(default_factory=dict)
    schema_pack: Identifier
    schema_version: PositiveInt = 1
    revision: PositiveInt = 1
    created_at: AwareDatetime | None = None
    updated_at: AwareDatetime | None = None
    deleted_at: AwareDatetime | None = None

    @field_validator("canonical_key")
    @classmethod
    def canonicalize(cls, value: str) -> str:
        normalized = normalize_key(value)
        if not 1 <= len(normalized) <= 256:
            raise ValueError("normalized key must contain 1 to 256 characters")
        return normalized

    @field_validator("aliases")
    @classmethod
    def normalize_aliases(cls, aliases: tuple[str, ...]) -> tuple[str, ...]:
        return tuple(sorted({cls.canonicalize(alias) for alias in aliases}))

    @property
    def entity_id(self) -> UUID:
        identity = (
            self.scope.tenant_id, self.scope.namespace, self.schema_pack,
            self.entity_type, self.canonical_key,
        )
        return uuid5(NAMESPACE_URL, "ncp:ontology:v1:" + json.dumps(identity))


class EvidenceRef(Contract):
    evidence_type: Literal["chunk", "external_attestation"]
    evidence_id: Identifier
    source_revision: Identifier
    observed_at: AwareDatetime


class Relation(Contract):
    relation_id: UUID
    scope: Scope
    src_entity_id: UUID
    dst_entity_id: UUID
    relation_type: Identifier
    status: Literal["candidate", "verified", "disputed", "revoked"] = "candidate"
    confidence: Annotated[float, Field(strict=True, ge=0, le=1)]
    valid_from: AwareDatetime
    valid_until: AwareDatetime | None = None
    observed_at: AwareDatetime
    created_by: Identifier
    revision: PositiveInt = 1
    schema_pack: Identifier
    schema_version: PositiveInt = 1
    attributes: dict[str, JsonValue] = Field(default_factory=dict)
    evidence: tuple[EvidenceRef, ...] = ()

    @model_validator(mode="after")
    def validate_shape(self) -> Relation:
        if self.valid_until is not None and self.valid_until <= self.valid_from:
            raise ValueError("valid_until must follow valid_from")
        if self.status == "verified" and not self.evidence:
            raise ValueError("verified relations require evidence references")
        return self


class EntityMatch(Contract):
    entity_ids: tuple[UUID, ...]
    matched_key: Identifier

    @property
    def resolved_id(self) -> UUID | None:
        return self.entity_ids[0] if len(self.entity_ids) == 1 else None


class GraphQuery(Contract):
    seeds: Annotated[tuple[UUID, ...], Field(max_length=100)] = ()
    relation_types: Annotated[tuple[Identifier, ...], Field(max_length=100)] = ()
    max_hops: Annotated[int, Field(strict=True, ge=0, le=5)] = 2
    max_nodes: Annotated[int, Field(strict=True, ge=1, le=100)] = 30
    max_edges: Annotated[int, Field(strict=True, ge=1, le=200)] = 60
    verified_only: bool = Field(default=True, strict=True)


class GraphPath(Contract):
    nodes: tuple[UUID, ...]
    relations: tuple[Relation, ...]


class GraphResult(Contract):
    paths: tuple[GraphPath, ...] = ()
    warnings: tuple[str, ...] = ()
