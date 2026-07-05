-- UP
ALTER TABLE {schema}.{prefix}whispers ADD COLUMN IF NOT EXISTS dissent_target TEXT;

-- DOWN
ALTER TABLE {schema}.{prefix}whispers DROP COLUMN IF EXISTS dissent_target;
