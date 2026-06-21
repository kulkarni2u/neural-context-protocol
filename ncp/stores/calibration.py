"""Shared trust-feedback math for calibration passes.

Both the direct retrieval-feedback boost and 1-hop trust propagation along
``caused_by`` edges live here so every store backend computes credit
assignment identically. Keeping this backend-agnostic (it operates on plain
``FeedbackRow`` records, not DB rows) makes it unit-testable without a store.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class FeedbackRow:
    """Minimal per-chunk inputs needed for feedback calibration."""

    chunk_id: str
    base_trust: float
    retrieval_count: int
    caused_by: str | None = None


@dataclass
class FeedbackResult:
    """Computed trust updates and reporting metadata."""

    updates: list[tuple[float, str]] = field(default_factory=list)  # (new_trust, chunk_id)
    change_log: list[dict] = field(default_factory=list)
    adjusted: int = 0
    skipped: int = 0


def compute_feedback_updates(
    rows: list[FeedbackRow],
    *,
    feedback_weight: float,
    propagation_factor: float,
) -> FeedbackResult:
    """Compute trust boosts from retrieval feedback plus 1-hop propagation.

    Direct boost: a chunk retrieved ``rc`` times gains
    ``feedback_weight * min(1, rc/10)`` trust.

    Propagation: a fraction (``propagation_factor``) of each chunk's direct
    boost flows to its ``caused_by`` parent — crediting a cause for effects
    that proved useful. Propagation is a single hop to keep credit assignment
    bounded and acyclic; the parent must be among ``rows`` (live and not
    protected) to receive credit. A chunk that is both retrieved and a parent
    accumulates both boosts.

    Caller is responsible for excluding protected (``user_verified``) chunks
    from ``rows`` before calling.
    """
    base_trust = {row.chunk_id: row.base_trust for row in rows}
    retrieval_count = {row.chunk_id: row.retrieval_count for row in rows}

    direct: dict[str, float] = {}
    for row in rows:
        if row.retrieval_count > 0:
            direct[row.chunk_id] = feedback_weight * min(1.0, row.retrieval_count / 10)

    total: dict[str, float] = dict(direct)
    if propagation_factor > 0.0:
        for row in rows:
            boost = direct.get(row.chunk_id)
            if boost is None:
                continue
            parent = row.caused_by
            if parent and parent in base_trust:
                total[parent] = total.get(parent, 0.0) + boost * propagation_factor

    result = FeedbackResult()
    adjusted_ids: set[str] = set()
    for chunk_id, boost in total.items():
        old_trust = base_trust[chunk_id]
        new_trust = min(1.0, old_trust + boost)
        if new_trust <= old_trust:
            continue
        entry: dict = {
            "chunk_id": chunk_id,
            "old_trust": old_trust,
            "new_trust": new_trust,
        }
        if chunk_id in direct:
            entry["reason"] = "retrieval_feedback"
            entry["retrieval_count"] = retrieval_count.get(chunk_id, 0)
        else:
            entry["reason"] = "trust_propagation"
        result.change_log.append(entry)
        result.updates.append((new_trust, chunk_id))
        adjusted_ids.add(chunk_id)

    result.adjusted = len(adjusted_ids)
    result.skipped = len(rows) - len(adjusted_ids)
    return result
