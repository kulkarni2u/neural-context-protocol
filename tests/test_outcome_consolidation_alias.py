"""Outcome feedback survives equivalent consolidation (loser -> keeper aliases).

Regression: an outcome naming a chunk that consolidation later merged away
was treated as settled (the loser is tombstoned), so the feedback was
consumed without ever crediting the surviving keeper.

Every scenario runs against SQLite, and against live sync and async pgvector
when NCP_RUN_PGVECTOR_INTEGRATION=1.
"""

from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest

from ncp.stores.calibration import (
    EQUIVALENT_CONSOLIDATION,
    apply_outcome_aliases,
    resolve_chunk_alias,
    validate_chunk_alias,
)
from ncp.stores.sqlite import SQLiteStore
from ncp.types import OutcomeRecord, SubconsciousChunk


_LIVE = os.getenv("NCP_RUN_PGVECTOR_INTEGRATION") == "1"
_DSN = os.getenv("NCP_PGVECTOR_DSN", "postgresql://postgres:postgres@127.0.0.1:5432/ncp")

KEEPER = "auth token refresh logic handles expiry and rotation"
LOSER_1 = "auth token refresh logic handles expiry"
LOSER_2 = "auth token refresh logic handles expiry for sessions"


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


# ── pure helpers ────────────────────────────────────────────────────────────


def test_validate_chunk_alias_only_accepts_equivalent_consolidation() -> None:
    validate_chunk_alias("loser", "keeper", EQUIVALENT_CONSOLIDATION)
    for reason in ("supersedes", "contradicts", "caused_by", ""):
        with pytest.raises(ValueError):
            validate_chunk_alias("loser", "keeper", reason)
    with pytest.raises(ValueError):
        validate_chunk_alias("same", "same", EQUIVALENT_CONSOLIDATION)


def test_resolve_chunk_alias_follows_chains_and_stops_on_cycles() -> None:
    assert resolve_chunk_alias("a", {"a": "b", "b": "c"}) == "c"
    assert resolve_chunk_alias("x", {"a": "b"}) == "x"
    assert resolve_chunk_alias("a", {"a": "b", "b": "a"}) == "b"


def test_apply_outcome_aliases_dedups_and_carries_prior_applications() -> None:
    outcomes = [
        OutcomeRecord(outcome_id="o1", chunk_ids=["l1", "l2", "k"], success=True),
        OutcomeRecord(outcome_id="o2", chunk_ids=["other"], success=True),
    ]
    effective, pairs, audit = apply_outcome_aliases(
        outcomes, {"l1": "k", "l2": "k"}, {("o3", "l1")}
    )

    assert effective[0].chunk_ids == ["k"]
    assert effective[1] is outcomes[1]  # untouched outcomes are not copied
    assert outcomes[0].chunk_ids == ["l1", "l2", "k"]  # originals preserved
    assert ("o3", "k") in pairs and ("o3", "l1") in pairs
    assert [(a["original_chunk_id"], a["effective_chunk_id"]) for a in audit] == [("l1", "k"), ("l2", "k")]


# ── backend facade ──────────────────────────────────────────────────────────


