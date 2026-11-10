"""Ontology configuration and request-mode precedence."""

from typing import Annotated, Literal

from pydantic import Field, field_validator

from ncp.ontology.models import Capability, Contract, Identifier, RetrievalMode


class OntologyConfig(Contract):
    enabled: bool = Field(default=False, strict=True)
    provider: Identifier = "builtin"
    store: Literal["inherit"] = "inherit"
    fail_mode: Literal["closed", "fallback"] = "fallback"
    packs: tuple[Identifier, ...] = ("infrastructure",)
    retrieval_mode: RetrievalMode = "auto"
    max_hops: Annotated[int, Field(strict=True, ge=0, le=5)] = 2
    max_nodes: Annotated[int, Field(strict=True, ge=1, le=100)] = 30
    max_edges: Annotated[int, Field(strict=True, ge=1, le=200)] = 60
    max_graph_tokens: Annotated[int, Field(strict=True, ge=0, le=4000)] = 500
    verified_only: bool = Field(default=True, strict=True)
    trigger_max_ms: Annotated[int, Field(strict=True, ge=1, le=1000)] = 10
    graph_timeout_ms: Annotated[int, Field(strict=True, ge=1, le=5000)] = 50
    fallback_to_memory: bool = Field(default=True, strict=True)
    # Capabilities granted to callers with no authenticated principal (stdio,
    # or HTTP without [principals]). verify/admin stay denied unless listed.
    unauthenticated_capabilities: tuple[Capability, ...] = ("read", "write")

    @field_validator("unauthenticated_capabilities")
    @classmethod
    def unique_capabilities(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(set(value)) != len(value):
            raise ValueError("unauthenticated_capabilities must not repeat a capability")
        return tuple(sorted(value))

    def resolve_mode(
        self,
        *,
        opt_out: bool = False,
        override: RetrievalMode | None = None,
        override_authorized: bool = False,
    ) -> RetrievalMode:
        if not self.enabled or self.provider == "disabled" or opt_out:
            return "off"
        if override is None:
            return self.retrieval_mode
        if not override_authorized:
            raise PermissionError("ontology mode override requires authorization")
        if override not in ("off", "auto", "always"):
            raise ValueError("invalid ontology retrieval mode")
        return override
