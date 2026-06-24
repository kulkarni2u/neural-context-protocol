-- UP
CREATE TABLE IF NOT EXISTS {schema}.{prefix}identities (
    identity_id TEXT PRIMARY KEY,
    public_key TEXT NOT NULL,
    alg TEXT NOT NULL DEFAULT 'ed25519',
    label TEXT,
    created_at DOUBLE PRECISION NOT NULL,
    revoked_at DOUBLE PRECISION
);

CREATE TABLE IF NOT EXISTS {schema}.{prefix}reputation (
    identity_id TEXT PRIMARY KEY,
    alpha DOUBLE PRECISION NOT NULL DEFAULT 1.0,
    beta DOUBLE PRECISION NOT NULL DEFAULT 1.0,
    obs_count INTEGER NOT NULL DEFAULT 0,
    last_updated DOUBLE PRECISION NOT NULL DEFAULT 0.0
);

CREATE INDEX IF NOT EXISTS {prefix}idx_reputation_updated
    ON {schema}.{prefix}reputation(last_updated);

-- DOWN
DROP INDEX IF EXISTS {schema}.{prefix}idx_reputation_updated;
DROP TABLE IF EXISTS {schema}.{prefix}reputation;
DROP TABLE IF EXISTS {schema}.{prefix}identities;
