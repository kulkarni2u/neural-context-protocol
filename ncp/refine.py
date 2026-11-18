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
import time
from typing import Any, Sequence

from ncp.stores.base import BaseStore
from ncp.stores.improvement_common import make_problem_id, new_attempt_id
from ncp.types import (
    ChunkEdge,
    ChunkSource,
    ImprovementAttempt,
    ImprovementProblem,
    OutcomeRecord,
    OutcomeStats,
    SubconsciousChunk,
)

PROCEDURE_LAYER = "procedural"
MAX_CONTENT_CHARS = 2000
PROPOSAL_TRUST = 0.4  # below default synthesis trust (0.70): an un-adopted machine proposal
REFINE_AUTHOR = "ncp:refine"
ROLLBACK_AUTHOR = "ncp:refine:rollback"
GATE_SPLITS = ("auto", "holdout", "all")
HOLDOUT_SPLIT = "holdout"

_SRC_DEFAULT_TRUST: dict[str, float] = {
    "user_verified": 0.95,
    "tool_result": 0.80,
    "synthesis": 0.70,
    "agent_inferred": 0.60,
    "subcon_retrieved": 0.55,
}


class RefineError(ValueError):
    """A refinement request that cannot proceed (subclasses ValueError for CLI compatibility)."""


class RefineGateError(RefineError):
    """A prune candidate failed the apply gate; ``details`` explains why."""

    def __init__(self, message: str, details: dict[str, Any]) -> None:
        super().__init__(message)
        self.details = details


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
    problem_id: str | None = None,
    problem_name: str | None = None,
    rationale: str | None = None,
    predicted: Sequence[str] | None = None,
    model: str | None = None,
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

    new_chunk = _write_candidate(
        store,
        head=head,
        key=key,
        pipeline_id=pipeline_id,
        content=proposal.proposed_content,
        dup_hint="the rationale may need more distinguishing detail",
    )
    summary = "; ".join(proposal.rationale)
    _record_attempt(
        store,
        kind="repair",
        key=key,
        pipeline_id=pipeline_id,
        problem_id=problem_id,
        problem_name=problem_name,
        default_name=f"recurring failures in {key}",
        what=f"repair: {summary}",
        rationale=rationale,
        predicted=predicted,
        model=model,
        candidate_chunk_id=new_chunk.chunk_id,
    )
    return RefinementResult(
        key=key, head_chunk_id=head.chunk_id, proposal=proposal, new_chunk=new_chunk, dry_run=False
    )


def _write_candidate(
    store: BaseStore,
    *,
    head: SubconsciousChunk,
    key: str,
    pipeline_id: str | None,
    content: str,
    dup_hint: str,
) -> SubconsciousChunk:
    new_chunk = SubconsciousChunk(
        layer=PROCEDURE_LAYER,
        content=content,
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
            f"in this pipeline (dedup guard) -- {dup_hint}"
        )
    store.add_chunk_edges(
        [ChunkEdge(src_chunk_id=new_chunk.chunk_id, dst_chunk_id=head.chunk_id, edge_type="supersedes", created_by=REFINE_AUTHOR)]
    )
    return new_chunk


def _resolve_problem(
    store: BaseStore,
    *,
    key: str,
    pipeline_id: str | None,
    problem_id: str | None,
    problem_name: str | None,
    default_name: str,
) -> ImprovementProblem:
    if problem_id:
        found = store.get_improvement_problem(problem_id)
        if found is None:
            raise RefineError(f"unknown improvement problem '{problem_id}'")
        return found
    name = (problem_name or "").strip() or default_name
    return store.upsert_improvement_problem(
        ImprovementProblem(
            problem_id=make_problem_id(pipeline_id=pipeline_id, scope=key, name=name),
            pipeline_id=pipeline_id,
            scope=key,
            name=name[:300],
            opened_at=time.time(),
        )
    )


