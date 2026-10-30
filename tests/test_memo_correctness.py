"""Memo reuse is keyed canonically, gated centrally and invalidated on change.

Regressions covered (ncp.run / ncp.stream):
- the memo key was just agent|task|slot + turn, so a result was reused
  across pipelines, models/providers, goals and constraints, and after the
  evidence it was built from changed;
- reuse ignored [memoization].allow_unverified and min_outcome, which
  ncp_lookup_memo enforced;
- re-recording different content kept the old verification and created_at.
"""

from __future__ import annotations

import os
from pathlib import Path
from uuid import uuid4

import pytest

import ncp
from ncp.adapters.base import BaseAdapter
from ncp.config import NCPConfig
from ncp.stores.memo import (
    compute_dependency_fingerprint,
    compute_turn_memo_signature,
    memo_ineligibility,
)
from ncp.stores.sqlite import SQLiteStore
from ncp.types import SubconsciousChunk

_LIVE = os.getenv("NCP_RUN_PGVECTOR_INTEGRATION") == "1"
_DSN = os.getenv("NCP_PGVECTOR_DSN", "postgresql://postgres:postgres@127.0.0.1:5432/ncp")


class _Counting(BaseAdapter):
    def __init__(self, model: str = "model-a") -> None:
        self._model = model
        self.calls = 0

    def call(self, ncp_context: str, user_turn: str) -> str:
        self.calls += 1
        return f"answer_{self.calls}"

    def stream(self, ncp_context: str, user_turn: str):
        self.calls += 1
        yield f"answer_{self.calls}"


class _OtherProvider(_Counting):
    pass


def _setup(tmp_path: Path, **memo: object) -> tuple[SQLiteStore, NCPConfig]:
    project = tmp_path / "repo"
    (project / ".git").mkdir(parents=True)
    config = NCPConfig(values={"memoization": {"enabled": True, **memo}}, project_root=project)
    return SQLiteStore(project / ".ncp" / "store.db", config=config), config


def _agent(**overrides: object):
    fields = dict(
        id="executor",
        role="build",
        owns=["implementation"],
        must_not=["planning"],
        task="memo_check",
        slot="classify",
        intent="reuse_work",
        pipeline_id="pipe_a",
    )
    fields.update(overrides)
    return ncp.agent(**fields)


def _run(store, config, adapter, **agent_overrides):
    return ncp.run(agent=_agent(**agent_overrides), turn="classify the retry bug", adapter=adapter,
                   store=store, config=config)


# ── signature / fingerprint / gate units ────────────────────────────────────


def _sig(**overrides: object) -> str:
    fields: dict = dict(
        turn="t", agent_id="a", role="r", pipeline_id="p", task="k", slot="s", intent="i",
        goal_version=1, owns=["x"], must_not=["y"], model="m", provider="P", dependency_fingerprint="d",
    )
    fields.update(overrides)
    return compute_turn_memo_signature(**fields)


@pytest.mark.parametrize(
    "change",
    [
        {"pipeline_id": "other"}, {"model": "m2"}, {"provider": "Q"}, {"goal_version": 2},
        {"owns": ["z"]}, {"must_not": []}, {"intent": "j"}, {"role": "review"},
        {"dependency_fingerprint": "d2"},
    ],
)
def test_signature_separates_every_keyed_dimension(change: dict) -> None:
    assert _sig(**change) != _sig()


def test_signature_normalizes_turn_and_constraint_order() -> None:
    assert _sig(turn="  Fix   THE bug ") == _sig(turn="fix the bug")
    assert _sig(owns=["b", "a"]) == _sig(owns=["a", "b"])


def test_dependency_fingerprint_tracks_content_whispers_and_policy() -> None:
    base = compute_dependency_fingerprint(evidence=[("c1", "alpha"), ("c2", "beta")])
    assert base == compute_dependency_fingerprint(evidence=[("c2", "beta"), ("c1", "alpha")])
    assert base != compute_dependency_fingerprint(evidence=[("c1", "alpha EDITED"), ("c2", "beta")])
    assert base != compute_dependency_fingerprint(evidence=[("c1", "alpha"), ("c2", "beta")], whisper_ids=["w"])
    assert base != compute_dependency_fingerprint(evidence=[("c1", "alpha"), ("c2", "beta")], policy_sha256="h")


def test_memo_ineligibility_reasons() -> None:
    good = {"result_summary": "x", "outcome": 1.0, "verified": 1}
    assert memo_ineligibility(good, min_outcome=0.5, allow_unverified=False) is None
    assert memo_ineligibility({**good, "verified": 0}, min_outcome=0.0, allow_unverified=False) == "unverified"
    assert memo_ineligibility({**good, "verified": 0}, min_outcome=0.0, allow_unverified=True) is None
    assert memo_ineligibility({**good, "outcome": 0.2}, min_outcome=0.5, allow_unverified=True) == "outcome_below_min"
    assert memo_ineligibility({**good, "result_summary": None}, min_outcome=0.0, allow_unverified=True) == "empty_result"
    assert memo_ineligibility({"result_summary": None, "verified": 1}, min_outcome=0.0,
                              allow_unverified=False, require_result=False) is None


# ── ncp.run reuse behavior ──────────────────────────────────────────────────


def test_unverified_memo_is_not_reused_by_default(tmp_path: Path) -> None:
    store, config = _setup(tmp_path)
    adapter = _Counting()

    first = _run(store, config, adapter)
    second = _run(store, config, adapter)

    assert adapter.calls == 2
    assert second.cost_source != "memoized"
    assert first.evidence is not None and first.evidence.memo_signature
    assert store.memo_stats()["hits"] == 0