class _Backend:
    """Uniform async surface over SQLite, sync pgvector and async pgvector."""

    def __init__(self, kind: str, tmp_path: Path) -> None:
        self.kind = kind
        if kind == "sqlite":
            self.store: Any = SQLiteStore(tmp_path / "ncp.db")
            self._prefix = ""
            return
        schema = f"ncp_alias_{uuid4().hex[:8]}"
        if kind == "pg_sync":
            from ncp.stores.pgvector import PgvectorStore

            self.store = PgvectorStore(_DSN, schema=schema, table_prefix="it_")
        else:
            from ncp.stores.pgvector_async import AsyncPgvectorStore

            self.store = AsyncPgvectorStore(_DSN, schema=schema, table_prefix="it_")
        self._prefix = f"{schema}.it_"

    async def _call(self, name: str, *args: Any, **kwargs: Any) -> Any:
        if self.kind == "pg_async":
            return await getattr(self.store, f"async_{name}")(*args, **kwargs)
        return getattr(self.store, name)(*args, **kwargs)

    async def write(self, cid: str, content: str, trust: float, pipeline: str = "P") -> None:
        chunk = SubconsciousChunk(
            chunk_id=cid,
            content=content,
            layer="semantic",
            zone="working",
            pipeline_id=pipeline,
            src="agent_inferred",
            written_by="tester",
            base_trust=trust,
            chunk_type="prose",
            scope="pipeline",
        )
        assert await self._call("write", chunk) is True

    async def outcome(self, oid: str, chunk_ids: list[str], *, success: bool = True) -> None:
        record = OutcomeRecord(outcome_id=oid, chunk_ids=chunk_ids, success=success, weight=0.2)
        assert await self._call("record_outcome", record) is True

    async def consolidate(self, pipeline: str = "P") -> Any:
        return await self._call("consolidate", pipeline_id=pipeline)

    async def calibrate(self, pipeline: str | None = "P") -> Any:
        return await self._call("calibrate", pipeline_id=pipeline, feedback_mode=True)

    async def supersede(self, old: str, new: str) -> None:
        assert await self._call("supersede", old, new) is True

    def _query(self, sql: str, params: tuple = ()) -> list[tuple]:
        sql = sql.replace("T.", self._prefix)
        if self.kind == "sqlite":
            with sqlite3.connect(self.store.path) as conn:
                return [tuple(r) for r in conn.execute(sql, params).fetchall()]
        import psycopg

        with psycopg.connect(_DSN) as conn:
            return [tuple(r) for r in conn.execute(sql.replace("?", "%s"), params).fetchall()]

    def trust(self, cid: str) -> float:
        return float(self._query("SELECT base_trust FROM T.chunks WHERE chunk_id = ?", (cid,))[0][0])

    def applications(self) -> set[tuple[str, str]]:
        return {(r[0], r[1]) for r in self._query("SELECT outcome_id, chunk_id FROM T.outcome_applications")}

    def consumed(self, oid: str) -> bool:
        return bool(self._query("SELECT consumed FROM T.outcomes WHERE outcome_id = ?", (oid,))[0][0])

    def stored_chunk_ids(self, oid: str) -> list[str]:
        return json.loads(self._query("SELECT chunk_ids FROM T.outcomes WHERE outcome_id = ?", (oid,))[0][0])

    async def close(self) -> None:
        if self.kind == "pg_async":
            await self.store.close()


_KINDS = ["sqlite", "pg_sync", "pg_async"]


@pytest.fixture
async def backend(request: pytest.FixtureRequest, tmp_path: Path):
    kind = request.param
    if kind != "sqlite":
        if not _LIVE:
            pytest.skip("set NCP_RUN_PGVECTOR_INTEGRATION=1 to run live pgvector tests")
        pytest.importorskip("psycopg")
        pytest.importorskip("psycopg_pool")
    b = _Backend(kind, tmp_path)
    try:
        yield b
    finally:
        await b.close()


def _alias_audit(report: Any) -> list[tuple[str, str, str]]:
    return [
        (e["outcome_id"], e["original_chunk_id"], e["effective_chunk_id"])
        for e in report.change_log
        if e.get("reason") == "outcome_alias"
    ]


# ── scenarios ───────────────────────────────────────────────────────────────


@pytest.mark.anyio
@pytest.mark.parametrize("backend", _KINDS, indirect=True)
async def test_outcome_on_merged_loser_credits_keeper(backend: _Backend) -> None:
    await backend.write("K", KEEPER, 0.6)
    await backend.write("L", LOSER_1, 0.5)
    await backend.outcome("o1", ["L"])

    report = await backend.consolidate()
    assert report.merge_log[0]["kept"] == "K" and report.merge_log[0]["merged"] == ["L"]

    cal = await backend.calibrate()
    assert backend.trust("K") == pytest.approx(0.8)
    assert backend.consumed("o1") is True
    assert backend.applications() == {("o1", "K")}
    assert _alias_audit(cal) == [("o1", "L", "K")]
    # The stored outcome keeps the original evidence for audit.
    assert backend.stored_chunk_ids("o1") == ["L"]

    # Repeated calibration is a no-op.
    await backend.calibrate()
    assert backend.trust("K") == pytest.approx(0.8)