def _record_attempt(
    store: BaseStore,
    *,
    kind: str,
    key: str,
    pipeline_id: str | None,
    problem_id: str | None,
    problem_name: str | None,
    default_name: str,
    what: str,
    rationale: str | None,
    predicted: Sequence[str] | None,
    model: str | None,
    candidate_chunk_id: str,
    anchor_rate: float | None = None,
    anchor_output_tokens: float | None = None,
) -> ImprovementAttempt | None:
    """File an attempt under its problem; improvement storage may be unsupported."""
    try:
        problem = _resolve_problem(
            store,
            key=key,
            pipeline_id=pipeline_id,
            problem_id=problem_id,
            problem_name=problem_name,
            default_name=default_name,
        )
        return store.record_improvement_attempt(
            ImprovementAttempt(
                attempt_id=new_attempt_id(),
                problem_id=problem.problem_id,
                kind=kind,  # type: ignore[arg-type]
                what=what[:500],
                rationale=(rationale[:1000] if rationale else None),
                candidate_chunk_id=candidate_chunk_id,
                predicted=list(predicted or []),
                anchor_rate=anchor_rate,
                anchor_output_tokens=anchor_output_tokens,
                model=model,
                created_at=time.time(),
            )
        )
    except NotImplementedError:
        return None


def _find_attempt(store: BaseStore, chunk_id: str) -> ImprovementAttempt | None:
    try:
        attempts = store.list_improvement_attempts(candidate_chunk_id=chunk_id)
    except NotImplementedError:
        return None
    # Only refine attempts gate a candidate: a manual log entry citing the
    # candidate must never displace the prune/repair attempt (gate bypass).
    refine = [a for a in attempts if a.kind in ("repair", "prune")]
    return refine[-1] if refine else None


def _safe_stats(
    store: BaseStore,
    chunk_id: str,
    model: str | None,
    exclude_chunk_ids: Sequence[str] = (),
    split: str | None = None,
) -> OutcomeStats | None:
    try:
        return store.outcome_stats(
            chunk_ids=[chunk_id], model=model, exclude_chunk_ids=exclude_chunk_ids, split=split
        )
    except NotImplementedError:
        return None


def _check_gate_split(gate_split: str) -> None:
    if gate_split not in GATE_SPLITS:
        raise RefineError(f"gate_split must be one of {', '.join(GATE_SPLITS)}; got {gate_split!r}")


@dataclass(frozen=True)
class _SplitChoice:
    """Which outcome split a trial is scored on, plus the stats on that split."""

    split_used: str  # "holdout" or "all"
    holdout_ok: bool
    stats: OutcomeStats | None
    anchor: OutcomeStats | None
    holdout_trial_weight: float
    holdout_anchor_weight: float


def _choose_split(
    gate_split: str,
    *,
    trial_holdout: OutcomeStats | None,
    anchor_holdout: OutcomeStats | None,
    trial_all: OutcomeStats | None,
    anchor_all: OutcomeStats | None,
    min_trial_outcomes: int,
    min_anchor_outcomes: int,
) -> _SplitChoice:
    """Pick holdout when both sides have enough holdout weight, else all outcomes.

    ``gate_split="holdout"`` always reports holdout (even without evidence) so
    callers can refuse; ``"all"`` never uses the split filter.
    """
    trial_w = trial_holdout.total_weight if trial_holdout else 0.0
    anchor_w = anchor_holdout.total_weight if anchor_holdout else 0.0
    ok = trial_w >= min_trial_outcomes and anchor_w >= min_anchor_outcomes
    if gate_split == "all" or (gate_split == "auto" and not ok):
        return _SplitChoice("all", ok, trial_all, anchor_all, trial_w, anchor_w)
    return _SplitChoice(HOLDOUT_SPLIT, ok, trial_holdout, anchor_holdout, trial_w, anchor_w)


def propose_prune(
    store: BaseStore,
    *,
    key: str,
    content: str,
    pipeline_id: str | None = None,
    problem_id: str | None = None,
    problem_name: str | None = None,
    rationale: str | None = None,
    predicted: Sequence[str] | None = None,
    model: str | None = None,
    min_anchor_outcomes: int = 5,
    dry_run: bool = False,
) -> RefinementResult:
    """Propose a shorter version of the head, only once the head has a working anchor."""
    head = find_procedure_head(store, key=key, pipeline_id=pipeline_id)
    if head is None:
        raise ValueError(f"no procedure ingested for key '{key}'; run `ncp refine ingest` first")
    content = content.strip()
    if not content:
        raise RefineError("prune content must not be empty")
    if len(content) > MAX_CONTENT_CHARS:
        raise RefineError(f"prune content is {len(content)} chars; max {MAX_CONTENT_CHARS}")
    try:
        anchor = store.outcome_stats(chunk_ids=[head.chunk_id], model=model)
    except NotImplementedError as exc:
        raise RefineError(f"cannot prune: {exc}") from exc
    if anchor.total_weight < min_anchor_outcomes:
        raise RefineError(
            f"no anchor: head has {anchor.n} outcomes; collect outcomes or run a repair first"
        )
    if len(content) >= len(head.content):
        raise RefineError(
            f"prune content ({len(content)} chars) must be strictly shorter than the head "
            f"({len(head.content)} chars)"
        )
    if dry_run:
        return RefinementResult(
            key=key, head_chunk_id=head.chunk_id, proposal=None, new_chunk=None, dry_run=True
        )
    new_chunk = _write_candidate(
        store,
        head=head,
        key=key,
        pipeline_id=pipeline_id,
        content=content,
        dup_hint="the pruned content is too similar to existing memory",
    )
    _record_attempt(
        store,
        kind="prune",
        key=key,
        pipeline_id=pipeline_id,
        problem_id=problem_id,
        problem_name=problem_name,
        default_name=f"token cost of {key}",
        what=f"prune: {len(head.content)} -> {len(content)} chars",
        rationale=rationale,
        predicted=predicted,
        model=model,
        candidate_chunk_id=new_chunk.chunk_id,
        anchor_rate=anchor.success_rate,
        anchor_output_tokens=anchor.mean_output_tokens,
    )
    return RefinementResult(
        key=key, head_chunk_id=head.chunk_id, proposal=None, new_chunk=new_chunk, dry_run=False
    )


