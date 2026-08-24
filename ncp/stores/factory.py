"""Store selection helpers."""

from __future__ import annotations

import logging

from ncp.config import NCPConfig
from ncp.stores.base import BaseStore
from ncp.stores.pgvector import PgvectorStore
from ncp.stores.sqlite import SQLiteStore

logger = logging.getLogger("ncp")


def _build_embedding_adapter(cfg: NCPConfig) -> object | None:
    """Construct the configured embedding adapter, or None.

    CAP-C4: [embedding].enabled defaults to true, so this runs on every
    default `create_store()` call. Adapter construction can legitimately
    fail for reasons outside anyone's control at deploy time -- the
    optional `fastembed`/`openai` package isn't installed, OPENAI_API_KEY
    isn't set, an unsupported provider string was configured, etc. None of
    those are programmer errors, so none of them should ever crash server
    startup: catch any exception construction raises, log one warning
    naming the fix, and return None so the caller falls back to exactly the
    lexical-only retrieval it used when embeddings were off.

    Note what this does NOT cover any more: `LocalEmbeddingAdapter`
    construction here is now cheap (import + attribute assignment only) --
    the actual `TextEmbedding(...)` model load, which downloads ~130MB from
    Hugging Face on first-ever use and can block or hang on a slow/offline/
    firewalled network, is deferred to that adapter's first `embed()` call
    (see `ncp/adapters/embedding.py::LocalEmbeddingAdapter`). A slow or
    failing download therefore can no longer block `create_store()` /
    server startup at all; instead it surfaces as a failure on the first
    real write or query, which each store's `_try_embed()` helper
    (`ncp/stores/sqlite.py`, `ncp/stores/pgvector.py`,
    `ncp/stores/pgvector_async.py`) catches and degrades from in the same
    lexical-only-fallback spirit as this function, except triggered lazily
    instead of at startup.

    An unrecognized `embedding_provider` value, by contrast, IS a
    config/programmer error -- it can never be fixed by installing
    something or waiting for the network -- so that branch still raises
    ValueError rather than degrading silently.
    """
    if not cfg.embedding_enabled:
        return None
    from ncp.adapters.embedding import LocalEmbeddingAdapter, OpenAIEmbeddingAdapter

    if cfg.embedding_provider == "openai":
        try:
            return OpenAIEmbeddingAdapter(model=cfg.embedding_model)
        except Exception as exc:
            logger.warning(
                "Semantic retrieval disabled: could not construct the OpenAI "
                "embedding adapter (%s: %s). Falling back to lexical-only "
                "retrieval. Set OPENAI_API_KEY and install "
                "'neural-context-protocol[providers]', or set "
                "[embedding].enabled = false to silence this warning.",
                type(exc).__name__,
                exc,
            )
            return None
    if cfg.embedding_provider == "local":
        try:
            return LocalEmbeddingAdapter(model=cfg.embedding_model)
        except Exception as exc:
            logger.warning(
                "Semantic retrieval disabled: could not construct the local "
                "fastembed embedding adapter (%s: %s). Falling back to "
                "lexical-only retrieval. Install the optional dependency "
                "with: pip install 'neural-context-protocol[local-embeddings]'"
                ", or set [embedding].enabled = false to silence this "
                "warning.",
                type(exc).__name__,
                exc,
            )
            return None
    raise ValueError(
        "Unsupported embedding provider "
        f"{cfg.embedding_provider!r}; expected 'local' or 'openai'"
    )


def create_store(config: NCPConfig) -> BaseStore:
    """Create the configured NCP store implementation."""

    if config.store_type == "sqlite":
        return SQLiteStore(
            config.store_path,
            config=config,
            max_working_chunks_per_pipeline=config.retention_max_working_chunks_per_pipeline,
            embedding_adapter=_build_embedding_adapter(config),
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
