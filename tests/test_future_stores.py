from pathlib import Path

import pytest

from ncp.config import NCPConfig
from ncp.stores.factory import create_store
from ncp.stores.pgvector import PgvectorStore, infra_hint as pgvector_hint
from ncp.stores.redis import RedisStore, infra_hint as redis_hint
from ncp.stores.sqlite import SQLiteStore


class _FakeCursor:
    def __init__(self) -> None:
        self.executed: list[str] = []

    def execute(self, sql: str) -> None:
        self.executed.append(sql)

    def close(self) -> None:
        return


class _FakeConnection:
    def __init__(self) -> None:
        self.cursor_instance = _FakeCursor()
        self.committed = False
        self.closed = False

    def cursor(self) -> _FakeCursor:
        return self.cursor_instance

    def commit(self) -> None:
        self.committed = True

    def rollback(self) -> None:
        return

    def close(self) -> None:
        self.closed = True


def test_pgvector_store_initializes_schema_with_fake_connection() -> None:
    fake = _FakeConnection()

    store = PgvectorStore(
        "postgresql://postgres:postgres@127.0.0.1:5432/ncp",
        schema="ncp_test",
        table_prefix="demo_",
        connect_factory=lambda dsn: fake,
    )

    assert isinstance(store, PgvectorStore)
    assert fake.committed is True
    assert fake.closed is True
    assert "CREATE EXTENSION IF NOT EXISTS vector;" in fake.cursor_instance.executed[0]
    assert "CREATE SCHEMA IF NOT EXISTS ncp_test;" in fake.cursor_instance.executed[0]
    assert "demo_chunks" in fake.cursor_instance.executed[0]


def test_pgvector_store_rejects_invalid_identifiers() -> None:
    with pytest.raises(ValueError, match="schema"):
        PgvectorStore(
            "postgresql://postgres:postgres@127.0.0.1:5432/ncp",
            schema="bad-name",
            connect_factory=lambda dsn: _FakeConnection(),
        )


def test_redis_store_placeholder_is_explicit() -> None:
    with pytest.raises(NotImplementedError, match="planned for NCP 0.2.0"):
        RedisStore("redis://127.0.0.1:6379/0")


def test_future_store_hints_point_to_local_scripts(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    assert "infra_up.sh" in pgvector_hint(root)
    assert "infra_up.sh" in redis_hint(root)


def test_create_store_selects_sqlite(tmp_path: Path) -> None:
    project = tmp_path / "repo"
    project.mkdir()
    config = NCPConfig(
        values={
            "store": {"type": "sqlite", "path": str(project / ".ncp" / "store.db")},
            "providers": {"pricing": {}},
        },
        project_root=project,
    )

    store = create_store(config)

    assert isinstance(store, SQLiteStore)


def test_create_store_selects_pgvector(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    captured: dict[str, str] = {}

    class _FakePgvectorStore:
        def __init__(self, dsn: str, *, schema: str, table_prefix: str) -> None:
            captured["dsn"] = dsn
            captured["schema"] = schema
            captured["table_prefix"] = table_prefix

    monkeypatch.setattr("ncp.stores.factory.PgvectorStore", _FakePgvectorStore)
    project = tmp_path / "repo"
    project.mkdir()
    config = NCPConfig(
        values={
            "store": {"type": "pgvector", "path": str(project / ".ncp" / "store.db")},
            "pgvector": {
                "dsn": "postgresql://postgres:postgres@127.0.0.1:5432/ncp",
                "schema": "ncp_test",
                "table_prefix": "demo_",
            },
            "providers": {"pricing": {}},
        },
        project_root=project,
    )

    create_store(config)

    assert captured == {
        "dsn": "postgresql://postgres:postgres@127.0.0.1:5432/ncp",
        "schema": "ncp_test",
        "table_prefix": "demo_",
    }
