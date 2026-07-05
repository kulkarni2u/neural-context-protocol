-- UP
-- S4.1: memoization telemetry (CAP-C3) — per-entry token-savings estimate
-- and a persistent miss counter for `ncp status`.
-- memo_entries is also created here for migration-driven installs that
-- predate the CAP-C3 schema template.
CREATE TABLE IF NOT EXISTS {schema}.{prefix}memo_entries (
    signature TEXT PRIMARY KEY,
    task TEXT NOT NULL,
    result_summary TEXT,
    chunk_ids TEXT NOT NULL DEFAULT '[]',
    outcome DOUBLE PRECISION DEFAULT 0.0,
    verified INTEGER DEFAULT 0,
    created_at DOUBLE PRECISION NOT NULL,
    last_hit_at DOUBLE PRECISION NOT NULL DEFAULT 0.0,
    hit_count INTEGER NOT NULL DEFAULT 0,
    output_tokens_est INTEGER NOT NULL DEFAULT 0
);

ALTER TABLE {schema}.{prefix}memo_entries
    ADD COLUMN IF NOT EXISTS output_tokens_est INTEGER NOT NULL DEFAULT 0;

CREATE TABLE IF NOT EXISTS {schema}.{prefix}memo_stats (
    stat_key TEXT PRIMARY KEY,
    stat_value BIGINT NOT NULL DEFAULT 0
);

-- DOWN
DROP TABLE IF EXISTS {schema}.{prefix}memo_stats;
ALTER TABLE {schema}.{prefix}memo_entries
    DROP COLUMN IF EXISTS output_tokens_est;
