"""Future pgvector-backed durable store helpers for NCP R2."""

from __future__ import annotations

from pathlib import Path


class PgvectorStore:
    """Placeholder for the R2 pgvector store.

    pgvector is the intended durable production backend once NCP moves beyond the
    SQLite-first alpha line.
    """

    def __init__(
        self,
        dsn: str,
        *,
        schema: str = "ncp",
        table_prefix: str = "ncp_",
    ) -> None:
        self.dsn = dsn
        self.schema = schema
        self.table_prefix = table_prefix
        raise NotImplementedError(
            "PgvectorStore is planned for NCP 0.2.0. Use compose.yaml plus scripts/infra_up.sh "
            "to start local Postgres/pgvector, but keep store.type=sqlite until the R2 backend "
            "implementation lands."
        )


def infra_hint(project_root: str | Path) -> str:
    root = Path(project_root)
    return (
        f"Start local Postgres/pgvector with {root / 'scripts' / 'infra_up.sh'} and set "
        "NCP_PGVECTOR_DSN when the pgvector backend lands."
    )
