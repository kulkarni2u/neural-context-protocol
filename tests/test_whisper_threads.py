"""Coordination primitives A: request/reply whisper threads.

See docs/specs/2026-11-02-ncp-coordination-primitives-design.md section 3.
"""

from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path
from typing import Any

import pytest

from ncp.config import load_config
from ncp.encoder import PidginEncoder
from ncp.mcp.server import _handle_request, make_handlers
from ncp.stores.redis_coordination import AsyncRedisCoordination, RedisCoordination
from ncp.stores.sqlite import SQLiteStore
from ncp.types import (
    ProgressPayload,
    ResultPayload,
    Whisper,
    normalize_whisper_payload,
    resolve_whisper_thread,
)

PIPE = "pipe_threads"


def _request(**overrides: Any) -> Whisper:
    fields: dict[str, Any] = {
        "from_agent": "router",
        "target": "reviewer",
        "whisper_type": "request",
        "payload": {"ask": "review the diff"},
        "confidence": 0.9,
        "pipeline_id": PIPE,
    }
    fields.update(overrides)
    return Whisper(**fields)


def _progress(thread_or_parent: str, pct: int, **overrides: Any) -> Whisper:
    fields: dict[str, Any] = {
        "from_agent": "reviewer",
        "target": "router",
        "whisper_type": "progress",
        "payload": {"status": "working", "pct": pct},
        "confidence": 0.9,
        "pipeline_id": PIPE,
        "in_reply_to": thread_or_parent,
    }
    fields.update(overrides)
    return Whisper(**fields)


def _result(parent: str, **overrides: Any) -> Whisper:
    fields: dict[str, Any] = {
        "from_agent": "reviewer",
        "target": "router",
        "whisper_type": "result",
        "payload": {"status": "failed", "summary": "race in retry loop"},
        "confidence": 0.9,
        "pipeline_id": PIPE,
        "in_reply_to": parent,
    }
    fields.update(overrides)
    return Whisper(**fields)


# ---------------------------------------------------------------------------
# Types


class TestPayloadsAndValidation:
    def test_progress_payload_bounds(self) -> None:
        assert ProgressPayload(status="working", pct=40).pct == 40
        with pytest.raises(ValueError):
            ProgressPayload(status="working", pct=101)
        with pytest.raises(ValueError):
            ProgressPayload(status="working", note="x" * 201)
        with pytest.raises(ValueError):
            ProgressPayload(status="done")  # type: ignore[arg-type]

    def test_result_payload_bounds(self) -> None:
        payload = ResultPayload(status="succeeded", summary="ok", result_chunk_ids=["sub_a"])
        assert payload.result_chunk_ids == ["sub_a"]
        with pytest.raises(ValueError):
            ResultPayload(status="succeeded", summary="  ")
        with pytest.raises(ValueError):
            ResultPayload(status="succeeded", summary="x" * 301)
        with pytest.raises(ValueError):
            ResultPayload(status="succeeded", summary="ok", result_chunk_ids=[f"c{i}" for i in range(11)])
        with pytest.raises(ValueError):
            ResultPayload(status="succeeded", summary="ok", result_chunk_ids=["has space"])

    def test_reply_types_require_linkage(self) -> None:
        with pytest.raises(ValueError, match="in_reply_to or thread_id"):
            _result("wsp_x", in_reply_to=None)
        assert _result("wsp_x", in_reply_to=None, thread_id="wsp_t").thread_id == "wsp_t"

    @pytest.mark.parametrize("whisper_type", ["progress", "result"])
    def test_reply_types_cannot_broadcast(self, whisper_type: str) -> None:
        builder = _progress if whisper_type == "progress" else _result
        kwargs: dict[str, Any] = {"target": "*"}
        if whisper_type == "progress":
            with pytest.raises(ValueError, match="cannot target"):
                builder("wsp_x", 10, **kwargs)  # type: ignore[call-arg]
        else:
            with pytest.raises(ValueError, match="cannot target"):
                builder("wsp_x", **kwargs)  # type: ignore[call-arg]

    def test_reply_payloads_are_structured_only(self) -> None:
        with pytest.raises(ValueError):
            normalize_whisper_payload("result", "it worked")
        canonical, fmt = normalize_whisper_payload("result", {"status": "succeeded", "summary": "ok"})
        assert fmt == "structured-v1"
        assert json.loads(canonical)["status"] == "succeeded"

    def test_link_ids_reject_spaces(self) -> None:
        with pytest.raises(ValueError):
            _result("wsp x")


