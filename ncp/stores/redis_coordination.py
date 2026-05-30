"""Redis-backed ephemeral coordination helpers for the 0.2.0 rollout."""

from __future__ import annotations

from collections.abc import Callable
import time
from typing import Any

import anyio

from ncp.stores.base import NCPStoreUnavailableError
from ncp.types import Whisper


def _default_redis_factory(url: str) -> Any:
    try:
        import redis
    except ModuleNotFoundError as exc:  # pragma: no cover - depends on optional extra
        raise NCPStoreUnavailableError(
            "Redis coordination requires the redis client. Install it with: pip install 'neural-context-protocol[redis]'"
        ) from exc
    return redis.from_url(url, decode_responses=True)


class RedisCoordination:
    """Ephemeral Redis coordination for whispers and fetch-session state."""

    def __init__(
        self,
        url: str,
        *,
        stream: str = "ncp:whispers",
        client_factory: Callable[[str], Any] | None = None,
    ) -> None:
        self.url = url
        self.stream = stream
        self._client_factory = client_factory or _default_redis_factory
        self._client: Any | None = None

    @property
    def whisper_index_prefix(self) -> str:
        return f"{self.stream}:target"

    @property
    def whisper_payload_prefix(self) -> str:
        return f"{self.stream}:payload"

    @property
    def fetch_prefix(self) -> str:
        return f"{self.stream}:fetch"

    def emit_whisper(self, whisper: Whisper) -> None:
        client = self._client_or_raise()
        whisper = Whisper.model_validate(whisper.model_dump())
        payload_key = self._payload_key(whisper.whisper_id)
        index_key = self._target_index_key(whisper.target)
        expires_at = whisper.created_at + whisper.ttl_seconds
        client.hset(
            payload_key,
            mapping={
                "whisper_id": whisper.whisper_id,
                "pipeline_id": whisper.pipeline_id or "",
                "from_agent": whisper.from_agent,
                "target": whisper.target,
                "whisper_type": whisper.whisper_type,
                "payload": whisper.payload,
                "confidence": str(whisper.confidence),
                "ref": whisper.ref or "",
                "created_at": str(whisper.created_at),
                "ttl_seconds": str(whisper.ttl_seconds),
                "expires_at": str(expires_at),
                "dissent_target": whisper.dissent_target or "",
            },
        )
        client.expire(payload_key, whisper.ttl_seconds + 5)
        client.zadd(index_key, {whisper.whisper_id: whisper.created_at})

    def peek_whispers(
        self,
        *,
        agent_id: str,
        pipeline_id: str | None = None,
        max_items: int = 3,
        min_confidence: float = 0.60,
    ) -> list[Whisper]:
        client = self._client_or_raise()
        loaded: dict[str, Whisper] = {}
        for target in (agent_id, "*"):
            for whisper_id in client.zrange(self._target_index_key(target), 0, -1):
                if whisper_id in loaded:
                    continue
                whisper = self._load_whisper(client, whisper_id)
                if whisper is None:
                    client.zrem(self._target_index_key(target), whisper_id)
                    continue
                if pipeline_id is None:
                    if whisper.pipeline_id is not None:
                        continue
                elif whisper.pipeline_id != pipeline_id:
                    continue
                if whisper.whisper_type not in {"alert", "world_check"} and whisper.confidence < min_confidence:
                    continue
                loaded[whisper_id] = whisper

        ordered = sorted(
            loaded.values(),
            key=lambda whisper: (0 if whisper.whisper_type == "alert" else 1, whisper.created_at),
        )
        return ordered[:max_items]

    def acknowledge_whispers(self, whisper_ids: list[str]) -> int:
        if not whisper_ids:
            return 0
        client = self._client_or_raise()
        deleted = 0
        for whisper_id in whisper_ids:
            payload = client.hgetall(self._payload_key(whisper_id))
            if payload:
                target = payload.get("target", "")
                if target:
                    client.zrem(self._target_index_key(target), whisper_id)
            deleted += int(bool(client.delete(self._payload_key(whisper_id))))
        return deleted

    def whisper_stats(self, *, pipeline_id: str | None = None) -> dict[str, float | int | None]:
        client = self._client_or_raise()
        count = 0
        latest: float | None = None
        for whisper_id in self._iter_whisper_ids(client):
            whisper = self._load_whisper(client, whisper_id)
            if whisper is None:
                continue
            if pipeline_id is not None and whisper.pipeline_id != pipeline_id:
                continue
            count += 1
            latest = whisper.created_at if latest is None else max(latest, whisper.created_at)
        return {"count": count, "last_activity_at": latest}

    def drain_whispers(
        self,
        *,
        agent_id: str,
        pipeline_id: str | None = None,
        max_items: int = 3,
        min_confidence: float = 0.60,
    ) -> list[Whisper]:
        whispers = self.peek_whispers(
            agent_id=agent_id,
            pipeline_id=pipeline_id,
            max_items=max_items,
            min_confidence=min_confidence,
        )
        self.acknowledge_whispers([whisper.whisper_id for whisper in whispers])
        return whispers

    def reset_fetch_session(self, session_id: str, *, pipeline_id: str | None = None, ttl_seconds: int = 3600) -> None:
        client = self._client_or_raise()
        client.hset(
            self._fetch_key(session_id),
            mapping={"fetch_count": "0", "pipeline_id": pipeline_id or ""},
        )
        client.expire(self._fetch_key(session_id), ttl_seconds)

    def claim_fetch_slot(
        self,
        session_id: str,
        *,
        pipeline_id: str | None = None,
        max_fetches: int = 3,
        ttl_seconds: int = 3600,
    ) -> tuple[int, str | None]:
        client = self._client_or_raise()
        key = self._fetch_key(session_id)
        payload = client.hgetall(key)
        current = int(payload.get("fetch_count", "0") or 0)
        if current >= max_fetches:
            raise ValueError("ncp_fetch limit reached: max 3 per session")
        resolved_pipeline = pipeline_id if pipeline_id is not None else (payload.get("pipeline_id") or None)
        updated = current + 1
        client.hset(
            key,
            mapping={
                "fetch_count": str(updated),
                "pipeline_id": resolved_pipeline or "",
            },
        )
        client.expire(key, ttl_seconds)
        return updated, resolved_pipeline

    def _payload_key(self, whisper_id: str) -> str:
        return f"{self.whisper_payload_prefix}:{whisper_id}"

    def _target_index_key(self, target: str) -> str:
        return f"{self.whisper_index_prefix}:{target}"

    def _fetch_key(self, session_id: str) -> str:
        return f"{self.fetch_prefix}:{session_id}"

    def _load_whisper(self, client: Any, whisper_id: str) -> Whisper | None:
        payload = client.hgetall(self._payload_key(whisper_id))
        if not payload:
            return None
        whisper = Whisper(
            whisper_id=str(payload["whisper_id"]),
            pipeline_id=str(payload.get("pipeline_id") or "") or None,
            from_agent=str(payload["from_agent"]),
            target=str(payload["target"]),
            whisper_type=str(payload["whisper_type"]),
            payload=str(payload["payload"]),
            confidence=float(payload["confidence"]),
            ref=str(payload.get("ref") or "") or None,
            created_at=float(payload["created_at"]),
            ttl_seconds=int(payload.get("ttl_seconds", 60)),
            dissent_target=str(payload.get("dissent_target") or "") or None,
        )
        return whisper

    def _iter_whisper_ids(self, client: Any) -> list[str]:
        pattern = f"{self.whisper_payload_prefix}:*"
        if hasattr(client, "scan_iter"):
            return [str(key).split(":")[-1] for key in client.scan_iter(match=pattern)]
        if hasattr(client, "keys"):
            return [str(key).split(":")[-1] for key in client.keys(pattern)]
        hashes = getattr(client, "hashes", None)
        if isinstance(hashes, dict):
            return [
                str(key).split(":")[-1]
                for key in hashes
                if str(key).startswith(f"{self.whisper_payload_prefix}:")
            ]
        return []

    def _client_or_raise(self, *, attempts: int = 2, delay_seconds: float = 0.1) -> Any:
        if self._client is not None:
            return self._client
        last_exc: Exception | None = None
        for attempt in range(attempts):
            try:
                self._client = self._client_factory(self.url)
                return self._client
            except NCPStoreUnavailableError:
                raise
            except Exception as exc:  # pragma: no cover - depends on runtime client
                last_exc = exc
                if attempt < attempts - 1:
                    time.sleep(delay_seconds)
        raise NCPStoreUnavailableError(
            f"Redis coordination unavailable at {self.url} after {attempts} attempts: {last_exc}"
        ) from last_exc


