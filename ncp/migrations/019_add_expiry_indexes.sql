-- UP
-- Soft GC deletes expired rows from chunks, tombstones, whispers and
-- turn_records on every write. None of those tables had an index leading on
-- its expiry column (whispers only had (target, expires_at) and
-- (pipeline_id, expires_at)), so each DELETE was a sequential scan. Additive:
-- indexes only, no data change.
CREATE INDEX IF NOT EXISTS {prefix}idx_chunks_expiry
    ON {schema}.{prefix}chunks (expiry)
    WHERE expiry IS NOT NULL;
CREATE INDEX IF NOT EXISTS {prefix}idx_turns_expires
    ON {schema}.{prefix}turn_records (expires_at);
CREATE INDEX IF NOT EXISTS {prefix}idx_tombstones_expires
    ON {schema}.{prefix}tombstones (expires_at);
CREATE INDEX IF NOT EXISTS {prefix}idx_whispers_expires
    ON {schema}.{prefix}whispers (expires_at);

-- DOWN
DROP INDEX IF EXISTS {schema}.{prefix}idx_whispers_expires;
DROP INDEX IF EXISTS {schema}.{prefix}idx_tombstones_expires;
DROP INDEX IF EXISTS {schema}.{prefix}idx_turns_expires;
DROP INDEX IF EXISTS {schema}.{prefix}idx_chunks_expiry;
