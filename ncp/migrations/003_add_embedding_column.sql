-- UP
ALTER TABLE {schema}.{prefix}chunks ADD COLUMN IF NOT EXISTS embedding vector(1536);

-- DOWN
ALTER TABLE {schema}.{prefix}chunks DROP COLUMN IF EXISTS embedding;
