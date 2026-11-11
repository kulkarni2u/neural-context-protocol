"""``ncp ontology wiki``: render a scan manifest as a browsable, diffable markdown wiki.

Pure functions only: :func:`build_wiki` maps a manifest dict (plus an optional scan
summary and an optional baseline manifest) to ``{relative_path: markdown}``;
:func:`write_wiki` puts the pages on disk. Output is deterministic (sorted, stable).

Rendering is generic over entity and relation types: a *destination* is any entity
that is the ``dst`` of a ``PRODUCES`` or ``CONSUMES`` relation.
"""

from __future__ import annotations

import fnmatch
import posixpath
import re
from collections import defaultdict
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ncp.ontology.scan_messaging import is_pattern_key, is_unresolved_key

PRODUCES = "PRODUCES"
CONSUMES = "CONSUMES"
MESSAGING = (PRODUCES, CONSUMES)
FLAG_NO_CONSUMER = "⚠ no consumer in repo"
FLAG_NO_PRODUCER = "⚠ no producer in repo"
FLAG_UNRESOLVED = "⚠ unresolved name"
FLAG_PATTERN = "pattern subscription"
PROBLEM_FLAGS = (FLAG_NO_CONSUMER, FLAG_NO_PRODUCER, FLAG_UNRESOLVED)

Ref = tuple[str, str]  # (entity type, key)

_SLUG_BAD = re.compile(r"[^a-z0-9._-]+")


# -- small helpers ---------------------------------------------------------------------


def slugify(text: str) -> str:
    slug = _SLUG_BAD.sub("-", text.lower()).strip("-.")
    return slug or "x"


def _cell(value: object) -> str:
    text = "" if value is None else str(value)
    text = text.replace("\r", " ").replace("\n", " ")
    return text.replace("\\", "\\\\").replace("|", "\\|").replace("`", "\\`")


def _inline(value: object) -> str:
    """Single-line text with backticks neutralised (safe inside a code span)."""
    return str(value).replace("`", "'").replace("\n", " ").replace("\r", " ")


def _label(ref: Ref) -> str:
    return f"{ref[0]}:{ref[1]}"


def _conf(value: object) -> str:
    try:
        return f"{float(value):.2f}"  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return ""


def _truthy(value: object) -> bool:
    return value is True or (isinstance(value, str) and value.strip().lower() in ("true", "1", "yes"))


def _link(from_page: str, to_page: str, text: str) -> str:
    rel = posixpath.relpath(to_page, posixpath.dirname(from_page) or ".")
    return f"[{_cell(text)}]({rel})"


def _table(headers: list[str], rows: list[list[str]]) -> list[str]:
    out = ["| " + " | ".join(headers) + " |", "|" + "|".join(" --- " for _ in headers) + "|"]
    out += ["| " + " | ".join(row) + " |" for row in rows]
    return out


# -- normalised model ------------------------------------------------------------------


@dataclass(frozen=True)
class Rel:
    src: Ref
    type: str
    dst: Ref
    status: str
    confidence: float | None
    attestation: str
    attributes: Mapping[str, Any]


@dataclass
class Model:
    entities: dict[Ref, dict[str, Any]]
    relations: list[Rel]
    by_src: dict[Ref, list[Rel]] = field(default_factory=lambda: defaultdict(list))
    by_dst: dict[Ref, list[Rel]] = field(default_factory=lambda: defaultdict(list))
    destinations: list[Ref] = field(default_factory=list)


def _build_model(manifest: Mapping[str, Any]) -> Model:
    entities: dict[Ref, dict[str, Any]] = {}
    by_key: dict[str, list[Ref]] = defaultdict(list)
    for raw in manifest.get("entities") or []:
        ref = (str(raw["type"]), str(raw["key"]))
        entities[ref] = dict(raw)
        by_key[ref[1]].append(ref)

    def resolve(text: str) -> Ref:
        text = str(text)
        if ":" in text:
            etype, _, key = text.partition(":")
            if (etype, key) in entities:
                return (etype, key)
        if len(by_key.get(text, ())) == 1:
            return by_key[text][0]
        etype, sep, key = text.partition(":")
        if sep and etype[:1].isupper():
            return (etype, key)
        return ("?", text)

    relations: list[Rel] = []
    seen: set[tuple[Ref, str, Ref]] = set()
    for raw in manifest.get("relations") or []:
        src, dst = resolve(raw["src"]), resolve(raw["dst"])
        ident = (src, str(raw["type"]), dst)
        if ident in seen:
            continue
        seen.add(ident)
        conf = raw.get("confidence")
        relations.append(Rel(
            src, str(raw["type"]), dst, str(raw.get("status") or "verified"),
            float(conf) if isinstance(conf, (int, float)) else None,
            str(raw.get("attestation") or ""), dict(raw.get("attributes") or {}),
        ))
    relations.sort(key=lambda r: (r.src, r.type, r.dst))
    for ref in (r.src for r in relations):
        entities.setdefault(ref, {"type": ref[0], "key": ref[1]})
    for ref in (r.dst for r in relations):
        entities.setdefault(ref, {"type": ref[0], "key": ref[1]})
    model = Model(entities, relations)
    for rel in relations:
        model.by_src[rel.src].append(rel)
        model.by_dst[rel.dst].append(rel)
    model.destinations = sorted({r.dst for r in relations if r.type in MESSAGING})
    return model


