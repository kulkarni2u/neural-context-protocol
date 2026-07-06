-- UP
-- CAP-C5: bi-temporal memory. valid_from/valid_to bound when a fact was
-- true in the *world* (valid time, epoch seconds); superseded_by records
-- the chunk_id that honestly replaced this one. The superseded row is
-- never deleted, only marked -- history is preserved, not rewritten.
ALTER TABLE {schema}.{prefix}chunks ADD COLUMN IF NOT EXISTS valid_from DOUBLE PRECISION;
ALTER TABLE {schema}.{prefix}chunks ADD COLUMN IF NOT EXISTS valid_to DOUBLE PRECISION;
ALTER TABLE {schema}.{prefix}chunks ADD COLUMN IF NOT EXISTS superseded_by TEXT;

-- DOWN
ALTER TABLE {schema}.{prefix}chunks DROP COLUMN IF EXISTS superseded_by;
ALTER TABLE {schema}.{prefix}chunks DROP COLUMN IF EXISTS valid_to;
ALTER TABLE {schema}.{prefix}chunks DROP COLUMN IF EXISTS valid_from;
