-- UP
-- CAP-T3: outcome-calibrated reputation evidence table.
CREATE TABLE IF NOT EXISTS {schema}.{prefix}outcomes (
    outcome_id TEXT PRIMARY KEY,
    turn_id TEXT,
    chunk_ids TEXT NOT NULL DEFAULT '[]',
    success INTEGER NOT NULL,
    weight REAL NOT NULL DEFAULT 1.0,
    note TEXT,
    created_at REAL NOT NULL,
    consumed INTEGER NOT NULL DEFAULT 0
);

-- DOWN
DROP TABLE IF EXISTS {schema}.{prefix}outcomes;