def _attrs(model: Model, ref: Ref) -> Mapping[str, Any]:
    return model.entities.get(ref, {}).get("attributes") or {}


def _is_unresolved(model: Model, ref: Ref) -> bool:
    attrs = _attrs(model, ref)
    return (
        is_unresolved_key(ref[1]) or _truthy(attrs.get("unresolved"))
        or any(_truthy(r.attributes.get("unresolved")) for r in model.by_dst.get(ref, ()))
    )


def _is_pattern(model: Model, ref: Ref) -> bool:
    attrs = _attrs(model, ref)
    return is_pattern_key(ref[1]) or _truthy(attrs.get("pattern"))


def _pattern_text(model: Model, ref: Ref) -> str:
    attrs = _attrs(model, ref)
    raw = attrs.get("pattern") if isinstance(attrs.get("pattern"), str) and not _truthy(attrs.get("pattern")) else None
    if raw:
        return str(raw)
    name = str(attrs.get("name") or ref[1])
    marked = _PATTERN_MARK.search(name)
    if marked:
        return name[marked.end():]  # ``pattern:x`` / ``<broker>:pattern:x``
    if ":" in name and ref[1].count(":") >= 2:
        name = name.split(":", 1)[1]
    return name


def _broker(model: Model, ref: Ref) -> str:
    attrs = _attrs(model, ref)
    if attrs.get("broker"):
        return str(attrs["broker"])
    if ref[0] == "KafkaTopic":
        return "kafka"
    return ref[0].lower()


def _display_name(model: Model, ref: Ref) -> str:
    attrs = _attrs(model, ref)
    return str(attrs.get("name") or ref[1])


_PATTERN_MARK = re.compile(r"(?:^|:)pattern:")
MAX_PATTERN_LENGTH = 200
_MAX_NAME_LENGTH = 500
_SAFE_REGEX_LITERAL = re.compile(r"[A-Za-z0-9_\-/]")


def _regex_tokens(pattern: str) -> list[str] | None:
    """Tokens of a repo-supplied topic regex limited to literals, ``\\.``, ``.``, ``.*`` and
    ``.+`` (optionally anchored with ``^`` / ``$``): ``"."`` any char, ``"*"`` any run, any other
    token a literal. None for anything richer. The regex is never compiled."""
    text = pattern[1:] if pattern.startswith("^") else pattern
    text = text[:-1] if text.endswith("$") and not text.endswith("\\$") else text
    tokens: list[str] = []
    index = 0
    while index < len(text):
        char = text[index]
        if char == "\\" and text[index + 1:index + 2] == ".":
            tokens.append("\\.")
            index += 2
        elif char == ".":
            follow = text[index + 1:index + 2]
            if follow == "*":
                tokens.append("*")
                index += 2
            elif follow == "+":
                tokens += [".", "*"]
                index += 2
            else:
                tokens.append(".")
                index += 1
        elif _SAFE_REGEX_LITERAL.fullmatch(char):
            tokens.append(char)
            index += 1
        else:
            return None
    return tokens


def _glob_tokens_match(tokens: list[str], name: str) -> bool:
    """Wildcard match in O(len(tokens) * len(name)): no backtracking."""
    reachable = [True] + [False] * len(name)
    for token in tokens:
        following = [False] * (len(name) + 1)
        if token == "*":
            seen = False
            for i in range(len(name) + 1):
                seen = seen or reachable[i]
                following[i] = seen
        else:
            literal = "." if token == "\\." else token
            for i, char in enumerate(name):
                if reachable[i] and (token == "." or char == literal):
                    following[i + 1] = True
        reachable = following
    return reachable[-1]


