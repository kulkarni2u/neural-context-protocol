"""Ontology-aware context enrichment for ``ncp_get_context`` (design §6.2-§6.4, PR 4).

:func:`build_enricher` resolves the effective mode (design §6.4 precedence:
disabled feature -> off; explicit per-request opt-out -> off; authorized
per-request override; configured ``retrieval_mode``) and returns ``None`` for
``off`` -- the assembler then runs exactly the legacy path, so disabled or
opted-out output is byte-for-byte unchanged.

Otherwise :class:`OntologyEnricher.enrich` runs inside the assembler, after
the existing retrieval has chosen its memory chunks and before the final
budget trim:

1. Eligibility: SQLite or pgvector store whose ontology schema already
   exists (the probe never creates tables), at least one enabled pack, a non-zero graph token
   slice that fits beside the conscious block and budget header.
2. Signals + decision within ``trigger_max_ms`` (:mod:`ncp.ontology.trigger`).
3. Bounded traversal, **always** ``verified_only``, within ``graph_timeout_ms``.
4. Evidence-to-chunk mapping: only live chunks readable by the caller (not
   superseded / expired / before ``valid_from`` / out of scope) or valid
   external attestations support a relation; a path with an unsupported or
   inactive relation, or an endpoint that vanished, is dropped.
5. Deterministic ranking (:mod:`ncp.ontology.ranking`) and compact
   serialization (:mod:`ncp.ontology.serializer`) inside the token slice.

Failures: an authorization failure always raises :class:`OntologyAuthError`
(fail closed, never retried unscoped). Timeouts and backend errors degrade to
ordinary context with ``fallback_reason`` telemetry when ``fail_mode =
"fallback"`` and ``fallback_to_memory = true``; otherwise they raise
:class:`OntologyUnavailableError`. No graph facts are ever fabricated.
"""

from __future__ import annotations

import sqlite3
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from ncp.ontology.models import (
    GLOBAL_NAMESPACE, AuthContext, Entity, GraphQuery, Relation, RetrievalMode,
)
from ncp.ontology.policy import OntologyConfig
from ncp.ontology.ranking import PathCandidate, rank_paths
from ncp.ontology.resolution import content_tokens, resolve
from ncp.ontology.serializer import SerializedGraph, entity_label, serialize
from ncp.ontology.stores.postgres import PostgresOntologyStore
from ncp.ontology.stores.sqlite import OntologyTimeoutError, SQLiteOntologyStore
from ncp.ontology.trigger import TriggerThresholds, evaluate
from ncp.principals import AuthorizationError

if TYPE_CHECKING:
    from ncp.config import NCPConfig
    from ncp.ontology.facade import OntologyFacade
    from ncp.principals import Principal
    from ncp.types import ConsciousBlock, SubconsciousChunk

CONTEXT_SCHEMA_VERSION = 1


def _backend_errors() -> tuple[type[BaseException], ...]:
    """Store errors that degrade to fallback: SQLite, psycopg (when installed), invalid input."""
    errors: tuple[type[BaseException], ...] = (sqlite3.Error, ValueError)
    try:
        import psycopg
    except ImportError:  # pragma: no cover - psycopg is an optional extra
        return errors
    return (*errors, psycopg.Error)


_BACKEND_ERRORS = _backend_errors()
_MODES = ("off", "auto", "always")


class OntologyAuthError(AuthorizationError, PermissionError):
    """The caller may not read the ontology scope of this request (fail closed)."""


class OntologyUnavailableError(RuntimeError):
    """Enrichment failed and the configuration forbids falling back to memory."""


# -- request extension ----------------------------------------------------------


