"""Measure scan accuracy: compare a scanned manifest against a golden one.

The golden file is an ordinary seed manifest (hand-curated, or a reviewed
``ncp-ontology.*`` file) with an optional top-level ``eval`` table::

    [eval]
    ignore_entity_types = ["Team"]
    ignore_relation_types = ["OWNS"]

Entities match on ``(type, canonical key)``; an alias on either side that
equals a key or alias on the other side also matches. Relations match on
``(src, type, dst)`` after refs are resolved through the matched entities.
Actual relations pointing at ``unresolved:*`` placeholders are reported in
their own bucket: they never count as false positives, and the expected
relation they most likely stand for (same source and type) stays a miss.
"""

from __future__ import annotations

import json
import tomllib
from collections.abc import Collection, Mapping
from dataclasses import dataclass, field
from pathlib import Path

from ncp.ontology.facade import OntologyError
from ncp.ontology.models import Entity
from ncp.ontology.seed import SeedManifest, SeedRelation, _split_ref, parse_manifest

UNRESOLVED_PREFIX = "unresolved:"


def _canon(value: str) -> str:
    return Entity.canonicalize(value)


@dataclass
class Metrics:
    tp: int = 0
    fp: int = 0
    fn: int = 0

    @property
    def precision(self) -> float:
        total = self.tp + self.fp
        return self.tp / total if total else 1.0

    @property
    def recall(self) -> float:
        total = self.tp + self.fn
        return self.tp / total if total else 1.0

    @property
    def f1(self) -> float:
        p, r = self.precision, self.recall
        return 2 * p * r / (p + r) if p + r else 0.0

    def to_dict(self) -> dict[str, float | int]:
        return {"tp": self.tp, "fp": self.fp, "fn": self.fn,
                "precision": round(self.precision, 4), "recall": round(self.recall, 4),
                "f1": round(self.f1, 4)}


@dataclass
class EvalReport:
    entities: Metrics = field(default_factory=Metrics)
    relations: Metrics = field(default_factory=Metrics)
    entities_by_type: dict[str, Metrics] = field(default_factory=dict)
    relations_by_type: dict[str, Metrics] = field(default_factory=dict)
    missing_entities: list[str] = field(default_factory=list)
    unexpected_entities: list[str] = field(default_factory=list)
    missing_relations: list[str] = field(default_factory=list)
    unexpected_relations: list[str] = field(default_factory=list)
    status_mismatches: list[str] = field(default_factory=list)
    # {"actual": <relation>, "expected": <likely intended relation> | None}
    unresolved: list[dict[str, str | None]] = field(default_factory=list)

    def to_dict(self) -> dict[str, object]:
        return {
            "entities": self.entities.to_dict(),
            "relations": self.relations.to_dict(),
            "entities_by_type": {k: v.to_dict() for k, v in sorted(self.entities_by_type.items())},
            "relations_by_type": {k: v.to_dict() for k, v in sorted(self.relations_by_type.items())},
            "missing_entities": list(self.missing_entities),
            "unexpected_entities": list(self.unexpected_entities),
            "missing_relations": list(self.missing_relations),
            "unexpected_relations": list(self.unexpected_relations),
            "status_mismatches": list(self.status_mismatches),
            "unresolved": list(self.unresolved),
        }


# -- loading -----------------------------------------------------------------------------


def split_eval(raw: Mapping[str, object]) -> tuple[dict[str, object], dict[str, object]]:
    """Separate the optional ``eval`` table from a manifest mapping."""
    body = dict(raw)
    options = body.pop("eval", None) or {}
    if not isinstance(options, Mapping):
        raise OntologyError("invalid", "'eval' must be an object/table")
    unknown = set(options) - {"ignore_entity_types", "ignore_relation_types"}
    if unknown:
        raise OntologyError("invalid", f"unknown 'eval' keys: {sorted(unknown)}")
    return body, dict(options)


def load_raw(path: str | Path) -> dict[str, object]:
    """Read a ``.json``/``.toml``/``.yaml`` file as a mapping; a ``ncp ontology
    scan --json`` payload is unwrapped to its embedded ``manifest``."""
    file = Path(path)
    try:
        text = file.read_text(encoding="utf-8")
    except OSError as exc:
        raise OntologyError("invalid", f"cannot read {file}: {exc.strerror}") from None
    try:
        suffix = file.suffix.lower()
        if suffix == ".toml":
            raw = tomllib.loads(text)
        elif suffix in (".yaml", ".yml"):
            import yaml  # type: ignore[import-untyped]
            raw = yaml.safe_load(text)
        else:
            raw = json.loads(text)
    except Exception as exc:
        raise OntologyError("invalid", f"cannot parse {file}: {exc}") from None
    if not isinstance(raw, dict):
        raise OntologyError("invalid", f"{file} must hold an object at the top level")
    if "entities" not in raw and isinstance(raw.get("manifest"), dict):
        raw = raw["manifest"]
    return raw


