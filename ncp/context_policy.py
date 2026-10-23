"""Named context policies and a pure configuration resolver.

Foundation only: nothing in the runtime request paths selects or enforces a
policy yet. ``resolve_context_policy`` turns a deployment ``NCPConfig`` plus an
optional policy name into a detached effective ``NCPConfig`` and a stable
identity. Policies may override only an explicit allowlist of context-assembly
settings; every other key or section is rejected.
"""

from __future__ import annotations

import copy
import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from ncp.config import NCPConfig
from ncp.types import ChunkEdgeType


class ContextPolicyError(ValueError):
    """A context policy definition or selection is invalid."""


class UnknownContextPolicyError(ContextPolicyError):
    """The selected policy name is not in the registry."""


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True, allow_inf_nan=False)


class RetrievalPolicy(_StrictModel):
    diversity_lambda: float | None = Field(default=None, ge=0.0, le=1.0)
    edge_expansion: bool | None = None
    edge_expansion_decay: float | None = Field(default=None, ge=0.0, le=1.0)
    edge_max_hops: int | None = Field(default=None, ge=0)
    edge_expansion_types: list[ChunkEdgeType] | None = None
    reduce_fanin_enabled: bool | None = None
    reduce_fanin_min_cluster: int | None = Field(default=None, ge=2)
    reduce_fanin_similarity_threshold: float | None = Field(default=None, ge=0.0, le=1.0)
    reduce_fanin_contradict_floor: float | None = Field(default=None, ge=0.0, le=1.0)
    reduce_fanin_overfetch: int | None = Field(default=None, ge=1)
    fallback_to_trust_recency_enabled: bool | None = None

    @field_validator("edge_expansion_types")
    @classmethod
    def _normalize_edge_types(cls, value: list[ChunkEdgeType] | None) -> list[ChunkEdgeType] | None:
        # Types are a traversal filter, so order and repeats carry no meaning;
        # normalizing keeps equivalent policies on the same identity hash.
        # Disable expansion with edge_expansion = false or edge_max_hops = 0.
        if value is None:
            return None
        if not value:
            raise ValueError("edge_expansion_types must list at least one edge type")
        return sorted(set(value))


class BudgetPolicy(_StrictModel):
    context_token_budget: int | None = Field(default=None, ge=1)
    adaptive_budget_enabled: bool | None = None
    adaptive_budget_floor_tokens: int | None = Field(default=None, ge=1)
    adaptive_budget_ceiling_tokens: int | None = Field(default=None, ge=1)

    @model_validator(mode="after")
    def _explicit_range_is_ordered(self) -> "BudgetPolicy":
        # Partial ranges are checked by the resolver against inherited values.
        floor = self.adaptive_budget_floor_tokens
        ceiling = self.adaptive_budget_ceiling_tokens
        if floor is not None and ceiling is not None and floor > ceiling:
            raise ValueError("adaptive_budget_floor_tokens exceeds adaptive_budget_ceiling_tokens")
        return self


class DistillationPolicy(_StrictModel):
    enabled: bool | None = None
    min_chunk_tokens: int | None = Field(default=None, ge=1)


class ContextPolicy(_StrictModel):
    name: str = Field(min_length=1)
    version: int = Field(ge=1)
    retrieval: RetrievalPolicy = Field(default_factory=RetrievalPolicy)
    budget: BudgetPolicy = Field(default_factory=BudgetPolicy)
    distillation: DistillationPolicy = Field(default_factory=DistillationPolicy)

    def overrides(self) -> dict[str, dict[str, Any]]:
        """Explicit (non-None) overrides per config section, deep-copied."""
        out: dict[str, dict[str, Any]] = {}
        for section in ("retrieval", "budget", "distillation"):
            body = getattr(self, section).model_dump(exclude_none=True)
            if body:
                out[section] = body
        return out

    def canonical_json(self) -> str:
        payload = {"name": self.name, "version": self.version, **self.overrides()}
        return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


@dataclass(frozen=True, slots=True)
class ContextPolicyIdentity:
    name: str
    version: int
    sha256: str


@dataclass(frozen=True, slots=True)
class ResolvedContextPolicy:
    config: NCPConfig
    identity: ContextPolicyIdentity | None


def _describe(exc: ValidationError) -> str:
    """Field paths and reasons only; never the rejected input values."""
    parts = []
    for err in exc.errors(include_input=False, include_url=False, include_context=False):
        path = ".".join(str(p) for p in err["loc"]) or "<policy>"
        parts.append(f"{path}: {err['msg']}")
    return "; ".join(parts)


def _require_name(name: Any) -> str:
    if not isinstance(name, str) or not name:
        raise ContextPolicyError("Context policy names must be non-empty strings")
    return name


