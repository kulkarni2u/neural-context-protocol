"""Tests for CAP-C8 procedural self-refinement (ncp/refine.py)."""

from __future__ import annotations

from pathlib import Path

import pytest

from ncp.refine import (
    MAX_CONTENT_CHARS,
    apply_refinement,
    build_refinement_proposal,
    find_procedure_head,
    ingest_procedure,
    procedure_chain,
    propose_refinement,
    rollback_refinement,
)
from ncp.stores.sqlite import SQLiteStore
from ncp.types import OutcomeRecord


def _outcome(oid: str, note: str | None, success: bool = False, chunk_ids: list[str] | None = None) -> OutcomeRecord:
    return OutcomeRecord(outcome_id=oid, chunk_ids=chunk_ids or ["head"], success=success, note=note)


# ---------------------------------------------------------------------------
# build_refinement_proposal: pure logic
# ---------------------------------------------------------------------------


def test_no_proposal_below_failure_threshold() -> None:
    outcomes = [_outcome("o1", "missing null check"), _outcome("o2", "missing null check")]
    assert build_refinement_proposal("Do the task.", outcomes, min_failed_outcomes=3) is None


def test_proposal_appends_ranked_failure_notes() -> None:
    outcomes = [
        _outcome("o1", "skips the null guard"),
        _outcome("o2", "skips the null guard"),
        _outcome("o3", "skips the null guard"),
        _outcome("o4", "forgets to run tests"),
        _outcome("o5", "forgets to run tests"),
    ]
    proposal = build_refinement_proposal("Fix the bug.", outcomes, min_failed_outcomes=3, max_bullets=5)
    assert proposal is not None
    assert proposal.failure_count == 5
    assert proposal.success_count == 0
    # Most-repeated issue (3x) ranks before the 2x one.
    assert "skips the null guard" in proposal.rationale[0]
    assert "(observed 3x)" in proposal.rationale[0]
    assert any("forgets to run tests" in r for r in proposal.rationale)
    assert proposal.proposed_content.startswith("Fix the bug.")
    assert "## Refinements (evidence-based, proposed)" in proposal.proposed_content
    assert set(proposal.based_on_outcome_ids) == {"o1", "o2", "o3", "o4", "o5"}


def test_proposal_ignores_successes_and_empty_notes() -> None:
    outcomes = [
        _outcome("o1", "issue a", success=True),
        _outcome("o2", None),
        _outcome("o3", "issue b"),
        _outcome("o4", "issue b"),
        _outcome("o5", "issue b"),
    ]
    proposal = build_refinement_proposal("Base.", outcomes, min_failed_outcomes=3)
    assert proposal is not None
    assert proposal.success_count == 1
    assert proposal.rationale == ["- issue b (observed 3x)"]


def test_proposal_dedupes_case_and_whitespace_insensitively() -> None:
    outcomes = [
        _outcome("o1", "Retry   count is null"),
        _outcome("o2", "retry count is null"),
        _outcome("o3", "  RETRY COUNT IS NULL  "),
    ]
    proposal = build_refinement_proposal("Base.", outcomes, min_failed_outcomes=3)
    assert proposal is not None
    assert len(proposal.rationale) == 1
    assert "(observed 3x)" in proposal.rationale[0]


def test_proposal_never_exceeds_content_bound() -> None:
    long_base = "x" * (MAX_CONTENT_CHARS - 50)
    outcomes = [_outcome(f"o{i}", f"distinct failure reason number {i}" * 3) for i in range(6)]
    proposal = build_refinement_proposal(long_base, outcomes, min_failed_outcomes=3, max_bullets=6)
    if proposal is not None:
        assert len(proposal.proposed_content) <= MAX_CONTENT_CHARS


def test_proposal_none_when_nothing_fits() -> None:
    long_base = "x" * MAX_CONTENT_CHARS
    outcomes = [_outcome(f"o{i}", "some real failure note here") for i in range(3)]
    assert build_refinement_proposal(long_base, outcomes, min_failed_outcomes=3) is None


