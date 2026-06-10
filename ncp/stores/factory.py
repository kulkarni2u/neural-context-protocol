"""Store selection helpers."""

from __future__ import annotations

from ncp.config import NCPConfig
from ncp.stores.base import BaseStore
from ncp.stores.pgvector import PgvectorStore
from ncp.stores.sqlite import SQLiteStore


def _build_embedding_adapter(cfg: NCPConfig) -> object | None:
    if not cfg.embedding_enabled:
        return None
    from ncp.adapters.embedding import LocalEmbeddingAdapter, OpenAIEmbeddingAdapter
    if cfg.embedding_provider == "openai":
        return OpenAIEmbeddingAdapter(model=cfg.embedding_model)
    return LocalEmbeddingAdapter(model=cfg.embedding_model)


def create_store(config: NCPConfig) -> BaseStore:
    """Create the configured NCP store implementation."""

    if config.store_type == "sqlite":
        return SQLiteStore(
            config.store_path,
            config=config,
            max_working_chunks_per_pipeline=config.retention_max_working_chunks_per_pipeline,
        )
    if config.store_type == "pgvector":
        return PgvectorStore(
            config.pgvector_dsn,
            schema=config.pgvector_schema,
            table_prefix=config.pgvector_table_prefix,
            redis_url=config.redis_url,
            redis_stream=config.redis_stream,
            config=config,
            embedding_adapter=_build_embedding_adapter(config),
            max_working_chunks_per_pipeline=config.retention_max_working_chunks_per_pipeline,
        )
    raise NotImplementedError(
        f"Store type '{config.store_type}' is not implemented yet."
    )
