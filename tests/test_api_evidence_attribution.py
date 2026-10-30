"""ncp.run / ncp.stream link the evidence actually delivered to the adapter.

Regression: run/stream never passed retrieved chunk ids to post_turn, so a
later ``record_outcome(turn_id=...)`` resolved to no chunks at all.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

import ncp
from ncp.adapters.base import BaseAdapter
from ncp.assembler import AssemblyResult, stored_evidence_ids
from ncp.config import NCPConfig
from ncp.stores.sqlite import SQLiteStore
from ncp.types import ConsciousBlock, OutcomeRecord, SubconsciousChunk


class _CapturingAdapter(BaseAdapter):
    """Records the exact model-visible context of every call."""

    def __init__(self) -> None:
        self.contexts: list[str] = []

    def call(self, ncp_context: str, user_turn: str) -> str:
        self.contexts.append(ncp_context)
        return f"response_{len(self.contexts)}"

    def stream(self, ncp_context: str, user_turn: str):
        self.contexts.append(ncp_context)
        yield f"response_{len(self.contexts)}"


def _agent() -> ConsciousBlock:
    return ncp.agent(
        id="executor",
        role="build",
        owns=["implementation"],
        must_not=["planning"],
        task="payment_retry",
        slot="retry_policy",
        intent="fix_retry_bug",
    )


def _store(tmp_path: Path, *, memo: bool = False) -> tuple[SQLiteStore, NCPConfig]:
    project = tmp_path / "repo"
    (project / ".git").mkdir(parents=True)
    values = {"memoization": {"enabled": True}} if memo else {}
    config = NCPConfig(values=values, project_root=project)
    return SQLiteStore(project / ".ncp" / "store.db", config=config), config


def _seed(store: SQLiteStore, count: int = 6) -> None:
    for i in range(count):
        store.write(
            SubconsciousChunk(
                chunk_id=f"c{i}",
                layer="semantic",
                # Growing sizes so a tight budget keeps a strict prefix.
                content=f"payment retry policy marker{i} " + "detail " * 30 * (i + 1),
                src="tool_result",
                written_by="worker",
            )
        )


def _delivered(context: str, count: int = 6) -> list[str]:
    return [f"c{i}" for i in range(count) if f"marker{i} " in context]


def _linked(store: SQLiteStore, turn_id: str) -> list[str]:
    with sqlite3.connect(store.path) as conn:
        rows = conn.execute(
            "SELECT chunk_id FROM turn_chunks WHERE turn_id = ? AND relation = 'retrieved' ORDER BY chunk_id",
            (turn_id,),
        ).fetchall()
    return [r[0] for r in rows]


def _latest_turn_id(store: SQLiteStore) -> str:
    with sqlite3.connect(store.path) as conn:
        row = conn.execute("SELECT turn_id FROM turn_records ORDER BY rowid DESC LIMIT 1").fetchone()
    return str(row[0])


def _run_turn(mode: str, store: SQLiteStore, config: NCPConfig, adapter: BaseAdapter, **kwargs) -> str:
    if mode == "run":
        response = ncp.run(agent=_agent(), turn="payment retry policy", adapter=adapter,
                           store=store, config=config, **kwargs)
        return response.turn_id
    "".join(ncp.stream(agent=_agent(), turn="payment retry policy", adapter=adapter,
                       store=store, config=config, **kwargs))
    return _latest_turn_id(store)


@pytest.mark.parametrize("mode", ["run", "stream"])
def test_links_exactly_the_evidence_in_the_model_visible_request(tmp_path: Path, mode: str) -> None:
    store, config = _store(tmp_path)
    _seed(store)
    adapter = _CapturingAdapter()

    turn_id = _run_turn(mode, store, config, adapter)

    delivered = _delivered(adapter.contexts[-1])
    assert delivered, "fixture should retrieve at least one chunk"
    assert _linked(store, turn_id) == sorted(delivered)


@pytest.mark.parametrize("mode", ["run", "stream"])
def test_budget_trimmed_chunks_are_not_credited(tmp_path: Path, mode: str) -> None:
    store, config = _store(tmp_path)
    _seed(store)
    adapter = _CapturingAdapter()

    turn_id = _run_turn(mode, store, config, adapter, max_tokens=300)

    delivered = _delivered(adapter.contexts[-1])
    assert 0 < len(delivered) < 4, delivered  # the budget really trimmed
    assert _linked(store, turn_id) == sorted(delivered)


def test_run_reports_evidence_sources_and_outcome_resolves(tmp_path: Path) -> None:
    store, config = _store(tmp_path)
    _seed(store)
    adapter = _CapturingAdapter()

    response = ncp.run(agent=_agent(), turn="payment retry policy", adapter=adapter, store=store, config=config)

    assert response.evidence is not None
    assert response.evidence.presented is True
    assert response.evidence.chunk_ids == _delivered(adapter.contexts[-1])
    assert set(response.evidence.sources.values()) <= {"retrieval", "graph_expansion"}

    outcome = OutcomeRecord(turn_id=response.turn_id, success=True)
    assert store.record_outcome(outcome) is True
    stored = store.get_outcome(outcome.outcome_id)
    assert stored is not None
    assert sorted(stored.chunk_ids) == sorted(response.evidence.chunk_ids)


def test_empty_retrieval_links_nothing_synthetic(tmp_path: Path) -> None:
    store, config = _store(tmp_path)
    adapter = _CapturingAdapter()

    response = ncp.run(agent=_agent(), turn="payment retry policy", adapter=adapter, store=store, config=config)

    assert response.evidence is not None
    assert response.evidence.chunk_ids == []
    # The cold-start chunk is delivered but is not a stored chunk.
    assert "cold_start" in response.evidence.sources.values()
    assert _linked(store, response.turn_id) == []


def test_memo_hit_records_cached_evidence_without_presenting_it(tmp_path: Path) -> None:
    store, config = _store(tmp_path, memo=True)
    _seed(store)
    adapter = _CapturingAdapter()

    first = ncp.run(agent=_agent(), turn="payment retry policy", adapter=adapter, store=store, config=config)
    second = ncp.run(agent=_agent(), turn="payment retry policy", adapter=adapter, store=store, config=config)

    assert len(adapter.contexts) == 1  # second turn was served from the memo
    assert first.evidence is not None and first.evidence.presented is True
    assert second.cost_source == "memoized"
    assert second.evidence is not None
    assert second.evidence.presented is False
    assert second.evidence.chunk_ids == []
    # The memo carries the evidence its cached result was built from.
    assert second.evidence.memo_chunk_ids == first.evidence.chunk_ids
    assert _linked(store, second.turn_id) == []


def test_memo_hit_from_evidence_free_memo(tmp_path: Path) -> None:
    store, config = _store(tmp_path, memo=True)
    adapter = _CapturingAdapter()

    ncp.run(agent=_agent(), turn="payment retry policy", adapter=adapter, store=store, config=config)
    second = ncp.run(agent=_agent(), turn="payment retry policy", adapter=adapter, store=store, config=config)

    assert second.evidence is not None
    assert second.evidence.presented is False
    assert second.evidence.memo_chunk_ids == []


def test_stored_evidence_ids_falls_back_to_synthetic_prefixes() -> None:
    def chunk(chunk_id: str) -> SubconsciousChunk:
        return SubconsciousChunk(chunk_id=chunk_id, layer="episodic", content="x", src="tool_result")

    chunks = [chunk("recent_turn_1"), chunk("cold_pipe"), chunk("c1"), chunk("c1"), chunk("c2")]

    assert stored_evidence_ids(chunks) == ["c1", "c2"]
    sources = {"recent_turn_1": "recent_ref", "cold_pipe": "retrieval", "c1": "graph_expansion", "c2": "retrieval"}
    # With origins, classification is by origin rather than by id shape.
    assert stored_evidence_ids(chunks, sources) == ["cold_pipe", "c1", "c2"]
    assert AssemblyResult.stored_evidence_ids is not None