@dataclass(frozen=True, slots=True)
class OntologyRequest:
    """The optional ``ontology`` argument of ``ncp_get_context`` (design §6.1)."""

    opt_out: bool = False
    mode: RetrievalMode | None = None

    @classmethod
    def parse(cls, raw: object) -> OntologyRequest:
        """``None`` / ``true`` (configured mode), ``false`` (opt out), or an
        object ``{"enabled": bool, "mode": "off"|"auto"|"always"}``."""
        if raw is None or raw is True:
            return cls()
        if raw is False:
            return cls(opt_out=True)
        if not isinstance(raw, Mapping):
            raise ValueError("ncp_get_context 'ontology' must be a boolean or an object")
        unknown = sorted(set(raw) - {"enabled", "mode"})
        if unknown:
            raise ValueError(f"ncp_get_context 'ontology' has unsupported fields: {unknown}")
        enabled = raw.get("enabled", True)
        if not isinstance(enabled, bool):
            raise ValueError("ncp_get_context 'ontology.enabled' must be a boolean")
        mode = raw.get("mode")
        if mode is not None and mode not in _MODES:
            raise ValueError("ncp_get_context 'ontology.mode' must be one of off, auto, always")
        if not enabled or mode == "off":
            return cls(opt_out=True)
        return cls(mode=mode)  # type: ignore[arg-type]


def ontology_config(config: NCPConfig | OntologyConfig | None) -> OntologyConfig:
    if config is None:
        return OntologyConfig()
    return config if isinstance(config, OntologyConfig) else config.ontology


def resolve_request_mode(
    ontology: OntologyConfig, request: OntologyRequest, *, principal: Principal | None,
) -> tuple[RetrievalMode, list[str]]:
    """Mode per design §6.4 precedence plus warnings. A mode override needs
    ``admin`` (the principal's, or ``unauthenticated_capabilities`` without
    one); an unauthorized override is ignored with a warning, never an error."""
    warnings: list[str] = []
    if request.mode is None or request.opt_out:
        return ontology.resolve_mode(opt_out=request.opt_out), warnings
    if principal is not None:
        authorized = principal.has("admin")
    else:
        authorized = "admin" in ontology.unauthenticated_capabilities
    try:
        return ontology.resolve_mode(override=request.mode, override_authorized=authorized), warnings
    except PermissionError:
        warnings.append("ignored: ontology.mode override requires the admin capability")
        return ontology.resolve_mode(), warnings


def build_enricher(
    config: NCPConfig | OntologyConfig | None,
    store: object,
    *,
    principal: Principal | None = None,
    request: object = None,
    facade_provider: Callable[[], OntologyFacade] | None = None,
) -> OntologyEnricher | None:
    """The enricher for one ``ncp_get_context`` call, or ``None`` when the
    effective mode is ``off`` (including the disabled default). With the
    feature disabled ``request`` is ignored entirely, as legacy servers did."""
    ontology = ontology_config(config)
    if not ontology.enabled or ontology.provider == "disabled":
        return None
    mode, warnings = resolve_request_mode(
        ontology, OntologyRequest.parse(request), principal=principal,
    )
    if mode == "off":
        return None
    if facade_provider is None:
        from ncp.ontology.facade import cached_facade

        def facade_provider() -> OntologyFacade:
            return cached_facade(ontology, store)

    return OntologyEnricher(
        ontology, store, mode=mode, principal=principal, facade_provider=facade_provider,
        warnings=warnings,
    )


