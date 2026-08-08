"""CAP-C8: evidence-backed procedural self-refinement.

NCP's calibration loop (``ncp calibrate --feedback``) reweights trust on
*stored memory* -- it never touches the instructions an agent operates
under. This module closes that gap for a narrow, deliberately bounded case:
a single named "procedure" (a chunk-sized block of operating instructions,
e.g. one rule out of a skill file or turn contract) can accumulate evidence
from ``ncp_record_outcome`` and be evolved through an explicit,
human-gated propose -> apply pipeline, with rollback.

Design choices, and why:

- **A procedure is one chunk, not a whole contract file.** Chunk content is
  capped at 2000 chars protocol-wide; a multi-KB CLAUDE.md/SKILL.md would
  never fit. Scoping refinement to a single focused instruction block also
  keeps every version a small, evidence-cited diff instead of an opaque
  full-document rewrite -- closer to what "small, focused updates" should
  mean than a giant regenerated file would be.
- **Deterministic by default, additive-only.** ``build_refinement_proposal``
  never edits or removes existing instruction text; it only appends
  deduplicated, frequency-ranked notes drawn from *failed* outcomes. No
  model call. This mirrors the same conservative posture as the ingestion
  noise filter (``ncp/chunker.py``): remove/add obvious signal, leave the
  rest alone.
- **Proposing never adopts.** ``propose_refinement`` writes a new,
  low-trust candidate chunk linked to its predecessor via ``supersedes`` --
  it does not call ``store.supersede()``, so the candidate is not yet the
  live procedure. Only ``apply_refinement`` promotes it (marks the old
  chunk retired via the existing CAP-C5 ``supersede`` machinery and lifts
  trust via the existing ``calibrate`` manual-override mode). This is the
  human-in-the-loop gate Prime Agent's ``/refine`` has no equivalent of
  attaching to.
- **Rollback never deletes.** Reverting writes a *new* generation whose
  content matches the prior version, keeping the chain monotonically
  increasing -- the same bi-temporal honesty NCP already applies to memory
  chunks (nothing is ever silently rewritten in place).
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
from pathlib import Path
from typing import Sequence

from ncp.stores.base import BaseStore
from ncp.types import ChunkEdge, ChunkSource, OutcomeRecord, SubconsciousChunk

PROCEDURE_LAYER = "procedural"
MAX_CONTENT_CHARS = 2000
PROPOSAL_TRUST = 0.4  # below default synthesis trust (0.70): an un-adopted machine proposal
REFINE_AUTHOR = "ncp:refine"
ROLLBACK_AUTHOR = "ncp:refine:rollback"

_SRC_DEFAULT_TRUST: dict[str, float] = {
    "user_verified": 0.95,
    "tool_result": 0.80,
    "synthesis": 0.70,
    "agent_inferred": 0.60,
    "subcon_retrieved": 0.55,
}


def _procedure_root_id(*, pipeline_id: str | None, key: str) -> str:
    """Deterministic chunk_id for generation 0 of a procedure.

    Deterministic on ``(pipeline_id, key)`` so the head of a procedure's
    version chain can always be located by recomputing this id and walking
    ``superseded_by`` forward -- no search/index required.
    """
    basis = f"{pipeline_id or ''}|{key}"
    return f"proc_{hashlib.sha256(basis.encode()).hexdigest()[:16]}"


def _normalize_note(note: str) -> str:
    return " ".join(note.split()).strip().lower()


@dataclass(frozen=True)
class RefinementProposal:
    """A deterministic, evidence-backed candidate for a procedure's next version."""

    proposed_content: str
    rationale: list[str]
    based_on_outcome_ids: list[str]
    failure_count: int
    success_count: int


