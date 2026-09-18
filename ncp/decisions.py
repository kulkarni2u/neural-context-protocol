"""Typed decision contract: schema registry, state hashing, advisory scores.

This module is deliberately model-free. Nothing here calls a provider, embeds
text, or reads the network -- ``compile_decision_query`` is a pure function of
the conscious block plus what is already in the store, and the acceptance
tests assert exactly that.

Three things live here:

* **Schema registry** -- just enough validation to make a type error on the
  decision path a protocol bug rather than a downstream surprise. Not a
  general JSON Schema platform: enum, boolean, number, string and object
  (presence-only) are the whole v1 vocabulary.
* **State hashing** -- the stable identity of "the state this decision was
  made over", designed so the same situation hashes identically across
  processes and across time.
* **Advisory scores** -- ``joint_confidence`` and ``contradiction_mass``.
  Both are bounded heuristics. Neither is calibration, and neither may be
  presented as such.
"""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Iterable, Literal, Sequence

from ncp.types import ChunkEdge, ConsciousBlock, DecisionRecord, LEGACY_SCHEMA_ID, SubconsciousChunk

if TYPE_CHECKING:
    from ncp.stores.base import BaseStore

ChoiceType = Literal["enum", "boolean", "number", "string", "object"]

SCHEMA_FILE_NAME = "decision_schemas.json"


@dataclass(frozen=True)
class DecisionSchema:
    """One registry entry: the closed output contract for a decision slot."""

    schema_id: str
    version: int = 1
    choice_type: ChoiceType = "enum"
    options: tuple[str, ...] = ()
    required_prob_keys: tuple[str, ...] = ()

    def as_questions(self) -> list[dict[str, Any]]:
        """The `questions` payload a backend needs to answer this schema."""
        question: dict[str, Any] = {"key": "choice", "type": self.choice_type}
        if self.options:
            question["options"] = list(self.options)
        return [question]


# Shipped with the package. A host that needs more registers its own via
# [decision_schemas] in .ncp/config.toml or .ncp/decision_schemas.json.
BUILTIN_SCHEMAS: tuple[DecisionSchema, ...] = (
    DecisionSchema(
        schema_id="ncp.slot.continue_or_escalate",
        choice_type="enum",
        options=("continue", "escalate", "stop"),
    ),
    DecisionSchema(schema_id="ncp.slot.binary", choice_type="boolean"),
    DecisionSchema(
        schema_id="ncp.handoff.accept",
        choice_type="enum",
        options=("accept", "reject", "defer"),
    ),
)


def _coerce_entry(schema_id: str, raw: object) -> DecisionSchema | None:
    if not isinstance(raw, dict):
        return None
    choice_type = str(raw.get("choice_type", "enum")).lower()
    if choice_type not in {"enum", "boolean", "number", "string", "object"}:
        return None
    options = tuple(str(opt) for opt in (raw.get("options") or ()))
    if choice_type == "enum" and not options:
        return None
    try:
        version = int(raw.get("version", 1))
    except (TypeError, ValueError):
        version = 1
    return DecisionSchema(
        schema_id=schema_id,
        version=max(1, version),
        choice_type=choice_type,  # type: ignore[arg-type]
        options=options,
        required_prob_keys=tuple(str(key) for key in (raw.get("required_prob_keys") or ())),
    )