def _pattern_supported(pattern: str) -> bool:
    """Whether :func:`_pattern_matches` can evaluate ``pattern`` (glob, or a simple regex)."""
    return len(pattern) <= MAX_PATTERN_LENGTH and (
        _regex_tokens(pattern) is not None or not any(c in pattern for c in "()|+{}\\"))


def _pattern_matches(pattern: str, name: str) -> bool:
    """Best-effort: ``fnmatch`` globs and simple regexes (literals, ``\\.``, ``.``, ``.*``,
    ``.+``). Repo-supplied regexes are never compiled (ReDoS); anything richer, or longer than
    :data:`MAX_PATTERN_LENGTH`, does not match."""
    if len(pattern) > MAX_PATTERN_LENGTH or len(name) > _MAX_NAME_LENGTH:
        return False
    if fnmatch.fnmatchcase(name, pattern):
        return True
    tokens = _regex_tokens(pattern)
    return tokens is not None and _glob_tokens_match(tokens, name)


@dataclass
class DestInfo:
    ref: Ref
    producers: list[Rel]
    consumers: list[Rel]
    pattern_consumers: list[tuple[Rel, Ref]]  # (consumer edge of the pattern dest, pattern dest)
    routed_from: list[Ref]
    flags: list[str]

    @property
    def problems(self) -> bool:
        return any(f in PROBLEM_FLAGS for f in self.flags)

    @property
    def status_mix(self) -> str:
        counts: dict[str, int] = defaultdict(int)
        for rel in self.producers + self.consumers:
            counts[rel.status] += 1
        return ", ".join(f"{k} {counts[k]}" for k in sorted(counts))


def _analyse(model: Model) -> dict[Ref, DestInfo]:
    patterns = [d for d in model.destinations if _is_pattern(model, d)]
    infos: dict[Ref, DestInfo] = {}
    for dest in model.destinations:
        edges = model.by_dst.get(dest, [])
        producers = [r for r in edges if r.type == PRODUCES]
        consumers = [r for r in edges if r.type == CONSUMES]
        routed_from = sorted({r.src for r in edges if r.type == "ROUTES_TO"})
        is_pattern = _is_pattern(model, dest)
        unresolved = _is_unresolved(model, dest)
        potential: list[tuple[Rel, Ref]] = []
        if not is_pattern and not unresolved:
            name = _display_name(model, dest)
            for pat in patterns:
                if pat[0] != dest[0] or _broker(model, pat) != _broker(model, dest):
                    continue
                if _pattern_matches(_pattern_text(model, pat), name):
                    potential += [(r, pat) for r in model.by_dst.get(pat, []) if r.type == CONSUMES]
        flags: list[str] = []
        if unresolved:
            flags.append(FLAG_UNRESOLVED)
        if is_pattern:
            flags.append(FLAG_PATTERN)
        if not is_pattern and not unresolved:
            if not consumers and not potential:
                flags.append(FLAG_NO_CONSUMER)
            if not producers and not routed_from:
                flags.append(FLAG_NO_PRODUCER)
        infos[dest] = DestInfo(dest, producers, consumers, sorted(potential, key=lambda x: (x[0].src, x[1])),
                               routed_from, flags)
    return infos


# -- paths -----------------------------------------------------------------------------


def _assign_paths(model: Model) -> dict[Ref, str]:
    """Deterministic page path per entity that gets a page."""
    messaging_sources = {r.src for r in model.relations if r.type in MESSAGING}
    wanted: list[tuple[Ref, str, str]] = []
    for ref in sorted(model.entities):
        if ref[0] == "Service":
            wanted.append((ref, "services", slugify(ref[1])))
        elif ref[0] == "Module" and ref in messaging_sources:
            wanted.append((ref, "modules", slugify(ref[1])))
    for ref in model.destinations:
        wanted.append((ref, "destinations", f"{slugify(ref[0])}-{slugify(ref[1])}"))
    used: set[str] = set()
    paths: dict[Ref, str] = {}
    for ref, folder, slug in wanted:
        candidate, n = slug, 1
        while f"{folder}/{candidate}" in used:
            n += 1
            candidate = f"{slug}-{n}"
        used.add(f"{folder}/{candidate}")
        paths[ref] = f"{folder}/{candidate}.md"
    return paths


# -- entity / destination links --------------------------------------------------------


