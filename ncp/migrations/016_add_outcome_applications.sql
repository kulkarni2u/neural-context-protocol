-- UP
-- Per-(outcome, chunk) consumption tracking so calibrating one pipeline does
-- not consume outcome feedback that belongs to chunks in another pipeline.
CREATE TABLE IF NOT EXISTS {schema}.{prefix}outcome_applications (
    outcome_id TEXT NOT NULL,
    chunk_id TEXT NOT NULL,
    applied_at DOUBLE PRECISION NOT NULL,
    PRIMARY KEY (outcome_id, chunk_id)
);

-- DOWN
DROP TABLE IF EXISTS {schema}.{prefix}outcome_applications;