@pytest.mark.anyio
@pytest.mark.parametrize("backend", _KINDS, indirect=True)
async def test_multiple_losers_credit_keeper_once_per_outcome(backend: _Backend) -> None:
    await backend.write("K", KEEPER, 0.6)
    await backend.write("L1", LOSER_1, 0.5)
    await backend.write("L2", LOSER_2, 0.5)
    await backend.outcome("o1", ["L1", "L2", "K"])

    report = await backend.consolidate()
    assert sorted(report.merge_log[0]["merged"]) == ["L1", "L2"]

    await backend.calibrate()
    assert backend.trust("K") == pytest.approx(0.8)  # +0.2 once, not 3x
    assert backend.applications() == {("o1", "K")}


@pytest.mark.anyio
@pytest.mark.parametrize("backend", _KINDS, indirect=True)
async def test_feedback_applied_before_merge_is_not_reapplied_to_keeper(backend: _Backend) -> None:
    await backend.write("K", KEEPER, 0.8)
    await backend.write("L", LOSER_1, 0.5)
    await backend.write("B", "billing export runs nightly at two", 0.5, pipeline="B")
    # Spans two pipelines so calibrating P applies (o1, L) but leaves o1 open.
    await backend.outcome("o1", ["L", "B"])

    await backend.calibrate()
    assert backend.trust("L") == pytest.approx(0.7)
    assert backend.consumed("o1") is False

    report = await backend.consolidate()
    assert report.merge_log[0]["kept"] == "K"
    await backend.calibrate()
    # L's credit was already applied; aliasing it to K must not re-apply it.
    assert backend.trust("K") == pytest.approx(0.8)


@pytest.mark.anyio
@pytest.mark.parametrize("backend", _KINDS, indirect=True)
async def test_outcome_before_and_after_consolidation(backend: _Backend) -> None:
    await backend.write("K", KEEPER, 0.6)
    await backend.write("L", LOSER_1, 0.5)
    await backend.outcome("before", ["L"])
    await backend.consolidate()
    await backend.outcome("after", ["K"])

    await backend.calibrate()
    assert backend.trust("K") == pytest.approx(1.0)
    assert backend.applications() == {("before", "K"), ("after", "K")}


@pytest.mark.anyio
@pytest.mark.parametrize("backend", _KINDS, indirect=True)
async def test_supersession_does_not_transfer_feedback(backend: _Backend) -> None:
    await backend.write("old", "deploy window is tuesday afternoon", 0.5)
    await backend.write("new", "deploy freeze starts friday at noon", 0.5)
    await backend.supersede("old", "new")
    await backend.outcome("o1", ["old"], success=False)

    await backend.calibrate()
    assert backend.trust("new") == pytest.approx(0.5)
    assert ("o1", "new") not in backend.applications()


@pytest.mark.anyio
@pytest.mark.parametrize("backend", _KINDS, indirect=True)
async def test_alias_respects_pipeline_scoped_calibration(backend: _Backend) -> None:
    await backend.write("K", KEEPER, 0.6)
    await backend.write("L", LOSER_1, 0.5)
    await backend.write("B", "billing export runs nightly at two", 0.5, pipeline="B")
    await backend.outcome("o1", ["L"])
    await backend.consolidate()

    await backend.calibrate(pipeline="B")
    assert backend.trust("K") == pytest.approx(0.6)
    assert backend.consumed("o1") is False

    await backend.calibrate(pipeline="P")
    assert backend.trust("K") == pytest.approx(0.8)
    assert backend.consumed("o1") is True