def _observed_fields(attempt: ImprovementAttempt, stats: OutcomeStats | None) -> dict[str, Any]:
    if stats is None:
        return {}
    succeeded = set(stats.tasks_succeeded)
    fixed = [t for t in attempt.predicted if t in succeeded]
    return {
        "passed": stats.success_weight,
        "total": stats.total_weight,
        "mean_output_tokens": stats.mean_output_tokens,
        "fixed": fixed,
        "missed": [t for t in attempt.predicted if t not in fixed],
    }


def _prune_gate_details(
    attempt: ImprovementAttempt,
    stats: OutcomeStats | None,
    *,
    gate_ratio: float,
    min_trial_outcomes: int,
    split_used: str = "all",
) -> dict[str, Any]:
    """Evaluate the prune gate; ``details["reasons"]`` is present only when it fails."""
    details: dict[str, Any] = {
        "split_used": split_used,
        "anchor_rate": attempt.anchor_rate,
        "candidate_rate": stats.success_rate if stats else None,
        "n": stats.n if stats else 0,
        "total_weight": stats.total_weight if stats else 0.0,
        "anchor_output_tokens": attempt.anchor_output_tokens,
        "candidate_output_tokens": stats.mean_output_tokens if stats else None,
        "required": {
            "min_trial_outcomes": min_trial_outcomes,
            "gate_ratio": gate_ratio,
            "min_rate": (
                gate_ratio * attempt.anchor_rate if attempt.anchor_rate is not None else None
            ),
        },
    }
    reasons: list[str] = []
    if stats is None or stats.total_weight < min_trial_outcomes:
        reasons.append(
            f"trial outcomes {details['total_weight']} < required {min_trial_outcomes}"
        )
    else:
        cand_rate = stats.success_rate
        if attempt.anchor_rate is not None and (
            cand_rate is None or cand_rate < gate_ratio * attempt.anchor_rate
        ):
            reasons.append(
                f"candidate rate {cand_rate} < {gate_ratio} x anchor rate {attempt.anchor_rate}"
            )
        if (
            attempt.anchor_output_tokens is not None
            and stats.mean_output_tokens is not None
            and stats.mean_output_tokens >= attempt.anchor_output_tokens
        ):
            reasons.append(
                f"candidate mean output tokens {stats.mean_output_tokens} >= anchor "
                f"{attempt.anchor_output_tokens}"
            )
    if reasons:
        details["reasons"] = reasons
    return details


def _check_prune_gate(
    attempt: ImprovementAttempt,
    stats: OutcomeStats | None,
    *,
    gate_ratio: float,
    min_trial_outcomes: int,
    split_used: str = "all",
) -> None:
    details = _prune_gate_details(
        attempt,
        stats,
        gate_ratio=gate_ratio,
        min_trial_outcomes=min_trial_outcomes,
        split_used=split_used,
    )
    if details.get("reasons"):
        raise RefineGateError("prune gate failed: " + "; ".join(details["reasons"]), details)


def _with_split_note(existing: str | None, split_used: str) -> str:
    tag = f"gate split: {split_used}"
    return (f"{existing}; {tag}" if existing else tag)[:500]


