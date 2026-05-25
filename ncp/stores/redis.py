"""Future Redis-backed ephemeral store helpers for NCP R2."""

from __future__ import annotations

from pathlib import Path


class RedisStore:
    """Placeholder for the R2 Redis store.

    Redis is intended for ephemeral coordination concerns such as whisper fanout,
    rate-limit state, and hot-path caches. It is not the durable source of truth.
    """

    def __init__(self, url: str, *, stream: str = "ncp:whispers") -> None:
        self.url = url
        self.stream = stream
        raise NotImplementedError(
            "RedisStore is planned for NCP 0.2.0. Use compose.yaml plus scripts/infra_up.sh "
            "to start the local Redis dependency, but keep store.type=sqlite until the R2 "
            "backend implementation lands."
        )


def infra_hint(project_root: str | Path) -> str:
    root = Path(project_root)
    return (
        f"Start local Redis with {root / 'scripts' / 'infra_up.sh'} and set NCP_REDIS_URL "
        "when the Redis-backed fast-path lands."
    )