class _Ctx:
    def __init__(self, model: Model, infos: dict[Ref, DestInfo], paths: dict[Ref, str]) -> None:
        self.model, self.infos, self.paths = model, infos, paths

    def ref_link(self, page: str, ref: Ref) -> str:
        target = self.paths.get(ref)
        text = _label(ref)
        return _link(page, target, text) if target else _cell(text)


def _evidence(rel: Rel) -> str:
    return _cell(rel.attestation)


def _edge_extras(rel: Rel) -> tuple[str, str]:
    return _cell(rel.attributes.get("resolved_from", "")), _cell(rel.attributes.get("via_module", ""))


# -- pages -----------------------------------------------------------------------------


def _identity_lines(ctx: _Ctx, ref: Ref) -> list[str]:
    model = ctx.model
    ent = model.entities.get(ref, {})
    attrs = ent.get("attributes") or {}
    owners = sorted({_label(r.src) for r in model.by_dst.get(ref, ()) if r.type == "OWNS"})
    lines = [f"- **Type:** {_cell(ref[0])}", f"- **Key:** `{_inline(ref[1])}`"]
    if ent.get("display_name"):
        lines.append(f"- **Display name:** {_cell(ent['display_name'])}")
    aliases = sorted(str(a) for a in ent.get("aliases") or [])
    lines.append("- **Aliases:** " + (", ".join(f"`{_inline(a)}`" for a in aliases) if aliases else "none"))
    lines.append(f"- **Detected by:** {_cell(attrs.get('detected_by')) or 'n/a'}")
    lines.append("- **Owners:** " + (", ".join(_cell(o) for o in owners) if owners else "none"))
    return lines


def _messaging_table(ctx: _Ctx, page: str, ref: Ref, rel_type: str) -> list[str]:
    rows = []
    for rel in ctx.model.by_src.get(ref, ()):
        if rel.type != rel_type:
            continue
        info = ctx.infos.get(rel.dst)
        resolved, via = _edge_extras(rel)
        rows.append([
            ctx.ref_link(page, rel.dst),
            _cell(f"{_broker(ctx.model, rel.dst)} / {rel.dst[0]}"),
            _cell(rel.status), _conf(rel.confidence), resolved, via, _evidence(rel),
            _cell(", ".join(info.flags)) if info else "",
        ])
    if not rows:
        return ["_none_"]
    return _table(["Destination", "Broker / type", "Status", "Confidence", "Resolved from",
                   "Via module", "Evidence", "Flags"], rows)


def _relation_list(ctx: _Ctx, page: str, refs: list[Ref]) -> list[str]:
    return [f"- {ctx.ref_link(page, r)}" for r in refs] or ["_none_"]


def _entity_page(ctx: _Ctx, ref: Ref, page: str, title_prefix: str) -> str:
    model = ctx.model
    out = [f"# {title_prefix}: {_cell(ref[1])}", "", f"[Index]({posixpath.relpath('index.md', posixpath.dirname(page))})", "",
           "## Identity", ""]
    out += _identity_lines(ctx, ref)
    out += ["", "## Produces", ""] + _messaging_table(ctx, page, ref, PRODUCES)
    out += ["", "## Consumes", ""] + _messaging_table(ctx, page, ref, CONSUMES)
    out += ["", "## Depends on", ""]
    out += _relation_list(ctx, page, sorted({r.dst for r in model.by_src.get(ref, ()) if r.type == "DEPENDS_ON"}))
    out += ["", "## Depended on by", ""]
    out += _relation_list(ctx, page, sorted({r.src for r in model.by_dst.get(ref, ()) if r.type == "DEPENDS_ON"}))
    out += ["", "## Builds / Contains", ""]
    structural = sorted({r.dst for r in model.by_src.get(ref, ()) if r.type in ("BUILDS", "CONTAINS")})
    out += _relation_list(ctx, page, structural)
    built_by = sorted({r.src for r in model.by_dst.get(ref, ()) if r.type in ("BUILDS", "CONTAINS")})
    if built_by:
        out += ["", "Built / contained by:", ""] + _relation_list(ctx, page, built_by)
    return "\n".join(out) + "\n"


def _side_cell(ctx: _Ctx, page: str, rel: Rel, pattern: Ref | None = None) -> str:
    text = ctx.ref_link(page, rel.src)
    detail = [rel.status]
    if rel.confidence is not None:
        detail.append(_conf(rel.confidence))
    if rel.attestation:
        detail.append(rel.attestation)
    if rel.attributes.get("via_module"):
        detail.append(f"via {rel.attributes['via_module']}")
    if pattern is not None:
        detail.append(f"via pattern {_pattern_text(ctx.model, pattern)}")
    return f"{text} ({_cell(', '.join(detail))})"


