"""Coordination primitives B: push delivery (doorbell) for whispers.

See docs/specs/2026-11-02-ncp-coordination-primitives-design.md section 4.
"""

from __future__ import annotations

import http.client
import json
import queue
import threading
import time
from pathlib import Path
from typing import Any

import pytest

from ncp import api
from ncp.config import load_config
from ncp.mcp.server import (
    MCP_TOOLS,
    _handle_request,
    create_http_server,
    make_handlers,
    tools_for_config,
)
from ncp.notify import (
    InProcessNotifier,
    NotifyEvent,
    RedisNotifier,
    create_notifier,
    emit_and_notify,
)
from ncp.stores.sqlite import SQLiteStore
from ncp.types import Whisper

PIPE = "pipe_notify"


def _whisper(**overrides: Any) -> Whisper:
    fields: dict[str, Any] = {
        "from_agent": "worker",
        "target": "router",
        "whisper_type": "nudge",
        "payload": "check the build",
        "confidence": 0.9,
        "pipeline_id": PIPE,
    }
    fields.update(overrides)
    return Whisper(**fields)


def _event(**overrides: Any) -> NotifyEvent:
    return NotifyEvent.from_whisper(_whisper(**overrides))


@pytest.fixture
def store(tmp_path: Path) -> SQLiteStore:
    return SQLiteStore(tmp_path / "store.db")