def apply_refinement(
    store: BaseStore,
    *,
    chunk_id: str,
    promote_trust: float = 0.80,
    write_to: Path | None = None,
    force: bool = False,
    gate_ratio: float = 0.9,
    min_trial_outcomes: int = 5,
    min_anchor_outcomes: int = 5,
    gate_split: str = "auto",
) -> SubconsciousChunk:
    """Adopt a proposed candidate: promote it to head and lift its trust.

    Refuses a stale candidate (its predecessor is no longer the head), even
    with ``force``. Prune candidates must pass the accuracy/token gate unless
    ``force`` is set. ``gate_split`` picks the outcomes the gate scores:
    ``auto`` uses ``split="holdout"`` outcomes when the candidate has
    ``min_trial_outcomes`` and the head ``min_anchor_outcomes`` of them (else
    all outcomes), ``holdout`` requires that, ``all`` ignores the split tag.

    Retires the candidate's predecessor via the existing CAP-C5
    ``store.supersede`` machinery (never deletes it) and promotes trust via
    the existing ``store.calibrate`` manual-override mode -- both reused as-is
    rather than duplicated here.
    """
    _check_gate_split(gate_split)
    found = store.get_chunks_by_ids([chunk_id])
    if not found:
        raise ValueError(f"no chunk found for id '{chunk_id}'")
    candidate = found[0]
    if candidate.layer != PROCEDURE_LAYER:
        raise ValueError(f"chunk '{chunk_id}' is not a procedural chunk")
    if not candidate.supersedes:
        raise ValueError(f"chunk '{chunk_id}' is not a refinement proposal (no predecessor)")

    key = candidate.source_refs[0] if candidate.source_refs else None
    if key is None:
        raise RefineError(f"cannot verify head for candidate '{chunk_id}': it has no source_refs key")
    head = find_procedure_head(store, key=key, pipeline_id=candidate.pipeline_id)
    if head is not None and head.chunk_id != candidate.supersedes:
        raise RefineError(
            f"stale candidate '{chunk_id}': it supersedes '{candidate.supersedes}' but the "
            f"current head of '{key}' is '{head.chunk_id}'; propose again"
        )

    attempt = _find_attempt(store, candidate.chunk_id)
    if attempt is not None and attempt.status != "proposed":
        raise RefineError(
            f"candidate '{chunk_id}' attempt '{attempt.attempt_id}' is already {attempt.status}; "
            "propose again"
        )
    model = attempt.model if attempt else None
    # Outcomes crediting both head and candidate belong to neither trial.
    def _side(chunk: str, other: str, split: str | None) -> OutcomeStats | None:
        return _safe_stats(store, chunk, model, [other], split=split)

    if gate_split == "all":
        trial_holdout = anchor_holdout = None
    else:
        trial_holdout = _side(candidate.chunk_id, candidate.supersedes, HOLDOUT_SPLIT)
        anchor_holdout = _side(candidate.supersedes, candidate.chunk_id, HOLDOUT_SPLIT)
    choice = _choose_split(
        gate_split,
        trial_holdout=trial_holdout,
        anchor_holdout=anchor_holdout,
        trial_all=_side(candidate.chunk_id, candidate.supersedes, None),
        anchor_all=_side(candidate.supersedes, candidate.chunk_id, None),
        min_trial_outcomes=min_trial_outcomes,
        min_anchor_outcomes=min_anchor_outcomes,
    )
    is_prune = attempt is not None and attempt.kind == "prune"
    if is_prune and gate_split == "holdout" and not choice.holdout_ok and not force:
        raise RefineGateError(
            "prune gate failed: not enough holdout evidence "
            f"(candidate {choice.holdout_trial_weight} < {min_trial_outcomes} or head "
            f"{choice.holdout_anchor_weight} < {min_anchor_outcomes} outcomes tagged "
            f"split={HOLDOUT_SPLIT!r}); tag outcomes with ctx.split or use --gate-split auto|all",
            {
                "split_used": HOLDOUT_SPLIT,
                "holdout_trial_weight": choice.holdout_trial_weight,
                "holdout_anchor_weight": choice.holdout_anchor_weight,
                "required": {
                    "min_trial_outcomes": min_trial_outcomes,
                    "min_anchor_outcomes": min_anchor_outcomes,
                },
                "reasons": ["insufficient holdout evidence"],
            },
        )
    split_used = choice.split_used
    if gate_split == "holdout" and not choice.holdout_ok:
        # Forced past missing holdout evidence (or a repair): observe on all outcomes.
        split_used = "all"
        choice = _SplitChoice(
            "all", False, _side(candidate.chunk_id, candidate.supersedes, None),
            _side(candidate.supersedes, candidate.chunk_id, None),
            choice.holdout_trial_weight, choice.holdout_anchor_weight,
        )
    stats = choice.stats
    if is_prune:
        assert attempt is not None
        # Recompute the anchor now: outcomes since the proposal are evidence too.
        anchor = choice.anchor
        if anchor is not None:
            attempt = attempt.model_copy(
                update={
                    "anchor_rate": anchor.success_rate,
                    "anchor_output_tokens": anchor.mean_output_tokens,
                }
            )
        if not force:
            _check_prune_gate(
                attempt,
                stats,
                gate_ratio=gate_ratio,
                min_trial_outcomes=min_trial_outcomes,
                split_used=split_used,
            )

    if not store.supersede(candidate.supersedes, candidate.chunk_id):
        raise ValueError(
            f"could not adopt '{chunk_id}': predecessor '{candidate.supersedes}' not found "
            "or in a different pipeline"
        )
    store.calibrate(chunk_id=candidate.chunk_id, trust=promote_trust)

    if attempt is not None:
        try:
            store.update_improvement_attempt(
                attempt.attempt_id,
                status="applied",
                accepted=True,
                resolved_at=time.time(),
                **(
                    {"anchor_rate": attempt.anchor_rate, "anchor_output_tokens": attempt.anchor_output_tokens}
                    if attempt.kind == "prune"
                    else {}
                ),
                **_observed_fields(attempt, stats),
                note=_with_split_note(attempt.note, split_used),
            )
        except NotImplementedError:
            pass

    if write_to is not None:
        write_to.write_text(candidate.content + "\n")

    return candidate.model_copy(update={"base_trust": promote_trust})