def _destination_page(ctx: _Ctx, info: DestInfo, page: str) -> str:
    model = ctx.model
    ref = info.ref
    attrs = _attrs(model, ref)
    out = [f"# {_cell(ref[0])}: {_cell(ref[1])}", "", f"[Index]({posixpath.relpath('index.md', posixpath.dirname(page))})", ""]
    out += ["## Identity", ""] + _identity_lines(ctx, ref)
    out.append(f"- **Broker:** {_cell(_broker(model, ref))}")
    for key in ("spel", "expression", "resolved_from"):
        if attrs.get(key):
            out.append(f"- **{key}:** `{_inline(attrs[key])}`")
    out += ["", "## Flags", ""]
    out += [f"- {f}" for f in info.flags] or ["- none"]
    if info.routed_from:
        out += ["", "## Routed from", ""] + _relation_list(ctx, page, info.routed_from)
    routes_to = sorted({r.dst for r in model.by_src.get(ref, ()) if r.type == "ROUTES_TO"})
    if routes_to:
        out += ["", "## Routes to", ""] + _relation_list(ctx, page, routes_to)
    prod_cells = [_side_cell(ctx, page, r) for r in info.producers]
    cons_cells = [_side_cell(ctx, page, r) for r in info.consumers]
    cons_cells += [_side_cell(ctx, page, r, pat) for r, pat in info.pattern_consumers]
    height = max(len(prod_cells), len(cons_cells), 1)
    rows = [[prod_cells[i] if i < len(prod_cells) else "", cons_cells[i] if i < len(cons_cells) else ""]
            for i in range(height)]
    out += ["", "## Producers vs consumers", ""] + _table(["Producers", "Consumers"], rows)
    if info.pattern_consumers:
        out += ["", "Consumers listed with `via pattern` subscribe through a `pattern:` destination "
                    "that matches this name (best-effort fnmatch/regex)."]
    if _is_pattern(model, ref):
        text = _pattern_text(model, ref)
        out += ["", f"Pattern: `{_inline(text)}`"
                + ("" if _pattern_supported(text) else " (pattern not evaluated)")]
        matches = sorted(d for d in model.destinations
                         if d != ref and not _is_pattern(model, d) and d[0] == ref[0]
                         and _pattern_matches(_pattern_text(model, ref), _display_name(model, d)))
        out += ["", "### Matching destinations", ""] + _relation_list(ctx, page, matches)
    return "\n".join(out) + "\n"


def _problem_sort(info: DestInfo) -> tuple[int, str, str]:
    return (0 if info.problems else 1, info.ref[0], info.ref[1])


def _messaging_page(ctx: _Ctx) -> str:
    page = "messaging.md"
    infos = sorted(ctx.infos.values(), key=_problem_sort)
    out = ["# Messaging matrix", "", "[Index](index.md)", "",
           "Every destination, problems first (orphans and unresolved names).", ""]
    rows = [[
        ctx.ref_link(page, i.ref), _cell(_broker(ctx.model, i.ref)),
        str(len(i.producers)), str(len(i.consumers) + len(i.pattern_consumers)),
        _cell(i.status_mix), _cell(", ".join(i.flags)),
    ] for i in infos]
    out += _table(["Destination", "Broker", "Producers", "Consumers", "Status mix", "Flags"], rows) if rows \
        else ["_No messaging destinations found._"]
    return "\n".join(out) + "\n"


