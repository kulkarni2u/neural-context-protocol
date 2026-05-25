from pathlib import Path

import pytest

from ncp.stores.pgvector import PgvectorStore, infra_hint as pgvector_hint
from ncp.stores.redis import RedisStore, infra_hint as redis_hint


def test_pgvector_store_placeholder_is_explicit() -> None:
    with pytest.raises(NotImplementedError, match="planned for NCP 0.2.0"):
        PgvectorStore("postgresql://postgres:postgres@127.0.0.1:5432/ncp")


def test_redis_store_placeholder_is_explicit() -> None:
    with pytest.raises(NotImplementedError, match="planned for NCP 0.2.0"):
        RedisStore("redis://127.0.0.1:6379/0")


def test_future_store_hints_point_to_local_scripts(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    assert "infra_up.sh" in pgvector_hint(root)
    assert "infra_up.sh" in redis_hint(root)