@dataclass(frozen=True)
class CandidateComparison:
    """One proposed candidate scored against the head (read-only)."""

    chunk_id: str
    attempt_id: str
    kind: str
    status: str  # "proposed" or "stale" (no longer supersedes the head)
    n: int
    total_weight: float
    success_rate: float | None
    mean_output_tokens: float | None
    split_used: str
    would_pass_gate: bool | None  # prune: the apply gate; repair: None (ungated)
    reasons: list[str]


@dataclass(frozen=True)
class CompareResult:
    key: str
    head_chunk_id: str
    anchor_split: str
    anchor_n: int
    anchor_total_weight: float
    anchor_success_rate: float | None
    anchor_mean_output_tokens: float | None
    candidates: list[CandidateComparison]

    @property
    def recommended(self) -> CandidateComparison | None:
        return next((c for c in self.candidates if c.status != "stale"), None)


def compare_candidates(
    store: BaseStore,
    *,
    key: str,
    pipeline_id: str | None = None,
    model: str | None = None,
    split: str = "auto",
    gate_ratio: float | None = None,
    min_trial_outcomes: int | None = None,
    min_anchor_outcomes: int | None = None,
) -> CompareResult:
    """Rank every proposed repair/prune candidate of a procedure (never writes).

    Each candidate is scored on outcomes that credit it and neither the head
    nor any other candidate; the anchor is the head minus all candidates.
    ``split`` follows ``apply_refinement``'s ``gate_split``. Rank order:
    non-stale first, then gate-passing, success rate desc, mean output tokens
    asc (unknown last), weight desc, chunk id.
    """
    _check_gate_split(split)
    gate_ratio = 0.9 if gate_ratio is None else gate_ratio
    min_trial = 5 if min_trial_outcomes is None else min_trial_outcomes
    min_anchor = 5 if min_anchor_outcomes is None else min_anchor_outcomes
    head = find_procedure_head(store, key=key, pipeline_id=pipeline_id)
    if head is None:
        raise ValueError(f"no procedure ingested for key '{key}'; run `ncp refine ingest` first")

    found: list[tuple[SubconsciousChunk, ImprovementAttempt]] = []
    for row in _procedure_rows(store, key=key, pipeline_id=pipeline_id):
        if row["chunk_id"] == head.chunk_id or not row.get("supersedes"):
            continue
        attempt = _find_attempt(store, str(row["chunk_id"]))
        if attempt is not None and attempt.status == "proposed":
            found.append((_row_to_chunk(row), attempt))
    candidate_ids = [chunk.chunk_id for chunk, _ in found]

    def anchor_for(split_filter: str | None) -> OutcomeStats | None:
        return _safe_stats(store, head.chunk_id, model, candidate_ids, split=split_filter)

    anchor_all = anchor_for(None)
    anchor_hold = None if split == "all" else anchor_for(HOLDOUT_SPLIT)

    comparisons: list[CandidateComparison] = []
    anchor_shown = anchor_all
    anchor_split = "all"
    for chunk, attempt in found:
        excluded = [head.chunk_id, *[c for c in candidate_ids if c != chunk.chunk_id]]
        trial_hold = (
            None if split == "all"
            else _safe_stats(store, chunk.chunk_id, model, excluded, split=HOLDOUT_SPLIT)
        )
        choice = _choose_split(
            split,
            trial_holdout=trial_hold,
            anchor_holdout=anchor_hold,
            trial_all=_safe_stats(store, chunk.chunk_id, model, excluded),
            anchor_all=anchor_all,
            min_trial_outcomes=min_trial,
            min_anchor_outcomes=min_anchor,
        )
        stats = choice.stats
        passes: bool | None = None
        reasons: list[str] = []
        if attempt.kind == "prune":
            if split == "holdout" and not choice.holdout_ok:
                reasons = ["insufficient holdout evidence"]
            else:
                anchor = choice.anchor
                details = _prune_gate_details(
                    attempt.model_copy(update={
                        "anchor_rate": anchor.success_rate if anchor else None,
                        "anchor_output_tokens": anchor.mean_output_tokens if anchor else None,
                    }),
                    stats,
                    gate_ratio=gate_ratio,
                    min_trial_outcomes=min_trial,
                    split_used=choice.split_used,
                )
                reasons = list(details.get("reasons", []))
            passes = not reasons
        status = "proposed" if chunk.supersedes == head.chunk_id else "stale"
        if status == "stale":
            reasons = [*reasons, f"stale: supersedes {chunk.supersedes}, head is {head.chunk_id}"]
        if choice.split_used == HOLDOUT_SPLIT:
            anchor_shown, anchor_split = choice.anchor, HOLDOUT_SPLIT
        comparisons.append(
            CandidateComparison(
                chunk_id=chunk.chunk_id,
                attempt_id=attempt.attempt_id,
                kind=attempt.kind,
                status=status,
                n=stats.n if stats else 0,
                total_weight=stats.total_weight if stats else 0.0,
                success_rate=stats.success_rate if stats else None,
                mean_output_tokens=stats.mean_output_tokens if stats else None,
                split_used=choice.split_used,
                would_pass_gate=passes,
                reasons=reasons,
            )
        )

    def rank(c: CandidateComparison) -> tuple:
        return (
            c.status == "stale",
            c.would_pass_gate is not True,
            c.success_rate is None,
            -(c.success_rate or 0.0),
            c.mean_output_tokens is None,
            c.mean_output_tokens or 0.0,
            -c.total_weight,
            c.chunk_id,
        )

    comparisons.sort(key=rank)
    return CompareResult(
        key=key,
        head_chunk_id=head.chunk_id,
        anchor_split=anchor_split,
        anchor_n=anchor_shown.n if anchor_shown else 0,
        anchor_total_weight=anchor_shown.total_weight if anchor_shown else 0.0,
        anchor_success_rate=anchor_shown.success_rate if anchor_shown else None,
        anchor_mean_output_tokens=anchor_shown.mean_output_tokens if anchor_shown else None,
        candidates=comparisons,
    )


def reject_refinement(
    store: BaseStore, *, chunk_id: str, reason: str | None = None
) -> ImprovementAttempt:
    """Mark a candidate's attempt rejected (the candidate chunk stays in place)."""
    try:
        attempt = _find_attempt(store, chunk_id)
        if attempt is None:
            raise RefineError(f"no improvement attempt found for candidate '{chunk_id}'")
        if attempt.status != "proposed":
            raise RefineError(f"attempt '{attempt.attempt_id}' is already {attempt.status}")
        stats = _safe_stats(store, chunk_id, attempt.model)
        fields = _observed_fields(attempt, stats)
        if reason:
            fields["note"] = reason[:500]
        return store.update_improvement_attempt(
            attempt.attempt_id, status="rejected", resolved_at=time.time(), **fields
        )
    except NotImplementedError as exc:
        raise RefineError(str(exc)) from exc


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

    reverted_attempt = _find_attempt(store, head.chunk_id)
    if reverted_attempt is not None:
        try:
            store.update_improvement_attempt(
                reverted_attempt.attempt_id, status="rolled_back", resolved_at=time.time()
            )
        except NotImplementedError:
            pass

    if write_to is not None:
        write_to.write_text(reverted.content + "\n")

    return reverted
