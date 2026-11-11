"""Deterministic automatic-invocation trigger (design §6.4, normative).

Cheap signals only (no LLM, no traversal):

* uniquely resolved entities from the request text (``resolution.resolve``),
* relationship intent: dependency / ownership / impact language and the
  enabled packs' relation type names,
* diagnostic intent: why / lag / outage / root cause style language,
* known entity type names that match a resolved entity's type,
* resolved entity keys mentioned by the memory chunks already retrieved.

Decision in ``auto`` mode: expand only with at least one uniquely resolved
entity *and* a signal score of at least :attr:`TriggerThresholds.min_score`.
Relationship and diagnostic intent are strong (2 each); a matching type name
and a memory reference are supporting (1 each). An entity alone, a type name
alone ("service") or an ambiguous alias never expands. ``always`` skips the
decision but still needs a resolved seed to traverse anything.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from uuid import UUID

from ncp.ontology.models import Entity, RetrievalMode, normalize_key
from ncp.ontology.resolution import Resolution, tokenize

RELATIONSHIP_TERMS = frozenset({
    "affect", "affected", "affects", "blast", "call", "calls", "connected", "consume",
    "consumer", "consumers", "consumes", "consuming", "depend", "dependency",
    "dependencies", "dependent", "dependents", "depends", "downstream", "expose",
    "exposed", "exposes", "impact", "impacted", "impacts", "owned", "owner", "owners",
    "ownership", "owns", "produce", "produced", "producer", "producers", "produces",
    "producing", "radius", "related", "relationship", "relationships", "topology",
    "upstream", "uses",
})
DIAGNOSTIC_TERMS = frozenset({
    "broken", "cause", "debug", "degraded", "diagnose", "error", "errors", "fail",
    "failing", "failure", "failures", "incident", "investigate", "lag", "lagging",
    "latency", "outage", "regression", "root", "slow", "timeout", "timeouts", "why",
})

# Reason codes (stable strings; telemetry consumers key on them).
EXPAND_REASONS = frozenset({
    "entity_relationship_intent", "entity_diagnostic_intent", "entity_supporting_signals",
    "mode_always",
})
SKIP_REASONS = frozenset({
    "no_entity", "ambiguous_entity", "entity_without_intent", "always_no_seed",
})

MEMORY_SCAN_CHARS = 2_000
MAX_SEEDS = 10


@dataclass(frozen=True, slots=True)
class TriggerThresholds:
    """Configurable decision thresholds (defaults are the normative policy)."""

    min_score: int = 2
    relationship_weight: int = 2
    diagnostic_weight: int = 2
    type_name_weight: int = 1
    memory_ref_weight: int = 1


@dataclass(frozen=True, slots=True)
class TriggerDecision:
    should_expand: bool
    reason_code: str
    seeds: tuple[UUID, ...] = ()
    candidate_count: int = 0
    signals: tuple[str, ...] = ()
    score: int = 0
    ambiguous_terms: tuple[str, ...] = field(default=())


def type_names(entity_types: Iterable[str]) -> dict[str, str]:
    """Normalized spellings of entity type names -> type (``KafkaTopic`` ->
    ``kafkatopic``, ``kafka topic``, ``kafka_topic``)."""
    names: dict[str, str] = {}
    for name in entity_types:
        words = [part.lower() for part in re.findall(r"[A-Z]+(?![a-z])|[A-Z]?[a-z0-9]+", name)]
        for spelling in {name.lower(), " ".join(words), "_".join(words), "-".join(words)}:
            if spelling:
                names.setdefault(normalize_key(spelling), name)
    return names


def _relation_terms(relation_types: Iterable[str]) -> set[str]:
    terms: set[str] = set()
    for name in relation_types:
        for part in re.split(r"[_\W]+", name.lower()):
            if len(part) > 2:
                terms.add(part)
    return terms


def evaluate(
    *,
    mode: RetrievalMode,
    texts: Sequence[str],
    resolution: Resolution,
    entities: Mapping[str, Entity],
    entity_types: Iterable[str],
    relation_types: Iterable[str],
    memory_texts: Sequence[str] = (),
    thresholds: TriggerThresholds = TriggerThresholds(),
) -> TriggerDecision:
    """The §6.4 decision for one request. ``entities`` maps resolved ids
    (``str(UUID)``) to their visible :class:`Entity` rows."""
    seeds = tuple(resolution.resolved)[:MAX_SEEDS]
    ambiguous = tuple(sorted(resolution.ambiguous))
    candidates = resolution.candidate_count
    if mode == "always":
        if not seeds:
            return TriggerDecision(False, "always_no_seed", (), candidates, (), 0, ambiguous)
        return TriggerDecision(True, "mode_always", seeds, candidates, ("mode_always",), 0, ambiguous)
    if not seeds:
        reason = "ambiguous_entity" if ambiguous else "no_entity"
        return TriggerDecision(False, reason, (), candidates, (), 0, ambiguous)

    words: list[str] = []
    for text in texts:
        words.extend(tokenize(text))
    vocabulary = set(words)
    joined = " ".join(words)
    signals: list[str] = []
    score = 0
    relationship = RELATIONSHIP_TERMS | _relation_terms(relation_types)
    if vocabulary & relationship:
        signals.append("relationship_intent")
        score += thresholds.relationship_weight
    if vocabulary & DIAGNOSTIC_TERMS:
        signals.append("diagnostic_intent")
        score += thresholds.diagnostic_weight
    resolved_types = {
        entities[str(seed)].entity_type for seed in seeds if str(seed) in entities
    }
    spelled = type_names(entity_types)
    mentioned = {
        type_name for spelling, type_name in spelled.items()
        if re.search(rf"(?<![\w-]){re.escape(spelling)}s?(?![\w-])", joined)
    }
    if mentioned & resolved_types:
        signals.append("type_name")
        score += thresholds.type_name_weight
    if memory_texts:
        keys = {
            normalize_key(entities[str(seed)].canonical_key)
            for seed in seeds if str(seed) in entities
        }
        memory = normalize_key(" ".join(text[:MEMORY_SCAN_CHARS] for text in memory_texts))
        if any(len(key) > 2 and key in memory for key in keys):
            signals.append("memory_entity_ref")
            score += thresholds.memory_ref_weight

    if score < thresholds.min_score:
        return TriggerDecision(False, "entity_without_intent", (), candidates, tuple(signals), score, ambiguous)
    if "relationship_intent" in signals:
        reason = "entity_relationship_intent"
    elif "diagnostic_intent" in signals:
        reason = "entity_diagnostic_intent"
    else:
        reason = "entity_supporting_signals"
    return TriggerDecision(True, reason, seeds, candidates, tuple(signals), score, ambiguous)


__all__ = [
    "DIAGNOSTIC_TERMS", "EXPAND_REASONS", "RELATIONSHIP_TERMS", "SKIP_REASONS",
    "TriggerDecision", "TriggerThresholds", "evaluate", "type_names",
]