def _coverage_page(ctx: _Ctx, summary: Mapping[str, Any] | None) -> str:
    model = ctx.model
    page = "coverage.md"
    out = ["# Coverage", "", "[Index](index.md)", ""]
    if not summary:
        out += ["_No scan summary supplied. Pass `--summary SUMMARY.json` (written by "
                "`ncp ontology scan --summary-out`) to see per-broker coverage._", ""]
    else:
        messaging = summary.get("messaging")
        if isinstance(messaging, Mapping) and messaging:
            cols = ["sites", "producer_sites", "consumer_sites", "matched_sites", "unmatched_sites",
                    "unresolved", "skipped_no_owner", "skipped_schema"]
            rows = [[_cell(b)] + [_cell(messaging[b].get(c, 0)) for c in cols] for b in sorted(messaging)]
            out += ["## Per-broker coverage", ""] + _table(["Broker"] + cols, rows) + [""]
            examples = [(b, e) for b in sorted(messaging) for e in messaging[b].get("unmatched_examples") or []]
        elif isinstance(summary.get("kafka"), Mapping):
            kafka = summary["kafka"]
            cols = ["files_examined", "kafka_like_sites", "matched_sites", "resolved_via_property",
                    "resolved_via_constant", "unresolved_placeholders", "unmatched_sites", "skipped_no_owner"]
            out += ["## Per-broker coverage", ""]
            out += _table(["Broker"] + cols, [["kafka"] + [_cell(kafka.get(c, 0)) for c in cols]]) + [""]
            examples = [("kafka", e) for e in kafka.get("unmatched_examples") or []]
        else:
            out += ["_The summary has no `messaging` or `kafka` section._", ""]
            examples = []
        attribution = summary.get("attribution")
        if isinstance(attribution, Mapping) and attribution:
            out += ["## Attribution", ""]
            out += _table(["Kind", "Edges"], [[_cell(k), _cell(attribution[k])] for k in sorted(attribution)]) + [""]
        if examples:
            out += ["## Unmatched examples", ""]
            out += [f"- {_cell(b)}: `{_inline(e)}`" for b, e in examples] + [""]
        warnings = [str(w) for w in summary.get("warnings") or []]
        if warnings:
            out += ["## Scan warnings", ""] + [f"- {_cell(w)}" for w in warnings] + [""]
    out += ["## Unresolved and pattern destinations", ""]
    rows = []
    for ref in model.destinations:
        info = ctx.infos[ref]
        if FLAG_UNRESOLVED not in info.flags and FLAG_PATTERN not in info.flags:
            continue
        attrs = _attrs(model, ref)
        edges = info.producers + info.consumers
        raw = attrs.get("spel") or attrs.get("expression") or next(
            (e.attributes[k] for e in edges for k in ("spel", "expression") if e.attributes.get(k)), "")
        resolved = attrs.get("resolved_from") or next(
            (e.attributes["resolved_from"] for e in edges if e.attributes.get("resolved_from")), "")
        rows.append([
            ctx.ref_link(page, ref), _cell(", ".join(f for f in info.flags if f != FLAG_NO_PRODUCER
                                                    and f != FLAG_NO_CONSUMER)),
            _cell(raw or (_pattern_text(model, ref) if _is_pattern(model, ref) else "")),
            _cell(resolved), _cell("; ".join(sorted({e.attestation for e in edges if e.attestation}))),
        ])
    out += _table(["Destination", "Kind", "Raw value", "Resolved from", "Evidence"], rows) if rows \
        else ["_none_"]
    return "\n".join(out) + "\n"


def _candidates_page(ctx: _Ctx) -> str:
    model = ctx.model
    out = [
        "# Candidate relations: review checklist", "", "[Index](index.md)", "",
        "These relations were inferred by the scan (status `candidate`) and are not yet trusted.",
        "",
        "## Promotion workflow", "",
        "1. Open the linked evidence (`path#Lnn`) for each line and check the relation is real.",
        "2. Tick (`[x]`) the lines you have verified; leave the rest unticked.",
        "3. Run `ncp ontology promote --from-checklist candidates.md` to promote only the ticked relations.",
        "",
        "Do not edit the text between the backticks: it is how a ticked line is matched back to its relation.",
        "",
    ]
    cands = [r for r in model.relations if r.status == "candidate"]
    if not cands:
        out.append("_No candidate relations._")
        return "\n".join(out) + "\n"
    groups: dict[Ref, list[Rel]] = defaultdict(list)
    for rel in cands:
        groups[rel.src].append(rel)
    for src in sorted(groups):
        out += [f"### {_cell(_label(src))}", ""]
        for rel in groups[src]:
            line = f"- [ ] `{_inline(_label(rel.src))} -{rel.type}-> {_inline(_label(rel.dst))}`"
            if rel.confidence is not None:
                line += f" — conf {_conf(rel.confidence)}"
            if rel.attestation:
                line += f" · `{_inline(rel.attestation)}`"
            if _is_unresolved(model, rel.dst) or _is_unresolved(model, rel.src):
                line += " · unresolved"
            out.append(line)
        out.append("")
    return "\n".join(out).rstrip("\n") + "\n"


# -- compare ---------------------------------------------------------------------------


@dataclass
class Diff:
    added_entities: list[Ref]
    removed_entities: list[Ref]
    added_relations: list[Rel]
    removed_relations: list[Rel]
    changed_relations: list[tuple[Rel, Rel]]  # (old, new)
    changed_destinations: list[tuple[Ref, set[Ref], set[Ref], set[Ref], set[Ref]]]
    # (dest, producers added, producers removed, consumers added, consumers removed)