# -- matching ----------------------------------------------------------------------------


class _Index:
    """Entities of one manifest, resolvable by ref (``key`` or ``Type:key``)."""

    def __init__(self, manifest: SeedManifest) -> None:
        self.entities: list[tuple[str, str, frozenset[str]]] = []
        for entity in manifest.entities:
            names = frozenset({_canon(entity.key), *(_canon(a) for a in entity.aliases)})
            self.entities.append((entity.type, _canon(entity.key), names))
        self.by_name: dict[str, list[int]] = {}
        for idx, (_type, _key, names) in enumerate(self.entities):
            for name in names:
                self.by_name.setdefault(name, []).append(idx)

    def resolve(self, ref: str) -> int | tuple[str | None, str]:
        """Entity index, or ``(type, key)`` for a ref that names no entity."""
        etype, key = _split_ref(ref)
        canon = _canon(key)
        hits = self.by_name.get(canon, [])
        if etype is not None:
            typed = [i for i in hits if self.entities[i][0] == etype]
            if typed:
                return typed[0]
        elif hits:
            return hits[0]
        full = self.by_name.get(_canon(ref), [])
        if full:
            return full[0]
        return (etype, canon)


def _fmt_rel(src: str, rtype: str, dst: str) -> str:
    return f"{src} -[{rtype}]-> {dst}"