# ---------------------------------------------------------------------------
# Store-backed lifecycle: ingest -> propose -> apply / rollback
# ---------------------------------------------------------------------------


def test_ingest_creates_generation_zero(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "store.db")
    chunk = ingest_procedure(store, key="null-guard-rule", content="Always null-check retryCount.")
    assert chunk.generation == 0
    assert chunk.layer == "procedural"
    assert chunk.source_refs == ["null-guard-rule"]

    head = find_procedure_head(store, key="null-guard-rule")
    assert head is not None
    assert head.chunk_id == chunk.chunk_id


def test_ingest_rejects_duplicate_key(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "store.db")
    ingest_procedure(store, key="dup", content="Base rule.")
    with pytest.raises(ValueError, match="already ingested"):
        ingest_procedure(store, key="dup", content="Different content.")


def test_ingest_rejects_oversized_content(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "store.db")
    with pytest.raises(ValueError, match="2000"):
        ingest_procedure(store, key="too-big", content="x" * (MAX_CONTENT_CHARS + 1))


def test_propose_without_ingest_raises(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "store.db")
    with pytest.raises(ValueError, match="no procedure ingested"):
        propose_refinement(store, key="ghost")


def test_propose_writes_candidate_linked_to_head(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "store.db")
    head = ingest_procedure(store, key="rule", content="Always validate input.")
    for i in range(3):
        store.record_outcome(
            OutcomeRecord(outcome_id=f"o{i}", chunk_ids=[head.chunk_id], success=False, note="skipped validation step")
        )

    result = propose_refinement(store, key="rule", min_failed_outcomes=3)
    assert result.proposal is not None
    assert result.new_chunk is not None
    assert result.new_chunk.supersedes == head.chunk_id
    assert result.new_chunk.generation == head.generation + 1
    assert result.new_chunk.base_trust < 0.7  # unadopted proposal stays low-trust

    # Proposing does NOT adopt: the head is unchanged until apply().
    still_head = find_procedure_head(store, key="rule")
    assert still_head is not None
    assert still_head.chunk_id == head.chunk_id


def test_propose_dry_run_writes_nothing(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "store.db")
    head = ingest_procedure(store, key="rule", content="Base.")
    for i in range(3):
        store.record_outcome(
            OutcomeRecord(outcome_id=f"o{i}", chunk_ids=[head.chunk_id], success=False, note="same recurring issue")
        )
    result = propose_refinement(store, key="rule", min_failed_outcomes=3, dry_run=True)
    assert result.proposal is not None
    assert result.new_chunk is None


def test_propose_insufficient_evidence_returns_no_proposal(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "store.db")
    head = ingest_procedure(store, key="rule", content="Base.")
    store.record_outcome(OutcomeRecord(outcome_id="o1", chunk_ids=[head.chunk_id], success=False, note="one-off"))
    result = propose_refinement(store, key="rule", min_failed_outcomes=3)
    assert result.proposal is None
    assert result.new_chunk is None


def test_apply_adopts_candidate_and_promotes_trust(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "store.db")
    head = ingest_procedure(store, key="rule", content="Base rule.")
    for i in range(3):
        store.record_outcome(
            OutcomeRecord(outcome_id=f"o{i}", chunk_ids=[head.chunk_id], success=False, note="recurring gap")
        )
    result = propose_refinement(store, key="rule", min_failed_outcomes=3)
    assert result.new_chunk is not None

    adopted = apply_refinement(store, chunk_id=result.new_chunk.chunk_id, promote_trust=0.9)
    assert adopted.base_trust == 0.9

    new_head = find_procedure_head(store, key="rule")
    assert new_head is not None
    assert new_head.chunk_id == result.new_chunk.chunk_id

    # get_chunks_by_ids applies the same "current" bitemporal filter as
    # retrieval, so a now-superseded chunk no longer shows up there --
    # inspect it through the raw chain rows instead.
    from ncp.refine import _procedure_rows

    rows = {r["chunk_id"]: r for r in _procedure_rows(store, key="rule", pipeline_id=None)}
    assert rows[head.chunk_id]["superseded_by"] == result.new_chunk.chunk_id