def _diff(new: Model, old: Model) -> Diff:
    new_rels = {(r.src, r.type, r.dst): r for r in new.relations}
    old_rels = {(r.src, r.type, r.dst): r for r in old.relations}
    changed = [(old_rels[k], new_rels[k]) for k in sorted(new_rels.keys() & old_rels.keys())
               if (old_rels[k].status, old_rels[k].confidence) != (new_rels[k].status, new_rels[k].confidence)]

    def sides(model: Model, dest: Ref) -> tuple[set[Ref], set[Ref]]:
        edges = model.by_dst.get(dest, ())
        return ({r.src for r in edges if r.type == PRODUCES}, {r.src for r in edges if r.type == CONSUMES})

    dests = []
    for dest in sorted(set(new.destinations) & set(old.destinations)):
        np_, nc = sides(new, dest)
        op, oc = sides(old, dest)
        if (np_, nc) != (op, oc):
            dests.append((dest, np_ - op, op - np_, nc - oc, oc - nc))
    return Diff(
        sorted(new.entities.keys() - old.entities.keys()), sorted(old.entities.keys() - new.entities.keys()),
        [new_rels[k] for k in sorted(new_rels.keys() - old_rels.keys())],
        [old_rels[k] for k in sorted(old_rels.keys() - new_rels.keys())],
        changed, dests,
    )


def _rel_line(rel: Rel) -> str:
    extra = f" (conf {_conf(rel.confidence)}, {rel.status})" if rel.confidence is not None else f" ({rel.status})"
    return f"- `{_inline(_label(rel.src))} -{rel.type}-> {_inline(_label(rel.dst))}`{extra}"


def _entity_groups(refs: list[Ref]) -> list[str]:
    by_type: dict[str, list[Ref]] = defaultdict(list)
    for ref in refs:
        by_type[ref[0]].append(ref)
    out: list[str] = []
    for etype in sorted(by_type):
        out += [f"### {_cell(etype)} ({len(by_type[etype])})", ""]
        out += [f"- `{_inline(r[1])}`" for r in by_type[etype]] + [""]
    return out or ["_none_", ""]


def _compare_page(diff: Diff) -> str:
    out = ["# Compare: baseline vs current", "", "[Index](index.md)", "",
           "Baseline is the `--compare` manifest (old); current is the manifest being rendered (new).", ""]
    out += [f"## Added entities ({len(diff.added_entities)})", ""] + _entity_groups(diff.added_entities)
    out += [f"## Removed entities ({len(diff.removed_entities)})", ""] + _entity_groups(diff.removed_entities)
    out += [f"## Added relations ({len(diff.added_relations)})", ""]
    out += [_rel_line(r) for r in diff.added_relations] or ["_none_"]
    out += ["", f"## Removed relations ({len(diff.removed_relations)})", ""]
    out += [_rel_line(r) for r in diff.removed_relations] or ["_none_"]
    out += ["", f"## Status or confidence changed ({len(diff.changed_relations)})", ""]
    out += [f"- `{_inline(_label(n.src))} -{n.type}-> {_inline(_label(n.dst))}`: "
            f"{o.status} {_conf(o.confidence) or 'n/a'} -> {n.status} {_conf(n.confidence) or 'n/a'}"
            for o, n in diff.changed_relations] or ["_none_"]
    out += ["", f"## Destinations with changed producers or consumers ({len(diff.changed_destinations)})", ""]
    if diff.changed_destinations:
        rows = []
        for dest, pa, pr, ca, cr in diff.changed_destinations:
            def fmt(sign: str, refs: set[Ref]) -> str:
                return _cell(", ".join(f"{sign}{_label(r)}" for r in sorted(refs)))
            rows.append([_cell(_label(dest)), fmt("+", pa) + (" " if pa and pr else "") + fmt("-", pr),
                         fmt("+", ca) + (" " if ca and cr else "") + fmt("-", cr)])
        out += _table(["Destination", "Producers", "Consumers"], rows)
    else:
        out.append("_none_")
    return "\n".join(out) + "\n"


# -- index -----------------------------------------------------------------------------


