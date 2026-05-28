# IVF-FLAT Index Migration — Design Spec (migration 004)

**Date:** 2026-05-28
**Slice:** NCP 0.6.x — IVF-FLAT index migration
**Status:** Approved

---

## Goal

Add an IVF-FLAT ANN index on the `embedding vector(1536)` column so that
`retrieval_mode="vector"` queries scale beyond a full brute-force scan.
Also wire `ivfflat.probes` into `PgvectorStore` so recall is configurable
without changing query call sites.

---

## Migration file — `004_add_ivfflat_index.sql`

```sql
-- UP
CREATE INDEX IF NOT EXISTS {prefix}idx_chunks_embedding
    ON {schema}.{prefix}chunks
    USING ivfflat (embedding vector_cosine_ops)
    WITH (lists = 100);

-- DOWN
DROP INDEX IF EXISTS {schema}.{prefix}idx_chunks_embedding;
```

**Operator class:** `vector_cosine_ops` matches the `<=>` cosine distance
operator already used in `_query_vector`.

**`lists = 100`:** pgvector's recommended starting point for tables up to
~10K rows (`sqrt(10000) = 100`). Can be retuned later via `REINDEX` without
a new migration.

**Naming:** follows the existing `{prefix}idx_chunks_*` convention from
migration 001.

**Empty-table behaviour:** IVF-FLAT creates cleanly on an empty table (0
centroids). Postgres updates centroid coverage as rows are inserted; no manual
rebuild required for normal insert workloads.

**No `CONCURRENTLY`:** the migration runner executes inside a transaction
context; `CREATE INDEX CONCURRENTLY` would fail there.

---

## `PgvectorStore` changes

### Constructor

Add `ivfflat_probes: int = 10` keyword argument. Store as `self._ivfflat_probes`.

```python
class PgvectorStore(BaseStore):
    def __init__(
        self,
        dsn: str,
        *,
        ivfflat_probes: int = 10,
        ...
    ) -> None:
        self._ivfflat_probes = ivfflat_probes
        ...
```

**Default `probes = 10`:** scans 10% of centroids with `lists = 100`. Good
recall for NCP's small-to-medium datasets (hundreds–thousands of chunks).
Postgres default is 1, which gives 1% coverage — too low for context retrieval
where recall matters.

### `_query_vector`

Prepend a `SET LOCAL` call before the SELECT:

```python
cur.execute("SET LOCAL ivfflat.probes = %s", (self._ivfflat_probes,))
# existing SELECT with <=> operator ...
```

`SET LOCAL` scopes the variable to the current transaction, so it cannot leak
across pool connections.

Only `_query_vector` is affected. The hybrid and trust-recency paths do not
use the index.

---

## Tests

All new tests go in `tests/test_embedding_ann.py`, following the existing
migration-003 test pattern.

### Migration file check

```python
def test_migration_004_exists() -> None:
    migration = Path(...) / "004_add_ivfflat_index.sql"
    assert migration.exists()
    content = migration.read_text()
    assert "-- UP" in content
    assert "-- DOWN" in content
    assert "ivfflat" in content
    assert "vector_cosine_ops" in content
    assert "lists" in content
```

### Probes wired into `_query_vector`

- Default store (`ivfflat_probes=10`): assert `SET LOCAL ivfflat.probes = 10`
  appears in mock cursor calls before the SELECT.
- Custom value (`ivfflat_probes=5`): assert the mock sees `5`.

Both use mock cursor (no real DB required).

---

## Out of scope

- Changing `lists` after the fact (use `REINDEX`, not a new migration)
- HNSW alternative
- `probes` on hybrid / trust-recency paths (those don't use the index)
- Embedding provider integration (next slice)
