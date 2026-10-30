-- UP
-- Durable loser -> keeper aliases written by equivalent consolidation merges,
-- so outcome feedback recorded against a merged-away chunk credits the
-- surviving keeper instead of being settled with no effect. Only the
-- 'equivalent_consolidation' reason is ever read; supersession and
-- contradiction never create aliases.
CREATE TABLE IF NOT EXISTS {schema}.{prefix}chunk_aliases (
    alias_id TEXT PRIMARY KEY,
    target_id TEXT NOT NULL,
    reason TEXT NOT NULL,
    pipeline_id TEXT,
    created_at DOUBLE PRECISION NOT NULL
);

-- DOWN
DROP TABLE IF EXISTS {schema}.{prefix}chunk_aliases;
