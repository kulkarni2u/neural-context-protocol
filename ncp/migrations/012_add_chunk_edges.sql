-- UP
-- Graph engineering: typed, directed edges between chunks (caused_by,
-- supersedes, supports, contradicts, refines, derived_from). The legacy
-- caused_by/supersedes scalar columns on chunks stay authoritative; this
-- table is an additive, queryable mirror that enables multi-hop traversal.
CREATE TABLE IF NOT EXISTS {schema}.{prefix}chunk_edges (
    edge_id TEXT PRIMARY KEY,
    src_chunk_id TEXT NOT NULL,
    dst_chunk_id TEXT NOT NULL,
    edge_type TEXT NOT NULL,
    weight DOUBLE PRECISION NOT NULL DEFAULT 1.0,
    created_at DOUBLE PRECISION NOT NULL,
    created_by TEXT,
    UNIQUE (src_chunk_id, dst_chunk_id, edge_type)
);

CREATE INDEX IF NOT EXISTS {prefix}idx_chunk_edges_src
    ON {schema}.{prefix}chunk_edges(src_chunk_id);
CREATE INDEX IF NOT EXISTS {prefix}idx_chunk_edges_dst
    ON {schema}.{prefix}chunk_edges(dst_chunk_id);

-- DOWN
DROP INDEX IF EXISTS {schema}.{prefix}idx_chunk_edges_dst;
DROP INDEX IF EXISTS {schema}.{prefix}idx_chunk_edges_src;
DROP TABLE IF EXISTS {schema}.{prefix}chunk_edges;