# -- enrichment result ------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class GraphContext:
    """What one enrichment produced: the serialized block (if any) and telemetry."""

    mode: RetrievalMode
    reason_code: str
    triggered: bool = False
    candidate_count: int = 0
    latency_ms: float = 0.0
    fallback_reason: str | None = None
    signals: tuple[str, ...] = ()
    ambiguous_count: int = 0
    warnings: tuple[str, ...] = ()
    serialized: SerializedGraph | None = None
    omitted: Mapping[str, int] = field(default_factory=dict)

    @property
    def text(self) -> str | None:
        return None if self.serialized is None else self.serialized.text

    @property
    def tokens(self) -> int:
        return 0 if self.serialized is None else self.serialized.tokens

    def shrink(self) -> GraphContext:
        """Drop the lowest-ranked statement to fit a tighter final budget."""
        if self.serialized is None:
            return self
        return replace(self, serialized=self.serialized.shrink())

    def metadata(self) -> dict[str, object]:
        """Structured telemetry + bundle (design §6.3 / §6.4 step 6). Holds
        ids, type names and canonical keys only: no raw query text, no
        attributes."""
        included = () if self.serialized is None else self.serialized.included
        omitted = dict(self.omitted)
        if self.serialized is not None and self.serialized.omitted_for_tokens:
            omitted["token_budget"] = (
                omitted.get("token_budget", 0) + self.serialized.omitted_for_tokens
            )
        entities: dict[str, dict[str, str]] = {}
        paths: list[dict[str, object]] = []
        for ranked in included:
            candidate = ranked.candidate
            for entity in candidate.nodes:
                entities.setdefault(str(entity.entity_id), {
                    "id": str(entity.entity_id), "type": entity.entity_type,
                    "label": entity_label(entity),
                })
            paths.append({
                "nodes": [str(entity.entity_id) for entity in candidate.nodes],
                "labels": [entity_label(entity) for entity in candidate.nodes],
                "relations": [relation.relation_type for relation in candidate.relations],
                "relation_ids": list(candidate.relation_ids),
                "evidence_ids": list(candidate.evidence_ids),
                "external_evidence_count": sum(candidate.external_evidence),
                "status": "verified",
                "score": ranked.score,
            })
        return {
            "schema_version": CONTEXT_SCHEMA_VERSION,
            "mode": self.mode,
            "ontology_triggered": self.triggered,
            "reason_code": self.reason_code,
            "candidate_count": self.candidate_count,
            "verified_path_count": len(paths),
            "latency_ms": self.latency_ms,
            "graph_tokens": self.tokens,
            "fallback_reason": self.fallback_reason,
            "signals": list(self.signals),
            "ambiguous_count": self.ambiguous_count,
            "entities": list(entities.values()),
            "paths": paths,
            "omitted": dict(sorted(omitted.items())),
            "warnings": list(self.warnings),
        }


# -- the enricher -------------------------------------------------------------------


