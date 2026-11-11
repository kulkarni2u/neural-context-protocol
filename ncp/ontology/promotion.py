"""Promote candidate relations to verified (``ncp ontology promote`` and seed auto-promote).

Heuristic sources (``ncp ontology scan``) emit ``candidate`` relations. Context
assembly only follows ``verified`` ones, so a candidate stays out of prompts
until someone (or a rule) vouches for it. This module holds:

* :func:`select_promotions` -- a pure rule engine: which candidates meet the
  ``[ontology.promotion]`` rules, and why each other one is held back;
* :func:`parse_checklist` -- ticked ``- [x] `<src> -TYPE-> <dst>` `` lines of a
  review checklist;
* :func:`promote_relation` / :func:`demote_relation` -- the store writes.

Promotion re-asserts the *same* relation id as ``verified`` (keeping its
evidence, adding an attestation only when it had none), so history and
revision counts stay intact. Demotion re-asserts it as ``candidate``.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from ncp.ontology.facade import OntologyError, _translate_errors
from ncp.ontology.models import AuthContext, EvidenceRef, Relation, normalize_key
from ncp.ontology.policy import PromotionConfig

if TYPE_CHECKING:
    from ncp.ontology.facade import OntologyFacade
    from ncp.ontology.seed import SeedManifest

PRODUCER, CONSUMER = "PRODUCES", "CONSUMES"
CLI_PRINCIPAL = "cli:promote"
# Contract shared with the wiki generator: a ticked checklist line.
CHECKLIST_RE = re.compile(
    r"^\s*[-*]\s*\[[xX]\]\s*`([^`]+?) -([A-Z][A-Z0-9_]*)-> ([^`]+?)`", re.MULTILINE,
)
_PLACEHOLDER_RE = re.compile(r"(^|:)(unresolved|pattern):")


@dataclass(frozen=True)
class PromotionItem:
    """One relation as the rules see it. ``src``/``dst`` are ``Type:key`` (or bare keys)."""

    key: str
    src: str
    type: str
    dst: str
    status: str = "candidate"
    confidence: float = 0.0
    evidence: str = ""

    @property
    def ref(self) -> str:
        return f"{self.src} -{self.type}-> {self.dst}"


@dataclass(frozen=True)
class PromotionDecision:
    item: PromotionItem
    reason: str


@dataclass
class PromotionPlan:
    promoted: list[PromotionDecision] = field(default_factory=list)
    held: list[PromotionDecision] = field(default_factory=list)

    def to_dict(self) -> dict[str, object]:
        def rows(items: list[PromotionDecision]) -> list[dict[str, object]]:
            return [{"ref": d.item.ref, "key": d.item.key, "reason": d.reason} for d in items]
        return {"promoted": rows(self.promoted), "held_back": rows(self.held)}


def is_unresolved(dst: str) -> bool:
    """Whether ``dst`` (``Type:key`` or a bare key) is a scanner placeholder."""
    return _PLACEHOLDER_RE.search(dst) is not None


def split_ref(ref: str) -> tuple[str | None, str]:
    """``"Type:key"`` -> ``("Type", "key")``; a bare key -> ``(None, key)``."""
    head, sep, tail = ref.strip().partition(":")
    if sep and head and head[0].isupper() and head.isidentifier():
        return head, tail
    return None, ref.strip()


def _key_of(ref: str) -> str:
    return normalize_key(split_ref(ref)[1])


def rule_label(rules: PromotionConfig) -> str:
    """Short name of the rule set, used in ``auto-promote:<rule>`` attestations."""
    parts = [f"conf{rules.min_confidence:g}"]
    if rules.require_resolved:
        parts.append("resolved")
    if rules.require_counterpart:
        parts.append("counterpart")
    return "-".join(parts)


def items_from_manifest(manifest: SeedManifest) -> list[PromotionItem]:
    types: dict[str, set[str]] = {}
    for entity in manifest.entities:
        types.setdefault(normalize_key(entity.key), set()).add(entity.type)

    def full(ref: str) -> str:
        entity_type, key = split_ref(ref)
        if entity_type is not None:
            return ref
        found = types.get(normalize_key(key), set())
        return f"{next(iter(found))}:{key}" if len(found) == 1 else ref

    return [
        PromotionItem(
            key=str(index), src=full(rel.src), type=rel.type, dst=full(rel.dst),
            status=rel.status, confidence=rel.confidence,
            evidence="text" if rel.evidence else (rel.attestation or ""),
        )
        for index, rel in enumerate(manifest.relations)
    ]


def select_promotions(
    source: SeedManifest | Sequence[PromotionItem],
    rules: PromotionConfig,
    *,
    context: Sequence[PromotionItem] | None = None,
    include_unresolved: bool = False,
) -> PromotionPlan:
    """Split the candidates of ``source`` into promoted and held-back lists.

    ``source`` is a seed manifest or a list of :class:`PromotionItem`. Only
    ``candidate`` items are judged. ``context`` (default: ``source`` itself)
    is every relation that counts when looking for the producer/consumer
    counterpart (``require_counterpart``), e.g. the whole store. Pure: no I/O.
    ``include_unresolved`` waives ``require_resolved``.
    """
    items = items_from_manifest(source) if hasattr(source, "relations") else list(source)  # type: ignore[arg-type]
    known = list(context) if context is not None else items
    seen: dict[tuple[str, str], int] = {}
    for item in known:
        if item.status != "revoked":
            slot = (item.type, _key_of(item.dst))
            seen[slot] = seen.get(slot, 0) + 1

    plan = PromotionPlan()
    for item in items:
        if item.status != "candidate":
            continue
        reason = None
        if item.type not in rules.types:
            reason = f"type {item.type} is not in promotion.types"
        elif item.confidence < rules.min_confidence:
            reason = f"confidence {item.confidence:g} < min_confidence {rules.min_confidence:g}"
        elif rules.require_resolved and not include_unresolved and is_unresolved(item.dst):
            reason = "unresolved or pattern destination"
        elif rules.require_counterpart and item.type in (PRODUCER, CONSUMER):
            other = CONSUMER if item.type == PRODUCER else PRODUCER
            if not seen.get((other, _key_of(item.dst))):
                reason = f"no {other} counterpart for {item.dst}"
        if reason is None:
            plan.promoted.append(PromotionDecision(item, "meets rules"))
        else:
            plan.held.append(PromotionDecision(item, reason))
    return plan


def parse_checklist(text: str) -> list[tuple[str, str, str]]:
    """``(src_ref, type, dst_ref)`` of every ticked checklist line in ``text``."""
    return [(m.group(1).strip(), m.group(2), m.group(3).strip()) for m in CHECKLIST_RE.finditer(text)]


# -- store writes ----------------------------------------------------------------


def cli_auth(namespace: str, *, tenant: str | None = None) -> AuthContext:
    """The local operator the CLI acts as: read/write/verify in ``namespace``
    (``admin`` too for ``global``), exactly like the seeder."""
    from ncp.ontology.facade import DEFAULT_TENANT
    from ncp.ontology.models import GLOBAL_NAMESPACE, Scope

    with _translate_errors():
        scope = Scope(tenant_id=tenant or DEFAULT_TENANT, namespace=namespace)
    capabilities = {"read", "write", "verify"}
    if namespace == GLOBAL_NAMESPACE:
        capabilities.add("admin")
    return AuthContext(
        principal_id=CLI_PRINCIPAL, scope=scope,
        capabilities=frozenset(capabilities),  # type: ignore[arg-type]
    )


def _key(kind: str, relation: Relation) -> str:
    digest = hashlib.sha256(json.dumps(
        relation.model_dump(mode="json"), sort_keys=True, separators=(",", ":"),
    ).encode()).hexdigest()
    return f"{kind}:{digest[:48]}"


def _write(facade: OntologyFacade, auth: AuthContext, relation: Relation, kind: str) -> Relation:
    store = facade._require_enabled()
    with _translate_errors():
        return store.assert_relation(  # type: ignore[attr-defined,no-any-return]
            relation, auth=auth, idempotency_key=_key(kind, relation),
        )


def promote_relation(
    facade: OntologyFacade, auth: AuthContext, relation: Relation, *, by: str,
    verified_confidence: float, now: datetime | None = None,
) -> Relation:
    """Re-assert ``relation`` as verified. ``by`` is ``cli`` or ``auto:<rule>``."""
    clock = now or datetime.now(timezone.utc)
    evidence: tuple[EvidenceRef, ...] = relation.evidence
    if not evidence:
        evidence = (EvidenceRef(
            evidence_type="external_attestation", evidence_id=f"promoted:{by.split(':')[0]}",
            source_revision="1", observed_at=clock,
        ),)
    attributes = {k: v for k, v in relation.attributes.items()
                  if k not in ("demoted_by", "demoted_at", "demote_reason")}
    attributes.update({"promoted_by": by, "promoted_at": clock.isoformat()})
    updated = relation.model_copy(update={
        "status": "verified", "confidence": max(relation.confidence, verified_confidence),
        "evidence": evidence, "attributes": attributes,
    })
    return _write(facade, auth, updated, "promote")


def demote_relation(
    facade: OntologyFacade, auth: AuthContext, relation: Relation, *, reason: str,
    now: datetime | None = None,
) -> Relation:
    """Turn a verified relation back into a candidate (it keeps its evidence)."""
    clock = now or datetime.now(timezone.utc)
    attributes = {k: v for k, v in relation.attributes.items()
                  if k not in ("promoted_by", "promoted_at")}
    attributes.update({"demoted_by": "cli", "demoted_at": clock.isoformat(),
                       "demote_reason": reason})
    updated = relation.model_copy(update={"status": "candidate", "attributes": attributes})
    return _write(facade, auth, updated, "demote")


@dataclass(frozen=True)
class StoredItem:
    """A stored relation with its endpoint refs resolved."""

    relation: Relation
    item: PromotionItem


def load_relations(facade: OntologyFacade, auth: AuthContext, *, limit: int = 10_000) -> list[StoredItem]:
    """Every active relation in ``auth``'s namespace with ``Type:key`` endpoints."""
    store = facade._require_enabled()
    with _translate_errors():
        relations = store.list_relations(auth=auth, limit=limit)  # type: ignore[attr-defined]
        names = store.get_entities(  # type: ignore[attr-defined]
            [r.src_entity_id for r in relations] + [r.dst_entity_id for r in relations],
            auth=auth,
        )

    def ref(entity_id: object) -> str:
        entity = names.get(str(entity_id))
        return str(entity_id) if entity is None else f"{entity.entity_type}:{entity.canonical_key}"

    out = []
    for r in relations:
        text = ", ".join(e.evidence_id for e in r.evidence)
        out.append(StoredItem(r, PromotionItem(
            key=str(r.relation_id), src=ref(r.src_entity_id), type=r.relation_type,
            dst=ref(r.dst_entity_id), status=r.status, confidence=r.confidence, evidence=text,
        )))
    return out


def ref_matches(wanted: str, actual: str) -> bool:
    """Whether checklist ref ``wanted`` names the ``Type:key`` ref ``actual``."""
    w_type, w_key = split_ref(wanted)
    a_type, a_key = split_ref(actual)
    return normalize_key(w_key) == normalize_key(a_key) and w_type in (None, a_type)


def checklist_matches(entries: Iterable[tuple[str, str, str]], item: PromotionItem) -> bool:
    return any(
        t == item.type and ref_matches(s, item.src) and ref_matches(d, item.dst)
        for s, t, d in entries
    )


__all__ = [
    "CHECKLIST_RE", "OntologyError", "PromotionDecision", "PromotionItem", "PromotionPlan",
    "StoredItem", "checklist_matches", "cli_auth", "demote_relation", "is_unresolved",
    "items_from_manifest", "load_relations", "parse_checklist", "promote_relation",
    "ref_matches", "rule_label", "select_promotions", "split_ref",
]