def test_verified_memo_is_reused_via_reported_signature(tmp_path: Path) -> None:
    store, config = _setup(tmp_path)
    adapter = _Counting()

    first = _run(store, config, adapter)
    assert first.evidence is not None
    store.update_memo_outcome(first.evidence.memo_signature, 1.0, verified=True)
    second = _run(store, config, adapter)

    assert adapter.calls == 1
    assert second.cost_source == "memoized"
    assert second.content == first.content


def test_failed_outcome_memo_is_not_reused(tmp_path: Path) -> None:
    store, config = _setup(tmp_path, min_outcome=0.5)
    adapter = _Counting()

    first = _run(store, config, adapter)
    assert first.evidence is not None
    store.update_memo_outcome(first.evidence.memo_signature, 0.0, verified=True)
    _run(store, config, adapter)

    assert adapter.calls == 2


@pytest.mark.parametrize(
    ("label", "second"),
    [
        ("pipeline", lambda s, c: _run(s, c, _Counting(), pipeline_id="pipe_b")),
        ("model", lambda s, c: _run(s, c, _Counting(model="model-b"))),
        ("provider", lambda s, c: _run(s, c, _OtherProvider())),
        ("constraints", lambda s, c: _run(s, c, _Counting(), must_not=["planning", "deploys"])),
    ],
)
def test_no_false_hit_across_keyed_dimensions(tmp_path: Path, label: str, second) -> None:
    store, config = _setup(tmp_path, allow_unverified=True)
    _run(store, config, _Counting())

    response = second(store, config)

    assert response.cost_source != "memoized", label


def test_changed_evidence_invalidates_memo(tmp_path: Path) -> None:
    store, config = _setup(tmp_path, allow_unverified=True)
    store.write(SubconsciousChunk(
        chunk_id="dep_1", layer="semantic", content="retry bug classify policy v1",
        src="tool_result", pipeline_id="pipe_a", written_by="w",
    ))
    adapter = _Counting()

    first = _run(store, config, adapter)
    assert first.evidence is not None and first.evidence.chunk_ids == ["dep_1"]
    assert _run(store, config, adapter).cost_source == "memoized"

    store.write(SubconsciousChunk(
        chunk_id="dep_2", layer="semantic", content="retry bug classify policy amended for v2",
        src="tool_result", pipeline_id="pipe_a", written_by="w",
    ))
    third = _run(store, config, adapter)

    assert third.cost_source != "memoized"
    assert adapter.calls == 2


def test_stream_uses_the_same_gate(tmp_path: Path) -> None:
    store, config = _setup(tmp_path)
    adapter = _Counting()

    for _ in range(2):
        "".join(ncp.stream(agent=_agent(), turn="classify the retry bug", adapter=adapter,
                           store=store, config=config))

    assert adapter.calls == 2  # unverified: never reused


# ── replacement invalidation, all backends ──────────────────────────────────


def _replacement_roundtrip(record, update, peek) -> None:
    record("sig_r", "task", ["c1"], "first")
    update("sig_r", 0.9, verified=True)
    created = peek("sig_r")["created_at"]

    record("sig_r", "task", ["c1"], "first")  # identical: keep review state
    same = peek("sig_r")
    assert (same["verified"], same["outcome"], same["created_at"]) == (1, pytest.approx(0.9), created)

    record("sig_r", "task", ["c1"], "second")  # replaced: reset review state
    replaced = peek("sig_r")
    assert replaced["result_summary"] == "second"
    assert (replaced["verified"], replaced["outcome"]) == (0, 0.0)
    assert replaced["created_at"] >= created


def test_sqlite_memo_replacement_resets_review_state(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "ncp.db")
    _replacement_roundtrip(store.record_memo, store.update_memo_outcome, store.peek_memo)


def _require_live() -> None:
    if not _LIVE:
        pytest.skip("set NCP_RUN_PGVECTOR_INTEGRATION=1 to run live pgvector tests")
    pytest.importorskip("psycopg")
    pytest.importorskip("psycopg_pool")


def test_pgvector_memo_replacement_resets_review_state() -> None:
    _require_live()
    from ncp.stores.pgvector import PgvectorStore

    store = PgvectorStore(_DSN, schema=f"ncp_memo_{uuid4().hex[:8]}", table_prefix="it_")
    _replacement_roundtrip(store.record_memo, store.update_memo_outcome, store.peek_memo)


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.mark.anyio
async def test_async_pgvector_memo_replacement_resets_review_state() -> None:
    _require_live()
    from ncp.stores.pgvector_async import AsyncPgvectorStore

    store = AsyncPgvectorStore(_DSN, schema=f"ncp_amemo_{uuid4().hex[:8]}", table_prefix="it_")
    try:
        await store.async_record_memo("sig_r", "task", ["c1"], "first")
        await store.async_update_memo_outcome("sig_r", 0.9, verified=True)
        created = (await store.async_peek_memo("sig_r"))["created_at"]
        await store.async_record_memo("sig_r", "task", ["c1"], "first")
        same = await store.async_peek_memo("sig_r")
        assert (same["verified"], same["created_at"]) == (1, created)
        await store.async_record_memo("sig_r", "task", ["c1"], "second")
        replaced = await store.async_peek_memo("sig_r")
        assert (replaced["verified"], replaced["outcome"]) == (0, 0.0)
    finally:
        await store.close()
