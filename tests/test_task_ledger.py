"""Coordination primitives C: the task ledger.

See docs/specs/2026-11-02-ncp-coordination-primitives-design.md section 5.
"""

from __future__ import annotations

import http.client
import importlib.util
import json
import os
import re
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from ncp.config import load_config
from ncp.encoder import PidginEncoder
from ncp.mcp.server import (
    CORE_TOOL_NAMES,
    _handle_request,
    create_http_server,
    make_handlers,
    tools_for_config,
)
from ncp.notify import InProcessNotifier, NotifyEvent
from ncp.stores import sqlite as sqlite_module
from ncp.stores.base import BaseStore, NCPStoreError
from ncp.stores.migrations import MigrationRunner
from ncp.stores.pgvector import PGVECTOR_SCHEMA_TEMPLATE, PgvectorStore
from ncp.stores.sqlite import SQLiteStore
from ncp.types import TaskRecord, Whisper

PIPE = "pipe_tasks"
OTHER_PIPE = "pipe_other"
MIGRATION = Path(__file__).resolve().parents[1] / "ncp" / "migrations" / "018_add_tasks.sql"


def _record(**overrides: Any) -> TaskRecord:
    fields: dict[str, Any] = {
        "pipeline_id": PIPE,
        "slot": "review",
        "title": "review the diff",
        "created_by": "router",
    }
    fields.update(overrides)
    return TaskRecord(**fields)


@pytest.fixture
def store(tmp_path: Path) -> SQLiteStore:
    return SQLiteStore(tmp_path / "ledger.db")


class _Clock:
    """Controllable replacement for ``time.time`` (no sleeping in lease tests)."""

    def __init__(self) -> None:
        self.now = time.time()

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> _Clock:
    fake = _Clock()
    monkeypatch.setattr(time, "time", fake)
    return fake


# ---------------------------------------------------------------------------
# Model


class TestTaskRecordValidation:
    def test_defaults_and_generated_id(self) -> None:
        task = _record()
        assert re.fullmatch(r"tsk_[0-9a-f]{12}", task.task_id)
        assert task.status == "open"
        assert task.attempt == 0 and task.max_attempts == 3 and task.version == 0
        assert task.result_chunk_ids == []
        assert _record().task_id != task.task_id

    @pytest.mark.parametrize(
        "field",
        ["task_id", "pipeline_id", "slot", "created_by", "owner", "lease_token", "dedupe_key",
         "parent_task_id", "origin_whisper_id", "checkpoint_chunk_id", "result_turn_id"],
    )
    def test_id_fields_reject_whitespace(self, field: str) -> None:
        with pytest.raises(ValueError, match="whitespace"):
            _record(**{field: "has space"})

    def test_title_bounds(self) -> None:
        assert _record(title="x" * 200).title == "x" * 200
        with pytest.raises(ValueError, match="200"):
            _record(title="x" * 201)
        with pytest.raises(ValueError, match="empty"):
            _record(title="   ")

    def test_error_bound(self) -> None:
        assert _record(error="e" * 300).error == "e" * 300
        with pytest.raises(ValueError, match="300"):
            _record(error="e" * 301)

    def test_result_chunk_ids_bounds(self) -> None:
        assert len(_record(result_chunk_ids=[f"c{i}" for i in range(10)]).result_chunk_ids) == 10
        with pytest.raises(ValueError, match="at most 10"):
            _record(result_chunk_ids=[f"c{i}" for i in range(11)])
        with pytest.raises(ValueError, match="whitespace"):
            _record(result_chunk_ids=["bad id"])

    def test_attempt_bounds(self) -> None:
        with pytest.raises(ValueError, match="attempt"):
            _record(attempt=-1)
        with pytest.raises(ValueError, match="max_attempts"):
            _record(max_attempts=0)
        assert _record(attempt=0, max_attempts=1).max_attempts == 1

    def test_unknown_status_rejected(self) -> None:
        with pytest.raises(ValueError):
            _record(status="paused")

    def test_public_dict_never_carries_the_lease_token(self) -> None:
        task = _record(lease_token="secret")
        assert "lease_token" not in task.public_dict()
        assert task.model_dump()["lease_token"] == "secret"

    def test_whisper_task_id_no_spaces(self) -> None:
        with pytest.raises(ValueError, match="whitespace"):
            Whisper(from_agent="a", target="b", whisper_type="nudge", payload="x",
                    confidence=0.9, task_id="bad id")


class TestWhisperLinkageRule:
    def test_progress_and_result_accept_task_id_alone(self) -> None:
        progress = Whisper(from_agent="w", target="r", whisper_type="progress",
                           payload={"status": "working"}, confidence=0.9, task_id="tsk_1")
        result = Whisper(from_agent="w", target="r", whisper_type="result",
                         payload={"status": "succeeded", "summary": "ok"}, confidence=0.9,
                         task_id="tsk_1")
        assert progress.task_id == result.task_id == "tsk_1"

    def test_no_linkage_still_rejected(self) -> None:
        with pytest.raises(ValueError, match="in_reply_to, thread_id or task_id"):
            Whisper(from_agent="w", target="r", whisper_type="result",
                    payload={"status": "succeeded", "summary": "ok"}, confidence=0.9)


# ---------------------------------------------------------------------------
# Store defaults


class TestStoreDefaults:
    def test_base_defaults_raise_store_error(self) -> None:
        class Minimal(BaseStore):  # only what ABC demands is irrelevant: call unbound defaults
            pass

        for name, args in (
            ("create_task", (_record(),)),
            ("get_task", ("tsk_x",)),
            ("list_tasks", ()),
            ("expire_task_leases", ()),
            ("claim_task", ()),
        ):
            with pytest.raises(NCPStoreError, match="task ledger not supported"):
                getattr(BaseStore, name)(object(), *args) if name != "claim_task" else BaseStore.claim_task(
                    object(), agent_id="a"
                )

    def test_fake_store_without_ledger_keeps_working(self) -> None:
        """A store that never implemented the ledger is still instantiable."""
        abstract = getattr(BaseStore, "__abstractmethods__", frozenset())
        assert not {n for n in abstract if "task" in n}


# ---------------------------------------------------------------------------
# State machine (SQLite)


class TestStateMachine:
    def test_full_happy_path(self, store: SQLiteStore) -> None:
        created, was_created = store.create_task(_record())
        assert was_created and created.status == "open"
        claimed = store.claim_task(agent_id="worker", task_id=created.task_id, lease_seconds=60, now=1000.0)
        assert claimed is not None
        assert claimed.status == "claimed" and claimed.owner == "worker"
        assert claimed.attempt == 1 and claimed.version == 1
        assert claimed.lease_expires_at == 1060.0 and claimed.heartbeat_at == 1000.0
        assert re.fullmatch(r"[0-9a-f]{32}", claimed.lease_token or "")

        beat = store.heartbeat_task(
            created.task_id, claimed.lease_token, lease_seconds=60,
            checkpoint_chunk_id="chk_1", now=1030.0,
        )
        assert beat.ok and beat.task is not None
        assert beat.task.lease_expires_at == 1090.0 and beat.task.checkpoint_chunk_id == "chk_1"
        assert beat.task.version == 2

        done = store.complete_task(
            created.task_id, claimed.lease_token, status="done", summary="fine",
            result_chunk_ids=["chk_2"], turn_id="turn_9", now=1040.0,
        )
        assert done.ok and done.task is not None
        assert done.task.status == "done" and done.task.lease_token is None
        assert done.task.owner == "worker" and done.task.result_chunk_ids == ["chk_2"]
        assert done.task.result_turn_id == "turn_9" and done.task.version == 3
        assert done.task.checkpoint_chunk_id == "chk_1"

    def test_complete_failed_keeps_summary_as_error(self, store: SQLiteStore) -> None:
        task, _ = store.create_task(_record())
        claimed = store.claim_task(agent_id="w", task_id=task.task_id, now=1.0)
        assert claimed is not None
        result = store.complete_task(task.task_id, claimed.lease_token, status="failed",
                                     summary="x" * 400, now=2.0)
        assert result.ok and result.task is not None
        assert result.task.status == "failed" and result.task.error == "x" * 300

    def test_complete_rejects_non_terminal_status(self, store: SQLiteStore) -> None:
        with pytest.raises(ValueError):
            store.complete_task("tsk_x", "tok", status="open")  # type: ignore[arg-type]

    def test_release_returns_to_open_and_attempt_counts(self, store: SQLiteStore) -> None:
        task, _ = store.create_task(_record())
        first = store.claim_task(agent_id="w1", task_id=task.task_id, now=1.0)
        assert first is not None
        released = store.release_task(task.task_id, first.lease_token, error="blocked", now=2.0)
        assert released.ok and released.task is not None
        assert released.task.status == "open" and released.task.owner is None
        assert released.task.lease_token is None and released.task.attempt == 1
        assert released.task.error == "blocked"
        second = store.claim_task(agent_id="w2", task_id=task.task_id, now=3.0)
        assert second is not None and second.attempt == 2 and second.owner == "w2"
        assert second.lease_token != first.lease_token

    def test_release_at_max_attempts_fails_instead_of_reopening(self, store: SQLiteStore) -> None:
        task, _ = store.create_task(_record(max_attempts=1))
        claimed = store.claim_task(agent_id="w", task_id=task.task_id, now=1.0)
        assert claimed is not None
        released = store.release_task(task.task_id, claimed.lease_token, now=2.0)
        assert released.ok and released.task is not None
        assert released.task.status == "failed"
        assert released.task.error == "max_attempts_exhausted"

    def test_terminal_states_cannot_be_claimed_or_mutated(self, store: SQLiteStore) -> None:
        task, _ = store.create_task(_record())
        claimed = store.claim_task(agent_id="w", task_id=task.task_id, now=1.0)
        assert claimed is not None
        store.complete_task(task.task_id, claimed.lease_token, status="done", summary="ok", now=2.0)
        assert store.claim_task(agent_id="w2", task_id=task.task_id, now=3.0) is None
        for op in (
            store.heartbeat_task(task.task_id, claimed.lease_token, now=3.0),
            store.release_task(task.task_id, claimed.lease_token, now=3.0),
            store.complete_task(task.task_id, claimed.lease_token, status="done", summary="again", now=3.0),
        ):
            assert not op.ok and op.reason == "lease_lost"
        assert store.get_task(task.task_id, now=4.0).status == "done"  # type: ignore[union-attr]

    def test_claim_requires_task_id_or_pipeline(self, store: SQLiteStore) -> None:
        with pytest.raises(ValueError):
            store.claim_task(agent_id="w")

    def test_unknown_task_results(self, store: SQLiteStore) -> None:
        assert store.get_task("tsk_missing") is None
        assert store.heartbeat_task("tsk_missing", "tok").reason == "not_found"
        assert store.cancel_task("tsk_missing", "router").reason == "not_found"
        assert store.claim_task(agent_id="w", task_id="tsk_missing") is None

    def test_version_increments_on_every_mutation(self, store: SQLiteStore) -> None:
        task, _ = store.create_task(_record())
        versions = [task.version]
        claimed = store.claim_task(agent_id="w", task_id=task.task_id, now=1.0)
        versions.append(claimed.version)  # type: ignore[union-attr]
        versions.append(store.heartbeat_task(task.task_id, claimed.lease_token, now=2.0).task.version)  # type: ignore[union-attr]
        versions.append(store.release_task(task.task_id, claimed.lease_token, now=3.0).task.version)  # type: ignore[union-attr]
        assert versions == [0, 1, 2, 3]

    def test_claim_next_is_oldest_first_and_filters_slot(self, store: SQLiteStore) -> None:
        a, _ = store.create_task(_record(slot="review", created_at=10.0, updated_at=10.0))
        b, _ = store.create_task(_record(slot="build", created_at=5.0, updated_at=5.0))
        c, _ = store.create_task(_record(slot="review", created_at=20.0, updated_at=20.0))
        d, _ = store.create_task(_record(pipeline_id=OTHER_PIPE, slot="review", created_at=1.0, updated_at=1.0))
        got = store.claim_task(agent_id="w", pipeline_id=PIPE, slot="review", now=100.0)
        assert got is not None and got.task_id == a.task_id
        got = store.claim_task(agent_id="w", pipeline_id=PIPE, slot="review", now=100.0)
        assert got is not None and got.task_id == c.task_id
        assert store.claim_task(agent_id="w", pipeline_id=PIPE, slot="review", now=100.0) is None
        got = store.claim_task(agent_id="w", pipeline_id=PIPE, now=100.0)
        assert got is not None and got.task_id == b.task_id
        other = store.claim_task(agent_id="w", pipeline_id=OTHER_PIPE, now=100.0)
        assert other is not None and other.task_id == d.task_id

    def test_list_is_bounded_oldest_first_and_filtered(self, store: SQLiteStore) -> None:
        for i in range(60):
            store.create_task(_record(title=f"t{i}", created_at=100.0 + i, updated_at=100.0 + i,
                                      slot="even" if i % 2 == 0 else "odd"))
        everything = store.list_tasks(pipeline_id=PIPE, limit=500)
        assert len(everything) == 50
        assert [t.title for t in everything[:3]] == ["t0", "t1", "t2"]
        assert len(store.list_tasks(pipeline_id=PIPE, slot="odd", limit=10)) == 10
        assert store.list_tasks(pipeline_id=PIPE, status="claimed") == []
        claimed = store.claim_task(agent_id="w9", pipeline_id=PIPE, now=1.0)
        assert claimed is not None
        assert [t.task_id for t in store.list_tasks(pipeline_id=PIPE, owner="w9", now=2.0)] == [claimed.task_id]
        assert store.list_tasks(pipeline_id=OTHER_PIPE) == []


