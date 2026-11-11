"""Additive interfaces. Implementations must enforce authorization and provenance."""

from collections.abc import Mapping, Sequence
from typing import Protocol
from uuid import UUID

from ncp.ontology.models import AuthContext, Entity, EntityMatch, GraphQuery, GraphResult, Relation


class OntologyStore(Protocol):
    def upsert_entity(
        self, entity: Entity, *, auth: AuthContext, expected_version: int | None = None,
    ) -> Entity: ...

    def assert_relation(
        self, relation: Relation, *, auth: AuthContext, idempotency_key: str,
    ) -> Relation: ...

    def get_entity(self, entity_id: UUID, *, auth: AuthContext) -> Entity | None: ...

    def traverse(self, request: GraphQuery, *, auth: AuthContext) -> GraphResult: ...

    def list_relations(
        self, *, auth: AuthContext, status: str | None = None, limit: int = 1000,
    ) -> list[Relation]: ...

    def invalidate_evidence(
        self, chunk_ids: Sequence[str], *, auth: AuthContext, reason: str,
    ) -> int: ...


class OntologyProvider(Protocol):
    provider_id: str

    def capabilities(self) -> Mapping[str, object]: ...

    def validate(
        self, assertion: Relation, *, source: Entity, target: Entity, auth: AuthContext,
    ) -> None: ...

    def resolve_entities(self, text: str, *, auth: AuthContext) -> tuple[EntityMatch, ...]: ...

    def expand(self, request: GraphQuery, *, auth: AuthContext) -> GraphResult: ...


class DisabledOntologyProvider:
    provider_id = "disabled"

    def capabilities(self) -> Mapping[str, object]:
        return {"enabled": False, "version": "1"}

    def validate(
        self, assertion: Relation, *, source: Entity, target: Entity, auth: AuthContext,
    ) -> None:
        raise RuntimeError("ontology is disabled")

    def resolve_entities(self, text: str, *, auth: AuthContext) -> tuple[EntityMatch, ...]:
        raise RuntimeError("ontology is disabled")

    def expand(self, request: GraphQuery, *, auth: AuthContext) -> GraphResult:
        raise RuntimeError("ontology is disabled")
