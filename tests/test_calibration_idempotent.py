from __future__ import annotations

from pathlib import Path

import pytest

from ncp.stores.sqlite import SQLiteStore
from ncp.types import SubconsciousChunk


def _chunk(chunk_id: str, *, base_trust: float = 0.5) -> SubconsciousChunk:
    return SubconsciousChunk(
        chunk_id=chunk_id,
        content="idempotent calibration feedback target",
        layer="semantic",
        pipeline_id="pipe_idempotent",
        src="agent_inferred",
        written_by="calibration_agent",
        base_trust=base_trust,
        chunk_type="prose",
        scope="pipeline",
    )


def _trust(store: SQLiteStore, chunk_id: str) -> float:
    with store._connect() as connection:
        row = connection.execute(
            "SELECT base_trust FROM chunks WHERE chunk_id = ?",
            (chunk_id,),
        ).fetchone()
    assert row is not None
    return float(row["base_trust"])


def _feedback_counts(store: SQLiteStore, chunk_id: str) -> tuple[int, int]:
    with store._connect() as connection:
        row = connection.execute(
            "SELECT retrieval_count, dissent_count FROM chunks WHERE chunk_id = ?",
            (chunk_id,),
        ).fetchone()
    assert row is not None
    return int(row["retrieval_count"]), int(row["dissent_count"])


def test_feedback_calibration_is_idempotent_without_new_activity(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "store.db")
    store.write(_chunk("cal_idem", base_trust=0.5))
    with store._connect() as connection:
        connection.execute(
            "UPDATE chunks SET retrieval_count = 5, dissent_count = 0 WHERE chunk_id = ?",
            ("cal_idem",),
        )

    first = store.calibrate(pipeline_id="pipe_idempotent", feedback_mode=True)
    trust_after_first = _trust(store, "cal_idem")
    counts_after_first = _feedback_counts(store, "cal_idem")
    second = store.calibrate(pipeline_id="pipe_idempotent", feedback_mode=True)
    trust_after_second = _trust(store, "cal_idem")
    third = store.calibrate(pipeline_id="pipe_idempotent", feedback_mode=True)
    trust_after_third = _trust(store, "cal_idem")

    assert first.feedback_adjusted == 1
    assert trust_after_first == pytest.approx(0.575)
    assert counts_after_first == (0, 0)
    assert second.feedback_adjusted == 0
    assert third.feedback_adjusted == 0
    assert trust_after_second == pytest.approx(trust_after_first)
    assert trust_after_third == pytest.approx(trust_after_first)