def evaluate(
    expected: SeedManifest | Mapping[str, object],
    actual: SeedManifest | Mapping[str, object],
    *,
    entity_types: Collection[str] | None = None,
    relation_types: Collection[str] | None = None,
    ignore_status: bool = True,
    match: str = "canonical",
) -> EvalReport:
    """Score ``actual`` against ``expected``.

    ``entity_types`` / ``relation_types`` restrict scoring to those types; an
    ``eval`` table in a mapping ``expected`` (``ignore_entity_types`` /
    ``ignore_relation_types``) excludes types. Relations touching an ignored
    entity type are ignored too.
    """
    if match != "canonical":
        raise ValueError(f"unsupported match mode {match!r}; expected 'canonical'")
    ignore_entities: set[str] = set()
    ignore_relations: set[str] = set()
    if isinstance(expected, Mapping):
        body, options = split_eval(expected)
        ignore_entities = set(options.get("ignore_entity_types") or ())  # type: ignore[call-overload]
        ignore_relations = set(options.get("ignore_relation_types") or ())  # type: ignore[call-overload]
        expected = parse_manifest(body)
    if isinstance(actual, Mapping):
        actual = parse_manifest(split_eval(actual)[0])

    def entity_ok(etype: str) -> bool:
        return etype not in ignore_entities and (entity_types is None or etype in entity_types)

    def relation_ok(rtype: str) -> bool:
        return rtype not in ignore_relations and (relation_types is None or rtype in relation_types)

    exp_idx, act_idx = _Index(expected), _Index(actual)
    report = EvalReport()

    # Entities: map each actual entity to the expected one it matches.
    act_to_exp: dict[int, int] = {}
    exp_matched: set[int] = set()
    for a, (atype, akey, anames) in enumerate(act_idx.entities):
        if akey.startswith(UNRESOLVED_PREFIX):
            continue
        for e, (etype, ekey, enames) in enumerate(exp_idx.entities):
            if etype == atype and e not in exp_matched and anames & enames:
                act_to_exp[a] = e
                exp_matched.add(e)
                break

    def bump(by_type: dict[str, Metrics], total: Metrics, kind: str, which: str) -> None:
        setattr(total, which, getattr(total, which) + 1)
        metrics = by_type.setdefault(kind, Metrics())
        setattr(metrics, which, getattr(metrics, which) + 1)

    for e, (etype, ekey, _names) in enumerate(exp_idx.entities):
        if not entity_ok(etype):
            continue
        if e in exp_matched:
            bump(report.entities_by_type, report.entities, etype, "tp")
        else:
            bump(report.entities_by_type, report.entities, etype, "fn")
            report.missing_entities.append(f"{etype}:{ekey}")
    for a, (atype, akey, _names) in enumerate(act_idx.entities):
        if not entity_ok(atype) or a in act_to_exp or akey.startswith(UNRESOLVED_PREFIX):
            continue
        bump(report.entities_by_type, report.entities, atype, "fp")
        report.unexpected_entities.append(f"{atype}:{akey}")

    # Relations: nodes are expected-entity indexes, else the raw (type, key).
    def node(index: _Index, ref: str, mapping: dict[int, int] | None) -> tuple[object, str, str | None]:
        resolved = index.resolve(ref)
        if isinstance(resolved, int):
            etype, key, _ = index.entities[resolved]
            ident: object = mapping.get(resolved, ("act", resolved)) if mapping is not None else resolved
            return ident, f"{etype}:{key}", etype
        etype, key = resolved
        return ("raw", etype, key), f"{etype}:{key}" if etype else key, etype

    def is_unresolved(label: str) -> bool:
        return UNRESOLVED_PREFIX in label

    expected_rels: dict[tuple[object, str, object], tuple[SeedRelation, str]] = {}
    for rel in expected.relations:
        s, slabel, stype = node(exp_idx, rel.src, None)
        d, dlabel, dtype = node(exp_idx, rel.dst, None)
        if not relation_ok(rel.type) or not all(t is None or entity_ok(t) for t in (stype, dtype)):
            continue
        expected_rels.setdefault((s, rel.type, d), (rel, _fmt_rel(slabel, rel.type, dlabel)))

    matched: set[tuple[object, str, object]] = set()
    unresolved_actual: list[tuple[SeedRelation, object, str]] = []
    seen_actual: set[tuple[object, str, object]] = set()
    for rel in actual.relations:
        s, slabel, stype = node(act_idx, rel.src, act_to_exp)
        d, dlabel, dtype = node(act_idx, rel.dst, act_to_exp)
        if not relation_ok(rel.type) or not all(t is None or entity_ok(t) for t in (stype, dtype)):
            continue
        key = (s, rel.type, d)
        label = _fmt_rel(slabel, rel.type, dlabel)
        if is_unresolved(slabel) or is_unresolved(dlabel):
            unresolved_actual.append((rel, s, label))
            continue
        if key in seen_actual:
            continue
        seen_actual.add(key)
        if key in expected_rels:
            matched.add(key)
            bump(report.relations_by_type, report.relations, rel.type, "tp")
            exp_rel = expected_rels[key][0]
            if not ignore_status and exp_rel.status != rel.status:
                report.status_mismatches.append(
                    f"{label}: expected {exp_rel.status}, got {rel.status}"
                )
        else:
            bump(report.relations_by_type, report.relations, rel.type, "fp")
            report.unexpected_relations.append(label)

    missing: dict[tuple[object, str, object], str] = {}
    for key, (rel, label) in expected_rels.items():
        if key not in matched:
            bump(report.relations_by_type, report.relations, rel.type, "fn")
            report.missing_relations.append(label)
            missing[key] = label
    for rel, src_ident, label in unresolved_actual:
        likely = next(
            (lab for (s, rtype, _d), lab in missing.items() if s == src_ident and rtype == rel.type),
            None,
        )
        report.unresolved.append({"actual": label, "expected": likely})
    return report


def render_text(report: EvalReport, *, show: int = 10, by_type: bool = False) -> str:
    """Human-readable summary."""
    def line(name: str, m: Metrics) -> str:
        return (f"{name:<14} precision {m.precision:.3f}  recall {m.recall:.3f}  "
                f"f1 {m.f1:.3f}  (tp {m.tp}, fp {m.fp}, fn {m.fn})")

    lines = [line("entities", report.entities), line("relations", report.relations)]
    if by_type:
        for kind, m in sorted(report.entities_by_type.items()):
            lines.append("  " + line(f"entity {kind}", m))
        for kind, m in sorted(report.relations_by_type.items()):
            lines.append("  " + line(f"rel {kind}", m))
    sections = (
        ("missing entities (FN)", report.missing_entities),
        ("unexpected entities (FP)", report.unexpected_entities),
        ("missing relations (FN)", report.missing_relations),
        ("unexpected relations (FP)", report.unexpected_relations),
        ("status mismatches", report.status_mismatches),
        ("unresolved destinations",
         [f"{u['actual']}" + (f"  (expected: {u['expected']})" if u["expected"] else "")
          for u in report.unresolved]),
    )
    for title, items in sections:
        if not items:
            continue
        lines.append(f"{title}: {len(items)}")
        lines.extend(f"  {item}" for item in items[:show])
        if len(items) > show:
            lines.append(f"  ... {len(items) - show} more")
    return "\n".join(lines)