def _index_page(ctx: _Ctx, pages: list[str], title: str, summary: Mapping[str, Any] | None,
                diff: Diff | None) -> str:
    model = ctx.model
    page = "index.md"
    services = [r for r in sorted(model.entities) if r[0] == "Service"]
    modules = [r for r in sorted(model.entities) if r[0] == "Module"]
    dest_types: dict[str, int] = defaultdict(int)
    for d in model.destinations:
        dest_types[d[0]] += 1
    rel_types: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    for rel in model.relations:
        rel_types[rel.type][rel.status] += 1
    problems = sum(1 for i in ctx.infos.values() if i.problems)
    out = [f"# {_cell(title)}", "", "## Totals", ""]
    out += [f"- Services: {len(services)}", f"- Modules: {len(modules)}",
            f"- Destinations: {len(model.destinations)} "
            f"({', '.join(f'{k} {dest_types[k]}' for k in sorted(dest_types)) or 'none'})",
            f"- Destinations with problems: {problems}",
            f"- Relations: {len(model.relations)}", ""]
    if rel_types:
        statuses = sorted({s for v in rel_types.values() for s in v})
        out += _table(["Relation"] + statuses + ["total"],
                      [[_cell(t)] + [str(rel_types[t].get(s, 0)) for s in statuses] + [str(sum(rel_types[t].values()))]
                       for t in sorted(rel_types)]) + [""]
    warnings = [str(w) for w in (summary or {}).get("warnings") or []]
    out += ["## Warnings", ""] + ([f"- {_cell(w)}" for w in warnings] or ["_none_"]) + [""]
    if diff is not None:
        out += ["## What changed", "",
                f"- Entities: +{len(diff.added_entities)} / -{len(diff.removed_entities)}",
                f"- Relations: +{len(diff.added_relations)} / -{len(diff.removed_relations)}, "
                f"{len(diff.changed_relations)} changed status or confidence",
                f"- Destinations with changed producers or consumers: {len(diff.changed_destinations)}",
                "", "See [compare.md](compare.md).", ""]
    out += ["## Pages", "",
            "- [Messaging matrix](messaging.md)", "- [Coverage](coverage.md)",
            "- [Candidate review checklist](candidates.md)"]
    if diff is not None:
        out.append("- [Compare](compare.md)")
    for heading, folder in (("Services", "services"), ("Modules", "modules"), ("Destinations", "destinations")):
        group = sorted(p for p in pages if p.startswith(folder + "/"))
        if group:
            out += ["", f"### {heading}", ""]
            refs = {v: k for k, v in ctx.paths.items()}
            out += [f"- {_link(page, p, _label(refs[p]))}" for p in group]
    return "\n".join(out) + "\n"


# -- public API ------------------------------------------------------------------------


def build_wiki(
    manifest: Mapping[str, Any],
    *,
    summary: Mapping[str, Any] | None = None,
    baseline: Mapping[str, Any] | None = None,
    title: str = "Ontology wiki",
) -> dict[str, str]:
    """Render ``manifest`` as ``{relative posix path: markdown}``."""
    model = _build_model(manifest)
    infos = _analyse(model)
    paths = _assign_paths(model)
    ctx = _Ctx(model, infos, paths)
    pages: dict[str, str] = {}
    for ref, path in paths.items():
        if ref in infos:
            pages[path] = _destination_page(ctx, infos[ref], path)
        else:
            pages[path] = _entity_page(ctx, ref, path, ref[0])
    pages["messaging.md"] = _messaging_page(ctx)
    pages["coverage.md"] = _coverage_page(ctx, summary)
    pages["candidates.md"] = _candidates_page(ctx)
    diff = None
    if baseline is not None:
        diff = _diff(model, _build_model(baseline))
        pages["compare.md"] = _compare_page(diff)
    pages["index.md"] = _index_page(ctx, list(pages), title, summary, diff)
    return dict(sorted(pages.items()))


def problem_count(manifest: Mapping[str, Any]) -> int:
    return sum(1 for i in _analyse(_build_model(manifest)).values() if i.problems)


def write_wiki(pages: Mapping[str, str], out_dir: Path) -> list[Path]:
    """Write ``pages`` under ``out_dir``; stale ``.md`` files in the generated folders are removed."""
    out_dir.mkdir(parents=True, exist_ok=True)
    for folder in ("services", "modules", "destinations"):
        for stale in (out_dir / folder).glob("*.md") if (out_dir / folder).is_dir() else ():
            if f"{folder}/{stale.name}" not in pages:
                stale.unlink()
    if "compare.md" not in pages and (out_dir / "compare.md").is_file():
        (out_dir / "compare.md").unlink()
    written = []
    for rel, text in pages.items():
        path = out_dir / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8", newline="\n")
        written.append(path)
    return written
