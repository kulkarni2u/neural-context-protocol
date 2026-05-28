"""Store selection helpers."""

from __future__ import annotations

from ncp.config import NCPConfig
from ncp.stores.base import BaseStore
from ncp.stores.pgvector import PgvectorStore
from ncp.stores.sqlite import SQLiteStore


def create_store(config: NCPConfig) -> BaseStore:
    """Create the configured NCP store implementation."""

    if config.store_type == "sqlite":
        return SQLiteStore(config.store_path, config=config)
    if config.store_type == "pgvector":
        return PgvectorStore(
            config.pgvector_dsn,
            schema=config.pgvector_schema,
            table_prefix=config.pgvector_table_prefix,
            redis_url=config.redis_url,
            redis_stream=config.redis_stream,
            config=config,
        )
    raise NotImplementedError(
        f"Store type '{config.store_type}' is not implemented yet."
    )
