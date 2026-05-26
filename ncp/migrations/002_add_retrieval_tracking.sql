-- UP
ALTER TABLE {schema}.{prefix}chunks ADD COLUMN IF NOT EXISTS retrieval_count INTEGER DEFAULT 0;
ALTER TABLE {schema}.{prefix}chunks ADD COLUMN IF NOT EXISTS last_retrieved_at DOUBLE PRECISION;

-- DOWN
ALTER TABLE {schema}.{prefix}chunks DROP COLUMN IF EXISTS retrieval_count;
ALTER TABLE {schema}.{prefix}chunks DROP COLUMN IF EXISTS last_retrieved_at;
