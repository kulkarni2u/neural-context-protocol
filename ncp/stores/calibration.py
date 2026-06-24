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
    dissent_count: int = 0


@dataclass
class FeedbackResult:
    """Computed trust updates and reporting metadata."""

    updates: list[tuple[float, str]] = field(default_factory=list)  # (new_trust, chunk_id)
    change_log: list[dict] = field(default_factory=list)
    adjusted: int = 0
    skipped: int = 0


@dataclass(frozen=True)
class ReputationUpdate:
    """New Beta posterior values for one identity."""

    identity_id: str
    new_alpha: float
    new_beta: float
    obs_delta: int
    positive_evidence: float = 0.0
    negative_evidence: float = 0.0


_DISSENT_SATURATION = 3  # dissents needed for full penalty (vs 10 retrievals for full boost)


def compute_feedback_updates(
    rows: list[FeedbackRow],
    *,
    feedback_weight: float,
    propagation_factor: float,
    dissent_weight: float = 0.2,
) -> FeedbackResult:
    """Compute net trust deltas from retrieval feedback and dissent, with 1-hop propagation.

    Each chunk earns a net delta combining two signals:
    - Positive: retrieved ``rc`` times → ``+feedback_weight * min(1, rc/10)``.
    - Negative: disputed ``dc`` times → ``-dissent_weight * min(1, dc/3)``.

    Propagation: a fraction (``propagation_factor``) of each chunk's net delta
    flows one hop to its ``caused_by`` parent — crediting a cause for useful
    effects and debiting it for disputed ones. The hop is single to keep credit
    assignment bounded and acyclic; the parent must be among ``rows`` (live and
    not protected) to be affected. A chunk that is both retrieved and a parent
    accumulates both contributions.

    Caller is responsible for excluding protected (``user_verified``) chunks
    from ``rows`` before calling.
    """
    base_trust = {row.chunk_id: row.base_trust for row in rows}
    retrieval_count = {row.chunk_id: row.retrieval_count for row in rows}
    dissent_count = {row.chunk_id: row.dissent_count for row in rows}

    direct: dict[str, float] = {}
    for row in rows:
        delta = 0.0
        if row.retrieval_count > 0:
            delta += feedback_weight * min(1.0, row.retrieval_count / 10)
        if row.dissent_count > 0:
            delta -= dissent_weight * min(1.0, row.dissent_count / _DISSENT_SATURATION)
        if delta != 0.0:
            direct[row.chunk_id] = delta

    total: dict[str, float] = dict(direct)
    if propagation_factor > 0.0:
        for row in rows:
            delta = direct.get(row.chunk_id, 0.0)
            if delta == 0.0:
                continue
            parent = row.caused_by
            if parent and parent in base_trust:
                total[parent] = total.get(parent, 0.0) + delta * propagation_factor

    result = FeedbackResult()
    adjusted_ids: set[str] = set()
    for chunk_id, delta in total.items():
        old_trust = base_trust[chunk_id]
        new_trust = min(1.0, max(0.0, old_trust + delta))
        if new_trust == old_trust:
            continue
        entry: dict = {
            "chunk_id": chunk_id,
            "old_trust": old_trust,
            "new_trust": new_trust,
        }
        if chunk_id in direct:
            rc = retrieval_count.get(chunk_id, 0)
            dc = dissent_count.get(chunk_id, 0)
            if rc > 0 and dc > 0:
                entry["reason"] = "mixed_feedback"
                entry["retrieval_count"] = rc
                entry["dissent_count"] = dc
            elif dc > 0:
                entry["reason"] = "dissent_penalty"
                entry["dissent_count"] = dc
            else:
                entry["reason"] = "retrieval_feedback"
                entry["retrieval_count"] = rc
        else:
            entry["reason"] = "trust_propagation"
        result.change_log.append(entry)
        result.updates.append((new_trust, chunk_id))
        adjusted_ids.add(chunk_id)

    result.adjusted = len(adjusted_ids)
    result.skipped = len(rows) - len(adjusted_ids)
    return result


def rollup_reputation(
    change_log: list[dict],
    chunk_author: dict[str, str],
    prior: dict[str, tuple[float, float]],
    *,
    gain: float,
    forget: float,
    K_CONF: int = 20,
) -> tuple[ReputationUpdate, ...]:
    """Roll chunk trust deltas up to per-identity Beta posterior updates."""

    del K_CONF  # Readers derive confidence from obs_count; keep the spec-level API.

    pos: dict[str, float] = {}
    neg: dict[str, float] = {}
    obs: dict[str, int] = {}

    for entry in change_log:
        chunk_id = entry.get("chunk_id")
        if not isinstance(chunk_id, str):
            continue
        identity_id = chunk_author.get(chunk_id)
        if identity_id is None:
            continue

        old_trust = float(entry.get("old_trust", 0.0))
        new_trust = float(entry.get("new_trust", old_trust))
        delta = new_trust - old_trust
        if delta > 0:
            pos[identity_id] = pos.get(identity_id, 0.0) + delta
        elif delta < 0:
            neg[identity_id] = neg.get(identity_id, 0.0) + (-delta)
        else:
            continue
        obs[identity_id] = obs.get(identity_id, 0) + 1

    updates: list[ReputationUpdate] = []
    for identity_id in sorted(obs):
        alpha, beta = prior.get(identity_id, (1.0, 1.0))
        decayed_alpha = 1.0 + max(0.0, alpha - 1.0) * forget
        decayed_beta = 1.0 + max(0.0, beta - 1.0) * forget
        positive_evidence = pos.get(identity_id, 0.0) * gain
        negative_evidence = neg.get(identity_id, 0.0) * gain
        updates.append(
            ReputationUpdate(
                identity_id=identity_id,
                new_alpha=max(1.0, decayed_alpha + positive_evidence),
                new_beta=max(1.0, decayed_beta + negative_evidence),
                obs_delta=obs[identity_id],
                positive_evidence=positive_evidence,
                negative_evidence=negative_evidence,
            )
        )
    return tuple(updates)
