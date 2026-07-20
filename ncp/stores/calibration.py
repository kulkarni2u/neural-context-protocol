"""Shared trust-feedback math for calibration passes.

Both the direct retrieval-feedback boost and 1-hop trust propagation along
``caused_by`` edges live here so every store backend computes credit
assignment identically. Keeping this backend-agnostic (it operates on plain
``FeedbackRow`` records, not DB rows) makes it unit-testable without a store.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ncp.types import OutcomeRecord


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
    consumed_chunk_ids: list[str] = field(default_factory=list)
    adjusted: int = 0
    skipped: int = 0
    # Ancestors adjusted via outcome-driven propagation (CAP-T3 multi-hop),
    # counted separately from retrieval/dissent-driven propagation so the
    # calibrate report can attribute credit by originating signal.
    outcome_propagated: int = 0


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
    usage_prior_weight: float = 1.0,
    outcome_evidence: dict[str, float] | None = None,
    propagation_max_hops: int = 1,
) -> FeedbackResult:
    """Compute net trust deltas from retrieval feedback, dissent, and outcomes.

    WHEN ``outcome_evidence`` is present (CAP-T3), outcome evidence becomes the
    PRIMARY signal for per-chunk trust deltas.  Each chunk's net delta combines:

    - Outcome evidence: ``outcome_evidence[chunk_id]`` (positive for success,
      negative for failure, scaled by weight).  Applied at full strength.
    - Retrieval usage (weak prior): ``+feedback_weight * min(1, rc/10) * usage_prior_weight``
      when outcome evidence is present, else at full ``usage_prior_weight``.
    - Dissent penalty: ``-dissent_weight * min(1, dc/3)`` (always at full).

    When ``outcome_evidence`` is empty/None the function behaves exactly as
    before (retrieval counts drive the update at their full weight).

    Propagation: a fraction (``propagation_factor``) of each chunk's net delta
    flows up to ``propagation_max_hops`` hops along ``caused_by`` ancestry —
    crediting a cause for useful effects and debiting it for disputed ones.
    Hop *h* receives ``propagation_factor ** h`` of the originating delta.
    ``propagation_max_hops=1`` (the default) reproduces the original single-hop
    behavior exactly. Each ``FeedbackRow.caused_by`` is taken as the already-
    resolved parent id (callers pick the current ``caused_by`` scalar, falling
    back to a ``caused_by`` edge row only when the scalar is absent); walking
    further ancestry is just following that chain across ``rows``. A visited
    set per originating chunk makes the walk cycle-safe, and an ancestor must
    be among ``rows`` (live and not protected) to receive credit. A chunk that
    is both retrieved and an ancestor of another accumulates both/all
    contributions, and multiple descendants sharing an ancestor accumulate
    into that ancestor together. Since ``direct[cid]`` already folds outcome
    evidence into the same net delta as retrieval/dissent, an outcome-driven
    delta takes this identical propagation path with no special-casing
    (WI-P3, CAP-T3 extension). Ancestor entries whose credit originated (at
    least in part) from an outcome-evidenced descendant are tagged
    ``reason="outcome_propagation"`` instead of ``"trust_propagation"`` in
    ``change_log``, and counted in ``FeedbackResult.outcome_propagated``, so
    callers can attribute propagated credit to outcomes vs. retrieval/dissent.

    Caller is responsible for excluding protected (``user_verified``) chunks
    from ``rows`` before calling.
    """
    base_trust = {row.chunk_id: row.base_trust for row in rows}
    retrieval_count = {row.chunk_id: row.retrieval_count for row in rows}
    dissent_count = {row.chunk_id: row.dissent_count for row in rows}
    outcome_evidence = outcome_evidence or {}

    direct: dict[str, float] = {}
    for row in rows:
        cid = row.chunk_id
        delta = 0.0

        # Outcome evidence is the primary signal (CAP-T3)
        outcome_delta = outcome_evidence.get(cid, 0.0)
        if outcome_delta != 0.0:
            delta += outcome_delta

        # Retrieval usage becomes a weak prior when outcomes are present
        if row.retrieval_count > 0:
            retrieval_boost = feedback_weight * min(1.0, row.retrieval_count / 10)
            if outcome_evidence:
                retrieval_boost *= usage_prior_weight
            delta += retrieval_boost

        if row.dissent_count > 0:
            delta -= dissent_weight * min(1.0, row.dissent_count / _DISSENT_SATURATION)

        if delta != 0.0:
            direct[cid] = delta

    total: dict[str, float] = dict(direct)
    # Ancestors that received propagated credit originating (at least in
    # part) from an outcome-evidenced descendant. A descendant's whole net
    # delta propagates regardless of source (outcome/retrieval/dissent), but
    # this set lets the report distinguish outcome-driven propagation from
    # purely retrieval/dissent-driven propagation for attribution purposes.
    outcome_sourced_ancestors: set[str] = set()
    if propagation_factor > 0.0 and propagation_max_hops > 0:
        parent_of = {row.chunk_id: row.caused_by for row in rows if row.caused_by}
        for row in rows:
            delta = direct.get(row.chunk_id, 0.0)
            if delta == 0.0:
                continue
            is_outcome_sourced = outcome_evidence.get(row.chunk_id, 0.0) != 0.0
            visited = {row.chunk_id}
            ancestor = parent_of.get(row.chunk_id)
            hop = 1
            while ancestor and ancestor in base_trust and ancestor not in visited and hop <= propagation_max_hops:
                total[ancestor] = total.get(ancestor, 0.0) + delta * (propagation_factor**hop)
                if is_outcome_sourced:
                    outcome_sourced_ancestors.add(ancestor)
                visited.add(ancestor)
                hop += 1
                ancestor = parent_of.get(ancestor)

    result = FeedbackResult()
    result.consumed_chunk_ids = [
        row.chunk_id for row in rows if row.retrieval_count > 0 or row.dissent_count > 0
    ]
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
            oe = outcome_evidence.get(chunk_id, 0.0)
            if oe > 0:
                entry["reason"] = "outcome_success"
                entry["outcome_evidence"] = oe
            elif oe < 0:
                entry["reason"] = "outcome_failure"
                entry["outcome_evidence"] = oe
            elif rc > 0 and dc > 0:
                entry["reason"] = "mixed_feedback"
                entry["retrieval_count"] = rc
                entry["dissent_count"] = dc
            elif dc > 0:
                entry["reason"] = "dissent_penalty"
                entry["dissent_count"] = dc
            else:
                entry["reason"] = "retrieval_feedback"
                entry["retrieval_count"] = rc
        elif chunk_id in outcome_sourced_ancestors:
            entry["reason"] = "outcome_propagation"
        else:
            entry["reason"] = "trust_propagation"
        result.change_log.append(entry)
        result.updates.append((new_trust, chunk_id))
        adjusted_ids.add(chunk_id)

    result.adjusted = len(adjusted_ids)
    result.skipped = len(rows) - len(adjusted_ids)
    result.outcome_propagated = sum(
        1 for entry in result.change_log if entry.get("reason") == "outcome_propagation"
    )
    return result


def compute_outcome_evidence(outcomes: list[OutcomeRecord]) -> dict[str, float]:
    """Aggregate outcome evidence into per-chunk deltas.

    Returns ``{chunk_id: net_delta}`` where success outcomes contribute
    ``+weight`` and failures contribute ``-weight``.
    """
    evidence: dict[str, float] = {}
    for out in outcomes:
        delta = out.weight if out.success else -out.weight
        for cid in out.chunk_ids:
            evidence[cid] = evidence.get(cid, 0.0) + delta
    return evidence


def rollup_reputation(
    change_log: list[dict],
    chunk_author: dict[str, str],
    prior: dict[str, tuple[float, float]],
    *,
    gain: float,
    forget: float,
    K_CONF: int = 20,
    outcome_evidence: dict[str, float] | None = None,
) -> tuple[ReputationUpdate, ...]:
    """Roll chunk trust deltas up to per-identity Beta posterior updates.

    When ``outcome_evidence`` is provided (CAP-T3), it is also reflected in
    the evidence counts so the reputation rollup correctly captures outcome-
    based evidence in addition to retrieval/dissent-based deltas.
    """
    outcome_evidence = outcome_evidence or {}
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

    # Fold raw outcome evidence into reputation even when the chunk trust did
    # not move (e.g. already clamped at 1.0).
    for chunk_id, oe_delta in outcome_evidence.items():
        identity_id = chunk_author.get(chunk_id)
        if identity_id is None:
            continue
        if oe_delta > 0:
            pos[identity_id] = pos.get(identity_id, 0.0) + oe_delta
        elif oe_delta < 0:
            neg[identity_id] = neg.get(identity_id, 0.0) + (-oe_delta)
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