# ---------------------------------------------------------------------------
# Atomic claim


def _run_threads(count: int, target: Any) -> list[Any]:
    barrier = threading.Barrier(count)
    results: list[Any] = [None] * count
    errors: list[BaseException] = []

    def runner(index: int) -> None:
        try:
            barrier.wait(timeout=10)
            results[index] = target(index)
        except BaseException as exc:  # noqa: BLE001 - surfaced below
            errors.append(exc)

    threads = [threading.Thread(target=runner, args=(i,), daemon=True) for i in range(count)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)
    assert not any(t.is_alive() for t in threads), "claim threads hung"
    assert not errors, errors
    return results


@pytest.fixture(params=["returning", "fallback"])
def claim_mode(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> str:
    """Run claim tests on the RETURNING path and on the pre-3.35 fallback path."""
    if request.param == "fallback":
        monkeypatch.setattr(sqlite_module, "_SQLITE_HAS_RETURNING", False)
    else:
        if sqlite3.sqlite_version_info < (3, 35):
            pytest.skip("linked SQLite has no RETURNING")
    return str(request.param)


class TestConcurrentClaim:
    def test_one_winner_per_task(self, store: SQLiteStore, claim_mode: str) -> None:
        task, _ = store.create_task(_record())
        results = _run_threads(
            10, lambda i: store.claim_task(agent_id=f"w{i}", task_id=task.task_id, lease_seconds=60)
        )
        winners = [r for r in results if r is not None]
        assert len(winners) == 1
        final = store.get_task(task.task_id)
        assert final is not None
        assert final.owner == winners[0].owner and final.attempt == 1 and final.version == 1
        assert final.status == "claimed"

    def test_claim_next_never_double_claims(self, store: SQLiteStore, claim_mode: str) -> None:
        count = 24
        ids = {store.create_task(_record(title=f"job {i}"))[0].task_id for i in range(count)}

        def drain(i: int) -> list[TaskRecord]:
            mine: list[TaskRecord] = []
            while True:
                got = store.claim_task(agent_id=f"w{i}", pipeline_id=PIPE, lease_seconds=60)
                if got is None:
                    return mine
                mine.append(got)

        results = _run_threads(8, drain)
        claimed = [t for batch in results for t in batch]
        assert len(claimed) == count
        assert {t.task_id for t in claimed} == ids
        assert len({t.lease_token for t in claimed}) == count
        for task in store.list_tasks(pipeline_id=PIPE, limit=50):
            assert task.status == "claimed" and task.attempt == 1

    def test_claim_token_returned_matches_stored_owner(self, store: SQLiteStore, claim_mode: str) -> None:
        task, _ = store.create_task(_record())
        claimed = store.claim_task(agent_id="w", task_id=task.task_id, now=5.0)
        assert claimed is not None
        # The token the claimer holds is the one that fences later calls.
        assert store.heartbeat_task(task.task_id, claimed.lease_token, now=6.0).ok


# ---------------------------------------------------------------------------
# Fencing + lazy expiry


class TestFencingAndExpiry:
    def test_stale_token_rejected_after_expiry_and_reclaim(self, store: SQLiteStore) -> None:
        task, _ = store.create_task(_record())
        first = store.claim_task(agent_id="slow", task_id=task.task_id, lease_seconds=10, now=100.0)
        assert first is not None
        # The lease lapses and a second worker takes over.
        second = store.claim_task(agent_id="fast", task_id=task.task_id, lease_seconds=10, now=120.0)
        assert second is not None and second.owner == "fast" and second.attempt == 2
        assert second.lease_token != first.lease_token
        # The slow worker wakes up and tries every owner action with its old token.
        for result in (
            store.heartbeat_task(task.task_id, first.lease_token, now=121.0),
            store.release_task(task.task_id, first.lease_token, now=121.0),
            store.complete_task(task.task_id, first.lease_token, status="done", summary="late", now=121.0),
        ):
            assert result.ok is False and result.reason == "lease_lost"
        current = store.get_task(task.task_id, now=121.0)
        assert current is not None
        assert current.owner == "fast" and current.status == "claimed"
        # The new owner is unaffected.
        assert store.complete_task(task.task_id, second.lease_token, status="done", summary="ok", now=122.0).ok

    def test_expired_lease_cannot_be_heartbeaten_even_before_reclaim(self, store: SQLiteStore) -> None:
        task, _ = store.create_task(_record())
        claimed = store.claim_task(agent_id="w", task_id=task.task_id, lease_seconds=10, now=100.0)
        assert claimed is not None
        late = store.heartbeat_task(task.task_id, claimed.lease_token, now=110.5)
        assert late.ok is False and late.reason == "lease_lost"
        # Lazy expiry returned it to open on that call.
        assert store.get_task(task.task_id, now=111.0).status == "open"  # type: ignore[union-attr]

    def test_lease_boundary_is_inclusive(self, store: SQLiteStore) -> None:
        task, _ = store.create_task(_record())
        claimed = store.claim_task(agent_id="w", task_id=task.task_id, lease_seconds=10, now=100.0)
        assert claimed is not None
        assert store.heartbeat_task(task.task_id, claimed.lease_token, lease_seconds=10, now=110.0).ok

    def test_expiry_goes_back_to_open_below_max_attempts(self, store: SQLiteStore) -> None:
        task, _ = store.create_task(_record(max_attempts=3))
        store.claim_task(agent_id="w", task_id=task.task_id, lease_seconds=10, now=100.0)
        expired = store.expire_task_leases(PIPE, now=200.0)
        assert [t.task_id for t in expired] == [task.task_id]
        assert expired[0].status == "open" and expired[0].owner is None
        assert expired[0].lease_token is None and expired[0].lease_expires_at is None
        assert expired[0].error is None and expired[0].attempt == 1 and expired[0].version == 2
        # Idempotent: nothing left to transition.
        assert store.expire_task_leases(PIPE, now=300.0) == []

    def test_expiry_fails_at_max_attempts(self, store: SQLiteStore) -> None:
        task, _ = store.create_task(_record(max_attempts=2))
        now = 100.0
        for _ in range(2):
            assert store.claim_task(agent_id="w", task_id=task.task_id, lease_seconds=10, now=now) is not None
            now += 50.0
            store.expire_task_leases(PIPE, now=now)
        final = store.get_task(task.task_id, now=now)
        assert final is not None
        assert final.status == "failed" and final.error == "lease_expired" and final.attempt == 2
        assert store.claim_task(agent_id="w", task_id=task.task_id, now=now) is None

    def test_expiry_is_scoped_by_pipeline(self, store: SQLiteStore) -> None:
        a, _ = store.create_task(_record())
        b, _ = store.create_task(_record(pipeline_id=OTHER_PIPE))
        store.claim_task(agent_id="w", task_id=a.task_id, lease_seconds=10, now=100.0)
        store.claim_task(agent_id="w", task_id=b.task_id, lease_seconds=10, now=100.0)
        assert [t.task_id for t in store.expire_task_leases(OTHER_PIPE, now=500.0)] == [b.task_id]
        assert [t.task_id for t in store.expire_task_leases(None, now=500.0)] == [a.task_id]

    def test_get_list_and_claim_run_lazy_expiry(self, store: SQLiteStore) -> None:
        task, _ = store.create_task(_record(max_attempts=5))
        store.claim_task(agent_id="w", task_id=task.task_id, lease_seconds=10, now=100.0)
        assert store.get_task(task.task_id, now=500.0).status == "open"  # type: ignore[union-attr]

        store.claim_task(agent_id="w", task_id=task.task_id, lease_seconds=10, now=600.0)
        assert store.list_tasks(pipeline_id=PIPE, status="open", now=900.0)[0].task_id == task.task_id

        store.claim_task(agent_id="w", task_id=task.task_id, lease_seconds=10, now=1000.0)
        again = store.claim_task(agent_id="w2", pipeline_id=PIPE, lease_seconds=10, now=2000.0)
        assert again is not None and again.owner == "w2"

    def test_heartbeat_keeps_a_task_alive(self, store: SQLiteStore) -> None:
        task, _ = store.create_task(_record())
        claimed = store.claim_task(agent_id="w", task_id=task.task_id, lease_seconds=10, now=100.0)
        assert claimed is not None
        now = 100.0
        for _ in range(5):
            now += 8.0
            assert store.heartbeat_task(task.task_id, claimed.lease_token, lease_seconds=10, now=now).ok
        assert store.expire_task_leases(PIPE, now=now + 5.0) == []


# ---------------------------------------------------------------------------
# dedupe_key


class TestDedupe:
    def test_same_key_returns_existing_task(self, store: SQLiteStore) -> None:
        first, created = store.create_task(_record(dedupe_key="github:pr/142:review"))
        assert created
        second, created_again = store.create_task(_record(dedupe_key="github:pr/142:review", title="other"))
        assert created_again is False
        assert second.task_id == first.task_id and second.title == "review the diff"
        assert len(store.list_tasks(pipeline_id=PIPE)) == 1

    def test_key_is_scoped_per_pipeline(self, store: SQLiteStore) -> None:
        a, ca = store.create_task(_record(dedupe_key="k"))
        b, cb = store.create_task(_record(pipeline_id=OTHER_PIPE, dedupe_key="k"))
        assert ca and cb and a.task_id != b.task_id

    def test_no_key_never_dedupes(self, store: SQLiteStore) -> None:
        _, c1 = store.create_task(_record())
        _, c2 = store.create_task(_record())
        assert c1 and c2 and len(store.list_tasks(pipeline_id=PIPE)) == 2

    def test_key_survives_terminal_state(self, store: SQLiteStore) -> None:
        first, _ = store.create_task(_record(dedupe_key="k"))
        store.cancel_task(first.task_id, "router")
        again, created = store.create_task(_record(dedupe_key="k"))
        assert created is False and again.task_id == first.task_id and again.status == "cancelled"

    def test_concurrent_creates_with_one_key(self, store: SQLiteStore) -> None:
        results = _run_threads(8, lambda i: store.create_task(_record(dedupe_key="same", title=f"t{i}")))
        assert sum(1 for _, created in results if created) == 1
        assert len({task.task_id for task, _ in results}) == 1
        assert len(store.list_tasks(pipeline_id=PIPE)) == 1

    def test_partial_unique_index_exists(self, store: SQLiteStore) -> None:
        with sqlite3.connect(store.path) as conn:
            row = conn.execute(
                "SELECT sql FROM sqlite_master WHERE type='index' AND name='idx_tasks_dedupe'"
            ).fetchone()
        assert row is not None
        assert "UNIQUE" in row[0] and "dedupe_key IS NOT NULL" in row[0]


# ---------------------------------------------------------------------------
# cancel


class TestCancel:
    def test_only_creator_may_cancel(self, store: SQLiteStore) -> None:
        task, _ = store.create_task(_record())
        denied = store.cancel_task(task.task_id, "intruder")
        assert denied.ok is False and denied.reason == "not_allowed"
        assert store.get_task(task.task_id).status == "open"  # type: ignore[union-attr]
        ok = store.cancel_task(task.task_id, "router", reason="obsolete")
        assert ok.ok and ok.task is not None
        assert ok.task.status == "cancelled" and ok.task.error == "obsolete"
        assert ok.previous_status == "open"

    def test_owner_cannot_cancel_unless_creator(self, store: SQLiteStore) -> None:
        task, _ = store.create_task(_record())
        claimed = store.claim_task(agent_id="worker", task_id=task.task_id, now=1.0)
        assert claimed is not None
        assert store.cancel_task(task.task_id, "worker", now=2.0).reason == "not_allowed"

    def test_cancel_claimed_invalidates_token_and_reports_owner(self, store: SQLiteStore) -> None:
        task, _ = store.create_task(_record())
        claimed = store.claim_task(agent_id="worker", task_id=task.task_id, now=1.0)
        assert claimed is not None
        result = store.cancel_task(task.task_id, "router", now=2.0)
        assert result.ok and result.previous_status == "claimed" and result.previous_owner == "worker"
        assert result.task is not None and result.task.lease_token is None
        assert store.heartbeat_task(task.task_id, claimed.lease_token, now=3.0).reason == "lease_lost"

    @pytest.mark.parametrize("terminal", ["done", "failed", "cancelled"])
    def test_terminal_states_cannot_be_cancelled(self, store: SQLiteStore, terminal: str) -> None:
        task, _ = store.create_task(_record())
        if terminal == "cancelled":
            store.cancel_task(task.task_id, "router")
        else:
            claimed = store.claim_task(agent_id="w", task_id=task.task_id, now=1.0)
            assert claimed is not None
            store.complete_task(task.task_id, claimed.lease_token, status=terminal, summary="s", now=2.0)  # type: ignore[arg-type]
        result = store.cancel_task(task.task_id, "router", now=3.0)
        assert result.ok is False and result.reason == "terminal"
        assert store.get_task(task.task_id).status == terminal  # type: ignore[union-attr]


# ---------------------------------------------------------------------------
# MCP tool


def _call(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": name, "arguments": arguments}}


def _content(response: str) -> dict[str, Any]:
    payload = json.loads(response)
    assert "error" not in payload, payload
    return json.loads(payload["result"]["content"][0]["text"])


def _error(response: str) -> str:
    payload = json.loads(response)
    assert "error" in payload, payload
    return str(payload["error"]["message"])


class _Env:
    def __init__(self, tmp_path: Path, *, tasks: bool = True, **tasks_cfg: Any) -> None:
        self.store = SQLiteStore(tmp_path / "mcp.db")
        self.config = load_config(cwd=tmp_path)
        self.config.values["tasks"]["enabled"] = tasks
        self.config.values["tasks"].update(tasks_cfg)
        self.notifier = InProcessNotifier()
        self.handlers = make_handlers(self.store, config=self.config, notifier=self.notifier)

    def task(self, **args: Any) -> dict[str, Any]:
        return _content(_handle_request(_call("ncp_task", args), self.handlers))

    def task_error(self, **args: Any) -> str:
        return _error(_handle_request(_call("ncp_task", args), self.handlers))

    def emit(self, **args: Any) -> dict[str, Any]:
        return _content(_handle_request(_call("ncp_emit_whisper", args), self.handlers))

    def emit_error(self, **args: Any) -> str:
        return _error(_handle_request(_call("ncp_emit_whisper", args), self.handlers))

    def create(self, **overrides: Any) -> dict[str, Any]:
        args: dict[str, Any] = {
            "action": "create", "agent_id": "router", "pipeline_id": PIPE, "slot": "review",
            "title": "review the diff",
        }
        args.update(overrides)
        return self.task(**args)

    def claim(self, task_id: str, agent: str = "worker", **extra: Any) -> dict[str, Any]:
        return self.task(action="claim", agent_id=agent, task_id=task_id, **extra)


@pytest.fixture
def env(tmp_path: Path) -> _Env:
    return _Env(tmp_path)


class TestToolGating:
    def test_hidden_when_disabled_by_default(self, tmp_path: Path) -> None:
        config = load_config(cwd=tmp_path)
        assert config.tasks_enabled is False
        assert "ncp_task" not in {t["name"] for t in tools_for_config(config)}
        assert "ncp_task" not in {t["name"] for t in tools_for_config(None)}

    def test_visible_when_enabled_full_profile(self, tmp_path: Path) -> None:
        config = load_config(cwd=tmp_path)
        config.values["tasks"]["enabled"] = True
        assert "ncp_task" in {t["name"] for t in tools_for_config(config)}

    def test_never_in_core_profile_even_when_enabled(self, tmp_path: Path) -> None:
        config = load_config(cwd=tmp_path)
        config.values["tasks"]["enabled"] = True
        config.values["tools"]["profile"] = "core"
        assert "ncp_task" not in {t["name"] for t in tools_for_config(config)}
        assert "ncp_task" not in CORE_TOOL_NAMES

    def test_not_callable_when_not_advertised(self, tmp_path: Path) -> None:
        config = load_config(cwd=tmp_path)
        tools = tools_for_config(config)
        handlers = make_handlers(SQLiteStore(tmp_path / "x.db"), config=config)
        response = json.loads(_handle_request(_call("ncp_task", {"action": "list", "pipeline_id": PIPE}),
                                              handlers, tools=tools))
        assert response["error"]["code"] == -32601

    def test_handler_refuses_when_disabled(self, tmp_path: Path) -> None:
        off = _Env(tmp_path, tasks=False)
        assert "disabled" in off.task_error(action="list", pipeline_id=PIPE)

    def test_config_defaults(self, tmp_path: Path) -> None:
        config = load_config(cwd=tmp_path)
        assert config.tasks_default_lease_seconds == 300
        assert config.tasks_max_lease_seconds == 3600
        assert config.tasks_default_max_attempts == 3
        assert config.tasks_stale_open_seconds == 1800

    def test_toml_config_is_read(self, tmp_path: Path) -> None:
        (tmp_path / ".git").mkdir()
        (tmp_path / ".ncp").mkdir()
        (tmp_path / ".ncp" / "config.toml").write_text(
            "[tasks]\nenabled = true\ndefault_lease_seconds = 120\nmax_lease_seconds = 600\n"
            "default_max_attempts = 5\n"
        )
        config = load_config(cwd=tmp_path)
        assert config.tasks_enabled and config.tasks_default_lease_seconds == 120
        assert config.tasks_max_lease_seconds == 600 and config.tasks_default_max_attempts == 5

    def test_schema_lists_every_action(self) -> None:
        from ncp.mcp.server import MCP_TOOLS

        tool = next(t for t in MCP_TOOLS if t["name"] == "ncp_task")
        enum = tool["inputSchema"]["properties"]["action"]["enum"]  # type: ignore[index]
        assert enum == ["create", "claim", "heartbeat", "release", "complete", "cancel", "get", "list"]


class TestNcpTaskTool:
    def test_create_claim_heartbeat_complete(self, env: _Env) -> None:
        created = env.create(slot="review")
        assert created["created"] is True
        task_id = created["task"]["task_id"]
        assert created["task"]["status"] == "open" and created["task"]["max_attempts"] == 3
        assert "lease_token" not in created["task"]

        claimed = env.claim(task_id)
        assert claimed["claimed"] is True and claimed["task"]["owner"] == "worker"
        token = claimed["lease_token"]
        assert re.fullmatch(r"[0-9a-f]{32}", token)
        assert "lease_token" not in claimed["task"]

        beat = env.task(action="heartbeat", task_id=task_id, lease_token=token, checkpoint_chunk_id="chk_1")
        assert beat["ok"] and beat["task"]["checkpoint_chunk_id"] == "chk_1"

        done = env.task(action="complete", task_id=task_id, lease_token=token, status="done",
                        summary="LGTM", result_chunk_ids=["chk_9"], turn_id="turn_7")
        assert done["ok"] and done["task"]["status"] == "done"
        assert done["result_turn_id"] == "turn_7" and done["whisper_id"].startswith("wsp_")

    def test_get_and_list_never_expose_lease_token(self, env: _Env) -> None:
        task_id = env.create()["task"]["task_id"]
        token = env.claim(task_id)["lease_token"]
        got = env.task(action="get", task_id=task_id)
        listed = env.task(action="list", pipeline_id=PIPE)
        assert got["found"] and "lease_token" not in got["task"]
        assert listed["count"] == 1 and "lease_token" not in listed["tasks"][0]
        assert token not in json.dumps([got, listed])

    def test_get_unknown(self, env: _Env) -> None:
        assert env.task(action="get", task_id="tsk_nope") == {"found": False, "task": None}

    def test_claim_miss_and_claim_next(self, env: _Env) -> None:
        assert env.task(action="claim", agent_id="w", pipeline_id=PIPE) == {"claimed": False}
        first = env.create(title="first")["task"]["task_id"]
        env.create(title="second")
        got = env.task(action="claim", agent_id="w", pipeline_id=PIPE, slot="review")
        assert got["claimed"] and got["task"]["task_id"] == first

    def test_claim_needs_task_or_pipeline(self, env: _Env) -> None:
        assert "task_id or pipeline_id" in env.task_error(action="claim", agent_id="w")

    def test_missing_required_arguments_are_clear(self, env: _Env) -> None:
        assert "'title'" in env.task_error(action="create", agent_id="r", pipeline_id=PIPE, slot="s")
        assert "'agent_id'" in env.task_error(action="create", pipeline_id=PIPE, slot="s", title="t")
        assert "'lease_token'" in env.task_error(action="heartbeat", task_id="tsk_1")
        assert "unknown ncp_task action" in env.task_error(action="explode")

    def test_stale_token_returns_lease_lost_not_error(self, env: _Env) -> None:
        task_id = env.create()["task"]["task_id"]
        env.claim(task_id)
        assert env.task(action="heartbeat", task_id=task_id, lease_token="0" * 32) == {
            "ok": False, "reason": "lease_lost",
        }
        assert env.task(action="release", task_id=task_id, lease_token="0" * 32)["reason"] == "lease_lost"
        assert env.task(action="complete", task_id=task_id, lease_token="0" * 32, status="done",
                        summary="x")["reason"] == "lease_lost"

    def test_dedupe_through_tool(self, env: _Env) -> None:
        first = env.create(dedupe_key="evt-1", assign_to="worker")
        second = env.create(dedupe_key="evt-1", assign_to="worker")
        assert first["created"] is True and second["created"] is False
        assert second["task"]["task_id"] == first["task"]["task_id"]
        # The retry must not re-ring the assignee with a second request.
        assert len(env.store.drain_whispers(agent_id="worker", pipeline_id=PIPE)) == 1

    def test_lease_seconds_clamped_to_config_max(self, tmp_path: Path) -> None:
        clamped = _Env(tmp_path, max_lease_seconds=100, default_lease_seconds=50)
        task_id = clamped.create()["task"]["task_id"]
        claimed = clamped.claim(task_id, lease_seconds=999999)["task"]
        assert claimed["lease_expires_at"] - claimed["heartbeat_at"] == pytest.approx(100, abs=0.01)
        token = clamped.store.get_task(task_id).lease_token  # type: ignore[union-attr]
        beat = clamped.task(action="heartbeat", task_id=task_id, lease_token=token, lease_seconds=5000)["task"]
        assert beat["lease_expires_at"] - beat["heartbeat_at"] == pytest.approx(100, abs=0.01)
        task2 = clamped.create(title="second")["task"]["task_id"]
        default = clamped.claim(task2)["task"]
        assert default["lease_expires_at"] - default["heartbeat_at"] == pytest.approx(50, abs=0.01)

    def test_max_attempts_default_and_override(self, tmp_path: Path) -> None:
        custom = _Env(tmp_path, default_max_attempts=7)
        assert custom.create()["task"]["max_attempts"] == 7
        assert custom.create(max_attempts=2, title="b")["task"]["max_attempts"] == 2

    def test_list_limit_is_capped_and_oldest_first(self, env: _Env) -> None:
        for i in range(55):
            env.create(title=f"t{i:02d}")
        listed = env.task(action="list", pipeline_id=PIPE, limit=500)
        assert listed["count"] == 50
        assert [t["title"] for t in listed["tasks"][:2]] == ["t00", "t01"]
        assert env.task(action="list", pipeline_id=PIPE, limit=3)["count"] == 3
        assert "invalid task status" in env.task_error(action="list", pipeline_id=PIPE, status="weird")

    def test_validation_errors_surface_before_any_write(self, env: _Env) -> None:
        assert "200" in env.task_error(action="create", agent_id="r", pipeline_id=PIPE, slot="s", title="x" * 201)
        assert env.task(action="list", pipeline_id=PIPE)["count"] == 0

    def test_bad_summary_does_not_complete_the_task(self, env: _Env) -> None:
        task_id = env.create()["task"]["task_id"]
        token = env.claim(task_id)["lease_token"]
        assert "300" in env.task_error(action="complete", task_id=task_id, lease_token=token,
                                       status="done", summary="s" * 301)
        assert env.task(action="get", task_id=task_id)["task"]["status"] == "claimed"

    def test_complete_does_not_record_an_outcome(self, env: _Env) -> None:
        task_id = env.create()["task"]["task_id"]
        token = env.claim(task_id)["lease_token"]
        with patch.object(env.store, "record_outcome", side_effect=AssertionError("must not record")) as rec:
            env.task(action="complete", task_id=task_id, lease_token=token, status="done", summary="ok",
                     turn_id="turn_1")
        assert rec.call_count == 0

    def test_release_through_tool(self, env: _Env) -> None:
        task_id = env.create()["task"]["task_id"]
        token = env.claim(task_id)["lease_token"]
        released = env.task(action="release", task_id=task_id, lease_token=token, error="need input")
        assert released["ok"] and released["task"]["status"] == "open"
        assert released["task"]["error"] == "need input"

    def test_cancel_through_tool(self, env: _Env) -> None:
        task_id = env.create()["task"]["task_id"]
        assert env.task(action="cancel", agent_id="intruder", task_id=task_id) == {
            "ok": False, "reason": "not_allowed",
        }
        cancelled = env.task(action="cancel", agent_id="router", task_id=task_id, reason="dup")
        assert cancelled["ok"] and cancelled["task"]["status"] == "cancelled"
        assert env.task(action="cancel", agent_id="router", task_id=task_id)["reason"] == "terminal"


# ---------------------------------------------------------------------------
# Whisper linkage


class TestWhisperLinkage:
    def test_create_assign_to_emits_linked_request(self, env: _Env) -> None:
        created = env.create(assign_to="worker", title="check the build")
        assert created["assigned"] is True
        task = created["task"]
        assert task["origin_whisper_id"] == created["whisper_id"]
        [request] = env.store.drain_whispers(agent_id="worker", pipeline_id=PIPE)
        assert request.whisper_type == "request" and request.from_agent == "router"
        assert request.task_id == task["task_id"]
        assert request.whisper_id == task["origin_whisper_id"]
        assert request.thread_id == request.whisper_id
        assert json.loads(request.payload)["ask"] == "check the build"

    def test_create_without_assign_emits_nothing(self, env: _Env) -> None:
        created = env.create()
        assert "assigned" not in created and created["task"]["origin_whisper_id"] is None
        assert env.store.drain_whispers(agent_id="worker", pipeline_id=PIPE) == []

    def test_complete_emits_linked_result_the_router_receives(self, env: _Env) -> None:
        created = env.create(assign_to="worker")
        task_id = created["task"]["task_id"]
        origin = created["task"]["origin_whisper_id"]
        token = env.claim(task_id)["lease_token"]
        done = env.task(action="complete", task_id=task_id, lease_token=token, status="done",
                        summary="all green", result_chunk_ids=["chk_a", "chk_b"], turn_id="turn_3")
        [result] = [w for w in env.store.drain_whispers(agent_id="router", pipeline_id=PIPE)
                    if w.whisper_type == "result"]
        assert result.whisper_id == done["whisper_id"]
        assert result.task_id == task_id and result.in_reply_to == origin
        assert result.thread_id == origin and result.from_agent == "worker"
        assert json.loads(result.payload) == {
            "status": "succeeded", "summary": "all green",
            "result_chunk_ids": ["chk_a", "chk_b"], "turn_id": "turn_3",
        }

    def test_failed_complete_maps_to_failed_result(self, env: _Env) -> None:
        task_id = env.create()["task"]["task_id"]
        token = env.claim(task_id)["lease_token"]
        env.task(action="complete", task_id=task_id, lease_token=token, status="failed", summary="build broke")
        [result] = env.store.drain_whispers(agent_id="router", pipeline_id=PIPE)
        assert json.loads(result.payload)["status"] == "failed"
        assert result.task_id == task_id and result.in_reply_to is None
        assert env.task(action="get", task_id=task_id)["task"]["error"] == "build broke"

    def test_result_rings_the_doorbell(self, env: _Env) -> None:
        task_id = env.create()["task"]["task_id"]
        token = env.claim(task_id)["lease_token"]
        with env.notifier.subscribe(agent_id="router", pipeline_id=PIPE) as sub:
            env.task(action="complete", task_id=task_id, lease_token=token, status="done", summary="ok")
            events = sub.poll(0.5)
        assert [(e.event, e.type, e.task_id) for e in events] == [("whisper", "result", task_id)]

    def test_assignment_rings_the_assignee(self, env: _Env) -> None:
        with env.notifier.subscribe(agent_id="worker", pipeline_id=PIPE) as sub:
            task_id = env.create(assign_to="worker")["task"]["task_id"]
            events = sub.poll(0.5)
        assert [(e.type, e.task_id) for e in events] == [("request", task_id)]

    def test_heartbeat_progress_is_coalesced_and_linked(self, env: _Env) -> None:
        created = env.create(assign_to="worker")
        task_id = created["task"]["task_id"]
        origin = created["task"]["origin_whisper_id"]
        token = env.claim(task_id)["lease_token"]
        first = env.task(action="heartbeat", task_id=task_id, lease_token=token,
                         progress={"status": "working", "pct": 10})
        second = env.task(action="heartbeat", task_id=task_id, lease_token=token,
                          progress={"status": "working", "pct": 60, "note": "halfway"})
        assert first["whisper_id"] != second["whisper_id"]
        progress = [w for w in env.store.drain_whispers(agent_id="router", pipeline_id=PIPE)
                    if w.whisper_type == "progress"]
        assert len(progress) == 1
        assert progress[0].whisper_id == second["whisper_id"]
        assert progress[0].task_id == task_id and progress[0].in_reply_to == origin
        assert json.loads(progress[0].payload)["pct"] == 60

    def test_heartbeat_without_progress_emits_nothing(self, env: _Env) -> None:
        task_id = env.create()["task"]["task_id"]
        token = env.claim(task_id)["lease_token"]
        beat = env.task(action="heartbeat", task_id=task_id, lease_token=token)
        assert "whisper_id" not in beat
        assert env.store.drain_whispers(agent_id="router", pipeline_id=PIPE) == []

    def test_invalid_progress_is_rejected_before_extending_lease(self, env: _Env) -> None:
        task_id = env.create()["task"]["task_id"]
        token = env.claim(task_id)["lease_token"]
        before = env.store.get_task(task_id)
        assert before is not None
        assert "progress" in env.task_error(action="heartbeat", task_id=task_id, lease_token=token,
                                            progress={"status": "sleeping"}).lower()
        after = env.store.get_task(task_id)
        assert after is not None and after.version == before.version

    def test_stale_heartbeat_emits_no_progress(self, env: _Env) -> None:
        task_id = env.create()["task"]["task_id"]
        env.claim(task_id)
        result = env.task(action="heartbeat", task_id=task_id, lease_token="f" * 32,
                          progress={"status": "working"})
        assert result["ok"] is False
        assert env.store.drain_whispers(agent_id="router", pipeline_id=PIPE) == []


class TestOwnerCheck:
    def _claimed(self, env: _Env) -> str:
        task_id = env.create(assign_to="worker")["task"]["task_id"]
        env.claim(task_id, "worker")
        return task_id

    def _result_args(self, task_id: str, sender: str, **extra: Any) -> dict[str, Any]:
        args: dict[str, Any] = {
            "from": sender, "target": "router", "type": "result", "confidence": 0.9,
            "payload": {"status": "succeeded", "summary": "forged"}, "pipeline_id": PIPE,
            "task_id": task_id,
        }
        args.update(extra)
        return args

    def test_forged_result_from_non_owner_is_rejected(self, env: _Env) -> None:
        task_id = self._claimed(env)
        message = env.emit_error(**self._result_args(task_id, "intruder"))
        assert "only the current owner" in message
        assert [w for w in env.store.drain_whispers(agent_id="router", pipeline_id=PIPE)] == []

    def test_forged_progress_from_non_owner_is_rejected(self, env: _Env) -> None:
        task_id = self._claimed(env)
        message = env.emit_error(**{**self._result_args(task_id, "intruder"), "type": "progress",
                                    "payload": {"status": "working"}})
        assert "only the current owner" in message

    def test_owner_may_emit_directly(self, env: _Env) -> None:
        task_id = self._claimed(env)
        emitted = env.emit(**self._result_args(task_id, "worker"))
        assert emitted["emitted"] is True
        [result] = env.store.drain_whispers(agent_id="router", pipeline_id=PIPE)
        assert result.task_id == task_id

    def test_non_claimed_task_is_rejected_even_from_the_former_owner(self, env: _Env) -> None:
        task_id = env.create()["task"]["task_id"]
        assert "only the current owner" in env.emit_error(**self._result_args(task_id, "worker"))
        token = env.claim(task_id, "worker")["lease_token"]
        env.task(action="complete", task_id=task_id, lease_token=token, status="done", summary="ok")
        env.store.drain_whispers(agent_id="router", pipeline_id=PIPE)
        assert "only the current owner" in env.emit_error(**self._result_args(task_id, "worker"))

    def test_reassigned_task_rejects_previous_owner(self, env: _Env) -> None:
        task_id = env.create()["task"]["task_id"]
        first = env.claim(task_id, "w1")["lease_token"]
        env.task(action="release", task_id=task_id, lease_token=first)
        env.claim(task_id, "w2")
        assert "only the current owner" in env.emit_error(**self._result_args(task_id, "w1"))
        assert env.emit(**self._result_args(task_id, "w2"))["emitted"] is True

    def test_unknown_task_and_wrong_pipeline(self, env: _Env) -> None:
        assert "unknown task_id" in env.emit_error(**self._result_args("tsk_nope", "worker"))
        task_id = self._claimed(env)
        assert "different pipeline" in env.emit_error(
            **self._result_args(task_id, "worker", pipeline_id=OTHER_PIPE)
        )

    def test_rejected_when_ledger_disabled(self, tmp_path: Path) -> None:
        off = _Env(tmp_path, tasks=False)
        message = off.emit_error(**self._result_args("tsk_x", "worker"))
        assert "task ledger is disabled" in message

    def test_rejected_when_store_does_not_support_ledger(self, tmp_path: Path) -> None:
        env = _Env(tmp_path)
        with patch.object(env.store, "get_task", side_effect=NCPStoreError("task ledger not supported")):
            message = env.emit_error(**self._result_args("tsk_x", "worker"))
        assert "task ledger not supported" in message

    def test_other_types_may_carry_task_id_unchecked(self, env: _Env) -> None:
        task_id = self._claimed(env)
        emitted = env.emit(**{"from": "anyone", "target": "router", "type": "nudge", "payload": "fyi",
                              "confidence": 0.9, "pipeline_id": PIPE, "task_id": task_id})
        assert emitted["emitted"] is True

    def test_result_still_requires_linkage_without_task_id(self, env: _Env) -> None:
        message = env.emit_error(**{"from": "w", "target": "router", "type": "result", "confidence": 0.9,
                                    "payload": {"status": "succeeded", "summary": "x"}, "pipeline_id": PIPE})
        assert "in_reply_to, thread_id or task_id" in message


# ---------------------------------------------------------------------------
# Task notifications


class TestTaskNotifications:
    def test_expiry_to_failed_rings_creator(self, tmp_path: Path, clock: _Clock) -> None:
        env = _Env(tmp_path)
        task_id = env.create(max_attempts=1)["task"]["task_id"]
        env.claim(task_id, lease_seconds=10)
        with env.notifier.subscribe(agent_id="router", pipeline_id=PIPE) as sub:
            clock.advance(11)
            fetched = env.task(action="get", task_id=task_id)
            events = sub.poll(0.5)
        assert fetched["task"]["status"] == "failed" and fetched["task"]["error"] == "lease_expired"
        assert len(events) == 1
        event = events[0]
        assert (event.event, event.task_id, event.status, event.target, event.pipeline_id) == (
            "task", task_id, "failed", "router", PIPE,
        )
        assert event.whisper_id is None

    def test_expiry_back_to_open_does_not_ring(self, tmp_path: Path, clock: _Clock) -> None:
        env = _Env(tmp_path)
        task_id = env.create(max_attempts=3)["task"]["task_id"]
        env.claim(task_id, lease_seconds=10)
        with env.notifier.subscribe(agent_id="router", pipeline_id=PIPE) as sub:
            clock.advance(11)
            assert env.task(action="get", task_id=task_id)["task"]["status"] == "open"
            assert sub.poll(0.05) == []

    def test_expiry_notifies_exactly_once(self, tmp_path: Path, clock: _Clock) -> None:
        env = _Env(tmp_path)
        task_id = env.create(max_attempts=1)["task"]["task_id"]
        env.claim(task_id, lease_seconds=10)
        with env.notifier.subscribe(agent_id="router", pipeline_id=PIPE) as sub:
            clock.advance(11)
            env.task(action="get", task_id=task_id)
            env.task(action="list", pipeline_id=PIPE)
            env.task(action="get", task_id=task_id)
            assert len(sub.poll(0.3)) == 1

    @pytest.mark.parametrize("trigger", ["claim", "list", "heartbeat", "cancel"])
    def test_every_lazy_expiry_entry_point_publishes(self, tmp_path: Path, clock: _Clock, trigger: str) -> None:
        env = _Env(tmp_path)
        task_id = env.create(max_attempts=1)["task"]["task_id"]
        env.claim(task_id, lease_seconds=10)
        calls = {
            "claim": dict(action="claim", agent_id="w2", pipeline_id=PIPE),
            "list": dict(action="list", pipeline_id=PIPE),
            "heartbeat": dict(action="heartbeat", task_id=task_id, lease_token="a" * 32),
            "cancel": dict(action="cancel", agent_id="router", task_id=task_id),
        }
        with env.notifier.subscribe(agent_id="router", pipeline_id=PIPE) as sub:
            clock.advance(11)
            env.task(**calls[trigger])
            events = sub.poll(0.5)
        assert [(e.event, e.status) for e in events] == [("task", "failed")]

    def test_cancel_while_claimed_rings_owner(self, env: _Env) -> None:
        task_id = env.create()["task"]["task_id"]
        env.claim(task_id, "worker")
        with env.notifier.subscribe(agent_id="worker", pipeline_id=PIPE) as sub:
            env.task(action="cancel", agent_id="router", task_id=task_id)
            events = sub.poll(0.5)
        assert [(e.event, e.task_id, e.status, e.target) for e in events] == [
            ("task", task_id, "cancelled", "worker")
        ]

    def test_cancel_of_open_task_rings_nobody(self, env: _Env) -> None:
        task_id = env.create(assign_to="worker")["task"]["task_id"]
        with env.notifier.subscribe(agent_id="worker", pipeline_id=PIPE) as sub:
            sub.poll(0.05)
            env.task(action="cancel", agent_id="router", task_id=task_id)
            assert sub.poll(0.05) == []

    def test_release_that_exhausts_attempts_rings_creator(self, env: _Env) -> None:
        task_id = env.create(max_attempts=1)["task"]["task_id"]
        token = env.claim(task_id)["lease_token"]
        with env.notifier.subscribe(agent_id="router", pipeline_id=PIPE) as sub:
            env.task(action="release", task_id=task_id, lease_token=token)
            assert [(e.event, e.status) for e in sub.poll(0.5)] == [("task", "failed")]

    def test_task_event_carries_no_text_fields(self) -> None:
        task = _record(title="secret title", error="secret error")
        event = NotifyEvent.from_task(task, target="router")
        dumped = json.dumps(event.to_dict())
        assert "secret" not in dumped
        assert set(event.to_dict()) == {
            "event", "whisper_id", "type", "from", "target", "pipeline_id", "thread_id", "task_id",
            "status", "created_at",
        }

    def test_notifier_failure_never_fails_the_call(self, tmp_path: Path, clock: _Clock) -> None:
        env = _Env(tmp_path)
        task_id = env.create(max_attempts=1)["task"]["task_id"]
        env.claim(task_id, lease_seconds=10)
        clock.advance(11)
        with patch.object(env.notifier, "publish", side_effect=RuntimeError("redis down")):
            assert env.task(action="get", task_id=task_id)["task"]["status"] == "failed"

    def test_wait_whispers_rows_carry_task_id(self, env: _Env) -> None:
        task_id = env.create(assign_to="worker")["task"]["task_id"]
        waited = _content(_handle_request(
            _call("ncp_wait_whispers", {"agent_id": "worker", "pipeline_id": PIPE, "timeout_seconds": 0}),
            env.handlers,
        ))
        assert [row["task_id"] for row in waited["whispers"]] == [task_id]


class _HttpServer:
    def __init__(self, tmp_path: Path) -> None:
        (tmp_path / ".git").mkdir(exist_ok=True)
        (tmp_path / ".ncp").mkdir(exist_ok=True)
        (tmp_path / ".ncp" / "config.toml").write_text("[tasks]\nenabled = true\n")
        self.server = create_http_server(host="127.0.0.1", port=0, cwd=tmp_path)
        self.port = self.server.server_address[1]
        self.conns: list[http.client.HTTPConnection] = []

    def __enter__(self) -> _HttpServer:
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        return self

    def __exit__(self, *exc: object) -> None:
        for conn in self.conns:
            conn.close()
        self.server._shutdown_event.set()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        assert not self.thread.is_alive()

    def get(self, path: str) -> http.client.HTTPResponse:
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        self.conns.append(conn)
        conn.request("GET", path)
        return conn.getresponse()

    def call(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        return _content(_handle_request(_call(name, arguments), self.server.handlers, tools=self.server.tools))


def _read_frame(resp: http.client.HTTPResponse) -> tuple[str, str]:
    event, data = "", ""
    while True:
        line = resp.readline().decode("utf-8").rstrip("\n")
        if line == "":
            if event or data:
                return event, data
            continue
        if line.startswith(":"):
            return "", line
        if line.startswith("event:"):
            event = line[len("event:"):].strip()
        elif line.startswith("data:"):
            data = line[len("data:"):].strip()


class TestEventsEndpointTaskFrames:
    def test_cancel_emits_event_task_frame(self, tmp_path: Path) -> None:
        with _HttpServer(tmp_path) as srv:
            resp = srv.get(f"/events?agent_id=worker&pipeline_id={PIPE}")
            assert _read_frame(resp)[0] == "ready"
            created = srv.call("ncp_task", {"action": "create", "agent_id": "router", "pipeline_id": PIPE,
                                            "slot": "review", "title": "do it"})
            task_id = created["task"]["task_id"]
            srv.call("ncp_task", {"action": "claim", "agent_id": "worker", "task_id": task_id})
            srv.call("ncp_task", {"action": "cancel", "agent_id": "router", "task_id": task_id})
            event, data = _read_frame(resp)
            assert event == "task"
            body = json.loads(data)
            assert body["event"] == "task" and body["task_id"] == task_id and body["status"] == "cancelled"
            assert body["target"] == "worker" and body["pipeline_id"] == PIPE
            assert "do it" not in data and "title" not in body and "payload" not in body

    def test_whisper_frames_keep_event_whisper(self, tmp_path: Path) -> None:
        with _HttpServer(tmp_path) as srv:
            resp = srv.get(f"/events?agent_id=worker&pipeline_id={PIPE}")
            assert _read_frame(resp)[0] == "ready"
            srv.call("ncp_task", {"action": "create", "agent_id": "router", "pipeline_id": PIPE,
                                  "slot": "review", "title": "do it", "assign_to": "worker"})
            event, data = _read_frame(resp)
            assert event == "whisper" and json.loads(data)["type"] == "request"


# ---------------------------------------------------------------------------
# Rendering


class TestTaskRendering:
    def _entry(self, whisper: Whisper) -> list[str]:
        return PidginEncoder()._encode_whisper_entry(whisper, now=whisper.created_at).split("\n")

    def test_task_token_rendered_only_when_set(self) -> None:
        linked = Whisper(from_agent="worker", target="router", whisper_type="result",
                         payload={"status": "succeeded", "summary": "ok"}, confidence=0.9,
                         in_reply_to="wsp_origin", thread_id="wsp_origin", task_id="tsk_abc123def456")
        header = self._entry(linked)[0]
        assert header.endswith(f"id:{linked.whisper_id} re:wsp_origin task:tsk_abc123def456")

    def test_task_only_whisper_still_shows_id(self) -> None:
        progress = Whisper(from_agent="worker", target="router", whisper_type="progress",
                           payload={"status": "working"}, confidence=0.9, task_id="tsk_1")
        assert self._entry(progress)[0].endswith(f"id:{progress.whisper_id} task:tsk_1")

    def test_task_less_whispers_render_byte_for_byte_unchanged(self) -> None:
        nudge = Whisper(from_agent="router", target="reviewer", whisper_type="nudge", payload="heads up",
                        confidence=0.9)
        assert self._entry(nudge)[0] == "wsp from:router to:reviewer t:nudge c:0.9 age:<1m"
        assert "task:" not in "\n".join(self._entry(nudge))
        threaded = Whisper(from_agent="router", target="reviewer", whisper_type="request",
                           payload={"ask": "x"}, confidence=0.9, thread_id="wsp_t")
        assert "task:" not in self._entry(threaded)[0]


# ---------------------------------------------------------------------------
# SQLite schema upgrade


class TestSqliteUpgrade:
    def _old_db(self, path: Path) -> None:
        with sqlite3.connect(path) as conn:
            conn.executescript(
                """
                CREATE TABLE whispers (
                    whisper_id TEXT PRIMARY KEY, pipeline_id TEXT, from_agent TEXT NOT NULL,
                    target TEXT NOT NULL, whisper_type TEXT NOT NULL, payload TEXT NOT NULL,
                    confidence REAL NOT NULL, ref TEXT, created_at REAL NOT NULL, expires_at REAL NOT NULL
                );
                INSERT INTO whispers VALUES ('wsp_old', 'p', 'a', 'b', 'nudge', 'hi', 0.9, NULL, 1.0, 9e12);
                """
            )

    def test_old_database_gains_tasks_table_and_whisper_column(self, tmp_path: Path) -> None:
        path = tmp_path / "old.db"
        self._old_db(path)
        store = SQLiteStore(path)
        with sqlite3.connect(path) as conn:
            tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            columns = {r[1] for r in conn.execute("PRAGMA table_info(whispers)")}
            indexes = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='index'")}
            task_columns = {r[1] for r in conn.execute("PRAGMA table_info(tasks)")}
        assert "tasks" in tables
        assert {"task_id", "in_reply_to", "thread_id"} <= columns
        assert {"idx_whispers_task", "idx_tasks_pipeline_status", "idx_tasks_owner_status",
                "idx_tasks_dedupe"} <= indexes
        assert set(TaskRecord.model_fields) == task_columns
        # The old whisper survived and the ledger works on the upgraded file.
        [old] = store.drain_whispers(agent_id="b", pipeline_id="p")
        assert old.whisper_id == "wsp_old" and old.task_id is None
        task, created = store.create_task(_record())
        assert created and store.get_task(task.task_id) is not None

    def test_reopening_is_idempotent(self, tmp_path: Path) -> None:
        path = tmp_path / "again.db"
        first = SQLiteStore(path)
        task, _ = first.create_task(_record())
        second = SQLiteStore(path)
        assert second.get_task(task.task_id) is not None

    def test_whisper_task_id_round_trips(self, store: SQLiteStore) -> None:
        whisper = Whisper(from_agent="w", target="r", whisper_type="progress",
                          payload={"status": "working"}, confidence=0.9, task_id="tsk_z", pipeline_id=PIPE)
        store.emit_whisper(whisper)
        [back] = store.peek_whispers(agent_id="r", pipeline_id=PIPE)
        assert back.task_id == "tsk_z"

    def test_task_progress_coalesces_per_task(self, store: SQLiteStore) -> None:
        def progress(task_id: str, pct: int) -> Whisper:
            return Whisper(from_agent="w", target="r", whisper_type="progress",
                           payload={"status": "working", "pct": pct}, confidence=0.9,
                           task_id=task_id, pipeline_id=PIPE)

        for whisper in (progress("tsk_a", 1), progress("tsk_b", 2), progress("tsk_a", 3)):
            store.emit_whisper(whisper)
        seen = {w.task_id: json.loads(w.payload)["pct"] for w in store.peek_whispers(agent_id="r", pipeline_id=PIPE)}
        assert seen == {"tsk_a": 3, "tsk_b": 2}


# ---------------------------------------------------------------------------
# Redis coordination carries task_id


class TestRedisCoordinationTaskId:
    def test_sync_and_async_hash_fields(self) -> None:
        import inspect

        from ncp.stores import redis_coordination

        source = inspect.getsource(redis_coordination)
        assert source.count('"task_id": whisper.task_id or ""') == 2
        assert source.count('task_id=str(payload.get("task_id") or "") or None') == 2

    def test_progress_coalescing_considers_task_id(self) -> None:
        from ncp.stores.redis_coordination import _is_superseded_progress

        newer = Whisper(from_agent="w", target="r", whisper_type="progress",
                        payload={"status": "working"}, confidence=0.9, task_id="tsk_a", pipeline_id=PIPE)
        stored = {"whisper_type": "progress", "from_agent": "w", "target": "r", "thread_id": "",
                  "pipeline_id": PIPE, "task_id": "tsk_a"}
        assert _is_superseded_progress(stored, newer)
        assert not _is_superseded_progress({**stored, "task_id": "tsk_b"}, newer)


# ---------------------------------------------------------------------------
# Postgres: migration + statements (fake connection)


class TestMigration018:
    def test_discovered_by_runner(self) -> None:
        runner = MigrationRunner(MagicMock(), schema="ncp", prefix="ncp_")
        files = {f.version: f for f in runner._discover()}
        assert files[18].name == "018_add_tasks"
        assert files[18].path.name == "018_add_tasks.sql"
        versions = sorted(files)
        assert versions == list(range(1, versions[-1] + 1))

    def test_has_up_and_down_sections(self) -> None:
        up, down = MigrationRunner.parse_sections(MIGRATION.read_text())
        assert "CREATE TABLE IF NOT EXISTS {schema}.{prefix}tasks" in up
        assert "result_chunk_ids JSONB" in up
        assert "WHERE dedupe_key IS NOT NULL" in up and "CREATE UNIQUE INDEX" in up
        assert "{prefix}tasks_pipeline_status_idx" in up and "{prefix}tasks_owner_status_idx" in up
        assert "DROP TABLE IF EXISTS {schema}.{prefix}tasks" in down

    def test_substitution_leaves_no_placeholders(self) -> None:
        runner = MigrationRunner(MagicMock(), schema="s1", prefix="p_")
        up, down = MigrationRunner.parse_sections(MIGRATION.read_text())
        for section in (runner._sub(up), runner._sub(down)):
            assert "{schema}" not in section and "{prefix}" not in section
        assert "s1.p_tasks" in runner._sub(up)

    @staticmethod
    def _columns(sql: str) -> list[str]:
        body = sql.split("tasks (", 1)[1].split("\n);", 1)[0]
        names = []
        for line in body.splitlines():
            line = line.strip()
            if line and not line.startswith("--"):
                names.append(line.split()[0])
        return names

    def test_fresh_install_template_matches_migration(self) -> None:
        up, _ = MigrationRunner.parse_sections(MIGRATION.read_text())
        assert self._columns(PGVECTOR_SCHEMA_TEMPLATE) == self._columns(up)
        assert self._columns(up) == list(TaskRecord.model_fields)
        assert "{prefix}tasks_dedupe_idx" in PGVECTOR_SCHEMA_TEMPLATE
        # The template is run through str.format; it must still format cleanly.
        PGVECTOR_SCHEMA_TEMPLATE.format(schema="ncp", prefix="ncp_")


def _pg_row(**overrides: Any) -> dict[str, Any]:
    row: dict[str, Any] = {
        "task_id": "tsk_abc123def456", "pipeline_id": PIPE, "slot": "review", "title": "t",
        "status": "open", "created_by": "router", "owner": None, "lease_token": None,
        "lease_expires_at": None, "heartbeat_at": None, "attempt": 0, "max_attempts": 3,
        "dedupe_key": None, "parent_task_id": None, "origin_whisper_id": None,
        "checkpoint_chunk_id": None, "result_chunk_ids": [], "result_turn_id": None, "error": None,
        "version": 0, "created_at": 1.0, "updated_at": 1.0,
    }
    row.update(overrides)
    return row


def _sync_pg() -> tuple[PgvectorStore, MagicMock]:
    conn = MagicMock()
    cursor = conn.cursor.return_value
    cursor.fetchall.return_value = []
    cursor.fetchone.return_value = None
    cursor.description = []
    return PgvectorStore("postgresql://localhost/test", connect_factory=lambda dsn: conn), conn


def _executed(conn: MagicMock) -> list[tuple[str, tuple[Any, ...]]]:
    return [(c.args[0], c.args[1] if len(c.args) > 1 else ()) for c in conn.cursor.return_value.execute.call_args_list]


class TestPostgresStatements:
    def test_claim_uses_skip_locked_and_expires_first(self) -> None:
        store, conn = _sync_pg()
        cursor = conn.cursor.return_value
        cursor.fetchall.side_effect = [
            [],
            [_pg_row(status="claimed", owner="w", lease_token="a" * 32, attempt=1, version=1,
                     lease_expires_at=61.0, heartbeat_at=1.0)],
        ]
        claimed = store.claim_task(agent_id="w", pipeline_id=PIPE, slot="review", lease_seconds=60, now=1.0)
        assert claimed is not None and claimed.status == "claimed" and claimed.attempt == 1
        statements = _executed(conn)[-2:]
        assert statements[0][0].startswith("UPDATE ncp.ncp_tasks SET") and "lease_expires_at < %s" in statements[0][0]
        claim_sql, params = statements[1]
        assert "FOR UPDATE SKIP LOCKED" in claim_sql
        assert "attempt = attempt + 1" in claim_sql and "version = version + 1" in claim_sql
        assert "ORDER BY created_at, task_id LIMIT 1" in claim_sql and "RETURNING" in claim_sql
        assert params[0] == "w" and params[2] == 61.0 and params[-2:] == (PIPE, "review")
        conn.commit.assert_called()

    def test_claim_miss_returns_none(self) -> None:
        store, _ = _sync_pg()
        assert store.claim_task(agent_id="w", pipeline_id=PIPE, now=1.0) is None

    def test_claim_requires_task_or_pipeline(self) -> None:
        store, _ = _sync_pg()
        with pytest.raises(ValueError):
            store.claim_task(agent_id="w")

    def test_create_inserts_jsonb_and_handles_dedupe_conflict(self) -> None:
        store, conn = _sync_pg()
        cursor = conn.cursor.return_value
        existing = _pg_row(task_id="tsk_existing0001", dedupe_key="k")
        cursor.fetchall.side_effect = [[], [existing]]
        task, created = store.create_task(_record(dedupe_key="k", result_chunk_ids=["c1"]))
        assert created is False and task.task_id == "tsk_existing0001"
        insert_sql, params = _executed(conn)[-2]
        assert "ON CONFLICT (pipeline_id, dedupe_key) WHERE dedupe_key IS NOT NULL DO NOTHING" in insert_sql
        assert "%s::jsonb" in insert_sql and json.dumps(["c1"]) in params
        select_sql, select_params = _executed(conn)[-1]
        assert "dedupe_key = %s" in select_sql and select_params == (PIPE, "k")

    def test_create_new_row(self) -> None:
        store, conn = _sync_pg()
        record = _record()
        conn.cursor.return_value.fetchall.side_effect = [[_pg_row(task_id=record.task_id)]]
        task, created = store.create_task(record)
        assert created and task.task_id == record.task_id

    def test_owner_mutation_is_fenced_and_reports_lease_lost(self) -> None:
        store, conn = _sync_pg()
        cursor = conn.cursor.return_value
        cursor.fetchall.side_effect = [[], [], [_pg_row(status="claimed", owner="other")]]
        result = store.heartbeat_task("tsk_abc123def456", "stale", lease_seconds=30,
                                      checkpoint_chunk_id="chk", now=10.0)
        assert result.ok is False and result.reason == "lease_lost"
        assert result.task is not None and result.task.owner == "other"
        update_sql, params = _executed(conn)[-2]
        assert "status = 'claimed' AND lease_token = %s AND lease_expires_at >= %s" in update_sql
        assert "COALESCE(%s, checkpoint_chunk_id)" in update_sql
        assert params == (40.0, 10.0, "chk", 10.0, "tsk_abc123def456", "stale", 10.0)

    def test_owner_mutation_not_found(self) -> None:
        store, _ = _sync_pg()
        assert store.release_task("tsk_none", "tok", now=1.0).reason == "not_found"

    def test_complete_writes_status_chunks_and_turn(self) -> None:
        store, conn = _sync_pg()
        conn.cursor.return_value.fetchall.side_effect = [
            [], [_pg_row(status="done", owner="w", result_chunk_ids=["c1", "c2"], result_turn_id="t1", version=3)],
        ]
        result = store.complete_task("tsk_abc123def456", "tok", status="done", summary="ok",
                                     result_chunk_ids=["c1", "c2"], turn_id="t1", now=5.0)
        assert result.ok and result.task is not None and result.task.result_chunk_ids == ["c1", "c2"]
        sql, params = _executed(conn)[-1]
        assert "result_chunk_ids = %s::jsonb" in sql and "lease_token = NULL" in sql
        assert params[:4] == ("done", json.dumps(["c1", "c2"]), "t1", None)

    def test_jsonb_text_values_are_decoded(self) -> None:
        store, conn = _sync_pg()
        conn.cursor.return_value.fetchall.side_effect = [[], [_pg_row(result_chunk_ids='["x"]')]]
        assert store.get_task("tsk_abc123def456", now=1.0).result_chunk_ids == ["x"]  # type: ignore[union-attr]

    def test_cancel_checks_creator_under_row_lock(self) -> None:
        store, conn = _sync_pg()
        conn.cursor.return_value.fetchall.side_effect = [[], [_pg_row(created_by="router")]]
        denied = store.cancel_task("tsk_abc123def456", "intruder", now=1.0)
        assert denied.reason == "not_allowed"
        assert "FOR UPDATE" in _executed(conn)[-1][0]
        assert not any("status = 'cancelled'" in sql for sql, _ in _executed(conn))

    def test_cancel_claimed_reports_previous_owner(self) -> None:
        store, conn = _sync_pg()
        conn.cursor.return_value.fetchall.side_effect = [
            [], [_pg_row(status="claimed", owner="w", lease_token="t" * 32)],
            [_pg_row(status="cancelled", owner="w")],
        ]
        result = store.cancel_task("tsk_abc123def456", "router", now=1.0)
        assert result.ok and result.previous_status == "claimed" and result.previous_owner == "w"

    def test_cancel_terminal(self) -> None:
        store, conn = _sync_pg()
        conn.cursor.return_value.fetchall.side_effect = [[], [_pg_row(status="done")]]
        assert store.cancel_task("tsk_abc123def456", "router", now=1.0).reason == "terminal"

    def test_list_is_bounded_and_ordered(self) -> None:
        store, conn = _sync_pg()
        store.list_tasks(pipeline_id=PIPE, status="open", slot="s", owner="o", limit=9999, now=1.0)
        sql, params = _executed(conn)[-1]
        assert "ORDER BY created_at, task_id LIMIT %s" in sql
        assert params == (PIPE, "open", "s", "o", 50)

    def test_expire_returns_transitioned_rows(self) -> None:
        store, conn = _sync_pg()
        conn.cursor.return_value.fetchall.side_effect = [
            [_pg_row(status="failed", error="lease_expired", attempt=3, version=4)]
        ]
        [row] = store.expire_task_leases(PIPE, now=99.0)
        assert row.status == "failed" and row.error == "lease_expired"
        sql, params = _executed(conn)[-1]
        assert "CASE WHEN attempt >= max_attempts THEN 'failed' ELSE 'open' END" in sql
        assert "RETURNING" in sql and params == (99.0, 99.0, PIPE)

    def test_table_prefix_and_schema_are_applied(self) -> None:
        conn = MagicMock()
        conn.cursor.return_value.fetchall.return_value = []
        conn.cursor.return_value.description = []
        store = PgvectorStore("postgresql://x/y", schema="myschema", table_prefix="mine_",
                              connect_factory=lambda dsn: conn)
        store.get_task("tsk_1", now=1.0)
        assert all("myschema.mine_tasks" in sql for sql, _ in _executed(conn)[-2:])

    def test_pg_store_is_a_task_store_and_sqlite_defaults_are_overridden(self) -> None:
        for name in ("create_task", "claim_task", "heartbeat_task", "release_task", "complete_task",
                     "cancel_task", "get_task", "list_tasks", "expire_task_leases"):
            assert getattr(PgvectorStore, name) is not getattr(BaseStore, name)
            assert getattr(SQLiteStore, name) is not getattr(BaseStore, name)


def _async_pg() -> tuple[Any, MagicMock]:
    pytest.importorskip("psycopg_pool", reason="pgvector extra not installed")
    from ncp.stores.pgvector_async import AsyncPgvectorStore

    pool = MagicMock()
    conn = MagicMock()
    conn.commit = AsyncMock()
    conn.rollback = AsyncMock()
    cursor = MagicMock()
    cursor.execute = AsyncMock()
    cursor.fetchall = AsyncMock(return_value=[])
    cursor.description = []
    cursor.__aenter__ = AsyncMock(return_value=cursor)
    cursor.__aexit__ = AsyncMock(return_value=False)
    conn.cursor.return_value = cursor
    pool.open = AsyncMock()
    pool.connection.return_value.__aenter__ = AsyncMock(return_value=conn)
    pool.connection.return_value.__aexit__ = AsyncMock(return_value=False)
    with patch("psycopg_pool.AsyncConnectionPool", return_value=pool):
        store = AsyncPgvectorStore("postgresql://localhost/test")
    store._apool = pool
    return store, cursor


@pytest.mark.anyio
class TestAsyncPostgresStatements:
    async def test_claim_uses_skip_locked(self) -> None:
        store, cursor = _async_pg()
        cursor.fetchall.side_effect = [[], [_pg_row(status="claimed", owner="w", attempt=1, version=1,
                                                    lease_token="b" * 32)]]
        claimed = await store.async_claim_task(agent_id="w", pipeline_id=PIPE, now=1.0)
        assert claimed is not None and claimed.owner == "w"
        sqls = [c.args[0] for c in cursor.execute.call_args_list]
        assert "lease_expires_at < %s" in sqls[-2] and "FOR UPDATE SKIP LOCKED" in sqls[-1]

    async def test_create_dedupe_conflict_returns_existing(self) -> None:
        store, cursor = _async_pg()
        cursor.fetchall.side_effect = [[], [_pg_row(task_id="tsk_existing0001", dedupe_key="k")]]
        task, created = await store.async_create_task(_record(dedupe_key="k"))
        assert created is False and task.task_id == "tsk_existing0001"

    async def test_owner_actions_report_lease_lost(self) -> None:
        store, cursor = _async_pg()
        cursor.fetchall.side_effect = [[], [], [_pg_row(status="claimed", owner="x")]]
        lost = await store.async_heartbeat_task("tsk_abc123def456", "stale", now=1.0)
        assert lost.ok is False and lost.reason == "lease_lost"
        cursor.fetchall.side_effect = [[], [], []]
        assert (await store.async_complete_task("tsk_none", "t", status="done", now=1.0)).reason == "not_found"

    async def test_cancel_get_list_expire(self) -> None:
        store, cursor = _async_pg()
        cursor.fetchall.side_effect = [[], [_pg_row(created_by="router")]]
        assert (await store.async_cancel_task("tsk_abc123def456", "intruder", now=1.0)).reason == "not_allowed"
        cursor.fetchall.side_effect = [[], [_pg_row()]]
        assert (await store.async_get_task("tsk_abc123def456", now=1.0)) is not None
        cursor.fetchall.side_effect = [[], [_pg_row(), _pg_row(task_id="tsk_second000001")]]
        assert len(await store.async_list_tasks(pipeline_id=PIPE, now=1.0)) == 2
        cursor.fetchall.side_effect = [[_pg_row(status="failed", error="lease_expired")]]
        [expired] = await store.async_expire_task_leases(PIPE, now=1.0)
        assert expired.error == "lease_expired"

    async def test_release_and_complete_sql(self) -> None:
        store, cursor = _async_pg()
        cursor.fetchall.side_effect = [[], [_pg_row(status="open")]]
        assert (await store.async_release_task("tsk_abc123def456", "tok", error="e", now=1.0)).ok
        assert "max_attempts_exhausted" in cursor.execute.call_args_list[-1].args[0]
        cursor.fetchall.side_effect = [[], [_pg_row(status="done")]]
        assert (await store.async_complete_task("tsk_abc123def456", "tok", status="done", now=1.0)).ok

    def test_sync_methods_on_async_store_are_not_the_sqlite_path(self) -> None:
        pytest.importorskip("psycopg_pool", reason="pgvector extra not installed")
        from ncp.stores.pgvector_async import AsyncPgvectorStore

        # The async store does not override the sync entry points; they fall
        # back to the "not supported" default instead of silently misbehaving.
        assert AsyncPgvectorStore.create_task is BaseStore.create_task
        assert AsyncPgvectorStore.async_claim_task is not BaseStore.async_claim_task


# ---------------------------------------------------------------------------
# Postgres: live database (skipped unless a DSN is provided)

LIVE_DSN = os.getenv("NCP_TASKS_PG_DSN")


def _live_available() -> bool:
    return bool(LIVE_DSN) and importlib.util.find_spec("psycopg") is not None


class _LedgerOnlyPgStore(PgvectorStore):
    """Real PgvectorStore whose schema init applies only migration 018.

    Uses the migration file itself (not the template) so the SQL a deployment
    runs is the SQL under test. Needs no pgvector extension.
    """

    def _init_db(self) -> None:
        up, _ = MigrationRunner.parse_sections(MIGRATION.read_text())
        with self._connect() as connection:
            cursor = connection.cursor()
            try:
                cursor.execute(self._sql(up))
            finally:
                self._close_cursor(cursor)


@pytest.mark.skipif(not _live_available(), reason="set NCP_TASKS_PG_DSN (and install psycopg) to run live Postgres tests")
class TestPostgresLive:
    @pytest.fixture
    def pg(self) -> Any:
        import psycopg

        schema = f"ncp_tasks_{uuid4().hex[:8]}"
        admin = psycopg.connect(LIVE_DSN, autocommit=True)
        admin.execute(f"CREATE SCHEMA {schema}")
        store = _LedgerOnlyPgStore(
            LIVE_DSN, schema=schema, table_prefix="t_", connect_factory=lambda dsn: psycopg.connect(dsn)
        )
        try:
            yield store
        finally:
            admin.execute(f"DROP SCHEMA {schema} CASCADE")
            admin.close()

    def test_state_machine_and_fencing(self, pg: Any) -> None:
        task, created = pg.create_task(_record(max_attempts=2))
        assert created
        first = pg.claim_task(agent_id="slow", task_id=task.task_id, lease_seconds=10, now=100.0)
        second = pg.claim_task(agent_id="fast", task_id=task.task_id, lease_seconds=10, now=120.0)
        assert first is not None and second is not None and second.attempt == 2
        assert pg.heartbeat_task(task.task_id, first.lease_token, now=121.0).reason == "lease_lost"
        beat = pg.heartbeat_task(task.task_id, second.lease_token, checkpoint_chunk_id="c", now=121.0)
        assert beat.ok and beat.task.checkpoint_chunk_id == "c"
        done = pg.complete_task(task.task_id, second.lease_token, status="done", summary="ok",
                                result_chunk_ids=["c1", "c2"], turn_id="t1", now=122.0)
        assert done.ok and done.task.result_chunk_ids == ["c1", "c2"] and done.task.status == "done"
        assert isinstance(done.task.lease_expires_at, float | type(None))

    def test_expiry_open_then_failed(self, pg: Any) -> None:
        task, _ = pg.create_task(_record(max_attempts=2))
        for expected in ("open", "failed"):
            assert pg.claim_task(agent_id="w", task_id=task.task_id, lease_seconds=10, now=1000.0) is not None
            [row] = pg.expire_task_leases(PIPE, now=2000.0)
            assert row.status == expected
        assert row.error == "lease_expired"

    def test_float_timestamps_keep_full_precision(self, pg: Any) -> None:
        stamp = 1789578308.8168755
        task, _ = pg.create_task(_record(created_at=stamp, updated_at=stamp))
        assert pg.get_task(task.task_id, now=1.0).created_at == stamp

    def test_dedupe_cancel_list(self, pg: Any) -> None:
        a, ca = pg.create_task(_record(dedupe_key="k"))
        b, cb = pg.create_task(_record(dedupe_key="k"))
        other, co = pg.create_task(_record(pipeline_id=OTHER_PIPE, dedupe_key="k"))
        assert ca and not cb and co and a.task_id == b.task_id != other.task_id
        assert pg.cancel_task(a.task_id, "intruder").reason == "not_allowed"
        assert pg.cancel_task(a.task_id, "router").task.status == "cancelled"
        assert pg.cancel_task(a.task_id, "router").reason == "terminal"
        assert [t.task_id for t in pg.list_tasks(pipeline_id=PIPE)] == [a.task_id]

    def test_concurrent_claim_has_one_winner(self, pg: Any) -> None:
        task, _ = pg.create_task(_record())
        results = _run_threads(
            8, lambda i: pg.claim_task(agent_id=f"w{i}", task_id=task.task_id, lease_seconds=60)
        )
        assert sum(1 for r in results if r is not None) == 1

    def test_concurrent_claim_next_never_double_claims(self, pg: Any) -> None:
        ids = {pg.create_task(_record(title=f"j{i}"))[0].task_id for i in range(20)}

        def drain(i: int) -> list[TaskRecord]:
            mine: list[TaskRecord] = []
            while (got := pg.claim_task(agent_id=f"w{i}", pipeline_id=PIPE, lease_seconds=60)) is not None:
                mine.append(got)
            return mine

        claimed = [t for batch in _run_threads(8, drain) for t in batch]
        assert len(claimed) == 20 and {t.task_id for t in claimed} == ids

    def test_concurrent_dedupe_creates(self, pg: Any) -> None:
        results = _run_threads(8, lambda i: pg.create_task(_record(dedupe_key="same", title=f"t{i}")))
        assert sum(1 for _, created in results if created) == 1
        assert len(pg.list_tasks(pipeline_id=PIPE)) == 1

    def test_migration_down_drops_table(self, pg: Any) -> None:
        import psycopg

        _, down = MigrationRunner.parse_sections(MIGRATION.read_text())
        with psycopg.connect(LIVE_DSN, autocommit=True) as conn:
            conn.execute(pg._sql(down))
            exists = conn.execute("SELECT to_regclass(%s)", (pg._sql("{schema}.{prefix}tasks"),)).fetchone()[0]
        assert exists is None

    @pytest.mark.anyio
    async def test_async_store_end_to_end(self, pg: Any) -> None:
        pytest.importorskip("psycopg_pool")
        from ncp.stores.pgvector_async import AsyncPgvectorStore

        class _AsyncLedger(AsyncPgvectorStore):
            async def _ainit_db(self) -> None:
                up, _ = MigrationRunner.parse_sections(MIGRATION.read_text())
                async with self._aconnect() as conn:
                    async with conn.cursor() as cur:
                        await cur.execute(self._sql(up))

        store = _AsyncLedger(LIVE_DSN, schema=pg.schema, table_prefix=pg.table_prefix)
        await store.open()
        try:
            task, created = await store.async_create_task(_record(dedupe_key="a"))
            again, created_again = await store.async_create_task(_record(dedupe_key="a"))
            assert created and not created_again and again.task_id == task.task_id
            claimed = await store.async_claim_task(agent_id="w", pipeline_id=PIPE, lease_seconds=30)
            assert claimed is not None and claimed.owner == "w"
            stale = await store.async_heartbeat_task(task.task_id, "0" * 32)
            assert stale.reason == "lease_lost"
            done = await store.async_complete_task(task.task_id, claimed.lease_token, status="done",
                                                   summary="ok", result_chunk_ids=["c"])
            assert done.ok and done.task.result_chunk_ids == ["c"]
            assert [t.status for t in await store.async_list_tasks(pipeline_id=PIPE)] == ["done"]
            assert (await store.async_cancel_task(task.task_id, "router")).reason == "terminal"
            assert await store.async_expire_task_leases(PIPE) == []
        finally:
            await store.close()
