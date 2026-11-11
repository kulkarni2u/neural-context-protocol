"""Compact, attribute-free serialization of ranked graph paths (design §6.2 step 7, §6.3).

Automatic context carries identity and structure only: ``Type:key`` labels,
relation type names with direction, and citations of the live evidence chunk
ids. Entity and relation *attributes* are never serialized here (design §8:
explicit allowlist for context serialization). Token counts use NCP's
``estimate_tokens``, the same counter as the budget trim::

    [NCP:ONTOLOGY] v:1 status:verified
    path:1 score:0.81 Service:payroll -CONSUMES-> KafkaTopic:payroll-events ev:c-12,c-40
    path:2 score:0.64 Service:payroll -DEPENDS_ON-> Database:oracle_payroll_db att:1
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass

from ncp.ontology.models import Entity
from ncp.ontology.ranking import RankedPath
from ncp.tokens import estimate_tokens

HEADER = "[NCP:ONTOLOGY] v:1 status:verified"
MAX_LABEL_CHARS = 80
MAX_CITATIONS = 3
_UNSAFE = re.compile(r"[^\w.:/@+-]+")


def entity_label(entity: Entity) -> str:
    """``Type:key`` with whitespace and markup folded to ``_`` (single line, bounded)."""
    key = _UNSAFE.sub("_", entity.canonical_key).strip("_") or "_"
    label = f"{_UNSAFE.sub('_', entity.entity_type)}:{key}"
    return label if len(label) <= MAX_LABEL_CHARS else label[: MAX_LABEL_CHARS - 1] + "~"


def path_statement(index: int, ranked: RankedPath) -> str:
    candidate = ranked.candidate
    parts = [f"path:{index}", f"score:{ranked.score:.2f}", entity_label(candidate.nodes[0])]
    for position, relation in enumerate(candidate.relations):
        here, there = candidate.nodes[position], candidate.nodes[position + 1]
        kind = _UNSAFE.sub("_", relation.relation_type)
        forward = (relation.src_entity_id, relation.dst_entity_id) == (here.entity_id, there.entity_id)
        parts.append(f"-{kind}->" if forward else f"<-{kind}-")
        parts.append(entity_label(there))
    cited = candidate.evidence_ids
    if cited:
        shown = ",".join(_UNSAFE.sub("_", chunk_id)[:64] for chunk_id in cited[:MAX_CITATIONS])
        extra = len(cited) - MAX_CITATIONS
        parts.append(f"ev:{shown}" + (f",+{extra}" if extra > 0 else ""))
    external = sum(candidate.external_evidence)
    if external:
        parts.append(f"att:{external}")
    return " ".join(parts)


@dataclass(frozen=True, slots=True)
class SerializedGraph:
    """The ``[NCP:ONTOLOGY]`` block and which ranked paths it holds."""

    statements: tuple[str, ...]
    included: tuple[RankedPath, ...]
    omitted_for_tokens: int

    @property
    def text(self) -> str | None:
        return "\n".join((HEADER, *self.statements)) if self.statements else None

    @property
    def tokens(self) -> int:
        text = self.text
        return 0 if text is None else estimate_tokens(text)

    def shrink(self) -> SerializedGraph:
        """Drop the lowest-ranked statement (an empty result has no text)."""
        return SerializedGraph(
            self.statements[:-1], self.included[:-1], self.omitted_for_tokens + 1,
        )


def serialize(ranked: Sequence[RankedPath], *, max_tokens: int) -> SerializedGraph:
    """Greedy in rank order: a statement is kept when the whole block still
    fits ``max_tokens``. Paths whose relations are all covered by statements
    already kept add nothing and are skipped."""
    statements: list[str] = []
    included: list[RankedPath] = []
    covered: set[str] = set()
    omitted = 0
    for path in ranked:
        relation_ids = set(path.candidate.relation_ids)
        if relation_ids <= covered:
            continue
        statement = path_statement(len(statements) + 1, path)
        if estimate_tokens("\n".join((HEADER, *statements, statement))) > max_tokens:
            omitted += 1
            continue
        statements.append(statement)
        included.append(path)
        covered |= relation_ids
    return SerializedGraph(tuple(statements), tuple(included), omitted)


__all__ = ["HEADER", "SerializedGraph", "entity_label", "path_statement", "serialize"]
