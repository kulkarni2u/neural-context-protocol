-- UP
-- Typed decision contract (spec 4h): a decision is a first-class durable
-- object, not a reasoning_trace chunk with extra prose. Additive -- existing
-- deployments gain an empty table and lose nothing.
CREATE TABLE IF NOT EXISTS {schema}.{prefix}decisions (
    decision_id TEXT PRIMARY KEY,
    schema_id TEXT NOT NULL,
    schema_version INTEGER NOT NULL DEFAULT 1,
    slot TEXT NOT NULL,
    pipeline_id TEXT,
    agent_id TEXT,
    options TEXT NOT NULL DEFAULT '[]',
    choice TEXT NOT NULL DEFAULT 'null',
    probs TEXT NOT NULL DEFAULT '{}',
    confidence REAL NOT NULL,
    confidence_source TEXT NOT NULL DEFAULT 'self_reported',
    backend TEXT NOT NULL DEFAULT 'unknown',
    state_hash TEXT NOT NULL DEFAULT '',
    chunk_ids TEXT NOT NULL DEFAULT '[]',
    turn_id TEXT,
    outcome_id TEXT,
    rationale TEXT,
    created_at REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS {prefix}decisions_schema_slot_idx
    ON {schema}.{prefix}decisions (schema_id, slot, created_at DESC);
CREATE INDEX IF NOT EXISTS {prefix}decisions_state_hash_idx
    ON {schema}.{prefix}decisions (state_hash);
CREATE INDEX IF NOT EXISTS {prefix}decisions_pipeline_idx
    ON {schema}.{prefix}decisions (pipeline_id, created_at DESC);

-- DOWN
DROP TABLE IF EXISTS {schema}.{prefix}decisions;