def _call(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": name, "arguments": arguments}}


def _content(response: str) -> dict[str, Any]:
    return json.loads(json.loads(response)["result"]["content"][0]["text"])


def _emit_later(store: SQLiteStore, notifier: InProcessNotifier, whisper: Whisper, delay: float = 0.15) -> threading.Thread:
    thread = threading.Thread(target=lambda: (time.sleep(delay), emit_and_notify(store, notifier, whisper)))
    thread.start()
    return thread


# ---------------------------------------------------------------------------
# Event schema and in-process notifier


class TestNotifyEvent:
    def test_no_payload_key_ever(self) -> None:
        event = _event(whisper_type="request", payload={"ask": "secret ask"})
        data = event.to_dict()
        assert set(data) == {
            "event", "whisper_id", "type", "from", "target", "pipeline_id", "thread_id", "created_at",
        }
        assert "payload" not in data
        assert "secret ask" not in event.to_json()
        assert "payload" not in NotifyEvent.model_fields

    def test_published_event_carries_no_payload(self) -> None:
        notifier = InProcessNotifier()
        with notifier.subscribe(agent_id="router", pipeline_id=PIPE) as sub:
            notifier.publish(_event(payload="top secret"))
            events = sub.poll(0.5)
        assert len(events) == 1
        assert "payload" not in events[0].to_dict()
        assert "top secret" not in events[0].to_json()


class TestInProcessNotifier:
    def test_wait_times_out_cleanly(self) -> None:
        notifier = InProcessNotifier()
        start = time.monotonic()
        assert notifier.wait(agent_id="router", pipeline_id=PIPE, timeout=0.1) == []
        assert time.monotonic() - start < 1.0

    def test_wait_wakes_on_publish_from_other_thread(self) -> None:
        notifier = InProcessNotifier()
        timer = threading.Timer(0.1, lambda: notifier.publish(_event()))
        timer.start()
        start = time.monotonic()
        events = notifier.wait(agent_id="router", pipeline_id=PIPE, timeout=5.0)
        timer.join()
        assert len(events) == 1
        assert time.monotonic() - start < 2.0

    def test_directed_event_only_reaches_target(self) -> None:
        notifier = InProcessNotifier()
        with notifier.subscribe(agent_id="router", pipeline_id=PIPE) as a, notifier.subscribe(
            agent_id="other", pipeline_id=PIPE
        ) as b:
            notifier.publish(_event(target="router"))
            assert len(a.poll(0.2)) == 1
            assert b.poll(0.05) == []

    def test_broadcast_wakes_every_subscriber_in_pipeline_only(self) -> None:
        notifier = InProcessNotifier()
        with notifier.subscribe(agent_id="a", pipeline_id=PIPE) as a, notifier.subscribe(
            agent_id="b", pipeline_id=PIPE
        ) as b, notifier.subscribe(agent_id="c", pipeline_id="elsewhere") as c, notifier.subscribe(
            agent_id="d", pipeline_id=None
        ) as d, notifier.subscribe(agent_id="sender", pipeline_id=PIPE) as sender:
            notifier.publish(_event(from_agent="sender", target="*"))
            assert len(a.poll(0.2)) == 1
            assert len(b.poll(0.2)) == 1
            assert c.poll(0.05) == []
            assert d.poll(0.05) == []
            assert sender.poll(0.05) == []  # a sender is not woken by its own broadcast

    def test_none_pipeline_matches_none_only(self) -> None:
        notifier = InProcessNotifier()
        with notifier.subscribe(agent_id="router", pipeline_id=None) as none_sub, notifier.subscribe(
            agent_id="router", pipeline_id=PIPE
        ) as pipe_sub:
            notifier.publish(_event(pipeline_id=None))
            assert len(none_sub.poll(0.2)) == 1
            assert pipe_sub.poll(0.05) == []

    def test_ring_buffer_is_bounded(self) -> None:
        notifier = InProcessNotifier()
        with notifier.subscribe(agent_id="router", pipeline_id=PIPE) as sub:
            for _ in range(1000):
                notifier.publish(_event())
            assert len(sub.poll(0.1)) <= 256


# ---------------------------------------------------------------------------
# Publish point


class TestEmitAndNotify:
    def test_publishes_after_persist(self, store: SQLiteStore) -> None:
        notifier = InProcessNotifier()
        whisper = _whisper()
        with notifier.subscribe(agent_id="router", pipeline_id=PIPE) as sub:
            emit_and_notify(store, notifier, whisper)
            events = sub.poll(0.2)
        assert [e.whisper_id for e in events] == [whisper.whisper_id]
        assert store.whisper_pending(whisper.whisper_id)

    def test_no_event_when_emit_raises(self, store: SQLiteStore, monkeypatch: pytest.MonkeyPatch) -> None:
        notifier = InProcessNotifier()

        def boom(_: Whisper) -> None:
            raise RuntimeError("store down")

        monkeypatch.setattr(store, "emit_whisper", boom)
        with notifier.subscribe(agent_id="router", pipeline_id=PIPE) as sub:
            with pytest.raises(RuntimeError):
                emit_and_notify(store, notifier, _whisper())
            assert sub.poll(0.05) == []

    def test_notifier_failure_does_not_fail_emit(self, store: SQLiteStore) -> None:
        class Broken:
            def publish(self, event: NotifyEvent) -> None:
                raise RuntimeError("redis down")

        whisper = _whisper()
        emit_and_notify(store, Broken(), whisper)  # type: ignore[arg-type]
        assert store.whisper_pending(whisper.whisper_id)

    def test_low_confidence_whisper_does_not_ring(self, store: SQLiteStore) -> None:
        notifier = InProcessNotifier()
        with notifier.subscribe(agent_id="router", pipeline_id=PIPE) as sub:
            emit_and_notify(store, notifier, _whisper(confidence=0.3))
            assert sub.poll(0.05) == []
            # Confidence-exempt types ring regardless.
            emit_and_notify(
                store,
                notifier,
                _whisper(whisper_type="alert", payload={"alert_code": "X1", "description": "x"}, confidence=0.3),
            )
            assert len(sub.poll(0.2)) == 1

    def test_event_carries_resolved_thread_id(self, store: SQLiteStore, tmp_path: Path) -> None:
        notifier = InProcessNotifier()
        handlers = make_handlers(store, config=load_config(cwd=tmp_path), notifier=notifier)
        with notifier.subscribe(agent_id="router", pipeline_id=PIPE) as sub:
            sent = _content(
                _handle_request(
                    _call(
                        "ncp_emit_whisper",
                        {"from": "router", "target": "reviewer", "type": "request",
                         "payload": {"ask": "review"}, "confidence": 0.9, "pipeline_id": PIPE},
                    ),
                    handlers,
                )
            )
            reply = _content(
                _handle_request(
                    _call(
                        "ncp_emit_whisper",
                        {"from": "reviewer", "target": "router", "type": "result",
                         "payload": {"status": "succeeded", "summary": "ok"}, "confidence": 0.9,
                         "pipeline_id": PIPE, "in_reply_to": sent["whisper_id"]},
                    ),
                    handlers,
                )
            )
            events = sub.poll(0.5)
        assert [e.thread_id for e in events] == [sent["whisper_id"]]
        assert events[0].whisper_id == reply["whisper_id"]
        assert events[0].type == "result"


# ---------------------------------------------------------------------------
# ncp_wait_whispers tool


def _wait(handlers: dict[str, Any], **args: Any) -> dict[str, Any]:
    return _content(_handle_request(_call("ncp_wait_whispers", args), handlers))


class TestWaitWhispersTool:
    @pytest.fixture
    def env(self, store: SQLiteStore, tmp_path: Path) -> tuple[SQLiteStore, InProcessNotifier, dict[str, Any]]:
        notifier = InProcessNotifier()
        handlers = make_handlers(store, config=load_config(cwd=tmp_path), notifier=notifier)
        return store, notifier, handlers

    def test_returns_immediately_when_queued(self, env: Any) -> None:
        store, notifier, handlers = env
        whisper = _whisper()
        emit_and_notify(store, notifier, whisper)
        start = time.monotonic()
        result = _wait(handlers, agent_id="router", pipeline_id=PIPE, timeout_seconds=10)
        assert time.monotonic() - start < 1.0
        assert result["ready"] is True and result["timed_out"] is False
        assert result["whispers"] == [
            {"whisper_id": whisper.whisper_id, "type": "nudge", "from": "worker",
             "thread_id": None, "in_reply_to": None}
        ]
        assert "payload" not in json.dumps(result)

    def test_does_not_drain(self, env: Any) -> None:
        store, notifier, handlers = env
        whisper = _whisper()
        emit_and_notify(store, notifier, whisper)
        _wait(handlers, agent_id="router", pipeline_id=PIPE, timeout_seconds=0)
        _wait(handlers, agent_id="router", pipeline_id=PIPE, timeout_seconds=0)
        assert store.whisper_pending(whisper.whisper_id)

    def test_times_out_cleanly(self, env: Any) -> None:
        _, _, handlers = env
        start = time.monotonic()
        result = _wait(handlers, agent_id="router", pipeline_id=PIPE, timeout_seconds=0.2)
        assert time.monotonic() - start < 1.5
        assert result == {"ready": False, "timed_out": True, "whispers": []}

    def test_zero_timeout_is_non_blocking(self, env: Any) -> None:
        _, _, handlers = env
        start = time.monotonic()
        result = _wait(handlers, agent_id="router", pipeline_id=PIPE, timeout_seconds=0)
        assert time.monotonic() - start < 0.5
        assert result["ready"] is False and result["timed_out"] is False

    def test_wakes_when_another_thread_emits(self, env: Any) -> None:
        store, notifier, handlers = env
        whisper = _whisper()
        thread = _emit_later(store, notifier, whisper)
        start = time.monotonic()
        result = _wait(handlers, agent_id="router", pipeline_id=PIPE, timeout_seconds=10)
        thread.join()
        assert time.monotonic() - start < 3.0
        assert result["ready"] is True
        assert [w["whisper_id"] for w in result["whispers"]] == [whisper.whisper_id]

    def test_timeout_is_clamped_to_max_wait(self, store: SQLiteStore, tmp_path: Path) -> None:
        config = load_config(cwd=tmp_path)
        config.values["notify"]["max_wait_seconds"] = 0.2
        handlers = make_handlers(store, config=config, notifier=InProcessNotifier())
        start = time.monotonic()
        result = _wait(handlers, agent_id="router", pipeline_id=PIPE, timeout_seconds=999)
        assert time.monotonic() - start < 2.0
        assert result["timed_out"] is True

    def test_types_filter(self, env: Any) -> None:
        store, notifier, handlers = env
        emit_and_notify(store, notifier, _whisper(whisper_type="nudge"))
        result = _wait(handlers, agent_id="router", pipeline_id=PIPE, timeout_seconds=0.1, types=["alert"])
        assert result["ready"] is False
        alert = _whisper(whisper_type="alert", payload={"alert_code": "X1", "description": "x"})
        emit_and_notify(store, notifier, alert)
        result = _wait(handlers, agent_id="router", pipeline_id=PIPE, timeout_seconds=0.1, types=["alert"])
        assert [w["whisper_id"] for w in result["whispers"]] == [alert.whisper_id]

    def test_non_matching_wake_keeps_waiting_for_matching(self, env: Any) -> None:
        store, notifier, handlers = env
        wanted = _whisper(whisper_type="alert", payload={"alert_code": "X1", "description": "x"})

        def later() -> None:
            time.sleep(0.1)
            emit_and_notify(store, notifier, _whisper(whisper_type="nudge"))
            time.sleep(0.1)
            emit_and_notify(store, notifier, wanted)

        thread = threading.Thread(target=later)
        thread.start()
        result = _wait(handlers, agent_id="router", pipeline_id=PIPE, timeout_seconds=5, types=["alert"])
        thread.join()
        assert [w["type"] for w in result["whispers"]] == ["alert"]

    def test_thread_id_filter(self, env: Any) -> None:
        store, notifier, handlers = env
        request = _whisper(from_agent="router", target="reviewer", whisper_type="request",
                           payload={"ask": "a"})
        emit_and_notify(store, notifier, request)
        result_whisper = _whisper(
            from_agent="reviewer", target="router", whisper_type="result",
            payload={"status": "succeeded", "summary": "ok"}, in_reply_to=request.whisper_id,
            thread_id=request.whisper_id,
        )
        emit_and_notify(store, notifier, result_whisper)
        emit_and_notify(store, notifier, _whisper())  # unthreaded nudge
        hit = _wait(handlers, agent_id="router", pipeline_id=PIPE, timeout_seconds=0, thread_id=request.whisper_id)
        assert [w["whisper_id"] for w in hit["whispers"]] == [result_whisper.whisper_id]
        assert hit["whispers"][0]["in_reply_to"] == request.whisper_id
        miss = _wait(handlers, agent_id="router", pipeline_id=PIPE, timeout_seconds=0, thread_id="wsp_nope")
        assert miss["ready"] is False

    def test_pipeline_scoping(self, env: Any) -> None:
        store, notifier, handlers = env
        emit_and_notify(store, notifier, _whisper(pipeline_id="other_pipe"))
        assert _wait(handlers, agent_id="router", pipeline_id=PIPE, timeout_seconds=0)["ready"] is False

    def test_low_confidence_whisper_is_not_ready(self, env: Any) -> None:
        store, notifier, handlers = env
        emit_and_notify(store, notifier, _whisper(confidence=0.2))
        assert _wait(handlers, agent_id="router", pipeline_id=PIPE, timeout_seconds=0)["ready"] is False


class TestToolVisibility:
    def test_not_in_core_profile(self, tmp_path: Path) -> None:
        config = load_config(cwd=tmp_path)
        config.values["tools"]["profile"] = "core"
        assert "ncp_wait_whispers" not in {t["name"] for t in tools_for_config(config)}

    def test_visible_in_full_profile(self, tmp_path: Path) -> None:
        names = {t["name"] for t in tools_for_config(load_config(cwd=tmp_path))}
        assert "ncp_wait_whispers" in names
        assert "ncp_wait_whispers" in {t["name"] for t in MCP_TOOLS}

    def test_hidden_when_notify_disabled(self, tmp_path: Path) -> None:
        config = load_config(cwd=tmp_path)
        config.values["notify"]["enabled"] = False
        assert "ncp_wait_whispers" not in {t["name"] for t in tools_for_config(config)}

    def test_config_defaults(self, tmp_path: Path) -> None:
        config = load_config(cwd=tmp_path)
        assert config.notify_enabled is True
        assert config.notify_backend == "auto"
        assert config.notify_max_subscribers == 64
        assert config.notify_max_wait_seconds == 60


# ---------------------------------------------------------------------------
# GET /events


class _Server:
    def __init__(self, tmp_path: Path, **kwargs: Any) -> None:
        self.server = create_http_server(host="127.0.0.1", port=0, cwd=tmp_path, **kwargs)
        self.port = self.server.server_address[1]
        self.conns: list[http.client.HTTPConnection] = []

    def __enter__(self) -> _Server:
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

    def get(self, path: str, headers: dict[str, str] | None = None) -> http.client.HTTPResponse:
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        self.conns.append(conn)
        conn.request("GET", path, headers=headers or {})
        return conn.getresponse()


def _read_frame(resp: http.client.HTTPResponse) -> tuple[str, str]:
    """Read one SSE frame, returning (event, data); comments return ('', comment)."""
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


class TestEventsEndpoint:
    def test_requires_auth_when_token_set(self, tmp_path: Path) -> None:
        with _Server(tmp_path, auth_token="s3cret") as srv:
            assert srv.get("/events?agent_id=router").status == 401
            bad = srv.get("/events?agent_id=router", {"Authorization": "Bearer wrong"})
            assert bad.status == 401
            ok = srv.get("/events?agent_id=router", {"Authorization": "Bearer s3cret"})
            assert ok.status == 200

    def test_agent_id_required(self, tmp_path: Path) -> None:
        with _Server(tmp_path) as srv:
            assert srv.get("/events").status == 400
            assert srv.get("/events?pipeline_id=x").status == 400

    def test_ready_frame_counts_queued(self, tmp_path: Path) -> None:
        with _Server(tmp_path) as srv:
            srv.server.store.emit_whisper(_whisper())
            srv.server.store.emit_whisper(_whisper())
            resp = srv.get(f"/events?agent_id=router&pipeline_id={PIPE}")
            assert resp.status == 200
            assert resp.getheader("Content-Type") == "text/event-stream"
            event, data = _read_frame(resp)
            assert event == "ready"
            assert json.loads(data) == {"queued": 2}

    def test_whisper_frame_after_emit_has_no_payload(self, tmp_path: Path) -> None:
        with _Server(tmp_path) as srv:
            resp = srv.get(f"/events?agent_id=router&pipeline_id={PIPE}")
            assert _read_frame(resp)[0] == "ready"
            whisper = _whisper(payload="do not leak this")
            emit_and_notify(srv.server.store, srv.server.notifier, whisper)
            event, data = _read_frame(resp)
            assert event == "whisper"
            body = json.loads(data)
            assert body["whisper_id"] == whisper.whisper_id
            assert body["from"] == "worker" and body["target"] == "router"
            assert "payload" not in body and "do not leak this" not in data

    def test_emit_through_mcp_tool_rings_events(self, tmp_path: Path) -> None:
        """The HTTP handlers and /events share one notifier instance."""
        with _Server(tmp_path) as srv:
            resp = srv.get(f"/events?agent_id=router&pipeline_id={PIPE}")
            assert _read_frame(resp)[0] == "ready"
            _handle_request(
                _call(
                    "ncp_emit_whisper",
                    {"from": "worker", "target": "router", "type": "nudge", "payload": "hi",
                     "confidence": 0.9, "pipeline_id": PIPE},
                ),
                srv.server.handlers,
                tools=srv.server.tools,
            )
            assert _read_frame(resp)[0] == "whisper"

    def test_keepalive_comment_frames(self, tmp_path: Path) -> None:
        with _Server(tmp_path, keepalive_seconds=0.1) as srv:
            resp = srv.get("/events?agent_id=router")
            assert _read_frame(resp)[0] == "ready"
            assert _read_frame(resp) == ("", ": keepalive")

    def test_503_above_subscriber_cap_then_slot_frees(self, tmp_path: Path) -> None:
        with _Server(tmp_path, max_subscribers=1, keepalive_seconds=0.1) as srv:
            first = srv.get("/events?agent_id=a")
            assert first.status == 200
            assert _read_frame(first)[0] == "ready"
            second = srv.get("/events?agent_id=b")
            assert second.status == 503
            assert second.getheader("Retry-After")
            # Releasing the first subscriber frees the slot.
            first.close()
            srv.conns[0].close()
            deadline = time.monotonic() + 5
            status = 503
            while status == 503 and time.monotonic() < deadline:
                time.sleep(0.1)
                status = srv.get("/events?agent_id=c").status
            assert status == 200

    def test_server_shutdown_ends_open_stream(self, tmp_path: Path) -> None:
        with _Server(tmp_path) as srv:
            resp = srv.get("/events?agent_id=router")
            assert _read_frame(resp)[0] == "ready"
        # __exit__ asserts the serve thread stopped; the stream must hit EOF.
        assert resp.readline() == b""

    def test_cors_header_on_events(self, tmp_path: Path) -> None:
        with _Server(tmp_path, cors_allowed_origins=["http://app.example"]) as srv:
            resp = srv.get("/events?agent_id=router", {"Origin": "http://app.example"})
            assert resp.getheader("Access-Control-Allow-Origin") == "http://app.example"


# ---------------------------------------------------------------------------
# Library API


class TestLibraryApi:
    def test_emit_publishes_and_wait_whispers_wakes(self, store: SQLiteStore, tmp_path: Path) -> None:
        config = load_config(cwd=tmp_path)
        whisper = _whisper()
        timer = threading.Timer(0.15, lambda: api.emit(whisper, store=store, config=config))
        timer.start()
        start = time.monotonic()
        found = api.wait_whispers(agent_id="router", pipeline_id=PIPE, timeout=5, store=store, config=config)
        timer.join()
        assert time.monotonic() - start < 3.0
        assert [w.whisper_id for w in found] == [whisper.whisper_id]

    def test_wait_whispers_timeout_returns_empty(self, store: SQLiteStore, tmp_path: Path) -> None:
        config = load_config(cwd=tmp_path)
        assert api.wait_whispers(agent_id="router", pipeline_id=PIPE, timeout=0.1, store=store, config=config) == []

    def test_subscribe_yields_events(self, store: SQLiteStore, tmp_path: Path) -> None:
        config = load_config(cwd=tmp_path)
        whisper = _whisper()
        with api.subscribe(agent_id="router", pipeline_id=PIPE, config=config, store=store) as sub:
            api.emit(whisper, store=store, config=config)
            events = sub.poll(1.0)
        assert [e.whisper_id for e in events] == [whisper.whisper_id]

    def test_subscribe_raises_when_disabled(self, tmp_path: Path) -> None:
        config = load_config(cwd=tmp_path)
        config.values["notify"]["enabled"] = False
        with pytest.raises(RuntimeError):
            api.subscribe(agent_id="router", config=config)


# ---------------------------------------------------------------------------
# Redis notifier (fake pub/sub client)


class _FakePubSub:
    def __init__(self, bus: _FakeRedis) -> None:
        self.bus = bus
        self.channels: set[str] = set()
        self.queue: queue.Queue[dict[str, Any]] = queue.Queue()
        self.closed = False

    def subscribe(self, *channels: str) -> None:
        self.channels.update(channels)
        self.bus.subscribers.append(self)

    def get_message(self, ignore_subscribe_messages: bool = False, timeout: float = 0.0) -> dict[str, Any] | None:
        try:
            return self.queue.get(timeout=timeout) if timeout else self.queue.get_nowait()
        except queue.Empty:
            return None

    def unsubscribe(self) -> None:
        self.channels.clear()

    def close(self) -> None:
        self.closed = True
        if self in self.bus.subscribers:
            self.bus.subscribers.remove(self)


class _FakeRedis:
    def __init__(self) -> None:
        self.subscribers: list[_FakePubSub] = []
        self.published: list[tuple[str, str]] = []

    def pubsub(self) -> _FakePubSub:
        return _FakePubSub(self)

    def publish(self, channel: str, data: str) -> int:
        self.published.append((channel, data))
        count = 0
        for sub in list(self.subscribers):
            if channel in sub.channels:
                sub.queue.put({"type": "message", "channel": channel, "data": data})
                count += 1
        return count


class TestRedisNotifier:
    def _notifier(self) -> tuple[RedisNotifier, _FakeRedis]:
        fake = _FakeRedis()
        return RedisNotifier("redis://fake", stream="s", client_factory=lambda _url: fake), fake

    def test_channel_naming(self) -> None:
        notifier, _ = self._notifier()
        assert notifier.channel(PIPE, "router") == f"s:notify:{PIPE}:router"
        assert notifier.channel(None, "*") == "s:notify:_:*"

    def test_publish_uses_channel_and_has_no_payload(self) -> None:
        notifier, fake = self._notifier()
        notifier.publish(_event(payload="classified"))
        channel, data = fake.published[0]
        assert channel == f"s:notify:{PIPE}:router"
        assert "payload" not in json.loads(data) and "classified" not in data

    def test_wait_receives_directed_and_broadcast(self) -> None:
        notifier, _ = self._notifier()
        with notifier.subscribe(agent_id="router", pipeline_id=PIPE) as sub:
            notifier.publish(_event(target="router"))
            notifier.publish(_event(target="*", from_agent="worker"))
            notifier.publish(_event(target="someone_else"))
            events = sub.poll(0.1)
        assert [e.target for e in events] == ["router", "*"]

    def test_broadcast_skips_sender(self) -> None:
        notifier, _ = self._notifier()
        with notifier.subscribe(agent_id="worker", pipeline_id=PIPE) as sub:
            notifier.publish(_event(target="*", from_agent="worker"))
            assert sub.poll(0.05) == []

    def test_wait_wakes_on_publish(self) -> None:
        notifier, _ = self._notifier()
        timer = threading.Timer(0.1, lambda: notifier.publish(_event()))
        timer.start()
        events = notifier.wait(agent_id="router", pipeline_id=PIPE, timeout=3.0)
        timer.join()
        assert len(events) == 1

    def test_wait_times_out(self) -> None:
        notifier, _ = self._notifier()
        assert notifier.wait(agent_id="router", pipeline_id=PIPE, timeout=0.05) == []

    def test_subscription_cleans_up(self) -> None:
        notifier, fake = self._notifier()
        with notifier.subscribe(agent_id="router", pipeline_id=PIPE):
            assert len(fake.subscribers) == 1
        assert fake.subscribers == []

    def test_malformed_message_is_ignored(self) -> None:
        notifier, fake = self._notifier()
        with notifier.subscribe(agent_id="router", pipeline_id=PIPE) as sub:
            fake.publish(notifier.channel(PIPE, "router"), "not json")
            assert sub.poll(0.05) == []


class TestCreateNotifier:
    def test_auto_is_in_process_for_sqlite(self, tmp_path: Path) -> None:
        assert isinstance(create_notifier(load_config(cwd=tmp_path)), InProcessNotifier)

    def test_memory_backend_forced(self, tmp_path: Path) -> None:
        config = load_config(cwd=tmp_path)
        config.values["notify"]["backend"] = "memory"
        assert isinstance(create_notifier(config), InProcessNotifier)

    def test_redis_backend_uses_injected_factory(self, tmp_path: Path) -> None:
        config = load_config(cwd=tmp_path)
        config.values["notify"]["backend"] = "redis"
        fake = _FakeRedis()
        notifier = create_notifier(config, client_factory=lambda _url: fake)
        assert isinstance(notifier, RedisNotifier)
        notifier.publish(_event())
        assert fake.published

    def test_auto_picks_redis_for_pgvector(self, tmp_path: Path) -> None:
        config = load_config(cwd=tmp_path)
        config.values["store"]["type"] = "pgvector"
        assert isinstance(create_notifier(config, client_factory=lambda _u: _FakeRedis()), RedisNotifier)