def build_refinement_proposal(
    current_content: str,
    outcomes: Sequence[OutcomeRecord],
    *,
    min_failed_outcomes: int = 3,
    max_bullets: int = 5,
) -> RefinementProposal | None:
    """Fold recurring failure notes into an additive proposal, or return None.

    Pure and deterministic: no model call, no mutation of ``current_content``
    beyond appending a ranked bullet list. Failures are deduplicated by
    whitespace-normalized, case-insensitive note text and ranked by how many
    distinct outcomes reported the same issue (most-repeated first). Returns
    ``None`` when there isn't yet ``min_failed_outcomes`` worth of evidence,
    or when the addition can't fit within the chunk content bound even after
    dropping the lowest-ranked bullets.
    """
    failures = [o for o in outcomes if not o.success and o.note and o.note.strip()]
    successes = [o for o in outcomes if o.success]
    if len(failures) < min_failed_outcomes:
        return None

    grouped: dict[str, list[str]] = {}
    first_text: dict[str, str] = {}
    order: list[str] = []
    for outcome in failures:
        key = _normalize_note(outcome.note or "")
        if not key:
            continue
        if key not in grouped:
            grouped[key] = []
            first_text[key] = outcome.note.strip()
            order.append(key)
        grouped[key].append(outcome.outcome_id)

    ranked = sorted(order, key=lambda k: len(grouped[k]), reverse=True)[:max_bullets]
    if not ranked:
        return None

    items: list[tuple[str, list[str]]] = [
        (f"- {first_text[key]} (observed {len(grouped[key])}x)", grouped[key]) for key in ranked
    ]

    def render(candidate_items: list[tuple[str, list[str]]]) -> str:
        bullets = [text for text, _ in candidate_items]
        addition = "\n\n## Refinements (evidence-based, proposed)\n" + "\n".join(bullets)
        return (current_content.rstrip() + addition).strip()

    proposed = render(items)
    while items and len(proposed) > MAX_CONTENT_CHARS:
        items.pop()
        proposed = render(items)
    if not items:
        return None

    return RefinementProposal(
        proposed_content=proposed,
        rationale=[text for text, _ in items],
        based_on_outcome_ids=[oid for _, ids in items for oid in ids],
        failure_count=len(failures),
        success_count=len(successes),
    )


def ingest_procedure(
    store: BaseStore,
    *,
    key: str,
    content: str,
    pipeline_id: str | None = None,
    written_by: str = "operator",
    src: ChunkSource = "user_verified",
) -> SubconsciousChunk:
    """Store the initial (generation 0) version of a refinable procedure.

    Raises if ``key`` is already ingested for this ``pipeline_id`` -- use
    ``propose_refinement``/``apply_refinement`` to evolve it instead.
    """
    content = content.strip()
    if not content:
        raise ValueError("content must not be empty")
    if len(content) > MAX_CONTENT_CHARS:
        raise ValueError(
            f"content is {len(content)} chars; a refinable procedure must fit in "
            f"{MAX_CONTENT_CHARS} chars (it is one chunk, not a whole contract file)"
        )

    root_id = _procedure_root_id(pipeline_id=pipeline_id, key=key)
    if _procedure_rows(store, key=key, pipeline_id=pipeline_id):
        raise ValueError(f"procedure '{key}' is already ingested (chunk_id={root_id})")

    chunk = SubconsciousChunk(
        chunk_id=root_id,
        layer=PROCEDURE_LAYER,
        content=content,
        src=src,
        written_by=written_by,
        pipeline_id=pipeline_id,
        source_refs=[key],
        generation=0,
        base_trust=_SRC_DEFAULT_TRUST.get(src, 0.70),
    )
    if not store.write(chunk):
        raise ValueError(
            f"could not ingest procedure '{key}': content is too similar to existing memory "
            "in this pipeline (dedup guard)"
        )
    return chunk


def _procedure_rows(
    store: BaseStore, *, key: str, pipeline_id: str | None, limit: int = 200
) -> list[dict]:
    """All non-tombstoned rows for this procedure's key, oldest generation first.

    Reads through ``list_chunks`` rather than ``get_chunks_by_ids``:
    ``get_chunks_by_ids`` applies the same "current, non-superseded"
    bitemporal filter as retrieval (see ``BaseStore.query``), so once a
    version has been superseded it silently disappears from that view --
    exactly the versions a chain walk needs to see. ``list_chunks`` applies
    no such filtering.

    Bounded by ``list_chunks``'s own page size (SQLite clamps ``limit`` to
    200 server-side): a single pipeline with more than 200 live procedural
    chunks across *all* keys could push an old version out of view. Fine for
    the current deliberately-narrow scope (a handful of named procedures per
    pipeline); a dedicated indexed lookup would be the fix if that stops
    holding.
    """
    page = store.list_chunks(layer=PROCEDURE_LAYER, pipeline_id=pipeline_id, limit=limit)
    rows = [
        row
        for row in page.get("chunks", [])
        if not row.get("tombstoned")
        and row.get("pipeline_id") == pipeline_id
        and key in (row.get("source_refs") or [])
    ]
    rows.sort(key=lambda r: int(r["generation"]))
    return rows