# ---------------------------------------------------------------------------
# Async Redis coordination — uses redis.asyncio for native async whispers
# ---------------------------------------------------------------------------

def _default_async_redis_factory(url: str) -> Any:
    try:
        import redis.asyncio as aioredis  # type: ignore[import]
    except (ModuleNotFoundError, ImportError) as exc:  # pragma: no cover
        raise NCPStoreUnavailableError(
            "AsyncRedisCoordination requires the redis client. "
            "Install it with: pip install 'neural-context-protocol[redis]'"
        ) from exc
    return aioredis.from_url(url, decode_responses=True)


class AsyncRedisCoordination:
    """Async Redis coordination for whispers using redis.asyncio — no thread shim."""

    def __init__(
        self,
        url: str,
        *,
        stream: str = "ncp:whispers",
        client_factory: Callable[[str], Any] | None = None,
    ) -> None:
        self.url = url
        self.stream = stream
        self._client_factory = client_factory or _default_async_redis_factory
        self._client: Any | None = None

    @property
    def whisper_index_prefix(self) -> str:
        return f"{self.stream}:target"

    @property
    def whisper_payload_prefix(self) -> str:
        return f"{self.stream}:payload"

    @property
    def fetch_prefix(self) -> str:
        return f"{self.stream}:fetch"

    def _payload_key(self, whisper_id: str) -> str:
        return f"{self.whisper_payload_prefix}:{whisper_id}"

    def _target_index_key(self, target: str) -> str:
        return f"{self.whisper_index_prefix}:{target}"

    async def _aclient_or_raise(self, *, attempts: int = 2, delay_seconds: float = 0.1) -> Any:
        if self._client is not None:
            return self._client
        last_exc: Exception | None = None
        for attempt in range(attempts):
            try:
                self._client = self._client_factory(self.url)
                return self._client
            except NCPStoreUnavailableError:
                raise
            except Exception as exc:  # pragma: no cover - depends on runtime client
                last_exc = exc
                if attempt < attempts - 1:
                    await anyio.sleep(delay_seconds)
        raise NCPStoreUnavailableError(
            f"Redis coordination unavailable at {self.url} after {attempts} attempts: {last_exc}"
        ) from last_exc

    async def emit_whisper(self, whisper: Whisper) -> None:
        client = await self._aclient_or_raise()
        whisper = Whisper.model_validate(whisper.model_dump())
        payload_key = self._payload_key(whisper.whisper_id)
        index_key = self._target_index_key(whisper.target)
        expires_at = whisper.created_at + whisper.ttl_seconds
        await client.hset(
            payload_key,
            mapping={
                "whisper_id": whisper.whisper_id,
                "pipeline_id": whisper.pipeline_id or "",
                "from_agent": whisper.from_agent,
                "target": whisper.target,
                "whisper_type": whisper.whisper_type,
                "payload": whisper.payload,
                "confidence": str(whisper.confidence),
                "ref": whisper.ref or "",
                "created_at": str(whisper.created_at),
                "ttl_seconds": str(whisper.ttl_seconds),
                "expires_at": str(expires_at),
                "dissent_target": whisper.dissent_target or "",
            },
        )
        await client.expire(payload_key, whisper.ttl_seconds + 5)
        await client.zadd(index_key, {whisper.whisper_id: whisper.created_at})

    async def drain_whispers(
        self,
        *,
        agent_id: str,
        pipeline_id: str | None = None,
        max_items: int = 3,
        min_confidence: float = 0.60,
    ) -> list[Whisper]:
        whispers = await self._async_peek_whispers(
            agent_id=agent_id,
            pipeline_id=pipeline_id,
            max_items=max_items,
            min_confidence=min_confidence,
        )
        await self._async_acknowledge_whispers([w.whisper_id for w in whispers])
        return whispers

    async def _async_peek_whispers(
        self,
        *,
        agent_id: str,
        pipeline_id: str | None,
        max_items: int,
        min_confidence: float,
    ) -> list[Whisper]:
        client = await self._aclient_or_raise()
        loaded: dict[str, Whisper] = {}
        for target in (agent_id, "*"):
            for whisper_id in await client.zrange(self._target_index_key(target), 0, -1):
                if whisper_id in loaded:
                    continue
                whisper = await self._async_load_whisper(client, whisper_id)
                if whisper is None:
                    await client.zrem(self._target_index_key(target), whisper_id)
                    continue
                if pipeline_id is None:
                    if whisper.pipeline_id is not None:
                        continue
                elif whisper.pipeline_id != pipeline_id:
                    continue
                if whisper.whisper_type not in {"alert", "world_check"} and whisper.confidence < min_confidence:
                    continue
                loaded[whisper_id] = whisper
        ordered = sorted(
            loaded.values(),
            key=lambda w: (0 if w.whisper_type == "alert" else 1, w.created_at),
        )
        return ordered[:max_items]

    async def _async_acknowledge_whispers(self, whisper_ids: list[str]) -> int:
        if not whisper_ids:
            return 0
        client = await self._aclient_or_raise()
        deleted = 0
        for whisper_id in whisper_ids:
            payload = await client.hgetall(self._payload_key(whisper_id))
            if payload:
                target = payload.get("target", "")
                if target:
                    await client.zrem(self._target_index_key(target), whisper_id)
            deleted += int(bool(await client.delete(self._payload_key(whisper_id))))
        return deleted

    async def _async_load_whisper(self, client: Any, whisper_id: str) -> Whisper | None:
        payload = await client.hgetall(self._payload_key(whisper_id))
        if not payload:
            return None
        return Whisper(
            whisper_id=str(payload["whisper_id"]),
            pipeline_id=str(payload.get("pipeline_id") or "") or None,
            from_agent=str(payload["from_agent"]),
            target=str(payload["target"]),
            whisper_type=str(payload["whisper_type"]),
            payload=str(payload["payload"]),
            confidence=float(payload["confidence"]),
            ref=str(payload.get("ref") or "") or None,
            created_at=float(payload["created_at"]),
            ttl_seconds=int(payload.get("ttl_seconds", 60)),
            dissent_target=str(payload.get("dissent_target") or "") or None,
        )
