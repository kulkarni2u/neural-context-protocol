-- UP
-- Turn-to-chunk associations. Turn-based outcomes (ncp_record_outcome with a
-- turn_id) resolve to "the chunks that turn wrote + retrieved"; nothing used
-- to persist that mapping, so such outcomes resolved to no chunks. relation is
-- 'wrote' (chunk persisted by the turn) or 'retrieved' (chunk served as
-- context to the turn). The primary key also indexes lookups by turn_id.
CREATE TABLE IF NOT EXISTS {schema}.{prefix}turn_chunks (
    turn_id TEXT NOT NULL,
    chunk_id TEXT NOT NULL,
    relation TEXT NOT NULL,
    created_at DOUBLE PRECISION NOT NULL,
    PRIMARY KEY (turn_id, chunk_id, relation)
);

-- DOWN
DROP TABLE IF EXISTS {schema}.{prefix}turn_chunks;