def _row_to_chunk(row: dict) -> SubconsciousChunk:
    return SubconsciousChunk(
        chunk_id=str(row["chunk_id"]),
        layer=row["layer"],
        content=str(row["content"]),
        src=row["src"],
        written_by=str(row["written_by"]),
        pipeline_id=row["pipeline_id"],
        scope=row["scope"],
        zone=row["zone"],
        chunk_type=row["chunk_type"],
        caused_by=row["caused_by"],
        supersedes=row["supersedes"],
        superseded_by=row["superseded_by"],
        base_trust=float(row["base_trust"]),
        generation=int(row["generation"]),
        source_refs=list(row.get("source_refs") or []),
    )


def find_procedure_head(
    store: BaseStore, *, key: str, pipeline_id: str | None = None
) -> SubconsciousChunk | None:
    """Return the current (adopted, live) version of a procedure, or None if not ingested.

    An un-adopted proposal (written by ``propose_refinement`` but never
    ``apply_refinement``-ed) also has ``superseded_by is None`` -- nothing
    points at it yet -- so "live" can't just mean "nothing supersedes this
    row." The adopted chain is exactly what's reachable by following
    ``superseded_by`` forward from generation 0 (the chunk ``ingest_procedure``
    always writes first); a candidate that hasn't been adopted is never
    linked into that walk, so it correctly doesn't count as head.
    """
    rows = _procedure_rows(store, key=key, pipeline_id=pipeline_id)
    if not rows:
        return None
    by_id = {r["chunk_id"]: r for r in rows}
    current = rows[0]  # lowest generation == the chunk ingest_procedure wrote
    seen = {current["chunk_id"]}
    while True:
        next_id = current.get("superseded_by")
        if not next_id or next_id not in by_id or next_id in seen:
            break
        seen.add(next_id)
        current = by_id[next_id]
    return _row_to_chunk(current)


def procedure_chain(
    store: BaseStore, *, key: str, pipeline_id: str | None = None
) -> list[SubconsciousChunk]:
    """Return every version of a procedure, oldest first."""
    return [_row_to_chunk(row) for row in _procedure_rows(store, key=key, pipeline_id=pipeline_id)]


@dataclass(frozen=True)
class RefinementResult:
    key: str
    head_chunk_id: str
    proposal: RefinementProposal | None
    new_chunk: SubconsciousChunk | None
    dry_run: bool


def propose_refinement(
    store: BaseStore,
    *,
    key: str,
    pipeline_id: str | None = None,
    min_failed_outcomes: int = 3,
    max_bullets: int = 5,
    dry_run: bool = False,
) -> RefinementResult:
    """Gather outcome evidence against the current head and propose a next version.

    Evidence comes from ``ncp_record_outcome`` calls whose ``chunk_ids``
    include this procedure's chunk id (any generation in its chain, so
    evidence recorded before a still-unreviewed proposal keeps counting).
    Writing the candidate does not adopt it -- see ``apply_refinement``.
    """
    head = find_procedure_head(store, key=key, pipeline_id=pipeline_id)
    if head is None:
        raise ValueError(f"no procedure ingested for key '{key}'; run `ncp refine ingest` first")

    chain_ids = [c.chunk_id for c in procedure_chain(store, key=key, pipeline_id=pipeline_id)]
    outcomes = store.list_outcomes(chunk_ids=chain_ids)
    proposal = build_refinement_proposal(
        head.content,
        outcomes,
        min_failed_outcomes=min_failed_outcomes,
        max_bullets=max_bullets,
    )
    if proposal is None or dry_run:
        return RefinementResult(
            key=key, head_chunk_id=head.chunk_id, proposal=proposal, new_chunk=None, dry_run=dry_run
        )

    new_chunk = SubconsciousChunk(
        layer=PROCEDURE_LAYER,
        content=proposal.proposed_content,
        src="synthesis",
        written_by=REFINE_AUTHOR,
        pipeline_id=pipeline_id,
        source_refs=[key],
        supersedes=head.chunk_id,
        generation=head.generation + 1,
        base_trust=PROPOSAL_TRUST,
    )
    if not store.write(new_chunk):
        raise ValueError(
            f"could not write proposal for '{key}': content is too similar to existing memory "
            "in this pipeline (dedup guard) -- the rationale may need more distinguishing detail"
        )
    store.add_chunk_edges(
        [ChunkEdge(src_chunk_id=new_chunk.chunk_id, dst_chunk_id=head.chunk_id, edge_type="supersedes", created_by=REFINE_AUTHOR)]
    )
    return RefinementResult(
        key=key, head_chunk_id=head.chunk_id, proposal=proposal, new_chunk=new_chunk, dry_run=False
    )