@dataclass
class SchemaRegistry:
    """Built-ins, overlaid by on-disk entries, overlaid by inline config."""

    entries: dict[str, DecisionSchema] = field(default_factory=dict)

    @classmethod
    def load(
        cls,
        *,
        inline: dict[str, Any] | None = None,
        project_root: Path | None = None,
    ) -> "SchemaRegistry":
        entries = {schema.schema_id: schema for schema in BUILTIN_SCHEMAS}
        if project_root is not None:
            path = Path(project_root) / ".ncp" / SCHEMA_FILE_NAME
            for schema_id, raw in _read_schema_file(path).items():
                entry = _coerce_entry(schema_id, raw)
                if entry is not None:
                    entries[schema_id] = entry
        for schema_id, raw in (inline or {}).items():
            entry = _coerce_entry(str(schema_id), raw)
            if entry is not None:
                entries[str(schema_id)] = entry
        return cls(entries=entries)

    def get(self, schema_id: str) -> DecisionSchema | None:
        return self.entries.get(schema_id)

    def is_registered(self, schema_id: str) -> bool:
        return schema_id in self.entries

    def validate_choice(
        self,
        schema_id: str,
        choice: object,
        *,
        probs: dict[str, float] | None = None,
    ) -> str | None:
        """Return a human-readable mismatch reason, or None when the choice fits.

        An unregistered ``schema_id`` is not a mismatch here -- forward
        compatibility is deliberate, and compile flags it as ``open_schema``
        instead. Callers that want hard rejection use
        ``[decisions].strict_registered_schemas``.
        """
        entry = self.get(schema_id)
        if entry is None:
            return None
        if entry.choice_type == "enum":
            if not isinstance(choice, str) or choice not in entry.options:
                return (
                    f"choice {choice!r} is not one of {list(entry.options)} "
                    f"for schema {schema_id}"
                )
        elif entry.choice_type == "boolean":
            if not isinstance(choice, bool):
                return f"choice {choice!r} must be a boolean for schema {schema_id}"
        elif entry.choice_type == "number":
            if isinstance(choice, bool) or not isinstance(choice, (int, float)):
                return f"choice {choice!r} must be a number for schema {schema_id}"
        elif entry.choice_type == "string":
            if not isinstance(choice, str):
                return f"choice {choice!r} must be a string for schema {schema_id}"
        elif entry.choice_type == "object":
            if not isinstance(choice, dict):
                return f"choice {choice!r} must be an object for schema {schema_id}"

        supplied = set(probs or {})
        if entry.options and supplied:
            unknown = sorted(supplied - set(entry.options))
            if unknown:
                return f"probs contains keys not in schema {schema_id} options: {unknown}"
        missing = sorted(set(entry.required_prob_keys) - supplied)
        if missing:
            return f"probs is missing required keys for schema {schema_id}: {missing}"
        return None


def _read_schema_file(path: Path) -> dict[str, Any]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(raw, dict):
        return {}
    # Accept both {"schemas": {...}} and a bare {schema_id: entry} mapping.
    inner = raw.get("schemas", raw)
    return dict(inner) if isinstance(inner, dict) else {}


# ── state hashing ─────────────────────────────────────────────────────────────

def canonical_state_identity(
    *,
    schema_id: str,
    slot: str,
    conscious: ConsciousBlock,
    chunk_ids: Sequence[str],
) -> dict[str, Any]:
    """The subset of compiled state that defines "the same situation".

    Deliberately narrow. Everything in NCP that moves continuously is left
    out, because a hash that includes it can never match twice:

    * retrieval scores and ``relevance`` -- recomputed per query from a 4h
      recency half-life, so they differ between two calls seconds apart
    * ``base_trust`` / ``result_confidence`` -- moved by decay, outcome
      feedback and dissent
    * ``age_seconds`` / timestamps -- monotonic by construction
    * chunk ``content`` -- truncated for display, and rewritten by
      consolidation
    * evidence *ordering* -- a ranking artifact, not part of the situation
    * ``drift_score`` / ``slot_confidence`` / context ratios -- continuous
      turn-local telemetry

    Also excluded on purpose: ``agent_id`` and ``pipeline_id``. Two agents
    hitting the same slot with the same evidence are in the same state, and
    scoping is applied at query time instead. List-valued fields are sorted so
    that "tried A then B" and "tried B then A" are one situation.
    """
    return {
        "schema_id": schema_id,
        "slot": slot,
        "task": conscious.task,
        "intent": conscious.intent,
        "owns": sorted(conscious.owns),
        "must_not": sorted(conscious.must_not),
        "tried": sorted(conscious.tried),
        "failed": sorted(conscious.failed),
        "chunk_ids": sorted(chunk_ids),
    }


