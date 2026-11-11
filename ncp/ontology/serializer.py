"""Compact, attribute-free serialization of ranked graph paths (design §6.2 step 7, §6.3).

Automatic context carries identity and structure only: short ``tag:key``
labels, relation type names with direction, and citations of the live
evidence chunk ids. Entity and relation *attributes* are never serialized
here (design §8: explicit allowlist for context serialization). Token counts
use NCP's ``estimate_tokens``, the same counter as the budget trim.

Block format v2 (graph-first, fact-grouped)::

    [NCP:ONTOLOGY] v:2 status:verified
    svc:checkout -DEPENDS_ON-> svc:cart,svc:payments,db:orders-db ev:c-1,c-2,c-3
    svc:payments -DEPENDS_ON-> api:card-network-api ev:c-9+1
    svc:pricing <-DEPENDS_ON- svc:cart ev:c-4
    svc:checkout -PRODUCES-> topic:order-events att

* Every verified relation of the selected paths is one *fact*, emitted once;
  facts sharing an anchor (the endpoint nearer the seed), type and direction
  are grouped on one line. Shared path prefixes are therefore never repeated.
* ``ev:`` lists one citation per target, in target order: the chunk id shown
  (a chunk already in the memory context when there is one, else the first
  live one), ``+N`` when the relation has N more evidence refs, ``att`` when
  it is backed by external attestations only.
* No per-line score: ranking decides *which* facts are shown; the structured
  ``ontology`` metadata keeps each path's score.
* ``tag`` comes from the schema pack's entity type (``Service`` -> ``svc``),
  falling back to the type name's lowercase initials / first letters.

Selection is greedy in rank order and stops on marginal value (see
:func:`serialize`): the token slice is a ceiling, not a target.
"""

from __future__ import annotations

import re
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass, field

from ncp.ontology.models import Entity
from ncp.ontology.ranking import RankedPath
from ncp.tokens import estimate_tokens

BLOCK_VERSION = 2
HEADER = f"[NCP:ONTOLOGY] v:{BLOCK_VERSION} status:verified"
MAX_LABEL_CHARS = 80
MAX_CITATION_CHARS = 64
_UNSAFE = re.compile(r"[^\w.:/@+-]+")


def _safe(value: str) -> str:
    return _UNSAFE.sub("_", value)


def entity_label(entity: Entity) -> str:
    """``Type:key`` with whitespace and markup folded to ``_`` (single line, bounded).
    Used in structured metadata; the block itself uses :func:`short_label`."""
    key = _safe(entity.canonical_key).strip("_") or "_"
    label = f"{_safe(entity.entity_type)}:{key}"
    return label if len(label) <= MAX_LABEL_CHARS else label[: MAX_LABEL_CHARS - 1] + "~"


def default_tag(entity_type: str) -> str:
    """Fallback short tag: initials of a multi-word type (``KafkaTopic`` -> ``kt``),
    else its first three letters (``Service`` -> ``ser``)."""
    words = re.findall(r"[A-Z]+(?![a-z])|[A-Z]?[a-z0-9]+", entity_type)
    if len(words) > 1:
        tag = "".join(word[0] for word in words).lower()
    else:
        tag = entity_type[:3].lower()
    return _safe(tag) or "e"


def short_label(entity: Entity, tags: Mapping[str, str]) -> str:
    key = _safe(entity.canonical_key).strip("_") or "_"
    tag = _safe(tags.get(entity.entity_type) or default_tag(entity.entity_type))
    label = f"{tag}:{key}"
    return label if len(label) <= MAX_LABEL_CHARS else label[: MAX_LABEL_CHARS - 1] + "~"


@dataclass(frozen=True, slots=True)
class _Fact:
    relation_id: str
    anchor: str
    relation_type: str
    forward: bool
    target: str
    citation: str
    cited_chunk: str | None


def _facts(path: RankedPath, tags: Mapping[str, str], context: Collection[str]) -> list[_Fact]:
    candidate = path.candidate
    facts: list[_Fact] = []
    for position, relation in enumerate(candidate.relations):
        here, there = candidate.nodes[position], candidate.nodes[position + 1]
        forward = (relation.src_entity_id, relation.dst_entity_id) == (here.entity_id, there.entity_id)
        chunks = candidate.chunk_evidence[position]
        external = candidate.external_evidence[position]
        chosen = next((chunk for chunk in chunks if chunk in context), chunks[0] if chunks else None)
        cited: str | None = None
        if chosen is not None:
            extra = len(chunks) - 1 + external
            # ``+`` is the extra-refs marker, so it is folded inside the id.
            shown = _safe(chosen).replace("+", "_")[:MAX_CITATION_CHARS]
            citation = shown + (f"+{extra}" if extra else "")
            # Only a chunk whose id is cited verbatim may lose its text to the
            # fact (substitution): a folded or truncated id no longer names it.
            cited = chosen if shown == chosen else None
        else:
            citation = "att" + (f"+{external - 1}" if external > 1 else "")
        facts.append(_Fact(
            relation_id=str(relation.relation_id), anchor=short_label(here, tags),
            relation_type=_safe(relation.relation_type), forward=forward,
            target=short_label(there, tags), citation=citation, cited_chunk=cited,
        ))
    return facts


