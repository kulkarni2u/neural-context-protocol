-- UP
ALTER TABLE {schema}.{prefix}chunks ADD COLUMN IF NOT EXISTS dissent_count INTEGER DEFAULT 0;

-- DOWN
ALTER TABLE {schema}.{prefix}chunks DROP COLUMN IF EXISTS dissent_count;