def apply_refinement(
    store: BaseStore,
    *,
    chunk_id: str,
    promote_trust: float = 0.80,
    write_to: Path | None = None,
) -> SubconsciousChunk:
    """Adopt a proposed candidate: promote it to head and lift its trust.

    Retires the candidate's predecessor via the existing CAP-C5
    ``store.supersede`` machinery (never deletes it) and promotes trust via
    the existing ``store.calibrate`` manual-override mode -- both reused as-is
    rather than duplicated here.
    """
    found = store.get_chunks_by_ids([chunk_id])
    if not found:
        raise ValueError(f"no chunk found for id '{chunk_id}'")
    candidate = found[0]
    if candidate.layer != PROCEDURE_LAYER:
        raise ValueError(f"chunk '{chunk_id}' is not a procedural chunk")
    if not candidate.supersedes:
        raise ValueError(f"chunk '{chunk_id}' is not a refinement proposal (no predecessor)")

    if not store.supersede(candidate.supersedes, candidate.chunk_id):
        raise ValueError(
            f"could not adopt '{chunk_id}': predecessor '{candidate.supersedes}' not found "
            "or in a different pipeline"
        )
    store.calibrate(chunk_id=candidate.chunk_id, trust=promote_trust)

    if write_to is not None:
        write_to.write_text(candidate.content + "\n")

    return candidate.model_copy(update={"base_trust": promote_trust})


def rollback_refinement(
    store: BaseStore,
    *,
    key: str,
    pipeline_id: str | None = None,
    write_to: Path | None = None,
) -> SubconsciousChunk:
    """Revert a procedure to its previous version by writing a new generation with the old content.

    Never deletes or rewrites history: the rollback is itself a new,
    ``user_verified`` version whose content matches the prior one.
    """
    head = find_procedure_head(store, key=key, pipeline_id=pipeline_id)
    if head is None:
        raise ValueError(f"no procedure ingested for key '{key}'")
    if not head.supersedes:
        raise ValueError(f"procedure '{key}' has no prior version to roll back to")

    parent_row = next(
        (r for r in _procedure_rows(store, key=key, pipeline_id=pipeline_id) if r["chunk_id"] == head.supersedes),
        None,
    )
    if parent_row is None:
        raise ValueError(f"prior version '{head.supersedes}' is no longer available")
    parent = _row_to_chunk(parent_row)

    reverted = SubconsciousChunk(
        layer=PROCEDURE_LAYER,
        content=parent.content,
        src="user_verified",
        written_by=ROLLBACK_AUTHOR,
        pipeline_id=pipeline_id,
        source_refs=[key],
        supersedes=head.chunk_id,
        generation=head.generation + 1,
        base_trust=parent.base_trust,
    )
    # allow_duplicate: a rollback's content is *supposed* to exactly match an
    # existing (older) row -- that's the whole point of restoring it. The
    # dedup guard exists to suppress noise, not to block an intentional,
    # byte-faithful restore of known-good prior content.
    if not store.write(reverted, allow_duplicate=True):
        raise ValueError(f"could not write rollback candidate for '{key}'")
    store.add_chunk_edges(
        [ChunkEdge(src_chunk_id=reverted.chunk_id, dst_chunk_id=head.chunk_id, edge_type="supersedes", created_by=ROLLBACK_AUTHOR)]
    )
    if not store.supersede(head.chunk_id, reverted.chunk_id):
        raise ValueError(f"could not roll back: head '{head.chunk_id}' not found")

    if write_to is not None:
        write_to.write_text(reverted.content + "\n")

    return reverted