def _render(facts: Sequence[_Fact]) -> tuple[str, ...]:
    groups: dict[tuple[str, str, bool], list[_Fact]] = {}
    for fact in facts:
        groups.setdefault((fact.anchor, fact.relation_type, fact.forward), []).append(fact)
    lines = []
    for (anchor, kind, forward), members in groups.items():
        arrow = f"-{kind}->" if forward else f"<-{kind}-"
        targets = ",".join(member.target for member in members)
        citations = ",".join(member.citation for member in members)
        lines.append(f"{anchor} {arrow} {targets} ev:{citations}")
    return tuple(lines)


@dataclass(frozen=True, slots=True)
class SerializedGraph:
    """The ``[NCP:ONTOLOGY]`` block and which ranked paths it holds."""

    statements: tuple[str, ...]
    included: tuple[RankedPath, ...]
    omitted_for_tokens: int
    tags: Mapping[str, str] = field(default_factory=dict)
    context_chunk_ids: frozenset[str] = frozenset()
    # Marginal-value stops: paths not shown for a reason other than tokens.
    stopped: Mapping[str, int] = field(default_factory=dict)
    cited_chunk_ids: frozenset[str] = frozenset()
    fact_count: int = 0

    @property
    def text(self) -> str | None:
        return "\n".join((HEADER, *self.statements)) if self.statements else None

    @property
    def tokens(self) -> int:
        text = self.text
        return 0 if text is None else estimate_tokens(text)

    def shrink(self) -> SerializedGraph:
        """Drop the lowest-ranked included path and re-render (an empty result has no text)."""
        kept = self.included[:-1]
        statements, cited, count = _materialize(kept, self.tags, self.context_chunk_ids)
        return SerializedGraph(
            statements, kept, self.omitted_for_tokens + 1, self.tags, self.context_chunk_ids,
            self.stopped, cited, count,
        )


def _materialize(
    paths: Sequence[RankedPath], tags: Mapping[str, str], context: Collection[str],
) -> tuple[tuple[str, ...], frozenset[str], int]:
    facts: dict[str, _Fact] = {}
    for path in paths:
        for fact in _facts(path, tags, context):
            facts.setdefault(fact.relation_id, fact)
    cited = frozenset(fact.cited_chunk for fact in facts.values() if fact.cited_chunk)
    return _render(list(facts.values())), cited, len(facts)


def serialize(
    ranked: Sequence[RankedPath],
    *,
    max_tokens: int,
    tags: Mapping[str, str] | None = None,
    context_chunk_ids: Collection[str] = (),
    max_paths: int | None = None,
    min_score: float = 0.0,
) -> SerializedGraph:
    """Greedy in rank order with marginal-value stopping. ``max_tokens`` is a
    ceiling: a path is added only when the whole block still fits. A path
    whose relations are all shown already adds no new fact and is skipped
    (``covered``); selection *stops* at the first path scoring below
    ``min_score`` (``low_score``) or once ``max_paths`` paths are shown
    (``path_cap``)."""
    tag_map = dict(tags or {})
    context = frozenset(context_chunk_ids)
    facts: dict[str, _Fact] = {}
    included: list[RankedPath] = []
    stopped = {"covered": 0, "low_score": 0, "path_cap": 0}
    omitted = 0
    for index, path in enumerate(ranked):
        if max_paths is not None and len(included) >= max_paths:
            stopped["path_cap"] += len(ranked) - index
            break
        if path.score < min_score:
            stopped["low_score"] += len(ranked) - index
            break
        new = [fact for fact in _facts(path, tag_map, context) if fact.relation_id not in facts]
        if not new:
            stopped["covered"] += 1
            continue
        trial = {**facts, **{fact.relation_id: fact for fact in new}}
        if estimate_tokens("\n".join((HEADER, *_render(list(trial.values()))))) > max_tokens:
            omitted += 1
            continue
        facts = trial
        included.append(path)
    cited = frozenset(fact.cited_chunk for fact in facts.values() if fact.cited_chunk)
    return SerializedGraph(
        _render(list(facts.values())), tuple(included), omitted, tag_map, context,
        {key: value for key, value in stopped.items() if value}, cited, len(facts),
    )


__all__ = [
    "BLOCK_VERSION", "HEADER", "SerializedGraph", "default_tag", "entity_label",
    "serialize", "short_label",
]