class OntologyEnricher:
    """Per-request graph enrichment; see the module docstring for the steps."""

    def __init__(
        self,
        config: OntologyConfig,
        store: object,
        *,
        mode: RetrievalMode,
        principal: Principal | None,
        facade_provider: Callable[[], OntologyFacade],
        warnings: Sequence[str] = (),
        thresholds: TriggerThresholds = TriggerThresholds(),
        clock: Callable[[], float] = time.monotonic,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self.config = config
        self.store = store
        self.mode = mode
        self.principal = principal
        self._facade_provider = facade_provider
        self._warnings = tuple(warnings)
        self._thresholds = thresholds
        self._clock = clock
        self._now = now or (lambda: datetime.now(timezone.utc))

    # -- helpers ---------------------------------------------------------------

    def _auth(self, namespace: str) -> AuthContext:
        from ncp.ontology.facade import OntologyError, auth_context_for

        try:
            auth = auth_context_for(self.principal, namespace, config=self.config)
        except OntologyError as exc:
            raise OntologyAuthError(exc.message) from None
        if "read" not in auth.capabilities:
            raise OntologyAuthError("ontology enrichment requires the read capability")
        return auth

    def _result(self, started: float, reason: str, **values: Any) -> GraphContext:
        latency = round((self._clock() - started) * 1000.0, 3)
        warnings = tuple(self._warnings) + tuple(values.pop("warnings", ()))
        return GraphContext(
            mode=self.mode, reason_code=reason, latency_ms=latency, warnings=warnings, **values,
        )

    def _fallback(self, started: float, reason: str, fallback: str, **values: Any) -> GraphContext:
        if self.config.fail_mode == "closed" or not self.config.fallback_to_memory:
            raise OntologyUnavailableError(f"ontology enrichment failed: {fallback}")
        return self._result(started, reason, fallback_reason=fallback, **values)

    def _ontology_store(self) -> SQLiteOntologyStore | PostgresOntologyStore | None:
        """The ontology store, or ``None`` when its schema does not exist yet.

        Probed read-only first so enrichment never creates ontology tables
        (e.g. under the core tool profile, where no ontology tool runs).
        """
        from ncp.stores.pgvector import PgvectorStore
        from ncp.stores.sqlite import SQLiteStore

        if isinstance(self.store, SQLiteStore):
            if not SQLiteOntologyStore.initialized(self.store.path):
                return None
        elif isinstance(self.store, PgvectorStore):
            if not PostgresOntologyStore.initialized(self.store):
                return None
        elif not isinstance(self.store, (SQLiteOntologyStore, PostgresOntologyStore)):
            raise LookupError("provider_unavailable")
        facade = self._facade_provider()
        if facade.store is None:
            raise LookupError("provider_unavailable")
        return facade.store

    # -- main entry --------------------------------------------------------------

    def enrich(
        self,
        *,
        conscious: ConsciousBlock,
        query_text: str | None,
        chunks: Sequence[SubconsciousChunk],
        token_limit: int | None,
    ) -> GraphContext:
        """Run eligibility, trigger and bounded expansion for one request.

        ``token_limit`` is what remains of the caller's budget after the
        conscious block and budget header (``None``: no caller budget); the
        graph never takes more than ``min(max_graph_tokens, token_limit)``.
        """
        started = self._clock()
        namespace = conscious.pipeline_id or GLOBAL_NAMESPACE
        auth = self._auth(namespace)  # fail closed before any lookup
        slice_tokens = self.config.max_graph_tokens
        if token_limit is not None:
            slice_tokens = min(slice_tokens, token_limit)
        if not self.config.packs:
            return self._result(started, "no_enabled_packs")
        if slice_tokens <= 0:
            return self._result(started, "graph_budget_unavailable")
        try:
            store = self._ontology_store()
        except LookupError:
            return self._fallback(started, "provider_unavailable", "provider_unavailable")
        if store is None:
            return self._result(started, "ontology_uninitialized")

        texts = [
            text for text in dict.fromkeys(
                (conscious.task, conscious.slot, conscious.intent, query_text or ""),
            ) if text
        ]
        packs = tuple(
            pack for pack in store.registry.packs() if pack.id in self.config.packs
        )
        pack_ids = tuple(sorted({pack.id for pack in packs}))
        # Budgeted from here: the one-time facade build above is not trigger work.
        trigger_deadline = self._clock() + self.config.trigger_max_ms / 1000.0
        try:
            resolution = resolve(
                store, texts, auth=auth, schema_packs=pack_ids, deadline=trigger_deadline,
            )
            resolved_entities = store.get_entities(
                list(resolution.resolved), auth=auth, deadline=trigger_deadline,
            ) if resolution.resolved else {}
        except OntologyTimeoutError:
            return self._fallback(started, "trigger_timeout", "trigger_timeout")
        except PermissionError as exc:
            raise OntologyAuthError(str(exc)) from None
        except _BACKEND_ERRORS as exc:
            return self._fallback(started, "trigger_error", f"trigger_error:{type(exc).__name__}")

        decision = evaluate(
            mode=self.mode,
            texts=texts,
            resolution=resolution,
            entities=resolved_entities,
            entity_types={name for pack in packs for name in pack.entity_types},
            relation_types={name for pack in packs for name in pack.relation_types},
            memory_texts=[chunk.content for chunk in chunks],
            thresholds=self._thresholds,
        )
        common = {
            "candidate_count": decision.candidate_count,
            "signals": decision.signals,
            "ambiguous_count": len(decision.ambiguous_terms),
        }
        if not decision.should_expand:
            return self._result(started, decision.reason_code, **common)

        graph_deadline = self._clock() + self.config.graph_timeout_ms / 1000.0
        try:
            candidates, omitted = self._expand(store, auth, decision.seeds, graph_deadline)
        except OntologyTimeoutError:
            return self._fallback(
                started, decision.reason_code, "graph_timeout", triggered=True, **common,
            )
        except PermissionError as exc:
            raise OntologyAuthError(str(exc)) from None
        except _BACKEND_ERRORS as exc:
            return self._fallback(
                started, decision.reason_code, f"graph_error:{type(exc).__name__}",
                triggered=True, **common,
            )

        context_ids = {chunk.chunk_id for chunk in chunks}
        ranked = rank_paths(
            candidates, query_terms=content_tokens(texts), context_chunk_ids=context_ids,
            now=self._now(),
        )
        serialized = serialize(ranked, max_tokens=slice_tokens)
        covered = len(ranked) - len(serialized.included) - serialized.omitted_for_tokens
        if covered:
            omitted["covered"] = omitted.get("covered", 0) + covered
        return self._result(
            started, decision.reason_code, triggered=True,
            serialized=serialized if serialized.statements else None,
            omitted={key: value for key, value in omitted.items() if value},
            **common,
        )

    def _expand(
        self,
        store: SQLiteOntologyStore | PostgresOntologyStore,
        auth: AuthContext,
        seeds: Sequence[Any],
        deadline: float,
    ) -> tuple[list[PathCandidate], dict[str, int]]:
        """Traverse (verified only), map evidence to live readable chunks and
        load endpoints; returns the supported paths and omission counts."""
        result = store.traverse(
            GraphQuery(
                seeds=tuple(seeds),
                max_hops=self.config.max_hops,
                max_nodes=self.config.max_nodes,
                max_edges=self.config.max_edges,
                verified_only=True,  # automatic enrichment never shows candidates
            ),
            auth=auth,
            deadline=deadline,
        )
        now = self._now()
        relations: dict[str, Relation] = {}
        for path in result.paths:
            for relation in path.relations:
                relations.setdefault(str(relation.relation_id), relation)
        by_namespace: dict[str, set[str]] = {}
        for relation in relations.values():
            by_namespace.setdefault(relation.scope.namespace, set()).update(
                ref.evidence_id for ref in relation.evidence if ref.evidence_type == "chunk"
            )
        live: dict[str, set[str]] = {
            namespace: store.live_chunk_evidence(
                sorted(ids), auth=auth, relation_namespace=namespace, deadline=deadline,
            )
            for namespace, ids in sorted(by_namespace.items())
        }
        node_ids = sorted({str(node) for path in result.paths for node in path.nodes})
        entities: dict[str, Entity] = store.get_entities(node_ids, auth=auth, deadline=deadline)

        support: dict[str, tuple[tuple[str, ...], int] | None] = {}
        for relation_id, relation in relations.items():
            active = (
                relation.status == "verified"
                and relation.valid_from <= now
                and (relation.valid_until is None or relation.valid_until > now)
            )
            chunk_ids = tuple(sorted(
                ref.evidence_id for ref in relation.evidence
                if ref.evidence_type == "chunk"
                and ref.evidence_id in live.get(relation.scope.namespace, set())
            ))
            external = sum(
                1 for ref in relation.evidence if ref.evidence_type == "external_attestation"
            )
            support[relation_id] = (chunk_ids, external) if active and (chunk_ids or external) else None

        omitted = {"evidence_not_live": 0, "entity_unavailable": 0}
        candidates: list[PathCandidate] = []
        for path in result.paths:
            nodes = [entities.get(str(node)) for node in path.nodes]
            if any(node is None or node.deleted_at is not None for node in nodes):
                omitted["entity_unavailable"] += 1
                continue
            backing = [support[str(relation.relation_id)] for relation in path.relations]
            if any(item is None for item in backing):
                omitted["evidence_not_live"] += 1
                continue
            candidates.append(PathCandidate(
                nodes=tuple(nodes),  # type: ignore[arg-type]
                relations=path.relations,
                chunk_evidence=tuple(item[0] for item in backing),  # type: ignore[index]
                external_evidence=tuple(item[1] for item in backing),  # type: ignore[index]
            ))
        if result.warnings:
            omitted["truncated"] = len(result.warnings)
        return candidates, omitted


__all__ = [
    "CONTEXT_SCHEMA_VERSION", "GraphContext", "OntologyAuthError", "OntologyEnricher",
    "OntologyRequest", "OntologyUnavailableError", "build_enricher", "ontology_config",
    "resolve_request_mode",
]
