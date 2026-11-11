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

from ncp.ontology.models import Entity, RetrievalMode, TraversalDirection, normalize_key
from ncp.ontology.resolution import Resolution, tokenize

RELATIONSHIP_TERMS = frozenset({
    "affect", "affected", "affects", "blast", "break", "breaks", "call", "calls", "connected",
    "consume",
    "consumer", "consumers", "consumes", "consuming", "depend", "dependency",
    "dependencies", "dependent", "dependents", "depends", "downstream", "expose",
    "exposed", "exposes", "impact", "impacted", "impacts", "owned", "owner", "owners",
    "ownership", "owns", "produce", "produced", "producer", "producers", "produces",
    "producing", "radius", "related", "relationship", "relationships", "relies", "rely",
    "require", "requires", "topology", "upstream", "uses",
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


# -- intent-directed expansion ------------------------------------------------------
#
# Once the trigger has decided to expand, the same cheap request words choose
# *what* to expand: which relation types (by the schema pack's ``intents``
# category, never by hardcoded type names), which direction and how many hops.
# Deterministic, no LLM. One recognised category -> its profile; none or more
# than one -> ``mixed`` (every type, both directions, one hop fewer).

DEPENDENCY_TERMS = frozenset({
    "call", "calls", "calling", "depend", "dependencies", "dependency", "depends",
    "need", "needs", "relies", "rely", "require", "requires", "upstream", "use", "uses",
    "using",
})
IMPACT_TERMS = frozenset({
    "affect", "affected", "affects", "blast", "break", "breaks", "callers", "consumers",
    "dependent", "dependents", "downstream", "impact", "impacted", "impacts", "radius",
})
OWNERSHIP_TERMS = frozenset({
    "maintainer", "maintainers", "maintains", "owned", "owner", "owners", "ownership",
    "owns", "team", "teams",
})
DEPLOYMENT_TERMS = frozenset({
    "cluster", "deployed", "deployment", "hosted", "hosting", "hosts", "region", "runs",
})
# "who uses X" / "what services depend on X" ask for dependents (incoming),
# while "what does X depend on" asks for dependencies (outgoing). When the
# word between the question word and the verb is a resolved entity ("what
# checkout depends on"), X is the subject: dependencies, not dependents.
_DEPENDENT_QUESTION = re.compile(
    r"\b(?:who|what|which)(?: ((?!does\b|do\b|did\b|is\b)[\w-]+))?"
    r" (call|calls|consume|consumes|depend|depends|need|needs|relies|rely|require|requires"
    r"|use|uses)\b"
)
INTENT_PROFILES = ("dependency", "impact", "ownership", "deployment", "mixed")
# Per-intent statement caps (further bounded by ``max_graph_paths``).
INTENT_PATH_CAPS = {"dependency": 16, "impact": 16, "ownership": 8, "deployment": 8, "mixed": 12}


@dataclass(frozen=True, slots=True)
class IntentProfile:
    """Traversal shape for one request: ``relation_types`` empty means all."""

    name: str
    relation_types: tuple[str, ...]
    direction: TraversalDirection
    hops: int
    path_cap: int
    categories: tuple[str, ...] = ()


def intent_categories(
    texts: Sequence[str], *, entity_terms: Iterable[str] = (),
) -> tuple[str, ...]:
    """Relationship-intent categories named by the request words (sorted).
    ``entity_terms`` are the normalized terms that resolved to seed entities."""
    words: list[str] = []
    for text in texts:
        words.extend(tokenize(text))
    vocabulary = set(words)
    joined = " ".join(words)
    found: set[str] = set()
    entity_words = {word for term in entity_terms for word in tokenize(term)}
    dependent_verbs = {
        match.group(2) for match in _DEPENDENT_QUESTION.finditer(joined)
        if match.group(1) not in entity_words  # None: no word in between
    }
    if dependent_verbs or vocabulary & IMPACT_TERMS:
        found.add("impact")
    if (vocabulary - dependent_verbs) & DEPENDENCY_TERMS:
        found.add("dependency")
    if vocabulary & OWNERSHIP_TERMS:
        found.add("ownership")
    if vocabulary & DEPLOYMENT_TERMS:
        found.add("deployment")
    if not found and vocabulary & DIAGNOSTIC_TERMS:
        found.add("dependency")  # a symptom's causes live upstream
    return tuple(sorted(found))


def classify_intent(
    texts: Sequence[str],
    *,
    relation_intents: Mapping[str, Iterable[str]],
    max_hops: int,
    max_paths: int,
    entity_terms: Iterable[str] = (),
) -> IntentProfile:
    """The traversal profile for ``texts``. ``relation_intents`` maps each
    enabled relation type to its schema-pack ``intents`` categories;
    ``entity_terms`` are the terms that resolved to seeds."""
    categories = intent_categories(texts, entity_terms=entity_terms)

    def types_for(category: str) -> tuple[str, ...]:
        return tuple(sorted(
            name for name, intents in relation_intents.items() if category in set(intents)
        ))

    def cap(name: str) -> int:
        return max(1, min(INTENT_PATH_CAPS[name], max_paths))

    if len(categories) == 1:
        category = categories[0]
        lookup = "dependency" if category == "impact" else category
        types = types_for(lookup)
        if types:
            if category in ("dependency", "impact"):
                direction: TraversalDirection = "out" if category == "dependency" else "in"
                return IntentProfile(category, types, direction, max_hops, cap(category),
                                     categories)
            return IntentProfile(category, types, "both", min(1, max_hops), cap(category),
                                 categories)
    return IntentProfile("mixed", (), "both", max(0, min(max_hops, max(1, max_hops - 1))),
                         cap("mixed"), categories)


__all__ = [
    "DEPENDENCY_TERMS", "DEPLOYMENT_TERMS", "DIAGNOSTIC_TERMS", "EXPAND_REASONS",
    "IMPACT_TERMS", "INTENT_PATH_CAPS", "INTENT_PROFILES", "OWNERSHIP_TERMS",
    "RELATIONSHIP_TERMS", "SKIP_REASONS", "IntentProfile", "TriggerDecision",
    "TriggerThresholds", "classify_intent", "evaluate", "intent_categories", "type_names",
]
