-- UP
-- CAP-T1 / WI-013: persist opt-in Ed25519 authorship verification status.
ALTER TABLE {schema}.{prefix}chunks ADD COLUMN IF NOT EXISTS verified BOOLEAN DEFAULT FALSE;
ALTER TABLE {schema}.{prefix}whispers ADD COLUMN IF NOT EXISTS verified BOOLEAN DEFAULT FALSE;

-- DOWN
ALTER TABLE {schema}.{prefix}whispers DROP COLUMN IF EXISTS verified;
ALTER TABLE {schema}.{prefix}chunks DROP COLUMN IF EXISTS verified;