def _parse_policy(name: Any, raw: Any) -> ContextPolicy:
    _require_name(name)
    if not isinstance(raw, dict):
        raise ContextPolicyError(f"Context policy {name!r} must be a table")
    data = copy.deepcopy(raw)
    if data.get("name", name) != name:
        raise ContextPolicyError(f"Context policy {name!r} has an inconsistent embedded name")
    data["name"] = name
    try:
        return ContextPolicy.model_validate(data)
    except ValidationError as exc:
        raise ContextPolicyError(f"Invalid context policy {name!r}: {_describe(exc)}") from None


def _raw_policies(config: NCPConfig) -> dict[str, Any]:
    raw = config.values.get("context_policies", {})
    if not isinstance(raw, dict):
        raise ContextPolicyError("[context_policies] must be a table of named policies")
    return raw


@dataclass(slots=True)
class ContextPolicyRegistry:
    """Validated policies loaded from ``[context_policies.<name>]``."""

    entries: dict[str, ContextPolicy] = field(default_factory=dict)

    @classmethod
    def from_config(cls, config: NCPConfig) -> "ContextPolicyRegistry":
        return cls(entries={n: _parse_policy(n, r) for n, r in _raw_policies(config).items()})

    def names(self) -> list[str]:
        return sorted(self.entries)

    def get(self, name: str) -> ContextPolicy:
        _require_name(name)
        policy = self.entries.get(name)
        if policy is None:
            raise UnknownContextPolicyError(
                f"Unknown context policy {name!r}; known policies: {self.names()}"
            )
        return policy.model_copy(deep=True)


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _validate_budget(values: dict[str, Any]) -> None:
    budget = values.get("budget", {})
    if not isinstance(budget, dict):
        raise ContextPolicyError("Deployment budget must be a table")
    maximum = budget.get("max_tokens_per_call")
    if not _is_int(maximum) or maximum < 1:
        raise ContextPolicyError(
            "Deployment budget.max_tokens_per_call must be a positive integer, "
            f"got {type(maximum).__name__}"
        )
    resolved = {
        key: budget.get(key)
        for key in (
            "context_token_budget",
            "adaptive_budget_floor_tokens",
            "adaptive_budget_ceiling_tokens",
        )
    }
    for key, value in resolved.items():
        if not _is_int(value) or value < 1:
            raise ContextPolicyError(
                f"Resolved budget.{key} must be a positive integer, got {type(value).__name__}"
            )
    if resolved["context_token_budget"] > maximum:
        raise ContextPolicyError(
            f"context_token_budget {resolved['context_token_budget']} exceeds "
            f"budget.max_tokens_per_call {maximum}"
        )
    if resolved["adaptive_budget_ceiling_tokens"] > maximum:
        raise ContextPolicyError(
            f"adaptive_budget_ceiling_tokens {resolved['adaptive_budget_ceiling_tokens']} exceeds "
            f"budget.max_tokens_per_call {maximum}"
        )
    if resolved["adaptive_budget_floor_tokens"] > resolved["adaptive_budget_ceiling_tokens"]:
        raise ContextPolicyError(
            f"adaptive_budget_floor_tokens {resolved['adaptive_budget_floor_tokens']} exceeds "
            f"adaptive_budget_ceiling_tokens {resolved['adaptive_budget_ceiling_tokens']}"
        )


def resolve_context_policy(config: NCPConfig, name: str | None = None) -> ResolvedContextPolicy:
    """Pure: return the effective config and identity for ``name``.

    ``name=None`` returns ``config`` itself with no identity and does not
    validate the registry. Otherwise only the selected entry is validated and
    overlaid onto a deep copy of the deployment values.
    """

    if name is None:
        return ResolvedContextPolicy(config=config, identity=None)
    _require_name(name)
    raw = _raw_policies(config)
    if name not in raw:
        raise UnknownContextPolicyError(
            f"Unknown context policy {name!r}; known policies: {sorted(map(str, raw))}"
        )
    policy = _parse_policy(name, raw[name])

    values = copy.deepcopy(config.values)
    for section, body in policy.overrides().items():
        target = values.setdefault(section, {})
        if not isinstance(target, dict):
            raise ContextPolicyError(f"Deployment {section} must be a table")
        target.update(body)
    _validate_budget(values)

    digest = hashlib.sha256(policy.canonical_json().encode("utf-8")).hexdigest()
    identity = ContextPolicyIdentity(name=policy.name, version=policy.version, sha256=digest)
    effective = NCPConfig(values=values, project_root=Path(config.project_root))
    return ResolvedContextPolicy(config=effective, identity=identity)