class TestResolveThread:
    def test_request_opens_thread_on_own_id(self) -> None:
        request = _request()
        assert resolve_whisper_thread(request).thread_id == request.whisper_id

    def test_unthreaded_types_stay_unthreaded(self) -> None:
        nudge = _request(whisper_type="nudge", payload="heads up")
        assert resolve_whisper_thread(nudge).thread_id is None

    def test_reply_joins_queued_parent_thread(self) -> None:
        parent = _request(thread_id="wsp_root")
        reply = _result(parent.whisper_id)
        assert resolve_whisper_thread(reply, parent).thread_id == "wsp_root"

    def test_reply_falls_back_to_in_reply_to(self) -> None:
        reply = _result("wsp_gone")
        assert resolve_whisper_thread(reply, None).thread_id == "wsp_gone"

    def test_explicit_thread_is_kept(self) -> None:
        reply = _result("wsp_parent", thread_id="wsp_explicit")
        assert resolve_whisper_thread(reply, _request()).thread_id == "wsp_explicit"

    def test_mismatched_parent_is_ignored(self) -> None:
        reply = _result("wsp_parent")
        unrelated = _request(thread_id="wsp_other")
        assert resolve_whisper_thread(reply, unrelated).thread_id == "wsp_parent"


# ---------------------------------------------------------------------------
# SQLite store


@pytest.fixture
def store(tmp_path: Path) -> SQLiteStore:
    return SQLiteStore(tmp_path / "store.db")


class TestSQLiteThreads:
    def test_request_is_stored_with_own_thread(self, store: SQLiteStore) -> None:
        request = _request()
        store.emit_whisper(request)
        stored = store.get_whisper(request.whisper_id)
        assert stored is not None
        assert stored.thread_id == request.whisper_id

    def test_reply_joins_parent_thread_while_parent_queued(self, store: SQLiteStore) -> None:
        parent = _request(thread_id="wsp_root")
        store.emit_whisper(parent)
        reply = _result(parent.whisper_id)
        store.emit_whisper(reply)
        stored = store.get_whisper(reply.whisper_id)
        assert stored is not None
        assert stored.in_reply_to == parent.whisper_id
        assert stored.thread_id == "wsp_root"

    def test_reply_after_parent_acknowledged_is_accepted(self, store: SQLiteStore) -> None:
        parent = _request()
        store.emit_whisper(parent)
        drained = store.drain_whispers(agent_id="reviewer", pipeline_id=PIPE)
        assert [w.whisper_id for w in drained] == [parent.whisper_id]
        assert store.get_whisper(parent.whisper_id) is None

        reply = _result(parent.whisper_id)
        store.emit_whisper(reply)
        delivered = store.drain_whispers(agent_id="router", pipeline_id=PIPE)
        assert len(delivered) == 1
        assert delivered[0].in_reply_to == parent.whisper_id
        assert delivered[0].thread_id == parent.whisper_id

    def test_progress_coalesces_on_same_thread(self, store: SQLiteStore) -> None:
        for pct in (10, 50, 90):
            store.emit_whisper(_progress("wsp_req", pct))
        queued = store.peek_whispers(agent_id="router", pipeline_id=PIPE, max_items=10)
        assert len(queued) == 1
        assert json.loads(queued[0].payload)["pct"] == 90

    def test_progress_coalescing_is_scoped(self, store: SQLiteStore) -> None:
        store.emit_whisper(_progress("wsp_a", 10))
        store.emit_whisper(_progress("wsp_b", 10))  # other thread
        store.emit_whisper(_progress("wsp_a", 10, from_agent="tester"))  # other sender
        store.emit_whisper(_progress("wsp_a", 10, pipeline_id="pipe_other"))  # other pipeline
        store.emit_whisper(_result("wsp_a"))  # results never coalesce
        store.emit_whisper(_progress("wsp_a", 60))
        queued = store.peek_whispers(agent_id="router", pipeline_id=PIPE, max_items=10)
        kinds = sorted((w.from_agent, w.thread_id, w.whisper_type) for w in queued)
        assert kinds == [
            ("reviewer", "wsp_a", "progress"),
            ("reviewer", "wsp_a", "result"),
            ("reviewer", "wsp_b", "progress"),
            ("tester", "wsp_a", "progress"),
        ]
        assert len(store.peek_whispers(agent_id="router", pipeline_id="pipe_other", max_items=10)) == 1

    def test_result_skips_confidence_filter_and_orders_after_alert(self, store: SQLiteStore) -> None:
        now = time.time()
        store.emit_whisper(
            _request(target="router", from_agent="planner", created_at=now - 30, thread_id="wsp_n")
        )
        store.emit_whisper(_result("wsp_req", confidence=0.1, created_at=now - 20))
        store.emit_whisper(
            Whisper(
                from_agent="monitor",
                target="router",
                whisper_type="alert",
                payload={"alert_code": "ci_red", "description": "main is failing"},
                confidence=0.2,
                pipeline_id=PIPE,
                created_at=now - 10,
            )
        )
        store.emit_whisper(_progress("wsp_req", 5, confidence=0.1, created_at=now - 5))
        queued = store.peek_whispers(agent_id="router", pipeline_id=PIPE, max_items=10)
        assert [w.whisper_type for w in queued] == ["alert", "result", "request"]

    def test_legacy_database_gains_thread_columns(self, tmp_path: Path) -> None:
        path = tmp_path / "legacy.db"
        connection = sqlite3.connect(path)
        connection.execute(
            """
            CREATE TABLE whispers (
                whisper_id TEXT PRIMARY KEY, pipeline_id TEXT, from_agent TEXT NOT NULL,
                target TEXT NOT NULL, whisper_type TEXT NOT NULL, payload TEXT NOT NULL,
                confidence REAL NOT NULL, ref TEXT, created_at REAL NOT NULL,
                expires_at REAL NOT NULL
            )
            """
        )
        now = time.time()
        connection.execute(
            "INSERT INTO whispers VALUES ('wsp_old', ?, 'a', 'b', 'nudge', 'hi', 0.9, NULL, ?, ?)",
            (PIPE, now, now + 600),
        )
        connection.commit()
        connection.close()

        legacy = SQLiteStore(path)
        old = legacy.get_whisper("wsp_old")
        assert old is not None
        assert old.thread_id is None and old.in_reply_to is None
        reply = _result("wsp_old", target="a", from_agent="b")
        legacy.emit_whisper(reply)
        stored = legacy.get_whisper(reply.whisper_id)
        assert stored is not None and stored.thread_id == "wsp_old"