def compute_state_hash(
    *,
    schema_id: str,
    slot: str,
    conscious: ConsciousBlock,
    chunk_ids: Sequence[str],
) -> str:
    """sha256 hex of the canonical state identity (sorted keys, no whitespace)."""
    identity = canonical_state_identity(
        schema_id=schema_id, slot=slot, conscious=conscious, chunk_ids=chunk_ids
    )
    payload = json.dumps(identity, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


# ── advisory scores ───────────────────────────────────────────────────────────

def evidence_confidence(chunk: SubconsciousChunk) -> float:
    """Per-chunk confidence: result_confidence when set, else base_trust."""
    value = chunk.result_confidence if chunk.result_confidence is not None else chunk.base_trust
    return max(0.0, min(1.0, float(value)))


def contradiction_mass(
    *,
    chunk_ids: Sequence[str],
    contradiction_pairs: Iterable[tuple[str, str]],
) -> float:
    """Fraction of injected evidence that is party to a contradiction.

    Measured over *chunks*, not pairs. The pair-denominator form (contradicting
    pairs / C(k,2)) was considered and rejected: with the default k=6 it has 15
    pairs in the denominator, so a genuine head-on contradiction between two
    retrieved chunks scores 0.067 and nothing short of a fully incoherent store
    can reach a 0.30 threshold. Chunk mass makes one contradiction among six
    chunks read as 0.33, which is what it actually means.

    ``contradiction_pairs`` fuses both signals NCP already has: ``contradicts``
    edges from the graph, and the assembler's fan-in reduce contradictions
    (same-topic claims that survived merge but diverge). Edges alone would be
    near-permanently zero -- write-time edge inference only ever emits
    ``refines``, so ``contradicts`` exists only where a host wrote one by hand.
    """
    injected = set(chunk_ids)
    if not injected:
        return 0.0
    implicated: set[str] = set()
    for left, right in contradiction_pairs:
        if left in injected and right in injected and left != right:
            implicated.add(left)
            implicated.add(right)
    return max(0.0, min(1.0, len(implicated) / len(injected)))


def joint_confidence(
    *,
    confidences: Sequence[float],
    contradiction_mass: float,
    drift_score: float,
) -> float:
    """Advisory joint confidence over injected evidence. NOT calibration.

    ``geometric_mean(conf_i) * (1 - 0.5 * contradiction_mass) * (1 - drift)``,
    clamped to [0, 1]. Zero evidence is 0.0 by definition -- a decision with
    nothing behind it is not a confident one.

    Known property, documented rather than hidden: the geometric mean is
    dragged down hard by a single weak chunk, so asking for more evidence can
    *lower* this number. That is intended as an evidence-quality signal, but it
    means the escalate threshold must be tuned against a measured escalate rate
    (`ncp dogfood --loop decision`) and not read as a probability.
    """
    usable = [max(0.0, min(1.0, float(c))) for c in confidences]
    if not usable:
        return 0.0
    if any(c <= 0.0 for c in usable):
        geo = 0.0
    else:
        geo = math.exp(sum(math.log(c) for c in usable) / len(usable))
    cm = max(0.0, min(1.0, float(contradiction_mass)))
    drift = max(0.0, min(1.0, float(drift_score)))
    return max(0.0, min(1.0, geo * (1.0 - 0.5 * cm) * (1.0 - drift)))


def persist_decision_record(
    decision: DecisionRecord,
    *,
    store: "BaseStore",
    registry: SchemaRegistry,
    enabled: bool = True,
    strict_schemas: bool = False,
    dual_write_chunks: bool = False,
    evidence_chunk_id: str | None = None,
) -> bool:
    """Shared Python/MCP validation and persistence; invalid contracts never write."""
    if not enabled:
        return False
    if strict_schemas and not registry.is_registered(decision.schema_id) and decision.schema_id != LEGACY_SCHEMA_ID:
        raise ValueError(f"schema_id {decision.schema_id!r} is not registered and strict mode is on")
    mismatch = registry.validate_choice(decision.schema_id, decision.choice, probs=decision.probs)
    if mismatch is not None:
        raise ValueError(mismatch)
    entry = registry.get(decision.schema_id)
    if entry is not None and decision.schema_version != entry.version:
        raise ValueError(f"schema_version must be {entry.version} for schema {decision.schema_id}")
    recorded = store.record_decision_record(decision)
    if not recorded or not dual_write_chunks:
        return recorded
    # Opt-in mirror so legacy retrieval still surfaces the decision. Off by
    # default: this writes into the same pool ncp_get_context retrieves
    # from, so enabling it changes ranking and token budgets.
    mirror = SubconsciousChunk(
        layer="reasoning_trace",
        content=decision.canonical_json()[:2000],
        src="tool_result",
        chunk_type="json",
        written_by=decision.agent_id or "system",
        pipeline_id=decision.pipeline_id,
        caused_by=evidence_chunk_id,
        base_trust=decision.confidence,
        result_confidence=decision.confidence,
        source_refs=list(decision.chunk_ids),
    )
    if store.write(mirror) and decision.chunk_ids:
        # Graph: the decision chunk is derived from each evidence chunk.
        # add_chunk_edges only joins chunks in the same pipeline scope, so
        # a decision recorded without a pipeline_id legitimately links to
        # nothing rather than reaching across a scope boundary.
        edges = [
            ChunkEdge(
                src_chunk_id=mirror.chunk_id,
                dst_chunk_id=evidence_id,
                edge_type="derived_from",
                created_by="ncp:decision",
            )
            for evidence_id in decision.chunk_ids
            if evidence_id != mirror.chunk_id
        ]
        if edges:
            try:
                store.add_chunk_edges(edges)
            except Exception:  # noqa: BLE001 - edges are additive, never fatal
                pass
    return recorded
