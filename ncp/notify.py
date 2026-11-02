"""Push delivery for whispers: a doorbell, never a delivery.

See docs/specs/2026-11-02-ncp-coordination-primitives-design.md section 4.

A :class:`NotifyEvent` says "something is queued for you". It never carries
the whisper payload or any chunk content. The whisper queue plus
``ncp_get_context`` remain the only way to deliver and acknowledge whispers,
so losing a notification is harmless: a client simply peeks again.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Callable, Iterator
from contextlib import contextmanager
import json
import logging
import threading
import time
from typing import TYPE_CHECKING, Any, ContextManager, Literal, Protocol, runtime_checkable

from pydantic import ConfigDict, Field

from ncp.types import CONFIDENCE_EXEMPT_WHISPER_TYPES, NCPModel, TaskRecord, Whisper

if TYPE_CHECKING:
    from ncp.config import NCPConfig
    from ncp.stores.base import BaseStore

logger = logging.getLogger(__name__)

# The assembler drains whispers with a fixed 0.60 confidence floor
# (Assembler._peek_whispers); the doorbell mirrors it.
WHISPER_MIN_CONFIDENCE = 0.60
_RING_SIZE = 256
DEFAULT_POLL_INTERVAL = 2.0
MIN_POLL_INTERVAL = 0.25
_MAX_ANNOUNCED = 1024


class NotifyEvent(NCPModel):
    """One doorbell ring. Deliberately has no payload or content field.

    ``event="whisper"`` rings for a queued whisper. ``event="task"`` rings for
    a task-ledger state change the target should know about (a lease expired
    into ``failed``, or a claimed task was cancelled); it carries ids and the
    new status only, never a title, summary or error text.
    """

    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    event: Literal["whisper", "task"] = "whisper"
    whisper_id: str | None = None
    type: str
    from_agent: str = Field(alias="from")
    target: str
    pipeline_id: str | None = None
    thread_id: str | None = None
    task_id: str | None = None
    status: str | None = None
    created_at: float

    @classmethod
    def from_whisper(cls, whisper: Whisper) -> NotifyEvent:
        return cls(
            whisper_id=whisper.whisper_id,
            type=whisper.whisper_type,
            from_agent=whisper.from_agent,
            target=whisper.target,
            pipeline_id=whisper.pipeline_id,
            thread_id=whisper.thread_id,
            task_id=whisper.task_id,
            created_at=whisper.created_at,
        )

    @classmethod
    def from_task(cls, task: TaskRecord, *, target: str, from_agent: str = "ncp_ledger") -> NotifyEvent:
        return cls(
            event="task",
            type="task",
            from_agent=from_agent,
            target=target,
            pipeline_id=task.pipeline_id,
            task_id=task.task_id,
            status=task.status,
            created_at=task.updated_at,
        )

    def to_dict(self) -> dict[str, Any]:
        return self.model_dump(by_alias=True)

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), separators=(",", ":"))


def event_matches(event: NotifyEvent, agent_id: str, pipeline_id: str | None) -> bool:
    """Matching rule shared by every notifier (mirrors the store's whisper scoping)."""

    if event.pipeline_id != pipeline_id:
        return False
    if event.target == "*":
        return event.from_agent != agent_id
    return event.target == agent_id


def should_notify(whisper: Whisper, *, min_confidence: float = WHISPER_MIN_CONFIDENCE) -> bool:
    """Cheap publish-time pre-filter: skip whispers the confidence rule would hide.

    The reputation gate is intentionally not applied (it needs a store read),
    so the doorbell may occasionally ring for a whisper that
    ``ncp_get_context`` later filters out.
    """

    if whisper.whisper_type in CONFIDENCE_EXEMPT_WHISPER_TYPES:
        return True
    return whisper.confidence >= min_confidence


def eligible_whispers(
    store: BaseStore,
    *,
    agent_id: str,
    pipeline_id: str | None,
    types: set[str] | None = None,
    thread_id: str | None = None,
) -> list[Whisper]:
    """Peek (never drain) whispers ``ncp_get_context`` would deliver, after filters."""

    found = store.peek_whispers(
        agent_id=agent_id,
        pipeline_id=pipeline_id,
        max_items=200,
        min_confidence=WHISPER_MIN_CONFIDENCE,
    )
    return [
        w
        for w in found
        if (types is None or w.whisper_type in types) and (thread_id is None or w.thread_id == thread_id)
    ]


class Subscription(Protocol):
    def poll(self, timeout: float) -> list[NotifyEvent]:
        """Block up to ``timeout`` seconds and return buffered events (maybe empty)."""

    def __iter__(self) -> Iterator[NotifyEvent]: ...


@runtime_checkable
class Notifier(Protocol):
    def publish(self, event: NotifyEvent) -> None: ...

    def wait(self, *, agent_id: str, pipeline_id: str | None, timeout: float) -> list[NotifyEvent]: ...

    def subscribe(self, *, agent_id: str, pipeline_id: str | None) -> ContextManager[Subscription]: ...


class _InProcessSubscription:
    def __init__(self, notifier: InProcessNotifier, agent_id: str, pipeline_id: str | None) -> None:
        self._notifier = notifier
        self.agent_id = agent_id
        self.pipeline_id = pipeline_id
        self._buffer: deque[NotifyEvent] = deque(maxlen=_RING_SIZE)
        self._closed = False

    def poll(self, timeout: float) -> list[NotifyEvent]:
        cond = self._notifier._cond
        deadline = time.monotonic() + max(0.0, timeout)
        with cond:
            while not self._buffer and not self._closed:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                cond.wait(remaining)
            events = list(self._buffer)
            self._buffer.clear()
            return events

    def close(self) -> None:
        with self._notifier._cond:
            self._closed = True
            self._notifier._cond.notify_all()

    def __iter__(self) -> Iterator[NotifyEvent]:
        while not self._closed:
            yield from self.poll(1.0)


class InProcessNotifier:
    """Thread-safe in-process doorbell: one Condition, a ring buffer per subscriber."""

    def __init__(self) -> None:
        self._cond = threading.Condition()
        self._subs: list[_InProcessSubscription] = []

    def publish(self, event: NotifyEvent) -> None:
        with self._cond:
            for sub in self._subs:
                if event_matches(event, sub.agent_id, sub.pipeline_id):
                    sub._buffer.append(event)
            self._cond.notify_all()

    @contextmanager
    def subscribe(self, *, agent_id: str, pipeline_id: str | None = None) -> Iterator[_InProcessSubscription]:
        sub = _InProcessSubscription(self, agent_id, pipeline_id)
        with self._cond:
            self._subs.append(sub)
        try:
            yield sub
        finally:
            sub.close()
            with self._cond:
                if sub in self._subs:
                    self._subs.remove(sub)

    def wait(self, *, agent_id: str, pipeline_id: str | None = None, timeout: float) -> list[NotifyEvent]:
        """Wait for events published *after* this call starts.

        Callers that must not miss events published between a store peek and
        the wait should ``subscribe`` first, then peek, then ``poll``.
        """

        with self.subscribe(agent_id=agent_id, pipeline_id=pipeline_id) as sub:
            return sub.poll(timeout)


class _RedisSubscription:
    def __init__(self, pubsub: Any, agent_id: str, pipeline_id: str | None) -> None:
        self._pubsub = pubsub
        self.agent_id = agent_id
        self.pipeline_id = pipeline_id
        self._closed = False

    def _decode(self, message: Any) -> NotifyEvent | None:
        if not isinstance(message, dict) or message.get("type") not in (None, "message", "pmessage"):
            return None
        data = message.get("data")
        if isinstance(data, bytes):
            data = data.decode("utf-8", "replace")
        if not isinstance(data, str):
            return None
        try:
            event = NotifyEvent.model_validate(json.loads(data))
        except Exception:
            return None
        return event if event_matches(event, self.agent_id, self.pipeline_id) else None

    def poll(self, timeout: float) -> list[NotifyEvent]:
        """Block until the first matching event (or timeout), then drain what is buffered."""
        deadline = time.monotonic() + max(0.0, timeout)
        events: list[NotifyEvent] = []
        while not self._closed:
            remaining = 0.0 if events else max(0.0, deadline - time.monotonic())
            message = self._pubsub.get_message(ignore_subscribe_messages=True, timeout=remaining)
            if message is None:
                if events or remaining <= 0:
                    break
                continue
            event = self._decode(message)
            if event is not None:
                events.append(event)
        return events

    def close(self) -> None:
        self._closed = True
        for name in ("unsubscribe", "close"):
            try:
                getattr(self._pubsub, name)()
            except Exception:  # pragma: no cover - best effort cleanup
                logger.debug("redis pubsub %s failed", name, exc_info=True)

    def __iter__(self) -> Iterator[NotifyEvent]:
        while not self._closed:
            yield from self.poll(1.0)


class RedisNotifier:
    """Redis pub/sub doorbell on ``{stream}:notify:{pipeline_id or '_'}:{target}``."""

    def __init__(
        self,
        url: str,
        *,
        stream: str = "ncp:whispers",
        client_factory: Callable[[str], Any] | None = None,
    ) -> None:
        from ncp.stores.redis_coordination import _default_redis_factory

        self.url = url
        self.stream = stream
        self._client_factory = client_factory or _default_redis_factory
        self._client: Any | None = None
        self._lock = threading.Lock()

    def channel(self, pipeline_id: str | None, target: str) -> str:
        return f"{self.stream}:notify:{pipeline_id or '_'}:{target}"

    def _client_or_create(self) -> Any:
        with self._lock:
            if self._client is None:
                self._client = self._client_factory(self.url)
            return self._client

    def publish(self, event: NotifyEvent) -> None:
        self._client_or_create().publish(self.channel(event.pipeline_id, event.target), event.to_json())

    @contextmanager
    def subscribe(self, *, agent_id: str, pipeline_id: str | None = None) -> Iterator[_RedisSubscription]:
        pubsub = self._client_or_create().pubsub()
        pubsub.subscribe(self.channel(pipeline_id, agent_id), self.channel(pipeline_id, "*"))
        sub = _RedisSubscription(pubsub, agent_id, pipeline_id)
        try:
            yield sub
        finally:
            sub.close()

    def wait(self, *, agent_id: str, pipeline_id: str | None = None, timeout: float) -> list[NotifyEvent]:
        with self.subscribe(agent_id=agent_id, pipeline_id=pipeline_id) as sub:
            return sub.poll(timeout)


def uses_redis(config: NCPConfig) -> bool:
    """True when the effective ``[notify]`` backend is Redis pub/sub."""

    backend = config.notify_backend
    if backend == "auto":
        store_type = str(config.values.get("store", {}).get("type", "sqlite"))
        return store_type == "pgvector" and bool(config.redis_url)
    return backend == "redis"


def effective_poll_interval(config: NCPConfig | None) -> float:
    """Store re-peek interval in seconds; 0 when polling is off.

    Polling only makes sense for the in-process (memory) backend, where the
    doorbell cannot cross processes. With Redis it is always off.
    """

    if config is None:
        return DEFAULT_POLL_INTERVAL
    if uses_redis(config):
        return 0.0
    return config.notify_poll_interval_seconds


def create_notifier(
    config: NCPConfig,
    *,
    store: BaseStore | None = None,
    client_factory: Callable[[str], Any] | None = None,
) -> Notifier:
    """Build the notifier for ``[notify] backend = auto|memory|redis``.

    ``auto`` picks Redis when Redis is the whisper path (``pgvector`` store with
    a Redis URL). The default ``[redis] url`` is always non-empty, so the mere
    presence of a URL cannot be the signal for a SQLite deployment.
    """

    if not uses_redis(config):
        return InProcessNotifier()
    factory = client_factory
    coordination = getattr(store, "coordination", None)
    if factory is None and coordination is not None:
        factory = getattr(coordination, "_client_factory", None)
    return RedisNotifier(config.redis_url, stream=config.redis_stream, client_factory=factory)


_PROCESS_NOTIFIERS: dict[tuple[str, str, str], Notifier] = {}
_PROCESS_LOCK = threading.Lock()


def get_process_notifier(config: NCPConfig, *, store: BaseStore | None = None) -> Notifier | None:
    """Process-level notifier per config (cached); ``None`` when notify is disabled."""

    if not config.notify_enabled:
        return None
    key = (config.notify_backend, config.redis_url, config.redis_stream)
    with _PROCESS_LOCK:
        notifier = _PROCESS_NOTIFIERS.get(key)
        if notifier is None:
            notifier = create_notifier(config, store=store)
            _PROCESS_NOTIFIERS[key] = notifier
        return notifier


def polling_enabled(notifier: Notifier | None, poll_interval: float) -> bool:
    """Poll fallback applies only to an in-process notifier with a positive interval."""

    return notifier is not None and poll_interval > 0 and isinstance(notifier, InProcessNotifier)


def wait_for_whispers(
    store: BaseStore,
    notifier: Notifier | None,
    *,
    agent_id: str,
    pipeline_id: str | None,
    timeout: float,
    types: set[str] | None = None,
    thread_id: str | None = None,
    poll_interval: float = 0.0,
) -> list[Whisper]:
    """Peek, then wait for the doorbell up to ``timeout``; peek only, never drain.

    Subscribes before the first peek so a whisper emitted in between still
    rings. With ``poll_interval > 0`` (in-process notifier) each wait is capped
    at the interval and followed by a fresh peek, so a whisper written by
    another process, which never rings this doorbell, is found within about
    one interval. Returns ``[]`` on timeout.
    """

    def _peek() -> list[Whisper]:
        return eligible_whispers(store, agent_id=agent_id, pipeline_id=pipeline_id, types=types, thread_id=thread_id)

    if notifier is None or timeout <= 0:
        return _peek()
    interval = max(MIN_POLL_INTERVAL, poll_interval) if polling_enabled(notifier, poll_interval) else 0.0
    deadline = time.monotonic() + timeout
    with notifier.subscribe(agent_id=agent_id, pipeline_id=pipeline_id) as sub:
        while True:
            found = _peek()
            remaining = deadline - time.monotonic()
            if found or remaining <= 0:
                return found
            sub.poll(min(remaining, interval) if interval > 0 else remaining)


class PollingSubscription:
    """Subscription wrapper that adds a store-peek fallback for foreign writers.

    Doorbell events pass through. Every ``interval`` seconds the store is peeked
    and a ``whisper`` event (via ``NotifyEvent.from_whisper``, no payload) is
    synthesised for each eligible whisper id not yet announced on this
    subscription. The announced set is seeded with whispers already queued at
    creation, so they are not re-announced, and ids announced by the doorbell
    are recorded so they are never repeated. ``task`` events are never polled:
    they remain in-process-only.
    """

    def __init__(
        self,
        inner: Subscription,
        store: BaseStore,
        *,
        agent_id: str,
        pipeline_id: str | None,
        interval: float,
    ) -> None:
        self._inner = inner
        self._store = store
        self.agent_id = agent_id
        self.pipeline_id = pipeline_id
        self._interval = max(MIN_POLL_INTERVAL, interval)
        self._announced: dict[str, None] = {}
        self._closed = False
        try:
            for w in self._peek():
                self._mark(w.whisper_id)
        except Exception:
            logger.debug("poll seed peek failed", exc_info=True)
        self._next_scan = time.monotonic() + self._interval

    def _peek(self) -> list[Whisper]:
        return eligible_whispers(self._store, agent_id=self.agent_id, pipeline_id=self.pipeline_id)

    def _mark(self, whisper_id: str) -> bool:
        """Record an id; False when it was already announced."""
        if whisper_id in self._announced:
            return False
        self._announced[whisper_id] = None
        while len(self._announced) > _MAX_ANNOUNCED:
            self._announced.pop(next(iter(self._announced)))
        return True

    def _scan(self) -> list[NotifyEvent]:
        self._next_scan = time.monotonic() + self._interval
        try:
            found = self._peek()
        except Exception:
            logger.debug("poll peek failed", exc_info=True)
            return []
        return [NotifyEvent.from_whisper(w) for w in found if self._mark(w.whisper_id)]

    def poll(self, timeout: float) -> list[NotifyEvent]:
        deadline = time.monotonic() + max(0.0, timeout)
        while True:
            now = time.monotonic()
            chunk = max(0.0, min(deadline - now, self._next_scan - now))
            events: list[NotifyEvent] = []
            for event in self._inner.poll(chunk):
                if event.whisper_id is None or self._mark(event.whisper_id):
                    events.append(event)
            if time.monotonic() >= self._next_scan:
                events.extend(self._scan())
            if events or self._closed or time.monotonic() >= deadline:
                return events

    def close(self) -> None:
        self._closed = True

    def __iter__(self) -> Iterator[NotifyEvent]:
        while not self._closed:
            yield from self.poll(1.0)


@contextmanager
def subscribe_with_poll(
    notifier: Notifier,
    store: BaseStore,
    *,
    agent_id: str,
    pipeline_id: str | None,
    poll_interval: float,
) -> Iterator[Subscription]:
    """``notifier.subscribe`` plus the poll fallback when it applies (else the plain sub)."""

    with notifier.subscribe(agent_id=agent_id, pipeline_id=pipeline_id) as sub:
        if not polling_enabled(notifier, poll_interval):
            yield sub
            return
        wrapped = PollingSubscription(sub, store, agent_id=agent_id, pipeline_id=pipeline_id, interval=poll_interval)
        try:
            yield wrapped
        finally:
            wrapped.close()


def emit_and_notify(store: BaseStore, notifier: Notifier | None, whisper: Whisper) -> None:
    """Persist ``whisper`` then ring the doorbell.

    The publish happens only after ``store.emit_whisper`` returns, so a failed
    persist never rings. A notifier failure is logged and never fails the emit.
    """

    store.emit_whisper(whisper)
    notify_emitted(notifier, whisper)


def notify_task(notifier: Notifier | None, task: TaskRecord, *, target: str, from_agent: str = "ncp_ledger") -> None:
    """Ring the doorbell for a ledger state change. Never fails the caller."""

    if notifier is None:
        return
    try:
        notifier.publish(NotifyEvent.from_task(task, target=target, from_agent=from_agent))
    except Exception:
        logger.warning("task notify publish failed for %s", task.task_id, exc_info=True)


def notify_emitted(notifier: Notifier | None, whisper: Whisper) -> None:
    if notifier is None or not should_notify(whisper):
        return
    try:
        notifier.publish(NotifyEvent.from_whisper(whisper))
    except Exception:
        logger.warning("whisper notify publish failed for %s", whisper.whisper_id, exc_info=True)