# ---------------------------------------------------------------------------
# Encoder


class TestRendering:
    def _header(self, whisper: Whisper) -> str:
        return PidginEncoder()._encode_whisper_entry(whisper, now=whisper.created_at).split("\n")[0]

    def test_unthreaded_whisper_renders_unchanged(self) -> None:
        nudge = _request(whisper_type="nudge", payload="heads up")
        assert self._header(nudge) == "wsp from:router to:reviewer t:nudge c:0.9 age:<1m"

    def test_thread_opener_shows_id(self) -> None:
        request = resolve_whisper_thread(_request())
        assert self._header(request).endswith(f"id:{request.whisper_id}")
        assert "th:" not in self._header(request)

    def test_reply_shows_id_and_re_without_redundant_th(self) -> None:
        reply = resolve_whisper_thread(_result("wsp_parent"))
        header = self._header(reply)
        assert header.endswith(f"id:{reply.whisper_id} re:wsp_parent")

    def test_th_shown_when_it_adds_information(self) -> None:
        reply = _result("wsp_parent", thread_id="wsp_root")
        assert self._header(reply).endswith(f"id:{reply.whisper_id} re:wsp_parent th:wsp_root")


# ---------------------------------------------------------------------------
# MCP tool


def _call(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    return {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {"name": name, "arguments": arguments},
    }


def _content(response: str) -> dict[str, Any]:
    return json.loads(json.loads(response)["result"]["content"][0]["text"])


class TestEmitWhisperTool:
    def test_request_reply_round_trip(self, store: SQLiteStore, tmp_path: Path) -> None:
        handlers = make_handlers(store, config=load_config(cwd=tmp_path))
        sent = _content(
            _handle_request(
                _call(
                    "ncp_emit_whisper",
                    {
                        "from": "router",
                        "target": "reviewer",
                        "type": "request",
                        "payload": {"ask": "review PR 142"},
                        "confidence": 0.9,
                        "pipeline_id": PIPE,
                    },
                ),
                handlers,
            )
        )
        assert sent["thread_id"] == sent["whisper_id"]

        reply = _content(
            _handle_request(
                _call(
                    "ncp_emit_whisper",
                    {
                        "from": "reviewer",
                        "target": "router",
                        "type": "result",
                        "payload": {
                            "status": "succeeded",
                            "summary": "approved",
                            "result_chunk_ids": ["sub_review"],
                        },
                        "confidence": 0.4,
                        "pipeline_id": PIPE,
                        "in_reply_to": sent["whisper_id"],
                    },
                ),
                handlers,
            )
        )
        assert reply["thread_id"] == sent["whisper_id"]

        delivered = store.drain_whispers(agent_id="router", pipeline_id=PIPE)
        assert [w.whisper_id for w in delivered] == [reply["whisper_id"]]
        assert delivered[0].in_reply_to == sent["whisper_id"]

    def test_unlinked_result_is_rejected(self, store: SQLiteStore, tmp_path: Path) -> None:
        handlers = make_handlers(store, config=load_config(cwd=tmp_path))
        response = json.loads(
            _handle_request(
                _call(
                    "ncp_emit_whisper",
                    {
                        "from": "reviewer",
                        "target": "router",
                        "type": "result",
                        "payload": {"status": "succeeded", "summary": "ok"},
                        "confidence": 0.9,
                        "pipeline_id": PIPE,
                    },
                ),
                handlers,
            )
        )
        failed = "error" in response or response["result"].get("isError")
        assert failed
        assert "in_reply_to or thread_id" in json.dumps(response)


# ---------------------------------------------------------------------------
# Redis coordination (sync and async) with in-memory fakes


class _FakeRedis:
    def __init__(self) -> None:
        self.hashes: dict[str, dict[str, str]] = {}
        self.sorted_sets: dict[str, dict[str, float]] = {}

    def hset(self, key: str, mapping: dict[str, str]) -> None:
        self.hashes.setdefault(key, {}).update(mapping)

    def hgetall(self, key: str) -> dict[str, str]:
        return dict(self.hashes.get(key, {}))

    def expire(self, key: str, ttl_seconds: int) -> None:
        return None

    def zadd(self, key: str, mapping: dict[str, float]) -> None:
        self.sorted_sets.setdefault(key, {}).update(mapping)

    def zrange(self, key: str, start: int, end: int) -> list[str]:
        members = [m for m, _ in sorted(self.sorted_sets.get(key, {}).items(), key=lambda kv: kv[1])]
        return members[start:] if end == -1 else members[start : end + 1]

    def zrem(self, key: str, member: str) -> int:
        return int(self.sorted_sets.get(key, {}).pop(member, None) is not None)

    def delete(self, key: str) -> int:
        return int(self.hashes.pop(key, None) is not None)


class _AsyncFakeRedis:
    def __init__(self) -> None:
        self.inner = _FakeRedis()

    def __getattr__(self, name: str) -> Any:
        method = getattr(self.inner, name)

        async def call(*args: Any, **kwargs: Any) -> Any:
            return method(*args, **kwargs)

        return call


class TestRedisThreads:
    def _coordination(self) -> RedisCoordination:
        client = _FakeRedis()
        return RedisCoordination("redis://fake", client_factory=lambda url: client)

    def test_thread_fields_round_trip_and_parent_lookup(self) -> None:
        coordination = self._coordination()
        parent = _request(thread_id="wsp_root")
        coordination.emit_whisper(parent)
        reply = _result(parent.whisper_id)
        coordination.emit_whisper(reply)
        stored = coordination.get_whisper(reply.whisper_id)
        assert stored is not None
        assert stored.in_reply_to == parent.whisper_id
        assert stored.thread_id == "wsp_root"

    def test_progress_coalesces_and_result_ordering(self) -> None:
        coordination = self._coordination()
        now = time.time()
        coordination.emit_whisper(_progress("wsp_req", 10, created_at=now - 3))
        coordination.emit_whisper(_progress("wsp_req", 70, created_at=now - 2))
        coordination.emit_whisper(_result("wsp_req", confidence=0.1, created_at=now - 1))
        queued = coordination.peek_whispers(agent_id="router", pipeline_id=PIPE, max_items=10)
        assert [w.whisper_type for w in queued] == ["result", "progress"]
        assert json.loads(queued[1].payload)["pct"] == 70


@pytest.mark.anyio
async def test_async_redis_thread_resolution_and_coalescing() -> None:
    client = _AsyncFakeRedis()
    coordination = AsyncRedisCoordination("redis://fake", client_factory=lambda url: client)
    parent = _request()
    await coordination.emit_whisper(parent)
    stored_parent = await coordination.get_whisper(parent.whisper_id)
    assert stored_parent is not None and stored_parent.thread_id == parent.whisper_id

    await coordination.emit_whisper(_progress(parent.whisper_id, 20))
    await coordination.emit_whisper(_progress(parent.whisper_id, 80))
    await coordination.emit_whisper(_result(parent.whisper_id, confidence=0.1))
    drained = await coordination.drain_whispers(agent_id="router", pipeline_id=PIPE, max_items=10)
    assert [w.whisper_type for w in drained] == ["result", "progress"]
    assert all(w.thread_id == parent.whisper_id for w in drained)
    assert json.loads(drained[1].payload)["pct"] == 80