def test_apply_writes_file(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "store.db")
    head = ingest_procedure(store, key="rule", content="Base rule.")
    for i in range(3):
        store.record_outcome(
            OutcomeRecord(outcome_id=f"o{i}", chunk_ids=[head.chunk_id], success=False, note="recurring gap")
        )
    result = propose_refinement(store, key="rule", min_failed_outcomes=3)
    assert result.new_chunk is not None

    target = tmp_path / "procedure.md"
    apply_refinement(store, chunk_id=result.new_chunk.chunk_id, write_to=target)
    assert target.read_text().strip() == result.new_chunk.content.strip()


def test_apply_rejects_non_candidate_chunk(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "store.db")
    head = ingest_procedure(store, key="rule", content="Base rule.")
    with pytest.raises(ValueError, match="not a refinement proposal"):
        apply_refinement(store, chunk_id=head.chunk_id)


def test_rollback_restores_prior_content_as_new_generation(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "store.db")
    head = ingest_procedure(store, key="rule", content="Original rule.")
    for i in range(3):
        store.record_outcome(
            OutcomeRecord(outcome_id=f"o{i}", chunk_ids=[head.chunk_id], success=False, note="recurring gap")
        )
    result = propose_refinement(store, key="rule", min_failed_outcomes=3)
    assert result.new_chunk is not None
    apply_refinement(store, chunk_id=result.new_chunk.chunk_id)

    reverted = rollback_refinement(store, key="rule")
    assert reverted.content == "Original rule."
    assert reverted.generation == result.new_chunk.generation + 1

    new_head = find_procedure_head(store, key="rule")
    assert new_head is not None
    assert new_head.chunk_id == reverted.chunk_id

    chain = procedure_chain(store, key="rule")
    # Nothing was deleted: original, adopted candidate, and rollback all remain.
    assert len(chain) == 3
    assert chain[0].chunk_id == head.chunk_id


def test_rollback_without_prior_version_raises(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "store.db")
    ingest_procedure(store, key="rule", content="Only version.")
    with pytest.raises(ValueError, match="no prior version"):
        rollback_refinement(store, key="rule")


def test_pipeline_scoping_keeps_procedures_independent(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "store.db")
    ingest_procedure(store, key="rule", content="Pipeline A rule.", pipeline_id="pipe_a")
    ingest_procedure(store, key="rule", content="Pipeline B rule.", pipeline_id="pipe_b")

    head_a = find_procedure_head(store, key="rule", pipeline_id="pipe_a")
    head_b = find_procedure_head(store, key="rule", pipeline_id="pipe_b")
    assert head_a is not None and head_b is not None
    assert head_a.chunk_id != head_b.chunk_id
    assert head_a.content == "Pipeline A rule."
    assert head_b.content == "Pipeline B rule."


# ---------------------------------------------------------------------------
# BaseStore.list_outcomes (SQLite)
# ---------------------------------------------------------------------------


def test_list_outcomes_filters_by_chunk_id(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "store.db")
    store.record_outcome(OutcomeRecord(outcome_id="o1", chunk_ids=["c1"], success=True))
    store.record_outcome(OutcomeRecord(outcome_id="o2", chunk_ids=["c2"], success=False, note="other"))

    only_c1 = store.list_outcomes(chunk_ids=["c1"])
    assert [o.outcome_id for o in only_c1] == ["o1"]

    everything = store.list_outcomes()
    assert {o.outcome_id for o in everything} == {"o1", "o2"}


def test_list_outcomes_does_not_consume(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "store.db")
    store.record_outcome(OutcomeRecord(outcome_id="o1", chunk_ids=["c1"], success=True))
    store.list_outcomes(chunk_ids=["c1"])
    store.list_outcomes(chunk_ids=["c1"])
    with store._connect() as conn:
        loaded = store._load_unconsumed_outcomes(conn)
    assert len(loaded) == 1
